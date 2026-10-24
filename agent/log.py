"""Run log: one JSON line per event (agent step, model call, SQL query). Shows judges how the agent reasoned."""
import json
import os
import time
from datetime import datetime, timezone

from . import config

_run_id = None
_path = None
_subscribers = []


def subscribe(fn):
    """Live listeners (the web UI) receive every event as it happens."""
    _subscribers.append(fn)
    return lambda: _subscribers.remove(fn) if fn in _subscribers else None


def start_run():
    global _run_id, _path
    os.makedirs(config.LOG_DIR, exist_ok=True)
    while True:  # run ids are per second; never reuse one (two runs in the same second would share a run context)
        _run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        _path = os.path.join(config.LOG_DIR, f"run_{_run_id}.jsonl")
        if not os.path.exists(_path):
            break
        time.sleep(1.05)
    open(_path, "a").close()
    return _run_id, _path


def event(kind, **fields):
    rec = {"t": round(time.time(), 3), "kind": kind, **fields}
    if _path:
        try:
            with open(_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, default=str) + "\n")
        except OSError:  # a missing or read-only log folder must never break a request
            pass
    for fn in list(_subscribers):
        try:
            fn(rec)
        except Exception:  # noqa: BLE001
            pass
    return rec


def path():
    return _path


def run_id():
    return _run_id
