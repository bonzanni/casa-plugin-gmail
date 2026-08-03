import inspect
import json
import re
import time
from pathlib import Path

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


def test_startup_recover_double_activation_builds_attachment_manager_once(monkeypatch, tmp_path):
    """One startup_recover call can activate() twice: load_active() activates
    an on-disk active credential, then reconcile_stage()'s promote() activates
    a pending stage in the same pass. AttachmentManager/SentLog are
    credential-independent and must be built exactly once; GmailClient is
    credential-derived and must be rebuilt on every activation.

    Driven through a REAL double activation — a real GmailAuth, a real
    TokenStore on disk, and the real auth_flow.startup_recover — not a mocked
    activate() call. Only the two network-touching methods (_refresh,
    refresh_and_verify) are stubbed, since they'd otherwise hit Google.
    """
    import server
    from auth import GmailAuth
    from auth_flow import startup_recover
    from token_store import Credential

    monkeypatch.setattr(server, "_client", None)
    monkeypatch.setattr(server, "_att", None)
    monkeypatch.setattr(server, "_log", None)
    monkeypatch.setattr(server, "_authenticated", False)

    mock_client_cls = MagicMock()
    mock_att_cls = MagicMock()
    mock_log_cls = MagicMock()
    monkeypatch.setattr(server, "GmailClient", mock_client_cls)
    monkeypatch.setattr(server, "AttachmentManager", mock_att_cls)
    monkeypatch.setattr(server, "SentLog", mock_log_cls)

    monkeypatch.setenv("GMAIL_CLIENT_ID", "client-id")
    monkeypatch.setenv("GMAIL_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("GMAIL_USER_EMAIL", "user@example.com")

    auth = GmailAuth(str(tmp_path))
    auth.on_activate = server._rebuild_runtime          # the real wiring

    active_flow = "a" * 64
    staged_flow = "b" * 64
    auth.store.write_active(Credential(
        refresh_token="rt-active", flow=active_flow, generation=1.0,
        account="user@example.com"))
    auth.store.stage("rt-staged", staged_flow, time.time())

    monkeypatch.setattr(auth, "_refresh", lambda rt: MagicMock())
    monkeypatch.setattr(auth, "refresh_and_verify", lambda rt: "user@example.com")

    outcome = startup_recover(auth, MagicMock())

    assert outcome == "promoted"                        # both activations ran
    assert mock_client_cls.call_count == 2               # rebuilt every time
    assert mock_att_cls.call_count == 1                  # built exactly once
    assert mock_log_cls.call_count == 1                  # built exactly once
    assert server._att is not None and server._log is not None
    assert server._authenticated is True


def test_startup_logs_the_recovery_outcome(monkeypatch, capsys, tmp_path):
    """Silent startup recovery is how the lost-outcome bug went unnoticed."""
    import server
    monkeypatch.setattr(server, "PLUGIN_DATA", str(tmp_path))
    monkeypatch.setattr(server, "_flow_startup", lambda auth, cb: "settled")

    server._startup()

    assert "settled" in capsys.readouterr().err


def test_the_runtime_rebuild_is_wired_to_activation(monkeypatch):
    """auth.py must not learn what a GmailClient is; server.py must not rebuild
    after the fact. The hook is the seam."""
    import server
    assert server._auth.on_activate is server._rebuild_runtime


def test_gmail_auth_complete_is_gone():
    import server
    assert not hasattr(server, "gmail_auth_complete")


def _manifest():
    return json.loads(
        (Path(__file__).parent.parent / ".claude-plugin" / "plugin.json").read_text())


def test_manifest_declares_the_callback_and_no_stale_protected_tool():
    manifest = _manifest()
    assert manifest["casa"]["callbacks"] == [{"name": "oauth"}]
    names = [t["name"] for t in manifest["casa"]["protectedTools"]]
    assert "gmail_auth_complete" not in names
    assert "gmail_auth_collect" not in names        # must stay unprotected
    assert manifest["version"] == "0.5.1"


# ── v0.5.1: casa.setupTool — the hand-back the consent gate was missing ────

# Casa's own grammar, copied from plugin_store.py:925 (`_SETUP_TOOL_RE`). A
# manifest that fails it raises StoreError(reason_code="setup_tool_invalid")
# from manifest_setup_tool(), which blocks install/update outright.
_CASA_SETUP_TOOL_RE = re.compile(r"^setup_[a-z0-9_]{1,64}$")


def test_manifest_declares_the_setup_tool_casa_auto_runs():
    """v0.5.0 shipped casa.callbacks but no casa.setupTool, so casa opened a
    setup episode, found nothing to dispatch ("No setup tool shipped — nothing
    to hand back") and the operator had to ask for authorization by hand."""
    manifest = _manifest()
    name = manifest["casa"]["setupTool"]
    assert name == "setup_gmail"
    assert _CASA_SETUP_TOOL_RE.fullmatch(name), (
        f"{name!r} fails casa's ^setup_[a-z0-9_]{{1,64}}$ — install would be "
        "rejected with setup_tool_invalid")


def test_the_setup_tool_is_not_protected():
    """Casa dispatches it unprompted, so a tap-approval prompt would deadlock
    the episode — the same reason gmail_auth_collect stays unprotected. The two
    real protected tools must be untouched."""
    names = [t["name"] for t in _manifest()["casa"]["protectedTools"]]
    assert "setup_gmail" not in names
    assert names == ["send_email", "reply_to_thread"]


def test_the_setup_tool_exists_and_is_argument_free():
    """Casa's composed instruction says "Call it with no arguments"
    (plugin_setup_episodes._compose), so a required parameter would strand it."""
    import server
    assert inspect.signature(server.setup_gmail).parameters == {}


def test_setup_gmail_mints_a_flow_when_not_connected(monkeypatch):
    import server
    monkeypatch.setattr(server, "_authenticated", False)
    calls = []

    def fake_start(auth, cb):
        calls.append((auth, cb))
        return {"auth_url": "https://accounts.google.com/o?state=s",
                "redirect_uri": "https://casa.example.com/callback/plg-gmail--oauth",
                "instructions": "open it"}
    monkeypatch.setattr(server, "_flow_start", fake_start)

    result = json.loads(server.setup_gmail())

    assert len(calls) == 1                      # the flow really was minted
    assert result["auth_url"].startswith("https://accounts.google.com/")
    assert result["redirect_uri"].endswith("/callback/plg-gmail--oauth")


def test_setup_gmail_is_idempotent_when_already_connected(monkeypatch):
    """Casa's authoring doctrine requires idempotence and casa may re-dispatch.
    A second run must not mint a second flow or re-authorize."""
    import server
    from token_store import Credential

    mock_auth = MagicMock()
    mock_auth.subject_email = "user@example.com"
    mock_auth.store.load_active.return_value = Credential(
        refresh_token="rt", flow="a" * 64, generation=1.0,
        account="user@example.com")
    monkeypatch.setattr(server, "_auth", mock_auth)
    monkeypatch.setattr(server, "_authenticated", True)

    def must_not_mint(auth, cb):
        raise AssertionError("setup_gmail minted a flow while already connected")
    monkeypatch.setattr(server, "_flow_start", must_not_mint)

    result = json.loads(server.setup_gmail())

    assert result["status"] == "already_connected"
    assert result["account"] == "user@example.com"


def test_setup_gmail_mints_when_the_active_credential_is_another_account(monkeypatch):
    """"Already connected" is an account match, not merely a live credential:
    a credential for the wrong inbox is exactly the case needing re-auth."""
    import server
    from token_store import Credential

    mock_auth = MagicMock()
    mock_auth.subject_email = "user@example.com"
    mock_auth.store.load_active.return_value = Credential(
        refresh_token="rt", flow="a" * 64, generation=1.0,
        account="someone.else@example.com")
    monkeypatch.setattr(server, "_auth", mock_auth)
    monkeypatch.setattr(server, "_authenticated", True)
    monkeypatch.setattr(server, "_flow_start", lambda auth, cb: {
        "auth_url": "https://accounts.google.com/o?state=s",
        "redirect_uri": "https://casa.example.com/callback/plg-gmail--oauth",
        "instructions": "open it"})

    result = json.loads(server.setup_gmail())
    assert result["auth_url"].startswith("https://accounts.google.com/")
    assert "status" not in result


def test_setup_gmail_surfaces_callback_unavailable_instead_of_raising(monkeypatch):
    """Casa dispatched this unprompted, so a raise reaches the operator as a
    bare tool error explaining nothing. Unlike gmail_auth_start — which answers
    a direct request and may raise — this returns the reason as its result."""
    import server
    from casa_callback import CallbackUnavailable

    monkeypatch.setattr(server, "_authenticated", False)

    def boom(auth, cb):
        raise CallbackUnavailable("route not open: callback_no_target ...")
    monkeypatch.setattr(server, "_flow_start", boom)

    result = json.loads(server.setup_gmail())          # must not raise
    assert result["status"] == "unavailable"
    assert "callback_no_target" in result["instructions"]
