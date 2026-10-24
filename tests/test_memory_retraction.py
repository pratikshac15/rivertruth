"""Stored conclusions: clean summaries, and a retraction that hides a wrong one without erasing history.

Oct 24: every stored conclusion began with the leaked heading "For the public", and one (Oct 23) said the NWS gauge was
"10 miles downstream" (it is about 1 km). Conclusions stored before the fixes are retracted, with a reason, and stay in
the World Model's history; the River check and the agents no longer use them.
"""
import unittest

from tests.fakes import Env

REPORT = ("## For the public\nOn the evening of Oct 22 the river rose to 5,640 cfs at 9 PM. It was most likely a dam "
          "release. A third sentence.\n\n## Answer\nA release.")


def store(run_id, report=REPORT):
    from agent import memory, scout
    site = scout.ensure_site("TODA1")
    memory.use_site(site)
    memory.finish_run(run_id, "", report, [{"claim": "A scheduled release", "status": "supported"}])
    return site


class StoredSummaries(unittest.TestCase):
    def test_the_section_heading_is_not_part_of_the_stored_answer(self):
        with Env():
            from agent import memory
            site = store("R1")
            ans = memory.learned(site)["investigation"]["answer"]
            self.assertTrue(ans.startswith("On the evening of Oct 22"), ans)
            self.assertNotIn("For the public", ans)

    def test_an_answer_stored_by_an_older_version_is_shown_without_its_heading(self):
        with Env():
            from agent import memory
            site = store("R0")
            w = memory.world()
            w.assert_fact(site.entity or memory.NORTHPORT, "latest_explanation", {"run": "OLD", "answer": "For the public   On the evening it rose."},
                          source={"kind": "agent", "by": "reporter", "at": "2099-01-01T00:00:00Z"})
            self.assertEqual(memory.learned(site)["investigation"]["answer"], "On the evening it rose.")


class Retraction(unittest.TestCase):
    def test_a_retracted_run_is_hidden_from_the_river_check_and_the_agents_but_kept_in_history(self):
        with Env():
            from agent import memory
            site = store("BAD1", REPORT.replace("5,640 cfs", "5,640 cfs, 10 miles downstream"))
            memory.retract_runs(site, ["BAD1"], "stored before the Oct 24 fixes")
            self.assertNotIn("investigation", memory.learned(site))
            memory.use_site(site)
            self.assertNotIn("10 miles", memory.prior_knowledge())
            history = memory.world().facts_about(site.entity or memory.NORTHPORT, "latest_explanation", include_superseded=True)
            self.assertTrue(any("10 miles" in (f["value"] or {}).get("answer", "") for f in history))

    def test_a_newer_run_is_shown_again_after_a_retraction(self):
        with Env():
            from agent import memory
            site = store("BAD1")
            memory.retract_runs(site, ["BAD1"], "old")
            store("GOOD2", REPORT.replace("5,640", "5,700"))
            self.assertEqual(memory.learned(site)["investigation"]["run"], "GOOD2")
            memory.retract_runs(site, ["OTHER"], "unrelated")       # retractions add up, nothing comes back
            self.assertEqual(memory.learned(site)["investigation"]["run"], "GOOD2")
            memory.use_site(site)
            self.assertNotIn("BAD1", memory.prior_knowledge())

    def test_retract_everything_stored_before_a_time(self):
        with Env():
            from agent import memory
            site = store("OLD1")
            w = memory.world()
            stamp = w.facts_about(site.entity or memory.NORTHPORT, "latest_explanation")[-1]["assertedAt"]
            n = memory.retract_before("9999-01-01T00:00", "stored before the fixes")
            self.assertGreaterEqual(n, 1)
            self.assertNotIn("investigation", memory.learned(site))
            self.assertEqual(memory.retract_before(stamp[:16], "nothing earlier"), 0)


class ExampleQuestions(unittest.TestCase):
    def test_examples_have_plain_labels(self):
        from agent.cases import CASES, LABELS
        self.assertEqual(set(LABELS), set(CASES))
        for k, label in LABELS.items():
            self.assertTrue(label.endswith("?"), label)
            self.assertNotIn("-", label)
            self.assertNotIn("usgs", label.lower().split())


if __name__ == "__main__":
    unittest.main()
