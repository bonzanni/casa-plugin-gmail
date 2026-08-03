# casa-plugin-gmail

Gives the agent (Casa resident assistant) full Gmail access on the user's behalf via per-user OAuth 2.0. Authorization runs through casa's authorization-callback facility: the user opens a link, grants access in the browser, and casa delivers the result back to the agent automatically — nothing is copied and pasted between browser and chat. The refresh token is persisted inside the plugin's data directory (`CLAUDE_PLUGIN_DATA`). No service account, no domain-wide delegation, no gcloud, no ADC.

## Prerequisites

- Google Cloud project with Gmail API enabled
- An OAuth 2.0 client of type **Web application** — not Desktop app (see below). The consent screen's **user type** depends on your account, and only one of the two is ever selectable: see Step 2.
- A casa deployment with `public_url` set to a public `https://` origin, and the plugin assigned a reachable role

### Why the OAuth client must be a Web application client

Casa's callback facility publishes a real, public `https://` redirect URI for this plugin and expects Google to redirect the user's browser straight to it. A **Desktop app** OAuth client only accepts loopback redirect URIs (`http://localhost:...`) or custom URI schemes — it cannot be configured with a public `https://` redirect URI at all, so it cannot work with this flow. The client **must** be created as **Web application** type.

## Setup

Setup spans two systems: the Google Cloud project (OAuth client) and the casa deployment (public URL, plugin role, consent, env vars). Both are required — skipping either leaves authorization stuck, usually without an obvious error.

### 1. Enable Gmail API

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and select your project
2. Navigate to **APIs & Services → Library**
3. Search for "Gmail API" and click **Enable**

### 2. Create a Web application OAuth client

1. Navigate to **[console.cloud.google.com/auth/clients](https://console.cloud.google.com/auth/clients)** — this page replaced the old "APIs & Services → Credentials" location.
2. Click **Create client**.
3. Application type: **Web application** — name it e.g. `casa-gmail`. Do **not** choose Desktop app; see above for why.
4. Leave **Authorized JavaScript origins** empty — this is not a browser-side (implicit/PKCE-in-browser) flow, and this field rejects paths anyway, so it cannot hold a callback URL.
5. Leave **Authorized redirect URIs** empty for now — you'll come back and fill it in during Step 3, once casa has told you the exact value to use.
6. Click **Create**, then note the **Client ID** and **Client Secret** shown in the dialog.

> If prompted to configure an OAuth consent screen first, fill in the app name and add the three Gmail scopes listed below. **User type** (called **Audience** in the current *Google Auth Platform* console): **Internal** is offered *only* inside a Google Workspace organization, needs no Google verification, and is free of the expiry problem described below; a personal Gmail account cannot select it and must use **External**. Neither choice changes the client type — it must still be **Web application**.

**Scopes needed** (add these on the consent screen — **Data Access** in the current console):
```
https://www.googleapis.com/auth/gmail.modify
https://www.googleapis.com/auth/gmail.send
https://www.googleapis.com/auth/gmail.settings.basic
```

#### External apps: publish, or the connection expires every 7 days

This is the one setup decision that breaks the plugin *later* rather than immediately, so it is worth getting right now. It does not apply to **Internal** (Workspace) apps.

An **External** app whose publishing status is **Testing** is issued refresh tokens that expire after **7 days**. Google's wording: *"A Google Cloud Platform project with an OAuth consent screen configured for an external user type and a publishing status of "Testing" is issued a refresh token expiring in 7 days, unless the only OAuth scopes requested are a subset of name, email address, and user profile"* ([OAuth 2.0 docs](https://developers.google.com/identity/protocols/oauth2), *Refresh token expiration*). Gmail scopes are not in that subset, and adding your own address under **Test users** does not exempt you — the limit applies to test users, including the project owner. In practice the plugin would lose Gmail access roughly weekly, each time needing a fresh `gmail_auth_start`, which defeats the point of persisting a refresh token at all.

**So set the publishing status to "In production"** (**Google Auth Platform → Audience → Publish app**). That, and nothing else, is what removes the 7-day expiry. Publishing does **not** require passing Google verification first, and for a personal single-user install you should not attempt verification:

- **What unverified production costs you.** `gmail.modify` and `gmail.settings.basic` are **restricted** scopes and `gmail.send` is **sensitive**, so until the app is verified the consent screen shows *"Google hasn't verified this app"* and you must click **Advanced → Go to … (unsafe)** to continue. The project is also capped at 100 users for its lifetime. Google allows this explicitly for personal use: *"If the app is for your personal use (fewer than 100 users), you and your limited number of users can continue using the app without going through verification"* ([Exceptions to verification requirements](https://support.google.com/cloud/answer/13464323)). Refresh tokens issued this way do not carry the Testing 7-day expiry.
- **What verification would cost you.** Restricted-scope verification is a ~6-week review and requires an annual third-party [CASA security assessment](https://support.google.com/cloud/answer/13465431), arranged and paid for directly with an assessor. That is a real project, not a formality — and it is unnecessary for one inbox.

Refresh tokens can still be invalidated for the ordinary reasons in any publishing status: you revoke access, the token goes six months unused, or (with Gmail scopes) the account password changes.

### 3. Configure the casa deployment

This is where most setup failures happen — each prerequisite below fails silently or with a generic-looking error if skipped. Do these in order.

1. **Set casa's `public_url`** to a clean `https://` origin: scheme + host only — no path, no trailing slash, no IP literal, no userinfo (e.g. `https://<your-casa-public-url>`, not `https://<your-casa-public-url>/`, not `https://1.2.3.4`, not `https://user@<your-casa-public-url>`). Casa needs this to construct a real, reachable redirect URI for the plugin.
2. **Give the plugin a reachable assigned role** *before* installing it. Casa uses this to know where to deliver the callback result.
3. **Install the plugin**, then **approve casa's callback-consent DM**. Casa asks for explicit consent before it will spool authorization results for this plugin; until approved, the callback route stays closed. Approving it is also what triggers Step 4: the plugin declares `setup_gmail` as its setup tool, and casa hands that to the agent automatically once consent settles.
4. **Set `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_USER_EMAIL`** in casa's plugin environment configuration for this plugin (not in a secret manager directly — casa is what passes these through to the plugin's server process). `GMAIL_USER_EMAIL` is the user's Gmail address (e.g. `user@workspace.example.com`). Do this **before** the next step: without all three the plugin's server exits at startup, so none of its tools — including the one that reports the redirect URI — can answer.
5. **Register casa's authoritative redirect URI** — the *exact* string — in the OAuth client's **Authorized redirect URIs** (the field you left empty in Step 2). Never construct this value yourself from the plugin's name: a scoped install has a different effective name than the plugin's base name, and Google matches the redirect URI byte-for-byte. Read it from one of these instead:
   - **Preferred:** ask the agent to connect Gmail (she calls `gmail_auth_start`) — the tool returns a `redirect_uri` field. The plugin reads that straight out of casa's callback index, so it is the same value casa will actually use. Requires the three variables from Step 3.4 to be set and the plugin's server to be running; the `auth_url` it also returns will report `redirect_uri_mismatch` until you finish this step, which is expected.
   - **From the host,** without a running server and without having to know the effective name:
     ```
     grep -o '"redirect_uri":[^,}]*' /data/callbacks/*/ready.json
     ```
   *Optional, rollback only:* if you are upgrading from v0.4.x, also keep (or add) `http://localhost:8080` as a second entry in **Authorized redirect URIs** — v0.4.1 used that loopback flow, so leaving it registered means a downgrade needs no Google console round-trip. It is unused by v0.5.0 and can be removed once the callback flow is confirmed working.

#### What breaks if a step above is skipped

Casa closes an authorization-callback route for exactly five reasons. The plugin cannot distinguish between them and reports all five as the same generic error, so use this table to diagnose from context instead:

| Casa reason code | Which step was skipped | How it presents |
|---|---|---|
| `callback_base_url_invalid` | `public_url` (Step 3.1) missing or not a clean `https://` origin | The callback route never becomes usable; the redirect URI in `ready.json` is missing or unusable |
| `callback_no_target` | Plugin has no reachable assigned role (Step 3.2) | The callback stays dark and **no consent DM is ever sent** — indistinguishable from casa simply not having gotten to it yet; if you've waited and there's still no DM, check the plugin's role assignment |
| `callback_pending_ack` | Consent DM not approved (Step 3.3) | Authorization result cannot be delivered until you approve the DM |
| `callback_invalid` | Malformed or stale callback state | Retry `gmail_auth_start` for a fresh authorization attempt |
| `callback_spool_error` | Casa-side failure writing the spooled result | Transient; retry `gmail_auth_start` |

### 4. Run the authorization flow

1. **Wait for the link — you should not have to ask for it.** Once the consent DM in Step 3.3 is approved, casa dispatches `setup_gmail` automatically and the agent posts the `auth_url` in chat without being asked. This is the normal path; the plugin declares the tool for exactly this purpose.
2. Open the link and sign in as the user. After granting access, the browser shows "Response received" — nothing more happens there, and nothing needs to be copied back.
3. Casa delivers the result to the agent, which calls `gmail_auth_collect` and reports the outcome in chat. Success, denial, and a stale/replayed link all show the same neutral browser page, so **chat is the only place you learn whether it actually worked** — read what the agent reports, don't assume from the browser page alone.

**If no link arrives,** ask the agent to connect Gmail and she will call `gmail_auth_start`, which returns the same `auth_url` — the manual fallback, and the route to use for any later re-authorization. A missing automatic link usually means the consent DM was never approved or the plugin has no reachable role, so check those first (see the reason-code table in Step 3). Re-running `setup_gmail` when Gmail is already connected is harmless: it reports the connected account and mints nothing.

If `gmail_auth_start` returns a `redirect_uri` that doesn't match what's registered on the OAuth client, Google will show `redirect_uri_mismatch` instead of the consent screen — re-check Step 3.5 above; the value must match `ready.json` exactly, byte-for-byte.

### Rotating credentials

- **Changing `GMAIL_USER_EMAIL`** invalidates the stored credential by design: at startup, the plugin refuses to serve an inbox that doesn't match the configured email, so a stored token for the old address is treated as unusable. Re-run the authorization flow (Step 4) after changing this value.
- **Rotating the OAuth client** (new Client ID/Secret) requires exactly one re-authorization — update `GMAIL_CLIENT_ID` / `GMAIL_CLIENT_SECRET` in casa's plugin env, then run `gmail_auth_start` again.

## Env vars

| Variable | Required | Description |
|---|---|---|
| `GMAIL_CLIENT_ID` | Yes | OAuth 2.0 client ID (Web application type) |
| `GMAIL_CLIENT_SECRET` | Yes | OAuth 2.0 client secret |
| `GMAIL_USER_EMAIL` | Yes | the user's Gmail address |
| `CLAUDE_PLUGIN_DATA` | Provided by Casa | Plugin-writable data directory (token + attachments) |

## Troubleshooting

**`Gmail is not authenticated. Call gmail_auth_start …`** (tool error)
→ No credential is in service. Ask the agent to connect Gmail, which calls `gmail_auth_start`; follow the link and confirm the outcome she reports (see Setup, Step 4).

**`Gmail plugin: stored token is dead — re-auth needed (…)`** (server log)
→ The refresh token was revoked or rejected as `invalid_grant`, and the stored credential has been removed. Run `gmail_auth_start` again.

**The connection keeps dying about once a week** (the message above, every 7 days)
→ Not a plugin fault: the OAuth app's user type is **External** and its publishing status is still **Testing**, so Google expires every refresh token it issues after 7 days. Publish the app — **Google Auth Platform → Audience → Publish app** — and re-authorize once. See "External apps: publish, or the connection expires every 7 days" under Setup, Step 2.

**`Gmail plugin: the stored credential authorizes '…' but GMAIL_USER_EMAIL is '…'`** (server log)
→ The stored credential is for a different inbox. The token file is kept, not deleted; either restore the old `GMAIL_USER_EMAIL` or run `gmail_auth_start` again for the new one.

**`Gmail plugin: could not refresh right now (…); token kept`** (server log)
→ A transient failure — network, or a Google 5xx / `temporarily_unavailable`. The token is deliberately **retained**; nothing needs re-authorizing. The next startup or tool call retries.

**`No authorization result was waiting …`** (from `gmail_auth_collect`)
→ The pass found nothing to collect. This is not a success: a stale or already-handled link produces it. If you were expecting a result, run `gmail_auth_start` and follow the fresh link.

**An authorization result never seems to arrive**
→ Check the reason-code table above. The most common cause is `callback_no_target` (plugin has no reachable assigned role) or `callback_pending_ack` (the consent DM hasn't been approved) — both leave the flow silently stuck rather than producing a visible error.

**`redirect_uri_mismatch` from Google**
→ The URI registered on the OAuth client doesn't match casa's authoritative value. Take the `redirect_uri` that `gmail_auth_start` returns (or use the discovery command in Step 3.5) and register that exact string — do not derive or guess it.

**`Authorization was not granted (access_denied). Nothing has changed.`** (from `gmail_auth_collect`)
→ The consent screen was declined. Ask the agent to run `gmail_auth_start` again for a fresh link.

**`That authorization was granted by <address>, but this plugin is configured for <address>`** (from `gmail_auth_collect`)
→ The wrong Google account was used. The existing stored connection (if any) is left untouched. Run `gmail_auth_start` again and sign in as the correct account.

## No workstation fallback

Earlier versions shipped `bootstrap/get_credentials.py` for completing OAuth outside chat. It has been removed: it requested the redirect URIs `urn:ietf:wg:oauth:2.0:oob` and `http://localhost`, neither of which a **Web application** OAuth client accepts (see Prerequisites above). The in-chat flow (Setup, Step 4) is the only supported path.

## Dismantling the old service-account / DWD setup

The previous auth approach (v0.2.x) used ADC + a service account with domain-wide delegation. That infrastructure can now be removed:

- **Service account:** delete `casa-gmail-agent` (or whichever SA was used) in GCP IAM
- **DWD entry:** remove the entry from Workspace Admin → Security → API Controls → Domain-wide delegation
- **IAM binding:** remove the `roles/iam.serviceAccountTokenCreator` binding that was granted to your user account
- **gcloud ADC:** if you ran `gcloud auth application-default login` solely for this plugin, you can revoke it: `gcloud auth application-default revoke`

## Tools

| Tool | Description |
|---|---|
| `setup_gmail` | Casa's declared setup tool (`casa.setupTool`), auto-dispatched once the consent DM is approved. Argument-free and idempotent: mints an authorization link when not connected, or reports `already_connected` and mints nothing when it is. Not a protected tool — casa dispatches it unprompted, so an approval prompt would deadlock the setup episode |
| `gmail_auth_start` | Begin OAuth: returns an authorization URL to open in a browser, the redirect URI in use, and instructions |
| `gmail_auth_collect` | Collect a pending authorization result delivered by casa's callback facility: returns `{status, messages, promoted}`. Not a protected tool — deliberately callable without the user's tap-approval, since it only checks for and consumes a result the browser step already produced; call it whenever a turn says a result is waiting, and it's safe to call repeatedly |
| `search_emails` | Search inbox with Gmail query syntax |
| `get_email` | Read full email content |
| `get_thread` | Read full email thread |
| `manage_email` | Archive, trash, label, mark read/unread |
| `list_attachments` | List attachments on an email |
| `download_attachment` | Download attachment to 7-day cache |
| `save_attachment` | Permanently save a cached attachment |
| `list_send_as` | List available SendAs aliases (includes verification status) |
| `send_email` ⚠️ | Send new email (optional `from_address` for SendAs alias; requires the user approval) |
| `reply_to_thread` ⚠️ | Reply to thread (optional `from_address` for SendAs alias; requires the user approval) |

⚠️ Protected tools — require tap-approval from the user before execution.

> **SendAs note:** Only aliases with `verification_status: accepted` can be used as `from_address`. Unverified aliases will be rejected by Gmail.
