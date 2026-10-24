#!/usr/bin/env bash
# Run every test: World Model conformance, unit, integration and browser tests.
# Nothing here needs Zetaris, Nightshift or the internet: tests/fakes.py stands in for them.
# The script exits with unittest's own exit code, so a failing test always fails the run.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PYTHON:-python3}
[ -x .venv/bin/python3 ] && PY=.venv/bin/python3
TEST_DIR=${RT_TEST_DIR:-tests}
TOP=$([ "$TEST_DIR" = tests ] && echo . || echo "$TEST_DIR")
if [ -z "${RT_SKIP_CONFORMANCE:-}" ]; then
  echo "== World Model conformance =="
  $PY scripts/world_model_conformance.py
fi
echo "== Unit, integration and browser tests ($TEST_DIR) =="
out=$(mktemp)
set +e
$PY -m unittest discover -s "$TEST_DIR" -t "$TOP" -v >"$out" 2>&1
code=$?
set -e
grep -v -E "^(SOURCE SCOUT|   )" "$out" | tail -n 80   # progress lines from the app are hidden; the verdict is not
rm -f "$out"
exit $code
