"""Lazurio overlay: the ACP viewer asks for clarify when its editor answers form elicitation.

The viewer reads the editor's raw ``initialize`` capabilities (upstream's observer); an editor that
advertised ``elicitation.form`` creates its session with ``editor.lazurio_form_elicitation``, but
only on an owner that advertises the key, so a newer viewer never fails against an older owner.
Overlay row: ``acp: clarify for an ACP editor that answers form elicitation``
(docs/operations/lazurio-fork-release.md).
"""
from unittest.mock import AsyncMock

import pytest

from acp_adapter.gateway_server import GatewayACPAgent
from gateway.lazurio_editor_clarify import CAPABILITY, EDITOR_KEY


@pytest.fixture(autouse=True)
def _picker(monkeypatch):
    from acp_adapter import gateway_session_meta
    monkeypatch.setattr(gateway_session_meta, "model_state", lambda info: None)
    monkeypatch.setattr(gateway_session_meta, "provenance_meta", lambda *args: None)


async def _created_editor(tmp_path, *, form, capabilities):
    created = []

    async def rpc(method, **params):
        if method == "runtime.describe":
            return {"session_create": {"sources": ["acp", "cli"]},
                    "capabilities": ["acp-editor-policy-v1", "acp-session-mcp-v1", *capabilities]}
        if method == "session.create":
            created.append(params)
            return {"session_id": "s", "info": {"model": "m", "edit_approval_policy": "ask"}}
        assert method == "config.set", method  # live reasoning for the new session
        return {}

    agent = GatewayACPAgent()
    agent._gateway = AsyncMock()
    agent._gateway.rpc.side_effect = rpc
    agent._observe_client_message(type("Event", (), {"direction": "incoming", "message": {
        "method": "initialize", "params": {"clientCapabilities": {"elicitation": {"form": {}}} if form else {}}}})())
    await agent.new_session(cwd=str(tmp_path), mcp_servers=[])
    (params,) = created
    return params["editor"]


@pytest.mark.asyncio
async def test_a_form_editor_asks_the_owner_for_clarify(tmp_path):
    editor = await _created_editor(tmp_path, form=True, capabilities=[CAPABILITY])
    assert editor == {"mcp_servers": [], "edit_approval_policy": "ask", EDITOR_KEY: True}


@pytest.mark.asyncio
async def test_an_editor_without_forms_keeps_upstreams_request(tmp_path):
    editor = await _created_editor(tmp_path, form=False, capabilities=[CAPABILITY])
    assert editor == {"mcp_servers": [], "edit_approval_policy": "ask"}


@pytest.mark.asyncio
async def test_an_owner_without_the_overlay_gets_upstreams_request(tmp_path):
    editor = await _created_editor(tmp_path, form=True, capabilities=[])
    assert editor == {"mcp_servers": [], "edit_approval_policy": "ask"}
