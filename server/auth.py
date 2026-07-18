import email.utils
import os
import sys

from google.auth import default as google_auth_default
from google.auth import exceptions as google_auth_exceptions
from google.auth import impersonated_credentials
from google.auth.transport.requests import Request as AuthRequest

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.settings.basic",
]

_SA_SUFFIX = ".gserviceaccount.com"


def _validate_email_format(label: str, value: str) -> None:
    _, addr = email.utils.parseaddr(value)
    local, _, domain = addr.partition("@")
    if not local or "." not in domain:
        print(
            f"Gmail plugin misconfigured: {label} '{value}' is not a valid email address.",
            file=sys.stderr,
        )
        sys.exit(1)


class GmailAuth:
    SCOPES = SCOPES

    def __init__(self):
        self._credentials = None
        self._subject_email = None

    def validate_and_init(self):
        impersonation_sa = os.environ.get("GMAIL_IMPERSONATION_SA", "")
        subject_email = os.environ.get("GMAIL_SUBJECT_EMAIL", "")

        if not impersonation_sa:
            print(
                "Gmail plugin misconfigured: GMAIL_IMPERSONATION_SA is missing.",
                file=sys.stderr,
            )
            sys.exit(1)
        if not subject_email:
            print(
                "Gmail plugin misconfigured: GMAIL_SUBJECT_EMAIL is missing.",
                file=sys.stderr,
            )
            sys.exit(1)

        _validate_email_format("GMAIL_IMPERSONATION_SA", impersonation_sa)
        _, _sa_addr = email.utils.parseaddr(impersonation_sa)
        if not _sa_addr.lower().endswith(_SA_SUFFIX):
            print(
                f"Gmail plugin misconfigured: GMAIL_IMPERSONATION_SA '{impersonation_sa}' "
                f"must be a service account email ending in {_SA_SUFFIX}.",
                file=sys.stderr,
            )
            sys.exit(1)

        _validate_email_format("GMAIL_SUBJECT_EMAIL", subject_email)
        _, _subj_addr = email.utils.parseaddr(subject_email)
        if _subj_addr.lower().endswith(_SA_SUFFIX):
            print(
                f"Gmail plugin misconfigured: GMAIL_SUBJECT_EMAIL '{subject_email}' "
                f"looks like a service account — it must be a Workspace user email.",
                file=sys.stderr,
            )
            sys.exit(1)
        if _subj_addr.lower().endswith("@gmail.com"):
            print(
                f"Gmail plugin misconfigured: GMAIL_SUBJECT_EMAIL '{subject_email}' "
                f"is a personal Gmail address. Domain-wide delegation requires a Google Workspace account.",
                file=sys.stderr,
            )
            sys.exit(1)

        try:
            source_credentials, _ = google_auth_default()
        except google_auth_exceptions.DefaultCredentialsError:
            print(
                "Gmail plugin: no Application Default Credentials found. "
                "Run: gcloud auth application-default login",
                file=sys.stderr,
            )
            sys.exit(1)

        credentials = impersonated_credentials.Credentials(
            source_credentials=source_credentials,
            target_principal=impersonation_sa,
            target_scopes=SCOPES,
            subject=subject_email,
        )

        try:
            credentials.refresh(AuthRequest())
        except google_auth_exceptions.RefreshError as exc:
            print(
                f"Gmail plugin: failed to impersonate {impersonation_sa} for subject {subject_email}. "
                f"Possible causes:\n"
                f"  1. Your ADC account lacks roles/iam.serviceAccountTokenCreator on the SA. Fix:\n"
                f"     gcloud iam service-accounts add-iam-policy-binding {impersonation_sa} "
                f"--member=user:$(gcloud config get-value account) --role=roles/iam.serviceAccountTokenCreator\n"
                f"  2. iamcredentials.googleapis.com not enabled. Fix:\n"
                f"     gcloud services enable iamcredentials.googleapis.com\n"
                f"  3. DWD not enabled on the SA or scopes not authorised in Workspace Admin.\n"
                f"Error: {exc}",
                file=sys.stderr,
            )
            sys.exit(1)
        except google_auth_exceptions.GoogleAuthError as exc:
            print(
                f"Gmail plugin: authentication error during impersonation: {exc}",
                file=sys.stderr,
            )
            sys.exit(1)

        self._credentials = credentials
        self._subject_email = subject_email

    @property
    def credentials(self):
        return self._credentials

    @property
    def subject_email(self):
        return self._subject_email
