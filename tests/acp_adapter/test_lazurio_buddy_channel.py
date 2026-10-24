"""Lazurio overlay: the ACP viewer hands its own LAZURIO_BUDDY_CHANNEL to the owner with every turn.

The viewer (``hermes acp``) reads the variable from its own process environment and sends it with
each ``prompt.submit``, only when it is a short channel token and only to an owner that advertises
``lazurio-acp-buddy-channel-v1``, so a newer viewer never fails against an older owner. Against a
fake owner over real websockets and the real ``GatewayClient``. Overlay row: ``acp: a turn's
commands see the LAZURIO_BUDDY_CHANNEL of the ACP viewer that submitted it``
(docs/operations/lazurio-fork-release.md).
"""
import asyncio
from contextlib import asynccontextmanager
import json
import logging
from unittest.mock import AsyncMock

import acp
import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

import acp_adapter.gateway_server as server
from acp_adapter.gateway_server import GatewayACPAgent
from gateway.lazurio_buddy_channel import CAPABILITY, ENV, SUBMIT_KEY, viewer_fields
from hermes_cli.gateway_client import GatewayClient


class Owner:
    """A gateway that answers the RPCs a viewer's attach and prompt use and records every submit."""

    def __init__(self, profile_id, capabilities):
        self.profile_id, self.capabilities, self.submits, self.server = profile_id, capabilities, [], None

    async def handler(self, ws):
        async for raw in ws:
            frame = json.loads(raw)
            method, params, result = frame["method"], frame["params"], {}
            if method == "runtime.describe":
                result = {"profile_id": self.profile_id, "capabilities": self.capabilities}
            elif method == "session.resume":
                result = {"session_id": params["session_id"], "messages": [], "pending": [], "prompts": [],
                          "replay_epoch": "e", "last_sequence": 0}
            elif method == "prompt.submit":
                self.submits.append(params)
                result = {"admission_id": f"a{len(self.submits)}"}
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": frame["id"], "result": result}))
            if method == "prompt.submit":
                await ws.send(json.dumps({"jsonrpc": "2.0", "method": "event", "params": {
                    "session_id": params["session_id"], "type": "message.complete", "replay_epoch": "e",
                    "seq": len(self.submits), "admission_id": result["admission_id"],
                    "payload": {"outcome": "completed", "text": "done"}}}))


async def _submits(monkeypatch, *, capabilities, environ_value, prompts=1):
    if environ_value is None:
        monkeypatch.delenv(ENV, raising=False)
    else:
        monkeypatch.setenv(ENV, environ_value)
    agent = GatewayACPAgent()
    agent._conn = AsyncMock()
    owner = Owner(str(agent._home), ["acp-editor-policy-v1", "acp-session-mcp-v1", *capabilities])
    owner.server = await serve(owner.handler, "127.0.0.1", 0)
    url = f"ws://127.0.0.1:{owner.server.sockets[0].getsockname()[1]}"

    @asynccontextmanager
    async def connect_gateway():
        async with connect(url) as ws:
            async with GatewayClient(ws) as client:
                yield client

    monkeypatch.setattr(server, "connect_gateway", connect_gateway)
    try:
        await agent.load_session(cwd="/", session_id="s")
        for n in range(prompts):
            response = await asyncio.wait_for(agent.prompt([acp.text_block(f"turn {n}")], "s"), 5)
            assert response.stop_reason == "end_turn"
    finally:
        await agent.aclose()
        owner.server.close()
    return owner.submits


@pytest.mark.asyncio
async def test_every_turn_of_a_viewer_started_with_the_channel_carries_it(monkeypatch):
    submits = await _submits(monkeypatch, capabilities=[CAPABILITY], environ_value="t3", prompts=2)
    assert [s.get(SUBMIT_KEY) for s in submits] == ["t3", "t3"]
    assert [s["text"] for s in submits] == ["turn 0", "turn 1"], "the prompt itself is unchanged"


@pytest.mark.asyncio
async def test_a_viewer_without_the_variable_sends_upstreams_submit(monkeypatch):
    (submit,) = await _submits(monkeypatch, capabilities=[CAPABILITY], environ_value=None)
    assert SUBMIT_KEY not in submit
    (submit,) = await _submits(monkeypatch, capabilities=[CAPABILITY], environ_value="")
    assert SUBMIT_KEY not in submit


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["T3", "t3 ", "buddy chat", "t3;id", "-t3", "x" * 33, "kanál"])
async def test_a_value_that_is_not_a_channel_token_is_not_forwarded(monkeypatch, caplog, value):
    with caplog.at_level(logging.WARNING, logger="gateway.lazurio_buddy_channel"):
        (submit,) = await _submits(monkeypatch, capabilities=[CAPABILITY], environ_value=value)
    assert SUBMIT_KEY not in submit
    assert any(ENV in record.getMessage() for record in caplog.records), "the refusal is logged"


@pytest.mark.asyncio
async def test_an_owner_from_before_this_overlay_gets_upstreams_submit(monkeypatch):
    (submit,) = await _submits(monkeypatch, capabilities=[], environ_value="t3")
    assert SUBMIT_KEY not in submit


def test_viewer_fields_reads_only_its_own_environment():
    described = {"capabilities": [CAPABILITY]}
    assert viewer_fields(described, {ENV: "t3"}) == {SUBMIT_KEY: "t3"}
    assert viewer_fields(described, {}) == {}
    assert viewer_fields(described, {"HERMES_SESSION_SOURCE": "acp"}) == {}, "a session's source never names the chat"
    assert viewer_fields({}, {ENV: "t3"}) == viewer_fields(None, {ENV: "t3"}) == {}
