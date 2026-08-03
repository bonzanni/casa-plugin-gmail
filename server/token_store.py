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
NOTICE_NAME = "pending_notices.json"
SCHEMA_VERSION = 2

# How many times a pending notice may be offered before it is retired for good
# (see "Pending user-facing notices" below). Three covers casa's nudge retrying
# a collect within one process AND a restart or two, without letting a sentence
# repeat forever. Not configurable: there is nothing here worth tuning.
NOTICE_OFFER_LIMIT = 3


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
    _fsync_dir(path.parent)


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
        self._notices = self._dir / NOTICE_NAME

    @property
    def dir(self) -> Path:
        return self._dir

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

    # ── Pending user-facing notices ────────────────────────────────────────
    # A flow can be resolved with nobody listening — startup recovery settles
    # and ACKS a stage, and casa's ack tears the attempt down, so the next
    # collect finds nothing left to report. The outcome would be lost exactly
    # in the cases the user most needs told (wrong account, dead flow). These
    # calls give the resolver somewhere durable to leave the sentence.
    #
    # Delivery is deliberately AT-LEAST-ONCE. Showing "that authorization was
    # granted by the wrong account" twice is a nuisance; never showing it is
    # the bug this file exists to prevent.
    #
    # There is no acknowledgement on this tool surface: returning a sentence
    # from gmail_auth_collect is not evidence that the response reached the
    # agent, and neither is a later pass — casa's nudge carries a six-dispatch
    # budget, so a retried collect in the SAME process is ordinary behaviour,
    # not a signal that the previous one landed. Nothing observable here can
    # confirm delivery, so nothing here pretends to. Instead each notice
    # carries a durable count of how many times it has been OFFERED:
    #
    #   peek_notices()           returns everything under the limit, removing
    #                            and counting nothing
    #   record_notices_offered() called ONLY after the pass returned them;
    #                            counts one offer against each and drops those
    #                            that have now reached NOTICE_OFFER_LIMIT
    #
    # The count is durable, so restarts neither reset it nor exempt anything:
    # a notice is offered at most NOTICE_OFFER_LIMIT times across any sequence
    # of passes and restarts, and then never again. Nothing else removes a
    # notice — in particular, "a later pass ran" never does.
    #
    # Each notice carries a key — flow + disposition — so re-queueing the same
    # outcome (a settlement whose ack failed, settled again next startup) is a
    # no-op instead of a second identical sentence.

    def queue_notice(self, key: str, message: str) -> None:
        """Durably record a user-facing outcome, at most once per `key`.

        Callers MUST write the notice BEFORE the ack it describes: the ack is a
        settlement receipt that tears the flow down, so a notice written after
        it is lost by any crash in between — which is the very scenario it
        exists for.
        """
        records = self._load_records()
        if any(r["key"] == key for r in records):
            return
        self._dir.mkdir(parents=True, exist_ok=True)
        self._write_records(
            records + [{"key": key, "message": message, "offered": 0}]
        )

    def peek_notices(self) -> list[str]:
        """Every notice still owed the user. Removes nothing, counts nothing.

        Read must not be destructive: the caller may still fail before it has
        delivered what it read. Call under the collect lock.
        """
        return [r["message"] for r in self._load_records()
                if r["offered"] < NOTICE_OFFER_LIMIT]

    def record_notices_offered(self) -> None:
        """Count one offer against every notice a completed pass returned, and
        retire those that have now been offered NOTICE_OFFER_LIMIT times.

        Call ONLY once the pass has returned normally with the notices in hand:
        counting at peek time would let any later failure in that pass burn an
        offer nobody ever read.
        """
        records = self._load_records()
        if not records:
            return
        for record in records:
            record["offered"] += 1
        self._write_records(
            [r for r in records if r["offered"] < NOTICE_OFFER_LIMIT]
        )

    def _load_records(self) -> list[dict]:
        try:
            raw = json.loads(self._notices.read_text())
        except (OSError, ValueError):
            return []
        if not isinstance(raw, dict):
            return []
        items = raw.get("notices")
        if not isinstance(items, list):
            return []
        out = []
        for item in items:
            if not isinstance(item, dict):
                continue
            key, message = item.get("key"), item.get("message")
            if not isinstance(key, str) or not isinstance(message, str) or not message:
                continue
            offered = item.get("offered")
            out.append({
                "key": key,
                "message": message,
                # A missing or nonsense count reads as "never offered": the
                # at-least-once bias says re-offer, never silently retire.
                "offered": offered if isinstance(offered, int)
                and not isinstance(offered, bool) and offered >= 0 else 0,
            })
        return out

    def _write_records(self, records: list[dict]) -> None:
        if not records:
            try:
                os.unlink(self._notices)
            except FileNotFoundError:
                return
            _fsync_dir(self._dir)
            return
        _durable_write(self._notices, {"v": SCHEMA_VERSION, "notices": records})
