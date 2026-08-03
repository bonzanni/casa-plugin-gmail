import json
import os
import sys
import time

from mcp.server.fastmcp import FastMCP

from auth import GmailAuth, RefreshRetryable, RefreshTerminal
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


def _connected_credential():
    """The credential actually in service, or None.

    "In service" is both halves: a rebuilt runtime (`_authenticated`) AND an
    active credential on disk whose account is the configured subject. A
    credential for a different inbox is precisely the case that needs
    re-authorization, so it must not read as connected.
    """
    if not _authenticated:
        return None
    active = _auth.store.load_active()
    if active is None or not active.account:
        return None
    subject = _auth.subject_email
    if not subject or active.account.lower() != subject.lower():
        return None
    return active


def _stored_credential_failure(cred) -> tuple[str, str] | None:
    """None when the stored credential still refreshes, else (kind, detail)
    with kind in {"terminal", "retryable"}.

    `_authenticated` is set once, at activation, and never cleared, and the
    on-disk account keeps matching after a revocation — so without this probe
    a credential revoked AFTER startup reports `already_connected` and mints
    nothing, telling the operator there is nothing to do at the exact moment
    Gmail calls are failing and they need a recovery link.

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


def _live_pending_attempt(now: float | None = None) -> dict | None:
    """The newest still-usable `awaiting_redirect` attempt, or None.

    `cb.attempts()` returns casa-validated records newest first, so the first
    match is the newest. `minted_ts` is legitimately None on a legacy or
    consumer-held record; liveness cannot be established for one, so it does
    NOT count as pending — minting a link the operator can definitely use
    beats pointing them at one that may already be dead.
    """
    now = time.time() if now is None else now
    for rec in _cb.attempts():
        if rec.get("status") != "awaiting_redirect":
            continue
        minted = rec.get("minted_ts")
        if not isinstance(minted, (int, float)) or isinstance(minted, bool):
            continue
        age = now - minted
        # Mirrors casa's claim gate in both directions: a beyond-skew future
        # mint clock is refused there, and so is anything past the TTL.
        if -_SKEW_S <= age <= _PENDING_TTL_S:
            return rec
    return None


@mcp.tool()
def setup_gmail() -> str:
    """Connect Gmail: returns an authorization link to open in a browser, or reports that Gmail is already connected. Takes no arguments and is safe to run repeatedly."""
    # Casa auto-runs this once the plugin's trigger-consent episode settles with
    # an approval (plugin_store.manifest_setup_tool), dispatching it to the
    # agent with no arguments. Three consequences shape the body:
    #
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
    #    what it CAN do is see that an attempt is still outstanding and say so.
    dead_credential = None
    connected = _connected_credential()
    if connected is not None:
        failure = _stored_credential_failure(connected)
        if failure is None:
            return _ok({
                "status": "already_connected",
                "account": connected.account,
                "instructions": (
                    f"Gmail is already connected as {connected.account} — "
                    "nothing to do. This is not a new authorization; do not "
                    "report it as one."
                ),
            })
        kind, detail = failure
        if kind == "retryable":
            # Transient: the credential is presumed good and is untouched.
            # Minting here would start a re-authorization nobody needs.
            return _ok({
                "status": "retry_later",
                "account": connected.account,
                "instructions": (
                    f"Gmail is connected as {connected.account}, but I could "
                    f"not confirm the connection just now ({detail}). This is "
                    "a temporary problem — nothing has changed and no new "
                    "authorization is needed. Do not start one; run "
                    "setup_gmail again shortly if it persists."
                ),
            })
        # Terminal (revoked / invalid_grant): the connection is genuinely
        # broken, so fall through and mint a recovery link. The credential is
        # deliberately NOT removed here.
        dead_credential = detail

    try:
        pending = _live_pending_attempt()
        if pending is not None:
            return _ok({
                "status": "already_pending",
                "instructions": (
                    "An authorization link for Gmail was already sent and is "
                    "still valid — no new link has been created, because a "
                    "second one would leave two live authorizations. Ask "
                    "the user to use the link from that earlier message. If she "
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
    """Send a plain-text email. from_address: optional SendAs alias (defaults to subject's primary address). Protected: requires the user tap-approval."""
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
    """Reply to an email thread. display_subject is for the approval prompt only. from_address: optional SendAs alias. Protected: requires the user tap-approval."""
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
