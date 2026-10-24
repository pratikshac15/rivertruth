"""Deterministic report guards: rules, applied in code, for mistakes the models were seen to repeat.

The fact-checker model catches open-ended contradictions; these rules catch three known failure modes for certain:
  1. naming places from a different river (the flagship's dams and gauges in a report about another gauge);
  2. describing a source that has NO data as reporting 0 cfs;
  3. reading "the river is below flood stage" as a vertical-datum difference.
Each removal or replacement is listed in the report's Fact-check section, so nothing changes silently.
"""
import re

FLAGSHIP_NAMES = ["Holt", "Northport", "Oliver", "Tuscaloosa", "Black Warrior", "TODA1", "18229923"]
_ZERO = re.compile(r"(\b0(\.0)?\s*(cfs|ft³/s|ft3/s|ft\^3/s)\b|\(0 vs\b|\b0 vs \d)")
_SENT = re.compile(r"(?<=[.;!?])\s+(?=[A-Z*(\[])")
_ARTICLE_AFTER = {"for", "from", "of", "at", "to", "by", "in", "on", "obtain", "request", "with", "below", "above",
                  "near", "upstream", "downstream", "verify", "check", "contact", "and", "or", "past", "through"}
_ITEM = re.compile(r"^(\s*)([-*]|\d+\.)\s+")


def _rules(site, snapshot):
    rules = []
    if not site.flagship:
        names = [n for n in FLAGSHIP_NAMES if n.lower() not in site.name.lower()]
        rules.append(("names a place on a different river",
                      lambda s: any(re.search(rf"\b{re.escape(n)}\b", s) for n in names), None))
    if "reports NO discharge" in snapshot:
        rules.append(("calls a source with no flow data 0 cfs", lambda s: bool(_ZERO.search(s)), None))
    m = re.search(r"Latest stage ([\d.]+) ft is ([\d.]+) ft below the NWS action stage \(([\d.]+) ft\)", snapshot)
    if m:
        fix = (f"Today's stage ({m.group(1)} ft) is {m.group(2)} ft below the NWS action stage ({m.group(3)} ft) because "
               "the river is below flood level; that is normal and is not evidence of a datum difference.")
        rules.append(("reads 'below flood stage' as a datum difference",
                      lambda s: "datum" in s.lower() and re.search(r"flood|threshold|action", s, re.I)
                      and not re.search(r"not evidence|no evidence|same sensor", s, re.I), fix))
    return rules


def _renumber(lines):
    out, n = [], 0
    for ln in lines:
        m = re.match(r"^(\s*)\d+\.\s+", ln)
        if m:
            n += 1
            ln = f"{m.group(1)}{n}. " + ln[m.end():]
        elif ln.strip():
            n = 0
        out.append(ln)
    return out


def _respell(text, site, applied):
    """'Holt Dam release records' in a report about another river -> 'dam release records' (keeps a useful sentence)."""
    if site.flagship:
        return text
    for n in FLAGSHIP_NAMES:
        if n.lower() in site.name.lower():
            continue
        pat = re.compile(rf"(\b\w+\s+)?\b(the )?{re.escape(n)}( Lock and Dam| Lock & Dam| Dam| reach| gauge)\b")

        def sub(m):
            before = m.group(1) or ""
            prev = before.strip().lower()
            thing = m.group(3).strip().lower().replace("lock & dam", "lock and dam")
            article = "the " if (m.group(2) or not prev or prev in _ARTICLE_AFTER) else ""
            new = f"{before}{article}{thing}"
            if not before and not m.group(2):
                new = new[0].upper() + new[1:] if m.start() == 0 or text[max(0, m.start() - 2):m.start()].strip() in (".", "") else new
            applied.append({"wrong": m.group(0).strip(), "right": new.strip(), "why": "rule: names a place on a different river"})
            return new
        text = pat.sub(sub, text)
    return text


def _drop_empty_sections(lines):
    out = []
    for i, ln in enumerate(lines):
        if ln.lstrip().startswith("## "):
            rest = lines[i + 1:]
            nxt = next((x for x in rest if x.strip()), "")
            if not nxt or nxt.lstrip().startswith("## "):
                continue
        out.append(ln)
    return out


def apply(text, site, snapshot):
    """Return (cleaned text, list of {wrong, right, why})."""
    rules = _rules(site, snapshot)
    if not rules:
        return text, []
    applied, out, used_fix = [], [], set()
    text = _respell(text, site, applied)
    section = ""
    for line in text.split("\n"):
        if line.lstrip().startswith("#") or not line.strip():
            if line.lstrip().startswith("#"):
                section = line.strip("# ").lower()
            out.append(line)
            continue
        m = _ITEM.match(line)
        prefix, body = (line[:m.end()], line[m.end():]) if m else ("", line)
        kept = []
        for sent in _SENT.split(body):
            hit = next((r for r in rules if r[1](sent)), None)
            if not hit:
                kept.append(sent)
                continue
            why, _, fix = hit
            if fix and why not in used_fix and ("why" in section or "answer" in section):
                used_fix.add(why)
                kept.append(fix)
                applied.append({"wrong": sent.strip(), "right": fix, "why": f"rule: {why}"})
            else:
                applied.append({"wrong": sent.strip(), "right": "", "why": f"rule: {why}"})
        body = " ".join(kept).strip()
        if not body or re.fullmatch(r"[*_\s—–-]*", body):
            continue
        out.append(prefix + body)
    return "\n".join(_drop_empty_sections(_renumber(out))).rstrip() + "\n", applied


def clean_claim(text, site, snapshot):
    return apply(text, site, snapshot)[0]


def strip_other_gauges(texts, site, known):
    """Drop critic notes that mention another river's gauge (code or name): such a note cannot be about this run's
    data, so it is a leak from another investigation (seen once in the logs) and must not reach the report."""
    own = {site.lid.upper(), site.name.lower()}
    marks = []
    for g in known:
        lid, name = str(g.get("lid", "")).upper(), str(g.get("name", "")).lower()
        if lid and lid not in own:
            marks.append(re.compile(rf"\b{re.escape(lid)}\b"))
        if name and name not in own and len(name) > 12:
            marks.append(re.compile(re.escape(name), re.I))
    if not site.flagship:
        marks += [re.compile(rf"\b{re.escape(n)}\b") for n in FLAGSHIP_NAMES if n.lower() not in site.name.lower()]
    kept, dropped = [], []
    for t in texts:
        (dropped if any(m.search(t) for m in marks) else kept).append(t)
    return kept, dropped


def numbers(text):
    """Numbers in a text, normalised (commas removed, 1.0 == 1); numbers below 10 are ignored (counts, list items)."""
    out = set()
    for m in re.findall(r"\d[\d,]*\.?\d*", text or ""):
        try:
            x = float(m.replace(",", "").rstrip("."))
        except ValueError:
            continue
        if x >= 10:
            out.add(f"{round(x):d}")
    return out
