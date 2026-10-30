"""Lazurio overlay: a Codex request that searches on the provider's side lists no web_search in execute_code.

Buddy signs in with his own ChatGPT account (openai-codex) and has no web search key of his own, so
Hermes's autodetect picks the ``openai-native`` backend. That backend never searches itself: on a
Codex request the transport swaps the client ``web_search`` function for OpenAI's server-side
built-in (``agent/transports/codex.py`` ``_alias_wire_tools``), and the model searches inside its own
turn. The ``execute_code`` tool of that same request still listed ``web_search`` among the
functions a script can import, and a ``web_search`` from a script runs on the client, where
``openai-native`` can only fail ("it cannot run as a client-side search"); the keyless rescue then
serves it from an anonymous free tier, often with no results. Buddy batched his searches in scripts,
so they failed while his direct searches worked.

Here the transport, in the step that swaps the search, also rebuilds that request's ``execute_code``
without ``web_search``: the model searches with the tool the request carries, which
the provider serves. It is decided per request, by the swap itself, so it holds exactly where the
server-side search exists (a Codex request with ``openai-native`` selected), follows a sign-in or a
change of backend at once, and leaves every other request, and the sandbox's own functions, as
upstream has them.

Overlay row ``tools: a Codex request that searches on the provider's side lists no web_search in
execute_code`` (docs/operations/lazurio-fork-release.md). Removal: when upstream's swap also drops
the client search from ``execute_code``, or ``openai-native`` serves client-side calls.
"""
from __future__ import annotations

from typing import Any

CLIENT_SEARCH = "web_search"


def without_sandbox_search(response_tools: list[Any]) -> list[Any]:
    """*response_tools* (Responses API functions) with ``execute_code`` rebuilt without
    ``web_search``, for a request whose client search the transport just swapped for the
    server-side one: its description and parameters as upstream's ``build_execute_code_schema``
    builds them for the other functions it lists. Every other tool stays as it was."""
    from tools.code_execution_tool import _TOOL_DOC_LINES, _get_execution_mode, build_execute_code_schema

    out = []
    for tool in response_tools:
        if isinstance(tool, dict) and tool.get("type") == "function" and tool.get("name") == "execute_code":
            description = str(tool.get("description") or "")
            listed = {name for name, doc in _TOOL_DOC_LINES if doc in description}
            if CLIENT_SEARCH in listed:
                schema = build_execute_code_schema(listed - {CLIENT_SEARCH}, mode=_get_execution_mode())
                tool = {**tool, "description": schema["description"], "parameters": schema["parameters"]}
        out.append(tool)
    return out
