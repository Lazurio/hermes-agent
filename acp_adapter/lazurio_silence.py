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

Here the viewer applies the gateway's rules with the gateway's predicates, per reply segment. A
segment's deltas are held while the segment so far may still be a bare marker and go out at once, in
order and under the segment's ``messageId``, as soon as it cannot. A segment ends where the gateway's
stream consumer breaks one: at the owner's segment boundary (a delta without text, which the agent
sends before it runs its tools, whether or not a tool then starts), a tool call or interim
commentary. Such a segment is commentary, never the final reply, so what it held goes out before
that boundary, the boundary delta closes the segment and its message as a tool call does, and the
next segment is held from its first delta again. Held text is never lost: a replay gap keeps it,
and the next boundary or the completion shows it as part of what the gap cut. At the turn's end a
held segment that is a bare marker is dropped and any other held text is shown. A reply that
streamed as a bare marker, or whose final text is one, is intentional silence: on a turn that did
not fail the editor gets no ``agent_message_chunk`` for it, neither the marker nor the owner's notice
in its place, however much commentary came before it, and the turn ends as it would have (its stop
reason, the owner's transcript and history unchanged). Thought chunks, tool calls and every other
update pass untouched; prose that mentions ``NO_REPLY`` is no marker and is delivered.

Overlay row ``acp: a reply of intentional silence (NO_REPLY, [SILENT]) sends nothing to the client``
(docs/operations/lazurio-fork-release.md). Removal: when upstream's ACP path (the gateway viewer, or
the owner's stream to local viewers) sends nothing for a reply of intentional silence, to an editor's
prompt too.
"""
from __future__ import annotations

from acp_adapter.gateway_reply import _text
from gateway.response_filters import is_intentional_silence_response, is_partial_silence_marker


def _may_become_silence(text: str) -> bool:
    """Whether a reply segment that starts with *text* may still turn out to be a bare marker.

    The gateway's prefix rule, and text with nothing in it yet that rules a marker out (leading
    whitespace, the ``*`` of ``*NO_REPLY*``): the gateway retracts a streamed preview, but an editor
    cannot take back a chunk."""
    return is_partial_silence_marker(text) or not any(ch.isalnum() for ch in text)


class LazurioSilenceMixin:
    """Mixed into ``GatewayACPAgent`` (``acp_adapter/gateway_server.py``), whose event projection
    (segment boundaries, then ``message.delta``) and ``_teardown`` call the hooks below, as does
    ``GatewayReplyMixin._finish_reply``. The held text is per admission and segment, beside
    ``_streamed`` (what the editor was shown of the segment) and ``_cut`` (that, once a replay gap cut
    the segment); a boundary marks the next segment held itself, so the hold never depends on when
    upstream resets ``_streamed``."""

    def _lazurio_held(self) -> dict:
        # admission -> the start of its current reply segment, received but not shown yet; "" marks a
        # segment a boundary opened that has shown nothing so far
        return self.__dict__.setdefault("_lazurio_silence_held", {})

    def _lazurio_unheld(self, aid, payload):
        """The text of a ``message.delta`` the editor sees now: ``""`` while the segment so far may still
        be a bare marker, then everything held with it, in order."""
        held, text = self._lazurio_held(), _text(payload)
        if aid not in held and self._streamed.get(aid):
            return text  # this segment already shows text, so it is no bare marker
        segment = held.pop(aid, "") + text
        if _may_become_silence(segment):
            held[aid] = segment
            return ""
        return segment

    async def _lazurio_segment_boundary(self, kind, sid, editor_id, aid, payload):
        """Where a reply segment ends, as the gateway's stream consumer ends one: a tool call, interim
        commentary or the owner's boundary delta (a ``message.delta`` without text). The segment is
        commentary, so what it held goes out now, under its ``messageId``, and the next one starts held.
        The boundary delta closes the segment and its message too, as a tool call does: no tool call may
        follow it, and the final reply after it is measured on its own (``_final_remainder``)."""
        boundary = kind == "message.delta" and payload.get("text") is None
        if not boundary and kind not in {"tool.start", "message.interim"}:
            return
        if boundary and aid in self._cut:
            return  # a replay gap stopped this admission's live deltas, a boundary's too (``_gap_guard``)
        held = self._lazurio_held()
        text, held[aid] = held.get(aid, ""), ""
        if text:
            await self._lazurio_show(sid, editor_id, aid, text)
        if boundary:
            self._close_segment(aid)
            self._end_message(sid)

    async def _lazurio_settle_silence(self, sid, editor_id, aid, payload):
        """The completion whose unseen rest ``_finish_reply`` shows: the held segment goes out first
        unless it is a bare marker, and the text is blanked when the reply is intentional silence."""
        held = self._lazurio_held().pop(aid, "")
        streamed_silence = is_intentional_silence_response(held)
        # A failed turn's text is its diagnosis, never silence (the gateway's rule). A plugin's rewrite of the
        # final (``response_transformed``) is its own text; otherwise the owner settles a reply that streamed
        # as a marker with nothing, the marker or its notice in its place. A final that is a marker settles a
        # reply a replay gap cut mid-marker too, so what the segment held of it is dropped, not shown.
        silent = payload.get("outcome") != "failed" and (
            (streamed_silence and not payload.get("response_transformed"))
            or is_intentional_silence_response(payload.get("text")))
        if held and not silent and not streamed_silence:
            await self._lazurio_show(sid, editor_id, aid, held)
        return {**payload, "text": ""} if silent else payload

    async def _lazurio_show(self, sid, editor_id, aid, text):
        # Recorded where the viewer measures what the segment showed: ``_cut`` once a replay gap cut it
        # (the completion owes the rest, ``owed_reply``), else ``_streamed``.
        shown = self._cut if aid in self._cut else self._streamed
        shown[aid] = shown.get(aid, "") + text
        if self._conn:
            await self._conn.session_update(session_id=editor_id, update=self._chunk(sid, text))
