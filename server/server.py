import json
import os
import sys
import time

from mcp.server.fastmcp import FastMCP

from auth import GmailAuth, RefreshConfigError, RefreshRetryable, RefreshTerminal
from gmail_client import GmailClient
from attachments import AttachmentManager
from sent_log import SentLog
from auth_flow import collect_pass as _flow_collect
from auth_flow import start as _flow_start
from auth_flow import startup_recover as _flow_startup
from casa_callback import CallbackUnavailable, CasaCallback

PLUGIN_DATA = os.environ.get("CLAUDE_PLUGIN_DATA", "/tmp/gmail-plugin-data")
PLUGIN_ROOT = os.environ.get("CLAUDE_PLUGIN_ROOT") or str(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

mcp = FastMCP("gmail")

_auth = GmailAuth(PLUGIN_DATA)
_client: GmailClient | None = None
_att: AttachmentManager | None = None
_log: SentLog | None = None
_authenticated = False

_cb = CasaCallback(PLUGIN_ROOT)


def _rebuild_runtime() -> None:
    """Rebuild everything derived from the credential.

    This is _auth.on_activate (wired just below), so it runs INSIDE
    GmailAuth.activate() and its failure modes — two makedirs, a cleanup sweep
    and a thread in AttachmentManager, a file read in SentLog — fail the
    activation itself rather than being discovered after the flow was acked.
    activate() is the ONLY thing that ever sets _auth._credentials, so this is
    also the only place the runtime needs rebuilding: no caller may call it
    again afterwards or the thread would be started twice.
    """
    global _client, _att, _log, _authenticated
    _client = GmailClient(_auth.credentials)
    # _att/_log are credential-independent (no credential goes into their
    # constructors), so a surviving instance is functionally identical to a
    # fresh one — build each only once. This matters because a single
    # startup_recover can activate twice in one call (an active credential
    # AND a pending stage both on disk: load_active() activates, then
    # reconcile_stage()'s promote() activates again). Without the guard, the
    # second AttachmentManager() would start a second cleanup timer thread.
    if _att is None:
        _att = AttachmentManager(PLUGIN_DATA)
    if _log is None:
        _log = SentLog(os.path.join(PLUGIN_DATA, "sent_log.json"))
    _authenticated = True


_auth.on_activate = _rebuild_runtime


def _startup():
    os.makedirs(PLUGIN_DATA, exist_ok=True)
    # startup_recover holds collect.lock across env validation, active-token
    # loading AND staged recovery — all three can mutate the store, so they are
    # one unit. Do NOT call _auth.validate_and_init() separately here.
    # The runtime rebuild needs no call here either: activate() runs it.
    try:
        # Log the outcome: silent startup recovery is how a whole class of
        # lost-outcome bugs went unnoticed. Any user-facing detail is durably
        # queued by startup_recover and drained by the next gmail_auth_collect;
        # this line is the operator's copy.
        outcome = _flow_startup(_auth, _cb)
        print(f"Gmail plugin: credential startup recovery — {outcome}.",
              file=sys.stderr)
    except Exception as exc:       # never let recovery break startup
        print(f"Gmail plugin: credential startup incomplete ({exc}).",
              file=sys.stderr)


def _require_auth() -> None:
    if not _authenticated:
        raise ValueError(
            "Gmail is not authenticated. Call gmail_auth_start to get an "
            "authorization link; after you grant access I'll be notified and "
            "will finish setup with gmail_auth_collect."
        )


def _validate_paths(paths: list[str]) -> None:
    real_data = os.path.realpath(PLUGIN_DATA)
    for path in paths:
        if not os.path.isabs(path) or not os.path.exists(path):
            raise ValueError(f"Attachment path {path} is invalid or outside plugin data directory.")
        real_path = os.path.realpath(path)
        if not (real_path == real_data or real_path.startswith(real_data + os.sep)):
            raise ValueError(f"Attachment path {path} is invalid or outside plugin data directory.")


def _ok(data) -> str:
    return json.dumps(data)


# ── OAuth setup ────────────────────────────────────────────────────────────

@mcp.tool()
def gmail_auth_start() -> str:
    """Begin Gmail OAuth: returns a link to open in a browser. After you grant access the browser shows a confirmation page and the setup completes automatically — nothing to copy back."""
    return _ok(_flow_start(_auth, _cb))


def _stored_credential():
    """The DURABLE credential for the configured subject, or None.

    Deliberately does NOT consult `_authenticated`. Three review rounds found
    the same bug in three disguises, and the common cause was that setup_gmail
    decided from in-memory runtime state: `_authenticated` records whether
    startup happened to succeed, which is a fact about this process, not about
    the credential. The two diverge exactly when it matters — `load_active`
    RETAINS the token and returns False on a rejected client, a transient
    refresh failure, or a failed runtime rebuild (auth.py `load_active`), so a
    perfectly good credential sits on disk with `_authenticated` false. Reading
    the runtime there skipped every check below and minted a link that could
    not complete. The store is the only thing that knows what is actually true.

    A credential for a different inbox, or a legacy one recording no account at
    all, returns None — neither can read as connected, and both are cases the
    mint path already handles (an account mismatch is precisely what needs
    re-authorizing; a v1 file is `load_active`'s to migrate).
    """
    active = _auth.store.load_active()
    if active is None or not active.account:
        return None
    subject = _auth.subject_email
    if not subject or active.account.lower() != subject.lower():
        return None
    return active


def _bring_into_service(cred) -> str | None:
    """Make a proven-live credential usable by the Gmail tools. None on success,
    else the failure detail.

    Reached only when the probe has just shown that `cred` refreshes and
    authorizes the configured account. If the runtime is already up there is
    nothing to do — re-activating would swap the live GmailClient for one whose
    access token has to be fetched again, for no gain.

    Why repair rather than merely report: on the restart path the credential is
    good and `_authenticated` is false, so "already connected" would be true of
    the store and false of the tools — every Gmail call still raises "Gmail is
    not authenticated" until the process is restarted. Reporting a connection
    the operator cannot use is the same class of lie as vouching for a revoked
    one, which is the bug fixed the round before this. `activate()` exists for
    exactly this, its `on_activate` hook rebuilds the runtime, and both are
    idempotent (see `_rebuild_runtime`). It writes no credential and removes
    none: activation is a runtime operation, and the store is untouched — which
    is also why it needs no collect lock, since that lock exists to serialize
    the single staged SLOT (auth_flow.collect_lock) and nothing here goes near
    it.

    Activation is allowed to fail (the hook may raise), and then the answer is
    NOT "connected". It is not a Google problem either, so it is not
    `retry_later`: it is the same shape as a closed callback route — automatic
    setup did not complete, nothing was authorized, and it is worth retrying
    once the local cause is fixed. That is `unavailable`.
    """
    if _authenticated:
        return None
    try:
        _auth.activate(cred)
    except Exception as exc:
        return str(exc)
    return None


def _stored_credential_failure(cred) -> tuple[str, str] | None:
    """None when the stored credential still refreshes, else (kind, detail)
    with kind in {"terminal", "retryable", "configuration"}.

    "configuration" is not a dead credential and must not be treated as one:
    the token endpoint refused the CLIENT (a rotated `GMAIL_CLIENT_SECRET`
    answers `invalid_client`), so the refresh token is still good and a fresh
    authorization would fail at the code exchange for the same reason.

    Nothing else can supply this verdict. The on-disk record keeps looking
    healthy after a revocation, and `_authenticated` — set once at activation
    and never cleared — keeps saying "yes" too, so without this probe a
    credential revoked AFTER startup reports `already_connected` and mints
    nothing, at the exact moment Gmail calls are failing and the operator needs
    a recovery link. It is equally the only verdict available BEFORE any
    activation: `load_active` retains the credential and returns False on a
    rejected client or a transient failure, so on that path the probe is what
    distinguishes a live credential from a dead one and a broken client.

    Performs NO writes and never removes the credential: reaping a dead token
    is `load_active`'s job on its own next pass, and a transient Google 5xx
    must never be able to destroy a working refresh token from here.
    """
    try:
        _auth.probe_refresh(cred.refresh_token)
    except RefreshTerminal as exc:
        return "terminal", str(exc)
    except RefreshRetryable as exc:
        return "retryable", str(exc)
    except RefreshConfigError as exc:
        return "configuration", str(exc)
    except Exception as exc:
        # Unclassified ⇒ retryable, the same policy load_active applies to an
        # ambiguous failure. Minting on "don't know" would hand the operator a
        # re-authorization they probably do not need.
        return "retryable", str(exc)
    return None


# Casa's own pending-state lifetime, read from callback_spool.py
# (`PENDING_TTL_S = 1800`, `SKEW_S = 300`). A minted state is claimable for 30
# minutes; past that casa's request path refuses the claim outright
# (callback_spool.py: `if now - st.st_mtime > PENDING_TTL_S: return None`), so
# the link is dead whatever the attempt record still says. And the record CAN
# still say `awaiting_redirect` well past it: the sweep that retires an expired
# pending runs on a 10-minute interval (casa_core.py, job
# `callback_spool_sweep`), leaving a window of up to ~40 minutes after minting
# in which a dead flow still reads open. That gap is exactly why liveness is
# computed from the mint clock here instead of being read off `status`.
_PENDING_TTL_S = 1800
_SKEW_S = 300


def _mint_is_live(minted, now: float) -> bool:
    """True when a state minted at `minted` is one casa would still claim.

    Mirrors casa's claim gate in both directions: a beyond-skew FUTURE mint
    clock is refused there (`st.st_mtime > now + SKEW_S`) and so is anything
    past the TTL. A missing or non-numeric clock is not live: liveness cannot
    be established for it, and minting a link the operator can definitely use
    beats pointing them at one that may already be dead. A NaN clock fails
    every comparison below and so lands on that same safe side.
    """
    if isinstance(minted, bool) or not isinstance(minted, (int, float)):
        return False
    age = now - minted
    return -_SKEW_S <= age <= _PENDING_TTL_S


def _outstanding_authorization(now: float | None = None) -> bool:
    """True when an authorization this plugin minted is still usable.

    BOTH spool directories are consulted, because casa populates them at
    different times. `mint()` publishes only `pending/<hash>.json`; the record
    in `attempts/` appears when casa's reconciliation pass next runs, five
    minutes apart. Reading `attempts/` alone therefore leaves a multi-minute
    blind window right after minting — the exact window casa's at-least-once
    setup dispatch lands in — during which a second call would see nothing
    outstanding and mint a second live link.

    `pending/` empties again once casa CLAIMS the state (the redirect arrived),
    and `attempts()` covers the flow from reconciliation onward; between the
    two, an outstanding link is visible for as long as it is worth reporting.
    """
    now = time.time() if now is None else now
    for minted in _cb.pending_mint_times():
        if _mint_is_live(minted, now):
            return True
    for rec in _cb.attempts():
        # Only `awaiting_redirect` is a link the operator still has to open;
        # `result_ready` is waiting on gmail_auth_collect instead.
        if rec.get("status") == "awaiting_redirect" \
                and _mint_is_live(rec.get("minted_ts"), now):
            return True
    return False


@mcp.tool()
def setup_gmail() -> str:
    """Connect Gmail: returns an authorization link to open in a browser, or reports that Gmail is already connected. Takes no arguments and is safe to run repeatedly."""
    # Casa auto-runs this once the plugin's trigger-consent episode settles with
    # an approval (plugin_store.manifest_setup_tool), dispatching it to the
    # agent with no arguments. Three consequences shape the body:
    #
    #  * Every decision is taken from the DURABLE store, never from
    #    `_authenticated`. If a credential exists on disk it is probed and the
    #    answer branches on the probe's verdict, whether or not startup managed
    #    to activate it — because the cases where startup did NOT are exactly
    #    the cases this tool is dispatched to explain, and `load_active`
    #    deliberately retains the credential in most of them. See
    #    _stored_credential.
    #  * It must be idempotent — casa may re-dispatch, so an existing, matching
    #    and still-LIVE connection returns a statement of fact and mints
    #    nothing. Re-minting would invalidate a working setup's in-flight links
    #    for no reason. Liveness is checked, not assumed: see
    #    _stored_credential_failure.
    #  * It must not raise when the callback route is closed. Nobody asked for
    #    this call, so an exception surfaces to the operator as a bare tool
    #    error explaining nothing. gmail_auth_start deliberately still raises:
    #    it answers a direct request, where a raise is the honest answer.
    #  * A re-dispatch must not mint a SECOND authorization. `_authenticated`
    #    is still false while the first link is outstanding, so minting again
    #    would produce a second independent state and a second live link —
    #    two authorizations the operator can both complete. Casa's spool
    #    stores only the state HASH, so the earlier `auth_url` cannot be
    #    reconstructed (and this plugin keeps no local flow store, by design);
    #    what it CAN do is see that a state is still outstanding and say so.
    #    That check reads casa's `pending/` directory as well as `attempts/`:
    #    minting publishes only the former, and the latter trails it by up to
    #    a reconciliation interval. See _outstanding_authorization.
    dead_credential = None
    stored = _stored_credential()
    if stored is not None:
        failure = _stored_credential_failure(stored)
        if failure is None:
            # Live and for the right inbox. If startup left it inactive, put it
            # into service before saying so — otherwise "already connected"
            # would be true of the store and false of every Gmail tool.
            blocked = _bring_into_service(stored)
            if blocked is not None:
                return _ok({
                    "status": "unavailable",
                    "account": stored.account,
                    "instructions": (
                        f"Gmail's stored authorization for {stored.account} is "
                        f"valid, but I could not bring it into service "
                        f"({blocked}), so the Gmail tools will still fail. "
                        "Nothing needs re-authorizing and no authorization "
                        "link has been created. Run setup_gmail again, or "
                        "restart the plugin, once that is resolved."
                    ),
                })
            return _ok({
                "status": "already_connected",
                "account": stored.account,
                "instructions": (
                    f"Gmail is already connected as {stored.account} — "
                    "nothing to do. This is not a new authorization; do not "
                    "report it as one. An update, reload or restart does not "
                    "change this authorization: the credential is kept in the "
                    "plugin's data directory, not in the plugin artifact. If "
                    "something reported that the integration would not be "
                    "live until setup ran, this result is the answer to it."
                ),
            })
        kind, detail = failure
        if kind == "retryable":
            # Transient: the credential is presumed good and is untouched.
            # Minting here would start a re-authorization nobody needs.
            return _ok({
                "status": "retry_later",
                "account": stored.account,
                "instructions": (
                    f"Gmail's stored authorization for {stored.account} is in "
                    f"place, but I could not confirm it just now ({detail}). "
                    "This is "
                    "a temporary problem — nothing has changed and no new "
                    "authorization is needed. Do not start one; run "
                    "setup_gmail again shortly if it persists."
                ),
            })
        if kind == "configuration":
            # The client was refused, not the grant. Minting here would be
            # doubly wrong: the stored credential does not need replacing, and
            # the new flow could not complete anyway — its code exchange uses
            # the same rejected client. Name the problem instead.
            return _ok({
                "status": "configuration_error",
                "account": stored.account,
                "instructions": (
                    f"Gmail's stored authorization for {stored.account} is "
                    f"intact and does not need replacing, but Google rejected this "
                    f"plugin's OAuth client credentials ({detail}). This is a "
                    "configuration problem, not a revoked connection: check "
                    "that GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET in casa's "
                    "plugin environment still match the Google OAuth client. "
                    "No authorization link has been created — a new one could "
                    "not complete either, because it would use the same "
                    "rejected credentials. Do not start one. Correcting those "
                    "values is not enough on its own: I read them once when "
                    "this session started, so setup_gmail will keep reporting "
                    "this until a new session (or a plugin restart) picks up "
                    "the corrected values."
                ),
            })
        # Terminal (revoked / invalid_grant): the connection is genuinely
        # broken, so fall through and mint a recovery link. The credential is
        # deliberately NOT removed here.
        dead_credential = detail

    try:
        if _outstanding_authorization():
            return _ok({
                "status": "already_pending",
                "instructions": (
                    "An authorization link for Gmail was already sent and is "
                    "still valid — no new link has been created, because a "
                    "second one would leave two live authorizations. Ask "
                    "the user to use the link from that earlier message. If they "
                    "no longer has it, run setup_gmail again once that link "
                    "expires and a fresh one will be minted."
                ),
            })
        result = _flow_start(_auth, _cb)
    except CallbackUnavailable as exc:
        return _ok({
            "status": "unavailable",
            "instructions": (
                f"Gmail could not be connected yet: {exc} Nothing has been "
                "authorized. Once that is resolved, run setup_gmail again."
            ),
        })
    if dead_credential is not None:
        result["status"] = "reauthorization_needed"
        result["instructions"] = (
            f"The stored Gmail connection is no longer valid "
            f"({dead_credential}), so it must be authorized again — this link "
            f"does that. {result['instructions']}"
        )
    return _ok(result)


@mcp.tool()
def gmail_auth_collect() -> str:
    """Collect any waiting Gmail authorization result and finish setup. Call this when casa reports an authorization result is waiting. Safe to call repeatedly."""
    # No rebuild here: activate() ran _rebuild_runtime before the flow was
    # acked, and a second call would start a second cleanup thread.
    return _ok(_flow_collect(_auth, _cb))


# ── Search / Read ──────────────────────────────────────────────────────────

@mcp.tool()
def search_emails(query: str, max_results: int = 20) -> str:
    """Search Gmail inbox using Gmail query syntax. Returns list of matching emails."""
    _require_auth()
    return _ok(_client.search_emails(query, max_results))


@mcp.tool()
def get_email(message_id: str) -> str:
    """Get full email content including headers, body, and attachment list."""
    _require_auth()
    return _ok(_client.get_email(message_id))


@mcp.tool()
def get_thread(thread_id: str, max_messages: int = 20) -> str:
    """Get all messages in an email thread (oldest first). Includes truncated/total_messages fields."""
    _require_auth()
    return _ok(_client.get_thread(thread_id, max_messages))


# ── Manage ─────────────────────────────────────────────────────────────────

@mcp.tool()
def manage_email(message_id: str, action: str, label: str = "") -> str:
    """Manage an email. action: archive|trash|mark_read|mark_unread|add_label|remove_label. label required for add/remove_label."""
    _require_auth()
    return _ok(_client.manage_email(message_id, action, label))


# ── Attachments ────────────────────────────────────────────────────────────

@mcp.tool()
def list_attachments(message_id: str) -> str:
    """List all attachments on an email (name, MIME type, size)."""
    _require_auth()
    email = _client.get_email(message_id)
    return _ok(email["attachments"])


@mcp.tool()
def download_attachment(message_id: str, attachment_id: str, max_bytes: int = 10485760) -> str:
    """Download an email attachment to the plugin cache. Returns path (ephemeral, 7-day TTL)."""
    _require_auth()
    email_data = _client.get_email(message_id)
    att_meta = next(
        (a for a in email_data["attachments"] if a["attachment_id"] == attachment_id), None
    )
    if att_meta is None:
        raise ValueError(f"Attachment {attachment_id} not found on message {message_id}.")
    size = att_meta["size_bytes"]
    if max_bytes > 0 and size > max_bytes:
        raise ValueError(f"Attachment exceeds size limit of {max_bytes} bytes. Pass max_bytes=0 to disable the limit.")
    data = _client.get_attachment_data(message_id, attachment_id)
    sanitized = _att.sanitize_filename(att_meta["filename"], attachment_id)
    path = _att.save_to_cache(message_id, sanitized, data)
    return _ok({
        "path": path,
        "filename": att_meta["filename"],
        "sanitized_filename": sanitized,
        "mime_type": att_meta["mime_type"],
        "size_bytes": size,
    })


@mcp.tool()
def save_attachment(cached_path: str, destination: str, overwrite: bool = False) -> str:
    """Permanently save a cached attachment. destination is a relative path under saved/."""
    _require_auth()
    path = _att.save_attachment(cached_path, destination, overwrite)
    return _ok({"path": path})


# ── SendAs ─────────────────────────────────────────────────────────────────

@mcp.tool()
def list_send_as() -> str:
    """List available SendAs aliases for the configured Gmail account. Only aliases with verification_status='accepted' can be used as from_address."""
    _require_auth()
    return _ok(_client.list_send_as())

# ── Compose / Send (protected) ─────────────────────────────────────────────

@mcp.tool()
def send_email(
    to: str,
    subject: str,
    body: str,
    request_id: str = "",
    attachment_paths: list[str] | None = None,
    from_address: str = "",
) -> str:
    """Send a plain-text email. from_address: optional SendAs alias (defaults to subject's primary address). Protected: requires the user's tap-approval."""
    _require_auth()
    _validate_paths(attachment_paths or [])
    if request_id:
        existing = _log.check(request_id, to, subject)
        if existing:
            return _ok({"message_id": existing, "already_sent": True})
    msg_id = _client.send_email(to, subject, body, attachment_paths or [], from_address=from_address)
    if request_id:
        _log.record(request_id, msg_id, to, subject)
    return _ok({"message_id": msg_id, "already_sent": False})


@mcp.tool()
def reply_to_thread(
    thread_id: str,
    display_subject: str,
    body: str,
    request_id: str = "",
    attachment_paths: list[str] | None = None,
    from_address: str = "",
) -> str:
    """Reply to an email thread. display_subject is for the approval prompt only. from_address: optional SendAs alias. Protected: requires the user's tap-approval."""
    _require_auth()
    _validate_paths(attachment_paths or [])
    if request_id:
        existing = _log.check(request_id, thread_id, display_subject)
        if existing:
            return _ok({"message_id": existing, "already_sent": True})
    msg_id = _client.reply_to_thread(thread_id, body, attachment_paths or [], from_address=from_address)
    if request_id:
        _log.record(request_id, msg_id, thread_id, display_subject)
    return _ok({"message_id": msg_id, "already_sent": False})


if __name__ == "__main__":
    _startup()
    mcp.run()
