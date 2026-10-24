"""Lazurio overlay: a session with its editor's MCP servers still reaches the configured ones.

Buddy's chat in Lazurio T3 Code is an ACP session whose editor passes an MCP server of its own. The
gateway owns such servers as session state: each turn of the session runs inside its own tool scope
(``gateway/session_local_mcp.py`` ``_borrowed``), so the editor's connections are keyed
``(session scope, name)`` and their tools live in the session's registry overlay, which no other
session or profile sees. The registry documents that overlay as additive to the profile registry,
and the profile's tools stay listed in it, but the MCP code those tools run is not additive: it
resolves the server's connection, and keys its trust level, spend limit, circuit breaker and lazy
connect, by the current registry scope, which inside the overlay is the session's. The profile's
connection, which the gateway's discovery opened at start and its messaging chats use all day, is
not the session's. So the availability check that builds the model's tool list (``registry.
get_definitions``: the agent's tools, the Tool Search catalog) found no connection and left a
configured server's tools out, unless a verdict cached outside the session still covered them; and
a tool the model was offered anyway answered "MCP server 'gbrain' is not connected", in exactly the
sessions whose editor passes a server (Lazurio/hermes-agent#12). The check's verdict is cached per
profile, not per session, so a session's verdict also answered for the profile's own chats.

Here an MCP tool runs, and is checked, in the scope that owns it: the scope its tools were
registered under. A configured server's tools belong to the profile, so in an editor's session they
are checked and run without the session's overlay, as in a session without editor servers: offered
by the profile's verdict, over the profile's connection, under the profile's trust level and spend
limit, nothing connected a second time. An editor's server's tools belong to its session's overlay
and keep being checked and run in it. Each verdict then depends on its owner alone, so a verdict
never answers for a scope it was not probed for: not a session's for the profile or for another
session, not the profile's for a session's own server.

Overlay row ``acp: a session whose editor passes MCP servers reaches the configured ones too``
(docs/operations/lazurio-fork-release.md). Removal: when upstream checks and runs a session scope's
calls to the profile's MCP servers against the profile's connections.
"""
from __future__ import annotations

from functools import wraps


def owner_scoped(registered_scope, fn):
    """*fn*, the handler or the availability check of MCP tools registered under *registered_scope*
    (``tools/mcp_tool_registration.py`` ``_register_candidates``), run in the scope that owns them: as
    called when they are the current session's own or no session is bound, else without the session's
    tool overlay."""
    @wraps(fn)
    def _scoped(*args, **kwargs):
        from tools.registry import current_session_tool_scope, session_tool_scope
        session = current_session_tool_scope()
        if session is None or session == registered_scope:
            return fn(*args, **kwargs)
        with session_tool_scope(None):
            return fn(*args, **kwargs)
    return _scoped
