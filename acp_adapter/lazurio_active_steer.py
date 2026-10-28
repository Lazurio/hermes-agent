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

Here the viewer answers the ACP extension request ``_lazurio/steer`` (``{"sessionId", "text"}``):

- it steers this editor's running admission as ``/steer`` does (``prompt.receipt``, then
  ``session.steer`` for that admission's generation) and answers ``{"steered": true}``;
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

from typing import Any

# What ``initialize`` adds to the agent capabilities' ``_meta``.
ACTIVE_STEER_META = {"lazurio.activeSteer": 1}
# The extension request (ACP sends it as ``_lazurio/steer``; the SDK strips the underscore).
STEER_METHOD = "lazurio/steer"


class LazurioActiveSteerMixin:
    """Mixed into ``GatewayACPAgent`` (``acp_adapter/gateway_server.py``): its ``ext_method``."""

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        from acp.exceptions import RequestError

        if method != STEER_METHOD:
            raise RequestError.method_not_found(f"_{method}")
        session_id = params.get("sessionId") if isinstance(params, dict) else None
        text = params.get("text") if isinstance(params, dict) else None
        if not isinstance(session_id, str) or not isinstance(text, str) or not text.strip():
            raise RequestError.invalid_params({"reason": "sessionId and a non-empty text are required"})
        session_id = self._aliases.get(session_id, session_id)
        if await steer_running_turn(self, session_id, text):
            return {"steered": True}
        return {"steered": False, "reason": "not_running"}


async def steer_running_turn(agent, session_id: str, text: str) -> bool:
    """Steer this editor's running admission of *session_id*; False where none runs or it ended
    meanwhile. Any other refusal of the owner is raised."""
    from hermes_cli.gateway_client import GatewayRPCError

    admission_id = agent._admissions.get(session_id)
    if admission_id is None:
        return False
    client = await agent._client()
    receipt = await client.rpc("prompt.receipt", session_id=session_id, admission_id=admission_id)
    if receipt.get("status") != "started":
        return False
    try:
        result = await client.rpc("session.steer", session_id=session_id, text=text,
                                  execution_generation=receipt["execution_generation"])
    except GatewayRPCError as exc:
        # Only a turn that ended meanwhile leaves the message for the next prompt.
        if str(exc) == "stale_generation":
            return False
        raise
    # A bare stop phrase that ended a LOCAL voice chat was consumed: sending it on as a prompt would
    # admit "stop" as a turn (main's ``_steer``).
    return result.get("status") == "queued" or result.get("voice_stopped") is True
