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


def site_question(name):
    """The default question for any gauge the Scout registers."""
    return (f"At {name}, do the USGS gauge, the National Water Model and the NWS gauge agree about river flow this week? "
            "Where they disagree, why, and which should a forecaster trust?")
