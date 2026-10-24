"""Lazurio overlay: a reply of intentional silence sends nothing to the editor, on the real wire.

An ordinary isolated gateway, the real ``hermes acp`` stdio process and a loopback model that streams
its reply in several deltas. Upstream's viewer passes the streamed ``NO_REPLY`` on and then the
owner's notice that the model returned only a silence marker (the owner cannot tell an editor's
prompt from a person's message); here the editor gets no ``agent_message_chunk`` for that reply, also
when it follows commentary and a tool call, and a reply that only starts like a marker arrives whole,
in order and in one message. Overlay row: ``acp: a reply of intentional silence (NO_REPLY,
[SILENT]) sends nothing to the client`` (docs/operations/lazurio-fork-release.md).
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

import pytest

from tests.acp_adapter.test_gateway_sessions import editor

# The deltas the loopback model streams for a prompt that names the key. A ``LOOKUP_`` key is first
# answered with commentary and a ``read_file`` call (Buddy checking his list), and with its reply
# after the tool's result.
REPLIES = {"SILENT_PROBE": ["NO", "_REP", "LY"], "STARTS_LIKE_PROBE": ["No", " problem, NO_REPLY", " is only a marker."],
           "LOOKUP_THEN_SILENT": ["NO", "_REP", "LY"], "LOOKUP_THEN_REPLY": ["No", " problem", ", nothing changed."]}
LOOKUP = ["Podívám se", " do seznamu."]
LISTS = "delegace.txt"
LOOKUP_CALL = {"index": 0, "id": "call_lookup", "type": "function",
               "function": {"name": "read_file", "arguments": json.dumps({"path": LISTS})}}


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
        messages = request.get("messages") or [{}]
        last = json.dumps(next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), ""))
        key = next((key for key in REPLIES if key in last), None)
        lookup = bool(key and key.startswith("LOOKUP_") and messages[-1].get("role") != "tool")
        parts = LOOKUP if lookup else REPLIES.get(key, ["LOCAL_ACK"])
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
        if lookup:
            deltas.append({"tool_calls": [LOOKUP_CALL]})
        for delta, finish in [*((delta, None) for delta in deltas), ({}, "tool_calls" if lookup else "stop")]:
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


async def _session(acp, tmp_path):
    assert "result" in await acp.rpc("initialize", protocolVersion=1, clientCapabilities={})
    return (await acp.rpc("session/new", cwd=str(tmp_path), mcpServers=[]))["result"]["sessionId"]


async def _prompt(acp, sid, text):
    """``(stop reason, the turn's session updates)``."""
    start = len(acp.frames)
    reply = await acp.rpc("session/prompt", sessionId=sid, prompt=[{"type": "text", "text": text}])
    assert "result" in reply, reply
    return reply["result"]["stopReason"], [frame["params"]["update"] for frame in acp.frames[start:]
                                           if frame.get("method") == "session/update"]


def _chunks(updates):
    """``[(text, messageId)]`` of the ``agent_message_chunk`` updates."""
    return [(u["content"]["text"], u.get("messageId")) for u in updates if u["sessionUpdate"] == "agent_message_chunk"]


def _around_the_tool_call(updates):
    """The message chunks before the tool call and after its completion."""
    kinds = [update["sessionUpdate"] for update in updates]
    assert "tool_call" in kinds and "tool_call_update" in kinds, kinds
    return _chunks(updates[:kinds.index("tool_call")]), _chunks(updates[kinds.index("tool_call_update") + 1:])


@pytest.mark.platforms("linux")
@pytest.mark.asyncio
async def test_a_silent_reply_reaches_the_editor_as_nothing_and_one_that_starts_like_it_arrives_whole(
        daemon, tmp_path, model_peer):
    async with editor(daemon, tmp_path) as acp:
        sid = await _session(acp, tmp_path)
        stop_reason, updates = await _prompt(acp, sid, "SILENT_PROBE anything new?")
        assert stop_reason == "end_turn"
        assert _chunks(updates) == []
        stop_reason, updates = await _prompt(acp, sid, "STARTS_LIKE_PROBE and now?")
        assert stop_reason == "end_turn"
        assert "".join(text for text, _ in _chunks(updates)) == "No problem, NO_REPLY is only a marker."
        assert len({message_id for _, message_id in _chunks(updates)}) == 1


@pytest.mark.platforms("linux")
@pytest.mark.asyncio
async def test_after_commentary_and_a_tool_call_a_silent_reply_sends_nothing_and_a_reply_arrives_whole(
        daemon, tmp_path, model_peer):
    (tmp_path / LISTS).write_text("nic nového\n")
    async with editor(daemon, tmp_path) as acp:
        sid = await _session(acp, tmp_path)
        stop_reason, updates = await _prompt(acp, sid, "LOOKUP_THEN_SILENT anything new?")
        assert stop_reason == "end_turn"
        before, after = _around_the_tool_call(updates)
        assert "".join(text for text, _ in before) == "Podívám se do seznamu."
        assert after == []
        stop_reason, updates = await _prompt(acp, sid, "LOOKUP_THEN_REPLY and now?")
        assert stop_reason == "end_turn"
        before, after = _around_the_tool_call(updates)
        assert "".join(text for text, _ in before) == "Podívám se do seznamu."
        assert "".join(text for text, _ in after).strip() == "No problem, nothing changed."
        assert len({message_id for _, message_id in after}) == 1
        assert {message_id for _, message_id in before}.isdisjoint(message_id for _, message_id in after)
