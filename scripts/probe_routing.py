"""Probe which model Nightshift actually serves for a given 'model' value.

Uses the OpenAI SDK exactly like smoke_test.py (which works), with retries OFF
so every failure is visible. No secrets are printed.

Usage:  python3 scripts/probe_routing.py [model ...]   (default: FAST_MODEL and DEEP_MODEL from .env)
"""
import os
import sys
import time

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()
client = OpenAI(
    base_url=os.environ["METERLESS_BASE_URL"],
    api_key=os.environ["METERLESS_RUNTIME_TOKEN"],
    default_headers={"meterless-app": os.environ.get("METERLESS_APP", "rivertruth"), "meterless-scope": "user"},
    timeout=120,
    max_retries=0,
)
models = sys.argv[1:] or [os.environ.get("FAST_MODEL"), os.environ.get("DEEP_MODEL")]
TRIES = 5

for model in models:
    results = []
    for _ in range(TRIES):
        t0 = time.time()
        try:
            r = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "Reply with the single word: ready"}],
                max_tokens=200,
            )
            text = (r.choices[0].message.content or "").strip()[:20] if r.choices else ""
            results.append(f"{r.model} ({time.time() - t0:.1f}s) '{text}'")
        except Exception as e:  # noqa: BLE001
            results.append(f"ERROR ({time.time() - t0:.1f}s) {str(e)[:90]}")
    ok = sum(not x.startswith("ERROR") for x in results)
    print(f"\nrequested {model}: {ok}/{TRIES} ok")
    for x in results:
        print("   ", x)
