"""``lazurio_web`` against the real gateway machinery: the stream consumer's send/edit protocol, the
turn runner's clarify callback, the runner's allowlist, and the session variables tools inherit."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest

from tests.waiting import async_wait_until


# -- streaming ---------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_streamed_reply_arrives_as_stable_pieces_then_one_final(web):
    """The consumer edits with whole snapshots that end in a cursor and close an open code fence that
    later moves. The pieces carry neither, join into a prefix of the answer, and one final replaces
    them."""
    from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig

    answer = "Tady je oprava:\n```py\nprint('ahoj')\n```\nHotovo."
    parts = ["Tady je ", "oprava:\n```py\n", "print(", "'ahoj')\n", "```\nHot", "ovo."]
    async with web.running() as rig:
        outbox = rig.adapter.outbox
        start = outbox.last_id()
        config = StreamConsumerConfig(edit_interval=0.01, buffer_threshold=1, cursor=" ▉", transport="edit",
                                      chat_type="dm")
        consumer = GatewayStreamConsumer(rig.adapter, "owner", config)
        task = asyncio.create_task(consumer.run())
        so_far = ""
        for part in parts:  # each part lands as its own snapshot before the next one is written
            so_far += part
            consumer.on_delta(part)
            await async_wait_until(
                lambda so_far=so_far: any(so_far in snapshot for snapshot, _ in rig.adapter._streams.values()),
                what="the snapshot with the new part")
        consumer.finish()
        await asyncio.wait_for(task, 10)
        events = await web.read_events(rig.client, rig.base, after=start,
                                       until=lambda evs: any(e["type"] == "message.final" for e in evs))
    pieces, finals = web.of_type(events, "message.delta"), web.of_type(events, "message.final")
    joined = "".join(piece["data"]["text"] for piece in pieces)
    assert pieces and answer.startswith(joined)
    assert "▉" not in joined
    (final,) = finals
    message_id = final["data"]["message_id"]
    assert (final["data"]["text"], final["data"]["role"], final["data"]["client_id"]) == (answer, "assistant", None)
    assert {piece["data"]["message_id"] for piece in pieces} == {message_id}
    assert consumer.final_response_sent is True


@pytest.mark.asyncio
async def test_a_piece_that_was_not_committed_is_sent_again_without_the_cursor(web, monkeypatch):
    """A failed commit leaves the stream as it was. The retry of the same snapshot sends the same
    piece, not the snapshot's cursor and closing fence, which did not move."""
    async with web.running() as rig:
        adapter, outbox = rig.adapter, rig.adapter.outbox
        start, append, attempts = outbox.last_id(), outbox.append, []

        def fails_once(event_type, data):
            attempts.append(event_type)
            if len(attempts) == 1:
                raise sqlite3.OperationalError("disk I/O error")
            return append(event_type, data)

        message_id = (await adapter.send("owner", "Tady ▉", metadata={"expect_edits": True})).message_id
        monkeypatch.setattr(outbox, "append", fails_once)
        snapshot = "Tady je oprava:\n```py\nprint( ▉\n```"
        assert (await adapter.edit_message("owner", message_id, snapshot)).retryable
        assert (await adapter.edit_message("owner", message_id, snapshot)).success
        assert (await adapter.edit_message("owner", message_id, "Tady je oprava:\n```py\nprint('ahoj') ▉\n```")).success
        assert (await adapter.edit_message("owner", message_id, "Tady je oprava:\n```py\nprint('ahoj')\n```",
                                           finalize=True)).success
        events = await web.read_events(rig.client, rig.base, after=start,
                                       until=lambda evs: any(e["type"] == "message.final" for e in evs))
    pieces = [event["data"]["text"] for event in web.of_type(events, "message.delta")]
    assert pieces == ["Tady ", "je oprava:\n```py\nprint("]


# -- clarify -----------------------------------------------------------------------------------

def _runner_for(adapter, loop):
    from gateway.run_turn_runner import TurnRunner

    runner = object.__new__(TurnRunner)
    runner._ctx = SimpleNamespace(
        _status_adapter=adapter, _status_chat_id="owner", _status_thread_metadata=None,
        session_key=adapter.owner_session_key(), stream_consumer_holder=[None])
    runner._schedule = lambda coro, label: asyncio.run_coroutine_threadsafe(coro, loop)
    runner._close_native_stream_boundary = lambda *a, **k: None
    return runner


@pytest.mark.asyncio
@pytest.mark.parametrize("multi,answer,expected", [
    (False, {"choice": "Brno"}, "Brno"),
    (False, {"text": "Do Ostravy"}, "Do Ostravy"),  # „Jiné…“
    (False, {"skip": True}, None),  # „Přeskočit“
    (True, {"choices": ["Brno", "Praha"]}, json.dumps(["Brno", "Praha"], ensure_ascii=False)),
])
async def test_clarify_round_trip_through_the_turn_runner(web, multi, answer, expected):
    async with web.running() as rig:
        loop = asyncio.get_running_loop()
        runner = _runner_for(rig.adapter, loop)
        start = rig.adapter.outbox.last_id()
        questions = [{"qid": "q0", "question": "Kam pojedeme?", "choices": ["Praha", "Brno"], "multi_select": multi}]
        reply = loop.run_in_executor(None, runner._clarify_callback_sync, questions)
        request, waiting = await web.read_events(rig.client, rig.base, after=start, until=lambda evs: len(evs) == 2)
        clarify_id = request["data"]["clarify_id"]
        assert request["type"] == "clarify.request" and waiting["data"] == {"state": "waiting"}
        assert request["data"] == {"clarify_id": clarify_id, "question": "Kam pojedeme?",
                                   "choices": ["Praha", "Brno"], "multi": multi}
        async with rig.client.get(f"{rig.base}/v1/history") as resp:
            snapshot = (await resp.json())["snapshot"]
        assert snapshot["state"] == "waiting" and snapshot["clarify"] == request["data"]
        async with rig.client.post(f"{rig.base}/v1/clarify/{clarify_id}", json=answer) as resp:
            assert resp.status == 202 and await resp.json() == {}
        assert await asyncio.wait_for(reply, 10) == {"answers": {"q0": expected}, "outcome": "submitted"}
        retired, working = await web.read_events(rig.client, rig.base, after=waiting["id"],
                                                 until=lambda evs: len(evs) == 2)
        assert (retired["type"], retired["data"]) == ("clarify.retired", {"clarify_id": clarify_id})
        assert working["data"] == {"state": "working"}
        async with rig.client.post(f"{rig.base}/v1/clarify/{clarify_id}", json={"choice": "Praha"}) as resp:
            assert resp.status == 409 and await resp.json() == {"error": {"code": "conflict"}}


@pytest.mark.asyncio
async def test_a_question_that_ends_elsewhere_is_retired(web):
    """Typed into the composer (the gateway's text intercept), /new, /stop or a timeout: the card
    must stop looking answerable in every open tab."""
    from tools import clarify_gateway as cg

    async with web.running() as rig:
        key = rig.adapter.owner_session_key()
        cg.register("cid-typed", key, "Ano?", ["Ano", "Ne"])
        start = rig.adapter.outbox.last_id()
        assert (await rig.adapter.send_clarify("owner", "Ano?", ["Ano", "Ne"], "cid-typed", key)).success
        cg.clear_session(key)
        events = await web.read_events(rig.client, rig.base, after=start,
                                       until=lambda evs: any(e["type"] == "clarify.retired" for e in evs))
    assert [e["type"] for e in events] == ["clarify.request", "status", "clarify.retired", "status"]


@pytest.mark.asyncio
async def test_long_or_repeated_choices_are_shown_as_distinct_lines_and_mapped_back(web):
    long_choice = "A " + "velmi dlouhá volba " * 20 + "\nna dvou řádcích"
    async with web.running() as rig:
        loop = asyncio.get_running_loop()
        runner = _runner_for(rig.adapter, loop)
        start = rig.adapter.outbox.last_id()
        questions = [{"qid": "q0", "question": "Která?", "choices": [long_choice, "Ne", "Ne "], "multi_select": False}]
        reply = loop.run_in_executor(None, runner._clarify_callback_sync, questions)
        request = (await web.read_events(rig.client, rig.base, after=start, until=lambda evs: len(evs) == 2))[0]
        shown = request["data"]["choices"]  # held to the contract by the reader
        async with rig.client.post(f"{rig.base}/v1/clarify/{request['data']['clarify_id']}",
                                   json={"choice": shown[0]}) as resp:
            assert resp.status == 202
        answers = (await asyncio.wait_for(reply, 10))["answers"]
    assert len(shown) == 3 and len(set(shown)) == 3 and all(len(choice) <= 200 for choice in shown)
    assert answers == {"q0": long_choice}  # Hermes' own label, not the shortened one


@pytest.mark.asyncio
async def test_another_sessions_question_cannot_be_answered_here(web):
    from tools import clarify_gateway as cg

    telegram_key = "agent:main:telegram:dm:42"
    cg.register("cid-telegram", telegram_key, "Smazat?", ["Ano", "Ne"])
    try:
        async with web.running() as rig:
            async with rig.client.post(f"{rig.base}/v1/clarify/cid-telegram", json={"choice": "Ano"}) as resp:
                assert resp.status == 404 and await resp.json() == {"error": {"code": "not-found"}}
        assert cg._entries["cid-telegram"].response is None
    finally:
        cg.clear_session(telegram_key)


@pytest.mark.asyncio
async def test_malformed_clarify_answers_are_refused(web):
    from tools import clarify_gateway as cg

    bodies = [{}, {"choice": "Ano", "text": "x"}, {"choice": ""}, {"choice": "a" * 201}, {"choices": []},
              {"choices": ["Ano", "Ano"]}, {"choices": [str(i) for i in range(9)]}, {"text": "  "},
              {"text": "a" * 2001}, {"skip": False}, {"skip": "true"}, {"answer": "Ano"}, ["Ano"],
              {"choice": "Možná"}, {"choices": ["Ano"]}]  # the last two: not offered / not a multi question
    async with web.running() as rig:
        key = rig.adapter.owner_session_key()
        cg.register("cid-bad", key, "Ano?", ["Ano", "Ne"])
        assert (await rig.adapter.send_clarify("owner", "Ano?", ["Ano", "Ne"], "cid-bad", key)).success
        for body in bodies:
            async with rig.client.post(f"{rig.base}/v1/clarify/cid-bad", json=body) as resp:
                assert resp.status == 400 and await resp.json() == {"error": {"code": "bad-request"}}, body
        assert cg._entries["cid-bad"].response is None
        cg.clear_session(key)


# -- authorization, registration, tool environment ---------------------------------------------

class _Ctx:
    """The two PluginContext calls the plugin makes, wired to the real platform registry."""

    def __init__(self):
        self.hooks: dict[str, list] = {}

    def register_platform(self, *, name, label, **kwargs):
        from gateway.platform_registry import PlatformEntry, platform_registry

        platform_registry.register(PlatformEntry(name=name, label=label, source="plugin", **kwargs))

    def register_hook(self, name, callback):
        self.hooks.setdefault(name, []).append(callback)


@pytest.fixture
def registered(monkeypatch):
    from gateway.platform_registry import platform_registry
    from plugins.platforms.lazurio_web import register

    for name in ("LAZURIO_WEB_ALLOWED_USERS", "GATEWAY_ALLOWED_USERS", "GATEWAY_ALLOW_ALL_USERS"):
        monkeypatch.delenv(name, raising=False)
    ctx = _Ctx()
    register(ctx)
    yield ctx, platform_registry.get("lazurio_web")
    platform_registry.unregister("lazurio_web")


def test_the_registration_never_trusts_inbound_and_gates_by_allowlist(registered):
    from plugins.platforms.lazurio_web.page_context import pre_llm_call

    ctx, entry = registered
    assert entry.trusted_inbound is False
    assert entry.allowed_users_env == "LAZURIO_WEB_ALLOWED_USERS"
    assert entry.allow_update_command is False
    assert ctx.hooks == {"pre_llm_call": [pre_llm_call]}


def test_the_allowlist_admits_the_owner_and_rejects_a_stranger(web, registered, monkeypatch):
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource

    runner = object.__new__(GatewayRunner)
    runner.pairing_store = None
    owner = web.make()._owner_source("m-1")
    stranger = SessionSource(platform=owner.platform, chat_id="owner", chat_type="dm", user_id="stranger")
    assert runner._is_user_authorized(owner) is False  # no allowlist: default deny
    monkeypatch.setenv("LAZURIO_WEB_ALLOWED_USERS", "owner")
    assert runner._is_user_authorized(owner) is True
    assert runner._is_user_authorized(stranger) is False


def test_terminal_children_see_the_originating_channel(web):
    """Budíček records where a delegation came from. The gateway binds the turn's channel as
    session variables and the terminal tool exports them to every child process."""
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    from gateway.session import build_session_context
    from tools.environments.local import _make_run_env

    runner = object.__new__(GatewayRunner)
    tokens = runner._set_session_env(build_session_context(web.make()._owner_source("m-7"), GatewayConfig()))
    try:
        env = _make_run_env({})
    finally:
        runner._clear_session_env(tokens)
    assert (env["HERMES_SESSION_PLATFORM"], env["HERMES_SESSION_CHAT_ID"], env["HERMES_SESSION_MESSAGE_ID"]) == (
        "lazurio_web", "owner", "m-7")
