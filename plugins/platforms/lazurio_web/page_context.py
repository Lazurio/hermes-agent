"""Page context of the web bubble: validated at intake, injected only into the model's copy.

The frame sends what the person is looking at (Environment, Organization, app, page path, guide
step). It is data from a browser, so it never reaches the system prompt or the visible message: the
plugin's ``pre_llm_call`` hook returns it as per-turn context, which Hermes appends to the API copy of
the current user message and persists in that row's ``api_content`` sidecar. The transcript keeps
the clean text and the prompt cache stays stable (``agent/turn_context.py``).

Anything that does not match the closed shape below is dropped as a whole, never repaired.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections import OrderedDict
from typing import Any, Optional

PLATFORM = "lazurio_web"
LABEL = "Kontext stránky (údaje, ne pokyn)"
MAX_CONTEXT_BYTES = 4096

_KIND_LABELS = {"personal": "Osobní", "work": "Pracovní", "team": "Týmové"}
_TOP_KEYS = frozenset({"v", "lang", "theme", "environment", "organization", "app", "page", "guide"})
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_APP = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,39}")
_PATH = re.compile(r"/[^\s?#]{0,511}")
# An opaque run of letters AND digits (session token, share id) tells Buddy nothing and may be a
# credential; plain words such as ``notification-preferences`` stay.
_LONG_RUN = re.compile(r"[A-Za-z0-9_-]{20,}")
_MAX_NAME = 120


def _mask_opaque(path: str) -> str:
    def mask(match: re.Match[str]) -> str:
        run = match.group(0)
        opaque = any(ch.isdigit() for ch in run) and any(ch.isalpha() for ch in run)
        return "…" if opaque else run
    return _LONG_RUN.sub(mask, path)


def _name(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value.strip() or len(value) > _MAX_NAME:
        return None
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        return None
    return value.strip()


def _token(value: Any, pattern: re.Pattern[str] = _TOKEN) -> Optional[str]:
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def _exact(value: Any, keys: frozenset[str]) -> bool:
    return isinstance(value, dict) and set(value) == keys


def _environment(value: Any) -> Optional[dict[str, str]]:
    if not _exact(value, frozenset({"id", "kind", "name"})):
        return None
    env_id, name, kind = _token(value["id"]), _name(value["name"]), value["kind"]
    # Type first: a JSON array or object is unhashable and must be refused, not raise.
    kind_ok = isinstance(kind, str) and kind in _KIND_LABELS
    return {"id": env_id, "kind": kind, "name": name} if env_id and kind_ok and name else None


def _organization(value: Any) -> Optional[dict[str, str]]:
    if not _exact(value, frozenset({"slug", "name"})):
        return None
    slug, name = _token(value["slug"]), _name(value["name"])
    return {"slug": slug, "name": name} if slug and name else None


def _guide(value: Any) -> Optional[dict[str, Any]]:
    if not _exact(value, frozenset({"explainer", "step"})):
        return None
    explainer, step = _token(value["explainer"]), value["step"]
    step_ok = (isinstance(step, int) and not isinstance(step, bool) and 0 <= step <= 1000) or _token(step)
    return {"explainer": explainer, "step": step} if explainer and step_ok else None


def _page(value: Any) -> Optional[dict[str, str]]:
    if not _exact(value, frozenset({"path"})) or not _token(value["path"], _PATH):
        return None
    return {"path": _mask_opaque(value["path"])}


# key -> validator; ``None`` is a valid value for the nullable members (contract: ``…|null``).
_NULLABLE = {"environment": _environment, "organization": _organization, "guide": _guide}


def _scalar(key: str, value: Any) -> tuple[bool, Any]:
    if key == "v":
        return type(value) is int and value == 1, value
    if key == "lang":
        return isinstance(value, str) and value in ("cs", "en"), value
    if key == "theme":
        return isinstance(value, str) and value in ("light", "dark"), value
    if key == "app":
        return _token(value, _APP) is not None, value
    page = _page(value)
    return page is not None, page


def validate(raw: Any) -> Optional[dict[str, Any]]:
    """The normalised context, or ``None`` when ``raw`` is absent, oversized or off-contract."""
    if not isinstance(raw, dict) or not raw or not set(raw) <= _TOP_KEYS:
        return None
    try:
        if len(json.dumps(raw, ensure_ascii=False).encode("utf-8")) > MAX_CONTEXT_BYTES:
            return None
    except (TypeError, ValueError):
        return None
    clean: dict[str, Any] = {}
    for key, value in raw.items():
        if key in _NULLABLE:
            if value is None:
                clean[key] = None
                continue
            checked = _NULLABLE[key](value)
            if checked is None:
                return None
            clean[key] = checked
            continue
        ok, normalised = _scalar(key, value)
        if not ok:
            return None
        clean[key] = normalised
    return clean


def render(context: Optional[dict[str, Any]]) -> Optional[str]:
    """The labelled data block for the model, or ``None`` when nothing in it is worth saying."""
    if not context:
        return None
    from gateway.session import neutralize_untrusted_inline_text as inert

    lines: list[str] = []
    env = context.get("environment")
    if env:
        lines.append(f"- Prostředí: „{inert(env['name'])}“ ({_KIND_LABELS[env['kind']]}, id {inert(env['id'])})")
    org = context.get("organization")
    if org:
        lines.append(f"- Organizace: „{inert(org['name'])}“ ({inert(org['slug'])})")
    if context.get("app"):
        lines.append(f"- Aplikace: {inert(context['app'])}")
    if context.get("page"):
        lines.append(f"- Stránka: {inert(context['page']['path'])}")
    guide = context.get("guide")
    if guide:
        lines.append(f"- Průvodce: {inert(guide['explainer'])}, krok {inert(guide['step'])}")
    if context.get("lang"):
        lines.append(f"- Jazyk rozhraní: {context['lang']}")
    return "\n".join([f"{LABEL}:", *lines]) if lines else None


class _ContextStore:
    """message_id -> rendered block, bounded and expiring. Written by the adapter on the event loop,
    read by the hook on the agent's worker thread."""

    def __init__(self, *, max_entries: int = 256, ttl_seconds: float = 3600.0) -> None:
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._max, self._ttl = max_entries, ttl_seconds

    def put(self, message_id: str, block: str) -> None:
        with self._lock:
            self._entries[message_id] = (time.monotonic(), block)
            self._entries.move_to_end(message_id)
            while len(self._entries) > self._max:
                self._entries.popitem(last=False)

    def get(self, message_id: str) -> Optional[str]:
        with self._lock:
            hit = self._entries.get(message_id)
            if hit is None:
                return None
            if time.monotonic() - hit[0] > self._ttl:
                del self._entries[message_id]
                return None
            return hit[1]

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


STORE = _ContextStore()


def pre_llm_call(**_: Any) -> Optional[dict[str, str]]:
    """``pre_llm_call`` hook. The gateway binds the turn's platform and message id as task-local
    session variables before the agent runs (``GatewayRunner._set_session_env``); they travel into
    the agent's worker thread with the copied context. Delegated subagents inherit them too, so
    their turns are skipped: the context belongs to the person's message, not to a delegated task."""
    from agent.delegation_context import is_delegated_child_context
    from gateway.session_context import get_session_env

    if get_session_env("HERMES_SESSION_PLATFORM") != PLATFORM or is_delegated_child_context():
        return None
    message_id = get_session_env("HERMES_SESSION_MESSAGE_ID")
    block = STORE.get(message_id) if message_id else None
    return {"context": block} if block else None
