"""The local server only serves this app's own page (audit blocker 2: CSRF, DNS rebinding, bad bodies)."""
import http.client
import json
import unittest

from tests.fakes import Env
from tests.test_integration_server import Server


def raw(server, method, path, body=None, headers=None):
    host, port = server.httpd.server_address
    c = http.client.HTTPConnection(host, port, timeout=30)
    c.request(method, path, body=body, headers=headers or {})
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, data


class Security(unittest.TestCase):
    def setUp(self):
        import threading
        from web import server
        self.token = server.TOKEN
        self.crashes = []
        self._hook = threading.excepthook
        threading.excepthook = lambda a: self.crashes.append(repr(a.exc_value))
        import socketserver
        self._handle_error = socketserver.BaseServer.handle_error
        socketserver.BaseServer.handle_error = lambda srv, req, addr: self.crashes.append("handler crashed")

    def tearDown(self):
        import socketserver
        import threading
        threading.excepthook = self._hook
        socketserver.BaseServer.handle_error = self._handle_error
        self.assertEqual(self.crashes, [], "a server thread raised an error")

    def good_headers(self, server, **extra):
        port = server.httpd.server_address[1]
        return {"Content-Type": "application/json", "Host": f"127.0.0.1:{port}", "Origin": f"http://127.0.0.1:{port}",
                "X-RiverTruth-Token": self.token, **extra}

    def test_page_carries_the_session_token(self):
        with Env() as env, Server(env) as s:
            code, body = raw(s, "GET", "/", headers={"Host": f"localhost:{s.httpd.server_address[1]}"})
            self.assertEqual(code, 200)
            self.assertIn(self.token.encode(), body)

    def test_spoofed_host_is_refused_dns_rebinding(self):
        with Env() as env, Server(env) as s:
            for path in ("/", "/api/status", "/api/events?since=0", "/api/gauges/all"):
                code, _ = raw(s, "GET", path, headers={"Host": "evil.example:80"})
                self.assertEqual(code, 403, path)

    def test_text_plain_post_from_another_site_is_refused_csrf(self):
        with Env() as env, Server(env) as s:
            body = json.dumps({"question": "x", "site": "TODA1"})
            code, _ = raw(s, "POST", "/api/run", body, {"Content-Type": "text/plain", "Origin": "http://evil.example",
                                                         "Host": f"127.0.0.1:{s.httpd.server_address[1]}"})
            self.assertIn(code, (403, 415))
            from web import server
            self.assertFalse(server.STATE["running"])

    def test_foreign_origin_is_refused_even_with_json(self):
        with Env() as env, Server(env) as s:
            h = self.good_headers(s, Origin="http://evil.example")
            code, _ = raw(s, "POST", "/api/check", json.dumps({"site": "TODA1"}), h)
            self.assertEqual(code, 403)

    def test_missing_or_wrong_token_is_refused(self):
        with Env() as env, Server(env) as s:
            for tok in (None, "wrong"):
                h = self.good_headers(s)
                if tok is None:
                    del h["X-RiverTruth-Token"]
                else:
                    h["X-RiverTruth-Token"] = tok
                code, _ = raw(s, "POST", "/api/check", json.dumps({"site": "TODA1"}), h)
                self.assertEqual(code, 403, tok)

    def test_malformed_json_is_a_400_not_a_dropped_connection(self):
        with Env() as env, Server(env) as s:
            code, body = raw(s, "POST", "/api/check", "{not json", self.good_headers(s))
            self.assertEqual(code, 400)
            self.assertIn(b"error", body)

    def test_oversized_body_and_question_are_refused(self):
        with Env() as env, Server(env) as s:
            code, _ = raw(s, "POST", "/api/run", json.dumps({"question": "x" * 2_000_000}), self.good_headers(s))
            self.assertEqual(code, 413)
            code, _ = raw(s, "POST", "/api/run", json.dumps({"question": "x" * 1200, "site": "TODA1"}), self.good_headers(s))
            self.assertEqual(code, 400)

    def test_a_proper_request_from_the_page_works(self):
        with Env() as env, Server(env) as s:
            code, body = raw(s, "POST", "/api/check", json.dumps({"site": "CKLA1"}), self.good_headers(s))
            self.assertEqual(code, 200, body[:300])


if __name__ == "__main__":
    unittest.main()
