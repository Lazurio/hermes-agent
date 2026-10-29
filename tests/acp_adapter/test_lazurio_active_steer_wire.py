"""Lazurio overlay: a message sent while Buddy works steers his running turn, on the real wire.

An ordinary isolated gateway, the real ``hermes acp`` stdio process and a loopback model. While a
prompt's model request is held, the extension request ``_lazurio/steer`` naming that prompt's turn
steers it: the turn is never cancelled, and within the same prompt the model answers, then gets the guidance as the next
user message and answers again, both streamed to the editor before the one ``end_turn``. Once the turn
has ended, a steer answers ``not_running`` and starts nothing. Overlay row: ``acp: a message sent while
Buddy works steers his running turn, never stops it`` (docs/operations/lazurio-fork-release.md).
"""
import asyncio
import json
import threading

import pytest

from tests.acp_adapter.test_gateway_sessions import editor


def _messages(request):
    return json.dumps(request["messages"])


async def _blocked_prompt(acp, sid, model_peer, text, turn):
    """A prompt for the client's turn *turn* whose model request the loopback model holds until
    ``model_peer.release``."""
    model_peer.blocked, model_peer.release = threading.Event(), threading.Event()
    running = asyncio.create_task(acp.rpc("session/prompt", sessionId=sid, prompt=[{"type": "text", "text": text}],
                                          _meta={"lazurio.turnId": turn}))
    reached = asyncio.create_task(asyncio.to_thread(model_peer.blocked.wait, 60))
    await asyncio.wait({running, reached}, return_when=asyncio.FIRST_COMPLETED)
    assert not running.done(), running.result()  # it ended without reaching the model
    assert await reached, "the turn never reached the model"
    return running


@pytest.mark.platforms("linux")
@pytest.mark.asyncio
async def test_a_steer_while_buddy_works_reaches_the_model_and_his_turn_is_never_cancelled(
        daemon, tmp_path, model_peer):
    work, guidance = "WS_SHARED BLOCK_FIFO draft the release notes", "WS_SHARED STEERED only the ACP changes"
    async with editor(daemon, tmp_path) as acp:
        hello = await acp.rpc("initialize", protocolVersion=1, clientCapabilities={})
        assert hello["result"]["agentCapabilities"]["_meta"] == {"lazurio.activeSteer": 1}, hello
        sid = (await acp.rpc("session/new", cwd=str(tmp_path), mcpServers=[]))["result"]["sessionId"]
        running = await _blocked_prompt(acp, sid, model_peer, work, "turn-1")

        # A steer naming another turn reaches nothing.
        other = await acp.rpc("_lazurio/steer", sessionId=sid, turnId="turn-0", messageId="m-0", text="WS_SHARED OTHER")
        assert other.get("result") == {"steered": False, "reason": "not_running"}, other
        steered = await acp.rpc("_lazurio/steer", sessionId=sid, turnId="turn-1", messageId="m-1", text=guidance)
        assert steered.get("result") == {"steered": True}, steered
        assert not running.done(), "the steer ended the running prompt"

        model_peer.release.set()
        ended = await running
        assert ended["result"]["stopReason"] == "end_turn"
        # Within the same turn: the held request without the guidance, then one with it as the
        # latest user message, before the prompt's end_turn.
        first, steered_request = model_peer.requests[0], model_peer.requests[1]
        assert "STEERED" not in _messages(first)
        assert [m["role"] for m in steered_request["messages"]][-3:] == ["user", "assistant", "user"]
        assert "STEERED" in json.dumps(steered_request["messages"][-1])
        answers = [frame for frame in acp.frames[:acp.frames.index(ended)]
                   if (frame.get("params") or {}).get("update", {}).get("sessionUpdate") == "agent_message_chunk"]
        assert len(answers) >= 2, answers  # both answers streamed in this prompt

        # Nothing of this editor runs any more: the steer starts nothing.
        before = len(model_peer.requests)
        later = await acp.rpc("_lazurio/steer", sessionId=sid, turnId="turn-1", messageId="m-2", text="WS_SHARED TOO_LATE")
        assert later.get("result") == {"steered": False, "reason": "not_running"}, later
        await asyncio.sleep(1)
        assert not any("TOO_LATE" in _messages(request) for request in model_peer.requests[before:])
        assert not any("OTHER" in _messages(request) for request in model_peer.requests)
    # No frame cancelled the turn.
    assert not any(frame.get("method") == "session/cancel" for frame in acp.frames)
