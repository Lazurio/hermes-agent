"""Lazurio overlay: ``hermes acp --check`` also checks the MCP SDK.

Lazurio checks Buddy's ACP server with ``hermes acp --check``. The MCP servers an editor passes to
its sessions (Lazurio T3 Code passes Buddy its own) are attached by the profile's gateway daemon
through the ``mcp`` SDK, an optional extra; without it upstream skips MCP with only a debug line, so
the chat simply lacks the editor's tools. Upstream's check imports only the ACP SDK and the adapter,
so such an install passed it. Here the check also imports the MCP SDK as the gateway's MCP client
does (``tools/mcp_tool.py``) and otherwise fails with the command that installs it.

Overlay row ``acp: hermes acp --check also checks the MCP SDK`` (docs/operations/lazurio-fork-release.md).
Removal: when upstream's ``hermes acp --check`` checks the MCP SDK.
"""
from __future__ import annotations

import sys


def check_mcp_sdk() -> None:
    """Return when the MCP SDK imports; otherwise exit 1 with the reason and the install command on
    stderr. It exits rather than raising ImportError, which ``hermes acp`` reports as a missing ACP
    extra."""
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError as exc:  # absent, or a build without these names (as ``tools/mcp_tool.py`` treats it)
        from pm import install_hint
        print(f"Hermes ACP check failed: the MCP SDK does not import ({type(exc).__name__}: {exc}), so the "
              "MCP servers an editor passes to its sessions would not be attached. Install it with: "
              f"{install_hint('mcp')}", file=sys.stderr)
        raise SystemExit(1) from None
