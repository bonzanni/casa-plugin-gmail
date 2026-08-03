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
