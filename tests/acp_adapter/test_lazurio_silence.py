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
from itertools import groupby
from unittest.mock import AsyncMock

import acp
import pytest

from acp_adapter.gateway_server import GatewayACPAgent
from gateway.run_turn import _unexpected_silence_reply
from tests.waiting import async_wait_until

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
async def test_what_a_segment_held_goes_out_before_interim_commentary_and_a_silent_final_after_it_sends_nothing():
    interim = ("message.interim", {"text": "Checking the list.", "already_streamed": False})
    async with viewer() as agent:
        _, updates = await turn(agent, "a1", deltas("No") + [interim] + deltas("NO_", "REPLY") + complete(""))
    assert [text for text, _ in message_chunks(updates)] == ["No", "Checking the list.\n\n"]


# Buddy looks something up (``delegace seznam``) and then decides, as the owner streams it: commentary
# the editor is shown at once, the end of that segment (a delta without text, which the agent sends
# before it runs its tools), the tool call, and the final reply as a segment of its own, which the
# agent opens with a paragraph break.
COMMENTARY = deltas("Podívám se", " do seznamu.", None)
TOOL_CALL = [("tool.start", {"tool_call_id": "t1", "tool_name": "terminal", "args": {"command": "delegace seznam"}}),
             ("tool.complete", {"tool_call_id": "t1", "tool_name": "terminal", "result": "nic nového"})]


def kinds_in_order(updates):
    """The updates' kinds with each run of chunks counted once (how the text is split is not the point)."""
    return [kind for kind, _ in groupby(update.session_update for update in updates)]


def message_chunks_around_the_tool_call(updates):
    """``(before, after)``: the message chunks before the tool call and after its completion."""
    kinds = [update.session_update for update in updates]
    return (message_chunks(updates[:kinds.index("tool_call")]),
            message_chunks(updates[kinds.index("tool_call_update") + 1:]))


@pytest.mark.asyncio
@pytest.mark.parametrize("final", ["", None, _unexpected_silence_reply(), "NO_REPLY"])
# The boundary comes before the agent's tool round whether or not a tool then starts: a call stopped
# before it runs projects no ``tool.start``.
@pytest.mark.parametrize("tool", [TOOL_CALL, []], ids=["a tool ran", "no tool started"])
async def test_a_silent_final_after_shown_commentary_sends_no_chunk_of_the_marker(tool, final):
    async with viewer() as agent:
        stop_reason, updates = await turn(agent, "a1", COMMENTARY + tool + deltas("\n\nNO", "_REP", "LY")
                                          + complete(final))
    assert stop_reason == "end_turn"
    assert kinds_in_order(updates) == ["agent_message_chunk", *(["tool_call", "tool_call_update"] if tool else [])]
    assert "".join(text for text, _ in message_chunks(updates)) == "Podívám se do seznamu."


@pytest.mark.asyncio
async def test_a_final_that_starts_like_a_marker_after_shown_commentary_and_a_tool_call_arrives_whole_in_its_bubble():
    async with viewer() as agent:
        _, updates = await turn(agent, "a1", COMMENTARY + TOOL_CALL + deltas("\n\nNo", " problem", ", nothing changed.")
                                + complete("No problem, nothing changed."))
    assert kinds_in_order(updates) == ["agent_message_chunk", "tool_call", "tool_call_update", "agent_message_chunk"]
    commentary, final = message_chunks_around_the_tool_call(updates)
    assert "".join(text for text, _ in commentary) == "Podívám se do seznamu."
    assert "".join(text for text, _ in final) == "\n\nNo problem, nothing changed."
    # The commentary's chunks share one message; the reply after the tool call opens the next one.
    assert len({message_id for _, message_id in commentary}) == len({message_id for _, message_id in final}) == 1
    assert commentary[0][1] != final[0][1]


def by_message(updates):
    """``[(messageId, text)]`` of the message chunks, one entry per message, in order."""
    return [(message_id, "".join(text for text, _ in chunks))
            for message_id, chunks in groupby(message_chunks(updates), key=lambda chunk: chunk[1])]


@pytest.mark.asyncio
async def test_a_final_after_a_boundary_without_a_tool_call_is_shown_once_in_its_own_message():
    """Pablo's probe on #11: commentary, the owner's boundary delta, a final that starts like a marker and its
    completion. The final is measured on its own segment, never appended a second time."""
    async with viewer() as agent:
        _, updates = await turn(agent, "a1", deltas("Checking.", None, "No", " problem.") + complete("No problem."))
    (first, commentary), (second, final) = by_message(updates)
    assert (commentary, final) == ("Checking.", "No problem.") and first != second


async def started_turn(agent):
    """A prompt whose admission ``a1`` the owner has started; a replay gap re-attaches it as started."""
    async def answer(method, **params):
        if method == "prompt.submit":
            return {"admission_id": "a1", "status": "queued"}
        assert method == "session.resume", method
        return {"replay_epoch": "e2", "last_sequence": 0, "prompts": [], "resumed": True,
                "pending": [{"admission_id": "a1", "status": "started"}]}

    agent._conn.session_update.reset_mock()
    agent._gateway.rpc.side_effect = answer
    prompt = asyncio.create_task(agent.prompt([acp.text_block("hello")], "s"))
    await async_wait_until(lambda: agent._admissions.get("s") == "a1")
    return prompt


async def across_a_replay_gap(agent, before, after):
    """Play *before*, a replay gap the pump re-attaches the started turn across, then *after*."""
    prompt = await started_turn(agent)
    for kind, payload in before + [("session.replay_gap", {})]:
        agent._gateway.publish("a1", kind, payload)
    await async_wait_until(lambda: agent._snapshots["s"].get("resumed") and "a1" in agent._cut)
    for kind, payload in after:
        agent._gateway.publish("a1", kind, payload)
    response = await asyncio.wait_for(prompt, 5)
    assert agent._failure is None
    return response.stop_reason, updates_of(agent)


@pytest.mark.asyncio
@pytest.mark.parametrize("after", [[("message.interim", {"text": "No", "already_streamed": True})] + complete(""),
                                   complete("")], ids=["an interim the owner streamed", "no interim"])
async def test_text_held_when_a_replay_gap_cut_the_reply_is_not_lost(after):
    """Pablo's probe on #11: held ``No``, a gap re-attaches the started turn, an interim ``No`` that the owner
    already streamed and an empty completion. Before the hold the editor showed ``No``; it still does, once."""
    async with viewer() as agent:
        stop_reason, updates = await across_a_replay_gap(agent, deltas("No"), after)
    assert stop_reason == "end_turn"
    assert [text for text, _ in message_chunks(updates)] == ["No"]


@pytest.mark.asyncio
async def test_text_held_when_a_replay_gap_cut_the_reply_is_shown_once_with_the_rest():
    async with viewer() as agent:
        _, updates = await across_a_replay_gap(agent, deltas("No"), complete("No problem, nothing changed."))
    assert "".join(text for text, _ in message_chunks(updates)) == "No problem, nothing changed."


@pytest.mark.asyncio
@pytest.mark.parametrize("before,final", [
    *((deltas(*MARKERS["NO_REPLY"]), final) for final in ["", None, _unexpected_silence_reply(), "NO_REPLY"]),
    (deltas("NO", "_REP"), "NO_REPLY")],
    ids=["the marker held, settled with nothing", "the marker held, settled with no text",
         "the marker held, the owner's notice in its place", "the marker held, settled with the marker",
         "cut mid-marker, settled with the marker"])
async def test_a_silent_reply_a_replay_gap_cut_sends_no_message_chunk(before, final):
    async with viewer() as agent:
        stop_reason, updates = await across_a_replay_gap(agent, before, complete(final))
    assert stop_reason == "end_turn"
    assert message_chunks(updates) == []


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
