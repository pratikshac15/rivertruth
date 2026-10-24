"""Integration: the real web server and agent code against the fake Zetaris and fake model.

Contract: every kind of gauge gets the same page - a River check, the gauge's details and a chart of its newest data
(flow when any source reports flow, water level otherwise), memory, and the in-depth investigation.
"""
import json
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

from tests.fakes import END, Env

LATEST = END.strftime("%Y-%m-%d %H:00")
ALL_KINDS = {"TODA1": "flow", "CKLA1": "flow", "AVLA1": "flow", "MMTW2": "flow", "PMAF1": "level", "DRYX1": "none",
             "SPGS2": "level", "EMMC2": "flow", "OMRC1": "flow", "SFPN5": "flow"}


class Server:
    def __init__(self, env):
        self.env = env

    def __enter__(self):
        from web import server
        server.STATE.update(events=[], running=False, question=None, error=None)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        return False

    def get(self, path):
        try:
            with urllib.request.urlopen(self.url + path, timeout=30) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def post(self, path, body):
        from web import server
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "X-RiverTruth-Token": server.TOKEN})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())


class EveryGaugeKind(unittest.TestCase):
    def test_river_check_answers_for_every_kind(self):
        with Env() as env, Server(env) as s:
            for lid, kind in ALL_KINDS.items():
                code, d = s.post("/api/check", {"site": lid, "when": "this evening", "activity": "fishing"})
                self.assertEqual(code, 200, (lid, d))
                self.assertTrue(d["card"]["headline"].strip(), lid)
                self.assertTrue(any(l["url"].endswith(lid) for l in d["links"]), lid)
                if kind == "level":
                    self.assertEqual(d["facts"].get("mode"), "stage", lid)
                if kind == "none":
                    self.assertFalse(d["facts"]["ok"], lid)

    def test_hydrograph_for_every_kind_is_newest_and_has_the_right_series(self):
        with Env() as env, Server(env) as s:
            for lid, kind in ALL_KINDS.items():
                code, d = s.get(f"/api/hydrograph?site={lid}")
                self.assertEqual(code, 200, (lid, d))
                self.assertIn("level_ft", d["columns"], lid)
                self.assertEqual(d["site"]["lid"] if lid != "TODA1" else "TODA1", lid)
                self.assertTrue(d["site"].get("tz_label"), lid)
                if kind == "none":
                    self.assertEqual(d["rows"], [], lid)
                    self.assertFalse(d["has_flow"], lid)
                    continue
                self.assertEqual(d["rows"][-1][0], LATEST, lid)
                self.assertEqual(d["has_flow"], kind == "flow", lid)
                if kind == "level":
                    lv = d["columns"].index("level_ft")
                    self.assertEqual(d["rows"][-1][lv], env.gauges[lid].latest("nws_stage_ft"), lid)

    def test_memory_and_question_for_every_kind(self):
        with Env() as env, Server(env) as s:
            for lid in ALL_KINDS:
                code, d = s.get(f"/api/memory?site={lid}")
                self.assertEqual(code, 200, (lid, d))
                self.assertIn("RIVER TOPOLOGY", d["text"])

    def test_scouted_view_is_created_before_it_is_queried(self):
        with Env() as env, Server(env) as s:
            s.post("/api/check", {"site": "CKLA1"})
            calls = env.zet.calls
            created = next(i for i, c in enumerate(calls) if c.startswith("CREATE SCHEMASTORE VIEW hc_ckla1"))
            queried = next(i for i, c in enumerate(calls) if "FROM water.hc_ckla1" in c and c.startswith("SELECT"))
            self.assertLess(created, queried)


class Investigation(unittest.TestCase):
    def _run(self, lid):
        with Env() as env, Server(env) as s:
            code, _ = s.post("/api/run", {"question": "Why do they disagree?", "site": lid})
            self.assertEqual(code, 200)
            from web import server
            for _ in range(200):
                if not server.STATE["running"]:
                    break
                time.sleep(0.05)
            kinds = [e["kind"] for e in server.STATE["events"]]
            self.assertIn("run_complete", kinds, (lid, server.STATE.get("error")))
            self.assertIn("fact_check", kinds)
            report = next(e for e in server.STATE["events"] if e["kind"] == "run_complete")["report"]
            self.assertIn("## For the public", report)
            return env

    def test_investigation_runs_for_flagship_flow_and_level_gauges(self):
        for lid in ("TODA1", "CKLA1", "PMAF1"):
            with self.subTest(lid=lid):
                env = self._run(lid)
                self.assertIn("reporter", env.llm.calls)


class WholeUSMap(unittest.TestCase):
    def test_all_gauges_load_by_region_and_survive_rate_limits(self):
        with Env(catalog_extra=300) as env, Server(env) as s:
            env.zet.fail_429 = {"gauges_r_m95_p33_m85_p42": 2}  # this region is refused twice, then answers
            s.get("/api/gauges/all")
            for _ in range(200):
                code, d = s.get("/api/gauges/all")
                if d["status"] == "ready":
                    break
                time.sleep(0.05)
            self.assertEqual(d["status"], "ready", d.get("error"))
            lids = {g[0] for g in d["gauges"]}
            self.assertTrue({"TODA1", "CKLA1", "PMAF1"} <= lids)
            self.assertGreaterEqual(len(lids), 300)

    def test_area_query_does_not_remember_a_rate_limit_as_empty(self):
        with Env() as env, Server(env) as s:
            env.zet.fail_429 = {"gauges_w088_n32": 1}
            code, d = s.get("/api/gauges?xmin=-87.9&ymin=33.0&xmax=-87.2&ymax=33.4")
            self.assertEqual(code, 200)
            self.assertTrue(d["note"])  # tells the person NOAA is limiting requests
            code, d = s.get("/api/gauges?xmin=-87.9&ymin=33.0&xmax=-87.2&ymax=33.4")
            self.assertIn("TODA1", {g["lid"] for g in d["gauges"]})


class Ranking(unittest.TestCase):
    def test_scan_ranks_requested_gauges(self):
        with Env() as env, Server(env) as s:
            code, _ = s.post("/api/radar/scan", {"lids": ["TODA1", "CKLA1", "DRYX1"]})
            self.assertEqual(code, 200)
            for _ in range(200):
                code, d = s.get("/api/radar")
                if d["status"] == "done":
                    break
                time.sleep(0.05)
            ok = [r for r in d["rows"] if r.get("ok")]
            self.assertEqual(ok[0]["lid"], "TODA1")  # the pulsing river disagrees most
            self.assertTrue(any(r["lid"] == "DRYX1" and not r["ok"] for r in d["rows"]))


if __name__ == "__main__":
    unittest.main()


class SourcesThatPublishNothing(unittest.TestCase):
    """Live Oct 23: SPGS2 showed '-9,999 cfs' (the Model's 'no value' code read as flow) and EMMC2 failed with
    INVALID_EXTRACT_BASE_FIELD_TYPE (its USGS station returned no readings)."""

    def test_no_value_codes_never_reach_the_page(self):
        with Env() as env, Server(env) as s:
            code, d = s.post("/api/check", {"site": "SPGS2", "when": "this evening", "activity": "fishing"})
            self.assertEqual(code, 200, d)
            self.assertNotIn("9999", json.dumps(d).replace(",", ""))
            self.assertEqual(d["facts"].get("mode"), "stage")
            code, h = s.get("/api/hydrograph?site=SPGS2")
            self.assertEqual(code, 200, h)
            self.assertNotIn("9999", json.dumps(h["rows"]))
            self.assertFalse(h["has_flow"])

    def test_a_usgs_station_with_no_readings_is_left_out_of_the_view(self):
        with Env() as env, Server(env) as s:
            code, d = s.post("/api/check", {"site": "EMMC2", "when": "this evening", "activity": "fishing"})
            self.assertEqual(code, 200, d)
            self.assertTrue(d["facts"]["ok"])
            view = [c for c in env.zet.calls if c.startswith("CREATE SCHEMASTORE VIEW hc_emmc2")]
            self.assertEqual(len(view), 1)
            self.assertNotIn("timeSeries", view[0])
            self.assertTrue(any("09081600" in l["url"] for l in d["links"]))  # the official page is still linked


class EverySourceIsTestedBeforeTheViewIsBuilt(unittest.TestCase):
    """Sweep Oct 24: 12 of 64 gauges failed with INVALID_EXTRACT_BASE_FIELD_TYPE on "d" (an empty NWS or Model reply)
    or "p" (a USGS series with no points). Each source is now tried on its own and an empty one is left out."""

    def test_empty_sources_are_left_out_and_the_rest_are_kept(self):
        with Env() as env, Server(env) as s:
            for lid, gone, kept in (("OMRC1", "_observed_raw", "analysisAssimilation"),
                                    ("SFPN5", "analysisAssimilation", "timeSeries")):
                code, d = s.post("/api/check", {"site": lid, "when": "this evening", "activity": "fishing"})
                self.assertEqual(code, 200, (lid, d))
                self.assertTrue(d["facts"]["ok"], lid)
                view = [c for c in env.zet.calls if c.startswith(f"CREATE SCHEMASTORE VIEW hc_{lid.lower()}")]
                self.assertEqual(len(view), 1, lid)
                self.assertNotIn(gone, view[0], lid)
                self.assertIn(kept, view[0], lid)

    def test_a_gauge_with_no_river_source_at_all_says_no_data_instead_of_an_error(self):
        with Env() as env, Server(env) as s:
            code, d = s.post("/api/check", {"site": "DRYX1", "when": "this evening", "activity": "fishing"})
            self.assertEqual(code, 200, d)
            self.assertFalse(d["facts"]["ok"])
