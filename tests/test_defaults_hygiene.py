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

    def test_model_defaults_match_env_example_so_a_fresh_clone_replays_the_samples(self):
        """The sample cache is keyed by model name; a clone without .env must use the recorded models."""
        from agent import config
        with open(os.path.join(ROOT, ".env.example")) as f:
            example = dict(l.strip().split("=", 1) for l in f if re.match(r"^(FAST|DEEP)_MODEL=", l))
        saved = {k: os.environ.pop(k, None) for k in ("FAST_MODEL", "DEEP_MODEL")}
        try:
            importlib.reload(config)
            self.assertEqual(config.FAST_MODEL, example["FAST_MODEL"])
            self.assertEqual(config.DEEP_MODEL, example["DEEP_MODEL"])
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v
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
        if p.startswith("/"):  # anchored: only the path from the repository root
            p = p[1:]
            if fnmatch.fnmatch(rel, p) or rel.startswith(p + os.sep):
                return True
            continue
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
        for must in (".env", "/logs/", ".venv/", "data/world/", "dev_archive/", ".DS_Store", "__pycache__/"):
            self.assertIn(must, self.ignore, must)

    def test_an_anchored_pattern_only_ignores_the_top_level_folder(self):
        self.assertTrue(gitignored(os.path.join(ROOT, "logs", "run_x.jsonl"), ["/logs/"]))
        self.assertFalse(gitignored(os.path.join(ROOT, "output_examples", "logs", "run_x.jsonl"), ["/logs/"]))
        self.assertTrue(gitignored(os.path.join(ROOT, "output_examples", "logs", "run_x.jsonl"), ["logs/"]))

    def test_recorded_samples_and_their_logs_are_committed(self):
        """Sample mode needs data/sample/ and the recording logs under output_examples/logs/ in the repo."""
        self.assertNotIn("data/sample/", self.ignore)
        self.assertNotIn("logs/", self.ignore, "a bare logs/ also hides output_examples/logs/; use /logs/")

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
            if p.endswith((".png", ".jpg", ".pdf", ".pyc")):
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


class FailedQueriesReplay(unittest.TestCase):
    """A model-written query that Zetaris rejected while recording must fail the same way in sample mode, so the
    model's retry prompt (which quotes the error) matches the recording and the rest of the run replays."""

    def setUp(self):
        import tempfile
        from agent import config
        self.config = config
        self.saved = {k: getattr(config, k) for k in ("SAMPLE_DIR", "SAMPLE_MODE", "RECORD_SAMPLES", "FREEZE_SQL")}
        config.SAMPLE_DIR = tempfile.mkdtemp()
        for k in ("ZETARIS_REST_URL", "ZETARIS_API_KEY", "ZETARIS_ORG_ID"):
            os.environ.setdefault(k, "x")

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(self.config, k, v)

    def _record_failure(self, msg):
        import sys
        import types
        from agent import zetaris

        class Resp:
            status_code = 400
            text = msg

            def json(self):
                return {"error": True, "msg": msg}
        fake = types.SimpleNamespace(post=lambda *a, **k: Resp())
        saved = sys.modules.get("requests")
        sys.modules["requests"] = fake
        try:
            self.config.RECORD_SAMPLES, self.config.SAMPLE_MODE = True, False
            with self.assertRaises(zetaris.SQLError):
                zetaris.run_sql("SELECT CONVERT_TZ(hr_utc) FROM water.hourly_compare")
        finally:
            if saved is not None:
                sys.modules["requests"] = saved
            else:
                sys.modules.pop("requests", None)

    def test_sample_mode_raises_the_recorded_error(self):
        from agent import zetaris
        msg = "[UNRESOLVED_ROUTINE] Cannot resolve function `CONVERT_TZ`"
        self._record_failure(msg)
        self.config.RECORD_SAMPLES, self.config.SAMPLE_MODE = False, True
        with self.assertRaises(zetaris.SQLError) as cm:
            zetaris.run_sql("SELECT CONVERT_TZ(hr_utc) FROM water.hourly_compare")
        self.assertEqual(str(cm.exception), msg)

    def test_frozen_recording_raises_the_recorded_error_too(self):
        from agent import zetaris
        msg = "[UNRESOLVED_ROUTINE] Cannot resolve function `CONVERT_TZ`"
        self._record_failure(msg)
        self.config.SAMPLE_MODE, self.config.FREEZE_SQL = False, True
        with self.assertRaises(zetaris.SQLError) as cm:
            zetaris.run_sql("SELECT CONVERT_TZ(hr_utc) FROM water.hourly_compare")
        self.assertEqual(str(cm.exception), msg)


class FailedModelCallsReplay(unittest.TestCase):
    """If the big model failed while recording and the run fell back to the fast lane, sample mode must take the
    same fallback instead of stopping at the missing reply."""

    def setUp(self):
        import tempfile
        from agent import config, llm
        self.config, self.llm = config, llm
        self.saved = {k: getattr(config, k) for k in ("SAMPLE_DIR", "SAMPLE_MODE", "RECORD_SAMPLES")}
        self.saved_stream, self.saved_dump = llm._stream, llm._dump_prompt
        config.SAMPLE_DIR = tempfile.mkdtemp()
        llm._dump_prompt = lambda *a, **k: None

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(self.config, k, v)
        self.llm._stream, self.llm._dump_prompt = self.saved_stream, self.saved_dump

    def test_recorded_failure_replays_the_fallback(self):
        config, llm = self.config, self.llm
        deep = "nvidia/nemotron-3-ultra-550b-a55b"

        def stream(model, msgs, *a, **k):
            if model == deep:
                raise RuntimeError("no first token")
            return "fast answer", model, {}, None
        llm._stream = stream
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
        saved_sleep = llm.time.sleep
        llm.time.sleep = lambda s: None
        try:
            config.RECORD_SAMPLES, config.SAMPLE_MODE = True, False
            self.assertEqual(llm.chat("critic", msgs, model=deep, attempts=1), "fast answer")
        finally:
            llm.time.sleep = saved_sleep

        def no_network(*a, **k):
            raise AssertionError("sample mode must not call the model")
        llm._stream = no_network
        config.RECORD_SAMPLES, config.SAMPLE_MODE = False, True
        self.assertEqual(llm.chat("critic", msgs, model=deep, attempts=1), "fast answer")
