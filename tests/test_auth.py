import io
import json
import pytest
from unittest.mock import patch, MagicMock


def make_auth(data_dir):
    from auth import GmailAuth
    return GmailAuth(str(data_dir))


_FULL_ENV = {
    "GMAIL_CLIENT_ID": "client-id",
    "GMAIL_CLIENT_SECRET": "client-secret",
    "GMAIL_USER_EMAIL": "user@workspace.example.com",
}


def _set_full_env(monkeypatch, **overrides):
    env = {**_FULL_ENV, **overrides}
    for key, val in env.items():
        if val is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, val)


# ── Env var validation ─────────────────────────────────────────────────────

@pytest.mark.parametrize("missing_var", [
    "GMAIL_CLIENT_ID",
    "GMAIL_CLIENT_SECRET",
    "GMAIL_USER_EMAIL",
])
def test_missing_required_var_exits(missing_var, monkeypatch, tmp_path):
    _set_full_env(monkeypatch, **{missing_var: None})
    with pytest.raises(SystemExit):
        make_auth(tmp_path).validate_and_init()


@pytest.mark.parametrize("missing_var", [
    "GMAIL_CLIENT_ID",
    "GMAIL_CLIENT_SECRET",
    "GMAIL_USER_EMAIL",
])
def test_missing_required_var_names_it_in_stderr(missing_var, monkeypatch, capsys, tmp_path):
    _set_full_env(monkeypatch, **{missing_var: None})
    with pytest.raises(SystemExit):
        make_auth(tmp_path).validate_and_init()
    assert missing_var in capsys.readouterr().err


# ── No token file ───────────────────────────────────────────────────────────

def test_no_token_file_returns_false(monkeypatch, tmp_path):
    _set_full_env(monkeypatch)
    auth = make_auth(tmp_path)
    assert auth.validate_and_init() is False
    assert not auth.is_authenticated


def test_no_token_file_credentials_is_none(monkeypatch, tmp_path):
    _set_full_env(monkeypatch)
    auth = make_auth(tmp_path)
    auth.validate_and_init()
    assert auth.credentials is None


# ── Active credential loading ───────────────────────────────────────────────

def _write_v2(tmp_path, **over):
    payload = {"v": 2, "refresh_token": "rt-abc", "flow": "a" * 64,
               "generation": 5.0, "account": "user@workspace.example.com",
               "committed_ts": 1.0}
    payload.update(over)
    (tmp_path / "oauth_token.json").write_text(json.dumps(payload))


@patch("auth.Credentials")
def test_valid_v2_token_authenticates(mock_creds_cls, monkeypatch, tmp_path):
    _set_full_env(monkeypatch)
    _write_v2(tmp_path)
    mock_creds_cls.return_value = MagicMock()

    auth = make_auth(tmp_path)
    assert auth.validate_and_init() is True
    assert auth.is_authenticated
    assert auth.subject_email == "user@workspace.example.com"


@patch("auth.Credentials")
def test_v2_account_mismatch_refuses_to_authenticate(mock_creds_cls, monkeypatch, tmp_path):
    """Changing GMAIL_USER_EMAIL must not silently keep serving the old inbox."""
    _set_full_env(monkeypatch)
    _write_v2(tmp_path, account="someone-else@example.com")
    mock_creds_cls.return_value = MagicMock()

    auth = make_auth(tmp_path)
    assert auth.validate_and_init() is False
    assert not auth.is_authenticated


@patch("auth.Credentials")
def test_v2_account_mismatch_keeps_the_token_file(mock_creds_cls, monkeypatch, tmp_path):
    _set_full_env(monkeypatch)
    _write_v2(tmp_path, account="someone-else@example.com")
    mock_creds_cls.return_value = MagicMock()

    make_auth(tmp_path).validate_and_init()
    assert (tmp_path / "oauth_token.json").exists()


@patch("auth.Credentials")
def test_account_comparison_is_case_insensitive(mock_creds_cls, monkeypatch, tmp_path):
    _set_full_env(monkeypatch)
    _write_v2(tmp_path, account="User@Workspace.Example.COM")
    mock_creds_cls.return_value = MagicMock()

    assert make_auth(tmp_path).validate_and_init() is True


@patch("auth.Credentials")
def test_terminal_refresh_failure_removes_the_token(mock_creds_cls, monkeypatch, tmp_path):
    from google.auth.exceptions import RefreshError
    _set_full_env(monkeypatch)
    _write_v2(tmp_path)
    creds = MagicMock()
    creds.refresh.side_effect = RefreshError("invalid_grant")
    mock_creds_cls.return_value = creds

    auth = make_auth(tmp_path)
    assert auth.validate_and_init() is False
    assert not (tmp_path / "oauth_token.json").exists()


@patch("auth.Credentials")
def test_transient_refresh_failure_RETAINS_the_token(mock_creds_cls, monkeypatch, tmp_path):
    """A network blip must never destroy a valid refresh token."""
    _set_full_env(monkeypatch)
    _write_v2(tmp_path)
    creds = MagicMock()
    creds.refresh.side_effect = OSError("connection reset")
    mock_creds_cls.return_value = creds

    auth = make_auth(tmp_path)
    assert auth.validate_and_init() is False
    assert (tmp_path / "oauth_token.json").exists()


# ── build_auth_url ──────────────────────────────────────────────────────────

def test_build_auth_url_uses_supplied_redirect_and_state(monkeypatch, tmp_path):
    _set_full_env(monkeypatch)
    auth = make_auth(tmp_path)
    auth.validate_and_init()

    url = auth.build_auth_url("https://casa.example.com/callback/plg-gmail--oauth",
                              "state-xyz")
    assert "accounts.google.com" in url
    assert "client-id" in url
    assert "state-xyz" in url
    assert "casa.example.com%2Fcallback%2Fplg-gmail--oauth" in url
    assert "localhost" not in url
    assert "offline" in url
    assert "gmail.modify" in url


# ── exchange_code ───────────────────────────────────────────────────────────

import urllib.error


def _http_error(status, body=b'{"error":"invalid_grant"}'):
    return urllib.error.HTTPError(
        "https://oauth2.googleapis.com/token", status, "err", {}, io.BytesIO(body))


def _ok_body(**over):
    payload = {"access_token": "at-123", "refresh_token": "rt-new",
               "expires_in": 3600, "token_type": "Bearer"}
    payload.update(over)
    return io.BytesIO(json.dumps(payload).encode())


def _exchange(auth):
    return auth.exchange_code("code-1", "https://casa.example.com/callback/x")


@patch("auth.urllib.request.urlopen")
def test_exchange_code_returns_token_response(mock_urlopen, monkeypatch, tmp_path):
    _set_full_env(monkeypatch)
    mock_urlopen.return_value = _ok_body(refresh_token="rt-persisted")
    auth = make_auth(tmp_path)
    auth.validate_and_init()

    assert _exchange(auth)["refresh_token"] == "rt-persisted"


@patch("auth.urllib.request.urlopen")
def test_exchange_code_writes_nothing_and_does_not_authenticate(
        mock_urlopen, monkeypatch, tmp_path):
    """Persistence and activation are the store's job, not the exchange's."""
    _set_full_env(monkeypatch)
    mock_urlopen.return_value = _ok_body()
    auth = make_auth(tmp_path)
    auth.validate_and_init()

    _exchange(auth)
    assert list(tmp_path.iterdir()) == []
    assert not auth.is_authenticated


@pytest.mark.parametrize("status", [400, 401, 403])
@patch("auth.urllib.request.urlopen")
def test_exchange_code_4xx_is_terminal(mock_urlopen, monkeypatch, tmp_path, status):
    from auth import ExchangeTerminal
    _set_full_env(monkeypatch)
    mock_urlopen.side_effect = _http_error(status)
    auth = make_auth(tmp_path)
    auth.validate_and_init()
    with pytest.raises(ExchangeTerminal):
        _exchange(auth)


@pytest.mark.parametrize("status", [429, 500, 503])
@patch("auth.urllib.request.urlopen")
def test_exchange_code_429_and_5xx_are_retryable(mock_urlopen, monkeypatch, tmp_path, status):
    from auth import ExchangeRetryable
    _set_full_env(monkeypatch)
    mock_urlopen.side_effect = _http_error(status)
    auth = make_auth(tmp_path)
    auth.validate_and_init()
    with pytest.raises(ExchangeRetryable):
        _exchange(auth)


@patch("auth.urllib.request.urlopen")
def test_exchange_code_connection_failure_is_retryable(mock_urlopen, monkeypatch, tmp_path):
    from auth import ExchangeRetryable
    _set_full_env(monkeypatch)
    mock_urlopen.side_effect = urllib.error.URLError("timed out")
    auth = make_auth(tmp_path)
    auth.validate_and_init()
    with pytest.raises(ExchangeRetryable):
        _exchange(auth)


@patch("auth.urllib.request.urlopen")
def test_exchange_code_malformed_error_body_still_terminal_on_4xx(
        mock_urlopen, monkeypatch, tmp_path):
    from auth import ExchangeTerminal
    _set_full_env(monkeypatch)
    mock_urlopen.side_effect = _http_error(400, b"<html>nope</html>")
    auth = make_auth(tmp_path)
    auth.validate_and_init()
    with pytest.raises(ExchangeTerminal):
        _exchange(auth)


@patch("auth.urllib.request.urlopen")
def test_exchange_code_malformed_2xx_body_is_terminal(mock_urlopen, monkeypatch, tmp_path):
    from auth import ExchangeTerminal
    _set_full_env(monkeypatch)
    mock_urlopen.return_value = io.BytesIO(b"<html>not json</html>")
    auth = make_auth(tmp_path)
    auth.validate_and_init()
    with pytest.raises(ExchangeTerminal):
        _exchange(auth)


@pytest.mark.parametrize("missing", ["refresh_token", "access_token"])
@patch("auth.urllib.request.urlopen")
def test_exchange_code_incomplete_2xx_body_is_terminal(
        mock_urlopen, monkeypatch, tmp_path, missing):
    from auth import ExchangeTerminal
    _set_full_env(monkeypatch)
    payload = {"access_token": "at", "refresh_token": "rt", "expires_in": 3600}
    del payload[missing]
    mock_urlopen.return_value = io.BytesIO(json.dumps(payload).encode())
    auth = make_auth(tmp_path)
    auth.validate_and_init()
    with pytest.raises(ExchangeTerminal):
        _exchange(auth)


# ── scopes ──────────────────────────────────────────────────────────────────

def test_scopes_include_settings_basic(monkeypatch, tmp_path):
    _set_full_env(monkeypatch)
    auth = make_auth(tmp_path)
    assert "https://www.googleapis.com/auth/gmail.settings.basic" in auth.SCOPES
