"""Unit tests: report guards, the Source Scout's SQL, site time zones, the gauge catalog and the fact-check step."""
import json
import re
import unittest
from datetime import datetime, timezone

from tests.fakes import Env


class Guards(unittest.TestCase):
    def setUp(self):
        from agent.sites import FLAGSHIP, Site
        self.flag = FLAGSHIP
        self.other = Site(lid="AVLA1", name="Tombigbee River at Bevill Lock and Dam", view="v", cols={})
        self.snap = ("reports NO discharge\nLatest stage 109.0 ft is 13.0 ft below the NWS action stage (122 ft): x")

    def apply(self, text, site=None):
        from agent import guards
        return guards.apply(text, site or self.other, self.snap)

    def test_place_from_another_river_is_respelled_with_grammar(self):
        out, ap = self.apply("## What to check next\n1. Obtain Holt Dam release records.\n2. Verify run type for the Northport reach.")
        self.assertIn("Obtain the dam release records", out)
        self.assertIn("for the reach", out)
        self.assertNotIn("Holt", out)
        self.assertTrue(all(a["why"].startswith("rule:") for a in ap))

    def test_unrespellable_foreign_place_sentence_is_removed_and_list_renumbered(self):
        out, _ = self.apply("## What to check next\n1. Northport is wet.\n2. Check the gauge.\n3. Ask the operator.")
        self.assertNotIn("Northport", out)
        self.assertIn("1. Check the gauge.", out)
        self.assertIn("2. Ask the operator.", out)

    def test_flagship_report_may_name_its_own_places(self):
        out, ap = self.apply("Holt Dam released water.", site=self.flag)
        self.assertIn("Holt Dam", out)

    def test_zero_cfs_for_a_source_without_data_is_removed(self):
        out, ap = self.apply("## Answer\nGood. Ground-truth confirms USGS daily mean 0 ft³/s vs NWS 3,008 ft³/s.")
        self.assertNotIn("0 ft³/s", out)
        self.assertIn("Good.", out)

    def test_datum_claim_is_replaced_only_in_explanation_sections(self):
        text = ("## Why the sources disagree\n- Stage agrees. The 13 ft gap to flood thresholds indicates a different vertical datum.\n"
                "## What we could not confirm\n- Exact vertical datum of NWS flood-stage thresholds.")
        out, _ = self.apply(text + "\n- Gate schedule unknown.")
        why, rest = out.split("## What we could not confirm")
        self.assertIn("not evidence of a datum difference", why)
        self.assertNotIn("datum", rest.lower())
        self.assertIn("Gate schedule unknown.", rest)

    def test_empty_section_heading_is_dropped(self):
        out, _ = self.apply("## Answer\nFine.\n## What to check next\n1. Ask Northport.\n## Confidence\nHigh.")
        self.assertNotIn("What to check next", out)


class ScoutSQL(unittest.TestCase):
    def test_view_uses_usgs_spine_when_linked_and_nws_spine_otherwise(self):
        from agent import scout
        with_usgs = scout._view_sql("CKLA1", scout.tables_for("CKLA1", "7"), "America/Chicago", True, True)
        nws_only = scout._view_sql("MMTW2", scout.tables_for("MMTW2", ""), "America/New_York", False, False)
        self.assertIn("SELECT u.hr_utc", with_usgs)
        self.assertEqual(with_usgs.count("LEFT JOIN"), 3)
        self.assertIn("SELECT w.hr_utc", nws_only)
        self.assertIn("CAST(NULL AS DOUBLE) AS usgs_cfs", nws_only)
        self.assertIn("CAST(NULL AS DOUBLE) AS nwm_cfs", nws_only)
        self.assertNotIn("usgs_nwis", nws_only)
        self.assertIn("'America/New_York'", scout._view_sql("X", scout.tables_for("X", "1"), "America/New_York"))

    def test_view_columns_match_the_generic_order(self):
        from agent import scout
        from agent.sites import GENERIC
        sql = scout._view_sql("CKLA1", scout.tables_for("CKLA1", "7"))
        sel = re.search(r"SELECT (.+?)\nFROM", sql).group(1)
        names = [c.strip().split(" AS ")[-1].split(".")[-1] for c in sel.split(",")]
        self.assertEqual(names, GENERIC)

    def test_unlinked_gauge_registers_only_its_own_sources(self):
        with Env() as env:
            from agent import scout
            site = scout.ensure_site("MMTW2")
            ddl = " ".join(c for c in env.zet.calls if c.startswith("CREATE"))
        self.assertNotIn("REST TABLE iv_mmtw2_raw", ddl)        # no USGS table: none is linked
        self.assertNotIn("reach_", ddl)                          # no model table: no reach is linked
        self.assertIn("mmtw2_observed_raw", ddl)
        self.assertEqual(site.tz, "America/New_York")
        self.assertEqual(site.usgs_id, "")

    def test_registered_gauge_is_rebuilt_from_memory_without_new_ddl(self):
        with Env() as env:
            from agent import scout
            scout.ensure_site("CKLA1")
            scout._SITES.clear()
            n = len(env.zet.calls)
            site = scout.ensure_site("CKLA1")
            new = env.zet.calls[n:]
            self.assertFalse([c for c in new if c.upper().startswith(("CREATE", "DROP", "CACHE"))])
            self.assertEqual(new, ["SELECT * FROM water.hc_ckla1 LIMIT 1"])  # one health check, nothing else
            self.assertEqual(site.name, "Cahaba River at Centreville")

    def test_bad_code_is_rejected(self):
        from agent import scout
        with self.assertRaises(scout.ScoutError):
            scout.discover("not a code!")


class Sites(unittest.TestCase):
    def test_time_zone_label_and_offset_follow_the_gauge(self):
        from agent.sites import Site, iana_tz
        at = datetime(2026, 10, 23, 22, tzinfo=timezone.utc)
        self.assertEqual(Site(lid="A", name="a", view="v", cols={}, tz=iana_tz("PST8PDT")).tz_now(at), ("PDT", -7))
        self.assertEqual(Site(lid="A", name="a", view="v", cols={}, tz=iana_tz("MST")).tz_now(at), ("MST", -7))
        self.assertEqual(Site(lid="A", name="a", view="v", cols={}, tz=iana_tz("EST5EDT")).tz_now(at), ("EDT", -4))
        self.assertEqual(iana_tz(None, -120.0), "America/Los_Angeles")

    def test_rows_sql_asks_for_the_newest_hours(self):
        from agent import snapshot
        from agent.sites import FLAGSHIP
        for sql in (FLAGSHIP.rows_sql(), snapshot.rows_sql(FLAGSHIP)):
            self.assertIn("ORDER BY hr_utc DESC LIMIT 200", sql)
            self.assertTrue(sql.rstrip().endswith("ORDER BY hr_utc"))

    def test_generic_catalog_uses_local_time(self):
        from agent.catalog import catalog
        from agent.sites import Site
        c = catalog(Site(lid="X", name="Snake River", view="water.hc_x", cols={}, tz="America/Los_Angeles"))
        self.assertIn("9 PM PDT on Oct 22", c)
        self.assertIn("'2026-10-23 04:00'", c)


class GaugeCatalog(unittest.TestCase):
    def test_kcfs_is_converted_and_missing_values_dropped(self):
        from agent import gauges
        row = gauges._compact(["A", "a", "33.2", "-87.5", 95.3, "ft", 0.921, "kcfs", "no_flooding", "t"])
        self.assertEqual(row, ["A", "a", 33.2, -87.5, "no_flooding", 95.3, 921])
        row = gauges._compact(["B", "b", 30, -80, -999, "ft", -999, "kcfs", None, "t"])
        self.assertEqual(row[4:], ["not_defined", None, None])

    def test_regions_cover_the_contiguous_us_alaska_hawaii_and_puerto_rico(self):
        from agent import gauges
        def covered(lat, lon):
            return any(x0 <= lon < x1 and y0 <= lat < y1 for x0, y0, x1, y1 in gauges.REGIONS)
        for lat, lon in [(47.6, -122.3), (25.8, -80.2), (44.9, -67.0), (32.7, -117.1), (61.2, -149.9),
                         (21.3, -157.9), (18.4, -66.1), (48.9, -97.0)]:
            self.assertTrue(covered(lat, lon), (lat, lon))

    def test_refused_region_is_split_into_quarters(self):
        with Env() as env:
            from agent import gauges
            env.zet.fail_429 = {"gauges_r_m95_p33_m85_p42": 99}
            rows = gauges._region((-95, 33, -85, 42), refresh=False)
            self.assertTrue(any("gauges_r_m95_p33_m90_p37_5" in c for c in env.zet.calls))
            self.assertIn("TODA1", {r[0] for r in rows})


class FactCheck(unittest.TestCase):
    def _run_with_corrections(self, corrections):
        import json
        with Env() as env:
            from agent import llm, orchestrator
            base = env.llm.chat

            def chat(role, messages, **k):
                if role.startswith("fact_checker"):
                    return json.dumps({"corrections": corrections})
                return base(role, messages, **k)
            llm.chat = chat
            return orchestrator.run("Why?", "TODA1").report

    def test_model_may_correct_but_never_delete_or_restyle(self):
        report = self._run_with_corrections([
            {"wrong": "A release.", "right": "A scheduled release at 9 PM.", "why": "matches the facts"},
            {"wrong": "Timing [E1].", "right": "", "why": "remove"},
            {"wrong": "High.", "right": "High!", "why": "is correct, no correction needed"},
            {"wrong": "Water was released from a dam.", "right": "Different words.", "why": "imprecise"},
        ])
        self.assertIn("## Fact-check", report)
        body = report.split("## Fact-check")[0]       # the corrections list quotes the originals, so check the body
        self.assertIn("A scheduled release at 9 PM.", body)
        self.assertIn("Timing [E1].", body)            # deletions refused
        self.assertIn("High.", body)                   # "correct, no correction" refused
        self.assertNotIn("High!", body)
        self.assertIn("most likely", body.split("## Answer")[0])  # public section is written in code from the facts
        self.assertNotIn("Different words.", body)

    def test_at_most_three_model_corrections(self):
        report = self._run_with_corrections([{"wrong": w, "right": w + " on Oct 22", "why": "facts"}
                                             for w in ("A release.", "Timing [E1].", "High.", "## Answer")])
        self.assertEqual(report.split("## Fact-check")[0].count(" on Oct 22"), 3)


if __name__ == "__main__":
    unittest.main()


class NoValueCodes(unittest.TestCase):
    def test_minus_9999_and_similar_become_missing(self):
        from agent import sites
        rows = [["2026-10-23 21:00", -9999.0, 9.46, None, "-9999", -2.5, 0, True]]
        self.assertEqual(sites.no_missing(rows), [["2026-10-23 21:00", None, 9.46, None, None, -2.5, 0, True]])

    def test_numbers_sent_as_text_by_zetaris_become_numbers(self):
        from agent import sites
        self.assertEqual(sites.no_missing([["2026-10-23 21:00", "2480", "9.46", "-9999.0", "", "abc"]]),
                         [["2026-10-23 21:00", 2480.0, 9.46, None, "", "abc"]])

    def test_snapshot_and_radar_rows_are_cleaned_too(self):
        with Env():
            from agent import radar, scout, snapshot
            site = scout.ensure_site("SPGS2")
            self.assertNotIn("9999", snapshot.build(site).replace(",", ""))
            self.assertNotIn("9999", json.dumps(radar.score(site)).replace(",", ""))


class FloodLevelsFromZetaris(unittest.TestCase):
    """Zetaris returns the NWS flood levels as text, with "-999" for a level NOAA has not set."""

    def test_flood_levels_are_numbers_and_unset_levels_are_missing(self):
        from agent import sites
        self.assertEqual(sites.clean_flood({"action": "188", "minor": "-999", "moderate": "196.5", "major": None}),
                         {"action": 188.0, "minor": None, "moderate": 196.5, "major": None})

    def test_a_newly_found_gauge_never_carries_minus_999_as_a_flood_level(self):
        from tests.fakes import make_gauges
        g = make_gauges()
        g["PMAF1"].flood = {"action": -999, "minor": -999, "moderate": -999, "major": -999}
        with Env(gauges=g):
            from agent import catalog, scout, snapshot
            site = scout.ensure_site("PMAF1")
            self.assertEqual(site.flood, {"action": None, "minor": None, "moderate": None, "major": None})
            self.assertNotIn("-999", catalog.catalog(site))
            self.assertNotIn("-999", snapshot.build(site))


class SavedMapListNeverShrinksOnAPartialLoad(unittest.TestCase):
    """Oct 24: the saved list went from 12,941 to 7,671 gauges after a refresh in which some regions failed (NOAA
    rate limits). A partial load now adds to the saved list instead of replacing it."""

    def _refresh_with(self, saved, loaded, errors):
        with Env():
            from agent import gauges
            with open(gauges._SAVE, "w") as f:
                json.dump({"at": 1, "gauges": saved}, f)
            real = gauges.load_all
            gauges.load_all = lambda refresh=False, progress=None: (loaded, errors)
            try:
                gauges._all_lock.acquire()
                gauges._refresh(True)
            finally:
                gauges.load_all = real
            with open(gauges._SAVE) as f:
                return {g[0]: g for g in json.load(f)["gauges"]}, {g[0] for g in gauges.ALL["gauges"]}

    def test_regions_that_failed_keep_their_saved_gauges(self):
        a, b, c = ["A", "a", 1, 1, "no_flooding", 1, 1], ["B", "b", 2, 2, "no_flooding", 1, 1], ["C", "c", 3, 3, "action", 1, 1]
        a2 = ["A", "a", 1, 1, "minor", 2, 2]
        saved, shown = self._refresh_with([a, b, c], [a2, b], ["NOAA kept refusing region m90_p40 (429)"])
        self.assertEqual(set(saved), {"A", "B", "C"})
        self.assertEqual(saved["A"][4], "minor")   # what was loaded is the newest
        self.assertEqual(shown, {"A", "B", "C"})

    def test_a_complete_load_replaces_the_list(self):
        a, b, c = ["A", "a", 1, 1, "no_flooding", 1, 1], ["B", "b", 2, 2, "no_flooding", 1, 1], ["C", "c", 3, 3, "action", 1, 1]
        saved, _ = self._refresh_with([a, b, c], [a, b], [])
        self.assertEqual(set(saved), {"A", "B"})  # C is gone from NOAA's catalog


class RegionsRefusedByNoaaAreRetriedAfterACooldown(unittest.TestCase):
    """Oct 24: NOAA started refusing (429) at region 13 of 21, and every later region (the whole East, Alaska, Hawaii,
    Puerto Rico) failed within seconds. Failed regions now get a second pass after a cool-down."""

    def test_failed_regions_are_tried_again_after_the_rest(self):
        with Env():
            from agent import gauges, zetaris
            calls, order = {}, []

            def region(b, refresh, depth=0):
                calls[b] = calls.get(b, 0) + 1
                order.append(b)
                if gauges.REGIONS.index(b) >= 12 and calls[b] == 1:
                    raise zetaris.SQLError(f"NOAA kept refusing region {gauges._region_tag(b)} (429)")
                return [[f"G{gauges.REGIONS.index(b)}", "g", b[1] + 1, b[0] + 1, "no_flooding", 1.0, None]]
            real, gauges._region = gauges._region, region
            gauges.COOLDOWN, saved = 0, getattr(gauges, "COOLDOWN", None)
            try:
                got, errors = gauges.load_all()
            finally:
                gauges._region = real
                gauges.COOLDOWN = saved
            self.assertEqual(len(got), len(gauges.REGIONS))           # every region in the end
            self.assertEqual(errors, [])
            self.assertEqual(order[:len(gauges.REGIONS)], list(gauges.REGIONS))  # first pass in order, then retries
            self.assertTrue(all(calls[b] == 2 for b in gauges.REGIONS[12:]))

    def test_a_region_that_fails_twice_is_reported(self):
        with Env():
            from agent import gauges, zetaris

            def region(b, refresh, depth=0):
                if b == gauges.REGIONS[-1]:
                    raise zetaris.SQLError("NOAA kept refusing (429)")
                return [[f"G{gauges.REGIONS.index(b)}", "g", b[1] + 1, b[0] + 1, "no_flooding", 1.0, None]]
            real, gauges._region = gauges._region, region
            gauges.COOLDOWN, saved = 0, getattr(gauges, "COOLDOWN", None)
            try:
                got, errors = gauges.load_all()
            finally:
                gauges._region = real
                gauges.COOLDOWN = saved
            self.assertEqual(len(got), len(gauges.REGIONS) - 1)
            self.assertEqual(len(errors), 1)


class StartingThePageDoesNotRedownloadEverything(unittest.TestCase):
    """Oct 24: every restart of the page re-downloaded all 21 regions from NOAA (UNCACHE + CACHE), which got the
    machine rate-limited and emptied the East. The first load after a start now reads Zetaris's cached copy; only the
    periodic refresh while the page runs asks NOAA again."""

    @staticmethod
    def _load(gauges):
        import time as _t
        gauges.all_gauges()
        for _ in range(200):
            if gauges._all_lock.acquire(blocking=False):
                gauges._all_lock.release()
                return
            _t.sleep(0.02)
        raise AssertionError("map load did not finish")

    def test_first_load_after_start_uses_the_cached_copy_and_later_refreshes_ask_noaa(self):
        with Env(catalog_extra=30) as env:
            from agent import gauges
            with open(gauges._SAVE, "w") as f:
                json.dump({"at": 1, "gauges": [["X", "x", 30, -90, "no_flooding", 1, 1]]}, f)
            gauges._load_saved()                       # what happens when the server starts
            self.assertEqual(gauges.ALL["at"], 1)
            self._load(gauges)
            self.assertFalse(any(c.upper().startswith("UNCACHE") for c in env.zet.calls))
            self.assertGreater(len(gauges.ALL["gauges"]), 1)
            gauges.ALL["at"], gauges.ALL["tried"] = 1, 0  # half an hour later, still running
            self._load(gauges)
            self.assertTrue(any(c.upper().startswith("UNCACHE") for c in env.zet.calls))
