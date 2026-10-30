"""Lazurio overlay: a Codex request that searches on the provider's side offers no web_extract that can never extract.

tools/lazurio_web_extract.py, called by the Codex transport's search swap: on Buddy's install (a
ChatGPT sign-in, no web key) Hermes resolves ``openai-native``, which is search-only, for extract
too, so his Codex request carries no web_extract and its ``execute_code`` lists none. A backend that
can extract keeps it, ready or not; off Codex, or without the provider-side search, the request stays
as upstream builds it. Decided per request, so a sign-in or a sign-out shows at once. Overlay row:
``tools: a Codex request that searches on the provider's side offers no web_extract that can never
extract`` (docs/operations/lazurio-fork-release.md).
"""
import json
from types import SimpleNamespace

import pytest

from agent.transports import get_transport
from tools import web_tools
from tools.code_execution_tool import SANDBOX_ALLOWED_TOOLS, build_execute_code_schema

_WEB_KEYS = ("TAVILY_API_KEY", "PERPLEXITY_API_KEY", "EXA_API_KEY", "PARALLEL_API_KEY", "KEENABLE_API_KEY",
             "FIRECRAWL_API_KEY", "FIRECRAWL_API_URL", "SEARXNG_URL", "BRAVE_SEARCH_API_KEY")
_EXTRACT_DOC = "web_extract(urls: list[str]"


@pytest.fixture
def transport():
    return get_transport("codex_responses")


@pytest.fixture
def buddys_install(monkeypatch, tmp_path):
    """Buddy's web as installed: no web key, no ddgs, no managed gateway, nothing configured; signed
    in to ChatGPT (openai-codex) once ``sign_in()`` runs, out again by ``sign_out()``;
    ``configure(text)`` writes his config.yaml."""
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

    def configure(text):
        (home / "config.yaml").write_text(text)

    return SimpleNamespace(sign_in=sign_in, sign_out=sign_out, configure=configure)


def _tools():
    """The functions a Buddy turn carries, as model_tools builds them: execute_code listing every
    sandbox function, the client web_search and web_extract."""
    def function(name):
        return {"type": "function", "function": {"name": name, "description": f"{name}.",
                                                  "parameters": {"type": "object", "properties": {}}}}
    return [{"type": "function", "function": build_execute_code_schema(SANDBOX_ALLOWED_TOOLS)},
            function("web_search"), function("web_extract")]


def _request(transport, is_codex_backend=True):
    kw = transport.build_kwargs(model="gpt-6.1-terra", messages=[{"role": "user", "content": "Read it."}],
                                tools=_tools(), is_codex_backend=is_codex_backend)
    tools = kw.get("tools", [])
    execute_code = next(t for t in tools if t.get("name") == "execute_code")
    listed = execute_code["description"] + "\n" + execute_code["parameters"]["properties"]["code"]["description"]
    return {t.get("name") or t.get("type") for t in tools}, listed


def test_buddys_codex_request_offers_no_web_extract(transport, buddys_install):
    buddys_install.sign_in()  # his ChatGPT sign-in: the real autodetect picks openai-native for both
    names, listed = _request(transport)
    assert "web_search" in names  # the provider-side search
    assert "web_extract" not in names
    assert _EXTRACT_DOC not in listed and "web_extract" not in listed
    for kept in ("terminal(command: str", "read_file(path: str"):
        assert kept in listed


def test_a_sign_in_and_a_sign_out_show_in_the_next_request_with_nothing_cached(transport, buddys_install):
    names, listed = _request(transport)  # signed out: no provider-side search, nothing changes
    assert "web_extract" in names and _EXTRACT_DOC in listed
    buddys_install.sign_in()
    names, listed = _request(transport)
    assert "web_extract" not in names and _EXTRACT_DOC not in listed
    buddys_install.sign_out()
    names, listed = _request(transport)
    assert "web_extract" in names and _EXTRACT_DOC in listed


def test_installer_config_without_the_keyless_tiers_offers_no_web_extract(transport, buddys_install):
    buddys_install.sign_in()
    buddys_install.configure("web:\n  search_backend: openai-native\n  keyless_fallback: false\n")
    names, _listed = _request(transport)
    assert "web_search" in names and "web_extract" not in names


def test_a_backend_that_can_extract_keeps_web_extract(transport, buddys_install, monkeypatch):
    buddys_install.sign_in()
    monkeypatch.setenv("FIRECRAWL_API_KEY", "key")
    buddys_install.configure("web:\n  search_backend: openai-native\n  extract_backend: firecrawl\n")
    names, listed = _request(transport)
    assert "web_search" in names
    assert "web_extract" in names and _EXTRACT_DOC in listed


def test_a_cold_process_runs_web_extracts_plugin_discovery_before_it_judges(transport, buddys_install, monkeypatch):
    """A fresh process (a delegate child, a script) has run no plugin discovery yet: the hook runs
    web_extract_tool's own discovery step before it resolves, so a configured Firecrawl keeps it."""
    from agent import web_search_registry

    buddys_install.sign_in()
    monkeypatch.setenv("FIRECRAWL_API_KEY", "key")
    buddys_install.configure("web:\n  search_backend: openai-native\n  extract_backend: firecrawl\n"
                             "  keyless_fallback: false\n")
    providers = web_search_registry._providers
    discovered = dict(providers)
    calls = []

    def discovery():
        calls.append(True)
        providers.update(discovered)

    monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", discovery)
    providers.clear()  # nothing discovered yet, as in a fresh process
    try:
        names, listed = _request(transport)
    finally:
        providers.clear()
        providers.update(discovered)
    assert calls
    assert "web_extract" in names and _EXTRACT_DOC in listed


def test_readiness_is_not_judged_a_backend_that_can_extract_answers_for_itself(transport, buddys_install):
    buddys_install.sign_in()  # Perplexity can extract; without its key its own error says so
    buddys_install.configure("web:\n  search_backend: openai-native\n  extract_backend: perplexity\n")
    names, listed = _request(transport)
    assert "web_extract" in names and _EXTRACT_DOC in listed


def test_off_codex_the_request_keeps_web_extract(transport, buddys_install):
    buddys_install.sign_in()
    names, listed = _request(transport, is_codex_backend=False)
    assert "web_extract" in names and _EXTRACT_DOC in listed


def test_a_resolution_that_raises_keeps_web_extract(transport, buddys_install, monkeypatch):
    import tools.web_tools_extract as extract

    def broken(_backend):
        raise RuntimeError("registry broken")

    buddys_install.sign_in()
    monkeypatch.setattr(extract, "_resolve_extract_provider", broken)
    names, listed = _request(transport)
    assert "web_extract" in names and _EXTRACT_DOC in listed
