"""RiverTruth test cases: real questions a flood forecaster could ask about the Black Warrior River data."""

CASES = {
    "1": ("evening-pulse",
          "On the evening of Oct 22, 2026 (Central time), USGS, the National Water Model and the NWS gauge at Oliver "
          "Lock and Dam reported very different flows for the Black Warrior River at Tuscaloosa/Northport. "
          "Why do these sources disagree, and what actually happened?"),
    "2": ("usgs-vs-nws-gap",
          "All week the NWS gauge at Oliver Lock and Dam has reported much higher flow than the USGS gauge at Northport, "
          "about 1 km upstream, most of the time. Which one should a forecaster trust, and why do they differ?"),
    "3": ("model-timing",
          "Does the National Water Model capture the daily flow surges at Northport? If it does, how late and how "
          "accurately compared with the USGS measurements, and why?"),
}

# What the example buttons say on the page: plain questions, not internal names.
LABELS = {
    "1": "Why does the river jump at 9 PM?",
    "2": "Why does the NWS gauge read higher?",
    "3": "Is the computer model late?",
}


def site_question(name):
    """The default question for any gauge the Scout registers."""
    return (f"At {name}, do the USGS gauge, the National Water Model and the NWS gauge agree about river flow this week? "
            "Where they disagree, why, and which should a forecaster trust?")


def suggest(name, facts, flood=""):
    """The question offered to the agent team, fitted to what the River check found, plus three plain examples.

    The River check's facts decide the framing: a flood, a daily surge, a recent rise, sources that disagree, or sources
    that agree. A question about why sources disagree is only offered when they do."""
    f = facts or {}
    others = f.get("others") or []
    apart = [o for o in others if not str(o.get("status", "")).startswith("agrees")]
    pattern = f.get("pattern") or {}
    rise = (f.get("now") or {}).get("recent_rise")
    fl = (flood or "").replace("_", " ").lower()
    if not f.get("ok"):
        q = site_question(name)
    elif fl in ("minor", "moderate", "major"):
        q = (f"The NWS lists {name} at {fl} flood stage. What is driving the high water, and do the USGS gauge, the "
             "National Water Model and the NWS gauge agree on how high it is?")
    elif pattern.get("kind") in ("daily surge", "regular daily rise"):
        q = (f"Why does the river at {name} rise around {pattern.get('local_hour')} {pattern.get('tz', '')} every day, "
             "and do the USGS gauge, the National Water Model and the NWS gauge all show it?").replace("  ", " ")
    elif rise:
        q = (f"What caused the rise at {name} since {rise.get('since_local')}, and did the USGS gauge, the National "
             "Water Model and the NWS gauge all catch it?")
    elif apart:
        q = site_question(name)
    else:
        q = (f"At {name}, the USGS gauge, the National Water Model and the NWS gauge roughly agree this week. Is "
             "anything unusual happening on this river, and which number is best to go by?")
    examples = [
        {"label": "What caused the latest change in this river?",
         "question": f"What caused the latest change in flow at {name} this week, and did every source show it?"},
        {"label": "Which number should I trust here, and why?",
         "question": f"At {name}, which of the USGS gauge, the National Water Model and the NWS gauge should I go by "
                     "this week, and why?"},
        {"label": "Is the computer model keeping up with this river?",
         "question": f"At {name}, does the National Water Model keep up with the measured flow this week? If not, how "
                     "far off or how late is it, and why?"},
    ]
    return {"question": q, "examples": examples}
