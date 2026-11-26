import fcntl
import json
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path

from token_store import _durable_write

RETENTION_DAYS = 90
CLEANUP_INTERVAL_SECONDS = 24 * 3600

LOCK_NAME = "sent_log.lock"


class _Held:
    """One lock file as this process holds it: the thread that has it, how
    many times over, and the single descriptor the file lock is on."""

    def __init__(self):
        self.thread = threading.RLock()
        self.depth = 0
        self.fd = None


_held: dict[str, _Held] = {}
_held_guard = threading.Lock()


@contextmanager
def log_lock(path: str):
    """Hold the lock that guards the sent log at `path`, waiting for it.

    Several server processes share one data folder, and each of them reads and
    rewrites the same log; so does the daily cleanup, on a second thread of
    each. The lock is `LOCK_NAME` beside the log, and it is two locks in one,
    always taken in this order: a per-thread one, then the file lock. The
    file lock is held on one descriptor per process, because a second open of
    the same file would wait for the first even inside one process.

    The thread that holds it may take it again (an erase holds it across the
    walk and calls `forget` inside); any other thread, and any other process,
    waits. A process that dies while holding it frees it. It guards file
    operations only: no network call runs under it.

    Whoever also needs the collect lock takes that one first.
    """
    directory = os.path.realpath(os.path.dirname(os.path.abspath(path)))
    lock_path = os.path.join(directory, LOCK_NAME)
    with _held_guard:
        held = _held.setdefault(lock_path, _Held())
    with held.thread:
        if held.depth == 0:
            os.makedirs(directory, exist_ok=True)
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            except BaseException:
                os.close(fd)
                raise
            held.fd = fd
        held.depth += 1
        try:
            yield
        finally:
            held.depth -= 1
            if held.depth == 0:
                fd, held.fd = held.fd, None
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)


def _address_set(name: str, value: str) -> list[str]:
    """The addresses of an address list as a sorted, de-duplicated key: display
    names dropped, domains lower-cased (local parts can be case-sensitive)."""
    if not value:
        return []
    from gmail_client import _parse_address_list
    out = set()
    for _, addr in _parse_address_list(name, value):
        local, _, domain = addr.rpartition("@")
        out.add(f"{local}@{domain.lower()}")
    return sorted(out)


class SentLog:
    """The record of what was sent, kept in one JSON file.

    Every method that reads or writes the file does so inside `log_lock`, and
    takes no other lock. `_load` and `_save` leave the locking to their caller.
    """

    def __init__(self, path: str):
        self._path = path
        with log_lock(path):
            self._data = self._load()
        self._schedule_cleanup()

    def _load(self) -> dict:
        if not os.path.exists(self._path):
            return {}
        try:
            with open(self._path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            corrupt_path = f"{self._path}.corrupt.{int(time.time())}"
            print(f"WARNING: sent_log.json corrupt ({exc}). Renaming to {os.path.basename(corrupt_path)}", flush=True)
            try:
                os.rename(self._path, corrupt_path)
            except OSError:
                pass
            return {}

    def _save(self):
        # Never a write in place: a reader, or a crash, would meet half a file.
        _durable_write(Path(self._path), self._data)

    def check(self, request_id: str, to: str, subject: str, cc: str = "", bcc: str = "") -> str | None:
        """Return message_id if this exact (request_id, to, subject, cc and bcc
        address sets) was already sent, else None. A changed cc or bcc is a new
        send; an entry from before cc/bcc were recorded matches only none."""
        key = (_address_set("cc", cc), _address_set("bcc", bcc))
        with log_lock(self._path):
            self._data = self._load()
            entry = self._data.get(request_id)
            if (entry and entry.get("to") == to and entry.get("subject") == subject
                    and (entry.get("cc", []), entry.get("bcc", [])) == key):
                return entry["message_id"]
            return None

    def record(self, request_id: str, message_id: str, to: str, subject: str,
               cc: str = "", bcc: str = ""):
        cc_set, bcc_set = _address_set("cc", cc), _address_set("bcc", bcc)
        with log_lock(self._path):
            # The file is the record, not this process's copy: another server
            # process, or an erase, may have changed it since this one read it,
            # and writing a stale copy back would restore what was erased.
            self._data = self._load()
            existing = self._data.get(request_id)
            if existing and (existing.get("to") != to or existing.get("subject") != subject
                             or existing.get("cc", []) != cc_set or existing.get("bcc", []) != bcc_set):
                print(f"WARNING: request_id '{request_id}' collision — proceeding with new send.", flush=True)
            self._data[request_id] = {
                "message_id": message_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "to": to,
                "subject": subject,
                "cc": cc_set,
                "bcc": bcc_set,
            }
            self._save()

    def forget(self):
        """Drop every record, in memory and on disk. Holding the lock keeps a
        concurrent record() or cleanup(), here or in another server process,
        from writing the old entries back. The lock file itself stays."""
        with log_lock(self._path):
            self._data = {}
            try:
                os.unlink(self._path)
            except FileNotFoundError:
                pass

    def cleanup(self):
        cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
        with log_lock(self._path):
            self._data = self._load()
            before = len(self._data)
            self._data = {
                k: v for k, v in self._data.items()
                if datetime.fromisoformat(v["timestamp"]) > cutoff
            }
            if len(self._data) < before:
                self._save()

    def _cleanup_loop(self):
        self.cleanup()
        t = threading.Timer(CLEANUP_INTERVAL_SECONDS, self._cleanup_loop)
        t.daemon = True
        t.start()

    def _schedule_cleanup(self):
        t = threading.Timer(CLEANUP_INTERVAL_SECONDS, self._cleanup_loop)
        t.daemon = True
        t.start()
