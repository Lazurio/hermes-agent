"""Lazurio overlay: the sign-in contract a gateway names in its control socket's ``identify``.

Lazurio's Launchpad signs Buddy in to ChatGPT through the OpenAI Codex sign-in of the gateway's
dashboard API (LazurioPlatform#635). It ends a sign-in only on Hermes' word: DELETE answering
``{"ok": true}`` or ``{"ok": false, "message": "session not found"}``, or the poll's 404. A gateway
whose ready ``identify`` lists ``CAPABILITY`` guarantees that once a sign-in has answered "not found",
no worker of it saves credentials any more, however long the worker waits before its save:

- Whatever removes a running sign-in from ``_oauth_sessions`` (the DELETE route, the 15-minute sweep, a
  start that failed) marks it ``cancelled`` under ``_oauth_sessions_lock``, and every poller checks that
  flag and saves in one critical section under that lock (``hermes_cli/web_routers/oauth.py``).
- After this process releases its runtime lock, a successor answers "not found" for every sign-in of
  this one, while its daemon pollers run on until the process exits. So the release
  (``gateway.status.release_gateway_runtime_lock``, which the stop, an early exit, the exit backstop,
  the shutdown watchdog and atexit all go through) closes the reservations through
  :func:`close_process_ownership`, which first runs :func:`end_sign_ins`: every sign-in still
  pending is marked ``cancelled`` under the same lock. A save that holds that lock past
  ``SAVE_WAIT_SECONDS`` can no longer be cancelled, so the runtime lock stays and process exit
  releases it, as ``ProfileOwnership.close`` keeps it for a writer still alive. On the normal stop
  no sign-in can start after the release, because the stop closes the API listener earlier
  (``settle_gateway_runtime``). A stop that wedges before that point, so that the shutdown watchdog
  releases, has answered ``identify`` as ``draining`` since its first step, and a client that
  honours that starts no sign-in there.

Not covered: a secondary profile that a multiplexing gateway stops serving while it runs on
(``run_runtime.release_profile_home``, a deleted or parked profile).

The capability is listed only while ``ready`` (``run_runtime.publish_gateway_runtime_ready``); a
draining gateway clears its capabilities, as it does every other one.

Overlay row ``oauth: identify names the sign-in cancel contract, and a stopping gateway keeps it``
(docs/operations/lazurio-fork-release.md), on top of the row ``oauth: a sign-in the 15-minute sweep
removes can no longer save its tokens``. Removal: when upstream ends a stopping gateway's sign-ins
the same way and names the guarantee in ``identify``, and Lazurio's Launchpad reads that name.
"""
from __future__ import annotations

import logging
import sys

logger = logging.getLogger(__name__)

CAPABILITY = "lazurio-oauth-cancel-contract-v1"

# How long the release waits for a save holding the sessions lock. A save that finishes in time is
# done before the release; past it the runtime lock stays held until process exit. The guarantee
# does not depend on this bound, only how soon a successor can start does.
SAVE_WAIT_SECONDS = 1.0


def end_sign_ins() -> bool:
    """Mark every dashboard sign-in of this process still pending ``cancelled``, under the lock each
    poller's final check-and-save holds. True when none of them can save any more, so the runtime
    lock may go; False when a save still held that lock after ``SAVE_WAIT_SECONDS`` or marking failed,
    so the caller keeps the runtime lock until process exit. Never raises."""
    state = sys.modules.get("hermes_cli.web_server_oauth")
    if state is None:
        return True  # the dashboard's sign-in state was never loaded here, so no sign-in exists
    try:
        if not state._oauth_sessions_lock.acquire(timeout=SAVE_WAIT_SECONDS):
            logger.warning("A dashboard sign-in is still saving; the gateway runtime lock stays held "
                           "until this process exits")
            return False
        try:
            for sess in state._oauth_sessions.values():
                # As the sweep does: a finished sign-in keeps its status.
                if sess.get("status") == "pending":
                    sess["cancelled"] = True
        finally:
            state._oauth_sessions_lock.release()
    except Exception:
        logger.warning("Dashboard sign-ins not ended; the gateway runtime lock stays held until this "
                       "process exits", exc_info=True)
        return False
    return True


def close_process_ownership() -> None:
    """``process_ownership.close()`` for ``gateway.status.release_gateway_runtime_lock``, once no
    dashboard sign-in of this process can save any more (:func:`end_sign_ins`). While a save still
    runs, the reservations stay until process exit, as ``ProfileOwnership.close`` keeps them for a
    writer still alive."""
    from gateway.runtime_ownership import process_ownership
    if end_sign_ins():
        process_ownership.close()
