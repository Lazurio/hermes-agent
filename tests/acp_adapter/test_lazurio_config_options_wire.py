"""Lazurio overlay: model and reasoning effort as ACP config options, on the real wire.

An ordinary isolated gateway, the real ``hermes acp`` stdio process and a loopback model: the editor
sees ``model`` and ``thought_level`` config options beside ``models`` and ``modes``; its pick and
another surface's pick both land on the owner, reach the editor as ``config_option_update`` and set
the reasoning effort of the next model request. Overlay row: ``acp: model and thought_level session
config options on the gateway viewer`` (docs/operations/lazurio-fork-release.md).
"""
import json
import uuid

import pytest

from tests.acp_adapter.test_gateway_sessions import editor, viewer
from tests.waiting import async_wait_until


def _options(result):
    return {option["id"]: option for option in result["configOptions"]}


def _config_updates(acp, sid):
    return [frame["params"]["update"] for frame in acp.frames if frame.get("method") == "session/update"
            and frame["params"]["sessionId"] == sid
            and frame["params"]["update"]["sessionUpdate"] == "config_option_update"]


@pytest.mark.platforms("linux")
@pytest.mark.asyncio
async def test_editor_and_another_surface_set_the_reasoning_effort_through_config_options(daemon, tmp_path, model_peer):
    async with editor(daemon, tmp_path) as acp:
        assert "result" in await acp.rpc("initialize", protocolVersion=1, clientCapabilities={})
        created = (await acp.rpc("session/new", cwd=str(tmp_path), mcpServers=[]))["result"]
        sid = created["sessionId"]
        # models and modes stay; the config options are beside them.
        assert "local-wire-stub" in created["models"]["currentModelId"], created
        assert created["modes"]["currentModeId"] == "default", created
        options = _options(created)
        assert options["model"]["category"] == "model"
        assert options["model"]["currentValue"] == created["models"]["currentModelId"]
        assert options["model"]["currentValue"] in [o["value"] for o in options["model"]["options"]]
        assert options["thought_level"]["category"] == "thought_level"
        assert options["thought_level"]["currentValue"] == "medium"  # the fixture profile sets no level

        picked = await acp.rpc("session/set_config_option", sessionId=sid, configId="thought_level", value="high")
        assert _options(picked["result"])["thought_level"]["currentValue"] == "high", picked
        assert "error" in await acp.rpc("session/set_config_option", sessionId=sid,
                                         configId="thought_level", value="turbo")

        # Another surface (a WS viewer of the same owner) picks a level: the editor is told.
        async with viewer(daemon) as ws:
            snapshot = await ws.rpc("session.resume", session_id=sid)
            await ws.rpc("session.mutate", session_id=sid, operation="reasoning", payload={"effort": "low"},
                         request_id=uuid.uuid4().hex, expected_revision=snapshot["revision"],
                         expected_generation=snapshot["execution_generation"])
        await async_wait_until(
            lambda: any(_options(update)["thought_level"]["currentValue"] == "low" for update in _config_updates(acp, sid)),
            timeout=15, what="the config_option_update for another surface's pick")

        reply = await acp.rpc("session/prompt", sessionId=sid, prompt=[{"type": "text", "text": "WS_SHARED effort"}])
        assert reply["result"]["stopReason"] == "end_turn", reply
    request = model_peer.requests[-1]
    assert request.get("reasoning_effort") == "low", json.dumps({k: v for k, v in request.items() if k != "messages"})
