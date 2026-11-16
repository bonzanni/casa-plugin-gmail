import base64
import difflib
import os
import re
from datetime import timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import hygiene


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


def _extract_parts(payload: dict, mime_type: str) -> list[str]:
    """The message's parts of a type. Where it offers alternative versions,
    the first with content; any other multipart's parts are pieces of it."""
    if payload.get("filename"):
        # Gmail's contract: filename is present only on attachment parts
        # (attachmentId absent just means the content is inline). Never the body.
        return []
    # Strip Content-Type parameters ("text/html; charset=utf-8") before matching.
    mime = payload.get("mimeType", "").split(";", 1)[0].strip().lower()
    if mime == mime_type:
        text = _decode_part_data(payload.get("body", {}).get("data", ""))
        return [text] if text.strip() else []
    found: list[str] = []
    if mime.startswith("multipart/"):
        for part in payload.get("parts", []):
            pieces = _extract_parts(part, mime_type)
            if pieces and mime == "multipart/alternative":
                return pieces
            found.extend(pieces)
    return found


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


def _body_and_links(payload: dict, include_html: bool) -> dict:
    """Body and link targets (issue #1), and the HTML part for the agent to read.

    Whether a plain part says all the HTML does is the agent's to judge: some
    are a stub, or a placeholder the sender's template left behind. Mail with
    no plain text has its HTML as its body, under include_html or not.
    """
    html_parts = _extract_parts(payload, "text/html")
    plain = "\n\n".join(_extract_parts(payload, "text/plain"))
    html = "\n\n".join(html_parts)
    links = [link for part in html_parts for link in _extract_links(part)]
    result = {"body": plain or html, "links": links}
    if plain and html and include_html:
        result["html"] = html
    return result


def _attachment_parts(payload: dict, root: bool = True) -> list[tuple[str, dict]]:
    """Every downloadable attachment part as ``(attachment_id, part)`` (issue #6).

    The attachment_id handed to callers is the part's partId, which Gmail
    documents as immutable. Gmail's own body.attachmentId is not: it is only
    good for the messages.get response it came from, so a listed one is not
    found on the refetch download_attachment does. The root part's partId is
    "", which is no id to hand a caller, so it is called "root".
    """
    found = []
    body = payload.get("body", {})
    if payload.get("filename") and body.get("attachmentId"):
        key = payload.get("partId") or ("root" if root else body["attachmentId"])
        found.append((key, payload))
    for part in payload.get("parts", []):
        found.extend(_attachment_parts(part, root=False))
    return found


def _attachment_meta(key: str, part: dict) -> dict:
    return {
        "attachment_id": key,
        "filename": part["filename"],
        "mime_type": part.get("mimeType", "application/octet-stream"),
        "size_bytes": part.get("body", {}).get("size", 0),
    }


def _extract_attachments(payload: dict) -> list[dict]:
    return [_attachment_meta(key, part) for key, part in _attachment_parts(payload)]


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


BULK_CEILING = 5000


def _refuse_header_breaks(**fields: str) -> None:
    """A line break in an address field would end the header and start another
    (a hidden Bcc, say), so it is refused before anything reaches Gmail."""
    for name, value in fields.items():
        if "\r" in value or "\n" in value:
            raise ValueError(f"{name} contains a line break; give the addresses "
                             f"on one line, separated by commas.")


# addr-spec, ASCII only: a dot-atom or quoted-string local part, "@", and a
# domain of at least two letter/digit/hyphen labels (no hyphen at either end).
_ATEXT = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
_ADDR_SPEC = re.compile(
    rf'(?:{_ATEXT}(?:\.{_ATEXT})*|"(?:[\x20\x21\x23-\x5b\x5d-\x7e]|\\[\x20-\x7e])+")'
    r"@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+")
_QUOTED_STRING = re.compile(r'"((?:[^"\\]|\\.)*)"', re.S)


def _outside_quotes(text: str):
    """(index, char) for each character of *text* outside a quoted-string,
    or None when a quote is left open."""
    out, quoted, escaped = [], False, False
    for i, ch in enumerate(text):
        if escaped:
            escaped = False
        elif quoted and ch == "\\":
            escaped = True
        elif ch == '"':
            quoted = not quoted
        elif not quoted:
            out.append((i, ch))
    return None if quoted else out


def _strip_comments(text: str):
    """*text* with each RFC 5322 comment (parentheses outside quoted strings,
    nested, backslash escapes) replaced by a space, or None when a comment is
    left open. Quoted strings pass through untouched."""
    out, depth, quoted, escaped = [], 0, False, False
    for ch in text:
        if escaped:
            escaped = False
            if not depth:
                out.append(ch)
        elif ch == "\\" and (quoted or depth):
            escaped = True
            if not depth:
                out.append(ch)
        elif depth:
            depth += {"(": 1, ")": -1}.get(ch, 0)
            if not depth:
                out.append(" ")
        elif quoted:
            out.append(ch)
            quoted = ch != '"'
        elif ch == "(":
            depth = 1
        else:
            out.append(ch)
            quoted = ch == '"'
    return None if depth else "".join(out)


def _parse_address_list(name: str, value: str, sender: bool = False) -> list[tuple[str, str]]:
    """*value* as (display name, addr-spec) pairs, or a ValueError naming the
    field and the entry as given. Nothing is ever rewritten into an address:
    the email package's parser turns what it cannot read into something else
    ("Invoice approver" -> "Invoice"), so this one only accepts, per entry
    between commas outside quotes and <...>, an addr-spec alone or a display
    part followed by <addr-spec>.

    In a list the caller wrote, an unquoted display name may not hold "@",
    "<", ">", ";" or ",": each is a sign of a missing comma ("a@x.com; Bob
    <b@x.com>" would reach only b@x.com), so it is refused; a quoted name is
    free. In the original sender's From header (*sender*) the last <...> is
    the sender and the rest is only a name, and RFC 5322 comments
    ("alice@x.org (Alice Smith)") are removed first. A comment never becomes
    the display name, and a refusal there quotes the entry without them."""
    def refuse(entry: str, why: str = "is not an email address") -> ValueError:
        return ValueError(f'{name} has an entry that {why}: "{entry}". Give each recipient '
                          f"as name@example.com or Name <name@example.com>, separated by commas.")

    if sender:
        stripped = _strip_comments(value)
        if stripped is None:
            raise refuse(value.strip(), "has an unclosed comment")
        value = stripped
    chars = _outside_quotes(value)
    if chars is None:
        raise refuse(value.strip(), "has an unclosed quote")
    entries, start, angle = [], 0, False
    for i, ch in chars:
        if ch == "<":
            angle = True
        elif ch == ">":
            angle = False
        elif ch == "," and not angle:
            entries.append(value[start:i])
            start = i + 1
    entries.append(value[start:])

    pairs = []
    for raw in entries:
        entry = raw.strip()
        if not entry:
            raise ValueError(f"{name} has an empty entry (a stray or trailing comma): "
                             f'"{value}". Separate the addresses with single commas.')
        opens = [i for i, ch in _outside_quotes(entry) if ch == "<"]
        if not opens:
            display, addr = "", entry
        elif entry.endswith(">"):
            display, addr = entry[:opens[-1]].strip(), entry[opens[-1] + 1:-1].strip()
        else:
            raise refuse(entry)
        if not _ADDR_SPEC.fullmatch(addr):
            raise refuse(entry)
        quoted = _QUOTED_STRING.fullmatch(display)
        if quoted:
            display = re.sub(r"\\(.)", r"\1", quoted.group(1), flags=re.S)
        elif not sender and any(ch in display for ch in "@<>;,"):
            raise refuse(entry, "looks like more than one address (a missing comma?); "
                                'quote a display name that holds "@", "<", ">", ";" or ","')
        if "=?" in display:
            from email.errors import HeaderParseError
            from email.header import decode_header, make_header
            try:
                display = str(make_header(decode_header(display)))
            except (HeaderParseError, LookupError, UnicodeDecodeError):
                pass  # only the display name; the address is already exact
        if "\r" in display or "\n" in display:
            raise refuse(entry, "has a line break in its display name")
        pairs.append((display, addr))
    return pairs


def _address_list(name: str, value: str, sender: bool = False) -> str:
    """*value* as an address header that encodes only the display names.

    Set raw, a non-ASCII display name makes the email package encode the whole
    header as one RFC 2047 word, addresses included, and Gmail cannot read the
    recipients in it."""
    from email.utils import formataddr
    return ", ".join(formataddr(pair, charset="utf-8")
                     for pair in _parse_address_list(name, value, sender))


class LabelNotFound(ValueError):
    """No label matches the given name or ID (as opposed to an ambiguous one)."""


def _label_term(label: dict) -> str:
    """The search term selecting mail that carries `label`."""
    if label["id"] == "IMPORTANT":
        return "is:important"
    if label["id"].startswith("CATEGORY_"):
        return "category:" + label["id"][len("CATEGORY_"):].lower()
    return "label:" + hygiene.label_search_term(label["name"])


def _refuse_system_label(label: str) -> None:
    upper = label.upper()
    if upper in SYSTEM_LABELS:
        raise ValueError(SYSTEM_LABEL_ERRORS.get(upper, f"Label {upper} is a system label."))


def _label_view(raw: dict) -> dict:
    view = {"id": raw["id"], "name": raw["name"], "type": str(raw.get("type", "user")).lower()}
    if raw.get("color"):
        view["color"] = raw["color"]
    return view


def _label_error(e: HttpError, label: str) -> ValueError:
    if e.resp.status == 404:
        return ValueError(f"Label '{label}' not found.")
    if e.resp.status == 400:
        return ValueError(f"Gmail rejected the label change for '{label}' (colours must come from "
                          f"Gmail's fixed label palette; names must be unique): {_reason(e)}")
    return _translate_error(e)


def _reason(e: HttpError) -> str:
    try:
        return e._get_reason()
    except Exception:
        return str(e)


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

    def _get_message(self, message_id: str) -> dict:
        try:
            return self._service.users().messages().get(
                userId="me", id=message_id, format="full"
            ).execute()
        except HttpError as exc:
            if exc.resp.status == 404:
                raise ValueError(f"Message {message_id} not found or no longer accessible.")
            raise _translate_error(exc)

    def find_attachment(self, message_id: str, attachment_id: str) -> dict:
        """The listed attachment's metadata from a fresh fetch, plus the
        gmail_attachment_id that fetch gives it — the one to download with.
        A Gmail attachmentId from that same response is accepted too."""
        parts = _attachment_parts(self._get_message(message_id).get("payload", {}))
        for key, part in parts:
            if attachment_id in (key, part["body"]["attachmentId"]):
                return {**_attachment_meta(key, part),
                        "gmail_attachment_id": part["body"]["attachmentId"]}
        raise ValueError(f"Attachment {attachment_id} not found on message {message_id}.")

    def get_email(self, message_id: str) -> dict:
        detail = self._get_message(message_id)
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
            **_body_and_links(detail.get("payload", {}), include_html=True),
            "attachments": _extract_attachments(detail.get("payload", {})),
        }

    def get_thread(self, thread_id: str, max_messages: int = 20, include_html: bool = False) -> dict:
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
                **_body_and_links(msg.get("payload", {}), include_html),
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
            _refuse_system_label(label)
            label = self.resolve_label(label)["id"]
            _refuse_system_label(label)
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

    # ── Labels ─────────────────────────────────────────────────────────────

    def _raw_labels(self) -> list[dict]:
        try:
            return self._service.users().labels().list(userId="me").execute().get("labels", [])
        except HttpError as exc:
            raise _translate_error(exc)

    def list_labels(self) -> list[dict]:
        return [_label_view(l) for l in self._raw_labels()]

    def resolve_label(self, label: str, labels: list[dict] | None = None) -> dict:
        """A label by ID, else by exact name, else by name ignoring case."""
        if not label or not label.strip():
            raise ValueError("A label name or ID is required.")
        labels = self._raw_labels() if labels is None else labels
        for test in (lambda l: l["id"] == label,
                     lambda l: l["name"] == label,
                     lambda l: l["name"].casefold() == label.casefold()):
            hits = [l for l in labels if test(l)]
            if len(hits) == 1:
                return _label_view(hits[0])
            if len(hits) > 1:
                raise ValueError(f"Label '{label}' is ambiguous: "
                                 + ", ".join(h["name"] for h in hits) + ". Use the label ID.")
        close = difflib.get_close_matches(label, [l["name"] for l in labels], n=5, cutoff=0.5)
        hint = f" Close: {', '.join(close)}." if close else " Call list_labels to see them."
        raise LabelNotFound(f"No label named '{label}'.{hint}")

    def create_label(self, name: str, background_color: str = "", text_color: str = "") -> dict:
        name = (name or "").strip()
        if not name:
            raise ValueError("A label name is required.")
        existing = self._find_by_name(name, self._raw_labels())
        if existing:
            return {**_label_view(existing), "created": False}
        body = {"name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"}
        if background_color and text_color:
            body["color"] = {"backgroundColor": background_color, "textColor": text_color}
        try:
            made = self._service.users().labels().create(userId="me", body=body).execute()
        except HttpError as exc:
            if exc.resp.status == 409:  # created meanwhile by another call
                existing = self._find_by_name(name, self._raw_labels())
                if existing:
                    return {**_label_view(existing), "created": False}
            raise _label_error(exc, name)
        return {**_label_view(made), "created": True}

    @staticmethod
    def _find_by_name(name: str, labels: list[dict]) -> dict | None:
        hits = [l for l in labels if l["name"].casefold() == name.casefold()]
        return hits[0] if hits else None

    def update_label(self, label: str, new_name: str = "", background_color: str = "",
                     text_color: str = "") -> dict:
        if not new_name and not (background_color and text_color):
            raise ValueError("Nothing to change: give new_name, or both background_color and text_color.")
        target = self._user_label(label)
        try:
            current = self._service.users().labels().get(userId="me", id=target["id"]).execute()
        except HttpError as exc:
            raise _label_error(exc, label)
        body = {"name": new_name.strip() if new_name else current["name"],
                "labelListVisibility": current.get("labelListVisibility", "labelShow"),
                "messageListVisibility": current.get("messageListVisibility", "show")}
        if background_color and text_color:
            body["color"] = {"backgroundColor": background_color, "textColor": text_color}
        try:
            updated = self._service.users().labels().patch(userId="me", id=target["id"], body=body).execute()
        except HttpError as exc:
            raise _label_error(exc, label)
        return _label_view(updated)

    def delete_label(self, label: str) -> dict:
        try:
            target = self._user_label(label)
        except LabelNotFound:
            return {"deleted": False, "reason": "not found (already deleted?)"}
        try:
            self._service.users().labels().delete(userId="me", id=target["id"]).execute()
        except HttpError as exc:
            if exc.resp.status == 404:
                return {"deleted": False, "reason": "not found (already deleted?)"}
            raise _label_error(exc, label)
        return {"deleted": True, "name": target["name"]}

    def _user_label(self, label: str) -> dict:
        _refuse_system_label(label)
        target = self.resolve_label(label)
        if target["type"] == "system":
            raise ValueError(f"Label {target['name']} is a system label and cannot be changed by this plugin.")
        return target

    # ── Bulk ───────────────────────────────────────────────────────────────

    def _snapshot(self, selector: str, cap: int) -> tuple[list[str], bool]:
        """IDs matching `selector`, up to `cap`, all collected before any change."""
        ids, token = [], None
        msgs = self._service.users().messages()
        try:
            while True:
                kw = {"userId": "me", "q": selector, "maxResults": 500}
                if token:
                    kw["pageToken"] = token
                resp = msgs.list(**kw).execute()
                for m in resp.get("messages", []):
                    if len(ids) == cap:
                        return ids, True
                    ids.append(m["id"])
                token = resp.get("nextPageToken")
                if not token:
                    return ids, False
                if len(ids) == cap:
                    return ids, True
        except HttpError as exc:
            raise _translate_error(exc)

    def apply_to_matching(self, query: str, add_ids: list[str], remove_ids: list[str],
                          needs: list[str], cap: int) -> dict:
        """Select every message matching `query` that still needs a change, then
        apply all the changes at once with batchModify (≤1000 IDs per call)."""
        try:
            ids, more = self._snapshot(hygiene.build_selector(query, needs), cap)
        except ValueError as exc:  # nothing has changed yet
            return {"matched": 0, "processed": 0, "more_remaining": False, "error": str(exc)}
        body = {}
        if add_ids:
            body["addLabelIds"] = list(add_ids)
        if remove_ids:
            body["removeLabelIds"] = list(remove_ids)
        out = {"matched": len(ids), "processed": 0, "more_remaining": more}
        msgs = self._service.users().messages()
        for start in range(0, len(ids), 1000):
            chunk = ids[start:start + 1000]
            try:
                msgs.batchModify(userId="me", body={"ids": chunk, **body}).execute()
            except HttpError as exc:
                out["error"] = str(_translate_error(exc))
                return out
            out["processed"] += len(chunk)
        return out

    def trash_matching(self, query: str, cap: int) -> dict:
        """Trash every message matching `query`, one call per message, stopping at
        the first failure. (A batch HTTP request would send the whole group before
        reporting any failure, and batchModify is not documented to take TRASH.)"""
        ids, more = self._snapshot(hygiene.build_selector(query, []), cap)
        out = {"matched": len(ids), "processed": 0, "more_remaining": more}
        msgs = self._service.users().messages()
        for mid in ids:
            try:
                msgs.trash(userId="me", id=mid).execute()
            except HttpError as exc:
                out["error"] = str(_translate_error(exc))
                return out
            out["processed"] += 1
        return out

    def bulk_manage(self, query: str, action: str, label: str = "", max_messages: int = 500) -> dict:
        valid = {"archive", "trash", "mark_read", "mark_unread", "add_label", "remove_label"}
        if action not in valid:
            raise ValueError(f"Unknown action '{action}'. Valid: {', '.join(sorted(valid))}")
        if not query or not query.strip():
            raise ValueError("A search query is required (use in:inbox to mean the whole inbox).")
        cap = max(1, min(int(max_messages), BULK_CEILING))
        if action == "trash":
            return self.trash_matching(query, cap)
        if action in ("add_label", "remove_label"):
            if not label:
                raise ValueError(f"label is required for action '{action}'.")
            _refuse_system_label(label)
            target = self.resolve_label(label)
            _refuse_system_label(target["id"])
            term = _label_term(target)
            if action == "add_label":
                return self.apply_to_matching(query, [target["id"]], [], [f"-{term}"], cap)
            return self.apply_to_matching(query, [], [target["id"]], [term], cap)
        change = {"archive": ([], ["INBOX"]), "mark_read": ([], ["UNREAD"]),
                  "mark_unread": (["UNREAD"], [])}[action]
        return self.apply_to_matching(query, *change, [hygiene.NEEDS[action]], cap)

    # ── Filters ────────────────────────────────────────────────────────────

    def _raw_filters(self) -> list[dict]:
        try:
            return self._service.users().settings().filters().list(userId="me").execute().get("filter", [])
        except HttpError as exc:
            raise _translate_error(exc)

    def list_filters(self) -> list[dict]:
        names = {l["id"]: l["name"] for l in self._raw_labels()}
        out = []
        for f in self._raw_filters():
            criteria, action = f.get("criteria", {}), dict(f.get("action", {}))
            shown = dict(action)
            for key in ("addLabelIds", "removeLabelIds"):
                if key in shown:
                    shown[key] = [names.get(i, i) for i in shown[key]]
            out.append({"id": f["id"], "description": hygiene.filter_description(criteria, action, names),
                        "criteria": criteria, "action": shown})
        return out

    def create_filter(self, query: str, label: str = "", skip_inbox: bool = False,
                      mark_read: bool = False, star: bool = False,
                      apply_to_existing: bool = False) -> dict:
        if not query or not query.strip():
            raise ValueError("A search query is required for the filter.")
        if not (label or skip_inbox or mark_read or star):
            raise ValueError("Give the filter at least one action: label, skip_inbox, mark_read or star.")
        add_ids, remove_ids, needs = [], [], []
        label_name, created_label = None, False
        if label:
            _refuse_system_label(label)
            labels = self._raw_labels()
            try:
                target = self.resolve_label(label, labels)
            except LabelNotFound:
                made = self.create_label(label)
                target, created_label = made, made["created"]
            _refuse_system_label(target["id"])
            add_ids.append(target["id"])
            needs.append(f"-{_label_term(target)}")
            label_name = target["name"]
        if star:
            add_ids.append("STARRED")
            needs.append(hygiene.NEEDS["star"])
        if mark_read:
            remove_ids.append("UNREAD")
            needs.append(hygiene.NEEDS["mark_read"])
        if skip_inbox:
            remove_ids.append("INBOX")
            needs.append(hygiene.NEEDS["skip_inbox"])
        criteria = {"query": query.strip()}
        action = {k: v for k, v in (("addLabelIds", add_ids), ("removeLabelIds", remove_ids)) if v}

        want = (hygiene.normalize_criteria(criteria), hygiene.normalize_action(action))
        existing = next((f for f in self._raw_filters()
                         if (hygiene.normalize_criteria(f.get("criteria", {})),
                             hygiene.normalize_action(f.get("action", {}))) == want), None)
        if existing:
            filter_id, created = existing["id"], False
        else:
            try:
                made = self._service.users().settings().filters().create(
                    userId="me", body={"criteria": criteria, "action": action}).execute()
            except HttpError as exc:
                raise _translate_error(exc)
            filter_id, created = made["id"], True
        applied = None
        if apply_to_existing:
            applied = self.apply_to_matching(criteria["query"], add_ids, remove_ids, needs, BULK_CEILING)
        return {"filter_id": filter_id, "created": created, "label": label_name,
                "created_label": created_label, "applied_to_existing": applied}

    def delete_filter(self, filter_id: str, description: str) -> dict:
        api = self._service.users().settings().filters()
        try:
            stored = api.get(userId="me", id=filter_id).execute()
        except HttpError as exc:
            if exc.resp.status == 404:
                return {"deleted": False, "reason": "not found"}
            raise _translate_error(exc)
        names = {l["id"]: l["name"] for l in self._raw_labels()}
        actual = hygiene.filter_description(stored.get("criteria", {}), stored.get("action", {}), names)
        if actual != description:
            raise ValueError(f"Filter {filter_id} is '{actual}', not '{description}'. Nothing was "
                             "deleted; call list_filters and pass that filter's description.")
        try:
            api.delete(userId="me", id=filter_id).execute()
        except HttpError as exc:
            if exc.resp.status == 404:
                return {"deleted": False, "reason": "not found"}
            raise _translate_error(exc)
        return {"deleted": True, "description": actual}

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
        attachments: list[tuple[str, bytes]] | None = None,
        from_address: str = "",
        cc: str = "",
        bcc: str = "",
    ) -> str:
        _refuse_header_breaks(to=to, cc=cc, bcc=bcc, from_address=from_address)
        msg = self._build_message(
            to=to,
            subject=subject,
            body=body,
            attachments=attachments or [],
            from_address=from_address,
            cc=cc,
            bcc=bcc,
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
        attachments: list[tuple[str, bytes]] | None = None,
        from_address: str = "",
        cc: str = "",
        bcc: str = "",
    ) -> str:
        _refuse_header_breaks(cc=cc, bcc=bcc, from_address=from_address)
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
        # Read leniently here; formataddr quotes any name holding "@" or "<",
        # so _build_message's strict pass reads the result unchanged.
        reply_to = _address_list("the original sender's From header",
                                 _get_header(h, "From"), sender=True)
        msg_id_header = _get_header(h, "Message-ID")
        refs = _get_header(h, "References")
        msg = self._build_message(
            to=reply_to,
            subject=subject,
            body=body,
            attachments=attachments or [],
            from_address=from_address,
            cc=cc,
            bcc=bcc,
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
        attachments: list[tuple[str, bytes]],
        to: str = "",
        subject: str = "",
        from_address: str = "",
        cc: str = "",
        bcc: str = "",
    ):
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText
        from email.mime.base import MIMEBase
        from email import encoders
        import mimetypes
        to, cc, bcc, from_address = (
            _address_list(name, value) if value else ""
            for name, value in (("to", to), ("cc", cc), ("bcc", bcc),
                                ("from_address", from_address)))
        if attachments:
            msg = MIMEMultipart()
            if to:
                msg["To"] = to
            msg["Subject"] = subject
            if from_address:
                msg["From"] = from_address
            if cc:
                msg["Cc"] = cc
            if bcc:
                # Gmail delivers to the Bcc header's recipients (users.messages.send).
                msg["Bcc"] = bcc
            msg.attach(MIMEText(body, "plain"))
            # (filename, bytes) pairs, already read through a guarded
            # descriptor by the caller — never a path to open here.
            for filename, data in attachments:
                mime_type, _ = mimetypes.guess_type(filename)
                maintype, subtype = (mime_type or "application/octet-stream").split("/", 1)
                part = MIMEBase(maintype, subtype)
                part.set_payload(data)
                encoders.encode_base64(part)
                part.add_header("Content-Disposition", "attachment", filename=filename)
                msg.attach(part)
        else:
            msg = MIMEText(body, "plain")
            if to:
                msg["To"] = to
            msg["Subject"] = subject
            if from_address:
                msg["From"] = from_address
            if cc:
                msg["Cc"] = cc
            if bcc:
                # Gmail delivers to the Bcc header's recipients (users.messages.send).
                msg["Bcc"] = bcc
        return msg
