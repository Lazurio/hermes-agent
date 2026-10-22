"""Lazurio overlay: sending while Buddy works, on the real wire.

An ordinary isolated gateway, the real ``hermes acp`` stdio process and a loopback model. Buddy's chat
steers by default: the editor's Stop (``session/cancel``) ends the running prompt, and its next prompt,
sent right after the Stop as the chat sends it, reaches the model as main's merge, the interrupted
request once and the correction once. The second
test pins what upstream's viewer already does for a prompt sent while a turn runs without a Stop
(queued behind it): its image content block reaches the model. Overlay row: ``acp: after the editor's
Stop, its next prompt carries the interrupted request`` (docs/operations/lazurio-fork-release.md).
"""
import asyncio
import base64
import json
import threading

import pytest

from tests.acp_adapter.test_gateway_sessions import _ONE_PX_PNG, editor, viewer

GUIDANCE = "\n\nUser correction/guidance after interrupt: "


def _last_user_text(request):
    content = next(m["content"] for m in reversed(request["messages"]) if m["role"] == "user")
    if isinstance(content, str):
        return content
    return "".join(part.get("text", "") for part in content if part.get("type") == "text")


async def _session(acp, tmp_path):
    assert "result" in await acp.rpc("initialize", protocolVersion=1, clientCapabilities={})
    return (await acp.rpc("session/new", cwd=str(tmp_path), mcpServers=[]))["result"]["sessionId"]


async def _blocked_prompt(acp, sid, model_peer, text):
    """A prompt whose model request the loopback model holds until ``model_peer.release``."""
    model_peer.blocked, model_peer.release = threading.Event(), threading.Event()
    running = asyncio.create_task(acp.rpc("session/prompt", sessionId=sid, prompt=[{"type": "text", "text": text}]))
    reached = asyncio.create_task(asyncio.to_thread(model_peer.blocked.wait, 60))
    await asyncio.wait({running, reached}, return_when=asyncio.FIRST_COMPLETED)
    assert not running.done(), running.result()  # it ended without reaching the model
    assert await reached, "the turn never reached the model"
    return running


@pytest.mark.platforms("linux")
@pytest.mark.asyncio
async def test_the_prompt_after_the_editors_stop_reaches_the_model_with_the_interrupted_request(
        daemon, tmp_path, model_peer):
    interrupted, correction = "WS_SHARED BLOCK_FIFO draft the release notes", "WS_SHARED only cover the ACP changes"
    async with editor(daemon, tmp_path) as acp:
        sid = await _session(acp, tmp_path)
        running = await _blocked_prompt(acp, sid, model_peer, interrupted)
        acp.process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "session/cancel",
                                            "params": {"sessionId": sid}}).encode() + b"\n")
        await acp.process.stdin.drain()
        # The correction goes right after the Stop, before the stopped prompt has answered.
        corrected = asyncio.create_task(acp.rpc("session/prompt", sessionId=sid,
                                                prompt=[{"type": "text", "text": correction}]))
        stopped = await running
        assert stopped["result"]["stopReason"] == "cancelled", stopped
        model_peer.release.set()
        reply = await corrected
        assert reply["result"]["stopReason"] == "end_turn", reply
    request = next(r for r in model_peer.requests if correction in json.dumps(r["messages"]))
    assert _last_user_text(request) == interrupted + GUIDANCE + correction, _last_user_text(request)
    # The correction is in the request once; the interrupted request is the merge's subject (and stays
    # in the history as the interrupted turn's own message).
    assert json.dumps(request["messages"]).count(correction) == 1


@pytest.mark.platforms("linux")
@pytest.mark.asyncio
# A loopback custom provider has no catalog entry; declaring vision is the documented knob (as in
# upstream's test_acp_image_prompt_reaches_model_as_image_part).
@pytest.mark.parametrize("daemon", [{"model": {"supports_vision": True}}], indirect=True)
async def test_a_prompt_queued_behind_a_running_turn_keeps_its_image(daemon, tmp_path, model_peer):
    async with editor(daemon, tmp_path) as acp:
        sid = await _session(acp, tmp_path)
        running = await _blocked_prompt(acp, sid, model_peer, "WS_SHARED BLOCK_FIFO first")
        queued = asyncio.create_task(acp.rpc("session/prompt", sessionId=sid, prompt=[
            {"type": "text", "text": "WS_SHARED what is in this image?"},
            {"type": "image", "mimeType": "image/png", "data": base64.b64encode(_ONE_PX_PNG).decode()}]))
        async with viewer(daemon) as ws:  # the owner holds it queued behind the running turn
            for _ in range(300):
                pending = (await ws.rpc("session.resume", session_id=sid)).get("pending", [])
                if any(row.get("status") == "queued" for row in pending):
                    break
                await asyncio.sleep(0.1)
            else:
                raise AssertionError(f"the image prompt never queued: {pending}")
        model_peer.release.set()
        assert (await running)["result"]["stopReason"] == "end_turn"
        assert (await queued)["result"]["stopReason"] == "end_turn"
    request = next(r for r in model_peer.requests if "what is in this image?" in _last_user_text(r))
    parts = next(m["content"] for m in reversed(request["messages"]) if m["role"] == "user")
    (image,) = [part for part in parts if part.get("type") == "image_url"]
    assert base64.b64decode(image["image_url"]["url"].split(",", 1)[1]) == _ONE_PX_PNG
