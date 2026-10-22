"""Lazurio overlay: the owner gives clarify to an ACP session whose editor answers form elicitation.

Upstream's ACP sessions get the ``hermes-acp`` toolset, the coding posture without ``clarify``.
An ACP editor that advertised ``elicitation.form`` creates its session with
``editor.lazurio_form_elicitation``, and the owner adds the ``clarify`` toolset, unless the profile
disables it; every other session keeps upstream's tools. Overlay row: ``acp: clarify for an ACP
editor that answers form elicitation`` (docs/operations/lazurio-fork-release.md).
"""
import pytest

from gateway.lazurio_editor_clarify import CAPABILITY, EDITOR_KEY
from hermes_state_runtime import RuntimeStoreError

CONFIG = {"platform_toolsets": {"acp": ["hermes-acp"], "cli": ["hermes-cli"]}}


def create(params, config=CONFIG):
    from gateway.session_policy import build_policy
    return build_policy(params, config, workspace_posture=True)


def acp(tmp_path, **editor):
    return {"source": "acp", "cwd": str(tmp_path),
            "editor": {"mcp_servers": [], "edit_approval_policy": "ask", **editor}}


def test_an_editor_that_answers_forms_gets_clarify(tmp_path):
    assert "clarify" in create(acp(tmp_path, **{EDITOR_KEY: True})).toolsets


@pytest.mark.parametrize("editor", [{}, {EDITOR_KEY: False}])
def test_other_acp_sessions_keep_upstreams_tools(tmp_path, editor):
    assert "clarify" not in create(acp(tmp_path, **editor)).toolsets


def test_a_profile_that_disables_clarify_keeps_it_off(tmp_path):
    config = {**CONFIG, "agent": {"disabled_toolsets": ["clarify"]}}
    assert "clarify" not in create(acp(tmp_path, **{EDITOR_KEY: True}), config).toolsets


def test_the_key_must_be_a_boolean_and_only_from_an_acp_launch(tmp_path):
    with pytest.raises(RuntimeStoreError) as refused:
        create(acp(tmp_path, **{EDITOR_KEY: "yes"}))
    assert getattr(refused.value, "field", None) == f"editor.{EDITOR_KEY}"
    with pytest.raises(RuntimeStoreError):
        create({"source": "cli", "cwd": str(tmp_path), "editor": {EDITOR_KEY: True}})
    # Upstream's own refusal of an unknown key still holds.
    with pytest.raises(RuntimeStoreError):
        create(acp(tmp_path, theme="dark"))


def test_a_session_following_its_profile_keeps_clarify_turn_after_turn(tmp_path):
    policy = create({**acp(tmp_path, **{EDITOR_KEY: True}), "follow_profile_config": True})
    assert policy.follows_profile
    assert "clarify" in policy.current_toolsets(CONFIG)
    plain = create({**acp(tmp_path), "follow_profile_config": True})
    assert "clarify" not in plain.current_toolsets(CONFIG)


def test_resume_refreshes_the_profiles_settings_and_keeps_the_editors_clarify(tmp_path):
    """``session.resume`` re-derives a session's toolsets from the profile's current config (CFG-2):
    the editor's clarify survives it; a profile that disables clarify later still wins."""
    import json
    from gateway.session_policy_refresh import refreshed_policy
    policy = create(acp(tmp_path, **{EDITOR_KEY: True}))
    terminal = json.loads(policy.terminal_json)
    refreshed, _, _ = refreshed_policy(policy, {**CONFIG, "platform_toolsets": {"acp": ["hermes-acp"]}}, terminal)
    assert "clarify" in refreshed.toolsets
    disabled, _, _ = refreshed_policy(policy, {**CONFIG, "agent": {"disabled_toolsets": ["clarify"]}}, terminal)
    assert "clarify" not in disabled.toolsets


@pytest.mark.asyncio
async def test_the_owner_advertises_the_key_so_a_newer_viewer_never_fails_an_older_owner():
    from types import SimpleNamespace
    from gateway.session_controls import AuthorityConnection

    connection = AuthorityConnection.__new__(AuthorityConnection)
    connection.authority = SimpleNamespace(instance_id="i", profile_id="p", epoch=1)

    async def ping(ref, params):
        return {"pong": True}

    connection.ping = ping
    described = await connection.describe(None, {})
    assert CAPABILITY in described["capabilities"]
    assert "acp-editor-policy-v1" in described["capabilities"]
