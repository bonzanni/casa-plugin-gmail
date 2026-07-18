import pytest
from unittest.mock import MagicMock


def test_send_email_passes_from_address_to_client(monkeypatch):
    import server
    mock_client = MagicMock()
    mock_client.send_email.return_value = "sent1"
    mock_log = MagicMock()
    mock_log.check.return_value = None

    monkeypatch.setattr(server, "_client", mock_client)
    monkeypatch.setattr(server, "_log", mock_log)

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

    server.send_email(to="to@b.com", subject="Hi", body="body")
    # from_address omitted — passes "" to client; Gmail uses DWD subject's primary address by default
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

    server.reply_to_thread(thread_id="thr1", display_subject="Re: Hello", body="body")
    mock_client.reply_to_thread.assert_called_once_with(
        "thr1", "body", [], from_address=""
    )
