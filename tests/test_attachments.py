import os
import time
import pytest


def make_manager(tmp_path):
    from attachments import AttachmentManager
    return AttachmentManager(str(tmp_path))


# --- Filename sanitisation ---

def test_sanitize_strips_forward_slash(tmp_path):
    m = make_manager(tmp_path)
    assert "/" not in m.sanitize_filename("../../etc/passwd", "id1")

def test_sanitize_strips_backslash(tmp_path):
    m = make_manager(tmp_path)
    assert "\\" not in m.sanitize_filename("..\\windows\\file.exe", "id1")

def test_sanitize_strips_null_bytes(tmp_path):
    m = make_manager(tmp_path)
    assert "\x00" not in m.sanitize_filename("file\x00name.pdf", "id1")

def test_sanitize_strips_leading_dots(tmp_path):
    m = make_manager(tmp_path)
    assert not m.sanitize_filename(".bashrc", "id1").startswith(".")

def test_sanitize_empty_result_uses_attachment_id(tmp_path):
    m = make_manager(tmp_path)
    assert m.sanitize_filename("///", "myid") == "attachment_myid"

def test_sanitize_preserves_unicode(tmp_path):
    m = make_manager(tmp_path)
    assert m.sanitize_filename("invoice-日本.pdf", "id1") == "invoice-日本.pdf"


# --- Unique path generation ---

def test_get_unique_path_no_conflict(tmp_path):
    m = make_manager(tmp_path)
    path = m.get_unique_path(str(tmp_path), "file.pdf")
    assert path == str(tmp_path / "file.pdf")

def test_get_unique_path_with_conflict(tmp_path):
    m = make_manager(tmp_path)
    (tmp_path / "file.pdf").write_bytes(b"x")
    path = m.get_unique_path(str(tmp_path), "file.pdf")
    assert path == str(tmp_path / "file_1.pdf")

def test_get_unique_path_multiple_conflicts(tmp_path):
    m = make_manager(tmp_path)
    (tmp_path / "file.pdf").write_bytes(b"x")
    (tmp_path / "file_1.pdf").write_bytes(b"x")
    path = m.get_unique_path(str(tmp_path), "file.pdf")
    assert path == str(tmp_path / "file_2.pdf")


# --- Cache save / download ---

def test_save_to_cache_creates_file(tmp_path):
    m = make_manager(tmp_path)
    path = m.save_to_cache("msg1", "invoice.pdf", b"PDF content")
    assert os.path.exists(path)
    assert open(path, "rb").read() == b"PDF content"

def test_save_to_cache_creates_message_subdir(tmp_path):
    m = make_manager(tmp_path)
    path = m.save_to_cache("msg1", "file.pdf", b"x")
    assert "msg1" in path

def test_save_to_cache_rejects_traversal_filename(tmp_path):
    m = make_manager(tmp_path)
    with pytest.raises(ValueError, match="pre-sanitized"):
        m.save_to_cache("msg1", "../../etc/passwd", b"x")

def test_save_to_cache_rejects_dot_prefix_filename(tmp_path):
    m = make_manager(tmp_path)
    with pytest.raises(ValueError, match="pre-sanitized"):
        m.save_to_cache("msg1", ".hidden", b"x")


# --- save_attachment path confinement ---

def test_save_attachment_path_traversal_rejected(tmp_path):
    m = make_manager(tmp_path)
    cached = m.save_to_cache("msg1", "file.pdf", b"x")
    with pytest.raises(ValueError, match="escapes plugin data directory"):
        m.validate_save_destination("../../etc/passwd")

def test_save_attachment_rejects_arbitrary_cached_path(tmp_path):
    m = make_manager(tmp_path)
    with pytest.raises(ValueError, match="must be in plugin cache directory"):
        m.save_attachment("/etc/passwd", "dest.pdf")

def test_save_attachment_ok(tmp_path):
    m = make_manager(tmp_path)
    cached = m.save_to_cache("msg1", "invoice.pdf", b"PDF")
    saved = m.save_attachment(cached, "invoices/2026-07/test.pdf")
    assert os.path.exists(saved)
    assert open(saved, "rb").read() == b"PDF"

def test_save_attachment_overwrite_false_raises(tmp_path):
    m = make_manager(tmp_path)
    cached = m.save_to_cache("msg1", "invoice.pdf", b"PDF")
    m.save_attachment(cached, "invoices/test.pdf")
    cached2 = m.save_to_cache("msg1", "invoice.pdf", b"PDF2")
    with pytest.raises(FileExistsError, match="already exists"):
        m.save_attachment(cached2, "invoices/test.pdf", overwrite=False)

def test_save_attachment_overwrite_true_replaces(tmp_path):
    m = make_manager(tmp_path)
    cached = m.save_to_cache("msg1", "invoice.pdf", b"PDF")
    m.save_attachment(cached, "invoices/test.pdf")
    cached2 = m.save_to_cache("msg1", "invoice.pdf", b"NEW")
    saved = m.save_attachment(cached2, "invoices/test.pdf", overwrite=True)
    assert open(saved, "rb").read() == b"NEW"


# --- TTL cleanup ---

def test_ttl_cleanup_removes_old_files(tmp_path):
    m = make_manager(tmp_path)
    path = m.save_to_cache("msg1", "old.pdf", b"x")
    # Fake mtime to 8 days ago + 61 sec grace
    old_mtime = time.time() - (8 * 24 * 3600 + 61)
    os.utime(path, (old_mtime, old_mtime))
    m._run_cleanup()
    assert not os.path.exists(path)

def test_ttl_cleanup_keeps_recent_files(tmp_path):
    m = make_manager(tmp_path)
    path = m.save_to_cache("msg1", "new.pdf", b"x")
    m._run_cleanup()
    assert os.path.exists(path)

def test_ttl_cleanup_respects_race_guard(tmp_path):
    m = make_manager(tmp_path)
    path = m.save_to_cache("msg1", "borderline.pdf", b"x")
    # Just over 7 days but within 60s race guard
    borderline_mtime = time.time() - (7 * 24 * 3600 + 30)
    os.utime(path, (borderline_mtime, borderline_mtime))
    m._run_cleanup()
    assert os.path.exists(path)  # still protected by race guard
