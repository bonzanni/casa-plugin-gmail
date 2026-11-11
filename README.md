# casa-plugin-gmail

Gives the agent (casa's resident assistant) full Gmail access on the user's behalf via per-user OAuth 2.0. Authorization runs through casa's authorization-callback facility: the user opens a link, grants access in the browser, and casa delivers the result back to the agent automatically — nothing is copied and pasted between browser and chat. The refresh token is persisted inside the plugin's data directory (`CLAUDE_PLUGIN_DATA`). No service account, no domain-wide delegation, no gcloud, no ADC.

## Prerequisites

- Google Cloud project with Gmail API enabled
- An OAuth 2.0 client of type **Web application** — not Desktop app (see below). The consent screen's **user type** depends on your account, and only one of the two is ever selectable: see Step 2.
- A casa deployment with `public_url` set to a public `https://` origin, and the plugin assigned to an agent that can reach it (Step 3)
- **Casa 0.338.0 or later.** An older Casa either refuses this plugin's manifest or lacks the sign-in link delivery, the two-choice erase at uninstall or the `read` profile described below.
- **An `amd64` Home Assistant host.** `server/vendor/` holds compiled modules for CPython 3.11 on linux x86_64 only, so on `aarch64` the plugin's server cannot start (see License).

### Why the OAuth client must be a Web application client

Casa's callback facility publishes a real, public `https://` redirect URI for this plugin and expects Google to redirect the user's browser straight to it. A **Desktop app** OAuth client only accepts loopback redirect URIs (`http://localhost:...`) or custom URI schemes — it cannot be configured with a public `https://` redirect URI at all, so it cannot work with this flow. The client **must** be created as **Web application** type.

## Setup

Setup spans two systems: the Google Cloud project (OAuth client) and the casa deployment (public URL, plugin install, consent, env vars). Both are required — skipping either leaves authorization stuck.

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

An **External** app whose publishing status is **Testing** is issued refresh tokens that expire after **7 days**. Google's wording: *"A Google Cloud Platform project with an OAuth consent screen configured for an external user type and a publishing status of "Testing" is issued a refresh token expiring in 7 days, unless the only OAuth scopes requested are a subset of name, email address, and user profile"* ([OAuth 2.0 docs](https://developers.google.com/identity/protocols/oauth2), *Refresh token expiration*). Gmail scopes are not in that subset, and adding your own address under **Test users** does not exempt you — the limit applies to test users, including the project owner. In practice the plugin would lose Gmail access roughly weekly, each time needing a fresh authorization through `setup_gmail`, which defeats the point of persisting a refresh token at all.

**So set the publishing status to "In production"** (**Google Auth Platform → Audience → Publish app**). That, and nothing else, is what removes the 7-day expiry. Publishing does **not** require passing Google verification first, and for a personal single-user install you should not attempt verification:

- **What unverified production costs you.** `gmail.modify` and `gmail.settings.basic` are **restricted** scopes and `gmail.send` is **sensitive**, so until the app is verified the consent screen shows *"Google hasn't verified this app"* and you must click **Advanced → Go to … (unsafe)** to continue. The project is also capped at 100 users for its lifetime. Google allows this explicitly for personal use: *"If the app is for your personal use (fewer than 100 users), you and your limited number of users can continue using the app without going through verification"* ([Exceptions to verification requirements](https://support.google.com/cloud/answer/13464323)). Refresh tokens issued this way do not carry the Testing 7-day expiry.
- **What verification would cost you.** Restricted-scope verification is a ~6-week review and requires an annual third-party [CASA security assessment](https://support.google.com/cloud/answer/13465431), arranged and paid for directly with an assessor. That is a real project, not a formality — and it is unnecessary for one inbox.

Refresh tokens can still be invalidated for the ordinary reasons in any publishing status: you revoke access, the token goes six months unused, or (with Gmail scopes) the account password changes.

### 3. Configure the casa deployment

This is where most setup failures happen. You do most of it by asking the assistant in chat; it opens a configurator task that does the installing and reports back.

1. **Set casa's `public_url`** to a clean `https://` origin: scheme + host only — no path, no trailing slash, no IP literal, no userinfo (e.g. `https://<your-casa-public-url>`, not `https://<your-casa-public-url>/`, not `https://1.2.3.4`, not `https://user@<your-casa-public-url>`). Casa needs this to construct a real, reachable redirect URI for the plugin. That origin must also forward `/callback/` to Casa's external API port (`18065/tcp`), which is not published by default: see *Plugin authorization callbacks* in Casa's documentation.
2. **Ask the assistant to install the plugin,** for example: *"Install the Gmail plugin from bonzanni/casa-plugin-gmail for the assistant."* The configurator installs the latest release and gives the plugin its assigned role, which is what tells Casa where to deliver the callback result. Assign it with full access here; the `read` profile (see Tools) cannot run sign-in setup.
3. **Give the plugin `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET` and `GMAIL_USER_EMAIL`.** `GMAIL_USER_EMAIL` is the user's Gmail address (e.g. `user@workspace.example.com`). The easy way: before Step 3.2, create a 1Password item in Casa's default vault with `gmail` in its title and fields labelled `client id`, `client secret` and `email`. The configurator finds it during the install and wires the three variables. It does not ask you for a secret's value in chat: anything it cannot find stays unwired and it tells you which, and you can then point it at the 1Password reference, or tell it you want a literal value set. Until all three resolve, Casa withholds the plugin, so no agent has its tools, and the setup run waits.
4. **Approve casa's callback-consent DM.** It arrives during the install and stays answerable for ten minutes; if you miss it, ask the assistant to send it again. Casa asks for explicit consent before it will spool authorization results for this plugin; until approved, the callback route stays closed.

   Approving also releases the setup run: the plugin declares `setup_gmail`, and casa runs it itself once the consent is approved and the three variables from Step 3.3 resolve, in whichever order those happen. Approving publishes casa's redirect URI as well, which is what makes Step 3.5 possible. Casa will post the sign-in link in your chat with the agent shortly after — **finish Step 3.5 before opening it**, or Google will answer `redirect_uri_mismatch`. The link stays valid for 30 minutes, which is ample.
5. **Register casa's authoritative redirect URI** — the *exact* string — in the OAuth client's **Authorized redirect URIs** (the field you left empty in Step 2). This step comes after consent because it has to: casa publishes a plugin's redirect URI only once its callback is *routed*, and an unapproved callback is never routed — before Step 3.4 the value does not exist to be read. Never construct it yourself from the plugin's name either: a scoped install has a different effective name than the plugin's base name, and Google matches the redirect URI byte-for-byte. Read it from one of these instead:
   - **Preferred:** ask the agent to connect Gmail and tell you the redirect URI (it calls `setup_gmail`) — the tool returns a `redirect_uri` field. Ask for it by name: the automatic run reports in one short line that may leave it out. The plugin reads that value straight out of casa's callback index, so it is the same value casa will actually use. Requires the three variables from Step 3.3 to be set and the plugin's server to be running; the sign-in link it hands to casa will report `redirect_uri_mismatch` until you finish this step, which is expected. `setup_gmail` never mints a second link while one is outstanding: if approving consent has already put a link in chat, asking again reports `already_pending`, repeats no link and still gives you the `redirect_uri`.
   - **From a shell inside Casa's container,** without a running server and without having to know the effective name:
     ```
     grep -o '"redirect_uri":[^,}]*' /data/callbacks/*/ready.json
     ```

#### What breaks if a step above is skipped

Casa closes an authorization-callback route for exactly five reasons. The plugin cannot distinguish between them and reports all five as the same generic error, so use this table to diagnose from context instead:

| Casa reason code | Which step was skipped | How it presents |
|---|---|---|
| `callback_base_url_invalid` | `public_url` (Step 3.1) missing or not a clean `https://` origin | The callback route never becomes usable; the redirect URI in `ready.json` is missing or unusable |
| `callback_no_target` | Plugin has no reachable assigned role (Step 3.2) | The callback stays dark and **no consent DM is ever sent** — indistinguishable from casa simply not having gotten to it yet; if you've waited and there's still no DM, check the plugin's role assignment |
| `callback_pending_ack` | Consent DM not approved (Step 3.4) | Authorization result cannot be delivered until you approve the DM |
| `callback_invalid` | Nothing you skipped: the plugin's `casa.callbacks` declaration could not be read | The route stays closed; update the plugin, and report it if the latest release shows this |
| `callback_spool_error` | Casa-side failure writing the spooled result | Transient; run `setup_gmail` again |

### 4. Run the authorization flow

1. **Wait for the link — you should not have to ask for it.** Once the consent DM in Step 3.4 is approved and the variables resolve, casa dispatches `setup_gmail` automatically, and casa posts the sign-in link in your chat with the agent without being asked — a "Sign in with Google" link naming its real destination, never inside a task topic. The agent never sees or writes the link itself. This is the normal path; the plugin declares the tool for exactly this purpose.
2. **Open the link in a real browser — do not tap it inside the chat client.** Tapping a link in a chat app usually opens it in that app's built-in (embedded) browser, and **Google refuses to run OAuth sign-in in an embedded browser**. When that happens Google shows a generic "Something went wrong" page *after* you sign in, never reaches the consent screen, and never redirects — so casa sees nothing at all and the flow just stops. Long-press the link and choose *Open in Chrome* / *Open in Safari* (or copy it into a browser). This is Google's policy and the plugin cannot see which browser is used, so it cannot detect or work around it — opening the link correctly is the only fix.
3. Sign in as the Gmail account being connected and grant access. The browser then shows "Response received" — nothing more happens there, and nothing needs to be copied back.
4. Casa delivers the result to the agent, which calls `gmail_auth_collect` and reports the outcome in chat. Success, denial, and a stale/replayed link all show the same neutral browser page, so **chat is the only place you learn whether it actually worked** — read what the agent reports, don't assume from the browser page alone.

**If no link arrives within about two minutes of completing Steps 3.3 and 3.4,** ask the assistant why the Gmail plugin is not working: it can read what the plugin is waiting for. Once the plugin's tools are available, ask the agent to connect Gmail and it will call `setup_gmail` itself — the manual fallback, and the route to use for any later re-authorization. If no link was minted this hands a fresh link to casa, which posts it; if one was minted and only the message went missing, it reports `already_pending` instead, and the only remedy is to wait for that link to expire (30 minutes from minting) and ask again. A setup run that never reached the tool is retried a few times, then casa tells you to run it by hand. Three causes account for nearly all missing links:

- **The plugin's MCP server is not running or not healthy** — most often a variable from Step 3.3 is still unwired, so casa is withholding the plugin. Check the plugin's health in casa and its server log.
- **The consent DM was never approved** (`callback_pending_ack`). An expired DM counts as declined until it is sent again.
- **The plugin has no reachable assigned role** (`callback_no_target`) — in which case no consent DM is ever sent either.

See the reason-code table in Step 3. Re-running `setup_gmail` will not leave you with two live links: if Gmail is already connected it reports the account and mints nothing, and if a link it minted is still outstanding it says so rather than issuing a second one. It checks casa's spool for that — both the `pending/` entry a mint publishes immediately and the `attempts/` record casa materializes a few minutes later — so a link minted seconds ago already counts. There is **no force-a-fresh-link path**: `setup_gmail` is the only tool that mints (a non-setup tool cannot hand a link to the operator under casa's result contract), so while a minted link is outstanding it reports `already_pending` and you wait for that link to expire (casa's pending window, 30 minutes from minting) before a new one can be issued.

If `setup_gmail` returns a `redirect_uri` that doesn't match what's registered on the OAuth client, Google will show `redirect_uri_mismatch` instead of the consent screen — re-check Step 3.5 above; the value must match `ready.json` exactly, byte-for-byte.

### Rotating credentials

- **Changing `GMAIL_USER_EMAIL`** invalidates the stored credential by design: when it loads the stored credential, the plugin refuses to serve an inbox that doesn't match the configured email, so a stored token for the old address is treated as unusable. Re-run the authorization flow (Step 4) after changing this value.
- **Rotating the OAuth client** (new Client ID/Secret) requires exactly one re-authorization — update `GMAIL_CLIENT_ID` / `GMAIL_CLIENT_SECRET` in casa's plugin env, start a **new session** so the plugin's server is spawned with the corrected values (it reads them once at startup; a full plugin restart also works), then run `setup_gmail` again. Rotating only the **secret** of the same client does not: a refresh token is tied to the client ID, so once the new secret is in place the existing connection resumes untouched. Between the rotation and the update the plugin reports a configuration problem rather than a revoked connection, and keeps the credential — see Troubleshooting.

### Updating an already-connected install

Updating the plugin on a working system does not disturb the connection, and does not need a re-authorization. The refresh token is kept in `CLAUDE_PLUGIN_DATA`, which is casa's persistent plugin-data directory for a role-assigned plugin — the update replaces the plugin artifact, and the credential is not in it. This plugin also declares a callback and **no triggers**, so an update re-mints no per-trigger secret that could reach the stored grant (a callback declaration carries no secret at all, and its consent ack survives an update that leaves `casa.callbacks` unchanged).

Casa also runs `setup_gmail` after an update. On a healthy install it answers `already_connected` with the connected account and mints nothing, so that line is your confirmation. You can ask the agent to run it at any time; it is argument-free and changes nothing when there is nothing to change.

### Uninstalling

When you uninstall the plugin, casa asks what to do with its data:

- **Keep data** — the plugin is removed and its data directory stays, so a reinstall is still connected.
- **Erase data, keep sign-ins** — `erase_gmail_data` deletes the sent log and saved attachments. The sign-in stays, so a reinstall is still connected.
- **Erase everything** — `erase_gmail` asks Google to revoke the plugin's access, then deletes everything in the data directory. Reconnecting needs a new sign-in.

Casa removes the plugin only when the eraser reports the erasure complete. If Google does not confirm the revocation (it cannot be reached, say), the eraser keeps the stored sign-in so a second run can still revoke it, reports the erasure incomplete, and the plugin stays installed. A stored sign-in the eraser cannot read whole (damaged by an interrupted write, say) is kept for the same reason. You can always remove the access yourself at [myaccount.google.com/permissions](https://myaccount.google.com/permissions), then uninstall choosing **Keep data**. An erase covers what exists when it runs: a send or attachment save already running in another casa session at that moment can still record its result afterwards. Neither eraser touches your mail at Google, attachments already handed to casa's handoff folder (casa expires them within 7 days), or Home Assistant backups taken earlier.

## Env vars

| Variable | Required | Description |
|---|---|---|
| `GMAIL_CLIENT_ID` | Yes | OAuth 2.0 client ID (Web application type) |
| `GMAIL_CLIENT_SECRET` | Yes | OAuth 2.0 client secret |
| `GMAIL_USER_EMAIL` | Yes | the user's Gmail address |
| `CLAUDE_PLUGIN_DATA` | Provided by Casa | Plugin-writable data directory (token + saved attachments) |
| `CASA_HANDOFF_DIR` | Casa (default `/data/handoff`) | Casa's shared file handoff folder: downloads land here, and attachments may come from here |

## Troubleshooting

**`Gmail is not authenticated. Call setup_gmail …`** (tool error)
→ No credential is in service. Ask the agent to connect Gmail, which calls `setup_gmail`; follow the link and confirm the outcome it reports (see Setup, Step 4).

**`Gmail is still starting up: another Gmail process is finishing its sign-in work …`** (tool error)
→ Casa can run more than one Gmail server at once (a session's own, and a short-lived one that lists the tools). The server loads the stored credential when a tool first needs it, under a lock shared with the others, and waits up to 20 seconds for another server to finish with it. Nothing is wrong with the connection; the next call retries. `setup_gmail` answers `retry_later` in the same situation and creates no link.

**`Gmail's stored authorization for … is in place, but I could not confirm it with Google just now`** (tool error)
→ A credential is on disk but could not be refreshed on this call (see the server log for why). Nothing needs re-authorizing; the next call retries. If it persists, `setup_gmail` reports the cause (`retry_later` or `configuration_error`).

**`Gmail plugin: stored token is dead — re-auth needed (…)`** (server log)
→ The refresh token was revoked or rejected as `invalid_grant`, and the stored credential has been removed. Run `setup_gmail` again.

**The connection keeps dying about once a week** (the message above, every 7 days)
→ Not a plugin fault: the OAuth app's user type is **External** and its publishing status is still **Testing**, so Google expires every refresh token it issues after 7 days. Publish the app — **Google Auth Platform → Audience → Publish app** — and re-authorize once. See "External apps: publish, or the connection expires every 7 days" under Setup, Step 2.

**`Gmail plugin: the stored credential authorizes '…' but GMAIL_USER_EMAIL is '…'`** (server log)
→ The stored credential is for a different inbox. The token file is kept, not deleted; either restore the old `GMAIL_USER_EMAIL` (then start a **new session**, or restart the plugin, so the server reads it — the value is read once at startup) or run `setup_gmail` again to authorize the new one.

**`Gmail plugin: the OAuth client configuration was rejected (…); token kept`** (server log, or `status: "configuration_error"` from `setup_gmail`)
→ Google refused the plugin's OAuth **client** credentials — typically `invalid_client` after `GMAIL_CLIENT_SECRET` was rotated, mistyped, or the client was deleted. This is **not** a revoked connection: the stored credential is deliberately kept, and no authorization link is offered, because a new one would fail at the same step. Check `GMAIL_CLIENT_ID` / `GMAIL_CLIENT_SECRET` in casa's plugin environment against the Google OAuth client — but re-running `setup_gmail` in the *same* session will keep reporting `configuration_error` no matter how many times you call it: the running MCP server read those variables once at startup and casa's env reload does not restart it, so it still probes with the old, stale values. The MCP server is spawned fresh per session, so simply starting a **new session** picks up the corrected values with no restart needed; a full plugin restart also works but is heavier than required. Only after that, re-run `setup_gmail` — it re-checks the stored credential and, if the fix worked, brings it straight back into service without a re-authorization (the credential is tied to the client **ID**, so rotating only the secret costs nothing).

**`Gmail plugin: could not refresh right now (…); token kept`** (server log)
→ A transient failure — network, or a Google 5xx / `temporarily_unavailable`. The token is deliberately **retained**; nothing needs re-authorizing. The next tool call that needs Gmail retries.

**`No authorization result was waiting …`** (from `gmail_auth_collect`)
→ The pass found nothing to collect. This is not a success: a stale or already-handled link produces it. If you were expecting a result, open the link you were already given and complete it; if that link is gone, run `setup_gmail` — it reports `already_pending` until the outstanding link expires, and mints a fresh one after that.

**An authorization result never seems to arrive**
→ Check the reason-code table above. The most common cause is `callback_no_target` (plugin has no reachable assigned role) or `callback_pending_ack` (the consent DM hasn't been approved) — both leave the flow silently stuck rather than producing a visible error.

**Google shows "Something went wrong" after sign-in, and the consent screen never appears**
→ The link was almost certainly opened in an app's embedded browser — tapping a link inside a chat client typically opens it there — and **Google refuses to run OAuth sign-in in an embedded browser**. Nothing is redirected back, so casa never receives a request and the flow stops silently. Retrying the tap will fail the same way every time: reopen the *same* link in a real browser instead (long-press → *Open in Chrome* / *Open in Safari*, or copy the URL into one). The plugin cannot see which browser is in use, so it cannot detect this — it is a Google policy, not a plugin fault. See Setup, Step 4.

**`redirect_uri_mismatch` from Google**
→ The URI registered on the OAuth client doesn't match casa's authoritative value. Take the `redirect_uri` that `setup_gmail` returns beside the link (or use the discovery command in Step 3.5) and register that exact string — do not derive or guess it.

**`Authorization was not granted (access_denied). Nothing has changed.`** (from `gmail_auth_collect`)
→ The consent screen was declined. Ask the agent to run `setup_gmail` again for a fresh link.

**`That authorization was granted by <address>, but this plugin is configured for <address>`** (from `gmail_auth_collect`)
→ The wrong Google account was used. The existing stored connection (if any) is left untouched. Run `setup_gmail` again and sign in as the correct account.

## Tools

| Tool | Description |
|---|---|
| `setup_gmail` | Casa's declared setup tool (`casa.setupTool`), auto-dispatched once the consent DM is approved. When it makes a link it hands it to casa, which posts it in your chat; its result carries only casa's reference in `auth_url`, and `auth_url` is `null` whenever no link was made. Argument-free and idempotent: it decides from the stored credential, not from whether startup happened to succeed, so a restart does not change its answer. It mints an authorization link only when one is actually needed — `reauthorization_needed` when the stored credential is genuinely revoked, or a plain link when there is none — and otherwise reports `link_not_handed_over` (a link was made but casa did not accept it; it stays outstanding until it expires), `already_connected` (verified live, not assumed, and put back into service if a restart left it inactive), `already_pending` (a valid link is already out — it will not issue a second), `configuration_error` (Google rejected the OAuth **client**; the credential is kept and no link is minted, because a new one would fail identically), `retry_later` (the connection could not be checked just now, a completed authorization is waiting to be confirmed, or another Gmail server is finishing its sign-in work; nothing changed), or `unavailable` (setup could not proceed at all — a closed callback route, or a credential that is valid but could not be brought into service; nothing was authorized). Not a protected tool — casa dispatches it unprompted, so an approval prompt would deadlock the setup episode |
| `gmail_auth_collect` | Collect a pending authorization result delivered by casa's callback facility: returns `{status, messages, promoted}`. Not a protected tool — deliberately callable without the user's tap-approval, since it only checks for and consumes a result the browser step already produced; call it whenever a turn says a result is waiting, and it's safe to call repeatedly |
| `search_emails` | Search inbox with Gmail query syntax |
| `get_email` | Read full email content |
| `get_thread` | Read full email thread |
| `manage_email` | Archive, trash, label, mark read/unread |
| `list_attachments` | List attachments on an email |
| `download_attachment` | Download an attachment into Casa's handoff folder (kept 7 days), where other plugins can take it |
| `save_attachment` | Permanently save a downloaded attachment under `saved/` |
| `list_send_as` | List available SendAs aliases (includes verification status) |
| `send_email` ⚠️ | Send new email (optional `from_address` for SendAs alias; requires the user's approval) |
| `reply_to_thread` ⚠️ | Reply to thread (optional `from_address` for SendAs alias; requires the user's approval) |
| `erase_gmail` ⚠️ | Casa's `casa.eraseTool`: revokes Gmail's access at Google, then deletes everything in the plugin's data directory. Run by casa at uninstall when you choose **Erase everything** |
| `erase_gmail_data` ⚠️ | Casa's `casa.eraseDataOnlyTool`: deletes the sent log and saved attachments but keeps the sign-in. Run by casa at uninstall when you choose **Erase data, keep sign-ins** |

Every tool except `setup_gmail` is declared `{"result": "safe"}` in `casa.resultContract`:
Casa refuses, before it runs, any tool missing from that declaration, so a new tool must be
added there too. `setup_gmail` is declared a `capability` that provides `auth_url` and
`delivers` it as an `operator_link`: it deposits the sign-in link with casa's broker, and casa
posts it in the chat you asked from and tells the agent whether it arrived. So the link never
passes through the agent, and never lands in a task topic.

The manifest declares one access profile, `read`: `search_emails`,
`get_email`, `get_thread`, `list_attachments`, `download_attachment` and `list_send_as`.
An agent assigned Gmail with that profile can search and read mail and download
attachments, and Casa denies it the other eight tools: sending, replying, archiving, trashing, marking
or labelling mail, saving attachments, the erasers, and sign-in setup. An assignment without a
profile gets every tool, as before. Casa sets a profile only on a new assignment, so
narrowing an existing one means unassigning and assigning again with `profile="read"`.
`casa.provides_tools` lists all fourteen tools by their full names. Casa requires the list
once a profile is declared and checks that it names every tool the profile lists, but it
does not check that the list is complete. The completeness matters, though: the tools Casa
hides from a read-profiled agent are the ones in this list and not in the profile, and the
plugin's own tests pin all fourteen.

⚠️ Protected tools — require tap-approval from the user before execution.

> **SendAs note:** Only aliases with `verification_status: accepted` can be used as `from_address`. Unverified aliases will be rejected by Gmail.

## License

[MIT](LICENSE) — copyright (c) 2026 Nicola Bonzanni.

The MIT license covers this plugin's own code (`server/*.py`, `skills/`, `tests/`, and the manifests). It does **not** cover `server/vendor/`, which vendors 37 third-party Python packages so the plugin can run without a network install step. Those packages remain under their own licenses (Apache-2.0, MIT, BSD and others); each ships its license text in its `*.dist-info/` directory — most under `*.dist-info/licenses/`, a few as `*.dist-info/LICENSE`.

**Platform constraint:** `server/vendor/` contains compiled extension modules built for **CPython 3.11 on linux x86_64** (`_cffi_backend`, `pydantic_core`, `charset_normalizer`, `cryptography`). The vendored tree will not import on another Python version or architecture, so a deployment or fork targeting anything else must re-vendor it from `server/requirements.txt`.

**Local patch:** `server/vendor/mcp/server/fastmcp/utilities/func_metadata.py` carries a one-line backport from mcp 1.30. Without it, mcp 1.3.0 JSON-decodes every string tool argument, so an attachment id `"1"` reaches the tool as the integer `1` and is rejected (#9). A re-vendor at 1.3.0 must re-apply that patch. `tests/test_server.py` fails if it is missing.

## Disclaimer

Not affiliated with, endorsed by, or sponsored by Google. "Gmail" is a trademark of Google LLC.
