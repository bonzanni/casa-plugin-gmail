import json
import os
import sys

from mcp.server.fastmcp import FastMCP

from auth import GmailAuth
from gmail_client import GmailClient
from attachments import AttachmentManager
from sent_log import SentLog
from auth_flow import collect_pass as _flow_collect
from auth_flow import start as _flow_start
from auth_flow import startup_recover as _flow_startup
from casa_callback import CasaCallback

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
        _flow_startup(_auth, _cb)
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
