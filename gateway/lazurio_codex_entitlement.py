"""Lazurio overlay: whether the signed-in ChatGPT account may run the configured Codex model.

A person signs their Buddy in with their own ChatGPT account in Lazurio (LazurioPlatform decision
F63). A free account, or one whose plan lacks the configured model, signs in without a problem and
then fails every turn with upstream's raw HTTP 400 ("The '<model>' model is not supported when using
Codex with a ChatGPT account."), which never names the plan (Lazurio/LazurioPlatform#694). Lazurio
requires a subscription (the product owner's decision of 2026-10-28) and asks right after the
sign-in:

- ``entitlement()`` answers for ``model.default`` under ``model.provider`` ``openai-codex``:
  - the account's plan: ``chatgpt_plan_type`` in the OAuth access token's
    ``https://api.openai.com/auth`` claim, read with no network call;
  - whether the account's own Codex model catalog lists the model: the probe of
    ``agent.model_metadata``, cached per token for an hour;
  - one verdict, decided in this order:
    1. no stored sign-in, another provider or no model: ``unknown``;
    2. the plan ``free``: ``no-subscription``, whatever the catalog says;
    3. a catalog that answers without the model (vendor prefix and ``-900k`` alias stripped):
       ``model-missing``;
    4. no catalog: ``unknown``, which blocks nothing;
    5. otherwise ``usable``.
- The credential is read the way status reads it (``read_only=True``). Nothing is refreshed,
  adopted or written. The answer never carries the token, the account id or an e-mail address.
- ``GET /api/providers/oauth/openai-codex/entitlement`` (``hermes_cli/web_routers/oauth.py``) serves
  it. A ready gateway's ``identify`` lists ``CAPABILITY``
  (``run_runtime.publish_gateway_runtime_ready``), so a client asks only where it gets an answer.

Overlay row ``oauth: whether the signed-in ChatGPT account may run the configured model``
(docs/operations/lazurio-fork-release.md). Removal: when upstream reports a Codex account's plan and
model entitlement itself.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

CAPABILITY = "lazurio-codex-entitlement-v1"
PROVIDER = "openai-codex"
AUTH_CLAIM = "https://api.openai.com/auth"
# The plans that never run a Buddy, whatever their catalog lists (the decision of 2026-10-28).
NO_SUBSCRIPTION_PLANS = frozenset({"free"})


def plan_of(access_token: Any) -> Optional[str]:
    """The account's plan from the access token's claim, lowercased; None where it names none."""
    from hermes_cli.auth_constants import _decode_jwt_claims

    auth = _decode_jwt_claims(access_token).get(AUTH_CLAIM)
    plan = auth.get("chatgpt_plan_type") if isinstance(auth, dict) else None
    return plan.strip().lower() if isinstance(plan, str) and plan.strip() else None


def catalog_slug(model: str) -> str:
    """The slug the account's catalog lists for *model*: lowercased, without a vendor prefix or
    the picker's ``-900k`` alias."""
    from agent.model_metadata import _bare_codex_slug, strip_codex_context_variant_suffix

    return _bare_codex_slug(strip_codex_context_variant_suffix(model))


def verdict(*, signed_in: bool, provider: str, model: str, plan: Optional[str],
            catalog: Optional[frozenset]) -> str:
    """The verdict of the rules above; *catalog* is the account's lowercased slugs, or None when it
    was not read or could not be."""
    if not signed_in or provider != PROVIDER or not model:
        return "unknown"
    if plan in NO_SUBSCRIPTION_PLANS:
        return "no-subscription"
    if catalog is None:
        return "unknown"
    return "usable" if catalog_slug(model) in catalog else "model-missing"


def _configured_model() -> tuple[str, str]:
    """(provider, model) of this profile's ``model`` section; empty where config.yaml could not be
    read or parsed, since the loader's last-known-good settings may name another model."""
    from hermes_cli.config import load_config_readonly
    from hermes_cli.config_read_errors import FailedConfigRead

    config = load_config_readonly()
    if isinstance(config, FailedConfigRead) or not isinstance(config, dict):
        return "", ""
    section = config.get("model")
    if isinstance(section, dict):
        model, provider = section.get("default") or section.get("name") or "", section.get("provider") or ""
    else:
        model, provider = (section if isinstance(section, str) else ""), ""
    return str(provider).strip().lower(), str(model).strip()


def _stored_sign_in() -> tuple[str, str]:
    """(access token, base URL) of the stored Codex sign-in, read-only; empty when there is none."""
    from hermes_cli.auth_codex import resolve_codex_runtime_credentials

    try:
        credentials = resolve_codex_runtime_credentials(read_only=True)
    except Exception:
        return "", ""
    token = credentials.get("api_key")
    return (token if isinstance(token, str) else ""), str(credentials.get("base_url") or "")


def _catalog(access_token: str, base_url: str) -> Optional[frozenset]:
    """The lowercased slugs of the account's own Codex catalog; None when it could not be read."""
    from agent.model_metadata import _fetch_codex_oauth_context_lengths_with_source

    try:
        lengths, _fresh = _fetch_codex_oauth_context_lengths_with_source(access_token, base_url=base_url)
    except Exception:
        logger.debug("The Codex catalog probe for the entitlement failed", exc_info=True)
        return None
    return frozenset(slug.lower() for slug in lengths) or None


def entitlement() -> dict[str, Any]:
    """The answer of ``GET /api/providers/oauth/openai-codex/entitlement`` for the current profile."""
    provider, model = _configured_model()
    token, base_url = _stored_sign_in() if provider == PROVIDER else ("", "")
    plan = plan_of(token) if token else None
    catalog: Optional[frozenset] = None
    state = "not-asked"
    # A free plan is refused whatever its catalog says, so its catalog is not asked.
    if token and model and plan not in NO_SUBSCRIPTION_PLANS:
        catalog = _catalog(token, base_url)
        state = "unreachable" if catalog is None else ("listed" if catalog_slug(model) in catalog else "missing")
    return {
        "provider": provider or None,
        "model": model or None,
        "plan": plan,
        "catalog": state,
        "verdict": verdict(signed_in=bool(token), provider=provider, model=model, plan=plan, catalog=catalog),
    }
