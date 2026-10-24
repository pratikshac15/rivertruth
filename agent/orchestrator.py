"""The agent loop: Planner -> Investigator (SQL on Zetaris, retries) -> Critic -> (revise loop) -> Reporter."""
import re
from datetime import datetime, timedelta
from . import guards, agents, config, log, memory, public, rivercheck, runctl, scout, snapshot, sqlguard, zetaris
from .catalog import catalog as site_catalog
from .state import CaseFile, Evidence, Finding


def say(msg):
    print(msg, flush=True)
    log.event("say", text=str(msg))


def run_check(case, check):
    sql, err = None, None
    for attempt in range(1, config.MAX_SQL_RETRIES + 2):
        try:
            q = agents.write_sql(case, check, previous_error=err, previous_sql=sql)
        except runctl.RunStopped:
            raise
        except Exception as e:  # noqa: BLE001  model unavailable: record and move on
            err = f"model call failed: {str(e)[:200]}"
            say(f"   {check['id']}: could not get SQL from the model ({str(e)[:80]})")
            break
        sql = (q.get("sql") or "").strip()
        log.event("step", agent="investigator", check=check["id"], attempt=attempt, sql=sql)
        try:
            sql = sqlguard.check(sql, [case.view], tz=getattr(case, "tz_iana", "America/Chicago"))  # read-only, this river only
            res = zetaris.run_sql(sql, purpose=check["purpose"], agent=True)
            ev = Evidence(case.next_evidence_id(), check["id"], check["purpose"], sql, res["columns"], res["rows"])
            case.evidence.append(ev)
            say(f"   {ev.id} <- {check['id']}: {len(ev.rows)} rows (attempt {attempt})")
            return ev
        except zetaris.SQLError as e:
            err = str(e)
            if isinstance(e, sqlguard.Rejected):
                log.event("sql_rejected", check=check["id"], sql=sql, reason=err)
                say(f"   {check['id']}: SQL rejected by the read-only gate (attempt {attempt}), asking for a new query")
            else:
                say(f"   {check['id']}: SQL failed (attempt {attempt}), retrying with the error")
    ev = Evidence(case.next_evidence_id(), check["id"], check["purpose"], sql or "", [], [], error=err or "failed")
    case.evidence.append(ev)
    return ev


def investigate(case, checks):
    for check in checks:
        runctl.check()
        say(f" > {check['id']} [{check['hypothesis']}] {check['purpose']}")
        ev = run_check(case, check)
        check["status"] = "failed" if ev.error else "done"
        _mem(memory.record_evidence, case.run_id, case.run_ctx, ev, check)
        if ev.error:
            case.findings.append(Finding(check["id"], check["hypothesis"], "Query could not be completed.", ev.id, "unclear"))
            continue
        try:
            interp = agents.interpret(case, check, ev)
        except runctl.RunStopped:
            raise
        except Exception as e:  # noqa: BLE001
            interp = {"finding": f"Result retrieved ({len(ev.rows)} rows) but could not be interpreted: {str(e)[:100]}",
                      "stance": "unclear"}
        f = Finding(check["id"], check["hypothesis"], interp.get("finding", ""), ev.id, interp.get("stance", "unclear"))
        case.findings.append(f)
        log.event("finding", **f.__dict__)
        _mem(memory.record_finding, case.run_id, f)
        say(f"   finding ({f.stance}): {f.finding}")


def _mem(fn, *args):
    """World Model write; a failure is logged loudly but never kills the investigation."""
    try:
        return fn(*args)
    except Exception as e:  # noqa: BLE001
        log.event("world_model_error", fn=fn.__name__, error=str(e)[:300])
        say(f"   [world model] {fn.__name__} failed: {str(e)[:100]}")
        return None


def _setup(case, question, site_lid):
    run_id = case.run_id
    site = scout.ensure_site(site_lid)
    case.site, case.view, case.catalog = site.lid, site.view, site_catalog(site)
    case.tz_desc = "Central time (CDT)" if site.flagship else f"local time at the gauge ({site.tz_now()[0]})"
    memory.use_site(site)
    log.event("site", site=site.to_dict())
    case.run_ctx = _mem(memory.start_run, run_id, question) or ""
    case.memory = _mem(memory.prior_knowledge, run_id) or ""
    say(f"WORLD MODEL: {memory.world().stats()}")
    say(case.memory + "\n")

    say("SNAPSHOT: reading the latest data from all sources in Zetaris...")
    case.snapshot = snapshot.build(site)
    m = re.search(r"LATEST DATA HOUR: (\d{4}-\d\d-\d\d \d\d:\d\d) UTC", case.snapshot)
    if m:  # the investigation window: the 7 days up to the newest hour in the data
        end = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M")
        case.window = ((end - timedelta(days=7)).strftime("%Y-%m-%d %H:%M"), m.group(1))
    case.tz_iana = site.tz
    say(case.snapshot.split("\n\n")[1] if "\n\n" in case.snapshot else case.snapshot)
    say("")

    return site


def _investigate(case, site):
    run_id = case.run_id
    statuses = None
    say("PLANNER: forming hypotheses...")
    try:
        plan = agents.planner(case)
    except runctl.RunStopped:
        raise
    except Exception as e:  # noqa: BLE001  unusable or unavailable planner: a safe default plan, said out loud
        say(f" planner reply unusable ({str(e)[:100]}); using the default plan")
        log.event("planner_fallback", error=str(e)[:300])
        plan = agents.fallback_plan(case)
    case.hypotheses = [{"id": h["id"], "claim": h["claim"], "status": "open"} for h in plan.get("hypotheses", [])]
    case.checks = [dict(c, status="todo") for c in plan.get("checks", [])][:config.MAX_INITIAL_CHECKS]
    log.event("step", agent="planner", hypotheses=case.hypotheses, checks=case.checks, memory=case.memory)
    _mem(memory.record_hypotheses, run_id, case.run_ctx, case.hypotheses)
    for h in case.hypotheses:
        say(f" {h['id']}: {h['claim']}")

    say("\nINVESTIGATOR: running checks on Zetaris...")
    investigate(case, case.checks)

    final_round = config.MAX_CRITIC_ROUNDS + 1  # one last review of the evidence gathered in the last revise round
    for rnd in range(1, final_round + 1):
        runctl.check()
        say(f"\nCRITIC: review round {rnd}...")
        try:
            try:
                c = agents.critic(case, rnd)
            except runctl.RunStopped:
                raise
            except Exception:  # noqa: BLE001  full critic timed out: try the compact one
                say(" full critic timed out; running the compact critic")
                c = agents.quick_critic(case, rnd)
        except runctl.RunStopped:
            raise
        except Exception as e:  # noqa: BLE001
            say(f" critic unavailable ({str(e)[:80]}); continuing to report")
            case.critiques.append({"round": rnd, "verdict": "unavailable", "issues": [str(e)[:200]]})
            break
        status = c.get("hypothesis_status", {}) or {}
        _mem(memory.record_critic, run_id, rnd, c.get("verdict"), status, c.get("issues", []))
        for h in case.hypotheses:
            h["status"] = status.get(h["id"], h["status"])
        issues, leaked = guards.strip_other_gauges(c["issues"], site, memory.known_sites())
        if leaked:
            log.event("leak_guard", round=rnd, dropped=leaked)
            say(f" dropped {len(leaked)} critic note(s) that mention another river's gauge")
        c["issues"] = issues
        case.critiques.append({"round": rnd, "verdict": c.get("verdict"), "issues": issues,
                               "new_checks": c.get("new_checks", [])})
        log.event("step", agent="critic", round=rnd, verdict=c.get("verdict"), issues=c.get("issues"),
                  hypothesis_status=status, new_checks=c.get("new_checks"))
        for i in c.get("issues", []):
            say(f" issue: {i}")
        new = [dict(x, status="todo") for x in c["new_checks"]][:config.MAX_NEW_CHECKS]  # ids already unique (schemas)
        if c.get("verdict") == "approve" or not new or rnd == final_round:
            say(f" verdict: {c.get('verdict')}")
            break
        say(f" verdict: revise -> {len(new)} new checks")
        case.checks.extend(new)
        investigate(case, new)

    statuses = _mem(memory.hypothesis_status, run_id, case.hypotheses)
    if statuses:
        log.event("hypothesis_status", statuses=statuses)
        case.hypotheses = [{k: s[k] for k in ("id", "claim", "status", "decided_by")} for s in statuses]
        say("\nWORLD MODEL hypothesis status (latest fact wins):")
        for s in statuses:
            say(f" {s['id']}: {s['status']} (decided by {s['decided_by']}; history {' -> '.join(s['history'])})")

    return statuses


def _report(case, site, statuses, stopped=None):
    run_id = case.run_id
    say("\nREPORTER: writing the explanation...")
    try:
        if stopped:
            raise runctl.RunStopped(stopped)
        runctl.check(min_seconds=5)
        case.report = agents.reporter(case)
    except runctl.RunStopped as e:
        stopped = stopped or e.reason
        say(f" run {STOP_TEXT[stopped]}; writing the findings so far without a model")
        case.report = _stopped_report(case, stopped)
    except Exception as e:  # noqa: BLE001  fall back to a plain evidence summary
        say(f" reporter unavailable ({str(e)[:80]}); writing summary from findings")
        case.report = ("## Answer\n(Model unavailable for the final write-up; findings below are from the investigation.)\n\n"
                       "## Findings\n" + case.findings_brief() + "\n\n## Hypothesis status\n" +
                       "\n".join(f"- {h['id']} ({h['status']}): {h['claim']}" for h in case.hypotheses))
    case.report = _ensure_sections(case.report)
    case.report, case.public_from_code = _public_from_facts(case.report, site)
    approved = bool(case.critiques) and case.critiques[-1].get("verdict") == "approve"
    if not stopped and not approved:
        case.report = _cap_confidence(case.report, case.critiques)
    say("\nFACT-CHECKER: rules in code, then the model, against the facts computed in code...")
    # 1) deterministic rules first (known failure modes), so the model checks an already-clean report
    case.report, applied = guards.apply(case.report, site, case.snapshot)
    for a in applied:
        say(f" rule: {a['why'][6:]}: " + (f"\"{a['wrong'][:80]}\" -> \"{a['right'][:80]}\"" if a["right"]
                                             else f"removed \"{a['wrong'][:90]}\""))
    # 2) the model proposes exact-text replacements; code applies only well-formed, non-destructive ones
    try:
        fixes = [] if stopped else agents.fact_checker(case)
    except Exception as e:  # noqa: BLE001  a failed fact-check (or the deadline) must not lose the report
        fixes = []
        say(f" fact-check model unavailable ({str(e)[:80]})")
    budget = 3  # a fact-check that wants to change many things is more likely wrong than the report
    for c in fixes:
        wrong, right, why = str(c["wrong"]).strip(), str(c.get("right") or "").strip(), str(c.get("why", ""))
        ok = (wrong and right and wrong in case.report and wrong != right and len(right) >= 0.5 * len(wrong)
              and not re.search(r"no correction|is (factually )?correct|correct per|consistent with|not contradict|phrasing|wording|"
                                r"more precise|exact (measurement|value|figure)|imprecise|style|no contradiction|"
                                r"for completeness", why, re.I)
              and not _only_a_note(wrong, right))
        pub = re.search(r"## For the public.*?(?=\n## |\Z)", case.report, re.S)
        if ok and getattr(case, "public_from_code", False) and pub and wrong in pub.group(0):
            ok = False  # written in code from the computed facts: the model may not edit it
        if ok:
            known = guards.numbers(case.snapshot)
            dropped = guards.numbers(wrong) - guards.numbers(right)
            added = guards.numbers(right) - guards.numbers(wrong)
            evidence = guards.numbers(case.evidence_brief(max_rows=50))
            if dropped & known or added - known - evidence:
                log.event("fact_check_refused", wrong=wrong[:200], right=right[:200],
                          why="drops a number the computed facts confirm" if dropped & known else "adds an unsupported number")
                ok = False
        if not ok or budget == 0:
            continue
        budget -= 1
        case.report = case.report.replace(wrong, right, 1)
        applied.append({"wrong": wrong, "right": right, "why": why})
        say(f" corrected: \"{wrong[:90]}\" -> \"{right[:90]}\"")
    if statuses:  # what goes into memory obeys the same rules, so a mistake is not remembered
        for st in statuses:
            st["claim"] = guards.clean_claim(st["claim"], site, case.snapshot) or st["claim"]
    if applied:
        case.report += "\n\n## Fact-check\n" + "\n".join(
            f"- Corrected \"{a['wrong']}\" to \"{a['right'] or '(removed)'}\": {a['why']}" for a in applied)
    else:
        say(" no contradictions with the computed facts")
    log.event("fact_check", proposed=len(fixes), applied=applied)
    log.event("report", text=case.report)
    if not stopped and approved:  # only conclusions the critic approved become memory for later runs
        _mem(memory.finish_run, run_id, case.run_ctx, case.report, statuses or [])
    elif not stopped:
        say(" not stored as memory: the critic did not approve this run's conclusions")
    log.event("world_model", stats=memory.world().stats())
    log.event("run_complete", run_id=run_id, report=case.report, status=stopped or "complete")
    return case


REQUIRED = ("For the public", "Answer", "Confidence")


def _ensure_sections(report):
    """The report always has the sections the page and the memory rely on."""
    r = (report or "").strip()
    if "## Answer" not in r:
        r = "## Answer\n" + r
    if "## For the public" not in r:
        r = "## For the public\nSee the answer below; this report did not include a plain-language summary.\n\n" + r
    if "## Confidence" not in r:
        r += "\n\n## Confidence\nLow: the report did not state its confidence."
    return r


def _public_from_facts(report, site):
    """Replace the model's public summary with one written in code from the River check facts (when available)."""
    try:
        text = public.summary(rivercheck.compute(site))
    except Exception as e:  # noqa: BLE001  keep the reporter's text, said in the log
        log.event("public_summary_error", error=str(e)[:200])
        return report, False
    if not text:
        return report, False
    head, rest = report.split("## For the public", 1)
    old_body, tail = (rest.split("\n## ", 1) + [""])[:2]
    log.event("public_summary", model_text=old_body.strip()[:600], code_text=text)
    return head + "## For the public\n" + text + "\n" + (("\n## " + tail) if tail else ""), True


def _only_a_note(wrong, right):
    """True when the 'correction' keeps the text and only appends a remark in brackets, e.g. '(The FACTS confirm this.)'.
    Saying the text is fine is not a correction."""
    if not right.startswith(wrong.rstrip(".")):
        return False
    extra = right[len(wrong.rstrip(".")):].strip(" .")
    return bool(re.fullmatch(r"\(.*\)\.?", extra, re.S)) or bool(
        re.search(r"(?i)\b(FACTS|ground[- ]truth)\b.*\b(confirm|support|agree|no contradiction)", extra))


def _cap_confidence(report, critiques):
    """Without the critic's approval, confidence is at most Medium, and the critic's open points are listed."""
    head, conf = report.split("## Confidence", 1)
    body, rest = (conf.split("\n## ", 1) + [""])[:2]
    lead = re.match(r"\A(\s*[*_]*)(High|Medium|Low)\b", body, re.I)  # only the rating at the start of the section
    if lead and lead.group(2).lower() == "high":
        body = body[:lead.start(2)] + "Medium" + body[lead.end(2):]
    elif not lead:
        body = "\n**Medium**" + body
    body = body.rstrip() + ("\nCapped at Medium: the critic did not approve the evidence; its open points are listed "
                            "under \"What the critic still questioned\".\n")
    last = critiques[-1] if critiques else {}
    items = [f"- {i}" for i in last.get("issues", [])] + [f"- Not checked: {x['purpose']}" for x in last.get("new_checks", [])]
    open_part = "\n## What the critic still questioned\n" + ("\n".join(items) or "- The critic did not give reasons.") + "\n"
    return head + "## Confidence" + body + open_part + (("\n## " + rest) if rest else "")


STOP_TEXT = {"timed_out": "reached its time limit", "cancelled": "was cancelled"}


def _stopped_report(case, reason):
    why = STOP_TEXT.get(reason, "stopped early")
    return (f"## For the public\nThis investigation {why} before it finished, so it gives no conclusion. "
            "The findings gathered so far are listed below.\n\n"
            f"## Answer\nNo answer: the run {why} ({reason.replace('_', ' ')}). The findings below have not all been "
            "reviewed by the critic.\n\n## Findings so far\n" + (case.findings_brief() or "(none yet)") +
            "\n\n## Confidence\nLow: the investigation did not finish.")


def run(question, site_lid=None):
    run_id, path = log.start_run()
    runctl.start(config.RUN_DEADLINE_S)
    say(f"RiverTruth run {run_id}  (log: {path})\nQ: {question}\n")
    case = CaseFile(question=question, run_id=run_id)
    status, note = "failed", ""
    try:
        site = _setup(case, question, site_lid)
        stopped, statuses = None, None
        try:
            statuses = _investigate(case, site)
        except runctl.RunStopped as e:
            stopped = e.reason
            say(f"\nSTOPPED: the run {STOP_TEXT[stopped]}.")
        _report(case, site, statuses, stopped)
        status = stopped or "complete"
        return case
    except runctl.RunStopped as e:  # stopped during setup or reporting
        status = e.reason
        case.report = case.report or _stopped_report(case, e.reason)
        log.event("run_complete", run_id=run_id, report=case.report, status=status)
        return case
    except Exception as e:
        note = f"{type(e).__name__}: {str(e)[:250]}"
        log.event("run_error", error=note)
        raise
    finally:
        _mem(memory.close_run, run_id, status, note)  # the run context never stays "running"
        runctl.start(None)
