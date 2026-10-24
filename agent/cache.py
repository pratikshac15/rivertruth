"""Sample-mode cache: records live SQL results and model replies so the agent can replay without Zetaris or Nightshift."""
import hashlib
import re
import json
import os

from . import config


# Run ids and timestamps change on every run; they are masked out of the cache key so a recorded investigation
# replays exactly (its prompts differ from the original only in those volatile tokens).
_VOLATILE = re.compile(r"\b\d{8}T\d{6}Z\b|\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?Z?")


def stable_key(key):
    return _VOLATILE.sub("<T>", key)


def _file(kind, key):
    h = hashlib.sha256(stable_key(key).encode("utf-8")).hexdigest()[:16]
    return os.path.join(config.SAMPLE_DIR, kind, h + ".json")


def get(kind, key):
    p = _file(kind, key)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return None


def put(kind, key, value):
    if not config.RECORD_SAMPLES:
        return
    p = _file(kind, key)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"key": key[:500], "value": value}, f, indent=1, default=str)
