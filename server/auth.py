import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request as AuthRequest
from google.oauth2.credentials import Credentials

from token_store import Credential, TokenStore

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.settings.basic",
]

_TOKEN_URI = "https://oauth2.googleapis.com/token"
_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"

_REQUIRED_ENV_VARS = ["GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_USER_EMAIL"]

# The `${VAR}` template form casa writes into .mcp.json. When the host spawns
# the server before the secret is wired, the substitution never happens and the
# variable arrives holding its own template text — non-empty, so the presence
# check below would pass it through, and every credential-consuming path
# downstream (the authorization URL, the code exchange, the API client) would
# be built from a string Google can only answer `invalid_client` to. An
# unresolved template is missing configuration, so it fails where absence
# fails: at startup, before anything is minted. Anchored and deliberately
# narrow — a real secret is an opaque string, and a looser match would refuse
# a credential that merely contains these characters.
_UNEXPANDED_RE = re.compile(r"^\$\{[A-Z_][A-Z0-9_]*\}$")


class ExchangeTerminal(RuntimeError):
    """The authorization code is dead — the token endpoint said the GRANT is
    invalid, or answered 2xx with an unusable body. The flow must be acked and
    restarted."""


class ExchangeRetryable(RuntimeError):
    """Transport failure, 5xx or 429. The flow may still succeed: do NOT ack."""


class ExchangeConfigError(RuntimeError):
    """The token endpoint refused the CLIENT or the request, not the code.

    Deliberately a SIBLING of ExchangeTerminal, not a subclass — the same
    reasoning as RefreshConfigError, one step earlier in the flow. Every
    `except ExchangeTerminal` means "this authorization code is dead" and acts
    on it: `auth_flow.collect_pass` acks the attempt, which tears the flow down
    at casa, and then falls through to the next attempt. Neither is right here.
    The authorization code is untouched and would exchange cleanly the moment
    the client credentials are corrected; and every remaining attempt would be
    exchanged against that same rejected client, so falling through would burn
    them all. Nor is it retryable: waiting changes nothing until the
    configuration does.
    """


class AccountMismatch(RuntimeError):
    """The credential authorizes an inbox other than GMAIL_USER_EMAIL."""


class RefreshTerminal(RuntimeError):
    """The refresh token is dead (revoked / invalid_grant)."""


class RefreshRetryable(RuntimeError):
    """Transport failure refreshing. The token is still good — retain it."""


class RefreshConfigError(RuntimeError):
    """The token endpoint refused the CLIENT or the request, not the grant.

    Deliberately a SIBLING of RefreshTerminal, not a subclass: every existing
    `except RefreshTerminal` means "this credential is dead" and acts on it —
    `load_active` deletes the token, `setup_gmail` mints a recovery link,
    `reconcile_stage` settles and discards the staged flow. None of those is
    right here. The refresh token is untouched and works again the moment the
    configuration is corrected; and a fresh authorization could not complete
    either, because the code exchange uses the same rejected client.
    """


# OAuth2 error codes that mean THE GRANT is dead — the only verdicts that
# justify deleting a stored refresh token, discarding an authorization code, or
# asking for a new authorization. `invalid_grant` is RFC 6749 §5.2's "the
# provided authorization grant ... is invalid, expired, revoked", and it covers
# BOTH grant types this plugin uses: it is what Google returns for a revoked or
# expired refresh token AND for a spent, expired or mismatched authorization
# code, which is why the refresh path and the exchange path read the same set.
# `invalid_token` (RFC 6750 §3.1) is the same verdict under a different name.
# Everything else non-retryable — `invalid_client` from a rotated secret,
# `unauthorized_client`, `invalid_request`, `redirect_uri_mismatch`, a
# RefreshError raised before any request, an error body that cannot be parsed
# at all — is a configuration failure, and the DEFAULT is that side of the
# line: destroying a working credential (or a still-usable authorization)
# requires positive evidence that it is dead, never the mere absence of
# evidence that it lives.
_CREDENTIAL_DEAD_CODES = frozenset({"invalid_grant", "invalid_token"})

_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _refresh_error_code(exc) -> str:
    """The OAuth2 error code carried by a google-auth RefreshError, or "".

    google-auth raises `RefreshError(f"{error}: {error_description}",
    response_data, retryable=...)` (oauth2/_client.py `_handle_error_response`),
    so the parsed body is read first and the message only as a fallback — and
    the fallback reads `args[0]`, not `str(exc)`, because a multi-arg exception
    stringifies to its whole args tuple. A RefreshError raised without ever
    reaching the token endpoint carries neither form and correctly yields "".
    """
    args = getattr(exc, "args", ())
    for arg in args:
        if isinstance(arg, dict):
            code = arg.get("error")
            if isinstance(code, str) and code.strip():
                return code.strip().lower()
    text = args[0] if args and isinstance(args[0], str) else str(exc)
    head = text.split(":", 1)[0].strip().lower()
    return head if _ERROR_CODE_RE.match(head) else ""


class GmailAuth:
    SCOPES = SCOPES

    def __init__(self, plugin_data_dir: str):
        self._credentials = None
        self._user_email = None
        self._client_id = None
        self._client_secret = None
        self.store = TokenStore(plugin_data_dir)
        # Assignable activation hook. The owner of the credential-derived
        # runtime (server.py) assigns it, so a rebuild failure raises INSIDE
        # activate() — i.e. before any caller acks a flow on the strength of it.
        # auth.py deliberately knows nothing about what gets rebuilt.
        self.on_activate = None

    def read_env(self) -> None:
        values = {name: os.environ.get(name, "") for name in _REQUIRED_ENV_VARS}
        missing = [name for name, val in values.items() if not val]
        if missing:
            print(
                f"Gmail plugin misconfigured: missing env var(s): {', '.join(missing)}",
                file=sys.stderr,
            )
            sys.exit(1)
        unexpanded = [name for name, val in values.items()
                      if _UNEXPANDED_RE.match(val)]
        if unexpanded:
            # Named separately from "missing" on purpose: the remedy is not the
            # operator's to apply here — the host has to resolve the secret and
            # respawn, and re-running setup against this process cannot help,
            # because these values are cached once, right here.
            print(
                "Gmail plugin misconfigured: unexpanded ${...} placeholder(s) "
                f"in env var(s): {', '.join(unexpanded)}. The host has not "
                "resolved them yet — wire the secrets and start a new session.",
                file=sys.stderr,
            )
            sys.exit(1)
        self._client_id = values["GMAIL_CLIENT_ID"]
        self._client_secret = values["GMAIL_CLIENT_SECRET"]
        self._user_email = values["GMAIL_USER_EMAIL"]

    def validate_and_init(self) -> bool:
        """Read env vars and load the ACTIVE credential. Staged recovery is the
        caller's job (auth_flow.startup_recover), which holds the collect lock."""
        self.read_env()
        return self.load_active()

    def _report_account_mismatch(self, account: str) -> None:
        print(
            "Gmail plugin: the stored credential authorizes "
            f"{account!r} but GMAIL_USER_EMAIL is {self._user_email!r}. "
            "Re-authorization is needed.",
            file=sys.stderr,
        )

    def load_active(self) -> bool:
        cred = self.store.load_active()
        if cred is None:
            return False
        if cred.account and cred.account.lower() != self._user_email.lower():
            self._report_account_mismatch(cred.account)
            return False
        try:
            if cred.account is None:
                # Legacy v1 file: it records no account, so the guard above
                # could not run and nothing would ever activate it. Verify once
                # with getProfile and migrate in place to v2, so every later
                # startup compares against a recorded account. The migrated
                # credential supersedes nothing: no flow, no generation.
                refreshed, account = self._refresh_and_profile(cred.refresh_token)
                if not account:
                    print("Gmail plugin: could not confirm which account the "
                          "stored token authorizes; token kept.", file=sys.stderr)
                    return False
                if account.lower() != self._user_email.lower():
                    self._report_account_mismatch(account)
                    return False
                cred = Credential(refresh_token=cred.refresh_token, flow=None,
                                  generation=None, account=account)
                self.store.write_active(cred)
            else:
                refreshed = self._refresh(cred.refresh_token)
        except RefreshTerminal as exc:
            print(f"Gmail plugin: stored token is dead — re-auth needed ({exc}).",
                  file=sys.stderr)
            self.store.remove_active()
            return False
        except RefreshConfigError as exc:
            # RETAIN the token: the client, not the grant, was refused. A
            # rotated or mistyped GMAIL_CLIENT_SECRET must not cost the
            # operator a working refresh token — it works again as soon as the
            # configuration is right, and no re-authorization can substitute.
            print(f"Gmail plugin: the OAuth client configuration was rejected "
                  f"({exc}); token kept.", file=sys.stderr)
            return False
        except RefreshRetryable as exc:
            # RETAIN the token: a network blip is not a revocation.
            print(f"Gmail plugin: could not refresh right now ({exc}); token kept.",
                  file=sys.stderr)
            return False
        except Exception as exc:
            # Unknown ⇒ retain, the same policy _refresh applies. getProfile
            # raises a plain ValueError on any HttpError and lets transport
            # failures through raw; neither may destroy a working credential.
            print(f"Gmail plugin: could not verify the stored token right now "
                  f"({exc}); token kept.", file=sys.stderr)
            return False
        # Pass the already-refreshed object through: rebuilding from the
        # refresh token alone would throw away the access token we just fetched.
        try:
            self.activate(cred, credentials=refreshed)
        except Exception as exc:
            # The activation hook failed (see activate). The credential stays on
            # disk; the process simply starts unauthenticated.
            print(f"Gmail plugin: could not bring the stored credential into "
                  f"service ({exc}); token kept.", file=sys.stderr)
            return False
        return True

    def _refresh(self, refresh_token: str) -> Credentials:
        credentials = self.credentials_for(refresh_token)
        try:
            credentials.refresh(AuthRequest())
        except RefreshError as exc:
            # NOT every RefreshError is a revocation. google-auth marks the
            # exception retryable when the token endpoint answered with a
            # retryable status, or with `internal_failure` / `server_error` /
            # `temporarily_unavailable` (oauth2/_client.py `_can_retry`), and
            # raises it only after exhausting its own internal retries. Calling
            # that terminal would delete a working refresh token — and discard a
            # freshly-staged, perfectly valid credential — because Google had a
            # transient outage. getattr, not `.retryable`: a future google-auth
            # that drops the attribute must degrade to "terminal", not crash.
            if getattr(exc, "retryable", False):
                raise RefreshRetryable(str(exc)) from exc
            # Nor is every non-retryable RefreshError a revocation. Rotate the
            # OAuth client secret and Google answers `invalid_client`: the
            # refresh token is fine, the CLIENT was refused. Calling that
            # terminal deleted the credential and offered a re-authorization
            # that could not complete either, since the code exchange uses the
            # same rejected secret. Only a grant-invalidating code is terminal.
            if _refresh_error_code(exc) in _CREDENTIAL_DEAD_CODES:
                raise RefreshTerminal(str(exc)) from exc
            raise RefreshConfigError(str(exc)) from exc
        except Exception as exc:
            raise RefreshRetryable(str(exc)) from exc
        return credentials

    def activate(self, cred: Credential, credentials=None) -> None:
        """Idempotent: make `cred` the live credential, then run `on_activate`.

        `credentials` lets a caller that has just refreshed hand the live object
        through instead of discarding its access token. Callers that hold only a
        stored Credential omit it; google-auth then refreshes on first use.

        The hook runs here, and is allowed to raise: everything derived from the
        credential must be in service before a caller treats the activation as
        successful and acks the flow that produced it.
        """
        self._credentials = credentials or self.credentials_for(cred.refresh_token)
        if self.on_activate is not None:
            self.on_activate()

    def _refresh_and_profile(self, refresh_token: str):
        """(live credentials, authorized address). One refresh, one getProfile."""
        credentials = self._refresh(refresh_token)
        from gmail_client import GmailClient
        return credentials, GmailClient(credentials).get_profile_email()

    def refresh_and_verify(self, refresh_token: str) -> str:
        """Refresh, then ask Google which account this credential belongs to."""
        return self._refresh_and_profile(refresh_token)[1]

    def probe_refresh(self, refresh_token: str) -> None:
        """Liveness probe: refresh once, discard the result, keep the token.

        A read-only public view of the SAME RefreshTerminal / RefreshRetryable
        / RefreshConfigError split `_refresh` already raises — no new OAuth
        behaviour, and no new classification. It exists so a caller can ask
        "is this credential still
        good?" without `load_active`'s side effects: `load_active` removes a
        terminally dead token and activates a live one, neither of which a mere
        health check may do. Performs no writes and touches no runtime state.
        """
        self._refresh(refresh_token)

    def build_auth_url(self, redirect_uri: str, state: str) -> str:
        """Authorization URL for casa's callback endpoint.

        `redirect_uri` is READ from casa's .index entry by the caller and never
        derived here: it is matched byte-for-byte by Google.
        """
        params = {
            "client_id": self._client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
        return f"{_AUTH_URI}?{urllib.parse.urlencode(params)}"

    def exchange_code(self, code: str, redirect_uri: str) -> dict:
        """Exchange the code for tokens. Performs NO writes and mutates no
        runtime state — persistence is TokenStore's job, activation is
        activate()'s. Raises ExchangeTerminal, ExchangeConfigError or
        ExchangeRetryable."""
        body = urllib.parse.urlencode({
            "code": code,
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }).encode()

        req = urllib.request.Request(_TOKEN_URI, data=body, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")

        try:
            with urllib.request.urlopen(req) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            code, detail = self._error_report(e)
            if e.code == 429 or e.code >= 500:
                raise ExchangeRetryable(f"Token exchange retryable ({e.code}): {detail}")
            # NOT every non-retryable status means the authorization CODE is
            # dead. Rotate the OAuth client secret and Google answers
            # `invalid_client`; register the wrong redirect URI and it answers
            # `redirect_uri_mismatch`. In both, the code is still perfectly
            # good and the CLIENT was refused — so acking the attempt would
            # destroy a usable authorization, and telling the operator to start
            # again would be false twice over, since a fresh authorization ends
            # at this same exchange against this same client. Only a
            # grant-invalidating code is terminal, and a failure this cannot
            # classify (no `error` field, an unparseable body) defaults to the
            # configuration side for the reason _CREDENTIAL_DEAD_CODES gives:
            # tearing a flow down needs positive evidence that it is dead.
            if code in _CREDENTIAL_DEAD_CODES:
                raise ExchangeTerminal(f"Token exchange failed ({e.code}): {detail}")
            raise ExchangeConfigError(
                f"Token exchange refused this OAuth client ({e.code}): {detail}")
        except urllib.error.URLError as e:
            raise ExchangeRetryable(f"Token exchange transport failure: {e}")

        try:
            token_data = json.loads(raw)
        except ValueError:
            raise ExchangeTerminal(
                "Token endpoint returned a 2xx whose body is not JSON."
            )
        if not isinstance(token_data, dict):
            raise ExchangeTerminal("Token endpoint returned a non-object body.")
        for field in ("refresh_token", "access_token"):
            if not token_data.get(field):
                raise ExchangeTerminal(
                    f"Token response is missing {field}. Re-authorization is needed."
                )
        return token_data

    @staticmethod
    def _error_report(e: "urllib.error.HTTPError") -> tuple[str, str]:
        """(OAuth2 error code, human detail) from a SINGLE read of the body.

        One read, two answers, on purpose: `e.read()` drains a stream, so a
        second reader gets b"" and would silently see an empty body. The code
        is normalized the way `_refresh_error_code` normalizes its own, and is
        "" whenever the body cannot supply one — which the caller treats as
        unclassified, not as a dead grant.
        """
        try:
            parsed = json.loads(e.read().decode())
        except Exception:
            return "", "unparseable error body"
        if not isinstance(parsed, dict):
            return "", "unparseable error body"
        raw = parsed.get("error")
        code = raw.strip().lower() if isinstance(raw, str) else ""
        return code, (f"{parsed.get('error', 'unknown')} — "
                      f"{parsed.get('error_description', '')}")

    def credentials_for(self, refresh_token: str) -> Credentials:
        return Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri=_TOKEN_URI,
            client_id=self._client_id,
            client_secret=self._client_secret,
            scopes=SCOPES,
        )

    @property
    def credentials(self) -> Credentials:
        return self._credentials

    @property
    def subject_email(self) -> str:
        return self._user_email

    @property
    def is_authenticated(self) -> bool:
        return self._credentials is not None
