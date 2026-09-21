import os
import threading
import time

import casa_handoff

TTL_SECONDS = 7 * 24 * 3600       # 7 days
RACE_GUARD_SECONDS = 60
CLEANUP_INTERVAL = 6 * 3600       # 6 hours


class AttachmentManager:
    def __init__(self, plugin_data: str):
        self._plugin_data = os.path.realpath(plugin_data)
        # Not written since 0.9.0 (downloads go to Casa's handoff folder);
        # kept readable and reaped so files downloaded before stay usable
        # until they age out.
        self._cache_dir = os.path.join(self._plugin_data, "attachments", "cache")
        self._saved_dir = os.path.join(self._plugin_data, "saved")
        os.makedirs(self._cache_dir, exist_ok=True)
        os.makedirs(self._saved_dir, exist_ok=True)
        self._run_cleanup()
        self._start_cleanup_thread()

    def read_source(self, path: str) -> tuple[str, bytes]:
        """``(filename, bytes)`` for a file this plugin may send or save.

        A path in Casa's handoff folder is taken with ``casa_handoff.capture``.
        Otherwise only this plugin's own ``attachments/cache/`` (files downloaded
        before the handoff existed) and ``saved/`` are accepted — never the rest
        of the data directory, which holds the OAuth token store. Either way the
        bytes come from one guarded read of the checked file; the caller must
        not open ``path`` again."""
        if not isinstance(path, str) or not os.path.isabs(path):
            raise ValueError(f"Attachment path {path!r} must be absolute.")
        real = os.path.realpath(path)
        handoff = os.path.realpath(casa_handoff.root_dir())
        try:
            if real.startswith(handoff + os.sep):
                return casa_handoff.capture(path)
            # Compared with the directories' EXPECTED paths, never their
            # resolved ones: a cache/ or saved/ that is itself a link to the
            # data root must not widen what is accepted.
            for base in (self._cache_dir, self._saved_dir):
                if real.startswith(base + os.sep):
                    return os.path.basename(real), casa_handoff.read_regular(real)
        except casa_handoff.HandoffError as exc:
            raise ValueError(f"Attachment path {path} cannot be used: {exc}") from None
        raise ValueError(
            f"Attachment path {path} is not a handoff file or a downloaded or "
            "saved attachment.")

    def validate_save_destination(self, destination: str) -> str:
        """Return resolved absolute path, or raise ValueError if it escapes saved_dir."""
        real_saved = os.path.realpath(self._saved_dir)
        # Lexically normalise the joined path (resolves .. without hitting disk)
        joined = os.path.normpath(os.path.join(real_saved, destination))
        if not (joined == real_saved or joined.startswith(real_saved + os.sep)):
            raise ValueError("Invalid destination: path escapes plugin data directory.")
        # Also walk existing parent dirs to check for symlinks
        parts = destination.replace("\\", "/").split("/")
        current = real_saved
        for part in parts[:-1]:
            current = os.path.join(current, part)
            if os.path.islink(current):
                link_real = os.path.realpath(current)
                if not (link_real == real_saved or link_real.startswith(real_saved + os.sep)):
                    raise ValueError("Invalid destination: path escapes plugin data directory.")
        return joined

    def save_attachment(self, cached_path: str, destination: str, overwrite: bool = False) -> str:
        """Copy a downloaded attachment (the path ``download_attachment``
        returned, or a legacy cache path) into ``saved/``."""
        real_saved = os.path.realpath(self._saved_dir)
        if os.path.realpath(cached_path).startswith(real_saved + os.sep):
            raise ValueError("Invalid cached_path: pass the path download_attachment returned.")
        _name, data = self.read_source(cached_path)
        resolved = self.validate_save_destination(destination)
        if os.path.exists(resolved) and not overwrite:
            raise FileExistsError(
                "Destination already exists. Pass overwrite=True to replace, or choose a different destination."
            )
        os.makedirs(os.path.dirname(resolved), exist_ok=True)
        with open(resolved, "wb") as f:
            f.write(data)
        return resolved

    def _run_cleanup(self):
        now = time.time()
        cutoff = now - TTL_SECONDS - RACE_GUARD_SECONDS
        if not os.path.isdir(self._cache_dir):
            return
        for msg_id in os.listdir(self._cache_dir):
            msg_dir = os.path.join(self._cache_dir, msg_id)
            if not os.path.isdir(msg_dir):
                continue
            for fname in list(os.listdir(msg_dir)):
                fpath = os.path.join(msg_dir, fname)
                try:
                    if os.path.getmtime(fpath) < cutoff:
                        os.remove(fpath)
                except OSError:
                    pass
            try:
                if not os.listdir(msg_dir):
                    os.rmdir(msg_dir)
            except OSError:
                pass

    def _cleanup_loop(self):
        self._run_cleanup()
        t = threading.Timer(CLEANUP_INTERVAL, self._cleanup_loop)
        t.daemon = True
        t.start()

    def _start_cleanup_thread(self):
        t = threading.Timer(CLEANUP_INTERVAL, self._cleanup_loop)
        t.daemon = True
        t.start()
