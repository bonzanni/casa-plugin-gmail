import os
import shutil
import threading
import time

TTL_SECONDS = 7 * 24 * 3600       # 7 days
RACE_GUARD_SECONDS = 60
CLEANUP_INTERVAL = 6 * 3600       # 6 hours


class AttachmentManager:
    def __init__(self, plugin_data: str):
        self._plugin_data = os.path.realpath(plugin_data)
        self._cache_dir = os.path.join(self._plugin_data, "attachments", "cache")
        self._saved_dir = os.path.join(self._plugin_data, "saved")
        os.makedirs(self._cache_dir, exist_ok=True)
        os.makedirs(self._saved_dir, exist_ok=True)
        self._run_cleanup()
        self._start_cleanup_thread()

    def sanitize_filename(self, filename: str, attachment_id: str) -> str:
        sanitized = filename.replace("/", "").replace("\\", "").replace("\x00", "")
        sanitized = sanitized.lstrip(".")
        return sanitized if sanitized else f"attachment_{attachment_id}"

    def get_unique_path(self, directory: str, filename: str) -> str:
        path = os.path.join(directory, filename)
        if not os.path.exists(path):
            return path
        base, ext = os.path.splitext(filename)
        counter = 1
        while True:
            candidate = os.path.join(directory, f"{base}_{counter}{ext}")
            if not os.path.exists(candidate):
                return candidate
            counter += 1

    def save_to_cache(self, message_id: str, sanitized_filename: str, data: bytes) -> str:
        if (
            not sanitized_filename
            or "/" in sanitized_filename
            or "\\" in sanitized_filename
            or "\x00" in sanitized_filename
            or sanitized_filename.startswith(".")
        ):
            raise ValueError(
                f"Filename must be pre-sanitized before saving to cache. Invalid: {sanitized_filename!r}"
            )
        msg_dir = os.path.join(self._cache_dir, message_id)
        os.makedirs(msg_dir, exist_ok=True)
        path = self.get_unique_path(msg_dir, sanitized_filename)
        with open(path, "wb") as f:
            f.write(data)
        return path

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
        real_cache = os.path.realpath(self._cache_dir)
        real_cached = os.path.realpath(cached_path)
        if not (real_cached == real_cache or real_cached.startswith(real_cache + os.sep)):
            raise ValueError("Invalid cached_path: file must be in plugin cache directory.")
        resolved = self.validate_save_destination(destination)
        if os.path.exists(resolved) and not overwrite:
            raise FileExistsError(
                "Destination already exists. Pass overwrite=True to replace, or choose a different destination."
            )
        os.makedirs(os.path.dirname(resolved), exist_ok=True)
        shutil.copy2(cached_path, resolved)
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
