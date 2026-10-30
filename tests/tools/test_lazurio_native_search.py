"""Lazurio overlay: a Codex request that searches on the provider's side lists no web_search in execute_code.

tools/lazurio_native_search.py, called by the Codex transport's search swap: with the
``openai-native`` backend (Buddy's: a ChatGPT sign-in and no search key), a Codex request carries
the server-side search and its ``execute_code`` description lists no ``web_search``, which a script
could only run client-side, where it fails. Off Codex, or with another backend, the request keeps
both as upstream builds them. Decided per request, so a sign-in shows at once. Overlay row:
``tools: a Codex request that searches on the provider's side lists no web_search in execute_code``
(docs/operations/lazurio-fork-release.md).
"""
import json
from types import SimpleNamespace

import pytest

from agent.transports import get_transport
from tools import web_tools
from tools.code_execution_tool import SANDBOX_ALLOWED_TOOLS, build_execute_code_schema

_WEB_KEYS = ("TAVILY_API_KEY", "PERPLEXITY_API_KEY", "EXA_API_KEY", "PARALLEL_API_KEY", "KEENABLE_API_KEY",
             "FIRECRAWL_API_KEY", "FIRECRAWL_API_URL", "SEARXNG_URL", "BRAVE_SEARCH_API_KEY")
_SEARCH_DOC = "web_search(query: str, limit: int = 5) -> dict"


@pytest.fixture
def transport():
    return get_transport("codex_responses")


@pytest.fixture
def buddys_install(monkeypatch, tmp_path):
    """Buddy's web search as installed: no search key, no ddgs, no managed gateway, nothing
    configured; signed in to ChatGPT (openai-codex) once the returned ``sign_in()`` runs, and out
    again by ``sign_out()``."""
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for key in _WEB_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(web_tools, "_ddgs_package_importable", lambda: False)
    monkeypatch.setattr(web_tools, "_is_tool_gateway_ready", lambda: False)
    web_tools._ensure_web_plugins_loaded()  # as the gateway has them

    auth = home / "auth.json"

    def sign_in():
        auth.write_text(json.dumps({"providers": {"openai-codex": {"tokens": {"access_token": "t"}}}}))

    def sign_out():
        auth.unlink()

    return SimpleNamespace(sign_in=sign_in, sign_out=sign_out)


def _tools():
    """The functions a Buddy turn carries, as model_tools builds them: execute_code listing every
    sandbox function, and the client web_search."""
    return [
        {"type": "function", "function": build_execute_code_schema(SANDBOX_ALLOWED_TOOLS)},
        {"type": "function", "function": {"name": "web_search", "description": "Search the web.",
                                          "parameters": {"type": "object", "properties": {"query": {"type": "string"}}}}},
    ]


def _request(transport, is_codex_backend=True):
    kw = transport.build_kwargs(model="gpt-6.1-terra", messages=[{"role": "user", "content": "Find it."}],
                                tools=_tools(), is_codex_backend=is_codex_backend)
    tools = kw.get("tools", [])
    execute_code = next(t for t in tools if t.get("name") == "execute_code")
    return tools, execute_code["description"] + "\n" + execute_code["parameters"]["properties"]["code"]["description"]


def test_buddys_codex_request_searches_server_side_and_execute_code_lists_no_web_search(transport, buddys_install):
    buddys_install.sign_in()  # his ChatGPT sign-in: the real autodetect picks openai-native
    tools, described = _request(transport)

    assert any(t.get("type") == "web_search" for t in tools), tools
    assert "web_search" not in described  # neither listed nor in the import example of ``code``
    for kept in ("web_extract(urls: list[str]", "terminal(command: str", "read_file(path: str"):
        assert kept in described


def test_a_sign_in_and_a_sign_out_show_in_the_next_request_with_nothing_cached(transport, buddys_install):
    # Signed out nothing selects the server-side search: the request keeps the client search.
    tools, described = _request(transport)
    assert not any(t.get("type") == "web_search" for t in tools)
    assert _SEARCH_DOC in described
    buddys_install.sign_in()
    tools, described = _request(transport)
    assert any(t.get("type") == "web_search" for t in tools)
    assert _SEARCH_DOC not in described
    buddys_install.sign_out()
    tools, described = _request(transport)
    assert not any(t.get("type") == "web_search" for t in tools)
    assert _SEARCH_DOC in described


def test_off_codex_the_request_keeps_web_search_in_execute_code(transport, buddys_install):
    buddys_install.sign_in()
    tools, described = _request(transport, is_codex_backend=False)
    assert not any(t.get("type") == "web_search" for t in tools)
    assert _SEARCH_DOC in described


def test_another_backend_keeps_web_search_in_execute_code(transport, buddys_install, monkeypatch):
    buddys_install.sign_in()
    monkeypatch.setenv("TAVILY_API_KEY", "key")
    tools, described = _request(transport)
    assert not any(t.get("type") == "web_search" for t in tools)
    assert _SEARCH_DOC in described


def test_only_execute_code_changes():
    from tools.lazurio_native_search import without_sandbox_search

    other = {"type": "function", "name": "read_file", "description": "web_search(query: str, limit: int = 5) -> dict"}
    builtin = {"type": "web_search"}
    assert without_sandbox_search([other, builtin]) == [other, builtin]
