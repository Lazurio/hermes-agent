"""``lazurio_web`` adapter: the web bubble's channel to Buddy (the Hermes Agent in the owner's
Osobní Prostředí).

Inbound, every request is the owner's direct message: ``chat_id = user_id = "owner"``, ``dm``, so the
gateway keys the conversation ``agent:main:lazurio_web:dm:owner`` and authorizes it through
``LAZURIO_WEB_ALLOWED_USERS`` like any other platform. Outbound, every gateway call becomes one event
in the durable outbox (``outbox.py``), and a send succeeds only once that event is committed:

=========================================  =====================================================
gateway call                                event
=========================================  =====================================================
processing starts                           ``turn.started {message_id}``, ``status {working}``
``send`` (``expect_edits``: stream start)   ``message.delta {message_id, text}``
``edit_message(finalize=False)``            ``message.delta`` (the whole text so far, no cursor)
``send`` / ``edit_message(finalize=True)``  ``message.final {message_id, text, media: []}``
``send_image_file`` / ``send_document`` …   ``message.final`` with ``media: [{id, kind, …}]``
``set_status_text`` (live tool status)      ``progress {text}`` (``""`` clears it)
``send_clarify``                            ``clarify.request {clarify_id, question, choices, multi}``
clarify answered, expired or retired        ``clarify.retired {clarify_id}``
processing ends                             ``error {code}`` on failure, ``status {idle}``
=========================================  =====================================================

``message.delta`` carries the full text so far rather than an increment: the stream consumer edits
with whole snapshots, and a replayed or dropped frame then never corrupts what the client shows.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import mimetypes
import os
import shutil
import sqlite3
import stat
import threading
import time
import uuid
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from gateway.config import DEFAULT_STREAMING_CURSOR, Platform, PlatformConfig
from gateway.platforms._shared import coerce_port, extra_or_secret
from gateway.platforms.base import BasePlatformAdapter, SendResult, cache_media_bytes_async
from gateway.platforms.event import MessageEvent, MessageType, ProcessingOutcome

from . import clarify, history, page_context
from .errors import ApiError
from .outbox import EventHub, InboundClaim, MediaRecord, Outbox

if TYPE_CHECKING:
    from .http_api import InboundMessage

logger = logging.getLogger(__name__)

PLATFORM = page_context.PLATFORM
OWNER = "owner"
CHAT_NAME = "Lazurio web"
LISTEN_HOST = "127.0.0.1"
MIN_SECRET_BYTES = 32
# Every event stream ends by then, so each reconnect passes the gateway route (and its session
# check) again; the contract caps a stream at 110 s.
STREAM_MAX_SECONDS = 100.0
MAX_MEDIA_BYTES = 50 * 1024 * 1024
EVENT_RETENTION_SECONDS = 3 * 24 * 3600
INBOUND_RETENTION_SECONDS = 7 * 24 * 3600
MEDIA_RETENTION_SECONDS = 7 * 24 * 3600
PRUNE_INTERVAL_SECONDS = 3600.0
CLARIFY_POLL_SECONDS = 0.5
_RETIRED_MEMORY = 256
_FINALS_MEMORY = 256
_DISPATCHED_MEMORY = 1024


class SecretFileError(ValueError):
    pass


def read_secret_file(path: str) -> bytes:
    """The bearer secret from ``path``: a regular file of the gateway's user, mode 0600 or stricter,
    at least ``MIN_SECRET_BYTES`` of visible ASCII. Anything else refuses to start the channel."""
    if not path:
        raise SecretFileError("no secret file configured (extra.secret_file or LAZURIO_WEB_SECRET_FILE)")
    target = Path(path).expanduser()
    try:
        info = target.lstat()
    except FileNotFoundError as exc:
        raise SecretFileError(f"secret file {target} does not exist") from exc
    if not stat.S_ISREG(info.st_mode):
        raise SecretFileError(f"secret file {target} is not a regular file")
    if info.st_mode & 0o077:
        raise SecretFileError(f"secret file {target} must be readable by its owner only (chmod 600)")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise SecretFileError(f"secret file {target} must belong to the gateway's user")
    secret = target.read_bytes().strip()
    if len(secret) < MIN_SECRET_BYTES or not all(33 <= byte <= 126 for byte in secret):
        raise SecretFileError(f"secret file {target} must hold at least {MIN_SECRET_BYTES} visible ASCII characters")
    return secret


def _media_message_type(kinds: list[str]) -> MessageType:
    if not kinds:
        return MessageType.TEXT
    return MessageType.PHOTO if all(kind == "image" for kind in kinds) else MessageType.DOCUMENT


class LazurioWebAdapter(BasePlatformAdapter):
    MAX_MESSAGE_LENGTH = 32_000
    # The finalize edit is the only signal that a streamed message is complete (``message.final``),
    # so the consumer must send it even when the text did not change since the last delta.
    REQUIRES_EDIT_FINALIZE = True
    supports_code_blocks = True
    # The live tool status (``set_status_text``) feeds the bubble's one-line progress indicator.
    supports_status_text = True

    def __init__(self, config: PlatformConfig):
        super().__init__(config=config, platform=Platform(PLATFORM))
        extra = config.extra or {}
        self.listen_port = coerce_port(extra.get("listen_port"), 0)
        self._secret_file = str(extra_or_secret(extra, "secret_file", "LAZURIO_WEB_SECRET_FILE") or "")
        self.secret = b""
        self.outbox: Optional[Outbox] = None
        self.hub = EventHub()
        self.stream_max_seconds = STREAM_MAX_SECONDS
        self._web_runner: Any = None
        self._media_dir: Optional[Path] = None
        self._state = "idle"
        self._progress_lock = threading.Lock()
        self._last_progress = ""
        self._clarify_watchers: dict[str, asyncio.Task] = {}
        self._retired: OrderedDict[str, None] = OrderedDict()
        self._finals: OrderedDict[str, str] = OrderedDict()
        self._intake_lock = asyncio.Lock()
        self._dispatched: OrderedDict[str, None] = OrderedDict()  # message ids this process dispatched
        self._prune_task: Optional[asyncio.Task] = None

    # -- lifecycle ------------------------------------------------------------------------------

    def _fatal(self, code: str, message: str, *, retryable: bool) -> bool:
        logger.error("[%s] %s", self.name, message)
        self._set_fatal_error(code, message, retryable=retryable)
        return False

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        try:
            from aiohttp import web
            from .http_api import build_app
        except ImportError:
            return self._fatal("lazurio_web_deps", "aiohttp is not installed (messaging extra)", retryable=False)
        if not 0 < self.listen_port < 65536:
            return self._fatal("lazurio_web_config", "extra.listen_port must be a TCP port", retryable=False)
        try:
            self.secret = await asyncio.to_thread(read_secret_file, self._secret_file)
        except (SecretFileError, OSError) as exc:
            return self._fatal("lazurio_web_secret", str(exc), retryable=False)
        from hermes_constants import get_hermes_home

        home = get_hermes_home() / "lazurio_web"
        try:
            self.outbox = await asyncio.to_thread(Outbox, home / "outbox.db")
            self._media_dir = home / "media"
            self._media_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        except (OSError, sqlite3.Error) as exc:
            self._close_outbox()
            return self._fatal("lazurio_web_outbox", f"cannot open the outbox in {home}: {exc}", retryable=True)
        self.hub.bind(asyncio.get_running_loop())
        await self._prune()
        await self._settle_state()
        runner = web.AppRunner(build_app(self), access_log=None)
        await runner.setup()
        try:
            await web.TCPSite(runner, LISTEN_HOST, self.listen_port).start()
        except OSError as exc:
            await runner.cleanup()
            self._close_outbox()
            return self._fatal("lazurio_web_bind", f"cannot listen on {LISTEN_HOST}:{self.listen_port}: {exc}",
                               retryable=True)
        self._web_runner = runner
        self._prune_task = asyncio.create_task(self._prune_loop())
        self._mark_connected()
        self._wire_plugin_handlers(None)
        logger.info("[%s] Listening on %s:%d", self.name, LISTEN_HOST, self.listen_port)
        return True

    async def disconnect(self) -> None:
        self._mark_disconnected()
        self.hub.close()
        tasks = [task for task in (self._prune_task, *self._clarify_watchers.values()) if task]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._prune_task = None
        if self._web_runner is not None:
            await self._web_runner.cleanup()
            self._web_runner = None
        self._close_outbox()
        logger.info("[%s] Disconnected", self.name)

    def _close_outbox(self) -> None:
        outbox, self.outbox = self.outbox, None
        if outbox is not None:
            outbox.close()

    async def _prune(self) -> None:
        outbox = self.outbox
        if outbox is None:
            return
        now = time.time()
        stale = await asyncio.to_thread(
            outbox.prune, events_before=now - EVENT_RETENTION_SECONDS,
            inbound_before=now - INBOUND_RETENTION_SECONDS, media_before=now - MEDIA_RETENTION_SECONDS)
        for path in stale:
            with contextlib.suppress(OSError):
                os.unlink(path)

    async def _prune_loop(self) -> None:
        while True:
            await asyncio.sleep(PRUNE_INTERVAL_SECONDS)
            try:
                await self._prune()
            except Exception:
                logger.warning("[%s] outbox pruning failed", self.name, exc_info=True)

    async def _settle_state(self) -> None:
        """A turn that was running when the gateway stopped never reported its end."""
        last = await asyncio.to_thread(self.outbox.last_of_type, "status")
        self._state = "idle"
        if last is not None and last.data.get("state") == "working":
            await self._emit("status", {"state": "idle"})

    # -- outbox ---------------------------------------------------------------------------------

    def _emit_now(self, event_type: str, data: dict[str, Any]) -> int:
        """Commit one event and wake the streams; blocking, callable from any thread."""
        outbox = self.outbox
        if outbox is None:
            raise RuntimeError("lazurio_web is not connected")
        event_id = outbox.append(event_type, data)
        self.hub.notify()
        return event_id

    async def _emit(self, event_type: str, data: dict[str, Any]) -> int:
        return await asyncio.to_thread(self._emit_now, event_type, data)

    async def _deliver(self, event_type: str, data: dict[str, Any], message_id: str) -> SendResult:
        """A send succeeds only after its event is durable. A failed commit is retryable: for a final
        text reply the base retry and the gateway's delivery ledger try again instead of counting it
        as delivered. Files are outside that ledger (core brackets final text only); a failed file
        event surfaces as the gateway's media-failure notice."""
        try:
            await self._emit(event_type, data)
        except (sqlite3.Error, OSError, RuntimeError) as exc:
            logger.error("[%s] outbox write failed for %s: %s", self.name, event_type, exc)
            return SendResult(success=False, error=f"lazurio_web outbox write failed: {exc}", retryable=True)
        return SendResult(success=True, message_id=message_id)

    async def _set_state(self, state: str) -> None:
        if state != self._state:
            self._state = state
            await self._emit("status", {"state": state})

    # -- outbound -------------------------------------------------------------------------------

    def _strip_cursor(self, text: str) -> str:
        streaming = getattr(getattr(self.gateway_runner, "config", None), "streaming", None)
        cursor = getattr(streaming, "cursor", None) if streaming is not None else DEFAULT_STREAMING_CURSOR
        return text[: -len(cursor)] if cursor and text.endswith(cursor) else text

    def _message_event(self, message_id: str, text: str, *, final: bool) -> tuple[str, dict[str, Any]]:
        if final:
            return "message.final", {"message_id": message_id, "text": text, "media": []}
        return "message.delta", {"message_id": message_id, "text": self._strip_cursor(text)}

    async def _send_message_event(self, message_id: str, text: str, *, final: bool) -> SendResult:
        # REQUIRES_EDIT_FINALIZE makes the consumer finalize even an unchanged message, and at the
        # end of a turn it may do so twice: one ``message.final`` per (message, text) is enough.
        if final and self._finals.get(message_id) == text:
            return SendResult(success=True, message_id=message_id)
        event_type, data = self._message_event(message_id, text, final=final)
        result = await self._deliver(event_type, data, message_id)
        if final and result.success:
            self._finals[message_id] = text
            while len(self._finals) > _FINALS_MEMORY:
                self._finals.popitem(last=False)
        return result

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[dict[str, Any]] = None) -> SendResult:
        final = not (metadata or {}).get("expect_edits")
        return await self._send_message_event(uuid.uuid4().hex, content or "", final=final)

    async def edit_message(self, chat_id: str, message_id: str, content: str, *, finalize: bool = False,
                           metadata: Optional[dict[str, Any]] = None) -> SendResult:
        return await self._send_message_event(str(message_id), content or "", final=finalize)

    def set_status_text(self, chat_id: str, text: Optional[str]) -> None:
        """Runs on the agent's worker thread (``TurnRunner._progress_live_status``)."""
        super().set_status_text(chat_id, text)
        value = (text or "").strip()
        with self._progress_lock:
            if value == self._last_progress:
                return
            self._last_progress = value
        try:
            self._emit_now("progress", {"text": value})
        except Exception:
            logger.debug("[%s] progress event dropped", self.name, exc_info=True)

    async def send_clarify(self, chat_id: str, question: str, choices: Optional[list], clarify_id: str,
                           session_key: str, metadata: Optional[dict[str, Any]] = None) -> SendResult:
        entry = clarify.open_entry(clarify_id)
        data = {"clarify_id": clarify_id, "question": clarify.visible_question(question),
                "choices": [str(choice) for choice in choices or []],
                "multi": bool(getattr(entry, "multi_select", False))}
        result = await self._deliver("clarify.request", data, clarify_id)
        if result.success:
            self._watch_clarify(clarify_id)
        return result

    async def retire_clarify_card(self, clarify_id: str, notice: str) -> None:
        await self._retire(clarify_id)

    def _watch_clarify(self, clarify_id: str) -> None:
        """Retire the card however the question ends: answered here, answered by typing into the
        composer (the gateway's text intercept), timed out, or cleared by ``/stop`` or ``/new``."""
        async def watch() -> None:
            try:
                while clarify.open_entry(clarify_id) is not None:
                    await asyncio.sleep(CLARIFY_POLL_SECONDS)
                await self._retire(clarify_id)
            finally:
                self._clarify_watchers.pop(clarify_id, None)

        self._clarify_watchers[clarify_id] = asyncio.create_task(watch())

    async def _retire(self, clarify_id: str) -> None:
        if clarify_id in self._retired:
            return
        self._retired[clarify_id] = None
        while len(self._retired) > _RETIRED_MEMORY:
            self._retired.popitem(last=False)
        try:
            await self._emit("clarify.retired", {"clarify_id": clarify_id})
        except Exception:
            logger.warning("[%s] could not record clarify.retired for %s", self.name, clarify_id, exc_info=True)

    def _store_media(self, path: str, file_name: Optional[str]) -> MediaRecord:
        source = Path(path)
        info = source.stat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("not a regular file")
        if info.st_size > MAX_MEDIA_BYTES:
            raise ValueError(f"file is larger than {MAX_MEDIA_BYTES} bytes")
        name = Path(file_name or source.name).name or "file"
        media_id = uuid.uuid4().hex
        target = self._media_dir / f"{media_id}{source.suffix.lower()[:16]}"
        shutil.copyfile(source, target)
        os.chmod(target, 0o600)
        record = MediaRecord(media_id, name, mimetypes.guess_type(name)[0] or "application/octet-stream",
                             info.st_size, str(target))
        self.outbox.add_media(record)
        return record

    async def _send_file(self, kind: str, path: str, caption: Optional[str],
                         file_name: Optional[str] = None) -> SendResult:
        """A file Buddy sends is copied into the channel's own store first: the original may live
        in a cache Hermes cleans up while the client has not fetched it yet."""
        if self.outbox is None or self._media_dir is None:
            return SendResult(success=False, error="lazurio_web is not connected", retryable=True)
        try:
            record = await asyncio.to_thread(self._store_media, path, file_name)
        except (OSError, ValueError) as exc:
            logger.warning("[%s] cannot hand %s to the web client: %s", self.name, kind, exc)
            return SendResult(success=False, error=str(exc))
        message_id = uuid.uuid4().hex
        media = [{"id": record.id, "kind": kind, "name": record.name, "mime": record.mime, "size": record.size}]
        return await self._deliver(
            "message.final", {"message_id": message_id, "text": caption or "", "media": media}, message_id)

    async def send_image_file(self, chat_id: str, image_path: str, caption: Optional[str] = None,
                              reply_to: Optional[str] = None, metadata: Optional[dict[str, Any]] = None,
                              **kwargs) -> SendResult:
        return await self._send_file("image", image_path, caption)

    async def send_document(self, chat_id: str, file_path: str, caption: Optional[str] = None,
                            file_name: Optional[str] = None, reply_to: Optional[str] = None,
                            metadata: Optional[dict[str, Any]] = None, **kwargs) -> SendResult:
        return await self._send_file("file", file_path, caption, file_name)

    async def send_voice(self, chat_id: str, audio_path: str, caption: Optional[str] = None,
                         reply_to: Optional[str] = None, metadata: Optional[dict[str, Any]] = None,
                         **kwargs) -> SendResult:
        return await self._send_file("audio", audio_path, caption)

    async def send_video(self, chat_id: str, video_path: str, caption: Optional[str] = None,
                         reply_to: Optional[str] = None, metadata: Optional[dict[str, Any]] = None,
                         **kwargs) -> SendResult:
        return await self._send_file("video", video_path, caption)

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {"name": CHAT_NAME, "type": "dm", "chat_id": chat_id}

    # -- processing lifecycle -------------------------------------------------------------------

    async def on_processing_start(self, event: MessageEvent) -> None:
        await self._emit("turn.started", {"message_id": str(event.message_id or "")})
        await self._set_state("working")

    async def on_processing_complete(self, event: MessageEvent, outcome: ProcessingOutcome) -> None:
        with self._progress_lock:
            self._last_progress = ""
        if outcome == ProcessingOutcome.FAILURE:
            await self._emit("error", {"code": "turn_failed"})
        await self._set_state("idle")

    # -- inbound (called by http_api) -----------------------------------------------------------

    def _owner_source(self, message_id: Optional[str] = None):
        return self.build_source(chat_id=OWNER, chat_name=CHAT_NAME, chat_type="dm", user_id=OWNER,
                                 message_id=message_id)

    def owner_session_key(self) -> str:
        return self._source_session_key(self._owner_source())

    def _require_owner(self) -> None:
        if self._message_handler is None:
            raise ApiError(503, "not_ready", "the gateway is not accepting messages yet")
        if self._is_sender_authorized(OWNER, "dm", OWNER) is False:
            raise ApiError(403, "forbidden_user", "the owner is not allowed: set LAZURIO_WEB_ALLOWED_USERS=owner")

    async def _build_event(self, inbound: InboundMessage, message_id: str) -> MessageEvent:
        media_urls: list[str] = []
        media_types: list[str] = []
        kinds: list[str] = []
        for item in inbound.attachments:
            cached = await cache_media_bytes_async(item.data, filename=Path(item.name).name, mime_type=item.mime)
            if cached is None:
                raise ApiError(400, "invalid_attachment", f"{item.name} is not a valid image")
            media_urls.append(cached.path)
            media_types.append(cached.media_type)
            kinds.append(cached.kind)
        return MessageEvent(
            text=inbound.text, message_type=_media_message_type(kinds), user_id=OWNER,
            source=self._owner_source(message_id), message_id=message_id, media_urls=media_urls,
            media_types=media_types, timestamp=datetime.now(tz=UTC))

    def _lineage_rows(self, session_key: str) -> list[dict[str, Any]]:
        """The conversation as displayed: root to tip across compression rotations, including rows an
        in-place compaction archived (``compacted=1``), never rows Undo/Rewind deactivated."""
        store = getattr(self, "_session_store", None)
        session_id = store.peek_session_id(session_key) if store is not None else None
        db = getattr(store, "_db", None) if store is not None else None
        if not session_id or db is None:
            return []
        return db.get_messages(db.resolve_resume_session_id(session_id), include_ancestors=True,
                               include_compacted=True)

    def _in_transcript(self, session_key: str, message_id: str) -> bool:
        """Whether Hermes persisted the person's message: its user row carries the API's message id
        as ``platform_message_id``."""
        return any(row.get("platform_message_id") == message_id for row in self._lineage_rows(session_key))

    async def _claim_settled(self, claim: InboundClaim) -> bool:
        """A repeated ``client_id`` is answered without dispatch only on evidence: this process
        already handed the message to the gateway (queued or running), or Hermes persisted it. A
        claim left behind by a gateway that died before either happened is dispatched again,
        however old, so a retry recovers the message instead of hitting a dead claim."""
        if claim.message_id in self._dispatched:
            return True
        return await asyncio.to_thread(self._in_transcript, self.owner_session_key(), claim.message_id)

    async def submit_message(self, inbound: InboundMessage) -> str:
        """Intake is serialized: a concurrent retry waits for the first attempt instead of being told
        202 for a claim that attempt may still roll back."""
        self._require_owner()
        async with self._intake_lock:
            claim = await asyncio.to_thread(self.outbox.claim_inbound, inbound.client_id, uuid.uuid4().hex)
            if not claim.created and await self._claim_settled(claim):
                return claim.message_id
            try:
                event = await self._build_event(inbound, claim.message_id)
                block = page_context.render(page_context.validate(inbound.context))
                if block:
                    page_context.STORE.put(claim.message_id, block)
                await self.handle_message(event)
            except BaseException:
                if claim.created:
                    await asyncio.to_thread(self.outbox.forget_inbound, inbound.client_id)
                raise
            self._dispatched[claim.message_id] = None
            while len(self._dispatched) > _DISPATCHED_MEMORY:
                self._dispatched.popitem(last=False)
            return claim.message_id

    async def request_stop(self) -> dict[str, Any]:
        """``/stop`` through the gateway's own command path, so it behaves as typed on Telegram."""
        self._require_owner()
        if self.owner_session_key() not in self._active_sessions:
            return {"stopped": False}
        message_id = uuid.uuid4().hex
        await self.handle_message(MessageEvent(
            text="/stop", message_type=MessageType.COMMAND, user_id=OWNER,
            source=self._owner_source(message_id), message_id=message_id))
        return {"stopped": True}

    async def answer_clarify(self, clarify_id: str, body: dict[str, Any]) -> dict[str, Any]:
        from tools.clarify_gateway import resolve_gateway_clarify

        self._require_owner()
        entry = clarify.open_entry(clarify_id)
        if entry is None or entry.session_key != self.owner_session_key():
            if clarify_id in self._clarify_watchers:
                await self._retire(clarify_id)
            raise ApiError(404, "unknown_clarify", "the question is no longer open")
        value = clarify.answer_value(entry, body)
        if not resolve_gateway_clarify(clarify_id, value):
            raise ApiError(409, "already_answered", "the question was answered or expired meanwhile")
        await self._retire(clarify_id)
        return {"resolved": True}

    def _history_page(self, session_key: str, before: Optional[int], limit: int):
        return history.page(self._lineage_rows(session_key), before, limit)

    async def read_history(self, before: Optional[int], limit: int) -> dict[str, Any]:
        """``last_event_id`` is read BEFORE the transcript: replaying from it may repeat a reply the
        page already holds (the client drops it by text), but never skips one."""
        last_event_id = await asyncio.to_thread(self.outbox.last_id)
        messages, next_before = await asyncio.to_thread(self._history_page, self.owner_session_key(), before, limit)
        return {"messages": messages, "next_before": next_before, "last_event_id": last_event_id}

    def _existing_media(self, media_id: str) -> Optional[MediaRecord]:
        record = self.outbox.get_media(media_id)
        return record if record is not None and os.path.isfile(record.path) else None

    async def media_record(self, media_id: str) -> Optional[MediaRecord]:
        return await asyncio.to_thread(self._existing_media, media_id)
