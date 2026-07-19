#!/usr/bin/env python3
"""
DOCUMENTED FALLBACK ONLY — not required for normal setup.

As of v0.4.0, the OAuth consent flow is completable entirely from chat using
the gmail_auth_start and gmail_auth_complete tools. Use this script only if
the chat-driven flow is unavailable (e.g., local dev testing).

Run on your workstation (not inside Casa). Opens a browser for the Google
consent screen and prints the refresh token to stdout. The refresh token
obtained here can be written directly to ${CLAUDE_PLUGIN_DATA}/oauth_token.json
as {"refresh_token": "<value>"} if you need to seed a deployment manually.

Requirements:
    pip install google-auth-oauthlib

Usage:
    Set GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET in your environment (or the
    script will prompt for them), then run:

        python bootstrap/get_credentials.py
"""

import os
import sys

try:
    from google_auth_oauthlib.flow import InstalledAppFlow
except ImportError:
    print("Missing dependency. Install with:  pip install google-auth-oauthlib")
    sys.exit(1)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.settings.basic",
]


def main():
    client_id = os.environ.get("GMAIL_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GMAIL_CLIENT_SECRET", "").strip()

    if not client_id:
        client_id = input("GMAIL_CLIENT_ID: ").strip()
    if not client_secret:
        client_secret = input("GMAIL_CLIENT_SECRET: ").strip()

    if not client_id or not client_secret:
        print("Error: GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET are required.")
        sys.exit(1)

    user_email = os.environ.get("GMAIL_USER_EMAIL", "").strip()
    if not user_email:
        user_email = input("Gmail address to authenticate as: ").strip()
    if not user_email:
        print("Error: Gmail address is required.")
        sys.exit(1)

    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["urn:ietf:wg:oauth:2.0:oob", "http://localhost"],
        }
    }

    print("\nOpening browser for Google consent screen...")
    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    credentials = flow.run_local_server(port=0, prompt="consent", access_type="offline")

    if not credentials.refresh_token:
        print(
            "\nError: no refresh token returned. Go to https://myaccount.google.com/permissions, "
            "revoke access for your app, then run this script again."
        )
        sys.exit(1)

    print("\n" + "=" * 60)
    print("Success! Store these four values in Casa's configurator:")
    print("=" * 60)
    print(f"\nGMAIL_CLIENT_ID={client_id}")
    print(f"GMAIL_CLIENT_SECRET={client_secret}")
    print(f"GMAIL_REFRESH_TOKEN={credentials.refresh_token}")
    print(f"GMAIL_USER_EMAIL={user_email}")
    print()


if __name__ == "__main__":
    main()
