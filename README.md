# casa-plugin-gmail

Gives the agent (Casa resident assistant) full Gmail access on the user's behalf via per-user OAuth 2.0 with a refresh token stored as a plugin env var. No service account, no domain-wide delegation, no gcloud, no ADC.

## Prerequisites

- Google Cloud project with Gmail API enabled
- An Internal OAuth 2.0 client (Desktop app type) created in that project
- Python 3 on your workstation (for the one-time bootstrap step)

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

### 3. Run the Bootstrap Script (once, on your workstation)

Install the one dependency and run the script:

```bash
pip install google-auth-oauthlib
python bootstrap/get_credentials.py
```

The script will prompt for your Client ID and Client Secret (or read them from `GMAIL_CLIENT_ID` / `GMAIL_CLIENT_SECRET` env vars), open a browser for the Google consent screen, and print four values when done:

```
GMAIL_CLIENT_ID=...
GMAIL_CLIENT_SECRET=...
GMAIL_REFRESH_TOKEN=...
GMAIL_USER_EMAIL=...
```

### 4. Configure Plugin via Casa

When the Casa configurator installs this plugin, it will prompt for:

- `GMAIL_CLIENT_ID` — the OAuth client ID from step 2
- `GMAIL_CLIENT_SECRET` — the OAuth client secret from step 2
- `GMAIL_REFRESH_TOKEN` — the refresh token printed by the bootstrap script
- `GMAIL_USER_EMAIL` — the user's Gmail address (e.g. `user@workspace.example.com`)

## Dismantling the old service-account / DWD setup

The previous auth approach (v0.2.x) used ADC + a service account with domain-wide delegation. That infrastructure can now be removed:

- **Service account:** delete `casa-gmail-agent` (or whichever SA was used) in GCP IAM
- **DWD entry:** remove the entry from Workspace Admin → Security → API Controls → Domain-wide delegation
- **IAM binding:** remove the `roles/iam.serviceAccountTokenCreator` binding that was granted to your user account
- **gcloud ADC:** if you ran `gcloud auth application-default login` solely for this plugin, you can revoke it: `gcloud auth application-default revoke`

## Troubleshooting

**`missing env var(s): GMAIL_REFRESH_TOKEN`**
→ Re-run `bootstrap/get_credentials.py` and store the printed values in Casa.

**`failed to refresh OAuth token`**
→ The refresh token may have been revoked. Go to [myaccount.google.com/permissions](https://myaccount.google.com/permissions), revoke access for your app, then re-run the bootstrap script.

**`no refresh token returned` during bootstrap**
→ A previous consent already exists. Revoke app access at [myaccount.google.com/permissions](https://myaccount.google.com/permissions) and run the script again — it passes `prompt=consent` to force a fresh grant.

## Tools

| Tool | Description |
|---|---|
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
