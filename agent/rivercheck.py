"""River check: a quick, plain-language answer for someone heading to a river.

"Which number should I trust here, what does this river do that the forecast will not tell me, and how is it now?"

Every number is computed in code from the gauge's Zetaris view (the same data the agent team investigates). A fast
Nemotron model only turns those facts into a few plain sentences for the person's plans; if it is unavailable, a
template does. A recurring pattern found here is stored on the gauge in the World Model, and the latest full
investigation of the gauge (if any) is shown alongside, so quick checks reuse what the agents learned.
"""
import collections
import json
import re
import statistics
from datetime import datetime, timedelta, timezone

from . import config, llm, log, memory, scout, sites, snapshot, zetaris

FLOOR = 20.0  # cfs; ratios against near-zero flows are meaningless


def _f(v):
    try:
        return None if v in (None, "", "null") else float(v)
    except (TypeError, ValueError):
        return None


def _hour(h):
    return f"{h % 12 or 12} {'AM' if h < 12 else 'PM'}"


def _cfs(v):
    return "-" if v is None else f"{v:,.0f} cfs"


def _seen_by_others(rows, surges, trusted, names):
    """Did the other sources show each surge, and when? (the part a forecaster or a weather app would miss)"""
    by_t = {r["t"]: r for r in rows}
    out = []
    for k in ("usgs", "nws", "nwm"):
        if k == trusted:
            continue
        seen, lags, flat, nodata = 0, [], 0, 0
        for s in surges:
            win = [by_t.get(s["t"] + timedelta(hours=h)) for h in range(-2, 5)]
            vals = [(h, r[k]) for h, r in zip(range(-2, 5), win) if r and r[k] is not None]
            day = [r[k] for r in rows if r[k] is not None and abs((r["t"] - s["t"]).total_seconds()) <= 12 * 3600]
            if len(vals) < 3 or not day:
                nodata += 1
                continue
            h, v = max(vals, key=lambda x: x[1])
            if v >= 2 * max(min(day), FLOOR):
                seen += 1
                lags.append(h)
            else:
                flat += 1
        if seen + flat == 0:
            summary = "no data on those days"
        elif seen == 0:
            summary = f"did not show it on any of {flat} days with data"
        else:
            def when(lag):
                return "at the same hour" if lag == 0 else f"about {abs(lag):g} h {'later' if lag > 0 else 'earlier'}"
            counts = sorted(collections.Counter(lags).items())
            if len(counts) == 1:  # the same timing every day
                summary = f"showed it on {seen} of {seen + flat} days with data, {when(counts[0][0])}"
            else:  # different timing on different days: say so, never average it
                summary = (f"showed it on {seen} of {seen + flat} days with data: " +
                           " and ".join(f"{when(lag)} on {n} day{'s' if n > 1 else ''}" for lag, n in counts))
        out.append({"source": names[k], "summary": summary})
    return out


def _stage_only(site, rows, tz_label, loc):
    """Gauges that report water level but no flow (many coastal, lake and lock gauges): level facts only."""
    st = [r for r in rows if r["stage"] is not None]
    last = st[-1]
    vals = [r["stage"] for r in st]
    med = statistics.median(vals)
    prev = next((r for r in st if r["t"] == last["t"] - timedelta(hours=3)), None)
    trend = None
    if prev:
        d = last["stage"] - prev["stage"]
        trend = "rising" if d > 0.3 else "falling" if d < -0.3 else "steady"
    lo, hi = min(vals), max(vals)
    diff = last["stage"] - med
    now = {"stage_ft": round(last["stage"], 2), "at_local": f"{loc(last['t']):%a} {_hour(loc(last['t']).hour)} {tz_label}",
           "vs_week_median_ft": round(diff, 2), "week_range_ft": [round(lo, 2), round(hi, 2)], "trend_last_3h": trend,
           "rain_last_24h_mm": round(sum(r["rain"] or 0 for r in rows if r["t"] > rows[-1]["t"] - timedelta(hours=24)), 1)}
    now["in_words"] = (f"Water level {now['stage_ft']} ft at {now['at_local']}: {abs(diff):.1f} ft "
                       f"{'above' if diff >= 0 else 'below'} this week's typical level (the week ranged {lo:.1f} to {hi:.1f} ft)"
                       + (f", {trend} over the last 3 hours" if trend else "") + ".")
    return {"ok": True, "mode": "stage", "site": site.lid, "name": site.name, "tz": tz_label,
            "trusted": f"NWS gauge water level ({site.nws_label})",
            "why_trusted": "this gauge reports water level only, not flow, so there is nothing to compare it with",
            "others": [], "pattern": None, "now": now,
            "regulated": any(w in site.name.lower() for w in ("dam", "lock", "l&d", "reservoir", "lake"))}


def compute(site):
    """All the facts for a River check, from SQL rows; no model involved."""
    res = zetaris.run_sql(site.rows_sql(), purpose=f"river check: hourly rows for {site.lid}")
    rows = []
    for r in sites.no_missing(res["rows"]):  # GENERIC order: hr, usgs_cfs, usgs_peak, usgs_stage, nwm, nws_cfs, nws_stage, rain
        t = datetime.strptime(r[0], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        rows.append({"t": t, "usgs": _f(r[1]), "nwm": _f(r[4]), "nws": _f(r[5]), "rain": _f(r[7]),
                     "stage": _f(r[6]) if _f(r[6]) is not None else _f(r[3])})
    if not rows:
        return {"ok": False, "why": "No data for this gauge in Zetaris yet."}
    tz_label, off = site.tz_now(rows[-1]["t"])
    loc = lambda t: t + timedelta(hours=off)  # noqa: E731

    names = {"usgs": f"USGS measured flow ({site.usgs_label})", "nws": f"NWS gauge flow ({site.nws_label})",
             "nwm": "National Water Model (simulated)"}
    if not site.usgs_id:
        names["usgs"] = "USGS (no gauge linked here)"
    if not site.reach_id:
        names["nwm"] = "National Water Model (no reach linked here)"
    counts = {k: sum(1 for r in rows if r[k] is not None) for k in names}
    trusted = next((k for k in ("usgs", "nws", "nwm") if counts[k] >= 24), None)
    if trusted is None and sum(1 for r in rows if r["stage"] is not None) >= 24:
        return _stage_only(site, rows, tz_label, loc)
    if trusted is None:
        return {"ok": False, "counts": counts,
                "why": ("This gauge reports water level but no flow this week, and no other source covers it, so there is "
                        "nothing to compare. The official gauge page below shows its water level.")}
    why_trusted = {"usgs": "it is a measured flow at a USGS gauge, updated every 15 minutes",
                   "nws": ("the USGS gauge here reports no flow" if site.usgs_id else "no USGS gauge is linked to this place") +
                          ", so the NWS gauge (flow converted from water level) is the best record",
                   "nwm": "no gauge reports flow here this week, so only the model is available"}[trusted]

    # How the other sources compare with the trusted one (median ratio over hours where both report)
    others = []
    for k in ("usgs", "nws", "nwm"):
        if k == trusted or counts[k] == 0:
            if k != trusted:
                linked = (k != "usgs" or site.usgs_id) and (k != "nwm" or site.reach_id)
                others.append({"source": names[k], "status": "no data this week" if linked else "not available for this gauge"})
            continue
        pairs = [(r[k], r[trusted]) for r in rows if r[k] is not None and r[trusted] is not None]
        if len(pairs) < 6:
            others.append({"source": names[k], "status": f"only {len(pairs)} hours overlap"})
            continue
        ratio = statistics.median(max(a, FLOOR) / max(b, FLOOR) for a, b in pairs)
        status = ("agrees within 20%" if 0.8 <= ratio <= 1.25 else
                  f"reads about {ratio:.1f}x higher" if ratio > 1 else f"reads about {1 / ratio:.1f}x lower")
        others.append({"source": names[k], "status": status, "hours_compared": len(pairs), "median_ratio": round(ratio, 2)})

    # Recurring surge: on most full local days, a peak several times the day's low at about the same hour
    days = {}
    for r in rows:
        if r[trusted] is not None:
            days.setdefault(loc(r["t"]).date(), []).append(r)
    full = {d: rs for d, rs in days.items() if len(rs) >= 20}
    surges = []
    for d, rs in sorted(full.items()):
        pk = max(rs, key=lambda r: r[trusted])
        low = min(r[trusted] for r in rs)
        if pk[trusted] >= 1.5 * max(low, FLOOR) and pk[trusted] - low >= 200:
            i = rs.index(pk)
            start = i
            while start > 0 and rs[start - 1][trusted] > 2 * max(low, FLOOR):
                start -= 1
            surges.append({"day": d, "t": pk["t"], "hour": loc(pk["t"]).hour, "peak": pk[trusted], "low": low,
                           "rise_hours": max(1, i - start + 1), "rain": sum(r["rain"] or 0 for r in rs)})
    pattern = None
    if full and len(surges) >= 3 and len(surges) >= 0.6 * len(full):
        near = lambda s, h: min(abs(s["hour"] - h), 24 - abs(s["hour"] - h))  # noqa: E731
        best = max(range(24), key=lambda h: (sum(1 for s in surges if near(s, h) <= 1), sum(1 for s in surges if near(s, h) == 0)))
        same = [s for s in surges if min(abs(s["hour"] - best), 24 - abs(s["hour"] - best)) <= 1]
        if len(same) >= 0.6 * len(surges):
            times = statistics.median(s["peak"] / max(s["low"], FLOOR) for s in same)
            pattern = {"kind": "daily surge" if times >= 3 else "regular daily rise", "local_hour": _hour(best), "tz": tz_label, "days": len(same),
                       "of_days": len(full), "typical_low_cfs": round(statistics.median(s["low"] for s in same)),
                       "typical_peak_cfs": round(statistics.median(s["peak"] for s in same)),
                       "times_higher": round(statistics.median(s["peak"] / max(s["low"], FLOOR) for s in same), 1),
                       "rise_hours": statistics.median(s["rise_hours"] for s in same),
                       "rain_mm_on_those_days": round(sum(s["rain"] for s in same), 1),
                       "other_sources": _seen_by_others(rows, same, trusted, names)}

    # Now
    last = next(r for r in reversed(rows) if r[trusted] is not None)
    week = [r[trusted] for r in rows if r[trusted] is not None]
    med = statistics.median(week)
    prev = next((r for r in rows if r["t"] == last["t"] - timedelta(hours=3) and r[trusted] is not None), None)
    trend = None
    if prev:
        ch = (last[trusted] - prev[trusted]) / max(prev[trusted], FLOOR)
        trend = "rising" if ch > 0.15 else "falling" if ch < -0.15 else "steady"
    rain24 = sum(r["rain"] or 0 for r in rows if r["t"] > rows[-1]["t"] - timedelta(hours=24))
    now = {"flow_cfs": round(last[trusted]), "at_local": f"{loc(last['t']):%a} {_hour(loc(last['t']).hour)} {tz_label}",
           "vs_week_median": round(last[trusted] / max(med, FLOOR), 2), "week_median_cfs": round(med),
           "week_range_cfs": [round(min(week)), round(max(week))], "trend_last_3h": trend,
           "rain_last_24h_mm": round(rain24, 1), "rain_week_mm": round(sum(r["rain"] or 0 for r in rows), 1)}
    # A recent rise (e.g. after rain): when it started and how much rain fell in the day before it
    if now["vs_week_median"] >= 1.3:
        series = [r for r in rows if r[trusted] is not None]
        i = len(series) - 1
        while i > 0 and series[i - 1][trusted] > 1.15 * med:
            i -= 1
        start = series[i]["t"]
        before = [r[trusted] for r in series[max(0, i - 12):i]]
        base = min(before) if before else med
        rain = sum(r["rain"] or 0 for r in rows if start - timedelta(hours=24) <= r["t"] <= last["t"])
        now["recent_rise"] = {"since_local": f"{loc(start):%a} {_hour(loc(start).hour)} {tz_label}",
                              "from_cfs": round(base), "to_cfs": round(last[trusted]),
                              "rain_mm_from_day_before": round(rain, 1)}
    lo, hi = now["week_range_cfs"]
    pos = (last[trusted] - lo) / max(hi - lo, 1)
    level = ("far above this week's usual, because the stream was dry or nearly dry for most of the week" if med < FLOOR and last[trusted] >= 5 * FLOOR else
             "much higher than usual for this week" if now["vs_week_median"] >= 1.5 else
             "a bit higher than usual for this week" if now["vs_week_median"] >= 1.15 else
             "lower than usual for this week" if now["vs_week_median"] <= 0.85 else "about usual for this week")
    now["in_words"] = (f"{now['flow_cfs']:,} cfs at {now['at_local']}: {level} (" +
                       (f"this week's typical flow is {now['week_median_cfs']:,} cfs; " if med >= FLOOR else "") +
                       f"the week ranged {lo:,} to {hi:,} cfs, so it is "
                       f"{'near the top of' if pos >= 0.75 else 'near the bottom of' if pos <= 0.25 else 'in the middle of'} "
                       f"that range)" + (f", {trend} over the last 3 hours" if trend else "") + ".")
    return {"ok": True, "site": site.lid, "name": site.name, "tz": tz_label, "trusted": names[trusted],
            "why_trusted": why_trusted, "others": others, "pattern": pattern, "now": now,
            "regulated": site.flagship or any(w in site.name.lower() for w in ("dam", "lock", "l&d", "reservoir", "lake"))}


def _numbers(text):
    """Numbers in a text, normalised (commas removed, trailing .0 dropped); small counts like 1-9 are ignored."""
    out = set()
    for m in re.findall(r"\d[\d,]*\.?\d*", text):
        v = m.replace(",", "").rstrip(".")
        try:
            x = float(v)
        except ValueError:
            continue
        if x >= 10:
            out.add(f"{x:g}")
            out.add(f"{round(x):d}")
    return out


def _template(f, when, activity):
    if f.get("mode") == "stage":
        lines = {"headline": f"{f['name']}: water level only at this gauge, {f['now']['trend_last_3h'] or 'no recent trend'}.",
                 "trust": f"Use {f['trusted']}: {f['why_trusted']}.",
                 "pattern": f"This week the level ranged {f['now']['week_range_ft'][0]} to {f['now']['week_range_ft'][1]} ft.",
                 "now": f["now"]["in_words"],
                 "plan": "Compare today's level with what you know of this spot, and check the official gauge page for its flood levels."}
        if f.get("official_flood_status", "") in ("minor", "moderate", "major"):
            lines["headline"] = f"The NWS lists this gauge at {f['official_flood_status']} flood stage now."
            lines["plan"] = "Stay out of the water and away from the banks, and follow local emergency guidance."
        return lines
    p = f["pattern"]
    lines = {"headline": (f"Flow here has risen about {p['times_higher']}x around {p['local_hour']} {p['tz']} on "
                          f"{p['days']} of the last {p['of_days']} days." if p else
                          f"No unusual daily pattern here this week; trust {f['trusted']}."),
             "trust": f"Use {f['trusted']} here: {f['why_trusted']}. " +
                      " ".join(f"{o['source']} {o['status']}." for o in f["others"]),
             "pattern": (("No regular daily pattern this week. " if not f["pattern"] else "") +
                         (f"Flow has risen from about {f['now']['recent_rise']['from_cfs']:,} to {f['now']['recent_rise']['to_cfs']:,} cfs "
                          f"since {f['now']['recent_rise']['since_local']}, with {f['now']['recent_rise']['rain_mm_from_day_before']} mm "
                          "of rain from the day before." if f["now"].get("recent_rise") else "")).strip()
                        if not f["pattern"] else
                        (f"On {f['pattern']['days']} of the last {f['pattern']['of_days']} days, flow rose about "
                         f"{f['pattern']['times_higher']}x (from ~{f['pattern']['typical_low_cfs']:,} to ~"
                         f"{f['pattern']['typical_peak_cfs']:,} cfs) around {f['pattern']['local_hour']} {f['pattern']['tz']}, "
                         f"with {f['pattern']['rain_mm_on_those_days']} mm of rain on those days."),
             "now": f["now"]["in_words"]}
    if f.get("official_flood_status", "") in ("minor", "moderate", "major"):
        lines["headline"] = f"The NWS lists this river at {f['official_flood_status']} flood stage now."
        lines["plan"] = "Stay out of the water and away from the banks, and follow local emergency guidance."
        return lines
    lines["plan"] = (f"For {activity or 'your visit'} {when or ''}: " +
                     (f"expect the water to rise quickly around {f['pattern']['local_hour']}." if f["pattern"]
                      else "nothing unusual in this week's pattern.")).strip()
    return lines


def write_card(f, when, activity):
    prompt = f"""FACTS about a river gauge (computed in code from USGS, NWS, the National Water Model and rainfall; always correct):
{json.dumps(f, default=str, indent=1)}

The person is planning: {activity or 'a visit'}; when: {when or 'today'}.

Write a short River check for this person in plain words (no jargon, no abbreviations except cfs). Use ONLY the facts.
- "headline": one sentence: the single most useful thing for their plans.
- "trust": which source to look at here and why, and how the others compare (1-2 sentences).
- "pattern": what this river has been doing this week that a forecast would not show (1-2 sentences), or that it has been steady.
- "plan": what this means for their plans and timing (1-2 sentences). Never say it is safe; do not give safety clearance.
If a daily surge is in the facts, say at what local time and how big. If the surge happens with no rain at a dam or lock,
you may say it is most likely a scheduled water release.
If official_flood_status is minor, moderate or major flood: the headline MUST start by saying the NWS lists the river at
that flood stage now, and the plan must say to stay out of the water and away from the banks and follow local emergency
guidance (do not suggest going ahead with the activity). If it is "action", say the river is near flood stage.
Only call the river "high" or "flooding" if official_flood_status says so; otherwise compare with this week only
(use now.in_words). Do not restate numbers that are not in the facts.
Reply with ONLY JSON: {{"headline": "...", "trust": "...", "pattern": "...", "plan": "..."}}"""
    try:
        from . import schemas
        card = llm.chat_json("river_check", validate=schemas.river_card, messages=[{"role": "system", "content": "You write short, accurate, plain-language river updates."},
                                             {"role": "user", "content": prompt}], model=config.FAST_MODEL, max_tokens=3000, attempts=2)
        tmpl = _template(f, when, activity)
        allowed = _numbers(json.dumps(f, default=str))
        fixed = []
        for k in ("headline", "trust", "pattern", "plan"):
            v = card.get(k)
            if not (isinstance(v, str) and v.strip()) or not _numbers(v) <= allowed:
                card[k] = tmpl[k]   # missing, or a number that is not in the facts: use the code's sentence
                fixed.append(k)
        card["now"] = tmpl["now"]  # the reading itself is always written by code
        if f.get("official_flood_status", "") in ("minor", "moderate", "major"):
            card["headline"], card["plan"] = tmpl["headline"], tmpl["plan"]  # flood advice is never left to the model
        if fixed:
            log.event("river_check_number_guard", replaced=fixed)
        return card, config.FAST_MODEL + (f" (code wrote: {', '.join(fixed)})" if fixed else "")
    except Exception as e:  # noqa: BLE001
        log.event("river_check_llm_error", error=str(e)[:300])
    return _template(f, when, activity), "template (model unavailable)"


def check(lid, when="", activity="", flood=""):
    site = scout.ensure_site(lid)
    f = compute(site)
    if f.get("ok"):
        f["official_flood_status"] = (flood or "unknown").replace("_", " ")
    out = {"site": site.to_dict(), "facts": f}
    links = [{"label": f"NWS gauge {site.lid}", "url": f"https://water.noaa.gov/gauges/{site.lid}"}]
    if site.usgs_id:
        links.append({"label": f"USGS {site.usgs_id}", "url": f"https://waterdata.usgs.gov/monitoring-location/{site.usgs_id}/"})
    out["links"] = links
    if not f.get("ok"):
        out["card"] = {"headline": f.get("why", "Not enough data."), "trust": "", "pattern": "", "now": "", "plan": ""}
        out["written_by"] = "code"
        return out
    card, by = write_card(f, when, activity)
    out["card"], out["written_by"] = card, by
    fl = (flood or "").lower()
    if fl in ("minor", "moderate", "major"):
        out["flood_alert"] = (f"The National Weather Service lists this gauge at {fl.upper()} FLOOD stage right now. Stay out of "
                              "the water and away from the banks, and follow local emergency guidance.")
    elif fl == "action":
        out["flood_alert"] = "The National Weather Service lists this gauge at action stage: the river is near flood level."
    out["caution"] = ("This is a heads-up from public data, not a safety clearance. " +
                      ("Check the dam operator's release schedule and posted warnings before going in or on the water."
                       if f["regulated"] or f["pattern"] else "Check posted warnings and the official gauge before going in or on the water."))
    try:
        out["learned"] = memory.learned(site)
        if f["pattern"]:
            memory.record_pattern(site, {k: v for k, v in f["pattern"].items()})
    except Exception as e:  # noqa: BLE001  memory is best-effort here
        log.event("river_check_memory_error", error=str(e)[:200])
    log.event("river_check", lid=site.lid, facts=f, card=out["card"], written_by=by)
    return out


if __name__ == "__main__":
    import sys
    print(json.dumps(check(sys.argv[1] if len(sys.argv) > 1 else "TODA1", "this evening", "kayaking"), indent=1, default=str))
