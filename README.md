# casa-plugin-gmail

Gives the agent (Casa resident assistant) full Gmail access on the user's behalf via Google Workspace service account with domain-wide delegation, using keyless ADC → service account impersonation (no JSON key file required).

## Prerequisites

- Google Workspace account with admin access
- Google Cloud project with Gmail API enabled
- `gcloud` CLI installed and authenticated

## Setup

### 1. Enable Gmail API

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and select your project
2. Navigate to **APIs & Services → Library**
3. Search for "Gmail API" and click **Enable**

### 2. Enable IAM Credentials API

```bash
gcloud services enable iamcredentials.googleapis.com
```

### 3. Create a Service Account (if not already exists)

1. Navigate to **IAM & Admin → Service Accounts**
2. Click **Create Service Account** — name it e.g. `casa-gmail-agent`
3. Skip optional role grants — click **Done**

### 4. Enable Domain-Wide Delegation on the Service Account

1. Click on the service account → **Edit**
2. Check **Enable Google Workspace Domain-wide Delegation** → Save
3. Note the **Client ID** shown on the service account list

### 5. Authorise Scopes in Google Workspace Admin

1. Go to [admin.google.com](https://admin.google.com)
2. Navigate to **Security → API Controls → Domain-wide delegation**
3. Click **Add new** and enter:
   - **Client ID:** (from step 4)
   - **OAuth scopes** (copy exactly — comma-separated, no spaces):
     ```
     https://www.googleapis.com/auth/gmail.modify,https://www.googleapis.com/auth/gmail.send,https://www.googleapis.com/auth/gmail.settings.basic
     ```
4. Click **Authorise**

### 6. Grant Your Account Permission to Impersonate the Service Account

```bash
gcloud iam service-accounts add-iam-policy-binding \
  casa-gmail-agent@YOUR_PROJECT.iam.gserviceaccount.com \
  --member="user:$(gcloud config get-value account)" \
  --role="roles/iam.serviceAccountTokenCreator"
```

### 7. Authenticate with Application Default Credentials

```bash
gcloud auth application-default login
```

> Note: Use the default login (which includes `cloud-platform` scope). Narrowing scopes with `--scopes` may prevent impersonation.

### 8. Configure Plugin via Casa

When the Casa configurator installs this plugin, it will prompt for:

- `GMAIL_IMPERSONATION_SA` — the service account email to impersonate (e.g. `casa-gmail-agent@YOUR_PROJECT.iam.gserviceaccount.com`)
- `GMAIL_SUBJECT_EMAIL` — the user's Workspace Gmail address (e.g. `user@workspace.example.com`)

## Troubleshooting

**`No Application Default Credentials found`**
→ Run: `gcloud auth application-default login`

**`Failed to impersonate ... RefreshError`**

1. Enable the IAM Credentials API: `gcloud services enable iamcredentials.googleapis.com`
2. Grant token creator role: `gcloud iam service-accounts add-iam-policy-binding GMAIL_IMPERSONATION_SA --member=user:$(gcloud config get-value account) --role=roles/iam.serviceAccountTokenCreator`
3. Ensure DWD is enabled on the service account in GCP Console
4. Ensure all three scopes are authorised in Workspace Admin (gmail.modify, gmail.send, gmail.settings.basic)

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
