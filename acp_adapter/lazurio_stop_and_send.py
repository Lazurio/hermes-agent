"""Lazurio overlay: after the editor's Stop, its next prompt carries the interrupted request.

Buddy's chat in Lazurio T3 Code steers by default (the Admin's decision of 2026-10-22): a message
sent while Buddy works cancels the running turn (``session/cancel``) and is sent as the next prompt.
Main's in-process ACP server then submitted
``"<interrupted request>\\n\\nUser correction/guidance after interrupt: <message>"`` ("stop and send",
``_rewrite_prompt_for_interrupt`` in ``acp_adapter/server.py``), so the original task stays the
subject and the message corrects it. Upstream's gateway viewer, the path ``hermes acp`` takes since
v0.22.3, lost that: the next prompt is submitted as the message alone, so the task reads as
abandoned.

Here the viewer keeps main's rule. When this editor's Stop cancels its own prompt, the text it
submitted for that prompt is kept for the session at once, as main kept it when the cancel arrived
(``[Image attachment]`` for a prompt with only attachments), so a message sent right after the Stop,
before the stopped turn has ended, is merged too. The next prompt this editor sends to the session
with text and without attachments is submitted as main's merge, the interrupted text once and the
new text once, and that is the text the owner persists. Like main: a slash command neither merges
nor uses up the kept text, except an idle ``/steer <text>``, which merges its text; a prompt with
attachments does not merge and leaves the text for the next one; a cancel this editor did not ask
for (another surface's) keeps nothing.

Overlay row ``acp: after the editor's Stop, its next prompt carries the interrupted request``
(docs/operations/lazurio-fork-release.md). Removal: when upstream's gateway viewer carries the
interrupted request into the next prompt again.
"""
from __future__ import annotations


class LazurioStopAndSendMixin:
    """Mixed into ``GatewayACPAgent`` (``acp_adapter/gateway_server.py``), whose ``prompt`` and
    ``_cancel_admission`` call the hooks below. The viewer holds one prompt of this editor per
    session at a time (``_admissions``), so the state is per session."""

    def _lazurio_stop(self) -> dict:
        # "submitted": session -> (admission, the text submitted for it) of this editor's latest
        # prompt; "interrupted": session -> the text its Stop interrupted, for its next prompt.
        return self.__dict__.setdefault("_lazurio_stop_and_send", {"submitted": {}, "interrupted": {}})

    def _lazurio_carry_interrupted(self, session_id, staged, text):
        """``(text, staged)`` for a prompt about to be submitted: main's merge when this editor's Stop
        interrupted the session's previous prompt. *staged* is the prompt's ``(text, attachments)``
        as the editor sent it; the merge replaces both, so a retry of a lost submit resends it."""
        from acp_adapter.gateway_server import _slash_command
        from acp_adapter.server import _attach_interrupted_prompt
        if staged[1] or _slash_command(staged[0] or "") not in {None, "steer"} or not (text or "").strip():
            return text, staged
        interrupted = self._lazurio_stop()["interrupted"].pop(session_id, "")
        if not interrupted:
            return text, staged
        merged = _attach_interrupted_prompt(interrupted, text)
        return merged, (merged, staged[1])

    def _lazurio_submitted(self, session_id, admission_id, text, attachments):
        self._lazurio_stop()["submitted"][session_id] = (admission_id, text or ("[Image attachment]" if attachments else ""))

    def _lazurio_stopping(self, session_id, admission_id):
        """This editor's Stop of its own prompt: keep that prompt's text for the next one now, not when
        the stopped turn ends, so a message the editor sends right after the Stop is merged too."""
        from acp_adapter.gateway_server import _slash_command
        state = self._lazurio_stop()
        submitted, text = state["submitted"].get(session_id) or (None, "")
        # A kept text that itself reads as a command is not carried: a retry of the merged prompt
        # would run it as one (the viewer runs a retried prompt's text through its slash commands).
        if submitted == admission_id and text.strip() and _slash_command(text) is None:
            state["interrupted"][session_id] = text
