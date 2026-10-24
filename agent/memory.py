"""RiverTruth on the Meterless World Model engine.

The World Model is the agents' shared, persistent world:
  * River topology: gauges, the model reach, data sources, and how they relate (upstream_of, measured_by...).
  * Each run: a `run` context holding hypotheses, checks, evidence and findings. The investigator and the critic
    both write hypothesis status as facts; the most recent wins and the full history is kept.
  * Memory across runs: what each run concluded is asserted as facts on the gauges, so the next run's planner
    starts from what earlier investigations learned.
Every write carries provenance (which agent, when, which run).
"""
import os
import re
from datetime import datetime, timezone

from world_model import WorldModel

from . import config

_world = None

GAUGES = [
    ("gauge", "usgs", "02465000", {"name": "Black Warrior River at Northport, AL", "agency": "USGS",
                                   "measures": "discharge (cfs) and pool stage (ft)", "zetaris_columns": "usgs_northport_*"}),
    ("gauge", "usgs", "02465005", {"name": "Black Warrior River at Oliver Lock and Dam (tailwater)", "agency": "USGS",
                                   "measures": "tailwater stage only (ft)", "zetaris_columns": "usgs_oliver_tailwater_ft"}),
    ("gauge", "nwps", "TODA1", {"name": "Black Warrior River at Oliver Lock and Dam", "agency": "NOAA NWS/NWPS",
                                "measures": "tailwater stage (ft) and flow (kcfs, rated)", "zetaris_columns": "nws_oliver_*"}),
    ("model_reach", "nwm", "18229923", {"name": "National Water Model reach 18229923", "agency": "NOAA OWP",
                                        "measures": "simulated flow (cfs)", "zetaris_columns": "nwm_cfs"}),
    ("dam", "usace", "oliver", {"name": "Oliver Lock and Dam"}),
    ("dam", "usace", "holt", {"name": "Holt Lock and Dam (hydropower)"}),
    ("rain_point", "openmeteo", "33.21,-87.59", {"name": "Open-Meteo rainfall at the gauge", "zetaris_columns": "rain_mm"}),
]
EDGES = [
    ("gauge:usgs:02465000", "upstream_of", "dam:usace:oliver"),
    ("dam:usace:holt", "upstream_of", "gauge:usgs:02465000"),
    ("gauge:usgs:02465005", "located_at", "dam:usace:oliver"),
    ("gauge:nwps:TODA1", "located_at", "dam:usace:oliver"),
    ("gauge:nwps:TODA1", "reports_same_stage_as", "gauge:usgs:02465005"),
    ("model_reach:nwm:18229923", "simulates", "dam:usace:oliver"),
]
NORTHPORT = "gauge:usgs:02465000"   # the flagship site's entity
_site_entity = NORTHPORT


def use_site(site):
    """Point this run's memory at the given site's gauge entity."""
    global _site_entity
    _site_entity = site.entity or NORTHPORT


def register_site(site):
    """Record a Scout-discovered gauge, its sources and its river neighbours in the World Model."""
    w = world()
    s = {"kind": "agent", "by": "scout", "at": _now(), "url": "rivertruth://scout"}
    w.upsert_entity(type="gauge", external_key={"system": "nwps", "id": site.lid},
                    attrs={**site.to_dict(), "agency": "NOAA NWS/NWPS", "registered_in_zetaris": site.view}, source=s)
    w.upsert_entity(type="gauge", external_key={"system": "usgs", "id": site.usgs_id},
                    attrs={"name": f"USGS {site.usgs_id}", "agency": "USGS"}, source=s)
    w.upsert_entity(type="model_reach", external_key={"system": "nwm", "id": site.reach_id},
                    attrs={"name": f"National Water Model reach {site.reach_id}", "agency": "NOAA OWP"}, source=s)
    w.relate(site.entity, "same_place_as", f"gauge:usgs:{site.usgs_id}", source=s, valid_from="2026-10-23T00:00:00Z")
    w.relate(f"model_reach:nwm:{site.reach_id}", "simulates", site.entity, source=s, valid_from="2026-10-23T00:00:00Z")
    if site.upstream:
        w.relate(f"gauge:nwps:{site.upstream}", "upstream_of", site.entity, source=s, valid_from="2026-10-23T00:00:00Z")
    if site.downstream:
        w.relate(site.entity, "upstream_of", f"gauge:nwps:{site.downstream}", source=s, valid_from="2026-10-23T00:00:00Z")


def known_sites():
    """Gauges the Scout has registered before (World Model registry)."""
    return [e["attrs"] for e in world().entities("gauge") if e["id"].startswith("gauge:nwps:") and e["attrs"].get("view")]


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def src(agent, run_id=None):
    # url = which agent in which run made the claim; it is part of the fact's content address, so two
    # agents asserting the same value at the same instant stay two distinct, attributable facts.
    return {"kind": "agent", "by": agent, "at": _now(), "runId": run_id, "url": f"rivertruth://{run_id}/{agent}"}


def world():
    global _world
    if _world is None:
        directory = os.environ.get("WORLD_DIR", "data/world")
        _world = WorldModel("rivertruth", storage="file", directory=directory)
        seed_topology(_world)
    return _world


def seed_topology(w):
    for type_, system, ext, attrs in GAUGES:
        if w.entity(f"{type_}:{system}:{ext}") is None:  # write once; topology is stable
            w.upsert_entity(type=type_, external_key={"system": system, "id": ext}, attrs=attrs,
                            source={"kind": "manual", "by": "setup", "at": "2026-10-23T00:00:00Z"})
    for f, t, to in EDGES:
        w.relate(f, t, to, source={"kind": "manual", "by": "setup", "at": "2026-10-23T00:00:00Z"},
                 valid_from="2026-10-23T00:00:00Z")  # content-addressed: re-adding is a no-op


# ------------------------------------------------------------------ per-run writes

def start_run(run_id, question):
    w = world()
    ctx = w.upsert_context(type="run", external_key={"system": "rivertruth", "id": run_id},
                           attrs={"question": question, "status": "running", "startedAt": _now()},
                           source=src("orchestrator", run_id))
    return ctx["id"]


def hyp_id(run_id, h):
    return f"hypothesis:{run_id}:{h}"


def record_hypotheses(run_id, run_ctx, hypotheses):
    w = world()
    for h in hypotheses:
        hid = w.upsert_entity(type="hypothesis", external_key={"system": run_id, "id": h["id"]},
                              attrs={"claim": h["claim"], "run": run_ctx}, source=src("planner", run_id))["id"]
        w.relate(hid, "part_of", _site_entity, context=run_ctx, source=src("planner", run_id))
        w.assert_fact(hid, "status", "open", source=src("planner", run_id))


def record_evidence(run_id, run_ctx, ev, check):
    w = world()
    eid = w.upsert_entity(type="evidence", external_key={"system": run_id, "id": ev.id},
                          attrs={"check": check["id"], "purpose": check["purpose"], "sql": ev.sql,
                                 "rows": len(ev.rows), "error": ev.error or None, "source": "zetaris"},
                          source=src("investigator", run_id))["id"]
    w.relate(eid, "tests", hyp_id(run_id, check["hypothesis"]), context=f"{run_ctx}|{check['id']}",
             source=src("investigator", run_id))
    return eid


def record_finding(run_id, finding):
    w = world()
    eid = f"evidence:{run_id}:{finding.evidence_id}"
    w.upsert_entity(type="evidence", external_key={"system": run_id, "id": finding.evidence_id},
                    attrs={"finding": finding.finding, "stance": finding.stance}, source=src("investigator", run_id))
    if finding.stance in ("supports", "refutes"):
        w.assert_fact(hyp_id(run_id, finding.hypothesis), "status",
                      "supported" if finding.stance == "supports" else "refuted",
                      confidence=0.6, source={**src("investigator", run_id), "evidence": eid})


def record_critic(run_id, round_no, verdict, status_map, issues):
    w = world()
    for h, status in (status_map or {}).items():
        if status in ("supported", "refuted", "unclear"):
            w.assert_fact(hyp_id(run_id, h), "status", status, confidence=0.8,
                          source={**src("critic", run_id), "round": round_no})
    w.upsert_context(type="run", external_key={"system": "rivertruth", "id": run_id},
                     attrs={f"critic_round_{round_no}": {"verdict": verdict, "issues": issues[:5]}},
                     source=src("critic", run_id))


def hypothesis_status(run_id, hypotheses):
    """The final word on each hypothesis comes from the World Model (latest fact wins), with who decided it."""
    w = world()
    out = []
    for h in hypotheses:
        hid = hyp_id(run_id, h["id"])
        hist = w.facts_about(hid, "status", include_superseded=True)
        cur = (w.entity(hid) or {}).get("attrs", {}).get("status", "open")
        decided_by = next((f["source"].get("by") for f in reversed(hist) if not f.get("supersededBy")), None)
        out.append({"id": h["id"], "claim": h["claim"], "status": cur, "decided_by": decided_by,
                    "history": [f"{f['source'].get('by')}:{f['value']}" for f in hist]})
    return out


def run_status(run_id):
    c = world().context(f"run:rivertruth:{run_id}")
    return (c or {}).get("attrs", {}).get("status")


def close_run(run_id, status, note=""):
    """Always called (try/finally): the run's context never stays 'running'."""
    world().upsert_context(type="run", external_key={"system": "rivertruth", "id": run_id},
                           attrs={"status": status, "finishedAt": _now(), **({"note": note[:300]} if note else {})},
                           source=src("orchestrator", run_id))


def finish_run(run_id, run_ctx, report, statuses):
    """Remember what this run concluded, on the gauge itself, for future runs."""
    w = world()
    answer = _first_sentences(report)
    supported = [s["claim"] for s in statuses if s["status"] == "supported"]
    refuted = [s["claim"] for s in statuses if s["status"] == "refuted"]
    w.assert_fact(_site_entity, "latest_explanation", {"run": run_id, "answer": answer}, confidence=0.7,
                  source=src("reporter", run_id))
    if supported:
        w.assert_fact(_site_entity, "supported_explanations", {"run": run_id, "claims": supported[:3]}, confidence=0.7,
                      source=src("reporter", run_id))
    if refuted:
        w.assert_fact(_site_entity, "ruled_out_explanations", {"run": run_id, "claims": refuted[:3]}, confidence=0.7,
                      source=src("reporter", run_id))


def _first_sentences(report, n=2):
    text = report.split("##", 2)[1] if report.count("##") >= 1 else report
    if report.count("##") >= 1:  # the section's first line is its heading ("For the public", "Answer"): not part of it
        text = re.sub(r"^[^\n]*\n", "", text.strip(), count=1)
    text = re.sub(r"[*_`#>|]", "", text).strip()
    parts = re.split(r"(?<=[.!?])\s+", text)
    return " ".join(parts[:n])[:500]


# ------------------------------------------------------------------ reads for prompts

def prior_knowledge(current_run_id=None, max_runs=3):
    """What earlier runs concluded (the World Model's memory), for the planner/critic/reporter prompts."""
    w = world()
    lines = []
    for pred in ("latest_explanation", "supported_explanations", "ruled_out_explanations"):
        gone = _retracted(_site_entity)
        facts = [f for f in w.facts_about(_site_entity, pred, include_superseded=True)
                 if (f["value"] or {}).get("run") not in gone | {current_run_id}][-max_runs:]
        for f in facts:
            v = f["value"]
            body = v.get("answer") or "; ".join(c[:200] for c in v.get("claims", []))
            body = body[:450]
            lines.append(f"- [{f['assertedAt'][:16]} run {v.get('run')}] {pred.replace('_', ' ')}: {body}")
    if _site_entity == NORTHPORT:
        header = ("RIVER TOPOLOGY (World Model): Holt Lock and Dam (hydropower) is upstream of the Northport gauge, "
                  "which is upstream of Oliver Lock and Dam; the NWS gauge TODA1 and USGS 02465005 both report Oliver's tailwater stage.")
    else:
        rels = []
        for e in w.neighbors_of(_site_entity, direction="in", via="upstream_of"):
            rels.append(f"upstream: {e['id'].split(':')[-1]} ({e['attrs'].get('name', 'not investigated yet')})")
        for e in w.neighbors_of(_site_entity, direction="out", via="upstream_of"):
            rels.append(f"downstream: {e['id'].split(':')[-1]} ({e['attrs'].get('name', 'not investigated yet')})")
        header = "RIVER TOPOLOGY (World Model): " + ("; ".join(rels) if rels else "no neighbouring gauges recorded yet.")
    if not lines:
        return header + "\nPRIOR INVESTIGATIONS (World Model memory): none yet."
    return header + "\nPRIOR INVESTIGATIONS (World Model memory; earlier conclusions that MAY BE WRONG: re-verify them with today's data, never copy them):\n" + "\n".join(lines)


# ------------------------------------------------------------------ River check (quick answers reuse what was learned)

_HEADING = re.compile(r"^\s*(for the public|answer)\s*[:\-]?\s*", re.I)


def _retracted(ent):
    """Runs whose stored conclusions were retracted for this gauge (they stay in the history)."""
    out = set()
    for f in world().facts_about(ent, "retracted_runs", include_superseded=True):
        out |= set((f["value"] or {}).get("runs") or [])
    return out


def retract_runs(site, runs, reason):
    """Retract earlier runs' conclusions for this gauge: the River check and the agents stop using them, and the
    World Model keeps both the conclusions and this retraction (with its reason) in its history."""
    w = world()
    ent = site.entity or NORTHPORT
    w.assert_fact(ent, "retracted_runs", {"runs": sorted(set(runs) | _retracted(ent)), "reason": reason}, confidence=1.0,
                  source={"kind": "human", "by": "maintainer", "at": _now(), "url": "rivertruth://maintenance"})


def retract_before(cutoff, reason):
    """Retract every stored conclusion asserted before `cutoff` (ISO time, UTC), on every gauge. Returns how many."""
    w = world()
    by_ent = {}
    for e in w.entities():
        for f in w.facts_about(e["id"], "latest_explanation", include_superseded=True):
            run = (f["value"] or {}).get("run")
            if run and f["assertedAt"][:16] < cutoff[:16] and run not in _retracted(e["id"]):
                by_ent.setdefault(e["id"], set()).add(run)
    for ent, runs in by_ent.items():
        w.assert_fact(ent, "retracted_runs", {"runs": sorted(runs | _retracted(ent)), "reason": reason}, confidence=1.0,
                      source={"kind": "human", "by": "maintainer", "at": _now(), "url": "rivertruth://maintenance"})
    return sum(len(r) for r in by_ent.values())


def learned(site):
    """The latest investigation's conclusion and the latest observed pattern for this gauge, if any."""
    w = world()
    ent = site.entity or NORTHPORT
    out = {}
    gone = _retracted(ent)
    exp = [f for f in w.facts_about(ent, "latest_explanation", include_superseded=True)
           if (f["value"] or {}).get("run") not in gone]
    if exp:
        v = exp[-1]["value"] or {}
        out["investigation"] = {"run": v.get("run"), "at": exp[-1]["assertedAt"][:16],
                                "answer": _HEADING.sub("", v.get("answer") or "")}
    pat = w.facts_about(ent, "observed_pattern")
    if pat:
        out["pattern"] = {**(pat[-1]["value"] or {}), "at": pat[-1]["assertedAt"][:16]}
    return out


def record_pattern(site, pattern):
    """Remember a recurring pattern seen at this gauge (latest wins, history kept)."""
    w = world()
    ent = site.entity or NORTHPORT
    if w.entity(ent) is None:
        return
    w.assert_fact(ent, "observed_pattern", pattern, confidence=0.7,
                  source={"kind": "agent", "by": "river_check", "at": _now(), "url": f"rivertruth://check/{site.lid}"})
