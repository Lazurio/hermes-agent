"""Lazurio overlay: after this editor's Stop, its next prompt carries the interrupted request.

Buddy's chat steers by default: Lazurio T3 Code cancels the running turn and sends the new message as
the next prompt. Main's in-process ACP server merged the interrupted request into that prompt; the
gateway viewer does the same here, against a fake owner, with the interrupted text exactly once and
the new text exactly once. Overlay row: ``acp: after the editor's Stop, its next prompt carries the
interrupted request`` (docs/operations/lazurio-fork-release.md).
"""
import asyncio
import base64
from unittest.mock import AsyncMock

import acp
import pytest

from acp_adapter.gateway_server import GatewayACPAgent
from acp_adapter.server import _attach_interrupted_prompt


def _merged(interrupted, text):
    return _attach_interrupted_prompt(interrupted, text)


class Owner:
    """A fake owner: ``prompt.submit`` admits ``a1``, ``a2``…; a prompt whose newest part (its text,
    or a merge's correction) holds ``RUN`` keeps running until ``prompt.cancel`` (this editor's Stop)
    or :meth:`settle` (another surface) ends it, any other prompt completes at once. With a
    ``cancel_gate``, a cancel is accepted at once and the turn ends only when the gate opens."""

    def __init__(self):
        self.submitted, self.agent, self.hold_next, self.cancel_gate, self.later = [], None, False, None, []

    async def rpc(self, method, **params):
        if method == "prompt.submit":
            admission = f"a{len(self.submitted) + 1}"
            self.submitted.append({"text": params["text"], "input_id": params["input_id"],
                                   "attachments": params.get("attachments")})
            held, self.hold_next = self.hold_next, False  # the next prompt keeps running, whatever it holds
            if not held and "RUN" not in params["text"].rsplit("User correction/guidance after interrupt: ", 1)[-1]:
                await self.settle(admission, "completed")
            return {"admission_id": admission}
        if method == "prompt.cancel":
            if self.cancel_gate is None:
                await self.settle(params["admission_id"], "cancelled")
            else:
                async def settle_later(gate=self.cancel_gate, admission=params["admission_id"]):
                    await gate.wait()
                    await self.settle(admission, "cancelled")
                self.later.append(asyncio.create_task(settle_later()))
            return {}
        raise AssertionError(f"unexpected owner call {method}")

    async def settle(self, admission, outcome):
        await self.agent._project({"session_id": "s", "type": "message.complete", "admission_id": admission,
                                   "payload": {"outcome": outcome, "text": ""}})


def _png():
    return acp.image_block(base64.b64encode(bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
        "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")).decode(), "image/png")


def _agent():
    owner = Owner()
    agent = GatewayACPAgent()
    agent._gateway, agent._conn = AsyncMock(), AsyncMock()
    agent._gateway.rpc.side_effect = owner.rpc
    agent._snapshots["s"] = {"execution_generation": 1}
    owner.agent = agent
    return agent, owner


async def _send(agent, text, *blocks):
    return await asyncio.wait_for(agent.prompt([acp.text_block(text), *blocks] if text else list(blocks), "s"), 5)


async def _run_then_stop(agent, owner, text, *blocks):
    """A prompt that runs, then this editor's Stop (``session/cancel``) while it runs."""
    owner.hold_next = True
    running = asyncio.create_task(_send(agent, text, *blocks))
    while not agent._admissions.get("s"):
        await asyncio.sleep(0)
    await agent.cancel("s")
    return await running


@pytest.mark.asyncio
async def test_the_next_prompt_after_this_editors_stop_carries_the_interrupted_request_once():
    agent, owner = _agent()
    stopped = await _run_then_stop(agent, owner, "RUN draft the release notes")
    assert stopped.stop_reason == "cancelled"
    assert (await _send(agent, "only cover the ACP changes")).stop_reason == "end_turn"
    await _send(agent, "thanks")
    texts = [entry["text"] for entry in owner.submitted]
    assert texts == ["RUN draft the release notes",
                     _merged("RUN draft the release notes", "only cover the ACP changes"),
                     "thanks"]
    merged = texts[1]
    assert merged.count("RUN draft the release notes") == 1 and merged.count("only cover the ACP changes") == 1


@pytest.mark.asyncio
async def test_a_correction_sent_right_after_the_stop_is_merged_before_the_stopped_turn_ends():
    """Pablo's race: the editor sends its correction right after ``session/cancel``, while the stopped
    turn has not ended yet (the owner ends it later). The correction still reaches the owner as the
    merge, the interrupted text once and the correction once."""
    agent, owner = _agent()
    owner.cancel_gate, owner.hold_next = asyncio.Event(), True
    running = asyncio.create_task(_send(agent, "RUN draft the release notes"))
    while not agent._admissions.get("s"):
        await asyncio.sleep(0)
    await agent.cancel("s")  # the Stop is sent; the owner has not ended the turn yet
    assert not running.done()
    correction = asyncio.create_task(_send(agent, "only cover the ACP changes"))
    while len(owner.submitted) < 2:
        await asyncio.sleep(0)
    owner.cancel_gate.set()
    assert (await running).stop_reason == "cancelled"
    assert (await correction).stop_reason == "end_turn"
    merged = _merged("RUN draft the release notes", "only cover the ACP changes")
    assert [entry["text"] for entry in owner.submitted] == ["RUN draft the release notes", merged]
    assert merged.count("RUN draft the release notes") == 1 and merged.count("only cover the ACP changes") == 1


@pytest.mark.asyncio
async def test_a_second_steer_carries_the_first_merge_forward_once():
    """Two corrections in a row: each Stop interrupts the merged prompt, which is carried once more."""
    agent, owner = _agent()
    await _run_then_stop(agent, owner, "RUN task")
    await _run_then_stop(agent, owner, "RUN tweak one")
    await _send(agent, "tweak two")
    texts = [entry["text"] for entry in owner.submitted]
    assert texts[1] == _merged("RUN task", "RUN tweak one")
    assert texts[2] == _merged(_merged("RUN task", "RUN tweak one"), "tweak two")
    assert texts[2].count("RUN task") == 1 and texts[2].count("tweak two") == 1


@pytest.mark.asyncio
async def test_a_slash_command_keeps_the_interrupted_request_for_the_prompt_after_it():
    agent, owner = _agent()
    await _run_then_stop(agent, owner, "RUN task")
    await _send(agent, "/help")  # answered by the viewer, never submitted
    await _send(agent, "go on")
    assert [entry["text"] for entry in owner.submitted] == ["RUN task", _merged("RUN task", "go on")]


@pytest.mark.asyncio
async def test_an_idle_steer_carries_the_interrupted_request_as_on_main():
    agent, owner = _agent()
    await _run_then_stop(agent, owner, "RUN task")
    await _send(agent, "/steer use pytest")
    assert [entry["text"] for entry in owner.submitted] == ["RUN task", _merged("RUN task", "use pytest")]


@pytest.mark.asyncio
async def test_only_this_editors_stop_of_a_running_prompt_is_carried():
    agent, owner = _agent()
    await _send(agent, "a finished turn")
    await _send(agent, "next")
    # Another surface ends this editor's running prompt: nothing is carried either.
    running = asyncio.create_task(_send(agent, "RUN elsewhere stopped"))
    while not agent._admissions.get("s"):
        await asyncio.sleep(0)
    await owner.settle(agent._admissions["s"], "cancelled")
    await running
    await _send(agent, "after")
    assert [entry["text"] for entry in owner.submitted] == ["a finished turn", "next", "RUN elsewhere stopped", "after"]


@pytest.mark.asyncio
async def test_a_prompt_with_only_attachments_leaves_the_interrupted_request_for_the_next_text():
    agent, owner = _agent()
    await _run_then_stop(agent, owner, "RUN task")
    await _send(agent, "", _png())
    await _send(agent, "what do you see")
    texts = [entry["text"] for entry in owner.submitted]
    assert texts[1] == "" and owner.submitted[1]["attachments"]
    assert texts[2] == _merged("RUN task", "what do you see")


@pytest.mark.asyncio
async def test_a_prompt_with_text_and_an_image_leaves_the_interrupted_request_for_the_next_text():
    """Main merges only a text-only prompt; one with an attachment goes as sent and keeps the text."""
    agent, owner = _agent()
    await _run_then_stop(agent, owner, "RUN task")
    await _send(agent, "look at this", _png())
    await _send(agent, "and fix it")
    texts = [entry["text"] for entry in owner.submitted]
    assert texts == ["RUN task", "look at this", _merged("RUN task", "and fix it")]
    assert owner.submitted[1]["attachments"]


@pytest.mark.asyncio
async def test_an_interrupted_prompt_with_only_an_image_is_carried_as_mains_placeholder():
    agent, owner = _agent()
    await _run_then_stop(agent, owner, "", _png())
    await _send(agent, "describe it")
    assert [entry["text"] for entry in owner.submitted] == ["", _merged("[Image attachment]", "describe it")]


@pytest.mark.asyncio
async def test_an_interrupted_text_that_reads_as_a_command_is_not_carried():
    """Text sent with an attachment skips the viewer's slash commands, so ``/help …`` reaches the owner
    as text; carried into a merge, a retry of that merge would run it as the command."""
    agent, owner = _agent()
    await _run_then_stop(agent, owner, "/help me read this screenshot", _png())
    await _send(agent, "never mind")
    assert [entry["text"] for entry in owner.submitted] == ["/help me read this screenshot", "never mind"]


@pytest.mark.asyncio
async def test_a_retry_of_a_lost_submit_resends_the_merged_text_with_the_same_identity():
    from hermes_cli.gateway_client import GatewayClientError
    agent, owner = _agent()
    await _run_then_stop(agent, owner, "RUN task")
    lost = {"once": True}
    original = owner.rpc

    async def flaky(method, **params):
        if method == "prompt.submit" and lost["once"]:
            lost["once"] = False
            owner.submitted.append({"text": params["text"], "input_id": params["input_id"], "attachments": None})
            raise GatewayClientError("Gateway connection failed; no local fallback")
        return await original(method, **params)

    agent._gateway.rpc.side_effect = flaky
    with pytest.raises(GatewayClientError):
        await _send(agent, "correction")
    await _send(agent, "correction")
    first, retry = owner.submitted[1], owner.submitted[2]
    assert first["text"] == retry["text"] == _merged("RUN task", "correction")
    assert first["input_id"] == retry["input_id"]
