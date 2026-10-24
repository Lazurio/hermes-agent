"""Lazurio overlay: a reply of intentional silence sends nothing to the editor, on the real wire.

An ordinary isolated gateway, the real ``hermes acp`` stdio process and a loopback model that streams
its reply in several deltas. Upstream's viewer passes the streamed ``NO_REPLY`` on and then the
owner's notice that the model returned only a silence marker (the owner cannot tell an editor's
prompt from a person's message); here the editor gets no ``agent_message_chunk`` for that turn, and
a reply that only starts like a marker arrives whole, in order and in one message. Overlay row:
``acp: a reply of intentional silence (NO_REPLY, [SILENT]) sends nothing to the client``
(docs/operations/lazurio-fork-release.md).
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

import pytest

from tests.acp_adapter.test_gateway_sessions import editor

# The deltas the loopback model streams for a prompt that names the key.
REPLIES = {"SILENT_PROBE": ["NO", "_REP", "LY"], "STARTS_LIKE_PROBE": ["No", " problem, NO_REPLY", " is only a marker."]}


class ScriptedModel(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"data": [{"id": "local-wire-stub", "object": "model"}]}).encode())

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        last = json.dumps(next((m.get("content", "") for m in reversed(request.get("messages", []))
                                if m.get("role") == "user"), ""))
        parts = next((reply for key, reply in REPLIES.items() if key in last), ["LOCAL_ACK"])
        base = {"id": "chatcmpl-local", "model": "local-wire-stub", "created": 1}
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream" if request.get("stream") else "application/json")
        self.end_headers()
        if not request.get("stream"):
            self.wfile.write(json.dumps({**base, "object": "chat.completion", "choices": [
                {"index": 0, "message": {"role": "assistant", "content": "".join(parts)}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}).encode())
            return
        deltas = [{"role": "assistant", "content": parts[0]}, *({"content": part} for part in parts[1:])]
        for delta, finish in [*((delta, None) for delta in deltas), ({}, "stop")]:
            chunk = {**base, "object": "chat.completion.chunk",
                     "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")


@pytest.fixture
def model_peer():
    """The ``daemon`` fixture's model: this module's scripted one instead of the shared fixed reply."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), ScriptedModel)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


async def _prompt(acp, sid, text):
    """``(stop reason, [(text, messageId)] of the turn's agent_message_chunk updates)``."""
    start = len(acp.frames)
    reply = await acp.rpc("session/prompt", sessionId=sid, prompt=[{"type": "text", "text": text}])
    assert "result" in reply, reply
    updates = [frame["params"]["update"] for frame in acp.frames[start:] if frame.get("method") == "session/update"]
    return reply["result"]["stopReason"], [(update["content"]["text"], update.get("messageId")) for update in updates
                                           if update["sessionUpdate"] == "agent_message_chunk"]


@pytest.mark.platforms("linux")
@pytest.mark.asyncio
async def test_a_silent_reply_reaches_the_editor_as_nothing_and_one_that_starts_like_it_arrives_whole(
        daemon, tmp_path, model_peer):
    async with editor(daemon, tmp_path) as acp:
        assert "result" in await acp.rpc("initialize", protocolVersion=1, clientCapabilities={})
        sid = (await acp.rpc("session/new", cwd=str(tmp_path), mcpServers=[]))["result"]["sessionId"]
        stop_reason, chunks = await _prompt(acp, sid, "SILENT_PROBE anything new?")
        assert stop_reason == "end_turn"
        assert chunks == []
        stop_reason, chunks = await _prompt(acp, sid, "STARTS_LIKE_PROBE and now?")
        assert stop_reason == "end_turn"
        assert "".join(text for text, _ in chunks) == "No problem, NO_REPLY is only a marker."
        assert len({message_id for _, message_id in chunks}) == 1
