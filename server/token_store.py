"""Durable credential store: one active file, one staged file.

Two phases exist because the account a code authorizes is only knowable AFTER
the exchange. Committing before verification would let a wrong-account consent
destroy a working credential; verifying before any durable write would lose a
credential whose one-use code is already spent. So: stage durably, verify,
promote.

Every write is atomic and crash-durable — temp file, fsync, os.replace, fsync
the directory — because casa's ack is a strict-fsync settlement receipt and
treats the consumer's store as already committed.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

ACTIVE_NAME = "oauth_token.json"
STAGED_NAME = "oauth_token.staged.json"
SCHEMA_VERSION = 2


class StagedFlowMismatch(RuntimeError):
    """The staged slot no longer holds the flow the caller verified."""


@dataclass(frozen=True)
class Credential:
    refresh_token: str
    flow: str | None
    generation: float | None
    account: str | None


def _durable_write(path: Path, payload: dict) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    data = json.dumps(payload).encode("utf-8")
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _read(path: Path) -> Credential | None:
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    rt = raw.get("refresh_token")
    if not isinstance(rt, str) or not rt:
        return None
    gen = raw.get("generation")
    return Credential(
        refresh_token=rt,
        flow=raw.get("flow") if isinstance(raw.get("flow"), str) else None,
        generation=float(gen) if isinstance(gen, (int, float)) else None,
        account=raw.get("account") if isinstance(raw.get("account"), str) else None,
    )


class TokenStore:
    def __init__(self, data_dir: str):
        self._dir = Path(data_dir)
        self._active = self._dir / ACTIVE_NAME
        self._staged = self._dir / STAGED_NAME

    def load_active(self) -> Credential | None:
        return _read(self._active)

    def load_staged(self) -> Credential | None:
        return _read(self._staged)

    def stage(self, refresh_token: str, flow: str, minted_ts: float | None) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        _durable_write(self._staged, {
            "v": SCHEMA_VERSION,
            "refresh_token": refresh_token,
            "flow": flow,
            # Normalized here, once: casa permits a null mint clock, and a null
            # generation would make the supersession tuple incomparable.
            "generation": minted_ts if minted_ts is not None else 0.0,
            "staged_ts": time.time(),
        })

    def promote(self, expected_flow: str, account: str) -> Credential:
        """Re-read the stage and refuse unless it still holds `expected_flow`.

        Binding promotion to an identity is what stops a slot replaced by a
        concurrent pass from being promoted under another flow's verified
        account. Refusal mutates nothing.
        """
        staged = self.load_staged()
        if staged is None or staged.flow != expected_flow:
            raise StagedFlowMismatch(
                "staged credential is absent or belongs to a different flow"
            )
        cred = Credential(
            refresh_token=staged.refresh_token,
            flow=staged.flow,
            generation=staged.generation,
            account=account,
        )
        self.write_active(cred)
        self.discard_staged()
        return cred

    def write_active(self, cred: Credential) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "v": SCHEMA_VERSION,
            "refresh_token": cred.refresh_token,
            "flow": cred.flow,
            "generation": cred.generation,
            "account": cred.account,
            "committed_ts": time.time(),
        }
        _durable_write(self._active, payload)

    def discard_staged(self) -> None:
        try:
            os.unlink(self._staged)
        except FileNotFoundError:
            return
        _fsync_dir(self._dir)

    def remove_active(self) -> None:
        try:
            os.unlink(self._active)
        except FileNotFoundError:
            return
        _fsync_dir(self._dir)
