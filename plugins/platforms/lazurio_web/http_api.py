"""HTTP surface of ``lazurio_web``: the runtime API ``lazurio.buddy-runtime.v1``.

Only the Lazurio frame server in the same Osobní Prostředí talks to it, over loopback, with the
per-install secret Machines write to a 0600 file. Every request passes three gates before a handler
runs: the peer and the ``Host`` header are loopback (no DNS rebinding), a present ``Origin`` is
loopback too (a page in the Environment's browser can reach 127.0.0.1 but is never same-origin),
and ``Authorization: Bearer <secret>`` matches in constant time.

Endpoints (contract owner: LazurioPlatform ``src/buddy/contract/``):

- ``POST /v1/messages`` ``{client_id, text, attachments:[{name, mime, data_base64}], context}`` ->
  ``202 {message_id}``; a repeated ``client_id`` returns the first ``message_id`` and dispatches
  nothing.
- ``GET /v1/events?after=<id>``: server-sent events replayed from the durable outbox, then live;
  every stream ends after ``adapter.stream_max_seconds`` (100 s) so each reconnect passes the
  gateway route again.
- ``GET /v1/history?before=<cursor>&limit=<n>``, ``POST /v1/clarify/<id>``, ``POST /v1/stop``.
- ``GET /v1/media/<id>``: a file Buddy sent (referenced from ``message.final.media``).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hmac
import ipaddress
import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional
from urllib.parse import urlsplit

from aiohttp import web

from .errors import ApiError
from .outbox import OutboxEvent

if TYPE_CHECKING:
    from .adapter import LazurioWebAdapter

KEEPALIVE_SECONDS = 15.0
MAX_STREAMS = 16
REPLAY_BATCH = 200
MAX_TEXT_CHARS = 32_000
MAX_ATTACHMENTS = 8
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_TOTAL_ATTACHMENT_BYTES = 20 * 1024 * 1024
# base64 inflates by 4/3; leave room for the JSON envelope.
CLIENT_MAX_BODY_BYTES = MAX_TOTAL_ATTACHMENT_BYTES * 4 // 3 + 1024 * 1024
HISTORY_DEFAULT_LIMIT = 50
HISTORY_MAX_LIMIT = 200

_CLIENT_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_MIME = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+-]{0,63}/[A-Za-z0-9][A-Za-z0-9.+-]{0,63}")
_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_LOOPBACK_NAMES = frozenset({"127.0.0.1", "localhost"})
_MESSAGE_KEYS = frozenset({"client_id", "text", "attachments", "context"})
_ATTACHMENT_KEYS = frozenset({"name", "mime", "data_base64"})
_JSON_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


@dataclass
class Attachment:
    name: str
    mime: str
    data: bytes


@dataclass
class InboundMessage:
    client_id: str
    text: str
    attachments: list[Attachment] = field(default_factory=list)
    context: Any = None


def json_response(payload: dict[str, Any], status: int = 200) -> web.Response:
    return web.json_response(payload, status=status, headers=_JSON_HEADERS,
                             dumps=lambda obj: json.dumps(obj, ensure_ascii=False))


def error_response(error: ApiError) -> web.Response:
    response = json_response({"error": {"code": error.code, "message": error.message}}, error.status)
    if error.status == 401:
        response.headers["WWW-Authenticate"] = "Bearer"
    return response


# -- request gates -----------------------------------------------------------------------------

def _is_loopback_ip(value: Optional[str]) -> bool:
    try:
        return ipaddress.ip_address((value or "").strip("[]")).is_loopback
    except ValueError:
        return False


def host_allowed(host: Optional[str], port: int) -> bool:
    """``Host`` must name this listener on loopback: 127.0.0.1 or localhost, our port or none."""
    if not host:
        return False
    parsed = urlsplit(f"//{host}")
    try:
        host_port = parsed.port
    except ValueError:
        return False
    return (parsed.hostname or "") in _LOOPBACK_NAMES and host_port in (None, port)


def origin_allowed(origin: str, port: int) -> bool:
    """A browser sends ``Origin``; the frame server's proxy does not. When present it must be this
    loopback listener itself (``null`` and every other origin are refused)."""
    parsed = urlsplit(origin)
    try:
        origin_port = parsed.port
    except ValueError:
        return False
    return parsed.scheme == "http" and (parsed.hostname or "") in _LOOPBACK_NAMES and origin_port == port


def bearer_matches(header: Optional[str], secret: bytes) -> bool:
    scheme, _, presented = (header or "").partition(" ")
    if scheme.lower() != "bearer" or not presented.strip() or not secret:
        return False
    return hmac.compare_digest(presented.strip().encode("utf-8"), secret)


def make_guard(api: LazurioWebAdapter):
    @web.middleware
    async def guard(request: web.Request, handler):
        if not _is_loopback_ip(request.remote) or not host_allowed(request.headers.get("Host"), api.listen_port):
            return error_response(ApiError(403, "forbidden_host", "loopback host only"))
        origin = request.headers.get("Origin")
        if origin is not None and not origin_allowed(origin, api.listen_port):
            return error_response(ApiError(403, "forbidden_origin", "cross-origin requests are refused"))
        if not bearer_matches(request.headers.get("Authorization"), api.secret):
            return error_response(ApiError(401, "unauthorized", "missing or wrong bearer secret"))
        try:
            return await handler(request)
        except ApiError as exc:
            return error_response(exc)
        except web.HTTPRequestEntityTooLarge:
            return error_response(ApiError(413, "too_large", "request body too large"))
    return guard


# -- request parsing ---------------------------------------------------------------------------

async def _json_body(request: web.Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except web.HTTPRequestEntityTooLarge:
        raise
    except (UnicodeDecodeError, ValueError) as exc:
        raise ApiError(400, "invalid_json", "body is not JSON") from exc
    if not isinstance(body, dict):
        raise ApiError(400, "invalid_body", "body must be a JSON object")
    return body


def _attachment(raw: Any) -> Attachment:
    if not isinstance(raw, dict) or set(raw) != _ATTACHMENT_KEYS:
        raise ApiError(400, "invalid_attachment", "attachment needs exactly name, mime, data_base64")
    name, mime, data = raw["name"], raw["mime"], raw["data_base64"]
    if not isinstance(name, str) or not name.strip() or len(name) > 255:
        raise ApiError(400, "invalid_attachment", "attachment name must be 1-255 characters")
    if not isinstance(mime, str) or not _MIME.fullmatch(mime):
        raise ApiError(400, "invalid_attachment", "attachment mime must be type/subtype")
    if not isinstance(data, str):
        raise ApiError(400, "invalid_attachment", "data_base64 must be a string")
    if len(data) > MAX_ATTACHMENT_BYTES * 4 // 3 + 4:
        raise ApiError(413, "attachment_too_large", "attachment exceeds the size cap")
    try:
        decoded = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ApiError(400, "invalid_attachment", "data_base64 is not base64") from exc
    if not decoded:
        raise ApiError(400, "invalid_attachment", "attachment is empty")
    if len(decoded) > MAX_ATTACHMENT_BYTES:
        raise ApiError(413, "attachment_too_large", "attachment exceeds the size cap")
    return Attachment(name.strip(), mime.lower(), decoded)


def parse_message(body: dict[str, Any]) -> InboundMessage:
    unknown = set(body) - _MESSAGE_KEYS
    if unknown:
        raise ApiError(400, "unknown_field", f"unknown field(s): {', '.join(sorted(unknown))}")
    client_id, text = body.get("client_id"), body.get("text", "")
    if not isinstance(client_id, str) or not _CLIENT_ID.fullmatch(client_id):
        raise ApiError(400, "invalid_client_id", "client_id must be 1-128 of [A-Za-z0-9._:-]")
    if not isinstance(text, str) or len(text) > MAX_TEXT_CHARS:
        raise ApiError(400, "invalid_text", f"text must be a string of at most {MAX_TEXT_CHARS} characters")
    raw_attachments = body.get("attachments") or []
    if not isinstance(raw_attachments, list) or len(raw_attachments) > MAX_ATTACHMENTS:
        raise ApiError(400, "invalid_attachments", f"attachments must be a list of at most {MAX_ATTACHMENTS}")
    attachments = [_attachment(item) for item in raw_attachments]
    if sum(len(item.data) for item in attachments) > MAX_TOTAL_ATTACHMENT_BYTES:
        raise ApiError(413, "attachments_too_large", "attachments exceed the total size cap")
    if not text.strip() and not attachments:
        raise ApiError(400, "empty_message", "text or an attachment is required")
    return InboundMessage(client_id, text, attachments, body.get("context"))


def _int_param(request: web.Request, name: str, *, default: Optional[int], low: int, high: int) -> Optional[int]:
    raw = request.query.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ApiError(400, f"invalid_{name}", f"{name} must be an integer") from exc
    if not low <= value <= high:
        raise ApiError(400, f"invalid_{name}", f"{name} must be between {low} and {high}")
    return value


def _stream_cursor(request: web.Request) -> Optional[int]:
    """``after`` wins; an ``EventSource`` reconnect sends ``Last-Event-ID`` instead."""
    if request.query.get("after") not in (None, ""):
        return _int_param(request, "after", default=None, low=0, high=2**63 - 1)
    header = (request.headers.get("Last-Event-ID") or "").strip()
    if not header:
        return None
    if not header.isdigit():
        raise ApiError(400, "invalid_after", "Last-Event-ID must be an event id")
    return int(header)


# -- handlers ----------------------------------------------------------------------------------

def sse_frame(event: OutboxEvent) -> bytes:
    data = json.dumps(event.data, ensure_ascii=False, separators=(",", ":"))
    return f"id: {event.id}\nevent: {event.type}\ndata: {data}\n\n".encode()


# No ``id:``: the client's cursor must not move past events it never received.
_GAP_FRAME = b'event: error\ndata: {"code":"replay_gap"}\n\n'


class Handlers:
    def __init__(self, api: LazurioWebAdapter) -> None:
        self.api = api
        self._streams = 0

    async def post_message(self, request: web.Request) -> web.Response:
        inbound = parse_message(await _json_body(request))
        message_id = await self.api.submit_message(inbound)
        return json_response({"message_id": message_id}, 202)

    async def get_history(self, request: web.Request) -> web.Response:
        before = _int_param(request, "before", default=None, low=1, high=2**63 - 1)
        limit = _int_param(request, "limit", default=HISTORY_DEFAULT_LIMIT, low=1, high=HISTORY_MAX_LIMIT)
        return json_response(await self.api.read_history(before, limit))

    async def post_clarify(self, request: web.Request) -> web.Response:
        clarify_id = request.match_info["clarify_id"]
        if not _ID.fullmatch(clarify_id):
            raise ApiError(404, "unknown_clarify", "no such question")
        return json_response(await self.api.answer_clarify(clarify_id, await _json_body(request)))

    async def post_stop(self, request: web.Request) -> web.Response:
        return json_response(await self.api.request_stop())

    async def get_media(self, request: web.Request) -> web.StreamResponse:
        media_id = request.match_info["media_id"]
        record = await self.api.media_record(media_id) if _ID.fullmatch(media_id) else None
        if record is None:
            raise ApiError(404, "unknown_media", "no such file")
        # Raster images may render inline; anything else (SVG, HTML, PDF) downloads, sandboxed.
        inline = record.mime in ("image/png", "image/jpeg", "image/gif", "image/webp")
        headers = {
            "Content-Type": record.mime, "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'", "Cache-Control": "private, max-age=3600",
            "Content-Disposition": f"{'inline' if inline else 'attachment'}; "
                                   f"filename={json.dumps(record.name, ensure_ascii=True)}",
        }
        return web.FileResponse(record.path, headers=headers)

    async def get_events(self, request: web.Request) -> web.StreamResponse:
        if self._streams >= MAX_STREAMS:
            raise ApiError(503, "too_many_streams", "too many open event streams")
        after = _stream_cursor(request)
        self._streams += 1
        try:
            return await self._stream(request, after)
        finally:
            self._streams -= 1

    async def _start_cursor(self, after: Optional[int]) -> tuple[int, bool]:
        """``(cursor, gap)``. No ``after`` means "from now on". A cursor older than the oldest
        retained event, or ahead of the newest id ever assigned, cannot be replayed exactly."""
        outbox = self.api.outbox
        last = await asyncio.to_thread(outbox.last_id)
        if after is None:
            return last, False
        if after > last:
            return last, True
        first = await asyncio.to_thread(outbox.first_id)
        pruned_all = first is None and after < last
        return after, pruned_all or (first is not None and first > after + 1)

    async def _stream(self, request: web.Request, after: Optional[int]) -> web.StreamResponse:
        api, outbox, hub = self.api, self.api.outbox, self.api.hub
        response = web.StreamResponse(headers={
            "Content-Type": "text/event-stream; charset=utf-8", "Cache-Control": "no-store",
            "X-Accel-Buffering": "no", "X-Content-Type-Options": "nosniff"})
        await response.prepare(request)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + api.stream_max_seconds
        wakeup = hub.subscribe()
        try:
            await response.write(b"retry: 1000\n\n")
            cursor, gap = await self._start_cursor(after)
            if gap:
                await response.write(_GAP_FRAME)
            while not hub.closed:
                wakeup.clear()
                batch = await asyncio.to_thread(outbox.read_after, cursor, REPLAY_BATCH)
                for event in batch:
                    await response.write(sse_frame(event))
                    cursor = event.id
                if len(batch) == REPLAY_BATCH:
                    continue
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait_for(wakeup.wait(), timeout=min(KEEPALIVE_SECONDS, remaining))
                except TimeoutError:
                    if deadline - loop.time() > 0:
                        await response.write(b": keepalive\n\n")
        except ConnectionError:
            return response
        finally:
            hub.unsubscribe(wakeup)
        with contextlib.suppress(ConnectionError, RuntimeError):
            await response.write_eof()
        return response


def build_app(api: LazurioWebAdapter) -> web.Application:
    handlers = Handlers(api)
    app = web.Application(middlewares=[make_guard(api)], client_max_size=CLIENT_MAX_BODY_BYTES)
    app.router.add_post("/v1/messages", handlers.post_message)
    app.router.add_get("/v1/events", handlers.get_events)
    app.router.add_get("/v1/history", handlers.get_history)
    app.router.add_post("/v1/clarify/{clarify_id}", handlers.post_clarify)
    app.router.add_post("/v1/stop", handlers.post_stop)
    app.router.add_get("/v1/media/{media_id}", handlers.get_media)
    return app
