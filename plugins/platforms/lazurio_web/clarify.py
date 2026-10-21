"""Clarify cards of the web bubble.

The gateway registers a pending question in ``tools.clarify_gateway`` and asks the adapter to show
it (``send_clarify``). The card offers the choices plus „Jiné…“ (words of the person's own) and
„Přeskočit“; ``POST /v1/clarify/<id>`` answers it through the same resolver Telegram's buttons use.
A multi-select answer is a JSON array of labels, exactly as the gateway's text path produces it.

The contract shows each choice as one line of at most 200 code points, all distinct, and the
question as text of at most 2000. Hermes' labels may be longer or span lines, so the card carries
cleaned labels and the answer is mapped back to Hermes' own.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

from . import wire


@dataclass(frozen=True)
class Card:
    request: dict[str, Any]  # the ``clarify.request`` event data
    originals: tuple[str, ...]  # Hermes' labels, in the order of ``request["choices"]``


def open_entry(clarify_id: str) -> Optional[Any]:
    """The pending entry, or ``None`` once answered, expired or cleared. The registry has no public
    lookup; the base adapter and Telegram read it the same way."""
    from tools import clarify_gateway as cg

    with cg._lock:
        return cg._entries.get(clarify_id)


def _visible_question(question: str) -> str:
    """The question without the gateway's typed-skip hint: the card has its own „Přeskočit“."""
    from agent.i18n import t

    hint = t("gateway.clarify.skip_hint")
    return question[: -len(hint) - 1] if hint and question.endswith("\n" + hint) else question


def _shown(choices: list[str]) -> list[str]:
    shown: list[str] = []
    for choice in choices[: wire.CHOICES]:
        label = wire.clean_line(choice, wire.CHOICE_LENGTH) or "…"
        base, number = label, 2
        while label in shown:
            suffix = f" ({number})"
            label, number = base[: wire.CHOICE_LENGTH - len(suffix)] + suffix, number + 1
        shown.append(label)
    return shown


def card(clarify_id: str, question: str, choices: Optional[list], multi: bool) -> Card:
    originals = [str(choice) for choice in choices or []][: wire.CHOICES]
    shown = _shown(originals)
    text = wire.clean_text(_visible_question(question), wire.QUESTION_LENGTH).strip() or "…"
    request = {"clarify_id": clarify_id, "question": text, "choices": shown, "multi": bool(multi and shown)}
    return Card(request, tuple(originals))


def answer_value(entry: Any, shown: Card, kind: str, value: Any) -> str:
    """The resolver value for one checked answer (``wire.clarify_answer``)."""
    from tools.clarify_gateway import SKIPPED, mark_awaiting_text

    if kind == "skip":
        return SKIPPED
    if kind == "text":
        mark_awaiting_text(entry.clarify_id)  # „Jiné…“: the answer is typed, not picked
        return value.strip()
    labels = shown.request["choices"]
    picked = [value] if kind == "choice" else list(value)
    if not labels or any(label not in labels for label in picked) or (kind == "choices" and not entry.multi_select):
        raise wire.refuse("bad-request")
    originals = [shown.originals[labels.index(label)] for label in picked]
    return json.dumps(originals, ensure_ascii=False) if entry.multi_select else originals[0]
