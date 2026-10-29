"""Lazurio overlay: a message sent while Buddy works steers his running turn and never stops it.

Buddy's chat in Lazurio T3 Code sends a message that arrives while Buddy works as a steer (the
Admin's decision of 2026-10-22). ACP has no steering request, so T3 Code steered every ACP agent by
cancelling the running turn and sending the message as the next prompt: each such message stopped
Buddy's work. The product owner's requirement of 2026-10-28 (Lazurio/t3code#138) is that it never
does.

Hermes steers natively: the owner's ``session.steer`` appends the guidance after the running turn's
current tool batch, and the turn goes on. Main's viewer reaches it only as the prompt
``/steer <text>``, which streams a confirmation into Buddy's answer and, where no turn of this
editor runs, falls through into a plain prompt: a new turn the client would take for a steer.

Here the viewer answers the ACP extension request ``_lazurio/steer``
(``{"sessionId", "turnId", "messageId", "text"}``):

- it steers this editor's running admission as ``/steer`` does (``prompt.receipt``, then
  ``session.steer`` for that admission's generation) and answers ``{"steered": true}``;
- only the turn it names: a prompt carries its client turn's id in ``_meta`` (``lazurio.turnId``),
  and a steer whose ``turnId`` is not the one of this editor's latest prompt is ``not_running``, so a
  turn that ended while the steer was on its way never passes the message to the next one;
- once per message: a ``messageId`` already steered into the turn it names, or being steered,
  answers as that steer did, without a second ``session.steer``, even after that turn ended and the
  next one started (a retry after a lost answer); the viewer keeps the newest 256 per session;
- once at the owner too: an owner that lists ``lazurio-steer-receipts-v1`` gets the steer with an
  ``idempotency_key`` (gateway/lazurio_steer_receipts.py), so a retry after the owner's answer was
  lost is never applied twice, and a turn that ended answers its receipt instead of
  ``stale_generation``;
- where no prompt of this editor runs, or its turn ended meanwhile (``stale_generation``), it answers
  ``{"steered": false, "reason": "not_running"}`` and starts nothing: the client sends the message as
  the next prompt;
- any other refusal of the owner is the request's error;
- it never streams text and never starts a turn.

``initialize`` lists ``lazurio.activeSteer`` (``1``) in the agent capabilities' ``_meta``, so a
client steers this way only where the viewer answers it.

Overlay row ``acp: a message sent while Buddy works steers his running turn, never stops it``
(docs/operations/lazurio-fork-release.md). Removal: when ACP or upstream's viewer offers a steering
request a client can use instead.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import Any

# What ``initialize`` adds to the agent capabilities' ``_meta``.
ACTIVE_STEER_META = {"lazurio.activeSteer": 1}
# The extension request (ACP sends it as ``_lazurio/steer``; the SDK strips the underscore).
STEER_METHOD = "lazurio/steer"
# The ``_meta`` key of ``session/prompt`` that names the client's turn (the SDK passes it as a keyword).
TURN_META = "lazurio.turnId"
NOT_RUNNING = {"steered": False, "reason": "not_running"}
# Steers kept per session, newest last: a retry finds its answer after its turn ended.
_STEERS = 256


class LazurioActiveSteerMixin:
    """Mixed into ``GatewayACPAgent`` (``acp_adapter/gateway_server.py``): the viewer's ``prompt``
    hands it the turn a prompt names, and its ``ext_method`` answers the steer."""

    def _lazurio_note_turn(self, session_id: str, kwargs: dict[str, Any]) -> None:
        """Take the client's turn id out of a ``session/prompt``'s keywords and keep it as this
        editor's latest turn of *session_id*; a prompt that names none is never steered."""
        turn_id = kwargs.pop(TURN_META, None)
        self.__dict__.setdefault("_lazurio_turns", {})[self._aliases.get(session_id, session_id)] = (
            turn_id if isinstance(turn_id, str) else None)

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        from acp.exceptions import RequestError

        if method != STEER_METHOD:
            raise RequestError.method_not_found(f"_{method}")
        fields = params if isinstance(params, dict) else {}
        session_id, turn_id, message_id, text = (fields.get(key) for key in ("sessionId", "turnId", "messageId", "text"))
        if not all(isinstance(value, str) and value.strip() for value in (session_id, turn_id, message_id, text)):
            raise RequestError.invalid_params(
                {"reason": "sessionId, turnId, messageId and a non-empty text are required"})
        session_id = self._aliases.get(session_id, session_id)
        # One steer per turn and message: a repeat waits for the first and answers as it did,
        # before the turn is checked, so a turn that ended still answers its own steer.
        steers = self.__dict__.setdefault("_lazurio_steers", {}).setdefault(session_id, OrderedDict())
        key = (turn_id, message_id)
        pending = steers.get(key)
        if pending is None:
            if self.__dict__.get("_lazurio_turns", {}).get(session_id) != turn_id:
                return NOT_RUNNING
            admission_id = self._admissions.get(session_id)
            if admission_id is None:
                return NOT_RUNNING
            pending = steers[key] = asyncio.ensure_future(
                steer_running_turn(self, session_id, admission_id, text, f"{turn_id}:{message_id}"))
            while len(steers) > _STEERS:
                steers.popitem(last=False)
        try:
            steered = await asyncio.shield(pending)
        except BaseException:
            if pending.done() and steers.get(key) is pending:
                del steers[key]  # asked again, it goes to the owner under the same key
            raise
        if not steered and steers.get(key) is pending:
            del steers[key]
        return {"steered": True} if steered else NOT_RUNNING


async def steer_running_turn(agent, session_id: str, admission_id: str, text: str, key: str) -> bool:
    """Steer this editor's running admission *admission_id* of *session_id*; False where it no longer
    runs or ended meanwhile. Any other refusal of the owner is raised. An owner that keeps receipts
    gets *key*, and answers a steer it already applied even after the turn ended."""
    from gateway.lazurio_steer_receipts import CAPABILITY, KEY
    from hermes_cli.gateway_client import GatewayRPCError

    client = await agent._client()
    receipt = await client.rpc("prompt.receipt", session_id=session_id, admission_id=admission_id)
    keyed = {KEY: key} if CAPABILITY in getattr(agent, "_lazurio_owner_capabilities", ()) else {}
    generation = receipt.get("execution_generation")
    # Only a started turn takes a steer; one that ended may still hold the receipt of this key's steer.
    if receipt.get("status") != "started" and not (keyed and receipt.get("status") == "terminal"
                                                      and isinstance(generation, int)):
        return False
    try:
        result = await client.rpc("session.steer", session_id=session_id, text=text,
                                  execution_generation=generation, **keyed)
    except GatewayRPCError as exc:
        # Only a turn that ended meanwhile leaves the message for the next prompt.
        if str(exc) == "stale_generation":
            return False
        raise
    # A bare stop phrase that ended a LOCAL voice chat was consumed: sending it on as a prompt would
    # admit "stop" as a turn (main's ``_steer``).
    return result.get("status") == "queued" or result.get("voice_stopped") is True
