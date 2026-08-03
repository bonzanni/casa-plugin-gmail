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


def test_gmail_auth_start_returns_url_and_redirect_uri(monkeypatch):
    import server
    monkeypatch.setattr(server, "_flow_start", lambda auth, cb: {
        "auth_url": "https://accounts.google.com/o?state=s",
        "redirect_uri": "https://casa.example.com/callback/plg-gmail--oauth",
        "instructions": "open it",
    })
    result = json.loads(server.gmail_auth_start())
    assert result["auth_url"].startswith("https://accounts.google.com/")
    assert result["redirect_uri"].endswith("/callback/plg-gmail--oauth")


def test_gmail_auth_start_surfaces_callback_unavailable(monkeypatch):
    import server
    from casa_callback import CallbackUnavailable

    def boom(auth, cb):
        raise CallbackUnavailable("route not open: callback_no_target ...")
    monkeypatch.setattr(server, "_flow_start", boom)
    with pytest.raises(Exception, match="callback_no_target"):
        server.gmail_auth_start()


def test_gmail_auth_collect_reports_and_rebuilds_clients(monkeypatch):
    """The rebuild is wired to activate(), so it must have happened by the time
    the tool returns — and it must be driven by the activation, not bolted on
    afterwards (which would rebuild twice, starting a second cleanup thread)."""
    import server
    from token_store import Credential

    monkeypatch.setattr(server, "GmailClient", MagicMock())
    monkeypatch.setattr(server, "AttachmentManager", MagicMock())
    monkeypatch.setattr(server, "SentLog", MagicMock())
    monkeypatch.setattr(server, "_client", None)
    monkeypatch.setattr(server, "_att", None)
    monkeypatch.setattr(server, "_log", None)
    monkeypatch.setattr(server, "_authenticated", False)

    def fake_collect(auth, cb):
        # What a real promotion does, and the only thing that does it.
        auth.activate(Credential(refresh_token="rt", flow="a" * 64,
                                 generation=1.0, account="a@b.c"))
        return {"status": "ok", "promoted": True,
                "messages": ["Gmail connected as a@b.c."]}
    monkeypatch.setattr(server, "_flow_collect", fake_collect)

    result = json.loads(server.gmail_auth_collect())

    assert result["status"] == "ok"
    assert result["messages"] == ["Gmail connected as a@b.c."]
    assert server._authenticated is True
    assert server._client is not None and server._att is not None
    assert server.GmailClient.call_count == 1        # rebuilt exactly once
    assert server.AttachmentManager.call_count == 1


def test_the_runtime_rebuild_is_wired_to_activation(monkeypatch):
    """auth.py must not learn what a GmailClient is; server.py must not rebuild
    after the fact. The hook is the seam."""
    import server
    assert server._auth.on_activate is server._rebuild_runtime


def test_gmail_auth_complete_is_gone():
    import server
    assert not hasattr(server, "gmail_auth_complete")


def test_manifest_declares_the_callback_and_no_stale_protected_tool():
    from pathlib import Path
    manifest = json.loads(
        (Path(__file__).parent.parent / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["casa"]["callbacks"] == [{"name": "oauth"}]
    names = [t["name"] for t in manifest["casa"]["protectedTools"]]
    assert "gmail_auth_complete" not in names
    assert "gmail_auth_collect" not in names        # must stay unprotected
    assert manifest["version"] == "0.5.0"
