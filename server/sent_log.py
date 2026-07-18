import json
import os
import threading
import time
from datetime import datetime, timezone, timedelta

RETENTION_DAYS = 90
CLEANUP_INTERVAL_SECONDS = 24 * 3600


class SentLog:
    def __init__(self, path: str):
        self._path = path
        self._lock = threading.Lock()
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
        with open(self._path, "w") as f:
            json.dump(self._data, f, indent=2)

    def check(self, request_id: str, to: str, subject: str) -> str | None:
        """Return message_id if this exact (request_id, to, subject) was already sent, else None."""
        with self._lock:
            entry = self._data.get(request_id)
            if entry and entry.get("to") == to and entry.get("subject") == subject:
                return entry["message_id"]
            return None

    def record(self, request_id: str, message_id: str, to: str, subject: str):
        with self._lock:
            existing = self._data.get(request_id)
            if existing and (existing.get("to") != to or existing.get("subject") != subject):
                print(f"WARNING: request_id '{request_id}' collision — proceeding with new send.", flush=True)
            self._data[request_id] = {
                "message_id": message_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "to": to,
                "subject": subject,
            }
            self._save()

    def cleanup(self):
        cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
        with self._lock:
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
