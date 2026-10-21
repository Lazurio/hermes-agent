"""Harness for the ``lazurio_web`` plugin tests: a connected adapter on a free loopback port, an
authenticated aiohttp client, and a server-sent-events reader. Nothing leaves 127.0.0.1."""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import json
import socket
from types import SimpleNamespace

import pytest

from gateway.config import PlatformConfig

SECRET = "lazurio-web-test-secret-" + "0123456789abcdef" * 2


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _parse_frame(frame: str):
    event = {"id": None, "type": "message", "data": None}
    for line in frame.split("\n"):
        if not line or line.startswith(":"):
            continue
        name, _, value = line.partition(": ")
        if name == "id":
            event["id"] = int(value)
        elif name == "event":
            event["type"] = value
        elif name == "data":
            event["data"] = json.loads(value)
    return event if event["data"] is not None else None


async def read_events(client, base: str, *, after=None, headers=None, until=None, timeout: float = 10.0):
    """Events of one ``GET /v1/events`` stream, until ``until(events)`` holds or the server ends it."""
    params = {} if after is None else {"after": str(after)}
    events: list[dict] = []

    async def _read() -> None:
        async with client.get(f"{base}/v1/events", params=params, headers=headers) as response:
            assert response.status == 200, await response.text()
            decoder, buffer = codecs.getincrementaldecoder("utf-8")(), ""
            async for chunk in response.content.iter_any():
                buffer += decoder.decode(chunk)
                while "\n\n" in buffer:
                    frame, buffer = buffer.split("\n\n", 1)
                    parsed = _parse_frame(frame)
                    if parsed is not None:
                        events.append(parsed)
                if until is not None and until(events):
                    return

    await asyncio.wait_for(_read(), timeout)
    return events


def of_type(events, *types):
    return [event for event in events if event["type"] in types]


async def _echo(event):
    return f"echo: {event.text}"


@pytest.fixture
def secret_file(tmp_path):
    path = tmp_path / "lazurio-web.secret"
    path.write_text(SECRET + "\n")
    path.chmod(0o600)
    return path


@pytest.fixture
def web(secret_file, monkeypatch):
    monkeypatch.delenv("LAZURIO_WEB_SECRET_FILE", raising=False)
    from plugins.platforms.lazurio_web import page_context
    from plugins.platforms.lazurio_web.adapter import LazurioWebAdapter

    page_context.STORE.clear()

    def make(**extra):
        config = PlatformConfig(enabled=True, extra={
            "listen_port": _free_port(), "secret_file": str(secret_file), **extra})
        return LazurioWebAdapter(config)

    @contextlib.asynccontextmanager
    async def running(adapter=None, *, handler=None, authorized=True):
        import aiohttp

        adapter = adapter or make()
        adapter.set_message_handler(handler or _echo)
        adapter.set_authorization_check(
            lambda user_id, chat_type=None, chat_id=None, **_: authorized and user_id == "owner")
        assert await adapter.connect(), adapter._fatal_error_message
        base = f"http://127.0.0.1:{adapter.listen_port}"
        async with aiohttp.ClientSession(headers={"Authorization": f"Bearer {SECRET}"}) as client:
            try:
                yield SimpleNamespace(adapter=adapter, client=client, base=base)
            finally:
                await adapter.cancel_background_tasks()
                await adapter.disconnect()

    return SimpleNamespace(make=make, running=running, read_events=read_events, of_type=of_type, secret=SECRET)
