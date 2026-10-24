"""Unit tests: River check facts (all computed in code) and the guards on the model's wording."""
import json
import math
import unittest
from datetime import timedelta

from tests.fakes import END, Env, Gauge, hours, make_gauges


def gauges_with(lid, rows, **kw):
    g = make_gauges()
    g[lid] = Gauge(lid, kw.pop("name", "Test River at Somewhere"), 35.0, -90.0, rows=rows, **kw)
    return g


def check(lid, gauges, flood="no_flooding", llm_reply=None):
    with Env(gauges) as env:
        if llm_reply is not None:
            env.llm.chat = lambda role, m, **k: llm_reply  # noqa: E731
            from agent import llm
            llm.chat = env.llm.chat
        from agent import rivercheck
        return rivercheck.check(lid, "this evening", "kayaking", flood)


class Patterns(unittest.TestCase):
    def test_daily_surge_at_a_fixed_hour_is_found_with_its_size(self):
        d = check("TODA1", make_gauges())
        p = d["facts"]["pattern"]
        self.assertEqual(p["kind"], "daily surge")
        self.assertEqual(p["local_hour"], "9 PM")
        self.assertGreaterEqual(p["times_higher"], 10)
        self.assertGreaterEqual(p["days"], 5)

    def test_other_sources_are_reported_as_missing_or_late(self):
        p = check("TODA1", make_gauges())["facts"]["pattern"]
        by = {o["source"]: o["summary"] for o in p["other_sources"]}
        nws = next(v for k, v in by.items() if k.startswith("NWS"))
        nwm = next(v for k, v in by.items() if k.startswith("National Water Model"))
        self.assertIn("did not show it", nws)
        self.assertIn("1 h later", nwm)

    def test_gentle_daily_rise_is_a_regular_daily_rise(self):
        rows = hours(168, lambda i, t: {"nws_cfs": 1100 + (1200 * math.exp(-((t.hour - 5 - 21) % 24) / 2)
                                                           if (t.hour - 5) % 24 in (21, 22, 23, 0, 1, 2, 3) else 0)})
        p = check("HOLT1", gauges_with("HOLT1", rows))["facts"]["pattern"]
        self.assertEqual(p["kind"], "regular daily rise")
        self.assertEqual(p["local_hour"], "9 PM")

    def test_steady_river_has_no_pattern(self):
        self.assertIsNone(check("CKLA1", make_gauges())["facts"]["pattern"])

    def test_recent_rise_after_rain_is_explained(self):
        rows = hours(168, lambda i, t: {"usgs_cfs": 500.0 if i < 140 else 500 + min(1, (i - 140) / 10) * 450,
                                        "rain_mm": 3.0 if 130 < i < 136 else 0.0})
        f = check("GWSC2", gauges_with("GWSC2", rows, usgs_id="09085000", tz="MST7MDT"))["facts"]
        rise = f["now"]["recent_rise"]
        self.assertEqual(rise["from_cfs"], 500)
        self.assertEqual(rise["to_cfs"], 950)
        self.assertGreater(rise["rain_mm_from_day_before"], 0)
        self.assertIn("MDT", f["now"]["at_local"])


class Trust(unittest.TestCase):
    def test_measured_usgs_flow_is_trusted_first(self):
        self.assertTrue(check("CKLA1", make_gauges())["facts"]["trusted"].startswith("USGS measured flow"))

    def test_nws_is_trusted_when_usgs_reports_stage_only(self):
        f = check("AVLA1", make_gauges())["facts"]
        self.assertTrue(f["trusted"].startswith("NWS gauge flow"))
        self.assertIn("USGS gauge here reports no flow", f["why_trusted"])

    def test_gauge_without_usgs_or_model_says_so(self):
        f = check("MMTW2", make_gauges())["facts"]
        self.assertIn("no USGS gauge is linked", f["why_trusted"])
        self.assertTrue(all("not available for this gauge" in o["status"] for o in f["others"]))

    def test_level_only_gauge_gets_a_level_check(self):
        f = check("PMAF1", make_gauges())["facts"]
        self.assertEqual(f["mode"], "stage")
        self.assertIn("0.09 ft", f["now"]["in_words"])

    def test_no_data_is_explained_not_crashed(self):
        d = check("DRYX1", make_gauges())
        self.assertFalse(d["facts"]["ok"])
        self.assertTrue(d["card"]["headline"])


class Wording(unittest.TestCase):
    def test_dry_stream_is_not_described_as_a_typical_flow_of_zero(self):
        rows = hours(168, lambda i, t: {"usgs_cfs": 0.0 if i < 120 else 14000.0})
        words = check("MOEA3", gauges_with("MOEA3", rows, usgs_id="09401260", tz="MST"))["facts"]["now"]["in_words"]
        self.assertIn("dry or nearly dry", words)
        self.assertNotIn("typical flow is 0", words)

    def test_now_line_is_always_written_by_code(self):
        reply = json.dumps({"headline": "Fine.", "trust": "Use it.", "pattern": "Usual.", "plan": "Go.",
                            "now": "It is 99,999 cfs and calm."})
        d = check("CKLA1", make_gauges(), llm_reply=reply)
        self.assertEqual(d["card"]["now"], d["facts"]["now"]["in_words"])

    def test_a_number_not_in_the_facts_is_replaced_by_the_code_sentence(self):
        reply = json.dumps({"headline": "River at 2,002 cfs, above the 2,750 cfs high.", "trust": "Use it.",
                            "pattern": "Usual.", "plan": "Go."})
        d = check("CKLA1", make_gauges(), llm_reply=reply)
        self.assertNotIn("2,002", d["card"]["headline"])
        self.assertIn("code wrote: headline", d["written_by"])

    def test_flood_advice_is_never_left_to_the_model(self):
        reply = json.dumps({"headline": "Great day for it.", "trust": "Use it.", "pattern": "Usual.", "plan": "Go fishing."})
        for flood in ("minor", "moderate", "major"):
            d = check("CKLA1", make_gauges(), flood=flood, llm_reply=reply)
            self.assertIn(flood.upper(), d["flood_alert"])
            self.assertIn(flood, d["card"]["headline"])
            self.assertIn("Stay out of the water", d["card"]["plan"])

    def test_action_stage_is_flagged_as_near_flood(self):
        d = check("CKLA1", make_gauges(), flood="action")
        self.assertIn("near flood", d["flood_alert"])

    def test_model_failure_falls_back_to_code_sentences(self):
        def boom(*a, **k):
            raise RuntimeError("model down")
        with Env() as env:
            from agent import llm, rivercheck
            llm.chat = boom
            d = rivercheck.check("TODA1", "tonight", "fishing")
        self.assertIn("template", d["written_by"])
        self.assertIn("9 PM", d["card"]["headline"])


class Memory(unittest.TestCase):
    def test_pattern_is_remembered_on_the_gauge(self):
        with Env():
            from agent import memory, rivercheck, scout
            rivercheck.check("TODA1")
            learned = memory.learned(scout.ensure_site("TODA1"))
        self.assertEqual(learned["pattern"]["local_hour"], "9 PM")


if __name__ == "__main__":
    unittest.main()
