"""Lazurio overlay: clarify for an ACP editor that answers form elicitation, on the real wire.

An ordinary isolated gateway, the real ``hermes acp`` stdio process and a loopback model that calls
``clarify`` whenever it is offered: an editor that advertised ``elicitation.form`` gets the tool,
answers the question in the ``elicitation/create`` form and the answer reaches the model; an editor
without forms keeps upstream's tools. Overlay row: ``acp: clarify for an ACP editor that answers
form elicitation`` (docs/operations/lazurio-fork-release.md).
"""
import asyncio
import json
import time
from http.server import BaseHTTPRequestHandler

import pytest

from tests.acp_adapter.test_gateway_sessions import editor


class ClarifyWhenOffered(BaseHTTPRequestHandler):
    """Calls clarify on a turn's first request when the request offers it, else answers in text."""

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"data": []}')

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if "messages" not in body:
            self.server.metadata_requests.append(body)
        else:
            self.server.requests.append(body)
        messages = body.get("messages", [])
        offered = {tool["function"]["name"] for tool in body.get("tools") or []}
        last = next((i for i, m in reversed(list(enumerate(messages))) if m.get("role") == "user"), -1)
        asking = "clarify" in offered and not any(m.get("role") == "tool" for m in messages[last + 1:])
        message = {"role": "assistant", "content": "PLAIN_REPLY"}
        if asking:
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "index": 0, "id": "lazurio-clarify", "type": "function", "function": {
                    "name": "clarify", "arguments": json.dumps({"questions": [
                        {"question": "Pick a color", "choices": ["blue", "green"]}]})}}]}
        finish = "tool_calls" if asking else "stop"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream" if body.get("stream") else "application/json")
        self.end_headers()
        if body.get("stream"):
            for delta, reason in [(message, None), ({}, finish)]:
                frame = {"id": "local", "object": "chat.completion.chunk", "created": 1, "model": "local",
                         "choices": [{"index": 0, "delta": delta, "finish_reason": reason}]}
                self.wfile.write(("data: " + json.dumps(frame) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
        else:
            self.wfile.write(json.dumps({"id": "local", "choices": [
                {"index": 0, "message": message, "finish_reason": finish}]}).encode())
        self.wfile.flush()


def _requests_for(model_peer, marker):
    return [r for r in model_peer.requests if marker in json.dumps(r.get("messages", []))]


@pytest.mark.platforms("linux")
@pytest.mark.asyncio
async def test_a_form_editor_gets_clarify_and_answers_it_while_others_keep_upstreams_tools(
        daemon, tmp_path, model_peer):
    model_peer.RequestHandlerClass = ClarifyWhenOffered

    async with editor(daemon, tmp_path) as acp:
        assert "result" in await acp.rpc("initialize", protocolVersion=1,
                                          clientCapabilities={"elicitation": {"form": {}}})
        sid = (await acp.rpc("session/new", cwd=str(tmp_path), mcpServers=[]))["result"]["sessionId"]
        turn = asyncio.create_task(acp.rpc("session/prompt", sessionId=sid,
                                           prompt=[{"type": "text", "text": "FORM_EDITOR ask my color"}]))
        deadline = time.monotonic() + 30
        while not any(f.get("method") == "elicitation/create" for f in acp.frames):
            assert not turn.done(), turn.result()
            assert time.monotonic() < deadline, "no elicitation/create reached the form editor"
            await asyncio.sleep(.05)
        request = next(f for f in acp.frames if f.get("method") == "elicitation/create")
        assert request["params"]["sessionId"] == sid
        choices = request["params"]["requestedSchema"]["properties"]["answer"]["enum"]
        assert choices[0].startswith("blue") and choices[1] == "green", choices  # upstream marks the first as recommended
        acp.process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": {
            "action": "accept", "content": {"answer": "green"}}}).encode() + b"\n")
        await acp.process.stdin.drain()
        reply = await asyncio.wait_for(turn, 40)
        assert reply["result"]["stopReason"] == "end_turn", reply
    form_requests = _requests_for(model_peer, "FORM_EDITOR")
    assert "clarify" in {t["function"]["name"] for t in form_requests[0].get("tools") or []}
    answers = [m for r in form_requests for m in r["messages"] if m.get("role") == "tool"]
    assert any("green" in json.dumps(m) for m in answers), answers

    async with editor(daemon, tmp_path) as acp:
        assert "result" in await acp.rpc("initialize", protocolVersion=1, clientCapabilities={})
        sid = (await acp.rpc("session/new", cwd=str(tmp_path), mcpServers=[]))["result"]["sessionId"]
        reply = await acp.rpc("session/prompt", sessionId=sid,
                              prompt=[{"type": "text", "text": "PLAIN_EDITOR ask my color"}])
        assert reply["result"]["stopReason"] == "end_turn", reply
        assert not any(f.get("method") in {"elicitation/create", "session/request_permission"} for f in acp.frames)
    plain_requests = _requests_for(model_peer, "PLAIN_EDITOR")
    assert "clarify" not in {t["function"]["name"] for t in plain_requests[0].get("tools") or []}
