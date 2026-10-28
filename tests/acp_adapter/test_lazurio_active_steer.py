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
from hermes_cli.gateway_client import GatewayRPCError


class Owner:
    """A fake owner: ``prompt.submit`` admits ``a1``, which runs until :meth:`settle`; ``prompt.receipt``
    answers ``receipt_status`` with generation 7; ``session.steer`` queues, or refuses with
    ``steer_error``. A cancel is a failure of this overlay."""

    def __init__(self):
        self.calls, self.agent, self.receipt_status, self.steer_error = [], None, "started", None

    async def rpc(self, method, **params):
        self.calls.append((method, params))
        if method == "prompt.submit":
            return {"admission_id": "a1"}
        if method == "prompt.receipt":
            return {"status": self.receipt_status, "execution_generation": 7}
        if method == "session.steer":
            if self.steer_error is not None:
                raise GatewayRPCError(self.steer_error)
            return {"status": "queued"}
        if method == "prompt.cancel":
            raise AssertionError("a steer must never cancel the running turn")
        raise AssertionError(f"unexpected owner call {method}")

    async def settle(self, outcome="completed"):
        await self.agent._project({"session_id": "s", "type": "message.complete", "admission_id": "a1",
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


async def _running(agent):
    """A prompt of this editor that runs until the owner settles it."""
    running = asyncio.create_task(asyncio.wait_for(agent.prompt([acp.text_block("draft the notes")], "s"), 5))
    while not agent._admissions.get("s"):
        await asyncio.sleep(0)
    return running


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

    answer = await agent.ext_method("lazurio/steer", {"sessionId": "s", "text": "only cover the ACP changes"})

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
    answer = await agent.ext_method("lazurio/steer", {"sessionId": "s", "text": "too late"})
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
    answer = await agent.ext_method("lazurio/steer", {"sessionId": "s", "text": "just in time"})
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
        await agent.ext_method("lazurio/steer", {"sessionId": "s", "text": "go on"})
    await owner.settle()
    await running


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [{"sessionId": "s"}, {"sessionId": "s", "text": "  "}, {"text": "go"}, []])
async def test_a_steer_without_a_session_or_a_text_is_refused(params):
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
