"""Hand the Google sign-in link to Casa, which posts it in the user's chat.

Casa >= 0.318.0's result contract lets a `capability` tool declare that a slot
it provides is `delivers`-ed as an `operator_link` (ha-casa-app#1015). The tool
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
         {"client", "slot", "value", "caption"?, "label"?}
      -> {"reference": "casa-cap-<32 hex>"} | {"error": "<code>"}

Casa refuses a label over 40 characters, a caption over 200, and either one if
it is not a single printable line or contains `://` or `www.`. It also refuses
a label with text that reads as a domain. This plugin's label and caption are
fixed literals, checked against those rules by the tests.

Nothing Casa sends back is echoed: a reference must have Casa's exact shape,
and an error must look like one of Casa's codes, or it is reported by a fixed
label. Standard library only.
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


class DepositFailed(Exception):
    """The link was not accepted for delivery. `code` is a Casa error code or
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


def deposit_link(slot: str, url: str, *, label: str, caption: str) -> str:
    """Deposit `url` in `slot` and return Casa's reference.

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
    body = {"client": client, "slot": slot, "value": url,
            "label": label, "caption": caption}
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
