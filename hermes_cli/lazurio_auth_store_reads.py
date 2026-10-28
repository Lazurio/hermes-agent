"""Lazurio overlay: auth store reads that no save follows leave the disk as they found it.

``_load_auth_store`` (``hermes_cli/auth.py``) copies a store it cannot parse to
``auth.json.corrupt`` before it reads it as empty, because the writer that read it saves next and
would overwrite the only copy of its credentials. A caller that only reads saves nothing: the file
stays where it is, and the next writer to read it keeps the copy. ``read_only=True`` credential
reads still went through that copy, so a status-like read wrote credential material to disk.

Inside ``reads_only()`` a store that cannot be parsed reads as empty without the copy, on every
path the read takes, including the credential pool's global-root fallback. Writers keep upstream's
recovery. The Codex entitlement (``gateway/lazurio_codex_entitlement.py``) reads the stored sign-in
inside it.

Overlay row ``oauth: whether the signed-in ChatGPT account may run the configured model``
(docs/operations/lazurio-fork-release.md). Removal: with that row, or when upstream's read-only
credential reads write nothing.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

_reads_only: ContextVar[bool] = ContextVar("lazurio_auth_store_reads_only", default=False)


@contextmanager
def reads_only() -> Iterator[None]:
    """Every auth store read inside the block leaves the disk as it is."""
    token = _reads_only.set(True)
    try:
        yield
    finally:
        _reads_only.reset(token)


def reading_only() -> bool:
    """Whether the current read runs inside ``reads_only()``."""
    return _reads_only.get()
