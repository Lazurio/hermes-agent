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
  - whether the account's own Codex model catalog lists the model: every row's slug, whatever
    other metadata the row carries, from the catalog endpoint ``agent.model_metadata`` asks;
  - one verdict, decided in this order:
    1. no stored sign-in, another provider, no model, or an access token past its ``exp``
       (the read-only path refreshes nothing, so an expired token cannot tell the current plan):
       ``unknown``;
    2. the plan ``free``: ``no-subscription``, whatever the catalog says;
    3. a catalog that answers without the model (vendor prefix and ``-900k`` alias stripped):
       ``model-missing``;
    4. no catalog: ``unknown``, which blocks nothing;
    5. otherwise ``usable``.
- The credential is read the way status reads it (``read_only=True``), inside
  ``hermes_cli/lazurio_auth_store_reads.py``'s ``reads_only()``. Nothing is refreshed, adopted or
  written, not even the ``auth.json.corrupt`` copy a writer keeps of a store that cannot be parsed:
  such a store reads as no sign-in. The answer never carries the token, the account id or an e-mail
  address, and a failed catalog probe is logged by its exception's type alone, since its message or
  traceback may carry the request's credential.
- ``GET /api/providers/oauth/openai-codex/entitlement`` (``hermes_cli/web_routers/oauth.py``) serves
  it. A ready gateway's ``identify`` lists ``CAPABILITY``
  (``run_runtime.publish_gateway_runtime_ready``), so a client asks only where it gets an answer.

Overlay row ``oauth: whether the signed-in ChatGPT account may run the configured model``
(docs/operations/lazurio-fork-release.md). Removal: when upstream reports a Codex account's plan and
model entitlement itself.
"""
from __future__ import annotations

import logging
import time
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


def token_expired(access_token: Any, now: Optional[float] = None) -> bool:
    """Whether the access token's ``exp`` lies in the past; a token without one is not known to be
    expired."""
    from hermes_cli.auth_constants import _decode_jwt_claims

    exp = _decode_jwt_claims(access_token).get("exp")
    return isinstance(exp, (int, float)) and not isinstance(exp, bool) and exp <= (time.time() if now is None else now)


def catalog_slug(model: str) -> str:
    """The slug the account's catalog lists for *model*: lowercased, without a vendor prefix or
    the picker's ``-900k`` alias."""
    from agent.model_metadata import _bare_codex_slug, strip_codex_context_variant_suffix

    return _bare_codex_slug(strip_codex_context_variant_suffix(model))


def verdict(*, signed_in: bool, provider: str, model: str, plan: Optional[str],
            catalog: Optional[frozenset], expired: bool = False) -> str:
    """The verdict of the rules above; *catalog* is the account's lowercased slugs, or None when it
    was not read or could not be; *expired*: the stored access token is past its ``exp``."""
    if not signed_in or provider != PROVIDER or not model or expired:
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
    from hermes_cli.lazurio_auth_store_reads import reads_only

    try:
        # read_only=True refreshes, adopts and saves nothing; reads_only() also keeps the copy of a
        # store that cannot be parsed off the disk, the pool's global-root fallback included.
        with reads_only():
            credentials = resolve_codex_runtime_credentials(read_only=True)
    except Exception:
        return "", ""
    token = credentials.get("api_key")
    return (token if isinstance(token, str) else ""), str(credentials.get("base_url") or "")


def _catalog(access_token: str, base_url: str) -> Optional[frozenset]:
    """The lowercased slugs of every row the account's own Codex catalog lists, whatever other
    metadata a row carries; None when it could not be read."""
    from agent import model_metadata
    from agent.codex_headers import codex_account_headers

    if not model_metadata._codex_catalog_probe_allowed(access_token, base_url):
        return None
    headers = {"Authorization": f"Bearer {access_token}", **codex_account_headers(access_token)}
    http = model_metadata.model_metadata_http
    try:
        entries, status = model_metadata.fetch_codex_catalog_entries(
            lambda url: http.get(url, headers=headers, timeout=(5, 10), verify=http.resolve_verify()),
            base_url=base_url,
        )
    except Exception as exc:
        # Only the type: the message or the traceback may carry the request's credential.
        logger.debug("The Codex catalog probe for the entitlement failed (%s)", type(exc).__name__)
        return None
    if status != 200:
        return None
    slugs = (entry.get("slug") for entry in entries if isinstance(entry, dict))
    return frozenset(slug.strip().lower() for slug in slugs if isinstance(slug, str) and slug.strip()) or None


def entitlement() -> dict[str, Any]:
    """The answer of ``GET /api/providers/oauth/openai-codex/entitlement`` for the current profile."""
    provider, model = _configured_model()
    token, base_url = _stored_sign_in() if provider == PROVIDER else ("", "")
    expired = bool(token) and token_expired(token)
    # An expired token cannot tell the current plan; nothing here refreshes it.
    plan = plan_of(token) if token and not expired else None
    catalog: Optional[frozenset] = None
    state = "not-asked"
    # A free plan is refused whatever its catalog says, so its catalog is not asked.
    if token and model and not expired and plan not in NO_SUBSCRIPTION_PLANS:
        catalog = _catalog(token, base_url)
        state = "unreachable" if catalog is None else ("listed" if catalog_slug(model) in catalog else "missing")
    return {
        "provider": provider or None,
        "model": model or None,
        "plan": plan,
        "catalog": state,
        "verdict": verdict(signed_in=bool(token), provider=provider, model=model, plan=plan, catalog=catalog,
                           expired=expired),
    }
