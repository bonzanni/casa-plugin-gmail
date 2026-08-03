"""The plugin's only casa-aware module.

Discovers this plugin's authorization-callback spool through casa's `.index`
entry and exposes the consumer half of the spool protocol. The protocol itself
is IMPORTED from casa rather than reimplemented — see `_protocol()`.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

CALLBACK_NAME = "oauth"
DEFAULT_SPOOL_ROOT = "/data/callbacks"
DEFAULT_LIB_DIR = "/opt/casa"

_REASONS = (
    "callback_pending_ack, callback_base_url_invalid, callback_no_target, "
    "callback_invalid, callback_spool_error"
)


class CallbackUnavailable(RuntimeError):
    """The callback route is not open. The plugin cannot tell which of casa's
    reasons applies, so it names them all and points at casa's health report."""


@dataclass(frozen=True)
class Route:
    spool_dir: Path
    effective: str
    redirect_uri: str


def _unavailable() -> CallbackUnavailable:
    return CallbackUnavailable(
        "The Gmail authorization callback route is not open. Check casa's plugin "
        f"health for the reason — it will be one of: {_REASONS}."
    )


def attempt_order(rec: dict) -> tuple[float, str]:
    """Casa's own total order (callback_spool.py:2702-2707): a null mint clock
    sorts oldest, and the state hash breaks ties deterministically."""
    ts = rec.get("minted_ts")
    return (ts if isinstance(ts, (int, float)) else 0.0, rec.get("state_hash") or "")


class CasaCallback:
    def __init__(self, plugin_root: str, spool_root: str | None = None,
                 lib_dir: str = DEFAULT_LIB_DIR):
        self._plugin_root = plugin_root
        self._spool_root = Path(
            spool_root or os.environ.get("CASA_CALLBACK_SPOOL_ROOT") or DEFAULT_SPOOL_ROOT
        )
        self._lib_dir = lib_dir

    def _index_key(self) -> str:
        return hashlib.sha256(
            os.path.realpath(self._plugin_root).encode("utf-8")
        ).hexdigest()

    def resolve(self) -> Route:
        entry = self._spool_root / ".index" / f"{self._index_key()}.json"
        try:
            payload = json.loads(entry.read_text())
        except (OSError, ValueError):
            raise _unavailable() from None
        if not isinstance(payload, dict) or payload.get("v") != 1:
            raise _unavailable()
        plugin_dir = payload.get("plugin_dir")
        callbacks = payload.get("callbacks")
        if not isinstance(plugin_dir, str) or not plugin_dir \
                or not isinstance(callbacks, dict):
            raise _unavailable()
        entry_cb = callbacks.get(CALLBACK_NAME)
        if not isinstance(entry_cb, dict):
            raise _unavailable()
        effective = entry_cb.get("effective")
        redirect_uri = entry_cb.get("redirect_uri")
        if not isinstance(effective, str) or not isinstance(redirect_uri, str) \
                or not effective or not redirect_uri:
            raise _unavailable()
        return Route(self._spool_root / plugin_dir, effective, redirect_uri)

    def _protocol(self):
        """Import casa's spool protocol rather than reimplementing it.

        APPENDED to sys.path, never inserted: /opt/casa holds modules whose
        names could otherwise shadow the plugin's own `auth` / `server`.
        """
        if self._lib_dir not in sys.path:
            sys.path.append(self._lib_dir)
        import callback_attempts
        import callback_spool
        return callback_spool, callback_attempts

    def mint(self, state: str, meta: dict) -> None:
        spool, _ = self._protocol()
        spool.mint(self.resolve().spool_dir, state, meta)

    def ack(self, state_hash: str) -> None:
        spool, _ = self._protocol()
        spool.ack(self.resolve().spool_dir, state_hash)

    def collect(self, state_hash: str) -> dict:
        """FileNotFoundError propagates: casa publishes the attempt a moment
        before the result link lands, so ENOENT is retryable, never ackable."""
        spool, _ = self._protocol()
        record, _held_path = spool.collect(self.resolve().spool_dir, state_hash)
        return record

    def held(self, state_hash: str) -> dict | None:
        """Read a surviving `.collect-*` crash journal. NEVER unlinks it —
        ack-teardown is what removes it."""
        results = self.resolve().spool_dir / "results"
        try:
            names = os.listdir(results)
        except OSError:
            return None
        prefix = f".collect-{state_hash}-"
        for name in sorted(names):
            if name.startswith(prefix):
                try:
                    return json.loads((results / name).read_text())
                except (OSError, ValueError):
                    return None
        return None

    def attempts(self) -> list[dict]:
        """Validated attempt records, newest first.

        Validation is casa's own `validate_attempt`, bound to the filename hash:
        a parseable-but-schema-invalid record is untrustworthy and must never
        drive an exchange or an ack.
        """
        _spool, attempts_mod = self._protocol()
        directory = self.resolve().spool_dir / "attempts"
        try:
            names = os.listdir(directory)
        except OSError:
            return []
        out: list[dict] = []
        for name in names:
            if name.startswith(".") or not name.endswith(".json"):
                continue
            h = name[:-len(".json")]
            try:
                obj = json.loads((directory / name).read_text())
            except (OSError, ValueError):
                continue
            rec = attempts_mod.validate_attempt(obj, expect_hash=h)
            if rec is not None:
                out.append(rec)
        out.sort(key=attempt_order, reverse=True)
        return out
