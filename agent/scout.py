"""Source Scout: discover and register the four data sources for any NWS river gauge, in Zetaris.

Given an NWS gauge code (e.g. CKLA1), the Scout
  1. registers the gauge's NOAA NWPS record as a Zetaris REST table and reads, with SQL, which USGS site,
     National Water Model reach and coordinates belong to it (the discovery step);
  2. registers the USGS, NWS, NWM and rainfall APIs for that place as Zetaris REST tables, caches them, and builds one
     hourly view in common units (water.hc_<lid>);
  3. records the gauge, its sources and its upstream/downstream neighbours in the World Model.
Re-running is safe: existing tables are reused. In SAMPLE_MODE nothing is created; the recorded discovery replays.
"""
import re

from . import config, log, zetaris
from .sites import FLAGSHIP, GENERIC, Site, clean_flood, iana_tz

UA = f'user-agent "{config.CONTACT}"'
DATABASES = {
    "USGS_NWIS": "USGS NWIS observed instantaneous values",
    "NOAA_NWPS": "NOAA NWPS gauges, NWS observed and forecast",
    "NOAA_NWM": "NOAA National Water Model reach streamflow via NWPS",
    "OPEN_METEO": "Open-Meteo hourly precipitation",
}


class ScoutError(Exception):
    pass


def _rest(db, table, url):
    return (f'CREATE LIGHTNING REST TABLE {table} FROM {db} REQUEST(\n endpoint "{url}",\n method "get",\n'
            f' response_type "json",\n http_encoding "URLENCODED"\n) HEADER (\n {UA}\n) BODY ()')


def _exec(sql, purpose):
    """Run DDL; an 'already exists' error means it was registered before, which is fine."""
    try:
        zetaris.run_sql(sql, purpose=purpose)
        return "created"
    except zetaris.SQLError as e:
        if "already exist" in str(e).lower():
            return "exists"
        raise


def _say(msg):
    log.event("say", text=msg)
    print(msg, flush=True)


def tables_for(lid, reach_id):
    l = lid.lower()
    return {
        "gauge": f"noaa_nwps.{l}_gauge_raw",
        "usgs": f"usgs_nwis.iv_{l}_raw",
        "nws": f"noaa_nwps.{l}_observed_raw",
        "nwm": f"noaa_nwm.reach_{reach_id}_analysis_raw",
        "rain": f"open_meteo.rain_{l}_raw",
    }


def discover(lid):
    """Register the NWPS gauge record in Zetaris and read its links with SQL."""
    lid = lid.strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{4,6}", lid):
        raise ScoutError(f"'{lid}' does not look like an NWS gauge code (e.g. CKLA1).")
    table = f"noaa_nwps.{lid.lower()}_gauge_raw"
    if not config.SAMPLE_MODE:
        for db, desc in DATABASES.items():
            _exec(f'CREATE LIGHTNING DATABASE {db} DESCRIBE BY "{desc}"', "scout: database")
        _exec(_rest("NOAA_NWPS", f"{lid.lower()}_gauge_raw", f"https://api.water.noaa.gov/nwps/v1/gauges/{lid}"),
              f"scout: register NWPS gauge {lid}")
        try:
            _exec(f"CACHE TABLE {table}", "scout: cache")
        except zetaris.SQLError:
            pass
    sql = (f"SELECT lid, name, `usgsId`, `reachId`, latitude, longitude, flood.categories.action.stage, "
           f"flood.categories.minor.stage, flood.categories.moderate.stage, flood.categories.major.stage, "
           f"`upstreamLid`, `downstreamLid`, `timeZone` FROM {table} LIMIT 1")
    try:
        row = zetaris.run_sql(sql, purpose=f"scout: discover links for {lid}")["rows"][0]
    except (zetaris.SQLError, IndexError) as e:
        raise ScoutError(f"NOAA has no usable record for gauge {lid}: {str(e)[:150]}") from e
    meta = dict(zip(["lid", "name", "usgs_id", "reach_id", "lat", "lon", "action", "minor", "moderate", "major",
                     "upstream", "downstream", "tz"], row))
    meta["tz"] = iana_tz(meta.get("tz"), meta.get("lon"))
    meta["usgs_id"] = str(meta.get("usgs_id") or "").strip()
    meta["reach_id"] = str(meta.get("reach_id") or "").strip()
    return meta


def _parts(t, tz):
    """The hourly SELECT for each source, in common units: u = USGS, n = National Water Model, w = NWS gauge, r = rain."""
    u = f"""(
  SELECT date_format(to_utc_timestamp(to_timestamp(substr(p.`dateTime`, 1, 19)), '{tz}'), 'yyyy-MM-dd HH:00') AS hr_utc,
         ROUND(AVG(CASE WHEN ts.`variable`.`variableCode`[0].`value` = '00060' AND CAST(p.`value` AS DOUBLE) > -999 THEN CAST(p.`value` AS DOUBLE) END), 1) AS usgs_cfs,
         MAX(CASE WHEN ts.`variable`.`variableCode`[0].`value` = '00060' AND CAST(p.`value` AS DOUBLE) > -999 THEN CAST(p.`value` AS DOUBLE) END) AS usgs_peak_cfs,
         ROUND(AVG(CASE WHEN ts.`variable`.`variableCode`[0].`value` = '00065' AND CAST(p.`value` AS DOUBLE) > -999 THEN CAST(p.`value` AS DOUBLE) END), 2) AS usgs_stage_ft
  FROM {t['usgs']}
  LATERAL VIEW explode(`value`.`timeSeries`) AS ts
  LATERAL VIEW explode(ts.`values`) AS v
  LATERAL VIEW explode(v.`value`) AS p
  GROUP BY 1
) u"""
    n = f"""(
  SELECT date_format(to_timestamp(substr(d.`validTime`, 1, 19)), 'yyyy-MM-dd HH:00') AS hr_utc,
         ROUND(AVG(CASE WHEN d.flow > -999 THEN d.flow END), 1) AS nwm_cfs
  FROM {t['nwm']} LATERAL VIEW explode(`analysisAssimilation`.series.data) AS d GROUP BY 1
) n"""
    w = f"""(
  SELECT date_format(to_timestamp(substr(d.`validTime`, 1, 19)), 'yyyy-MM-dd HH:00') AS hr_utc,
         ROUND(AVG(CASE WHEN d.`secondary` >= 0 THEN d.`secondary` * 1000 END), 1) AS nws_cfs,
         ROUND(AVG(CASE WHEN d.`primary` > -999 THEN d.`primary` END), 2) AS nws_stage_ft
  FROM {t['nws']} LATERAL VIEW explode(data) AS d GROUP BY 1
) w"""
    r = f"""(
  SELECT date_format(to_timestamp(substr(t, 1, 16)), 'yyyy-MM-dd HH:00') AS hr_utc, ROUND(SUM(CAST(hourly.precipitation[i] AS DOUBLE)), 2) AS rain_mm
  FROM {t['rain']} LATERAL VIEW posexplode(hourly.time) AS i, t GROUP BY 1
) r"""
    return {"u": u, "n": n, "w": w, "r": r}


def _view_sql(lid, t, tz="America/Chicago", has_usgs=True, has_nwm=True, has_nws=True, has_rain=True):
    """One hourly view in common units over the sources that work. The time spine is the first of USGS, NWS gauge,
    Model and rain that is there; a source that is missing gives NULL columns."""
    l = lid.lower()
    parts = _parts(t, tz)
    have = [k for k, ok in (("u", has_usgs), ("w", has_nws), ("n", has_nwm), ("r", has_rain)) if ok]
    if not have:
        raise ScoutError(f"No source for gauge {lid} published readings this week.")
    sk = have[0]
    null = "CAST(NULL AS DOUBLE)"
    cols = {"u": ["usgs_cfs", "usgs_peak_cfs", "usgs_stage_ft"], "n": ["nwm_cfs"], "w": ["nws_cfs", "nws_stage_ft"],
            "r": ["rain_mm"]}
    sel = [f"{sk}.hr_utc"] + [f"{k}.{c}" if k in have else f"{null} AS {c}" for k in ("u", "n", "w", "r") for c in cols[k]]
    sql = (f"CREATE SCHEMASTORE VIEW hc_{l} WITH CONTAINER water AS\n"
           f"SELECT {', '.join(sel)}\nFROM {parts[sk]}")
    for k in have[1:]:
        sql += f"\nLEFT JOIN {parts[k]} ON {k}.hr_utc = {sk}.hr_utc"
    return sql


def _source_works(part):
    """An agency can answer with an empty reply (a seasonal station, a reach with no model run, a gauge with no
    observations). Zetaris then cannot read its shape and any view over it fails with INVALID_EXTRACT_BASE_FIELD_TYPE,
    so each source's hourly SELECT is tried on its own first."""
    try:
        zetaris.run_sql(f"SELECT * FROM {part} LIMIT 1", limit=1, purpose="scout: does this source have readings")
        return True
    except zetaris.SQLError:
        return False


def register(meta):
    """Register the four sources for a discovered gauge and build its hourly view in Zetaris."""
    lid = meta["lid"].upper()
    t = tables_for(lid, meta["reach_id"])
    l = lid.lower()
    urls = ([("USGS_NWIS", t["usgs"].split(".")[1],
              f"https://waterservices.usgs.gov/nwis/iv/?format=json&sites={meta['usgs_id']}&parameterCd=00060,00065&period=P7D")]
            if meta["usgs_id"] else []) + [
        ("NOAA_NWPS", t["nws"].split(".")[1], f"https://api.water.noaa.gov/nwps/v1/gauges/{lid}/stageflow/observed"),
    ] + ([("NOAA_NWM", t["nwm"].split(".")[1],
           f"https://api.water.noaa.gov/nwps/v1/reaches/{meta['reach_id']}/streamflow?series=analysis_assimilation")]
         if meta["reach_id"] else []) + [
        ("OPEN_METEO", t["rain"].split(".")[1],
         f"https://api.open-meteo.com/v1/forecast?latitude={float(meta['lat']):.3f}&longitude={float(meta['lon']):.3f}"
         "&hourly=precipitation&past_days=7&forecast_days=2&timezone=UTC"),
    ]
    _exec("CREATE SCHEMASTORE CONTAINER water", "scout: container")
    for db, table, url in urls:
        _say(f"   registering {db}.{table} in Zetaris")
        _exec(_rest(db, table, url), f"scout: register {db}.{table}")
        try:
            _exec(f"CACHE TABLE {db.lower()}.{table}", "scout: cache")
        except zetaris.SQLError as e:
            raise ScoutError(f"{db} returned no usable data for {lid}: {str(e)[:150]}") from e
    tz = meta.get("tz") or "America/Chicago"
    parts = _parts(t, tz)
    listed = {"u": bool(meta["usgs_id"]), "n": bool(meta["reach_id"]), "w": True, "r": True}
    works = {k: listed[k] and _source_works(parts[k]) for k in parts}
    names = {"u": f"USGS {meta['usgs_id']}", "n": "the National Water Model", "w": f"NWS gauge {lid}", "r": "rain"}
    for k in parts:
        if listed[k] and not works[k]:
            _say(f"   {names[k]} returned no readings this week: building the view without it")
    _say(f"   building unified hourly view water.hc_{l}")
    view_sql = _view_sql(lid, t, tz, works["u"], works["n"], works["w"], works["r"])
    if _exec(view_sql, f"scout: view water.hc_{l}") == "exists" and not _view_works(f"water.hc_{l}"):
        _say(f"   water.hc_{l} exists but no longer works: rebuilding it")
        zetaris.run_sql(f"DROP VIEW water.hc_{l}", purpose="scout: drop broken view")
        _exec(view_sql, f"scout: rebuild view water.hc_{l}")
    return t


def _view_works(view):
    try:
        zetaris.run_sql(f"SELECT * FROM {view} LIMIT 1", limit=1, purpose="scout: check existing view")
        return True
    except zetaris.SQLError:
        return False


_SITES = {}


def ensure_site(lid):
    """Return a Site ready for investigation, discovering and registering it first if needed (once per process)."""
    lid = (lid or FLAGSHIP.lid).strip().upper()
    if lid == FLAGSHIP.lid:
        return FLAGSHIP
    if lid in _SITES:
        return _SITES[lid]
    from . import memory  # local import: memory imports sites
    known = next((g for g in memory.known_sites() if g.get("lid") == lid), None)
    if known and not config.SAMPLE_MODE and _view_works(known.get("view", "")):  # registered before and still works
        fields = {k: known[k] for k in ("lid", "name", "view", "usgs_id", "reach_id", "lat", "lon", "usgs_label",
                                        "nws_label", "flood", "upstream", "downstream", "tz") if k in known}
        fields["flood"] = clean_flood(fields.get("flood"))  # saved by an older version as text
        site = Site(cols={g: g for g in GENERIC}, entity=f"gauge:nwps:{lid}", **fields)
        _SITES[lid] = site
        return site
    _say(f"SOURCE SCOUT: looking up NWS gauge {lid} through Zetaris...")
    meta = discover(lid)
    _say(f"   {meta['name']}: USGS site {meta['usgs_id'] or 'none linked'}, "
         f"National Water Model reach {meta['reach_id'] or 'none linked'}")
    if not config.SAMPLE_MODE:
        register(meta)
    site = Site(
        lid=lid, name=meta["name"], view=f"water.hc_{lid.lower()}", cols={g: g for g in GENERIC},
        usgs_id=str(meta["usgs_id"]), reach_id=str(meta["reach_id"]), lat=float(meta["lat"]), lon=float(meta["lon"]),
        usgs_label=f"USGS {meta['usgs_id']}" if meta["usgs_id"] else "USGS (no gauge linked)", nws_label=f"NWS {lid}",
        flood=clean_flood({k: meta.get(k) for k in ("action", "minor", "moderate", "major")}),
        upstream=meta.get("upstream") or "", downstream=meta.get("downstream") or "",
        entity=f"gauge:nwps:{lid}", tz=meta.get("tz") or "America/Chicago",
    )
    memory.register_site(site)
    log.event("site", site=site.to_dict())
    _SITES[lid] = site
    return site
