"""Find the setting that makes Nemotron answer fast (less hidden reasoning) through Nightshift.

Nightshift aborts requests that take longer than ~45 s, and Nemotron 3.5 reasons for thousands of tokens
before answering. This sends the same mid-size prompt with different "reasoning off" switches and reports
time, total tokens and whether a JSON answer came back.   Run:  python3 scripts/probe_thinking.py
"""
import json
import os
import time

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()
client = OpenAI(base_url=os.environ["METERLESS_BASE_URL"], api_key=os.environ["METERLESS_RUNTIME_TOKEN"],
                default_headers={"meterless-app": "rivertruth-probe", "meterless-scope": "user"},
                timeout=120, max_retries=0)
MODEL = os.environ.get("FAST_MODEL", "nvidia/nemotron-3.5-lightning-30b-a3b")
TASK = ("Hourly river flow (cfs) at a gauge: 00:00 176, 01:00 1104, 02:00 4377, 03:00 2715, 04:00 1693. "
        "Rainfall in all those hours: 0 mm. A dam is upstream. "
        'Reply with ONLY JSON: {"most_likely_cause": "...", "evidence": "one sentence with numbers"}')

VARIANTS = [
    ("baseline", None, None),
    ("chat_template_kwargs enable_thinking=false", None, {"chat_template_kwargs": {"enable_thinking": False}}),
    ("system '/no_think'", "/no_think", None),
    ("system 'detailed thinking off'", "detailed thinking off", None),
    ("reasoning_effort=low", None, {"reasoning_effort": "low"}),
    ("nvext reasoning off", None, {"nvext": {"reasoning": False}}),
]

for name, system, extra in VARIANTS:
    msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": TASK}]
    t0 = time.time()
    try:
        r = client.chat.completions.create(model=MODEL, messages=msgs, max_tokens=4000, temperature=0.2, extra_body=extra)
        text = (r.choices[0].message.content or "").strip()
        u = r.usage
        ok = "{" in text
        print(f"{name:45s} {time.time()-t0:5.1f}s  out_tokens={getattr(u,'completion_tokens',None)}  json={'yes' if ok else 'no'}  {text[:70]!r}")
    except Exception as e:  # noqa: BLE001
        print(f"{name:45s} {time.time()-t0:5.1f}s  ERROR {str(e)[:90]}")
