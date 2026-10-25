"""Where do the sources disagree right now? Rank the watch-list gauges by how far apart their sources are.

For each gauge (registered by the Source Scout if needed), read the last 48 hours from its Zetaris view and compute,
per hour, the ratio between the highest and lowest flow reported by USGS, the National Water Model and the NWS.
All numbers come from SQL on Zetaris; the ranking itself is arithmetic, done in code.
"""
import json
import os
import statistics
import threading
import time

from . import log, scout, sites, snapshot, zetaris
from .sites import WATCHLIST

FLOOR_CFS = 20.0   # tiny flows make ratios meaningless; floor the denominator
STATE = {"status": "idle", "rows": [], "done": 0, "total": 0, "error": None, "at": None}
_LOCK = threading.Lock()
_SAVED = os.path.join(os.environ.get("WORLD_DIR", "data/world"), "..", "radar_last.json")


def _load():
    """Show the last completed scan right away (marked with its time) until a new scan runs."""
    try:
        with open(_SAVED) as f:
            saved = json.load(f)
        if saved.get("rows"):
            STATE.update(status="done", rows=saved["rows"], done=len(saved["rows"]), total=len(saved["rows"]),
                         at=saved.get("at"))
    except (OSError, ValueError):
        pass


def _save():
    try:
        os.makedirs(os.path.dirname(os.path.abspath(_SAVED)), exist_ok=True)
        with open(_SAVED, "w") as f:
            json.dump({"rows": STATE["rows"], "at": STATE["at"]}, f)
    except OSError:
        pass


def _f(v):
    return None if v in (None, "", "null") else float(v)


def score(site):
    res = zetaris.run_sql(snapshot.rows_sql(site), purpose=f"radar: rows for {site.lid}")
    rows = [[r[0]] + [_f(x) for x in r[1:]] for r in sites.no_missing(res["rows"])]
    rows = rows[-48:]
    ratios = []
    for hr, usgs, _peak, _stage, nwm, nws in (r[:6] for r in rows):
        vals = [v for v in (usgs, nwm, nws) if v is not None and v >= 0]
        if len(vals) >= 2:
            ratios.append((max(vals) / max(min(vals), FLOOR_CFS), hr, usgs, nwm, nws))
    if not ratios:
        return {"lid": site.lid, "name": site.name, "ok": False, "note": "fewer than two sources reporting"}
    latest = ratios[-1]
    worst = max(ratios, key=lambda x: x[0])
    regulated = site.flagship or any(w in site.name.lower() for w in ("dam", "lock", "reservoir"))
    return {"lid": site.lid, "name": site.name, "ok": True, "regulated": regulated,
            "typical": round(statistics.median(r[0] for r in ratios), 2),
            "now": round(latest[0], 2), "now_hour": latest[1], "now_values": {"usgs": latest[2], "nwm": latest[3], "nws": latest[4]},
            "worst": round(worst[0], 1), "worst_hour": worst[1], "hours": len(ratios)}


def scan(lids=None):
    lids = lids or WATCHLIST
    with _LOCK:
        if STATE["status"] == "scanning":
            return
        STATE.update(status="scanning", rows=[], done=0, total=len(lids), error=None)
    rows = []
    for lid in lids:
        try:
            rows.append(score(scout.ensure_site(lid)))
        except Exception as e:  # noqa: BLE001  one bad gauge must not stop the scan
            rows.append({"lid": lid, "name": lid, "ok": False, "note": str(e)[:160]})
            log.event("radar_error", lid=lid, error=str(e)[:300])
        STATE["done"] += 1
        STATE["rows"] = sorted(rows, key=lambda r: (not r.get("ok"), -(r.get("typical") or 0)))
    STATE.update(status="done", at=time.time())
    _save()
    log.event("radar", rows=STATE["rows"])


_load()


def scan_async(lids=None):
    threading.Thread(target=scan, args=(lids,), daemon=True).start()


if __name__ == "__main__":
    scan()
    for r in STATE["rows"]:
        if r.get("ok"):
            print(f"{r['lid']:6} typical {r['typical']:>6}x  now {r['now']:>6}x  worst {r['worst']:>6}x @ {r['worst_hour']}  {r['name']}")
        else:
            print(f"{r['lid']:6} --  {r.get('note')}")
