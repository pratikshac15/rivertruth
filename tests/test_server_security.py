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


class PlainErrorsForPeople(unittest.TestCase):
    """Oct 24: a River check showed 'REST API call failed: api.water.noaa.gov URL: GET https://...' to the person.
    Technical failures now reach the page as one plain sentence; the full error still goes to the log."""

    LIVE = ("REST API call failed: api.water.noaa.gov URL: GET https://api.water.noaa.gov/nwps/v1/gauges/WDNC2 "
            "Content-Type: application/x-www-form-urlencoded Headers: Map(User-Agent -> rivertruth-hackathon-demo)")

    def test_messages(self):
        from web import server
        for raw, want in ((self.LIVE, "didn't answer"),
                          ("REST API call failed: Server returned HTTP response code: 429 for URL", "too many requests"),
                          ("org.apache.hive.service.cli.HiveSQLException: Error running query: [INVALID_EXTRACT_BASE_FIELD_TYPE]",
                           "couldn't read"),
                          ("HTTPConnectionPool(host='localhost', port=8888): Read timed out.", "didn't answer")):
            with self.subTest(raw=raw[:40]):
                msg = server.plain_error(Exception(raw))
                self.assertIn(want, msg)
                for tech in ("http", "Exception", "REST", "Map(", "INVALID_"):
                    self.assertNotIn(tech, msg)
        self.assertEqual(server.plain_error(Exception("'XX' does not look like an NWS gauge code (e.g. CKLA1).")),
                         "'XX' does not look like an NWS gauge code (e.g. CKLA1).")  # already plain: kept

    def test_a_failing_check_shows_the_plain_sentence_and_logs_the_full_error(self):
        with Env() as env, Server(env) as s:
            from agent import zetaris
            real = env.zet.run_sql

            def fail(sql, **k):
                if "ckla1_gauge_raw" in sql:
                    raise zetaris.SQLError(self.LIVE)
                return real(sql, **k)
            zetaris.run_sql = fail
            from web import server
            body = json.dumps({"site": "CKLA1", "when": "today", "activity": "fishing"}).encode()
            port = s.httpd.server_address[1]
            code, data = raw(s, "POST", "/api/check", body=body,
                             headers={"Content-Type": "application/json", "Host": f"127.0.0.1:{port}",
                                      "X-RiverTruth-Token": server.TOKEN})
            self.assertEqual(code, 503)
            err = json.loads(data)["error"]
            self.assertIn("didn't answer", err)
            self.assertNotIn("api.water.noaa.gov", err)
