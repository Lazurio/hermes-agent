"""``lazurio_web`` adapter: the web bubble's channel to Buddy (the Hermes Agent in the owner's
Osobní Prostředí), implementing LazurioPlatform's runtime contract ``lazurio.buddy-runtime.v1``.

Inbound, every request is the owner's direct message: ``chat_id = user_id = "owner"``, ``dm``, so the
gateway keys the conversation ``agent:main:lazurio_web:dm:owner`` and authorizes it through
``LAZURIO_WEB_ALLOWED_USERS`` like any other platform. Outbound, every gateway call becomes events in
the durable outbox (``outbox.py``), and a send succeeds only once they are committed:

=========================================  ======================================================
gateway call                                events
=========================================  ======================================================
a message arrives                           ``message.final`` of the person's message (``client_id``)
processing starts                           ``turn.started {reply_to}``, ``status working``
``send`` with ``expect_edits``, edits       ``message.delta``: each new stable piece of the answer
``send`` / ``edit_message(finalize=True)``  ``message.final`` of Buddy's answer (``client_id`` null)
``send_image_file``, ``send_document`` …    ``message.final`` with ``media``
``set_status_text`` (live tool status)      ``progress {text}``
``send_clarify``                            ``clarify.request``, ``status waiting``
the question ends, however                  ``clarify.retired``, ``status working``
processing ends                             ``error turn-failed`` on failure, ``status idle``
=========================================  ======================================================

The stream consumer edits with whole snapshots that end in a cursor and may close an open code
fence that later moves. A piece is therefore what two consecutive snapshots agree on beyond what was
already sent: never the cursor, never a synthetic fence. ``message.final`` replaces the pieces.
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
from typing import Any, Optional

from gateway.platforms._shared import coerce_port, extra_or_secret
from gateway.platforms.base import BasePlatformAdapter, SendResult, cache_media_bytes_async
from gateway.platforms.event import MessageEvent, MessageType, ProcessingOutcome
from gateway.config import Platform, PlatformConfig

from . import clarify, history, page_context, wire
from .outbox import EventHub, InboundClaim, MediaRecord, Outbox

logger = logging.getLogger(__name__)

PLATFORM = page_context.PLATFORM
OWNER = "owner"
CHAT_NAME = "Lazurio web"
LISTEN_HOST = "127.0.0.1"
MIN_SECRET_BYTES = 32
# Every event stream ends by then, so each reconnect passes the gateway's admission again; the
# contract caps a stream at 110 s.
STREAM_MAX_SECONDS = 100.0
MAX_MEDIA_BYTES = 50 * 1024 * 1024
EVENT_RETENTION_SECONDS = 3 * 24 * 3600
INBOUND_RETENTION_SECONDS = 7 * 24 * 3600
MEDIA_RETENTION_SECONDS = 7 * 24 * 3600
PRUNE_INTERVAL_SECONDS = 3600.0
CLARIFY_POLL_SECONDS = 0.5
_FINALS_SCAN = 200  # recent message.final events history matches Buddy's replies against
_MEMORY = 1024  # bounded per-message bookkeeping


class SecretFileError(ValueError):
    pass


def read_secret_file(path: str) -> bytes:
    """The bearer secret from ``path``: a regular file of the gateway's user, mode 0600 or stricter,
    one line of at least ``MIN_SECRET_BYTES`` visible ASCII. Anything else refuses the channel."""
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


def _remember(store: OrderedDict, key: str, value: Any) -> None:
    store[key] = value
    store.move_to_end(key)
    while len(store) > _MEMORY:
        store.popitem(last=False)


def _common_prefix(left: str, right: str) -> int:
    limit = min(len(left), len(right))
    index = 0
    while index < limit and left[index] == right[index]:
        index += 1
    return index


def _file_name(name: str) -> str:
    clean = wire.clean_line(name.replace("/", "_").replace("\\", "_"), wire.NAME_LENGTH)
    return clean if clean not in ("", ".", "..") else "file"


def _media_item(record: MediaRecord) -> dict[str, Any]:
    return {"media_id": record.id, "name": record.name, "mime": record.mime, "size": record.size}


def _media_message_type(kinds: list[str]) -> MessageType:
    if not kinds:
        return MessageType.TEXT
    return MessageType.PHOTO if all(kind == "image" for kind in kinds) else MessageType.DOCUMENT


class LazurioWebAdapter(BasePlatformAdapter):
    MAX_MESSAGE_LENGTH = wire.TEXT_LENGTH
    # The finalize edit is the only signal that a streamed message is complete (``message.final``),
    # so the consumer must send it even when the text did not change since the last edit.
    REQUIRES_EDIT_FINALIZE = True
    supports_code_blocks = True
    # The live tool status (``set_status_text``) feeds the bubble's one-line progress indicator.
    supports_status_text = True

    def __init__(self, config: PlatformConfig):
        super().__init__(config=config, platform=Platform(PLATFORM))
        extra = config.extra or {}
        self.listen_port = coerce_port(extra.get("listen_port"), 0)
        self._secret_file = str(extra_or_secret(extra, "secret_file", "LAZURIO_WEB_SECRET_FILE") or "")
        self._secret = b""
        self._secret_stamp: Optional[tuple[int, ...]] = None
        self.outbox: Optional[Outbox] = None
        self.hub = EventHub()
        self.stream_max_seconds = STREAM_MAX_SECONDS
        self._web_runner: Any = None
        self._media_dir: Optional[Path] = None
        self._state = "idle"
        self._progress_lock = threading.Lock()
        self._last_progress = ""
        self._cards: dict[str, clarify.Card] = {}  # open questions, by clarify_id
        self._clarify_watchers: dict[str, asyncio.Task] = {}
        self._retired: OrderedDict[str, None] = OrderedDict()
        self._finals: OrderedDict[str, str] = OrderedDict()  # message_id -> last final text
        self._streams: OrderedDict[str, tuple[str, int]] = OrderedDict()  # message_id -> (snapshot, sent)
        self._intake_lock = asyncio.Lock()
        self._dispatched: OrderedDict[str, None] = OrderedDict()  # message ids this process dispatched
        self._prune_task: Optional[asyncio.Task] = None

    # -- lifecycle ------------------------------------------------------------------------------

    def _fatal(self, code: str, message: str, *, retryable: bool) -> bool:
        logger.error("[%s] %s", self.name, message)
        self._set_fatal_error(code, message, retryable=retryable)
        return False

    @property
    def secret(self) -> bytes:
        """The current secret. Machines may rotate the file; a changed file is read again, and one
        that turned unsafe locks the API (401 for everyone) until it is fixed."""
        try:
            info = os.lstat(Path(self._secret_file).expanduser())
            stamp = (info.st_ino, info.st_mtime_ns, info.st_size, info.st_mode, info.st_uid)
        except OSError:
            return b""
        if stamp != self._secret_stamp:
            try:
                self._secret = read_secret_file(self._secret_file)
            except (SecretFileError, OSError):
                logger.error("[%s] the secret file is no longer usable; refusing every request", self.name)
                self._secret = b""
            self._secret_stamp = stamp
        return self._secret

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        try:
            from aiohttp import web
            from .http_api import build_app
        except ImportError:
            return self._fatal("lazurio_web_deps", "aiohttp is not installed (messaging extra)", retryable=False)
        if not 0 < self.listen_port < 65536:
            return self._fatal("lazurio_web_config", "extra.listen_port must be a TCP port", retryable=False)
        try:
            await asyncio.to_thread(read_secret_file, self._secret_file)
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
        if last is not None and last.data.get("state") != "idle":
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

    @staticmethod
    def _final(message_id: str, text: str, *, role: str = "assistant", media: Optional[list] = None,
               client_id: Optional[str] = None) -> dict[str, Any]:
        return {"message_id": message_id, "role": role, "text": wire.clean_text(text), "at": wire.instant(),
                "via": PLATFORM, "media": media or [], "client_id": client_id}

    async def _send_final(self, message_id: str, text: str) -> SendResult:
        # REQUIRES_EDIT_FINALIZE makes the consumer finalize even an unchanged message, and at the
        # end of a turn it may do so twice: one ``message.final`` per (message, text) is enough.
        if self._finals.get(message_id) == text:
            return SendResult(success=True, message_id=message_id)
        self._streams.pop(message_id, None)
        result = await self._deliver("message.final", self._final(message_id, text), message_id)
        if result.success:
            _remember(self._finals, message_id, text)
        return result

    async def _send_piece(self, message_id: str, snapshot: str) -> SendResult:
        """Emit what this snapshot and the previous one agree on beyond the pieces already sent. A
        repeated snapshot adds nothing (its cursor and closing fence did not move), and a piece that
        could not be committed leaves the state as it was, so the retry sends the same piece."""
        previous, sent = self._streams.get(message_id, ("", 0))
        if snapshot == previous:
            return SendResult(success=True, message_id=message_id)
        stable = _common_prefix(previous, snapshot)
        piece = wire.clean_text(snapshot[sent:stable]) if stable > sent else ""
        if not piece.strip():  # blank so far: it rides along with the next piece
            _remember(self._streams, message_id, (snapshot, sent))
            return SendResult(success=True, message_id=message_id)
        result = await self._deliver("message.delta", {"message_id": message_id, "text": piece}, message_id)
        if result.success:
            _remember(self._streams, message_id, (snapshot, stable))
        return result

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[dict[str, Any]] = None) -> SendResult:
        text = content or ""
        if (metadata or {}).get("expect_edits"):
            return await self._send_piece(uuid.uuid4().hex, text)
        chunks = self.truncate_message(text, wire.TEXT_LENGTH) if len(text) > wire.TEXT_LENGTH else [text]
        sent: list[str] = []
        for chunk in chunks:
            result = await self._send_final(uuid.uuid4().hex, chunk)
            if not result.success:
                return result
            sent.append(result.message_id)
        return SendResult(success=True, message_id=sent[-1], continuation_message_ids=tuple(sent[:-1]))

    async def edit_message(self, chat_id: str, message_id: str, content: str, *, finalize: bool = False,
                           metadata: Optional[dict[str, Any]] = None) -> SendResult:
        if finalize:
            return await self._send_final(str(message_id), content or "")
        return await self._send_piece(str(message_id), content or "")

    def set_status_text(self, chat_id: str, text: Optional[str]) -> None:
        """Runs on the agent's worker thread (``TurnRunner._progress_live_status``). The contract's
        progress is one non-blank line; a cleared status sends nothing."""
        super().set_status_text(chat_id, text)
        line = wire.clean_line(text or "", wire.PROGRESS_LENGTH)
        with self._progress_lock:
            if not line or line == self._last_progress:
                return
            self._last_progress = line
        try:
            self._emit_now("progress", {"text": line})
        except Exception:
            logger.debug("[%s] progress event dropped", self.name, exc_info=True)

    async def send_clarify(self, chat_id: str, question: str, choices: Optional[list], clarify_id: str,
                           session_key: str, metadata: Optional[dict[str, Any]] = None) -> SendResult:
        entry = clarify.open_entry(clarify_id)
        shown = clarify.card(clarify_id, question, choices, bool(getattr(entry, "multi_select", False)))
        result = await self._deliver("clarify.request", shown.request, clarify_id)
        if result.success:
            self._cards[clarify_id] = shown
            await self._set_state("waiting")
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
        _remember(self._retired, clarify_id, None)
        self._cards.pop(clarify_id, None)
        try:
            await self._emit("clarify.retired", {"clarify_id": clarify_id})
            if self._state == "waiting" and not self._cards:
                await self._set_state("working")
        except Exception:
            logger.warning("[%s] could not record clarify.retired for %s", self.name, clarify_id, exc_info=True)

    def _store_media(self, source: Path, name: str, mime: Optional[str] = None) -> MediaRecord:
        """Copy a file into the channel's own store: the original may live in a cache Hermes cleans
        up before the client fetched it."""
        info = source.stat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("not a regular file")
        if info.st_size > MAX_MEDIA_BYTES:
            raise ValueError(f"file is larger than {MAX_MEDIA_BYTES} bytes")
        name = _file_name(name or source.name)
        guessed = mime or mimetypes.guess_type(name)[0] or "application/octet-stream"
        media_id = uuid.uuid4().hex
        target = self._media_dir / f"{media_id}{source.suffix.lower()[:16]}"
        shutil.copyfile(source, target)
        os.chmod(target, 0o600)
        record = MediaRecord(media_id, name, guessed.lower(), info.st_size, str(target))
        self.outbox.add_media(record)
        return record

    async def _send_file(self, path: str, caption: Optional[str], file_name: Optional[str] = None) -> SendResult:
        if self.outbox is None or self._media_dir is None:
            return SendResult(success=False, error="lazurio_web is not connected", retryable=True)
        try:
            record = await asyncio.to_thread(self._store_media, Path(path), file_name or Path(path).name)
        except (OSError, ValueError, sqlite3.Error) as exc:
            logger.warning("[%s] cannot hand a file to the web client: %s", self.name, exc)
            return SendResult(success=False, error=str(exc))
        message_id = uuid.uuid4().hex
        data = self._final(message_id, caption or "", media=[_media_item(record)])
        return await self._deliver("message.final", data, message_id)

    async def send_image_file(self, chat_id: str, image_path: str, caption: Optional[str] = None,
                              reply_to: Optional[str] = None, metadata: Optional[dict[str, Any]] = None,
                              **kwargs) -> SendResult:
        return await self._send_file(image_path, caption)

    async def send_document(self, chat_id: str, file_path: str, caption: Optional[str] = None,
                            file_name: Optional[str] = None, reply_to: Optional[str] = None,
                            metadata: Optional[dict[str, Any]] = None, **kwargs) -> SendResult:
        return await self._send_file(file_path, caption, file_name)

    async def send_voice(self, chat_id: str, audio_path: str, caption: Optional[str] = None,
                         reply_to: Optional[str] = None, metadata: Optional[dict[str, Any]] = None,
                         **kwargs) -> SendResult:
        return await self._send_file(audio_path, caption)

    async def send_video(self, chat_id: str, video_path: str, caption: Optional[str] = None,
                         reply_to: Optional[str] = None, metadata: Optional[dict[str, Any]] = None,
                         **kwargs) -> SendResult:
        return await self._send_file(video_path, caption)

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {"name": CHAT_NAME, "type": "dm", "chat_id": chat_id}

    # -- processing lifecycle -------------------------------------------------------------------

    @staticmethod
    def _reply_to(event: MessageEvent) -> Optional[str]:
        """The person's message a turn answers; null for a turn Buddy starts himself (a wake)."""
        message_id = str(event.message_id or "")
        return message_id if not event.internal and wire.is_id(message_id) else None

    async def on_processing_start(self, event: MessageEvent) -> None:
        await self._emit("turn.started", {"reply_to": self._reply_to(event)})
        await self._set_state("working")

    async def on_processing_complete(self, event: MessageEvent, outcome: ProcessingOutcome) -> None:
        with self._progress_lock:
            self._last_progress = ""
        if outcome == ProcessingOutcome.FAILURE:
            await self._emit("error", {"code": "turn-failed", "message_id": self._reply_to(event)})
        await self._set_state("idle")

    # -- inbound (called by http_api) -----------------------------------------------------------

    def _owner_source(self, message_id: Optional[str] = None):
        return self.build_source(chat_id=OWNER, chat_name=CHAT_NAME, chat_type="dm", user_id=OWNER,
                                 message_id=message_id)

    def owner_session_key(self) -> str:
        return self._source_session_key(self._owner_source())

    def _require_owner(self) -> None:
        """The gateway does not take messages yet, or its allowlist does not admit the owner
        (``LAZURIO_WEB_ALLOWED_USERS=owner``): either way the agent does not run for this channel."""
        if self._message_handler is None or self._is_sender_authorized(OWNER, "dm", OWNER) is False:
            raise wire.refuse("unavailable")

    async def _intake_files(self, inbound: wire.InboundMessage) -> tuple[list, list[str], list[str], list[str]]:
        """``(media items, hermes paths, mime types, kinds)``: each file cached for the turn and kept
        in the channel's store for every tab and the history."""
        items, paths, mimes, kinds = [], [], [], []
        for attachment in inbound.attachments:
            cached = await cache_media_bytes_async(attachment.data, filename=attachment.name,
                                                   mime_type=attachment.mime)
            if cached is None:
                raise wire.refuse("bad-request")  # an image that is no image
            record = await asyncio.to_thread(self._store_media, Path(cached.path), attachment.name, attachment.mime)
            items.append(_media_item(record))
            paths.append(cached.path)
            mimes.append(cached.media_type)
            kinds.append(cached.kind)
        return items, paths, mimes, kinds

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
        claim left behind by a gateway that died before either happened, or whose dispatch failed,
        is dispatched again under the same id, so a retry recovers the message."""
        if claim.message_id in self._dispatched:
            return True
        return await asyncio.to_thread(self._in_transcript, self.owner_session_key(), claim.message_id)

    async def submit_message(self, inbound: wire.InboundMessage) -> str:
        """Intake is serialized: a concurrent retry waits for the first attempt, so ``202`` always
        names a message that was handed to the gateway."""
        self._require_owner()
        async with self._intake_lock:
            claim = await asyncio.to_thread(self.outbox.claim_inbound, inbound.client_id, uuid.uuid4().hex)
            if not claim.created and await self._claim_settled(claim):
                return claim.message_id
            message_id = claim.message_id
            items, paths, mimes, kinds = await self._intake_files(inbound)
            await asyncio.to_thread(self.outbox.link_media, message_id, [item["media_id"] for item in items])
            if inbound.context is not None:
                page_context.STORE.put(message_id, page_context.render(inbound.context))
            # The person's own message, for every open tab, before its turn can start.
            await self._emit("message.final", self._final(
                message_id, inbound.text, role="user", media=items, client_id=inbound.client_id))
            await self.handle_message(MessageEvent(
                text=inbound.text, message_type=_media_message_type(kinds), user_id=OWNER,
                source=self._owner_source(message_id), message_id=message_id, media_urls=paths,
                media_types=mimes, timestamp=datetime.now(tz=UTC)))
            _remember(self._dispatched, message_id, None)
            return message_id

    async def request_stop(self) -> None:
        """``/stop`` through the gateway's own command path, as typed on Telegram; nothing when no
        turn runs (the answer is ``202 {}`` either way)."""
        self._require_owner()
        if self.owner_session_key() not in self._active_sessions:
            return
        message_id = uuid.uuid4().hex
        await self.handle_message(MessageEvent(
            text="/stop", message_type=MessageType.COMMAND, user_id=OWNER,
            source=self._owner_source(message_id), message_id=message_id))

    async def answer_clarify(self, clarify_id: str, kind: str, value: Any) -> None:
        """``404 not-found`` for no such question here, ``409 conflict`` for one answered or retired."""
        from tools.clarify_gateway import resolve_gateway_clarify

        self._require_owner()
        entry, shown = clarify.open_entry(clarify_id), self._cards.get(clarify_id)
        if entry is None or shown is None or entry.session_key != self.owner_session_key():
            raise wire.refuse("conflict" if clarify_id in self._retired else "not-found")
        if not resolve_gateway_clarify(clarify_id, clarify.answer_value(entry, shown, kind, value)):
            raise wire.refuse("conflict")
        await self._retire(clarify_id)

    def _history_page(self, session_key: str, before: Optional[str], limit: int):
        outbox = self.outbox

        def media_for(message_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
            linked = outbox.message_media(message_ids)
            return {key: [_media_item(record) for record in records if os.path.isfile(record.path)]
                    for key, records in linked.items()}

        return history.page(self._lineage_rows(session_key), before, limit,
                            finals=outbox.recent_assistant_finals(_FINALS_SCAN), media_for=media_for)

    async def read_history(self, before: Optional[str], limit: int) -> dict[str, Any]:
        """Only the newest page carries the snapshot, and its ``event_id`` is read BEFORE the
        transcript: the events after it may repeat a reply the page holds (same ``message_id``, so
        the client keeps one) but never miss one."""
        snapshot = None
        if before is None:
            event_id = await asyncio.to_thread(self.outbox.last_id)
            open_cards = [card.request for card in self._cards.values()]
            snapshot = {"event_id": str(event_id), "state": self._state,
                        "clarify": open_cards[-1] if open_cards else None}
        messages, cursor = await asyncio.to_thread(self._history_page, self.owner_session_key(), before, limit)
        return {"messages": messages, "before": cursor, "snapshot": snapshot}

    def _existing_media(self, media_id: str) -> Optional[MediaRecord]:
        record = self.outbox.get_media(media_id)
        return record if record is not None and os.path.isfile(record.path) else None

    async def media_record(self, media_id: str) -> Optional[MediaRecord]:
        return await asyncio.to_thread(self._existing_media, media_id)
