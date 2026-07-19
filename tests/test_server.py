import json
import pytest
from unittest.mock import MagicMock


def _setup_authenticated(monkeypatch):
    import server
    monkeypatch.setattr(server, "_authenticated", True)


def test_send_email_passes_from_address_to_client(monkeypatch):
    import server
    mock_client = MagicMock()
    mock_client.send_email.return_value = "sent1"
    mock_log = MagicMock()
    mock_log.check.return_value = None

    monkeypatch.setattr(server, "_client", mock_client)
    monkeypatch.setattr(server, "_log", mock_log)
    _setup_authenticated(monkeypatch)

    server.send_email(to="to@b.com", subject="Hi", body="body", from_address="alias@workspace.example.com")
    mock_client.send_email.assert_called_once_with(
        "to@b.com", "Hi", "body", [], from_address="alias@workspace.example.com"
    )


def test_send_email_passes_empty_from_address_to_client(monkeypatch):
    import server
    mock_client = MagicMock()
    mock_client.send_email.return_value = "sent2"
    mock_log = MagicMock()
    mock_log.check.return_value = None

    monkeypatch.setattr(server, "_client", mock_client)
    monkeypatch.setattr(server, "_log", mock_log)
    _setup_authenticated(monkeypatch)

    server.send_email(to="to@b.com", subject="Hi", body="body")
    mock_client.send_email.assert_called_once_with(
        "to@b.com", "Hi", "body", [], from_address=""
    )


def test_reply_to_thread_passes_from_address_to_client(monkeypatch):
    import server
    mock_client = MagicMock()
    mock_client.reply_to_thread.return_value = "reply1"
    mock_log = MagicMock()
    mock_log.check.return_value = None

    monkeypatch.setattr(server, "_client", mock_client)
    monkeypatch.setattr(server, "_log", mock_log)
    _setup_authenticated(monkeypatch)

    server.reply_to_thread(
        thread_id="thr1",
        display_subject="Re: Hello",
        body="body",
        from_address="alias@workspace.example.com",
    )
    mock_client.reply_to_thread.assert_called_once_with(
        "thr1", "body", [], from_address="alias@workspace.example.com"
    )


def test_reply_to_thread_passes_empty_from_address_to_client(monkeypatch):
    import server
    mock_client = MagicMock()
    mock_client.reply_to_thread.return_value = "reply2"
    mock_log = MagicMock()
    mock_log.check.return_value = None

    monkeypatch.setattr(server, "_client", mock_client)
    monkeypatch.setattr(server, "_log", mock_log)
    _setup_authenticated(monkeypatch)

    server.reply_to_thread(thread_id="thr1", display_subject="Re: Hello", body="body")
    mock_client.reply_to_thread.assert_called_once_with(
        "thr1", "body", [], from_address=""
    )


def test_unauthenticated_tools_raise(monkeypatch):
    import server
    monkeypatch.setattr(server, "_authenticated", False)

    with pytest.raises(ValueError, match="not authenticated"):
        server.search_emails("from:me")


def test_gmail_auth_start_returns_url(monkeypatch):
    import server
    mock_auth = MagicMock()
    mock_auth.build_auth_url.return_value = "https://accounts.google.com/o/oauth2/auth?client_id=x"
    monkeypatch.setattr(server, "_auth", mock_auth)

    result = json.loads(server.gmail_auth_start())
    assert "auth_url" in result
    assert "instructions" in result
    assert "accounts.google.com" in result["auth_url"]


def test_gmail_auth_complete_parses_code_and_reinitialises(monkeypatch):
    import server
    mock_auth = MagicMock()
    mock_auth.credentials = MagicMock()
    monkeypatch.setattr(server, "_auth", mock_auth)
    monkeypatch.setattr(server, "_authenticated", False)

    result = json.loads(server.gmail_auth_complete("http://localhost:8080?code=abc123&scope=gmail"))

    mock_auth.exchange_code.assert_called_once_with("abc123")
    assert result["status"] == "authenticated"


def test_gmail_auth_complete_rejects_error_param(monkeypatch):
    import server

    with pytest.raises(ValueError, match="OAuth error"):
        server.gmail_auth_complete("http://localhost:8080?error=access_denied")


def test_gmail_auth_complete_rejects_missing_code(monkeypatch):
    import server

    with pytest.raises(ValueError, match="No authorization code"):
        server.gmail_auth_complete("http://localhost:8080?scope=gmail")
