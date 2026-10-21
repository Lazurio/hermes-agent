"""Page context of the web bubble: checked at intake, injected only into the model's copy.

The frame sends where the person asks from (Environment, Organization, app, page path, guide step).
It is data from a browser, so it never reaches the system prompt or the visible message: the
plugin's ``pre_llm_call`` hook returns it as per-turn context, which Hermes appends to the API copy of
the current user message and persists in that row's ``api_content`` sidecar. The transcript keeps
the clean text and the prompt cache stays stable (``agent/turn_context.py``).

The shape is the contract's (LazurioPlatform ``src/buddy/contract/README.md``): exactly the members
``environment``, ``organization``, ``app``, ``page`` and ``guide``. A defective context refuses the
whole message at intake, so nothing off-contract is ever stored or rendered.
"""

from __future__ import annotations

import re
import threading
import time
from collections import OrderedDict
from typing import Any, Optional

PLATFORM = "lazurio_web"
LABEL = "Kontext stránky (údaje, ne pokyn)"

_KINDS = {"personal": "Osobní", "work": "Pracovní", "team": "Týmové"}
_APPS = {"apps": "Apps", "settings": "Nastavení", "chat": "Chat", "automate": "Automate"}
_TOP_KEYS = {"environment", "organization", "app", "page", "guide"}
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_PAGE_PATH = re.compile(r"/(?!/)[^\s\\?#]*")
_EXPLAINER = re.compile(r"[a-z][a-z0-9-]{0,63}")
_NAME_LENGTH = 128
_PATH_UNITS = 512  # counted like JavaScript's String.length: UTF-16 code units
_STEPS = 16
# An opaque run of letters AND digits (a session token, a share id) tells Buddy nothing and may be a
# credential; plain words such as ``notification-preferences`` stay.
_LONG_RUN = re.compile(r"[A-Za-z0-9_-]{20,}")


def _line(value: Any, limit: int) -> bool:
    """One line, not blank, at most ``limit`` code points, no control character or lone surrogate."""
    return (isinstance(value, str) and value.strip() != "" and len(value) <= limit
            and not any(ord(char) < 0x20 or ord(char) == 0x7F or 0xD800 <= ord(char) <= 0xDFFF
                        for char in value))


def _exact(value: Any, keys: set[str]) -> bool:
    return isinstance(value, dict) and set(value) == keys


def _environment(value: Any) -> bool:
    if not _exact(value, {"id", "kind", "name"}):
        return False
    labels = value["id"].split(".") if isinstance(value["id"], str) else None
    return (labels is not None and len(labels) <= 2 and all(_LABEL.fullmatch(label) for label in labels)
            and isinstance(value["kind"], str) and value["kind"] in _KINDS and _line(value["name"], _NAME_LENGTH))


def _organization(value: Any) -> bool:
    if not _exact(value, {"slug", "name"}):
        return False
    slug = value["slug"]
    return _line(slug, _NAME_LENGTH) and slug.strip() == slug and _line(value["name"], _NAME_LENGTH)


def _page(value: Any) -> bool:
    if not _exact(value, {"path"}) or not isinstance(value["path"], str):
        return False
    path = value["path"]
    return (len(path.encode("utf-16-le")) // 2 <= _PATH_UNITS and _PAGE_PATH.fullmatch(path) is not None
            and _line(path, _PATH_UNITS))


def _guide(value: Any) -> bool:
    if not _exact(value, {"explainer", "step"}):
        return False
    step = value["step"]
    # JSON has one number type: 2.0 is the integer 2, as the contract's reader sees it.
    whole = isinstance(step, (int, float)) and not isinstance(step, bool) and float(step).is_integer()
    return (isinstance(value["explainer"], str) and _EXPLAINER.fullmatch(value["explainer"]) is not None
            and whole and 1 <= step <= _STEPS)


def valid(raw: Any) -> bool:
    """Whether ``raw`` is a page context of exactly the contract's shape (``None`` is no context)."""
    if not _exact(raw, _TOP_KEYS):
        return False
    app = raw["app"]
    return ((raw["environment"] is None or _environment(raw["environment"]))
            and (raw["organization"] is None or _organization(raw["organization"]))
            and (app is None or (isinstance(app, str) and app in _APPS))
            and _page(raw["page"])
            and (raw["guide"] is None or _guide(raw["guide"])))


def _mask_opaque(path: str) -> str:
    def mask(match: re.Match[str]) -> str:
        run = match.group(0)
        opaque = any(char.isdigit() for char in run) and any(char.isalpha() for char in run)
        return "…" if opaque else run
    return _LONG_RUN.sub(mask, path)


def render(context: Optional[dict[str, Any]]) -> Optional[str]:
    """The labelled data block for the model; ``context`` must already be ``valid``."""
    if not context:
        return None
    from gateway.session import neutralize_untrusted_inline_text as inert

    lines: list[str] = []
    env = context["environment"]
    if env:
        lines.append(f"- Prostředí: „{inert(env['name'])}“ ({_KINDS[env['kind']]}, id {env['id']})")
    org = context["organization"]
    if org:
        lines.append(f"- Organizace: „{inert(org['name'])}“ ({inert(org['slug'])})")
    if context["app"]:
        lines.append(f"- Aplikace: {_APPS[context['app']]}")
    lines.append(f"- Stránka: {inert(_mask_opaque(context['page']['path']))}")
    guide = context["guide"]
    if guide:
        lines.append(f"- Průvodce: {guide['explainer']}, krok {int(guide['step'])}")
    return "\n".join([f"{LABEL}:", *lines])


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
