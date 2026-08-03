# tests/test_auth_flow.py
import pytest


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
