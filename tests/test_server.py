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
    # Grammar FIRST: a name that fails it is a hard StoreError blocking
    # install/update, which is worse than merely naming the wrong tool.
    assert _CASA_SETUP_TOOL_RE.fullmatch(name), (
        f"{name!r} fails casa's ^setup_[a-z0-9_]{{1,64}}$ — install would be "
        "rejected with setup_tool_invalid")
    assert name == "setup_gmail"


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


def _spool(monkeypatch, *attempts, pending=()):
    """Install a stub casa spool exposing `attempts` and `pending_mint_times`.

    setup_gmail reads the spool before minting (it has to: casa's spool is the
    only record of an outstanding link), so every setup_gmail test needs one —
    the real `_cb` points at /data/callbacks and would raise CallbackUnavailable.

    Both directories are stubbed because casa fills them at different times:
    `mint()` publishes only `pending/<hash>.json`, and the matching record in
    `attempts/` appears at the next reconciliation pass, up to five minutes
    later. `pending` here is that directory's mint clocks (mtimes).
    """
    import server
    cb = MagicMock()
    cb.attempts.return_value = list(attempts)
    cb.pending_mint_times.return_value = list(pending)
    monkeypatch.setattr(server, "_cb", cb)
    return cb


def _awaiting(minted_ts, state_hash="b" * 64):
    """An `awaiting_redirect` attempt record, shaped as casa's
    callback_attempts.new_attempt builds one."""
    return {"v": 1, "state_hash": state_hash, "minted_ts": minted_ts,
            "status": "awaiting_redirect", "outcome": None, "claimed": False,
            "meta": {"kind": "gmail-oauth", "v": 1}}


def _minting_start(calls):
    def fake_start(auth, cb):
        calls.append((auth, cb))
        return {"auth_url": "https://accounts.google.com/o?state=s",
                "redirect_uri": "https://casa.example.com/callback/plg-gmail--oauth",
                "instructions": "open it"}
    return fake_start


def test_setup_gmail_mints_a_flow_when_not_connected(monkeypatch):
    import server
    monkeypatch.setattr(server, "_authenticated", False)
    _spool(monkeypatch)
    calls = []
    monkeypatch.setattr(server, "_flow_start", _minting_start(calls))

    result = json.loads(server.setup_gmail())

    assert len(calls) == 1                      # the flow really was minted
    assert result["auth_url"].startswith("https://accounts.google.com/")
    assert result["redirect_uri"].endswith("/callback/plg-gmail--oauth")


# ── Fix 2: a re-dispatch must not mint a second concurrent authorization ───

def test_setup_gmail_does_not_mint_twice_while_disconnected(monkeypatch):
    """Casa's dispatch is at-least-once. The already-connected branch does not
    cover this: while the FIRST link is outstanding `_authenticated` is still
    false, so the old code minted a second independent state and a second live
    link — both usable, and both completable by the operator.

    The double models what casa's `mint()` ACTUALLY leaves behind: a file in
    `pending/` and NOTHING in `attempts/`. The attempt record is materialized
    by casa's `callback_spool_recovery` job, which runs every five minutes
    (casa_core.py) — observed at ~3 minutes on the live host. An earlier
    version of this test fabricated the attempt record at mint time, which is
    why a check that read only `attempts/` looked like it worked.
    """
    import server
    monkeypatch.setattr(server, "_authenticated", False)
    cb = _spool(monkeypatch)
    calls = []
    started = _minting_start(calls)

    def start_and_publish(auth, cb_arg):
        out = started(auth, cb_arg)
        cb.pending_mint_times.return_value = [time.time()]
        return out
    monkeypatch.setattr(server, "_flow_start", start_and_publish)

    first = json.loads(server.setup_gmail())
    second = json.loads(server.setup_gmail())          # casa re-dispatches

    assert cb.attempts.return_value == [], "the double invented an attempt record"
    assert len(calls) == 1, "a re-dispatch minted a second authorization"
    assert first["auth_url"].startswith("https://accounts.google.com/")
    assert second["status"] == "already_pending"
    assert "auth_url" not in second, "a second link was handed out"


def test_setup_gmail_defers_to_a_pending_state_with_no_attempt_record_yet(monkeypatch):
    """The blind window itself, stated directly: casa's `mint()` publishes only
    `pending/<hash>.json`, so a freshly minted flow is invisible in `attempts/`
    for up to a reconciliation interval. It is still a live link."""
    import server
    monkeypatch.setattr(server, "_authenticated", False)
    _spool(monkeypatch, pending=[time.time() - 30])   # no attempt record at all

    def must_not_mint(auth, cb):
        raise AssertionError("minted a second flow while a pending state was live")
    monkeypatch.setattr(server, "_flow_start", must_not_mint)

    result = json.loads(server.setup_gmail())
    assert result["status"] == "already_pending"


def test_setup_gmail_mints_when_the_pending_state_is_past_casas_ttl(monkeypatch):
    """A pending file older than `PENDING_TTL_S` is a link casa's claim gate
    would refuse (`now - st.st_mtime > PENDING_TTL_S`), and casa's sweep only
    removes it every ten minutes. Never point the operator at a dead link."""
    import server
    monkeypatch.setattr(server, "_authenticated", False)
    _spool(monkeypatch, pending=[time.time() - 1801])
    calls = []
    monkeypatch.setattr(server, "_flow_start", _minting_start(calls))

    result = json.loads(server.setup_gmail())

    assert len(calls) == 1, "deferred to a pending state casa would refuse to claim"
    assert result["auth_url"].startswith("https://accounts.google.com/")


def test_setup_gmail_mints_when_a_pending_mint_clock_is_beyond_casas_skew(monkeypatch):
    """Casa fails closed on a materially future mtime (`st.st_mtime > now +
    SKEW_S`), so such an entry is not a link anyone can use."""
    import server
    monkeypatch.setattr(server, "_authenticated", False)
    _spool(monkeypatch, pending=[time.time() + 301])
    calls = []
    monkeypatch.setattr(server, "_flow_start", _minting_start(calls))

    json.loads(server.setup_gmail())
    assert len(calls) == 1


def test_setup_gmail_reports_an_outstanding_attempt_instead_of_minting(monkeypatch):
    """The plugin cannot reconstruct the earlier auth_url — casa's spool stores
    only the state hash — so the honest answer is that one is already out."""
    import server
    monkeypatch.setattr(server, "_authenticated", False)
    _spool(monkeypatch, _awaiting(time.time() - 60))

    def must_not_mint(auth, cb):
        raise AssertionError("minted a second flow while one was outstanding")
    monkeypatch.setattr(server, "_flow_start", must_not_mint)

    result = json.loads(server.setup_gmail())

    assert result["status"] == "already_pending"
    assert "already sent" in result["instructions"]


def test_setup_gmail_mints_when_the_outstanding_attempt_is_past_casas_ttl(monkeypatch):
    """casa's PENDING_TTL_S is 1800s and its claim gate refuses an older state
    outright, but the sweep that retires it runs only every 10 minutes — so a
    DEAD flow still reads `awaiting_redirect` for up to ~40 minutes. Trusting
    the status alone would send the operator to a link that cannot work."""
    import server
    monkeypatch.setattr(server, "_authenticated", False)
    _spool(monkeypatch, _awaiting(time.time() - 1801))
    calls = []
    monkeypatch.setattr(server, "_flow_start", _minting_start(calls))

    result = json.loads(server.setup_gmail())

    assert len(calls) == 1, "deferred to an attempt casa would refuse to claim"
    assert result["auth_url"].startswith("https://accounts.google.com/")


def test_setup_gmail_mints_when_the_outstanding_attempt_has_no_mint_clock(monkeypatch):
    """`minted_ts` is legitimately None on a legacy or consumer-held record.
    Liveness cannot be established, so it must not read as pending."""
    import server
    monkeypatch.setattr(server, "_authenticated", False)
    _spool(monkeypatch, _awaiting(None))
    calls = []
    monkeypatch.setattr(server, "_flow_start", _minting_start(calls))

    json.loads(server.setup_gmail())
    assert len(calls) == 1


def test_setup_gmail_mints_when_the_only_attempt_already_has_its_result(monkeypatch):
    """`result_ready` is waiting on gmail_auth_collect, not on the browser —
    it is not an outstanding link, so it must not suppress a mint."""
    import server
    monkeypatch.setattr(server, "_authenticated", False)
    ready = _awaiting(time.time() - 60)
    ready["status"] = "result_ready"
    _spool(monkeypatch, ready)
    calls = []
    monkeypatch.setattr(server, "_flow_start", _minting_start(calls))

    json.loads(server.setup_gmail())
    assert len(calls) == 1


def test_a_boolean_mint_clock_is_never_live(monkeypatch):
    """`True` is an `int` in Python, so without the bool guard it would read as
    the clock `1` — and at these coordinates that is a LIVE mint, as the first
    assertion shows. The guard, not the arithmetic, is what rejects it."""
    import server
    assert server._mint_is_live(1.0, 100.0) is True        # same clock, as a float
    assert server._mint_is_live(True, 100.0) is False      # a bool is not a clock


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
    _spool(monkeypatch)

    def must_not_mint(auth, cb):
        raise AssertionError("setup_gmail minted a flow while already connected")
    monkeypatch.setattr(server, "_flow_start", must_not_mint)

    result = json.loads(server.setup_gmail())

    assert result["status"] == "already_connected"
    assert result["account"] == "user@example.com"
    # The claim is checked, not assumed: a live connection is one that refreshes.
    mock_auth.probe_refresh.assert_called_once_with("rt")
    # A runtime already in service is left alone. Re-activating would swap the
    # live GmailClient for one that has to fetch an access token again, and
    # would run the on_activate hook a second time, for nothing.
    mock_auth.activate.assert_not_called()


# ── Fix 3: "already connected" must mean the credential still works ────────

def _connected_auth(monkeypatch, probe_error=None):
    import server
    from token_store import Credential
    mock_auth = MagicMock()
    mock_auth.subject_email = "user@example.com"
    mock_auth.store.load_active.return_value = Credential(
        refresh_token="rt", flow="a" * 64, generation=1.0,
        account="user@example.com")
    if probe_error is not None:
        mock_auth.probe_refresh.side_effect = probe_error
    monkeypatch.setattr(server, "_auth", mock_auth)
    monkeypatch.setattr(server, "_authenticated", True)
    return mock_auth


def test_setup_gmail_mints_a_recovery_link_when_the_stored_token_is_revoked(monkeypatch):
    """`_authenticated` is set once at activation and never cleared, and the
    on-disk account still matches after a revocation — so the old code reported
    `already_connected` and minted nothing at the exact moment Gmail was
    failing and the operator (who did not ask for this call) needed a link."""
    import server
    from auth import RefreshTerminal

    mock_auth = _connected_auth(
        monkeypatch, RefreshTerminal("invalid_grant: Token has been expired or revoked."))
    _spool(monkeypatch)
    calls = []
    monkeypatch.setattr(server, "_flow_start", _minting_start(calls))

    result = json.loads(server.setup_gmail())

    assert result.get("status") != "already_connected"
    assert len(calls) == 1, "a revoked credential produced no recovery link"
    assert result["auth_url"].startswith("https://accounts.google.com/")
    assert result["status"] == "reauthorization_needed"
    assert "invalid_grant" in result["instructions"]
    # Never destroy a credential here — reaping is load_active's job.
    mock_auth.store.remove_active.assert_not_called()


def test_setup_gmail_does_not_mint_on_a_transient_refresh_failure(monkeypatch):
    """The RefreshTerminal/RefreshRetryable split is exactly this distinction: a
    Google 5xx or a network blip must not trigger a re-authorization."""
    import server
    from auth import RefreshRetryable

    mock_auth = _connected_auth(
        monkeypatch, RefreshRetryable("temporarily_unavailable"))
    _spool(monkeypatch)

    def must_not_mint(auth, cb):
        raise AssertionError("a transient refresh failure minted a new flow")
    monkeypatch.setattr(server, "_flow_start", must_not_mint)

    result = json.loads(server.setup_gmail())

    assert result["status"] == "retry_later"
    assert result["account"] == "user@example.com"
    assert "temporarily_unavailable" in result["instructions"]
    mock_auth.store.remove_active.assert_not_called()


def test_setup_gmail_reports_a_rejected_client_as_configuration_not_revocation(
        monkeypatch):
    """Rotate the OAuth client secret and Google answers `invalid_client`. The
    refresh token is fine — and a fresh flow could not complete either, because
    its code exchange uses the same rejected secret. So: no revocation claim,
    and no link."""
    import server
    from auth import RefreshConfigError

    mock_auth = _connected_auth(
        monkeypatch, RefreshConfigError("invalid_client: Unauthorized"))
    _spool(monkeypatch)

    def must_not_mint(auth, cb):
        raise AssertionError("a configuration error minted an unusable flow")
    monkeypatch.setattr(server, "_flow_start", must_not_mint)

    result = json.loads(server.setup_gmail())

    assert result["status"] == "configuration_error"
    assert "auth_url" not in result
    assert result["status"] != "reauthorization_needed"
    assert "invalid_client" in result["instructions"]
    assert "GMAIL_CLIENT_SECRET" in result["instructions"]
    mock_auth.store.remove_active.assert_not_called()


def test_the_configuration_error_instructions_require_a_new_session(monkeypatch):
    """SKILL.md has the agent relay these `instructions` VERBATIM, so this
    string is not a diagnostic — it IS what the operator hears, and it is the
    only recovery advice she gets.

    `read_env()` copied GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET into process
    memory once at startup and nothing re-reads them, so "correct them and run
    setup_gmail again" is an instruction to loop forever: every later probe
    uses the cached secret. The README was corrected to say so; this string,
    which the README's own skill mandates relaying word for word, still carried
    the promise the README had just dropped. Pinned on the emitted payload
    rather than the source text, because the payload is what ships."""
    import server
    from auth import RefreshConfigError

    _connected_auth(monkeypatch, RefreshConfigError("invalid_client: Unauthorized"))
    _spool(monkeypatch)

    def must_not_mint(auth, cb):
        raise AssertionError("a configuration error minted an unusable flow")
    monkeypatch.setattr(server, "_flow_start", must_not_mint)

    instructions = json.loads(server.setup_gmail())["instructions"]

    assert "new session" in instructions, \
        "the operator is never told what actually picks up the corrected values"
    assert "restart" in instructions
    # ...and it must not stop at "fix it and run me again", which is precisely
    # the advice that cannot work.
    assert "run setup_gmail again" not in instructions


def test_setup_gmail_treats_an_unclassified_probe_failure_as_transient(monkeypatch):
    """Ambiguity must never mint or destroy — the same policy load_active
    applies to an error it cannot classify."""
    import server

    mock_auth = _connected_auth(monkeypatch, ValueError("something odd"))
    _spool(monkeypatch)

    def must_not_mint(auth, cb):
        raise AssertionError("an unclassified failure minted a new flow")
    monkeypatch.setattr(server, "_flow_start", must_not_mint)

    result = json.loads(server.setup_gmail())

    assert result["status"] == "retry_later"
    mock_auth.store.remove_active.assert_not_called()


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
    _spool(monkeypatch)
    monkeypatch.setattr(server, "_flow_start", lambda auth, cb: {
        "auth_url": "https://accounts.google.com/o?state=s",
        "redirect_uri": "https://casa.example.com/callback/plg-gmail--oauth",
        "instructions": "open it"})

    result = json.loads(server.setup_gmail())
    assert result["auth_url"].startswith("https://accounts.google.com/")
    assert "status" not in result


# ── Fix 4: decide from the DURABLE store, not from runtime activation ──────
#
# Every one of the three fixes above was the same defect wearing a new hat: a
# guard that covered one path and missed its sibling, because setup_gmail
# decided from `_authenticated` — a fact about whether THIS PROCESS's startup
# happened to succeed — instead of from what is on disk. The classification
# above sat behind `_connected_credential`, which returned None whenever
# `_authenticated` was false, so on the restart path every check was skipped
# and setup_gmail minted a link that could not complete.
#
# `load_active` reaches that state on purpose: a rejected client, a transient
# refresh failure, an unconfirmable account and a failed runtime rebuild all
# RETAIN the credential and return False (auth.py). The tests below are that
# state — credential on disk, runtime inactive — one per probe verdict.

def _restart_auth(monkeypatch, probe_error=None, activate_error=None):
    """Sol's reproduction: a retained credential, an INACTIVE runtime.

    Nothing here is exotic — it is exactly what a restart leaves behind after
    `load_active` retains the token and returns False.
    """
    import server
    from token_store import Credential

    cred = Credential(refresh_token="rt", flow="a" * 64, generation=1.0,
                      account="user@example.com")
    mock_auth = MagicMock()
    mock_auth.subject_email = "user@example.com"
    mock_auth.store.load_active.return_value = cred
    if probe_error is not None:
        mock_auth.probe_refresh.side_effect = probe_error
    if activate_error is not None:
        mock_auth.activate.side_effect = activate_error
    monkeypatch.setattr(server, "_auth", mock_auth)
    monkeypatch.setattr(server, "_authenticated", False)   # post-restart
    return mock_auth, cred


def test_setup_gmail_reports_configuration_error_after_a_restart(monkeypatch):
    """THE reproduction. A rotated `GMAIL_CLIENT_SECRET` makes `load_active`
    raise RefreshConfigError, retain the token and return False — so on the next
    restart `_authenticated` is false. The classification was gated on it, so
    setup_gmail skipped the probe entirely and minted: one mint, zero probes,
    and a link whose code exchange uses the same rejected secret."""
    import server
    from auth import RefreshConfigError

    mock_auth, _ = _restart_auth(
        monkeypatch, probe_error=RefreshConfigError("invalid_client: Unauthorized"))
    _spool(monkeypatch)
    calls = []
    monkeypatch.setattr(server, "_flow_start", _minting_start(calls))

    result = json.loads(server.setup_gmail())

    assert result["status"] == "configuration_error"
    # Counts, not just the status: the bug WAS mint=1/probe=0 while the status
    # string read plausibly ("authorization_needed" from a real mint).
    assert len(calls) == 0, "minted a link its own client credentials would reject"
    assert mock_auth.probe_refresh.call_count == 1, "never probed the stored token"
    assert "invalid_client" in result["instructions"]
    assert "GMAIL_CLIENT_SECRET" in result["instructions"]
    assert "auth_url" not in result
    mock_auth.store.remove_active.assert_not_called()


def test_setup_gmail_mints_a_recovery_link_after_a_restart_when_revoked(monkeypatch):
    """The same on-disk state with the opposite verdict: deciding from the store
    must still mint when the grant really is dead. `load_active` removes such a
    token, but not if the revocation happened after it ran."""
    import server
    from auth import RefreshTerminal

    mock_auth, _ = _restart_auth(
        monkeypatch,
        probe_error=RefreshTerminal("invalid_grant: Token has been expired or revoked."))
    _spool(monkeypatch)
    calls = []
    monkeypatch.setattr(server, "_flow_start", _minting_start(calls))

    result = json.loads(server.setup_gmail())

    assert result["status"] == "reauthorization_needed"
    assert len(calls) == 1, "a revoked credential produced no recovery link"
    assert mock_auth.probe_refresh.call_count == 1
    assert result["auth_url"].startswith("https://accounts.google.com/")
    assert "invalid_grant" in result["instructions"]
    mock_auth.store.remove_active.assert_not_called()


def test_setup_gmail_does_not_mint_after_a_restart_on_a_transient_failure(monkeypatch):
    """A Google 5xx during startup leaves exactly this state too. Ambiguity must
    not mint: the credential is presumed good and is untouched."""
    import server
    from auth import RefreshRetryable

    mock_auth, _ = _restart_auth(
        monkeypatch, probe_error=RefreshRetryable("temporarily_unavailable"))
    _spool(monkeypatch)
    calls = []
    monkeypatch.setattr(server, "_flow_start", _minting_start(calls))

    result = json.loads(server.setup_gmail())

    assert result["status"] == "retry_later"
    assert len(calls) == 0, "a transient failure minted a re-authorization"
    assert mock_auth.probe_refresh.call_count == 1
    assert result["account"] == "user@example.com"
    assert "temporarily_unavailable" in result["instructions"]
    mock_auth.store.remove_active.assert_not_called()


def test_setup_gmail_puts_a_live_credential_back_into_service_after_a_restart(
        monkeypatch):
    """The design question, answered: REPAIR, then report.

    The credential probes live and matches — the operator has fixed the
    configuration and restarted, or the outage that blocked startup has passed.
    Reporting "already connected" and stopping would be true of the store and
    false of every Gmail tool, which keeps raising "Gmail is not authenticated"
    until someone restarts the process. `activate()` is what ends that, and its
    `on_activate` hook rebuilds the runtime; both are idempotent."""
    import server

    mock_auth, cred = _restart_auth(monkeypatch)          # probe succeeds
    _spool(monkeypatch)
    calls = []
    monkeypatch.setattr(server, "_flow_start", _minting_start(calls))

    result = json.loads(server.setup_gmail())

    assert result["status"] == "already_connected"
    assert result["account"] == "user@example.com"
    assert len(calls) == 0, "minted a link for a credential that still works"
    assert mock_auth.probe_refresh.call_count == 1
    # Repaired, not merely reported: without this the status is a claim the
    # Gmail tools cannot honour.
    mock_auth.activate.assert_called_once_with(cred)


def test_setup_gmail_does_not_claim_connected_when_activation_fails(monkeypatch):
    """`activate()` runs the runtime rebuild and is allowed to raise. Then the
    honest answer is not "connected" — and not `retry_later` either, since
    nothing about Google is at fault. It is the same shape as a closed callback
    route: automatic setup did not complete, nothing was authorized, retry when
    the local cause is fixed."""
    import server

    mock_auth, _ = _restart_auth(monkeypatch,
                                 activate_error=OSError("no space left on device"))
    _spool(monkeypatch)
    calls = []
    monkeypatch.setattr(server, "_flow_start", _minting_start(calls))

    result = json.loads(server.setup_gmail())

    assert result["status"] == "unavailable"
    assert result["status"] != "already_connected"
    assert len(calls) == 0, "a failed runtime rebuild minted a needless link"
    assert mock_auth.probe_refresh.call_count == 1
    assert "no space left on device" in result["instructions"]
    assert "auth_url" not in result
    mock_auth.store.remove_active.assert_not_called()


def test_setup_gmail_surfaces_callback_unavailable_instead_of_raising(monkeypatch):
    """Casa dispatched this unprompted, so a raise reaches the operator as a
    bare tool error explaining nothing. Unlike gmail_auth_start — which answers
    a direct request and may raise — this returns the reason as its result."""
    import server
    from casa_callback import CallbackUnavailable

    monkeypatch.setattr(server, "_authenticated", False)
    _spool(monkeypatch)          # route open enough to read: the mint is what fails

    def boom(auth, cb):
        raise CallbackUnavailable("route not open: callback_no_target ...")
    monkeypatch.setattr(server, "_flow_start", boom)

    result = json.loads(server.setup_gmail())          # must not raise
    assert result["status"] == "unavailable"
    assert "callback_no_target" in result["instructions"]
