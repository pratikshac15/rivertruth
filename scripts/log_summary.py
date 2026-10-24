"""Summarise a run log: every model call (role, seconds, tokens, served model, cache) and SQL query.

Usage:  python3 scripts/log_summary.py            (latest run)
        python3 scripts/log_summary.py logs/run_XXXX.jsonl
"""
import glob
import json
import os
import sys

path = sys.argv[1] if len(sys.argv) > 1 else max(glob.glob("logs/run_*.jsonl"), key=os.path.getmtime)
print("log:", path)
llm = sql = errs = 0
for line in open(path, encoding="utf-8"):
    e = json.loads(line)
    k = e.get("kind")
    if k == "llm":
        llm += 1
        r = e.get("receipt") or {}
        print(f"LLM   {e.get('role',''):24} {e.get('seconds','?'):>6}s tok={e.get('tokens')} max={e.get('max_tokens')} "
              f"served={e.get('served_by')} cache={r.get('from_cache', r.get('cache'))}")
    elif k == "llm_error":
        errs += 1
        print(f"ERR   {e.get('role',''):24} {e.get('seconds','?'):>6}s max={e.get('max_tokens')} {e.get('error','')[:110]}")
    elif k == "sql":
        sql += 1
        status = ("ERROR " + e["error"][:80]) if e.get("error") else f"{e.get('rows')} rows"
        print(f"SQL   {e.get('seconds','?'):>6}s {status} | {' '.join(e.get('sql','').split())[:90]}")
print(f"\n{llm} model calls ok, {errs} model errors, {sql} SQL queries")
