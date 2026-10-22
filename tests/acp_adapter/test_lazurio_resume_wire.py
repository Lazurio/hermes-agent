"""Lazurio overlay: ``session/resume`` without the history, and an unknown id refused, on the real wire.

An ordinary isolated gateway and the real ``hermes acp`` stdio process. ``session/resume`` attaches a
known session without streaming its conversation back (ACP), ``session/load`` streams it. The test
also pins an upstream behaviour Buddy's chat relies on: resuming an id the owner does not know is an
error and creates no session, so Lazurio T3 Code never keeps talking to an unaddressable one.
Overlay row: ``acp: session/resume attaches without replaying history``
(docs/operations/lazurio-fork-release.md).
"""
import pytest

from tests.acp_adapter.test_gateway_sessions import editor, viewer

REPLAY = {"user_message_chunk", "agent_message_chunk", "agent_thought_chunk"}


def _updates_after(acp, start, sid):
    return [frame["params"]["update"]["sessionUpdate"] for frame in acp.frames[start:]
            if frame.get("method") == "session/update" and frame["params"]["sessionId"] == sid]


@pytest.mark.platforms("linux")
@pytest.mark.asyncio
async def test_resume_attaches_without_the_history_load_replays_and_an_unknown_id_is_refused(
        daemon, tmp_path, model_peer):
    async with editor(daemon, tmp_path) as acp:
        assert "result" in await acp.rpc("initialize", protocolVersion=1, clientCapabilities={})
        sid = (await acp.rpc("session/new", cwd=str(tmp_path), mcpServers=[]))["result"]["sessionId"]
        reply = await acp.rpc("session/prompt", sessionId=sid, prompt=[{"type": "text", "text": "WS_SHARED remember"}])
        assert reply["result"]["stopReason"] == "end_turn", reply

    async with editor(daemon, tmp_path) as acp:
        assert "result" in await acp.rpc("initialize", protocolVersion=1, clientCapabilities={})
        start = len(acp.frames)
        resumed = await acp.rpc("session/resume", cwd=str(tmp_path), sessionId=sid, mcpServers=[])
        assert "result" in resumed, resumed
        assert not REPLAY & set(_updates_after(acp, start, sid)), _updates_after(acp, start, sid)
        start = len(acp.frames)
        loaded = await acp.rpc("session/load", cwd=str(tmp_path), sessionId=sid, mcpServers=[])
        assert "result" in loaded, loaded
        assert {"user_message_chunk", "agent_message_chunk"} <= set(_updates_after(acp, start, sid))

        async with viewer(daemon) as ws:
            before = {row["session_id"] for row in (await ws.rpc("session.list"))["sessions"]}
        unknown = await acp.rpc("session/resume", cwd=str(tmp_path), sessionId="no-such-session", mcpServers=[])
        assert "error" in unknown and "result" not in unknown, unknown
        async with viewer(daemon) as ws:
            after = {row["session_id"] for row in (await ws.rpc("session.list"))["sessions"]}
        assert after == before
