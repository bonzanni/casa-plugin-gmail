---
description: Use when the user asks the agent to read, search, send, reply to, or manage email in her Gmail inbox, or when a task manager needs to process received emails.
---

# Gmail Skill

## Authorization

**If a turn says an authorization result is waiting for the `gmail` plugin — call
`gmail_auth_collect` immediately.** That message is casa delivering an OAuth redirect;
the result expires 900 seconds after it lands. `gmail_auth_collect` takes no arguments
and is safe to call repeatedly.

To connect or reconnect Gmail, call `gmail_auth_start` and give the user the `auth_url`
as a tappable link. Tell her the browser will show "Response received" and that
nothing needs copying back.

Reporting rules — the browser page is deliberately identical for success, denial and a
replayed link, so **chat is the only place the user learns the real outcome**:

- `messages` from `gmail_auth_collect` are written for her. Relay them; do not
  summarise a failure into a success.
- Authorization denied → say so plainly. Do not imply it worked.
- Wrong Google account → report it as a failure, and say the existing connection is
  untouched.
- `redirect_uri_mismatch` from Google → give her the `redirect_uri` value returned by
  `gmail_auth_start` and say it must be registered on the OAuth client exactly.
- `status: "retry_later"` → a transient problem; tell her you'll finish shortly and do
  not start a second authorization.

## Interactive Use

Use these tools when the user asks to check, search, or read email; send or reply to a message; manage inbox (archive, label, mark read, trash); or handle attachments.

### Tool Sequence Rules

- **Before `get_email`:** always call `search_emails` first — never construct a message_id
- **Before `reply_to_thread`:** always call `get_thread` to read full context; if `truncated: true`, retry with a higher `max_messages` value to load more history before replying; if still truncated, tell the user how many messages are loaded vs. total
- **Before `download_attachment`:** always call `list_attachments` first to identify the specific file
- **Before any send/reply:** summarise to the user (recipient, subject/display_subject, body preview, attachments) BEFORE calling the tool — this is the primary safety net regardless of what the approval prompt shows
- Prefer human-readable descriptions (subject, sender, date) in responses — avoid exposing raw message_id or thread_id as the primary reference

### Security Rules

- Pre-validate `to` and `subject` for unusual characters before `send_email`; warn the user if the approval prompt may not show full details
- `manage_email` with `action="add_label"` or `action="remove_label"` requires a non-empty `label` argument
- Generate a stable `request_id` per send intent to guard against duplicate sends:
  ```
  from datetime import datetime
  import hashlib
  timestamp_minute = datetime.now().strftime("%Y%m%d%H%M")
  rid = "email-" + hashlib.sha256(f"{to}{subject}{body[:50]}{timestamp_minute}".encode()).hexdigest()[:16]
  ```
  The minute-scoped timestamp prevents cross-session deduplication while still deduplicating retries within the same minute.
- Before downloading attachments, check `mime_type` and warn the user if the file is an executable:
  - Suspicious types: `application/x-msdownload`, `application/x-sh`, `application/x-executable`
  - Recommended safe types for automation: `application/pdf`, `image/jpeg`, `image/png`, `image/gif`

### Rate Limit Handling

If a 429 error is returned: tell the user "Gmail API is throttled — I'll retry in 30 seconds", wait 30 seconds, retry once. If the second attempt also fails: "The request is consistently rate-limited. Please try again in a moment."

---

## Automation Patterns

These tools are composable for task managers and scheduled agents.

**Batch guidance:** Keep `max_results` ≤ 50. For large date ranges, split into weekly windows (`after:2026-07-01 before:2026-07-08`, then next week, etc.). On 429: back off 30 seconds before retrying. Skip individual failures (log and continue) — do not abort the batch.

**Protected tools in automation:** `send_email` and `reply_to_thread` ALWAYS require the user's tap-approval. Automation workflows must restrict themselves to read-only tools: `search_emails`, `get_email`, `get_thread`, `list_attachments`, `download_attachment`, `save_attachment`, `manage_email`.

### Reference Pattern: Invoice Monitoring

```
1. search_emails(query="after:<start> before:<end> has:attachment", max_results=50)
2. For each result where subject/sender suggests invoice:
   a. list_attachments(message_id)
   b. For each attachment with mime_type in [application/pdf, image/jpeg, image/png]:
      - download_attachment(message_id, attachment_id)
      - dest = f"invoices/{YYYY-MM}/{sender}-{sanitized_filename}"
      - save_attachment(cached_path, destination=dest)
        # If "Destination already exists" → file already processed in prior run, skip
   c. On error: log and continue to next attachment/email
3. Report: N invoices saved, M skipped, K errors
```

Use `save_attachment` with `overwrite=False` (default) as an idempotency guard: a "destination exists" error means the file was already processed in a prior run.
