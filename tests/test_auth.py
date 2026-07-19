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


# ── Token file present ──────────────────────────────────────────────────────

@patch("auth.Credentials")
def test_valid_token_file_returns_true(mock_creds_cls, monkeypatch, tmp_path):
    _set_full_env(monkeypatch)
    (tmp_path / "oauth_token.json").write_text(json.dumps({"refresh_token": "rt-abc"}))
    mock_creds = MagicMock()
    mock_creds_cls.return_value = mock_creds

    assert make_auth(tmp_path).validate_and_init() is True


@patch("auth.Credentials")
def test_valid_token_file_sets_authenticated(mock_creds_cls, monkeypatch, tmp_path):
    _set_full_env(monkeypatch)
    (tmp_path / "oauth_token.json").write_text(json.dumps({"refresh_token": "rt-abc"}))
    mock_creds_cls.return_value = MagicMock()

    auth = make_auth(tmp_path)
    auth.validate_and_init()
    assert auth.is_authenticated
    assert auth.subject_email == "user@workspace.example.com"


@patch("auth.Credentials")
def test_valid_token_calls_refresh(mock_creds_cls, monkeypatch, tmp_path):
    _set_full_env(monkeypatch)
    (tmp_path / "oauth_token.json").write_text(json.dumps({"refresh_token": "rt-abc"}))
    mock_creds = MagicMock()
    mock_creds_cls.return_value = mock_creds

    make_auth(tmp_path).validate_and_init()
    mock_creds.refresh.assert_called_once()


@patch("auth.Credentials")
def test_invalid_token_returns_false(mock_creds_cls, monkeypatch, tmp_path):
    _set_full_env(monkeypatch)
    (tmp_path / "oauth_token.json").write_text(json.dumps({"refresh_token": "bad-token"}))
    mock_creds = MagicMock()
    mock_creds.refresh.side_effect = Exception("token expired")
    mock_creds_cls.return_value = mock_creds

    auth = make_auth(tmp_path)
    assert auth.validate_and_init() is False
    assert not auth.is_authenticated


@patch("auth.Credentials")
def test_invalid_token_removes_file(mock_creds_cls, monkeypatch, tmp_path):
    _set_full_env(monkeypatch)
    token_file = tmp_path / "oauth_token.json"
    token_file.write_text(json.dumps({"refresh_token": "bad-token"}))
    mock_creds = MagicMock()
    mock_creds.refresh.side_effect = Exception("token expired")
    mock_creds_cls.return_value = mock_creds

    make_auth(tmp_path).validate_and_init()
    assert not token_file.exists()


# ── build_auth_url ──────────────────────────────────────────────────────────

def test_build_auth_url_contains_expected_components(monkeypatch, tmp_path):
    _set_full_env(monkeypatch)
    auth = make_auth(tmp_path)
    auth.validate_and_init()

    url = auth.build_auth_url()
    assert "accounts.google.com" in url
    assert "client-id" in url
    assert "localhost" in url
    assert "offline" in url
    assert "gmail.modify" in url
    assert "gmail.send" in url


# ── exchange_code ───────────────────────────────────────────────────────────

def _make_token_response(refresh_token="rt-new", access_token="at-123"):
    return io.BytesIO(json.dumps({
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_in": 3600,
        "token_type": "Bearer",
    }).encode())


@patch("auth.urllib.request.urlopen")
def test_exchange_code_saves_token_file(mock_urlopen, monkeypatch, tmp_path):
    _set_full_env(monkeypatch)
    mock_urlopen.return_value = _make_token_response("rt-persisted")

    auth = make_auth(tmp_path)
    auth.validate_and_init()
    auth.exchange_code("auth-code-xyz")

    token_file = tmp_path / "oauth_token.json"
    assert token_file.exists()
    assert json.loads(token_file.read_text())["refresh_token"] == "rt-persisted"


@patch("auth.urllib.request.urlopen")
def test_exchange_code_sets_authenticated(mock_urlopen, monkeypatch, tmp_path):
    _set_full_env(monkeypatch)
    mock_urlopen.return_value = _make_token_response()

    auth = make_auth(tmp_path)
    auth.validate_and_init()
    auth.exchange_code("auth-code-xyz")

    assert auth.is_authenticated
    assert auth.credentials is not None


@patch("auth.urllib.request.urlopen")
def test_exchange_code_raises_if_no_refresh_token(mock_urlopen, monkeypatch, tmp_path):
    _set_full_env(monkeypatch)
    mock_urlopen.return_value = io.BytesIO(json.dumps({
        "access_token": "at-only",
        "expires_in": 3600,
    }).encode())

    auth = make_auth(tmp_path)
    auth.validate_and_init()
    with pytest.raises(ValueError, match="refresh_token"):
        auth.exchange_code("auth-code-xyz")


# ── scopes ──────────────────────────────────────────────────────────────────

def test_scopes_include_settings_basic(monkeypatch, tmp_path):
    _set_full_env(monkeypatch)
    auth = make_auth(tmp_path)
    assert "https://www.googleapis.com/auth/gmail.settings.basic" in auth.SCOPES
