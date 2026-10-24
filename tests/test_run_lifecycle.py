"""Every run ends: it is always closed in the World Model, bounded by a total deadline, and can be cancelled
(audit blocker 4)."""
import threading
import time
import unittest

from tests.fakes import Env


def slow_sql(env, seconds):
    from agent import llm
    base = env.llm.chat

    def chat(role, messages, **k):
        if role == "investigator.sql":
            time.sleep(seconds)
        return base(role, messages, **k)
    llm.chat = chat


class Lifecycle(unittest.TestCase):
    def test_a_crash_still_closes_the_run_as_failed(self):
        with Env():
            from agent import log, memory, orchestrator, snapshot
            saved = snapshot.build
            snapshot.build = lambda site: (_ for _ in ()).throw(RuntimeError("disk on fire"))
            try:
                with self.assertRaises(RuntimeError):
                    orchestrator.run("Why?", "TODA1")
            finally:
                snapshot.build = saved
            self.assertEqual(memory.run_status(log.run_id()), "failed")

    def test_a_normal_run_is_closed_as_complete(self):
        with Env():
            from agent import log, memory, orchestrator
            orchestrator.run("Why?", "TODA1")
            self.assertEqual(memory.run_status(log.run_id()), "complete")

    def test_the_total_deadline_stops_a_slow_run_and_still_reports(self):
        with Env() as env:
            from agent import config, log, memory, orchestrator
            slow_sql(env, 1.0)   # two checks take 2 s: more than the 1.5 s limit
            config.RUN_DEADLINE_S, saved = 1.5, config.RUN_DEADLINE_S
            try:
                t0 = time.time()
                case = orchestrator.run("Why?", "TODA1")
                took = time.time() - t0
            finally:
                config.RUN_DEADLINE_S = saved
            self.assertLess(took, 6)
            self.assertIn("time limit", case.report)
            self.assertEqual(memory.run_status(log.run_id()), "timed_out")

    def test_cancel_stops_the_run(self):
        with Env() as env:
            from agent import log, memory, orchestrator, runctl
            slow_sql(env, 0.4)
            out = {}
            t = threading.Thread(target=lambda: out.setdefault("case", orchestrator.run("Why?", "TODA1")))
            t.start()
            time.sleep(0.5)
            runctl.cancel()
            t.join(10)
            self.assertFalse(t.is_alive())
            self.assertIn("cancelled", out["case"].report.lower())
            self.assertEqual(memory.run_status(log.run_id()), "cancelled")

    def test_each_model_call_gets_only_the_time_left(self):
        with Env() as env:
            from agent import llm, runctl
            seen = []
            saved = llm._stream
            llm._stream = lambda model, msgs, budget, temp, extra, timeout=None: seen.append(timeout) or ('{"a":1}', model, {}, None)
            llm.chat = env._saved[1]
            try:
                runctl.start(30)
                llm.chat("x", [{"role": "user", "content": "q"}])
                runctl.start(0.01)
                time.sleep(0.05)
                with self.assertRaises(runctl.RunStopped):
                    llm.chat("x", [{"role": "user", "content": "q"}])
            finally:
                llm._stream = saved
                runctl.start(None)
            self.assertLessEqual(seen[0], 30)


class CancelEndpoint(unittest.TestCase):
    def test_cancel_endpoint(self):
        from tests.test_integration_server import Server
        with Env() as env, Server(env) as s:
            slow_sql(env, 0.4)
            self.assertEqual(s.post("/api/run", {"question": "Why?", "site": "TODA1"})[0], 200)
            time.sleep(0.3)
            code, d = s.post("/api/cancel", {})
            self.assertEqual(code, 200)
            self.assertTrue(d["cancelling"])
            from web import server
            for _ in range(100):
                if not server.STATE["running"]:
                    break
                time.sleep(0.1)
            self.assertFalse(server.STATE["running"])


if __name__ == "__main__":
    unittest.main()
