"""What the agent knows about the data in Zetaris: tables, columns, units, and gotchas (per site)."""

CATALOG = """
DATA IN ZETARIS (Lightning SQL = Spark SQL dialect). Region: Black Warrior River at Tuscaloosa/Northport, Alabama.
Four separate agencies/systems, unified in Zetaris. Local time is US Central (CDT = UTC-5 in October).

=== START HERE: water.hourly_compare  (one row per UTC hour, all sources in common units) ===
  hr_utc                    string 'yyyy-MM-dd HH:00' in UTC (e.g. '2026-10-23 02:00' = 9 PM CDT on Oct 22)
  usgs_northport_cfs        USGS 02465000 Black Warrior R at Northport: MEASURED discharge, hourly mean, cfs
  usgs_northport_peak_cfs   same gauge, highest 15-min discharge in that hour, cfs
  usgs_northport_stage_ft   same gauge, gage height, ft
  usgs_oliver_tailwater_ft  USGS 02465005 at Oliver Lock and Dam: TAILWATER STAGE ONLY in FEET (this gauge has NO discharge)
  nwm_cfs                   NOAA National Water Model reach 18229923: SIMULATED flow (analysis/assimilation), cfs. NULL = not published yet
  nws_oliver_cfs            NOAA NWS/NWPS gauge TODA1 at Oliver Lock and Dam: observed flow, ALREADY CONVERTED TO cfs (do NOT multiply again)
  nws_oliver_tailwater_ft   same NWS gauge: tailwater stage, ft
  rain_mm                   Open-Meteo modeled rainfall at ONE point (the gauge), mm in that hour. There is no spatial grid: never filter by lat/lon
  NULL means that source has no value for that hour.

Background facts (use them to reason, verify with data):
- The Northport gauge (02465000) sits in the pool just UPSTREAM of Oliver Lock and Dam (TODA1 / 02465005), ~1 km apart.
  Water passing Northport then passes through Oliver Lock and Dam.
- Upstream of both is Holt Lock and Dam, which has a hydropower plant; above it are more locks/dams.
  Hydropower plants commonly release water in scheduled daily pulses ("hydropeaking"). No dam operation records are in the data,
  so this can only be inferred from the flow pattern (e.g. the same hour every day, with no rain).
- Mass balance: water passing Northport must pass Oliver ~1 km downstream. Over a full day, the DAILY MEAN flows at the two
  gauges should be similar unless the pool level (usgs_northport_stage_ft) rose or fell (storage change). Comparing daily
  means and the day's pool-stage change is the decisive test between "the pool re-regulates the pulses" and "one gauge's
  stage-to-flow rating is biased".
- The NWM is a simulation; it assimilates USGS observations in its analysis, and does not explicitly model dam operations.
- NWPS flood stages for TODA1 (in water.nwps_gauge_info): action 122 ft, minor 129, moderate 138, major 146.
  They apply to the Oliver Lock and Dam TAILWATER stage (nws_oliver_tailwater_ft, normally ~95 ft).
  They do NOT apply to usgs_northport_stage_ft (~123 ft), which is the pool level upstream of the dam on a different reference.
- usgs_oliver_tailwater_ft and nws_oliver_tailwater_ft have been observed to report identical values (likely the same stage sensor); verify this, and check whether the agencies convert stage to flow differently.

Detailed source views (only if hourly_compare is not enough):
  water.usgs_observed(site_id, param_code '00060' cfs | '00065' ft, unit, valid_time local-with-offset string, value)
  water.nwps_stageflow(lid, kind 'observed'|'forecast', stage ft, flow KCFS, valid_time UTC string)
  water.nwm_streamflow(series, valid_time UTC string, flow cfs)
  water.rain_hourly(valid_time UTC string, precip mm)
  water.nwps_gauge_info(flood stage thresholds)

SQL RULES:
- One statement, no trailing semicolon, always LIMIT <= 200. Prefer aggregates (MIN/MAX/AVG/SUM, GROUP BY day) over raw rows.
- Filter hourly_compare with string comparisons on hr_utc, e.g. hr_utc BETWEEN '2026-10-22 00:00' AND '2026-10-23 06:00'.
- Use window functions (LAG, ROW_NUMBER) instead of self-joins.
- Never call a stage (ft) a discharge (cfs).
- Only standard Spark SQL functions exist (no ST_* spatial functions).
- 'Tonight' / local evening times must be converted: 9 PM CDT Oct 22 = '2026-10-23 02:00' UTC.
"""


GENERIC_CATALOG = """
DATA IN ZETARIS (Lightning SQL = Spark SQL dialect). Site: {name} (NWS gauge {lid}).
The Source Scout registered four separate agencies' data for this place in Zetaris. Local time at this gauge is {tzl} (UTC{off:+d}).

=== START HERE: {view}  (one row per UTC hour, all sources in common units) ===
  hr_utc         string 'yyyy-MM-dd HH:00' in UTC (e.g. '{ex_utc}' = 9 PM {tzl} on Oct 22)
  usgs_cfs       USGS {usgs_id}: MEASURED discharge, hourly mean, cfs (NULL if this USGS site does not report discharge)
  usgs_peak_cfs  same USGS gauge, highest 15-min discharge in that hour, cfs
  usgs_stage_ft  same USGS gauge, gage height, ft
  nwm_cfs        NOAA National Water Model reach {reach_id}: SIMULATED flow (analysis/assimilation), cfs. NULL = not published
  nws_cfs        NOAA NWS gauge {lid}: observed flow, ALREADY CONVERTED TO cfs from kcfs (do NOT multiply again)
  nws_stage_ft   same NWS gauge: stage, ft
  rain_mm        Open-Meteo modeled rainfall at ONE point (the gauge), mm in that hour. Never filter by lat/lon
  NULL means that source has no value for that hour.

Background facts (verify with data):
- The NWS gauge {lid} is linked by NOAA to USGS site {usgs_id}; they are normally the same physical place, so their stages
  should agree. Differences in FLOW then come from how each agency converts stage to flow (rating) or from processing delays.
- Upstream gauge (per NOAA): {upstream}. Downstream gauge: {downstream}.
- {regulation}
- The NWM is a simulation; it assimilates USGS observations in its analysis (so it can lag them), and does not explicitly
  model dam operations.
- NWS flood stages for {lid} (ft): {flood}. They apply to nws_stage_ft. They are levels reached only in floods, so a stage
  several feet below them is normal and says nothing about datums; to test a datum offset, compare usgs_stage_ft with
  nws_stage_ft hour by hour.
- If usgs_cfs is NULL for every hour, this USGS site reports stage only: it is not a flow source (never call its flow 0).

SQL RULES:
- One statement, no trailing semicolon, always LIMIT <= 200. Prefer aggregates (MIN/MAX/AVG/SUM, GROUP BY day) over raw rows.
- Filter {view} with string comparisons on hr_utc, e.g. hr_utc BETWEEN '2026-10-22 00:00' AND '2026-10-23 06:00'.
- Use window functions (LAG, ROW_NUMBER) instead of self-joins.
- Never call a stage (ft) a discharge (cfs). Only standard Spark SQL functions exist.
- Local evening times must be converted: 9 PM {tzl} Oct 22 = '{ex_utc}' UTC.
"""


def catalog(site):
    if site.flagship:
        return CATALOG
    regulated = any(w in site.name.lower() for w in ("dam", "lock", "l&d", "reservoir", "lake"))
    regulation = ("This gauge is at a dam or lock, so its flow is regulated: releases can make flow jump at scheduled times "
                  "regardless of rain." if regulated else
                  "Nothing in the gauge name suggests a dam at this location; flow is expected to follow rainfall and "
                  "upstream conditions, but upstream dams may still regulate it.")
    tzl, off = site.tz_now()
    h = 21 - off  # 9 PM local on Oct 22 in UTC hours from Oct 22 00:00
    ex_utc = f"2026-10-{22 + h // 24:02d} {h % 24:02d}:00"
    return GENERIC_CATALOG.format(tzl=tzl, off=off, ex_utc=ex_utc, name=site.name, lid=site.lid, view=site.view, usgs_id=site.usgs_id,
                                  reach_id=site.reach_id, upstream=site.upstream or "unknown",
                                  downstream=site.downstream or "unknown", regulation=regulation,
                                  flood=", ".join(f"{k} {v}" for k, v in site.flood.items() if v not in (None, -999)) or "not listed")
