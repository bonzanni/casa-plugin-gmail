# casa-plugin-gmail

Gives the agent (Casa resident assistant) full Gmail access on the user's behalf via per-user OAuth 2.0. The consent flow is completable entirely from chat — no workstation bootstrap step required. The refresh token is persisted inside the plugin's data directory (`CLAUDE_PLUGIN_DATA`). No service account, no domain-wide delegation, no gcloud, no ADC.

## Prerequisites

- Google Cloud project with Gmail API enabled
- An Internal OAuth 2.0 client (Desktop app type) created in that project

## Setup

### 1. Enable Gmail API

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and select your project
2. Navigate to **APIs & Services → Library**
3. Search for "Gmail API" and click **Enable**

### 2. Create an Internal OAuth 2.0 Client

1. Navigate to **APIs & Services → Credentials**
2. Click **Create Credentials → OAuth client ID**
3. Application type: **Desktop app** — name it e.g. `casa-gmail`
4. Click **Create**
5. Note the **Client ID** and **Client Secret** shown in the dialog

> If prompted to configure an OAuth consent screen first, set the User Type to **Internal** (Google Workspace only), fill in the app name, and add the three Gmail scopes listed below. Internal apps do not require Google verification.

**Scopes needed** (add these on the consent screen):
```
https://www.googleapis.com/auth/gmail.modify
https://www.googleapis.com/auth/gmail.send
https://www.googleapis.com/auth/gmail.settings.basic
```

### 3. Configure Plugin via Casa

When the Casa configurator installs this plugin, it will prompt for:

- `GMAIL_CLIENT_ID` — the OAuth client ID from step 2
- `GMAIL_CLIENT_SECRET` — the OAuth client secret from step 2
- `GMAIL_USER_EMAIL` — the user's Gmail address (e.g. `user@workspace.example.com`)

### 4. Complete OAuth from Chat

After the plugin is installed, run the consent flow directly in chat:

1. Call `gmail_auth_start` — it returns an authorization URL
2. Open the URL in any browser and sign in as the user
3. After granting access, the browser redirects to `http://localhost:8080` (which won't load — that is expected)
4. Copy the **full URL** from the browser's address bar (it contains `?code=...`)
5. Call `gmail_auth_complete` with that URL — the plugin exchanges the code, persists the refresh token, and all Gmail tools become available

The refresh token is stored at `${CLAUDE_PLUGIN_DATA}/oauth_token.json`. If the token is ever revoked, repeat step 1–5 from chat; no Casa config change is needed.

## Env vars

| Variable | Required | Description |
|---|---|---|
| `GMAIL_CLIENT_ID` | Yes | OAuth 2.0 client ID |
| `GMAIL_CLIENT_SECRET` | Yes | OAuth 2.0 client secret |
| `GMAIL_USER_EMAIL` | Yes | the user's Gmail address |
| `CLAUDE_PLUGIN_DATA` | Provided by Casa | Plugin-writable data directory (token + attachments) |

## Troubleshooting

**`Gmail not authenticated`**
→ Call `gmail_auth_start` and complete the consent flow from chat (steps 4.1–4.5 above).

**`stored token invalid or expired — re-auth needed`**
→ The refresh token was revoked. Repeat the chat-driven consent flow — `gmail_auth_start` and then `gmail_auth_complete`.

**`No refresh_token in response` from `gmail_auth_complete`**
→ A previous consent grant may still be active. Go to [myaccount.google.com/permissions](https://myaccount.google.com/permissions), revoke access for the app, then call `gmail_auth_start` again — the auth URL includes `prompt=consent` to force a fresh grant.

**`OAuth error: access_denied`**
→ The user declined the consent screen. Call `gmail_auth_start` to get a fresh URL and try again.

## Fallback: workstation bootstrap

`bootstrap/get_credentials.py` remains available as a documented fallback for cases where the chat-driven flow cannot be completed (e.g., testing in a local dev environment). It requires `pip install google-auth-oauthlib` and prints the refresh token to stdout. **Do not use this for production setup** — the chat-driven flow is the supported path and keeps the token out of env vars.

## Dismantling the old service-account / DWD setup

The previous auth approach (v0.2.x) used ADC + a service account with domain-wide delegation. That infrastructure can now be removed:

- **Service account:** delete `casa-gmail-agent` (or whichever SA was used) in GCP IAM
- **DWD entry:** remove the entry from Workspace Admin → Security → API Controls → Domain-wide delegation
- **IAM binding:** remove the `roles/iam.serviceAccountTokenCreator` binding that was granted to your user account
- **gcloud ADC:** if you ran `gcloud auth application-default login` solely for this plugin, you can revoke it: `gcloud auth application-default revoke`

## Tools

| Tool | Description |
|---|---|
| `gmail_auth_start` | Begin OAuth: returns authorization URL to open in a browser |
| `gmail_auth_complete` ⚠️ | Complete OAuth: exchange redirect URL → persists refresh token |
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
