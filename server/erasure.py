"""The two erasers Casa runs when the plugin is uninstalled with an erase choice.

Casa (>= 0.331.0) asks the operator at uninstall, runs the eraser this plugin
declares for the chosen kind, and removes the plugin only when the eraser
reports ``{"erasure": "complete"}``. So "complete" is a promise: nothing of the
kind the operator chose is left, and every revocation Google was asked for was
confirmed. Anything short of that is ``incomplete`` with a report that says
what is left, and the plugin stays installed so the erase can be run again.

Two kinds, one walk over the data directory:

* ``erase_everything`` (``casa.eraseTool``) revokes every stored grant at
  Google, then deletes everything in ``CLAUDE_PLUGIN_DATA``. A grant whose
  revocation is not confirmed keeps its token file, so a second run can still
  revoke it — deleting it would leave the grant live at Google with no way to
  reach it from Casa.
* ``erase_data`` (``casa.eraseDataOnlyTool``) deletes everything EXCEPT the
  sign-in files (the token store and the collect lock), so a reinstall carries
  on without authorizing again.

Deletion is by an allowlist of what to keep, never a list of what to delete: a
file a future version adds is erased by default.
"""
from __future__ import annotations

import json
import os
import shutil
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from auth_flow import LOCK_NAME, collect_lock
from token_store import ACTIVE_NAME, NOTICE_NAME, STAGED_NAME

REVOKE_URI = "https://oauth2.googleapis.com/revoke"
PERMISSIONS_PAGE = "https://myaccount.google.com/permissions"

# What a data-only erase keeps: exactly what a reinstall needs to carry on
# signed in, plus the notices that report a sign-in's outcome.
SIGN_IN_FILES = frozenset({ACTIVE_NAME, STAGED_NAME, NOTICE_NAME})

_HANDOFF_NOTE = (
    "Attachments already handed to Casa's handoff folder are Casa's and expire "
    "there within 7 days. Your mail at Google is not touched, and Home "
    "Assistant backups taken earlier still hold this data.")


def revoke_token(token: str, timeout: float = 15.0) -> tuple[str, str]:
    """Ask Google to revoke `token`: ``(outcome, detail)``.

    ``revoked`` on 200. ``already_invalid`` on 400 ``invalid_token`` — Google's
    answer for a token that is revoked or expired, so no grant is live either
    way. Everything else, transport failures included, is ``failed``: the grant
    may still be live, and only a confirmed answer may say otherwise.
    """
    body = urllib.parse.urlencode({"token": token}).encode()
    req = urllib.request.Request(REVOKE_URI, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
    except urllib.error.HTTPError as exc:
        try:
            parsed = json.loads(exc.read().decode())
        except Exception:
            parsed = None
        code = parsed.get("error") if isinstance(parsed, dict) else None
        if exc.code == 400 and code == "invalid_token":
            return "already_invalid", ""
        return "failed", f"Google answered {exc.code} {code or ''}".strip()
    except (urllib.error.URLError, OSError) as exc:
        return "failed", f"could not reach Google ({exc})"
    if status == 200:
        return "revoked", ""
    return "failed", f"Google answered {status}"


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def _result(complete: bool, report: str) -> dict:
    return {"erasure": "complete" if complete else "incomplete", "report": report}


def erase(store, *, keep_sign_in: bool, revoke=revoke_token,
          before_delete=None) -> dict:
    """Run one eraser over `store`'s directory; the result Casa reads.

    Holds the collect lock throughout, so no collect pass or startup recovery
    can stage or promote a credential between the revocation and the delete.
    A held lock is an incomplete erasure, never a wait. `before_delete` runs
    under the lock just before the walk, for in-memory state that would
    otherwise write deleted files back.
    """
    data_dir = store.dir
    if not data_dir.exists():
        return _result(True, "Gmail held no data here. " + _HANDOFF_NOTE)
    with collect_lock(data_dir) as held:
        if not held:
            return _result(False, (
                "Nothing was erased: a Gmail sign-in is being finished right "
                "now. Run the erase again in a moment."))
        keep = set(SIGN_IN_FILES) if keep_sign_in else set()
        lines = []
        failed = []
        if not keep_sign_in:
            tokens = {}
            for name, cred in ((ACTIVE_NAME, store.load_active()),
                               (STAGED_NAME, store.load_staged())):
                if cred is not None:
                    tokens.setdefault(cred.refresh_token, []).append(name)
            for token, names in tokens.items():
                outcome, detail = revoke(token)
                if outcome == "failed":
                    keep.update(names)
                    failed.append(detail)
            if tokens and not failed:
                lines.append("Revoked Gmail's access at Google.")
        if before_delete is not None:
            before_delete()
        removed = []
        errors = []
        for entry in sorted(data_dir.iterdir()):
            if entry.name == LOCK_NAME or entry.name in keep:
                continue
            try:
                _remove(entry)
                removed.append(entry.name)
            except OSError as exc:
                errors.append(f"{entry.name} ({exc.strerror or exc})")
        left = sorted(e.name for e in data_dir.iterdir()
                      if e.name != LOCK_NAME and e.name not in keep)
        if not keep_sign_in and not failed and not left:
            try:
                (data_dir / LOCK_NAME).unlink()
            except FileNotFoundError:
                pass
    if removed:
        lines.append("Deleted " + ", ".join(removed) + ".")
    else:
        lines.append("There was no stored Gmail data to delete.")
    if keep_sign_in:
        lines.append("The Gmail sign-in is kept, so a reinstall stays connected.")
    if failed:
        lines.append(
            "Gmail's access could NOT be confirmed revoked ("
            + "; ".join(failed) + "), so its sign-in is kept to try again. "
            f"You can also remove access yourself at {PERMISSIONS_PAGE}.")
    if errors or left:
        lines.append("Could not delete: " + ", ".join(errors or left) + ".")
    lines.append(_HANDOFF_NOTE)
    return _result(not failed and not errors and not left, " ".join(lines))
