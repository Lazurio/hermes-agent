"""Lazurio overlay: whether the signed-in ChatGPT account may run the configured Codex model.

A free account, or one whose catalog lacks the model, signs in and then fails every turn with
upstream's raw 400 (Lazurio/LazurioPlatform#694). ``GET /api/providers/oauth/openai-codex/entitlement``
answers right after the sign-in from the token's plan claim and the account's own catalog, and a ready
``identify`` names the capability. Overlay row ``oauth: whether the signed-in ChatGPT account may run
the configured model`` (docs/operations/lazurio-fork-release.md).
"""
import base64
import json
from types import SimpleNamespace

import pytest

import gateway.lazurio_codex_entitlement as subject
from gateway.lazurio_codex_entitlement import CAPABILITY, catalog_slug, entitlement, plan_of, verdict
from hermes_constants import get_hermes_home

TERRA = "gpt-6.1-terra"
CATALOG = frozenset({"gpt-6.1-terra", "gpt-6.1-sol", "gpt-6-luna"})
ACCOUNT_ID = "acct-synthetic-0001"
EMAIL = "person@example.invalid"


def _jwt(claims: dict) -> str:
    def part(value: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    return f"{part({'alg': 'none'})}.{part(claims)}.signature"


def _token(plan=None) -> str:
    """A synthetic access token in the shape ChatGPT's OAuth issues: the plan sits in the auth claim."""
    auth = {"chatgpt_account_id": ACCOUNT_ID}
    if plan is not None:
        auth["chatgpt_plan_type"] = plan
    return _jwt({"https://api.openai.com/auth": auth, "https://api.openai.com/profile": {"email": EMAIL}})


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


def test_the_catalog_is_the_accounts_lowercased_slugs_or_nothing(monkeypatch):
    import agent.model_metadata

    answers = iter([({"GPT-6.1-Terra": 500_000, "gpt-6.1-sol": 500_000}, True), ({}, False)])
    monkeypatch.setattr(agent.model_metadata, "_fetch_codex_oauth_context_lengths_with_source",
                        lambda token, base_url="": next(answers))
    assert subject._catalog("token", "") == frozenset({"gpt-6.1-terra", "gpt-6.1-sol"})
    assert subject._catalog("token", "") is None

    def broken(token, base_url=""):
        raise OSError("network down")

    monkeypatch.setattr(agent.model_metadata, "_fetch_codex_oauth_context_lengths_with_source", broken)
    assert subject._catalog("token", "") is None


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
