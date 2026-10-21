"""Transcript pages for ``GET /v1/history``.

The bubble's conversation is the Hermes session behind ``agent:main:lazurio_web:dm:owner``. A page is
read from ``SessionDB.get_messages(include_ancestors=True, include_compacted=True)``: the display read,
so it spans compression rotations and keeps what an in-place compaction archived. Only what the
person saw survives: their own messages and Buddy's replies. Tool traffic, compaction summaries,
internal notices and the injected page context stay out.

A message is ``{message_id, role, text, at, via, media}`` (the contract's ``ConversationMessage``).
The person's message keeps the id the API answered for it (Hermes stores it as
``platform_message_id``) and the files they attached. Buddy's reply takes the id of the
``message.final`` event that delivered it, matched by its text while the outbox still holds that
event, so a reply both on the newest page and replayed from the snapshot is one message, not two.
Older replies get a stable id from their row.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Optional

from . import wire
from .page_context import LABEL, PLATFORM

# Text parts a turn carries only for the model: recalled memory and per-turn plugin context ride a
# multimodal turn as a durable part (agent/turn_context.py ``_append_multimodal_context``).
_MODEL_ONLY_PREFIXES = ("<memory-context>", f"{LABEL}:")
# Recovery scaffolding persists as an untyped ``[System: …]`` user row; the dashboard hides it too.
_SCAFFOLD_PREFIX = "[System:"


def _visible_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        text = part.get("text") if isinstance(part, dict) and part.get("type") in ("text", "input_text") else None
        if isinstance(text, str) and text.strip() and not text.lstrip().startswith(_MODEL_ONLY_PREFIXES):
            parts.append(text)
    return "\n".join(parts)


def _visible(rows: list[dict[str, Any]]) -> list[tuple[dict[str, Any], str]]:
    """``(row, text)`` of every row the person saw, in display order."""
    from agent.context_compressor import is_compaction_summary_message

    seen: list[tuple[dict[str, Any], str]] = []
    for row in rows:
        role = row.get("role")
        if role not in ("user", "assistant") or row.get("display_kind") or row.get("_compressed_summary"):
            continue
        if is_compaction_summary_message(row):  # the flag can be lost in round-trips; this reads content too
            continue
        text = _visible_text(row.get("content")).strip()
        if text and not (role == "user" and text.startswith(_SCAFFOLD_PREFIX)):
            seen.append((row, text))
    return seen


def _assistant_ids(visible: list[tuple[dict[str, Any], str]], finals: list[tuple[str, str]]) -> dict[int, str]:
    """Row id -> the ``message.final`` id that delivered it, newest reply to newest event."""
    unused = list(finals)
    matched: dict[int, str] = {}
    for row, text in reversed(visible):
        if row.get("role") != "assistant":
            continue
        hit = next((index for index, (_, final_text) in enumerate(unused) if final_text == text), None)
        if hit is not None:
            matched[int(row["id"])] = unused.pop(hit)[0]
    return matched


def _message_id(row: dict[str, Any], matched: dict[int, str]) -> str:
    if row.get("role") == "user":
        platform_id = row.get("platform_message_id")
        if wire.is_id(platform_id):
            return platform_id
    return matched.get(int(row["id"])) or f"h{int(row['id'])}"


def page(rows: list[dict[str, Any]], before: Optional[int], limit: int, *,
         finals: list[tuple[str, str]], media_for: Callable[[list[str]], dict[str, list[dict[str, Any]]]],
         ) -> tuple[list[dict[str, Any]], Optional[str]]:
    """``(messages oldest first, cursor of the next older page or None)``. ``before`` is a row id."""
    visible = _visible(rows)
    matched = _assistant_ids(visible, finals)  # over the whole conversation: ids stay the same on every page
    if before is not None:
        # Position, not id order: in-place compaction re-sequences carried rows under fresh ids.
        index = next((i for i, (row, _) in enumerate(visible) if int(row["id"]) == before), None)
        visible = visible[:index] if index is not None else [item for item in visible if int(item[0]["id"]) < before]
    chunk = visible[-limit:]
    ids = [_message_id(row, matched) for row, _ in chunk]
    media = media_for([message_id for (row, _), message_id in zip(chunk, ids) if row.get("role") == "user"])
    messages = [{
        "message_id": message_id, "role": row["role"], "text": wire.clean_text(text),
        "at": wire.instant(float(row.get("timestamp") or 0)), "via": PLATFORM,
        "media": media.get(message_id, []) if row["role"] == "user" else [],
    } for (row, text), message_id in zip(chunk, ids)]
    cursor = str(int(chunk[0][0]["id"])) if len(visible) > len(chunk) else None
    return messages, cursor
