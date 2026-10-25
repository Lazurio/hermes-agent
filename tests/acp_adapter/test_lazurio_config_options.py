"""Lazurio overlay: the session's fast mode as the one ACP session config option, against a fake owner.

Buddy's chat in Lazurio T3 Code offers Normal or Fast and no model or reasoning effort, which the
Lazurio Platform sets in Hermes's config. A session whose route offers fast mode advertises one
``fast`` select beside upstream's ``models`` and ``modes``; setting it runs the owner's
session-scoped fast control (``config.set fast``, the TUI's ``/fast``); afterwards what the editor
was shown follows the owner by reading it, so a change from any surface reaches the editor as
``config_option_update`` and an older read can never overwrite a newer answer. Overlay row:
``acp: fast mode session config option on the gateway viewer`` (docs/operations/lazurio-fork-release.md).
"""
import asyncio
from unittest.mock import AsyncMock

import acp
import pytest
from acp.schema import ConfigOptionUpdate, ModelInfo, SessionModelState

from acp_adapter.gateway_server import GatewayACPAgent
from hermes_cli.gateway_client import GatewayClientError, GatewayRPCError

# Buddy's route: ChatGPT sign-in (openai-codex) on GPT-6.1 Sol, which offers fast mode.
ROUTE = {"model": "gpt-6.1-sol", "provider": "openai-codex"}
# A local model: no fast mode.
LOCAL = {"model": "local-wire-stub", "provider": "custom"}


def _agent(rpc, *, conn=True):
    agent = GatewayACPAgent()
    agent._gateway = AsyncMock()
    agent._conn = AsyncMock() if conn else None
    agent._gateway.rpc.side_effect = rpc
    agent._snapshots["s"] = {"execution_generation": 7}
    return agent


@pytest.fixture(autouse=True)
def _picker(monkeypatch):
    from acp_adapter import gateway_session_meta

    def picker(info):
        choice = f"{info.get('provider')}:{info.get('model')}"
        return SessionModelState(current_model_id=choice, available_models=[ModelInfo(model_id=choice, name="m")])
    monkeypatch.setattr(gateway_session_meta, "model_state", picker)
    monkeypatch.setattr(gateway_session_meta, "provenance_meta", lambda *args: None)


def _options(options):
    return {option.id: option for option in options}


def _fast(options):
    return _options(options)["fast"].current_value


def _updates(agent):
    return [call.kwargs["update"] for call in agent._conn.session_update.await_args_list
            if isinstance(call.kwargs["update"], ConfigOptionUpdate)]


def _shown(agent):
    """What every config_option_update told the editor: the fast value, or None for no option."""
    return [_options(u.config_options)["fast"].current_value if u.config_options else None for u in _updates(agent)]


async def _drain(agent):
    while agent._background:
        await asyncio.gather(*list(agent._background))
        await asyncio.sleep(0)  # a finished task leaves the set in its done callback


async def _no_owner(method, **params):
    raise AssertionError(f"no owner call expected, got {method}")


def _attached(agent, info, tier=""):
    """The session as its attach reply showed it, after the attach's own refresh."""
    agent._lazurio_config_fields("s", {**info, "service_tier": tier})
    agent._lazurio_states()["s"]["dirty"] = False


class Owner:
    """A fake owner: ``session.info`` answers the session's route (with the tier its agent last ran
    with), ``config.get fast`` and ``config.set fast`` its next turn's tier, as
    ``gateway/session_config.py`` and ``gateway/session_tui_config.py`` do, and ``session.mutate`` a
    model pick. A route without fast mode refuses the fast tier; so does every route with
    ``refuse_fast`` (one whose base URL ``session.info`` does not show)."""

    def __init__(self, info=ROUTE, *, tier=None, refuse_fast=False):
        self.info, self.tier, self.refuse_fast, self.calls = dict(info), tier, refuse_fast, []

    def word(self):
        return {"priority": "fast", "ultrafast": "ultrafast"}.get(self.tier, "normal")

    async def rpc(self, method, **params):
        self.calls.append((method, params))
        if method == "session.info":
            return dict(self.info)
        if method == "session.resume":
            return {"revision": 3, "execution_generation": 7, "info": dict(self.info)}
        if method == "config.get":
            assert params == {"session_id": "s", "key": "fast"}, params
            return {"value": self.word()}
        if method == "config.set":
            assert set(params) == {"session_id", "key", "value"} and params["key"] == "fast", params
            tier = {"fast": "priority", "normal": None}[params["value"]]
            from acp_adapter.lazurio_config_options import fast_offered
            if tier and (self.refuse_fast or not fast_offered(self.info)):
                raise GatewayRPCError("fast_unavailable")
            self.tier = tier
            return {"key": "fast", "value": self.word()}
        assert method == "session.mutate" and params["operation"] == "model", (method, params)
        self.info = {"model": params["payload"]["model"], "provider": params["payload"]["provider"]}
        return {"session_id": "s", "operation": "model", "revision": 4}

    def methods(self):
        return [method for method, _ in self.calls]


@pytest.mark.asyncio
async def test_a_session_reply_advertises_only_fast_beside_upstreams_models_and_modes():
    agent = _agent(_no_owner, conn=False)
    fields = await agent._session_fields("s", {**ROUTE, "service_tier": "", "reasoning_effort": "high"}, "s")
    assert fields["models"].current_model_id == "openai-codex:gpt-6.1-sol"  # upstream's picker, for Zed
    assert fields["modes"].current_mode_id == "default"
    [fast] = fields["config_options"]  # no model and no thought_level option
    assert (fast.type, fast.id, fast.name, fast.category, fast.current_value) == (
        "select", "fast", "Fast mode", None, "normal")
    assert [(choice.value, choice.name) for choice in fast.options] == [("normal", "Normal"), ("fast", "Fast")]


@pytest.mark.parametrize("tier, value", [
    ("", "normal"), (None, "normal"), ("priority", "fast"), ("ultrafast", "fast"), ("auto", "normal"),
    ("cold", "normal"), ("fast", "fast"), ("normal", "normal")])
def test_a_static_tier_shows_as_fast_and_anything_else_as_normal(tier, value):
    """The owner's tiers (``session.info``) and its words for them (``config.get fast``)."""
    from acp_adapter.lazurio_config_options import build_config_options, fast_value
    assert fast_value(tier) == value
    assert _fast(build_config_options({**ROUTE, "service_tier": tier}, fast_value(tier))) == value


@pytest.mark.parametrize("info", [
    ROUTE, {"model": "gpt-6.1-sol", "provider": "openai"}, {"model": "claude-opus-5-5", "provider": "anthropic"},
    {"model": "gpt-6.1-sol"}])
def test_fast_is_offered_where_the_route_offers_fast_mode(info):
    from acp_adapter.lazurio_config_options import build_config_options
    assert [option.id for option in build_config_options(info, "normal")] == ["fast"]


@pytest.mark.parametrize("info", [
    LOCAL, {"model": "gpt-6.1-sol", "provider": "openrouter"}, {"model": "gpt-6.1-sol", "provider": "custom"},
    {"model": "gpt-5.3-codex", "provider": "openai-codex"}, {"provider": "openai-codex"}, {}])
def test_fast_is_hidden_where_the_route_offers_no_fast_mode(info):
    from acp_adapter.lazurio_config_options import build_config_options
    assert build_config_options(info, "normal") == []


@pytest.mark.asyncio
async def test_a_session_on_a_route_without_fast_mode_advertises_no_options():
    agent = _agent(_no_owner, conn=False)
    fields = await agent._session_fields("s", {**LOCAL, "service_tier": ""}, "s")
    assert fields["config_options"] == []
    assert fields["models"].current_model_id == "custom:local-wire-stub" and fields["modes"] is not None


@pytest.mark.asyncio
async def test_fast_and_back_to_normal_run_the_owners_session_fast_control():
    owner = Owner()
    agent = _agent(owner.rpc)
    _attached(agent, ROUTE)
    response = await agent.set_config_option("fast", "s", "fast")
    assert _fast(response.config_options) == "fast" and owner.tier == "priority"
    response = await agent.set_config_option("fast", "s", "normal")
    assert _fast(response.config_options) == "normal" and owner.tier is None
    await _drain(agent)
    sets = [params for method, params in owner.calls if method == "config.set"]
    assert sets == [{"session_id": "s", "key": "fast", "value": "fast"},
                    {"session_id": "s", "key": "fast", "value": "normal"}]
    # Session-scoped: no durable mutation, and the refreshes after the answers find nothing new.
    assert "session.mutate" not in owner.methods() and _updates(agent) == []
    assert _fast(agent._lazurio_shown_options("s")) == "normal"


@pytest.mark.asyncio
async def test_a_set_retires_what_a_lost_submit_and_an_ambiguous_control_retained():
    owner = Owner()
    agent = _agent(owner.rpc)
    _attached(agent, ROUTE)
    agent._unacked["s"] = ("retry-key", "input-id", ("text", []))
    agent._mutations.pending[("s", "model", '{"model": "m"}')] = {"params": {}}
    await agent.set_config_option("fast", "s", "fast")
    assert "s" not in agent._unacked and not agent._mutations.pending
    await _drain(agent)


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["ultrafast", "on", "toggle", "priority", "Fast", "", None])
async def test_a_value_other_than_normal_or_fast_is_refused_before_the_owner(value):
    agent = _agent(_no_owner)
    _attached(agent, ROUTE)
    with pytest.raises(acp.RequestError) as refused:
        await agent.set_config_option("fast", "s", value)
    assert refused.value.code == -32602


@pytest.mark.asyncio
async def test_the_owners_refusal_where_the_route_has_no_fast_mode_reaches_the_editor():
    """``session.info`` shows no base URL: a first-party provider behind another endpoint passes the
    viewer's check, and the owner, which sees the base URL, refuses fast there."""
    owner = Owner(refuse_fast=True)
    agent = _agent(owner.rpc)
    _attached(agent, ROUTE)
    with pytest.raises(GatewayRPCError, match="fast_unavailable"):
        await agent.set_config_option("fast", "s", "fast")
    await _drain(agent)
    assert owner.tier is None and _fast(agent._lazurio_shown_options("s")) == "normal"
    assert _updates(agent) == []
    # Normal is never refused.
    assert _fast((await agent.set_config_option("fast", "s", "normal")).config_options) == "normal"
    await _drain(agent)


@pytest.mark.asyncio
@pytest.mark.parametrize("config_id, value", [
    ("model", "openai-codex:gpt-6-luna"), ("thought_level", "high"), ("thought_level", "turbo"), ("theme", "dark")])
async def test_the_model_and_the_reasoning_effort_answer_like_any_unknown_config_id(config_id, value):
    """No model pick and no reasoning mutation: nothing reaches the owner, and the answer is the
    options the editor was last shown."""
    agent = _agent(_no_owner)
    # Nothing shown yet: upstream's empty answer.
    assert (await agent.set_config_option(config_id, "s", value)).config_options == []
    _attached(agent, ROUTE)
    shown = (await agent.set_config_option(config_id, "s", value)).config_options
    assert [option.id for option in shown] == ["fast"]
    await _drain(agent)


@pytest.mark.asyncio
async def test_another_surfaces_change_reaches_the_editor_as_config_option_update():
    owner = Owner()
    agent = _agent(owner.rpc)
    _attached(agent, ROUTE)

    # Another surface pins fast (the TUI's /fast): the owner publishes no event, and its session.info
    # still reports the tier the agent last ran with, so nothing is read yet.
    owner.tier = "priority"
    await agent._project({"session_id": "s", "type": "session.info", "payload": {**ROUTE, "service_tier": ""}})
    await _drain(agent)
    assert owner.calls == [] and _updates(agent) == []

    # The next turn runs with it, and its session.info tells: the editor is told.
    owner.info["service_tier"] = "priority"
    await agent._project({"session_id": "s", "type": "session.info", "payload": {**ROUTE, "service_tier": "priority"}})
    await _drain(agent)
    assert _shown(agent) == ["fast"]

    # The same tier again, or a session.info without route or tier (a title, pending rows): nothing.
    reads = len(owner.calls)
    await agent._project({"session_id": "s", "type": "session.info", "payload": {"service_tier": "priority"}})
    await agent._project({"session_id": "s", "type": "session.info", "payload": {"title": "Plans"}})
    await _drain(agent)
    assert len(owner.calls) == reads and _shown(agent) == ["fast"]

    # A model pick elsewhere onto a route without fast mode hides the option; back again shows it.
    owner.info = dict(LOCAL)
    await agent._project({"session_id": "s", "type": "session.info", "payload": dict(LOCAL)})
    await _drain(agent)
    owner.info = dict(ROUTE)
    await agent._project({"session_id": "s", "type": "session.info", "payload": dict(ROUTE)})
    await _drain(agent)
    assert _shown(agent) == ["fast", None, "fast"]


@pytest.mark.asyncio
async def test_another_surfaces_return_to_normal_reaches_the_editor_with_the_next_session_info():
    """The editor shows fast; another surface pins normal. The next session.info that reports a tier
    other than fast asks for a refresh: a turn that ran normal, or a mutation's session.info of a
    session whose agent is not built yet, which reports the profile's tier."""
    owner = Owner(tier="priority")
    agent = _agent(owner.rpc)
    _attached(agent, ROUTE, tier="priority")
    owner.tier = None
    await agent._project({"session_id": "s", "type": "session.info", "payload": {**ROUTE, "service_tier": ""}})
    await _drain(agent)
    assert _shown(agent) == ["normal"]


@pytest.mark.asyncio
async def test_a_change_published_while_the_reply_is_built_reaches_the_editor_after_the_reply():
    """The attach reply shows the tier the session's agent last ran with; a pin made since (or a
    change published while the reply is built, before the session has state to project it onto) is
    told by the refresh at the end of the post-attach notices."""
    owner = Owner(tier="priority")
    agent = _agent(owner.rpc)
    agent._after_attach = AsyncMock()  # the reply's post-attach notices run below, after the reply
    await agent._project({"session_id": "s", "type": "session.info", "payload": {**ROUTE, "service_tier": "priority"}})
    fields = await agent._session_fields("s", {**ROUTE, "service_tier": ""}, "s")
    assert _fast(fields["config_options"]) == "normal"
    await agent._lazurio_reconcile("s")
    assert _shown(agent) == ["fast"]


@pytest.mark.asyncio
async def test_an_older_read_never_overwrites_a_newer_answer():
    """A session.info of a turn that ran fast asks for a refresh whose read is still out when this
    editor's own set of normal commits and is answered. The older read lands first (the editor
    briefly sees fast), the answer shows normal, the refresh after it finds nothing new, and a late
    copy of the older event changes nothing: the editor ends on the owner's normal."""
    owner = Owner(tier="priority")
    held, release = asyncio.Event(), asyncio.Event()
    plain = owner.rpc

    async def rpc(method, **params):
        if method == "config.get" and not held.is_set():
            seen = await plain(method, **params)  # what the owner holds when the read arrives
            held.set()
            await release.wait()
            return seen
        return await plain(method, **params)

    agent = _agent(rpc)
    _attached(agent, ROUTE)
    await agent._project({"session_id": "s", "type": "session.info", "payload": {**ROUTE, "service_tier": "priority"}})
    await asyncio.wait_for(held.wait(), 5)  # the refresh's read of fast is out

    pick = asyncio.create_task(agent.set_config_option("fast", "s", "normal"))
    while owner.tier is not None:
        await asyncio.sleep(0)
    release.set()
    response = await asyncio.wait_for(pick, 5)
    assert _fast(response.config_options) == "normal"
    await agent._project({"session_id": "s", "type": "session.info", "payload": {**ROUTE, "service_tier": "priority"}})
    await _drain(agent)
    assert _fast(agent._lazurio_shown_options("s")) == "normal"
    assert _shown(agent) == ["fast"], _shown(agent)  # never told fast after the answer


@pytest.mark.asyncio
async def test_a_route_change_an_adopted_snapshot_covers_reaches_the_editor_without_its_event():
    """A control that adopts the owner's fresh snapshot (upstream's ``session/set_model``, kept for
    Zed's model picker, or a typed ``/model``) makes the pump skip the events at or below that
    snapshot's watermark, including the owner's session.info for the change: the adoption itself
    asks for the refresh, which hides the option on a route without fast mode."""
    owner = Owner()
    agent = _agent(owner.rpc)
    _attached(agent, ROUTE)
    await agent.set_session_model(model_id="custom:local-wire-stub", session_id="s")  # its session.info never arrives
    await _drain(agent)
    assert [params["payload"] for method, params in owner.calls if method == "session.mutate"] == [
        {"model": "local-wire-stub", "provider": "custom"}]
    assert _shown(agent) == [None]


def _reconnecting(agent, owner):
    """A replacement connection to *owner*, installed as ``_client`` does: re-attach first."""
    async def rpc(method, **params):
        if method == "session.resume":
            return {"revision": 5, "execution_generation": 7, "replay_epoch": "e2", "last_sequence": 9,
                    "info": dict(owner.info), "prompts": []}
        if method == "config.set" and params.get("key") == "show_reasoning":  # the re-attach asks again
            return {}
        return await owner.rpc(method, **params)

    fresh = AsyncMock()
    fresh.rpc.side_effect = rpc

    async def reconnect():
        agent._gateway = None  # ``_teardown`` dropped the lost connection
        async with agent._connect_lock:
            await agent._reattach(fresh)
            for _ in range(5):
                await asyncio.sleep(0)  # a refresh the adoption asked for waits for the connection
            agent._gateway = fresh
        await _drain(agent)
    return reconnect


@pytest.mark.asyncio
async def test_a_pin_an_owner_restart_dropped_reaches_the_editor_on_reconnect():
    """The editor pinned fast and no turn ran since, so every session.info it read still reported
    normal. The pin lives in the owner's memory: a restarted owner runs the session's own tier
    again, and the re-attach's snapshot, unchanged since, still asks for the read because it reports
    normal against the fast the editor was shown."""
    owner = Owner(info={**ROUTE, "service_tier": ""})
    agent = _agent(owner.rpc)
    _attached(agent, ROUTE)
    assert _fast((await agent.set_config_option("fast", "s", "fast")).config_options) == "fast"
    await _drain(agent)
    assert agent._lazurio_states()["s"]["info"]["service_tier"] == ""  # what the owner reported
    owner.tier = None  # the restart
    await _reconnecting(agent, owner)()
    assert _shown(agent) == ["normal"]


@pytest.mark.asyncio
async def test_a_pin_whose_answer_never_came_reaches_the_editor():
    """The owner pinned fast, but its answer did not come in time (the connection stays): the editor
    gets the error, and the refresh after it tells the editor what the owner holds."""
    owner = Owner()
    agent = _agent(owner.rpc)
    _attached(agent, ROUTE)

    async def late(method, **params):
        answer = await owner.rpc(method, **params)
        if method == "config.set":
            raise GatewayClientError("timeout")
        return answer
    agent._gateway.rpc.side_effect = late
    with pytest.raises(GatewayClientError, match="timeout"):
        await agent.set_config_option("fast", "s", "fast")
    await _drain(agent)
    assert owner.tier == "priority" and _shown(agent) == ["fast"]


@pytest.mark.asyncio
async def test_a_pin_whose_connection_was_lost_reaches_the_editor_as_another_surfaces_does():
    """The owner pinned fast, then the connection went down with the answer. The reconnect reads only
    when its snapshot differs from what the editor was shown (upstream's reconnect tests pin every
    call on the new connection), and this one still reports the tier the agent last ran with; the
    session.info of the next turn, which runs fast, tells the editor."""
    owner = Owner()
    agent = _agent(owner.rpc)
    _attached(agent, ROUTE)

    async def lost(method, **params):
        await owner.rpc(method, **params)
        agent._gateway = None  # the transport went down with the answer
        raise GatewayClientError("Gateway connection failed; no local fallback")
    agent._gateway.rpc.side_effect = lost
    with pytest.raises(GatewayClientError):
        await agent.set_config_option("fast", "s", "fast")
    await _drain(agent)
    assert owner.tier == "priority" and _updates(agent) == []
    calls = len(owner.calls)
    await _reconnecting(agent, owner)()
    assert len(owner.calls) == calls and _updates(agent) == []  # nothing read through the new connection
    owner.info["service_tier"] = "priority"
    await agent._project({"session_id": "s", "type": "session.info", "payload": {**ROUTE, "service_tier": "priority"}})
    await _drain(agent)
    assert _shown(agent) == ["fast"]


def _flaky(owner, fail):
    """*owner*, whose reads fail while *fail* (the read's number) says so."""
    reads = []

    async def rpc(method, **params):
        if method == "session.info":
            reads.append(method)
            if fail(len(reads)):
                raise GatewayClientError("Gateway connection failed; no local fallback")
        return await owner.rpc(method, **params)
    return rpc, reads


@pytest.mark.asyncio
async def test_a_failed_read_after_an_adopted_snapshot_is_retried_until_the_editor_follows(monkeypatch):
    """The adopted snapshot carries another surface's model pick onto a route without fast mode (its
    event is under the snapshot's watermark), and the refresh's first owner read fails once. The
    request stays and is retried on the same connection, so the option goes."""
    monkeypatch.setattr(GatewayACPAgent, "_LAZURIO_RETRY_DELAYS", (0, 0, 0))
    rpc, reads = _flaky(Owner(LOCAL), lambda n: n == 1)
    agent = _agent(rpc)
    _attached(agent, ROUTE)
    await agent._adopt("s", {"execution_generation": 7, "replay_epoch": "e", "last_sequence": 2, "info": dict(LOCAL)})
    await _drain(agent)
    assert len(reads) == 2 and _shown(agent) == [None]
    assert agent._lazurio_states()["s"]["dirty"] is False


@pytest.mark.asyncio
async def test_retries_are_bounded_and_the_next_change_reads_again(monkeypatch):
    monkeypatch.setattr(GatewayACPAgent, "_LAZURIO_RETRY_DELAYS", (0, 0, 0))
    failing = {"on": True}
    rpc, reads = _flaky(Owner(tier="priority"), lambda n: failing["on"])
    agent = _agent(rpc)
    agent._lazurio_config_fields("s", {**ROUTE, "service_tier": ""})
    await agent._lazurio_reconcile("s")
    await _drain(agent)
    assert len(reads) == 4 and _updates(agent) == []  # the read and three retries, then it waits
    assert agent._lazurio_states()["s"]["dirty"] is True  # the request stays
    failing["on"] = False
    await agent._project({"session_id": "s", "type": "session.info", "payload": {"title": "Plans"}})
    await _drain(agent)
    assert len(reads) == 4  # a session.info without route or tier asks for nothing
    await agent._project({"session_id": "s", "type": "session.info", "payload": {"service_tier": "priority"}})
    await _drain(agent)
    assert len(reads) == 5 and _shown(agent) == ["fast"]


@pytest.mark.asyncio
async def test_a_retry_never_reads_through_another_connection(monkeypatch):
    """A read that failed with its connection is not retried through a reconnect's new connection:
    the reconnect's own adoption serves it (upstream's reconnect tests pin every call there)."""
    monkeypatch.setattr(GatewayACPAgent, "_LAZURIO_RETRY_DELAYS", (0.05, 0.05, 0.05))
    rpc, reads = _flaky(Owner(), lambda n: True)
    agent = _agent(rpc)
    agent._lazurio_config_fields("s", {**ROUTE, "service_tier": ""})
    await agent._lazurio_reconcile("s")
    replacement = AsyncMock()
    replacement.rpc.side_effect = _no_owner
    agent._gateway = replacement  # a reconnect installed a new connection before the retry's turn
    await _drain(agent)
    assert len(reads) == 1 and replacement.rpc.await_count == 0


@pytest.mark.asyncio
async def test_a_refresh_reads_the_route_and_the_tier_and_is_quiet_when_nothing_changed():
    owner = Owner()
    agent = _agent(owner.rpc)
    agent._lazurio_config_fields("s", {**ROUTE, "service_tier": ""})
    await agent._lazurio_reconcile("s")
    assert owner.methods() == ["session.info", "config.get"] and _updates(agent) == []


@pytest.mark.asyncio
async def test_the_refresh_is_the_last_post_attach_notice_and_never_reconnects():
    owner = Owner()

    async def rpc(method, **params):
        return {"input_tokens": 0} if method == "session.stats" else await owner.rpc(method, **params)

    agent = _agent(rpc)
    agent._lazurio_config_fields("s", {**ROUTE, "service_tier": ""})
    await agent._after_attach("s", "s")
    assert owner.methods() == ["session.info", "config.get"]
    # Without a live connection (aclose, a lost transport) it opens none.
    agent._lazurio_config_fields("s", {**ROUTE, "service_tier": ""})
    agent._gateway = None
    await agent._lazurio_reconcile("s")
    assert owner.methods() == ["session.info", "config.get"]


@pytest.mark.asyncio
async def test_a_session_this_editor_never_attached_gets_no_update():
    agent = _agent(_no_owner)
    await agent._project({"session_id": "other", "type": "session.info", "payload": {"service_tier": "priority"}})
    await _drain(agent)
    assert _updates(agent) == []
