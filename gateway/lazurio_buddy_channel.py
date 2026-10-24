"""Lazurio overlay: a turn's commands see the ``LAZURIO_BUDDY_CHANNEL`` of the ACP viewer that submitted it.

Buddy's chat in Lazurio is a Lazurio T3 Code instance whose ACP provider runs ``hermes acp`` with
``LAZURIO_BUDDY_CHANNEL=t3``. Lazurio's Delegace (``delegace zadej``, a command Buddy runs from his
terminal tool) reads that variable to report a bot's result back into the chat instead of Telegram.
Since v0.22.4 ``hermes acp`` only views a session of the gateway, which runs the turn and every tool
in its own process with its own environment, so the variable never reached those commands. A
session's source cannot stand in for it: every ACP viewer's session has the source ``acp``.

Here the provenance is bound to the viewer that submitted the turn:

* the viewer (``acp_adapter/gateway_server.py``) hands its own ``LAZURIO_BUDDY_CHANNEL``, when it is a
  short token, to the owner with every ``prompt.submit`` it sends (``lazurio_buddy_channel``), only
  to an owner that advertises ``CAPABILITY``, so a new viewer never fails against an older owner;
* the owner commits it with that admission (``lazurio_buddy_channel_v1``), so a queued drain or a
  retry runs exactly what was admitted, and while the admission executes the turn it opens binds
  it as the session-context variable ``LAZURIO_BUDDY_CHANNEL``
  (``gateway.session_context._LAZURIO_BUDDY_CHANNEL``, in ``_VAR_MAP``);
* the env bridge (``tools/environments/local._inject_session_context_env``) exports it to every
  command that turn runs, and the subagents and background commands it starts carry it as they
  carry ``HERMES_SESSION_*``. Every other binding (a messaging platform's turn, another viewer's
  turn, cron, Kanban) leaves it unset, so the bridge strips it, also when the gateway's own
  environment has it, and the shared bash snapshot never keeps it
  (``tools/environments/base_session_env``).

Overlay row ``acp: a turn's commands see the LAZURIO_BUDDY_CHANNEL of the ACP viewer that submitted
it`` (docs/operations/lazurio-fork-release.md). Lazurio-only. Remove when Lazurio's Delegace no
longer reads ``LAZURIO_BUDDY_CHANNEL`` from its environment.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import logging
import os
import re

logger = logging.getLogger(__name__)

ENV = "LAZURIO_BUDDY_CHANNEL"
CAPABILITY = "lazurio-acp-buddy-channel-v1"
SUBMIT_KEY = "lazurio_buddy_channel"
PAYLOAD_KEY = "lazurio_buddy_channel_v1"
_TOKEN = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")

# (admission id, channel) of the admission executing in this context (``admission_scope``).
_executing: ContextVar[tuple[str, str] | None] = ContextVar("lazurio_buddy_channel_admission", default=None)


def channel(value) -> str | None:
    """``value`` when it is a channel token (``t3``), else None."""
    return value if isinstance(value, str) and _TOKEN.fullmatch(value) else None


def viewer_fields(descriptor, environ=None) -> dict:
    """Viewer side, once per gateway connection: the ``prompt.submit`` field that hands this
    process's ``LAZURIO_BUDDY_CHANNEL`` to the owner, or ``{}`` without the variable, for a value
    that is not a channel token, and for an owner whose ``runtime.describe`` does not advertise
    ``CAPABILITY`` (one from before this overlay refuses the unknown field)."""
    value = (os.environ if environ is None else environ).get(ENV)
    if not value:
        return {}
    if channel(value) is None:
        logger.warning("%s=%r is not a channel token; this viewer's turns carry no channel", ENV, value[:64])
        return {}
    if CAPABILITY not in ((descriptor or {}).get("capabilities") or ()):
        logger.info("The gateway does not take the viewer's %s; this viewer's turns carry no channel", ENV)
        return {}
    return {SUBMIT_KEY: value}


def submit_fields(params) -> dict:
    """``AuthorityConnection.submission``: the raw wire field of a ``prompt.submit``, forwarded to admission."""
    return {SUBMIT_KEY: params[SUBMIT_KEY]} if SUBMIT_KEY in params else {}


def admit(params) -> dict:
    """``SessionAuthority._submit``: the wire field -> the committed ``{PAYLOAD_KEY: channel}`` (``{}``
    without one); a value that is not a channel token is refused."""
    if params.get(SUBMIT_KEY) is None:
        return {}
    value = channel(params[SUBMIT_KEY])
    if value is None:
        from hermes_state_runtime import RuntimeStoreError
        raise RuntimeStoreError("invalid_params")
    return {PAYLOAD_KEY: value}


@contextmanager
def admission_scope(admission_id, committed):
    """``execute_finite_admission``: the committed channel of the admission executing in this block."""
    value = channel(committed)
    token = _executing.set((str(admission_id), value) if value is not None and admission_id else None)
    try:
        yield
    finally:
        _executing.reset(token)


def bind_turn(event) -> None:
    """``_hmwa_prepare_turn``, right after the turn's session variables are bound, which leave the
    channel unset: the executing admission's channel, for the turn that admission opens (its event
    carries the admission id). A turn another task opens from this context, a notification or a
    goal continuation, has another id and keeps it unset."""
    executing = _executing.get()
    message_id = getattr(event, "message_id", None)
    if executing is None or message_id is None or str(message_id) != executing[0]:
        return
    from gateway.session_context import _LAZURIO_BUDDY_CHANNEL
    _LAZURIO_BUDDY_CHANNEL.set(executing[1])
