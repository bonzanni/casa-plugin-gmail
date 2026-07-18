import json
import pytest
from unittest.mock import patch, MagicMock


VALID_SA = {
    "type": "service_account",
    "private_key": "-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----\n",
    "client_email": "svc@proj.iam.gserviceaccount.com",
    "token_uri": "https://oauth2.googleapis.com/token",
}


def make_auth():
    from auth import GmailAuth
    return GmailAuth()


def test_missing_sa_json_exits(monkeypatch):
    monkeypatch.delenv("GMAIL_SERVICE_ACCOUNT_JSON", raising=False)
    monkeypatch.setenv("GMAIL_USER_EMAIL", "user@example.com")
    with pytest.raises(SystemExit):
        make_auth().validate_and_init()


def test_missing_user_email_exits(monkeypatch):
    monkeypatch.setenv("GMAIL_SERVICE_ACCOUNT_JSON", json.dumps(VALID_SA))
    monkeypatch.delenv("GMAIL_USER_EMAIL", raising=False)
    with pytest.raises(SystemExit):
        make_auth().validate_and_init()


def test_invalid_json_exits(monkeypatch):
    monkeypatch.setenv("GMAIL_SERVICE_ACCOUNT_JSON", "not-json{")
    monkeypatch.setenv("GMAIL_USER_EMAIL", "user@example.com")
    with pytest.raises(SystemExit):
        make_auth().validate_and_init()


def test_wrong_type_exits(monkeypatch):
    monkeypatch.setenv("GMAIL_SERVICE_ACCOUNT_JSON", json.dumps({**VALID_SA, "type": "authorized_user"}))
    monkeypatch.setenv("GMAIL_USER_EMAIL", "user@example.com")
    with pytest.raises(SystemExit):
        make_auth().validate_and_init()


def test_missing_required_field_exits(monkeypatch):
    sa = {k: v for k, v in VALID_SA.items() if k != "private_key"}
    monkeypatch.setenv("GMAIL_SERVICE_ACCOUNT_JSON", json.dumps(sa))
    monkeypatch.setenv("GMAIL_USER_EMAIL", "user@example.com")
    with pytest.raises(SystemExit):
        make_auth().validate_and_init()


def test_invalid_email_no_at_exits(monkeypatch):
    monkeypatch.setenv("GMAIL_SERVICE_ACCOUNT_JSON", json.dumps(VALID_SA))
    monkeypatch.setenv("GMAIL_USER_EMAIL", "notanemail")
    with pytest.raises(SystemExit):
        make_auth().validate_and_init()


def test_invalid_email_no_dot_exits(monkeypatch):
    monkeypatch.setenv("GMAIL_SERVICE_ACCOUNT_JSON", json.dumps(VALID_SA))
    monkeypatch.setenv("GMAIL_USER_EMAIL", "user@nodot")
    with pytest.raises(SystemExit):
        make_auth().validate_and_init()


@patch("auth.service_account.Credentials.from_service_account_info")
def test_valid_config_initialises(mock_creds, monkeypatch):
    monkeypatch.setenv("GMAIL_SERVICE_ACCOUNT_JSON", json.dumps(VALID_SA))
    monkeypatch.setenv("GMAIL_USER_EMAIL", "user@workspace.example.com")
    mock_creds.return_value = MagicMock()
    auth = make_auth()
    auth.validate_and_init()
    mock_creds.assert_called_once_with(VALID_SA, scopes=auth.SCOPES, subject="user@workspace.example.com")
    assert auth.credentials is not None
