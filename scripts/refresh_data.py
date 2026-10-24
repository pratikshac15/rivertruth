"""Refresh the live data in Zetaris: re-fetch every registered REST source and re-cache it.

A Lightning REST table re-fetches its API on each uncached query; we cache each one for speed and
reproducibility, so a refresh = uncache + cache.     Run:  python3 scripts/refresh_data.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent import zetaris  # noqa: E402

RAW_TABLES = [
    "usgs_nwis.iv_black_warrior_raw",
    "noaa_nwps.toda1_gauge_raw",
    "noaa_nwps.toda1_observed_raw",
    "noaa_nwps.toda1_forecast_raw",
    "noaa_nwm.reach_18229923_analysis_raw",
    "open_meteo.tuscaloosa_rain_raw",
]


def try_sql(*stmts):
    last = None
    for s in stmts:
        try:
            zetaris.run_sql(s, purpose="refresh")
            return s.split()[0]
        except zetaris.SQLError as e:
            last = e
    raise last


# plus every gauge the Source Scout has registered (from the World Model registry)
try:
    from agent import memory, scout
    for g in memory.known_sites():
        t = scout.tables_for(g["lid"], g["reach_id"])
        RAW_TABLES += [t["gauge"], t["usgs"], t["nws"], t["nwm"], t["rain"]]
except Exception as e:  # noqa: BLE001
    print("note: could not list Scout-registered gauges:", str(e)[:100])
RAW_TABLES = list(dict.fromkeys(RAW_TABLES))

for t in RAW_TABLES:
    try:
        how = try_sql(f"UNCACHE TABLE IF EXISTS {t}", f"UNCACHE TABLE {t}", f"OFFSITE UNCACHE TABLE {t}")
    except zetaris.SQLError as e:
        how = f"uncache skipped ({str(e)[:60]})"
    try:
        zetaris.run_sql(f"CACHE TABLE {t}", purpose="refresh")
        print(f"refreshed  {t}   ({how})")
    except zetaris.SQLError as e:
        print(f"FAILED     {t}: {str(e)[:150]}")

res = zetaris.run_sql("""SELECT MAX(hr_utc) AS last_hour, MAX(CASE WHEN nwm_cfs IS NOT NULL THEN hr_utc END) AS nwm_last,
       MAX(CASE WHEN nws_oliver_cfs IS NOT NULL THEN hr_utc END) AS nws_last, COUNT(*) AS hours FROM water.hourly_compare""",
                      purpose="refresh check")
print("\nlatest data now:", dict(zip(["usgs_last_hour", "nwm_last_hour", "nws_last_hour", "hours"], res["rows"][0])))
