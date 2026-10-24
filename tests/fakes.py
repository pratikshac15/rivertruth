"""Test doubles: a SQL-aware fake Zetaris, a fake model, and a set of gauges covering every kind RiverTruth meets.

The fake Zetaris behaves like the real one where it matters for correctness:
  * a scouted view can only be queried after its CREATE ... VIEW statement has run;
  * ORDER BY / LIMIT are honoured, so a query that asks for the *oldest* 200 hours gets the oldest hours;
  * NOAA catalog regions and tiles answer by bounding box, and can be told to fail with HTTP 429 a number of times.
Nothing here touches the network, Docker or a model.
"""
import json
import math
import os
import re
import shutil
import tempfile
from datetime import datetime, timedelta, timezone

END = datetime(2026, 10, 23, 22, tzinfo=timezone.utc)   # latest hour in every fake series
GENERIC = ["hr_utc", "usgs_cfs", "usgs_peak_cfs", "usgs_stage_ft", "nwm_cfs", "nws_cfs", "nws_stage_ft", "rain_mm"]
FLAGSHIP_COLS = {"usgs_cfs": "usgs_northport_cfs", "usgs_peak_cfs": "usgs_northport_peak_cfs",
                 "usgs_stage_ft": "usgs_northport_stage_ft", "nws_cfs": "nws_oliver_cfs",
                 "nws_stage_ft": "nws_oliver_tailwater_ft"}


def hours(n, fn):
    """n hourly rows ending at END (oldest first); fn(i, t) returns a dict of GENERIC values for hour i."""
    out = []
    for i in range(n):
        t = END - timedelta(hours=n - 1 - i)
        row = {k: None for k in GENERIC}
        row.update(fn(i, t))
        row["hr_utc"] = t.strftime("%Y-%m-%d %H:00")
        out.append(row)
    return out


def _pulse(t, base=180.0, peak=5400.0, local_hour=21, offset=-5):
    h = (t.hour + offset) % 24
    d = (h - local_hour) % 24
    return base + (peak * math.exp(-d / 1.2) if d <= 5 else 0.0)


class Gauge:
    def __init__(self, lid, name, lat, lon, rows, usgs_id="", reach_id="", tz="CST6CDT", flood=None,
                 catalog_flood="no_flooding", view=None):
        self.lid, self.name, self.lat, self.lon, self.rows = lid, name, lat, lon, rows
        self.usgs_id, self.reach_id, self.tz = usgs_id, reach_id, tz
        self.flood = flood or {"action": 10, "minor": 12, "moderate": 14, "major": 16}
        self.catalog_flood = catalog_flood
        self.view = view or f"water.hc_{lid.lower()}"

    def has_usgs_readings(self):
        return any(r.get("usgs_cfs") is not None or r.get("usgs_stage_ft") is not None for r in self.rows)

    def empty_sources(self):
        """Which agency replies are empty (Zetaris then cannot read their shape)."""
        has = lambda *k: any(r.get(x) is not None for r in self.rows for x in k)  # noqa: E731
        return {name for name, ok in (("usgs", has("usgs_cfs", "usgs_stage_ft")), ("nws", has("nws_cfs", "nws_stage_ft")),
                                      ("nwm", has("nwm_cfs"))) if not ok}

    def latest(self, key):
        return next((r[key] for r in reversed(self.rows) if r[key] is not None), None)


def make_gauges():
    """One gauge of every kind the app must handle."""
    g = {}
    g["TODA1"] = Gauge("TODA1", "Black Warrior River at Oliver Lock and Dam", 33.209, -87.596, view="water.hourly_compare",
                       usgs_id="02465005", reach_id="18229923", rows=hours(168, lambda i, t: {
                           "usgs_cfs": _pulse(t), "usgs_peak_cfs": _pulse(t) * 1.05, "usgs_stage_ft": 123.0,
                           "nwm_cfs": (_pulse(t - timedelta(hours=1)) if i >= 120 else None),
                           "nws_cfs": 1050.0 + (150 if (t.hour - 5) % 24 in (21, 22) else 0), "nws_stage_ft": 95.3,
                           "rain_mm": 0.0}))
    # Full set of sources, a gentle river (all agree)
    g["CKLA1"] = Gauge("CKLA1", "Cahaba River at Centreville", 32.94, -87.14, usgs_id="02424000", reach_id="777",
                       rows=hours(168, lambda i, t: {"usgs_cfs": 450.0 + i, "usgs_peak_cfs": 460.0 + i, "usgs_stage_ft": 3.1,
                                                     "nwm_cfs": (440.0 + i if i >= 120 else None), "nws_cfs": 455.0 + i,
                                                     "nws_stage_ft": 3.1, "rain_mm": 0.0}))
    # USGS linked but reports stage only; NWS has flow; model low (like Bevill Lock and Dam)
    g["AVLA1"] = Gauge("AVLA1", "Tombigbee River at Bevill Lock and Dam", 33.21, -88.29, usgs_id="02444161", reach_id="888",
                       rows=hours(168, lambda i, t: {"usgs_stage_ft": 109.0, "nwm_cfs": (430.0 if i >= 120 else None),
                                                     "nws_cfs": 3000.0 + 100 * (t.hour % 5), "nws_stage_ft": 109.0,
                                                     "rain_mm": 0.0}))
    # No USGS gauge and no model reach; NWS reports flow (spine = NWS)
    g["MMTW2"] = Gauge("MMTW2", "Kanawha River below Marmet Locks", 38.25, -81.57, tz="EST5EDT", catalog_flood="action",
                       rows=hours(168, lambda i, t: {"nws_cfs": 9000.0 + 10 * i, "nws_stage_ft": 1030.0 + 0.02 * i,
                                                     "rain_mm": 0.0}))
    # Tide gauge: water level only, with a month of history (the newest hour must be the one shown)
    g["PMAF1"] = Gauge("PMAF1", "Tide Gauge at Port Manatee (in MHHW)", 27.64, -82.56, tz="EST5EDT",
                       rows=hours(720, lambda i, t: {"nws_stage_ft": round(1.2 * math.sin(i / 2.0), 2) if i < 719 else 0.09,
                                                     "rain_mm": 0.0}))
    # Live Oct 23: the National Water Model publishes -9999 ("no value") every hour; NWS has water level only
    g["SPGS2"] = Gauge("SPGS2", "Missouri River at Springfield", 42.85, -97.9, usgs_id="06466700", reach_id="999",
                       catalog_flood="action", flood={"action": 9, "minor": 11, "moderate": 13, "major": 15},
                       rows=hours(168, lambda i, t: {"nwm_cfs": -9999.0, "nws_stage_ft": 9.46, "rain_mm": 0.0}))
    # Live Oct 23: a seasonal USGS station is listed but returns no readings (empty timeSeries)
    g["EMMC2"] = Gauge("EMMC2", "Roaring Fork River at Emma", 39.37, -107.08, usgs_id="09081600", reach_id="555",
                       tz="MST7MDT", rows=hours(168, lambda i, t: {"nwm_cfs": 300.0, "nws_cfs": 310.0, "nws_stage_ft": 3.2,
                                                                   "rain_mm": 0.0}))
    # Sweep Oct 24 (OMRC1): the NWS observed reply is empty, so Zetaris cannot read its 'data' (d)
    g["OMRC1"] = Gauge("OMRC1", "Sacramento-San Joaquin Delta above Old River", 37.8, -121.5, usgs_id="11313405", reach_id="444",
                       tz="PST8PDT", rows=hours(168, lambda i, t: {"usgs_cfs": 2000.0, "usgs_peak_cfs": 2100.0,
                                                                   "usgs_stage_ft": 4.0, "nwm_cfs": 1900.0, "rain_mm": 0.0}))
    # Sweep Oct 24 (SFPN5): the National Water Model reply for the reach is empty
    g["SFPN5"] = Gauge("SFPN5", "Rio Grande above San Felipe Pueblo", 35.4, -106.4, usgs_id="08319000", reach_id="333",
                       tz="MST7MDT", rows=hours(168, lambda i, t: {"usgs_cfs": 400.0, "usgs_peak_cfs": 410.0, "usgs_stage_ft": 2.0,
                                                                   "nws_cfs": 420.0, "nws_stage_ft": 2.0, "rain_mm": 0.0}))
    # Registered but nothing published this week
    g["DRYX1"] = Gauge("DRYX1", "Empty Creek near Nowhere", 35.0, -100.0, rows=[])
    return g


class FakeZetaris:
    def __init__(self, gauges, catalog_extra=0):
        self.gauges = gauges
        self.created = {"water.hourly_compare"}
        self.calls = []                 # every SQL statement, in order
        self.fail_429 = {}              # substring -> remaining failures
        self.broken = set()             # views that exist but fail when queried (stale definition)
        self.catalog = [[g.lid, g.name, g.lat, g.lon, g.latest("nws_stage_ft") or -999, "ft",
                         (g.latest("nws_cfs") or -999) / 1000 if g.latest("nws_cfs") else -999, "kcfs",
                         g.catalog_flood, END.isoformat()] for g in gauges.values()]
        for k in range(catalog_extra):  # filler gauges spread over the contiguous US
            self.catalog.append([f"FILL{k}", f"Filler gauge {k}", 25 + (k * 7) % 24, -124 + (k * 13) % 57, 2.0, "ft",
                                 0.5, "kcfs", "no_flooding", END.isoformat()])

    # ----------------------------------------------------------------- helpers
    def _by_view(self, view):
        return next((g for g in self.gauges.values() if g.view == view), None)

    @staticmethod
    def _bbox(tag):
        m = re.fullmatch(r"r_([mp][\d_]+)_([mp][\d_]+)_([mp][\d_]+)_([mp][\d_]+)", tag)
        if m:
            v = [(-1 if p[0] == "m" else 1) * float(p[1:].replace("_", ".")) for p in m.groups()]
            return v
        m = re.fullmatch(r"([we])(\d+)_([ns])(\d+)", tag)
        if m:
            x0 = (-1 if m.group(1) == "w" else 1) * int(m.group(2))
            y0 = (-1 if m.group(3) == "s" else 1) * int(m.group(4))
            return [x0, y0, x0 + 2, y0 + 2]
        return None

    def _gauge_for_table(self, text):
        for g in self.gauges.values():
            l = g.lid.lower()
            if (f"iv_{l}_raw" in text or f"{l}_observed_raw" in text or f"rain_{l}_raw" in text
                    or (g.reach_id and f"reach_{g.reach_id}_analysis_raw" in text)):
                return g
        return None

    @staticmethod
    def _fail_on_empty_source(g, sql):
        """Like Zetaris: SQL that reads into an empty agency reply fails to compile."""
        from agent import zetaris
        empty = g.empty_sources()
        for src, marker, alias in (("usgs", "timeSeries", "ts"), ("nws", "_observed_raw", "d"), ("nwm", "analysisAssimilation", "d")):
            if src in empty and marker in sql:
                raise zetaris.SQLError(f"[INVALID_EXTRACT_BASE_FIELD_TYPE] Can't extract a value from \"{alias}\". "
                                       "Need a complex type [STRUCT, ARRAY, MAP] but got \"STRING\".")

    def _maybe_429(self, sql):
        from agent import zetaris
        for key, n in list(self.fail_429.items()):
            if key in sql and n > 0:
                self.fail_429[key] = n - 1
                raise zetaris.SQLError("REST API call failed: Server returned HTTP response code: 429 for URL")

    # ----------------------------------------------------------------- the API
    def run_sql(self, sql, limit=200, purpose="", agent=False):
        """Like the real Zetaris REST API, every value comes back as text (or null)."""
        res = self._run_sql(sql, limit, purpose, agent)
        res["rows"] = [[v if v is None or isinstance(v, str) else (str(v).lower() if isinstance(v, bool) else str(v))
                        for v in r] for r in res["rows"]]
        return res

    def _run_sql(self, sql, limit=200, purpose="", agent=False):
        from agent import zetaris
        sql = sql.strip().rstrip(";")
        self.calls.append(sql)
        up = sql.upper()
        if up.startswith(("CREATE", "CACHE", "UNCACHE", "DROP")):
            self._maybe_429(sql)
            m = re.match(r"CREATE SCHEMASTORE VIEW (\w+) WITH CONTAINER (\w+)", sql)
            if m:
                name = f"{m.group(2)}.{m.group(1)}"
                g = self._by_view(name)
                if g is not None:
                    self._fail_on_empty_source(g, sql)
                if name in self.created:
                    raise zetaris.SQLError(f'A view named "{m.group(1)}" already exists.')
                self.created.add(name)
            m = re.match(r"DROP VIEW (?:IF EXISTS )?(\S+)", sql, re.I)
            if m:
                self.created.discard(m.group(1))
                self.broken.discard(m.group(1))
            return {"columns": [], "rows": []}
        m = re.search(r"FROM noaa_nwps\.(\w+)_gauge_raw LIMIT 1", sql)
        if m:  # Source Scout discovery
            g = self.gauges.get(m.group(1).upper())
            if g is None:
                raise zetaris.SQLError("REST API call failed: 404")
            f = g.flood
            return {"columns": [], "rows": [[g.lid, g.name, g.usgs_id or None, g.reach_id or None, g.lat, g.lon,
                                             f["action"], f["minor"], f["moderate"], f["major"], "", "", g.tz]]}
        m = re.search(r"FROM (usgs_nwis\.iv_|noaa_nwps\.|noaa_nwm\.reach_|open_meteo\.rain_)(\w+?)_(raw|observed_raw|analysis_raw)\b", sql)
        if m and "water." not in sql:  # a probe of one agency's raw table (the Source Scout testing a source)
            g = self._gauge_for_table(m.group(0))
            if g is not None:
                self._fail_on_empty_source(g, sql)
            return {"columns": [], "rows": []}
        m = re.search(r"FROM noaa_nwps\.gauges_(\w+) LATERAL VIEW explode\(gauges\)", sql)
        if m:  # NOAA catalog by region / tile
            self._maybe_429(sql)
            x0, y0, x1, y1 = self._bbox(m.group(1))
            return {"columns": [], "rows": [r for r in self.catalog if y0 <= r[2] < y1 and x0 <= r[3] < x1]}
        m = re.search(r"SELECT\s+((?:(?!SELECT).)+?)\s+FROM\s+(water\.\w+)", sql, re.S)  # innermost select
        if m:
            view = m.group(2)
            if view not in self.created:
                raise zetaris.SQLError(f"Table or view not found: {view}")
            if view in self.broken:
                raise zetaris.SQLError(f"cannot resolve column in {view}")
            g = self._by_view(view)
            rows = list(g.rows) if g else []
            d = re.search(r"ORDER BY hr_utc DESC LIMIT (\d+)", sql, re.I)
            a = re.search(r"ORDER BY hr_utc LIMIT (\d+)", sql, re.I)
            if d:
                rows = rows[-int(d.group(1)):]
            elif a:
                rows = rows[:int(a.group(1))]
            items = [x.strip() for x in m.group(1).split(",")]
            alias = {v: k for k, v in FLAGSHIP_COLS.items()}
            out = []
            for r in rows:
                vals = []
                for it in items:
                    expr = re.split(r"\s+AS\s+", it, flags=re.I)[0].strip()
                    if expr.upper() == "NULL":
                        vals.append(None)
                        continue
                    key = alias.get(expr, expr)
                    vals.append(r.get(key))
                out.append(vals)
            return {"columns": [], "rows": out[:max(limit, 1)] if not d else out}
        if not re.match(r"(?i)\s*(SELECT|WITH)\b", sql):
            raise zetaris.SQLError(f"fake Zetaris: unsupported statement: {sql[:60]}")
        views = re.findall(r"(?i)\bFROM\s+(water\.\w+)", sql)
        if not views or any(v not in self.created for v in views):
            raise zetaris.SQLError(f"Table or view not found: {views or sql[:60]}")
        return {"columns": ["hr_utc", "value"], "rows": [["2026-10-23 02:00", 5640.0]]}  # free-form agent SQL


class FakeLLM:
    """Canned, well-formed replies per agent role; records every call."""

    def __init__(self):
        self.calls = []

    def chat(self, role, messages, **kw):
        self.calls.append(role)
        r = role.split(":")[0]
        if r == "planner":
            return json.dumps({"restated_question": "q", "hypotheses": [{"id": "H1", "claim": "A scheduled release"},
                                                                       {"id": "H2", "claim": "Rain"}],
                               "checks": [{"id": "C1", "hypothesis": "H1", "purpose": "recurrence", "how": "view"},
                                          {"id": "C2", "hypothesis": "H2", "purpose": "rain total", "how": "view"}]})
        if r == "investigator.sql":
            text = " ".join(m["content"] for m in messages)
            view = (re.search(r"START HERE: (water\.\w+)", text) or re.search(r"(water\.hourly_compare)", text)).group(1)
            return json.dumps({"sql": f"SELECT hr_utc FROM {view} LIMIT 5", "purpose": "x"})
        if r == "investigator.interpret":
            return json.dumps({"finding": "Peaks recur at the same hour with no rain.", "stance": "supports"})
        if r.startswith("critic"):
            return json.dumps({"verdict": "approve", "hypothesis_status": {"H1": "supported", "H2": "refuted"},
                               "issues": ["ok"], "new_checks": []})
        if r == "reporter":
            return ("## For the public\nWater was released from a dam.\n\n## Answer\nA release.\n\n"
                    "## Why the sources disagree\n- Timing [E1].\n\n## Confidence\nHigh.")
        if r == "fact_checker":
            return json.dumps({"corrections": []})
        if r == "river_check":
            return json.dumps({"headline": "Check the official gauge before you go.", "trust": "Use the measured gauge here.",
                               "pattern": "The river has followed its usual pattern.", "plan": "Plan around the evening."})
        raise AssertionError(f"unexpected model role {role}")


class Env:
    """Installs the fakes and gives every test a fresh World Model and fresh module state."""

    def __init__(self, gauges=None, catalog_extra=0):
        self.gauges = gauges or make_gauges()
        self.zet = FakeZetaris(self.gauges, catalog_extra)
        self.llm = FakeLLM()

    def __enter__(self):
        from agent import config, gauges, llm, memory, radar, scout, zetaris
        self.dir = tempfile.mkdtemp(prefix="rt-test-")
        os.environ["WORLD_DIR"] = os.path.join(self.dir, "world")
        self._saved = (zetaris.run_sql, llm.chat, config.RECORD_SAMPLES, config.LOG_DIR, config.SAMPLE_MODE,
                       gauges._SAVE, radar._SAVED)
        zetaris.run_sql, llm.chat = self.zet.run_sql, self.llm.chat
        config.RECORD_SAMPLES, config.SAMPLE_MODE = False, False
        config.LOG_DIR = os.path.join(self.dir, "logs")
        memory._world = None
        scout._SITES.clear()
        gauges._registered.clear()
        gauges._rows.clear()
        gauges.ALL.update(status="idle", at=None, gauges=[], error=None, tried=0, done=0)
        gauges._SAVE = os.path.join(self.dir, "gauges_us.json")
        gauges.PAUSE = 0
        gauges.BACKOFF = 0
        gauges.COOLDOWN = 0
        gauges._loaded_here = False
        from agent import log
        log._path = None
        radar.STATE.update(status="idle", rows=[], done=0, total=0, error=None, at=None)
        radar._SAVED = os.path.join(self.dir, "radar_last.json")
        return self

    def __exit__(self, *exc):
        from agent import config, gauges, llm, memory, radar, zetaris
        (zetaris.run_sql, llm.chat, config.RECORD_SAMPLES, config.LOG_DIR, config.SAMPLE_MODE,
         gauges._SAVE, radar._SAVED) = self._saved
        memory._world = None
        shutil.rmtree(self.dir, ignore_errors=True)
        return False
