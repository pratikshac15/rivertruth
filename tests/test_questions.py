"""The question offered to the agent team fits what the River check found, every gauge gets example questions, and a
reading that is days old is called that, not "right now".

Oct 24 (Moenkopi Wash): the page said "Sources roughly agree" and then offered "Where they disagree, why…"; only the
Black Warrior had example questions; and a Thursday 5 PM reading was shown as "right now" on Saturday.
"""
import unittest
from datetime import timedelta

from tests.fakes import END, Env

NAME = "Moenkopi Wash near Tuba city"
AGREE = [{"source": "NWS gauge flow (NWS MOEA3)", "status": "agrees within 20%"}]
APART = [{"source": "National Water Model (simulated)", "status": "reads about 3.1x lower"}]


def facts(others=AGREE, pattern=None, rise=None):
    now = {"at_local": "Thu 5 PM MDT"}
    if rise:
        now["recent_rise"] = rise
    return {"ok": True, "others": others, "pattern": pattern, "now": now}


class SuggestedQuestion(unittest.TestCase):
    def ask(self, f, flood=""):
        from agent.cases import suggest
        return suggest(NAME, f, flood)["question"]

    def test_a_flood_asks_about_the_flood(self):
        q = self.ask(facts(), "major")
        self.assertIn(NAME, q)
        self.assertIn("major flood", q)
        self.assertNotIn("disagree", q)

    def test_agreeing_sources_are_not_asked_why_they_disagree(self):
        q = self.ask(facts(AGREE))
        self.assertNotIn("disagree", q)
        self.assertIn("agree", q)

    def test_a_daily_surge_asks_why_it_happens_at_that_hour(self):
        q = self.ask(facts(pattern={"kind": "daily surge", "local_hour": "9 PM", "tz": "CDT"}))
        self.assertIn("9 PM", q)
        self.assertIn("every day", q)

    def test_a_recent_rise_asks_what_caused_it(self):
        q = self.ask(facts(rise={"since_local": "Wed 6 AM MDT", "from_cfs": 0, "to_cfs": 14300}))
        self.assertIn("Wed 6 AM MDT", q)
        self.assertIn("caused", q)

    def test_disagreeing_sources_keep_the_disagreement_question(self):
        self.assertIn("disagree", self.ask(facts(APART)))

    def test_no_usable_facts_falls_back_to_the_general_question(self):
        from agent.cases import site_question
        self.assertEqual(self.ask({"ok": False}), site_question(NAME))


class Examples(unittest.TestCase):
    def test_every_gauge_gets_three_plain_examples(self):
        from agent.cases import suggest
        ex = suggest(NAME, facts(), "")["examples"]
        self.assertEqual(len(ex), 3)
        for e in ex:
            self.assertTrue(e["label"].endswith("?"), e)
            self.assertIn(NAME, e["question"])


class InTheApp(unittest.TestCase):
    def test_the_river_check_carries_the_question_and_examples(self):
        with Env():
            from agent import rivercheck
            out = rivercheck.check("CKLA1", "today", "fishing", "", now=END)
            self.assertNotIn("disagree", out["question"])   # the fake Cahaba's sources agree
            self.assertEqual(len(out["examples"]), 3)


class StaleReading(unittest.TestCase):
    def test_a_reading_days_old_is_called_that(self):
        with Env():
            from agent import rivercheck
            out = rivercheck.check("CKLA1", "today", "fishing", "", now=END + timedelta(days=2, hours=1))
            self.assertIn("2 days ago", out["stale"])
            self.assertIn("nothing newer", out["stale"])

    def test_a_fresh_reading_has_no_note(self):
        with Env():
            from agent import rivercheck
            self.assertNotIn("stale", rivercheck.check("CKLA1", "today", "fishing", "", now=END + timedelta(hours=2)))

    def test_level_only_gauges_get_the_note_too(self):
        with Env():
            from agent import rivercheck
            out = rivercheck.check("PMAF1", "today", "fishing", "", now=END + timedelta(hours=30))
            self.assertIn("ago", out["stale"])


if __name__ == "__main__":
    unittest.main()
