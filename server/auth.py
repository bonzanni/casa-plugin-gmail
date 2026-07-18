import email.utils
import json
import os
import sys

from google.oauth2 import service_account

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]


class GmailAuth:
    SCOPES = SCOPES

    def __init__(self):
        self._credentials = None

    def validate_and_init(self):
        sa_json_str = os.environ.get("GMAIL_SERVICE_ACCOUNT_JSON", "")
        user_email = os.environ.get("GMAIL_USER_EMAIL", "")

        if not sa_json_str:
            print("Gmail plugin misconfigured: GMAIL_SERVICE_ACCOUNT_JSON is missing.", file=sys.stderr)
            sys.exit(1)
        if not user_email:
            print("Gmail plugin misconfigured: GMAIL_USER_EMAIL is missing.", file=sys.stderr)
            sys.exit(1)

        try:
            sa_info = json.loads(sa_json_str)
        except json.JSONDecodeError as exc:
            print(f"Gmail plugin misconfigured: GMAIL_SERVICE_ACCOUNT_JSON is not valid JSON: {exc}", file=sys.stderr)
            sys.exit(1)

        for field in ("type", "private_key", "client_email", "token_uri"):
            if field not in sa_info:
                print(f"Gmail plugin misconfigured: GMAIL_SERVICE_ACCOUNT_JSON missing field '{field}'.", file=sys.stderr)
                sys.exit(1)

        if sa_info["type"] != "service_account":
            print(f"Invalid service account: type must be service_account, got {sa_info['type']}.", file=sys.stderr)
            sys.exit(1)

        _, addr = email.utils.parseaddr(user_email)
        local, _, domain = addr.partition("@")
        if not local or "." not in domain:
            print(f"Gmail plugin misconfigured: GMAIL_USER_EMAIL '{user_email}' is not a valid email address.", file=sys.stderr)
            sys.exit(1)

        self._credentials = service_account.Credentials.from_service_account_info(
            sa_info,
            scopes=SCOPES,
            subject=user_email,
        )

    @property
    def credentials(self):
        return self._credentials
