"""The gauge sweep: checks a sample of gauges across the US through the real web server and reports every problem."""
import json
import os
import tempfile
import unittest

from tests.fakes import END, Env

CATALOG = [  # lid, name, lat, lon, flood, stage_ft, flow_cfs  (the format of data/gauges_us.json)
    ["TODA1", "Black Warrior River at Oliver Lock and Dam", 33.209, -87.596, "no_flooding", 95.3, 1050],
    ["CKLA1", "Cahaba River at Centreville", 32.94, -87.14, "no_flooding", 3.1, 455],
    ["AVLA1", "Tombigbee River at Bevill Lock and Dam", 33.21, -88.29, "no_flooding", 109.0, 3000],
    ["MMTW2", "Kanawha River below Marmet Locks", 38.25, -81.57, "action", 1030.0, 9000],
    ["PMAF1", "Tide Gauge at Port Manatee (in MHHW)", 27.64, -82.56, "no_flooding", 0.09, None],
    ["SPGS2", "Missouri River at Springfield", 42.85, -97.9, "action", 9.46, None],
    ["EMMC2", "Roaring Fork River at Emma", 39.37, -107.08, "no_flooding", 3.2, 310],
    ["DRYX1", "Empty Creek near Nowhere", 35.0, -100.0, "no_flooding", None, None],
]


class Select(unittest.TestCase):
    def test_sample_covers_every_region_kind_and_status(self):
        from scripts import sweep_gauges as sw
        status = ["no_flooding", "not_defined", "obs_not_current", "out_of_service", "action", "minor"]
        many = [[f"G{i:04d}", f"Gauge {i}", 25 + (i * 7) % 24, -124 + (i * 13) % 57,
                 "moderate" if i % 97 == 0 else status[i % len(status)], 2.0, (100 if i % 2 else None)] for i in range(2000)]
        picked = sw.select(many, per_cell=1, seed=1)
        lids = {g[0] for g in picked}
        self.assertTrue({g[0] for g in many if g[4] == "moderate"} <= lids)       # every moderate/major flood gauge
        for st in status:                                                          # some of every NWS status
            self.assertGreaterEqual(sum(1 for g in picked if g[4] == st), min(sw.PER_STATUS, 2), st)
        self.assertTrue(any(g[6] is None for g in picked) and any(g[6] for g in picked))  # level-only and flow
        cells = {(int(g[2] // sw.CELL), int(g[3] // sw.CELL)) for g in many}
        self.assertEqual({(int(g[2] // sw.CELL), int(g[3] // sw.CELL)) for g in picked}, cells)
        self.assertLess(len(picked), len(many) / 3)
        self.assertEqual(picked, sw.select(many, per_cell=1, seed=1))             # repeatable

    def test_named_gauges_only(self):
        from scripts import sweep_gauges as sw
        self.assertEqual([g[0] for g in sw.select(CATALOG, lids=["spgs2", "TODA1"])], ["SPGS2", "TODA1"])


class Problems(unittest.TestCase):
    def test_text_that_should_never_reach_a_person_is_flagged(self):
        from scripts import sweep_gauges as sw
        for bad in ("-9,999 cfs", "flow nan", "None ft", "undefined", "action -999"):
            with self.subTest(bad):
                self.assertTrue(sw.text_problems({"card": {"now": f"Now: {bad}."}}), bad)
        self.assertEqual(sw.text_problems({"card": {"now": "Now: 2,480 cfs at 6 PM CDT, 2.41 ft."}}), [])


class Run(unittest.TestCase):
    def test_the_fake_gauges_are_all_clean_and_a_crash_is_reported_with_its_error(self):
        from scripts import sweep_gauges as sw
        with Env() as env:
            from agent import rivercheck
            real = rivercheck.compute

            def boom(site):
                if site.lid == "CKLA1":
                    raise TypeError("'>' not supported between instances of 'str' and 'int'")
                return real(site)
            rivercheck.compute = boom
            out = tempfile.mkdtemp()
            try:
                results, summary = sw.run(CATALOG, out_dir=out, pause=0, now=END)
            finally:
                rivercheck.compute = real
        by = {r["lid"]: r for r in results}
        self.assertEqual(set(by), {g[0] for g in CATALOG})
        self.assertIn("'>' not supported", " ".join(by["CKLA1"]["errors"]))
        for lid in ("TODA1", "AVLA1", "MMTW2", "PMAF1", "SPGS2", "EMMC2"):
            self.assertEqual(by[lid]["errors"], [], (lid, by[lid]))
        self.assertIn("no data", " ".join(by["DRYX1"]["notes"]))
        self.assertTrue(by["MMTW2"]["flood_alert"])                                # action stage is shown
        self.assertIn("'>' not supported between instances of 'str' and 'int'", summary)
        self.assertIn("CKLA1", summary)
        self.assertTrue(os.path.exists(os.path.join(out, "results.jsonl")))
        self.assertTrue(os.path.exists(os.path.join(out, "summary.md")))

    def test_resume_skips_gauges_already_checked(self):
        from scripts import sweep_gauges as sw
        with Env() as env:
            out = tempfile.mkdtemp()
            sw.run(CATALOG[:2], out_dir=out, pause=0, now=END)
            before = len(env.zet.calls)
            results, _ = sw.run(CATALOG[:3], out_dir=out, pause=0, now=END, resume=True)
            self.assertEqual([r["lid"] for r in results], ["TODA1", "CKLA1", "AVLA1"])
            self.assertFalse(any("hc_ckla1" in c.lower() and c.startswith("CREATE") for c in env.zet.calls[before:]))

    def test_stale_data_is_noted(self):
        from datetime import timedelta
        from scripts import sweep_gauges as sw
        with Env():
            results, _ = sw.run(CATALOG[1:2], out_dir=tempfile.mkdtemp(), pause=0, now=END + timedelta(days=5))
        self.assertIn("stale", " ".join(results[0]["warnings"]))


if __name__ == "__main__":
    unittest.main()


class GaugeList(unittest.TestCase):
    """Live Oct 24: the sweep sat silent for minutes re-reading all 21 regions from Zetaris. It now uses the list the
    map saved, and only asks Zetaris when there is none."""

    def test_the_saved_map_list_is_used_without_asking_zetaris(self):
        from scripts import sweep_gauges as sw
        path = os.path.join(tempfile.mkdtemp(), "gauges_us.json")
        with open(path, "w") as f:
            json.dump({"at": 1, "gauges": CATALOG}, f)
        with Env() as env:
            gauges, where = sw.gauge_list(path, say=lambda *a: None)
            self.assertEqual([g[0] for g in gauges], [g[0] for g in CATALOG])
            self.assertIn("saved", where)
            self.assertEqual(env.zet.calls, [])

    def test_without_a_saved_list_it_asks_zetaris_and_says_so(self):
        from scripts import sweep_gauges as sw
        said = []
        with Env():
            gauges, where = sw.gauge_list(os.path.join(tempfile.mkdtemp(), "none.json"), say=said.append)
        self.assertTrue(gauges)
        self.assertIn("Zetaris", where)
        self.assertTrue(said)  # it tells the person what it is doing instead of sitting silent
