"""The report's "For the public" section, written in code from the River check facts.

The public summary is the part most people read and the one a model got wrong most often (a wrong distance, a cause
stated as fact). So it is built from facts computed in code, the same ones the River check shows: what the river did,
whether rain explains it, and whether the other sources showed it. An inferred cause is always "most likely".
"""


def _who(source):
    s = source.lower()
    if s.startswith("usgs"):
        return "the USGS gauge"
    if s.startswith("nws"):
        return "the NWS gauge"
    if s.startswith("national water model"):
        return "the National Water Model"
    return source


def _hours(text):
    """'about 1 h later' -> 'about 1 hour later'."""
    for n in range(1, 13):
        text = text.replace(f"about {n} h ", f"about {n} hour{'s' if n > 1 else ''} ")
    return text


def _seen(o):
    who, s = _who(o["source"]), o["summary"]
    if s.startswith("did not show it"):
        return f"{who} did not show it"
    if s.startswith("no data"):
        return f"{who} had no data on those days"
    if ": " in s:  # different timing on different days
        return f"{who} showed it " + _hours(s.split(": ", 1)[1].replace("at the same hour", "at the same time"))
    if "same hour" in s:
        return f"{who} showed it at the same time"
    if "showed it" in s:
        return f"{who} showed it {_hours(s.split(', ', 1)[-1])}".rstrip()
    return f"{who}: {s}"


def _agrees(o):
    who, s = _who(o["source"]), o["status"]
    if s.startswith("agrees"):
        return f"{who} agrees with it"
    if s.startswith("reads about"):
        return f"{who} {s.replace('x ', ' times ', 1)}"
    return None


def _join(parts):
    parts = [p for p in parts if p]
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1] if parts else ""


def summary(f):
    """Two plain sentences, or None when the facts are not usable (the reporter's text is then kept)."""
    if not f or not f.get("ok"):
        return None
    if f.get("mode") == "stage":
        return (f"This gauge reports water level only. {f['now']['in_words']} There is no flow from other sources "
                "to compare it with.")
    p, now = f.get("pattern"), f["now"]
    trusted = _who(f["trusted"])
    if p:
        dry = (p.get("rain_mm_on_those_days") or 0) < 1
        cause = (", with no rain: most likely a scheduled water release from a dam" if dry and f.get("regulated") else
                 ", with no rain" if dry else "")
        first = (f"On {p['days']} of the last {p['of_days']} days, the river here rose from about "
                 f"{p['typical_low_cfs']:,} to {p['typical_peak_cfs']:,} cubic feet per second around "
                 f"{p['local_hour']} {p['tz']}{cause}.")
        seen = _join([_seen(o) for o in p.get("other_sources", [])])
        second = f"Measured by {trusted}; of the other sources, {seen}." if seen else f"Measured by {trusted}."
        return f"{first} {second}"
    rise = now.get("recent_rise")
    if rise:
        rain = rise.get("rain_mm_from_day_before") or 0
        first = (f"The river here rose from about {rise['from_cfs']:,} to {rise['to_cfs']:,} cubic feet per second "
                 f"since {rise['since_local']}" + (f", after {rain:g} mm of rain" if rain >= 1 else "") + ".")
    else:
        r = now.get("vs_week_median") or 1
        level = ("higher than usual" if r >= 1.5 else "lower than usual" if r <= 0.85 else "about as usual")
        first = f"The river here has been running {level} this week, with no regular daily pattern."
    same = [_who(o["source"]) for o in f.get("others", []) if o["status"].startswith("agrees")]
    rest = [_agrees(o) for o in f.get("others", []) if not o["status"].startswith("agrees")]
    agree = _join(([f"{_join(same)} agree{'s' if len(same) == 1 else ''} with it"] if same else []) + rest)
    second = f"{trusted[0].upper() + trusted[1:]} is the one to trust here" + (f"; {agree}." if agree else ".")
    return f"{first} {second}"
