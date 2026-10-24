"""Lazurio overlay: a session whose editor passes MCP servers reaches the configured ones too.

The gateway's discovery connects a ``config.yaml`` server outside any session, as it does at start;
an ACP session's editor passes a server of its own, and the session's turn runs in its real
execution scope (``policy_scope``). Both servers' tools are listed there and both answer: the
configured one over the profile's connection, with the profile's trust level, and without a second
connection; the editor's under the session's own key, released with the session. Overlay row:
``acp: a session whose editor passes MCP servers reaches the configured ones too``
(docs/operations/lazurio-fork-release.md); Lazurio/hermes-agent#12.
"""
import sys

import pytest

from gateway.session_policy import bind_launch_key, build_policy, policy_scope
from tests.acp_adapter.test_session_mcp_policy import owner

CONFIGURED = "config_srv"


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
