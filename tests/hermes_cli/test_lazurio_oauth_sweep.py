"""Lazurio overlay: a sign-in the 15-minute sweep removes can no longer save its tokens.

``_gc_oauth_sessions`` (run by every ``/start``) forgets the sessions created more than 15 minutes
ago. The OpenAI Codex worker's own poll deadline starts only once its user code has arrived, so its
last poll or token exchange can still be in flight then. Forgotten without ``cancelled``, the session
answered poll 404 and DELETE ``{"ok": false, "message": "session not found"}`` while the worker went
on to ``_save_codex_tokens``: a client that takes "not found" as the end of the sign-in (Lazurio's
Launchpad, LazurioPlatform#635) could sign out or start another sign-in and then be overwritten. A
fake OpenAI holds the worker at that exact step, a real ``/start`` sweeps, and the worker continues.
Overlay row: ``oauth: a sign-in the 15-minute sweep removes can no longer save its tokens``
(docs/operations/lazurio-fork-release.md).
"""
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


@pytest.fixture
def sign_in(monkeypatch):
    """``(start, saved)``: ``start(hold)`` runs a dashboard Codex sign-in's worker in a thread against an
    ``OpenAI(hold)`` and returns ``(session id, session dict, openai, worker)``; *saved* lists what the
    worker handed to ``_save_codex_tokens``."""
    from hermes_cli import auth
    monkeypatch.setattr(oauth.time, "sleep", lambda *_a, **_k: None)  # the poll interval
    saved = []
    monkeypatch.setattr(auth, "_save_codex_tokens", saved.append)
    started = []

    def start(hold=None):
        openai = OpenAI(hold)
        monkeypatch.setattr(httpx, "Client", openai.client())
        sid, sess = oauth._new_oauth_session("openai-codex", "device_code")
        worker = threading.Thread(target=oauth._codex_full_login_worker, args=(sid,), daemon=True)
        worker.start()
        started.append((sid, openai, worker))
        return sid, sess, openai, worker
    yield start, saved
    for sid, openai, worker in started:
        openai.release.set()
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


@pytest.mark.parametrize("hold", ["poll", "exchange"], ids=["held at its last poll", "held at the token exchange"])
def test_a_sign_in_the_sweep_removes_while_its_worker_is_in_flight_saves_nothing(sign_in, hold):
    start, saved = sign_in
    sid, sess, openai, worker = start(hold)
    assert openai.held.wait(10)
    age(sess)
    sweep()
    # What a client reads now is the end of the sign-in...
    assert poll(sid).status_code == 404
    assert cancel(sid) == {"ok": False, "message": "session not found"}
    # ...and it is: released, the worker saves nothing.
    openai.release.set()
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
