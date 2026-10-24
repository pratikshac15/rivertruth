"""Predictable defaults (audit blocker 9), a clean repository (blocker 10), and repair of a broken Zetaris view."""
import fnmatch
import importlib
import os
import re
import unittest

from tests.fakes import Env

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Defaults(unittest.TestCase):
    def test_live_runs_do_not_write_into_the_demo_samples_by_default(self):
        from agent import config
        saved = os.environ.pop("RECORD_SAMPLES", None)
        try:
            importlib.reload(config)
            self.assertFalse(config.RECORD_SAMPLES)
        finally:
            if saved is not None:
                os.environ["RECORD_SAMPLES"] = saved
            importlib.reload(config)

    def test_recording_script_turns_recording_on_explicitly(self):
        with open(os.path.join(ROOT, "scripts", "record_samples.sh")) as f:
            self.assertIn("RECORD_SAMPLES=true", f.read())

    def test_temperature_is_zero_for_sql_and_json_roles(self):
        from agent import llm
        for role in ("planner", "investigator.sql", "investigator.interpret", "critic", "critic.quick", "fact_checker",
                     "planner:repair", "critic:fallback"):
            self.assertEqual(llm.temperature_for(role), 0.0, role)
        self.assertGreater(llm.temperature_for("reporter"), 0.0)

    def test_no_random_run_marker_and_role_temperature_reaches_the_provider(self):
        with Env() as env:
            from agent import llm
            seen = []
            saved = llm._stream
            llm._stream = lambda model, msgs, budget, temp, extra, timeout=None: seen.append((msgs, temp)) or ('{"a":1}', model, {}, None)
            llm.chat = env._saved[1]
            try:
                llm.chat("investigator.sql", [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}])
            finally:
                llm._stream = saved
            msgs, temp = seen[0]
            self.assertEqual(temp, 0.0)
            self.assertNotIn("[investigation", " ".join(m["content"] for m in msgs))


def gitignored(path, patterns):
    rel = os.path.relpath(path, ROOT)
    parts = rel.split(os.sep)
    for p in patterns:
        p = p.rstrip("/")
        if any(fnmatch.fnmatch(part, p) for part in parts) or fnmatch.fnmatch(rel, p) or rel.startswith(p + os.sep):
            return True
    return False


class Hygiene(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, ".gitignore")) as f:
            self.ignore = [l.strip() for l in f if l.strip() and not l.startswith("#")]

    def tracked_files(self):
        for d, dirs, files in os.walk(ROOT):
            dirs[:] = [x for x in dirs if x != ".git" and not gitignored(os.path.join(d, x), self.ignore)]
            for f in files:
                p = os.path.join(d, f)
                if not gitignored(p, self.ignore):
                    yield p

    def test_secrets_and_local_state_are_ignored(self):
        for must in (".env", "logs/", ".venv/", "data/world/", "dev_archive/", ".DS_Store", "__pycache__/"):
            self.assertIn(must, self.ignore, must)

    def test_env_example_has_placeholders_only(self):
        with open(os.path.join(ROOT, ".env.example")) as f:
            for line in f:
                if "=" in line and not line.startswith("#"):
                    key, val = line.strip().split("=", 1)
                    if "KEY" in key or "TOKEN" in key:
                        self.assertTrue(val.startswith("your-") or val == "", f"{key} must be a placeholder")

    def test_no_secret_or_personal_email_in_files_that_would_be_committed(self):
        pat = re.compile(r"(nvapi-[A-Za-z0-9_-]{10,}|sk-[A-Za-z0-9]{20,}|eyJhbGciOi|[A-Za-z0-9._%+-]+@gmail\.com)")
        hits = []
        for p in self.tracked_files():
            if p.endswith((".png", ".jpg", ".pdf", ".pyc")) or "/data/sample/" in p:
                continue
            try:
                text = open(p, encoding="utf-8").read()
            except (UnicodeDecodeError, OSError):
                continue
            if os.path.basename(p) == "test_defaults_hygiene.py":
                continue
            for m in pat.finditer(text):
                hits.append(f"{os.path.relpath(p, ROOT)}: {m.group(0)[:12]}...")
        self.assertEqual(hits, [])

    def test_no_empty_placeholder_files(self):
        p = os.path.join(ROOT, "conftest.py")
        if os.path.exists(p):
            self.assertGreater(os.path.getsize(p), 20, "conftest.py is an empty placeholder")


class ViewRepair(unittest.TestCase):
    def test_a_broken_existing_view_is_dropped_and_rebuilt(self):
        with Env() as env:
            from agent import scout
            env.zet.created.add("water.hc_ckla1")       # registered earlier...
            env.zet.broken.add("water.hc_ckla1")        # ...but its definition no longer works
            site = scout.ensure_site("CKLA1")
            self.assertTrue(any(c.startswith("DROP VIEW water.hc_ckla1") for c in env.zet.calls))
            self.assertNotIn("water.hc_ckla1", env.zet.broken)
            from agent import zetaris
            self.assertTrue(zetaris.run_sql(site.rows_sql())["rows"])

    def test_a_healthy_existing_view_is_left_alone(self):
        with Env() as env:
            from agent import scout
            env.zet.created.add("water.hc_ckla1")
            scout.ensure_site("CKLA1")
            self.assertFalse(any(c.startswith("DROP") for c in env.zet.calls))


if __name__ == "__main__":
    unittest.main()


class SampleModeCoversTheWebApp(unittest.TestCase):
    """Judges open the web app in sample mode: the recording must include the map and River checks, and the logs that
    show which model handled each request must be copied into the repo."""

    def test_recording_includes_map_river_checks_and_logs(self):
        with open(os.path.join(ROOT, "scripts", "record_samples.sh")) as f:
            s = f.read()
        self.assertIn("scripts/record_web_samples.py", s)
        self.assertIn("output_examples/logs", s)

    def test_web_sample_recorder_replays_without_services(self):
        """Record the web flows against the fakes, then replay them in SAMPLE_MODE with no Zetaris and no model."""
        import subprocess
        import sys
        import tempfile
        d = tempfile.mkdtemp(prefix="rt-sample-")
        env = {**os.environ, "SAMPLE_DIR": d, "WORLD_DIR": os.path.join(d, "world"), "RT_FAKE_SERVICES": "1"}
        rec = subprocess.run([sys.executable, "scripts/record_web_samples.py"], cwd=ROOT, capture_output=True, text=True,
                             env={**env, "RECORD_SAMPLES": "true"}, timeout=300)
        self.assertEqual(rec.returncode, 0, rec.stdout[-800:] + rec.stderr[-800:])
        rep = subprocess.run([sys.executable, "scripts/record_web_samples.py", "--replay"], cwd=ROOT, capture_output=True,
                             text=True, env={**env, "SAMPLE_MODE": "true", "RECORD_SAMPLES": "false",
                                             "WORLD_DIR": os.path.join(d, "world2"), "RT_FAKE_SERVICES": ""}, timeout=300)
        self.assertEqual(rep.returncode, 0, rep.stdout[-800:] + rep.stderr[-800:])
        self.assertIn("gauges on the map", rep.stdout)
        self.assertIn("River check OK", rep.stdout)
