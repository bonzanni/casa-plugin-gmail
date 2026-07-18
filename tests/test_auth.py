import pytest
from unittest.mock import patch, MagicMock


def make_auth():
    from auth import GmailAuth
    return GmailAuth()


# ── Env var validation ─────────────────────────────────────────────────────

def test_missing_impersonation_sa_exits(monkeypatch):
    monkeypatch.delenv("GMAIL_IMPERSONATION_SA", raising=False)
    monkeypatch.setenv("GMAIL_SUBJECT_EMAIL", "user@example.com")
    with pytest.raises(SystemExit):
        make_auth().validate_and_init()


def test_missing_subject_email_exits(monkeypatch):
    monkeypatch.setenv("GMAIL_IMPERSONATION_SA", "sa@project.iam.gserviceaccount.com")
    monkeypatch.delenv("GMAIL_SUBJECT_EMAIL", raising=False)
    with pytest.raises(SystemExit):
        make_auth().validate_and_init()


def test_impersonation_sa_must_be_service_account_email(monkeypatch):
    # Catches operator pasting a regular email in GMAIL_IMPERSONATION_SA
    monkeypatch.setenv("GMAIL_IMPERSONATION_SA", "notanSA@example.com")
    monkeypatch.setenv("GMAIL_SUBJECT_EMAIL", "user@example.com")
    with pytest.raises(SystemExit):
        make_auth().validate_and_init()


def test_subject_email_rejects_service_account_address(monkeypatch):
    # Catches operator putting SA email in both slots
    monkeypatch.setenv("GMAIL_IMPERSONATION_SA", "sa@project.iam.gserviceaccount.com")
    monkeypatch.setenv("GMAIL_SUBJECT_EMAIL", "sa@project.iam.gserviceaccount.com")
    with pytest.raises(SystemExit):
        make_auth().validate_and_init()


def test_subject_email_rejects_gmail_com(monkeypatch):
    # DWD requires Workspace; personal @gmail.com is rejected
    monkeypatch.setenv("GMAIL_IMPERSONATION_SA", "sa@project.iam.gserviceaccount.com")
    monkeypatch.setenv("GMAIL_SUBJECT_EMAIL", "user@gmail.com")
    with pytest.raises(SystemExit):
        make_auth().validate_and_init()


def test_subject_email_invalid_format_exits(monkeypatch):
    monkeypatch.setenv("GMAIL_IMPERSONATION_SA", "sa@project.iam.gserviceaccount.com")
    monkeypatch.setenv("GMAIL_SUBJECT_EMAIL", "notanemail")
    with pytest.raises(SystemExit):
        make_auth().validate_and_init()


# ── Auth error paths ────────────────────────────────────────────────────────

@patch("auth.google_auth_default")
def test_default_credentials_error_exits(mock_default, monkeypatch):
    from google.auth.exceptions import DefaultCredentialsError
    monkeypatch.setenv("GMAIL_IMPERSONATION_SA", "sa@project.iam.gserviceaccount.com")
    monkeypatch.setenv("GMAIL_SUBJECT_EMAIL", "user@example.com")
    mock_default.side_effect = DefaultCredentialsError("no credentials")
    with pytest.raises(SystemExit):
        make_auth().validate_and_init()


@patch("auth.google_auth_default")
def test_default_credentials_error_message_mentions_gcloud_login(mock_default, monkeypatch, capsys):
    from google.auth.exceptions import DefaultCredentialsError
    monkeypatch.setenv("GMAIL_IMPERSONATION_SA", "sa@project.iam.gserviceaccount.com")
    monkeypatch.setenv("GMAIL_SUBJECT_EMAIL", "user@example.com")
    mock_default.side_effect = DefaultCredentialsError("no credentials")
    with pytest.raises(SystemExit):
        make_auth().validate_and_init()
    assert "gcloud auth application-default login" in capsys.readouterr().err


@patch("auth.impersonated_credentials.Credentials")
@patch("auth.google_auth_default")
def test_refresh_error_exits(mock_default, mock_imp_creds, monkeypatch):
    from google.auth.exceptions import RefreshError
    monkeypatch.setenv("GMAIL_IMPERSONATION_SA", "sa@project.iam.gserviceaccount.com")
    monkeypatch.setenv("GMAIL_SUBJECT_EMAIL", "user@example.com")
    mock_default.return_value = (MagicMock(), "project")
    mock_creds = MagicMock()
    mock_creds.refresh.side_effect = RefreshError("permission denied")
    mock_imp_creds.return_value = mock_creds
    with pytest.raises(SystemExit):
        make_auth().validate_and_init()


@patch("auth.impersonated_credentials.Credentials")
@patch("auth.google_auth_default")
def test_refresh_error_message_mentions_token_creator(mock_default, mock_imp_creds, monkeypatch, capsys):
    from google.auth.exceptions import RefreshError
    monkeypatch.setenv("GMAIL_IMPERSONATION_SA", "sa@project.iam.gserviceaccount.com")
    monkeypatch.setenv("GMAIL_SUBJECT_EMAIL", "user@example.com")
    mock_default.return_value = (MagicMock(), "project")
    mock_creds = MagicMock()
    mock_creds.refresh.side_effect = RefreshError("permission denied")
    mock_imp_creds.return_value = mock_creds
    with pytest.raises(SystemExit):
        make_auth().validate_and_init()
    err = capsys.readouterr().err
    assert "serviceAccountTokenCreator" in err or "add-iam-policy-binding" in err


# ── Happy path ──────────────────────────────────────────────────────────────

@patch("auth.impersonated_credentials.Credentials")
@patch("auth.google_auth_default")
def test_valid_config_initialises(mock_default, mock_imp_creds, monkeypatch):
    monkeypatch.setenv("GMAIL_IMPERSONATION_SA", "sa@project.iam.gserviceaccount.com")
    monkeypatch.setenv("GMAIL_SUBJECT_EMAIL", "user@workspace.example.com")
    mock_source = MagicMock()
    mock_default.return_value = (mock_source, "project")
    mock_creds = MagicMock()
    mock_imp_creds.return_value = mock_creds

    auth = make_auth()
    auth.validate_and_init()

    mock_imp_creds.assert_called_once_with(
        source_credentials=mock_source,
        target_principal="sa@project.iam.gserviceaccount.com",
        target_scopes=auth.SCOPES,
        subject="user@workspace.example.com",
    )
    mock_creds.refresh.assert_called_once()
    assert auth.credentials is mock_creds
    assert auth.subject_email == "user@workspace.example.com"


@patch("auth.impersonated_credentials.Credentials")
@patch("auth.google_auth_default")
def test_scopes_include_settings_basic(mock_default, mock_imp_creds, monkeypatch):
    monkeypatch.setenv("GMAIL_IMPERSONATION_SA", "sa@project.iam.gserviceaccount.com")
    monkeypatch.setenv("GMAIL_SUBJECT_EMAIL", "user@workspace.example.com")
    mock_default.return_value = (MagicMock(), "project")
    mock_imp_creds.return_value = MagicMock()
    auth = make_auth()
    auth.validate_and_init()
    assert "https://www.googleapis.com/auth/gmail.settings.basic" in auth.SCOPES
