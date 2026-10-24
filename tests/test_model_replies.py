"""Every model reply is parsed carefully and validated before use (audit blocker 3), and the failure paths of the
agent loop are tested: malformed JSON, the critic's revise loop, SQL retry, and falling back to the fast model."""
import json
import unittest

from tests.fakes import Env


class ExtractJson(unittest.TestCase):
    def test_last_valid_object_wins_over_an_example(self):
        from agent import llm
        text = 'For example {"sql": "SELECT 1"} ... final answer: {"sql": "SELECT 2"}'
        self.assertEqual(llm.extract_json(text)["sql"], "SELECT 2")

    def test_braces_inside_strings_do_not_break_parsing(self):
        from agent import llm
        self.assertEqual(llm.extract_json('{"sql": "SELECT \'}\' AS x"}')["sql"], "SELECT '}' AS x")
        self.assertEqual(llm.extract_json('note {"a": "{{"} done')["a"], "{{")

    def test_fenced_block_and_think_tags(self):
        from agent import llm
        self.assertEqual(llm.extract_json('<think>{"x":1}</think>\n```json\n{"x": 2}\n```')["x"], 2)

    def test_no_object_is_none(self):
        from agent import llm
        self.assertIsNone(llm.extract_json("no json here"))


class Schemas(unittest.TestCase):
    def test_planner_drops_broken_items_and_renumbers_checks(self):
        from agent import schemas
        p = schemas.planner({"hypotheses": [{"id": "H1", "claim": "release"}, {"id": "H2"}, "junk"],
                             "checks": [{"id": "C?", "hypothesis": "H1", "purpose": "a", "how": "b"},
                                        {"id": "C?", "purpose": "no hypothesis"},
                                        {"id": "C1", "hypothesis": "H7", "purpose": "unknown H"},
                                        {"id": "C9", "hypothesis": "H1", "purpose": "c"}]})
        self.assertEqual([h["id"] for h in p["hypotheses"]], ["H1"])
        self.assertEqual([c["id"] for c in p["checks"]], ["C1", "C2"])

    def test_planner_with_no_usable_hypothesis_is_an_error(self):
        from agent import schemas
        with self.assertRaises(schemas.SchemaError):
            schemas.planner({"hypotheses": [{"id": "H1"}], "checks": []})

    def test_critic_is_coerced_to_known_ids_and_enums(self):
        from agent import schemas
        c = schemas.critic({"verdict": "MAYBE", "hypothesis_status": [{"id": "H1", "status": "supported"}, {"id": "H9", "status": "x"}],
                            "issues": "one issue", "new_checks": [{"id": "C?", "hypothesis": "H1", "purpose": "p"},
                                                                  {"id": "C?", "hypothesis": "H2", "purpose": "q"}]},
                           hyp_ids={"H1", "H2"}, next_check=3)
        self.assertEqual(c["verdict"], "revise")
        self.assertEqual(c["hypothesis_status"], {"H1": "supported"})
        self.assertEqual(c["issues"], ["one issue"])
        self.assertEqual([x["id"] for x in c["new_checks"]], ["C3", "C4"])
        c = schemas.critic({"verdict": "approve", "hypothesis_status": {"H1": "DEFINITELY TRUE", "H9": "supported"}},
                           hyp_ids={"H1"}, next_check=5)
        self.assertEqual(c["hypothesis_status"], {})

    def test_interpret_and_sql_and_factcheck(self):
        from agent import schemas
        self.assertEqual(schemas.interpret({"finding": "x", "stance": "PROVES IT"})["stance"], "unclear")
        with self.assertRaises(schemas.SchemaError):
            schemas.interpret({"stance": "supports"})
        with self.assertRaises(schemas.SchemaError):
            schemas.sql({"query": "SELECT 1"})
        self.assertEqual(schemas.fact_check({"corrections": [{"wrong": "a", "right": "b"}, {"right": "c"}, 5]}),
                         {"corrections": [{"wrong": "a", "right": "b", "why": ""}]})


class ChatJson(unittest.TestCase):
    def test_invalid_reply_gets_one_repair_then_raises(self):
        with Env() as env:
            from agent import llm, schemas
            replies = iter(['{"hypotheses": []}', '{"hypotheses": [{"id": "H1"}]}'])
            calls = []
            llm.chat = lambda role, m, **k: calls.append(role) or next(replies)  # noqa: E731
            with self.assertRaises(schemas.SchemaError):
                llm.chat_json("planner", [{"role": "user", "content": "x"}], validate=schemas.planner)
            self.assertEqual(calls, ["planner", "planner:repair"])

    def test_repair_succeeds(self):
        with Env() as env:
            from agent import llm, schemas
            replies = iter(["not json", '{"sql": "SELECT 1"}'])
            llm.chat = lambda role, m, **k: next(replies)  # noqa: E731
            self.assertEqual(llm.chat_json("s", [{"role": "user", "content": "x"}], validate=schemas.sql)["sql"], "SELECT 1")


class LoopFailurePaths(unittest.TestCase):
    def _run(self, override, lid="TODA1"):
        with Env() as env:
            from agent import llm, orchestrator
            base = env.llm.chat

            def chat(role, messages, **k):
                out = override(role, messages)
                return base(role, messages, **k) if out is None else out
            llm.chat = chat
            case = orchestrator.run("Why?", lid)
            return env, case

    def test_audit_malformed_replies_do_not_crash_the_run(self):
        cases = {
            "critic status as a list": lambda r, m: json.dumps({"verdict": "approve", "hypothesis_status": ["H1", "supported"],
                                                                 "issues": [], "new_checks": []}) if r.startswith("critic") else None,
            "check without hypothesis": lambda r, m: json.dumps({"hypotheses": [{"id": "H1", "claim": "a"}],
                                                                  "checks": [{"id": "C1", "purpose": "x"},
                                                                             {"id": "C2", "hypothesis": "H1", "purpose": "y"}]})
            if r == "planner" else None,
            "hypothesis without claim": lambda r, m: json.dumps({"hypotheses": [{"id": "H1"}, {"id": "H2", "claim": "b"}],
                                                                  "checks": [{"id": "C1", "hypothesis": "H2", "purpose": "y"}]})
            if r == "planner" else None,
            "not json at all": lambda r, m: "I cannot answer that." if r in ("planner", "planner:repair", "critic", "critic:repair") else None,
        }
        for name, fn in cases.items():
            with self.subTest(name):
                _, case = self._run(fn)
                self.assertIn("## For the public", case.report)
                ids = [c["id"] for c in case.checks]
                self.assertEqual(len(ids), len(set(ids)))
                self.assertTrue(all(i[0] == "C" and i[1:].isdigit() for i in ids), ids)

    def test_critic_placeholder_ids_become_unique(self):
        state = {"n": 0}

        def fn(role, messages):
            if role == "critic":
                state["n"] += 1
                if state["n"] == 1:
                    return json.dumps({"verdict": "revise", "hypothesis_status": {"H1": "unclear"}, "issues": ["more"],
                                       "new_checks": [{"id": "C?", "hypothesis": "H1", "purpose": "a", "how": "x"},
                                                      {"id": "C?", "hypothesis": "H2", "purpose": "b", "how": "x"}]})
            return None
        _, case = self._run(fn)
        self.assertEqual([c["id"] for c in case.checks], ["C1", "C2", "C3", "C4"])
        self.assertEqual(state["n"], 2)  # the revise loop ran a second review over the new evidence

    def test_sql_error_is_retried_with_the_error_text(self):
        with Env() as env:
            from agent import llm, orchestrator, zetaris
            real, seen, fails = env.zet.run_sql, [], {"n": 1}

            def flaky(sql, **k):
                if k.get("agent") and fails["n"]:
                    fails["n"] -= 1
                    raise zetaris.SQLError("cannot resolve column foo")
                return real(sql, **k)
            zetaris.run_sql = flaky
            base = env.llm.chat
            llm.chat = lambda role, m, **k: (seen.append(m[-1]["content"]) if role == "investigator.sql" else None) or base(role, m, **k)  # noqa: E731
            case = orchestrator.run("Why?", "TODA1")
            self.assertIn("cannot resolve column foo", seen[1])
            self.assertFalse(case.evidence[0].error)

    def test_deep_model_failure_falls_back_to_the_fast_model(self):
        with Env() as env:
            from agent import config, llm
            real_chat = env._saved[1]
            served = []

            def stream(model, msgs, budget, temperature, extra, timeout=None):
                if model == config.DEEP_MODEL and model != config.FAST_MODEL:
                    raise RuntimeError("502 from provider")
                served.append(model)
                return '{"ok": 1}', model, {}, None
            llm._stream, saved = stream, llm._stream
            llm.chat = real_chat
            config.DEEP_MODEL, saved_deep = "deep/model", config.DEEP_MODEL
            try:
                out = llm.chat("planner", [{"role": "user", "content": "x"}], model="deep/model", attempts=1)
            finally:
                llm._stream, config.DEEP_MODEL = saved, saved_deep
            self.assertEqual(out, '{"ok": 1}')
            self.assertEqual(served, [config.FAST_MODEL])


if __name__ == "__main__":
    unittest.main()


LIVE_CRITIC = {  # live run 20261024T034054Z: "revise", yet everything settled and nothing left to check
    "verdict": "revise",
    "issues": ["C1/E1 correctly supports H1: ...", "C3/E3 correctly supports H3: ...",
               "No finding contradicts ground-truth facts. All three hypotheses are evidence-backed. "
               "The explanation is complete for the Oct 22 evening discrepancy."],
    "hypothesis_status": {"H1": "supported", "H2": "supported", "H3": "supported"}, "new_checks": []}


class CriticSaysReviseWithNothingToRevise(unittest.TestCase):
    """The critic must either approve or say what is wrong. A 'revise' with every hypothesis settled and no new check is
    sent back once; if it is still 'revise', the verdict stands (the code never upgrades a verdict itself)."""

    def test_the_live_reply_is_flagged_as_inconsistent_but_keeps_its_cleaned_value(self):
        from agent import schemas
        with self.assertRaises(schemas.Inconsistent) as cm:
            schemas.critic(LIVE_CRITIC, {"H1", "H2", "H3"}, 5)
        self.assertIn("approve", str(cm.exception))
        self.assertEqual(cm.exception.value["verdict"], "revise")
        self.assertTrue(issubclass(schemas.Inconsistent, schemas.SchemaError))

    def test_a_revise_with_a_reason_to_revise_is_accepted(self):
        from agent import schemas
        ids = {"H1", "H2", "H3"}
        unclear = dict(LIVE_CRITIC, hypothesis_status={"H1": "supported", "H2": "unclear", "H3": "supported"})
        self.assertEqual(schemas.critic(unclear, ids, 5)["verdict"], "revise")
        missing = dict(LIVE_CRITIC, hypothesis_status={"H1": "supported"})
        self.assertEqual(schemas.critic(missing, ids, 5)["verdict"], "revise")
        more = dict(LIVE_CRITIC, new_checks=[{"id": "C?", "hypothesis": "H2", "purpose": "p", "how": "h"}])
        self.assertEqual(schemas.critic(more, ids, 5)["verdict"], "revise")
        self.assertEqual(schemas.critic(dict(LIVE_CRITIC, verdict="approve"), ids, 5)["verdict"], "approve")

    def _run(self, repair_reply):
        with Env() as env:
            from agent import llm, orchestrator
            base, seen = env.llm.chat, []

            def chat(role, messages, **k):
                if role.startswith("critic"):
                    seen.append((role, messages[-1]["content"]))
                    hyp = {h: "supported" for h in ("H1", "H2", "H3")}
                    reply = dict(LIVE_CRITIC, hypothesis_status=hyp)
                    if role.endswith(":repair"):
                        if repair_reply == "garbage":
                            return "Sorry, I cannot."
                        reply = dict(reply, verdict=repair_reply)
                    return json.dumps(reply)
                return base(role, messages, **k)
            llm.chat = chat
            case = orchestrator.run("Why?", "TODA1")
            return case, seen

    def test_in_the_loop_the_critic_is_asked_once_and_can_approve(self):
        case, seen = self._run("approve")
        self.assertEqual([r for r, _ in seen], ["critic", "critic:repair"])
        self.assertIn("approve", seen[1][1])
        self.assertEqual(case.critiques[-1]["verdict"], "approve")
        self.assertNotIn("Capped at Medium", case.report)

    def test_if_it_still_says_revise_the_verdict_stands_and_confidence_stays_capped(self):
        case, seen = self._run("revise")
        self.assertEqual([r for r, _ in seen], ["critic", "critic:repair"])  # no third call, no compact critic
        self.assertEqual(case.critiques[-1]["verdict"], "revise")
        self.assertIn("Capped at Medium", case.report)

    def test_an_unreadable_second_reply_keeps_the_first_verdict(self):
        case, seen = self._run("garbage")
        self.assertEqual([r for r, _ in seen], ["critic", "critic:repair"])
        self.assertEqual(case.critiques[-1]["verdict"], "revise")
        self.assertIn("Capped at Medium", case.report)
