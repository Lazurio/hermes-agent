"""HTTP surface of ``lazurio_web``: the runtime API ``lazurio.buddy-runtime.v1``.

The contract belongs to LazurioPlatform (``src/buddy/contract/README.md``; its vectors are the
truth). The Lazurio frame server in the same Osobní Prostředí is the only client. Admission runs in
the contract's order before any route is named:

1. ``Host`` is ``127.0.0.1:<port>``, ``localhost:<port>`` or ``[::1]:<port>``, the peer is loopback,
   and there is no ``Origin`` and no ``Sec-Fetch-*`` header (current browsers send one with every
   request): the exact ``Host`` stops DNS rebinding, and a web page's request is refused before the
   secret is even looked at. Else ``403 forbidden``.
2. ``Authorization: Bearer <secret>``, compared in constant time. Else ``401 unauthorized``.
3. The route. Else ``404 not-found`` or ``405 method-not-allowed``.

Every error is ``{"error": {"code"}}`` and nothing else: no text of the adapter reaches the person.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import ipaddress
import json
import logging
from typing import TYPE_CHECKING, Any, Optional
from urllib.parse import quote

from aiohttp import web

from . import wire
from .outbox import OutboxEvent

if TYPE_CHECKING:
    from .adapter import LazurioWebAdapter

logger = logging.getLogger(__name__)

KEEPALIVE_SECONDS = 14.0  # the contract wants a comment at least every 15 s
MAX_STREAMS = 16
REPLAY_BATCH = 200
_ID_ROUTE = "{%s:[A-Za-z0-9][A-Za-z0-9._:-]{0,63}}"


def error_response(code: str) -> web.Response:
    return web.json_response({"error": {"code": code}}, status=wire.STATUS[code],
                             headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


def json_response(payload: dict[str, Any], status: int = 200) -> web.Response:
    return web.json_response(payload, status=status, dumps=lambda obj: json.dumps(obj, ensure_ascii=False),
                             headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


# -- admission ---------------------------------------------------------------------------------

def _loopback_peer(value: Optional[str]) -> bool:
    try:
        return ipaddress.ip_address((value or "").strip("[]")).is_loopback
    except ValueError:
        return False


def admitted_host(host: Optional[str], port: int) -> bool:
    return host in (f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}")


def from_browser(headers: Any) -> bool:
    return any(name.lower() == "origin" or name.lower().startswith("sec-fetch-") for name in headers)


def bearer_matches(header: Optional[str], secret: bytes) -> bool:
    if not secret or not (header or "").startswith("Bearer "):
        return False
    return hmac.compare_digest(header[len("Bearer "):].encode("utf-8"), secret)


_ROUTE_ERRORS = {404: "not-found", 405: "method-not-allowed", 413: "payload-too-large"}


def make_guard(api: LazurioWebAdapter):
    @web.middleware
    async def guard(request: web.Request, handler):
        if (not _loopback_peer(request.remote) or not admitted_host(request.headers.get("Host"), api.listen_port)
                or from_browser(request.headers)):
            return error_response("forbidden")
        if not bearer_matches(request.headers.get("Authorization"), api.secret):
            return error_response("unauthorized")
        try:
            return await handler(request)
        except wire.ApiError as exc:
            return error_response(exc.code)
        except web.HTTPException as exc:
            return error_response(_ROUTE_ERRORS.get(exc.status, "bad-request"))
        except Exception:
            logger.exception("[lazurio_web] request %s %s failed", request.method, request.path)
            return error_response("internal")
    return guard


# -- requests ----------------------------------------------------------------------------------

async def _json_body(request: web.Request, limit: int) -> Any:
    if request.content_length is not None and request.content_length > limit:
        raise wire.refuse("payload-too-large")
    raw = await request.read()  # client_max_size bounds every body to the message limit
    if len(raw) > limit:
        raise wire.refuse("payload-too-large")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise wire.refuse("bad-request") from exc


def _query(request: web.Request, allowed: set[str]) -> dict[str, str]:
    """Each parameter at most once, none but ``allowed``."""
    query = request.rel_url.query
    if any(name not in allowed or len(query.getall(name)) > 1 for name in query):
        raise wire.refuse("bad-request")
    return {name: query[name] for name in query}


def _history_query(request: web.Request) -> tuple[Optional[int], int]:
    query = _query(request, {"before", "limit"})
    before, limit = query.get("before"), query.get("limit")
    if before is not None and not wire.CURSOR.fullmatch(before):
        raise wire.refuse("bad-request")
    if before is not None and not before.isdigit():
        raise wire.refuse("bad-request")  # a well-formed cursor this adapter never minted
    if limit is not None and not (limit[:1] in "123456789" and limit.isdigit() and len(limit) <= 3
                                  and int(limit) <= wire.HISTORY_LIMIT_MAX):
        raise wire.refuse("bad-request")
    return (int(before) if before is not None else None,
            int(limit) if limit is not None else wire.HISTORY_LIMIT_DEFAULT)


def _events_cursor(request: web.Request) -> Optional[str]:
    after = _query(request, {"after"}).get("after")
    if after is not None and not wire.is_id(after):
        raise wire.refuse("bad-request")
    return after


def content_disposition(disposition: str, name: str) -> str:
    """RFC 6266: an ASCII fallback plus the exact UTF-8 name („Zpráva.pdf“)."""
    fallback = "".join(ch if 32 <= ord(ch) < 127 and ch not in '"\\' else "_" for ch in name) or "file"
    return f"{disposition}; filename=\"{fallback}\"; filename*=UTF-8''{quote(name, safe='')}"


def sse_frame(event: OutboxEvent) -> bytes:
    data = json.dumps(event.data, ensure_ascii=False, separators=(",", ":"))
    return f"id: {event.id}\nevent: {event.type}\ndata: {data}\n\n".encode()


# -- handlers ----------------------------------------------------------------------------------

class Handlers:
    def __init__(self, api: LazurioWebAdapter) -> None:
        self.api = api
        self._streams = 0

    async def post_message(self, request: web.Request) -> web.Response:
        inbound = wire.message(await _json_body(request, wire.MESSAGE_BODY_BYTES))
        return json_response({"message_id": await self.api.submit_message(inbound)}, 202)

    async def get_history(self, request: web.Request) -> web.Response:
        before, limit = _history_query(request)
        return json_response(await self.api.read_history(before, limit))

    async def post_clarify(self, request: web.Request) -> web.Response:
        kind, value = wire.clarify_answer(await _json_body(request, wire.SMALL_BODY_BYTES))
        await self.api.answer_clarify(request.match_info["clarify_id"], kind, value)
        return json_response({}, 202)

    async def post_stop(self, request: web.Request) -> web.Response:
        wire.empty(await _json_body(request, wire.SMALL_BODY_BYTES))
        await self.api.request_stop()
        return json_response({}, 202)

    async def get_media(self, request: web.Request) -> web.StreamResponse:
        record = await self.api.media_record(request.match_info["media_id"])
        if record is None:
            raise wire.refuse("not-found")
        # Raster images may render inline; anything else (SVG, HTML, PDF) downloads, sandboxed.
        inline = record.mime in ("image/png", "image/jpeg", "image/gif", "image/webp")
        return web.FileResponse(record.path, headers={
            "Content-Type": record.mime, "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'", "Cache-Control": "private, max-age=3600",
            "Content-Disposition": content_disposition("inline" if inline else "attachment", record.name)})

    async def get_events(self, request: web.Request) -> web.StreamResponse:
        after = _events_cursor(request)
        if self._streams >= MAX_STREAMS:
            raise wire.refuse("unavailable")
        cursor = await self._start_cursor(after)
        self._streams += 1
        try:
            return await self._stream(request, cursor)
        finally:
            self._streams -= 1

    async def _start_cursor(self, after: Optional[str]) -> int:
        """Without ``after`` the stream starts with the next event. An ``after`` the outbox cannot
        continue exactly (pruned, never minted here, or ahead of every id) is ``409 cursor-expired``:
        the client reloads the history and goes on from its snapshot."""
        outbox = self.api.outbox
        last = await asyncio.to_thread(outbox.last_id)
        if after is None:
            return last
        cursor = int(after) if after.isdigit() else None
        if cursor is None or cursor > last:
            raise wire.refuse("cursor-expired")
        following = await asyncio.to_thread(outbox.read_after, cursor, 1)
        if (following and following[0].id != cursor + 1) or (not following and cursor < last):
            raise wire.refuse("cursor-expired")
        return cursor

    async def _stream(self, request: web.Request, cursor: int) -> web.StreamResponse:
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
            while not hub.closed:
                wakeup.clear()
                batch = await asyncio.to_thread(outbox.read_after, cursor, REPLAY_BATCH)
                # Ids are assigned one by one and only pruning deletes rows: a jump past cursor + 1
                # means unread events were pruned. End the stream; the reconnect gets cursor-expired.
                if batch and batch[0].id != cursor + 1:
                    break
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
                        await response.write(b": ping\n\n")
        except ConnectionError:
            return response
        finally:
            hub.unsubscribe(wakeup)
        with contextlib.suppress(ConnectionError, RuntimeError):
            await response.write_eof()
        return response


def build_app(api: LazurioWebAdapter) -> web.Application:
    handlers = Handlers(api)
    app = web.Application(middlewares=[make_guard(api)], client_max_size=wire.MESSAGE_BODY_BYTES)
    app.router.add_post("/v1/messages", handlers.post_message)
    app.router.add_get("/v1/events", handlers.get_events, allow_head=False)
    app.router.add_get("/v1/history", handlers.get_history, allow_head=False)
    app.router.add_post("/v1/clarify/" + _ID_ROUTE % "clarify_id", handlers.post_clarify)
    app.router.add_post("/v1/stop", handlers.post_stop)
    app.router.add_get("/v1/media/" + _ID_ROUTE % "media_id", handlers.get_media, allow_head=False)
    return app
