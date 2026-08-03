import json
import os
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


class ExchangeTerminal(RuntimeError):
    """The authorization code is dead — 4xx, or a 2xx whose body is unusable.
    The flow must be acked and restarted."""


class ExchangeRetryable(RuntimeError):
    """Transport failure, 5xx or 429. The flow may still succeed: do NOT ack."""


class AccountMismatch(RuntimeError):
    """The credential authorizes an inbox other than GMAIL_USER_EMAIL."""


class RefreshTerminal(RuntimeError):
    """The refresh token is dead (revoked / invalid_grant)."""


class RefreshRetryable(RuntimeError):
    """Transport failure refreshing. The token is still good — retain it."""


class GmailAuth:
    SCOPES = SCOPES

    def __init__(self, plugin_data_dir: str):
        self._credentials = None
        self._user_email = None
        self._client_id = None
        self._client_secret = None
        self.store = TokenStore(plugin_data_dir)

    def _read_env(self) -> None:
        values = {name: os.environ.get(name, "") for name in _REQUIRED_ENV_VARS}
        missing = [name for name, val in values.items() if not val]
        if missing:
            print(
                f"Gmail plugin misconfigured: missing env var(s): {', '.join(missing)}",
                file=sys.stderr,
            )
            sys.exit(1)
        self._client_id = values["GMAIL_CLIENT_ID"]
        self._client_secret = values["GMAIL_CLIENT_SECRET"]
        self._user_email = values["GMAIL_USER_EMAIL"]

    def validate_and_init(self) -> bool:
        """Read env vars and load the ACTIVE credential. Staged recovery is the
        caller's job (auth_flow.startup_recover), which holds the collect lock."""
        self._read_env()
        return self.load_active()

    def load_active(self) -> bool:
        cred = self.store.load_active()
        if cred is None:
            return False
        if cred.account and cred.account.lower() != self._user_email.lower():
            print(
                "Gmail plugin: the stored credential authorizes "
                f"{cred.account!r} but GMAIL_USER_EMAIL is {self._user_email!r}. "
                "Re-authorization is needed.",
                file=sys.stderr,
            )
            return False
        try:
            refreshed = self._refresh(cred.refresh_token)
        except RefreshTerminal as exc:
            print(f"Gmail plugin: stored token is dead — re-auth needed ({exc}).",
                  file=sys.stderr)
            self.store.remove_active()
            return False
        except RefreshRetryable as exc:
            # RETAIN the token: a network blip is not a revocation.
            print(f"Gmail plugin: could not refresh right now ({exc}); token kept.",
                  file=sys.stderr)
            return False
        # Pass the already-refreshed object through: rebuilding from the
        # refresh token alone would throw away the access token we just fetched.
        self.activate(cred, credentials=refreshed)
        return True

    def _refresh(self, refresh_token: str) -> Credentials:
        credentials = self.credentials_for(refresh_token)
        try:
            credentials.refresh(AuthRequest())
        except RefreshError as exc:
            raise RefreshTerminal(str(exc)) from exc
        except Exception as exc:
            raise RefreshRetryable(str(exc)) from exc
        return credentials

    def activate(self, cred: Credential, credentials=None) -> None:
        """Idempotent: make `cred` the live credential.

        `credentials` lets a caller that has just refreshed hand the live object
        through instead of discarding its access token. Callers that hold only a
        stored Credential omit it; google-auth then refreshes on first use.
        """
        self._credentials = credentials or self.credentials_for(cred.refresh_token)

    def refresh_and_verify(self, refresh_token: str) -> str:
        """Refresh, then ask Google which account this credential belongs to."""
        credentials = self._refresh(refresh_token)
        from gmail_client import GmailClient
        return GmailClient(credentials).get_profile_email()

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
        activate()'s. Raises ExchangeTerminal or ExchangeRetryable."""
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
            detail = self._error_detail(e)
            if e.code == 429 or e.code >= 500:
                raise ExchangeRetryable(f"Token exchange retryable ({e.code}): {detail}")
            raise ExchangeTerminal(f"Token exchange failed ({e.code}): {detail}")
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
    def _error_detail(e: "urllib.error.HTTPError") -> str:
        try:
            parsed = json.loads(e.read().decode())
            return f"{parsed.get('error', 'unknown')} — {parsed.get('error_description', '')}"
        except Exception:
            return "unparseable error body"

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
