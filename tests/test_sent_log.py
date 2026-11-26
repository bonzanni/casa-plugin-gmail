import fcntl
import json
import multiprocessing
import os
import signal
import threading
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


# --- several server processes share one log ---
# Real child processes (started fresh, sharing nothing but the data folder):
# a file lock is per open file, and the defects are between processes.

_SPAWN = multiprocessing.get_context("spawn")
# The upper bound on any wait below; a healthy run uses a fraction of it.
_WAIT = 30.0


def _record_many(path, prefix, count, ready, go):
    from sent_log import SentLog
    log = SentLog(path)
    ready.set()
    if not go.wait(_WAIT):
        raise SystemExit(2)
    for i in range(count):
        log.record(f"{prefix}-{i}", f"msg-{prefix}-{i}", "a@b.com", "Hello")


def _record_until_stopped(path, recorded, stop, result):
    """Record w-0, w-1, ... until `stop` is set, setting `recorded` after each
    one, then send how many were recorded. An exception ends the process with
    its traceback and a non-zero exit code instead."""
    from sent_log import SentLog
    log = SentLog(path)
    count = 0
    deadline = time.monotonic() + 2 * _WAIT
    while not stop.is_set() and time.monotonic() < deadline:
        log.record(f"w-{count}", f"msg-{count}", "a@b.com", "Hello")
        count += 1
        recorded.set()
    result.send(count)


def _hold_the_lock(path, held, release):
    from sent_log import log_lock
    with log_lock(path):
        held.set()
        release.wait(2 * _WAIT)


def _take_the_lock(path, about_to, taken):
    from sent_log import log_lock
    about_to.set()
    with log_lock(path):
        taken.set()


def _wait_for(event, child):
    """Wait until the child sets `event`; fail at once if it died instead."""
    deadline = time.monotonic() + _WAIT
    while not event.wait(0.05):
        assert child.is_alive(), f"the child exited with {child.exitcode}"
        assert time.monotonic() < deadline, "the child never got there"


def _finishes_within(seconds, call):
    """Whether `call` returns in time, run aside so that a call stuck on a
    lock fails its test rather than hanging the run."""
    done = threading.Event()

    def run():
        call()
        done.set()

    threading.Thread(target=run, daemon=True).start()
    return done.wait(seconds)


def _end(*children):
    """No child outlives its test, whichever way the test ends."""
    for child in children:
        if child.pid is None:       # never started
            continue
        if child.is_alive():
            child.kill()
        child.join(_WAIT)


def test_two_processes_recording_at_once_both_keep_their_entry(tmp_path):
    from sent_log import SentLog
    path = str(tmp_path / "sent_log.json")
    go = _SPAWN.Event()
    ready = {name: _SPAWN.Event() for name in ("a", "b")}
    children = [_SPAWN.Process(target=_record_many, args=(path, name, 50, ready[name], go))
                for name in ready]
    try:
        for child in children:
            child.start()
        for child, event in zip(children, ready.values()):
            _wait_for(event, child)
        go.set()
        for child in children:
            child.join(_WAIT)
            assert child.exitcode == 0
    finally:
        _end(*children)
    log = SentLog(path)
    missing = [f"{name}-{i}" for name in ready for i in range(50)
               if log.check(f"{name}-{i}", "a@b.com", "Hello") != f"msg-{name}-{i}"]
    assert missing == []


def test_a_reader_never_sees_half_a_file(tmp_path):
    log = make_log(tmp_path)
    recorded, stop = _SPAWN.Event(), _SPAWN.Event()
    result, sender = _SPAWN.Pipe(duplex=False)
    writer = _SPAWN.Process(target=_record_until_stopped,
                            args=(str(tmp_path / "sent_log.json"), recorded, stop, sender))
    try:
        writer.start()
        _wait_for(recorded, writer)
        for _ in range(500):
            log.check("w-0", "a@b.com", "Hello")
        stop.set()
        assert result.poll(_WAIT)
        count = result.recv()
        writer.join(_WAIT)
        assert writer.exitcode == 0
    finally:
        stop.set()
        _end(writer)
    assert [f for f in os.listdir(tmp_path) if ".corrupt." in f] == []
    on_disk = json.loads((tmp_path / "sent_log.json").read_text())
    assert set(on_disk) == {f"w-{i}" for i in range(count)}


def test_a_lock_held_by_a_dead_process_is_free(tmp_path):
    import sent_log
    log = make_log(tmp_path)
    held, release = _SPAWN.Event(), _SPAWN.Event()
    holder = _SPAWN.Process(target=_hold_the_lock,
                            args=(str(tmp_path / "sent_log.json"), held, release))
    try:
        holder.start()
        _wait_for(held, holder)
        # The child really holds it: another open of the lock file is refused.
        fd = os.open(tmp_path / sent_log.LOCK_NAME, os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)
        os.kill(holder.pid, signal.SIGKILL)
        holder.join(_WAIT)
        assert holder.exitcode == -signal.SIGKILL
    finally:
        _end(holder)
    assert _finishes_within(1.0, lambda: log.record("req-1", "msg-abc", "a@b.com", "Hello"))
    assert log.check("req-1", "a@b.com", "Hello") == "msg-abc"


def test_a_write_that_raises_leaves_the_lock_free(tmp_path, monkeypatch):
    """A write that fails must not leave this process, which lives on, holding
    the lock: every other server process would wait on it for good."""
    import sent_log
    log = make_log(tmp_path)
    path = str(tmp_path / "sent_log.json")

    def disk_full(*_):
        raise OSError("the disk is full")

    monkeypatch.setattr(sent_log, "_durable_write", disk_full)
    with pytest.raises(OSError, match="the disk is full"):
        log.record("req-1", "msg-abc", "a@b.com", "Hello")

    # Another process takes the lock as soon as it asks for it.
    about_to, taken = _SPAWN.Event(), _SPAWN.Event()
    other = _SPAWN.Process(target=_take_the_lock, args=(path, about_to, taken))
    try:
        other.start()
        _wait_for(about_to, other)
        assert taken.wait(5.0)
        other.join(_WAIT)
        assert other.exitcode == 0
    finally:
        _end(other)

    # And so does another thread of this one.
    def take():
        with sent_log.log_lock(path):
            pass

    assert _finishes_within(1.0, take)


def test_a_write_replaces_the_file_and_never_rewrites_it_in_place(tmp_path):
    log = make_log(tmp_path)
    log.record("req-1", "msg-1", "a@b.com", "Hello")
    with open(tmp_path / "sent_log.json") as opened_before_the_write:
        log.record("req-2", "msg-2", "a@b.com", "Hello")
        # Whoever had the file open still reads the whole earlier log.
        assert set(json.load(opened_before_the_write)) == {"req-1"}
    assert set(json.loads((tmp_path / "sent_log.json").read_text())) == {"req-1", "req-2"}
    assert sorted(os.listdir(tmp_path)) == ["sent_log.json", "sent_log.lock"]


def test_one_folder_spelled_two_ways_is_one_lock(tmp_path):
    """The lock is the folder's, however the path to it is written, a link to
    the folder included: a thread that holds it under one spelling takes it
    again under another."""
    from sent_log import log_lock
    data = tmp_path / "data"
    (data / "saved").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(data, target_is_directory=True)

    def nested():
        with log_lock(str(data / "sent_log.json")):
            with log_lock(f"{data}//saved/../sent_log.json"):
                with log_lock(str(link / "sent_log.json")):
                    pass

    assert _finishes_within(1.0, nested)
