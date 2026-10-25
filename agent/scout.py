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


def tables_for(lid, reach_id, slots=None):
    """Zetaris table names for a gauge's sources. Each source has two cached copies ("" and "b"), so a refresh can
    fetch into the spare one and switch only when the new data has arrived."""
    l, slots = lid.lower(), slots or {}
    sfx = lambda k: "_b" if slots.get(k) == "b" else ""  # noqa: E731
    return {
        "gauge": f"noaa_nwps.{l}_gauge_raw",
        "usgs": f"usgs_nwis.iv_{l}_raw{sfx('usgs')}",
        "nws": f"noaa_nwps.{l}_observed_raw{sfx('nws')}",
        "nwm": f"noaa_nwm.reach_{reach_id}_analysis_raw{sfx('nwm')}",
        "rain": f"open_meteo.rain_{l}_raw{sfx('rain')}",
    }


def _urls(lid, usgs_id, reach_id, lat, lon, t):
    """(source, database, table, URL) for each source this gauge has."""
    return ([("usgs", "USGS_NWIS", t["usgs"].split(".")[1],
              f"https://waterservices.usgs.gov/nwis/iv/?format=json&sites={usgs_id}&parameterCd=00060,00065&period=P7D")]
            if usgs_id else []) + [
        ("nws", "NOAA_NWPS", t["nws"].split(".")[1], f"https://api.water.noaa.gov/nwps/v1/gauges/{lid}/stageflow/observed"),
    ] + ([("nwm", "NOAA_NWM", t["nwm"].split(".")[1],
           f"https://api.water.noaa.gov/nwps/v1/reaches/{reach_id}/streamflow?series=analysis_assimilation")]
         if reach_id else []) + [
        ("rain", "OPEN_METEO", t["rain"].split(".")[1],
         f"https://api.open-meteo.com/v1/forecast?latitude={float(lat):.3f}&longitude={float(lon):.3f}"
         "&hourly=precipitation&past_days=7&forecast_days=2&timezone=UTC"),
    ]


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
    null = "CAST(NULL AS DOUBLE)"
    cols = {"u": ["usgs_cfs", "usgs_peak_cfs", "usgs_stage_ft"], "n": ["nwm_cfs"], "w": ["nws_cfs", "nws_stage_ft"],
            "r": ["rain_mm"]}
    # The time axis is every hour any river source published (so NWS readings still show when USGS stops). Rain is
    # never part of it: its feed includes forecast hours. With no river source at all, rain is the axis.
    river = [k for k in ("u", "w", "n") if k in have] or ["r"]
    spine = "(" + " UNION ".join(f"SELECT hr_utc FROM {parts[k]}" for k in river) + ") s"
    sel = ["s.hr_utc"] + [f"{k}.{c}" if k in have else f"{null} AS {c}" for k in ("u", "n", "w", "r") for c in cols[k]]
    sql = (f"CREATE SCHEMASTORE VIEW hc_{l} WITH CONTAINER water AS\n"
           f"SELECT {', '.join(sel)}\nFROM {spine}")
    for k in have:
        sql += f"\nLEFT JOIN {parts[k]} ON {k}.hr_utc = s.hr_utc"
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
    urls = [u[1:] for u in _urls(lid, meta["usgs_id"], meta["reach_id"], meta["lat"], meta["lon"], t)]
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
_REFRESHED = {}        # lid -> when its sources were last fetched (or last tried) in this process
REFRESH_AFTER = 3600   # seconds: older data is fetched again when a River check is run


def refresh(site, now=None):
    """Fetch a gauge's sources again if this process has not done so in the last hour.

    Each source is fetched into its spare cached copy; the view switches to the new copy only for sources that
    actually arrived, so a refusal (NOAA 429) or an empty reply leaves the copy in use untouched.
    Returns "refreshed", "fresh" (fetched within the hour), "failed" (nothing new arrived), or None (not applicable:
    the flagship's hand-built tables, or sample mode).
    """
    from datetime import datetime, timezone
    if site.flagship or config.SAMPLE_MODE:
        return None
    now = now or datetime.now(timezone.utc)
    last = _REFRESHED.get(site.lid)
    if last and (now - last).total_seconds() < REFRESH_AFTER:
        return "fresh"
    _REFRESHED[site.lid] = now  # also after a failure: do not ask the agencies again for an hour
    old = dict(site.slots or {})
    spare = {k: ("" if old.get(k) == "b" else "b") for k in ("usgs", "nws", "nwm", "rain")}
    t_new = tables_for(site.lid, site.reach_id, spare)
    arrived = {}
    for key, db, table, url in _urls(site.lid, site.usgs_id, site.reach_id, site.lat, site.lon, t_new):
        full = f"{db.lower()}.{table}"
        try:
            _exec(_rest(db, table, url), f"refresh: register {full}")
            try:
                zetaris.run_sql(f"UNCACHE TABLE IF EXISTS {full}", purpose="refresh: clear the spare copy")
            except zetaris.SQLError:
                pass
            _exec(f"CACHE TABLE {full}", f"refresh: fetch {full}")
            arrived[key] = True
        except zetaris.SQLError as e:
            log.event("refresh_source_failed", lid=site.lid, table=full, error=str(e)[:200])
    if not arrived:
        _say(f"   could not fetch newer data for {site.lid}; keeping the copy in Zetaris")
        return "failed"
    slots = {k: (spare[k] if arrived.get(k) else old.get(k, "")) for k in spare}
    t = tables_for(site.lid, site.reach_id, slots)
    parts = _parts(t, site.tz)
    listed = {"u": bool(site.usgs_id), "n": bool(site.reach_id), "w": True, "r": True}
    works = {k: listed[k] and _source_works(parts[k]) for k in parts}
    view_sql = _view_sql(site.lid, t, site.tz, works["u"], works["n"], works["w"], works["r"])
    try:
        zetaris.run_sql(f"DROP VIEW IF EXISTS {site.view}", purpose="refresh: replace the view")
        _exec(view_sql, f"refresh: view {site.view}")
    except (zetaris.SQLError, ScoutError) as e:  # rebuild the view on the copies it used before
        log.event("refresh_view_failed", lid=site.lid, error=str(e)[:200])
        t_old = tables_for(site.lid, site.reach_id, old)
        po = _parts(t_old, site.tz)
        ok = {k: listed[k] and _source_works(po[k]) for k in po}
        _exec(_view_sql(site.lid, t_old, site.tz, ok["u"], ok["n"], ok["w"], ok["r"]), f"refresh: restore {site.view}")
        return "failed"
    site.slots = slots
    from . import memory
    memory.register_site(site)  # remember which copy each source uses
    _say(f"   fetched the latest data for {site.lid} ({', '.join(k for k in arrived)})")
    return "refreshed"


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
                                        "nws_label", "flood", "upstream", "downstream", "tz", "slots") if k in known}
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
        from datetime import datetime, timezone
        _REFRESHED[lid] = datetime.now(timezone.utc)  # registering just fetched every source
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
