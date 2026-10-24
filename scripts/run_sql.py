"""Run a .sql file against Zetaris over REST, one statement at a time.

Usage:  python3 scripts/run_sql.py sql/01_register_sources.sql [--stop-on-error]
Skips comments; treats "already exists" as OK so re-runs are safe; prints result rows (first 20).
"""
import os
import re
import sys
import time
import uuid

import requests
from dotenv import load_dotenv

load_dotenv()
URL = os.environ["ZETARIS_REST_URL"].rstrip("/") + "/sql-editor/sqls/run"
HEADERS = {"Authorization": "Bearer " + os.environ["ZETARIS_API_KEY"], "X-Org-ID": os.environ["ZETARIS_ORG_ID"]}


def split_statements(text):
    text = re.sub(r"--[^\n]*", "", text)  # drop line comments (no '--' inside our string literals)
    out, buf, quote = [], [], None
    for ch in text:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
        elif ch in "\"'`":
            quote = ch
            buf.append(ch)
        elif ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
        else:
            buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


def run(sql, limit=200):
    r = requests.post(URL, headers={**HEADERS, "X-Request-ID": str(uuid.uuid4())},
                      json={"queryId": str(uuid.uuid4()), "sql": sql, "source": "SqlEditor", "limit": limit},
                      timeout=300)
    try:
        body = r.json()
    except ValueError:
        body = {"raw": r.text[:300]}
    return r.status_code, body


def main():
    path = sys.argv[1]
    stop = "--stop-on-error" in sys.argv
    stmts = split_statements(open(path, encoding="utf-8").read())
    ok = fail = 0
    for i, sql in enumerate(stmts, 1):
        head = " ".join(sql.split())[:90]
        t0 = time.time()
        code, body = run(sql)
        dt = time.time() - t0
        text = str(body)
        if code == 200 and not body.get("error"):
            ok += 1
            print(f"[{i}/{len(stmts)}] OK   {dt:5.1f}s  {head}")
            cols = body.get("columns") or body.get("schema")
            rows = body.get("data") or []
            if rows and sql.lstrip().upper().startswith(("SELECT", "DESCRIBE", "SHOW")):
                if cols:
                    print("        cols:", [c.get("name", c) if isinstance(c, dict) else c for c in cols])
                for row in rows[:20]:
                    print("       ", row)
        elif "already exist" in text.lower():
            ok += 1
            print(f"[{i}/{len(stmts)}] SKIP {dt:5.1f}s  already exists: {head}")
        else:
            fail += 1
            print(f"[{i}/{len(stmts)}] FAIL {dt:5.1f}s  {head}\n        HTTP {code}: {text[:600]}")
            if stop:
                break
    print(f"\nDone: {ok} ok, {fail} failed")
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    main()
