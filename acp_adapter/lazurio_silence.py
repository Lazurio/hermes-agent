"""Lazurio overlay: a reply of intentional silence (NO_REPLY, [SILENT]) sends nothing to the editor.

Buddy answers a notice that tells the person nothing new (a helper's result he has already reported)
with intentional silence: a reply that is exactly a silence marker. On a messaging chat the gateway
delivers nothing for it (``gateway/response_filters.py``): its stream consumer holds a streamed reply
back while it may still become a marker (``is_partial_silence_marker``) and retracts one that did,
and the whole-response rule (``is_intentional_silence_response``) sends no text. Upstream's gateway
viewer, the path ``hermes acp`` takes, does neither. The owner streams a local viewer's reply as it
is generated (``message.delta``) and the viewer passes every delta on as an ``agent_message_chunk``,
so Buddy's chat in Lazurio T3 Code showed the literal ``NO_REPLY``. Then came the owner's settled
text: nothing for a notice the gateway itself injected, but for an editor's prompt, which the owner
cannot tell from a person's message (T3 Code posts Buddy's notices as prompts in his thread), the
gateway's notice that the model returned only a silence marker. A reply settled from a receipt (a
retry, a replay gap) carried the raw marker.

Here the viewer applies the gateway's rules with the gateway's predicates. A reply segment's deltas
are held while the segment so far may still be a bare marker and go out at once, in order and under
the segment's ``messageId``, as soon as it cannot. A segment that ends at a tool call or interim
commentary is commentary, never the final reply, so what it held goes out before that boundary, as
the gateway's stream consumer flushes at a segment break. At the turn's end a held segment that is a
bare marker is dropped and any other held text is shown. A reply that streamed as a bare marker, or
whose final text is one, is intentional silence: on a turn that did not fail the editor gets no
``agent_message_chunk`` for it, neither the marker nor the owner's notice in its place, and the turn
ends as it would have (its stop reason, the owner's transcript and history unchanged). Thought
chunks, tool calls and every other update pass untouched; prose that mentions ``NO_REPLY`` is no
marker and is delivered.

Overlay row ``acp: a reply of intentional silence (NO_REPLY, [SILENT]) sends nothing to the client``
(docs/operations/lazurio-fork-release.md). Removal: when upstream's ACP path (the gateway viewer, or
the owner's stream to local viewers) sends nothing for a reply of intentional silence, to an editor's
prompt too.
"""
from __future__ import annotations

from gateway.response_filters import is_intentional_silence_response, is_partial_silence_marker


def _may_become_silence(text: str) -> bool:
    """Whether a reply segment that starts with *text* may still turn out to be a bare marker.

    The gateway's prefix rule, and text with nothing in it yet that rules a marker out (leading
    whitespace, the ``*`` of ``*NO_REPLY*``): the gateway retracts a streamed preview, but an editor
    cannot take back a chunk."""
    return is_partial_silence_marker(text) or not any(ch.isalnum() for ch in text)


class LazurioSilenceMixin:
    """Mixed into ``GatewayACPAgent`` (``acp_adapter/gateway_server.py``), whose ``message.delta``
    projection, segment boundaries, replay-gap cut (``_recover``) and ``_teardown`` call the hooks
    below, as does ``GatewayReplyMixin._finish_reply``. Like ``_streamed``, which keeps meaning what
    the editor was shown, the held text is per admission."""

    def _lazurio_held(self) -> dict:
        # admission -> the start of its current reply segment, received but not shown yet
        return self.__dict__.setdefault("_lazurio_silence_held", {})

    def _lazurio_unheld(self, aid, text):
        """The part of a reply delta the editor sees now: ``""`` while the segment so far may still be
        a bare marker, then everything held with it, in order."""
        held = self._lazurio_held()
        if aid not in held and self._streamed.get(aid):
            return text  # the segment already shows text, so it is no bare marker
        segment = held.pop(aid, "") + text
        if segment and _may_become_silence(segment):
            held[aid] = segment
            return ""
        return segment

    async def _lazurio_show_held(self, kind, sid, editor_id, aid):
        """Before a segment boundary (a tool call, interim commentary): the segment that ends there is
        commentary, so the editor sees what it held now, under the segment's ``messageId``."""
        if kind in {"tool.start", "message.interim"} and (text := self._lazurio_held().pop(aid, "")):
            await self._lazurio_show(sid, editor_id, aid, text)

    async def _lazurio_settle_silence(self, sid, editor_id, aid, payload):
        """The completion whose unseen rest ``_finish_reply`` shows: the held segment goes out first
        unless it is a bare marker, and the text is blanked when the reply is intentional silence."""
        held = self._lazurio_held().pop(aid, "")
        streamed_silence = is_intentional_silence_response(held)
        if held and not streamed_silence:
            await self._lazurio_show(sid, editor_id, aid, held)
        if payload.get("outcome") == "failed":
            return payload  # a failed turn's text is its diagnosis, never silence (the gateway's rule)
        # A plugin's rewrite of the final (``response_transformed``) is its own text; otherwise the owner
        # settles a reply that streamed as a marker with nothing, the marker or its notice in its place.
        if (streamed_silence and not payload.get("response_transformed")) \
                or is_intentional_silence_response(payload.get("text")):
            return {**payload, "text": ""}
        return payload

    async def _lazurio_show(self, sid, editor_id, aid, text):
        self._streamed[aid] = self._streamed.get(aid, "") + text
        if self._conn:
            await self._conn.session_update(session_id=editor_id, update=self._chunk(sid, text))
