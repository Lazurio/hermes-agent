"""Lazurio overlay: a session whose editor passes MCP servers reaches the configured ones too, on the wire.

An ordinary isolated gateway with an MCP server in its ``config.yaml``, the real ``hermes acp`` stdio
process whose ``session/new`` passes the editor's own MCP server, and a loopback model that calls one
tool per prompt. Upstream's gateway answered the configured server's tool with "MCP server
'config_echo' is not connected" in such a session; here it answers, and so does the editor's.
Overlay row: ``acp: a session whose editor passes MCP servers reaches the configured ones too``
(docs/operations/lazurio-fork-release.md); Lazurio/hermes-agent#12.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sys
import threading

import pytest

from tests.acp_adapter.test_gateway_sessions import editor

# A stdio MCP server whose one tool, ``echo``, answers with its BORROWED_CREDENTIAL (``server_spec``'s peer).
ECHO_SERVER = """import json, os, sys
for line in sys.stdin:
    request = json.loads(line)
    if request.get('id') is None:
        continue
    method = request.get('method')
    if method == 'initialize':
        result = {'protocolVersion': request['params']['protocolVersion'], 'capabilities': {'tools': {}},
                  'serverInfo': {'name': 'configured-peer', 'version': '1'}}
    elif method == 'tools/list':
        result = {'tools': [{'name': 'echo', 'description': 'configured echo',
                             'inputSchema': {'type': 'object', 'properties': {}}}]}
    elif method == 'tools/call':
        result = {'content': [{'type': 'text', 'text': os.environ['BORROWED_CREDENTIAL']}]}
    else:
        result = {}
    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)
"""
CONFIGURED = {"config_echo": {"command": sys.executable, "args": ["-c", ECHO_SERVER],
                              "env": {"BORROWED_CREDENTIAL": "FROM_CONFIG"}}}
# Tool search off, so the model is offered the MCP tools by name, as Buddy's model was.
DAEMON_CONFIG = {"mcp_servers": CONFIGURED, "tools": {"tool_search": {"enabled": "off"}}}
# The tool the loopback model calls for a prompt that names the key (a prefix of the tool's name).
CALLS = {"CALL_CONFIGURED": "mcp__config_echo__", "CALL_EDITOR": "mcp__editor_"}


class ToolCallingModel(BaseHTTPRequestHandler):
    """Calls the named tool once, then answers; records every tool result it was sent."""

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
        prefix = next((prefix for key, prefix in CALLS.items() if key in last), None)
        offered = [tool.get("function", {}).get("name", "") for tool in request.get("tools") or []]
        tool = next((name for name in offered if prefix and name.startswith(prefix)), None)
        if messages[-1].get("role") == "tool":
            self.server.tool_results.append((prefix, str(messages[-1].get("content"))))
            tool = None
        elif prefix and tool is None:
            self.server.tool_results.append((prefix, f"not offered: {offered}"))
        delta = ({"role": "assistant", "tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                                        "function": {"name": tool, "arguments": "{}"}}]}
                 if tool else {"role": "assistant", "content": "LOCAL_ACK"})
        base = {"id": "chatcmpl-local", "model": "local-wire-stub", "created": 1}
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream" if request.get("stream") else "application/json")
        self.end_headers()
        if not request.get("stream"):
            self.wfile.write(json.dumps({**base, "object": "chat.completion", "choices": [
                {"index": 0, "message": {"role": "assistant", "content": "LOCAL_ACK"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}).encode())
            return
        for chunk_delta, finish in [(delta, None), ({}, "tool_calls" if tool else "stop")]:
            chunk = {**base, "object": "chat.completion.chunk",
                     "choices": [{"index": 0, "delta": chunk_delta, "finish_reason": finish}]}
            self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")


@pytest.fixture
def model_peer():
    """The ``daemon`` fixture's model: this module's tool-calling one instead of the shared fixed reply."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), ToolCallingModel)
    server.tool_results = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


async def _prompt(acp, sid, text):
    reply = await acp.rpc("session/prompt", sessionId=sid, prompt=[{"type": "text", "text": text}])
    assert "result" in reply, reply
    return reply["result"]["stopReason"]


@pytest.mark.platforms("linux")
@pytest.mark.asyncio
@pytest.mark.parametrize("daemon", [DAEMON_CONFIG], indirect=True)
async def test_a_session_whose_editor_passes_a_server_calls_the_configured_server_too(
        daemon, tmp_path, model_peer, server_spec):
    async with editor(daemon, tmp_path) as acp:
        assert "result" in await acp.rpc("initialize", protocolVersion=1, clientCapabilities={})
        created = await acp.rpc("session/new", cwd=str(tmp_path), mcpServers=[server_spec])
        sid = created["result"]["sessionId"]
        assert await _prompt(acp, sid, "CALL_CONFIGURED anything remembered?") == "end_turn"
        assert await _prompt(acp, sid, "CALL_EDITOR and the editor?") == "end_turn"
    results = dict(model_peer.tool_results)
    assert "FROM_CONFIG" in results.get("mcp__config_echo__", ""), model_peer.tool_results
    assert "PRIVATE_A" in results.get("mcp__editor_", ""), model_peer.tool_results
