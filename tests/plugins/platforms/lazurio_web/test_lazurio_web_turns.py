"""``lazurio_web`` against the real gateway machinery: the stream consumer's send/edit protocol, the
turn runner's clarify callback, the runner's allowlist, and the session variables tools inherit."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from tests.waiting import async_wait_until


# -- streaming ---------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_streamed_reply_arrives_as_deltas_then_one_final(web):
    from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig

    async with web.running() as rig:
        start = rig.adapter.outbox.last_id()
        config = StreamConsumerConfig(edit_interval=0.01, buffer_threshold=1, cursor=" ▉", transport="edit",
                                      chat_type="dm")
        consumer = GatewayStreamConsumer(rig.adapter, "owner", config)
        task = asyncio.create_task(consumer.run())
        consumer.on_delta("Dobrý")
        await async_wait_until(lambda: rig.adapter.outbox.last_id() > start, what="the first delta")
        consumer.on_delta(" den, jak")
        consumer.on_delta(" se máte?")
        consumer.finish()
        await asyncio.wait_for(task, 10)
        events = await web.read_events(rig.client, rig.base, after=start,
                                       until=lambda evs: any(e["type"] == "message.final" for e in evs))
    deltas, finals = web.of_type(events, "message.delta"), web.of_type(events, "message.final")
    assert deltas[0]["data"]["text"] == "Dobrý"
    assert not any(d["data"]["text"].endswith("▉") for d in deltas)  # the cursor is the client's business
    assert len(finals) == 1
    message_id = finals[0]["data"]["message_id"]
    assert finals[0]["data"] == {"message_id": message_id, "text": "Dobrý den, jak se máte?", "media": []}
    assert {d["data"]["message_id"] for d in deltas} == {message_id}
    assert consumer.final_response_sent is True


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
    (False, {"choice": 1}, "Brno"),
    (False, {"choice": "Praha"}, "Praha"),
    (False, {"text": "Do Ostravy"}, "Do Ostravy"),  # „Jiné…“
    (False, {"skip": True}, None),  # „Přeskočit“
    (True, {"choices": [1, 0]}, json.dumps(["Brno", "Praha"], ensure_ascii=False)),
])
async def test_clarify_round_trip_through_the_turn_runner(web, multi, answer, expected):
    async with web.running() as rig:
        loop = asyncio.get_running_loop()
        runner = _runner_for(rig.adapter, loop)
        start = rig.adapter.outbox.last_id()
        questions = [{"qid": "q0", "question": "Kam pojedeme?", "choices": ["Praha", "Brno"], "multi_select": multi}]
        reply = loop.run_in_executor(None, runner._clarify_callback_sync, questions)
        (request,) = await web.read_events(rig.client, rig.base, after=start, until=lambda evs: len(evs) == 1)
        clarify_id = request["data"]["clarify_id"]
        assert request["type"] == "clarify.request"
        assert request["data"] == {"clarify_id": clarify_id, "question": "Kam pojedeme?",
                                   "choices": ["Praha", "Brno"], "multi": multi}
        async with rig.client.post(f"{rig.base}/v1/clarify/{clarify_id}", json=answer) as resp:
            assert resp.status == 200 and await resp.json() == {"resolved": True}
        assert await asyncio.wait_for(reply, 10) == {"answers": {"q0": expected}, "outcome": "submitted"}
        (retired,) = await web.read_events(rig.client, rig.base, after=request["id"], until=lambda evs: len(evs) == 1)
        assert (retired["type"], retired["data"]) == ("clarify.retired", {"clarify_id": clarify_id})
        async with rig.client.post(f"{rig.base}/v1/clarify/{clarify_id}", json={"choice": 0}) as resp:
            assert resp.status == 404


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
    assert [e["type"] for e in events] == ["clarify.request", "clarify.retired"]


@pytest.mark.asyncio
async def test_another_sessions_question_cannot_be_answered_here(web):
    from tools import clarify_gateway as cg

    telegram_key = "agent:main:telegram:dm:42"
    cg.register("cid-telegram", telegram_key, "Smazat?", ["Ano", "Ne"])
    try:
        async with web.running() as rig:
            async with rig.client.post(f"{rig.base}/v1/clarify/cid-telegram", json={"choice": 0}) as resp:
                assert resp.status == 404
        assert cg._entries["cid-telegram"].response is None
    finally:
        cg.clear_session(telegram_key)


@pytest.mark.parametrize("body", [
    {"choice": 5}, {"choice": True}, {"choice": "Ostrava"}, {"choices": [0]}, {"skip": "yes"}, {"text": "  "},
    {"choice": 0, "text": "x"}, {},
])
def test_malformed_clarify_answers_are_refused(body):
    from plugins.platforms.lazurio_web.clarify import answer_value
    from plugins.platforms.lazurio_web.errors import ApiError

    entry = SimpleNamespace(clarify_id="cid", choices=["Praha", "Brno"], multi_select=False)
    with pytest.raises(ApiError) as refused:
        answer_value(entry, body)
    assert refused.value.status == 400


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
