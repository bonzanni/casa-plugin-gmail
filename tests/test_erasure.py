"""The two erasers Casa runs at uninstall (casa.eraseTool, casa.eraseDataOnlyTool).

"complete" lets Casa remove the plugin, so every test below asserts what is
left on disk and what Google was asked, not only the verdict.
"""
import inspect
import io
import json
import urllib.error

import pytest

import erasure
from auth_flow import LOCK_NAME, collect_lock
from token_store import Credential, TokenStore


class _Google:
    """The revoke endpoint: records each token, answers per `answers`."""

    def __init__(self, default=("revoked", "")):
        self.asked = []
        self.answers = {}
        self.default = default

    def __call__(self, token):
        self.asked.append(token)
        return self.answers.get(token, self.default)


def _populated(tmp_path, *, staged=False):
    store = TokenStore(str(tmp_path))
    store.write_active(Credential("rt-active", "f1", 1.0, "user@example.com"))
    if staged:
        store.stage("rt-staged", "f2", 2.0)
    store.queue_notice("k", "a notice")
    (tmp_path / "sent_log.json").write_text("{}")
    (tmp_path / "saved" / "tax").mkdir(parents=True)
    (tmp_path / "saved" / "tax" / "invoice.pdf").write_bytes(b"%PDF")
    (tmp_path / "attachments" / "cache").mkdir(parents=True)
    (tmp_path / "sent_log.json.corrupt.1").write_text("x")
    return store


def _names(path):
    return sorted(p.name for p in path.iterdir())


def test_everything_revokes_every_grant_then_leaves_nothing(tmp_path):
    store = _populated(tmp_path, staged=True)
    google = _Google()
    result = erasure.erase(store, keep_sign_in=False, revoke=google)
    assert result["erasure"] == "complete"
    assert sorted(google.asked) == ["rt-active", "rt-staged"]
    assert _names(tmp_path) == []
    assert "Revoked" in result["report"]


def test_a_token_google_already_invalidated_counts_as_revoked(tmp_path):
    store = _populated(tmp_path)
    result = erasure.erase(store, keep_sign_in=False,
                           revoke=_Google(("already_invalid", "")))
    assert result["erasure"] == "complete"
    assert _names(tmp_path) == []


def test_an_unconfirmed_revocation_keeps_that_token_and_is_incomplete(tmp_path):
    store = _populated(tmp_path, staged=True)
    google = _Google()
    google.answers["rt-active"] = ("failed", "could not reach Google (boom)")
    result = erasure.erase(store, keep_sign_in=False, revoke=google)
    assert result["erasure"] == "incomplete"
    # The unrevoked grant's file stays so a second run can still revoke it;
    # the confirmed one and all the data are gone.
    assert _names(tmp_path) == [LOCK_NAME, "oauth_token.json"]
    assert "could not reach Google (boom)" in result["report"]
    assert erasure.PERMISSIONS_PAGE in result["report"]
    # A second run with Google reachable finishes the job.
    again = erasure.erase(store, keep_sign_in=False, revoke=_Google())
    assert again["erasure"] == "complete"
    assert _names(tmp_path) == []


def test_one_token_in_both_slots_is_revoked_once(tmp_path):
    store = TokenStore(str(tmp_path))
    store.write_active(Credential("rt", "f1", 1.0, "user@example.com"))
    store.stage("rt", "f1", 1.0)
    google = _Google()
    assert erasure.erase(store, keep_sign_in=False, revoke=google)["erasure"] == "complete"
    assert google.asked == ["rt"]


def test_an_unreadable_token_file_is_deleted_without_asking_google(tmp_path):
    (tmp_path / "oauth_token.json").write_text("not json")
    google = _Google()
    result = erasure.erase(TokenStore(str(tmp_path)), keep_sign_in=False, revoke=google)
    assert result["erasure"] == "complete"
    assert google.asked == []
    assert _names(tmp_path) == []


def test_data_only_keeps_exactly_the_sign_in_and_asks_google_nothing(tmp_path):
    store = _populated(tmp_path, staged=True)
    google = _Google()
    result = erasure.erase(store, keep_sign_in=True, revoke=google)
    assert result["erasure"] == "complete"
    assert google.asked == []
    assert _names(tmp_path) == sorted([LOCK_NAME, "oauth_token.json",
                                       "oauth_token.staged.json",
                                       "pending_notices.json"])
    assert store.load_active().refresh_token == "rt-active"


def test_a_file_a_later_version_adds_is_erased_by_default(tmp_path):
    store = _populated(tmp_path)
    (tmp_path / "something_new.db").write_text("personal")
    for keep in (True, False):
        erasure.erase(store, keep_sign_in=keep, revoke=_Google())
        assert not (tmp_path / "something_new.db").exists()


def test_a_symlink_is_removed_not_followed(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("not ours")
    data = tmp_path / "data"
    data.mkdir()
    (data / "saved").symlink_to(outside)
    result = erasure.erase(TokenStore(str(data)), keep_sign_in=True, revoke=_Google())
    assert result["erasure"] == "complete"
    assert (outside / "keep.txt").exists()
    assert not (data / "saved").exists()


def test_a_held_collect_lock_erases_nothing(tmp_path):
    store = _populated(tmp_path)
    google = _Google()
    ran = []
    with collect_lock(tmp_path) as held:
        assert held
        before = _names(tmp_path)
        result = erasure.erase(store, keep_sign_in=False, revoke=google,
                               before_delete=lambda: ran.append(1))
    assert result["erasure"] == "incomplete"
    assert google.asked == [] and ran == []
    assert _names(tmp_path) == before


def test_a_deletion_that_fails_is_incomplete_and_named(tmp_path, monkeypatch):
    store = _populated(tmp_path)
    real = erasure._remove

    def refuse_saved(path):
        if path.name == "saved":
            raise PermissionError(13, "Permission denied")
        real(path)

    monkeypatch.setattr(erasure, "_remove", refuse_saved)
    result = erasure.erase(store, keep_sign_in=True, revoke=_Google())
    assert result["erasure"] == "incomplete"
    assert "saved (Permission denied)" in result["report"]


def test_a_missing_data_directory_is_nothing_to_erase(tmp_path):
    result = erasure.erase(TokenStore(str(tmp_path / "never")), keep_sign_in=False,
                           revoke=_Google())
    assert result["erasure"] == "complete"
    assert not (tmp_path / "never").exists()


# ── revoke_token against Google's documented answers ─────────────────────

class _Resp:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(code, body):
    return urllib.error.HTTPError(erasure.REVOKE_URI, code, "x", {},
                                  io.BytesIO(json.dumps(body).encode()))


@pytest.mark.parametrize("raised, returned, outcome", [
    (None, 200, "revoked"),
    (_http_error(400, {"error": "invalid_token"}), None, "already_invalid"),
    (_http_error(400, {"error": "invalid_request"}), None, "failed"),
    (_http_error(503, {"error": "backend_error"}), None, "failed"),
    (urllib.error.URLError("no route"), None, "failed"),
    (TimeoutError("timed out"), None, "failed"),
])
def test_revoke_token_confirms_only_what_google_confirmed(monkeypatch, raised, returned, outcome):
    seen = {}

    def fake_urlopen(req, timeout):
        seen["req"] = req
        if raised is not None:
            raise raised
        return _Resp(returned)

    monkeypatch.setattr(erasure.urllib.request, "urlopen", fake_urlopen)
    assert erasure.revoke_token("rt-1")[0] == outcome
    req = seen["req"]
    assert req.full_url == "https://oauth2.googleapis.com/revoke"
    assert req.get_method() == "POST"
    assert req.data == b"token=rt-1"


# ── the served tools and the manifest ────────────────────────────────────

def _manifest():
    from pathlib import Path
    return json.loads((Path(__file__).parent.parent / ".claude-plugin"
                       / "plugin.json").read_text())


def test_the_manifest_declares_both_erasers_safe_and_protected():
    casa = _manifest()["casa"]
    assert casa["eraseTool"] == "erase_gmail"
    assert casa["eraseDataOnlyTool"] == "erase_gmail_data"
    for name in ("erase_gmail", "erase_gmail_data"):
        assert casa["resultContract"]["tools"][name] == {"result": "safe"}
        assert name in [t["name"] for t in casa["protectedTools"]]


def test_the_erasers_are_argument_free():
    import server
    assert inspect.signature(server.erase_gmail).parameters == {}
    assert inspect.signature(server.erase_gmail_data).parameters == {}


def _server_on(monkeypatch, tmp_path):
    import server
    from auth import GmailAuth
    from sent_log import SentLog
    auth = GmailAuth(str(tmp_path))
    auth._credentials = object()
    monkeypatch.setattr(server, "_auth", auth)
    monkeypatch.setattr(server, "_client", object())
    monkeypatch.setattr(server, "_authenticated", True)
    log = SentLog.__new__(SentLog)          # no cleanup timer thread
    import threading
    log._path, log._lock, log._data = str(tmp_path / "sent_log.json"), threading.Lock(), {}
    log.record("r1", "m1", "a@example.com", "hello")
    monkeypatch.setattr(server, "_log", log)
    return server, auth, log


def test_erasing_everything_signs_the_running_server_out(monkeypatch, tmp_path):
    server, auth, log = _server_on(monkeypatch, tmp_path)
    auth.store.write_active(Credential("rt", "f", 1.0, "user@example.com"))
    # erase() binds its default at definition time; route through the fake.
    real = erasure.erase
    monkeypatch.setattr(erasure, "erase",
                        lambda store, **kw: real(store, revoke=_Google(), **kw))
    out = json.loads(server.erase_gmail())
    assert out["erasure"] == "complete"
    assert server._authenticated is False and server._client is None
    assert auth.credentials is None
    with pytest.raises(ValueError, match="not authenticated"):
        server.search_emails("x")


def test_erasing_data_forgets_the_sent_log_in_memory_too(monkeypatch, tmp_path):
    server, auth, log = _server_on(monkeypatch, tmp_path)
    auth.store.write_active(Credential("rt", "f", 1.0, "user@example.com"))
    out = json.loads(server.erase_gmail_data())
    assert out["erasure"] == "complete"
    assert server._authenticated is True
    # A later send must not write the erased entries back.
    log.record("r2", "m2", "b@example.com", "later")
    assert set(json.loads((tmp_path / "sent_log.json").read_text())) == {"r2"}
    assert log.check("r1", "a@example.com", "hello") is None
