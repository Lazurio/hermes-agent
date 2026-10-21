"""Transcript pages for ``GET /v1/history``.

The bubble's conversation is the Hermes session behind ``agent:main:lazurio_web:dm:owner``. A page is
read from ``SessionDB.get_messages(include_ancestors=True, include_compacted=True)``: the display read,
so it spans compression rotations and keeps what an in-place compaction archived. Only what the
person saw survives: their own messages and Buddy's replies. Tool traffic, compaction summaries,
internal notices and the injected page context stay out.

A message is ``{message_id, role, text, at, via, media}`` (the contract's ``ConversationMessage``).
The person's message keeps the id the API answered for it (Hermes stores it as
``platform_message_id``) and the files they attached. Buddy's reply takes the id and the exact text
of the ``message.final`` event that delivered it, matched by its text while the outbox still holds
that event, so a reply both on the newest page and replayed from the snapshot is one message, not
two. Older replies get a stable id from their row and keep their text as persisted.

A cursor is the id of the visible row that starts a page. One that names no visible row (never
minted here, or its row is no longer shown) is ``404 not-found``, never a page guessed by id order.
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
    """``(row, text)`` of every row the person saw, in display order, the text as persisted. Blank
    text only decides that a row is not shown; it never trims what is."""
    from agent.context_compressor import is_compaction_summary_message

    seen: list[tuple[dict[str, Any], str]] = []
    for row in rows:
        role = row.get("role")
        if role not in ("user", "assistant") or row.get("display_kind") or row.get("_compressed_summary"):
            continue
        if is_compaction_summary_message(row):  # the flag can be lost in round-trips; this reads content too
            continue
        text = _visible_text(row.get("content"))
        if text.strip() and not (role == "user" and text.lstrip().startswith(_SCAFFOLD_PREFIX)):
            seen.append((row, text))
    return seen


def _reply_key(text: str) -> str:
    """What a delivered reply and its persisted row agree on: the same text as the contract carries
    it, whatever whitespace one of them keeps around it."""
    return wire.clean_text(text).strip()


def _delivered(visible: list[tuple[dict[str, Any], str]],
               finals: list[tuple[str, str]]) -> dict[int, tuple[str, str]]:
    """Row id -> ``(message_id, text)`` of the ``message.final`` that delivered it, newest reply to
    newest event."""
    unused = [(message_id, text, _reply_key(text)) for message_id, text in finals]
    matched: dict[int, tuple[str, str]] = {}
    for row, text in reversed(visible):
        if not unused:
            break
        if row.get("role") != "assistant":
            continue
        key = _reply_key(text)
        hit = next((index for index, (_, _, final_key) in enumerate(unused) if final_key == key), None)
        if hit is not None:
            message_id, final_text, _ = unused.pop(hit)
            matched[int(row["id"])] = (message_id, final_text)
    return matched


def _message_id(row: dict[str, Any], delivered: dict[int, tuple[str, str]]) -> str:
    if row.get("role") == "user":
        platform_id = row.get("platform_message_id")
        if wire.is_id(platform_id):
            return platform_id
    hit = delivered.get(int(row["id"]))
    return hit[0] if hit else f"h{int(row['id'])}"


def _text(row: dict[str, Any], text: str, delivered: dict[int, tuple[str, str]]) -> str:
    hit = delivered.get(int(row["id"])) if row.get("role") == "assistant" else None
    return hit[1] if hit else wire.clean_text(text)


def page(rows: list[dict[str, Any]], before: Optional[str], limit: int, *,
         finals: list[tuple[str, str]], media_for: Callable[[list[str]], dict[str, list[dict[str, Any]]]],
         ) -> tuple[list[dict[str, Any]], Optional[str]]:
    """``(messages oldest first, cursor of the next older page or None)``. ``before`` must name a
    visible row; anything else is ``404 not-found``."""
    visible = _visible(rows)
    delivered = _delivered(visible, finals)  # over the whole conversation: ids stay the same on every page
    if before is not None:
        # Position, not id order: in-place compaction re-sequences carried rows under fresh ids.
        index = next((i for i, (row, _) in enumerate(visible) if str(int(row["id"])) == before), None)
        if index is None:
            raise wire.refuse("not-found")
        visible = visible[:index]
    chunk = visible[-limit:]
    ids = [_message_id(row, delivered) for row, _ in chunk]
    media = media_for([message_id for (row, _), message_id in zip(chunk, ids) if row.get("role") == "user"])
    messages = [{
        "message_id": message_id, "role": row["role"], "text": _text(row, text, delivered),
        "at": wire.instant(float(row.get("timestamp") or 0)), "via": PLATFORM,
        "media": media.get(message_id, []) if row["role"] == "user" else [],
    } for (row, text), message_id in zip(chunk, ids)]
    cursor = str(int(chunk[0][0]["id"])) if len(visible) > len(chunk) else None
    return messages, cursor
