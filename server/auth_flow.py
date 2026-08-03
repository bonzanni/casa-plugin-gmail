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


def reconcile_stage(auth, cb, notify: bool = False) -> tuple[str, str]:
    """Step 0. Resolve the single staged slot before any attempt is considered.

    A stage does not only appear after a crash — a getProfile timeout leaves one
    behind in a perfectly healthy process. Since no flow may be exchanged while
    a stage exists, every stage must reach a decision here.

    Returns (outcome, message) with outcome in {none, promoted, settled, retain}.

    `notify` is the ROUTING decision for the message, and belongs here because
    ordering does: when the caller has no live channel to the user (startup
    recovery), every message that accompanies an ack is persisted BEFORE that
    ack, so a later collect can still say what happened. `collect_pass` relays
    its own return value, so it leaves notify False — that, and not any
    after-the-fact string comparison, is what makes a duplicate impossible.
    """
    def settle(disposition: str, message: str) -> str:
        """Persist the notice (if nobody is listening), THEN let the caller ack.

        Keyed by flow + disposition, and not by chance: an ack that fails
        leaves the stage in place, so the next startup reaches this same
        disposition for this same flow and would otherwise queue the identical
        sentence a second time.
        """
        if notify:
            auth.store.queue_notice(f"{staged.flow}:{disposition}", message)
        return message

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
        # successor never finds a journal with no disposition — and settle()
        # BEFORE the ack, so the sentence outlives the teardown.
        message = settle(
            "terminal",
            f"The pending Gmail authorization is no longer valid ({exc}). "
            "Please start authorization again."
        )
        cb.ack(staged.flow)
        auth.store.discard_staged()
        return "settled", message
    except Exception as exc:
        # refresh_and_verify is a refresh AND a getProfile; the second half
        # raises neither typed error (a plain ValueError for any HttpError, and
        # transport failures raw). Unclassified ⇒ retain, the same policy the
        # refresh itself uses: never destroy a credential on ambiguity, and
        # never let the stage wedge every future attempt at step 0.
        return "retain", f"Could not verify the pending authorization yet ({exc})."

    if not account:
        # getProfile answered without an emailAddress. That is a failed
        # verification, NOT a wrong-account verdict — settling it here would ack
        # and discard a stage that may be perfectly good.
        return "retain", (
            "Could not confirm which account the pending authorization belongs to."
        )

    if account.lower() != auth.subject_email.lower():
        message = settle(
            "wrong_account",
            f"That authorization was granted by {account}, but this plugin is "
            f"configured for {auth.subject_email}. Nothing was changed — the "
            "existing connection is untouched. Please retry with the right account."
        )
        cb.ack(staged.flow)
        auth.store.discard_staged()
        return "settled", message

    try:
        committed = auth.store.promote(staged.flow, account)
    except StagedFlowMismatch as exc:
        return "retain", f"Pending authorization changed under us ({exc})."

    try:
        auth.activate(committed)
    except Exception as exc:
        # Activation failure is retryable, and the ack must not happen: the
        # credential is already durably promoted, so the next pass recovers it
        # through the `claimed` / `active.flow == h` committed path.
        return "retain", (
            f"Gmail is authorized but I could not finish setting up ({exc}); "
            "I'll retry."
        )
    message = settle("connected", f"Gmail connected as {account}.")
    cb.ack(staged.flow)
    return "promoted", message


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

    The env read is hoisted OUT of the lock. It touches no store state, so the
    "env validation, active-token loading and staged recovery are one unit"
    rule is untouched — but a contended lock must not leave the process with no
    client id and no subject email, which would make gmail_auth_start emit
    `client_id=None` and gmail_auth_collect fail on the account comparison.
    """
    auth.read_env()
    with collect_lock(auth.store.dir) as acquired:
        if not acquired:
            return "busy"
        auth.validate_and_init()
        try:
            # notify=True: there is no live channel here. Whatever this
            # resolves — a promote, a wrong-account rejection, a terminal
            # settle — is acked, and casa's ack tears the attempt down, so the
            # next collect_pass would find nothing left to report. The notice
            # is what carries the outcome into that call.
            outcome, _message = reconcile_stage(auth, cb, notify=True)
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
    with collect_lock(auth.store.dir) as acquired:
        if not acquired:
            return {"status": "busy", "messages": [], "promoted": False}
        out = _locked_pass(auth, cb)
        # Only here: the pass returned normally, so `out` really does carry
        # every notice the peek handed over, and this offer of it counts.
        # Counting at the peek instead would let any failure below — a raising
        # cb.attempts(), a crash mid-exchange — burn an offer nobody ever read.
        # A return is still not proof anyone READ it, which is why the offer is
        # counted rather than the notice retired; the durable count is what
        # bounds the repeats.
        auth.store.record_notices_offered()

    if out["status"] == "ok" and not out["messages"] and not out["promoted"]:
        # A pass that did nothing must not read as a success. This tool is
        # documented as safe to call repeatedly, so an empty pass is normal —
        # a stale or replayed link produces one — and `status: "ok"` with no
        # messages leaves the agent with nothing to relay but a green light it
        # has no basis for. Say plainly that nothing was found. Not an error:
        # nothing failed.
        out["messages"].append(
            "No authorization result was waiting — nothing was collected. This "
            "does NOT confirm that Gmail authorization succeeded. If you were "
            "expecting a result, run gmail_auth_start and follow the link again."
        )
    return out


def _locked_pass(auth, cb) -> dict:
    """The pass proper, run under the collect lock.

    Every `return` here is a normal return whose `messages` carry the peeked
    notices; anything raised leaves the offer uncounted and them on disk.
    """
    out = {"status": "ok", "messages": [], "promoted": False}

    # Anything a previous startup recovery resolved with nobody listening.
    # Peeked FIRST so it reads in the order it happened, and peeked here rather
    # than at the end because every early return below must still carry it.
    # Peek, not drain: the durable copy outlives this pass, and collect_pass
    # counts this offer against it only once the pass has returned — the copy
    # is retired by exhausting its offer budget and by nothing else.
    # reconcile_stage runs with notify False from this point on, so nothing
    # this pass produces can also land in the notice file.
    out["messages"].extend(auth.store.peek_notices())

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
            try:
                auth.activate(active)
            except Exception as exc:
                # Ack only after activate() succeeds. A failed runtime
                # rebuild is process-wide, so the remaining attempts would
                # fail identically: end the pass, ack nothing, let the
                # nudge re-fire.
                out["status"] = "retry_later"
                out["messages"].append(
                    f"Gmail is authorized but I could not finish setting up "
                    f"({exc}); I'll retry.")
                return out
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
