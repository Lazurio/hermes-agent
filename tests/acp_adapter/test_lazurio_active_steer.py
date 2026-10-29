"""Lazurio overlay: a message sent while Buddy works steers his running turn and never stops it.

Lazurio T3 Code steered every ACP agent by cancelling the running turn and sending the message as the
next prompt (Lazurio/t3code#138). The gateway viewer now answers the extension request
``_lazurio/steer``: it steers this editor's running admission through the owner's ``session.steer``,
never cancels it, never streams text and never starts a turn; where nothing of this editor runs, it
says so, and the client sends the message as the next prompt. Against a fake owner. Overlay row:
``acp: a message sent while Buddy works steers his running turn, never stops it``
(docs/operations/lazurio-fork-release.md).
"""
import asyncio
from unittest.mock import AsyncMock

import acp
import pytest
from acp.exceptions import RequestError

from acp_adapter.gateway_server import GatewayACPAgent
from acp_adapter.lazurio_active_steer import TURN_META
from hermes_cli.gateway_client import GatewayRPCError


class Owner:
    """A fake owner: each ``prompt.submit`` admits the next ``a<n>``, which runs until :meth:`settle`;
    ``prompt.receipt`` answers ``receipt_status`` with generation 7; ``session.steer`` waits for
    ``steer_gate`` when one is set, then queues, or refuses with ``steer_error``. A cancel is a
    failure of this overlay."""

    def __init__(self):
        self.calls, self.agent, self.receipt_status, self.steer_error = [], None, "started", None
        self.steer_gate = None

    async def rpc(self, method, **params):
        self.calls.append((method, params))
        if method == "prompt.submit":
            return {"admission_id": f"a{len(self.called('prompt.submit'))}"}
        if method == "prompt.receipt":
            return {"status": self.receipt_status, "execution_generation": 7}
        if method == "session.steer":
            if self.steer_gate is not None:
                await self.steer_gate.wait()
            if self.steer_error is not None:
                raise GatewayRPCError(self.steer_error)
            return {"status": "queued"}
        if method == "prompt.cancel":
            raise AssertionError("a steer must never cancel the running turn")
        raise AssertionError(f"unexpected owner call {method}")

    async def settle(self, admission_id="a1", outcome="completed"):
        await self.agent._project({"session_id": "s", "type": "message.complete", "admission_id": admission_id,
                                   "payload": {"outcome": outcome, "text": ""}})

    def called(self, method):
        return [params for name, params in self.calls if name == method]


def _agent():
    owner = Owner()
    agent = GatewayACPAgent()
    agent._gateway, agent._conn = AsyncMock(), AsyncMock()
    agent._gateway.rpc.side_effect = owner.rpc
    agent._snapshots["s"] = {"execution_generation": 1}
    owner.agent = agent
    return agent, owner


async def _running(agent, turn="t1", admission="a1"):
    """A prompt of this editor, for the client's turn *turn*, that runs until the owner settles it."""
    meta = {} if turn is None else {TURN_META: turn}
    running = asyncio.create_task(asyncio.wait_for(
        agent.prompt([acp.text_block("draft the notes")], "s", **meta), 5))
    while agent._admissions.get("s") != admission:
        await asyncio.sleep(0)
    return running


def _steer(text, turn="t1", message="m1"):
    return {"sessionId": "s", "turnId": turn, "messageId": message, "text": text}


def _streamed(agent):
    return list(agent._conn.session_update.await_args_list)


@pytest.mark.asyncio
async def test_initialize_names_the_steer_extension_in_the_agent_capabilities_meta():
    response = await GatewayACPAgent().initialize(protocol_version=1)
    wire = response.model_dump(by_alias=True, exclude_none=True)
    assert wire["agentCapabilities"]["_meta"] == {"lazurio.activeSteer": 1}
    # The capabilities main advertises stay as they were.
    assert wire["agentCapabilities"]["loadSession"] is True
    assert wire["agentCapabilities"]["promptCapabilities"]["image"] is True


@pytest.mark.asyncio
async def test_a_steer_while_the_prompt_runs_reaches_its_turn_and_nothing_is_cancelled_or_streamed():
    agent, owner = _agent()
    running = await _running(agent)
    streamed_before = len(_streamed(agent))

    answer = await agent.ext_method("lazurio/steer", _steer("only cover the ACP changes"))

    assert answer == {"steered": True}
    # The running admission's own generation, as main's /steer steers it.
    assert owner.called("prompt.receipt") == [{"session_id": "s", "admission_id": "a1"}]
    assert owner.called("session.steer") == [{"session_id": "s", "text": "only cover the ACP changes",
                                               "execution_generation": 7}]
    assert owner.called("prompt.cancel") == []
    assert len(owner.called("prompt.submit")) == 1  # no turn started for the steer
    assert len(_streamed(agent)) == streamed_before  # no confirmation text in Buddy's answer
    assert not running.done()
    await owner.settle()
    assert (await running).stop_reason == "end_turn"


@pytest.mark.asyncio
async def test_where_no_prompt_of_this_editor_runs_the_steer_says_so_and_starts_nothing():
    agent, owner = _agent()
    answer = await agent.ext_method("lazurio/steer", _steer("too late"))
    assert answer == {"steered": False, "reason": "not_running"}
    assert owner.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("ended", ["receipt", "stale_generation"])
async def test_a_turn_that_ended_meanwhile_is_not_running_and_the_message_is_left_to_the_client(ended):
    agent, owner = _agent()
    running = await _running(agent)
    if ended == "receipt":
        owner.receipt_status = "terminal"
    else:
        owner.steer_error = "stale_generation"
    answer = await agent.ext_method("lazurio/steer", _steer("just in time"))
    assert answer == {"steered": False, "reason": "not_running"}
    assert owner.called("prompt.cancel") == []
    assert len(owner.called("prompt.submit")) == 1
    await owner.settle()
    await running


@pytest.mark.asyncio
async def test_another_refusal_of_the_owner_is_the_requests_error():
    agent, owner = _agent()
    running = await _running(agent)
    owner.steer_error = "permission_denied"
    with pytest.raises(GatewayRPCError, match="permission_denied"):
        await agent.ext_method("lazurio/steer", _steer("go on"))
    # A failed steer is not remembered: the same message, asked again, is steered.
    owner.steer_error = None
    assert await agent.ext_method("lazurio/steer", _steer("go on")) == {"steered": True}
    assert len(owner.called("session.steer")) == 2
    await owner.settle()
    await running


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [
    {"sessionId": "s"}, _steer("  "), {"turnId": "t1", "messageId": "m1", "text": "go"},
    {"sessionId": "s", "messageId": "m1", "text": "go"}, _steer("go", turn=" "),
    {"sessionId": "s", "turnId": "t1", "text": "go"}, _steer("go", message=""), []])
async def test_a_steer_without_a_session_a_turn_a_message_or_a_text_is_refused(params):
    agent, owner = _agent()
    with pytest.raises(RequestError) as refused:
        await agent.ext_method("lazurio/steer", params)
    assert refused.value.code == -32602
    assert owner.calls == []


@pytest.mark.asyncio
async def test_any_other_extension_request_is_not_found():
    agent, _ = _agent()
    with pytest.raises(RequestError) as refused:
        await agent.ext_method("lazurio/unknown", {})
    assert refused.value.code == -32601


@pytest.mark.asyncio
async def test_a_steer_for_a_turn_that_ended_never_reaches_the_next_one():
    """Lazurio/t3code#141, review: the client's turn A ended and its next turn B started while A's
    steer was on its way. The steer names A, so B is never steered; a steer naming B is."""
    agent, owner = _agent()
    first = await _running(agent, turn="tA", admission="a1")
    await owner.settle("a1")
    await first
    second = await _running(agent, turn="tB", admission="a2")

    late = await agent.ext_method("lazurio/steer", _steer("only for A", turn="tA"))

    assert late == {"steered": False, "reason": "not_running"}
    assert owner.called("session.steer") == []
    assert owner.called("prompt.receipt") == []
    assert await agent.ext_method("lazurio/steer", _steer("for B", turn="tB", message="m2")) == {"steered": True}
    assert owner.called("prompt.receipt") == [{"session_id": "s", "admission_id": "a2"}]
    await owner.settle("a2")
    await second


@pytest.mark.asyncio
async def test_a_prompt_that_names_no_turn_is_never_steered():
    agent, owner = _agent()
    running = await _running(agent, turn=None)
    assert await agent.ext_method("lazurio/steer", _steer("go on")) == {"steered": False, "reason": "not_running"}
    assert owner.called("session.steer") == []
    await owner.settle()
    await running


@pytest.mark.asyncio
async def test_a_message_asked_again_is_steered_once():
    """Lazurio/t3code#141, review: a retry after a lost answer, or a second request while the first
    still runs, never puts the same guidance into the turn twice."""
    agent, owner = _agent()
    running = await _running(agent)

    assert await agent.ext_method("lazurio/steer", _steer("only the ACP changes")) == {"steered": True}
    assert await agent.ext_method("lazurio/steer", _steer("only the ACP changes")) == {"steered": True}
    assert len(owner.called("session.steer")) == 1

    owner.steer_gate = asyncio.Event()
    both = asyncio.gather(*(agent.ext_method("lazurio/steer", _steer("and the tests", message="m2"))
                            for _ in range(2)))
    while len(owner.called("session.steer")) < 2:
        await asyncio.sleep(0)
    owner.steer_gate.set()
    assert await both == [{"steered": True}, {"steered": True}]
    assert [call["text"] for call in owner.called("session.steer")] == ["only the ACP changes", "and the tests"]
    await owner.settle()
    await running


@pytest.mark.asyncio
async def test_a_message_refused_before_its_turn_started_is_steered_when_asked_again():
    """The client asks again while a not yet started prompt stays unsettled; the refusal is not kept."""
    agent, owner = _agent()
    running = await _running(agent)
    owner.receipt_status = "queued"
    assert await agent.ext_method("lazurio/steer", _steer("go on")) == {"steered": False, "reason": "not_running"}
    owner.receipt_status = "started"
    assert await agent.ext_method("lazurio/steer", _steer("go on")) == {"steered": True}
    assert len(owner.called("session.steer")) == 1
    await owner.settle()
    await running


@pytest.mark.asyncio
async def test_a_steer_asked_again_after_its_turn_ended_and_the_next_started_answers_as_it_did():
    """Lazurio/hermes-agent#26, review: A took the steer, its answer was lost, A ended and B
    started; the retry for A answers that steer instead of not_running, so the client never sends
    the message again as a new turn."""
    agent, owner = _agent()
    first = await _running(agent, turn="tA", admission="a1")
    assert await agent.ext_method("lazurio/steer", _steer("only the ACP changes", turn="tA")) == {"steered": True}
    await owner.settle("a1")
    await first
    second = await _running(agent, turn="tB", admission="a2")

    again = await agent.ext_method("lazurio/steer", _steer("only the ACP changes", turn="tA"))

    assert again == {"steered": True}
    assert len(owner.called("session.steer")) == 1
    # A message A never took is still refused for A.
    assert await agent.ext_method("lazurio/steer", _steer("late", turn="tA", message="m2")) == {
        "steered": False, "reason": "not_running"}
    await owner.settle("a2")
    await second


class ReceiptOwner(Owner):
    """An owner that keeps keyed steer receipts, as gateway/lazurio_steer_receipts.py does: the first
    keyed steer is applied and its answer then lost (``lose_first``)."""

    def __init__(self):
        super().__init__()
        self.applied, self.receipts, self.lose_first = [], {}, True

    async def rpc(self, method, **params):
        if method != "session.steer" or "idempotency_key" not in params:
            return await super().rpc(method, **params)
        key = (params["execution_generation"], params["idempotency_key"])
        if key not in self.receipts and self.steer_error is not None:
            return await super().rpc(method, **params)
        self.calls.append((method, params))
        if key not in self.receipts:
            self.applied.append(params["text"])
            self.receipts[key] = {"status": "queued"}
            if self.lose_first:
                self.lose_first = False
                raise ConnectionResetError("the answer was lost")
        return self.receipts[key]


def _receipt_agent():
    from gateway.lazurio_steer_receipts import CAPABILITY

    owner = ReceiptOwner()
    agent = GatewayACPAgent()
    agent._gateway, agent._conn = AsyncMock(), AsyncMock()
    agent._gateway.rpc.side_effect = owner.rpc
    agent._snapshots["s"] = {"execution_generation": 1}
    agent._lazurio_owner_capabilities = (CAPABILITY,)
    owner.agent = agent
    return agent, owner


@pytest.mark.asyncio
async def test_a_steer_whose_owner_answer_was_lost_is_applied_once_when_asked_again():
    """Lazurio/hermes-agent#26, review: the owner applied the steer, its answer was lost; the retry
    carries the same key, so the owner answers its receipt instead of applying it again."""
    agent, owner = _receipt_agent()
    running = await _running(agent, turn="tA", admission="a1")

    with pytest.raises(ConnectionResetError):
        await agent.ext_method("lazurio/steer", _steer("only the ACP changes", turn="tA"))
    assert await agent.ext_method("lazurio/steer", _steer("only the ACP changes", turn="tA")) == {"steered": True}

    assert owner.applied == ["only the ACP changes"]
    assert [call["idempotency_key"] for call in owner.called("session.steer")] == ["tA:m1", "tA:m1"]
    await owner.settle("a1")
    await running


@pytest.mark.asyncio
async def test_an_owner_receipt_answers_a_retry_even_after_the_turn_ended():
    agent, owner = _receipt_agent()
    running = await _running(agent, turn="tA", admission="a1")
    with pytest.raises(ConnectionResetError):
        await agent.ext_method("lazurio/steer", _steer("only the ACP changes", turn="tA"))
    owner.receipt_status = "terminal"  # the turn ended before the retry

    assert await agent.ext_method("lazurio/steer", _steer("only the ACP changes", turn="tA")) == {"steered": True}
    assert owner.applied == ["only the ACP changes"]
    # A message the ended turn never took finds no receipt: the owner's fence answers it.
    owner.steer_error = "stale_generation"
    assert await agent.ext_method("lazurio/steer", _steer("late", turn="tA", message="m2")) == {
        "steered": False, "reason": "not_running"}
    await owner.settle("a1")
    await running


@pytest.mark.asyncio
async def test_a_lost_owner_answer_is_asked_again_about_its_own_turn_after_the_next_started():
    """Lazurio/t3code#141, review round 2: the owner applied A's steer and its answer was lost, A
    ended and B started. The retry asks the owner about A's admission under the same key, so it
    answers A's receipt, and the client never sends the message again as a new turn."""
    agent, owner = _receipt_agent()
    first = await _running(agent, turn="tA", admission="a1")
    with pytest.raises(ConnectionResetError):
        await agent.ext_method("lazurio/steer", _steer("only the ACP changes", turn="tA"))
    await owner.settle("a1")
    await first
    second = await _running(agent, turn="tB", admission="a2")
    owner.receipt_status = "terminal"  # A's admission

    again = await agent.ext_method("lazurio/steer", _steer("only the ACP changes", turn="tA"))

    assert again == {"steered": True}
    assert owner.applied == ["only the ACP changes"]
    assert [call["admission_id"] for call in owner.called("prompt.receipt")] == ["a1", "a1"]
    assert [call["idempotency_key"] for call in owner.called("session.steer")] == ["tA:m1", "tA:m1"]
    # A message A never took is still refused for A, and B is never asked about it.
    assert await agent.ext_method("lazurio/steer", _steer("late", turn="tA", message="m2")) == {
        "steered": False, "reason": "not_running"}
    assert len(owner.called("session.steer")) == 2
    await owner.settle("a2")
    await second
