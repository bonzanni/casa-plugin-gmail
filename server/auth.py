import os
import sys

from google.auth.transport.requests import Request as AuthRequest
from google.oauth2.credentials import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.settings.basic",
]

_TOKEN_URI = "https://oauth2.googleapis.com/token"

_REQUIRED_VARS = [
    "GMAIL_CLIENT_ID",
    "GMAIL_CLIENT_SECRET",
    "GMAIL_REFRESH_TOKEN",
    "GMAIL_USER_EMAIL",
]


class GmailAuth:
    SCOPES = SCOPES

    def __init__(self):
        self._credentials = None
        self._user_email = None

    def validate_and_init(self):
        values = {name: os.environ.get(name, "") for name in _REQUIRED_VARS}
        missing = [name for name, val in values.items() if not val]
        if missing:
            print(
                f"Gmail plugin misconfigured: missing env var(s): {', '.join(missing)}",
                file=sys.stderr,
            )
            sys.exit(1)

        credentials = Credentials(
            token=None,
            refresh_token=values["GMAIL_REFRESH_TOKEN"],
            token_uri=_TOKEN_URI,
            client_id=values["GMAIL_CLIENT_ID"],
            client_secret=values["GMAIL_CLIENT_SECRET"],
            scopes=SCOPES,
        )
        try:
            credentials.refresh(AuthRequest())
        except Exception as exc:
            print(
                f"Gmail plugin: failed to refresh OAuth token — check GMAIL_CLIENT_ID, "
                f"GMAIL_CLIENT_SECRET, and GMAIL_REFRESH_TOKEN. Error: {exc}",
                file=sys.stderr,
            )
            sys.exit(1)

        self._credentials = credentials
        self._user_email = values["GMAIL_USER_EMAIL"]

    @property
    def credentials(self):
        return self._credentials

    @property
    def subject_email(self):
        return self._user_email
