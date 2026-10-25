"""Lazurio overlay: a sign-in the 15-minute sweep removes can no longer save its tokens.

``_gc_oauth_sessions`` (run by every ``/start``) forgets the sessions created more than 15 minutes
ago. The OpenAI Codex worker's own poll deadline starts only once its user code has arrived, so its
last poll or token exchange can still be in flight then. Forgotten without ``cancelled``, the session
answered poll 404 and DELETE ``{"ok": false, "message": "session not found"}`` while the worker went
on to ``_save_codex_tokens``: a client that takes "not found" as the end of the sign-in (Lazurio's
Launchpad, LazurioPlatform#635) could sign out or start another sign-in and then be overwritten. A
fake OpenAI holds the worker at that exact step (or another request holds the profile scope the
worker saves in), a real ``/start`` sweeps, and the worker continues.
Overlay row: ``oauth: a sign-in the 15-minute sweep removes can no longer save its tokens``
(docs/operations/lazurio-fork-release.md).

A stopping gateway answers "not found" too, through its successor, once it has released its runtime
lock, so the release first cancels every sign-in still running (``gateway/lazurio_oauth_contract.py``).
Overlay row: ``oauth: identify names the sign-in cancel contract, and a stopping gateway keeps it``.
"""
import contextlib
import threading

import httpx
import pytest
from fastapi.testclient import TestClient

from hermes_cli.web_routers import oauth
from hermes_cli.web_server import _SESSION_TOKEN, app
import hermes_cli.web_server_oauth as oauth_state

client = TestClient(app)
HEADERS = {"X-Hermes-Session-Token": _SESSION_TOKEN}
TOKENS = {"access_token": "at", "refresh_token": "rt"}


class Response:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class OpenAI:
    """OpenAI's device-login endpoints as the worker reaches them. *hold* names the step whose request
    blocks until ``release``: the last ``poll`` (the one that brings the authorization) or the token
    ``exchange``."""

    def __init__(self, hold=None):
        self.hold, self.held, self.release = hold, threading.Event(), threading.Event()

    def answer(self, url):
        from hermes_cli.auth import CODEX_OAUTH_TOKEN_URL
        step = {url.endswith("/deviceauth/usercode"): "usercode", url.endswith("/deviceauth/token"): "poll",
                url == CODEX_OAUTH_TOKEN_URL: "exchange"}.get(True)
        assert step, url
        if step == self.hold:
            self.held.set()
            assert self.release.wait(10), "the test never released the worker"
        return Response({"usercode": {"device_auth_id": "device-auth-id", "interval": 3, "user_code": "CODEX-1234"},
                         "poll": {"authorization_code": "auth-code", "code_verifier": "verifier"},
                         "exchange": TOKENS}[step])

    def client(self):
        openai = self

        class Client:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def post(self, url, **kwargs):
                return openai.answer(url)
        return Client


class ProfileScope:
    """Another request working in a profile scope: it holds the process-wide lock behind
    ``_profile_scope``, which the worker waits on with no timeout before its save, until ``release``.
    ``held`` is set once the worker has its tokens and waits there."""

    def __init__(self, monkeypatch):
        from hermes_cli import web_server_profiles
        self.held, self.release, locked = threading.Event(), threading.Event(), threading.Event()

        def other_request():
            with web_server_profiles._SKILLS_PROFILE_LOCK:
                locked.set()
                assert self.release.wait(10), "the test never released the worker"

        @contextlib.contextmanager
        def worker_scope(profile):
            self.held.set()
            with web_server_profiles._profile_scope(profile) as scoped:
                yield scoped

        threading.Thread(target=other_request, daemon=True).start()
        assert locked.wait(10)
        monkeypatch.setattr(oauth, "_profile_scope", worker_scope)


@pytest.fixture
def sign_in(monkeypatch):
    """``(start, saved)``: ``start(hold)`` runs a dashboard Codex sign-in's worker in a thread against a
    fake OpenAI and returns ``(session id, session dict, gate, worker)``. *gate* (``held``, ``release``)
    is the ``OpenAI`` holding the step *hold* names (its last ``poll`` or the token ``exchange``), or
    for ``"profile scope"`` the ``ProfileScope`` the worker waits for. *saved* lists what the worker
    handed to ``_save_codex_tokens``."""
    from hermes_cli import auth
    monkeypatch.setattr(oauth.time, "sleep", lambda *_a, **_k: None)  # the poll interval
    saved = []
    monkeypatch.setattr(auth, "_save_codex_tokens", saved.append)
    started = []

    def start(hold=None):
        openai = OpenAI(None if hold == "profile scope" else hold)
        monkeypatch.setattr(httpx, "Client", openai.client())
        gate = ProfileScope(monkeypatch) if hold == "profile scope" else openai
        sid, sess = oauth._new_oauth_session("openai-codex", "device_code")
        worker = threading.Thread(target=oauth._codex_full_login_worker, args=(sid,), daemon=True)
        worker.start()
        started.append((sid, gate, worker))
        return sid, sess, gate, worker
    yield start, saved
    for sid, gate, worker in started:
        gate.release.set()
        worker.join(10)
        oauth_state._oauth_sessions.pop(sid, None)


def age(sess):
    """Make *sess* older than the sweep's 15 minutes (its worker's own deadline started later)."""
    sess["created_at"] -= oauth._OAUTH_SESSION_TTL_SECONDS + 1


def sweep():
    """Another ``/start`` from the dashboard: the route sweeps before it even looks at the provider."""
    assert client.post("/api/providers/oauth/not-a-provider/start", headers=HEADERS).status_code == 400


def poll(sid):
    return client.get(f"/api/providers/oauth/openai-codex/poll/{sid}", headers=HEADERS)


def cancel(sid):
    return client.delete(f"/api/providers/oauth/sessions/{sid}", headers=HEADERS).json()


IN_FLIGHT = pytest.mark.parametrize("hold", ["poll", "exchange", "profile scope"], ids=[
    "held at its last poll", "held at the token exchange", "waiting for its profile scope"])


@IN_FLIGHT
def test_a_sign_in_the_sweep_removes_while_its_worker_is_in_flight_saves_nothing(sign_in, hold):
    """Wherever the worker waits before its save (OpenAI's last poll, the token exchange, or its profile
    scope, a process-wide lock it waits on with no timeout), it holds no lock the sweep takes: the sweep
    lands meanwhile, however long the wait, and the worker checks ``cancelled`` only once it holds the
    lock it saves under."""
    start, saved = sign_in
    sid, sess, gate, worker = start(hold)
    assert gate.held.wait(10)
    age(sess)
    sweep()
    # What a client reads now is the end of the sign-in...
    assert poll(sid).status_code == 404
    assert cancel(sid) == {"ok": False, "message": "session not found"}
    assert worker.is_alive()  # still held: nothing above waited for it
    # ...and it is: released, the worker saves nothing.
    gate.release.set()
    worker.join(10)
    assert not worker.is_alive()
    assert saved == []


def test_a_sign_in_cancelled_and_then_swept_saves_nothing_and_stays_ended(sign_in):
    start, saved = sign_in
    sid, sess, openai, worker = start("exchange")
    assert openai.held.wait(10)
    assert cancel(sid) == {"ok": True, "session_id": sid}
    age(sess)
    sweep()
    assert cancel(sid) == {"ok": False, "message": "session not found"}
    openai.release.set()
    worker.join(10)
    assert saved == []


def test_a_sign_in_approved_within_its_15_minutes_still_saves_through_a_sweep(sign_in):
    start, saved = sign_in
    sid, sess, openai, worker = start("exchange")
    assert openai.held.wait(10)
    sweep()  # another sign-in starts meanwhile; this one is younger than 15 minutes
    openai.release.set()
    worker.join(10)
    assert saved == [TOKENS]
    assert poll(sid).json()["status"] == "approved"
    # Swept once old, a finished sign-in is forgotten as before: approved, never relabelled cancelled.
    age(sess)
    sweep()
    assert poll(sid).status_code == 404
    assert sess["status"] == "approved" and not sess.get("cancelled")


def test_a_sweep_landing_during_the_save_waits_for_it(sign_in, monkeypatch):
    """The sweep marks under the lock that the worker's final check and save share, so a sign-in is either
    saved before it is forgotten or cancelled before its save, never forgotten and then saved."""
    from hermes_cli import auth
    start, saved = sign_in
    swept, sweeps = threading.Event(), []

    def save(tokens):
        age(sess)
        sweeps.append(threading.Thread(target=lambda: (sweep(), swept.set()), daemon=True))
        sweeps[0].start()
        saved.append((tokens, swept.wait(0.2)))  # the sweep cannot finish while the save holds the lock

    monkeypatch.setattr(auth, "_save_codex_tokens", save)
    sid, sess, openai, worker = start("exchange")
    assert openai.held.wait(10)
    openai.release.set()
    worker.join(10)
    sweeps[0].join(10)
    assert saved == [(TOKENS, False)]
    assert swept.is_set() and sid not in oauth_state._oauth_sessions
    assert sess["status"] == "approved" and not sess.get("cancelled")


@pytest.mark.parametrize("removal", ["sweep", "cancel", "drop"])
def test_every_removal_of_a_running_sign_in_from_the_map_cancels_it(removal):
    """A worker keeps its own reference to the session dict, so whatever takes the session out of the map
    marks it ``cancelled`` for that worker: the sweep, the DELETE route, and dropping a start that failed."""
    sid, sess = oauth._new_oauth_session("openai-codex", "device_code")
    try:
        if removal == "sweep":
            age(sess)
            sweep()
        elif removal == "cancel":
            assert cancel(sid)["ok"] is True
        else:
            oauth._drop_oauth_session(sid)
        assert sid not in oauth_state._oauth_sessions
        assert sess.get("cancelled") is True
    finally:
        oauth_state._oauth_sessions.pop(sid, None)


@pytest.fixture
def runtime_lock():
    """This process as the gateway of the test's profile: it holds the runtime lock a successor needs.
    Requested before ``sign_in``, so the workers are gone before the teardown releases it."""
    from gateway.status import acquire_gateway_runtime_lock, release_gateway_runtime_lock
    assert acquire_gateway_runtime_lock()
    yield
    release_gateway_runtime_lock()


def successor_can_start():
    """Whether a successor gateway could reserve this profile now, and so answer "not found" for every
    sign-in of this process."""
    from gateway.runtime_ownership import OwnershipConflict, ProfileOwnership
    from hermes_constants import get_hermes_home
    successor = ProfileOwnership()
    try:
        successor.reserve([get_hermes_home()])
    except OwnershipConflict:
        return False
    successor.close()
    return True


@IN_FLIGHT
def test_a_sign_in_still_running_when_its_gateway_releases_the_runtime_lock_saves_nothing(
        runtime_lock, sign_in, hold):
    """Every way out of a gateway ends in ``release_gateway_runtime_lock`` (the stop, an early exit, the exit
    backstop, the shutdown watchdog, atexit), and the daemon worker lives on until the process exits."""
    from gateway.status import release_gateway_runtime_lock
    start, saved = sign_in
    _sid, sess, gate, worker = start(hold)
    assert gate.held.wait(10)
    assert not successor_can_start()
    release_gateway_runtime_lock()
    assert successor_can_start()  # from here on, this sign-in is "not found" to whoever asks the successor
    gate.release.set()
    worker.join(10)
    assert not worker.is_alive()
    assert saved == []
    assert sess["cancelled"] is True


def test_a_save_still_running_at_the_release_keeps_the_runtime_lock_until_it_is_done(
        runtime_lock, sign_in, monkeypatch):
    """A save that already passed its check cannot be cancelled, so the release cannot vouch for it: the
    profile stays reserved, as ``ProfileOwnership.close`` keeps it for a writer still alive, and a later
    release (the exit backstop's) or process exit lets it go."""
    from gateway import lazurio_oauth_contract
    from gateway.status import release_gateway_runtime_lock
    from hermes_cli import auth
    start, saved = sign_in
    saving, done = threading.Event(), threading.Event()

    def save(tokens):
        saving.set()
        assert done.wait(10)
        saved.append(tokens)

    monkeypatch.setattr(auth, "_save_codex_tokens", save)
    monkeypatch.setattr(lazurio_oauth_contract, "SAVE_WAIT_SECONDS", 0.05)
    _sid, sess, _gate, worker = start()
    try:
        assert saving.wait(10)
        release_gateway_runtime_lock()
        assert not successor_can_start()
    finally:
        done.set()
        worker.join(10)
    assert saved == [TOKENS]  # saved while this process still held the profile
    release_gateway_runtime_lock()
    assert successor_can_start()
    assert sess["status"] == "approved" and not sess.get("cancelled")  # a finished sign-in keeps its status
