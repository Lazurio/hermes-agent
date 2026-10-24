"""Lazurio overlay: a turn's commands see the LAZURIO_BUDDY_CHANNEL of the ACP viewer that submitted it.

On the real wire: an ordinary isolated gateway (the ``daemon`` of tests/acp_adapter/conftest.py,
here started without and with ``LAZURIO_BUDDY_CHANNEL=t3`` in its own environment), two real
``hermes acp`` stdio processes attached to it, one started with ``LAZURIO_BUDDY_CHANNEL=t3`` (Buddy's
chat in Lazurio) and one without (another editor on the same Hermes home), a WebSocket client, and a
loopback model whose every turn runs one terminal command that prints the variable. Only the turns
the chat's viewer submits see ``t3``: the other editor's turns do not, also in the chat's own
session, and neither does a WebSocket client's turn. Overlay row: ``acp: a turn's commands see the
LAZURIO_BUDDY_CHANNEL of the ACP viewer that submitted it`` (docs/operations/lazurio-fork-release.md).
"""
import asyncio
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

import pytest

from tests.acp_adapter.test_gateway_sessions import editor, viewer

ENV = "LAZURIO_BUDDY_CHANNEL"
PROBE = f'printf "CHANNEL=[%s]" "${{{ENV}-absent}}"'


class PrintsTheChannel(BaseHTTPRequestHandler):
    """Runs ``PROBE`` through the terminal tool once in every turn, then answers in text."""

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"data": []}')

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        (self.server.requests if "messages" in body else self.server.metadata_requests).append(body)
        messages = body.get("messages", [])
        offered = {tool["function"]["name"] for tool in body.get("tools") or []}
        last = next((i for i, m in reversed(list(enumerate(messages))) if m.get("role") == "user"), -1)
        calling = "terminal" in offered and not any(m.get("role") == "tool" for m in messages[last + 1:])
        message = {"role": "assistant", "content": "PROBED"}
        if calling:
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "index": 0, "id": f"probe-{len(self.server.requests)}", "type": "function",
                "function": {"name": "terminal", "arguments": json.dumps({"command": PROBE})}}]}
        finish = "tool_calls" if calling else "stop"
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


@pytest.fixture(params=[{}, {ENV: "t3"}], ids=["gateway-without-it", "gateway-started-with-it"])
def channel_daemon(tmp_path, model_peer, request):
    """The ``daemon`` fixture's ordinary isolated gateway, with ``request.param`` added to the gateway's
    own environment only: ``(home, descriptor, env, root)``, where ``env`` (for the clients) lacks it."""
    from tests.gateway.test_normal_runtime_boot import control

    home = tmp_path / "state"
    home.mkdir(mode=0o700)
    user = tmp_path / "user"
    user.mkdir()
    root = Path(__file__).resolve().parents[2]
    model_url = f"http://127.0.0.1:{model_peer.server_port}/v1"
    (home / "config.yaml").write_text(json.dumps({
        "gateway": {"multiplex_profiles": False},
        "approvals": {"mode": "manual", "timeout": 60},
        "model": {"provider": "custom", "default": "local-wire-stub", "base_url": model_url},
        "auxiliary": {"title_generation": {"enabled": False}},
    }))
    env = {k: os.environ[k] for k in ("PATH", "LANG", "TZ") if k in os.environ}
    env.update(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(home),
               PYTHONPATH=str(root), PYTHONUNBUFFERED="1",
               OPENAI_API_KEY="loopback-only", OPENAI_BASE_URL=model_url,
               HERMES_ACP_SKIP_CONFIGURED_MCP="1")
    log_path = tmp_path / "gateway.log"
    with log_path.open("w") as log:
        process = subprocess.Popen([sys.executable, "-m", "gateway.run"], cwd=root, env={**env, **request.param},
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 40
            descriptor = {}
            while process.poll() is None and time.monotonic() < deadline:
                try:
                    descriptor = control(home, "identify")
                    if descriptor.get("state") == "ready":
                        break
                except (OSError, ValueError):
                    pass
                time.sleep(.1)
            assert descriptor.get("state") == "ready", log_path.read_text()
            yield home, descriptor, env, root
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def _seen(model_peer):
    """Marker of each turn -> what its terminal command printed."""
    seen = {}
    for request in model_peer.requests:
        messages = request["messages"]
        last = next(i for i, m in reversed(list(enumerate(messages))) if m.get("role") == "user")
        marker = re.search(r"TURN_[A-Z_]+", json.dumps(messages[last]))
        for message in messages[last + 1:]:
            printed = re.search(r"CHANNEL=\[([^\]]*)\]", json.dumps(message)) if message.get("role") == "tool" else None
            if marker and printed:
                seen[marker.group(0)] = printed.group(1)
    return seen


async def _prompt(acp, sid, marker):
    reply = await acp.rpc("session/prompt", sessionId=sid, prompt=[{"type": "text", "text": f"{marker} probe"}])
    assert reply.get("result", {}).get("stopReason") == "end_turn", reply
    assert not any(f.get("method") == "session/request_permission" for f in acp.frames)


@pytest.mark.platforms("linux")
@pytest.mark.asyncio
async def test_only_the_turns_of_the_viewer_started_with_the_channel_see_it(channel_daemon, tmp_path, model_peer):
    model_peer.RequestHandlerClass = PrintsTheChannel
    home, descriptor, env, root = channel_daemon
    assert ENV not in env
    chat_dir, other_dir = tmp_path / "chat", tmp_path / "other"
    chat_dir.mkdir()
    other_dir.mkdir()

    async with editor((home, descriptor, {**env, ENV: "t3"}, root), chat_dir) as chat:
        assert "result" in await chat.rpc("initialize", protocolVersion=1, clientCapabilities={})
        sid = (await chat.rpc("session/new", cwd=str(tmp_path), mcpServers=[]))["result"]["sessionId"]
        await _prompt(chat, sid, "TURN_CHAT_FIRST")
        async with editor((home, descriptor, env, root), other_dir) as other:
            assert "result" in await other.rpc("initialize", protocolVersion=1, clientCapabilities={})
            loaded = await other.rpc("session/load", cwd=str(tmp_path), sessionId=sid, mcpServers=[])
            assert "result" in loaded, loaded
            await _prompt(other, sid, "TURN_OTHER_IN_THE_CHATS_SESSION")
            own = (await other.rpc("session/new", cwd=str(tmp_path), mcpServers=[]))["result"]["sessionId"]
            await _prompt(other, own, "TURN_OTHER_OWN_SESSION")
        await _prompt(chat, sid, "TURN_CHAT_AGAIN")

    async with viewer(channel_daemon) as ws:
        created = await ws.rpc("session.create", request_id="ws-probe", source="cli", cwd=str(tmp_path))
        accepted = await ws.rpc("prompt.submit", session_id=created["session_id"], input_id="ws-probe",
                                text="TURN_WEBSOCKET probe")
        deadline = time.monotonic() + 30
        while (receipt := await ws.rpc("prompt.receipt", session_id=created["session_id"],
                                       admission_id=accepted["admission_id"]))["status"] != "terminal":
            assert time.monotonic() < deadline, receipt
            await asyncio.sleep(.05)
        assert receipt["outcome"] == "completed", receipt

    assert _seen(model_peer) == {
        "TURN_CHAT_FIRST": "t3", "TURN_OTHER_IN_THE_CHATS_SESSION": "absent", "TURN_OTHER_OWN_SESSION": "absent",
        "TURN_CHAT_AGAIN": "t3", "TURN_WEBSOCKET": "absent"}
