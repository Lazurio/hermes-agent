"""Lazurio overlay: a message that arrives while Buddy works steers his running turn by default.

Upstream's gateway ends the running turn for a message that arrives while it runs
(``display.busy_input_mode`` defaults to ``interrupt``), so each message Buddy's person sends him
on Telegram while he works stops that work. The product owner's decision of 2026-10-28 is that such
a message steers by default, for agents, bots and Buddy alike: ``steer`` folds a text into the
running turn (``agent.steer``) and queues what it cannot fold in, a photo or a voice message, as the
next turn (``_hm_busy_steer`` in ``gateway/run_inbound.py``).

Only the default changes: an explicit ``display.busy_input_mode``, the person's own ``/busy`` among
its writers, and ``HERMES_GATEWAY_BUSY_INPUT_MODE`` still win, ``interrupt`` included.

Overlay row ``gateway: a message that arrives while Buddy works steers his running turn by default``
(docs/operations/lazurio-fork-release.md). Removal: when upstream's default is ``steer``, or when
every Lazurio Buddy's config names its mode.
"""
from __future__ import annotations

# The busy input mode where neither config.yaml nor the environment names one.
BUSY_INPUT_DEFAULT = "steer"
