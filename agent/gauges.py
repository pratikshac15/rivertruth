"""River gauges near a place, anywhere in the US, found with SQL in Zetaris.

NOAA's NWPS publishes every NWS river gauge with its current stage, flow and flood category. RiverTruth registers
that catalog in Zetaris in 2-degree map tiles (one Lightning REST table per tile, created the first time someone looks
at that area) and answers "which gauges are in this map view?" with a SQL query over the tiles.
"""
import json
import math
import os
import threading
import time

from . import config, log, zetaris

TILE = 2.0            # degrees per tile side
MAX_TILES = 12        # a wider view must be zoomed in first
CACHE_SECONDS = 600   # current conditions in the catalog are refreshed at most every 10 minutes
UA = f'user-agent "{config.CONTACT}"'

_registered = set()
_rows = {}            # tile -> (fetched_at, rows)


def _tag(x0, y0):
    return f"{'w' if x0 < 0 else 'e'}{abs(int(x0)):03d}_{'s' if y0 < 0 else 'n'}{abs(int(y0)):02d}"


def table_for(x0, y0):
    return f"noaa_nwps.gauges_{_tag(x0, y0)}"


def tiles_for(xmin, ymin, xmax, ymax):
    xs = range(math.floor(xmin / TILE), math.floor(xmax / TILE) + 1)
    ys = range(math.floor(ymin / TILE), math.floor(ymax / TILE) + 1)
    return [(x * TILE, y * TILE) for x in xs for y in ys]


def _ensure_tile(x0, y0):
    key = (x0, y0)
    if key in _registered or config.SAMPLE_MODE:
        return
    url = (f"https://api.water.noaa.gov/nwps/v1/gauges?bbox.xmin={x0:g}&bbox.ymin={y0:g}"
           f"&bbox.xmax={x0 + TILE:g}&bbox.ymax={y0 + TILE:g}&srid=EPSG_4326")
    name = table_for(x0, y0).split(".")[1]
    for sql in (f'CREATE LIGHTNING DATABASE NOAA_NWPS DESCRIBE BY "NOAA NWPS gauges, NWS observed and forecast"',
                f'CREATE LIGHTNING REST TABLE {name} FROM NOAA_NWPS REQUEST(\n endpoint "{url}",\n method "get",\n'
                f' response_type "json",\n http_encoding "URLENCODED"\n) HEADER (\n {UA}\n) BODY ()'):
        try:
            zetaris.run_sql(sql, purpose="map: register gauge catalog tile")
        except zetaris.SQLError as e:
            if "already exist" not in str(e).lower():
                raise
    try:
        zetaris.run_sql(f"CACHE TABLE {table_for(x0, y0)}", purpose="map: cache tile")
    except zetaris.SQLError as e:
        if "429" in str(e) or "REST API call failed" in str(e):
            raise
    _registered.add(key)


def _tile_rows(x0, y0, refresh=False):
    key = (x0, y0)
    hit = _rows.get(key)
    if hit and not refresh and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    _ensure_tile(x0, y0)
    t = table_for(x0, y0)
    if refresh and not config.SAMPLE_MODE:
        for sql in (f"UNCACHE TABLE IF EXISTS {t}", f"CACHE TABLE {t}"):
            try:
                zetaris.run_sql(sql, purpose="map: refresh tile")
            except zetaris.SQLError:
                pass
    sql = (f"SELECT g.lid, g.name, g.latitude, g.longitude, g.status.observed.`primary`, g.status.observed.`primaryUnit`, "
           f"g.status.observed.`secondary`, g.status.observed.`secondaryUnit`, g.status.observed.`floodCategory`, "
           f"g.status.observed.`validTime` FROM {t} LATERAL VIEW explode(gauges) AS g LIMIT 1000")
    try:
        try:
            rows = zetaris.run_sql(sql, limit=1000, purpose="map: gauges in tile")["rows"]
        except zetaris.SQLError as e:
            if "429" not in str(e):
                raise
            time.sleep(BACKOFF / 5)  # NOAA rate limit: wait and try once more
            rows = zetaris.run_sql(sql, limit=1000, purpose="map: gauges in tile (retry)")["rows"]
    except zetaris.SQLError as e:  # e.g. a tile over the sea has no gauges (no schema), or NOAA is rate-limiting
        log.event("map_tile_empty", tile=_tag(x0, y0), error=str(e)[:200])
        if "429" in str(e) or "REST API call failed" in str(e):
            raise  # do not remember a failure as "no gauges here"
        rows = []
    _rows[key] = (time.time(), rows)
    return rows


def _num(v):
    try:
        v = float(v)
        return None if v <= -999 else v
    except (TypeError, ValueError):
        return None


def nearby(xmin, ymin, xmax, ymax):
    """Gauges inside the box: [{lid, name, lat, lon, stage_ft, flow_cfs, flood, valid}]."""
    tiles = tiles_for(xmin, ymin, xmax, ymax)
    if len(tiles) > MAX_TILES:
        return {"zoom_in": True, "gauges": []}
    out, limited = {}, False
    for x0, y0 in tiles:
        try:
            tile = _tile_rows(x0, y0)
        except zetaris.SQLError:
            limited = True
            continue
        for r in tile:
            lid, name, lat, lon, prim, punit, sec, sunit, flood, valid = (list(r) + [None] * 10)[:10]
            try:
                lat, lon = float(lat), float(lon)
            except (TypeError, ValueError):
                continue
            if not (ymin <= lat <= ymax and xmin <= lon <= xmax):
                continue
            flow = _num(sec)
            if flow is not None and str(sunit).lower() == "kcfs":
                flow *= 1000
            elif str(sunit).lower() != "cfs":
                flow = None
            out[lid] = {"lid": lid, "name": name, "lat": lat, "lon": lon,
                        "stage_ft": _num(prim) if str(punit).lower() == "ft" else None,
                        "flow_cfs": flow, "flood": flood or "not_defined", "valid": valid}
    return {"zoom_in": False, "gauges": list(out.values()),
            "note": "NOAA is limiting requests right now; some areas may be missing. Try again in a minute." if limited else ""}


# ------------------------------------------------------------------ the whole US at once

# US regions (lon_min, lat_min, lon_max, lat_max): contiguous US in 18 boxes, plus Alaska, Hawaii, Puerto Rico
REGIONS = ([(x, y, x + 10, y2) for x in range(-125, -65, 10) for y, y2 in ((24, 33), (33, 42), (42, 50))] +
           [(-170, 51, -129, 72), (-161, 18, -154, 23), (-68, 17, -64, 19)])
PAUSE = 1.5            # seconds between NOAA requests
BACKOFF = 12           # seconds to wait after NOAA says 429 (doubles on the second try)
REFRESH_SECONDS = 1800
COOLDOWN = 60          # seconds to wait before a second pass over the regions NOAA refused
ALL = {"status": "idle", "at": None, "gauges": [], "error": None, "tried": 0, "done": 0, "total": len(REGIONS)}
RETRY_SECONDS = 180   # after a failure, wait before asking NOAA again (it rate-limits)
_SAVE = os.path.join(os.environ.get("WORLD_DIR", "data/world"), "..", "gauges_us.json")
_all_lock = threading.Lock()
_loaded_here = False   # has this process loaded the map yet? (the first load uses Zetaris's cache)


def _compact(r):
    lid, name, lat, lon, prim, punit, sec, sunit, flood, _valid = (list(r) + [None] * 10)[:10]
    try:
        lat, lon = round(float(lat), 4), round(float(lon), 4)
    except (TypeError, ValueError):
        return None
    flow = _num(sec)
    flow = flow * 1000 if flow is not None and str(sunit).lower() == "kcfs" else flow if str(sunit).lower() == "cfs" else None
    stage = _num(prim) if str(punit).lower() == "ft" else None
    return [lid, name, lat, lon, flood or "not_defined", stage, None if flow is None else round(flow)]


def _region_tag(b):
    x0, y0, x1, y1 = b
    f = lambda v: f"{'m' if v < 0 else 'p'}{abs(v):g}".replace(".", "_")  # noqa: E731
    return f"{f(x0)}_{f(y0)}_{f(x1)}_{f(y1)}"


def _limited(e):
    return "429" in str(e)


def _region(b, refresh, depth=0):
    """Gauges in one region via its own Zetaris REST table (created and cached once); split it if NOAA refuses."""
    x0, y0, x1, y1 = b
    table = f"noaa_nwps.gauges_r_{_region_tag(b)}"
    url = (f"https://api.water.noaa.gov/nwps/v1/gauges?bbox.xmin={x0:g}&bbox.ymin={y0:g}&bbox.xmax={x1:g}"
           f"&bbox.ymax={y1:g}&srid=EPSG_4326")
    steps = [] if config.SAMPLE_MODE else [
        (f'CREATE LIGHTNING REST TABLE {table.split(".")[1]} FROM NOAA_NWPS REQUEST(\n endpoint "{url}",\n method "get",\n'
         f' response_type "json",\n http_encoding "URLENCODED"\n) HEADER (\n {UA}\n) BODY ()', "already exist"),
    ] + ([(f"UNCACHE TABLE IF EXISTS {table}", "")] if refresh else []) + [(f"CACHE TABLE {table}", "already")]
    sql = (f"SELECT g.lid, g.name, g.latitude, g.longitude, g.status.observed.`primary`, g.status.observed.`primaryUnit`, "
           f"g.status.observed.`secondary`, g.status.observed.`secondaryUnit`, g.status.observed.`floodCategory`, "
           f"g.status.observed.`validTime` FROM {table} LATERAL VIEW explode(gauges) AS g LIMIT 5000")
    for attempt in range(2):
        try:
            for st, ok in steps:
                try:
                    zetaris.run_sql(st, purpose="map: register US region")
                except zetaris.SQLError as e:
                    if not (ok and ok in str(e).lower()):
                        raise
                time.sleep(PAUSE if "CACHE TABLE" in st or "CREATE" in st else 0)
            rows = zetaris.run_sql(sql, limit=5000, purpose=f"map: gauges in region {_region_tag(b)}")["rows"]
            return [c for c in (_compact(r) for r in rows) if c]
        except zetaris.SQLError as e:
            if not _limited(e):
                if "REST API call failed" in str(e) or "cannot resolve" in str(e).lower():
                    return []  # an empty region (no gauges, so no schema)
                raise
            time.sleep(BACKOFF * (attempt + 1))  # NOAA rate limit: back off, then retry
    if depth >= 2:
        raise zetaris.SQLError(f"NOAA kept refusing region {_region_tag(b)} (429)")
    mx, my = (x0 + x1) / 2, (y0 + y1) / 2
    out = []
    for q in ((x0, y0, mx, my), (mx, y0, x1, my), (x0, my, mx, y1), (mx, my, x1, y1)):
        out += _region(q, refresh, depth + 1)
    return out


def load_all(refresh=False, progress=None):
    """Every NWS river gauge in the US: one Zetaris table per region over NOAA's catalog, read with SQL."""
    if not config.SAMPLE_MODE:
        try:
            zetaris.run_sql('CREATE LIGHTNING DATABASE NOAA_NWPS DESCRIBE BY "NOAA NWPS gauges, NWS observed and forecast"',
                            purpose="map: database")
        except zetaris.SQLError:
            pass
    seen, failed = {}, []
    for i, b in enumerate(REGIONS):
        try:
            for c in _region(b, refresh):
                seen[c[0]] = c
        except zetaris.SQLError:
            failed.append(b)
        if progress:
            progress(i + 1, list(seen.values()))
    errors = []
    if failed:  # NOAA refused (429): once it starts, every later region fails within seconds. Let it cool down.
        time.sleep(COOLDOWN)
        for b in failed:
            try:
                for c in _region(b, refresh):
                    seen[c[0]] = c
            except zetaris.SQLError as e:
                errors.append(str(e)[:160])
            if progress:
                progress(len(REGIONS), list(seen.values()))
    if not seen:
        raise zetaris.SQLError("; ".join(errors) or "no gauges returned")
    return list(seen.values()), errors


def _progress(done, gauges):
    ALL["done"] = done
    if ALL["status"] == "loading" or len(gauges) > len(ALL["gauges"]):
        ALL["gauges"] = gauges


def _merge_saved(loaded):
    """The saved list with what was just loaded on top: a partial load must not shrink the map."""
    try:
        with open(_SAVE) as f:
            saved = json.load(f).get("gauges") or []
    except (OSError, ValueError):
        saved = []
    merged = {x[0]: x for x in saved}
    merged.update({x[0]: x for x in loaded})
    return list(merged.values())


def _refresh(refresh):
    try:
        ALL["done"] = 0
        g, errors = load_all(refresh, _progress)
        if errors:  # some regions failed (NOAA rate limits): keep their gauges from the saved list
            g = _merge_saved(g)
        ALL.update(status="ready", gauges=g, at=time.time(), error="; ".join(errors)[:300] or None)
        global _loaded_here
        _loaded_here = True
        try:
            os.makedirs(os.path.dirname(os.path.abspath(_SAVE)), exist_ok=True)
            with open(_SAVE, "w") as f:
                json.dump({"at": ALL["at"], "gauges": g}, f)
        except OSError:
            pass
        log.event("map_all_loaded", gauges=len(g))
    except Exception as e:  # noqa: BLE001  the map falls back to loading by area
        if not ALL["gauges"]:
            ALL.update(status="failed")
        ALL["error"] = str(e)[:300]
        log.event("map_all_error", error=str(e)[:300])
    finally:
        _all_lock.release()


def all_gauges():
    """Current state of the whole-US layer; starts a background (re)load when missing or older than 15 minutes."""
    stale = ALL["at"] is None or time.time() - ALL["at"] > REFRESH_SECONDS
    if stale and time.time() - ALL["tried"] < RETRY_SECONDS:
        stale = False
    if stale and _all_lock.acquire(blocking=False):
        ALL["tried"] = time.time()
        if not ALL["gauges"]:
            ALL["status"] = "loading"
        # The first load after the page starts reads Zetaris's cached copy (no NOAA requests, so a restart cannot get
        # the machine rate-limited); refreshes while the page keeps running ask NOAA for current flood status.
        threading.Thread(target=_refresh, args=(_loaded_here,), daemon=True).start()
    return ALL


def _load_saved():
    try:
        with open(_SAVE) as f:
            saved = json.load(f)
        if saved.get("gauges"):
            ALL.update(status="ready", gauges=saved["gauges"], at=saved.get("at"))
    except (OSError, ValueError):
        pass


_load_saved()
