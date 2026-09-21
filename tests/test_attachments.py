import os
import time

import pytest

import casa_handoff

TOKEN = b'{"refresh_token": "do-not-mail"}'


@pytest.fixture
def handoff(tmp_path, monkeypatch):
    root = tmp_path / "handoff"
    root.mkdir(mode=0o770)
    monkeypatch.setenv(casa_handoff.HANDOFF_ENV, str(root))
    return str(root)


def make_manager(tmp_path):
    from attachments import AttachmentManager
    return AttachmentManager(str(tmp_path / "data"))


def legacy_cache_file(m, msg_id, name, data):
    """A file downloaded before 0.9.0, when downloads went to the cache."""
    d = os.path.join(m._cache_dir, msg_id)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, name)
    with open(path, "wb") as f:
        f.write(data)
    return path


def token_store(m):
    path = os.path.join(m._plugin_data, "gmail_token.json")
    with open(path, "wb") as f:
        f.write(TOKEN)
    return path


# --- read_source: what may be attached or saved (issue #3) ---

def test_a_handoff_file_is_read_under_its_name(tmp_path, handoff):
    m = make_manager(tmp_path)
    out = casa_handoff.publish("accounting", "q3.zip", data=b"PK")
    assert m.read_source(out["path"]) == ("q3.zip", b"PK")


def test_legacy_cache_and_saved_files_are_read(tmp_path, handoff):
    m = make_manager(tmp_path)
    cached = legacy_cache_file(m, "msg1", "invoice.pdf", b"PDF")
    saved = os.path.join(m._saved_dir, "kept.pdf")
    with open(saved, "wb") as f:
        f.write(b"KEPT")
    assert m.read_source(cached) == ("invoice.pdf", b"PDF")
    assert m.read_source(saved) == ("kept.pdf", b"KEPT")


def test_the_token_store_is_never_an_attachment(tmp_path, handoff):
    m = make_manager(tmp_path)
    with pytest.raises(ValueError, match="not a handoff file"):
        m.read_source(token_store(m))


@pytest.mark.parametrize("where", ["cache", "saved"])
def test_a_link_to_the_token_store_is_refused(tmp_path, handoff, where):
    m = make_manager(tmp_path)
    token = token_store(m)
    base = os.path.join(m._cache_dir, "msg1") if where == "cache" else m._saved_dir
    os.makedirs(base, exist_ok=True)
    os.symlink(token, os.path.join(base, "sym.pdf"))
    os.link(token, os.path.join(base, "hard.pdf"))
    for name in ("sym.pdf", "hard.pdf"):
        with pytest.raises(ValueError):
            m.read_source(os.path.join(base, name))


def test_a_link_planted_in_the_handoff_folder_is_refused(tmp_path, handoff):
    m = make_manager(tmp_path)
    d = os.path.join(handoff, "other", casa_handoff.new_id())
    os.makedirs(d)
    os.link(token_store(m), os.path.join(d, "invoice.pdf"))
    with pytest.raises(ValueError, match="cannot be used"):
        m.read_source(os.path.join(d, "invoice.pdf"))


@pytest.mark.parametrize("which", ["_cache_dir", "_saved_dir"])
def test_a_linked_cache_or_saved_directory_does_not_widen_acceptance(tmp_path, handoff, which):
    m = make_manager(tmp_path)
    token_store(m)
    base = getattr(m, which)
    os.rmdir(base)
    os.symlink(m._plugin_data, base)          # the directory now points at the data root
    with pytest.raises(ValueError):
        m.read_source(os.path.join(base, "gmail_token.json"))


@pytest.mark.parametrize("path", ["/etc/passwd", "relative.pdf"])
def test_paths_outside_are_refused(tmp_path, handoff, path):
    m = make_manager(tmp_path)
    with pytest.raises(ValueError):
        m.read_source(path)


# --- save_attachment ---

def test_save_attachment_path_traversal_rejected(tmp_path, handoff):
    m = make_manager(tmp_path)
    with pytest.raises(ValueError, match="escapes plugin data directory"):
        m.validate_save_destination("../../etc/passwd")


def test_save_attachment_from_the_handoff_folder(tmp_path, handoff):
    m = make_manager(tmp_path)
    out = casa_handoff.publish("gmail", "invoice.pdf", data=b"PDF")
    saved = m.save_attachment(out["path"], "invoices/2026-07/test.pdf")
    assert open(saved, "rb").read() == b"PDF"
    assert os.stat(saved).st_ino != os.stat(out["path"]).st_ino


def test_save_attachment_from_a_legacy_cache_file(tmp_path, handoff):
    m = make_manager(tmp_path)
    cached = legacy_cache_file(m, "msg1", "invoice.pdf", b"OLD")
    assert open(m.save_attachment(cached, "old.pdf"), "rb").read() == b"OLD"


def test_save_attachment_rejects_arbitrary_and_saved_sources(tmp_path, handoff):
    m = make_manager(tmp_path)
    with pytest.raises(ValueError):
        m.save_attachment("/etc/passwd", "dest.pdf")
    with pytest.raises(ValueError):
        m.save_attachment(token_store(m), "dest.pdf")
    first = m.save_attachment(casa_handoff.publish("gmail", "a.pdf", data=b"A")["path"], "a.pdf")
    with pytest.raises(ValueError, match="download_attachment"):
        m.save_attachment(first, "b.pdf")


def test_save_attachment_overwrite(tmp_path, handoff):
    m = make_manager(tmp_path)
    one = casa_handoff.publish("gmail", "invoice.pdf", data=b"PDF")["path"]
    two = casa_handoff.publish("gmail", "invoice.pdf", data=b"NEW")["path"]
    m.save_attachment(one, "invoices/test.pdf")
    with pytest.raises(FileExistsError, match="already exists"):
        m.save_attachment(two, "invoices/test.pdf", overwrite=False)
    saved = m.save_attachment(two, "invoices/test.pdf", overwrite=True)
    assert open(saved, "rb").read() == b"NEW"


# --- TTL cleanup of the legacy cache ---

def test_ttl_cleanup_removes_old_files(tmp_path, handoff):
    m = make_manager(tmp_path)
    path = legacy_cache_file(m, "msg1", "old.pdf", b"x")
    old_mtime = time.time() - (8 * 24 * 3600 + 61)
    os.utime(path, (old_mtime, old_mtime))
    m._run_cleanup()
    assert not os.path.exists(path)


def test_ttl_cleanup_keeps_recent_files(tmp_path, handoff):
    m = make_manager(tmp_path)
    path = legacy_cache_file(m, "msg1", "new.pdf", b"x")
    m._run_cleanup()
    assert os.path.exists(path)


def test_ttl_cleanup_respects_race_guard(tmp_path, handoff):
    m = make_manager(tmp_path)
    path = legacy_cache_file(m, "msg1", "borderline.pdf", b"x")
    borderline_mtime = time.time() - (7 * 24 * 3600 + 30)
    os.utime(path, (borderline_mtime, borderline_mtime))
    m._run_cleanup()
    assert os.path.exists(path)
