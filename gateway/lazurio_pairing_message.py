"""Lazurio overlay: a configured text replaces the DM that gives an unknown sender its pairing code.

A person connects their own Telegram bot to their personal Hermes in Lazurio (LazurioPlatform
decision F66) and presses Start. The first thing their bot says is upstream's
``gateway.pairing.code_reply``: it tells them to open a terminal and run ``hermes pairing approve
<platform> <code>``. Lazurio approves the request from its own page instead, so it sets the text of
this one reply, in the person's language and in the agent's own words:

- ``unauthorized_dm_pairing_message`` in config.yaml, at the top level or in the ``gateway:`` section,
  read in that order as upstream reads its sibling ``unauthorized_dm_decline_message``
  (``gateway/config_loader.py``): the reply's text. ``{code}`` and ``{validity}`` become the pairing
  code and how long it holds (upstream's words for it); any other brace stays as written. Empty,
  absent or not text keeps upstream's reply.
- It is read when the reply is sent (``load_config_readonly``, which rereads the file once it
  changed), so a change applies to the next Start without a restart. The read follows the profile
  scope the gateway binds around inbound handling (``_make_profile_message_handler``), where upstream's
  reply reads its language too, so a multiplexed profile's sender gets that profile's text.
- The request is recorded exactly as upstream records it, and the reply to a sender who is
  rate-limited stays upstream's: only the text of the code's reply changes.
- A ready gateway's ``identify`` lists ``CAPABILITY`` (``run_runtime.publish_gateway_runtime_ready``),
  so a client sets the key only where it is honoured.

Overlay row ``pairing: a configured text replaces the reply that gives an unknown sender its code``
(docs/operations/lazurio-fork-release.md). Removal: when upstream reads a configurable text for this
reply and Lazurio's Launchpad writes upstream's key.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

CAPABILITY = "lazurio-pairing-message-v1"
CONFIG_KEY = "unauthorized_dm_pairing_message"


def configured_text(config: Any) -> str:
    """The configured text in a loaded config.yaml: the top-level key first, then the ``gateway:``
    section's; ``""`` where neither holds text."""
    if not isinstance(config, dict):
        return ""
    section = config.get("gateway")
    for source in (config, section if isinstance(section, dict) else {}):
        value = source.get(CONFIG_KEY)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def pairing_reply(code: str) -> Optional[str]:
    """The configured reply for ``code``, or None to send upstream's. Never raises: a config that
    cannot be read sends upstream's reply."""
    try:
        from hermes_cli.config import load_config_readonly

        text = configured_text(load_config_readonly())
        if not text:
            return None
        from agent.i18n import t
        from gateway.pairing import CODE_TTL_SECONDS

        hours = max(1, CODE_TTL_SECONDS // 3600)
        validity = t("gateway.pairing.validity_hour" if hours == 1 else "gateway.pairing.validity_hours", hours=hours)
        return text.replace("{code}", code).replace("{validity}", validity)
    except Exception:
        logger.warning("Could not read the configured pairing reply; upstream's is sent", exc_info=True)
        return None
