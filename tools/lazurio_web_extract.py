"""Lazurio overlay: a Codex request that searches on the provider's side offers no web_extract that can never extract.

Buddy signs in with his own ChatGPT account (openai-codex) and has no web key, so Hermes picks the
``openai-native`` backend for search and for extract. It is search-only: every web_extract call
answers that it "cannot extract URL content" and points the model at a third-party extract backend
to configure, yet upstream offers the tool, because web_search and web_extract share one check
(``check_web_api_key``: is any web backend available). Lazurio decided against third-party
extraction (Matěj, 2026-10-30): Buddy reads public pages through the provider-side search, which
opens a URL, and uses the Environment's browser for anything it can't (the browser tools, which
web_extract's own description already names as the fallback).

Here the Codex transport, in the step that swaps the client web_search for the server-side one
(``_alias_wire_tools``, the same step as tools/lazurio_native_search.py), also drops web_extract
from that request, and rebuilds the request's ``execute_code`` without it, when the backend
web_extract would call can never extract: ``_get_extract_backend`` and
``_resolve_extract_provider``, the two calls ``web_extract_tool`` makes, with its plugin discovery
between them, resolve no extract-capable provider (a search-only backend, an unknown strict
selection, or none). Readiness is not judged: a backend that can extract keeps web_extract and
answers for itself. Decided per request, so a sign-in or a change of backend shows at once, with
nothing cached; any other request stays as upstream builds it, and so does the sandbox's own
function set.

Overlay row ``tools: a Codex request that searches on the provider's side offers no web_extract that
can never extract`` (docs/operations/lazurio-fork-release.md). Removal: when upstream offers
web_extract only where a backend can extract.
"""
from __future__ import annotations

from typing import Any

EXTRACT = "web_extract"


def extract_impossible() -> bool:
    """Whether the backend web_extract would call resolves to no provider that can extract, in
    ``web_extract_tool``'s own order: the backend, plugin discovery (a fresh process, a delegate
    child or a script has run none yet), then the resolution. A resolution that raises judges
    nothing (False), so the request stays as upstream builds it."""
    try:
        from tools.web_tools import _ensure_web_plugins_loaded, _get_extract_backend
        from tools.web_tools_extract import _resolve_extract_provider

        backend = _get_extract_backend()
        _ensure_web_plugins_loaded()
        provider, _error = _resolve_extract_provider(backend)
    except Exception:
        return False
    return provider is None


def without_web_extract(response_tools: list[Any]) -> list[Any]:
    """*response_tools* (Responses API functions) without web_extract, and with ``execute_code``
    rebuilt without it, when no backend can extract; as they were otherwise. ``execute_code``'s
    description and parameters come from upstream's ``build_execute_code_schema`` for the other
    functions it lists. Every other tool stays as it was."""
    if not extract_impossible():
        return response_tools
    from tools.code_execution_tool import _TOOL_DOC_LINES, _get_execution_mode, build_execute_code_schema

    out = []
    for tool in response_tools:
        if isinstance(tool, dict) and tool.get("type") == "function" and tool.get("name") == EXTRACT:
            continue
        if isinstance(tool, dict) and tool.get("type") == "function" and tool.get("name") == "execute_code":
            description = str(tool.get("description") or "")
            listed = {name for name, doc in _TOOL_DOC_LINES if doc in description}
            if EXTRACT in listed:
                schema = build_execute_code_schema(listed - {EXTRACT}, mode=_get_execution_mode())
                tool = {**tool, "description": schema["description"], "parameters": schema["parameters"]}
        out.append(tool)
    return out
