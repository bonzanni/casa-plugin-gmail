# casa-plugin-gmail

Gives the agent (Casa resident assistant) full Gmail access on the user's behalf via Google Workspace service account with domain-wide delegation.

## Prerequisites

- Google Workspace account with admin access
- Google Cloud project (existing or new)
- 1Password for secret storage

## Setup

### 1. Create GCP Project and Enable Gmail API

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and create or select a project
2. Navigate to **APIs & Services → Library**
3. Search for "Gmail API" and click **Enable**

### 2. Create a Service Account

1. Navigate to **IAM & Admin → Service Accounts**
2. Click **Create Service Account**
3. Name it (e.g. `casa-gmail-agent`) and click **Create and Continue**
4. Skip optional role grants — click **Done**
5. Click on the new service account → **Keys** tab → **Add Key → Create new key → JSON**
6. Download the JSON file — keep it safe

### 3. Enable Domain-Wide Delegation

1. On the service account page, click **Edit** (pencil icon)
2. Expand **Advanced Settings** → check **Enable Google Workspace Domain-wide Delegation**
3. Save
4. Note the **Client ID** shown on the service account list

### 4. Authorise Scopes in Workspace Admin

1. Go to [admin.google.com](https://admin.google.com)
2. Navigate to **Security → API Controls → Domain-wide delegation**
3. Click **Add new** and enter:
   - **Client ID:** (from step 3)
   - **OAuth scopes:**
     ```
     https://www.googleapis.com/auth/gmail.modify,https://www.googleapis.com/auth/gmail.send
     ```
4. Click **Authorise**

### 5. Store in 1Password

1. Create a new **Document** item in 1Password
2. Paste the entire service account JSON file content as the document body
3. Note the 1Password reference path (e.g. `op://Personal/Gmail Service Account/notesPlain`)

### 6. Configure Plugin via Casa

When the Casa configurator installs this plugin, it will prompt for:

- `GMAIL_SERVICE_ACCOUNT_JSON` → point to your 1Password JSON reference
- `GMAIL_USER_EMAIL` → the user's Gmail address (e.g. `user@workspace.example.com`)

## Key Rotation

Rotate the service account key **quarterly**:

1. In GCP → Service Accounts → Keys → **Add Key** (create new JSON)
2. Store new key in 1Password
3. Update the 1Password reference in Casa plugin config
4. Delete the old key from GCP

If a key is compromised: revoke it immediately in GCP Console, create a new key, and update Casa config.

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
| `send_email` ⚠️ | Send new email (requires the user approval) |
| `reply_to_thread` ⚠️ | Reply to thread (requires the user approval) |

⚠️ Protected tools — require tap-approval from the user before execution.
