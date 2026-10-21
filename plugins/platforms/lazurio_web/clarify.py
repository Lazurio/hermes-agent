"""Clarify cards of the web bubble.

The gateway registers a pending question in ``tools.clarify_gateway`` and asks the adapter to show
it (``send_clarify``). The card offers the choices plus „Jiné…“ (a typed answer) and „Přeskočit“;
``POST /v1/clarify/<id>`` answers it through the same resolver Telegram's buttons use. A
multi-select answer is a JSON array of labels, exactly as the gateway's text path produces it.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from .errors import ApiError

MAX_TEXT_ANSWER = 8000


def open_entry(clarify_id: str) -> Optional[Any]:
    """The pending entry, or ``None`` once answered, expired or cleared. The registry has no public
    lookup; the base adapter and Telegram read it the same way."""
    from tools import clarify_gateway as cg

    with cg._lock:
        return cg._entries.get(clarify_id)


def visible_question(question: str) -> str:
    """The question without the gateway's typed-skip hint: the card has its own „Přeskočit“."""
    from agent.i18n import t

    hint = t("gateway.clarify.skip_hint")
    return question[: -len(hint) - 1] if hint and question.endswith("\n" + hint) else question


def _pick(choices: list[str], value: Any) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        if 0 <= value < len(choices):
            return choices[value]
    elif isinstance(value, str):
        wanted = value.strip()
        for choice in choices:
            if str(choice).strip() == wanted:
                return str(choice)
    raise ApiError(400, "invalid_choice", "choice must be an index or the exact label of an offered choice")


def answer_value(entry: Any, body: dict[str, Any]) -> str:
    """The resolver value for one answer body: ``{choice}``, ``{choices}``, ``{text}`` or ``{skip}``."""
    from tools.clarify_gateway import SKIPPED, mark_awaiting_text

    if len(body) != 1 or not set(body) <= {"choice", "choices", "text", "skip"}:
        raise ApiError(400, "invalid_answer", "send exactly one of choice, choices, text, skip")
    key, value = next(iter(body.items()))
    if key == "skip":
        if value is not True:
            raise ApiError(400, "invalid_answer", "skip must be true")
        return SKIPPED
    if key == "text":
        if not isinstance(value, str) or not value.strip() or len(value) > MAX_TEXT_ANSWER:
            raise ApiError(400, "invalid_answer", f"text must be 1-{MAX_TEXT_ANSWER} characters")
        mark_awaiting_text(entry.clarify_id)  # „Jiné…“: the answer is typed, not picked
        return value.strip()
    choices = [str(choice) for choice in entry.choices or []]
    if not choices:
        raise ApiError(400, "invalid_answer", "this question takes a text answer")
    if key == "choice":
        if entry.multi_select:
            raise ApiError(400, "invalid_answer", "this question takes several choices: use choices")
        return _pick(choices, value)
    if not entry.multi_select:
        raise ApiError(400, "invalid_answer", "this question takes one choice: use choice")
    if not isinstance(value, list) or not value:
        raise ApiError(400, "invalid_answer", "choices must be a non-empty list")
    labels: list[str] = []
    for item in value:
        label = _pick(choices, item)
        if label not in labels:
            labels.append(label)
    return json.dumps(labels, ensure_ascii=False)
