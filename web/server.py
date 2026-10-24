"""RiverTruth web app: ask a question, watch the agents investigate live, read the evidence-backed answer.

Standard library only.   Run:  python3 -m web.server    then open http://localhost:8765
"""
import hmac
import json
import os
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent import config, gauges, log, memory, radar, rivercheck, runctl, scout, sites, snapshot, zetaris  # noqa: E402
from agent.cases import CASES, site_question  # noqa: E402
from agent.orchestrator import run  # noqa: E402
from agent.sites import FLAGSHIP, WATCHLIST  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PORT", "8765"))

STATE = {"events": [], "running": False, "question": None, "error": None}
TOKEN = secrets.token_urlsafe(24)   # per server session; embedded in the page, required on every POST
ALLOWED_HOSTS = {"localhost", "127.0.0.1", "[::1]"}
MAX_BODY = 64 * 1024
MAX_QUESTION = 1000
LOCK = threading.Lock()



def _capture(rec):
    with LOCK:
        STATE["events"].append(rec)


log.subscribe(_capture)


def _run_in_background(question, site):
    try:
        run(question, site)
    except Exception as e:  # noqa: BLE001
        log.event("run_error", error=str(e)[:500])
        STATE["error"] = str(e)
    finally:
        STATE["running"] = False


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---- request hygiene: only this app's own page may talk to this server
    def _host_ok(self):
        host = (self.headers.get("Host") or "").strip().lower()
        name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
        return name in ALLOWED_HOSTS  # a spoofed Host (DNS rebinding) is refused

    def _origin_ok(self):
        origin = self.headers.get("Origin")
        if not origin:
            return True  # same-origin fetches and non-browser clients may omit it; the token still applies
        u = urlparse(origin)
        return u.scheme == "http" and (u.hostname or "") in {"localhost", "127.0.0.1", "::1"}

    def _refuse(self, code, msg):
        self._json({"error": msg}, code)
        return None

    def _guard_get(self):
        if self._host_ok():
            return True
        self._refuse(403, "This server only answers requests for localhost.")
        return False

    def _read_json(self):
        """Validate a POST and return its JSON body, or send the error and return None."""
        if not self._host_ok():
            return self._refuse(403, "This server only answers requests for localhost.")
        if not self._origin_ok():
            return self._refuse(403, "Requests from other websites are not allowed.")
        if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            return self._refuse(415, "Send JSON (Content-Type: application/json).")
        if not hmac.compare_digest(self.headers.get("X-RiverTruth-Token") or "", TOKEN):
            return self._refuse(403, "Missing or wrong session token: reload the page.")
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if n < 0 or n > MAX_BODY:
            left = min(max(n, 0), 8 * 1024 * 1024)  # drain what the client is sending so it gets a clean answer
            while left > 0:
                chunk = self.rfile.read(min(left, 65536))
                if not chunk:
                    break
                left -= len(chunk)
            self.close_connection = True
            return self._refuse(413, "Request body too large.")
        try:
            data = json.loads(self.rfile.read(n) or b"{}") if n else {}
        except (ValueError, UnicodeDecodeError):
            return self._refuse(400, "The request body is not valid JSON.")
        if not isinstance(data, dict):
            return self._refuse(400, "The request body must be a JSON object.")
        return data

    def do_GET(self):  # noqa: N802
        if not self._guard_get():
            return
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                body = f.read().replace(b"__RT_TOKEN__", TOKEN.encode())
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif u.path == "/api/cases":
            self._json([{"key": k, "name": n, "question": q} for k, (n, q) in CASES.items()])
        elif u.path == "/api/sites":
            known = {g["lid"]: g for g in memory.known_sites()}
            out = [{"lid": FLAGSHIP.lid, "name": FLAGSHIP.name, "registered": True, "flagship": True}]
            for lid in WATCHLIST:
                if lid != FLAGSHIP.lid:
                    g = known.get(lid, {})
                    out.append({"lid": lid, "name": g.get("name", lid), "registered": lid in known, "flagship": False})
            for lid, g in known.items():
                if lid not in WATCHLIST:
                    out.append({"lid": lid, "name": g.get("name", lid), "registered": True, "flagship": False})
            self._json(out)
        elif u.path == "/api/gauges/all":
            a = gauges.all_gauges()
            self._json({"status": a["status"], "at": a["at"], "error": a["error"], "done": a["done"], "total": a["total"],
                        "fields": ["lid", "name", "lat", "lon", "flood", "stage_ft", "flow_cfs"], "gauges": a["gauges"]})
        elif u.path == "/api/gauges":
            q = parse_qs(u.query)
            try:
                box = [float(q[k][0]) for k in ("xmin", "ymin", "xmax", "ymax")]
                self._json(gauges.nearby(*box))
            except (KeyError, ValueError):
                self._json({"error": "xmin, ymin, xmax, ymax are required"}, 400)
            except Exception as e:  # noqa: BLE001
                self._json({"error": str(e)[:300]}, 503)
        elif u.path == "/api/radar":
            self._json(radar.STATE)
        elif u.path == "/api/status":
            self._json({"running": STATE["running"], "sample_mode": config.SAMPLE_MODE,
                        "fast_model": config.FAST_MODEL, "deep_model": config.DEEP_MODEL,
                        "world": memory.world().stats()})
        elif u.path == "/api/hydrograph":
            try:
                lid = (parse_qs(u.query).get("site") or [FLAGSHIP.lid])[0]
                site = scout.ensure_site(lid)
                res = zetaris.run_sql(site.rows_sql(), purpose="ui hydrograph")
                # GENERIC order: hr, usgs_cfs, usgs_peak, usgs_stage, nwm, nws_cfs, nws_stage, rain
                rows = [[r[0], r[1], r[4], r[5], r[7], r[6] if r[6] is not None else r[3]] for r in sites.no_missing(res["rows"])]
                has_flow = any(v is not None for r in rows for v in r[1:4])
                self._json({"columns": ["hr_utc", "usgs_cfs", "nwm_cfs", "nws_cfs", "rain_mm", "level_ft"],
                            "rows": rows, "has_flow": has_flow,
                            "site": {**site.to_dict(), "tz_label": site.tz_now()[0], "tz_offset": site.tz_now()[1]},
                            "question": None if site.flagship else site_question(site.name)})
            except Exception as e:  # noqa: BLE001
                self._json({"error": str(e)[:300]}, 503)
        elif u.path == "/api/memory":
            try:
                lid = (parse_qs(u.query).get("site") or [FLAGSHIP.lid])[0]
                if not STATE["running"]:
                    memory.use_site(scout.ensure_site(lid))
                self._json({"text": memory.prior_knowledge(), "stats": memory.world().stats()})
            except Exception as e:  # noqa: BLE001
                self._json({"error": str(e)[:300]}, 503)
        elif u.path == "/api/events":
            # Server-sent events: replay from ?since=N, then stream new events as they arrive.
            since = int((parse_qs(u.query).get("since") or ["0"])[0])
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            i = since
            try:
                while True:
                    with LOCK:
                        batch = STATE["events"][i:]
                    for ev in batch:
                        self.wfile.write(f"id: {i}\ndata: {json.dumps(ev, default=str)}\n\n".encode())
                        i += 1
                    if batch:
                        self.wfile.flush()
                    elif not STATE["running"]:
                        self.wfile.write(b"event: idle\ndata: {}\n\n")
                        self.wfile.flush()
                        return
                    time.sleep(0.4)
            except (BrokenPipeError, ConnectionResetError):
                return
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        data = self._read_json()
        if data is None:
            return
        if path == "/api/cancel":
            running = STATE["running"]
            if running:
                runctl.cancel()
                log.event("say", text="Cancel requested: stopping at the next step.")
            return self._json({"cancelling": running})
        if path == "/api/radar/scan":
            if STATE["running"]:
                return self._json({"error": "Wait for the investigation to finish, then scan."}, 409)
            lids = [str(x).strip().upper() for x in (data.get("lids") or []) if str(x).strip()][:8]
            radar.scan_async(lids or None)
            return self._json({"started": True})
        if path == "/api/check":
            lid = (data.get("site") or "").strip().upper()
            if not lid:
                return self._json({"error": "Pick a gauge first."}, 400)
            try:
                return self._json(rivercheck.check(lid, (data.get("when") or "")[:80], (data.get("activity") or "")[:40],
                                                  (data.get("flood") or "")[:30]))
            except Exception as e:  # noqa: BLE001
                return self._json({"error": str(e)[:300]}, 503)
        if path != "/api/run":
            return self._json({"error": "not found"}, 404)
        q = (data.get("question") or "").strip()
        site = (data.get("site") or FLAGSHIP.lid).strip().upper()
        if not q:
            return self._json({"error": "Type a question or pick a case first."}, 400)
        if len(q) > MAX_QUESTION:
            return self._json({"error": f"Please keep the question under {MAX_QUESTION} characters."}, 400)
        if not re_lid(site):
            return self._json({"error": "Unknown gauge code."}, 400)
        with LOCK:
            if STATE["running"]:
                return self._json({"error": "An investigation is already running. Wait for it to finish."}, 409)
            STATE.update(events=[], running=True, question=q, error=None)
        threading.Thread(target=_run_in_background, args=(q, site), daemon=True).start()
        self._json({"started": True})


def re_lid(s):
    import re
    return bool(re.fullmatch(r"[A-Z0-9]{4,6}", s or ""))


def main():
    gauges.all_gauges()  # start loading the whole-US map layer in the background
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"RiverTruth UI on http://localhost:{PORT}  (sample mode: {config.SAMPLE_MODE})")
    srv.serve_forever()


if __name__ == "__main__":
    main()
