"""Fresh data: a gauge's sources are re-fetched when a River check finds them over an hour old, without ever losing
the copy it has.

Oct 24: every gauge showed data from the moment it was first opened (Moenkopi stopped at Thursday 5 PM on Saturday),
because the cached copies in Zetaris were never refreshed. A refresh now fetches into a spare table; the view switches
to it only for the sources that actually arrived, so a NOAA refusal leaves the old copy in place.
"""
import unittest
from datetime import datetime, timedelta, timezone

from tests.fakes import END, Env

LATER = datetime.now(timezone.utc) + timedelta(hours=2)   # well past the fetch made when the gauge was registered


def fresh_site(env, lid="CKLA1"):
    from agent import scout
    return scout.ensure_site(lid)


def new_calls(env, before):
    return env.zet.calls[before:]


class Refresh(unittest.TestCase):
    def test_old_data_is_fetched_into_the_spare_tables_and_the_view_switches(self):
        with Env() as env:
            from agent import scout
            site = fresh_site(env)
            n = len(env.zet.calls)
            self.assertEqual(scout.refresh(site, now=LATER), "refreshed")
            calls = new_calls(env, n)
            self.assertIn("CACHE TABLE usgs_nwis.iv_ckla1_raw_b", calls)
            self.assertTrue(any(c.startswith("CREATE SCHEMASTORE VIEW hc_ckla1") and "iv_ckla1_raw_b" in c for c in calls))
            self.assertEqual(site.slots.get("usgs"), "b")
            self.assertFalse(any(c.upper().startswith("UNCACHE") and "iv_ckla1_raw " in c + " " for c in calls),
                             "the copy in use is never dropped")

    def test_within_the_hour_nothing_is_fetched_again(self):
        with Env() as env:
            from agent import scout
            site = fresh_site(env)
            scout.refresh(site, now=LATER)
            n = len(env.zet.calls)
            self.assertEqual(scout.refresh(site, now=LATER + timedelta(minutes=40)), "fresh")
            self.assertEqual(new_calls(env, n), [])
            self.assertEqual(scout.refresh(site, now=LATER + timedelta(minutes=70)), "refreshed")
            self.assertEqual(site.slots.get("usgs"), "", "slots alternate")

    def test_when_every_agency_refuses_the_old_copy_stays_in_use(self):
        with Env() as env:
            from agent import rivercheck, scout
            site = fresh_site(env)
            env.zet.fail_429["_raw_b"] = 99
            n = len(env.zet.calls)
            self.assertEqual(scout.refresh(site, now=LATER), "failed")
            self.assertFalse(any(c.upper().startswith(("DROP VIEW", "CREATE SCHEMASTORE VIEW")) for c in new_calls(env, n)))
            self.assertEqual(site.slots.get("usgs", ""), "")
            self.assertTrue(rivercheck.compute(site)["ok"])

    def test_one_refusing_source_keeps_its_old_copy_and_the_rest_switch(self):
        with Env() as env:
            from agent import scout
            site = fresh_site(env)
            env.zet.fail_429["reach_777_analysis_raw_b"] = 99
            n = len(env.zet.calls)
            self.assertEqual(scout.refresh(site, now=LATER), "refreshed")
            view = [c for c in new_calls(env, n) if c.startswith("CREATE SCHEMASTORE VIEW hc_ckla1")][-1]
            self.assertIn("iv_ckla1_raw_b", view)
            self.assertIn("reach_777_analysis_raw ", view.replace("\n", " ") + " ")
            self.assertEqual(site.slots.get("nwm", ""), "")

    def test_which_copy_is_in_use_survives_a_restart(self):
        with Env() as env:
            from agent import scout
            site = fresh_site(env)
            scout.refresh(site, now=LATER)
            scout._SITES.clear()
            again = scout.ensure_site("CKLA1")
            self.assertEqual(again.slots.get("usgs"), "b")

    def test_the_flagship_and_sample_mode_are_left_alone(self):
        with Env() as env:
            from agent import scout
            from agent.sites import FLAGSHIP
            n = len(env.zet.calls)
            self.assertIsNone(scout.refresh(FLAGSHIP, now=LATER))
            self.assertEqual(new_calls(env, n), [])


class OneTimeAxisForAllSources(unittest.TestCase):
    def test_the_view_has_every_river_sources_hours_not_just_usgs(self):
        from agent import scout
        sql = scout._view_sql("CKLA1", scout.tables_for("CKLA1", "7"), "America/Chicago", True, True, True, True)
        self.assertIn("UNION", sql)
        spine = sql.split("LEFT JOIN")[0]
        self.assertNotIn("open_meteo", spine, "rain includes forecast hours: never part of the time axis")
        self.assertIn("usgs_nwis", spine)
        self.assertIn("_observed_raw", spine)


class HonestOldReadingNote(unittest.TestCase):
    def test_after_a_successful_refresh_the_note_says_nothing_newer_was_published(self):
        with Env():
            from agent import rivercheck
            out = rivercheck.check("CKLA1", "today", "fishing", "", now=LATER + timedelta(days=2))
            self.assertIn("nothing newer", out["stale"])

    def test_when_the_refresh_fails_the_note_says_so(self):
        with Env() as env:
            from agent import rivercheck
            env.zet.fail_429["_raw_b"] = 99
            out = rivercheck.check("CKLA1", "today", "fishing", "", now=LATER + timedelta(days=2))
            self.assertIn("couldn't get newer data", out["stale"])


if __name__ == "__main__":
    unittest.main()
