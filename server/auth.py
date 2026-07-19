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
_REDIRECT_URI = "http://localhost:8080"

_REQUIRED_ENV_VARS = ["GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_USER_EMAIL"]


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

    def build_auth_url(self) -> str:
        """Build the OAuth authorization URL for the loopback redirect flow."""
        params = {
            "client_id": self._client_id,
            "redirect_uri": _REDIRECT_URI,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "access_type": "offline",
            "prompt": "consent",
        }
        return f"{_AUTH_URI}?{urllib.parse.urlencode(params)}"

    def exchange_code(self, code: str) -> Credentials:
        """Exchange authorization code for tokens, persist refresh token, return credentials."""
        body = urllib.parse.urlencode({
            "code": code,
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "redirect_uri": _REDIRECT_URI,
            "grant_type": "authorization_code",
        }).encode()

        req = urllib.request.Request(_TOKEN_URI, data=body, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")

        try:
            with urllib.request.urlopen(req) as resp:
                token_data = json.load(resp)
        except urllib.error.HTTPError as e:
            error_body = json.loads(e.read().decode())
            raise ValueError(
                f"Token exchange failed: {error_body.get('error', 'unknown')} — "
                f"{error_body.get('error_description', str(e))}"
            )

        if "refresh_token" not in token_data:
            raise ValueError(
                "No refresh_token in response. This can happen if consent was previously "
                "granted; the auth URL forces re-consent (prompt=consent), so re-visiting "
                "the URL from gmail_auth_start should resolve this."
            )

        os.makedirs(os.path.dirname(self._token_file), exist_ok=True)
        with open(self._token_file, "w") as f:
            json.dump({"refresh_token": token_data["refresh_token"]}, f)

        credentials = Credentials(
            token=token_data.get("access_token"),
            refresh_token=token_data["refresh_token"],
            token_uri=_TOKEN_URI,
            client_id=self._client_id,
            client_secret=self._client_secret,
            scopes=SCOPES,
        )
        self._credentials = credentials
        return credentials

    @property
    def credentials(self) -> Credentials:
        return self._credentials

    @property
    def subject_email(self) -> str:
        return self._user_email

    @property
    def is_authenticated(self) -> bool:
        return self._credentials is not None
