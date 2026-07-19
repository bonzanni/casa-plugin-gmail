import json
import os
import shutil
import urllib.parse

from mcp.server.fastmcp import FastMCP

from auth import GmailAuth
from gmail_client import GmailClient
from attachments import AttachmentManager
from sent_log import SentLog

PLUGIN_DATA = os.environ.get("CLAUDE_PLUGIN_DATA", "/tmp/gmail-plugin-data")

mcp = FastMCP("gmail")

_auth = GmailAuth(PLUGIN_DATA)
_client: GmailClient | None = None
_att: AttachmentManager | None = None
_log: SentLog | None = None
_authenticated = False


def _migrate_legacy_token() -> None:
    # v0.4.0 self-declared CLAUDE_PLUGIN_DATA in .mcp.json, which the runtime
    # delivered as the literal unexpanded string "${CLAUDE_PLUGIN_DATA}". If a
    # token was persisted there, move it to the now-correct PLUGIN_DATA path.
    old_token = os.path.join("${CLAUDE_PLUGIN_DATA}", "oauth_token.json")
    new_token = os.path.join(PLUGIN_DATA, "oauth_token.json")
    if os.path.exists(old_token) and not os.path.exists(new_token):
        os.makedirs(PLUGIN_DATA, exist_ok=True)
        shutil.move(old_token, new_token)


def _startup():
    global _client, _att, _log, _authenticated
    _migrate_legacy_token()
    os.makedirs(PLUGIN_DATA, exist_ok=True)
    _authenticated = _auth.validate_and_init()
    if _authenticated:
        _client = GmailClient(_auth.credentials)
        _att = AttachmentManager(PLUGIN_DATA)
        _log = SentLog(os.path.join(PLUGIN_DATA, "sent_log.json"))


def _require_auth() -> None:
    if not _authenticated:
        raise ValueError(
            "Gmail not authenticated. Use gmail_auth_start to get the authorization URL, "
            "visit it in a browser, then call gmail_auth_complete with the redirect URL "
            "from your browser's address bar."
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
    """Begin Gmail OAuth: returns an authorization URL to open in any browser. After granting access, the browser redirects to localhost (which won't load) — copy the full URL from the address bar and call gmail_auth_complete with it."""
    url = _auth.build_auth_url()
    return _ok({
        "auth_url": url,
        "instructions": (
            "Open auth_url in any browser and sign in. After granting access, your browser "
            "will redirect to http://localhost:8080 which won't load — that is expected. "
            "Copy the full URL from your browser's address bar (it contains ?code=...) "
            "and call gmail_auth_complete with that URL."
        ),
    })


@mcp.tool()
def gmail_auth_complete(redirect_url: str) -> str:
    """Complete Gmail OAuth by exchanging the authorization code from the redirect URL. Persists the refresh token to the plugin data directory. Protected: requires operator approval."""
    global _client, _att, _log, _authenticated

    parsed = urllib.parse.urlparse(redirect_url)
    params = urllib.parse.parse_qs(parsed.query)

    if "error" in params:
        raise ValueError(f"OAuth error: {params['error'][0]}")

    codes = params.get("code")
    if not codes:
        raise ValueError(
            "No authorization code found in redirect URL. "
            "Ensure you copied the full URL from the browser address bar after the redirect."
        )
    code = codes[0]

    _auth.exchange_code(code)

    _client = GmailClient(_auth.credentials)
    _att = AttachmentManager(PLUGIN_DATA)
    _log = SentLog(os.path.join(PLUGIN_DATA, "sent_log.json"))
    _authenticated = True

    return _ok({
        "status": "authenticated",
        "message": "Gmail OAuth complete. Refresh token persisted to plugin data directory. All Gmail tools are now available.",
    })


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
