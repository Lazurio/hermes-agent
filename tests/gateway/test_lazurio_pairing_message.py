"""Lazurio overlay: a configured text replaces the reply that gives an unknown sender its code.

Lazurio's Launchpad (LazurioPlatform decision F66) approves a pairing request from its own page, so
the person who presses Start in their own bot reads Lazurio's words instead of upstream's
``hermes pairing approve`` command. ``unauthorized_dm_pairing_message`` sets the reply's text; the
request is recorded as upstream records it, and a ready ``identify`` names the capability. Overlay row
``pairing: a configured text replaces the reply that gives an unknown sender its code``
(docs/operations/lazurio-fork-release.md).
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.lazurio_pairing_message import CAPABILITY, CONFIG_KEY, configured_text, pairing_reply
from gateway.pairing import PairingStore
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override

TEXT = "Dobrý den, tady Rex. Potvrďte prosím v Lazuriu, že jste to vy, a pak mi napište znovu."


def _runner(store, adapter):
    runner = object.__new__(GatewayRunner)
    runner._pairing_store_for = lambda _source: store
    runner._delivery_adapter_for = lambda _source: adapter
    runner.config = None
    return runner


def _source(user_id):
    return SessionSource(platform=Platform.TELEGRAM, chat_id=user_id, chat_type="dm", user_id=user_id,
                         user_name=f"name-{user_id}")


def _config(text: str) -> None:
    """config.yaml of the test's home, as a writer of Hermes leaves it."""
    (get_hermes_home() / "config.yaml").write_text(text, encoding="utf-8")


def _sent(adapter) -> str:
    adapter.send.assert_awaited_once()
    return adapter.send.await_args.args[1]


def test_the_key_is_read_at_the_top_level_first_then_in_the_gateway_section():
    assert configured_text({CONFIG_KEY: "  top  ", "gateway": {CONFIG_KEY: "section"}}) == "top"
    assert configured_text({"gateway": {CONFIG_KEY: " section "}}) == "section"
    # Blank, absent, not text or no mapping: upstream's reply.
    for config in ({CONFIG_KEY: "   "}, {CONFIG_KEY: 7, "gateway": {CONFIG_KEY: ["x"]}}, {"gateway": "x"}, {},
                   None, "text"):
        assert configured_text(config) == ""
    assert configured_text({CONFIG_KEY: "", "gateway": {CONFIG_KEY: "section"}}) == "section"


@pytest.mark.asyncio
async def test_the_configured_text_is_the_reply_and_the_request_is_recorded_as_upstream_records_it():
    _config(f"gateway:\n  {CONFIG_KEY}: 'Code {{code}}, holds {{validity}}; {{name}} stays. {TEXT}'\n")
    store = PairingStore()
    adapter = AsyncMock()
    await _runner(store, adapter)._hm_offer_pairing_code(_source("4242"))
    reply = _sent(adapter)
    pending = store.list_pending("telegram")
    assert [(entry["user_id"], entry["user_name"]) for entry in pending] == [("4242", "name-4242")]
    assert len(pending[0]["request_id"]) == 16
    # The code is the recorded one: approving it by code lets the sender in.
    code = reply.split()[1].rstrip(",")
    assert reply == f"Code {code}, holds 1 hour; {{name}} stays. {TEXT}"
    assert store.approve_code("telegram", code)["user_id"] == "4242"
    assert "hermes pairing approve" not in reply


@pytest.mark.asyncio
async def test_without_the_key_upstreams_reply_is_sent():
    _config("display:\n  language: en\n")
    adapter = AsyncMock()
    await _runner(PairingStore(), adapter)._hm_offer_pairing_code(_source("5151"))
    assert "hermes pairing approve telegram" in _sent(adapter)


@pytest.mark.asyncio
async def test_a_change_of_the_key_applies_to_the_next_start_without_a_restart():
    store = PairingStore()
    _config(f"{CONFIG_KEY}: first\n")
    first = AsyncMock()
    await _runner(store, first)._hm_offer_pairing_code(_source("1001"))
    assert _sent(first) == "first"
    _config(f"gateway:\n  {CONFIG_KEY}: second {{code}}\n")
    second = AsyncMock()
    await _runner(store, second)._hm_offer_pairing_code(_source("1002"))
    assert _sent(second).startswith("second ")
    _config("{}\n")
    third = AsyncMock()
    await _runner(store, third)._hm_offer_pairing_code(_source("1003"))
    assert "hermes pairing approve" in _sent(third)


def test_the_text_is_read_in_the_profile_scope_the_gateway_binds():
    """A multiplexed profile's handler runs under that profile's home override: its text, not the
    launch profile's."""
    _config(f"{CONFIG_KEY}: launch\n")
    profile_home = get_hermes_home() / "profiles" / "work"
    profile_home.mkdir(parents=True)
    (profile_home / "config.yaml").write_text(f"{CONFIG_KEY}: work {{code}}\n", encoding="utf-8")
    token = set_hermes_home_override(profile_home)
    try:
        assert pairing_reply("ABCD2345") == "work ABCD2345"
    finally:
        reset_hermes_home_override(token)
    assert pairing_reply("ABCD2345") == "launch"


@pytest.mark.asyncio
async def test_a_config_that_no_longer_parses_sends_upstreams_reply_not_the_last_good_text():
    """The loader serves the last-known-good settings for a file it cannot parse; this reply never
    takes its text from them."""
    from hermes_cli.config import load_config_readonly
    from hermes_cli.config_read_errors import FailedConfigRead

    _config(f"{CONFIG_KEY}: previous message\n")
    assert pairing_reply("ABCD2345") == "previous message"
    _config("gateway: [broken\n")
    # Every other reader keeps the last-known-good settings, this text among them.
    fallback = load_config_readonly()
    assert isinstance(fallback, FailedConfigRead)
    assert fallback.get(CONFIG_KEY) == "previous message"
    assert pairing_reply("ABCD2345") is None
    adapter = AsyncMock()
    store = PairingStore()
    await _runner(store, adapter)._hm_offer_pairing_code(_source("8181"))
    assert "hermes pairing approve telegram" in _sent(adapter)
    assert [entry["user_id"] for entry in store.list_pending("telegram")] == ["8181"]
    # Fixed again: the configured text again.
    _config(f"gateway:\n  {CONFIG_KEY}: fixed {{code}}\n")
    assert pairing_reply("ABCD2345") == "fixed ABCD2345"


@pytest.mark.asyncio
async def test_a_config_that_cannot_be_read_sends_upstreams_reply(monkeypatch):
    import hermes_cli.config

    def broken():
        raise OSError("unreadable")

    monkeypatch.setattr(hermes_cli.config, "load_config_readonly", broken)
    assert pairing_reply("ABCD2345") is None
    adapter = AsyncMock()
    await _runner(PairingStore(), adapter)._hm_offer_pairing_code(_source("6161"))
    assert "hermes pairing approve telegram" in _sent(adapter)


@pytest.mark.asyncio
async def test_the_rate_limited_reply_stays_upstreams():
    _config(f"{CONFIG_KEY}: {TEXT}\n")
    store = PairingStore()
    for user in ("7001", "7002", "7003"):
        await _runner(store, AsyncMock())._hm_offer_pairing_code(_source(user))
    # A fourth sender finds three requests pending: upstream's rate-limited reply.
    adapter = AsyncMock()
    await _runner(store, adapter)._hm_offer_pairing_code(_source("7004"))
    reply = _sent(adapter)
    assert reply != TEXT and "pairing requests" in reply


def test_a_ready_gateway_names_the_capability(monkeypatch):
    import gateway.session_hosted_service
    from gateway.run_runtime import publish_gateway_runtime_ready

    monkeypatch.setattr(gateway.session_hosted_service, "start_ready_hosted_services", lambda _runner: None)
    descriptor = {}
    runner = SimpleNamespace(
        session_runtime_descriptor=SimpleNamespace(update=lambda **fields: descriptor.update(fields)),
        session_api=SimpleNamespace(task=SimpleNamespace(done=lambda: False)),
        _running=True,
        _draining=False,
    )
    publish_gateway_runtime_ready(runner)
    assert descriptor["state"] == "ready"
    # Beside upstream's own capabilities and the sign-in contract, which every reader still finds.
    assert {"session-authority-v1", "durable-admission-v1", "event-replay-v1",
            "lazurio-oauth-cancel-contract-v1", CAPABILITY} <= set(descriptor["capabilities"])
    assert CAPABILITY == "lazurio-pairing-message-v1"
