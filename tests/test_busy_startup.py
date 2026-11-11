"""A second Gmail server process holding collect.lock must not leave this one
signed out.

Casa starts this server more than once at the same moment: a session's own
server, and a short-lived one that only lists the tools. Before 0.12.1 the
loser of that race skipped credential loading at start-up and nothing ever
retried, so every Gmail tool said "not authenticated" for the life of the
process while a good credential sat on disk.

The lock here is held by a REAL second process (fcntl.flock is per open file
description, and the defect is between processes), the store is the real
TokenStore, and the auth is a real GmailAuth with only the calls that would
reach Google replaced.
"""
import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

SUBJECT = "user@example.com"
FLOW = "a" * 64

_HOLDER = r"""
import fcntl, os, sys
fd = os.open(os.path.join(sys.argv[1], "collect.lock"), os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
print("held", flush=True)
sys.stdin.readline()
"""


@contextmanager
def lock_held_by_another_process(directory):
    """Yields a `release()`; the lock is held from entry until it is called
    (or the block ends)."""
    child = subprocess.Popen([sys.executable, "-c", _HOLDER, str(directory)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             text=True)
    assert child.stdout.readline().strip() == "held"

    def release():
        if child.poll() is None:
            child.stdin.write("\n")
            child.stdin.flush()
            child.wait(timeout=10)

    try:
        yield release
    finally:
        release()


class _Refresh:
    """Stands in for the token endpoint: counts calls and raises the queued
    failures first."""

    def __init__(self, *failures):
        self.calls = 0
        self.failures = list(failures)

    def __call__(self, refresh_token):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return MagicMock(name="live-credentials")


@pytest.fixture
def world(monkeypatch, tmp_path):
    """A fresh server module state over a real store in `tmp_path`."""
    import server
    from auth import GmailAuth

    for key, val in {"GMAIL_CLIENT_ID": "client-id",
                     "GMAIL_CLIENT_SECRET": "client-secret",
                     "GMAIL_USER_EMAIL": SUBJECT}.items():
        monkeypatch.setenv(key, val)
    auth = GmailAuth(str(tmp_path))
    auth.on_activate = server._rebuild_runtime
    refresh = _Refresh()
    monkeypatch.setattr(auth, "_refresh", refresh)
    monkeypatch.setattr(server, "_auth", auth)
    monkeypatch.setattr(server, "PLUGIN_DATA", str(tmp_path))
    monkeypatch.setattr(server, "_authenticated", False)
    monkeypatch.setattr(server, "_client", None)
    monkeypatch.setattr(server, "_att", None)
    monkeypatch.setattr(server, "_log", None)
    client = MagicMock()
    client.search_emails.return_value = [{"id": "m1"}]
    monkeypatch.setattr(server, "GmailClient", MagicMock(return_value=client))
    monkeypatch.setattr(server, "AttachmentManager", MagicMock())
    monkeypatch.setattr(server, "SentLog", MagicMock())
    cb = MagicMock()
    cb.attempts.return_value = []
    cb.pending_mint_times.return_value = []
    monkeypatch.setattr(server, "_cb", cb)
    mints = []
    monkeypatch.setattr(server, "_flow_start",
                        lambda a, c: mints.append(1) or {"auth_url": "https://g/x",
                                                         "redirect_uri": "https://r",
                                                         "instructions": "open it"})

    class World:
        pass
    w = World()
    w.server, w.auth, w.refresh, w.cb, w.mints, w.dir = (
        server, auth, refresh, cb, mints, tmp_path)
    return w


def _active(world):
    from token_store import Credential
    world.auth.store.write_active(Credential(
        refresh_token="rt-active", flow=FLOW, generation=1.0, account=SUBJECT))


def test_a_busy_lock_at_start_up_no_longer_leaves_the_process_signed_out(world):
    """THE reproduction: another process holds the lock while this one starts,
    then lets go. The first Gmail call must work."""
    _active(world)
    with lock_held_by_another_process(world.dir) as release:
        world.server._startup()
        release()

    assert json.loads(world.server.search_emails("from:me")) == [{"id": "m1"}]
    assert world.server._authenticated is True
    assert world.refresh.calls == 1


def test_start_up_does_no_credential_work(world):
    """Casa starts a server just to list its tools; that start must not wait on
    Google, and must not take the lock a real session's server needs."""
    _active(world)
    world.server._startup()

    assert world.refresh.calls == 0
    assert world.server._authenticated is False
    with lock_held_by_another_process(world.dir):
        pass                                   # the lock was free to take


def test_start_up_still_refuses_a_missing_setting(world, monkeypatch):
    monkeypatch.delenv("GMAIL_CLIENT_ID")
    with pytest.raises(SystemExit):
        world.server._startup()


def test_a_call_while_the_lock_stays_held_says_gmail_is_starting(world, monkeypatch):
    """Past the bounded wait the call fails, but it must not send the agent to
    sign in again, and the next call after the holder lets go must work."""
    _active(world)
    monkeypatch.setattr(world.server, "RECOVERY_WAIT_S", 0.3)
    with lock_held_by_another_process(world.dir):
        world.server._startup()
        with pytest.raises(ValueError) as caught:
            world.server.search_emails("from:me")
    assert "still starting up" in str(caught.value)
    assert "setup_gmail" not in str(caught.value)

    assert json.loads(world.server.search_emails("from:me")) == [{"id": "m1"}]


def test_a_call_waits_for_the_other_process_to_let_go(world):
    """The holder is another process's recovery: a refresh or two. The call
    that needs the credential waits it out instead of failing."""
    import threading
    _active(world)
    with lock_held_by_another_process(world.dir) as release:
        world.server._startup()
        threading.Timer(0.3, release).start()
        assert json.loads(world.server.search_emails("from:me")) == [{"id": "m1"}]


def test_one_transient_refresh_failure_heals_at_the_next_call(world):
    from auth import RefreshRetryable
    _active(world)
    world.refresh.failures.append(RefreshRetryable("temporarily_unavailable"))
    world.server._startup()

    with pytest.raises(ValueError) as caught:
        world.server.search_emails("from:me")
    assert "authorization link" not in str(caught.value)
    assert world.auth.store.load_active() is not None          # kept

    assert json.loads(world.server.search_emails("from:me")) == [{"id": "m1"}]
    assert world.refresh.calls == 2


def test_recovery_stops_once_the_process_is_in_service(world, monkeypatch):
    """Once in service, a call does no store, lock or token work: a second
    activation would swap the live client for nothing."""
    _active(world)
    world.server._startup()
    world.server.search_emails("from:me")
    calls = []
    real = world.server._flow_recover
    monkeypatch.setattr(world.server, "_flow_recover",
                        lambda *a, **k: calls.append(1) or real(*a, **k))

    world.server.search_emails("from:me")
    world.server.search_emails("from:me")

    assert calls == []
    assert world.refresh.calls == 1
    assert world.server.AttachmentManager.call_count == 1


def test_setup_mints_nothing_while_another_process_holds_a_pending_stage(world, monkeypatch):
    """A completed sign-in waits in the staged slot and another process holds
    the lock. Nothing shows that a new sign-in is needed."""
    world.auth.store.stage("rt-staged", FLOW, time.time())
    monkeypatch.setattr(world.server, "RECOVERY_WAIT_S", 0.3)
    with lock_held_by_another_process(world.dir):
        world.server._startup()
        result = json.loads(world.server.setup_gmail())

    assert world.mints == []
    assert result["status"] == "retry_later"
    assert result["auth_url"] is None


def test_setup_mints_nothing_when_a_pending_stage_cannot_be_verified_yet(world, monkeypatch):
    from auth import RefreshRetryable
    world.auth.store.stage("rt-staged", FLOW, time.time())

    def unverifiable(refresh_token):
        raise RefreshRetryable("temporarily_unavailable")
    monkeypatch.setattr(world.auth, "refresh_and_verify", unverifiable)
    world.server._startup()

    result = json.loads(world.server.setup_gmail())

    assert world.mints == []
    assert result["status"] == "retry_later"
    assert world.auth.store.load_staged() is not None          # kept


def test_setup_after_a_busy_start_reports_the_credential_on_disk(world):
    _active(world)
    with lock_held_by_another_process(world.dir) as release:
        world.server._startup()
        release()

    result = json.loads(world.server.setup_gmail())

    assert result["status"] == "already_connected"
    assert world.mints == []
    assert world.server._authenticated is True


def test_a_crash_left_sign_in_collected_first_is_kept_as_a_notice(world, monkeypatch):
    """A previous process staged a good sign-in and died. If this process's
    first call is gmail_auth_collect, the outcome must be written down before
    Casa's attempt is acknowledged, as start-up recovery did, so a crash
    before the reply cannot lose it."""
    world.auth.store.stage("rt-staged", FLOW, time.time())
    monkeypatch.setattr(world.auth, "refresh_and_verify", lambda rt: SUBJECT)
    world.server._startup()

    out = json.loads(world.server.gmail_auth_collect())

    connected = [m for m in out["messages"] if m.startswith("Gmail connected as")]
    assert connected == [f"Gmail connected as {SUBJECT}."]
    assert world.auth.store.peek_notices() == [f"Gmail connected as {SUBJECT}."]
    world.cb.ack.assert_called_once_with(FLOW)
    assert world.server._authenticated is True


def test_the_lock_is_taken_once_the_other_process_lets_go_within_the_wait(tmp_path):
    import threading
    from auth_flow import collect_lock
    with lock_held_by_another_process(tmp_path) as release:
        threading.Timer(0.3, release).start()
        started = time.monotonic()
        with collect_lock(tmp_path, wait=5.0) as acquired:
            waited = time.monotonic() - started
    assert acquired is True
    assert 0.25 <= waited < 5.0


def test_without_a_wait_the_lock_is_tried_once(tmp_path):
    from auth_flow import collect_lock
    with lock_held_by_another_process(tmp_path):
        started = time.monotonic()
        with collect_lock(tmp_path) as acquired:
            waited = time.monotonic() - started
    assert acquired is False
    assert waited < 0.1


def test_a_wait_gives_up_while_the_other_process_still_holds_the_lock(tmp_path):
    from auth_flow import collect_lock
    with lock_held_by_another_process(tmp_path):
        started = time.monotonic()
        with collect_lock(tmp_path, wait=0.3) as acquired:
            waited = time.monotonic() - started
    assert acquired is False
    assert 0.3 <= waited < 1.0


def test_setup_mints_nothing_while_a_newer_sign_in_waits_behind_a_revoked_one(world, monkeypatch):
    """Recovery brings the old credential into service, but the completed
    sign-in waiting in the staged slot cannot be confirmed yet. The old one
    then turns out to be revoked. The waiting sign-in may well be good, so
    nothing shows that another is needed."""
    from auth import RefreshRetryable, RefreshTerminal
    _active(world)
    world.auth.store.stage("rt-staged", "b" * 64, time.time())

    def unverifiable(refresh_token):
        raise RefreshRetryable("temporarily_unavailable")

    def revoked(refresh_token):
        raise RefreshTerminal("invalid_grant")
    monkeypatch.setattr(world.auth, "refresh_and_verify", unverifiable)
    monkeypatch.setattr(world.auth, "probe_refresh", revoked)
    world.server._startup()

    result = json.loads(world.server.setup_gmail())

    assert world.mints == []
    assert result["status"] == "retry_later"
    assert world.auth.store.load_staged() is not None          # kept
