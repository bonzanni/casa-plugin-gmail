"""`casa_broker` against a fake broker on a real Unix socket.

The fake speaks Casa's deposit route the way Casa's aiohttp handler does: one
HTTP POST, a JSON body in, a JSON object out. What it answers is chosen per
test, so the helper is exercised on every shape Casa can send and on the
shapes a broken or hostile one could; whatever the failure, the link never
comes back out of the helper.
"""
import http.server
import json
import os
import socketserver
import tempfile
import threading
import unittest

import casa_broker

URL = "https://accounts.google.com/o/oauth2/v2/auth?state=" + "c" * 64
REFERENCE = "casa-cap-" + "0123456789abcdef" * 2
CLIENT = "0f" * 16


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    block_on_close = False

    def handle_error(self, request, client_address):
        pass                  # a client that timed out and left is the test


class FakeBroker:
    """A Unix-socket HTTP server. `answer(body) -> bytes` decides the reply."""

    def __init__(self, path, answer):
        self.requests = []            # (method, path, parsed body)
        broker = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def address_string(self):          # a Unix peer has no host
                return "unix"

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                broker.requests.append((self.command, self.path, body))
                data = answer(body)
                if data is None:                   # hang up without a reply
                    self.close_connection = True
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = _Server(path, Handler)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.02},
                                       daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _json(obj):
    return json.dumps(obj).encode("utf-8")


class DepositTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.sock = os.path.join(self.dir.name, "internal.sock")
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        os.environ[casa_broker.ENV_SOCKET] = self.sock
        os.environ[casa_broker.ENV_CLIENT] = CLIENT

    def serve(self, answer):
        broker = FakeBroker(self.sock, answer)
        self.addCleanup(broker.close)
        return broker

    def deposit(self):
        return casa_broker.deposit_link(
            "auth_url", URL, label="Sign in with Google",
            caption="Open in a real browser")

    def refused(self):
        with self.assertRaises(casa_broker.DepositFailed) as caught:
            self.deposit()
        code = caught.exception.code
        # Whatever the failure, the value never comes back out of the helper.
        self.assertNotIn(URL, code)
        self.assertNotIn(URL, str(caught.exception))
        self.assertNotIn("accounts.google", repr(caught.exception))
        return code

    def test_a_deposit_posts_casas_body_and_returns_the_reference(self):
        broker = self.serve(lambda body: _json({"reference": REFERENCE}))
        self.assertEqual(self.deposit(), REFERENCE)
        self.assertEqual(len(broker.requests), 1)
        method, path, body = broker.requests[0]
        self.assertEqual((method, path), ("POST", "/internal/broker/deposit"))
        self.assertEqual(body, {
            "client": CLIENT, "slot": "auth_url", "value": URL,
            "label": "Sign in with Google",
            "caption": "Open in a real browser"})

    def test_casas_error_code_is_the_failure_code(self):
        for code in ("bad_link", "bad_caption", "bad_label",
                     "no_call_in_flight", "no_identity", "ambiguous_call",
                     "slot_already_deposited", "value_too_large"):
            with self.subTest(code=code):
                self.dir2 = tempfile.TemporaryDirectory()
                self.addCleanup(self.dir2.cleanup)
                os.environ[casa_broker.ENV_SOCKET] = os.path.join(
                    self.dir2.name, "s.sock")
                self.sock = os.environ[casa_broker.ENV_SOCKET]
                self.serve(lambda body, c=code: _json({"error": c}))
                self.assertEqual(self.refused(), code)

    def test_no_broker_in_the_environment_is_reported_without_connecting(self):
        broker = self.serve(lambda body: _json({"reference": REFERENCE}))
        for var in (casa_broker.ENV_SOCKET, casa_broker.ENV_CLIENT):
            with self.subTest(unset=var):
                saved = os.environ.pop(var)
                try:
                    self.assertEqual(self.refused(), "broker_env_missing")
                finally:
                    os.environ[var] = saved
        self.assertEqual(broker.requests, [])

    def test_an_absent_socket_is_unreachable_by_class_name(self):
        self.assertEqual(self.refused(),
                         "broker_unreachable:FileNotFoundError")

    def test_a_broker_that_hangs_up_is_unreachable(self):
        self.serve(lambda body: None)
        self.assertTrue(self.refused().startswith("broker_unreachable:"))

    def test_a_broker_that_never_answers_times_out(self):
        release = threading.Event()
        self.serve(lambda body: (release.wait(5), _json({}))[1])
        self.addCleanup(release.set)
        real = casa_broker.TIMEOUT_S
        casa_broker.TIMEOUT_S = 0.2
        self.addCleanup(setattr, casa_broker, "TIMEOUT_S", real)
        self.assertEqual(self.refused(), "broker_unreachable:TimeoutError")

    def test_an_answer_that_is_not_one_json_object_is_a_bad_response(self):
        for data in (b"not json", _json([REFERENCE]), _json(REFERENCE),
                     b"\xff\xfe", b"{" + b" " * (casa_broker.MAX_RESPONSE_BYTES + 1) + b"}"):
            with self.subTest(data=data[:20]):
                d = tempfile.TemporaryDirectory()
                self.addCleanup(d.cleanup)
                self.sock = os.environ[casa_broker.ENV_SOCKET] = os.path.join(d.name, "s")
                self.serve(lambda body, x=data: x)
                self.assertEqual(self.refused(), "broker_bad_response")

    def test_an_answer_over_the_size_bound_is_refused_even_when_well_formed(self):
        # Exactly one byte over the bound, a whole valid object with a good
        # reference: only the bound itself refuses it (a truncated read of a
        # longer answer would fail to parse anyway, and prove nothing).
        head = json.dumps({"reference": REFERENCE, "pad": ""})[:-2]
        pad = casa_broker.MAX_RESPONSE_BYTES + 1 - len(head) - 2
        data = (head + "x" * pad + '"}').encode("utf-8")
        self.assertEqual(len(data), casa_broker.MAX_RESPONSE_BYTES + 1)
        self.assertEqual(json.loads(data)["reference"], REFERENCE)
        self.serve(lambda body: data)
        self.assertEqual(self.refused(), "broker_bad_response")

    def test_nothing_the_broker_sends_back_is_echoed(self):
        # A broker that reflects the value, in either field, or in a
        # reference-shaped string with the value appended, is reported by a
        # fixed label: the helper returns only a reference of casa's exact
        # shape, and an error only when it looks like one of casa's codes.
        answers = (
            lambda body: _json({"reference": body["value"]}),
            lambda body: _json({"reference": REFERENCE + body["value"]}),
            lambda body: _json({"error": body["value"]}),
            lambda body: _json({"error": "bad_link " + body["value"]}),
            lambda body: _json({"error": "Bad_Link"}),
            lambda body: _json({}),
        )
        for i, answer in enumerate(answers):
            with self.subTest(answer=i):
                d = tempfile.TemporaryDirectory()
                self.addCleanup(d.cleanup)
                self.sock = os.environ[casa_broker.ENV_SOCKET] = os.path.join(d.name, "s")
                self.serve(answer)
                self.assertEqual(self.refused(), "unrecognized_error")
