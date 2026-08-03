# tests/test_auth_flow.py
import pytest
from unittest.mock import MagicMock


def test_parse_returns_code():
    from auth_flow import parse_callback_query
    assert parse_callback_query([["code", "c1"], ["scope", "gmail"]]) == ("c1", None)


def test_parse_returns_error():
    from auth_flow import parse_callback_query
    assert parse_callback_query([["error", "access_denied"]]) == (None, "access_denied")


@pytest.mark.parametrize("pairs", [
    [["code", "c1"], ["code", "c2"]],
    [["error", "e1"], ["error", "e2"]],
])
def test_parse_rejects_duplicates(pairs):
    from auth_flow import MalformedCallback, parse_callback_query
    with pytest.raises(MalformedCallback):
        parse_callback_query(pairs)


def test_parse_rejects_both_code_and_error():
    from auth_flow import MalformedCallback, parse_callback_query
    with pytest.raises(MalformedCallback):
        parse_callback_query([["code", "c1"], ["error", "access_denied"]])


def test_parse_rejects_neither():
    from auth_flow import MalformedCallback, parse_callback_query
    with pytest.raises(MalformedCallback):
        parse_callback_query([["scope", "gmail"]])


@pytest.mark.parametrize("pairs", [[["code", ""]], [["error", ""]]])
def test_parse_rejects_empty_value(pairs):
    from auth_flow import MalformedCallback, parse_callback_query
    with pytest.raises(MalformedCallback):
        parse_callback_query(pairs)


def test_parse_rejects_non_pair_shapes():
    from auth_flow import MalformedCallback, parse_callback_query
    with pytest.raises(MalformedCallback):
        parse_callback_query("code=c1")


def test_lock_is_exclusive_across_holders(tmp_path):
    from auth_flow import collect_lock
    with collect_lock(tmp_path) as first:
        assert first is True
        with collect_lock(tmp_path) as second:
            assert second is False


def test_lock_is_reacquirable_after_release(tmp_path):
    from auth_flow import collect_lock
    with collect_lock(tmp_path) as a:
        assert a is True
    with collect_lock(tmp_path) as b:
        assert b is True


def fake_route(redirect_uri="https://casa.example.com/callback/plg-gmail--oauth"):
    from casa_callback import Route
    from pathlib import Path
    return Route(Path("/spool/gmail"), "plg-gmail--oauth", redirect_uri)


def test_start_mints_a_state_and_returns_the_url():
    from auth_flow import start
    cb, auth = MagicMock(), MagicMock()
    cb.resolve.return_value = fake_route()
    auth.build_auth_url.side_effect = lambda uri, state: f"https://accounts.google.com/o?state={state}"

    out = start(auth, cb)

    minted_state = cb.mint.call_args[0][0]
    assert len(minted_state) >= 32
    assert minted_state in out["auth_url"]
    assert out["redirect_uri"] == "https://casa.example.com/callback/plg-gmail--oauth"


def test_start_mints_non_bearer_meta():
    """meta inherits the attempt's retention — no secrets in it."""
    from auth_flow import start
    cb, auth = MagicMock(), MagicMock()
    cb.resolve.return_value = fake_route()
    auth.build_auth_url.return_value = "https://accounts.google.com/o"

    start(auth, cb)
    assert cb.mint.call_args[0][1] == {"kind": "gmail-oauth", "v": 1}


def test_start_uses_the_redirect_uri_from_casa_verbatim():
    from auth_flow import start
    cb, auth = MagicMock(), MagicMock()
    cb.resolve.return_value = fake_route("https://x.example/callback/plg-fin.gmail--oauth")
    auth.build_auth_url.return_value = "https://accounts.google.com/o"

    start(auth, cb)
    assert auth.build_auth_url.call_args[0][0] == \
        "https://x.example/callback/plg-fin.gmail--oauth"


def test_start_generates_a_fresh_state_each_call():
    from auth_flow import start
    cb, auth = MagicMock(), MagicMock()
    cb.resolve.return_value = fake_route()
    auth.build_auth_url.return_value = "https://accounts.google.com/o"

    start(auth, cb)
    start(auth, cb)
    assert cb.mint.call_args_list[0][0][0] != cb.mint.call_args_list[1][0][0]


def build_env(tmp_path, staged=None, active=None):
    """Return (auth_double, cb_double, store) wired to a real TokenStore."""
    from token_store import Credential, TokenStore
    store = TokenStore(str(tmp_path))
    if active:
        store.write_active(active)
    if staged:
        store.stage(*staged)
    auth = MagicMock()
    auth.store = store
    auth.subject_email = "user@example.com"
    cb = MagicMock()
    return auth, cb, store


def cred(rt="rt", flow=None, gen=None, account="user@example.com"):
    from token_store import Credential
    return Credential(refresh_token=rt, flow=flow, generation=gen, account=account)


FLOW = "a" * 64


def test_reconcile_no_stage_is_a_noop(tmp_path):
    from auth_flow import reconcile_stage
    auth, cb, _ = build_env(tmp_path)
    assert reconcile_stage(auth, cb)[0] == "none"
    cb.ack.assert_not_called()


def test_reconcile_promotes_a_verified_stage(tmp_path):
    from auth_flow import reconcile_stage
    auth, cb, store = build_env(tmp_path, staged=("rt-new", FLOW, 9.0))
    auth.refresh_and_verify.return_value = "user@example.com"

    assert reconcile_stage(auth, cb)[0] == "promoted"
    assert store.load_active().refresh_token == "rt-new"
    assert store.load_staged() is None
    cb.ack.assert_called_once_with(FLOW)
    auth.activate.assert_called_once()


def test_reconcile_acks_before_it_activates_nothing_is_lost(tmp_path):
    """Ordering pin: promote must precede ack."""
    from auth_flow import reconcile_stage
    auth, cb, store = build_env(tmp_path, staged=("rt-new", FLOW, 9.0))
    auth.refresh_and_verify.return_value = "user@example.com"
    seen = {}
    cb.ack.side_effect = lambda h: seen.update(active=store.load_active())

    reconcile_stage(auth, cb)
    assert seen["active"] is not None and seen["active"].refresh_token == "rt-new"


def test_reconcile_retains_stage_on_retryable_failure(tmp_path):
    from auth import RefreshRetryable
    from auth_flow import reconcile_stage
    auth, cb, store = build_env(tmp_path, staged=("rt-new", FLOW, 9.0))
    auth.refresh_and_verify.side_effect = RefreshRetryable("timeout")

    assert reconcile_stage(auth, cb)[0] == "retain"
    assert store.load_staged() is not None
    cb.ack.assert_not_called()


def test_reconcile_retains_on_an_unclassified_verification_failure(tmp_path):
    """refresh_and_verify is a refresh AND a getProfile. getProfile raises a
    plain ValueError for every HttpError (403 with the Gmail API disabled, say).
    Unclassified must mean RETAIN — an escaping exception would wedge the slot
    and fail every future attempt at step 0."""
    from auth_flow import reconcile_stage
    auth, cb, store = build_env(tmp_path, staged=("rt-new", FLOW, 9.0))
    auth.refresh_and_verify.side_effect = ValueError("Gmail API error 403: disabled")

    outcome, message = reconcile_stage(auth, cb)

    assert outcome == "retain"
    assert "403" in message
    assert store.load_staged() is not None
    cb.ack.assert_not_called()


def test_reconcile_retains_when_the_verified_account_is_blank(tmp_path):
    """getProfile without an emailAddress is a FAILED verification, not a
    wrong-account verdict: acking and discarding here would throw away a stage
    that may be perfectly good."""
    from auth_flow import reconcile_stage
    auth, cb, store = build_env(tmp_path, staged=("rt-new", FLOW, 9.0))
    auth.refresh_and_verify.return_value = ""

    outcome, message = reconcile_stage(auth, cb)

    assert outcome == "retain"
    assert "configured for" not in message          # not the mismatch verdict
    assert store.load_staged() is not None
    cb.ack.assert_not_called()


def test_reconcile_does_not_ack_when_activation_fails(tmp_path):
    """The rebuild of everything derived from the credential happens inside
    activate(). If it fails, the flow must stay unacked — the credential is
    already durably promoted and the next pass recovers it."""
    from auth_flow import reconcile_stage
    auth, cb, store = build_env(tmp_path, staged=("rt-new", FLOW, 9.0))
    auth.refresh_and_verify.return_value = "user@example.com"
    auth.activate.side_effect = RuntimeError("attachment cache unwritable")

    outcome, _message = reconcile_stage(auth, cb)

    assert outcome != "promoted"
    assert outcome == "retain"
    cb.ack.assert_not_called()
    assert store.load_active().refresh_token == "rt-new"    # promoted on disk


def _auth_with_real_refresh(tmp_path, monkeypatch, refresh_side_effect):
    """A real GmailAuth over a real TokenStore, with only the google-auth
    Credentials object faked. The RefreshError → Refresh{Retryable,Terminal}
    classification therefore runs for real, which is the point: a MagicMock
    auth would let reconcile_stage's catch-all decide instead."""
    from unittest.mock import patch
    from auth import GmailAuth
    for key, val in {"GMAIL_CLIENT_ID": "client-id",
                     "GMAIL_CLIENT_SECRET": "client-secret",
                     "GMAIL_USER_EMAIL": "user@example.com"}.items():
        monkeypatch.setenv(key, val)
    auth = GmailAuth(str(tmp_path))
    auth.read_env()
    creds = MagicMock()
    creds.refresh.side_effect = refresh_side_effect
    return auth, patch("auth.Credentials", return_value=creds)


def test_reconcile_retains_a_stage_when_google_is_transiently_down(tmp_path, monkeypatch):
    """BLOCKER regression. google-auth raises RefreshError(retryable=True) for a
    transient Google 5xx, after its own retries are spent. Settling that would
    ack and unlink a perfectly valid freshly-granted credential."""
    from google.auth.exceptions import RefreshError
    from auth_flow import reconcile_stage
    auth, patched = _auth_with_real_refresh(
        tmp_path, monkeypatch,
        RefreshError("server_error: backend error", retryable=True))
    cb = MagicMock()
    auth.store.stage("rt-new", FLOW, 9.0)

    with patched:
        outcome, _message = reconcile_stage(auth, cb)

    assert outcome == "retain"
    assert auth.store.load_staged() is not None
    cb.ack.assert_not_called()


def test_reconcile_still_settles_a_genuinely_revoked_stage(tmp_path, monkeypatch):
    """The other half: a non-retryable RefreshError must still settle, or a dead
    stage wedges the slot forever."""
    from google.auth.exceptions import RefreshError
    from auth_flow import reconcile_stage
    auth, patched = _auth_with_real_refresh(
        tmp_path, monkeypatch, RefreshError("invalid_grant", retryable=False))
    cb = MagicMock()
    auth.store.stage("rt-dead", FLOW, 9.0)

    with patched:
        outcome, _message = reconcile_stage(auth, cb)

    assert outcome == "settled"
    assert auth.store.load_staged() is None
    cb.ack.assert_called_once_with(FLOW)


def test_reconcile_settles_a_terminally_dead_stage(tmp_path):
    """A revoked stage must not wedge the slot forever."""
    from auth import RefreshTerminal
    from auth_flow import reconcile_stage
    auth, cb, store = build_env(tmp_path, staged=("rt-new", FLOW, 9.0))
    auth.refresh_and_verify.side_effect = RefreshTerminal("invalid_grant")

    assert reconcile_stage(auth, cb)[0] == "settled"
    assert store.load_staged() is None
    cb.ack.assert_called_once_with(FLOW)


def test_terminal_settle_acks_before_unlinking(tmp_path):
    """Unlinking first would leave a journal with no disposition."""
    from auth import RefreshTerminal
    from auth_flow import reconcile_stage
    auth, cb, store = build_env(tmp_path, staged=("rt-new", FLOW, 9.0))
    auth.refresh_and_verify.side_effect = RefreshTerminal("invalid_grant")
    seen = {}
    cb.ack.side_effect = lambda h: seen.update(staged=store.load_staged())

    reconcile_stage(auth, cb)
    assert seen["staged"] is not None      # stage still present when ack ran


def test_reconcile_account_mismatch_discards_stage_and_keeps_active(tmp_path):
    from auth_flow import reconcile_stage
    auth, cb, store = build_env(
        tmp_path, staged=("rt-new", FLOW, 9.0),
        active=cred(rt="rt-old", flow="b" * 64, gen=1.0))
    auth.refresh_and_verify.return_value = "someone-else@example.com"

    outcome, message = reconcile_stage(auth, cb)
    assert outcome == "settled"
    assert "someone-else@example.com" in message
    assert store.load_staged() is None
    assert store.load_active().refresh_token == "rt-old"
    cb.ack.assert_called_once_with(FLOW)


def test_reconcile_unlinks_post_promote_residue(tmp_path):
    from auth_flow import reconcile_stage
    auth, cb, store = build_env(
        tmp_path, staged=("rt-new", FLOW, 9.0),
        active=cred(rt="rt-new", flow=FLOW, gen=9.0))

    assert reconcile_stage(auth, cb)[0] == "none"
    assert store.load_staged() is None
    auth.refresh_and_verify.assert_not_called()


def test_startup_recover_returns_busy_when_lock_contended(tmp_path):
    """Contention is a no-op: no validate_and_init, no stage work."""
    from auth_flow import collect_lock, startup_recover
    auth, cb, _ = build_env(tmp_path)

    with collect_lock(tmp_path):
        result = startup_recover(auth, cb)

    assert result == "busy"
    auth.validate_and_init.assert_not_called()
    auth.refresh_and_verify.assert_not_called()


def _real_auth(tmp_path, monkeypatch, **env):
    """A real GmailAuth (not a double) so env reading can be observed."""
    from auth import GmailAuth
    values = {"GMAIL_CLIENT_ID": "client-id", "GMAIL_CLIENT_SECRET": "client-secret",
              "GMAIL_USER_EMAIL": "user@example.com", **env}
    for key, val in values.items():
        if val is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, val)
    return GmailAuth(str(tmp_path))


def test_startup_recover_reads_env_even_when_the_lock_is_contended(tmp_path, monkeypatch):
    """Two overlapping server processes are the only reason the lock exists. The
    loser must still be configured: without the env, gmail_auth_start emits
    client_id=None and gmail_auth_collect crashes on the account comparison."""
    from auth_flow import collect_lock, startup_recover
    auth = _real_auth(tmp_path, monkeypatch)

    with collect_lock(tmp_path):
        result = startup_recover(auth, MagicMock())

    assert result == "busy"
    assert auth.subject_email == "user@example.com"
    assert "client_id=client-id" in auth.build_auth_url("https://casa/x", "s")


def test_startup_recover_exits_on_missing_env_even_when_contended(tmp_path, monkeypatch):
    """The hoisted env read still exits the process: SystemExit is a
    BaseException and nothing here may catch it."""
    from auth_flow import collect_lock, startup_recover
    auth = _real_auth(tmp_path, monkeypatch, GMAIL_CLIENT_ID=None)

    with collect_lock(tmp_path):
        with pytest.raises(SystemExit):
            startup_recover(auth, MagicMock())


def test_startup_recover_loads_active_before_reconciling_stage(tmp_path):
    """Ordering pin: validate_and_init runs before stage verification."""
    from auth_flow import startup_recover
    auth, cb, store = build_env(tmp_path, staged=("rt-new", FLOW, 9.0))
    order = []
    auth.validate_and_init.side_effect = lambda: order.append("validate_and_init")

    def verify(rt):
        order.append("refresh_and_verify")
        return "user@example.com"
    auth.refresh_and_verify.side_effect = verify

    startup_recover(auth, cb)

    assert order == ["validate_and_init", "refresh_and_verify"]


def test_startup_recover_holds_the_lock_through_reconcile_stage(tmp_path):
    """Both validate_and_init and reconcile_stage run under ONE acquisition.

    Proof: from inside refresh_and_verify (called by reconcile_stage), a
    re-entrant startup_recover call can only see "busy" if the outer call's
    lock is still held at that point — i.e. reconcile_stage has not yet run
    past the `with collect_lock(...)` block.
    """
    from auth_flow import startup_recover
    auth, cb, store = build_env(tmp_path, staged=("rt-new", FLOW, 9.0))
    calls = {"n": 0}
    nested = {}

    def verify(rt):
        calls["n"] += 1
        if calls["n"] == 1:
            nested["result"] = startup_recover(auth, cb)
        return "user@example.com"
    auth.refresh_and_verify.side_effect = verify

    startup_recover(auth, cb)

    assert nested["result"] == "busy"


def test_startup_recover_returns_error_when_reconcile_stage_raises(tmp_path, capsys):
    """A bug or I/O error in step 0 must not propagate — and must be logged.

    An unclassified *verification* failure is now classified as "retain" inside
    reconcile_stage, so this net is exercised from the settlement instead: it
    must still catch anything reconcile_stage does not."""
    from auth import RefreshTerminal
    from auth_flow import startup_recover
    auth, cb, store = build_env(tmp_path, staged=("rt-new", FLOW, 9.0))
    auth.refresh_and_verify.side_effect = RefreshTerminal("revoked")
    cb.ack.side_effect = RuntimeError("boom")

    result = startup_recover(auth, cb)

    assert result == "error"
    captured = capsys.readouterr()
    assert "boom" in captured.err


def test_startup_recover_does_not_catch_system_exit(tmp_path):
    """A missing env var still exits the process: SystemExit is a BaseException."""
    from auth_flow import startup_recover
    auth, cb, _ = build_env(tmp_path)
    auth.validate_and_init.side_effect = SystemExit(1)

    with pytest.raises(SystemExit):
        startup_recover(auth, cb)


# ── Startup-recovery outcomes must reach the user ──────────────────────────
# Casa's browser page is identical for success, denial and a replayed link, so
# chat is the only place the real outcome is ever learned. A stage resolved by
# startup_recover is ACKED, and the ack tears the attempt down — so without a
# durable notice the next collect finds nothing and says nothing.

def _next_collect(auth):
    """A fresh collect_pass with no attempts waiting: only a drained notice can
    put anything in `messages`."""
    from auth_flow import collect_pass
    cb = MagicMock()
    cb.attempts.return_value = []
    return collect_pass(auth, cb)


def test_startup_promotion_is_reported_by_the_next_collect(tmp_path):
    from auth_flow import startup_recover
    auth, cb, store = build_env(tmp_path, staged=("rt-new", FLOW, 9.0))
    auth.refresh_and_verify.return_value = "user@example.com"

    assert startup_recover(auth, cb) == "promoted"

    out = _next_collect(auth)
    assert any("Gmail connected as user@example.com" in m for m in out["messages"])


def test_startup_wrong_account_rejection_is_reported_by_the_next_collect(tmp_path):
    """The case that matters most: the user granted from the wrong inbox and
    would otherwise get a neutral browser page and total silence in chat."""
    from auth_flow import startup_recover
    auth, cb, store = build_env(tmp_path, staged=("rt-new", FLOW, 9.0))
    auth.refresh_and_verify.return_value = "someone-else@example.com"

    assert startup_recover(auth, cb) == "settled"

    out = _next_collect(auth)
    assert any("someone-else@example.com" in m for m in out["messages"])


def test_startup_terminal_settlement_is_reported_by_the_next_collect(tmp_path):
    from auth import RefreshTerminal
    from auth_flow import startup_recover
    auth, cb, store = build_env(tmp_path, staged=("rt-new", FLOW, 9.0))
    auth.refresh_and_verify.side_effect = RefreshTerminal("invalid_grant")

    assert startup_recover(auth, cb) == "settled"

    out = _next_collect(auth)
    assert any("no longer valid" in m for m in out["messages"])


def test_the_notice_is_written_before_the_ack(tmp_path):
    """Ordering pin. Casa's ack is a settlement receipt that tears the flow
    down; a notice written after it is lost by a crash in between — which is
    the exact scenario the notice exists for."""
    from auth import RefreshTerminal
    from auth_flow import startup_recover
    auth, cb, store = build_env(tmp_path, staged=("rt-new", FLOW, 9.0))
    auth.refresh_and_verify.side_effect = RefreshTerminal("invalid_grant")
    seen = {}
    cb.ack.side_effect = lambda h: seen.update(notices=store.peek_notices())

    startup_recover(auth, cb)

    assert seen["notices"], "the notice must already be on disk when ack runs"
    assert "no longer valid" in seen["notices"][0]


# ── The notice must not be lost, and must not repeat forever ───────────────
# Removing the durable copy because a pass ran throws away the only record of
# the outcome without ever learning that anyone read it. Delivery is
# at-least-once by choice: a repeated "granted by the wrong account" is a
# nuisance, a silent one is the bug. A durable per-notice offer count is what
# keeps "at least once" from becoming "forever".

def test_a_notice_survives_a_pass_that_raises_after_reading_it(tmp_path):
    """Sol's case: the notice is read, then cb.attempts() blows up — the tool
    errors out and the sentence must still be waiting for the retry."""
    from auth_flow import collect_pass, startup_recover
    auth, cb, store = build_env(tmp_path, staged=("rt-new", FLOW, 9.0))
    auth.refresh_and_verify.return_value = "someone-else@example.com"
    assert startup_recover(auth, cb) == "settled"

    broken = MagicMock()
    broken.attempts.side_effect = RuntimeError("callback index unreadable")
    with pytest.raises(RuntimeError):
        collect_pass(auth, broken)

    assert any("someone-else@example.com" in m
               for m in _next_collect(auth)["messages"])


def test_a_notice_survives_a_retried_collect_in_the_same_process(tmp_path):
    """P1, and Terra's MAJOR. Casa's nudge carries a six-dispatch budget, so a
    second gmail_auth_collect in the same process is ordinary — it is NOT
    evidence that the first response reached the agent. If the second pass
    purges what the first returned, the user is told nothing was waiting when
    their authorization had in fact been rejected as the wrong account."""
    from auth_flow import startup_recover
    auth, cb, store = build_env(tmp_path, staged=("rt-new", FLOW, 9.0))
    auth.refresh_and_verify.return_value = "someone-else@example.com"
    assert startup_recover(auth, cb) == "settled"

    first = _next_collect(auth)["messages"]
    assert any("someone-else@example.com" in m for m in first)

    retry = _next_collect(auth)["messages"]
    assert any("someone-else@example.com" in m for m in retry), \
        "the retried collect must still carry the outcome"
    assert not any("No authorization result was waiting" in m for m in retry)


def test_a_pass_that_raises_does_not_consume_an_offer(tmp_path):
    """P4. The offer is counted only on a normal return, so a pass that blew up
    after peeking leaves the whole budget intact."""
    from token_store import NOTICE_OFFER_LIMIT
    from auth_flow import collect_pass, startup_recover
    auth, cb, store = build_env(tmp_path, staged=("rt-new", FLOW, 9.0))
    auth.refresh_and_verify.return_value = "someone-else@example.com"
    assert startup_recover(auth, cb) == "settled"

    broken = MagicMock()
    broken.attempts.side_effect = RuntimeError("callback index unreadable")
    for _ in range(3):
        with pytest.raises(RuntimeError):
            collect_pass(auth, broken)

    offers = sum(1 for _ in range(NOTICE_OFFER_LIMIT + 3)
                 if any("someone-else@example.com" in m
                        for m in _next_collect(auth)["messages"]))
    assert offers == NOTICE_OFFER_LIMIT


def test_a_notice_is_offered_a_bounded_number_of_times_then_never_again(tmp_path):
    """P2. The other half: at-least-once must not become forever, and a restart
    must neither reset the budget nor exempt a notice from it — the count is on
    disk, and no process identity is involved anywhere."""
    from token_store import NOTICE_OFFER_LIMIT, TokenStore
    from auth_flow import startup_recover
    auth, cb, store = build_env(tmp_path, staged=("rt-new", FLOW, 9.0))
    auth.refresh_and_verify.return_value = "someone-else@example.com"
    startup_recover(auth, cb)

    offers = 0
    for _ in range(NOTICE_OFFER_LIMIT + 5):
        # A fresh store every round: each pass is a restarted server.
        auth.store = TokenStore(str(tmp_path))
        if any("someone-else@example.com" in m
               for m in _next_collect(auth)["messages"]):
            offers += 1

    assert offers == NOTICE_OFFER_LIMIT
    assert not (tmp_path / "pending_notices.json").exists()


def test_a_failed_ack_does_not_queue_the_notice_twice(tmp_path):
    """The stage survives an ack that failed, so the next startup settles the
    same flow the same way — and must not append the same sentence again."""
    from auth import RefreshTerminal
    from auth_flow import startup_recover
    auth, cb, store = build_env(tmp_path, staged=("rt-new", FLOW, 9.0))
    auth.refresh_and_verify.side_effect = RefreshTerminal("invalid_grant")
    cb.ack.side_effect = RuntimeError("casa unreachable")

    assert startup_recover(auth, cb) == "error"
    assert store.load_staged() is not None, "a failed ack must keep the stage"

    cb.ack.side_effect = None
    assert startup_recover(auth, cb) == "settled"

    messages = _next_collect(auth)["messages"]
    assert len([m for m in messages if "no longer valid" in m]) == 1


def attempt(h, minted_ts=100.0, status="result_ready", outcome=None, claimed=False):
    return {"state_hash": h, "minted_ts": minted_ts, "status": status,
            "outcome": outcome, "claimed": claimed, "meta": None}


NEW, OLD = "f" * 64, "0" * 64


def wire(tmp_path, attempts, active=None, staged=None):
    auth, cb, store = build_env(tmp_path, staged=staged, active=active)
    cb.attempts.return_value = attempts
    cb.resolve.return_value = fake_route()
    auth.exchange_code.return_value = {"refresh_token": "rt-new", "access_token": "at"}
    auth.refresh_and_verify.return_value = "user@example.com"
    return auth, cb, store


def test_collect_exchanges_and_promotes_a_ready_result(tmp_path):
    from auth_flow import collect_pass
    auth, cb, store = wire(tmp_path, [attempt(NEW)])
    cb.collect.return_value = {"query": [["code", "c1"]]}

    out = collect_pass(auth, cb)
    assert out["promoted"] is True
    assert store.load_active().refresh_token == "rt-new"
    cb.ack.assert_called_with(NEW)


def test_collect_reports_a_denial_and_acks(tmp_path):
    from auth_flow import collect_pass
    auth, cb, store = wire(tmp_path, [attempt(NEW)])
    cb.collect.return_value = {"query": [["error", "access_denied"]]}

    out = collect_pass(auth, cb)
    assert out["promoted"] is False
    assert any("access_denied" in m for m in out["messages"])
    cb.ack.assert_called_with(NEW)
    auth.exchange_code.assert_not_called()


def test_collect_reports_malformed_query_without_exchanging(tmp_path):
    from auth_flow import collect_pass
    auth, cb, _ = wire(tmp_path, [attempt(NEW)])
    cb.collect.return_value = {"query": [["code", "c1"], ["code", "c2"]]}

    collect_pass(auth, cb)
    auth.exchange_code.assert_not_called()
    cb.ack.assert_called_with(NEW)


def test_collect_acks_a_done_attempt_and_reports_its_outcome(tmp_path):
    from auth_flow import collect_pass
    auth, cb, _ = wire(tmp_path, [attempt(NEW, status="done", outcome="expired_unread")])

    out = collect_pass(auth, cb)
    assert any("expired" in m for m in out["messages"])
    cb.ack.assert_called_with(NEW)
    cb.collect.assert_not_called()


def test_collect_leaves_awaiting_redirect_alone(tmp_path):
    from auth_flow import collect_pass
    auth, cb, _ = wire(tmp_path, [attempt(NEW, status="awaiting_redirect")])

    collect_pass(auth, cb)
    cb.ack.assert_not_called()
    cb.collect.assert_not_called()


def test_supersedes_an_older_attempt_across_a_later_pass(tmp_path):
    """The newer flow was acked and torn down in an earlier pass; the older
    callback arrives now and must NOT be exchanged."""
    from auth_flow import collect_pass
    auth, cb, _ = wire(tmp_path, [attempt(OLD, minted_ts=50.0)],
                       active=cred(rt="rt-good", flow=NEW, gen=100.0))

    collect_pass(auth, cb)
    cb.ack.assert_called_once_with(OLD)
    cb.collect.assert_not_called()
    auth.exchange_code.assert_not_called()


def test_supersedes_a_claimed_older_attempt_without_reading_its_journal(tmp_path):
    from auth_flow import collect_pass
    auth, cb, _ = wire(tmp_path, [attempt(OLD, minted_ts=50.0, claimed=True)],
                       active=cred(rt="rt-good", flow=NEW, gen=100.0))

    collect_pass(auth, cb)
    cb.held.assert_not_called()
    cb.ack.assert_called_once_with(OLD)


def test_legacy_active_token_supersedes_nothing(tmp_path):
    from auth_flow import collect_pass
    auth, cb, _ = wire(tmp_path, [attempt(NEW)],
                       active=cred(rt="rt-legacy", flow=None, gen=None))
    cb.collect.return_value = {"query": [["code", "c1"]]}

    collect_pass(auth, cb)
    auth.exchange_code.assert_called_once()


def test_claimed_attempt_already_committed_activates_then_acks(tmp_path):
    from auth_flow import collect_pass
    auth, cb, _ = wire(tmp_path, [attempt(NEW, claimed=True)],
                       active=cred(rt="rt-new", flow=NEW, gen=100.0))
    cb.collect.side_effect = FileNotFoundError()

    out = collect_pass(auth, cb)
    auth.activate.assert_called_once()
    auth.exchange_code.assert_not_called()
    cb.ack.assert_called_with(NEW)
    assert out["promoted"] is True


def test_committed_activation_failure_ends_the_pass_without_acking(tmp_path):
    """Same rule on the already-committed shortcut: ack only after activate()."""
    from auth_flow import collect_pass
    auth, cb, _ = wire(tmp_path, [attempt(NEW, claimed=True)],
                       active=cred(rt="rt-new", flow=NEW, gen=100.0))
    cb.collect.side_effect = FileNotFoundError()
    auth.activate.side_effect = RuntimeError("attachment cache unwritable")

    out = collect_pass(auth, cb)

    assert out["status"] == "retry_later"
    assert out["promoted"] is False
    cb.ack.assert_not_called()


def test_claimed_attempt_resumes_from_the_held_journal(tmp_path):
    from auth_flow import collect_pass
    auth, cb, store = wire(tmp_path, [attempt(NEW, claimed=True)])
    cb.collect.side_effect = FileNotFoundError()
    cb.held.return_value = {"query": [["code", "c-from-journal"]]}

    collect_pass(auth, cb)
    assert auth.exchange_code.call_args[0][0] == "c-from-journal"
    assert store.load_active().refresh_token == "rt-new"


def test_terminal_exchange_falls_through_to_an_older_flow(tmp_path):
    """Regression pin for the configuration split. A genuinely dead
    authorization code (`invalid_grant`) must keep the settled behaviour it has
    had since round 3: ack THAT attempt, then move on to the next one. Counts,
    not just statuses — a status-only assertion cannot tell "acked and moved
    on" apart from "acked and stopped"."""
    from unittest.mock import call
    from auth import ExchangeTerminal
    from auth_flow import collect_pass
    auth, cb, store = wire(tmp_path, [attempt(NEW), attempt(OLD, minted_ts=50.0)])
    cb.collect.return_value = {"query": [["code", "c1"]]}
    auth.exchange_code.side_effect = [ExchangeTerminal("invalid_grant"),
                                      {"refresh_token": "rt-old-flow", "access_token": "at"}]

    out = collect_pass(auth, cb)
    assert store.load_active().refresh_token == "rt-old-flow"
    assert auth.exchange_code.call_count == 2       # DID fall through
    assert cb.ack.call_args_list == [call(NEW), call(OLD)]
    assert out["status"] == "ok"
    assert any("Please start again" in m for m in out["messages"])


def test_configuration_failure_at_exchange_neither_acks_nor_falls_through(tmp_path):
    """A rotated client secret at code-exchange time. The authorization code is
    still good, so the attempt must NOT be acked — casa's ack tears it down.
    And every other attempt would be exchanged against the same rejected
    client, so the pass must not fall through and burn them either."""
    from auth import ExchangeConfigError
    from auth_flow import collect_pass
    auth, cb, store = wire(tmp_path, [attempt(NEW), attempt(OLD, minted_ts=50.0)])
    cb.collect.return_value = {"query": [["code", "c1"]]}
    auth.exchange_code.side_effect = ExchangeConfigError(
        "Token exchange refused this OAuth client (401): invalid_client — "
        "The OAuth client was not found.")

    out = collect_pass(auth, cb)

    assert cb.ack.call_count == 0                   # nothing torn down
    assert auth.exchange_code.call_count == 1       # did NOT fall through
    assert out["status"] == "configuration_error"
    assert out["promoted"] is False
    assert store.load_active() is None
    assert store.load_staged() is None


def test_the_configuration_message_names_the_cause_and_the_new_session(tmp_path):
    """It is relayed verbatim to the operator, so it has to be true prose: name
    the configuration problem, not a dead authorization, and say plainly that
    correcting the variables needs a new session before it takes effect. The
    "Please start again" of the terminal branch would be false twice over
    here."""
    from auth import ExchangeConfigError
    from auth_flow import collect_pass
    auth, cb, _ = wire(tmp_path, [attempt(NEW)])
    cb.collect.return_value = {"query": [["code", "c1"]]}
    auth.exchange_code.side_effect = ExchangeConfigError("invalid_client — nope")

    message = " ".join(collect_pass(auth, cb)["messages"])

    assert "OAuth client configuration" in message
    assert "GMAIL_CLIENT_ID" in message and "GMAIL_CLIENT_SECRET" in message
    assert "new session" in message and "restart" in message
    assert "Please start again" not in message
    assert "invalid_client — nope" in message


def test_retryable_exchange_ends_the_pass_without_acking(tmp_path):
    from auth import ExchangeRetryable
    from auth_flow import collect_pass
    auth, cb, _ = wire(tmp_path, [attempt(NEW), attempt(OLD, minted_ts=50.0)])
    cb.collect.return_value = {"query": [["code", "c1"]]}
    auth.exchange_code.side_effect = ExchangeRetryable("503")

    collect_pass(auth, cb)
    cb.ack.assert_not_called()
    assert auth.exchange_code.call_count == 1     # did NOT fall through


def test_retain_from_step0_ends_the_pass_before_any_attempt(tmp_path):
    from auth import RefreshRetryable
    from auth_flow import collect_pass
    auth, cb, _ = wire(tmp_path, [attempt(NEW)], staged=("rt-s", "c" * 64, 5.0))
    auth.refresh_and_verify.side_effect = RefreshRetryable("timeout")

    collect_pass(auth, cb)
    cb.collect.assert_not_called()
    auth.exchange_code.assert_not_called()


def test_a_settled_dead_stage_lets_a_later_flow_succeed(tmp_path):
    """Regression for the round-4 deadlock: the slot must not wedge."""
    from auth import RefreshTerminal
    from auth_flow import collect_pass
    auth, cb, store = wire(tmp_path, [attempt(NEW)], staged=("rt-dead", "c" * 64, 5.0))
    cb.collect.return_value = {"query": [["code", "c1"]]}
    auth.refresh_and_verify.side_effect = [RefreshTerminal("revoked"),
                                           "user@example.com"]

    collect_pass(auth, cb)
    assert store.load_active().refresh_token == "rt-new"


def test_a_live_collect_message_is_never_also_queued_as_a_notice(tmp_path):
    """No-duplicate pin. collect_pass relays reconcile_stage's message itself,
    so reconcile_stage must queue nothing on its behalf — otherwise the very
    next pass would repeat 'Gmail connected as …' out of nowhere."""
    from auth_flow import collect_pass
    auth, cb, store = wire(tmp_path, [attempt(NEW)])
    cb.collect.return_value = {"query": [["code", "c1"]]}

    out = collect_pass(auth, cb)

    assert [m for m in out["messages"] if "Gmail connected as" in m] == \
        ["Gmail connected as user@example.com."]
    assert store.peek_notices() == []
    assert not any("Gmail connected as" in m
                   for m in _next_collect(auth)["messages"])


def test_an_empty_pass_says_plainly_that_nothing_was_waiting(tmp_path):
    """`status: ok` with no messages reads as confirmation of success. It
    confirms nothing — a stale or replayed link produces exactly this."""
    from auth_flow import collect_pass
    auth, cb, _ = wire(tmp_path, [])

    out = collect_pass(auth, cb)

    assert out["status"] == "ok"          # nothing failed
    assert out["promoted"] is False
    assert out["messages"], "an empty pass must not be silent"
    assert any("does NOT confirm" in m for m in out["messages"])


def test_contended_lock_is_a_noop(tmp_path):
    from auth_flow import collect_lock, collect_pass
    auth, cb, _ = wire(tmp_path, [attempt(NEW)])
    with collect_lock(tmp_path):
        out = collect_pass(auth, cb)
    assert out["status"] == "busy"
    cb.attempts.assert_not_called()
