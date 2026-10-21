"""The shapes of ``lazurio.buddy-runtime.v1`` on this side of the wire.

The contract belongs to LazurioPlatform (``src/buddy/contract/README.md`` and its vectors): every
check is strict, a defect refuses the whole value, and no text of the adapter reaches the person
(an error is only ``{"error": {"code"}}``). This module is the adapter's reading of those rules:
the request checks, the bounds, and the cleaning that keeps every outgoing text inside them.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Optional

from . import page_context

ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")
CURSOR = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:=-]{0,127}")
_MIME = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}")
_BASE64 = re.compile(r"(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?")

TEXT_LENGTH = 16_000
ATTACHMENTS = 4
ATTACHMENT_BYTES = 5 * 1024 * 1024
ATTACHMENTS_BYTES = 10 * 1024 * 1024
NAME_LENGTH = 255
HISTORY_LIMIT_DEFAULT = 30
HISTORY_LIMIT_MAX = 100
QUESTION_LENGTH = 2_000
CHOICES = 8
CHOICE_LENGTH = 200
CLARIFY_TEXT_LENGTH = 2_000
PROGRESS_LENGTH = 300
MESSAGE_BODY_BYTES = 15 * 1024 * 1024
SMALL_BODY_BYTES = 64 * 1024

# Every error code with its HTTP status (the contract's ``runtimeErrors``).
STATUS = {
    "bad-request": 400, "unauthorized": 401, "forbidden": 403, "not-found": 404,
    "method-not-allowed": 405, "conflict": 409, "cursor-expired": 409, "payload-too-large": 413,
    "unavailable": 503, "internal": 500,
}

_MESSAGE_KEYS = {"client_id", "text", "attachments", "context"}
_ATTACHMENT_KEYS = {"name", "mime", "data_base64"}


class ApiError(Exception):
    """A refused request: the API answers ``{"error": {"code"}}`` with the code's status."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code, self.status = code, STATUS[code]


def refuse(code: str) -> ApiError:
    return ApiError(code)


def is_id(value: Any) -> bool:
    return isinstance(value, str) and ID.fullmatch(value) is not None


def _clean(value: str, *, lines: bool) -> bool:
    """No lone surrogate, no C0 control and no DEL; tab, LF and CR only where text spans lines."""
    for char in value:
        code = ord(char)
        if 0xD800 <= code <= 0xDFFF or code == 0x7F:
            return False
        if code < 0x20 and not (lines and char in "\t\n\r"):
            return False
    return True


def is_text(value: Any, limit: int, *, empty: bool = False) -> bool:
    return (isinstance(value, str) and (empty or value != "") and len(value) <= limit
            and _clean(value, lines=True))


def is_line(value: Any, limit: int) -> bool:
    return isinstance(value, str) and value.strip() != "" and len(value) <= limit and _clean(value, lines=False)


def is_file_name(value: Any) -> bool:
    return (is_line(value, NAME_LENGTH) and "/" not in value and "\\" not in value
            and value not in (".", ".."))


def _base64_bytes(value: Any) -> Optional[bytes]:
    if not isinstance(value, str) or _BASE64.fullmatch(value) is None:
        return None
    return base64.b64decode(value, validate=True)


@dataclass
class Attachment:
    name: str
    mime: str
    data: bytes


@dataclass
class InboundMessage:
    client_id: str
    text: str
    attachments: list[Attachment] = field(default_factory=list)
    context: Optional[dict[str, Any]] = None


def _attachments(raw: Any) -> list[Attachment]:
    if not isinstance(raw, list) or len(raw) > ATTACHMENTS:
        raise refuse("bad-request")
    attachments, total = [], 0
    for item in raw:
        if not isinstance(item, dict) or set(item) != _ATTACHMENT_KEYS:
            raise refuse("bad-request")
        if not is_file_name(item["name"]) or not isinstance(item["mime"], str) or not _MIME.fullmatch(item["mime"]):
            raise refuse("bad-request")
        data = _base64_bytes(item["data_base64"])
        if not data or len(data) > ATTACHMENT_BYTES:
            raise refuse("bad-request")
        total += len(data)
        attachments.append(Attachment(item["name"], item["mime"], data))
    if total > ATTACHMENTS_BYTES:
        raise refuse("bad-request")
    return attachments


def message(body: Any) -> InboundMessage:
    """``POST /v1/messages``: exactly its four members; a defective context refuses the message."""
    if not isinstance(body, dict) or set(body) != _MESSAGE_KEYS:
        raise refuse("bad-request")
    if not is_id(body["client_id"]) or not is_text(body["text"], TEXT_LENGTH, empty=True):
        raise refuse("bad-request")
    attachments = _attachments(body["attachments"])
    if body["text"].strip() == "" and not attachments:
        raise refuse("bad-request")
    if body["context"] is not None and not page_context.valid(body["context"]):
        raise refuse("bad-request")
    return InboundMessage(body["client_id"], body["text"], attachments, body["context"])


def clarify_answer(body: Any) -> tuple[str, Any]:
    """``(kind, value)`` of exactly one answer: ``choice``, ``choices``, ``text`` or ``skip``."""
    if not isinstance(body, dict) or len(body) != 1:
        raise refuse("bad-request")
    kind, value = next(iter(body.items()))
    ok = {
        "choice": lambda: is_line(value, CHOICE_LENGTH),
        "choices": lambda: (isinstance(value, list) and 0 < len(value) <= CHOICES
                            and all(is_line(item, CHOICE_LENGTH) for item in value)
                            and len(set(value)) == len(value)),
        "text": lambda: is_text(value, CLARIFY_TEXT_LENGTH) and value.strip() != "",
        "skip": lambda: value is True,
    }.get(kind)
    if ok is None or not ok():
        raise refuse("bad-request")
    return kind, value


def empty(body: Any) -> None:
    if not isinstance(body, dict) or body:
        raise refuse("bad-request")


# -- outgoing text --------------------------------------------------------------------------------

_REPLACEMENT = "�"
_SPACES = re.compile(r"\s+")


def clean_text(value: str, limit: int = TEXT_LENGTH) -> str:
    """Text the contract admits: disallowed characters replaced, at most ``limit`` code points."""
    text = "".join(char if _clean(char, lines=True) else _REPLACEMENT for char in value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def clean_line(value: str, limit: int) -> str:
    """One line: whitespace runs collapsed, disallowed characters replaced, at most ``limit``."""
    return clean_text(_SPACES.sub(" ", value).strip(), limit)


def instant(timestamp: Optional[float] = None) -> str:
    """An instant in UTC with ``Z``, as the contract writes it."""
    moment = datetime.now(tz=UTC) if timestamp is None else datetime.fromtimestamp(timestamp, tz=UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")
