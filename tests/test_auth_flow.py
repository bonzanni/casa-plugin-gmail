# tests/test_auth_flow.py
import json

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
