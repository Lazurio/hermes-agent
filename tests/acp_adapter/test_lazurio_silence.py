"""Lazurio overlay: a reply of intentional silence (NO_REPLY, [SILENT]) sends nothing to the editor.

A fake owner publishes each turn's frames through the viewer's real event pump, as
``test_gateway_delivery_shapes`` does. A reply that streams as a bare silence marker, or whose final
text is one, reaches the editor as no ``agent_message_chunk`` and the turn ends as usual; a reply
that only starts like a marker, or mentions one, arrives whole, in order and in one message. Overlay
row: ``acp: a reply of intentional silence (NO_REPLY, [SILENT]) sends nothing to the client``
(docs/operations/lazurio-fork-release.md).
"""
import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import acp
import pytest

from acp_adapter.gateway_server import GatewayACPAgent
from gateway.run_turn import _unexpected_silence_reply

# Each marker as the model streams it, in several deltas.
MARKERS = {"NO_REPLY": ["NO", "_REP", "LY"], "[SILENT]": ["[", "SIL", "ENT]"], "*NO_REPLY*": ["*", "NO_REPLY", "*"]}


class Gateway:
    def __init__(self):
        self.events, self.rpc, self.seq = asyncio.Queue(), AsyncMock(), 0

    def publish(self, admission_id, kind, payload):
        self.seq += 1
        self.events.put_nowait({"params": {"session_id": "s", "type": kind, "admission_id": admission_id,
                                           "replay_epoch": "e", "seq": self.seq, "payload": payload}})


@asynccontextmanager
async def viewer():
    agent = GatewayACPAgent()
    agent._gateway, agent._conn = Gateway(), AsyncMock()
    agent._snapshots["s"] = {"replay_epoch": "e", "last_sequence": 0}
    agent._event_task = asyncio.create_task(agent._events())
    try:
        yield agent
    finally:
        agent._event_task.cancel()


def deltas(*texts):
    return [("message.delta", {"text": text}) for text in texts]


def complete(text, outcome="completed", **extra):
    return [("message.complete", {"outcome": outcome, "text": text, **extra})]


def updates_of(agent):
    return [call.kwargs["update"] for call in agent._conn.session_update.await_args_list]


def message_chunks(updates):
    return [(update.content.text, update.message_id) for update in updates
            if update.session_update == "agent_message_chunk"]


def publish_turn(agent, admission_id, frames):
    agent._conn.session_update.reset_mock()
    agent._gateway.rpc.return_value = {"admission_id": admission_id}
    for kind, payload in frames:
        agent._gateway.publish(admission_id, kind, payload)
    return agent.prompt([acp.text_block("hello")], "s")


async def turn(agent, admission_id, frames):
    """One prompt whose turn the owner plays as *frames*: ``(stop reason, the editor's updates)``."""
    response = await asyncio.wait_for(publish_turn(agent, admission_id, frames), 5)
    assert agent._failure is None
    return response.stop_reason, updates_of(agent)


# How the owner settles a reply that streamed as a marker: with nothing (a notice the gateway itself
# injected), with its notice in the marker's place (an editor's prompt, which it cannot tell from a
# person's message), with the marker again; and a reply that was never streamed.
SETTLEMENTS = {
    "streamed, settled with nothing": lambda marker: deltas(*MARKERS[marker]) + complete(""),
    "streamed, settled with no text": lambda marker: deltas(*MARKERS[marker]) + complete(None),
    "streamed, the owner's notice in its place": lambda marker: deltas(*MARKERS[marker]) + complete(
        _unexpected_silence_reply()),
    "streamed, settled with the marker": lambda marker: deltas(*MARKERS[marker]) + complete(marker),
    "unstreamed": lambda marker: complete(marker),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("settlement", sorted(SETTLEMENTS))
@pytest.mark.parametrize("marker", sorted(MARKERS))
async def test_a_reply_of_intentional_silence_sends_no_message_chunk_and_ends_the_turn_normally(marker, settlement):
    async with viewer() as agent:
        stop_reason, updates = await turn(agent, "a1", SETTLEMENTS[settlement](marker))
        assert stop_reason == "end_turn"
        assert message_chunks(updates) == []
        # Nothing held back leaks into the next reply, which arrives as usual.
        stop_reason, updates = await turn(agent, "a2", deltas("Second", " reply.") + complete("Second reply."))
        assert stop_reason == "end_turn"
        assert "".join(text for text, _ in message_chunks(updates)) == "Second reply."


@pytest.mark.asyncio
@pytest.mark.parametrize("parts", [["No", " problem, I'll check the delegation."], ["NO", " worries"],
                                   ["\n\n", "*", "No", "*, that is not it."]])
async def test_a_reply_that_starts_like_a_marker_arrives_whole_in_order_and_in_one_message(parts):
    reply = "".join(parts)
    async with viewer() as agent:
        stop_reason, updates = await turn(agent, "a1", deltas(*parts) + complete(reply))
    chunks = message_chunks(updates)
    assert stop_reason == "end_turn"
    assert "".join(text for text, _ in chunks) == reply
    assert len({message_id for _, message_id in chunks}) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("frames", [deltas("Use ", "NO_REPLY", " when nothing is new.")
                                    + complete("Use NO_REPLY when nothing is new."),
                                    complete("Use NO_REPLY when nothing is new.")], ids=["streamed", "unstreamed"])
async def test_a_reply_that_mentions_the_marker_mid_sentence_is_delivered(frames):
    async with viewer() as agent:
        _, updates = await turn(agent, "a1", frames)
    assert "".join(text for text, _ in message_chunks(updates)) == "Use NO_REPLY when nothing is new."


@pytest.mark.asyncio
@pytest.mark.parametrize("final", ["No.", None])
async def test_a_short_reply_that_could_still_have_become_a_marker_is_shown_once_at_the_end(final):
    async with viewer() as agent:
        _, updates = await turn(agent, "a1", deltas("No.") + complete(final))
    assert [text for text, _ in message_chunks(updates)] == ["No."]


@pytest.mark.asyncio
async def test_what_a_segment_held_goes_out_before_its_tool_call_and_a_silent_final_after_it_sends_nothing():
    tool = {"tool_call_id": "t1", "tool_name": "terminal", "args": {}}
    async with viewer() as agent:
        _, updates = await turn(agent, "a1", deltas("No") + [("tool.start", tool),
                                                             ("tool.complete", {**tool, "result": "ok"})]
                                + deltas("NO_", "REPLY") + complete(""))
    assert [update.session_update for update in updates] == ["agent_message_chunk", "tool_call", "tool_call_update"]
    assert updates[0].content.text == "No"


@pytest.mark.asyncio
async def test_thought_chunks_pass_while_the_reply_is_held():
    async with viewer() as agent:
        _, updates = await turn(agent, "a1", deltas("NO") + [("reasoning.delta", {"text": "Nothing new."})]
                                + deltas("_REPLY") + complete(""))
    assert [(update.session_update, update.content.text) for update in updates] == [
        ("agent_thought_chunk", "Nothing new.")]


@pytest.mark.asyncio
@pytest.mark.parametrize("final", [{"outcome": "completed", "text": "The rewritten reply.", "response_transformed": True},
                                   {"outcome": "failed", "text": "The provider failed."}],
                         ids=["a plugin's rewrite", "a failed turn's diagnosis"])
async def test_the_owners_own_text_after_a_streamed_marker_is_still_shown(final):
    async with viewer() as agent:
        prompt = publish_turn(agent, "a1", deltas(*MARKERS["NO_REPLY"]) + [("message.complete", final)])
        if final["outcome"] == "failed":
            with pytest.raises(Exception, match="admitted_turn_failed"):
                await asyncio.wait_for(prompt, 5)
        else:
            await asyncio.wait_for(prompt, 5)
        assert [text for text, _ in message_chunks(updates_of(agent))] == [final["text"]]


@pytest.mark.asyncio
async def test_a_silent_reply_settled_from_a_retrys_receipt_sends_nothing():
    """A retried submit the owner already settled reports its turn from the receipt's raw final text."""
    async with viewer() as agent:
        async def rpc(method, **kwargs):
            if method == "prompt.submit":
                return {"admission_id": "a1", "status": "terminal", "outcome": "completed"}
            assert method == "prompt.receipt"
            return {"admission_id": "a1", "status": "terminal", "outcome": "completed",
                    "result": {"final_response": "NO_REPLY"}}

        agent._gateway.rpc.side_effect = rpc
        response = await asyncio.wait_for(agent.prompt([acp.text_block("hello")], "s"), 5)
        assert response.stop_reason == "end_turn"
        assert message_chunks(updates_of(agent)) == []
