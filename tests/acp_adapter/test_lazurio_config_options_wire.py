"""Lazurio overlay: the session's fast mode as the one ACP config option, on the real wire.

An ordinary isolated gateway and the real ``hermes acp`` stdio process. On Buddy's route (ChatGPT
sign-in, GPT-6.1 Sol) the editor sees one ``fast`` option beside ``models`` and ``modes``; its set
and another surface's both land on the owner's session-scoped fast control, never in config.yaml,
and the other surface's reaches the editor as ``config_option_update``. On a route without fast mode
the editor sees no option, and the ids of the model and the reasoning effort change nothing. Overlay
row: ``acp: fast mode session config option on the gateway viewer``
(docs/operations/lazurio-fork-release.md).
"""
import json
import uuid

import pytest

from tests.acp_adapter.test_gateway_sessions import editor, viewer
from tests.waiting import async_wait_until

# Buddy's route. No turn runs on it, so it is never called.
BUDDY_ROUTE = {"model": {"provider": "openai-codex", "default": "gpt-6.1-sol",
                         "base_url": "https://chatgpt.com/backend-api/codex"}}


def _options(result):
    return {option["id"]: option for option in result.get("configOptions") or []}


def _config_updates(acp, sid, after=0):
    return [frame["params"]["update"] for frame in acp.frames[after:] if frame.get("method") == "session/update"
            and frame["params"]["sessionId"] == sid
            and frame["params"]["update"]["sessionUpdate"] == "config_option_update"]


def _answered(acp):
    """The position of the answer to the editor's last request among what the editor received."""
    return next(i for i, frame in enumerate(acp.frames) if frame.get("id") == acp.serial and "method" not in frame)


@pytest.mark.platforms("linux")
@pytest.mark.asyncio
@pytest.mark.parametrize("daemon", [BUDDY_ROUTE], indirect=True)
async def test_buddys_route_offers_fast_which_the_editor_and_another_surface_set(daemon, tmp_path):
    home = daemon[0]
    config = (home / "config.yaml").read_bytes()
    async with editor(daemon, tmp_path) as acp:
        assert "result" in await acp.rpc("initialize", protocolVersion=1, clientCapabilities={})
        created = (await acp.rpc("session/new", cwd=str(tmp_path), mcpServers=[]))["result"]
        sid = created["sessionId"]
        # models and modes stay as upstream has them; the one config option beside them is fast.
        assert "gpt-6.1-sol" in created["models"]["currentModelId"], created
        assert created["modes"]["currentModeId"] == "default", created
        options = _options(created)
        assert list(options) == ["fast"], created
        assert options["fast"]["currentValue"] == "normal"
        assert [choice["value"] for choice in options["fast"]["options"]] == ["normal", "fast"]

        picked = await acp.rpc("session/set_config_option", sessionId=sid, configId="fast", value="fast")
        assert _options(picked["result"])["fast"]["currentValue"] == "fast", picked
        assert "error" in await acp.rpc("session/set_config_option", sessionId=sid, configId="fast",
                                         value="ultrafast")
        async with viewer(daemon) as ws:
            # The owner's own session fast control holds the editor's pick.
            assert (await ws.rpc("config.get", key="fast", session_id=sid))["value"] == "fast"
            back = await acp.rpc("session/set_config_option", sessionId=sid, configId="fast", value="normal")
            assert _options(back["result"])["fast"]["currentValue"] == "normal", back
            assert (await ws.rpc("config.get", key="fast", session_id=sid))["value"] == "normal"

            # Another surface's /fast after the editor's fast: the owner publishes nothing for it, and
            # the next session.info (here another surface's edit policy change) reports the session's
            # own tier against the fast the editor shows, so the editor reads and is told.
            again = await acp.rpc("session/set_config_option", sessionId=sid, configId="fast", value="fast")
            assert _options(again["result"])["fast"]["currentValue"] == "fast", again
            answered = _answered(acp)
            assert await ws.rpc("config.set", key="fast", value="normal", session_id=sid) == {
                "key": "fast", "value": "normal"}
            snapshot = await ws.rpc("session.resume", session_id=sid)
            await ws.rpc("session.mutate", session_id=sid, operation="edit_policy",
                         payload={"policy": "workspace_session"}, request_id=uuid.uuid4().hex,
                         expected_revision=snapshot["revision"],
                         expected_generation=snapshot["execution_generation"])
        # A refresh still reading when a set lands may tell the editor first what the answer then
        # says; after the answer, the editor ends on the other surface's normal.
        await async_wait_until(
            lambda: [update["configOptions"][0]["currentValue"] for update in _config_updates(acp, sid, answered)]
            [-1:] == ["normal"], timeout=15, what="the config_option_update for another surface's /fast")
    # Session-scoped like upstream's /fast: config.yaml is untouched.
    assert (home / "config.yaml").read_bytes() == config


@pytest.mark.platforms("linux")
@pytest.mark.asyncio
async def test_a_route_without_fast_mode_offers_no_option_and_the_model_and_effort_ids_change_nothing(
        daemon, tmp_path, model_peer):
    async with editor(daemon, tmp_path) as acp:
        assert "result" in await acp.rpc("initialize", protocolVersion=1, clientCapabilities={})
        created = (await acp.rpc("session/new", cwd=str(tmp_path), mcpServers=[]))["result"]
        sid = created["sessionId"]
        assert "local-wire-stub" in created["models"]["currentModelId"], created
        assert _options(created) == {}, created

        # The ids of the model and the reasoning effort answer as any unknown id does.
        for config_id, value in (("model", "custom:another-model"), ("thought_level", "high")):
            answer = await acp.rpc("session/set_config_option", sessionId=sid, configId=config_id, value=value)
            assert answer["result"]["configOptions"] == [], answer
        # The owner refuses fast on this route.
        refused = await acp.rpc("session/set_config_option", sessionId=sid, configId="fast", value="fast")
        assert "fast_unavailable" in json.dumps(refused.get("error")), refused

        reply = await acp.rpc("session/prompt", sessionId=sid, prompt=[{"type": "text", "text": "WS_SHARED unchanged"}])
        assert reply["result"]["stopReason"] == "end_turn", reply
        assert _config_updates(acp, sid) == []
    request = model_peer.requests[-1]
    shown = json.dumps({k: v for k, v in request.items() if k != "messages"})
    assert request.get("model") == "local-wire-stub", shown
    assert request.get("reasoning_effort") != "high" and "service_tier" not in request, shown
