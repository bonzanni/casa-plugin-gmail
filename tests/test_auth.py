import io
import json
import pytest
from unittest.mock import patch, MagicMock


def make_auth(data_dir):
    from auth import GmailAuth
    return GmailAuth(str(data_dir))


_FULL_ENV = {
    "GMAIL_CLIENT_ID": "client-id",
    "GMAIL_CLIENT_SECRET": "client-secret",
    "GMAIL_USER_EMAIL": "user@workspace.example.com",
}


def _set_full_env(monkeypatch, **overrides):
    env = {**_FULL_ENV, **overrides}
    for key, val in env.items():
        if val is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, val)


# ── Env var validation ─────────────────────────────────────────────────────

@pytest.mark.parametrize("missing_var", [
    "GMAIL_CLIENT_ID",
    "GMAIL_CLIENT_SECRET",
    "GMAIL_USER_EMAIL",
])
def test_missing_required_var_exits(missing_var, monkeypatch, tmp_path):
    _set_full_env(monkeypatch, **{missing_var: None})
    with pytest.raises(SystemExit):
        make_auth(tmp_path).validate_and_init()


@pytest.mark.parametrize("missing_var", [
    "GMAIL_CLIENT_ID",
    "GMAIL_CLIENT_SECRET",
    "GMAIL_USER_EMAIL",
])
def test_missing_required_var_names_it_in_stderr(missing_var, monkeypatch, capsys, tmp_path):
    _set_full_env(monkeypatch, **{missing_var: None})
    with pytest.raises(SystemExit):
        make_auth(tmp_path).validate_and_init()
    assert missing_var in capsys.readouterr().err


# ── No token file ───────────────────────────────────────────────────────────

def test_no_token_file_returns_false(monkeypatch, tmp_path):
    _set_full_env(monkeypatch)
    auth = make_auth(tmp_path)
    assert auth.validate_and_init() is False
    assert not auth.is_authenticated


def test_no_token_file_credentials_is_none(monkeypatch, tmp_path):
    _set_full_env(monkeypatch)
    auth = make_auth(tmp_path)
    auth.validate_and_init()
    assert auth.credentials is None


# ── Active credential loading ───────────────────────────────────────────────

def _write_v2(tmp_path, **over):
    payload = {"v": 2, "refresh_token": "rt-abc", "flow": "a" * 64,
               "generation": 5.0, "account": "user@workspace.example.com",
               "committed_ts": 1.0}
    payload.update(over)
    (tmp_path / "oauth_token.json").write_text(json.dumps(payload))


@patch("auth.Credentials")
def test_valid_v2_token_authenticates(mock_creds_cls, monkeypatch, tmp_path):
    _set_full_env(monkeypatch)
    _write_v2(tmp_path)
    mock_creds_cls.return_value = MagicMock()

    auth = make_auth(tmp_path)
    assert auth.validate_and_init() is True
    assert auth.is_authenticated
    assert auth.subject_email == "user@workspace.example.com"


@patch("auth.Credentials")
def test_v2_account_mismatch_refuses_to_authenticate(mock_creds_cls, monkeypatch, tmp_path):
    """Changing GMAIL_USER_EMAIL must not silently keep serving the old inbox."""
    _set_full_env(monkeypatch)
    _write_v2(tmp_path, account="someone-else@example.com")
    mock_creds_cls.return_value = MagicMock()

    auth = make_auth(tmp_path)
    assert auth.validate_and_init() is False
    assert not auth.is_authenticated


@patch("auth.Credentials")
def test_v2_account_mismatch_keeps_the_token_file(mock_creds_cls, monkeypatch, tmp_path):
    _set_full_env(monkeypatch)
    _write_v2(tmp_path, account="someone-else@example.com")
    mock_creds_cls.return_value = MagicMock()

    make_auth(tmp_path).validate_and_init()
    assert (tmp_path / "oauth_token.json").exists()


@patch("auth.Credentials")
def test_account_comparison_is_case_insensitive(mock_creds_cls, monkeypatch, tmp_path):
    _set_full_env(monkeypatch)
    _write_v2(tmp_path, account="User@Workspace.Example.COM")
    mock_creds_cls.return_value = MagicMock()

    assert make_auth(tmp_path).validate_and_init() is True


@patch("auth.Credentials")
def test_terminal_refresh_failure_removes_the_token(mock_creds_cls, monkeypatch, tmp_path):
    from google.auth.exceptions import RefreshError
    _set_full_env(monkeypatch)
    _write_v2(tmp_path)
    creds = MagicMock()
    creds.refresh.side_effect = RefreshError("invalid_grant")
    mock_creds_cls.return_value = creds

    auth = make_auth(tmp_path)
    assert auth.validate_and_init() is False
    assert not (tmp_path / "oauth_token.json").exists()


@patch("auth.Credentials")
def test_transient_refresh_failure_RETAINS_the_token(mock_creds_cls, monkeypatch, tmp_path):
    """A network blip must never destroy a valid refresh token."""
    _set_full_env(monkeypatch)
    _write_v2(tmp_path)
    creds = MagicMock()
    creds.refresh.side_effect = OSError("connection reset")
    mock_creds_cls.return_value = creds

    auth = make_auth(tmp_path)
    assert auth.validate_and_init() is False
    assert (tmp_path / "oauth_token.json").exists()


@patch("auth.Credentials")
def test_retryable_RefreshError_RETAINS_the_token(mock_creds_cls, monkeypatch, tmp_path):
    """A transient Google 5xx surfaces as RefreshError(retryable=True) after
    google-auth exhausts its own retries. Treating that as terminal would
    destroy a working refresh token over an outage."""
    from google.auth.exceptions import RefreshError
    _set_full_env(monkeypatch)
    _write_v2(tmp_path)
    creds = MagicMock()
    creds.refresh.side_effect = RefreshError(
        "server_error: backend error", retryable=True)
    mock_creds_cls.return_value = creds

    auth = make_auth(tmp_path)
    assert auth.validate_and_init() is False
    assert (tmp_path / "oauth_token.json").exists()


@patch("auth.Credentials")
def test_non_retryable_RefreshError_still_removes_the_token(
        mock_creds_cls, monkeypatch, tmp_path):
    """The other half: an actual revocation must still be terminal."""
    from google.auth.exceptions import RefreshError
    _set_full_env(monkeypatch)
    _write_v2(tmp_path)
    creds = MagicMock()
    creds.refresh.side_effect = RefreshError("invalid_grant", retryable=False)
    mock_creds_cls.return_value = creds

    auth = make_auth(tmp_path)
    assert auth.validate_and_init() is False
    assert not (tmp_path / "oauth_token.json").exists()


@patch("auth.Credentials")
def test_RefreshError_without_a_retryable_attribute_is_terminal(
        mock_creds_cls, monkeypatch, tmp_path):
    """Defensive read: a google-auth that drops `.retryable` must degrade to
    today's behaviour rather than raising AttributeError."""
    _set_full_env(monkeypatch)
    _write_v2(tmp_path)

    class BareRefreshError(Exception):
        pass

    creds = MagicMock()
    creds.refresh.side_effect = BareRefreshError("invalid_grant")
    mock_creds_cls.return_value = creds

    auth = make_auth(tmp_path)
    with patch("auth.RefreshError", BareRefreshError):
        assert auth.validate_and_init() is False
    assert not (tmp_path / "oauth_token.json").exists()


# ── Legacy v1 migration ─────────────────────────────────────────────────────

def _write_v1(tmp_path, refresh_token="rt-legacy"):
    """A v0.4.x file: no `v`, no `flow`, no `account`."""
    (tmp_path / "oauth_token.json").write_text(
        json.dumps({"refresh_token": refresh_token}))


def _raw_active(tmp_path):
    return json.loads((tmp_path / "oauth_token.json").read_text())


@patch("gmail_client.GmailClient")
@patch("auth.Credentials")
def test_v1_token_is_verified_and_migrated_in_place_to_v2(
        mock_creds_cls, mock_client_cls, monkeypatch, tmp_path):
    """A v1 file records no account, so the mismatch guard cannot run on it.
    Verify with getProfile once, then rewrite it as v2 so it can never again be
    served unverified. The migrated credential supersedes nothing."""
    _set_full_env(monkeypatch)
    _write_v1(tmp_path)
    mock_creds_cls.return_value = MagicMock()
    mock_client_cls.return_value.get_profile_email.return_value = (
        "User@Workspace.Example.COM")

    auth = make_auth(tmp_path)
    assert auth.validate_and_init() is True
    assert auth.is_authenticated

    raw = _raw_active(tmp_path)
    assert raw["v"] == 2
    assert raw["refresh_token"] == "rt-legacy"
    assert raw["account"] == "User@Workspace.Example.COM"
    assert raw["flow"] is None
    assert raw["generation"] is None
    mock_client_cls.return_value.get_profile_email.assert_called_once()


@patch("gmail_client.GmailClient")
@patch("auth.Credentials")
def test_v1_token_for_another_account_is_refused_and_left_on_disk(
        mock_creds_cls, mock_client_cls, monkeypatch, tmp_path):
    """Changing GMAIL_USER_EMAIL must not silently keep serving the old inbox
    just because the stored file predates the account field."""
    _set_full_env(monkeypatch)
    _write_v1(tmp_path)
    mock_creds_cls.return_value = MagicMock()
    mock_client_cls.return_value.get_profile_email.return_value = "someone-else@example.com"

    auth = make_auth(tmp_path)
    assert auth.validate_and_init() is False
    assert not auth.is_authenticated
    assert _raw_active(tmp_path) == {"refresh_token": "rt-legacy"}   # untouched


@patch("gmail_client.GmailClient")
@patch("auth.Credentials")
def test_v1_transient_verification_failure_retains_the_token(
        mock_creds_cls, mock_client_cls, monkeypatch, tmp_path):
    _set_full_env(monkeypatch)
    _write_v1(tmp_path)
    creds = MagicMock()
    creds.refresh.side_effect = OSError("connection reset")
    mock_creds_cls.return_value = creds

    auth = make_auth(tmp_path)
    assert auth.validate_and_init() is False
    assert _raw_active(tmp_path) == {"refresh_token": "rt-legacy"}


@patch("gmail_client.GmailClient")
@patch("auth.Credentials")
def test_v1_unclassified_getprofile_failure_retains_the_token(
        mock_creds_cls, mock_client_cls, monkeypatch, tmp_path):
    """get_profile_email raises a plain ValueError for every HttpError — a 403
    from an unenabled Gmail API must not destroy a working credential."""
    _set_full_env(monkeypatch)
    _write_v1(tmp_path)
    mock_creds_cls.return_value = MagicMock()
    mock_client_cls.return_value.get_profile_email.side_effect = ValueError(
        "Gmail API error 403: accessNotConfigured")

    auth = make_auth(tmp_path)
    assert auth.validate_and_init() is False
    assert not auth.is_authenticated
    assert _raw_active(tmp_path) == {"refresh_token": "rt-legacy"}


@patch("gmail_client.GmailClient")
@patch("auth.Credentials")
def test_v1_blank_verified_account_retains_the_token(
        mock_creds_cls, mock_client_cls, monkeypatch, tmp_path):
    _set_full_env(monkeypatch)
    _write_v1(tmp_path)
    mock_creds_cls.return_value = MagicMock()
    mock_client_cls.return_value.get_profile_email.return_value = ""

    auth = make_auth(tmp_path)
    assert auth.validate_and_init() is False
    assert _raw_active(tmp_path) == {"refresh_token": "rt-legacy"}


@patch("gmail_client.GmailClient")
@patch("auth.Credentials")
def test_v1_terminal_refresh_removes_the_token(
        mock_creds_cls, mock_client_cls, monkeypatch, tmp_path):
    from google.auth.exceptions import RefreshError
    _set_full_env(monkeypatch)
    _write_v1(tmp_path)
    creds = MagicMock()
    creds.refresh.side_effect = RefreshError("invalid_grant")
    mock_creds_cls.return_value = creds

    auth = make_auth(tmp_path)
    assert auth.validate_and_init() is False
    assert not (tmp_path / "oauth_token.json").exists()


@patch("gmail_client.GmailClient")
@patch("auth.Credentials")
def test_v2_token_is_not_re_verified_over_the_network(
        mock_creds_cls, mock_client_cls, monkeypatch, tmp_path):
    """The extra getProfile is the price of a legacy file only — a v2 file
    carries the account it was verified against."""
    _set_full_env(monkeypatch)
    _write_v2(tmp_path)
    mock_creds_cls.return_value = MagicMock()

    make_auth(tmp_path).validate_and_init()
    mock_client_cls.return_value.get_profile_email.assert_not_called()


# ── activate / activation hook ──────────────────────────────────────────────

def _credential(rt="rt-x"):
    from token_store import Credential
    return Credential(refresh_token=rt, flow=None, generation=None, account="a@b.c")


def test_activate_runs_the_activation_hook_after_setting_credentials(
        monkeypatch, tmp_path):
    """server.py hangs the runtime rebuild here so it happens BEFORE any caller
    acks the flow — auth.py itself stays ignorant of what gets rebuilt."""
    _set_full_env(monkeypatch)
    auth = make_auth(tmp_path)
    auth.validate_and_init()
    seen = []
    auth.on_activate = lambda: seen.append(auth.credentials)

    auth.activate(_credential())

    assert len(seen) == 1
    assert seen[0] is auth.credentials       # credential set before the hook ran


def test_activate_propagates_a_failing_hook(monkeypatch, tmp_path):
    """The failure must reach the caller: an activation whose rebuild failed is
    not a successful activation."""
    _set_full_env(monkeypatch)
    auth = make_auth(tmp_path)
    auth.validate_and_init()

    def boom():
        raise RuntimeError("attachment cache unwritable")
    auth.on_activate = boom

    with pytest.raises(RuntimeError, match="unwritable"):
        auth.activate(_credential())


@patch("auth.Credentials")
def test_load_active_reports_a_failing_hook_and_keeps_the_token(
        mock_creds_cls, monkeypatch, tmp_path, capsys):
    _set_full_env(monkeypatch)
    _write_v2(tmp_path)
    mock_creds_cls.return_value = MagicMock()
    auth = make_auth(tmp_path)

    def boom():
        raise RuntimeError("attachment cache unwritable")
    auth.on_activate = boom

    assert auth.validate_and_init() is False
    assert (tmp_path / "oauth_token.json").exists()
    assert "unwritable" in capsys.readouterr().err


# ── refresh_and_verify ──────────────────────────────────────────────────────

@patch("gmail_client.GmailClient")
@patch("auth.Credentials")
def test_refresh_and_verify_returns_profile_email(
        mock_creds_cls, mock_gmail_client_cls, monkeypatch, tmp_path):
    """Proves the composition: refresh, then hand the live credentials to
    GmailClient and return what get_profile_email() reports."""
    _set_full_env(monkeypatch)
    mock_creds_cls.return_value = MagicMock()
    mock_gmail_client_cls.return_value.get_profile_email.return_value = (
        "user@workspace.example.com"
    )

    auth = make_auth(tmp_path)
    auth.validate_and_init()

    assert auth.refresh_and_verify("rt-abc") == "user@workspace.example.com"
    mock_gmail_client_cls.return_value.get_profile_email.assert_called_once()


@patch("auth.Credentials")
def test_refresh_and_verify_propagates_terminal_failure(mock_creds_cls, monkeypatch, tmp_path):
    from auth import RefreshTerminal
    from google.auth.exceptions import RefreshError
    _set_full_env(monkeypatch)
    creds = MagicMock()
    creds.refresh.side_effect = RefreshError("invalid_grant")
    mock_creds_cls.return_value = creds

    auth = make_auth(tmp_path)
    auth.validate_and_init()

    with pytest.raises(RefreshTerminal):
        auth.refresh_and_verify("rt-abc")


@patch("auth.Credentials")
def test_refresh_and_verify_propagates_retryable_failure(mock_creds_cls, monkeypatch, tmp_path):
    from auth import RefreshRetryable
    _set_full_env(monkeypatch)
    creds = MagicMock()
    creds.refresh.side_effect = OSError("connection reset")
    mock_creds_cls.return_value = creds

    auth = make_auth(tmp_path)
    auth.validate_and_init()

    with pytest.raises(RefreshRetryable):
        auth.refresh_and_verify("rt-abc")


# ── A refused CLIENT is not a dead credential ──────────────────────────────
#
# Rotate the OAuth client secret and Google answers `invalid_client`. Vendored
# google-auth marks that non-retryable (`_can_retry` lists only
# internal_failure / server_error / temporarily_unavailable), and treating
# every non-retryable answer as a revocation deleted a perfectly good refresh
# token and offered a re-authorization that could not complete either — the
# code exchange uses the same rejected secret.

def _refresh_error(*args):
    from google.auth.exceptions import RefreshError
    return RefreshError(*args)


@patch("auth.Credentials")
def test_a_rejected_client_is_a_config_error_not_a_terminal_one(
        mock_creds_cls, monkeypatch, tmp_path):
    from auth import RefreshConfigError
    _set_full_env(monkeypatch)
    creds = MagicMock()
    creds.refresh.side_effect = _refresh_error(
        "invalid_client: The OAuth client was not found.",
        {"error": "invalid_client",
         "error_description": "The OAuth client was not found."})
    mock_creds_cls.return_value = creds

    auth = make_auth(tmp_path)
    auth.validate_and_init()

    with pytest.raises(RefreshConfigError):
        auth.probe_refresh("rt-abc")


@patch("auth.Credentials")
def test_an_unclassifiable_non_retryable_refresh_is_not_terminal(
        mock_creds_cls, monkeypatch, tmp_path):
    """google-auth also raises RefreshError before any request — e.g. when the
    credential lacks the fields needed to refresh. Nothing there says the grant
    is dead, and destroying a credential requires positive evidence."""
    from auth import RefreshConfigError
    _set_full_env(monkeypatch)
    creds = MagicMock()
    creds.refresh.side_effect = _refresh_error(
        "The credentials do not contain the necessary fields need to "
        "refresh the access token.")
    mock_creds_cls.return_value = creds

    auth = make_auth(tmp_path)
    auth.validate_and_init()

    with pytest.raises(RefreshConfigError):
        auth.probe_refresh("rt-abc")


@patch("auth.Credentials")
def test_the_parsed_error_body_decides_not_the_message_text(
        mock_creds_cls, monkeypatch, tmp_path):
    """google-auth passes the decoded body as a second argument; it is the
    authoritative source for the code, and the message only a fallback."""
    from auth import RefreshTerminal
    _set_full_env(monkeypatch)
    creds = MagicMock()
    creds.refresh.side_effect = _refresh_error(
        "Bad Request", {"error": "invalid_grant", "error_description": "Bad Request"})
    mock_creds_cls.return_value = creds

    auth = make_auth(tmp_path)
    auth.validate_and_init()

    with pytest.raises(RefreshTerminal):
        auth.probe_refresh("rt-abc")


@patch("auth.Credentials")
def test_load_active_keeps_the_token_when_the_client_is_rejected(
        mock_creds_cls, monkeypatch, tmp_path, capsys):
    """`load_active` is the consumer most at risk from this taxonomy: its
    RefreshTerminal arm DELETES the credential. A rotated client secret must
    not cost the operator their refresh token."""
    _set_full_env(monkeypatch)
    _write_v2(tmp_path)
    creds = MagicMock()
    creds.refresh.side_effect = _refresh_error(
        "invalid_client: Unauthorized", {"error": "invalid_client"})
    mock_creds_cls.return_value = creds

    auth = make_auth(tmp_path)
    assert auth.validate_and_init() is False
    assert (tmp_path / "oauth_token.json").exists(), \
        "a configuration error destroyed a working credential"
    err = capsys.readouterr().err
    assert "OAuth client configuration was rejected" in err
    assert "token kept" in err


@patch("auth.Credentials")
def test_load_active_still_removes_a_genuinely_dead_token(
        mock_creds_cls, monkeypatch, tmp_path):
    """The other half: `invalid_grant` must keep reaping the credential."""
    _set_full_env(monkeypatch)
    _write_v2(tmp_path)
    creds = MagicMock()
    creds.refresh.side_effect = _refresh_error(
        "invalid_grant: Token has been expired or revoked.",
        {"error": "invalid_grant"})
    mock_creds_cls.return_value = creds

    auth = make_auth(tmp_path)
    assert auth.validate_and_init() is False
    assert not (tmp_path / "oauth_token.json").exists()


# ── build_auth_url ──────────────────────────────────────────────────────────

def test_build_auth_url_uses_supplied_redirect_and_state(monkeypatch, tmp_path):
    _set_full_env(monkeypatch)
    auth = make_auth(tmp_path)
    auth.validate_and_init()

    url = auth.build_auth_url("https://casa.example.com/callback/plg-gmail--oauth",
                              "state-xyz")
    assert "accounts.google.com" in url
    assert "client-id" in url
    assert "state-xyz" in url
    assert "casa.example.com%2Fcallback%2Fplg-gmail--oauth" in url
    assert "localhost" not in url
    assert "offline" in url
    assert "gmail.modify" in url


# ── exchange_code ───────────────────────────────────────────────────────────

import urllib.error


def _http_error(status, body=b'{"error":"invalid_grant"}'):
    return urllib.error.HTTPError(
        "https://oauth2.googleapis.com/token", status, "err", {}, io.BytesIO(body))


def _ok_body(**over):
    payload = {"access_token": "at-123", "refresh_token": "rt-new",
               "expires_in": 3600, "token_type": "Bearer"}
    payload.update(over)
    return io.BytesIO(json.dumps(payload).encode())


def _exchange(auth):
    return auth.exchange_code("code-1", "https://casa.example.com/callback/x")


@patch("auth.urllib.request.urlopen")
def test_exchange_code_returns_token_response(mock_urlopen, monkeypatch, tmp_path):
    _set_full_env(monkeypatch)
    mock_urlopen.return_value = _ok_body(refresh_token="rt-persisted")
    auth = make_auth(tmp_path)
    auth.validate_and_init()

    assert _exchange(auth)["refresh_token"] == "rt-persisted"


@patch("auth.urllib.request.urlopen")
def test_exchange_code_writes_nothing_and_does_not_authenticate(
        mock_urlopen, monkeypatch, tmp_path):
    """Persistence and activation are the store's job, not the exchange's."""
    _set_full_env(monkeypatch)
    mock_urlopen.return_value = _ok_body()
    auth = make_auth(tmp_path)
    auth.validate_and_init()

    _exchange(auth)
    assert list(tmp_path.iterdir()) == []
    assert not auth.is_authenticated


@pytest.mark.parametrize("status", [400, 401, 403])
@patch("auth.urllib.request.urlopen")
def test_exchange_code_4xx_is_terminal(mock_urlopen, monkeypatch, tmp_path, status):
    from auth import ExchangeTerminal
    _set_full_env(monkeypatch)
    mock_urlopen.side_effect = _http_error(status)
    auth = make_auth(tmp_path)
    auth.validate_and_init()
    with pytest.raises(ExchangeTerminal):
        _exchange(auth)


@pytest.mark.parametrize("status", [429, 500, 503])
@patch("auth.urllib.request.urlopen")
def test_exchange_code_429_and_5xx_are_retryable(mock_urlopen, monkeypatch, tmp_path, status):
    from auth import ExchangeRetryable
    _set_full_env(monkeypatch)
    mock_urlopen.side_effect = _http_error(status)
    auth = make_auth(tmp_path)
    auth.validate_and_init()
    with pytest.raises(ExchangeRetryable):
        _exchange(auth)


@patch("auth.urllib.request.urlopen")
def test_exchange_code_connection_failure_is_retryable(mock_urlopen, monkeypatch, tmp_path):
    from auth import ExchangeRetryable
    _set_full_env(monkeypatch)
    mock_urlopen.side_effect = urllib.error.URLError("timed out")
    auth = make_auth(tmp_path)
    auth.validate_and_init()
    with pytest.raises(ExchangeRetryable):
        _exchange(auth)


@patch("auth.urllib.request.urlopen")
def test_exchange_code_malformed_error_body_still_terminal_on_4xx(
        mock_urlopen, monkeypatch, tmp_path):
    from auth import ExchangeTerminal
    _set_full_env(monkeypatch)
    mock_urlopen.side_effect = _http_error(400, b"<html>nope</html>")
    auth = make_auth(tmp_path)
    auth.validate_and_init()
    with pytest.raises(ExchangeTerminal):
        _exchange(auth)


@patch("auth.urllib.request.urlopen")
def test_exchange_code_malformed_2xx_body_is_terminal(mock_urlopen, monkeypatch, tmp_path):
    from auth import ExchangeTerminal
    _set_full_env(monkeypatch)
    mock_urlopen.return_value = io.BytesIO(b"<html>not json</html>")
    auth = make_auth(tmp_path)
    auth.validate_and_init()
    with pytest.raises(ExchangeTerminal):
        _exchange(auth)


@pytest.mark.parametrize("missing", ["refresh_token", "access_token"])
@patch("auth.urllib.request.urlopen")
def test_exchange_code_incomplete_2xx_body_is_terminal(
        mock_urlopen, monkeypatch, tmp_path, missing):
    from auth import ExchangeTerminal
    _set_full_env(monkeypatch)
    payload = {"access_token": "at", "refresh_token": "rt", "expires_in": 3600}
    del payload[missing]
    mock_urlopen.return_value = io.BytesIO(json.dumps(payload).encode())
    auth = make_auth(tmp_path)
    auth.validate_and_init()
    with pytest.raises(ExchangeTerminal):
        _exchange(auth)


# ── scopes ──────────────────────────────────────────────────────────────────

def test_scopes_include_settings_basic(monkeypatch, tmp_path):
    _set_full_env(monkeypatch)
    auth = make_auth(tmp_path)
    assert "https://www.googleapis.com/auth/gmail.settings.basic" in auth.SCOPES
