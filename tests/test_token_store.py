# tests/test_token_store.py
import json
import os
import pytest
import stat
from pathlib import Path


def store(tmp_path):
    from token_store import TokenStore
    return TokenStore(str(tmp_path))


def test_load_active_absent_returns_none(tmp_path):
    assert store(tmp_path).load_active() is None


def test_stage_then_load_staged_roundtrips(tmp_path):
    s = store(tmp_path)
    s.stage("rt-1", "a" * 64, 123.5)
    got = s.load_staged()
    assert got.refresh_token == "rt-1"
    assert got.flow == "a" * 64
    assert got.generation == 123.5
    assert got.account is None


def test_stage_normalizes_null_minted_ts_to_zero(tmp_path):
    """Casa legitimately reports minted_ts: null; persisting None would make the
    (generation, flow) supersession tuple incomparable."""
    s = store(tmp_path)
    s.stage("rt-1", "a" * 64, None)
    assert s.load_staged().generation == 0.0


def test_stage_writes_atomically_leaving_no_temp(tmp_path):
    s = store(tmp_path)
    s.stage("rt-1", "a" * 64, 1.0)
    names = [p.name for p in Path(tmp_path).iterdir()]
    assert names == ["oauth_token.staged.json"]


def test_write_fsyncs_the_file_before_replacing_and_the_dir_after(tmp_path, monkeypatch):
    """Casa's ack is a strict-fsync settlement receipt: it treats this store as
    already committed. The order — fsync the data, THEN publish it by rename,
    THEN fsync the directory entry — is the whole guarantee, and a refactor to
    path.write_text() would silently destroy it."""
    import token_store
    real_fsync, real_replace = os.fsync, os.replace
    calls = []

    def spy_fsync(fd):
        kind = "dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"
        calls.append(f"fsync:{kind}")
        return real_fsync(fd)

    def spy_replace(src, dst):
        calls.append("replace")
        return real_replace(src, dst)

    monkeypatch.setattr(token_store.os, "fsync", spy_fsync)
    monkeypatch.setattr(token_store.os, "replace", spy_replace)

    store(tmp_path).write_active(_cred("rt-x", "a" * 64, 1.0, "a@b.c"))

    assert calls == ["fsync:file", "replace", "fsync:dir"]


def test_a_crash_between_temp_write_and_replace_leaves_the_previous_file_intact(
        tmp_path, monkeypatch):
    import token_store
    s = store(tmp_path)
    s.write_active(_cred("rt-old", "a" * 64, 1.0, "old@example.com"))

    def crash(src, dst):
        raise OSError("power cut")
    monkeypatch.setattr(token_store.os, "replace", crash)

    with pytest.raises(OSError):
        s.write_active(_cred("rt-new", "b" * 64, 2.0, "new@example.com"))

    survivor = s.load_active()
    assert survivor.refresh_token == "rt-old"
    assert survivor.account == "old@example.com"


def test_promote_moves_staged_to_active_with_account(tmp_path):
    s = store(tmp_path)
    s.stage("rt-1", "a" * 64, 7.0)
    cred = s.promote("a" * 64, "user@example.com")

    assert cred.account == "user@example.com"
    assert cred.refresh_token == "rt-1"
    assert cred.generation == 7.0
    assert s.load_active().account == "user@example.com"
    assert s.load_staged() is None
    assert not (Path(tmp_path) / "oauth_token.staged.json").exists()


def test_promote_refuses_when_staged_flow_changed(tmp_path):
    """Guards against a slot replaced under a concurrent verifier."""
    from token_store import StagedFlowMismatch
    s = store(tmp_path)
    s.stage("rt-other", "b" * 64, 7.0)
    with pytest.raises(StagedFlowMismatch):
        s.promote("a" * 64, "user@example.com")


def test_promote_refusal_leaves_stage_and_active_untouched(tmp_path):
    from token_store import StagedFlowMismatch
    s = store(tmp_path)
    s.write_active(_cred("rt-old", "c" * 64, 1.0, "old@example.com"))
    s.stage("rt-other", "b" * 64, 7.0)
    with pytest.raises(StagedFlowMismatch):
        s.promote("a" * 64, "user@example.com")

    assert s.load_staged().refresh_token == "rt-other"
    assert s.load_active().refresh_token == "rt-old"


def test_promote_with_no_stage_raises_mismatch(tmp_path):
    from token_store import StagedFlowMismatch
    with pytest.raises(StagedFlowMismatch):
        store(tmp_path).promote("a" * 64, "user@example.com")


def _cred(rt, flow, gen, account):
    from token_store import Credential
    return Credential(refresh_token=rt, flow=flow, generation=gen, account=account)


def test_discard_staged_is_idempotent(tmp_path):
    s = store(tmp_path)
    s.discard_staged()
    s.stage("rt-1", "a" * 64, 1.0)
    s.discard_staged()
    s.discard_staged()
    assert s.load_staged() is None


def test_legacy_v1_file_loads_with_absent_flow_and_generation(tmp_path):
    (Path(tmp_path) / "oauth_token.json").write_text(json.dumps({"refresh_token": "rt-legacy"}))
    got = store(tmp_path).load_active()
    assert got.refresh_token == "rt-legacy"
    assert got.flow is None
    assert got.generation is None
    assert got.account is None


def test_malformed_active_file_loads_as_none(tmp_path):
    (Path(tmp_path) / "oauth_token.json").write_text("{not json")
    assert store(tmp_path).load_active() is None


def test_active_file_without_refresh_token_loads_as_none(tmp_path):
    (Path(tmp_path) / "oauth_token.json").write_text(json.dumps({"v": 2}))
    assert store(tmp_path).load_active() is None


def test_write_active_roundtrips_v2_schema(tmp_path):
    s = store(tmp_path)
    s.write_active(_cred("rt-x", "d" * 64, 42.0, "a@b.c"))
    raw = json.loads((Path(tmp_path) / "oauth_token.json").read_text())
    assert raw["v"] == 2
    assert raw["flow"] == "d" * 64
    assert raw["generation"] == 42.0
    assert "committed_ts" in raw


def test_files_written_with_0o600_permissions(tmp_path):
    """Credential files hold refresh tokens: they must be written 0o600."""
    s = store(tmp_path)
    s.stage("rt-1", "a" * 64, 1.0)
    staged_path = Path(tmp_path) / "oauth_token.staged.json"
    assert stat.S_IMODE(os.stat(staged_path).st_mode) == 0o600

    s.write_active(_cred("rt-x", "b" * 64, 2.0, "a@b.c"))
    active_path = Path(tmp_path) / "oauth_token.json"
    assert stat.S_IMODE(os.stat(active_path).st_mode) == 0o600


def test_remove_active_is_idempotent(tmp_path):
    s = store(tmp_path)
    s.remove_active()
    s.write_active(_cred("rt-x", None, None, "a@b.c"))
    s.remove_active()
    assert s.load_active() is None


# ── Pending notices ────────────────────────────────────────────────────────

def test_queued_notices_survive_a_new_store_instance(tmp_path):
    """The point of the file: the process that queued it is gone."""
    store(tmp_path).queue_notice("first")
    store(tmp_path).queue_notice("second")
    assert store(tmp_path).drain_notices() == ["first", "second"]


def test_draining_removes_the_file_so_nothing_repeats(tmp_path):
    s = store(tmp_path)
    s.queue_notice("only once")
    assert s.drain_notices() == ["only once"]
    assert s.drain_notices() == []
    assert not (Path(tmp_path) / "pending_notices.json").exists()


def test_draining_nothing_is_not_an_error(tmp_path):
    assert store(tmp_path).drain_notices() == []


def test_an_unreadable_notice_file_is_ignored_not_fatal(tmp_path):
    (Path(tmp_path) / "pending_notices.json").write_text("{not json")
    assert store(tmp_path).load_notices() == []
