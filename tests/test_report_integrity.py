"""The report says only what the review supports (audit blockers 5, 6, 8)."""
import json
import unittest

from tests.fakes import Env


def run_with(override, lid="TODA1", prepare=None):
    with Env() as env:
        from agent import llm, memory, orchestrator
        if prepare:
            prepare(env)
        base, prompts = env.llm.chat, {}

        def chat(role, messages, **k):
            prompts.setdefault(role, []).append(messages[-1]["content"])
            out = override(role, messages)
            return base(role, messages, **k) if out is None else out
        llm.chat = chat
        case = orchestrator.run("Why?", lid)
        from agent import scout
        learned = memory.learned(scout.ensure_site(lid))
        return case, prompts, learned


def critic_says(verdict, issues=("The cited window is wrong.",)):
    def fn(role, messages):
        if role.startswith("critic"):
            return json.dumps({"verdict": verdict, "hypothesis_status": {"H1": "supported", "H2": "refuted"},
                               "issues": list(issues), "new_checks": []})
        return None
    return fn


def section(report, name):
    part = report.split(f"## {name}", 1)[1]
    return part.split("\n## ", 1)[0]


class ConfidenceFollowsTheCritic(unittest.TestCase):
    def test_without_approval_confidence_is_capped_and_open_issues_are_listed(self):
        case, _, learned = run_with(critic_says("revise"))
        conf = section(case.report, "Confidence")
        self.assertNotIn("High", conf)
        self.assertIn("Medium", conf)
        self.assertIn("critic did not approve", conf)
        self.assertIn("The cited window is wrong.", section(case.report, "What the critic still questioned"))
        self.assertNotIn("investigation", learned)  # unapproved conclusions are not remembered

    def test_with_approval_high_confidence_stands_and_is_remembered(self):
        case, _, learned = run_with(critic_says("approve"))
        self.assertIn("High", section(case.report, "Confidence"))
        self.assertNotIn("What the critic still questioned", case.report)
        self.assertIn("investigation", learned)

    def test_reporter_is_told_the_verdict(self):
        _, prompts, _ = run_with(critic_says("revise"))
        self.assertIn("did NOT approve", prompts["reporter"][0])


class PublicSection(unittest.TestCase):
    def test_without_any_facts_the_reporters_public_text_is_kept(self):
        """No data this week: there are no computed facts to build (or check) a summary from."""
        def fn(role, messages):
            if role == "reporter":
                return "## For the public\nNo data was published this week.\n\n## Answer\nNone.\n\n## Confidence\nLow."
            return critic_says("approve")(role, messages)
        case, _, _ = run_with(fn, lid="DRYX1")
        self.assertIn("No data was published this week.", section(case.report, "For the public"))

    def test_reporter_is_told_to_hedge_inferred_causes(self):
        _, prompts, _ = run_with(critic_says("approve"))
        self.assertIn("most likely", prompts["reporter"][0])

    def test_missing_sections_are_added(self):
        def fn(role, messages):
            if role == "reporter":
                return "The sources disagree because of a release."
            return critic_says("approve")(role, messages)
        case, _, _ = run_with(fn)
        for name in ("For the public", "Answer", "Confidence"):
            self.assertIn(f"## {name}", case.report)


class CrossRunLeak(unittest.TestCase):
    def test_other_gauges_in_critic_notes_never_reach_the_reporter(self):
        def prep(env):
            from agent import scout
            scout.ensure_site("AVLA1")  # a gauge investigated earlier, as in the audited logs

        def fn(role, messages):
            if role.startswith("critic"):
                return json.dumps({"verdict": "revise", "hypothesis_status": {},
                                   "issues": ["The NWS AVLA1 coverage window is wrong.", "Units check out."],
                                   "new_checks": []})
            return None
        case, prompts, _ = run_with(fn, prepare=prep)
        self.assertNotIn("AVLA1", prompts["reporter"][0])
        self.assertNotIn("Tombigbee", prompts["reporter"][0])
        self.assertIn("Units check out.", prompts["reporter"][0])

    def test_nightshift_receipt_keeps_cache_fields(self):
        from types import SimpleNamespace
        from agent import llm
        ev = SimpleNamespace(model_extra={"meterless": {"routed_model": "nvidia/x", "decision_id": "d1", "from_cache": True,
                                                        "cache": {"status": "hit", "key": "abc"}}})
        r = llm._receipt(ev)
        self.assertEqual(r["cache_status"], "hit")
        self.assertTrue(r["from_cache"])
        self.assertEqual(r["cache"], {"status": "hit", "key": "abc"})


if __name__ == "__main__":
    unittest.main()


class LiveRunFindings(unittest.TestCase):
    """Regression tests built from the first live run after the audit fixes (Oct 23, 22:13 CDT)."""

    def test_public_summary_is_built_from_code_facts(self):
        def fn(role, messages):
            if role == "reporter":
                return ("## For the public\nOn Oct 22 the river surged when Holt Dam released water. The NWS gauge at "
                        "Oliver Lock and Dam, 10 miles downstream, stayed steady.\n\n## Answer\nA release.\n\n"
                        "## Confidence\nMedium.")
            return critic_says("approve")(role, messages)
        case, _, _ = run_with(fn)
        pub = section(case.report, "For the public")
        self.assertNotIn("10 miles", pub)
        self.assertIn("most likely", pub)
        self.assertIn("9 PM", pub)
        self.assertIn("did not show it", pub)       # what the NWS gauge did, from code
        self.assertIn("1 hour later", pub)          # what the model did, from code

    def test_cap_only_touches_the_rating_not_later_words(self):
        from agent.orchestrator import _cap_confidence
        r = ("## Answer\nx\n\n## Confidence\n**Medium** - three lines converge. Confidence is not High because the "
             "run type is unverified.\n\n## What we could not confirm\n- y")
        out = _cap_confidence(r, [{"verdict": "revise", "issues": ["z"], "new_checks": []}])
        self.assertIn("Confidence is not High because", out)
        self.assertNotIn("not Medium because", out)
        out = _cap_confidence(r.replace("**Medium**", "**High**"), [{"verdict": "revise", "issues": [], "new_checks": []}])
        self.assertIn("**Medium** - three", out)

    def test_fact_check_may_not_drop_a_number_the_facts_confirm(self):
        def fn(role, messages):
            if role == "reporter":
                return ("## For the public\nx\n\n## Answer\nAt the USGS peak hour the model showed 170 cfs and peaked "
                        "one hour later.\n\n## Confidence\nMedium.")
            if role == "fact_checker":
                return json.dumps({"corrections": [{"wrong": "At the USGS peak hour the model showed 170 cfs and peaked one hour later.",
                                                    "right": "The model had no data for the pulse hour and peaked one hour later.",
                                                    "why": "conflates the overlap value with missing data"}]})
            return critic_says("approve")(role, messages)

        def prep(env):  # make the computed facts say 170 cfs at the peak hour, as on the real river
            g = env.gauges["TODA1"]
            for r in g.rows:
                if r["hr_utc"] == "2026-10-23 02:00":
                    r["nwm_cfs"] = 170.0
        case, _, _ = run_with(fn, prepare=prep)
        self.assertIn("showed 170 cfs", case.report)
        self.assertNotIn("had no data for the pulse hour", case.report)


class PublicWording(unittest.TestCase):
    def test_sources_that_agree_are_grouped(self):
        from agent import public
        f = {"ok": True, "trusted": "USGS measured flow (X)", "pattern": None, "now": {"vs_week_median": 1.0},
             "others": [{"source": "NWS gauge flow (Y)", "status": "agrees within 20%"},
                        {"source": "National Water Model (simulated)", "status": "agrees within 20%"}]}
        text = public.summary(f)
        self.assertEqual(text.count("agree"), 1, text)
        self.assertIn("the NWS gauge and the National Water Model agree with it", text)


class SecondLiveRunFindings(unittest.TestCase):
    """From the second live run (Oct 23, 22:32 CDT)."""

    def test_mixed_model_timing_is_described_day_by_day_not_averaged(self):
        def prep(env):  # the model on time on one day and an hour late on another, as on the real river
            from tests.fakes import _pulse
            from datetime import timedelta
            for i, r in enumerate(env.gauges["TODA1"].rows):
                if i >= 120:
                    from datetime import datetime, timezone
                    t = datetime.strptime(r["hr_utc"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                    r["nwm_cfs"] = _pulse(t) if i < 144 else _pulse(t - timedelta(hours=1))
        case, _, _ = run_with(critic_says("approve"), prepare=prep)
        pub = section(case.report, "For the public")
        self.assertNotIn("0.5", pub)
        self.assertIn("at the same time on 1 day and about 1 hour later on 1 day", pub)

    def test_public_summary_gives_real_flows_not_a_vague_ratio(self):
        case, _, _ = run_with(critic_says("approve"))
        pub = section(case.report, "For the public")
        self.assertRegex(pub, r"rose from about [\d,]+ to [\d,]+ cubic feet per second")

    def test_model_cannot_edit_the_code_written_public_summary(self):
        def fn(role, messages):
            if role == "fact_checker":
                text = messages[-1]["content"]
                start = text.index("## For the public") + len("## For the public\n")
                first = text[start:].split(".")[0] + "."
                return json.dumps({"corrections": [{"wrong": first, "right": first.replace("most likely", "certainly"),
                                                    "why": "the facts show it"}]})
            return critic_says("approve")(role, messages)
        case, _, _ = run_with(fn)
        self.assertNotIn("certainly", case.report)

    def test_sql_writer_gets_the_window_and_a_local_time_formula(self):
        _, prompts, _ = run_with(critic_says("approve"))
        p = prompts["investigator.sql"][0]
        self.assertIn("hr_utc BETWEEN '2026-10-16 22:00' AND '2026-10-23 22:00'", p)
        self.assertIn("from_utc_timestamp(to_timestamp(hr_utc, 'yyyy-MM-dd HH:mm'), 'America/Chicago')", p)


class WholeLocalDays(unittest.TestCase):
    """Live run 20261024T035519Z: the window began 5 PM CDT, so the model's 'Oct 17' daily mean covered only 7 hours
    (with the 9 PM release) and read 2,094 cfs instead of about 878. Daily figures now get whole-local-day bounds."""

    def test_bounds_for_the_live_window(self):
        from agent import agents
        self.assertEqual(agents.full_local_days(("2026-10-16 22:00", "2026-10-23 21:00"), "America/Chicago"),
                         ("2026-10-17 05:00", "2026-10-23 05:00", "2026-10-17", "2026-10-22"))

    def test_a_window_starting_at_local_midnight_keeps_that_day(self):
        from agent import agents
        self.assertEqual(agents.full_local_days(("2026-10-17 05:00", "2026-10-24 05:00"), "America/Chicago")[:2],
                         ("2026-10-17 05:00", "2026-10-24 05:00"))

    def test_daylight_saving_change_is_respected(self):
        from agent import agents  # DST ends Nov 1 2026: midnight CST is 06:00 UTC, midnight CDT is 05:00 UTC
        self.assertEqual(agents.full_local_days(("2026-10-29 12:00", "2026-11-04 12:00"), "America/Chicago")[:2],
                         ("2026-10-30 05:00", "2026-11-04 06:00"))
        self.assertEqual(agents.full_local_days(("2026-10-16 22:00", "2026-10-23 21:00"), "America/Phoenix")[:2],
                         ("2026-10-17 07:00", "2026-10-23 07:00"))

    def test_the_sql_writer_is_given_the_whole_day_bounds(self):
        _, prompts, _ = run_with(critic_says("approve"))
        p = prompts["investigator.sql"][0]
        self.assertIn("DAILY FIGURES", p)
        self.assertIn("hr_utc >= '2026-10-17 05:00' AND hr_utc < '2026-10-23 05:00'", p)
        self.assertIn("2026-10-17 to 2026-10-22", p)


class RulesReachEveryoneWhoWritesSql(unittest.TestCase):
    """Live run 20261024T040200Z: the planner and critic put whole SQL (DATE_SUB, a 5 PM start) in each check's 'how',
    and the investigator copied it. They now get the same rules, and the investigator is told the rules win."""

    def test_planner_and_critic_get_the_rules(self):
        _, prompts, _ = run_with(critic_says("approve"))
        for role in ("planner", "critic"):
            with self.subTest(role):
                p = prompts[role][0]
                self.assertIn("hr_utc >= '2026-10-17 05:00' AND hr_utc < '2026-10-23 05:00'", p)
                self.assertIn("from_utc_timestamp(to_timestamp(hr_utc, 'yyyy-MM-dd HH:mm'), 'America/Chicago')", p)

    def test_investigator_reads_the_rules_after_the_suggestion_and_is_told_they_win(self):
        _, prompts, _ = run_with(critic_says("approve"))
        p = prompts["investigator.sql"][0]
        self.assertGreater(p.index("DAILY FIGURES"), p.index("Suggested approach"))
        self.assertIn("these rules override the suggested approach", p)


class FactCheckNotesAreNotCorrections(unittest.TestCase):
    """Live run 20261024T040831Z: the fact-checker 'corrected' a sentence by appending
    '(The FACTS confirm this interpretation; no contradiction.)', why 'No contradiction; included for completeness.'
    A note that the text is fine is not a correction and must not enter the report."""

    def _run(self, make):
        def fn(role, messages):
            if role == "fact_checker":
                text = messages[-1]["content"]
                start = text.index("## Answer") + len("## Answer")
                first = text[start:].strip().split(".")[0] + "."
                right, why = make(first)
                return json.dumps({"corrections": [{"wrong": first, "right": right, "why": why}]})
            return critic_says("approve")(role, messages)
        case, _, _ = run_with(fn)
        return case

    def test_the_live_note_is_refused(self):
        case = self._run(lambda s: (s + " (The FACTS confirm this interpretation; no contradiction.)",
                                    "No contradiction; included for completeness."))
        self.assertNotIn("no contradiction", case.report.lower())
        self.assertNotIn("## Fact-check", case.report)

    def test_an_appended_confirmation_is_refused_even_with_a_plausible_reason(self):
        case = self._run(lambda s: (s + " (Confirmed by the ground-truth facts.)", "The FACTS support it."))
        self.assertNotIn("Confirmed by the ground-truth", case.report)

    def test_a_real_correction_still_applies(self):
        case = self._run(lambda s: (s.replace(".", " on Oct 22.", 1) if s.endswith(".") else s + " on Oct 22.",
                                    "The date was missing; the facts give Oct 22."))
        self.assertIn("## Fact-check", case.report)
