"""Lazurio overlay: a turn's commands see the LAZURIO_BUDDY_CHANNEL of the ACP viewer that submitted it.

The owner side: the field a viewer sends with ``prompt.submit`` is committed with that admission and
bound only while that admission's own turn runs, as the session-context variable the env bridge
exports to the turn's commands. Every other binding (a messaging platform's turn, another viewer's,
a task the turn starts that binds a session of its own) leaves it unset, so the bridge strips it,
also when the gateway's own environment has it. Overlay row: ``acp: a turn's commands see the
LAZURIO_BUDDY_CHANNEL of the ACP viewer that submitted it`` (docs/operations/lazurio-fork-release.md).
"""
import asyncio
import contextvars
import threading
from types import SimpleNamespace

import pytest

import gateway.session_context as sc
from gateway.lazurio_buddy_channel import (
    CAPABILITY, ENV, PAYLOAD_KEY, SUBMIT_KEY, admission_scope, admit, bind_turn, channel,
)
from gateway.session_context import _VAR_MAP, clear_session_vars, reset_session_vars, set_session_vars
from tools.environments.local import _make_run_env, hermes_subprocess_env


@pytest.fixture(autouse=True)
def _isolate_session_context(monkeypatch):
    """A clean ContextVar and engaged-latch slate per test, restored afterwards."""
    saved_ctx = {name: var.get() for name, var in _VAR_MAP.items()}
    saved_engaged = sc._session_context_engaged
    for var in _VAR_MAP.values():
        var.set(sc._UNSET)
    sc._session_context_engaged = False
    monkeypatch.delenv(ENV, raising=False)
    try:
        yield
    finally:
        for name, value in saved_ctx.items():
            _VAR_MAP[name].set(value)
        sc._session_context_engaged = saved_engaged


def _viewer_turn(admission_id="adm-1", value="t3"):
    """What the gateway does for an admission's own turn: reset at handler entry, the turn's session
    variables, then the overlay's bind (``_hmwa_prepare_turn``)."""
    reset_session_vars()
    tokens = set_session_vars(platform="local", source="acp", session_key="acp-session", session_id="s")
    with admission_scope(admission_id, value):
        bind_turn(SimpleNamespace(message_id=admission_id))
    return tokens


@pytest.mark.parametrize("value", ["t3", "web", "a", "x" * 32, "buddy_2", "chat-1", "0"])
def test_a_channel_token_is_committed(value):
    assert channel(value) == value
    assert admit({SUBMIT_KEY: value}) == {PAYLOAD_KEY: value}


@pytest.mark.parametrize("value", ["", "T3", " t3", "t3 ", "-t3", "_t3", "x" * 33, "t3\nx", "t3;rm",
                                   "kanál", 3, True, ["t3"], {"t3": 1}])
def test_anything_else_is_refused_at_admission(value):
    from hermes_state_runtime import RuntimeStoreError
    assert channel(value) is None
    with pytest.raises(RuntimeStoreError, match="invalid_params"):
        admit({SUBMIT_KEY: value})


def test_without_the_field_nothing_is_committed():
    assert admit({}) == {} and admit({SUBMIT_KEY: None}) == {}


def test_a_local_sessions_claim_takes_the_committed_channel_as_the_viewers_own_input():
    # A local session's drain authorizes every head against the fields admission commits
    # (``check_local_input``), and the turn is a viewer's (``viewer_turn``) only for those fields.
    from gateway.session_operator import check_local_input, is_viewer_input
    from gateway.session_queued_controls import _TEXT_ONLY
    row = {"request_id": "r", "principal_id": "owner", "payload": {"text": "x", PAYLOAD_KEY: "t3"}}
    check_local_input(SimpleNamespace(profile_id="p"), SimpleNamespace(session_id="s"), row,
                      source=SimpleNamespace(user_id="owner"))
    assert is_viewer_input(row)
    # A queued row's Steer would hand its text alone to another running turn and drop the channel, so
    # the owner offers no Steer or Edit for it, as for any row a steer cannot carry whole.
    assert PAYLOAD_KEY not in _TEXT_ONLY


def test_the_bridge_and_every_strip_site_know_the_variable():
    # The env bridge, the gateway daemon's start, Kanban dispatch and bot-relay delivery children
    # all iterate _VAR_MAP; the shared bash snapshot excludes the same names.
    import re
    from tools.environments.base_session_env import _SNAPSHOT_EXCLUDED_ENV_REGEX
    assert ENV in _VAR_MAP
    assert re.search(_SNAPSHOT_EXCLUDED_ENV_REGEX, f'declare -x {ENV}="t3"')


def test_the_viewers_own_turn_exports_it_to_its_commands():
    tokens = _viewer_turn()
    try:
        # Foreground terminal commands, background commands and other spawned children.
        from tools.process_registry import ProcessRegistry
        assert _make_run_env({}).get(ENV) == "t3"
        assert ProcessRegistry._spawn_env({}).get(ENV) == "t3"
        assert hermes_subprocess_env().get(ENV) == "t3"
    finally:
        clear_session_vars(tokens)
    assert ENV not in _make_run_env({}), "the turn's end clears it"


def test_only_the_executing_admissions_own_turn_binds_it():
    set_session_vars(platform="local", source="acp")
    with admission_scope("adm-1", "t3"):
        bind_turn(SimpleNamespace(message_id="another-turn"))  # a notification, a goal continuation
        assert ENV not in _make_run_env({})
        bind_turn(SimpleNamespace(message_id=None))
        assert ENV not in _make_run_env({})
    bind_turn(SimpleNamespace(message_id="adm-1"))  # after its admission finished
    assert ENV not in _make_run_env({})
    with admission_scope("adm-2", None):  # an admission without a channel
        bind_turn(SimpleNamespace(message_id="adm-2"))
    with admission_scope("adm-3", "NOT A TOKEN"):  # never committed, refused even if it were
        bind_turn(SimpleNamespace(message_id="adm-3"))
    assert ENV not in _make_run_env({})


def test_a_platform_turn_never_carries_it_even_from_the_gateways_environment(monkeypatch):
    monkeypatch.setenv(ENV, "t3")  # the gateway process itself was started with it
    tokens = set_session_vars(platform="telegram", chat_id="123", user_id="u", session_key="tg")
    try:
        assert ENV not in _make_run_env({})
        assert ENV not in hermes_subprocess_env()
    finally:
        clear_session_vars(tokens)
    assert ENV not in _make_run_env({})


def test_a_platform_turn_after_a_viewers_turn_in_the_same_context_does_not_inherit_it(monkeypatch):
    monkeypatch.setenv(ENV, "t3")
    _viewer_turn()
    assert _make_run_env({}).get(ENV) == "t3"
    # The next message on this task: the handler's entry reset, then its own binding.
    reset_session_vars()
    set_session_vars(platform="telegram", chat_id="123", session_key="tg")
    assert ENV not in _make_run_env({})
    # A binding without the handler's reset (Kanban, cron, a side task) names no channel either.
    _viewer_turn()
    set_session_vars(session_key="kanban-card", session_id="k", source="kanban")
    assert ENV not in _make_run_env({})


def test_an_unengaged_cli_keeps_its_own_environment(monkeypatch):
    # A plain CLI never binds a session context: its commands inherit what its shell set.
    monkeypatch.setenv(ENV, "t3")
    assert _make_run_env({}).get(ENV) == "t3"


@pytest.mark.asyncio
async def test_work_a_turn_starts_behaves_like_the_other_session_variables():
    tokens = _viewer_turn()
    try:
        # A subagent runs in a copy of the turn's context (delegate_task): it carries the turn's
        # session variables, the channel among them, also after the turn has ended.
        copied = contextvars.copy_context()
        child = {}
        thread = threading.Thread(target=copied.run, args=(lambda: child.update(_make_run_env({})),))

        # A task that binds a session of its own (Kanban, cron) replaces the turn's variables.
        async def kanban_run():
            set_session_vars(session_key="kanban-card", session_id="k", source="kanban")
            return _make_run_env({})
        kanban = await asyncio.create_task(kanban_run())
    finally:
        clear_session_vars(tokens)
    thread.start()
    thread.join()
    assert (child.get(ENV), child.get("HERMES_SESSION_KEY")) == ("t3", "acp-session")
    assert ENV not in kanban and kanban.get("HERMES_SESSION_KEY") == "kanban-card"


async def _authority(tmp_path, monkeypatch, answer):
    from gateway.config import GatewayConfig, Platform
    from gateway.session import SessionSource, SessionStore
    from gateway.session_authority import LiveSession, initialize_session_authority

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    runner = SimpleNamespace(_session_db=store._db, session_store=store, _draining=False,
                             _handle_message=answer, _adapter_for_source=lambda source: None)
    authority = await initialize_session_authority(runner, profile_id="p", instance_id="owner")
    # The authority fixture of tests/gateway/test_surface_admission.py: a session any attached viewer
    # submits to. The binding is per admission, whatever the session's platform.
    store._db.create_session("s", source="telegram")
    authority.sessions["s"] = LiveSession(SessionSource(platform=Platform.TELEGRAM, chat_id="c"), "s")
    return authority


@pytest.mark.asyncio
async def test_the_field_is_committed_with_its_admission_and_bound_for_that_turn_only(tmp_path, monkeypatch):
    from gateway.session_controls import AuthorityConnection
    from hermes_state_runtime import list_session_admissions

    monkeypatch.setenv(ENV, "t3")  # also in the gateway's own environment: only the binding counts
    seen = []

    async def answer(event):
        # The gateway's turn (``_hm_admit_event`` reset, ``_set_session_env``, the overlay's bind).
        reset_session_vars()
        tokens = set_session_vars(platform="local", source="acp", session_key="s")
        bind_turn(event)
        try:
            seen.append((event.text, _make_run_env({}).get(ENV)))
        finally:
            clear_session_vars(tokens)
        return "ok"

    authority = await _authority(tmp_path, monkeypatch, answer)
    connection = AuthorityConnection(authority, object(), {"user_id": "owner"})
    try:
        described = await connection.dispatch({"id": 0, "method": "runtime.describe", "params": {}})
        assert CAPABILITY in described["result"]["capabilities"]
        await connection.dispatch({"id": 1, "method": "session.resume", "params": {"session_id": "s"}})
        for rid, extra in (("chat", {SUBMIT_KEY: "t3"}), ("other-viewer", {}), ("chat-again", {SUBMIT_KEY: "t3"})):
            reply = await connection.dispatch({"id": 2, "method": "prompt.submit", "params": {
                "session_id": "s", "input_id": rid, "text": rid, **extra}})
            assert reply.get("result", {}).get("status") == "queued", reply
            await authority.sessions["s"].task
        for bad in ("T3", "", 7):
            refused = await connection.dispatch({"id": 3, "method": "prompt.submit", "params": {
                "session_id": "s", "input_id": f"bad-{bad!r}", "text": "x", SUBMIT_KEY: bad}})
            assert refused["error"]["message"] == "invalid_params", refused
    finally:
        await connection.close()

    rows = {row["request_id"]: row for row in list_session_admissions(authority.db, session_id="s", pending_only=False)}
    assert rows["chat"]["payload"][PAYLOAD_KEY] == "t3"
    assert PAYLOAD_KEY not in rows["other-viewer"]["payload"]
    assert set(rows) == {"chat", "other-viewer", "chat-again"}, "a refused value admits nothing"
    assert seen == [("chat", "t3"), ("other-viewer", None), ("chat-again", "t3")]
