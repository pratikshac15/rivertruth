"""Check a sample of gauges across the US through the real web server, and write down every problem found.

Each gauge goes through what a person's click does: the Source Scout registers it in Zetaris, the River check runs,
and the chart data is loaded. Words are written by code (the model is switched off) unless --words is given, so a
sweep costs no model calls and tests exactly the code where the bugs have been.

Problems recorded per gauge:
  errors    the page would show an error (an exception or an HTTP error)
  warnings  it works but something is wrong: -999/nan/None in the text, a number sent as text, stale data,
            a flood stage with no flood banner, or the map and the River check disagreeing about flow
  notes     expected limits: no data published this week

Usage (from the rivertruth folder; the gauge list comes from data/gauges_us.json, built by the map):
  .venv/bin/python3 scripts/sweep_gauges.py                    # ~190 gauges across the US, about 1 hour
  .venv/bin/python3 scripts/sweep_gauges.py --resume           # continue the last sweep after a stop
  .venv/bin/python3 scripts/sweep_gauges.py --lids ETAG1,SPGS2 # named gauges only
Results: logs/sweep/<time>/results.jsonl (one line per gauge) and summary.md (problems grouped by kind).
"""
import argparse
import json
import os
import random
import re
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

CELL = 6.0                      # degrees: the sample takes gauges from every CELL x CELL square that has any
PER_STATUS = 6                  # and up to this many gauges of each NWS status (out_of_service, action, ...)
ALWAYS = ("moderate", "major")  # every gauge in a serious flood is checked
FLOODING = ("action", "minor", "moderate", "major")
STALE_HOURS = 48
BAD_TEXT = re.compile(r"-9,?999|-999\b|\bnan\b|\bNone\b|\bnull\b|undefined|\binf\b|\{|\}", re.I)


def select(gauges, per_cell=1, seed=7, lids=None, limit=None):
    """A repeatable sample (about 190 of the ~13,000 gauges): every gauge in a moderate or major flood, up to
    PER_STATUS gauges of every other NWS status, and per_cell gauges of each kind (reports flow / level only) from
    every CELL x CELL square. Or exactly the named lids."""
    if lids:
        want = [x.strip().upper() for x in lids if x.strip()]
        by = {g[0]: g for g in gauges}
        return [by[x] for x in want if x in by]
    rnd = random.Random(seed)
    picked, seen = [], set()

    def add(g):
        if g[0] not in seen:
            seen.add(g[0])
            picked.append(g)
    by_status = {}
    for g in gauges:
        by_status.setdefault(g[4] or "", []).append(g)
    for st in sorted(by_status):
        gs = by_status[st]
        for g in (gs if st in ALWAYS else rnd.sample(gs, min(PER_STATUS, len(gs)))):
            add(g)
    groups = {}
    for g in gauges:
        if g[2] is None or g[3] is None:
            continue
        kind = "flow" if g[6] not in (None, "", -999) else "level"
        groups.setdefault((int(g[2] // CELL), int(g[3] // CELL), kind), []).append(g)
    for key in sorted(groups):
        for g in rnd.sample(groups[key], min(per_cell, len(groups[key]))):
            add(g)
    return picked[:limit] if limit else picked


def text_problems(check):
    """Text a person would read that contains a no-value code, nan, None or leftover JSON."""
    out = []
    card = check.get("card") or {}
    texts = [card.get(k) or "" for k in ("headline", "trust", "pattern", "now", "plan")]
    texts += [check.get("flood_alert") or "", check.get("caution") or ""]
    for t in texts:
        m = BAD_TEXT.search(t)
        if m:
            out.append(f"text shows '{m.group(0)}': {t[:120]}")
    return out


class _Server:
    def __enter__(self):
        from http.server import ThreadingHTTPServer
        from web import server
        server.STATE.update(events=[], running=False, question=None, error=None)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_address[1]
        self.token = server.TOKEN
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        return False

    def call(self, method, path, body=None):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=600)
        headers = {"Host": f"127.0.0.1:{self.port}"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers.update({"Content-Type": "application/json", "X-RiverTruth-Token": self.token})
        c.request(method, path, body=data, headers=headers)
        r = c.getresponse()
        raw = r.read()
        c.close()
        try:
            return r.status, json.loads(raw)
        except ValueError:
            return r.status, {"error": raw[:300].decode(errors="replace")}


def check_one(srv, g, now):
    lid, name, flood, cat_flow = g[0], g[1], (g[4] or ""), g[6]
    rec = {"lid": lid, "name": name, "flood": flood, "errors": [], "warnings": [], "notes": [], "flood_alert": None}
    t0 = time.time()
    try:
        code, d = srv.call("POST", "/api/check", {"site": lid, "when": "this evening", "activity": "kayaking or canoeing",
                                                  "flood": flood})
        if code != 200:
            rec["errors"].append(f"River check HTTP {code}: {str(d.get('error', d))[:300]}")
        else:
            f = d.get("facts") or {}
            rec["mode"] = f.get("mode") or ("flow" if f.get("ok") else "none")
            rec["trusted"] = f.get("trusted")
            rec["flood_alert"] = d.get("flood_alert")
            rec["headline"] = (d.get("card") or {}).get("headline", "")[:160]
            if not f.get("ok"):
                rec["notes"].append(f"no data: {f.get('why', '')[:160]}")
            rec["warnings"] += text_problems(d)
            if flood in FLOODING and not d.get("flood_alert") and f.get("ok"):
                rec["warnings"].append(f"NWS lists {flood} stage but the page shows no flood banner")
            if f.get("ok") and cat_flow not in (None, "", -999) and rec["mode"] == "stage":
                rec["warnings"].append(f"the map shows flow ({cat_flow} cfs) but the River check found level only")
        code, h = srv.call("GET", f"/api/hydrograph?site={lid}")
        if code != 200:
            rec["errors"].append(f"chart HTTP {code}: {str(h.get('error', h))[:300]}")
        else:
            rows = h.get("rows") or []
            rec["chart_rows"] = len(rows)
            texty = sorted({type(v).__name__ for r in rows for v in r[1:] if v is not None and not isinstance(v, (int, float))})
            if texty:
                rec["warnings"].append(f"chart values sent as {', '.join(texty)} instead of numbers")
            if rows:
                last = datetime.strptime(rows[-1][0], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                age = (now - last).total_seconds() / 3600
                rec["latest_hour"] = rows[-1][0]
                if age > STALE_HOURS:
                    rec["warnings"].append(f"stale: newest data is {age / 24:.1f} days old ({rows[-1][0]} UTC)")
            if rec.get("mode") == "flow" and not h.get("has_flow"):
                rec["warnings"].append("River check uses flow but the chart has no flow")
    except Exception as e:  # noqa: BLE001  a sweep never stops on one gauge
        rec["errors"].append(f"{type(e).__name__}: {e}"[:300])
        rec["traceback"] = traceback.format_exc()[-1500:]
    rec["seconds"] = round(time.time() - t0, 1)
    return rec


def _kind(msg):
    """Group similar messages: numbers, gauge codes and quoted values are blanked out."""
    m = re.sub(r"\b[A-Z]{3,5}\d\b", "<gauge>", msg)
    m = re.sub(r"'[^']{0,40}'", "'…'", m) if "text shows" in m else m
    m = re.sub(r"-?\d[\d,.]*", "#", m)
    return m[:160]


def summarize(results):
    lines = [f"# Gauge sweep: {len(results)} gauges", ""]
    for label in ("errors", "warnings", "notes"):
        groups = {}
        for r in results:
            for msg in r[label]:
                groups.setdefault(_kind(msg), []).append((r["lid"], msg))
        n = sum(1 for r in results if r[label])
        lines.append(f"## {label.capitalize()}: {n} gauge{'s' if n != 1 else ''}")
        if not groups:
            lines.append("None.")
        for kind, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            lids = ", ".join(sorted({x for x, _ in items})[:15]) + (" …" if len({x for x, _ in items}) > 15 else "")
            lines.append(f"- **{len(items)}×** {kind}\n  - gauges: {lids}\n  - example: {items[0][1][:300]}")
        lines.append("")
    modes = {}
    for r in results:
        modes[r.get("mode", "error")] = modes.get(r.get("mode", "error"), 0) + 1
    lines.append("## What the River check found\n" + ", ".join(f"{k}: {v}" for k, v in sorted(modes.items())))
    secs = [r["seconds"] for r in results if "seconds" in r]
    if secs:
        lines.append(f"\nTime per gauge: median {sorted(secs)[len(secs) // 2]} s, slowest {max(secs)} s.")
    return "\n".join(lines) + "\n"


def run(gauges, out_dir, pause=1.5, now=None, resume=False, words=False, progress=lambda m: print(m, flush=True)):
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "results.jsonl")
    done = {}
    if resume and os.path.exists(path):
        with open(path) as f:
            for line in f:
                r = json.loads(line)
                done[r["lid"]] = r
    from agent import llm
    real_chat = llm.chat
    if not words:
        def no_model(*a, **k):
            raise RuntimeError("sweep: words are written by code")
        llm.chat = no_model
    results = []
    try:
        with _Server() as srv, open(path, "a") as out:
            for i, g in enumerate(gauges):
                if g[0] in done:
                    results.append(done[g[0]])
                    continue
                rec = check_one(srv, g, now or datetime.now(timezone.utc))
                out.write(json.dumps(rec) + "\n")
                out.flush()
                results.append(rec)
                flag = "ERROR" if rec["errors"] else "warn " if rec["warnings"] else "ok   "
                progress(f"[{i + 1}/{len(gauges)}] {flag} {g[0]:6} {rec['seconds']:5}s  {g[1][:50]}"
                         + (f"  -> {(rec['errors'] or rec['warnings'])[0][:90]}" if rec["errors"] or rec["warnings"] else ""))
                if pause:
                    time.sleep(pause)
    finally:
        llm.chat = real_chat
    summary = summarize(results)
    with open(os.path.join(out_dir, "summary.md"), "w") as f:
        f.write(summary)
    return results, summary


def gauge_list(saved=os.path.join(ROOT, "data", "gauges_us.json"), say=print):
    """The gauge list the map saved (instant); only when there is none, read all regions from Zetaris (minutes)."""
    try:
        with open(saved) as f:
            gauges = json.load(f)["gauges"]
        if gauges:
            return gauges, f"the saved map list ({os.path.basename(saved)})"
    except (OSError, ValueError, KeyError):
        pass
    say("No saved gauge list: reading all US regions from Zetaris (a few minutes)...")
    from agent import gauges as G
    return G.load_all(progress=lambda done, g: say(f"  region {done}/{len(G.REGIONS)}: {len(g)} gauges"))[0], "Zetaris"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lids", help="comma-separated gauge codes to check instead of a sample")
    ap.add_argument("--per-cell", type=int, default=1)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--pause", type=float, default=1.5, help="seconds between gauges (NOAA rate limits)")
    ap.add_argument("--resume", action="store_true", help="continue the most recent sweep")
    ap.add_argument("--words", action="store_true", help="let the model write the River check words (uses model calls)")
    a = ap.parse_args()
    base = os.path.join(ROOT, "logs", "sweep")
    if a.resume and os.path.isdir(base) and os.listdir(base):
        out_dir = os.path.join(base, sorted(os.listdir(base))[-1])
    else:
        out_dir = os.path.join(base, datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    print("Loading the gauge list...", flush=True)
    us, where = gauge_list(say=lambda m: print(m, flush=True))
    print(f"{len(us)} gauges from {where}", flush=True)
    if not us:
        sys.exit("No gauge list: open the map once (python3 -m web.server) so data/gauges_us.json is built.")
    picked = select(us, per_cell=a.per_cell, lids=(a.lids or "").split(",") if a.lids else None, limit=a.limit)
    print(f"Sweep of {len(picked)} of {len(us)} gauges -> {os.path.relpath(out_dir, ROOT)} (one line per gauge)", flush=True)
    _, summary = run(picked, out_dir, pause=a.pause, resume=a.resume, words=a.words)
    print("\n" + summary)
    print(f"Saved: {os.path.relpath(os.path.join(out_dir, 'summary.md'), ROOT)}")


if __name__ == "__main__":
    main()
