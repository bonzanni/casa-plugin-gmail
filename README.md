# casa-plugin-gmail

Gives the agent (Casa resident assistant) full Gmail access on the user's behalf via per-user OAuth 2.0. Authorization runs through casa's authorization-callback facility: the user opens a link, grants access in the browser, and casa delivers the result back to the agent automatically — nothing is copied and pasted between browser and chat. The refresh token is persisted inside the plugin's data directory (`CLAUDE_PLUGIN_DATA`). No service account, no domain-wide delegation, no gcloud, no ADC.

## Prerequisites

- Google Cloud project with Gmail API enabled
- An Internal OAuth 2.0 client of type **Web application** — not Desktop app (see below)
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

> If prompted to configure an OAuth consent screen first, set the User Type to **Internal** (Google Workspace only), fill in the app name, and add the three Gmail scopes listed below. Internal apps do not require Google verification.

**Scopes needed** (add these on the consent screen):
```
https://www.googleapis.com/auth/gmail.modify
https://www.googleapis.com/auth/gmail.send
https://www.googleapis.com/auth/gmail.settings.basic
```

### 3. Configure the casa deployment

This is where most setup failures happen — each prerequisite below fails silently or with a generic-looking error if skipped. Do these in order.

1. **Set casa's `public_url`** to a clean `https://` origin: scheme + host only — no path, no trailing slash, no IP literal, no userinfo (e.g. `https://<your-casa-public-url>`, not `https://<your-casa-public-url>/`, not `https://1.2.3.4`, not `https://user@<your-casa-public-url>`). Casa needs this to construct a real, reachable redirect URI for the plugin.
2. **Give the plugin a reachable assigned role** *before* installing it. Casa uses this to know where to deliver the callback result.
3. **Install the plugin**, then **approve casa's callback-consent DM**. Casa asks for explicit consent before it will spool authorization results for this plugin; until approved, the callback route stays closed.
4. **Read the authoritative redirect URI** from `/data/callbacks/<plugin-name>/ready.json` and register that *exact* string in the OAuth client's **Authorized redirect URIs** (the field you left empty in Step 2). Never construct this value yourself from the plugin's name — a scoped install has a different effective name than the plugin's base name, and Google matches the redirect URI byte-for-byte. Only the value in `ready.json` is authoritative. *Optional, rollback only:* if you are upgrading from v0.4.x, also keep (or add) `http://localhost:8080` as a second entry in **Authorized redirect URIs** — v0.4.1 used that loopback flow, so leaving it registered means a downgrade needs no Google console round-trip. It is unused by v0.5.0 and can be removed once the callback flow is confirmed working.
5. **Set `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_USER_EMAIL`** in casa's plugin environment configuration for this plugin (not in a secret manager directly — casa is what passes these through to the plugin's server process). `GMAIL_USER_EMAIL` is the user's Gmail address (e.g. `user@workspace.example.com`).

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

1. In chat, ask the agent to connect Gmail — she calls `gmail_auth_start`, which returns an `auth_url`.
2. Open the link and sign in as the user. After granting access, the browser shows "Response received" — nothing more happens there, and nothing needs to be copied back.
3. Casa delivers the result to the agent, which calls `gmail_auth_collect` and reports the outcome in chat. Success, denial, and a stale/replayed link all show the same neutral browser page, so **chat is the only place you learn whether it actually worked** — read what the agent reports, don't assume from the browser page alone.

If `gmail_auth_start` returns a `redirect_uri` that doesn't match what's registered on the OAuth client, Google will show `redirect_uri_mismatch` instead of the consent screen — re-check Step 3.4 above; the value must match `ready.json` exactly, byte-for-byte.

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

**`Gmail not authenticated`**
→ Ask the agent to connect Gmail, which calls `gmail_auth_start`; follow the link and confirm the outcome she reports (see Setup, Step 4).

**`stored token invalid or expired — re-auth needed`**
→ The refresh token was revoked or the stored credential no longer matches `GMAIL_USER_EMAIL`. Run `gmail_auth_start` again.

**An authorization result never seems to arrive**
→ Check the reason-code table above. The most common cause is `callback_no_target` (plugin has no reachable assigned role) or `callback_pending_ack` (the consent DM hasn't been approved) — both leave the flow silently stuck rather than producing a visible error.

**`redirect_uri_mismatch` from Google**
→ The URI registered on the OAuth client doesn't match casa's authoritative value. Re-read `/data/callbacks/<plugin-name>/ready.json` and register that exact string — do not derive or guess it.

**`OAuth error: access_denied`**
→ The user declined the consent screen. `gmail_auth_collect` reports this as a failure — ask the agent to run `gmail_auth_start` again for a fresh link.

**Wrong Google account was authorized**
→ Reported as a failure; the existing stored connection (if any) is left untouched. Run `gmail_auth_start` again and sign in as the correct account.

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
