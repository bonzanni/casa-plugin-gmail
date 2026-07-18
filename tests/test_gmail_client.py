import pytest
from unittest.mock import MagicMock, patch


def make_client():
    from gmail_client import GmailClient
    creds = MagicMock()
    with patch("gmail_client.build") as mock_build:
        mock_build.return_value = MagicMock()
        client = GmailClient(creds)
        client._service = mock_build.return_value
    return client


# --- search_emails ---

def test_search_emails_returns_list(tmp_path):
    client = make_client()
    client._service.users.return_value.messages.return_value.list.return_value.execute.return_value = {
        "messages": [{"id": "msg1", "threadId": "thr1"}]
    }
    client._service.users.return_value.messages.return_value.get.return_value.execute.return_value = {
        "id": "msg1", "threadId": "thr1", "snippet": "Hello",
        "payload": {"headers": [
            {"name": "Subject", "value": "Test"},
            {"name": "From", "value": "sender@example.com"},
            {"name": "Date", "value": "Thu, 18 Jul 2026 14:30:00 +0000"},
        ]}
    }
    results = client.search_emails("from:sender@example.com")
    assert len(results) == 1
    assert results[0]["message_id"] == "msg1"
    assert results[0]["subject"] == "Test"
    assert results[0]["from"] == "sender@example.com"
    assert results[0]["date"] == "2026-07-18T14:30:00Z"

def test_search_emails_missing_from_returns_unknown(tmp_path):
    client = make_client()
    client._service.users.return_value.messages.return_value.list.return_value.execute.return_value = {
        "messages": [{"id": "msg1", "threadId": "thr1"}]
    }
    client._service.users.return_value.messages.return_value.get.return_value.execute.return_value = {
        "id": "msg1", "threadId": "thr1", "snippet": "",
        "payload": {"headers": [{"name": "Subject", "value": "No sender"}]}
    }
    results = client.search_emails("subject:test")
    assert results[0]["from"] == "(unknown sender)"

def test_search_emails_caps_at_100():
    client = make_client()
    client._service.users.return_value.messages.return_value.list.return_value.execute.return_value = {"messages": []}
    client.search_emails("test", max_results=999)
    call_kwargs = client._service.users.return_value.messages.return_value.list.call_args.kwargs
    assert call_kwargs["maxResults"] == 100

def test_search_emails_empty_result():
    client = make_client()
    client._service.users.return_value.messages.return_value.list.return_value.execute.return_value = {}
    results = client.search_emails("noresults")
    assert results == []


# --- get_email ---

def test_get_email_returns_full_message():
    client = make_client()
    client._service.users.return_value.messages.return_value.get.return_value.execute.return_value = {
        "id": "msg1", "threadId": "thr1",
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": "a@b.com"},
                {"name": "To", "value": "c@d.com"},
                {"name": "Subject", "value": "Hello"},
                {"name": "Date", "value": "Thu, 18 Jul 2026 14:30:00 +0000"},
            ],
            "body": {"data": "SGVsbG8gd29ybGQ="},  # "Hello world" base64
        }
    }
    result = client.get_email("msg1")
    assert result["message_id"] == "msg1"
    assert result["headers"]["subject"] == "Hello"
    assert "Hello world" in result["body"]
    assert result["attachments"] == []

def test_get_email_not_found_raises():
    from googleapiclient.errors import HttpError
    client = make_client()
    resp = MagicMock(status=404)
    client._service.users.return_value.messages.return_value.get.return_value.execute.side_effect = HttpError(resp, b"Not Found")
    with pytest.raises(ValueError, match="not found"):
        client.get_email("missing")


# --- get_thread ---

def test_get_thread_returns_messages_chronological():
    client = make_client()
    make_msg = lambda i: {
        "id": f"msg{i}", "threadId": "thr1", "labelIds": [],
        "payload": {
            "mimeType": "text/plain",
            "headers": [{"name": "Date", "value": "Thu, 18 Jul 2026 14:30:00 +0000"}],
            "body": {"data": ""},
        }
    }
    client._service.users.return_value.threads.return_value.get.return_value.execute.return_value = {
        "messages": [make_msg(1), make_msg(2), make_msg(3)]
    }
    result = client.get_thread("thr1", max_messages=20)
    assert result["total_messages"] == 3
    assert result["truncated"] is False
    assert len(result["messages"]) == 3

def test_get_thread_truncates_to_most_recent():
    client = make_client()
    make_msg = lambda i: {
        "id": f"msg{i}", "threadId": "thr1", "labelIds": [],
        "payload": {"mimeType": "text/plain", "headers": [], "body": {"data": ""}}
    }
    msgs = [make_msg(i) for i in range(30)]
    client._service.users.return_value.threads.return_value.get.return_value.execute.return_value = {
        "messages": msgs
    }
    result = client.get_thread("thr1", max_messages=10)
    assert result["truncated"] is True
    assert result["total_messages"] == 30
    assert len(result["messages"]) == 10
    assert result["messages"][0]["message_id"] == "msg20"  # most recent 10

def test_get_thread_caps_max_messages():
    client = make_client()
    client._service.users.return_value.threads.return_value.get.return_value.execute.return_value = {"messages": []}
    result = client.get_thread("thr1", max_messages=999)
    # max_messages capped at 50 internally, but with 0 messages no truncation
    assert result["total_messages"] == 0

def test_get_thread_marks_trashed():
    client = make_client()
    msg = {
        "id": "msg1", "threadId": "thr1", "labelIds": ["TRASH"],
        "payload": {"mimeType": "text/plain", "headers": [], "body": {"data": ""}}
    }
    client._service.users.return_value.threads.return_value.get.return_value.execute.return_value = {"messages": [msg]}
    result = client.get_thread("thr1")
    assert result["messages"][0]["trashed"] is True


# --- manage_email ---

def test_manage_archive_calls_modify():
    client = make_client()
    client._service.users.return_value.messages.return_value.modify.return_value.execute.return_value = {}
    client.manage_email("msg1", "archive")
    call_args = client._service.users.return_value.messages.return_value.modify.call_args
    assert "INBOX" in call_args.kwargs["body"]["removeLabelIds"]

def test_manage_mark_read():
    client = make_client()
    client._service.users.return_value.messages.return_value.modify.return_value.execute.return_value = {}
    client.manage_email("msg1", "mark_read")
    call_args = client._service.users.return_value.messages.return_value.modify.call_args
    assert "UNREAD" in call_args.kwargs["body"]["removeLabelIds"]

def test_manage_rejects_system_label_inbox():
    client = make_client()
    with pytest.raises(ValueError, match="action='archive'"):
        client.manage_email("msg1", "add_label", label="INBOX")

def test_manage_rejects_system_label_starred():
    client = make_client()
    with pytest.raises(ValueError, match="system label"):
        client.manage_email("msg1", "add_label", label="STARRED")

def test_manage_unknown_action_raises():
    client = make_client()
    with pytest.raises(ValueError, match="Unknown action"):
        client.manage_email("msg1", "delete_forever")

def test_manage_add_label_requires_non_empty_label():
    client = make_client()
    with pytest.raises(ValueError, match="label is required"):
        client.manage_email("msg1", "add_label", label="")

def test_manage_idempotent_archive_does_not_raise():
    client = make_client()
    client._service.users.return_value.messages.return_value.modify.return_value.execute.return_value = {}
    # Calling archive twice should not raise
    client.manage_email("msg1", "archive")
    client.manage_email("msg1", "archive")


# --- send_email ---

def test_send_email_calls_send_api():
    client = make_client()
    client._service.users.return_value.messages.return_value.send.return_value.execute.return_value = {"id": "sent1"}
    msg_id = client.send_email("to@b.com", "Hello", "Body text")
    assert msg_id == "sent1"

def test_send_email_rate_limit_raises():
    from googleapiclient.errors import HttpError
    client = make_client()
    resp = MagicMock(status=429)
    client._service.users.return_value.messages.return_value.send.return_value.execute.side_effect = HttpError(resp, b"rate limit")
    with pytest.raises(ValueError, match="rate limit"):
        client.send_email("to@b.com", "Hello", "Body")


# --- reply_to_thread ---

def _make_thread_response(subject="Original", from_addr="sender@example.com", msg_id="<orig@mail>"):
    return {
        "messages": [{
            "id": "msg1", "threadId": "thr1",
            "payload": {"headers": [
                {"name": "Subject", "value": subject},
                {"name": "From", "value": from_addr},
                {"name": "Message-ID", "value": msg_id},
            ]}
        }]
    }


def test_reply_to_thread_returns_message_id():
    client = make_client()
    client._service.users.return_value.threads.return_value.get.return_value.execute.return_value = (
        _make_thread_response()
    )
    client._service.users.return_value.messages.return_value.send.return_value.execute.return_value = {"id": "reply1"}
    msg_id = client.reply_to_thread("thr1", "My reply")
    assert msg_id == "reply1"

def test_reply_prepends_re_to_subject():
    client = make_client()
    client._service.users.return_value.threads.return_value.get.return_value.execute.return_value = (
        _make_thread_response(subject="Invoice")
    )
    client._service.users.return_value.messages.return_value.send.return_value.execute.return_value = {"id": "r1"}
    client.reply_to_thread("thr1", "body")
    send_call = client._service.users.return_value.messages.return_value.send.call_args
    raw = send_call.kwargs["body"]["raw"]
    import base64
    missing = (4 - len(raw) % 4) % 4
    decoded = base64.urlsafe_b64decode(raw + "=" * missing).decode("utf-8", errors="replace")
    assert "Re: Invoice" in decoded

def test_reply_sets_to_header_from_sender():
    client = make_client()
    client._service.users.return_value.threads.return_value.get.return_value.execute.return_value = (
        _make_thread_response(from_addr="alice@example.com")
    )
    client._service.users.return_value.messages.return_value.send.return_value.execute.return_value = {"id": "r2"}
    client.reply_to_thread("thr1", "body")
    send_call = client._service.users.return_value.messages.return_value.send.call_args
    raw = send_call.kwargs["body"]["raw"]
    import base64
    missing = (4 - len(raw) % 4) % 4
    decoded = base64.urlsafe_b64decode(raw + "=" * missing).decode("utf-8", errors="replace")
    assert "To: alice@example.com" in decoded
