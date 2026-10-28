"""Lazurio overlay: a message that arrives while Buddy works steers his running turn by default.

Upstream's gateway ends the running turn for a message that arrives while it runs
(``display.busy_input_mode`` defaults to ``interrupt``), so each message Buddy's person sends him
on Telegram while he works stops that work. The product owner's decision of 2026-10-28 is that such
a message steers by default, for agents, bots and Buddy alike: ``steer`` folds a text into the
running turn (``agent.steer``) and queues what it cannot fold in, a photo or a voice message, as the
next turn, within upstream's queue cap (``_hm_busy_steer`` in ``gateway/run_inbound.py``).

Upstream's Telegram post-start grace window (``HERMES_TELEGRAM_FOLLOWUP_GRACE_SECONDS``, 3 s) holds a
follow-up for the next turn instead of interrupting a turn that has just started. In steer mode a text
steers that turn instead (``grace_holds``); a photo or a voice message keeps the window, and the
queue and interrupt modes keep it whole.

Only the default changes: an explicit ``display.busy_input_mode``, the person's own ``/busy`` among
its writers, and ``HERMES_GATEWAY_BUSY_INPUT_MODE`` still win, ``interrupt`` included.

Overlay row ``gateway: a message that arrives while Buddy works steers his running turn by default``
(docs/operations/lazurio-fork-release.md). Removal: when upstream's default is ``steer``, or when
every Lazurio Buddy's config names its mode.
"""
from __future__ import annotations

# The busy input mode where neither config.yaml nor the environment names one.
BUSY_INPUT_DEFAULT = "steer"


def grace_holds(event, mode: str) -> bool:
    """Whether upstream's Telegram grace window (``_hm_busy_telegram_grace_queue``) may hold *event*
    for the next turn: never a text in steer mode, which steers the running turn instead."""
    from gateway.platforms.event import MessageType
    text_only = (event.message_type == MessageType.TEXT and not getattr(event, "media_urls", None)
                 and not getattr(event, "media_types", None))
    return not (mode == "steer" and text_only)
