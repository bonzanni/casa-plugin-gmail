import base64
import os
from datetime import timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


SYSTEM_LABELS = {"INBOX", "SENT", "DRAFT", "TRASH", "SPAM", "UNREAD", "STARRED"}
SYSTEM_LABEL_ERRORS = {
    "INBOX": "Use action='archive' to remove from inbox.",
    "TRASH": "Use action='trash' to move to trash.",
    "UNREAD": "Use action='mark_read' or 'mark_unread' instead.",
    "SENT": "Label SENT is a system label and cannot be modified by this plugin.",
    "DRAFT": "Label DRAFT is a system label and cannot be modified by this plugin.",
    "SPAM": "Label SPAM is a system label and cannot be modified by this plugin.",
    "STARRED": "Label STARRED is a system label and cannot be modified by this plugin.",
}


def _parse_date(date_str: str | None) -> str | None:
    if not date_str:
        return None
    try:
        dt = parsedate_to_datetime(date_str)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return None


def _get_header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _decode_part_data(data: str) -> str:
    if not data:
        return ""
    missing = (4 - len(data) % 4) % 4
    return base64.urlsafe_b64decode(data + "=" * missing).decode("utf-8", errors="replace")


def _extract_part(payload: dict, mime_type: str) -> str:
    if payload.get("filename"):
        return ""  # attachments are _extract_attachments' business, never the body
    # Strip Content-Type parameters ("text/html; charset=utf-8") before matching.
    mime = payload.get("mimeType", "").split(";", 1)[0].strip().lower()
    if mime == mime_type:
        return _decode_part_data(payload.get("body", {}).get("data", ""))
    if mime.startswith("multipart/"):
        for part in payload.get("parts", []):
            body = _extract_part(part, mime_type)
            if body:
                return body
    return ""


def _extract_body(payload: dict) -> str:
    return _extract_part(payload, "text/plain")


class _LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[dict] = []
        self._href: str | None = None
        self._text_parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            # First href wins on duplicates, matching how browsers render.
            href = next((v for k, v in attrs if k == "href"), None)
            if href:
                self._href = href
                self._text_parts = []

    def handle_data(self, data):
        if self._href is not None:
            self._text_parts.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            text = " ".join("".join(self._text_parts).split())
            self.links.append({"text": text, "href": self._href})
            self._href = None
            self._text_parts = []


def _extract_links(html: str) -> list[dict]:
    parser = _LinkParser()
    parser.feed(html)
    return parser.links


def _body_and_links(payload: dict) -> dict:
    """Body plus link targets from the HTML part (issue #1).

    Plain text stays the body; HTML-only mail falls back to the raw HTML
    so the body is never empty when content exists.
    """
    body = _extract_body(payload)
    html = _extract_part(payload, "text/html")
    if not body and html:
        body = html
    return {"body": body, "links": _extract_links(html) if html else []}


def _extract_attachments(payload: dict) -> list[dict]:
    attachments = []
    body = payload.get("body", {})
    if payload.get("filename") and body.get("attachmentId"):
        attachments.append({
            "attachment_id": body["attachmentId"],
            "filename": payload["filename"],
            "mime_type": payload.get("mimeType", "application/octet-stream"),
            "size_bytes": body.get("size", 0),
        })
    for part in payload.get("parts", []):
        attachments.extend(_extract_attachments(part))
    return attachments


def _translate_error(e: HttpError) -> ValueError:
    if e.resp.status == 404:
        return ValueError("Message not found or no longer accessible.")
    if e.resp.status == 429:
        return ValueError("Gmail API rate limit reached. Retry in a moment.")
    try:
        reason = e._get_reason()
    except Exception:
        reason = str(e)
    return ValueError(f"Gmail API error {e.resp.status}: {reason}")


class GmailClient:
    def __init__(self, credentials):
        self._service = build("gmail", "v1", credentials=credentials, cache_discovery=False)

    def get_profile_email(self) -> str:
        """The address this credential actually authorizes."""
        try:
            resp = self._service.users().getProfile(userId="me").execute()
        except HttpError as exc:
            raise _translate_error(exc)
        return resp.get("emailAddress", "")

    # ── Search / Read ──────────────────────────────────────────────────────

    def search_emails(self, query: str, max_results: int = 20) -> list[dict]:
        max_results = min(max_results, 100)
        try:
            resp = self._service.users().messages().list(
                userId="me", q=query, maxResults=max_results
            ).execute()
        except HttpError as exc:
            raise _translate_error(exc)
        messages = resp.get("messages", [])
        results = []
        for msg in messages:
            try:
                detail = self._service.users().messages().get(
                    userId="me", id=msg["id"], format="metadata",
                    metadataHeaders=["Subject", "From", "Date"]
                ).execute()
            except HttpError as exc:
                raise _translate_error(exc)
            headers = detail.get("payload", {}).get("headers", [])
            results.append({
                "message_id": detail["id"],
                "thread_id": detail["threadId"],
                "subject": _get_header(headers, "Subject") or "(no subject)",
                "from": _get_header(headers, "From") or "(unknown sender)",
                "date": _parse_date(_get_header(headers, "Date")),
                "snippet": detail.get("snippet", ""),
            })
        return results

    def get_email(self, message_id: str) -> dict:
        try:
            detail = self._service.users().messages().get(
                userId="me", id=message_id, format="full"
            ).execute()
        except HttpError as exc:
            if exc.resp.status == 404:
                raise ValueError(f"Message {message_id} not found or no longer accessible.")
            raise _translate_error(exc)
        headers = detail.get("payload", {}).get("headers", [])
        return {
            "message_id": detail["id"],
            "thread_id": detail["threadId"],
            "headers": {
                "from": _get_header(headers, "From") or "(unknown sender)",
                "to": _get_header(headers, "To"),
                "cc": _get_header(headers, "Cc"),
                "date": _parse_date(_get_header(headers, "Date")),
                "subject": _get_header(headers, "Subject") or "(no subject)",
            },
            **_body_and_links(detail.get("payload", {})),
            "attachments": _extract_attachments(detail.get("payload", {})),
        }

    def get_thread(self, thread_id: str, max_messages: int = 20) -> dict:
        max_messages = min(max_messages, 50)
        try:
            resp = self._service.users().threads().get(
                userId="me", id=thread_id, format="full"
            ).execute()
        except HttpError as exc:
            if exc.resp.status == 404:
                raise ValueError(f"Thread {thread_id} not found or no longer accessible.")
            raise _translate_error(exc)
        all_msgs = resp.get("messages", [])
        total = len(all_msgs)
        truncated = total > max_messages
        selected = all_msgs[-max_messages:] if truncated else all_msgs
        messages = []
        for msg in selected:
            headers = msg.get("payload", {}).get("headers", [])
            messages.append({
                "message_id": msg["id"],
                "thread_id": msg["threadId"],
                "trashed": "TRASH" in msg.get("labelIds", []),
                "headers": {
                    "from": _get_header(headers, "From") or "(unknown sender)",
                    "to": _get_header(headers, "To"),
                    "cc": _get_header(headers, "Cc"),
                    "date": _parse_date(_get_header(headers, "Date")),
                    "subject": _get_header(headers, "Subject") or "(no subject)",
                },
                **_body_and_links(msg.get("payload", {})),
                "attachments": _extract_attachments(msg.get("payload", {})),
            })
        return {"messages": messages, "truncated": truncated, "total_messages": total}

    def get_attachment_data(self, message_id: str, attachment_id: str) -> bytes:
        try:
            resp = self._service.users().messages().attachments().get(
                userId="me", messageId=message_id, id=attachment_id
            ).execute()
        except HttpError as exc:
            raise _translate_error(exc)
        raw_data = resp.get("data", "")
        missing = (4 - len(raw_data) % 4) % 4
        return base64.urlsafe_b64decode(raw_data + "=" * missing)

    # ── Manage ─────────────────────────────────────────────────────────────

    def manage_email(self, message_id: str, action: str, label: str = "") -> dict:
        valid = {"archive", "trash", "mark_read", "mark_unread", "add_label", "remove_label"}
        if action not in valid:
            raise ValueError(f"Unknown action '{action}'. Valid: {', '.join(sorted(valid))}")
        if action in ("add_label", "remove_label"):
            if not label:
                raise ValueError(f"label is required for action '{action}'.")
            upper = label.upper()
            if upper in SYSTEM_LABELS:
                raise ValueError(SYSTEM_LABEL_ERRORS.get(upper, f"Label {upper} is a system label."))
        try:
            svc = self._service.users().messages()
            if action == "archive":
                svc.modify(userId="me", id=message_id, body={"removeLabelIds": ["INBOX"]}).execute()
            elif action == "trash":
                svc.trash(userId="me", id=message_id).execute()
            elif action == "mark_read":
                svc.modify(userId="me", id=message_id, body={"removeLabelIds": ["UNREAD"]}).execute()
            elif action == "mark_unread":
                svc.modify(userId="me", id=message_id, body={"addLabelIds": ["UNREAD"]}).execute()
            elif action == "add_label":
                svc.modify(userId="me", id=message_id, body={"addLabelIds": [label]}).execute()
            elif action == "remove_label":
                svc.modify(userId="me", id=message_id, body={"removeLabelIds": [label]}).execute()
        except HttpError as exc:
            if exc.resp.status == 404:
                raise ValueError(f"Message {message_id} not found or no longer accessible.")
            raise _translate_error(exc)
        return {"success": True}

    # ── SendAs ─────────────────────────────────────────────────────────────

    def list_send_as(self) -> list[dict]:
        try:
            resp = self._service.users().settings().sendAs().list(userId="me").execute()
        except HttpError as exc:
            raise _translate_error(exc)
        return [
            {
                "send_as_email": alias.get("sendAsEmail", ""),
                "display_name": alias.get("displayName", ""),
                "is_default": alias.get("isDefault", False),
                "is_primary": alias.get("isPrimary", False),
                "reply_to_address": alias.get("replyToAddress", ""),
                "verification_status": alias.get("verificationStatus", ""),
            }
            for alias in resp.get("sendAs", [])
        ]

    # ── Send / Reply ────────────────────────────────────────────────────────

    def send_email(
        self,
        to: str,
        subject: str,
        body: str,
        attachment_paths: list[str] | None = None,
        from_address: str = "",
    ) -> str:
        msg = self._build_message(
            to=to,
            subject=subject,
            body=body,
            attachment_paths=attachment_paths or [],
            from_address=from_address,
        )
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        try:
            resp = self._service.users().messages().send(userId="me", body={"raw": raw}).execute()
        except HttpError as exc:
            raise _translate_error(exc)
        return resp["id"]

    def reply_to_thread(
        self,
        thread_id: str,
        body: str,
        attachment_paths: list[str] | None = None,
        from_address: str = "",
    ) -> str:
        try:
            thread = self._service.users().threads().get(
                userId="me", id=thread_id, format="metadata",
                metadataHeaders=["Subject", "From", "To", "Message-ID", "References"]
            ).execute()
        except HttpError as exc:
            raise _translate_error(exc)
        msgs = thread.get("messages", [])
        if not msgs:
            raise ValueError(f"Thread {thread_id} not found or no longer accessible.")
        last = msgs[-1]
        h = last.get("payload", {}).get("headers", [])
        subject = _get_header(h, "Subject") or ""
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"
        reply_to = _get_header(h, "From")
        msg_id_header = _get_header(h, "Message-ID")
        refs = _get_header(h, "References")
        msg = self._build_message(
            to=reply_to,
            subject=subject,
            body=body,
            attachment_paths=attachment_paths or [],
            from_address=from_address,
        )
        msg["In-Reply-To"] = msg_id_header
        msg["References"] = f"{refs} {msg_id_header}".strip() if refs else msg_id_header
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        try:
            resp = self._service.users().messages().send(
                userId="me", body={"raw": raw, "threadId": thread_id}
            ).execute()
        except HttpError as exc:
            raise _translate_error(exc)
        return resp["id"]

    def _build_message(
        self,
        body: str,
        attachment_paths: list[str],
        to: str = "",
        subject: str = "",
        from_address: str = "",
    ):
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText
        from email.mime.base import MIMEBase
        from email import encoders
        import mimetypes
        if attachment_paths:
            msg = MIMEMultipart()
            if to:
                msg["To"] = to
            msg["Subject"] = subject
            if from_address:
                msg["From"] = from_address
            msg.attach(MIMEText(body, "plain"))
            for path in attachment_paths:
                mime_type, _ = mimetypes.guess_type(path)
                maintype, subtype = (mime_type or "application/octet-stream").split("/", 1)
                with open(path, "rb") as f:
                    part = MIMEBase(maintype, subtype)
                    part.set_payload(f.read())
                    encoders.encode_base64(part)
                    part.add_header("Content-Disposition", "attachment", filename=os.path.basename(path))
                    msg.attach(part)
        else:
            msg = MIMEText(body, "plain")
            if to:
                msg["To"] = to
            msg["Subject"] = subject
            if from_address:
                msg["From"] = from_address
        return msg
