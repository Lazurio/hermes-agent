"""Durable outbox of the ``lazurio_web`` channel.

Every event the web client can receive is committed here before the adapter reports a send as
delivered. A client that reconnects replays exactly what it missed by event id, and a crash after
the gateway's delivery ledger marked a reply delivered can no longer lose it: the commit already
happened. One SQLite file per Hermes home, WAL with ``synchronous=FULL``, so a committed append
survives power loss. Event ids come from ``AUTOINCREMENT`` and are never reused, even after pruning.

The same file remembers the client ids of accepted messages (idempotent retries) and the files the
adapter handed to the client (``GET /v1/media/<id>``).
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    data TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS inbound (
    client_id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS media (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    mime TEXT NOT NULL,
    size INTEGER NOT NULL,
    path TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS message_media (
    message_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    media_id TEXT NOT NULL,
    PRIMARY KEY (message_id, position)
);
"""


@dataclass(frozen=True)
class OutboxEvent:
    id: int
    type: str
    data: dict[str, Any]


@dataclass(frozen=True)
class InboundClaim:
    message_id: str
    created: bool


@dataclass(frozen=True)
class MediaRecord:
    id: str
    name: str
    mime: str
    size: int
    path: str


class Outbox:
    """Thread-safe: the event loop writes through ``asyncio.to_thread`` and the agent thread writes
    progress events directly, so one connection is shared behind a lock."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        self._lock = threading.Lock()
        # isolation_level=None: every statement commits on its own, which is the durability point.
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(_SCHEMA)
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{path}{suffix}")
            if candidate.exists():
                os.chmod(candidate, 0o600)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- events ---------------------------------------------------------------------------------

    def append(self, event_type: str, data: dict[str, Any]) -> int:
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO events (type, data, created_at) VALUES (?, ?, ?)",
                (event_type, payload, time.time()))
            return int(cursor.lastrowid)

    def read_after(self, after: int, limit: int) -> list[OutboxEvent]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, type, data FROM events WHERE id > ? ORDER BY id LIMIT ?",
                (after, limit)).fetchall()
        return [OutboxEvent(int(row[0]), row[1], json.loads(row[2])) for row in rows]

    def last_id(self) -> int:
        """Highest id ever assigned (``sqlite_sequence`` survives pruning; ``MAX(id)`` would not)."""
        with self._lock:
            row = self._conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'events'").fetchone()
        return int(row[0]) if row else 0

    def recent_assistant_finals(self, limit: int) -> list[tuple[str, str]]:
        """``(message_id, text)`` of Buddy's newest ``message.final`` events, newest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM events WHERE type = 'message.final' ORDER BY id DESC LIMIT ?",
                (limit,)).fetchall()
        finals = (json.loads(row[0]) for row in rows)
        return [(final["message_id"], final["text"]) for final in finals if final.get("role") == "assistant"]

    def last_of_type(self, event_type: str) -> Optional[OutboxEvent]:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, type, data FROM events WHERE type = ? ORDER BY id DESC LIMIT 1",
                (event_type,)).fetchone()
        return OutboxEvent(int(row[0]), row[1], json.loads(row[2])) if row else None

    # -- idempotent intake ----------------------------------------------------------------------

    def claim_inbound(self, client_id: str, message_id: str) -> InboundClaim:
        """The claim stored for ``client_id``; ``created`` when this call stored it."""
        with self._lock:
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO inbound (client_id, message_id, created_at) VALUES (?, ?, ?)",
                (client_id, message_id, time.time()))
            if cursor.rowcount:
                return InboundClaim(message_id, True)
            row = self._conn.execute(
                "SELECT message_id FROM inbound WHERE client_id = ?", (client_id,)).fetchone()
        return InboundClaim(row[0], False)

    # -- media ----------------------------------------------------------------------------------

    def add_media(self, record: MediaRecord) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO media (id, name, mime, size, path, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (record.id, record.name, record.mime, record.size, record.path, time.time()))

    def link_media(self, message_id: str, media_ids: list[str]) -> None:
        """Remember the files of the person's message, for its history entry."""
        with self._lock:
            self._conn.execute("DELETE FROM message_media WHERE message_id = ?", (message_id,))
            self._conn.executemany(
                "INSERT INTO message_media (message_id, position, media_id) VALUES (?, ?, ?)",
                [(message_id, position, media_id) for position, media_id in enumerate(media_ids)])

    def message_media(self, message_ids: list[str]) -> dict[str, list[MediaRecord]]:
        """The retained files of each message, in the order they were sent."""
        if not message_ids:
            return {}
        marks = ",".join("?" * len(message_ids))
        with self._lock:
            rows = self._conn.execute(
                "SELECT l.message_id, m.id, m.name, m.mime, m.size, m.path FROM message_media l"
                f" JOIN media m ON m.id = l.media_id WHERE l.message_id IN ({marks})"
                " ORDER BY l.message_id, l.position", message_ids).fetchall()
        linked: dict[str, list[MediaRecord]] = {}
        for row in rows:
            linked.setdefault(row[0], []).append(MediaRecord(row[1], row[2], row[3], int(row[4]), row[5]))
        return linked

    def get_media(self, media_id: str) -> Optional[MediaRecord]:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, name, mime, size, path FROM media WHERE id = ?", (media_id,)).fetchone()
        return MediaRecord(row[0], row[1], row[2], int(row[3]), row[4]) if row else None

    # -- retention ------------------------------------------------------------------------------

    def prune(self, *, events_before: float, inbound_before: float, media_before: float) -> list[str]:
        """Drop rows older than the cut-offs; returns the media file paths the caller must delete."""
        with self._lock:
            self._conn.execute("DELETE FROM events WHERE created_at < ?", (events_before,))
            self._conn.execute("DELETE FROM inbound WHERE created_at < ?", (inbound_before,))
            paths = [row[0] for row in self._conn.execute(
                "SELECT path FROM media WHERE created_at < ?", (media_before,)).fetchall()]
            self._conn.execute("DELETE FROM media WHERE created_at < ?", (media_before,))
            self._conn.execute("DELETE FROM message_media WHERE media_id NOT IN (SELECT id FROM media)")
        return paths


class EventHub:
    """Wakes the open event streams after an append. ``notify`` is safe from any thread: the agent
    thread appends progress events while the streams wait on the gateway's event loop."""

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._subscribers: set[asyncio.Event] = set()
        self.closed = False

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop, self.closed = loop, False

    def subscribe(self) -> asyncio.Event:
        wakeup = asyncio.Event()
        self._subscribers.add(wakeup)
        return wakeup

    def unsubscribe(self, wakeup: asyncio.Event) -> None:
        self._subscribers.discard(wakeup)

    def notify(self) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._wake()
            return
        try:
            loop.call_soon_threadsafe(self._wake)
        except RuntimeError:  # the loop closed between the check and the call: nobody to wake
            pass

    def close(self) -> None:
        self.closed = True
        self.notify()

    def _wake(self) -> None:
        for wakeup in list(self._subscribers):
            wakeup.set()
