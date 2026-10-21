"""Harness for the ``lazurio_web`` plugin tests: a connected adapter on a free loopback port, an
authenticated aiohttp client, and a server-sent-events reader that holds every event to the shapes
of LazurioPlatform's ``lazurio.buddy-runtime.v1``. The shapes here are written from the contract's
text, not copied from its vectors. Nothing leaves 127.0.0.1."""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import json
import re
import socket
from datetime import datetime
from types import SimpleNamespace

import pytest

from gateway.config import PlatformConfig

SECRET = "lazurio-web-test-secret-" + "0123456789abcdef" * 2
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")
INSTANT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z")
EVENT_KEYS = {
    "turn.started": {"reply_to"},
    "message.delta": {"message_id", "text"},
    "message.final": {"message_id", "role", "text", "at", "via", "media", "client_id"},
    "progress": {"text"},
    "status": {"state"},
    "clarify.request": {"clarify_id", "question", "choices", "multi"},
    "clarify.retired": {"clarify_id"},
    "error": {"code", "message_id"},
}
MESSAGE_KEYS = {"message_id", "role", "text", "at", "via", "media"}
MEDIA_KEYS = {"media_id", "name", "mime", "size"}


def is_id(value) -> bool:
    return isinstance(value, str) and ID.fullmatch(value) is not None


def assert_message(message: dict, *, final: bool = False) -> None:
    """A conversation message as history lists it and ``message.final`` delivers it."""
    assert set(message) == MESSAGE_KEYS | ({"client_id"} if final else set()), message
    assert is_id(message["message_id"]) and message["role"] in ("user", "assistant"), message
    assert isinstance(message["text"], str) and len(message["text"]) <= 16_000, message
    assert INSTANT.fullmatch(message["at"]) and re.fullmatch(r"[a-z][a-z0-9_]{0,31}", message["via"]), message
    datetime.strptime(message["at"][:19], "%Y-%m-%dT%H:%M:%S")  # a day that exists
    for item in message["media"]:
        assert set(item) == MEDIA_KEYS and is_id(item["media_id"]) and item["size"] >= 0, item
    if final:
        assert (message["client_id"] is None) == (message["role"] == "assistant"), message


def _clarify_ok(data: dict) -> bool:
    choices = data["choices"]
    return (bool(data["question"]) and len(choices) <= 8 and len(set(choices)) == len(choices)
            and all(choice.strip() and "\n" not in choice and len(choice) <= 200 for choice in choices)
            and not (data["multi"] and not choices))


# event type -> what its data must satisfy beyond its exact members
_EVENT_RULES = {
    "turn.started": lambda data: data["reply_to"] is None or is_id(data["reply_to"]),
    "message.delta": lambda data: is_id(data["message_id"]) and data["text"] != "",
    "progress": lambda data: data["text"].strip() != "" and "\n" not in data["text"] and len(data["text"]) <= 300,
    "status": lambda data: data["state"] in ("idle", "working", "waiting"),
    "clarify.request": _clarify_ok,
    "clarify.retired": lambda data: is_id(data["clarify_id"]),
    "error": lambda data: data["code"] in ("turn-failed", "unavailable", "internal"),
}


def assert_event(event: dict) -> None:
    """One event of the stream: its own id, a known type and exactly that type's members."""
    data = event["data"]
    assert is_id(event["id"]), event
    assert set(data) == EVENT_KEYS[event["type"]], event
    if event["type"] == "message.final":
        assert_message(data, final=True)
    else:
        assert _EVENT_RULES[event["type"]](data), event


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
            event["id"] = value
        elif name == "event":
            event["type"] = value
        elif name == "data":
            event["data"] = json.loads(value)
    return event if event["data"] is not None else None


async def read_events(client, base: str, *, after=None, until=None, timeout: float = 10.0):
    """Events of one ``GET /v1/events`` stream, until ``until(events)`` holds or the server ends it."""
    params = {} if after is None else {"after": str(after)}
    events: list[dict] = []

    async def _read() -> None:
        async with client.get(f"{base}/v1/events", params=params) as response:
            assert response.status == 200, await response.text()
            decoder, buffer = codecs.getincrementaldecoder("utf-8")(), ""
            async for chunk in response.content.iter_any():
                buffer += decoder.decode(chunk)
                while "\n\n" in buffer:
                    frame, buffer = buffer.split("\n\n", 1)
                    parsed = _parse_frame(frame)
                    if parsed is not None:
                        assert_event(parsed)
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

    return SimpleNamespace(make=make, running=running, read_events=read_events, of_type=of_type, secret=SECRET,
                           assert_message=assert_message)
