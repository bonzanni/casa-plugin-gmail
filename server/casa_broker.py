"""Hand the Google sign-in link, or a file, to Casa, which posts it in the
user's chat.

Casa >= 0.318.0's result contract lets a `capability` tool declare that a slot
it provides is `delivers`-ed as an `operator_link`. The tool
does not return the URL. During the call it DEPOSITS the URL with Casa's
broker, over the internal Unix socket named by `$CASA_BROKER_SOCKET`, as the
client named by `$CASA_BROKER_CLIENT` (Casa puts both in this server's
environment). Casa answers with a reference, which the tool returns in the
slot's field. After the result passes Casa's structural check, Casa posts ONE
message to the user's chat: a link whose text is the label and the host Casa
prints from the URL, with the caption beneath it. So the URL never enters the
assistant's context, and never the chat or topic the assistant happens to be in.

Protocol (Casa `result_broker.py`, 0.318.0):

    POST /internal/broker/deposit
         {"client", "slot", "value", "caption"?, "label"?, "tool_use_id"?}
      -> {"reference": "casa-cap-<32 hex>"} | {"error": "<code>"}

Casa refuses a label over 40 characters, a caption over 200, and either one if
it is not a single printable line or contains `://` or `www.`. It also refuses
a label with text that reads as a domain. This plugin's label and caption are
fixed literals, checked against those rules by the tests.

A file goes the same way as an `operator_file` (Casa >= 0.339.0; the
`filename` member >= 0.344.0): the value is the path of a file staged in Casa's
plugin outbox, with `"kind"` (a Casa media kind) and optionally `"filename"`
and `"caption"`.

Every deposit made while a tool call is served names that call: Claude Code
sends the call's id in the request's metadata (`_meta["claudecode/toolUseId"]`)
and the body gains `"tool_use_id": <id>`. With no id (an older Claude Code, or
a call made with no model) the member is left out and the body is as above.
Casa versions that do not read it ignore it.

Nothing Casa sends back is echoed: a reference must have Casa's exact shape,
and an error must look like one of Casa's codes, or it is reported by a fixed
label. Standard library only, apart from reading the call id from the `mcp`
server serving the call.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import socket

ENV_CLIENT = "CASA_BROKER_CLIENT"
ENV_SOCKET = "CASA_BROKER_SOCKET"
REFERENCE_RE = re.compile(r"^casa-cap-[0-9a-f]{32}$")
DEPOSIT_ROUTE = "/internal/broker/deposit"
TIMEOUT_S = 10.0
MAX_RESPONSE_BYTES = 64 * 1024
_CODE_RE = re.compile(r"^[a-z][a-z_]{0,39}$")
CALL_ID_KEY = "claudecode/toolUseId"
MAX_CALL_ID_CHARS = 128


class DepositFailed(Exception):
    """The link or file was not accepted for delivery. `code` is a Casa error code or
    one of this module's own fixed labels, never text Casa or the transport
    wrote."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class _UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path: str):
        super().__init__("localhost", timeout=TIMEOUT_S)
        self._path = path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(TIMEOUT_S)
        try:
            sock.connect(self._path)
        except BaseException:
            sock.close()
            raise
        self.sock = sock


def call_id() -> str | None:
    """The id of the tool call this process is serving, or None.

    Read from the request context the `mcp` server keeps for the call being
    handled (a tool runs inside it), at the moment of asking. Only a non-empty
    string of at most 128 characters is an id; anything else, no call being
    served, or any failure to read it gives None."""
    try:
        from mcp.server.lowlevel.server import request_ctx
        meta = request_ctx.get().meta
        value = (meta.model_extra or {}).get(CALL_ID_KEY) if meta else None
    except Exception:                            # noqa: BLE001 — no id, no harm
        return None
    if isinstance(value, str) and 0 < len(value) <= MAX_CALL_ID_CHARS:
        return value
    return None


def deposit_link(slot: str, url: str, *, label: str, caption: str) -> str:
    """Deposit `url` in `slot` and return Casa's reference. Raises
    `DepositFailed` as `_deposit` does."""
    return _deposit({"slot": slot, "value": url, "label": label, "caption": caption})


def deposit_file(slot: str, path: str, *, kind: str, caption: str, filename: str) -> str:
    """Deposit a file staged in Casa's plugin outbox as an `operator_file`
    (Casa >= 0.344.0): Casa claims `path`, checks it against media `kind` and
    sends it to the user's chat as `filename`, with `caption` (sent only when
    non-empty) beneath Casa's own label. Raises `DepositFailed` as `_deposit`
    does."""
    fields = {"slot": slot, "value": path, "kind": kind, "filename": filename}
    if caption:
        fields["caption"] = caption
    return _deposit(fields)


def _deposit(fields: dict) -> str:
    """POST one deposit, naming the call being served when there is one, and
    return Casa's reference.

    Raises `DepositFailed` with: `broker_env_missing` when Casa gave this
    server no broker; `broker_unreachable:<ExceptionClass>` on a transport
    failure; `broker_bad_response` when the answer is not one JSON object
    within the size bound; Casa's own error code; or `unrecognized_error`
    when the answer carries neither a well-formed reference nor a code.
    """
    path = os.environ.get(ENV_SOCKET, "")
    client = os.environ.get(ENV_CLIENT, "")
    if not path or not client:
        raise DepositFailed("broker_env_missing")
    body = {"client": client, **fields}
    tool_use_id = call_id()
    if tool_use_id is not None:
        body["tool_use_id"] = tool_use_id
    conn = _UnixHTTP(path)
    try:
        conn.request("POST", DEPOSIT_ROUTE,
                     body=json.dumps(body).encode("utf-8"),
                     headers={"Content-Type": "application/json"})
        raw = conn.getresponse().read(MAX_RESPONSE_BYTES + 1)
    except Exception as exc:                     # noqa: BLE001 — the class only
        raise DepositFailed("broker_unreachable:%s" % type(exc).__name__) from None
    finally:
        conn.close()
    try:
        answer = json.loads(raw.decode("utf-8")) \
            if len(raw) <= MAX_RESPONSE_BYTES else None
    except ValueError:
        answer = None
    if not isinstance(answer, dict):
        raise DepositFailed("broker_bad_response")
    reference = answer.get("reference")
    if isinstance(reference, str) and REFERENCE_RE.fullmatch(reference):
        return reference
    error = answer.get("error")
    if isinstance(error, str) and _CODE_RE.fullmatch(error):
        raise DepositFailed(error)
    raise DepositFailed("unrecognized_error")
