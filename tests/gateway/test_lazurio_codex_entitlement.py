"""Lazurio overlay: whether the signed-in ChatGPT account may run the configured Codex model.

A free account, or one whose catalog lacks the model, signs in and then fails every turn with
upstream's raw 400 (Lazurio/LazurioPlatform#694). ``GET /api/providers/oauth/openai-codex/entitlement``
answers right after the sign-in from the token's plan claim and the account's own catalog, and a ready
``identify`` names the capability. Overlay row ``oauth: whether the signed-in ChatGPT account may run
the configured model`` (docs/operations/lazurio-fork-release.md).
"""
import base64
import json
import logging
import time
from types import SimpleNamespace

import pytest

import gateway.lazurio_codex_entitlement as subject
from gateway.lazurio_codex_entitlement import (CAPABILITY, catalog_slug, entitlement, plan_of, token_expired,
                                               verdict)
from hermes_constants import get_hermes_home

TERRA = "gpt-6.1-terra"
CATALOG = frozenset({"gpt-6.1-terra", "gpt-6.1-sol", "gpt-6-luna"})
ACCOUNT_ID = "acct-synthetic-0001"
EMAIL = "person@example.invalid"


def _jwt(claims: dict) -> str:
    def part(value: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    return f"{part({'alg': 'none'})}.{part(claims)}.signature"


def _token(plan=None, exp="fresh") -> str:
    """A synthetic access token in the shape ChatGPT's OAuth issues: the plan sits in the auth claim;
    *exp* is ``"fresh"`` (an hour ahead), ``"expired"`` (an hour ago) or None (no claim)."""
    auth = {"chatgpt_account_id": ACCOUNT_ID}
    if plan is not None:
        auth["chatgpt_plan_type"] = plan
    claims = {"https://api.openai.com/auth": auth, "https://api.openai.com/profile": {"email": EMAIL}}
    if exp is not None:
        claims["exp"] = int(time.time()) + (3600 if exp == "fresh" else -3600)
    return _jwt(claims)


@pytest.fixture
def account(monkeypatch):
    """This profile's model, its stored sign-in and its catalog, each one a field the test sets."""
    state = {"provider": "openai-codex", "model": TERRA, "token": _token("plus"), "catalog": CATALOG,
             "probes": []}
    monkeypatch.setattr(subject, "_configured_model", lambda: (state["provider"], state["model"]))
    monkeypatch.setattr(subject, "_stored_sign_in",
                        lambda: (state["token"], "https://chatgpt.com/backend-api/codex"))

    def catalog(token, base_url):
        state["probes"].append((token, base_url))
        return state["catalog"]

    monkeypatch.setattr(subject, "_catalog", catalog)
    return state


def test_the_plan_comes_from_the_access_tokens_auth_claim():
    assert plan_of(_token("free")) == "free"
    assert plan_of(_token("Plus")) == "plus"
    assert plan_of(_token(" pro ")) == "pro"
    assert plan_of(_token()) is None
    assert plan_of(_token("")) is None
    assert plan_of("not-a-jwt") is None
    assert plan_of(None) is None


def test_the_catalog_slug_drops_the_vendor_and_the_picker_alias():
    assert catalog_slug("gpt-6.1-terra") == "gpt-6.1-terra"
    assert catalog_slug("GPT-6.1-Terra") == "gpt-6.1-terra"
    assert catalog_slug("openai/gpt-6.1-sol-900k") == "gpt-6.1-sol"


@pytest.mark.parametrize(("signed_in", "provider", "model", "plan", "catalog", "expected"), [
    (False, "openai-codex", TERRA, None, None, "unknown"),
    (True, "openrouter", TERRA, "plus", CATALOG, "unknown"),
    (True, "openai-codex", "", "plus", CATALOG, "unknown"),
    # A free plan is refused even where its catalog lists the model (the decision of 2026-10-28).
    (True, "openai-codex", TERRA, "free", CATALOG, "no-subscription"),
    (True, "openai-codex", TERRA, "plus", frozenset({"gpt-6-luna"}), "model-missing"),
    (True, "openai-codex", TERRA, "plus", None, "unknown"),
    (True, "openai-codex", TERRA, None, CATALOG, "usable"),
    (True, "openai-codex", TERRA, "pro", CATALOG, "usable"),
    (True, "openai-codex", TERRA, "business", CATALOG, "usable"),
    # The picker's alias of an eligible base is that base; an ineligible one is no model of the catalog.
    (True, "openai-codex", "openai/gpt-6.1-sol-900k", "plus", CATALOG, "usable"),
    (True, "openai-codex", "gpt-6.1-terra-900k", "plus", CATALOG, "model-missing"),
])
def test_the_verdict_follows_its_rules_in_order(signed_in, provider, model, plan, catalog, expected):
    assert verdict(signed_in=signed_in, provider=provider, model=model, plan=plan, catalog=catalog) == expected


def test_a_free_account_is_refused_and_its_catalog_is_not_asked(account):
    account["token"] = _token("free")
    assert entitlement() == {"provider": "openai-codex", "model": TERRA, "plan": "free",
                             "catalog": "not-asked", "verdict": "no-subscription"}
    assert account["probes"] == []


def test_a_paid_account_whose_catalog_lists_the_model_is_usable(account):
    assert entitlement() == {"provider": "openai-codex", "model": TERRA, "plan": "plus",
                             "catalog": "listed", "verdict": "usable"}
    # The probe goes to the sign-in's own route with its own token.
    assert account["probes"] == [(account["token"], "https://chatgpt.com/backend-api/codex")]


def test_a_paid_account_whose_catalog_lacks_the_model_is_refused(account):
    account["catalog"] = frozenset({"gpt-6-luna", "gpt-6-sol"})
    answer = entitlement()
    assert (answer["catalog"], answer["verdict"]) == ("missing", "model-missing")


def test_an_unreachable_catalog_blocks_nothing(account):
    account["catalog"] = None
    answer = entitlement()
    assert (answer["catalog"], answer["verdict"]) == ("unreachable", "unknown")


def test_without_a_sign_in_or_another_provider_nothing_is_asked(account):
    account["token"] = ""
    assert entitlement() == {"provider": "openai-codex", "model": TERRA, "plan": None,
                             "catalog": "not-asked", "verdict": "unknown"}
    account["token"] = _token("plus")
    account["provider"] = "openrouter"
    assert entitlement()["verdict"] == "unknown"
    account["provider"], account["model"] = "", ""
    assert entitlement() == {"provider": None, "model": None, "plan": None, "catalog": "not-asked",
                             "verdict": "unknown"}
    assert account["probes"] == []


def test_the_answer_never_carries_the_token_the_account_or_an_address(account):
    for plan in ("free", "plus"):
        account["token"] = _token(plan)
        text = json.dumps(entitlement())
        for secret in (account["token"], account["token"].split(".")[1], ACCOUNT_ID, EMAIL):
            assert secret not in text


def test_the_configured_model_is_this_profiles_model_section():
    config = get_hermes_home() / "config.yaml"
    config.write_text("model:\n  default: gpt-6.1-terra\n  provider: OpenAI-Codex\n", encoding="utf-8")
    assert subject._configured_model() == ("openai-codex", TERRA)
    config.write_text("model:\n  name: gpt-6.1-sol\n  provider: openai-codex\n", encoding="utf-8")
    assert subject._configured_model() == ("openai-codex", "gpt-6.1-sol")
    config.write_text("model: gpt-6.1-terra\n", encoding="utf-8")
    assert subject._configured_model() == ("", TERRA)


def test_a_config_that_no_longer_parses_names_no_model():
    config = get_hermes_home() / "config.yaml"
    config.write_text("model:\n  default: gpt-6.1-terra\n  provider: openai-codex\n", encoding="utf-8")
    assert subject._configured_model() == ("openai-codex", TERRA)
    # The loader's last-known-good settings may name another model than the file now holds.
    config.write_text("model: [unclosed\n", encoding="utf-8")
    assert subject._configured_model() == ("", "")


def test_the_stored_sign_in_is_read_without_refreshing_anything(monkeypatch):
    import hermes_cli.auth_codex

    calls = []

    def resolve(**kwargs):
        calls.append(kwargs)
        return {"api_key": "token-value", "base_url": "https://chatgpt.com/backend-api/codex"}

    monkeypatch.setattr(hermes_cli.auth_codex, "resolve_codex_runtime_credentials", resolve)
    assert subject._stored_sign_in() == ("token-value", "https://chatgpt.com/backend-api/codex")
    assert calls == [{"read_only": True}]

    def missing(**_kwargs):
        raise RuntimeError("No Codex credentials stored.")

    monkeypatch.setattr(hermes_cli.auth_codex, "resolve_codex_runtime_credentials", missing)
    assert subject._stored_sign_in() == ("", "")


def _files(*roots):
    """Every file under *roots* with its bytes and mtime, so a comparison sees any write."""
    return {str(path): (path.read_bytes(), path.stat().st_mtime_ns)
            for root in roots for path in sorted(root.rglob("*")) if path.is_file()}


@pytest.fixture
def stores(tmp_path, monkeypatch):
    """This profile's auth.json and a global root's, which a profile's reads fall back to."""
    import hermes_cli.auth

    global_store = tmp_path / "global-root" / "auth.json"
    global_store.parent.mkdir()
    monkeypatch.setattr(hermes_cli.auth, "_global_auth_file_path", lambda: global_store)
    monkeypatch.setattr(hermes_cli.auth, "_global_auth_store_cache", None)
    return get_hermes_home() / "auth.json", global_store


def test_a_store_that_cannot_be_parsed_reads_as_no_sign_in_and_nothing_is_written(stores, monkeypatch, caplog):
    profile_store, global_store = stores
    # Cut off mid-write, a synthetic access token already in it.
    cut = '{"version": 1, "providers": {"openai-codex": {"tokens": {"access_token": "' + _token("plus")
    profile_store.write_text(cut, encoding="utf-8")
    global_store.write_text(cut, encoding="utf-8")
    before = _files(profile_store.parent, global_store.parent)
    monkeypatch.setattr(subject, "_configured_model", lambda: ("openai-codex", TERRA))

    with caplog.at_level(logging.WARNING, logger="hermes_cli.auth"):
        assert subject._stored_sign_in() == ("", "")
        assert entitlement()["verdict"] == "unknown"

    assert _files(profile_store.parent, global_store.parent) == before
    assert not profile_store.with_suffix(".json.corrupt").exists()
    assert not global_store.with_suffix(".json.corrupt").exists()
    # Both stores were read: the singleton's and the pool's fallback to the global root.
    read = " ".join(record.getMessage() for record in caplog.records)
    assert f"failed to parse {profile_store}" in read and f"failed to parse {global_store}" in read


def test_a_writer_still_keeps_the_copy_of_a_store_that_cannot_be_parsed(stores):
    import hermes_cli.auth

    profile_store, _ = stores
    profile_store.write_text('{"version": 1, "providers": {', encoding="utf-8")
    assert subject._stored_sign_in() == ("", "")
    assert not profile_store.with_suffix(".json.corrupt").exists()
    # Outside the entitlement's read, upstream's recovery runs as before.
    assert hermes_cli.auth._load_auth_store()["providers"] == {}
    assert profile_store.with_suffix(".json.corrupt").read_bytes() == profile_store.read_bytes()


def test_a_stored_sign_in_is_read_without_a_lock_or_any_other_write(stores):
    profile_store, global_store = stores
    token = _token("plus")
    profile_store.write_text(json.dumps({"version": 1, "providers": {"openai-codex": {
        "tokens": {"access_token": token, "refresh_token": "refresh-synthetic"}}}}), encoding="utf-8")
    before = _files(profile_store.parent, global_store.parent)

    assert subject._stored_sign_in()[0] == token
    assert _files(profile_store.parent, global_store.parent) == before


def test_the_catalog_is_every_listed_slug_lowercased_or_nothing(monkeypatch):
    import agent.model_metadata

    monkeypatch.setattr(agent.model_metadata, "_codex_catalog_probe_allowed", lambda token, base_url="": True)
    # A row counts by its slug alone, also without a context window (review of #20).
    answers = iter([
        ([{"slug": "GPT-6.1-Terra"}, {"slug": "gpt-6.1-sol", "context_window": 500_000}, {"name": "no slug"}], 200),
        ([], 200),
        ([{"slug": "gpt-6.1-terra"}], 401),
    ])
    monkeypatch.setattr(agent.model_metadata, "fetch_codex_catalog_entries",
                        lambda get, base_url="": next(answers))
    assert subject._catalog("token", "") == frozenset({"gpt-6.1-terra", "gpt-6.1-sol"})
    assert subject._catalog("token", "") is None
    assert subject._catalog("token", "") is None
    # A credential the probe may not carry to the endpoint is never sent.
    monkeypatch.setattr(agent.model_metadata, "_codex_catalog_probe_allowed", lambda token, base_url="": False)
    assert subject._catalog("token", "") is None


def test_a_failed_catalog_probe_never_logs_the_credential(monkeypatch, caplog):
    import agent.model_metadata

    token = _token("plus")
    monkeypatch.setattr(agent.model_metadata, "_codex_catalog_probe_allowed", lambda t, base_url="": True)

    def broken(get, base_url=""):
        raise OSError(f"network down for Bearer {token}")

    monkeypatch.setattr(agent.model_metadata, "fetch_codex_catalog_entries", broken)
    with caplog.at_level(logging.DEBUG, logger="gateway.lazurio_codex_entitlement"):
        assert subject._catalog(token, "") is None
    assert "OSError" in caplog.text
    assert token not in caplog.text and token.split(".")[1] not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_an_expired_token_tells_no_plan_and_asks_no_catalog(account):
    assert token_expired(_token("free", exp="expired"))
    assert not token_expired(_token("free"))
    assert not token_expired(_token("free", exp=None))
    for plan in ("free", "plus"):
        account["token"] = _token(plan, exp="expired")
        assert entitlement() == {"provider": "openai-codex", "model": TERRA, "plan": None,
                                 "catalog": "not-asked", "verdict": "unknown"}
    assert account["probes"] == []
    # A token without an exp claim is not known to be expired: the plan rule applies.
    account["token"] = _token("free", exp=None)
    assert entitlement()["verdict"] == "no-subscription"


def test_the_dashboard_route_serves_the_answer(account):
    from fastapi.testclient import TestClient

    from hermes_cli import web_server

    account["token"] = _token("free")
    with TestClient(web_server.app, raise_server_exceptions=False) as client:
        client.headers["Authorization"] = f"Bearer {web_server._SESSION_TOKEN}"
        response = client.get("/api/providers/oauth/openai-codex/entitlement")
    assert response.status_code == 200
    assert response.json() == {"provider": "openai-codex", "model": TERRA, "plan": "free",
                               "catalog": "not-asked", "verdict": "no-subscription"}


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
    # Beside upstream's own capabilities and the earlier Lazurio ones, which every reader still finds.
    assert {"session-authority-v1", "durable-admission-v1", "event-replay-v1",
            "lazurio-oauth-cancel-contract-v1", "lazurio-pairing-message-v1",
            CAPABILITY} <= set(descriptor["capabilities"])
    assert CAPABILITY == "lazurio-codex-entitlement-v1"
