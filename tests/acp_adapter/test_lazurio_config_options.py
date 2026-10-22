"""Lazurio overlay: model and reasoning effort as ACP session config options, against a fake owner.

Lazurio T3 Code reads its model list only from ``configOptions`` of category ``model`` and offers a
``thought_level`` select in its composer; ``models`` and ``modes`` stay for Zed. Setting either
option runs the owner's existing mutation; afterwards what the editor was shown follows the owner by
reading it, so a change from any surface reaches the editor as ``config_option_update`` and an older
event can never overwrite a newer answer. Overlay row: ``acp: model and thought_level session config
options on the gateway viewer`` (docs/operations/lazurio-fork-release.md).
"""
import asyncio
from unittest.mock import AsyncMock

import acp
import pytest
from acp.schema import ConfigOptionUpdate, ModelInfo, SessionModelState

from acp_adapter.gateway_server import GatewayACPAgent
from hermes_constants import VALID_REASONING_EFFORTS

LEVELS = ["none", *VALID_REASONING_EFFORTS]


def _models(current="custom:m1"):
    return SessionModelState(current_model_id=current, available_models=[
        ModelInfo(model_id="custom:m1", name="m1"), ModelInfo(model_id="custom:m2", name="m2")])


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
    monkeypatch.setattr(gateway_session_meta, "model_state", lambda info: _models(f"custom:{info.get('model', 'm1')}"))
    monkeypatch.setattr(gateway_session_meta, "provenance_meta", lambda *args: None)


def _options(options):
    return {option.id: option for option in options}


def _updates(agent):
    return [call.kwargs["update"] for call in agent._conn.session_update.await_args_list
            if isinstance(call.kwargs["update"], ConfigOptionUpdate)]


async def _drain(agent):
    while agent._background:
        await asyncio.gather(*list(agent._background))
        await asyncio.sleep(0)  # a finished task leaves the set in its done callback


async def _no_owner(method, **params):
    raise AssertionError(f"no owner call expected, got {method}")


def _reads(truth):
    """A fake owner whose ``session.info`` answers the current *truth* (a dict the test may change)."""
    async def rpc(method, **params):
        assert method == "session.info", method
        return dict(truth)
    return rpc


@pytest.mark.asyncio
async def test_a_session_reply_advertises_model_and_thought_level_beside_models_and_modes():
    agent = _agent(_no_owner, conn=False)
    fields = await agent._session_fields("s", {"model": "m1", "provider": "custom", "reasoning_effort": "high"}, "s")
    assert fields["models"].current_model_id == "custom:m1"
    assert fields["modes"].current_mode_id == "default"
    options = _options(fields["config_options"])
    assert list(options) == ["model", "thought_level"]
    model, level = options["model"], options["thought_level"]
    assert (model.type, model.category, model.current_value) == ("select", "model", "custom:m1")
    assert [choice.value for choice in model.options] == ["custom:m1", "custom:m2"]
    assert (level.type, level.category, level.current_value) == ("select", "thought_level", "high")
    assert [choice.value for choice in level.options] == LEVELS


@pytest.mark.asyncio
async def test_a_session_without_a_level_shows_upstreams_default_and_a_current_model_off_the_list():
    from acp_adapter.lazurio_config_options import build_config_options
    options = _options(build_config_options({"model": "m9", "provider": "custom"}, _models("custom:m9")))
    assert options["thought_level"].current_value == "medium"
    assert options["model"].current_value == "custom:m9"
    assert options["model"].options[0].value == "custom:m9"
    # No picker at all: the reasoning effort is still offered.
    assert list(_options(build_config_options({}, None))) == ["thought_level"]


def _owner(operation, before, after, *, fail_first_readback=False):
    """A fake owner for one mutation: ``session.resume`` and ``session.info`` answer *before* until
    the mutation lands, then *after*; ``fail_first_readback`` loses the first snapshot read after it."""
    sent, state = [], {"committed": False, "lost": fail_first_readback}

    async def rpc(method, **params):
        if method == "session.info":
            return dict(after if state["committed"] else before)
        if method == "session.resume":
            if state["committed"] and state["lost"]:
                state["lost"] = False
                from hermes_cli.gateway_client import GatewayClientError
                raise GatewayClientError("Gateway connection failed; no local fallback")
            return {"revision": 3, "execution_generation": 7, "info": after if state["committed"] else before}
        assert method == "session.mutate", method
        sent.append((params["operation"], params["payload"], params["expected_generation"], params["request_id"]))
        state["committed"] = True
        return {"session_id": "s", "operation": operation, "revision": 4}

    return rpc, sent


@pytest.mark.asyncio
async def test_thought_level_runs_the_owners_reasoning_mutation_and_answers_the_new_options():
    rpc, sent = _owner("reasoning", {"model": "m1", "provider": "custom", "reasoning_effort": "high"},
                       {"model": "m1", "provider": "custom", "reasoning_effort": "xhigh"})
    agent = _agent(rpc)
    response = await agent.set_config_option("thought_level", "s", "xhigh")
    assert [(op, payload, generation) for op, payload, generation, _ in sent] == [("reasoning", {"effort": "xhigh"}, 7)]
    options = _options(response.config_options)
    assert options["thought_level"].current_value == "xhigh"
    assert options["model"].current_value == "custom:m1"
    # The pick is not retained as a pending control once the answer was built, and the refresh that
    # follows the answer finds nothing new to tell the editor.
    assert not agent._mutations.pending
    await _drain(agent)
    assert _updates(agent) == []


@pytest.mark.asyncio
async def test_a_retry_after_a_lost_answer_replays_the_owners_receipt_instead_of_writing_twice():
    """The owner committed the level, then the snapshot read for the answer was lost: the editor's
    retry of the same pick must not send a second, newly fenced mutation."""
    from hermes_cli.gateway_client import GatewayClientError
    rpc, sent = _owner("reasoning", {"model": "m1", "provider": "custom"},
                       {"model": "m1", "provider": "custom", "reasoning_effort": "low"}, fail_first_readback=True)
    agent = _agent(rpc)
    with pytest.raises(GatewayClientError):
        await agent.set_config_option("thought_level", "s", "low")
    response = await agent.set_config_option("thought_level", "s", "low")
    assert len(sent) == 1, sent
    assert _options(response.config_options)["thought_level"].current_value == "low"
    assert not agent._mutations.pending
    await _drain(agent)


@pytest.mark.asyncio
async def test_the_model_option_is_the_model_pick_with_its_provider():
    rpc, sent = _owner("model", {"model": "m1", "provider": "custom"}, {"model": "m2", "provider": "custom"})
    agent = _agent(rpc)
    response = await agent.set_config_option("model", "s", "custom:m2")
    assert [(op, payload) for op, payload, _, _ in sent] == [("model", {"model": "m2", "provider": "custom"})]
    assert _options(response.config_options)["model"].current_value == "custom:m2"
    await _drain(agent)
    assert _updates(agent) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("config_id, value", [("thought_level", "turbo"), ("thought_level", ""), ("model", " ")])
async def test_an_invalid_value_is_refused_before_the_owner(config_id, value):
    agent = _agent(_no_owner)
    with pytest.raises(acp.RequestError) as refused:
        await agent.set_config_option(config_id, "s", value)
    assert refused.value.code == -32602


@pytest.mark.asyncio
async def test_another_surfaces_change_reaches_the_editor_as_config_option_update():
    truth = {"model": "m1", "provider": "custom", "reasoning_effort": "high"}
    agent = _agent(_reads(truth))
    agent._lazurio_config_fields("s", dict(truth), _models())

    truth["reasoning_effort"] = "low"
    await agent._project({"session_id": "s", "type": "session.info", "payload": {"reasoning_effort": "low"}})
    await _drain(agent)
    updates = _updates(agent)
    assert len(updates) == 1 and updates[0].session_update == "config_option_update", updates
    assert _options(updates[0].config_options)["thought_level"].current_value == "low"

    # The same level again, or a session.info without model or level (a title, pending rows): nothing.
    await agent._project({"session_id": "s", "type": "session.info", "payload": {"reasoning_effort": "low"}})
    await agent._project({"session_id": "s", "type": "session.info", "payload": {"title": "Plans"}})
    await _drain(agent)
    assert len(_updates(agent)) == 1

    # A model switch elsewhere rebuilds the picker for the new model.
    truth.update(model="m2")
    await agent._project({"session_id": "s", "type": "session.info", "payload": {"model": "m2", "provider": "custom"}})
    await _drain(agent)
    updates = _updates(agent)
    assert len(updates) == 2
    assert _options(updates[-1].config_options)["model"].current_value == "custom:m2"
    assert _options(updates[-1].config_options)["thought_level"].current_value == "low"


@pytest.mark.asyncio
async def test_a_change_published_while_the_reply_is_built_reaches_the_editor_after_the_reply():
    """The owner publishes a new model while the attach reply is being built, before the session has
    state to project it onto: the reply shows the old model, and the refresh at the end of the
    post-attach notices tells the editor the new one."""
    agent = _agent(_reads({"model": "m2", "provider": "custom", "reasoning_effort": "high"}))
    agent._after_attach = AsyncMock()  # the reply's post-attach notices run below, after the reply
    await agent._project({"session_id": "s", "type": "session.info", "payload": {"model": "m2", "provider": "custom"}})
    fields = await agent._session_fields("s", {"model": "m1", "provider": "custom", "reasoning_effort": "high"}, "s")
    assert _options(fields["config_options"])["model"].current_value == "custom:m1"
    await agent._lazurio_reconcile("s")
    assert [_options(u.config_options)["model"].current_value for u in _updates(agent)] == ["custom:m2"]


@pytest.mark.asyncio
async def test_an_older_event_never_overwrites_a_newer_answer():
    """Pablo's interleaving: an owner event for m2 asks for a refresh whose read is still out when this
    editor's own pick of m3 commits and is answered from the owner's newer snapshot. The older read
    lands first (the editor briefly sees m2), the adopted m3 snapshot asks for another read, which
    tells the editor m3, the answer shows m3, and a late copy of the m2 event changes nothing: the
    editor ends on the owner's m3."""
    truth = {"model": "m1", "provider": "custom", "reasoning_effort": "high"}
    sent, held, release = [], asyncio.Event(), asyncio.Event()

    async def rpc(method, **params):
        if method == "session.info":
            seen = dict(truth)  # what the owner holds when the read arrives
            if not held.is_set():
                held.set()
                await release.wait()
            return seen
        if method == "session.resume":
            return {"revision": 3, "execution_generation": 7, "info": dict(truth)}
        assert method == "session.mutate", method
        sent.append(params["payload"])
        truth.update(model=params["payload"]["model"])
        return {"session_id": "s", "operation": "model", "revision": 4}

    agent = _agent(rpc)
    agent._lazurio_config_fields("s", dict(truth), _models())
    truth.update(model="m2")
    await agent._project({"session_id": "s", "type": "session.info", "payload": {"model": "m2", "provider": "custom"}})
    await asyncio.wait_for(held.wait(), 5)  # the refresh's read of m2 is out

    pick = asyncio.create_task(agent.set_config_option("model", "s", "custom:m3"))
    while not sent:
        await asyncio.sleep(0)
    release.set()
    response = await asyncio.wait_for(pick, 5)
    assert _options(response.config_options)["model"].current_value == "custom:m3"
    await agent._project({"session_id": "s", "type": "session.info", "payload": {"model": "m2", "provider": "custom"}})
    await _drain(agent)

    shown = agent._lazurio_shown_options("s")
    assert _options(shown)["model"].current_value == "custom:m3"
    messages = [("update", _options(u.config_options)["model"].current_value) for u in _updates(agent)]
    assert messages == [("update", "custom:m2"), ("update", "custom:m3")], messages  # never back to m2


@pytest.mark.asyncio
async def test_a_change_an_adopted_snapshot_covers_reaches_the_editor_without_its_event():
    """A control that adopts the owner's fresh snapshot (upstream's ``session/set_model``, a typed
    ``/model``) makes the pump skip the events at or below that snapshot's watermark, including the
    owner's ``session.info`` for the change: the adoption itself asks for the refresh."""
    rpc, sent = _owner("model", {"model": "m1", "provider": "custom"}, {"model": "m2", "provider": "custom"})
    agent = _agent(rpc)
    agent._lazurio_config_fields("s", {"model": "m1", "provider": "custom"}, _models())
    agent._lazurio_states()["s"]["dirty"] = False  # the attach's own refresh already ran
    await agent.set_session_model(model_id="custom:m2", session_id="s")  # its session.info event never arrives
    await _drain(agent)
    assert [(op, payload) for op, payload, _, _ in sent] == [("model", {"model": "m2", "provider": "custom"})]
    assert [_options(u.config_options)["model"].current_value for u in _updates(agent)] == ["custom:m2"]


@pytest.mark.asyncio
async def test_a_reconnect_tells_the_editor_what_changed_while_the_connection_was_lost():
    """A change made while this viewer's connection was down is in the snapshot the reconnect adopts,
    whose watermark makes the pump skip its event. The re-attach adopts before ``_client`` installs
    the new connection, so the refresh waits for that and reads through it."""
    truth = {"model": "m2", "provider": "custom", "reasoning_effort": "low"}
    agent = _agent(_no_owner)
    agent._lazurio_config_fields("s", {"model": "m1", "provider": "custom", "reasoning_effort": "high"}, _models())
    agent._lazurio_states()["s"]["dirty"] = False  # the attach's own refresh already ran

    async def rpc(method, **params):
        if method == "session.resume":
            return {"revision": 5, "execution_generation": 7, "replay_epoch": "e2", "last_sequence": 9,
                    "info": dict(truth), "prompts": []}
        if method == "config.set":  # the re-attach asks for live reasoning again
            return {}
        assert method == "session.info", method
        return dict(truth)

    fresh = AsyncMock()
    fresh.rpc.side_effect = rpc
    agent._gateway = None  # ``_teardown`` dropped the lost connection
    async with agent._connect_lock:  # as ``_client`` does: re-attach first, then install the connection
        await agent._reattach(fresh)
        for _ in range(5):
            await asyncio.sleep(0)  # the refresh the adoption asked for is waiting for the connection
        agent._gateway = fresh
    await _drain(agent)
    shown = [(_options(u.config_options)["model"].current_value, _options(u.config_options)["thought_level"].current_value)
             for u in _updates(agent)]
    assert shown == [("custom:m2", "low")], shown


def _flaky_reads(truth, fail):
    """A fake owner whose ``session.info`` fails while *fail* says so, then answers *truth*."""
    reads = []

    async def rpc(method, **params):
        assert method == "session.info", method
        reads.append(method)
        if fail(len(reads)):
            from hermes_cli.gateway_client import GatewayClientError
            raise GatewayClientError("Gateway connection failed; no local fallback")
        return dict(truth)
    return rpc, reads


@pytest.mark.asyncio
async def test_a_failed_read_after_an_adopted_snapshot_is_retried_until_the_editor_follows(monkeypatch):
    """Pablo's third case: the adopted snapshot carries another surface's m2 (its event is under the
    snapshot's watermark), and the refresh's first owner read fails once. The request stays and is
    retried on the same connection, so the editor ends on m2."""
    monkeypatch.setattr(GatewayACPAgent, "_LAZURIO_RETRY_DELAYS", (0, 0, 0))
    rpc, reads = _flaky_reads({"model": "m2", "provider": "custom"}, lambda n: n == 1)
    agent = _agent(rpc)
    agent._lazurio_config_fields("s", {"model": "m1", "provider": "custom"}, _models())
    agent._lazurio_states()["s"]["dirty"] = False  # the attach's own refresh already ran
    await agent._adopt("s", {"execution_generation": 7, "replay_epoch": "e", "last_sequence": 2,
                             "info": {"model": "m2", "provider": "custom"}})
    await _drain(agent)
    assert len(reads) == 2
    assert [_options(u.config_options)["model"].current_value for u in _updates(agent)] == ["custom:m2"]
    assert agent._lazurio_states()["s"]["dirty"] is False


@pytest.mark.asyncio
async def test_retries_are_bounded_and_the_next_change_reads_again(monkeypatch):
    monkeypatch.setattr(GatewayACPAgent, "_LAZURIO_RETRY_DELAYS", (0, 0, 0))
    failing = {"on": True}
    rpc, reads = _flaky_reads({"model": "m2", "provider": "custom"}, lambda n: failing["on"])
    agent = _agent(rpc)
    agent._lazurio_config_fields("s", {"model": "m1", "provider": "custom"}, _models())
    await agent._lazurio_reconcile("s")
    await _drain(agent)
    assert len(reads) == 4 and _updates(agent) == []  # the read and three retries, then it waits
    assert agent._lazurio_states()["s"]["dirty"] is True  # the request stays
    failing["on"] = False
    await agent._project({"session_id": "s", "type": "session.info", "payload": {"model": "m2", "provider": "custom"}})
    await _drain(agent)
    assert len(reads) == 5
    assert [_options(u.config_options)["model"].current_value for u in _updates(agent)] == ["custom:m2"]


@pytest.mark.asyncio
async def test_a_retry_never_reads_through_another_connection(monkeypatch):
    """A read that failed with its connection is not retried through a reconnect's new connection:
    the reconnect's own adoption decides (upstream's reconnect tests pin every call there)."""
    monkeypatch.setattr(GatewayACPAgent, "_LAZURIO_RETRY_DELAYS", (0.05, 0.05, 0.05))
    rpc, reads = _flaky_reads({"model": "m2", "provider": "custom"}, lambda n: True)
    agent = _agent(rpc)
    agent._lazurio_config_fields("s", {"model": "m1", "provider": "custom"}, _models())
    await agent._lazurio_reconcile("s")
    replacement = AsyncMock()
    replacement.rpc.side_effect = _no_owner
    agent._gateway = replacement  # a reconnect installed a new connection before the retry's turn
    await _drain(agent)
    assert len(reads) == 1 and replacement.rpc.await_count == 0


@pytest.mark.asyncio
async def test_a_refresh_is_quiet_when_nothing_changed():
    reads = []

    async def rpc(method, **params):
        reads.append(method)
        return {"model": "m1", "provider": "custom", "reasoning_effort": "high"}

    agent = _agent(rpc)
    agent._lazurio_config_fields("s", {"model": "m1", "provider": "custom", "reasoning_effort": "high"}, _models())
    await agent._lazurio_reconcile("s")
    assert reads == ["session.info"] and _updates(agent) == []


@pytest.mark.asyncio
async def test_the_refresh_is_the_last_post_attach_notice_and_never_reconnects():
    calls = []

    async def rpc(method, **params):
        calls.append(method)
        return {"input_tokens": 0} if method == "session.stats" else {"model": "m1", "provider": "custom"}

    agent = _agent(rpc)
    agent._lazurio_config_fields("s", {"model": "m1", "provider": "custom"}, _models())
    await agent._after_attach("s", "s")
    assert calls == ["session.stats", "session.info"]
    # Without a live connection (aclose, a lost transport) it opens none.
    agent._lazurio_config_fields("s", {"model": "m1", "provider": "custom"}, _models())
    agent._gateway = None
    await agent._lazurio_reconcile("s")
    assert calls == ["session.stats", "session.info"]


@pytest.mark.asyncio
async def test_a_session_this_editor_never_attached_gets_no_update():
    agent = _agent(_no_owner)
    await agent._project({"session_id": "other", "type": "session.info", "payload": {"reasoning_effort": "low"}})
    await _drain(agent)
    assert _updates(agent) == []


@pytest.mark.asyncio
async def test_other_config_ids_keep_upstreams_answer_and_return_the_options_last_shown():
    agent = _agent(_no_owner)
    # Nothing shown yet: upstream's empty answer, and no call to the owner.
    assert (await agent.set_config_option("theme", "s", "dark")).config_options == []
    agent._lazurio_config_fields("s", {"model": "m1", "provider": "custom"}, _models())
    shown = (await agent.set_config_option("theme", "s", "dark")).config_options
    assert [option.id for option in shown] == ["model", "thought_level"]
