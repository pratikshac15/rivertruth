"""Does Nightshift serve cached replies, and what bypasses the cache?

Sends a reasoning question several ways and shows latency + whether the reply text is identical to the first.
A cached reply is near-instant and identical.        Run:  python3 scripts/probe_cache.py
"""
import os
import time
import uuid

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()
c = OpenAI(base_url=os.environ["METERLESS_BASE_URL"], api_key=os.environ["METERLESS_RUNTIME_TOKEN"],
           default_headers={"meterless-app": "rivertruth", "meterless-scope": "user"}, timeout=120, max_retries=0)
MODEL = os.environ.get("FAST_MODEL", "nvidia/nemotron-3.5-lightning-30b-a3b")
BASE = ("River flow at a gauge went from 176 to 4377 cfs in two hours with 0 mm rain, at 9 PM, "
        "and did the same at 9 PM on the previous six days. In two sentences, what is the most likely cause?")


def ask(label, content, headers=None, temperature=0.7):
    t0 = time.time()
    r = c.chat.completions.create(model=MODEL, messages=[{"role": "user", "content": content}], max_tokens=3000,
                                  temperature=temperature, extra_headers=headers or {}, stream=False)
    text = (r.choices[0].message.content or "").strip()
    extra = (getattr(r, "model_extra", None) or {}).get("meterless") or {}
    return label, time.time() - t0, text, extra.get("from_cache"), extra.get("cache")


first = ask("1 original", BASE)
rows = [first,
        ask("2 identical again", BASE),
        ask("3 + nonce in text", BASE + f"\n(request {uuid.uuid4().hex[:8]})"),
        ask("4 + header cache-control: no-cache", BASE, {"Cache-Control": "no-cache"}),
        ask("5 + header meterless-cache: off", BASE, {"meterless-cache": "off"}),
        ask("6 slightly different numbers", BASE.replace("4377", "4120"))]
for label, dt, text, fc, cache in rows:
    same = "IDENTICAL to #1" if text == first[2] else "different"
    print(f"{label:36s} {dt:5.1f}s  {same:16s} from_cache={fc} cache={str(cache)[:40]}  {text[:50]!r}")
