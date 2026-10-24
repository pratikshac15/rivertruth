"""Record (or replay) the web app's flows for sample mode: the whole-US map, River checks and charts for a few
gauges. Run by scripts/record_samples.sh with RECORD_SAMPLES=true; `--replay` runs the same flows in SAMPLE_MODE and
fails if anything is missing, so judges can open the web app with no Zetaris, no Nightshift and no credentials.

Gauges recorded: RT_SAMPLE_GAUGES (comma-separated NWS codes), default: a dam-release river, a stage-only USGS site,
a free-flowing river after rain, a flash flood, and a tide gauge.
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DEFAULT = "TODA1,AVLA1,GWSC2,MOEA3,PMAF1"
WHEN, ACTIVITY = "this evening", "kayaking or canoeing"


def _use_fakes():
    """Test hook: record against tests/fakes.py, writing the sample cache exactly as live recording would."""
    from agent import cache, config, llm, zetaris
    from tests.fakes import Env
    env = Env().__enter__()
    config.RECORD_SAMPLES = os.environ.get("RECORD_SAMPLES", "false").lower() == "true"  # Env turns it off for tests
    fake_sql, fake_chat = zetaris.run_sql, llm.chat

    def sql(q, limit=200, purpose="", agent=False):
        res = fake_sql(q, limit=limit, purpose=purpose, agent=agent)
        if config.RECORD_SAMPLES:
            cache.put("sql", q.strip().rstrip(";"), res)
        return res

    def chat(role, messages, model=None, **kw):
        text = fake_chat(role, messages, model=model, **kw)
        if config.RECORD_SAMPLES:
            key = json.dumps({"model": model or config.FAST_MODEL, "messages": messages}, sort_keys=True)
            cache.put("llm", key, {"text": text, "served_by": model or config.FAST_MODEL, "receipt": {}})
        return text
    zetaris.run_sql, llm.chat = sql, chat
    return env


def main():
    replay = "--replay" in sys.argv
    if os.environ.get("RT_FAKE_SERVICES"):
        _use_fakes()
        lids = os.environ.get("RT_SAMPLE_GAUGES", "TODA1,CKLA1,AVLA1,PMAF1")
    else:
        lids = os.environ.get("RT_SAMPLE_GAUGES", DEFAULT)
    from agent import config, gauges, memory, rivercheck, scout, zetaris
    if replay and not config.SAMPLE_MODE:
        sys.exit("--replay must run with SAMPLE_MODE=true")
    manifest = os.path.join(config.SAMPLE_DIR, "web_gauges.json")
    if replay and os.path.exists(manifest):  # replay exactly what was recorded
        with open(manifest) as f:
            lids = ",".join(json.load(f)["gauges"])
    print("Mode:", "replay (sample mode)" if replay else "record")
    us = gauges.load_all()[0]
    print(f"{len(us)} gauges on the map")
    if not us:
        sys.exit("no gauges")
    floods = {g[0]: g[4] for g in us}
    failures = 0
    for lid in [x.strip().upper() for x in lids.split(",") if x.strip()]:
        try:
            out = rivercheck.check(lid, WHEN, ACTIVITY, floods.get(lid, ""))
            site = scout.ensure_site(lid)
            rows = zetaris.run_sql(site.rows_sql(), purpose="ui hydrograph")["rows"]
            memory.use_site(site)
            memory.prior_knowledge()
            print(f"River check OK {lid}: {out['card']['headline'][:80]} ({len(rows)} hours charted, words: {out['written_by']})")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"River check FAILED {lid}: {type(e).__name__}: {str(e)[:200]}")
    if not replay and not failures:
        os.makedirs(config.SAMPLE_DIR, exist_ok=True)
        with open(manifest, "w") as f:
            json.dump({"gauges": [x.strip().upper() for x in lids.split(",") if x.strip()], "when": WHEN,
                       "activity": ACTIVITY}, f)
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
