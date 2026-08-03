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
