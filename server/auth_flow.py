"""Orchestration for the casa-callback OAuth flow.

Holds the collect lock, parses casa's relayed query, starts a flow, and runs
the collect pass. Everything that decides ORDER lives here; `auth` owns the
OAuth protocol, `token_store` owns durability, `casa_callback` owns the spool.
"""
from __future__ import annotations

import errno
import fcntl
import os
import secrets
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from auth import ExchangeRetryable, ExchangeTerminal, RefreshRetryable, RefreshTerminal
from casa_callback import attempt_order
from token_store import StagedFlowMismatch

LOCK_NAME = "collect.lock"


class MalformedCallback(ValueError):
    """The provider's query is not a usable single code or single error."""


@contextmanager
def collect_lock(data_dir):
    """Serialize the collect pass and startup recovery.

    Both touch the single staged slot, so they must not interleave. On the
    pinned mcp 1.3.0 a sync @mcp.tool() runs on the event loop thread, so two
    tool calls cannot overlap in-process today; this guards a second server
    process and a future version bump.

    Yields True when held, False when another holder has it. Contention is a
    no-op for the caller — never a second held-journal recovery.
    """
    directory = Path(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    fd = os.open(directory / LOCK_NAME, os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN):
                raise
        yield acquired
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def parse_callback_query(pairs) -> tuple[str | None, str | None]:
    """Extract exactly one `code` OR one `error` from casa's ordered pair list.

    Casa preserves duplicate keys and their order on purpose, so this is a list
    of [key, value] lists, NOT a mapping — a dict() would let a duplicate
    shadow the real value.
    """
    if not isinstance(pairs, list):
        raise MalformedCallback("callback query is not a list of pairs")
    codes, errors = [], []
    for pair in pairs:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            continue
        key, value = pair[0], pair[1]
        if key == "code":
            codes.append(value)
        elif key == "error":
            errors.append(value)
    if len(codes) > 1 or len(errors) > 1:
        raise MalformedCallback("callback query carries a duplicated code or error")
    if codes and errors:
        raise MalformedCallback("callback query carries both a code and an error")
    if not codes and not errors:
        raise MalformedCallback("callback query carries neither a code nor an error")
    if codes:
        if not codes[0]:
            raise MalformedCallback("callback query carries an empty code")
        return codes[0], None
    if not errors[0]:
        raise MalformedCallback("callback query carries an empty error")
    return None, errors[0]


MINT_META = {"kind": "gmail-oauth", "v": 1}


def start(auth, cb) -> dict:
    """Mint a state into the spool and build the Google authorization URL.

    No local flow store: only a state we minted can produce a result, and the
    result's filename IS that state's hash, so CSRF protection is structural.
    """
    route = cb.resolve()
    state = secrets.token_urlsafe(32)
    cb.mint(state, dict(MINT_META))
    return {
        "auth_url": auth.build_auth_url(route.redirect_uri, state),
        "redirect_uri": route.redirect_uri,
        "instructions": (
            "Open auth_url and grant access. When the browser shows "
            "'Response received', you're done — I'll be notified automatically "
            "and will finish the setup. If Google reports redirect_uri_mismatch, "
            "register the redirect_uri above with the OAuth client."
        ),
    }


def reconcile_stage(auth, cb) -> tuple[str, str]:
    """Step 0. Resolve the single staged slot before any attempt is considered.

    A stage does not only appear after a crash — a getProfile timeout leaves one
    behind in a perfectly healthy process. Since no flow may be exchanged while
    a stage exists, every stage must reach a decision here.

    Returns (outcome, message) with outcome in {none, promoted, settled, retain}.
    """
    staged = auth.store.load_staged()
    if staged is None or not staged.flow:
        return "none", ""

    active = auth.store.load_active()
    if active is not None and active.flow == staged.flow:
        # Post-promote residue: the flow already committed.
        auth.store.discard_staged()
        return "none", ""

    try:
        account = auth.refresh_and_verify(staged.refresh_token)
    except RefreshRetryable as exc:
        # Keep the stage AND the active credential; the nudge will re-fire.
        return "retain", f"Could not verify the pending authorization yet ({exc})."
    except RefreshTerminal as exc:
        # Settle it, or it wedges the slot forever. Ack BEFORE unlink so a
        # successor never finds a journal with no disposition.
        cb.ack(staged.flow)
        auth.store.discard_staged()
        return "settled", (
            f"The pending Gmail authorization is no longer valid ({exc}). "
            "Please start authorization again."
        )

    if account.lower() != auth.subject_email.lower():
        cb.ack(staged.flow)
        auth.store.discard_staged()
        return "settled", (
            f"That authorization was granted by {account}, but this plugin is "
            f"configured for {auth.subject_email}. Nothing was changed — the "
            "existing connection is untouched. Please retry with the right account."
        )

    try:
        committed = auth.store.promote(staged.flow, account)
    except StagedFlowMismatch as exc:
        return "retain", f"Pending authorization changed under us ({exc})."

    auth.activate(committed)
    cb.ack(staged.flow)
    return "promoted", f"Gmail connected as {account}."


def startup_recover(auth, cb) -> str:
    """Read env, load the active credential, then reconcile any stage — all
    under the lock, as ONE unit.

    Active FIRST: a transient failure verifying a stage must never leave the
    process unauthenticated when a good active credential is on disk.

    `validate_and_init` can itself mutate the store (it removes a terminally
    dead active token), so it must be inside the lock, not before it. This is
    the whole of the plugin's startup credential work — `server._startup` must
    not call `validate_and_init` separately. A missing env var still exits the
    process: SystemExit is a BaseException and is not caught by the caller.
    """
    with collect_lock(auth.store.dir) as acquired:
        if not acquired:
            return "busy"
        auth.validate_and_init()
        try:
            outcome, _message = reconcile_stage(auth, cb)
        except Exception as exc:
            print(
                f"Gmail plugin: credential startup recovery failed ({exc}).",
                file=sys.stderr,
            )
            return "error"
        return outcome


_COLLECT_RETRIES = 3
_COLLECT_BACKOFF_S = 0.2

_DONE_TEXT = {
    "expired": "that authorization link expired before it was used",
    "expired_unread": "that authorization completed but expired before I collected it",
    "publish_failed": "casa could not record that authorization result",
    "evicted": "that authorization was discarded by casa",
    "collected": "that authorization was already handled",
}


def _committed_generation(active):
    """(generation, flow) or None. A migrated legacy credential has neither and
    therefore supersedes nothing."""
    if active is None or active.flow is None or active.generation is None:
        return None
    return (active.generation, active.flow)


def _obtain_result(cb, rec, active):
    """Return (kind, payload).

    kind: "record" (usable result), "committed" (this flow already promoted),
    "unrecoverable" (claimed with no journal), "retry" (transient ENOENT).
    """
    h = rec["state_hash"]
    for attempt_no in range(_COLLECT_RETRIES):
        try:
            return "record", cb.collect(h)
        except FileNotFoundError:
            if rec.get("claimed"):
                break
            if attempt_no < _COLLECT_RETRIES - 1:
                time.sleep(_COLLECT_BACKOFF_S)
    if not rec.get("claimed"):
        return "retry", None
    # Claimed: our own store is the tiebreaker (casa's collect() contract).
    if active is not None and active.flow == h:
        return "committed", None
    held = cb.held(h)
    if held is None:
        return "unrecoverable", None
    return "record", held


def collect_pass(auth, cb) -> dict:
    """Collect and settle every callback result waiting for this plugin.

    Idempotent by contract — casa may nudge more than once.
    """
    out = {"status": "ok", "messages": [], "promoted": False}
    with collect_lock(auth.store.dir) as acquired:
        if not acquired:
            return {"status": "busy", "messages": [], "promoted": False}

        outcome, message = reconcile_stage(auth, cb)
        if message:
            out["messages"].append(message)
        if outcome == "retain":
            out["status"] = "retry_later"
            return out
        if outcome == "promoted":
            out["promoted"] = True

        committed = _committed_generation(auth.store.load_active())

        for rec in cb.attempts():
            h = rec["state_hash"]
            if committed is not None and attempt_order(rec) < committed:
                cb.ack(h)
                out["messages"].append("Discarded a superseded authorization link.")
                continue

            status = rec.get("status")
            if status == "awaiting_redirect":
                continue
            if status == "done":
                reason = _DONE_TEXT.get(rec.get("outcome"), "that authorization ended")
                out["messages"].append(f"Nothing to collect — {reason}.")
                cb.ack(h)
                continue

            active = auth.store.load_active()
            kind, payload = _obtain_result(cb, rec, active)
            if kind == "retry":
                out["status"] = "retry_later"
                continue
            if kind == "committed":
                auth.activate(active)
                cb.ack(h)
                out["promoted"] = True
                out["messages"].append("Gmail is already connected.")
                continue
            if kind == "unrecoverable":
                out["messages"].append(
                    "An authorization result was lost before I could read it. "
                    "Please start authorization again.")
                cb.ack(h)
                continue

            try:
                code, error = parse_callback_query(payload.get("query"))
            except MalformedCallback as exc:
                out["messages"].append(f"Unusable authorization response ({exc}).")
                cb.ack(h)
                continue
            if error:
                out["messages"].append(
                    f"Authorization was not granted ({error}). Nothing has changed.")
                cb.ack(h)
                continue

            try:
                token = auth.exchange_code(code, cb.resolve().redirect_uri)
            except ExchangeTerminal as exc:
                out["messages"].append(f"That authorization could not be completed "
                                       f"({exc}). Please start again.")
                cb.ack(h)
                continue                      # terminal: fall through is safe
            except ExchangeRetryable as exc:
                out["messages"].append(f"Temporary problem completing authorization "
                                       f"({exc}); I'll retry.")
                out["status"] = "retry_later"
                return out                    # never fall through: it would
                                              # overwrite the staged slot

            auth.store.stage(token["refresh_token"], h, rec.get("minted_ts"))
            stage_outcome, stage_message = reconcile_stage(auth, cb)
            if stage_message:
                out["messages"].append(stage_message)
            if stage_outcome == "promoted":
                out["promoted"] = True
                committed = _committed_generation(auth.store.load_active())
                continue
            if stage_outcome == "retain":
                out["status"] = "retry_later"
                return out
            # "settled" — dead or wrong-account; try the next attempt.

    return out
