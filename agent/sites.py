"""River sites RiverTruth can investigate.

A Site maps one place on a river to its four sources in Zetaris and to plain-language labels. The flagship site
(Black Warrior River at Tuscaloosa, NWS gauge TODA1) has a hand-built view and expert background notes. Every other
gauge is discovered and registered automatically by the Source Scout (agent/scout.py).
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# NOAA NWPS gauge time zones (POSIX style) -> IANA zones
NWPS_TZ = {"EST5EDT": "America/New_York", "CST6CDT": "America/Chicago", "MST7MDT": "America/Denver",
           "MST": "America/Phoenix", "PST8PDT": "America/Los_Angeles", "AKST9AKDT": "America/Anchorage",
           "YST9YDT": "America/Anchorage", "HST": "Pacific/Honolulu", "HST10": "Pacific/Honolulu",
           "AST": "America/Puerto_Rico", "AST4": "America/Puerto_Rico", "ChST": "Pacific/Guam"}


def iana_tz(nwps_tz, lon=None):
    if nwps_tz in NWPS_TZ:
        return NWPS_TZ[nwps_tz]
    if isinstance(nwps_tz, str) and "/" in nwps_tz:
        return nwps_tz
    if lon is None:
        return "America/Chicago"
    lon = float(lon)  # rough fallback by longitude
    return ("America/New_York" if lon > -85 else "America/Chicago" if lon > -101 else
            "America/Denver" if lon > -114 else "America/Los_Angeles")

# Generic column names every site view exposes (the flagship maps its own column names onto these)
GENERIC = ["hr_utc", "usgs_cfs", "usgs_peak_cfs", "usgs_stage_ft", "nwm_cfs", "nws_cfs", "nws_stage_ft", "rain_mm"]

# Alabama watch list: dam-regulated and free-flowing rivers (NWS gauge codes)
WATCHLIST = ["TODA1", "BLDA1", "AVLA1", "CLBA1", "CKLA1", "BSBA1", "ARTA1"]


@dataclass
class Site:
    lid: str
    name: str
    view: str
    cols: dict                       # generic name -> column in `view`
    usgs_id: str = ""
    reach_id: str = ""
    lat: float = 0.0
    lon: float = 0.0
    usgs_label: str = "USGS gauge"
    nws_label: str = "NWS gauge"
    flood: dict = field(default_factory=dict)
    upstream: str = ""
    downstream: str = ""
    flagship: bool = False
    entity: str = ""                 # World Model entity the run's memory attaches to
    tz: str = "America/Chicago"      # local time zone of the gauge (IANA)

    def tz_now(self, at=None):
        """(label, offset hours) of local time at `at` (UTC datetime) or now, e.g. ('CDT', -5)."""
        at = at or datetime.now(timezone.utc)
        local = at.astimezone(ZoneInfo(self.tz))
        return local.tzname(), round(local.utcoffset().total_seconds() / 3600)

    def rows_sql(self, limit=200):
        """The NEWEST `limit` hours, returned oldest-first (some gauges keep a month of history)."""
        sel = ", ".join(f"{self.cols.get(g, 'NULL')} AS {g}" for g in GENERIC)
        return (f"SELECT * FROM (SELECT {sel} FROM {self.view} ORDER BY hr_utc DESC LIMIT {limit}) latest "
                f"ORDER BY hr_utc")

    def to_dict(self):
        return {k: getattr(self, k) for k in ("lid", "name", "view", "usgs_id", "reach_id", "lat", "lon", "usgs_label",
                                              "nws_label", "flood", "upstream", "downstream", "flagship", "tz")}


def no_missing(rows):
    """NOAA and USGS write -9999 (or -999) for 'no value'. Turn those into None so they are never read as a reading,
    and numbers that Zetaris sends as text into numbers."""
    def clean(v):
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return None if v <= -999 else v
        if isinstance(v, str):  # Zetaris returns numbers as text
            try:
                f = float(v)
            except ValueError:
                return v
            return None if f <= -999 else f
        return v
    return [[clean(v) for v in r] for r in rows]


def clean_flood(flood):
    """NWS flood levels as numbers; a level NOAA has not set (-999, empty) becomes None."""
    out = {}
    for k, v in (flood or {}).items():
        try:
            f = float(v)
        except (TypeError, ValueError):
            f = None
        out[k] = None if f is None or f <= -999 else f
    return out


FLAGSHIP = Site(
    lid="TODA1",
    name="Black Warrior River at Tuscaloosa and Northport, Alabama",
    view="water.hourly_compare",
    cols={"hr_utc": "hr_utc", "usgs_cfs": "usgs_northport_cfs", "usgs_peak_cfs": "usgs_northport_peak_cfs",
          "usgs_stage_ft": "usgs_northport_stage_ft", "nwm_cfs": "nwm_cfs", "nws_cfs": "nws_oliver_cfs",
          "nws_stage_ft": "nws_oliver_tailwater_ft", "rain_mm": "rain_mm"},
    usgs_id="02465000", reach_id="18229923", lat=33.209, lon=-87.596,
    usgs_label="USGS Northport", nws_label="NWS Oliver",
    flood={"action": 122, "minor": 129, "moderate": 138, "major": 146},
    flagship=True, entity="gauge:usgs:02465000",
)
