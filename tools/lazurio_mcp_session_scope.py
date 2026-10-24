"""Lazurio overlay: a session with its editor's MCP servers still reaches the configured ones.

Buddy's chat in Lazurio T3 Code is an ACP session whose editor passes an MCP server of its own. The
gateway owns such servers as session state: each turn of the session runs inside its own tool scope
(``gateway/session_local_mcp.py`` ``_borrowed``), so the editor's connections are keyed
``(session scope, name)`` and no other session or profile sees them. The registry documents that
overlay as additive to the profile registry, and the profile's tools stay listed in it: the session's
toolsets keep every enabled ``mcp_servers`` entry, so the model is offered ``mcp__gbrain__search``.
The MCP call path is not additive, though. It resolves the server's connection, and keys the
server's trust level, spend limit, circuit breaker and lazy connect, by the current registry scope,
which inside the overlay is the session's. The profile's connection, which the gateway's discovery
opened at start and its messaging chats use all day, is not the session's, so every configured
server answered "MCP server 'gbrain' is not connected" in exactly the sessions whose editor passes a
server (Lazurio/hermes-agent#12). Without an editor server the turn runs outside any session scope
and the same call works.

Here a tool of an MCP server the current session scope has no connection of its own for (a server
from ``config.yaml``) runs without the session's overlay, as it does in a session without editor
servers: the profile's live or lazily registered connection, the profile's trust level and spend
limit, nothing connected a second time, and a multiplexed gateway still resolves only the session's
own profile. The editor's own servers keep their session scope.

Overlay row ``acp: a session whose editor passes MCP servers reaches the configured ones too``
(docs/operations/lazurio-fork-release.md). Removal: when upstream runs a session scope's calls to
the profile's MCP servers against the profile's connections.
"""
from __future__ import annotations

from functools import wraps


def profile_scoped(server_name, handler):
    """*handler* for a tool of MCP server *server_name*, run without the current session tool scope
    when that scope has no connection of its own for the server."""
    @wraps(handler)
    def _handler(args, **kwargs):
        from tools.registry import current_session_tool_scope, session_tool_scope
        if current_session_tool_scope() is None or _session_connection(server_name):
            return handler(args, **kwargs)
        with session_tool_scope(None):
            return handler(args, **kwargs)
    return _handler


def _session_connection(server_name):
    """Whether the current scope has a live, reconnecting or lazily registered connection for the server."""
    from tools.mcp_tool_common import _core
    from tools.mcp_tool_scope import _resolve_server_key
    key = _resolve_server_key(server_name)
    return key in _core._servers or key in _core._lazy_server_configs
