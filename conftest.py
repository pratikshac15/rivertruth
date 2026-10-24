"""pytest configuration: lets `python -m pytest` find the `agent`, `world_model` and `tests` packages from the repo root.
(`bash scripts/run_tests.sh` uses the standard library's unittest and needs no pytest.)"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
