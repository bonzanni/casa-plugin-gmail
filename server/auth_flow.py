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
