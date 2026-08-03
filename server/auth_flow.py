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
from contextlib import contextmanager
from pathlib import Path

from auth import RefreshRetryable, RefreshTerminal
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
        except Exception:
            return "error"
        return outcome
