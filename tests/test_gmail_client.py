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


# --- HTML bodies and links (issue #1) ---

def _b64(text: str) -> str:
    import base64
    return base64.urlsafe_b64encode(text.encode()).decode()

def _alternative_payload(plain: str, html: str) -> dict:
    return {
        "mimeType": "multipart/alternative",
        "headers": [],
        "parts": [
            {"mimeType": "text/plain", "body": {"data": _b64(plain)}},
            {"mimeType": "text/html", "body": {"data": _b64(html)}},
        ],
    }

def test_get_email_extracts_links_from_html_part():
    client = make_client()
    html = '<html><body><p>Hi,</p><a href="https://example.com/magic?t=abc">Sign in to Provider</a></body></html>'
    client._service.users.return_value.messages.return_value.get.return_value.execute.return_value = {
        "id": "msg1", "threadId": "thr1",
        "payload": _alternative_payload("Sign in to Provider", html),
    }
    result = client.get_email("msg1")
    assert result["body"] == "Sign in to Provider"
    assert result["links"] == [
        {"text": "Sign in to Provider", "href": "https://example.com/magic?t=abc"}
    ]

def test_get_email_plain_only_has_empty_links():
    client = make_client()
    client._service.users.return_value.messages.return_value.get.return_value.execute.return_value = {
        "id": "msg1", "threadId": "thr1",
        "payload": {
            "mimeType": "text/plain",
            "headers": [],
            "body": {"data": _b64("just text, no html part")},
        },
    }
    result = client.get_email("msg1")
    assert result["links"] == []

def test_get_email_html_only_falls_back_to_html_body():
    client = make_client()
    html = '<html><body><a href="https://example.com/confirm">Confirm</a></body></html>'
    client._service.users.return_value.messages.return_value.get.return_value.execute.return_value = {
        "id": "msg1", "threadId": "thr1",
        "payload": {
            "mimeType": "text/html",
            "headers": [],
            "body": {"data": _b64(html)},
        },
    }
    result = client.get_email("msg1")
    assert "https://example.com/confirm" in result["body"]
    assert result["links"] == [{"text": "Confirm", "href": "https://example.com/confirm"}]

def test_get_email_links_found_in_nested_multipart():
    client = make_client()
    html = '<div><a href="https://example.com/unsub">Unsubscribe</a></div>'
    client._service.users.return_value.messages.return_value.get.return_value.execute.return_value = {
        "id": "msg1", "threadId": "thr1",
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [],
            "parts": [
                _alternative_payload("plain body", html),
                {"mimeType": "application/pdf", "filename": "a.pdf",
                 "body": {"attachmentId": "att1", "size": 10}},
            ],
        },
    }
    result = client.get_email("msg1")
    assert result["body"] == "plain body"
    assert result["links"] == [{"text": "Unsubscribe", "href": "https://example.com/unsub"}]

def test_get_email_links_skip_anchors_without_href():
    client = make_client()
    html = '<a name="top">Anchor</a><a href="">Empty</a><a href="https://x.example/a">Real</a>'
    client._service.users.return_value.messages.return_value.get.return_value.execute.return_value = {
        "id": "msg1", "threadId": "thr1",
        "payload": _alternative_payload("body", html),
    }
    result = client.get_email("msg1")
    assert result["links"] == [{"text": "Real", "href": "https://x.example/a"}]

def test_get_email_html_attachment_is_not_body_or_links():
    # An HTML *attachment* must not supply the body fallback or the links
    # field — its links belong to the attachment, not the message.
    client = make_client()
    html = '<a href="https://attacker.example/pay">Pay invoice</a>'
    client._service.users.return_value.messages.return_value.get.return_value.execute.return_value = {
        "id": "msg1", "threadId": "thr1",
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [],
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64("See attached invoice.")}},
                {"mimeType": "text/html", "filename": "invoice.html",
                 "body": {"data": _b64(html), "attachmentId": "att1", "size": 10}},
            ],
        },
    }
    result = client.get_email("msg1")
    assert result["body"] == "See attached invoice."
    assert result["links"] == []

def test_get_email_inline_data_attachment_is_not_body_or_links():
    # Gmail's contract (discovery doc): filename is present ONLY on attachment
    # parts, and a missing attachmentId means the attachment's content is
    # inline in body.data. Such a part is still an attachment, never the body.
    client = make_client()
    html = '<a href="https://attacker.example/pay">Pay invoice</a>'
    client._service.users.return_value.messages.return_value.get.return_value.execute.return_value = {
        "id": "msg1", "threadId": "thr1",
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [],
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64("See attached.")}},
                {"mimeType": "text/html", "filename": "invoice.html",
                 "body": {"data": _b64(html)}},
            ],
        },
    }
    result = client.get_email("msg1")
    assert result["body"] == "See attached."
    assert result["links"] == []

def test_get_email_duplicate_href_keeps_first_like_rendering():
    client = make_client()
    html = '<a href="https://rendered.example/confirm" href="https://wrong.example/confirm">Confirm</a>'
    client._service.users.return_value.messages.return_value.get.return_value.execute.return_value = {
        "id": "msg1", "threadId": "thr1",
        "payload": _alternative_payload("body", html),
    }
    result = client.get_email("msg1")
    assert result["links"] == [{"text": "Confirm", "href": "https://rendered.example/confirm"}]

def test_get_email_mime_type_with_parameters_still_matches():
    client = make_client()
    client._service.users.return_value.messages.return_value.get.return_value.execute.return_value = {
        "id": "msg1", "threadId": "thr1",
        "payload": {
            "mimeType": "multipart/alternative; boundary=x",
            "headers": [],
            "parts": [
                {"mimeType": "text/plain; charset=utf-8", "body": {"data": _b64("Plain")}},
                {"mimeType": "TEXT/HTML; charset=utf-8",
                 "body": {"data": _b64('<a href="https://example.test/v">Hi</a>')}},
            ],
        },
    }
    result = client.get_email("msg1")
    assert result["body"] == "Plain"
    assert result["links"] == [{"text": "Hi", "href": "https://example.test/v"}]

def test_get_thread_messages_include_links():
    client = make_client()
    html = '<a href="https://example.com/verify">Verify</a>'
    msg = {
        "id": "msg1", "threadId": "thr1", "labelIds": [],
        "payload": _alternative_payload("Verify", html),
    }
    client._service.users.return_value.threads.return_value.get.return_value.execute.return_value = {"messages": [msg]}
    result = client.get_thread("thr1")
    assert result["messages"][0]["links"] == [
        {"text": "Verify", "href": "https://example.com/verify"}
    ]


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


# --- list_send_as ---

def test_list_send_as_returns_aliases():
    client = make_client()
    client._service.users.return_value.settings.return_value.sendAs.return_value.list.return_value.execute.return_value = {
        "sendAs": [
            {
                "sendAsEmail": "user@workspace.example.com",
                "displayName": "the user",
                "isDefault": True,
                "isPrimary": True,
                "replyToAddress": "",
                "verificationStatus": "accepted",
            },
            {
                "sendAsEmail": "user+alias@workspace.example.com",
                "displayName": "the user Alias",
                "isDefault": False,
                "isPrimary": False,
                "replyToAddress": "user@workspace.example.com",
                "verificationStatus": "pending",
            },
        ]
    }
    result = client.list_send_as()
    assert len(result) == 2
    assert result[0]["send_as_email"] == "user@workspace.example.com"
    assert result[0]["is_primary"] is True
    assert result[0]["verification_status"] == "accepted"
    assert result[1]["send_as_email"] == "user+alias@workspace.example.com"
    assert result[1]["reply_to_address"] == "user@workspace.example.com"
    assert result[1]["verification_status"] == "pending"


def test_list_send_as_empty():
    client = make_client()
    client._service.users.return_value.settings.return_value.sendAs.return_value.list.return_value.execute.return_value = {}
    result = client.list_send_as()
    assert result == []


def test_list_send_as_http_error_raises():
    from googleapiclient.errors import HttpError
    client = make_client()
    resp = MagicMock(status=403)
    client._service.users.return_value.settings.return_value.sendAs.return_value.list.return_value.execute.side_effect = HttpError(resp, b"Forbidden")
    with pytest.raises(ValueError):
        client.list_send_as()


# --- send_email with from_address ---

def test_send_email_sets_from_header_no_attachments():
    client = make_client()
    client._service.users.return_value.messages.return_value.send.return_value.execute.return_value = {"id": "sent2"}
    client.send_email("to@b.com", "Hello", "Body", from_address="alias@workspace.example.com")
    send_call = client._service.users.return_value.messages.return_value.send.call_args
    raw = send_call.kwargs["body"]["raw"]
    import base64
    decoded = base64.urlsafe_b64decode(raw + "=" * ((4 - len(raw) % 4) % 4)).decode("utf-8", errors="replace")
    assert "From: alias@workspace.example.com" in decoded


def test_send_email_sets_from_header_with_attachments(tmp_path):
    client = make_client()
    client._service.users.return_value.messages.return_value.send.return_value.execute.return_value = {"id": "sent3"}
    client.send_email("to@b.com", "Hello", "Body", attachments=[("file.txt", b"content")], from_address="alias@workspace.example.com")
    send_call = client._service.users.return_value.messages.return_value.send.call_args
    raw = send_call.kwargs["body"]["raw"]
    import base64
    decoded = base64.urlsafe_b64decode(raw + "=" * ((4 - len(raw) % 4) % 4)).decode("utf-8", errors="replace")
    assert "From: alias@workspace.example.com" in decoded
    assert 'filename="file.txt"' in decoded
    assert base64.b64encode(b"content").decode() in decoded


# --- reply_to_thread with from_address ---

def test_reply_to_thread_sets_from_header():
    client = make_client()
    client._service.users.return_value.threads.return_value.get.return_value.execute.return_value = (
        _make_thread_response()
    )
    client._service.users.return_value.messages.return_value.send.return_value.execute.return_value = {"id": "r3"}
    client.reply_to_thread("thr1", "body", from_address="alias@workspace.example.com")
    send_call = client._service.users.return_value.messages.return_value.send.call_args
    raw = send_call.kwargs["body"]["raw"]
    import base64
    decoded = base64.urlsafe_b64decode(raw + "=" * ((4 - len(raw) % 4) % 4)).decode("utf-8", errors="replace")
    assert "From: alias@workspace.example.com" in decoded


# --- get_profile_email ---

def test_get_profile_email_returns_address():
    client = make_client()
    client._service.users.return_value.getProfile.return_value.execute.return_value = {
        "emailAddress": "user@workspace.example.com", "messagesTotal": 100,
    }
    assert client.get_profile_email() == "user@workspace.example.com"
    client._service.users.return_value.getProfile.assert_called_once_with(userId="me")


def test_get_profile_email_http_error_translates():
    from googleapiclient.errors import HttpError
    client = make_client()
    resp = MagicMock(status=403)
    client._service.users.return_value.getProfile.return_value.execute.side_effect = (
        HttpError(resp, b"Forbidden")
    )
    with pytest.raises(ValueError):
        client.get_profile_email()


def test_get_profile_email_missing_address_returns_empty_string():
    """The caller's account-mismatch check must fail closed: '' must never
    equal a real GMAIL_USER_EMAIL."""
    client = make_client()
    client._service.users.return_value.getProfile.return_value.execute.return_value = {
        "messagesTotal": 100,
    }
    assert client.get_profile_email() == ""


# --- #6: attachment ids survive a refetch ---
# Gmail's attachmentId is only good for the messages.get response it came
# from (no stability is documented; live, the listed id was never found on
# the refetch, 6/6). partId is "the immutable ID of the message part".

def _rotating_attachment_service(client):
    """messages.get hands out a fresh attachmentId on every call, as Gmail may."""
    fetches = []

    def _get(**kwargs):
        n = len(fetches)
        fetches.append(kwargs)
        resp = MagicMock()
        resp.execute.return_value = {
            "id": "msg1", "threadId": "thr1",
            "payload": {
                "partId": "", "mimeType": "multipart/mixed", "headers": [],
                "parts": [
                    {"partId": "0", "mimeType": "text/plain",
                     "body": {"data": _b64("See attached.")}},
                    {"partId": "1", "mimeType": "application/pdf", "filename": "invoice.pdf",
                     "body": {"attachmentId": f"ANGjdJ-fetch{n}-inv", "size": 1234}},
                    {"partId": "2", "mimeType": "image/png", "filename": "logo.png",
                     "body": {"attachmentId": f"ANGjdJ-fetch{n}-logo", "size": 99}},
                ],
            },
        }
        return resp

    client._service.users.return_value.messages.return_value.get.side_effect = _get
    return fetches


def test_listed_attachment_id_is_found_on_a_later_fetch():
    client = make_client()
    fetches = _rotating_attachment_service(client)
    listed = client.get_email("msg1")["attachments"]
    assert [a["filename"] for a in listed] == ["invoice.pdf", "logo.png"]
    found = client.find_attachment("msg1", listed[0]["attachment_id"])
    assert len(fetches) == 2, "find_attachment must refetch the message"
    assert found["filename"] == "invoice.pdf"
    assert found["size_bytes"] == 1234
    # ... and downloads with the id from ITS OWN fetch, not the listed one.
    assert found["gmail_attachment_id"] == "ANGjdJ-fetch1-inv"


def test_listed_attachment_ids_are_stable_and_distinct():
    client = make_client()
    _rotating_attachment_service(client)
    first = [a["attachment_id"] for a in client.get_email("msg1")["attachments"]]
    second = [a["attachment_id"] for a in client.get_email("msg1")["attachments"]]
    assert first == second
    assert len(set(first)) == 2


def test_find_attachment_accepts_the_current_gmail_id():
    # A caller holding a Gmail attachmentId from the same response still works.
    client = make_client()
    _rotating_attachment_service(client)
    found = client.find_attachment("msg1", "ANGjdJ-fetch0-logo")
    assert found["filename"] == "logo.png"
    assert found["gmail_attachment_id"] == "ANGjdJ-fetch0-logo"


def test_find_attachment_unknown_id_raises():
    client = make_client()
    _rotating_attachment_service(client)
    with pytest.raises(ValueError, match="not found on message msg1"):
        client.find_attachment("msg1", "7")


def test_attachment_on_the_root_part_gets_a_usable_id():
    # A single-part message whose payload IS the attachment: Gmail gives the
    # root part the empty partId, which is no id to hand a caller.
    client = make_client()
    fetches = []

    def _get(**kwargs):
        fetches.append(kwargs)
        resp = MagicMock()
        resp.execute.return_value = {
            "id": "msg1", "threadId": "thr1",
            "payload": {"partId": "", "mimeType": "application/pdf", "filename": "scan.pdf",
                        "headers": [],
                        "body": {"attachmentId": f"ANGjdJ-root{len(fetches)}", "size": 5}},
        }
        return resp

    client._service.users.return_value.messages.return_value.get.side_effect = _get
    [att] = client.get_email("msg1")["attachments"]
    assert att["attachment_id"]
    assert client.find_attachment("msg1", att["attachment_id"])["gmail_attachment_id"] == "ANGjdJ-root2"


def test_thread_messages_carry_the_same_stable_ids():
    client = make_client()
    _rotating_attachment_service(client)
    from_email = [a["attachment_id"] for a in client.get_email("msg1")["attachments"]]
    msg = client._service.users.return_value.messages.return_value.get(userId="me", id="msg1").execute()
    client._service.users.return_value.threads.return_value.get.return_value.execute.return_value = {
        "messages": [msg]}
    from_thread = [a["attachment_id"] for a in client.get_thread("thr1")["messages"][0]["attachments"]]
    assert from_thread == from_email


# --- The HTML part goes to the agent as it is ---

def _html_part(html: str) -> dict:
    return {"mimeType": "text/html", "body": {"data": _b64(html)}}


def _plain_part(text: str) -> dict:
    return {"mimeType": "text/plain", "body": {"data": _b64(text)}}


def _read(payload: dict) -> dict:
    client = make_client()
    client._service.users.return_value.messages.return_value.get.return_value.execute.return_value = {
        "id": "msg1", "threadId": "thr1", "payload": payload,
    }
    return client.get_email("msg1")


def _read_thread_message(payload: dict, **kwargs) -> dict:
    client = make_client()
    client._service.users.return_value.threads.return_value.get.return_value.execute.return_value = {
        "messages": [{"id": "msg1", "threadId": "thr1", "labelIds": [], "payload": payload}],
    }
    return client.get_thread("thr1", **kwargs)["messages"][0]


def test_placeholder_plain_part_comes_with_the_html():
    # The shape of a real booking reminder: a placeholder left in the sender's
    # template as the plain part, the whole message only in the HTML part.
    html = '<table><tr><td><p>Cancelling costs EUR 24.95.</p><a href="https://x.example/b">Booking</a></td></tr></table>'
    result = _read(_alternative_payload("Example text content", html))
    assert result["body"] == "Example text content"
    assert result["html"] == html
    assert result["links"] == [{"text": "Booking", "href": "https://x.example/b"}]


def test_plain_only_mail_has_no_html_field():
    assert "html" not in _read({**_plain_part("just text"), "headers": []})


def test_html_only_mail_is_the_body_once():
    result = _read({**_html_part("<p>only html</p>"), "headers": []})
    assert result["body"] == "<p>only html</p>"
    assert "html" not in result


def test_blank_plain_part_does_not_count_as_a_body():
    result = _read(_alternative_payload(" \r\n ", "<p>real</p>"))
    assert result["body"] == "<p>real</p>"
    assert "html" not in result


def test_html_reaches_the_agent_exactly_as_sent():
    html = ('<html><head><style>.a{color:red}</style></head><body><!-- note -->'
            '<div title="<!--">Pay EUR 120</div><!-- footer --><script>var a;</script></body></html>')
    assert _read(_alternative_payload("stub", html))["html"] == html


def test_mixed_message_keeps_every_text_part():
    result = _read({"mimeType": "multipart/mixed", "headers": [], "parts": [
        _plain_part("Before the photo."),
        {"mimeType": "image/png", "filename": "a.png", "body": {"attachmentId": "a", "size": 1}},
        _plain_part("After the photo."),
    ]})
    assert result["body"] == "Before the photo.\n\nAfter the photo."


def test_html_followed_by_a_plain_footer_keeps_both():
    result = _read({"mimeType": "multipart/mixed", "headers": [], "parts": [
        _html_part("<p>Transfer EUR 120 today.</p>"), _plain_part("Confidential."),
    ]})
    assert result["body"] == "Confidential."
    assert result["html"] == "<p>Transfer EUR 120 today.</p>"


def test_every_html_piece_comes_along():
    result = _read({"mimeType": "multipart/alternative", "headers": [], "parts": [
        _plain_part("Hi"),
        {"mimeType": "multipart/mixed", "parts": [
            _html_part("<p>first</p>"),
            {"mimeType": "application/pdf", "filename": "a.pdf", "body": {"attachmentId": "a", "size": 1}},
            _html_part("<p>second</p>"),
        ]},
    ]})
    assert result["html"] == "<p>first</p>\n\n<p>second</p>"


def test_alternative_takes_one_version_only():
    result = _read({"mimeType": "multipart/alternative", "headers": [], "parts": [
        _plain_part(""), _plain_part("first version"), _plain_part("second version"),
    ]})
    assert result["body"] == "first version"


def test_malformed_html_piece_does_not_swallow_the_next_pieces_links():
    result = _read({"mimeType": "multipart/mixed", "headers": [], "parts": [
        _html_part("<p>Forwarded</p><!-- footer"),
        _html_part('<a href="https://x.example/c">Confirm</a>'),
    ]})
    assert result["links"] == [{"text": "Confirm", "href": "https://x.example/c"}]


def test_thread_leaves_html_out_unless_asked():
    payload = _alternative_payload("stub", "<p>real</p>")
    assert "html" not in _read_thread_message(payload)
    assert _read_thread_message(payload, include_html=True)["html"] == "<p>real</p>"


def test_thread_html_only_message_is_still_its_body():
    assert _read_thread_message({**_html_part("<p>only</p>"), "headers": []})["body"] == "<p>only</p>"


def test_mime_nesting_that_survives_json_parsing_does_not_crash():
    payload = _plain_part("deep")
    for _ in range(490):
        payload = {"mimeType": "multipart/mixed", "parts": [payload]}
    assert _read(payload)["body"] == "deep"


# --- labels (0.13.0) ---

def _http_error(status):
    from googleapiclient.errors import HttpError
    return HttpError(MagicMock(status=status), b"err")


_LABELS = [
    {"id": "INBOX", "name": "INBOX", "type": "system"},
    {"id": "STARRED", "name": "STARRED", "type": "system"},
    {"id": "IMPORTANT", "name": "IMPORTANT", "type": "system"},
    {"id": "CATEGORY_PROMOTIONS", "name": "CATEGORY_PROMOTIONS", "type": "system"},
    {"id": "Label_1", "name": "News", "type": "user",
     "labelListVisibility": "labelShow", "messageListVisibility": "show"},
    {"id": "Label_2", "name": "news-old", "type": "user"},
]


def _labels_api(client):
    return client._service.users.return_value.labels.return_value


def set_labels(client, labels=_LABELS):
    _labels_api(client).list.return_value.execute.return_value = {"labels": [dict(l) for l in labels]}


def test_list_labels_shapes_user_and_system():
    client = make_client()
    set_labels(client)
    out = client.list_labels()
    types = {l["name"]: l["type"] for l in out}
    assert types["INBOX"] == "system" and types["News"] == "user"
    assert set(out[0]) <= {"id", "name", "type", "color"}


def test_resolve_label_prefers_id_then_exact_then_casefold():
    client = make_client()
    set_labels(client)
    assert client.resolve_label("Label_1")["id"] == "Label_1"
    assert client.resolve_label("News")["id"] == "Label_1"
    assert client.resolve_label("news")["id"] == "Label_1"


def test_resolve_label_unknown_names_candidates():
    client = make_client()
    set_labels(client)
    with pytest.raises(ValueError, match="News"):
        client.resolve_label("Nws")


def test_create_label_existing_returns_created_false_without_api_create():
    client = make_client()
    set_labels(client)
    out = client.create_label("news")
    assert out["created"] is False and out["id"] == "Label_1"
    _labels_api(client).create.assert_not_called()


def test_create_label_sends_required_visibility():
    client = make_client()
    set_labels(client)
    _labels_api(client).create.return_value.execute.return_value = {"id": "Label_9", "name": "Receipts/2026", "type": "user"}
    out = client.create_label("Receipts/2026")
    body = _labels_api(client).create.call_args.kwargs["body"]
    assert body["labelListVisibility"] == "labelShow"
    assert body["messageListVisibility"] == "show"
    assert body["name"] == "Receipts/2026"
    assert "color" not in body
    assert out["created"] is True and out["id"] == "Label_9"


def test_create_label_409_returns_existing():
    client = make_client()
    api = _labels_api(client)
    api.list.return_value.execute.side_effect = [
        {"labels": [dict(l) for l in _LABELS]},
        {"labels": [dict(l) for l in _LABELS] + [{"id": "Label_7", "name": "Travel", "type": "user"}]},
    ]
    api.create.return_value.execute.side_effect = _http_error(409)
    out = client.create_label("Travel")
    assert out["created"] is False and out["id"] == "Label_7"


def test_update_label_recolour_keeps_name_and_visibility():
    client = make_client()
    set_labels(client)
    _labels_api(client).get.return_value.execute.return_value = dict(_LABELS[4])
    _labels_api(client).patch.return_value.execute.return_value = {"id": "Label_1", "name": "News", "type": "user"}
    client.update_label("News", background_color="#fb4c2f", text_color="#ffffff")
    body = _labels_api(client).patch.call_args.kwargs["body"]
    assert body["name"] == "News"
    assert body["labelListVisibility"] == "labelShow" and body["messageListVisibility"] == "show"
    assert body["color"] == {"backgroundColor": "#fb4c2f", "textColor": "#ffffff"}


def test_update_label_requires_a_change():
    client = make_client()
    set_labels(client)
    with pytest.raises(ValueError):
        client.update_label("News")


def test_delete_label_404_is_not_an_error():
    client = make_client()
    set_labels(client)
    _labels_api(client).delete.return_value.execute.side_effect = _http_error(404)
    out = client.delete_label("News")
    assert out["deleted"] is False and "not found" in out["reason"]


def test_delete_label_refuses_system_label():
    client = make_client()
    set_labels(client)
    with pytest.raises(ValueError):
        client.delete_label("INBOX")
    _labels_api(client).delete.assert_not_called()


def test_manage_email_add_label_by_name():
    client = make_client()
    set_labels(client)
    client.manage_email("msg1", "add_label", label="News")
    body = client._service.users.return_value.messages.return_value.modify.call_args.kwargs["body"]
    assert body == {"addLabelIds": ["Label_1"]}


def test_manage_email_refuses_resolved_system_label():
    client = make_client()
    set_labels(client)
    with pytest.raises(ValueError, match="system label"):
        client.manage_email("msg1", "add_label", label="starred")
    client.manage_email("msg1", "add_label", label="IMPORTANT")
    client.manage_email("msg1", "add_label", label="CATEGORY_PROMOTIONS")
    calls = client._service.users.return_value.messages.return_value.modify.call_args_list
    assert [c.kwargs["body"]["addLabelIds"] for c in calls] == [["IMPORTANT"], ["CATEGORY_PROMOTIONS"]]


# --- bulk (0.13.0) ---

def _msgs(client):
    return client._service.users.return_value.messages.return_value


def set_list_pages(client, total, page=500):
    """messages.list returns `total` IDs in pages of `page`, honouring pageToken."""
    ids = [f"m{i}" for i in range(total)]
    def fake_list(**kw):
        start = int(kw.get("pageToken") or 0)
        chunk = ids[start:start + min(page, kw.get("maxResults", page))]
        resp = {"messages": [{"id": i} for i in chunk]}
        if start + len(chunk) < total:
            resp["nextPageToken"] = str(start + len(chunk))
        req = MagicMock()
        req.execute.return_value = resp
        return req
    _msgs(client).list.side_effect = fake_list


def test_bulk_snapshots_before_changing():
    client = make_client()
    set_labels(client)
    set_list_pages(client, 700)
    log = []
    orig = _msgs(client).list.side_effect
    _msgs(client).list.side_effect = lambda **kw: (log.append("list"), orig(**kw))[1]
    _msgs(client).batchModify.side_effect = lambda **kw: (log.append("modify"), MagicMock())[1]
    client.bulk_manage("from:x", "archive", max_messages=5000)
    assert log.index("modify") > max(i for i, e in enumerate(log) if e == "list")


def test_bulk_archive_selector_keeps_query():
    client = make_client()
    set_list_pages(client, 0)
    client.bulk_manage("from:x", "archive")
    assert _msgs(client).list.call_args.kwargs["q"] == "(from:x) in:inbox"


def test_bulk_add_label_selector_excludes_already_labelled():
    client = make_client()
    set_labels(client)
    set_list_pages(client, 0)
    client.bulk_manage("from:x", "add_label", "News")
    assert _msgs(client).list.call_args.kwargs["q"] == "(from:x) -label:news"


def test_bulk_remove_label_selector_keeps_only_labelled():
    client = make_client()
    set_labels(client)
    set_list_pages(client, 0)
    client.bulk_manage("from:x", "remove_label", "News")
    assert _msgs(client).list.call_args.kwargs["q"] == "(from:x) label:news"


def test_bulk_cap_reports_more_remaining():
    client = make_client()
    set_list_pages(client, 600)
    out = client.bulk_manage("from:x", "archive", max_messages=500)
    assert out == {"matched": 500, "processed": 500, "more_remaining": True}
    body = _msgs(client).batchModify.call_args.kwargs["body"]
    assert len(body["ids"]) == 500 and body["removeLabelIds"] == ["INBOX"]


def test_bulk_exact_cap_without_more_is_not_more_remaining():
    client = make_client()
    set_list_pages(client, 500)
    out = client.bulk_manage("from:x", "archive", max_messages=500)
    assert out["more_remaining"] is False


def test_bulk_chunks_at_1000():
    client = make_client()
    set_list_pages(client, 1500)
    out = client.bulk_manage("from:x", "mark_read", max_messages=5000)
    sizes = [len(c.kwargs["body"]["ids"]) for c in _msgs(client).batchModify.call_args_list]
    assert sizes == [1000, 500] and out["processed"] == 1500


def test_bulk_max_messages_ceiling_is_5000():
    client = make_client()
    set_list_pages(client, 6000)
    out = client.bulk_manage("from:x", "mark_read", max_messages=99999)
    assert out["matched"] == 5000 and out["more_remaining"] is True


def test_bulk_partial_failure_returns_counts():
    client = make_client()
    set_list_pages(client, 1500)
    ok = MagicMock()
    bad = MagicMock()
    bad.execute.side_effect = _http_error(429)
    _msgs(client).batchModify.side_effect = [ok, bad]
    out = client.bulk_manage("from:x", "archive", max_messages=5000)
    assert out["processed"] == 1000 and out["matched"] == 1500 and "error" in out


def test_bulk_blank_query_refused():
    client = make_client()
    with pytest.raises(ValueError):
        client.bulk_manage("  ", "archive")
    _msgs(client).list.assert_not_called()


def test_bulk_refuses_system_label():
    client = make_client()
    set_labels(client)
    with pytest.raises(ValueError):
        client.bulk_manage("from:x", "add_label", "TRASH")
    with pytest.raises(ValueError):
        client.bulk_manage("from:x", "add_label", "starred")
    _msgs(client).list.assert_not_called()


def test_bulk_unknown_action_refused():
    client = make_client()
    with pytest.raises(ValueError, match="Unknown action"):
        client.bulk_manage("from:x", "delete_forever")


def test_bulk_trash_stops_at_first_failure():
    client = make_client()
    set_list_pages(client, 3)
    good, bad = MagicMock(), MagicMock()
    bad.execute.side_effect = _http_error(429)
    _msgs(client).trash.side_effect = [good, bad, good]
    out = client.bulk_manage("from:x", "trash")
    assert out["processed"] == 1 and "error" in out
    assert _msgs(client).trash.call_count == 2
    assert _msgs(client).list.call_args.kwargs["q"] == "(from:x)"
    _msgs(client).batchModify.assert_not_called()


# --- filters (0.13.0) ---

def _filters(client):
    return client._service.users.return_value.settings.return_value.filters.return_value


def set_filters(client, filters):
    _filters(client).list.return_value.execute.return_value = {"filter": filters}


def test_create_filter_refuses_trash_label():
    client = make_client()
    set_labels(client)
    set_filters(client, [])
    with pytest.raises(ValueError):
        client.create_filter("from:x", label="TRASH")
    with pytest.raises(ValueError):
        client.create_filter("from:x", label="starred")
    _filters(client).create.assert_not_called()


def test_create_filter_requires_an_action():
    client = make_client()
    with pytest.raises(ValueError):
        client.create_filter("from:x")
    _filters(client).create.assert_not_called()


def test_create_filter_requires_a_query():
    client = make_client()
    with pytest.raises(ValueError):
        client.create_filter("  ", skip_inbox=True)


def test_create_filter_creates_missing_label_then_filter():
    client = make_client()
    set_labels(client)
    set_filters(client, [])
    log = []
    _labels_api(client).create.side_effect = lambda **kw: (log.append("label"), MagicMock(
        execute=MagicMock(return_value={"id": "Label_9", "name": "Receipts", "type": "user"})))[1]
    _filters(client).create.side_effect = lambda **kw: (log.append("filter"), MagicMock(
        execute=MagicMock(return_value={"id": "F1"})))[1]
    out = client.create_filter("from:shop", label="Receipts")
    assert log == ["label", "filter"]
    body = _filters(client).create.call_args.kwargs["body"]
    assert body == {"criteria": {"query": "from:shop"}, "action": {"addLabelIds": ["Label_9"]}}
    assert out == {"filter_id": "F1", "created": True, "label": "Receipts",
                   "created_label": True, "applied_to_existing": None}


def test_create_filter_duplicate_ignores_gmail_defaults():
    client = make_client()
    set_labels(client)
    set_filters(client, [{"id": "F0", "criteria": {"query": "from:x", "sizeComparison": "unspecified"},
                          "action": {"addLabelIds": ["Label_1"], "removeLabelIds": ["INBOX"]}}])
    out = client.create_filter("from:x", label="News", skip_inbox=True)
    assert out["created"] is False and out["filter_id"] == "F0"
    _filters(client).create.assert_not_called()


def test_create_filter_duplicate_needs_full_match():
    client = make_client()
    set_labels(client)
    set_filters(client, [{"id": "F0", "criteria": {"query": "from:x", "hasAttachment": True},
                          "action": {"addLabelIds": ["Label_1"]}}])
    _filters(client).create.return_value.execute.return_value = {"id": "F1"}
    out = client.create_filter("from:x", label="News")
    assert out["created"] is True and out["filter_id"] == "F1"


def test_create_filter_apply_runs_once_with_all_changes(monkeypatch):
    client = make_client()
    set_labels(client)
    set_filters(client, [])
    _filters(client).create.return_value.execute.return_value = {"id": "F1"}
    calls = []
    monkeypatch.setattr(client, "apply_to_matching",
                        lambda *a, **kw: (calls.append((a, kw)), {"matched": 3, "processed": 3,
                                                                   "more_remaining": False})[1])
    out = client.create_filter("from:x", label="News", star=True, mark_read=True,
                               skip_inbox=True, apply_to_existing=True)
    assert len(calls) == 1
    (query, add_ids, remove_ids, needs, cap), _ = calls[0]
    assert query == "from:x" and cap == 5000
    assert sorted(add_ids) == ["Label_1", "STARRED"] and sorted(remove_ids) == ["INBOX", "UNREAD"]
    assert sorted(needs) == sorted(["-label:news", "-is:starred", "is:unread", "in:inbox"])
    assert out["applied_to_existing"]["processed"] == 3


def test_create_filter_apply_error_is_reported_not_raised(monkeypatch):
    client = make_client()
    set_labels(client)
    set_filters(client, [])
    _filters(client).create.return_value.execute.return_value = {"id": "F1"}
    monkeypatch.setattr(client, "apply_to_matching", lambda *a, **kw: {
        "matched": 5, "processed": 0, "more_remaining": False, "error": "rate limit"})
    out = client.create_filter("from:x", skip_inbox=True, apply_to_existing=True)
    assert out["created"] is True and out["applied_to_existing"]["error"] == "rate limit"


def test_create_filter_duplicate_still_applies_to_existing(monkeypatch):
    client = make_client()
    set_labels(client)
    set_filters(client, [{"id": "F0", "criteria": {"query": "from:x"}, "action": {"removeLabelIds": ["INBOX"]}}])
    called = []
    monkeypatch.setattr(client, "apply_to_matching", lambda *a, **kw: (called.append(1), {
        "matched": 0, "processed": 0, "more_remaining": False})[1])
    out = client.create_filter("from:x", skip_inbox=True, apply_to_existing=True)
    assert out["created"] is False and called == [1]


_STORED = {"id": "F5", "criteria": {"from": "news@x.com"},
           "action": {"addLabelIds": ["Label_1"], "removeLabelIds": ["INBOX"]}}


def test_list_filters_resolves_label_names_and_keeps_all_criteria():
    client = make_client()
    set_labels(client)
    set_filters(client, [{"id": "F6", "criteria": {"from": "a@b", "size": 10, "sizeComparison": "larger",
                                                    "hasAttachment": True},
                          "action": {"addLabelIds": ["Label_1", "Label_404"]}}])
    [f] = client.list_filters()
    assert f["criteria"] == {"from": "a@b", "size": 10, "sizeComparison": "larger", "hasAttachment": True}
    assert f["action"]["addLabelIds"] == ["News", "Label_404"]
    assert f["description"] == "from=a@b; hasAttachment=true; size=10; sizeComparison=larger → label News, label Label_404"


def test_delete_filter_refuses_mismatched_description():
    client = make_client()
    set_labels(client)
    _filters(client).get.return_value.execute.return_value = dict(_STORED)
    with pytest.raises(ValueError, match="from=news@x.com"):
        client.delete_filter("F5", "from=payroll@x.com → label News, skip inbox")
    _filters(client).delete.assert_not_called()


def test_delete_filter_matching_description_deletes():
    client = make_client()
    set_labels(client)
    _filters(client).get.return_value.execute.return_value = dict(_STORED)
    desc = "from=news@x.com → label News, skip inbox"
    out = client.delete_filter("F5", desc)
    assert out == {"deleted": True, "description": desc}
    _filters(client).delete.assert_called_once_with(userId="me", id="F5")


def test_delete_filter_404_returns_not_found():
    client = make_client()
    _filters(client).get.return_value.execute.side_effect = _http_error(404)
    out = client.delete_filter("F404", "anything")
    assert out == {"deleted": False, "reason": "not found"}
    _filters(client).delete.assert_not_called()


# --- code review r1 (Astra) ---

def test_create_filter_reports_filter_when_existing_mail_search_fails():
    """The real apply path: the filter exists even when the search after it fails."""
    client = make_client()
    set_labels(client)
    set_filters(client, [])
    _filters(client).create.return_value.execute.return_value = {"id": "F1"}
    _msgs(client).list.return_value.execute.side_effect = _http_error(429)
    out = client.create_filter("from:x", skip_inbox=True, apply_to_existing=True)
    assert out["filter_id"] == "F1" and out["created"] is True
    assert out["applied_to_existing"]["processed"] == 0 and "error" in out["applied_to_existing"]
    _msgs(client).batchModify.assert_not_called()


def test_create_filter_refuses_label_resolving_to_trash():
    client = make_client()
    set_labels(client, _LABELS + [{"id": "TRASH", "name": "TRASH", "type": "system"}])
    set_filters(client, [])
    with pytest.raises(ValueError):
        client.create_filter("from:x", label=" TRASH ")
    _filters(client).create.assert_not_called()
    _labels_api(client).create.assert_not_called()


def test_delete_label_already_gone_returns_not_deleted():
    client = make_client()
    set_labels(client)
    out = client.delete_label("Vanished")
    assert out["deleted"] is False and "not found" in out["reason"]
    _labels_api(client).delete.assert_not_called()


# --- cc / bcc (0.14.0) ---

def _sent_raw(client):
    import base64
    raw = client._service.users.return_value.messages.return_value.send.call_args.kwargs["body"]["raw"]
    return base64.urlsafe_b64decode(raw + "=" * ((4 - len(raw) % 4) % 4))


@pytest.mark.parametrize("attachments", [[], [("file.txt", b"content")]])
def test_build_message_sets_cc_and_bcc(attachments):
    client = make_client()
    msg = client._build_message(body="b", attachments=attachments, to="to@b.com", subject="s",
                                cc="acct@b.com, boss@b.com", bcc="archive@b.com")
    assert msg["To"] == "to@b.com"
    assert msg["Cc"] == "acct@b.com, boss@b.com"
    assert msg["Bcc"] == "archive@b.com"


@pytest.mark.parametrize("attachments", [[], [("file.txt", b"content")]])
def test_build_message_omits_empty_cc_bcc(attachments):
    client = make_client()
    msg = client._build_message(body="b", attachments=attachments, to="to@b.com", subject="s")
    assert "Cc" not in msg
    assert "Bcc" not in msg


def test_send_email_puts_cc_and_bcc_in_the_raw_message():
    from email import message_from_bytes
    client = make_client()
    client._service.users.return_value.messages.return_value.send.return_value.execute.return_value = {"id": "s"}
    client.send_email("to@b.com", "Hello", "Body", cc="acct@b.com", bcc="archive@b.com")
    sent = message_from_bytes(_sent_raw(client))
    assert sent["To"] == "to@b.com"
    assert sent["Cc"] == "acct@b.com"
    # Gmail delivers to the Bcc header's recipients (users.messages.send).
    assert sent["Bcc"] == "archive@b.com"


def test_reply_sets_cc_header():
    from email import message_from_bytes
    client = make_client()
    client._service.users.return_value.threads.return_value.get.return_value.execute.return_value = (
        _make_thread_response(from_addr="alice@example.com", msg_id="<orig@mail>")
    )
    client._service.users.return_value.messages.return_value.send.return_value.execute.return_value = {"id": "r"}
    client.reply_to_thread("thr1", "body", cc="acct@b.com", bcc="archive@b.com")
    sent = message_from_bytes(_sent_raw(client))
    assert sent["To"] == "alice@example.com"
    assert sent["Cc"] == "acct@b.com"
    assert sent["Bcc"] == "archive@b.com"
    assert sent["In-Reply-To"] == "<orig@mail>"
    assert sent["References"] == "<orig@mail>"


_BREAKS = ["a@b.com\r\nBcc: evil@x.com", "a@b.com\nBcc: evil@x.com", "a@b.com\r", "a@b.com\n x"]


@pytest.mark.parametrize("field", ["to", "cc", "bcc"])
@pytest.mark.parametrize("value", _BREAKS)
def test_send_email_refuses_a_line_break_in_an_address_field(field, value):
    client = make_client()
    args = {"to": "to@b.com", "cc": "", "bcc": ""} | {field: value}
    with pytest.raises(ValueError, match=field):
        client.send_email(args["to"], "Hello", "Body", cc=args["cc"], bcc=args["bcc"])
    client._service.users.return_value.messages.return_value.send.assert_not_called()


@pytest.mark.parametrize("field", ["cc", "bcc"])
@pytest.mark.parametrize("value", _BREAKS)
def test_reply_refuses_a_line_break_in_cc_or_bcc_before_reading_the_thread(field, value):
    client = make_client()
    with pytest.raises(ValueError, match=field):
        client.reply_to_thread("thr1", "body", **{field: value})
    client._service.users.return_value.threads.return_value.get.assert_not_called()
    client._service.users.return_value.messages.return_value.send.assert_not_called()
