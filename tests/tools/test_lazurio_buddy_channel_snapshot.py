"""Lazurio overlay: the shared bash snapshot never keeps an ACP viewer's LAZURIO_BUDDY_CHANNEL.

One long-lived terminal backend serves every session of the gateway and ``source``s one snapshot
before each command (``tests/tools/test_snapshot_session_id_leak.py``). The channel the gateway
bridges onto the commands of a viewer's turn, or one a command exports itself, must not land in that
snapshot: every later session, Telegram's included, would source it and report to the chat. Overlay
row: ``acp: a turn's commands see the LAZURIO_BUDDY_CHANNEL of the ACP viewer that submitted it``
(docs/operations/lazurio-fork-release.md).
"""
import os
import threading
from types import SimpleNamespace

import pytest

import gateway.session_context as sc
from gateway.lazurio_buddy_channel import ENV, admission_scope, bind_turn
from gateway.session_context import _UNSET, _VAR_MAP, set_session_vars

PROBE = f'printf "[%s]" "${{{ENV}-absent}}"'


@pytest.fixture(autouse=True)
def _isolate_session_context():
    saved = {name: var.get() for name, var in _VAR_MAP.items()}
    engaged = sc._session_context_engaged
    try:
        yield
    finally:
        for name, value in saved.items():
            _VAR_MAP[name].set(value)
        sc._session_context_engaged = engaged


def _run_as(env, *, viewer, command=PROBE):
    """``command`` in a fresh turn context: the viewer's own turn (channel bound) or a platform turn."""
    out = {}

    def worker():
        for var in _VAR_MAP.values():
            var.set(_UNSET)
        if viewer:
            set_session_vars(platform="local", source="acp", session_key="acp", session_id="acp-s")
            with admission_scope("adm", "t3"):
                bind_turn(SimpleNamespace(message_id="adm"))
        else:
            set_session_vars(platform="telegram", chat_id="1", session_key="tg", session_id="tg-s")
        out["result"] = env.execute(command)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    return out["result"].get("output", "")


@pytest.mark.platforms("posix")  # POSIX bash snapshot path
def test_a_viewers_channel_never_reaches_a_later_session_through_the_snapshot(tmp_path, monkeypatch):
    from tools.environments.local import LocalEnvironment

    # The gateway's own environment has it too (started from the viewer's shell, say), and the
    # snapshot is taken before any session engaged the bridge: the login shell sees it.
    monkeypatch.setenv(ENV, "t3")
    sc._session_context_engaged = False
    env = LocalEnvironment(cwd=str(tmp_path), timeout=30)
    env.init_session()
    try:
        assert "[t3]" in _run_as(env, viewer=True)
        # A command of the viewer's turn that exports it itself does not persist it either.
        assert "[leaked]" in _run_as(env, viewer=True, command=f"export {ENV}=leaked; {PROBE}")
        assert "[absent]" in _run_as(env, viewer=False), "a later platform turn sourced the viewer's channel"
        assert "[t3]" in _run_as(env, viewer=True)
        snapshot = env._snapshot_path
        assert os.path.exists(snapshot)
        with open(snapshot) as f:
            assert ENV not in f.read()
    finally:
        env.cleanup()
