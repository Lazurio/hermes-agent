"""Lazurio overlay: ``session/resume`` attaches without replaying the history; ``session/load`` replays.

ACP: an agent answers ``session/load`` by streaming the conversation back and ``session/resume`` by
attaching only. Upstream's gateway viewer replayed on both. Overlay row: ``acp: session/resume
attaches without replaying history`` (docs/operations/lazurio-fork-release.md).
"""
from unittest.mock import AsyncMock

import pytest
from acp.schema import AgentMessageChunk, UserMessageChunk

from acp_adapter.gateway_server import GatewayACPAgent


@pytest.fixture(autouse=True)
def _picker(monkeypatch):
    from acp_adapter import gateway_session_meta
    monkeypatch.setattr(gateway_session_meta, "model_state", lambda info: None)
    monkeypatch.setattr(gateway_session_meta, "provenance_meta", lambda *args: None)


def _agent(cwd):
    info = {"model": "m", "cwd": str(cwd), "edit_approval_policy": "ask"}

    async def rpc(method, **params):
        if method == "session.info":
            return dict(info)
        if method == "session.resume":
            return {"revision": 1, "execution_generation": 1, "info": dict(info), "prompts": [], "pending": [],
                    "messages": [{"role": "user", "content": "earlier question", "row_id": 1},
                                 {"role": "assistant", "content": "earlier answer", "row_id": 2}]}
        return {}  # config.set (live reasoning), session.stats (post-attach notices)

    agent = GatewayACPAgent()
    agent._gateway, agent._conn = AsyncMock(), AsyncMock()
    agent._gateway.rpc.side_effect = rpc
    return agent


def _replayed(agent):
    return [call.kwargs["update"] for call in agent._conn.session_update.await_args_list
            if isinstance(call.kwargs["update"], (UserMessageChunk, AgentMessageChunk))]


@pytest.mark.asyncio
async def test_resume_attaches_without_the_history(tmp_path):
    agent = _agent(tmp_path)
    response = await agent.resume_session(cwd=str(tmp_path), session_id="s", mcp_servers=[])
    assert response.modes.current_mode_id == "default"
    assert "s" in agent._snapshots
    assert _replayed(agent) == []


@pytest.mark.asyncio
async def test_load_still_replays_the_history(tmp_path):
    agent = _agent(tmp_path)
    await agent.load_session(cwd=str(tmp_path), session_id="s", mcp_servers=[])
    assert [chunk.content.text for chunk in _replayed(agent)] == ["earlier question", "earlier answer"]
