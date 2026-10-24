"""Does streaming avoid Nightshift's ~45 s abort on long (reasoning-heavy) requests?

Sends the real planner prompt twice: normal and streamed. Run:  python3 scripts/probe_stream.py
"""
import os
import sys
import time

sys.path.insert(0, os.getcwd())
from dotenv import load_dotenv  # noqa: E402
from openai import OpenAI  # noqa: E402

from agent.catalog import CATALOG  # noqa: E402

load_dotenv()
client = OpenAI(base_url=os.environ["METERLESS_BASE_URL"], api_key=os.environ["METERLESS_RUNTIME_TOKEN"],
                default_headers={"meterless-app": "rivertruth-probe", "meterless-scope": "user"},
                timeout=300, max_retries=0)
MODEL = os.environ.get("FAST_MODEL", "nvidia/nemotron-3.5-lightning-30b-a3b")
PROMPT = CATALOG + """
QUESTION: Black Warrior River flow at Northport jumped from 180 to 5,640 cfs around 9 PM CDT on Oct 22 with zero rain,
while the NWM showed ~200 cfs and the NWS gauge at Oliver Lock and Dam showed ~1,000-1,200 cfs. Why do the sources disagree?
Act as the PLANNER. Propose 3-4 competing hypotheses and up to 4 SQL-answerable checks.
Reply with ONLY JSON: {"hypotheses":[{"id":"H1","claim":"..."}],"checks":[{"id":"C1","hypothesis":"H1","purpose":"...","how":"..."}]}"""
MSGS = [{"role": "system", "content": "/no_think"}, {"role": "user", "content": PROMPT}]
EXTRA = {"chat_template_kwargs": {"enable_thinking": False}}

for mode in ("normal", "stream"):
    t0 = time.time()
    try:
        if mode == "normal":
            r = client.chat.completions.create(model=MODEL, messages=MSGS, max_tokens=8000, temperature=0.2, extra_body=EXTRA)
            text = (r.choices[0].message.content or "").strip()
            info = f"out_tokens={getattr(r.usage, 'completion_tokens', None)} model={r.model}"
        else:
            first = None
            chunks, reasoning, model = [], 0, None
            stream = client.chat.completions.create(model=MODEL, messages=MSGS, max_tokens=8000, temperature=0.2,
                                                    extra_body=EXTRA, stream=True)
            for ev in stream:
                model = model or getattr(ev, "model", None)
                if not ev.choices:
                    continue
                d = ev.choices[0].delta
                if first is None:
                    first = time.time() - t0
                if getattr(d, "content", None):
                    chunks.append(d.content)
                rc = getattr(d, "reasoning_content", None) or (getattr(d, "model_extra", None) or {}).get("reasoning_content")
                if rc:
                    reasoning += 1
            text = "".join(chunks).strip()
            info = f"first_chunk={first if first is None else round(first, 1)}s reasoning_chunks={reasoning} model={model}"
        print(f"{mode:7s} {time.time() - t0:6.1f}s  {info}  json={'yes' if '{' in text else 'no'}  {text[:60]!r}")
    except Exception as e:  # noqa: BLE001
        print(f"{mode:7s} {time.time() - t0:6.1f}s  ERROR {str(e)[:110]}")
