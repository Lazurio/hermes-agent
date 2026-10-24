"""Lazurio overlay: a session whose editor passes MCP servers reaches the configured ones too.

The gateway's discovery connects a ``config.yaml`` server outside any session, as it does at start;
an ACP session's editor passes a server of its own, and the session's turn runs in its real
execution scope (``policy_scope``). Both servers' tools are offered there, after a cold
availability check too, in the model's tool list and in the Tool Search catalog, and both answer:
the configured one over the profile's connection, with the profile's trust level and availability
verdict, and without a second connection; the editor's under the session's own key, offered only in
its own session and released with it. Overlay row: ``acp: a session whose editor passes MCP servers
reaches the configured ones too`` (docs/operations/lazurio-fork-release.md); Lazurio/hermes-agent#12.
"""
import json
import sys
from types import SimpleNamespace

import pytest

from gateway.session_policy import bind_launch_key, build_policy, policy_scope
from tests.acp_adapter.test_session_mcp_policy import owner

CONFIGURED = "config_srv"
CONFIGURED_TOOL = f"mcp__{CONFIGURED}__echo"


@pytest.fixture
def configured(server_spec):
    """Connect a configured server as the gateway's discovery does (no session scope); yield its config."""
    from tools.mcp_tool_discovery import register_mcp_servers
    from tools.mcp_tool_lifecycle import shutdown_mcp_servers

    def connect(**extra):
        register_mcp_servers({CONFIGURED: {"command": sys.executable, "args": server_spec["args"],
                                           "env": {"BORROWED_CREDENTIAL": "FROM_CONFIG"}, **extra}})
    yield connect
    shutdown_mcp_servers(names={CONFIGURED})


def session(authority, sid, tmp_path, editor_servers):
    private = {}
    result = build_policy({"source": "acp", "cwd": str(tmp_path), "model": "local", "toolsets": [f"mcp-{CONFIGURED}"],
                           "editor": {"mcp_servers": editor_servers}}, {}, private_secrets=private)
    return bind_launch_key(authority, sid, result, None, config_secrets=private)


def mcp_tools():
    from tools.registry import registry
    return sorted(name for name in registry.get_all_tool_names() if name.startswith("mcp__"))


def connection_keys(name):
    from tools.mcp_tool import _servers
    return [key for key in _servers if (key[-1] if isinstance(key, tuple) else key) == name]


def cold():
    """Drop every availability verdict and the tool-definitions memo, as ``/reload-mcp`` does; an expired
    verdict (30 s after the last probe) is as cold: the next tool list probes each server afresh."""
    from tools.mcp_tool_agent import reprobe_tool_availability
    reprobe_tool_availability()


def offered(names):
    """Which of *names* the registry offers the model: every tool's availability check runs
    (``registry.get_definitions``, the step that builds the model's tool list)."""
    from tools.registry import registry
    return {definition["function"]["name"] for definition in registry.get_definitions(set(names), quiet=True)}


def test_a_session_whose_editor_passes_a_server_calls_the_configured_server_too(configured, server_spec, tmp_path):
    from tools.registry import registry
    configured()
    before = connection_keys(CONFIGURED)
    authority = owner()
    with policy_scope(session(authority, "with-editor", tmp_path, [server_spec]), authority=authority):
        (editor_tool,) = [name for name in mcp_tools() if name.startswith("mcp__editor_")]
        assert f"mcp__{CONFIGURED}__echo" in mcp_tools()
        assert "FROM_CONFIG" in registry.dispatch(f"mcp__{CONFIGURED}__echo", {})
        assert "PRIVATE_A" in registry.dispatch(editor_tool, {})
        # The configured server answers over the profile's one connection: none was opened for the session.
        assert connection_keys(CONFIGURED) == before
    # The editor's connection is the session's and goes with it; the profile's stays.
    assert not [key for key in connection_keys(editor_tool.split("__")[1])]
    assert connection_keys(CONFIGURED) == before


def test_a_session_without_editor_servers_calls_the_configured_server_as_before(configured, tmp_path):
    from tools.registry import registry
    configured()
    authority = owner()
    with policy_scope(session(authority, "plain", tmp_path, []), authority=authority):
        assert "FROM_CONFIG" in registry.dispatch(f"mcp__{CONFIGURED}__echo", {})


def test_the_configured_servers_trust_level_holds_in_a_session_whose_editor_passes_a_server(
        configured, server_spec, tmp_path):
    """A write-capable tool of a ``trust: untrusted`` server asks for approval in the editor's session as
    outside it, never running unasked under the session's own (unconfigured, fully trusted) key."""
    from tools.registry import registry
    configured(trust="untrusted")
    outside = registry.dispatch(f"mcp__{CONFIGURED}__echo", {})
    authority = owner()
    with policy_scope(session(authority, "untrusted", tmp_path, [server_spec]), authority=authority):
        inside = registry.dispatch(f"mcp__{CONFIGURED}__echo", {})
    assert "untrusted server" in outside and "FROM_CONFIG" not in outside
    assert inside == outside


def test_after_a_cold_check_a_session_whose_editor_passes_a_server_is_offered_the_configured_tools(
        configured, server_spec, tmp_path):
    """The model's tool list is built in the session's scope (the agent's ``get_tool_definitions``): a
    fresh availability check there finds the profile's connection, beside the editor's own."""
    from model_tools import get_tool_definitions
    configured()
    authority = owner()
    policy = session(authority, "cold", tmp_path, [server_spec])
    with policy_scope(policy, authority=authority):
        (editor_tool,) = [name for name in mcp_tools() if name.startswith("mcp__editor_")]
        both = {CONFIGURED_TOOL, editor_tool}
        cold()
        assert offered(both) == both
        cold()
        definitions = get_tool_definitions(enabled_toolsets=list(policy.toolsets), quiet_mode=True,
                                           skip_tool_search_assembly=True)
        assert both <= {definition["function"]["name"] for definition in definitions}


def test_after_a_cold_check_tool_search_finds_and_calls_the_configured_tool_in_the_session(
        configured, server_spec, tmp_path):
    """With Tool Search the configured server's tools are deferred behind the bridge, whose catalog is the
    session's checked tool list (``model_tools._dispatch_bridge_tool``): cold, it still has the tool."""
    import model_tools
    configured()
    authority = owner()
    policy = session(authority, "bridge", tmp_path, [server_spec])
    with policy_scope(policy, authority=authority):
        cold()
        found = json.loads(model_tools.handle_function_call(
            function_name="tool_search", function_args={"queries": ["owned echo"]},
            enabled_toolsets=list(policy.toolsets)))
        assert CONFIGURED_TOOL in found["tools"], found
        cold()
        called = model_tools.handle_function_call(
            function_name="tool_call", function_args={"name": CONFIGURED_TOOL, "arguments": {}},
            enabled_toolsets=list(policy.toolsets))
        assert "FROM_CONFIG" in called, called


def test_an_availability_verdict_checked_in_the_editors_session_never_answers_for_the_profile(
        configured, server_spec, tmp_path):
    """The registry caches a verdict per profile, not per session. The configured server's verdict, first
    probed inside the editor's session, is the profile's own, so a later turn of the profile outside any
    session (a Telegram chat) is offered the tool from that cached verdict."""
    configured()
    authority = owner()
    cold()
    with policy_scope(session(authority, "first", tmp_path, [server_spec]), authority=authority):
        offered({CONFIGURED_TOOL})  # probed and cached here, in the session's scope
    assert offered({CONFIGURED_TOOL}) == {CONFIGURED_TOOL}


def test_an_editors_tools_are_offered_only_in_their_own_session_by_their_own_check(
        configured, server_spec, tmp_path):
    """Editor isolation holds for the catalog: an editor's tool is checked in, and offered to, its own
    session alone; the profile and another editor session are never offered it."""
    from gateway.session_local_mcp import release_editor_mcp
    configured()
    authority = owner()
    # Both sessions are viewed, so their editor connections stay connected between executions.
    authority.sessions = {sid: SimpleNamespace(subscribers={"editor": object()}) for sid in ("one", "two")}
    one, two = (session(authority, sid, tmp_path, [server_spec]) for sid in ("one", "two"))
    try:
        with policy_scope(one, authority=authority):
            (tool_one,) = [name for name in mcp_tools() if name.startswith("mcp__editor_")]
            cold()
            assert offered({tool_one, CONFIGURED_TOOL}) == {tool_one, CONFIGURED_TOOL}
        cold()
        assert offered({tool_one}) == set()
        with policy_scope(two, authority=authority):
            (tool_two,) = [name for name in mcp_tools() if name.startswith("mcp__editor_")]
            cold()
            assert offered({tool_one, tool_two}) == {tool_two}
    finally:
        for live in authority.sessions.values():
            live.subscribers.clear()
        release_editor_mcp(authority, list(authority.sessions))
