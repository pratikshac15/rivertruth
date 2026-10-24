#!/usr/bin/env bash
# Replay the recorded demo with no Zetaris, no Nightshift and no credentials (sample mode).
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PYTHON:-python3}
[ -x .venv/bin/python3 ] && PY=.venv/bin/python3
export SAMPLE_MODE=true RECORD_SAMPLES=false
export WORLD_DIR="$(mktemp -d)" LOG_DIR="logs/replay"
for c in 1 2 3; do
  $PY -m agent.main --case "$c"
done
$PY scripts/record_web_samples.py --replay
echo "Replayed all cases and the web app flows from data/sample (World Model: $WORLD_DIR)."
echo "To open the web app in sample mode: SAMPLE_MODE=true $PY -m web.server"
