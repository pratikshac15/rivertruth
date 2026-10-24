"""The test runner must never report success for a failing suite, and must run every test file."""
import os
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Runner(unittest.TestCase):
    def test_a_failing_test_makes_the_runner_fail(self):
        d = tempfile.mkdtemp(prefix="rt-failing-")
        with open(os.path.join(d, "test_always_fails.py"), "w") as f:
            f.write("import unittest\nclass T(unittest.TestCase):\n    def test_fails(self):\n        self.fail('on purpose')\n")
        r = subprocess.run(["bash", os.path.join(ROOT, "scripts", "run_tests.sh")], cwd=ROOT, capture_output=True,
                           text=True, env={**os.environ, "RT_TEST_DIR": d, "RT_SKIP_CONFORMANCE": "1"}, timeout=120)
        self.assertNotEqual(r.returncode, 0, r.stdout[-500:] + r.stderr[-500:])
        self.assertIn("FAILED", r.stdout + r.stderr)

    def test_world_model_tests_are_collected_by_unittest(self):
        suite = unittest.defaultTestLoader.loadTestsFromName("tests.test_world_model")
        self.assertGreaterEqual(suite.countTestCases(), 11)


if __name__ == "__main__":
    unittest.main()
