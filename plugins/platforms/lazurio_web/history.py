"""Transcript pages for ``GET /v1/history``.

The bubble's conversation is the Hermes session behind ``agent:main:lazurio_web:dm:owner``. A page is
read from ``SessionDB.get_messages(include_ancestors=True, include_compacted=True)``: the display read,
so it spans compression rotations and keeps what an in-place compaction archived. Only what the
person saw survives: their own messages and Buddy's replies. Tool traffic, compaction summaries,
internal notices and the injected page context stay out.
"""

from __future__ import annotations

from typing import Any, Optional

from .page_context import LABEL

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


def project(row: dict[str, Any]) -> Optional[dict[str, Any]]:
    """One visible bubble for a stored message, or ``None`` when the person never saw it."""
    from agent.context_compressor import is_compaction_summary_message

    role = row.get("role")
    if role not in ("user", "assistant") or row.get("display_kind") or row.get("_compressed_summary"):
        return None
    if is_compaction_summary_message(row):  # the flag can be lost in round-trips; this reads content too
        return None
    text = _visible_text(row.get("content")).strip()
    if not text or (role == "user" and text.startswith(_SCAFFOLD_PREFIX)):
        return None
    message_id = row.get("platform_message_id") if role == "user" else None
    return {"id": int(row["id"]), "role": role, "text": text, "ts": row.get("timestamp"),
            "message_id": str(message_id) if message_id else None}


def page(rows: list[dict[str, Any]], before: Optional[int], limit: int) -> tuple[list[dict[str, Any]], Optional[int]]:
    """``(messages oldest-first, next_before)``; ``next_before`` is ``None`` on the oldest page."""
    visible = [item for item in map(project, rows) if item is not None]
    if before is not None:
        # Position, not id order: in-place compaction re-sequences carried rows under fresh ids.
        index = next((i for i, item in enumerate(visible) if item["id"] == before), None)
        visible = visible[:index] if index is not None else [item for item in visible if item["id"] < before]
    chunk = visible[-limit:]
    return chunk, (chunk[0]["id"] if len(visible) > len(chunk) else None)
