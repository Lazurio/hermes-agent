"""The ``lazurio_web`` runtime API end to end on loopback: request gates, intake, the durable event
stream with replay, /stop, transcript pages and files. The adapter runs its real aiohttp server; the
gateway runner is replaced by a message handler, as on every adapter test."""

from __future__ import annotations

import asyncio
import base64
import sqlite3
import time
from pathlib import Path

import pytest

from hermes_constants import get_hermes_home
from tests.waiting import async_wait_until

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


def _message(client_id: str, text: str = "Ahoj", **extra):
    return {"client_id": client_id, "text": text, **extra}


# -- request gates -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wrong_or_missing_secret_is_401(web):
    async with web.running() as rig:
        for header in ("Bearer wrong-" + web.secret, "", f"Basic {web.secret}"):
            async with rig.client.get(f"{rig.base}/v1/history", headers={"Authorization": header}) as resp:
                assert resp.status == 401
                assert resp.headers["WWW-Authenticate"] == "Bearer"
                assert (await resp.json())["error"]["code"] == "unauthorized"
        async with rig.client.get(f"{rig.base}/v1/history") as resp:
            assert resp.status == 200


@pytest.mark.asyncio
async def test_foreign_host_and_origin_are_refused(web):
    async with web.running() as rig:
        port = rig.adapter.listen_port
        for host in ("evil.example", f"evil.example:{port}", "127.0.0.1:1", "[::1]:1"):
            async with rig.client.get(f"{rig.base}/v1/history", headers={"Host": host}) as resp:
                assert resp.status == 403, host
                assert (await resp.json())["error"]["code"] == "forbidden_host"
        for origin in ("https://evil.example", "null", f"http://127.0.0.1:{port + 1}", f"https://127.0.0.1:{port}"):
            async with rig.client.post(f"{rig.base}/v1/messages", json=_message("o1"),
                                       headers={"Origin": origin}) as resp:
                assert resp.status == 403, origin
                assert (await resp.json())["error"]["code"] == "forbidden_origin"
        async with rig.client.get(f"{rig.base}/v1/history", headers={
                "Host": f"localhost:{port}", "Origin": f"http://localhost:{port}"}) as resp:
            assert resp.status == 200


@pytest.mark.asyncio
async def test_connect_refuses_an_unsafe_or_short_secret_file(web, secret_file):
    secret_file.chmod(0o644)
    adapter = web.make()
    assert await adapter.connect() is False
    assert adapter._fatal_error_code == "lazurio_web_secret"
    secret_file.chmod(0o600)
    secret_file.write_text("short\n")
    adapter = web.make()
    assert await adapter.connect() is False
    assert adapter._fatal_error_code == "lazurio_web_secret"


# -- intake and turns --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_message_creates_a_turn_and_the_reply_reaches_the_stream(web):
    seen = []

    async def handler(event):
        seen.append(event)
        return f"echo: {event.text}"

    async with web.running(handler=handler) as rig:
        start = rig.adapter.outbox.last_id()
        stream = asyncio.create_task(web.read_events(
            rig.client, rig.base, after=start,
            until=lambda evs: any(e["type"] == "status" and e["data"]["state"] == "idle" for e in evs)))
        async with rig.client.post(f"{rig.base}/v1/messages", json=_message("c1", "Ahoj Buddy")) as resp:
            assert resp.status == 202
            message_id = (await resp.json())["message_id"]
        events = await stream
    assert [e["type"] for e in events] == ["turn.started", "status", "message.final", "status"]
    assert events[0]["data"] == {"message_id": message_id}
    assert events[1]["data"] == {"state": "working"}
    assert events[2]["data"]["text"] == "echo: Ahoj Buddy" and events[2]["data"]["media"] == []
    assert events[3]["data"] == {"state": "idle"}
    assert [e["id"] for e in events] == sorted({e["id"] for e in events})
    (event,) = seen
    source = event.source
    assert (source.platform.value, source.chat_id, source.chat_type, source.user_id) == (
        "lazurio_web", "owner", "dm", "owner")
    assert event.message_id == message_id
    assert rig.adapter.owner_session_key() == "agent:main:lazurio_web:dm:owner"


@pytest.mark.asyncio
async def test_a_retried_client_id_dispatches_once(web):
    seen = []

    async def handler(event):
        seen.append(event.message_id)
        return "ok"

    async with web.running(handler=handler) as rig:
        ids = []
        for _ in range(2):
            async with rig.client.post(f"{rig.base}/v1/messages", json=_message("same-client-id")) as resp:
                assert resp.status == 202
                ids.append((await resp.json())["message_id"])
        await async_wait_until(lambda: seen and not rig.adapter._active_sessions, what="the turn finished")
    assert ids[0] == ids[1]
    assert seen == [ids[0]]


@pytest.mark.asyncio
async def test_the_owner_is_refused_when_the_allowlist_does_not_admit_them(web):
    seen = []

    async def handler(event):
        seen.append(event)

    async with web.running(handler=handler, authorized=False) as rig:
        async with rig.client.post(f"{rig.base}/v1/messages", json=_message("c2")) as resp:
            assert resp.status == 403
            assert (await resp.json())["error"]["code"] == "forbidden_user"
        async with rig.client.post(f"{rig.base}/v1/stop") as resp:
            assert resp.status == 403
    assert seen == []


@pytest.mark.asyncio
async def test_invalid_bodies_are_refused(web, monkeypatch):
    from plugins.platforms.lazurio_web import http_api

    monkeypatch.setattr(http_api, "MAX_ATTACHMENT_BYTES", 16)
    async with web.running() as rig:
        cases = [
            ({"client_id": "x", "text": ""}, 400, "empty_message"),
            ({"client_id": "bad id!", "text": "hi"}, 400, "invalid_client_id"),
            ({"client_id": "x", "text": "hi", "extra": 1}, 400, "unknown_field"),
            (_message("x", attachments=[{"name": "a.png", "mime": "image/png", "data_base64": "%%%"}]),
             400, "invalid_attachment"),
            (_message("x", attachments=[{"name": "a.bin", "mime": "application/octet-stream",
                                         "data_base64": base64.b64encode(b"x" * 64).decode()}]),
             413, "attachment_too_large"),
        ]
        for body, status, code in cases:
            async with rig.client.post(f"{rig.base}/v1/messages", json=body) as resp:
                assert resp.status == status, body
                assert (await resp.json())["error"]["code"] == code


@pytest.mark.asyncio
async def test_attachments_reach_the_turn_as_cached_files(web):
    seen = []

    async def handler(event):
        seen.append(event)
        return "mám to"

    async with web.running(handler=handler) as rig:
        body = _message("c3", "Co je na obrázku?", attachments=[
            {"name": "../../etc/shot.png", "mime": "image/png", "data_base64": base64.b64encode(PNG_1X1).decode()}])
        async with rig.client.post(f"{rig.base}/v1/messages", json=body) as resp:
            assert resp.status == 202
        await async_wait_until(lambda: seen, what="the turn started")
    (event,) = seen
    assert event.message_type.value == "photo"
    assert event.media_types == ["image/png"]
    assert len(event.media_urls) == 1 and "/etc/" not in event.media_urls[0]
    assert await asyncio.to_thread(Path(event.media_urls[0]).read_bytes) == PNG_1X1


# -- the durable outbox ------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_succeeds_only_after_the_event_is_durable(web, monkeypatch):
    async with web.running() as rig:
        result = await rig.adapter.send("owner", "Hotovo.")
        assert result.success
        # A separate connection sees the committed row: the success came after the commit.
        db = sqlite3.connect(get_hermes_home() / "lazurio_web" / "outbox.db")
        try:
            rows = db.execute("SELECT type, data FROM events").fetchall()
        finally:
            db.close()
        assert ("message.final", f'{{"message_id":"{result.message_id}","text":"Hotovo.","media":[]}}') in rows

        def fail(*_args, **_kwargs):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(rig.adapter.outbox, "append", fail)
        failed = await rig.adapter.send("owner", "Ztratí se?")
        assert failed.success is False and failed.retryable is True


@pytest.mark.asyncio
async def test_replay_after_reconnect_loses_and_repeats_nothing(web):
    async with web.running() as rig:
        start = rig.adapter.outbox.last_id()
        for text in ("one", "two", "three"):
            await rig.adapter.send("owner", text)
        first = await web.read_events(rig.client, rig.base, after=start, until=lambda evs: len(evs) == 3)
        for text in ("four", "five"):
            await rig.adapter.send("owner", text)
        resumed = await web.read_events(rig.client, rig.base, after=first[-1]["id"], until=lambda evs: len(evs) == 2)
        by_header = await web.read_events(rig.client, rig.base, headers={"Last-Event-ID": str(first[-1]["id"])},
                                          until=lambda evs: len(evs) == 2)
    texts = [e["data"]["text"] for e in first + resumed]
    assert texts == ["one", "two", "three", "four", "five"]
    ids = [e["id"] for e in first + resumed]
    assert ids == list(range(ids[0], ids[0] + 5))
    assert [e["id"] for e in by_header] == [e["id"] for e in resumed]


@pytest.mark.asyncio
async def test_a_pruned_cursor_is_reported_as_a_replay_gap(web):
    async with web.running() as rig:
        start = rig.adapter.outbox.last_id()
        for text in ("old", "older"):
            await rig.adapter.send("owner", text)
        rig.adapter.outbox.prune(events_before=time.time() + 1, inbound_before=0, media_before=0)
        await rig.adapter.send("owner", "new")
        events = await web.read_events(rig.client, rig.base, after=start, until=lambda evs: len(evs) == 2)
    assert events[0] == {"id": None, "type": "error", "data": {"code": "replay_gap"}}
    assert events[1]["data"]["text"] == "new"


@pytest.mark.asyncio
async def test_every_stream_ends_by_itself(web):
    adapter = web.make()
    assert adapter.stream_max_seconds <= 110
    adapter.stream_max_seconds = 0.3
    async with web.running(adapter) as rig:
        started = time.monotonic()
        await web.read_events(rig.client, rig.base, timeout=10)
        assert time.monotonic() - started < 10


# -- stop, history, files ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stop_interrupts_the_running_turn(web):
    running = asyncio.Event()

    async def handler(event):
        if event.text == "/stop":
            return "Zastaveno."
        running.set()
        await asyncio.Event().wait()

    async with web.running(handler=handler) as rig:
        async with rig.client.post(f"{rig.base}/v1/stop") as resp:
            assert await resp.json() == {"stopped": False}
        start = rig.adapter.outbox.last_id()
        async with rig.client.post(f"{rig.base}/v1/messages", json=_message("long", "Udělej něco dlouhého")) as resp:
            assert resp.status == 202
        await asyncio.wait_for(running.wait(), 10)
        stream = asyncio.create_task(web.read_events(
            rig.client, rig.base, after=start,
            until=lambda evs: any(e["type"] == "status" and e["data"]["state"] == "idle" for e in evs)))
        async with rig.client.post(f"{rig.base}/v1/stop") as resp:
            assert resp.status == 200 and await resp.json() == {"stopped": True}
        events = await stream
    assert any(e["type"] == "message.final" and e["data"]["text"] == "Zastaveno." for e in events)
    assert not web.of_type(events, "error")  # a stopped turn is cancelled, not failed


@pytest.mark.asyncio
async def test_history_pages_hold_only_what_the_person_saw(web, tmp_path):
    from types import SimpleNamespace

    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("s1", source="lazurio_web")
    db.append_message("s1", "user", content="Ahoj", platform_message_id="m-1")
    db.append_message("s1", "assistant", content="", tool_calls=[{
        "id": "c1", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}])
    db.append_message("s1", "tool", content="ls output", tool_call_id="c1")
    db.append_message("s1", "assistant", content="Dobrý den.")
    db.append_message("s1", "user", content="[System: stream timed out]")
    db.append_message("s1", "user", content="internal wake", display_kind="background_notice")
    db.append_message("s1", "user", content="Díky", platform_message_id="m-2")
    db.append_message("s1", "assistant", content="Rádo se stalo.")
    async with web.running() as rig:
        key = rig.adapter.owner_session_key()
        rig.adapter.set_session_store(SimpleNamespace(peek_session_id=lambda k: "s1" if k == key else None, _db=db))
        async with rig.client.get(f"{rig.base}/v1/history", params={"limit": "2"}) as resp:
            newest = await resp.json()
        async with rig.client.get(f"{rig.base}/v1/history",
                                  params={"limit": "50", "before": str(newest["next_before"])}) as resp:
            older = await resp.json()
    db.close()
    assert [(m["role"], m["text"], m["message_id"]) for m in newest["messages"]] == [
        ("user", "Díky", "m-2"), ("assistant", "Rádo se stalo.", None)]
    assert newest["next_before"] == newest["messages"][0]["id"]
    assert isinstance(newest["last_event_id"], int)
    assert [(m["role"], m["text"]) for m in older["messages"]] == [("user", "Ahoj"), ("assistant", "Dobrý den.")]
    assert older["next_before"] is None


@pytest.mark.asyncio
async def test_files_buddy_sends_are_listed_and_served(web, tmp_path):
    report = tmp_path / "out.pdf"
    report.write_bytes(b"%PDF-1.4 test")
    async with web.running() as rig:
        start = rig.adapter.outbox.last_id()
        result = await rig.adapter.send_document("owner", str(report), caption="Zpráva", file_name="report.pdf")
        assert result.success
        report.unlink()  # the channel keeps its own copy
        (event,) = await web.read_events(rig.client, rig.base, after=start, until=lambda evs: len(evs) == 1)
        (media,) = event["data"]["media"]
        assert (event["type"], event["data"]["text"]) == ("message.final", "Zpráva")
        assert (media["kind"], media["name"], media["mime"], media["size"]) == (
            "file", "report.pdf", "application/pdf", 13)
        async with rig.client.get(f"{rig.base}/v1/media/{media['id']}") as resp:
            assert resp.status == 200
            assert await resp.read() == b"%PDF-1.4 test"
            assert resp.headers["Content-Disposition"].startswith("attachment;")
            assert resp.headers["X-Content-Type-Options"] == "nosniff"
        async with rig.client.get(f"{rig.base}/v1/media/unknown") as resp:
            assert resp.status == 404
