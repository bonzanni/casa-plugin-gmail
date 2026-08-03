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
3. **Install the plugin, then set `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_USER_EMAIL`** in casa's plugin environment configuration for this plugin (not in a secret manager directly — casa is what passes these through to the plugin's server process). `GMAIL_USER_EMAIL` is the user's Gmail address (e.g. `user@workspace.example.com`). Without all three the plugin's server **exits at startup**, so none of its tools can answer — including the one that reports the redirect URI, and the setup tool casa dispatches in Step 3.4.
4. **Approve casa's callback-consent DM — but only once Step 3.3 is done and the plugin's MCP server is healthy.** Casa asks for explicit consent before it will spool authorization results for this plugin; until approved, the callback route stays closed.

   **Order matters here more than anywhere else in this document.** Approving is what makes casa dispatch the setup tool: the plugin declares `setup_gmail`, and casa hands it to the agent automatically the moment consent settles. Casa dispatches it **once** — it treats an accepted agent turn as dispatched and does not correlate whether the tool actually ran, so there is **no automatic retry**. If the three environment variables are still missing, the server exits at startup, the dispatched call fails, and the setup episode is spent: the promised automatic link never arrives. That is not hypothetical — it is exactly how a live install failed, with consent approved about a minute before the variables were wired. Recovery is manual (ask the agent to connect Gmail — see Step 4) and entirely avoidable by configuring the environment first.

   Approving also publishes casa's redirect URI, which is what makes Step 3.5 possible. the agent will post an `auth_url` in chat within a minute or two — **finish Step 3.5 before opening it**, or Google will answer `redirect_uri_mismatch`. The link stays valid for 30 minutes, which is ample.
5. **Register casa's authoritative redirect URI** — the *exact* string — in the OAuth client's **Authorized redirect URIs** (the field you left empty in Step 2). This step comes after consent because it has to: casa publishes a plugin's redirect URI only once its callback is *routed*, and an unapproved callback is never routed — before Step 3.4 the value does not exist to be read. Never construct it yourself from the plugin's name either: a scoped install has a different effective name than the plugin's base name, and Google matches the redirect URI byte-for-byte. Read it from one of these instead:
   - **Preferred:** ask the agent to connect Gmail (she calls `gmail_auth_start`) — the tool returns a `redirect_uri` field. The plugin reads that straight out of casa's callback index, so it is the same value casa will actually use. Requires the three variables from Step 3.3 to be set and the plugin's server to be running; the `auth_url` it also returns will report `redirect_uri_mismatch` until you finish this step, which is expected. Note that `gmail_auth_start` answers a direct request and therefore **always mints a fresh link**: if approving consent has already put a link in chat, asking for another leaves two live authorizations. Either use the message already posted (it carries the same `redirect_uri`) or use the host-side command below, and treat the newest link as the one to open.
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
| `callback_pending_ack` | Consent DM not approved (Step 3.4) | Authorization result cannot be delivered until you approve the DM |
| `callback_invalid` | Malformed or stale callback state | Retry `gmail_auth_start` for a fresh authorization attempt |
| `callback_spool_error` | Casa-side failure writing the spooled result | Transient; retry `gmail_auth_start` |

### 4. Run the authorization flow

1. **Wait for the link — you should not have to ask for it.** Once the consent DM in Step 3.4 is approved, casa dispatches `setup_gmail` automatically and the agent posts the `auth_url` in chat without being asked. This is the normal path; the plugin declares the tool for exactly this purpose.
2. **Open the link in a real browser — do not tap it inside the chat client.** Tapping a link in a chat app usually opens it in that app's built-in (embedded) browser, and **Google refuses to run OAuth sign-in in an embedded browser**. When that happens Google shows a generic "Something went wrong" page *after* you sign in, never reaches the consent screen, and never redirects — so casa sees nothing at all and the flow just stops. Long-press the link and choose *Open in Chrome* / *Open in Safari* (or copy it into a browser). This is Google's policy and the plugin cannot see which browser is used, so it cannot detect or work around it — opening the link correctly is the only fix.
3. Sign in as the user and grant access. The browser then shows "Response received" — nothing more happens there, and nothing needs to be copied back.
4. Casa delivers the result to the agent, which calls `gmail_auth_collect` and reports the outcome in chat. Success, denial, and a stale/replayed link all show the same neutral browser page, so **chat is the only place you learn whether it actually worked** — read what the agent reports, don't assume from the browser page alone.

**If no link arrives within about two minutes of approving the consent DM,** ask the agent to connect Gmail and she will call `gmail_auth_start`, which returns the same `auth_url` — the manual fallback, and the route to use for any later re-authorization. Casa dispatches the setup tool only once, so waiting longer does not help; nothing will retry on its own. Three causes account for nearly all missing links:

- **The plugin's MCP server is not running or not healthy** — most often the three environment variables (Step 3.3) were still missing when the consent DM was approved, so the server exited at startup and the dispatched call failed. Check the plugin's health in casa and its server log.
- **The consent DM was never approved** (`callback_pending_ack`).
- **The plugin has no reachable assigned role** (`callback_no_target`) — in which case no consent DM is ever sent either.

See the reason-code table in Step 3. Re-running `setup_gmail` will not leave you with two live links: if Gmail is already connected it reports the account and mints nothing, and if a link it minted is still outstanding it says so rather than issuing a second one. It checks casa's spool for that — both the `pending/` entry a mint publishes immediately and the `attempts/` record casa materializes a few minutes later — so a link minted seconds ago already counts. `gmail_auth_start` is different by design: it answers a direct request and always mints a fresh link, so ask for one only when you actually need a new one.

If `gmail_auth_start` returns a `redirect_uri` that doesn't match what's registered on the OAuth client, Google will show `redirect_uri_mismatch` instead of the consent screen — re-check Step 3.5 above; the value must match `ready.json` exactly, byte-for-byte.

### Rotating credentials

- **Changing `GMAIL_USER_EMAIL`** invalidates the stored credential by design: at startup, the plugin refuses to serve an inbox that doesn't match the configured email, so a stored token for the old address is treated as unusable. Re-run the authorization flow (Step 4) after changing this value.
- **Rotating the OAuth client** (new Client ID/Secret) requires exactly one re-authorization — update `GMAIL_CLIENT_ID` / `GMAIL_CLIENT_SECRET` in casa's plugin env, then run `gmail_auth_start` again. Rotating only the **secret** of the same client does not: a refresh token is tied to the client ID, so once the new secret is in place the existing connection resumes untouched. Between the rotation and the update the plugin reports a configuration problem rather than a revoked connection, and keeps the credential — see Troubleshooting.

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

**`Gmail plugin: the OAuth client configuration was rejected (…); token kept`** (server log, or `status: "configuration_error"` from `setup_gmail`)
→ Google refused the plugin's OAuth **client** credentials — typically `invalid_client` after `GMAIL_CLIENT_SECRET` was rotated, mistyped, or the client was deleted. This is **not** a revoked connection: the stored credential is deliberately kept, and no authorization link is offered, because a new one would fail at the same step. Check `GMAIL_CLIENT_ID` / `GMAIL_CLIENT_SECRET` in casa's plugin environment against the Google OAuth client — but re-running `setup_gmail` in the *same* session will keep reporting `configuration_error` no matter how many times you call it: the running MCP server read those variables once at startup and casa's env reload does not restart it, so it still probes with the old, stale values. The MCP server is spawned fresh per session, so simply starting a **new session** picks up the corrected values with no restart needed; a full plugin restart also works but is heavier than required. Only after that, re-run `setup_gmail` — it re-checks the stored credential and, if the fix worked, brings it straight back into service without a re-authorization (the credential is tied to the client **ID**, so rotating only the secret costs nothing).

**`Gmail plugin: could not refresh right now (…); token kept`** (server log)
→ A transient failure — network, or a Google 5xx / `temporarily_unavailable`. The token is deliberately **retained**; nothing needs re-authorizing. The next startup or tool call retries.

**`No authorization result was waiting …`** (from `gmail_auth_collect`)
→ The pass found nothing to collect. This is not a success: a stale or already-handled link produces it. If you were expecting a result, run `gmail_auth_start` and follow the fresh link.

**An authorization result never seems to arrive**
→ Check the reason-code table above. The most common cause is `callback_no_target` (plugin has no reachable assigned role) or `callback_pending_ack` (the consent DM hasn't been approved) — both leave the flow silently stuck rather than producing a visible error.

**Google shows "Something went wrong" after sign-in, and the consent screen never appears**
→ The link was almost certainly opened in an app's embedded browser — tapping a link inside a chat client typically opens it there — and **Google refuses to run OAuth sign-in in an embedded browser**. Nothing is redirected back, so casa never receives a request and the flow stops silently. Retrying the tap will fail the same way every time: reopen the *same* link in a real browser instead (long-press → *Open in Chrome* / *Open in Safari*, or copy the URL into one). The plugin cannot see which browser is in use, so it cannot detect this — it is a Google policy, not a plugin fault. See Setup, Step 4.

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
| `setup_gmail` | Casa's declared setup tool (`casa.setupTool`), auto-dispatched once the consent DM is approved. Argument-free and idempotent: it decides from the stored credential, not from whether startup happened to succeed, so a restart does not change its answer. It mints an authorization link only when one is actually needed — `reauthorization_needed` when the stored credential is genuinely revoked, or a plain link when there is none — and otherwise reports `already_connected` (verified live, not assumed, and put back into service if a restart left it inactive), `already_pending` (a valid link is already out — it will not issue a second), `configuration_error` (Google rejected the OAuth **client**; the credential is kept and no link is minted, because a new one would fail identically), `retry_later` (the connection could not be checked just now; nothing changed), or `unavailable` (setup could not proceed at all — a closed callback route, or a credential that is valid but could not be brought into service; nothing was authorized). Not a protected tool — casa dispatches it unprompted, so an approval prompt would deadlock the setup episode |
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
