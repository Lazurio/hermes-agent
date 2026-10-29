"""Lazurio overlay: a keyed ``session.steer`` is applied once.

The Lazurio ACP viewer steers Buddy's running turn through this owner's ``session.steer``
(acp_adapter/lazurio_active_steer.py). Its call can lose the answer after the owner applied the
steer, for example when the connection drops mid-call; asked again, the owner would append the same
guidance to the turn twice (review of Lazurio/t3code#141).

A ``session.steer`` that carries ``idempotency_key`` is now applied once per session, execution
generation and key:

- an applied steer (``queued`` or ``redirected``) is kept as its receipt, the newest 256 at most;
- the same key for the same generation answers that receipt and applies nothing, even after the
  turn ended, so a retry never reads ``stale_generation`` for a steer the turn already took;
- a refusal is not kept: nothing was applied, and asked again the call goes on as upstream's.

A call without the key is upstream's. ``runtime.describe`` lists ``CAPABILITY``, so a viewer sends
the key only to an owner that takes it.

Overlay row ``acp: a message sent while Buddy works steers his running turn, never stops it``
(docs/operations/lazurio-fork-release.md). Removal: when upstream's ``session.steer`` takes an
idempotency key.
"""
from __future__ import annotations

from collections import OrderedDict
from typing import Any

CAPABILITY = "lazurio-steer-receipts-v1"
KEY = "idempotency_key"
_LIMIT = 256
_APPLIED = frozenset({"queued", "redirected"})


def _receipts(authority) -> OrderedDict:
    return authority.__dict__.setdefault("_lazurio_steer_receipts", OrderedDict())


def _key(params: dict[str, Any]) -> str | None:
    return params.get(KEY)


def valid(params: dict[str, Any]) -> bool:
    """Whether a ``session.steer``'s key, when it has one, is usable."""
    key = _key(params)
    return key is None or (isinstance(key, str) and bool(key.strip()))


def replay(authority, session_id: str, params: dict[str, Any]) -> dict[str, Any] | None:
    """The receipt of the keyed steer this call repeats, or None to go on as upstream's."""
    key = _key(params)
    if key is None:
        return None
    return _receipts(authority).get((session_id, params["execution_generation"], key))


def remember(authority, session_id: str, params: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
    """Keep an applied keyed steer's *response* as its receipt; answer *response*."""
    key = _key(params)
    if key is not None and response.get("status") in _APPLIED:
        receipts = _receipts(authority)
        receipts[(session_id, params["execution_generation"], key)] = response
        while len(receipts) > _LIMIT:
            receipts.popitem(last=False)
    return response
