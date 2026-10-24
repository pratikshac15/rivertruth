"""Every reading the app shows must come from the newest data, whatever the length of a gauge's history.

Regression: hourly queries asked for 'ORDER BY hr_utc LIMIT 200' (the OLDEST 200 hours). Gauges whose history is
longer than 200 hours (tide gauges keep about a month) showed readings many hours old.
"""
import unittest

from tests.fakes import END, Env

LATEST = END.strftime("%Y-%m-%d %H:00")


class LatestRows(unittest.TestCase):
    def test_site_rows_sql_returns_newest_hours_in_order(self):
        with Env() as env:
            from agent import scout, zetaris
            site = scout.ensure_site("PMAF1")
            rows = zetaris.run_sql(site.rows_sql())["rows"]
            self.assertEqual(rows[-1][0], LATEST)
            self.assertEqual([r[0] for r in rows], sorted(r[0] for r in rows), "rows must be oldest-first")
            self.assertLessEqual(len(rows), 200)

    def test_snapshot_rows_sql_returns_newest_hours(self):
        with Env():
            from agent import scout, snapshot, zetaris
            for lid in ("TODA1", "PMAF1", "CKLA1"):
                rows = zetaris.run_sql(snapshot.rows_sql(scout.ensure_site(lid)))["rows"]
                self.assertEqual(rows[-1][0], LATEST, lid)

    def test_river_check_reports_the_latest_level(self):
        with Env():
            from agent import rivercheck, scout
            f = rivercheck.compute(scout.ensure_site("PMAF1"))
            self.assertTrue(f["ok"])
            self.assertEqual(f["now"]["stage_ft"], 0.09)
            self.assertIn("Fri", f["now"]["at_local"])  # END is Fri Oct 23 22:00 UTC = Fri 6 PM EDT
            self.assertIn("6 PM", f["now"]["at_local"])

    def test_snapshot_latest_hour_is_newest(self):
        with Env():
            from agent import scout, snapshot
            text = snapshot.build(scout.ensure_site("PMAF1"))
            self.assertIn(f"LATEST DATA HOUR: {END:%Y-%m-%d %H:%M} UTC", text)


if __name__ == "__main__":
    unittest.main()
