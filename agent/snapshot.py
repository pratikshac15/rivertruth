"""At the start of every run, pull the latest data from Zetaris and compute the facts agents tend to get wrong.

Time alignment across sources (UTC vs Central days, model lag, missing data vs zero) is arithmetic, so it is done
in code here, not by the language model. Every agent sees the result, so nobody looks at the wrong day, mistakes
'not published' for 'missed', or misreads a lag.
"""
from datetime import datetime, timedelta, timezone

from . import log, sites, zetaris

from .sites import FLAGSHIP

ROWS_SQL = """SELECT * FROM (SELECT hr_utc, usgs_northport_cfs, usgs_northport_peak_cfs, usgs_northport_stage_ft, nwm_cfs, nws_oliver_cfs, rain_mm
FROM water.hourly_compare ORDER BY hr_utc DESC LIMIT 200) latest ORDER BY hr_utc"""   # the flagship site's newest rows
COLS = ["hr_utc", "usgs", "usgs_peak", "pool_ft", "nwm", "nws", "rain"]


def rows_sql(site):
    """Hourly rows for a site, columns in COLS order."""
    if site.flagship:
        return ROWS_SQL
    c = site.cols
    return (f"SELECT * FROM (SELECT {c['hr_utc']}, {c['usgs_cfs']}, {c['usgs_peak_cfs']}, {c['usgs_stage_ft']}, "
            f"{c['nwm_cfs']}, {c['nws_cfs']}, {c['rain_mm']} FROM {site.view} ORDER BY hr_utc DESC LIMIT 200) latest "
            f"ORDER BY hr_utc")
CDT = timedelta(hours=-5)   # local offset of the site being analysed (set per site; flagship = Central)
TZL = "CDT"


def _set_tz(site, at=None):
    global CDT, TZL
    TZL, off = site.tz_now(at)
    CDT = timedelta(hours=off)


def _f(v):
    return None if v in (None, "", "null") else float(v)


def _t(hr):
    return datetime.strptime(hr, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)


def _cdt(t):
    t = t + CDT
    h = t.hour % 12 or 12
    return f"{t.month}/{t.day} {h} {'AM' if t.hour < 12 else 'PM'}"


def _fmt(v):
    return "-" if v is None else f"{v:,.0f}"


def analyse(rows, site=FLAGSHIP):
    if rows:
        _set_tz(site, _t(rows[-1][0]))
    U, N = site.usgs_label, site.nws_label
    data = [dict(zip(COLS, r)) for r in rows]
    for d in data:
        d["t"] = _t(d["hr_utc"])
        for k in COLS[1:]:
            d[k] = _f(d[k])
    if not data:
        return f"No data in {site.view}."
    lines = []

    # Coverage: where each source actually has data (NULL outside this = not published / not available)
    cov = []
    for key, name in (("usgs", U), ("nwm", "National Water Model"), ("nws", N), ("rain", "Rain")):
        have = [d["t"] for d in data if d[key] is not None]
        cov.append(f"{name}: {_cdt(have[0])} to {_cdt(have[-1])} {TZL} ({len(have)} hours)" if have else f"{name}: no data")
    lines.append("DATA COVERAGE (outside a source's window, a missing value means NO DATA, never 'the source missed it'):\n  "
                 + "\n  ".join(cov))

    # Per Central-time day
    by_day = {}
    for d in data:
        by_day.setdefault((d["t"] + CDT).date(), []).append(d)
    nwm_times = [d["t"] for d in data if d["nwm"] is not None]
    hdr = (f"day ({TZL}) | USGS peak cfs @ hour | NWM peak from -2h to +4h of it (lag) | NWS at USGS peak | "
           f"daily mean {U} | daily mean {N} | {'pool' if site.flagship else 'USGS'} stage change ft | rain mm")
    out = [hdr]
    facts = []
    for day in sorted(by_day):
        ds = by_day[day]
        peaks = [d for d in ds if d["usgs_peak"] is not None]
        if not peaks:
            if not site.flagship:  # e.g. a USGS site that reports stage only: still summarise the other sources
                mn = [d["nws"] for d in ds if d["nws"] is not None]
                nw = [d["nwm"] for d in ds if d["nwm"] is not None]
                npk = max((d for d in ds if d["nws"] is not None), key=lambda d: d["nws"], default=None)
                out.append(" | ".join([
                    f"{day:%m-%d}{'' if len(ds) >= 20 else ' (partial day)'}", "no USGS discharge",
                    f"NWM daily mean {_fmt(sum(nw) / len(nw))} ({len(nw)} h)" if nw else "NWM no data",
                    f"NWS peak {_fmt(npk['nws'])} @ {_cdt(npk['t'])}" if npk else "-", "-",
                    _fmt(sum(mn) / len(mn)) if mn else "-", "-", f"{sum(d['rain'] or 0 for d in ds):.1f}"]))
            continue
        pk = max(peaks, key=lambda d: d["usgs_peak"])
        low = min((d["usgs"] for d in ds if d["usgs"] is not None), default=0) or 1
        idx = data.index(pk)
        prev_mean = data[idx - 1]["usgs"] if idx > 0 else None
        rising = prev_mean is None or (pk["usgs"] or 0) > prev_mean  # a real pulse peak is a local maximum, not a falling limb
        is_pulse = pk["usgs_peak"] >= 1000 and pk["usgs_peak"] >= 3 * low and rising
        win = [d for d in data if -2 * 3600 <= (d["t"] - pk["t"]).total_seconds() <= 4 * 3600]
        nwin = [d for d in win if d["nwm"] is not None]
        if not is_pulse:
            nwm_txt = "no pulse in USGS data for this day (yet)"
        elif not nwm_times or pk["t"] < nwm_times[0] - timedelta(hours=4) or pk["t"] > nwm_times[-1] + timedelta(hours=4) or not nwin:
            nwm_txt = "NWM has no data for this window"
            facts.append(f"{day:%b %d} ({TZL}): USGS pulse peaked at {_fmt(pk['usgs_peak'])} cfs at {_cdt(pk['t'])}; "
                         "the NWM has NO DATA for that window, so this day is not evidence of the model missing anything.")
        else:
            nmax = max(nwin, key=lambda d: d["nwm"])
            lag = round((nmax["t"] - pk["t"]).total_seconds() / 3600)
            partial = " (window partly unpublished)" if len(nwin) < len(win) else ""
            nwm_txt = f"{_fmt(nmax['nwm'])} @ {_cdt(nmax['t'])} (lag {lag:+d} h){partial}"
            at_peak = pk["nwm"]
            facts.append(f"{day:%b %d} ({TZL}): USGS pulse peaked at {_fmt(pk['usgs_peak'])} cfs (hourly mean {_fmt(pk['usgs'])}) "
                         f"at {_cdt(pk['t'])}; the NWM peaked at {_fmt(nmax['nwm'])} cfs at {_cdt(nmax['t'])}, "
                         f"{'the same hour' if lag == 0 else f'{abs(lag)} h ' + ('later' if lag > 0 else 'earlier')}"
                         f"{'' if at_peak is None or lag == 0 else f' (at the USGS peak hour the NWM showed {_fmt(at_peak)} cfs)'}{partial}.")
        full = len(ds) >= 20
        mu = [d["usgs"] for d in ds if d["usgs"] is not None]
        mn = [d["nws"] for d in ds if d["nws"] is not None]
        pool = [d["pool_ft"] for d in ds if d["pool_ft"] is not None]
        rain = sum(d["rain"] or 0 for d in ds)
        out.append(" | ".join([
            f"{day:%m-%d}{'' if full else ' (partial day)'}",
            f"{_fmt(pk['usgs_peak'])} @ {_cdt(pk['t'])}",
            nwm_txt,
            _fmt(pk["nws"]),
            _fmt(sum(mu) / len(mu)) if mu else "-",
            _fmt(sum(mn) / len(mn)) if mn else "-",
            f"{pool[-1] - pool[0]:+.2f}" if len(pool) > 1 else "-",
            f"{rain:.1f}",
        ]))
    full_days = [d for d in sorted(by_day) if len(by_day[d]) >= 20]
    has_usgs_flow = any(d["usgs"] is not None for d in data)
    if not has_usgs_flow:
        facts.append(f"{U} reports NO discharge at all in this window (it is a stage-only site here, or its flow is not "
                     f"published). It is not a flow source to compare: compare flow between the NWM and {N}, and use "
                     f"{U} for stage only. Never describe its flow as 0 cfs.")
    if not site.flagship:
        st = [d["pool_ft"] for d in data if d["pool_ft"] is not None]
        action = (site.flood or {}).get("action")
        try:
            action = float(action) if action not in (None, "", -999) else None
        except (TypeError, ValueError):
            action = None
        if st and action and action > 0:
            facts.append(f"Latest stage {st[-1]:.1f} ft is {action - st[-1]:.1f} ft below the NWS action stage ({action:g} ft): "
                         "the river is below flood level, which is normal and is NOT evidence of a datum difference.")
        both = [(d["nws"] / max(d["nwm"], 1.0), d) for d in data if d["nws"] is not None and d["nwm"] is not None and d["nws"] > 0]
        if both:
            rs = sorted(r for r, _ in both)
            facts.append(f"Hours where both the NWM and {N} report flow: {len(both)}. In those hours {N} / NWM ranges "
                         f"{rs[0]:.2f}x to {rs[-1]:.2f}x (median {rs[len(rs) // 2]:.2f}x); NWM mean "
                         f"{_fmt(sum(d['nwm'] for _, d in both) / len(both))} cfs vs {N} mean "
                         f"{_fmt(sum(d['nws'] for _, d in both) / len(both))} cfs.")
    if full_days and has_usgs_flow:
        um = [sum(x["usgs"] for x in by_day[d] if x["usgs"] is not None) / max(1, sum(1 for x in by_day[d] if x["usgs"] is not None)) for d in full_days]
        nm = [sum(x["nws"] for x in by_day[d] if x["nws"] is not None) / max(1, sum(1 for x in by_day[d] if x["nws"] is not None)) for d in full_days]
        facts.append(f"Mass balance over the {len(full_days)} full days: average daily mean flow is {_fmt(sum(um) / len(um))} cfs at "
                     f"{U} vs {_fmt(sum(nm) / len(nm))} cfs at {N}.")
    rain_total = sum(d["rain"] or 0 for d in data)
    facts.append(f"Total rain over the whole window: {rain_total:.1f} mm.")
    lines.insert(0, "GROUND-TRUTH FACTS (computed in code from the data; these override any finding that contradicts them):\n- "
                 + "\n- ".join(facts))
    lines.append(f"DAILY SUMMARY, computed in code from {site.view} (USGS peak = highest 15-min reading; "
                 "lag = NWM peak hour minus USGS peak hour):\n" + "\n".join(out))

    # Last 12 hours, all sources
    recent = [f"hour {TZL} | {U} cfs (peak) | NWM cfs | {N} cfs | {'pool' if site.flagship else 'USGS'} stage ft | rain mm"]
    for d in data[-12:][::-1]:
        recent.append(f"{_cdt(d['t'])} | {_fmt(d['usgs'])} ({_fmt(d['usgs_peak'])}) | {_fmt(d['nwm'])} | "
                      f"{_fmt(d['nws'])} | {'-' if d['pool_ft'] is None else d['pool_ft']} | {d['rain'] if d['rain'] is not None else '-'}")
    lines.append("LAST 12 HOURS, newest first:\n" + "\n".join(recent))
    return "\n\n".join(lines)


def build(site=FLAGSHIP):
    # "Now" is the latest hour in the data (not the wall clock), so a recorded run replays identically.
    try:
        res = zetaris.run_sql(rows_sql(site), purpose="snapshot: hourly rows for alignment")
        rows = sites.no_missing(res["rows"])
        latest = _t(rows[-1][0]) if rows else datetime.now(timezone.utc)
        _set_tz(site, latest)
        off = int(CDT.total_seconds() // 3600)
        if site.flagship:
            head = (f"LATEST DATA HOUR: {latest:%Y-%m-%d %H:%M} UTC = {_cdt(latest)} CDT. Central time (CDT) = UTC - 5 h. "
                    "'Oct 22 evening' means Oct 22 CDT, i.e. Oct 23 00:00-05:00 UTC.")
        else:
            head = (f"LATEST DATA HOUR: {latest:%Y-%m-%d %H:%M} UTC = {_cdt(latest)} {TZL}. Local time at this gauge "
                    f"({TZL}) = UTC {off:+d} h. A local date's evening (6 PM-midnight) is "
                    f"{(18 - off) % 24:02d}:00-{(24 - off) % 24:02d}:00 UTC.")
        text = head + "\n\n" + analyse(rows, site)
    except Exception as e:  # noqa: BLE001  never let the snapshot stop an investigation
        text = f"Snapshot unavailable ({type(e).__name__}: {str(e)[:150]}). Work from query results only."
    log.event("snapshot", text=text)
    return text
