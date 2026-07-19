import pytest
from unittest.mock import patch, MagicMock


def make_auth():
    from auth import GmailAuth
    return GmailAuth()


_FULL_ENV = {
    "GMAIL_CLIENT_ID": "client-id",
    "GMAIL_CLIENT_SECRET": "client-secret",
    "GMAIL_REFRESH_TOKEN": "refresh-token",
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
    "GMAIL_REFRESH_TOKEN",
    "GMAIL_USER_EMAIL",
])
def test_missing_required_var_exits(missing_var, monkeypatch):
    _set_full_env(monkeypatch, **{missing_var: None})
    with pytest.raises(SystemExit):
        make_auth().validate_and_init()


@pytest.mark.parametrize("missing_var", [
    "GMAIL_CLIENT_ID",
    "GMAIL_CLIENT_SECRET",
    "GMAIL_REFRESH_TOKEN",
    "GMAIL_USER_EMAIL",
])
def test_missing_required_var_names_it_in_stderr(missing_var, monkeypatch, capsys):
    _set_full_env(monkeypatch, **{missing_var: None})
    with pytest.raises(SystemExit):
        make_auth().validate_and_init()
    assert missing_var in capsys.readouterr().err


# ── Auth error paths ────────────────────────────────────────────────────────

@patch("auth.Credentials")
def test_refresh_error_exits(mock_creds_cls, monkeypatch):
    _set_full_env(monkeypatch)
    mock_creds = MagicMock()
    mock_creds.refresh.side_effect = Exception("token expired")
    mock_creds_cls.return_value = mock_creds
    with pytest.raises(SystemExit):
        make_auth().validate_and_init()


@patch("auth.Credentials")
def test_refresh_error_message_mentions_env_vars(mock_creds_cls, monkeypatch, capsys):
    _set_full_env(monkeypatch)
    mock_creds = MagicMock()
    mock_creds.refresh.side_effect = Exception("token expired")
    mock_creds_cls.return_value = mock_creds
    with pytest.raises(SystemExit):
        make_auth().validate_and_init()
    err = capsys.readouterr().err
    assert "GMAIL_REFRESH_TOKEN" in err or "GMAIL_CLIENT_SECRET" in err


# ── Happy path ──────────────────────────────────────────────────────────────

@patch("auth.Credentials")
def test_valid_config_initialises(mock_creds_cls, monkeypatch):
    _set_full_env(monkeypatch)
    mock_creds = MagicMock()
    mock_creds_cls.return_value = mock_creds

    auth = make_auth()
    auth.validate_and_init()

    mock_creds_cls.assert_called_once_with(
        token=None,
        refresh_token="refresh-token",
        token_uri="https://oauth2.googleapis.com/token",
        client_id="client-id",
        client_secret="client-secret",
        scopes=auth.SCOPES,
    )
    mock_creds.refresh.assert_called_once()
    assert auth.credentials is mock_creds
    assert auth.subject_email == "user@workspace.example.com"


@patch("auth.Credentials")
def test_scopes_include_settings_basic(mock_creds_cls, monkeypatch):
    _set_full_env(monkeypatch)
    mock_creds_cls.return_value = MagicMock()
    auth = make_auth()
    auth.validate_and_init()
    assert "https://www.googleapis.com/auth/gmail.settings.basic" in auth.SCOPES
