import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

from google.auth.transport.requests import Request as AuthRequest
from google.oauth2.credentials import Credentials

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


class GmailAuth:
    SCOPES = SCOPES

    def __init__(self, plugin_data_dir: str):
        self._credentials = None
        self._user_email = None
        self._client_id = None
        self._client_secret = None
        self._token_file = os.path.join(plugin_data_dir, "oauth_token.json")

    def validate_and_init(self) -> bool:
        """Read env vars and try to load persisted token. Returns True if authenticated."""
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

        if not os.path.exists(self._token_file):
            return False

        try:
            with open(self._token_file) as f:
                data = json.load(f)
            credentials = Credentials(
                token=None,
                refresh_token=data["refresh_token"],
                token_uri=_TOKEN_URI,
                client_id=self._client_id,
                client_secret=self._client_secret,
                scopes=SCOPES,
            )
            credentials.refresh(AuthRequest())
            self._credentials = credentials
            return True
        except Exception as exc:
            print(
                f"Gmail plugin: stored token invalid or expired — re-auth needed. Error: {exc}",
                file=sys.stderr,
            )
            os.remove(self._token_file)
            return False

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
