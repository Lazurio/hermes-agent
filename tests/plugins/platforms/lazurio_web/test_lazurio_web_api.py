"""The ``lazurio_web`` runtime API end to end on loopback: admission, intake, the durable event
stream with replay and cursor expiry, /stop, transcript pages and files. The adapter runs its real
aiohttp server; the gateway runner is replaced by a message handler, as on every adapter test.
Expectations are written from LazurioPlatform's contract text (``src/buddy/contract/README.md``)."""

from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
import time
from pathlib import Path

import pytest

from hermes_constants import get_hermes_home
from tests.waiting import async_wait_until

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
PAGE = {"environment": {"id": "vm-01.acme", "kind": "work", "name": "vm-01"},
        "organization": {"slug": "acme", "name": "Acme s.r.o."}, "app": "apps",
        "page": {"path": "/files/Smlouvy"}, "guide": {"explainer": "files", "step": 2}}


def _message(client_id: str, text: str = "Ahoj", *, attachments=None, context=None):
    return {"client_id": client_id, "text": text, "attachments": attachments or [], "context": context}


async def _error(resp) -> tuple[int, dict]:
    return resp.status, await resp.json()


def _code(code: str) -> dict:
    return {"error": {"code": code}}


def _transcript_store(adapter, tmp_path, *rows):
    """A session store whose transcript holds ``rows`` of ``(role, content, platform_message_id)``."""
    from types import SimpleNamespace

    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("s1", source="lazurio_web")
    for role, content, platform_message_id in rows:
        db.append_message("s1", role, content=content, platform_message_id=platform_message_id)
    key = adapter.owner_session_key()
    adapter.set_session_store(SimpleNamespace(peek_session_id=lambda k: "s1" if k == key else None, _db=db))
    return db


# -- admission ---------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_admission_runs_in_the_contracts_order(web):
    async with web.running() as rig:
        port, url = rig.adapter.listen_port, f"{rig.base}/v1/history"
        for host in (f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"):
            async with rig.client.get(url, headers={"Host": host}) as resp:
                assert resp.status == 200, host
        forbidden = [{"Host": "buddy.jana.lazurio.io"}, {"Host": f"127.0.0.1:{port + 1}"},
                     {"Host": "127.0.0.1"}, {"Origin": f"http://127.0.0.1:{port}"},
                     {"Origin": "https://evil.example"}, {"Sec-Fetch-Site": "same-origin"}]
        for headers in forbidden:
            async with rig.client.get(url, headers=headers) as resp:
                assert await _error(resp) == (403, _code("forbidden")), headers
        for header in ("", "Bearer wrong-" + web.secret, f"Bearer {web.secret}x", "Bearer lazurio",
                       f"Basic {web.secret}", f"bearer {web.secret}"):
            async with rig.client.get(url, headers={"Authorization": header}) as resp:
                assert await _error(resp) == (401, _code("unauthorized")), header
        async with rig.client.get(f"{rig.base}/v1/nothing", headers={"Authorization": ""}) as resp:
            assert await _error(resp) == (401, _code("unauthorized"))  # no route is named before the secret
        for method, path, status, code in (("GET", "/v1/nothing", 404, "not-found"),
                                           ("POST", "/v1/clarify/", 404, "not-found"),
                                           ("POST", "/v1/clarify/q-1/x", 404, "not-found"),
                                           ("GET", "/v1/messages", 405, "method-not-allowed")):
            async with rig.client.request(method, f"{rig.base}{path}") as resp:
                assert await _error(resp) == (status, _code(code)), path


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


@pytest.mark.asyncio
async def test_a_rotated_secret_takes_effect_without_a_restart(web, secret_file):
    async with web.running() as rig:
        rotated = "rotated-secret-" + "f" * 40
        secret_file.write_text(rotated + "\n")
        async with rig.client.get(f"{rig.base}/v1/history") as resp:
            assert resp.status == 401
        async with rig.client.get(f"{rig.base}/v1/history", headers={"Authorization": f"Bearer {rotated}"}) as resp:
            assert resp.status == 200
        secret_file.chmod(0o644)  # turned unsafe: nobody gets in until it is fixed
        async with rig.client.get(f"{rig.base}/v1/history", headers={"Authorization": f"Bearer {rotated}"}) as resp:
            assert resp.status == 401


# -- intake and turns --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_message_creates_a_turn_and_both_messages_reach_the_stream(web):
    seen = []

    async def handler(event):
        seen.append(event)
        return f"echo: {event.text}"

    async with web.running(handler=handler) as rig:
        start = rig.adapter.outbox.last_id()
        stream = asyncio.create_task(web.read_events(
            rig.client, rig.base, after=start,
            until=lambda evs: any(e["type"] == "status" and e["data"]["state"] == "idle" for e in evs)))
        async with rig.client.post(f"{rig.base}/v1/messages", json=_message("c-01HZY3", "Ahoj Buddy")) as resp:
            assert resp.status == 202
            body = await resp.json()
        events = await stream
    message_id = body["message_id"]
    assert list(body) == ["message_id"]
    assert [e["type"] for e in events] == ["message.final", "turn.started", "status", "message.final", "status"]
    mine, started, working, reply, idle = (e["data"] for e in events)
    assert (mine["message_id"], mine["role"], mine["text"], mine["client_id"], mine["via"]) == (
        message_id, "user", "Ahoj Buddy", "c-01HZY3", "lazurio_web")
    assert started == {"reply_to": message_id} and working == {"state": "working"} and idle == {"state": "idle"}
    assert (reply["role"], reply["text"], reply["client_id"], reply["media"]) == (
        "assistant", "echo: Ahoj Buddy", None, [])
    assert [int(e["id"]) for e in events] == sorted({int(e["id"]) for e in events})
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
async def test_a_retry_recovers_a_claim_left_by_a_gateway_that_died(web, tmp_path):
    """Review findings: the claim used to outlive the in-memory dispatch, so after a crash the retry
    got the old id and nothing ran; then a claim older than an hour was taken as settled by age. A
    claim this process never dispatched and Hermes never persisted is dispatched again, under the
    same id and however old; once dispatched, a retry stays a no-op."""
    seen = []

    async def handler(event):
        seen.append(event.message_id)
        return "ok"

    async with web.running(handler=handler) as rig:
        db = _transcript_store(rig.adapter, tmp_path, ("user", "Uloženo", "m-persisted"))
        outbox = rig.adapter.outbox
        outbox.claim_inbound("lost-client", "m-lost")  # the previous gateway's claims
        outbox.claim_inbound("aged-client", "m-aged")
        outbox.claim_inbound("kept-client", "m-persisted")
        with outbox._lock:  # a gateway that stayed down for a day
            outbox._conn.execute("UPDATE inbound SET created_at = ? WHERE client_id = 'aged-client'",
                                 (time.time() - 24 * 3600,))
        for client_id, expected in (("lost-client", "m-lost"), ("lost-client", "m-lost"),
                                    ("aged-client", "m-aged"), ("kept-client", "m-persisted")):
            async with rig.client.post(f"{rig.base}/v1/messages", json=_message(client_id)) as resp:
                assert resp.status == 202
                assert (await resp.json())["message_id"] == expected
        await async_wait_until(lambda: len(seen) == 2 and not rig.adapter._active_sessions,
                               what="both recovered turns finished")
    db.close()
    assert seen == ["m-lost", "m-aged"]


@pytest.mark.asyncio
async def test_history_and_retry_dedupe_survive_an_in_place_compaction(web, tmp_path):
    """Review finding: an in-place compaction archives earlier rows (active=0, compacted=1); the
    active-only read lost them from the history and let a retry of a processed message run again."""
    seen = []

    async def handler(event):
        seen.append(event.message_id)
        return "ok"

    async with web.running(handler=handler) as rig:
        db = _transcript_store(rig.adapter, tmp_path, ("user", "Ahoj", "m-confirmed"),
                               ("assistant", "Dobrý den.", None))
        db.archive_and_compact("s1", [{"role": "user", "content": "[summary]", "_compressed_summary": True}])
        db.append_message("s1", "user", content="Ještě něco", platform_message_id="m-later")
        rig.adapter.outbox.claim_inbound("confirmed-client", "m-confirmed")  # a previous gateway's claim
        async with rig.client.post(f"{rig.base}/v1/messages", json=_message("confirmed-client")) as resp:
            assert resp.status == 202 and (await resp.json())["message_id"] == "m-confirmed"
        async with rig.client.get(f"{rig.base}/v1/history", params={"limit": "2"}) as resp:
            newest = await resp.json()
        async with rig.client.get(f"{rig.base}/v1/history", params={"before": newest["before"]}) as resp:
            older = await resp.json()
    db.close()
    assert seen == []  # already processed before the compaction: not run again
    assert [(m["text"], m["role"]) for m in newest["messages"]] == [("Dobrý den.", "assistant"), ("Ještě něco", "user")]
    assert newest["messages"][1]["message_id"] == "m-later"
    assert [(m["text"], m["message_id"]) for m in older["messages"]] == [("Ahoj", "m-confirmed")]
    assert older["before"] is None and older["snapshot"] is None


@pytest.mark.asyncio
async def test_a_concurrent_retry_is_never_acknowledged_for_a_failed_dispatch(web, monkeypatch):
    """Review finding: a retry racing a first attempt that then failed used to get 202 for a claim
    nothing ran under. Intake is serialized, and a failed dispatch leaves the claim to be dispatched
    again under the same id, so the 202 always names a message that runs."""
    seen = []

    async def handler(event):
        seen.append(event.message_id)
        return "ok"

    async with web.running(handler=handler) as rig:
        real_handle = rig.adapter.handle_message
        calls = []

        async def flaky_handle(event):
            calls.append(event.message_id)
            if len(calls) == 1:
                raise RuntimeError("dispatch failed")
            await real_handle(event)

        monkeypatch.setattr(rig.adapter, "handle_message", flaky_handle)

        async def post():
            async with rig.client.post(f"{rig.base}/v1/messages", json=_message("racing")) as resp:
                return resp.status, await resp.json()

        answers = await asyncio.gather(post(), post())
        await async_wait_until(lambda: seen and not rig.adapter._active_sessions, what="the turn finished")
    assert sorted(status for status, _ in answers) == [202, 500]
    assert [body for status, body in answers if status == 500] == [_code("internal")]
    (acknowledged,) = [body["message_id"] for status, body in answers if status == 202]
    assert seen == [acknowledged] and calls == [acknowledged, acknowledged]


@pytest.mark.asyncio
async def test_the_owner_is_refused_when_the_allowlist_does_not_admit_them(web):
    seen = []

    async def handler(event):
        seen.append(event)

    async with web.running(handler=handler, authorized=False) as rig:
        async with rig.client.post(f"{rig.base}/v1/messages", json=_message("c2")) as resp:
            assert await _error(resp) == (503, _code("unavailable"))
        async with rig.client.post(f"{rig.base}/v1/stop", json={}) as resp:
            assert await _error(resp) == (503, _code("unavailable"))
    assert seen == []


@pytest.mark.asyncio
async def test_defective_messages_are_refused_whole(web, monkeypatch):
    """The contract's message rules, each case refused as ``400 bad-request``; a defective context
    refuses the message too (review finding: a wrong type there once raised)."""
    from plugins.platforms.lazurio_web import wire

    monkeypatch.setattr(wire, "ATTACHMENT_BYTES", 16)

    def attach(**changes):
        return {"name": "a.txt", "mime": "text/plain", "data_base64": "YQ==", **changes}

    cases = [
        {"text": "Ahoj", "attachments": [], "context": None},  # no client_id
        {**_message("c-8"), "lang": "cs"},
        _message(""), _message("c 9"), _message("c" * 65), _message("c-10", "a" * 16_001),
        _message("c-11", "a\x00b"), _message("c-13", ""), _message("c-14", "  \n\t "),
        {**_message("c-15"), "attachments": None},
        _message("c-16", "", attachments=[attach(name=f"{i}.txt") for i in range(5)]),
        _message("c-17", "", attachments=[attach(path="/etc/passwd")]),
        _message("c-18", "", attachments=[attach(name="../a.txt")]),
        _message("c-19", "", attachments=[attach(name="..")]),
        _message("c-20", "", attachments=[attach(mime="Image/PNG")]),
        _message("c-21", "", attachments=[attach(mime="text/plain; charset=utf-8")]),
        _message("c-22", "", attachments=[attach(data_base64="@@@@")]),
        _message("c-23", "", attachments=[attach(data_base64="YQ")]),
        _message("c-24", "", attachments=[attach(data_base64="-_-_")]),
        _message("c-25", "Prázdný", attachments=[attach(data_base64="")]),
        _message("c-26", "", attachments=[attach(data_base64=base64.b64encode(b"x" * 17).decode())]),
        _message("c-28", context={**PAGE, "app": "module"}),
        _message("c-29", context={**PAGE, "environment": {"id": "a", "kind": [], "name": "b"}}),
        [], "Ahoj",
    ]
    async with web.running() as rig:
        for body in cases:
            async with rig.client.post(f"{rig.base}/v1/messages", json=body) as resp:
                assert await _error(resp) == (400, _code("bad-request")), body
        lone = '{"client_id":"c-12","text":"a\\ud800b","attachments":[],"context":null}'
        async with rig.client.post(f"{rig.base}/v1/messages", data=lone,
                                   headers={"Content-Type": "application/json"}) as resp:
            assert await _error(resp) == (400, _code("bad-request"))


@pytest.mark.asyncio
async def test_bodies_over_their_bound_are_refused(web, monkeypatch):
    from plugins.platforms.lazurio_web import wire

    monkeypatch.setattr(wire, "SMALL_BODY_BYTES", 32)
    async with web.running() as rig:
        async with rig.client.post(f"{rig.base}/v1/stop", data=json.dumps({"x": "y" * 64}),
                                   headers={"Content-Type": "application/json"}) as resp:
            assert await _error(resp) == (413, _code("payload-too-large"))
        async with rig.client.post(f"{rig.base}/v1/messages",
                                   data=b"{" + b" " * (wire.MESSAGE_BODY_BYTES + 1) + b"}",
                                   headers={"Content-Type": "application/json"}) as resp:
            assert await _error(resp) == (413, _code("payload-too-large"))


@pytest.mark.asyncio
async def test_attachments_reach_the_turn_and_every_tab(web):
    seen = []

    async def handler(event):
        seen.append(event)
        return "mám to"

    async with web.running(handler=handler) as rig:
        start = rig.adapter.outbox.last_id()
        body = _message("c-3", "Co je na obrázku?", attachments=[
            {"name": "screenshot.png", "mime": "image/png", "data_base64": base64.b64encode(PNG_1X1).decode()}])
        async with rig.client.post(f"{rig.base}/v1/messages", json=body) as resp:
            assert resp.status == 202
        await async_wait_until(lambda: seen, what="the turn started")
        events = await web.read_events(rig.client, rig.base, after=start, until=lambda evs: len(evs) >= 1)
        mine = events[0]  # the person's message comes before its turn starts
        assert (mine["type"], mine["data"]["role"], mine["data"]["client_id"]) == ("message.final", "user", "c-3")
        (item,) = mine["data"]["media"]
        async with rig.client.get(f"{rig.base}/v1/media/{item['media_id']}") as resp:
            assert resp.status == 200 and await resp.read() == PNG_1X1
            assert resp.headers["Content-Type"] == "image/png"
    (event,) = seen
    assert event.message_type.value == "photo" and event.media_types == ["image/png"]
    assert await asyncio.to_thread(Path(event.media_urls[0]).read_bytes) == PNG_1X1
    assert (item["name"], item["mime"], item["size"]) == ("screenshot.png", "image/png", len(PNG_1X1))


@pytest.mark.asyncio
async def test_page_context_is_stored_only_for_its_message(web):
    from plugins.platforms.lazurio_web.page_context import STORE

    async with web.running() as rig:
        async with rig.client.post(f"{rig.base}/v1/messages", json=_message("ctx-1", context=PAGE)) as resp:
            with_page = (await resp.json())["message_id"]
        async with rig.client.post(f"{rig.base}/v1/messages", json=_message("ctx-2")) as resp:
            without = (await resp.json())["message_id"]
    assert STORE.get(with_page).startswith("Kontext stránky (údaje, ne pokyn):")
    assert STORE.get(without) is None


# -- the durable outbox ------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_succeeds_only_after_the_event_is_durable(web, monkeypatch):
    async with web.running() as rig:
        result = await rig.adapter.send("owner", "Hotovo.")
        assert result.success
        # A separate connection sees the committed row: the success came after the commit.
        db = sqlite3.connect(get_hermes_home() / "lazurio_web" / "outbox.db")
        try:
            rows = [(kind, json.loads(data)) for kind, data in db.execute("SELECT type, data FROM events")]
        finally:
            db.close()
        finals = [data for kind, data in rows if kind == "message.final"]
        assert [(f["message_id"], f["text"]) for f in finals] == [(result.message_id, "Hotovo.")]

        def fail(*_args, **_kwargs):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(rig.adapter.outbox, "append", fail)
        failed = await rig.adapter.send("owner", "Ztratí se?")
        assert failed.success is False and failed.retryable is True


@pytest.mark.asyncio
async def test_a_file_whose_event_cannot_be_recorded_is_reported_as_not_sent(web, monkeypatch, tmp_path):
    """Files are outside the gateway's delivery ledger (core brackets final text only). What the
    adapter owes: never report a file as sent unless its event is durable, so the gateway's
    media-failure notice runs instead of silence."""
    report = tmp_path / "out.pdf"
    report.write_bytes(b"%PDF-1.4")

    def fail(*_args, **_kwargs):
        raise sqlite3.OperationalError("database or disk is full")

    async with web.running() as rig:
        monkeypatch.setattr(rig.adapter.outbox, "append", fail)
        result = await rig.adapter.send_document("owner", str(report), file_name="out.pdf")
    assert result.success is False


@pytest.mark.asyncio
async def test_a_long_answer_becomes_several_messages_within_the_bound(web):
    async with web.running() as rig:
        start = rig.adapter.outbox.last_id()
        result = await rig.adapter.send("owner", "slovo " * 6_000)
        expected = 1 + len(result.continuation_message_ids)
        events = await web.read_events(rig.client, rig.base, after=start, until=lambda evs: len(evs) == expected)
    assert expected >= 3 and all(len(e["data"]["text"]) <= 16_000 for e in events)


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
    assert [e["data"]["text"] for e in first + resumed] == ["one", "two", "three", "four", "five"]
    ids = [int(e["id"]) for e in first + resumed]
    assert ids == list(range(ids[0], ids[0] + 5))


@pytest.mark.asyncio
async def test_a_cursor_the_outbox_cannot_continue_is_expired(web):
    async with web.running() as rig:
        start = rig.adapter.outbox.last_id()
        for text in ("old", "older"):
            await rig.adapter.send("owner", text)
        rig.adapter.outbox.prune(events_before=time.time() + 1, inbound_before=0, media_before=0)
        await rig.adapter.send("owner", "new")
        last = rig.adapter.outbox.last_id()
        for after in (start, "e-981", last + 5):  # pruned, never minted here, ahead of every id
            async with rig.client.get(f"{rig.base}/v1/events", params={"after": str(after)}) as resp:
                assert await _error(resp) == (409, _code("cursor-expired")), after
        for query in ("after=", "after=1&after=2", "since=1"):
            async with rig.client.get(f"{rig.base}/v1/events?{query}") as resp:
                assert await _error(resp) == (400, _code("bad-request")), query
        (event,) = await web.read_events(rig.client, rig.base, after=last - 1, until=lambda evs: len(evs) == 1)
    assert event["data"]["text"] == "new"


@pytest.mark.asyncio
async def test_a_prune_between_replay_batches_ends_the_stream_instead_of_skipping(web, monkeypatch):
    """Review finding: a prune between two replay batches skipped events silently. The stream ends
    where events are missing, and the reconnect from there is ``409 cursor-expired``."""
    from plugins.platforms.lazurio_web import http_api

    monkeypatch.setattr(http_api, "REPLAY_BATCH", 2)
    async with web.running() as rig:
        outbox = rig.adapter.outbox
        start = outbox.last_id()
        for text in ("1", "2", "3", "4", "5", "6"):
            await rig.adapter.send("owner", text)
        real_read = outbox.read_after

        def read_then_prune(after, limit):
            batch = real_read(after, limit)
            if after == start:  # the hourly prune lands right after the first batch
                with outbox._lock:
                    outbox._conn.execute("DELETE FROM events WHERE id IN (?, ?)", (start + 3, start + 4))
            return batch

        monkeypatch.setattr(outbox, "read_after", read_then_prune)
        events = await web.read_events(rig.client, rig.base, after=start)  # ends by itself
        async with rig.client.get(f"{rig.base}/v1/events", params={"after": events[-1]["id"]}) as resp:
            assert await _error(resp) == (409, _code("cursor-expired"))
    assert [e["data"]["text"] for e in events] == ["1", "2"]


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
    seen = []

    async def handler(event):
        seen.append(event.text)
        if event.text == "/stop":
            return "Zastaveno."
        running.set()
        await asyncio.Event().wait()

    async with web.running(handler=handler) as rig:
        async with rig.client.post(f"{rig.base}/v1/stop", json={}) as resp:
            assert resp.status == 202 and await resp.json() == {}
        assert seen == []  # nothing ran, nothing to stop
        for body in ({"now": True}, [], None):
            async with rig.client.post(f"{rig.base}/v1/stop", json=body) as resp:
                assert await _error(resp) == (400, _code("bad-request"))
        start = rig.adapter.outbox.last_id()
        async with rig.client.post(f"{rig.base}/v1/messages", json=_message("long", "Udělej něco dlouhého")) as resp:
            assert resp.status == 202
        await asyncio.wait_for(running.wait(), 10)
        stream = asyncio.create_task(web.read_events(
            rig.client, rig.base, after=start,
            until=lambda evs: any(e["type"] == "status" and e["data"]["state"] == "idle" for e in evs)))
        async with rig.client.post(f"{rig.base}/v1/stop", json={}) as resp:
            assert resp.status == 202 and await resp.json() == {}
        events = await stream
    assert any(e["type"] == "message.final" and e["data"]["text"] == "Zastaveno." for e in events)
    assert not web.of_type(events, "error")  # a stopped turn is cancelled, not failed


@pytest.mark.asyncio
async def test_history_pages_follow_the_contract(web, tmp_path):
    async with web.running() as rig:
        db = _transcript_store(rig.adapter, tmp_path, ("user", "Ahoj", "m-1"))
        db.append_message("s1", "assistant", content="", tool_calls=[{
            "id": "c1", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}])
        db.append_message("s1", "tool", content="ls output", tool_call_id="c1")
        db.append_message("s1", "assistant", content="Dobrý den.")
        db.append_message("s1", "user", content="[System: stream timed out]")
        db.append_message("s1", "user", content="internal wake", display_kind="background_notice")
        db.append_message("s1", "user", content="Díky", platform_message_id="m-2")
        reply = await rig.adapter.send("owner", "Rádo se stalo.")  # delivered, then persisted
        db.append_message("s1", "assistant", content="Rádo se stalo.")
        async with rig.client.get(f"{rig.base}/v1/history", params={"limit": "2"}) as resp:
            newest = await resp.json()
        async with rig.client.get(f"{rig.base}/v1/history", params={"before": newest["before"]}) as resp:
            older = await resp.json()
        async with rig.client.get(f"{rig.base}/v1/history") as resp:
            whole = await resp.json()
        for query in ("limit=0", "limit=101", "limit=030", "limit=-1", "before=", "before=a&before=b",
                      "page=2", "before=c%20d"):
            async with rig.client.get(f"{rig.base}/v1/history?{query}") as resp:
                assert await _error(resp) == (400, _code("bad-request")), query
        async with rig.client.get(f"{rig.base}/v1/history?before=c%3A10&limit=100") as resp:
            assert await _error(resp) == (404, _code("not-found"))  # well-formed, but never minted here
    db.close()
    for page in (newest, older, whole):
        assert set(page) == {"messages", "before", "snapshot"}
        for message in page["messages"]:
            web.assert_message(message)
    assert [(m["role"], m["text"], m["message_id"]) for m in newest["messages"]] == [
        ("user", "Díky", "m-2"), ("assistant", "Rádo se stalo.", reply.message_id)]
    assert newest["snapshot"]["event_id"] == str(int(newest["snapshot"]["event_id"]))
    assert newest["snapshot"]["state"] == "idle" and newest["snapshot"]["clarify"] is None
    assert [(m["role"], m["text"]) for m in older["messages"]] == [("user", "Ahoj"), ("assistant", "Dobrý den.")]
    assert older["before"] is None and older["snapshot"] is None
    assert older["messages"][1]["message_id"].startswith("h")  # no event delivered it any more
    assert len(whole["messages"]) == 4 and whole["before"] is None and whole["snapshot"] is not None


@pytest.mark.asyncio
async def test_files_buddy_sends_are_listed_and_served(web, tmp_path):
    report = tmp_path / "out.pdf"
    report.write_bytes(b"%PDF-1.4 test")
    async with web.running() as rig:
        start = rig.adapter.outbox.last_id()
        result = await rig.adapter.send_document("owner", str(report), caption="Zpráva", file_name="Zpráva.pdf")
        assert result.success
        report.unlink()  # the channel keeps its own copy
        (event,) = await web.read_events(rig.client, rig.base, after=start, until=lambda evs: len(evs) == 1)
        (media,) = event["data"]["media"]
        assert (event["type"], event["data"]["text"], event["data"]["role"]) == ("message.final", "Zpráva", "assistant")
        assert (media["name"], media["mime"], media["size"]) == ("Zpráva.pdf", "application/pdf", 13)
        async with rig.client.get(f"{rig.base}/v1/media/{media['media_id']}") as resp:
            assert resp.status == 200
            assert await resp.read() == b"%PDF-1.4 test"
            assert resp.headers["Content-Disposition"] == (
                "attachment; filename=\"Zpr_va.pdf\"; filename*=UTF-8''Zpr%C3%A1va.pdf")
            assert resp.headers["X-Content-Type-Options"] == "nosniff"
        async with rig.client.get(f"{rig.base}/v1/media/unknown") as resp:
            assert await _error(resp) == (404, _code("not-found"))
