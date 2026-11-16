import json
import os
import time
import pytest


def make_log(tmp_path):
    from sent_log import SentLog
    return SentLog(str(tmp_path / "sent_log.json"))


def test_empty_log_returns_none(tmp_path):
    log = make_log(tmp_path)
    assert log.check("req-1", "a@b.com", "Hello") is None


def test_record_and_retrieve(tmp_path):
    log = make_log(tmp_path)
    log.record("req-1", "msg-abc", "a@b.com", "Hello")
    assert log.check("req-1", "a@b.com", "Hello") == "msg-abc"


def test_same_request_id_different_recipient_returns_none(tmp_path):
    log = make_log(tmp_path)
    log.record("req-1", "msg-abc", "a@b.com", "Hello")
    # hash collision: same request_id, different to → not a duplicate
    assert log.check("req-1", "other@b.com", "Hello") is None


def test_same_request_id_different_subject_returns_none(tmp_path):
    log = make_log(tmp_path)
    log.record("req-1", "msg-abc", "a@b.com", "Hello")
    assert log.check("req-1", "a@b.com", "Different subject") is None


def test_corrupt_json_creates_fresh_log(tmp_path):
    log_path = tmp_path / "sent_log.json"
    log_path.write_text("{ invalid json ")
    from sent_log import SentLog
    log = SentLog(str(log_path))
    # Corrupt file renamed, fresh log created
    corrupt_files = [f for f in os.listdir(tmp_path) if "corrupt" in f]
    assert len(corrupt_files) == 1
    assert log.check("req-1", "a@b.com", "Hello") is None


def test_cleanup_removes_old_entries(tmp_path):
    log = make_log(tmp_path)
    from datetime import datetime, timezone, timedelta
    old_time = (datetime.now(timezone.utc) - timedelta(days=91)).isoformat()
    log_path = tmp_path / "sent_log.json"
    log_path.write_text(json.dumps({
        "req-old": {"message_id": "old-id", "timestamp": old_time, "to": "a@b.com", "subject": "Old"},
        "req-new": {"message_id": "new-id", "timestamp": datetime.now(timezone.utc).isoformat(), "to": "b@b.com", "subject": "New"},
    }))
    from sent_log import SentLog
    log = SentLog(str(log_path))
    log.cleanup()
    assert log.check("req-old", "a@b.com", "Old") is None
    assert log.check("req-new", "b@b.com", "New") == "new-id"


def test_persistence_across_instances(tmp_path):
    log1 = make_log(tmp_path)
    log1.record("req-1", "msg-abc", "a@b.com", "Hello")
    from sent_log import SentLog
    log2 = SentLog(str(tmp_path / "sent_log.json"))
    assert log2.check("req-1", "a@b.com", "Hello") == "msg-abc"



# --- cc and bcc are part of the key (post-cap fix) ---

def test_cc_and_bcc_are_compared_as_address_sets(tmp_path):
    log = make_log(tmp_path)
    log.record("r", "m", "a@b.com", "S", cc="Bob <b@x.com>, c@Y.org", bcc="h@x.com")
    assert log.check("r", "a@b.com", "S", cc="c@y.org, b@x.com", bcc="H <h@X.com>") == "m"
    assert log.check("r", "a@b.com", "S", cc="b@x.com", bcc="h@x.com") is None
    assert log.check("r", "a@b.com", "S", cc="b@x.com, c@y.org") is None
    assert log.check("r", "a@b.com", "S", cc="B@x.com, c@y.org", bcc="h@x.com") is None  # local part keeps case


def test_record_stores_the_address_sets(tmp_path):
    log = make_log(tmp_path)
    log.record("r", "m", "a@b.com", "S", cc="Z <z@X.com>, a@x.com")
    entry = json.loads((tmp_path / "sent_log.json").read_text())["r"]
    assert entry["cc"] == ["a@x.com", "z@x.com"] and entry["bcc"] == []


def test_a_legacy_entry_matches_only_empty_cc_and_bcc(tmp_path):
    (tmp_path / "sent_log.json").write_text(json.dumps({"r": {
        "message_id": "m", "timestamp": "2026-11-15T10:00:00+00:00", "to": "a@b.com", "subject": "S"}}))
    log = make_log(tmp_path)
    assert log.check("r", "a@b.com", "S") == "m"
    assert log.check("r", "a@b.com", "S", cc="c@x.com") is None
    assert log.check("r", "a@b.com", "S", bcc="c@x.com") is None
