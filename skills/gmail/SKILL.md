---
description: Use when the user asks the agent to read, search, send, reply to, or manage email in their Gmail inbox, or when a task manager needs to process received emails.
---

# Gmail Skill

## Authorization

**If a turn says an authorization result is waiting for the `gmail` plugin — call
`gmail_auth_collect` immediately.** That message is casa delivering an OAuth redirect;
the result expires 900 seconds after it lands. `gmail_auth_collect` takes no arguments
and is safe to call repeatedly.

To connect or reconnect Gmail, call `gmail_auth_start` and give the user the `auth_url`.
Tell them the browser will show "Response received" and that nothing needs copying back.

**Whenever you give the user an `auth_url`, tell them to open it in a real browser rather
than tapping it here — Google refuses OAuth sign-in inside a chat app's built-in
browser.** Say it every time, in one short clause ("open this in Chrome/Safari rather
than tapping it — Google blocks sign-in inside the chat app's browser"), and put that
clause **before the link** in your message, never after — on a phone they tap the link
before they finish reading whatever comes next, so the link must come last. Without that
line the flow fails silently on their phone: they sign in, Google shows a generic
**"Something went wrong"** page, the consent screen never appears and nothing is ever
redirected back, so no result arrives and there is nothing for you to collect.

If they report "Something went wrong" **after signing in**, that is the signature of
this. The fix is to reopen the *same* link in a proper browser (long-press → *Open in
Chrome* / *Open in Safari*, or copy the URL across) — tapping it again will fail
identically, and a new link will not help. Do not mint a fresh authorization for this.

**Unprompted setup:** casa may dispatch `setup_gmail` on its own — it does this once
the user approves the plugin's consent DM, so the instruction arrives without them having
asked for anything. Call it with no arguments and relay its output exactly as you would
`gmail_auth_start`'s: an `auth_url` gets the same "open it in a real browser" and
"Response received" wording. Its other results are not links:

- `status` of `already_connected` → Gmail is already connected as the named `account`
  and nothing was changed. Say that plainly, and do not offer a link — it is not a
  new connection, so **do not report it as a new authorization**.
- `status` of `already_pending` → a valid authorization link was already sent and is
  still good, so no second one was created. Point the user back to the earlier message
  rather than asking for a new link; do not call the tool again to get one.
- `status` of `reauthorization_needed` → the stored connection was found revoked. The
  `auth_url` is a genuine link and reconnects Gmail; relay it as one.
- `status` of `configuration_error` → Google rejected the plugin's OAuth **client**
  credentials, not the user's authorization. The stored connection is intact and needs
  no re-authorizing — and a new link could not work anyway, which is why none was
  created. Relay the `instructions` verbatim (they name what to check) and do **not**
  offer or request an authorization link.
- `status` of `retry_later` → the connection could not be checked just now. Nothing
  changed and nothing needs re-authorizing; say you'll confirm shortly and do **not**
  start an authorization.
- `status` of `unavailable` → **automatic setup did not complete** and no authorization
  was started. This is actionable and retryable, not a dead end: relay the
  `instructions` verbatim — they carry the reason — and say `setup_gmail` can be run
  again once it is resolved. Do not guess at the cause or attribute one of your own.

Reporting rules — the browser page is deliberately identical for success, denial and a
replayed link, so **chat is the only place the user learns the real outcome**:

- `messages` from `gmail_auth_collect` are written for the user. Relay them; do not
  summarise a failure into a success.
- Nothing waiting → `messages` says so in as many words. Relay that too, and never
  report `status: "ok"` on its own as confirmation: a stale or already-handled link
  produces a perfectly successful call that authorized nothing.
- Authorization denied → say so plainly. Do not imply it worked.
- Wrong Google account → report it as a failure, and say the existing connection is
  untouched.
- `redirect_uri_mismatch` from Google → give them the `redirect_uri` value returned by
  `gmail_auth_start` and say it must be registered on the OAuth client exactly.
- `status: "retry_later"` → a transient problem; tell them you'll finish shortly and do
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
