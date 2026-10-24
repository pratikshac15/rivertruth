"""World Model conformance tests.

wm-001..wm-008 are a check-for-check Python port of the official Meterless World Model conformance suite
(engines/world-model/conformance/runner.ts). rt-001..rt-003 cover what RiverTruth relies on:
two agents writing concurrently without losing changes, persistence across restarts, and the critic
overriding an investigator's hypothesis status with full history kept.

Run:  python3 scripts/world_model_conformance.py      (scorecard, no dependencies)
  or: python3 -m pytest tests/ -q
"""
import tempfile
import threading
from datetime import datetime, timezone

from world_model import WorldModel, decide, entity_id_from_name, normalize_name

OBSERVATIONS = [
    {"externalId": "a1", "title": "Fed holds rates", "at": "2026-05-17T14:00:00.000Z",
     "source": {"kind": "rss", "at": "2026-05-17T14:00:00.000Z"}, "entities": [{"type": "person", "name": "Jerome Powell"}]},
    {"externalId": "a2", "title": "Powell signals patience", "at": "2026-05-17T16:30:00.000Z",
     "source": {"kind": "rss", "at": "2026-05-17T16:30:00.000Z"}, "entities": [{"type": "person", "name": "Jerome Powell"}]},
]
CLOCK = lambda: datetime(2026, 5, 18, tzinfo=timezone.utc)  # noqa: E731


def man(at):
    return {"kind": "manual", "at": at}


# ---------------------------------------------------------------- official conformance (ported)

def test_wm001_stable_ids_normalize_diacritics_and_nbsp():
    """sid-001: diacritics and NBSP normalize identically (NFKD + strip marks)"""
    assert normalize_name("José  García") == normalize_name("Jose Garcia")
    assert entity_id_from_name("person", "José García") == entity_id_from_name("person", "jose garcia")


def test_wm002_distinct_names_stay_distinct():
    """sid-002: distinct names stay distinct (no over-normalization); type participates in the key"""
    assert entity_id_from_name("org", "Acme Corp") != entity_id_from_name("org", "Acme Corporation")
    assert entity_id_from_name("person", "x") != entity_id_from_name("org", "x")


def test_wm003_merge_bands_at_boundaries():
    """band-001: 0.85 auto-merge, 0.84 review, 0.82 review, 0.81 new"""
    assert decide(0.85) == "auto-merge"
    assert decide(0.84) == "review"
    assert decide(0.82) == "review"
    assert decide(0.81) == "new"


def test_wm004_writes_without_provenance_rejected():
    """prov-001: no provenance, no write"""
    w = WorldModel("conf")
    try:
        w.upsert_entity(type="person", name="No Provenance", source=None)
    except ValueError:
        return
    raise AssertionError("a write without provenance was accepted")


def test_wm005_ingest_idempotent():
    """idem-001: same batch twice produces an empty diff"""
    w = WorldModel("conf", clock=CLOCK)
    w.ingest(OBSERVATIONS)
    shape1 = w.state_shape_canonical()
    second = w.ingest(OBSERVATIONS)
    assert second["changed"] is False, "second run must report changed=False"
    assert w.state_shape_canonical() == shape1


def test_wm006_idempotency_check_can_fail():
    """idem-002: a mutated batch changes the shape"""
    w = WorldModel("conf", clock=CLOCK)
    w.ingest(OBSERVATIONS)
    shape1 = w.state_shape_canonical()
    mutated = [{**OBSERVATIONS[0], "title": OBSERVATIONS[0]["title"] + " (edited)"}, OBSERVATIONS[1]]
    run = w.ingest(mutated)
    assert run["changed"] is True
    assert w.state_shape_canonical() != shape1


def test_wm007_bitemporal_superseding_edges():
    """bitemp-001: a superseding edge closes the old one; point-in-time queries respect validity"""
    w = WorldModel("conf")
    w.upsert_entity(type="player", external_key={"system": "g", "id": "p1"}, attrs={}, source=man("2087-01-01T00:00:00.000Z"))
    w.upsert_entity(type="team", external_key={"system": "g", "id": "a"}, attrs={}, source=man("2087-01-01T00:00:00.000Z"))
    w.upsert_entity(type="team", external_key={"system": "g", "id": "b"}, attrs={}, source=man("2088-03-01T00:00:00.000Z"))
    w.relate("player:g:p1", "plays-for", "team:g:a", valid_from="2087-01-01T00:00:00.000Z", source=man("2087-01-01T00:00:00.000Z"))
    w.relate("player:g:p1", "plays-for", "team:g:b", valid_from="2088-03-01T00:00:00.000Z", source=man("2088-03-01T00:00:00.000Z"))
    now = w.neighbors_of("player:g:p1", via="plays-for")
    assert [e["id"] for e in now] == ["team:g:b"], f"open edge should point at team b, got {[e['id'] for e in now]}"
    then = w.neighbors_of("player:g:p1", via="plays-for", at="2087-06-01T00:00:00.000Z")
    assert [e["id"] for e in then] == ["team:g:a"], f"2087 query should return team a, got {[e['id'] for e in then]}"


def test_wm008_audit_in_order():
    """audit-001: every mutation writes an audit entry, seq strictly increasing"""
    w = WorldModel("conf")
    w.upsert_entity(type="x", name="Audit One", source=man("2026-01-01T00:00:00.000Z"))
    w.upsert_entity(type="x", name="Audit Two", source=man("2026-01-02T00:00:00.000Z"))
    audit = w.audit()
    assert len(audit) >= 2
    assert all(audit[i]["seq"] > audit[i - 1]["seq"] for i in range(1, len(audit)))


# ---------------------------------------------------------------- RiverTruth-specific

def test_rt001_two_agents_concurrently_no_lost_updates():
    """Two agents (investigator + critic) write to the same hypothesis at the same time; no update is lost."""
    w = WorldModel("rivertruth-test")
    hyp = w.upsert_entity(type="hypothesis", external_key={"system": "run1", "id": "H1"},
                          attrs={"claim": "daily hydropower release"}, source={"kind": "agent", "by": "planner", "at": "2026-10-23T01:00:00Z"})["id"]
    errors = []

    def agent(role, n):
        try:
            for i in range(n):
                w.upsert_entity(type="hypothesis", external_key={"system": "run1", "id": "H1"},
                                attrs={f"{role}_note_{i}": f"{role} wrote {i}"},
                                source={"kind": "agent", "by": role, "at": f"2026-10-23T02:{i:02d}:00Z"})
                w.assert_fact(hyp, f"{role}_check_{i}", "done", source={"kind": "agent", "by": role, "at": f"2026-10-23T02:{i:02d}:00Z"})
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=agent, args=("investigator", 25)), threading.Thread(target=agent, args=("critic", 25))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    attrs = w.entity(hyp)["attrs"]
    for role in ("investigator", "critic"):
        for i in range(25):
            assert attrs.get(f"{role}_note_{i}") == f"{role} wrote {i}", f"lost {role} note {i}"
            assert attrs.get(f"{role}_check_{i}") == "done", f"lost {role} fact {i}"
    seqs = [e["seq"] for e in w.events()]
    assert seqs == sorted(seqs) and len(seqs) == len(set(seqs)), "event log must be strictly ordered"


def test_rt002_persists_across_restarts():
    """The world survives a process restart: a second instance on the same file sees the same state."""
    with tempfile.TemporaryDirectory() as d:
        w1 = WorldModel("rt", storage="file", directory=d)
        w1.upsert_entity(type="gauge", external_key={"system": "usgs", "id": "02465000"},
                         attrs={"name": "Black Warrior River at Northport"}, source={"kind": "manual", "at": "2026-10-23T00:00:00Z"})
        w1.assert_fact("gauge:usgs:02465000", "daily_peak_hour_cdt", 21, confidence=0.9,
                       source={"kind": "agent", "by": "reporter", "at": "2026-10-23T06:00:00Z"})
        shape = w1.state_shape_canonical()
        w2 = WorldModel("rt", storage="file", directory=d)
        assert w2.state_shape_canonical() == shape
        assert w2.entity("gauge:usgs:02465000")["attrs"]["daily_peak_hour_cdt"] == 21


def test_rt003_critic_overrides_investigator_with_history():
    """most-recent-wins: the critic's later status lands on the hypothesis; the investigator's is kept, superseded."""
    w = WorldModel("rt")
    h = "hypothesis:run1:H2"
    w.assert_fact(h, "status", "supported", source={"kind": "agent", "by": "investigator", "at": "2026-10-23T02:00:00Z"})
    w.assert_fact(h, "status", "refuted", source={"kind": "agent", "by": "critic", "at": "2026-10-23T02:05:00Z"})
    assert w.entity(h)["attrs"]["status"] == "refuted"
    history = w.facts_about(h, "status", include_superseded=True)
    assert [f["source"]["by"] for f in history] == ["investigator", "critic"]
    assert history[0].get("supersededBy") == history[1]["id"]


def load_tests(loader, tests, pattern):
    """Let `python -m unittest` (no pytest needed) run the test_* functions above."""
    import unittest
    suite = unittest.TestSuite()
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            suite.addTest(unittest.FunctionTestCase(fn, description=name))
    return suite
