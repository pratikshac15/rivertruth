#!/usr/bin/env bash
# Record the demo LIVE (Zetaris + Nightshift running) into data/sample, starting from an empty World Model:
# the three investigation cases, plus the web app's map, River checks and charts for a few gauges.
# Afterwards anyone can replay them with scripts/replay_samples.sh (no services or credentials), and the run logs,
# which show the model that handled every request, are copied to output_examples/logs/.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PYTHON:-python3}
[ -x .venv/bin/python3 ] && PY=.venv/bin/python3
stamp=$(date +%Y%m%d_%H%M%S)
[ -d data/sample ] && mv data/sample "dev_archive/sample_old_$stamp" 2>/dev/null || true
[ -d data/world ] && mv data/world "dev_archive/world_old_$stamp" 2>/dev/null || true
$PY scripts/refresh_data.py
rm -rf data/sample   # the refresh queries are not part of the samples
export RECORD_SAMPLES=true   # recording is the only thing that writes data/sample
export FREEZE_SQL=true       # every case sees the same data snapshot, so all of them replay exactly
export LOG_DIR="logs/recording_$stamp"
for c in 1 2 3; do
  $PY -m agent.main --case "$c"
done
$PY scripts/record_web_samples.py
mkdir -p output_examples/logs
cp "$LOG_DIR"/run_*.jsonl output_examples/logs/
echo "Recorded. Commit data/sample/ and output_examples/ (reports, case files and logs) to the repo."
