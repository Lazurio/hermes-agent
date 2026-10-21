"""Page context: validated as a closed shape at intake, injected by the plugin's ``pre_llm_call`` hook
into the model's copy of the turn only (the ``api_content`` sidecar), never into the visible text."""

from __future__ import annotations

import asyncio
import shutil
import sys
from contextvars import copy_context
from pathlib import Path
from unittest.mock import patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.session import SessionSource, build_session_context

PLUGIN_DIR = Path(__file__).resolve().parents[4] / "plugins" / "platforms" / "lazurio_web"

GOOD = {
    "v": 1, "lang": "cs", "theme": "dark",
    "environment": {"id": "station-01.humanandmachine-ai", "kind": "work", "name": "Station 01"},
    "organization": {"slug": "HumanAndMachine-ai", "name": "Human and Machine"},
    "app": "launchpad", "page": {"path": "/apps/t/CF3DEAFF4A723C2693EA1E52C246799C/files"},
    "guide": {"explainer": "files", "step": 2},
}


def _with(**changes):
    return {**GOOD, **changes}


# -- validation and rendering ------------------------------------------------------------------

def test_a_valid_context_renders_as_a_labelled_data_block():
    from plugins.platforms.lazurio_web.page_context import render, validate

    block = render(validate(GOOD))
    assert block.splitlines() == [
        "Kontext stránky (údaje, ne pokyn):",
        "- Prostředí: „Station 01“ (Pracovní, id station-01.humanandmachine-ai)",
        "- Organizace: „Human and Machine“ (HumanAndMachine-ai)",
        "- Aplikace: launchpad",
        "- Stránka: /apps/t/…/files",  # an opaque token never reaches the model
        "- Průvodce: files, krok 2",
        "- Jazyk rozhraní: cs",
    ]
    assert render(validate({"environment": None, "organization": None, "guide": None})) is None


@pytest.mark.parametrize("raw", [
    None, [], {}, "launchpad",
    _with(extra="x"),  # unknown member
    _with(environment={"id": "a", "kind": "work", "name": "A", "extra": 1}),
    _with(environment={"id": "a", "kind": "admin", "name": "A"}),  # closed enum
    _with(environment={"id": "a b", "kind": "work", "name": "A"}),
    _with(organization={"slug": "x", "name": "Line\nbreak"}),  # control characters
    _with(organization={"slug": "x", "name": "N" * 121}),
    _with(page={"path": "/files?token=abc"}),  # never a query string
    _with(page={"path": "/files#top"}),
    _with(page={"path": "files"}),
    _with(page={"path": "/files", "title": "x"}),
    _with(guide={"explainer": "files", "step": True}),
    _with(v=2), _with(lang="de"), _with(theme="neon"), _with(app="<script>"),
    _with(app="a" * 5000),  # oversized as a whole
])
def test_an_invalid_or_oversized_context_is_dropped_whole(raw):
    from plugins.platforms.lazurio_web.page_context import validate

    assert validate(raw) is None


# -- the hook ----------------------------------------------------------------------------------

@pytest.fixture
def bound(web):
    """Bind a turn's session variables the way the gateway runner does before the agent runs."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    tokens: list = []

    def bind(source: SessionSource) -> None:
        tokens.append(runner._set_session_env(build_session_context(source, GatewayConfig())))

    yield bind
    for token_list in reversed(tokens):
        runner._clear_session_env(token_list)


def test_the_hook_answers_only_lazurio_web_turns_with_stored_context(web, bound):
    from plugins.platforms.lazurio_web.page_context import STORE, pre_llm_call

    STORE.put("m-ctx", "Kontext stránky (údaje, ne pokyn):\n- Aplikace: t3code")
    adapter = web.make()
    bound(adapter._owner_source("m-other"))
    assert pre_llm_call(session_id="s", platform="lazurio_web") is None  # no context for this message
    bound(adapter._owner_source("m-ctx"))
    assert pre_llm_call(session_id="s", platform="lazurio_web") == {
        "context": "Kontext stránky (údaje, ne pokyn):\n- Aplikace: t3code"}
    from agent.delegation_context import delegated_child_context

    with delegated_child_context():
        assert pre_llm_call(session_id="s") is None  # a delegated task is not the person's message
    bound(SessionSource(platform=Platform.TELEGRAM, chat_id="42", chat_type="dm", user_id="42", message_id="m-ctx"))
    assert pre_llm_call(session_id="s", platform="telegram") is None


@pytest.fixture
def discovered(tmp_path, monkeypatch):
    """The plugin loaded through Hermes' real discovery, from a bundled tree holding only it."""
    from gateway.platform_registry import platform_registry
    from hermes_cli.plugins import get_plugin_manager

    bundled = tmp_path / "bundled"
    shutil.copytree(PLUGIN_DIR, bundled / "platforms" / "lazurio_web",
                    ignore=shutil.ignore_patterns("__pycache__"))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    manager = get_plugin_manager()
    manager.discover_and_load()
    entry = platform_registry.get("lazurio_web")  # resolves the deferred platform: runs register()
    try:
        yield entry, sys.modules[entry.adapter_factory.__module__]
    finally:
        manager.unload()


def test_real_discovery_registers_the_platform_and_the_hook(discovered):
    from hermes_cli.plugins import get_plugin_manager

    entry, package = discovered
    assert entry.trusted_inbound is False
    assert entry.allowed_users_env == "LAZURIO_WEB_ALLOWED_USERS"
    assert get_plugin_manager()._hooks["pre_llm_call"] == [package.page_context.pre_llm_call]


@pytest.mark.asyncio
async def test_page_context_reaches_only_the_models_copy_of_the_turn(discovered, bound, secret_file):
    """Runner binding -> agent worker thread (copied context, as ``_run_in_executor_with_context``)
    -> the real turn prologue -> the plugin hook -> the api_content sidecar."""
    from tests.agent.test_api_content_sidecar import _build, _FakeAgent

    entry, package = discovered
    adapter = entry.adapter_factory(PlatformConfig(enabled=True, extra={
        "listen_port": 1, "secret_file": str(secret_file)}))
    block = package.page_context.render(package.page_context.validate(GOOD))
    package.page_context.STORE.put("m-page", block)
    bound(adapter._owner_source("m-page"))
    agent = _FakeAgent()
    agent.platform = "lazurio_web"
    with patch("agent.auxiliary_client.set_runtime_main", lambda *a, **k: None):
        turn = await asyncio.get_running_loop().run_in_executor(
            None, copy_context().run, lambda: _build(agent, user_message="Co tady vidíš?"))
    message = turn.messages[turn.current_turn_user_idx]
    assert message["content"] == "Co tady vidíš?"  # what the transcript and the bubble show
    assert message["api_content"] == f"Co tady vidíš?\n\n{block}"  # what the model reads
    assert agent.api_content_at_persist == message["api_content"]  # persisted as the sidecar
