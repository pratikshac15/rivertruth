"""The four agent roles. Each is a focused prompt + a structured JSON contract."""
import json
import re

from . import config, llm, schemas

SYSTEM = ("You are part of RiverTruth, a team of agents that explains WHY river data sources disagree, "
          "for flood forecasters and emergency managers. Be precise, quantitative and honest about uncertainty. "
          "Never invent numbers: every claim must come from query results. "
          "Answer directly and concisely; do not deliberate at length.")


def planner(case):
    prompt = f"""{case.catalog}

CURRENT DATA SNAPSHOT (from Zetaris, taken at the start of this run):
{case.snapshot}

{case.memory}

SQL RULES (any SQL you write in a check's "how" must follow them):
{_sql_rules(case)}QUESTION from the user: {case.question}

Act as the PLANNER. Propose 2-4 competing hypotheses that could explain the disagreement or behaviour asked about
(e.g. upstream dam/lock release, rainfall-driven runoff, unit or datum mismatch, different gauge location/measurement method,
model limitation, data latency or gaps). List at most 4 checks in total, the most decisive first; each must be answerable with SQL (prefer {case.view}).
Earlier conclusions in the World Model memory may be wrong: test them, but never copy them as your hypotheses;
write each hypothesis fresh from today's snapshot.
If the snapshot shows a repeating pattern (e.g. a peak at the same hour on several days), include a check that tests
that recurrence across all available days, because a recurring schedule is strong evidence about the cause.

Reply with ONLY this JSON:
{{"restated_question": "...",
  "hypotheses": [{{"id": "H1", "claim": "..."}}],
  "checks": [{{"id": "C1", "hypothesis": "H1", "purpose": "what this check establishes", "how": "which table/columns/time window"}}]}}"""
    return llm.chat_json("planner", [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
                         validate=schemas.planner,
                         model=config.DEEP_MODEL)


def full_local_days(win, tz):
    """The whole local days inside a UTC window: (start_utc, end_utc_exclusive, first_day, last_day)."""
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo
    z, fmt = ZoneInfo(tz), "%Y-%m-%d %H:%M"
    a = datetime.strptime(win[0], fmt).replace(tzinfo=timezone.utc).astimezone(z)
    b = datetime.strptime(win[1], fmt).replace(tzinfo=timezone.utc).astimezone(z)
    first = a.date() if (a.hour, a.minute) == (0, 0) else a.date() + timedelta(days=1)
    end = b.date()  # exclusive: the local day the window ends in is not complete

    def utc(d):
        return datetime(d.year, d.month, d.day, tzinfo=z).astimezone(timezone.utc).strftime(fmt)
    return utc(first), utc(end), first.isoformat(), (end - timedelta(days=1)).isoformat()


def _sql_rules(case):
    """The two things investigators most often got wrong in the logs: the time window and local time."""
    tz = getattr(case, "tz_iana", "America/Chicago")
    local = f"from_utc_timestamp(to_timestamp(hr_utc, 'yyyy-MM-dd HH:mm'), '{tz}')"
    win = getattr(case, "window", None)
    return ((f"TIME WINDOW: use only hr_utc BETWEEN '{win[0]}' AND '{win[1]}' (the last 7 days; older rows are "
             "outside this investigation).\n" if win else "") +
            _daily_rule(win, tz) +
            f"LOCAL TIME (never use the UTC hour as local; never date_sub/date_add, which shift DAYS): "
            f"local hour = hour({local}); local day = to_date({local}).\n\n")


def _daily_rule(win, tz):
    if not win:
        return ""
    a, b, first, last = full_local_days(win, tz)
    return (f"DAILY FIGURES (any per-day mean, max or count): use whole local days only, "
            f"hr_utc >= '{a}' AND hr_utc < '{b}' ({first} to {last} local), grouped by local day; a part-day mixes "
            "a few hours with a full day and gives wrong daily numbers.\n")


def write_sql(case, check, previous_error=None, previous_sql=None):
    fix = ""
    if previous_error:
        fix = f"\nYour previous SQL failed.\nSQL: {previous_sql}\nERROR: {previous_error[:500]}\nFix it."
    prompt = f"""{case.catalog}

DATA SNAPSHOT (for orientation; your query must still fetch the evidence):
{case.snapshot}

QUESTION: {case.question}
CHECK {check['id']} (for {check['hypothesis']}): {check['purpose']}
Suggested approach: {check.get('how', '')}

RULES (these rules override the suggested approach wherever they differ):
{_sql_rules(case)}{fix}

Act as the INVESTIGATOR. Write ONE Spark SQL query against the tables above that answers this check.
Reply with ONLY this JSON: {{"sql": "SELECT ...", "purpose": "one line"}}"""
    return llm.chat_json("investigator.sql", [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
                         validate=schemas.sql)


def interpret(case, check, ev):
    prompt = f"""QUESTION: {case.question}
HYPOTHESES: {json.dumps(case.hypotheses)}
CHECK {check['id']} (for {check['hypothesis']}): {check['purpose']}
QUERY RESULT {ev.id}: columns={ev.columns} rows={ev.rows[:40]}{' (truncated)' if len(ev.rows) > 40 else ''}

Act as the INVESTIGATOR. State what this result shows, with the key numbers. Decide whether it supports or refutes {check['hypothesis']}.
Reply with ONLY this JSON: {{"finding": "1-3 sentences with numbers", "stance": "supports|refutes|unclear"}}"""
    return llm.chat_json("investigator.interpret", [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
                         validate=schemas.interpret)


def critic(case, round_no):
    prompt = f"""{case.catalog}

DATA SNAPSHOT:
{case.snapshot}

{case.memory}

SQL RULES (any SQL you write in a check's "how" must follow them):
{_sql_rules(case)}QUESTION: {case.question}
HYPOTHESES: {json.dumps(case.hypotheses)}
FINDINGS:
{case.findings_brief()}
EVIDENCE:
{case.evidence_brief(max_rows=6)}

Act as the CRITIC (review round {round_no}). Challenge the investigation like a skeptical senior hydrologist:
- Is each finding actually supported by the rows (check numbers, units, time zones, kcfs vs cfs)?
- Does it contradict the GROUND-TRUTH FACTS in the snapshot (computed in code)? Those facts win: any finding that conflicts
  with them (wrong day/hour, a lag that differs, treating "NWM has no data" as a model miss) must be flagged and the
  affected hypothesis status corrected.
- Do any two findings, or a finding and its stance, contradict each other? Is a recurring pattern in the snapshot ignored?
- Is a conclusion from earlier runs (World Model memory) being repeated without checking it against today's data?
- Are there unsupported claims, missing comparisons, or an untested alternative explanation?
- Is there enough evidence to rank the hypotheses?
If something important is missing, request at most 2 NEW checks. Approve only if the explanation is evidence-backed.

Reply with ONLY this JSON:
{{"verdict": "approve|revise",
  "hypothesis_status": {{"H1": "supported|refuted|unclear"}},
  "issues": ["..."],
  "new_checks": [{{"id": "C?", "hypothesis": "H?", "purpose": "...", "how": "..."}}]}}"""
    return llm.chat_json("critic", [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
                         validate=_critic_validator(case),
                         model=config.DEEP_MODEL, attempts=1)


def reporter(case):
    site_line = ("SITE: Black Warrior River at Tuscaloosa and Northport, Alabama. The USGS Northport gauge is about 1 km "
                 "upstream of Oliver Lock and Dam (NWS gauge TODA1); Holt Lock and Dam (hydropower) is further upstream.\n"
                 ) if case.view == "water.hourly_compare" else (
        f"SITE: {case.catalog.splitlines()[1].split('Site: ', 1)[-1].strip()} Name only places, dams and gauges that appear in this prompt; "
        "do not bring in places from other rivers.\n")
    approved = bool(case.critiques) and case.critiques[-1].get("verdict") == "approve"
    verdict_line = ("CRITIC VERDICT: the critic approved the evidence.\n" if approved else
                    "CRITIC VERDICT: the critic did NOT approve this evidence. Confidence must be Medium or Low, and say "
                    "plainly what is still unresolved.\n")
    prompt = f"""{site_line}{verdict_line}QUESTION: {case.question}
HYPOTHESES (final status from the World Model, with which agent decided it): {json.dumps(case.hypotheses)}
{case.memory}
FINDINGS:
{case.findings_brief()}
CRITIC NOTES: {json.dumps(case.critiques[-1] if case.critiques else {})}
EVIDENCE:
{case.evidence_brief(max_rows=8)}

DATA SNAPSHOT:
{case.snapshot}

Act as the REPORTER. Give times in {getattr(case, "tz_desc", "Central time (CDT)")} with UTC in brackets.
Start with a "## For the public" section: two plain sentences, no jargon or abbreviations, saying what happened on the
river and why the sources disagreed. A cause that is inferred from the data rather than observed (for example a dam
release seen only in the flow pattern) is "most likely", never stated as fact. Use distances and place names only
from the SITE line and the data. Mention what it means for people on or near the water only if the evidence supports it. Write the answer for a flood forecaster in Markdown:
## For the public  (see above)
## Answer  (2-3 sentences: the most likely explanation, for a forecaster)
## Why the sources disagree  (ranked explanations; each bullet cites evidence ids like [E2] and gives the numbers)
## Confidence  (High/Medium/Low and why)
## What we could not confirm  (limitations, missing data)
## What to check next  (1-3 concrete actions)
Lead with the hypotheses whose final status is "supported". If today's data agrees or disagrees with earlier runs
(World Model memory), say so in one line. Write the report fresh from today's evidence: do not reuse wording from
earlier runs, and where an earlier conclusion conflicts with the GROUND-TRUTH FACTS, say the earlier run was wrong.
Only use numbers that appear in the evidence or the GROUND-TRUTH FACTS. Where a finding conflicts with the
GROUND-TRUTH FACTS (computed in code), the facts win; never count a day with no data from a source as that source failing,
not in the reasons and not in the confidence. Compare like with like: the NWM is hourly, so compare it with the USGS
HOURLY MEAN, not the 15-minute peak. Ignore partial days with no pulse."""
    return llm.chat("reporter", [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
                    model=config.DEEP_MODEL, max_tokens=10000)


def quick_critic(case, round_no):
    """Shorter critic prompt (no catalog, findings + snapshot only), used if the full critic times out."""
    prompt = f"""DATA SNAPSHOT:
{case.snapshot}

QUESTION: {case.question}
HYPOTHESES: {json.dumps(case.hypotheses)}
FINDINGS:
{case.findings_brief()}

Act as the CRITIC (round {round_no}). In a few lines: is each finding consistent with the snapshot (right day/hour, units)?
Which hypotheses are supported or refuted? Request at most 2 new checks only if essential.
Reply with ONLY JSON: {{"verdict": "approve|revise", "hypothesis_status": {{"H1": "supported|refuted|unclear"}},
"issues": ["..."], "new_checks": [{{"id": "C?", "hypothesis": "H?", "purpose": "...", "how": "use {case.view}"}}]}}"""
    return llm.chat_json("critic.quick", [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
                         validate=_critic_validator(case),
                         attempts=2)


def ground_truth(snapshot):
    """The code-computed part of the snapshot: facts + coverage (always correct by construction)."""
    m = re.search(r"GROUND-TRUTH FACTS.*?(?=\n\nDAILY SUMMARY|\Z)", snapshot, re.S)
    return m.group(0) if m else ""


def fact_checker(case):
    """Last line of defence: compare the finished report with the facts computed in code; return exact-text fixes.

    The model only proposes corrections; the code applies them verbatim, so a fact-check can never rewrite the report.
    """
    facts = ground_truth(case.snapshot)
    if not facts:
        return []
    places = ("" if case.view == "water.hourly_compare" else
              f"\n- names a dam, lock, city, reach or gauge that is not part of this site ({case.catalog.splitlines()[1].split('Site: ', 1)[-1].strip()}),"
              " e.g. a place from a different river")
    prompt = f"""FACTS (computed in code from the data; always correct):
{facts}

REPORT:
{case.report}

Act as the FACT-CHECKER. Find sentences or phrases in the REPORT that
- contradict the FACTS (a number, a time, or a source called 0 / missing / failing when the FACTS say otherwise),{places}
- or state a guess as if it were measured.
For each one, copy the wrong text EXACTLY as it appears in the REPORT (one phrase or sentence, not a whole section) and
write the corrected sentence, consistent with the FACTS. List ONLY text that is wrong: never list a correct sentence,
never delete, restyle or shorten. Most reports need 0-2 corrections. If nothing is wrong, return an empty list.

Reply with ONLY this JSON:
{{"corrections": [{{"wrong": "exact text from the report", "right": "replacement", "why": "which fact it contradicts"}}]}}"""
    out = llm.chat_json("fact_checker", [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
                        validate=schemas.fact_check,
                        model=config.FAST_MODEL, attempts=2)
    return [c for c in (out or {}).get("corrections", []) if isinstance(c, dict) and c.get("wrong")]


def _critic_validator(case):
    ids = {h["id"] for h in case.hypotheses}
    nxt = len(case.checks) + 1
    return lambda o: schemas.critic(o, ids, nxt)


def fallback_plan(case):
    """A safe default plan, used only when the planner's reply cannot be used after one repair attempt."""
    view = case.view
    return {"restated_question": case.question,
            "hypotheses": [{"id": "H1", "claim": "A regulated release (dam or lock operation) explains the flow changes."},
                           {"id": "H2", "claim": "Rainfall-driven runoff explains the flow changes."},
                           {"id": "H3", "claim": "The sources measure or model different things (location, rating, "
                                                 "model limits or publishing delays)."}],
            "checks": [{"id": "C1", "hypothesis": "H1", "purpose": "Daily peak flow and the hour it occurs, per source",
                        "how": f"{view}, group by day"},
                       {"id": "C2", "hypothesis": "H2", "purpose": "Total rain per day and flow on rainy days",
                        "how": f"{view}, rain_mm by day"},
                       {"id": "C3", "hypothesis": "H3", "purpose": "Hourly ratio between sources where both report",
                        "how": f"{view}, hours with two sources"}]}
