"""World Model: event-sourced canonical store + write API + read views (spec: AGENTS.md sections 2-11).

Single-writer discipline: every write takes one lock, so concurrent agents serialize instead of
overwriting each other (spec 11.1). Every write must carry provenance {kind, at, by?} or it is rejected.
State is a fold over the append-only event log; storage="file" persists the log as JSON so the world
survives restarts (and every derived view can be rebuilt from it).
"""
import copy
import json
import os
import tempfile
import threading
from datetime import datetime, timedelta, timezone

from .ids import (canonical_json, context_id_from_attrs, entity_id_from_name, external_id, fact_id,
                  relationship_id, sha256_hex)
from .resolve import decide, name_similarity

CLUSTER_JACCARD = 0.5


def _iso(at) -> str:
    if isinstance(at, datetime):
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        return at.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return str(at)


def _jaccard(a, b) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    return inter / (len(sa) + len(sb) - inter)


class _Store:
    def __init__(self, namespace, storage, directory, conflict_policy="most-recent-wins"):
        self.namespace = namespace
        self.conflict_policy = conflict_policy
        self.events, self.audit, self.merge_queue = [], [], []
        self.seq = 0
        self.audit_seq = 0
        self.entities, self.contexts, self.relationships, self.facts, self.aliases = {}, {}, {}, {}, {}
        self.observations, self.stories = {}, {}
        self.file = None
        if storage == "file":
            os.makedirs(directory or ".", exist_ok=True)
            self.file = os.path.join(directory or ".", f"{namespace}.world.json")
            if os.path.exists(self.file):
                with open(self.file, encoding="utf-8") as f:
                    saved = json.load(f)
                for e in saved.get("events", []):
                    self.events.append(e)
                    self.seq = max(self.seq, e["seq"])
                    self._fold(e)
                self.audit = saved.get("audit", [])
                self.audit_seq = max([a["seq"] for a in self.audit], default=0)
                self.observations = dict(saved.get("observations", {}))
                self.stories = dict(saved.get("stories", {}))
                self.merge_queue = saved.get("mergeQueue", [])
        elif storage != "memory":
            raise ValueError("storage must be 'memory' or 'file'")

    # ---- event log
    def append(self, kind, payload, at):
        self.seq += 1
        event = {"id": f"evt_{self.seq}", "seq": self.seq, "kind": kind, "payload": copy.deepcopy(payload),
                 "schemaVersion": 1, "at": at}
        self.events.append(event)
        self._fold(event)
        return event

    def _fold(self, event):
        k, p = event["kind"], event["payload"]
        if k == "upsertEntity":
            ex = self.entities.get(p["id"])
            if ex:
                ex["attrs"] = {**ex["attrs"], **p["attrs"]}
                ex["updatedAt"], ex["source"] = p["updatedAt"], p["source"]
                if p.get("name"):
                    if p["name"] not in ex["aliases"]:
                        ex["aliases"].append(p["name"])
                    ex["name"] = p["name"]
                if p.get("state"):
                    ex["state"] = p["state"]
            else:
                self.entities[p["id"]] = copy.deepcopy(p)
        elif k == "upsertContext":
            ex = self.contexts.get(p["id"])
            if ex:
                ex["attrs"] = {**ex["attrs"], **p["attrs"]}
                ex["updatedAt"] = p["updatedAt"]
            else:
                self.contexts[p["id"]] = copy.deepcopy(p)
        elif k == "relate":
            # Superseding: a new open edge with the same (from, type, context) but a different target
            # closes the previous open edge (bitemporal validity).
            for other in self.relationships.values():
                if (other["from"] == p["from"] and other["type"] == p["type"]
                        and (other.get("context") or "") == (p.get("context") or "")
                        and other["to"] != p["to"] and not other.get("validTo") and not other.get("supersededBy")):
                    other["validTo"] = p.get("validFrom") or p["createdAt"]
                    other["supersededBy"] = p["id"]
            self.relationships.setdefault(p["id"], copy.deepcopy(p))
        elif k == "assertFact":
            # Conflict resolution happens in the fold, so replaying the log rebuilds the same winner.
            if p["id"] in self.facts:
                return
            fact = copy.deepcopy(p)
            rivals = [x for x in self.facts.values()
                      if x["about"] == fact["about"] and x["predicate"] == fact["predicate"] and not x.get("supersededBy")]
            self.facts[fact["id"]] = fact
            winner = fact
            for r in rivals:
                better = (r["confidence"] > winner["confidence"] if self.conflict_policy == "highest-confidence-wins"
                          else r["assertedAt"] > winner["assertedAt"])
                if better:
                    winner = r
            for x in [fact, *rivals]:
                if x["id"] != winner["id"]:
                    x["supersededBy"] = winner["id"]
            ent = self.entities.get(fact["about"])
            if ent is not None:
                ent["attrs"][fact["predicate"]] = winner["value"]
                ent["updatedAt"] = max(ent["updatedAt"], fact["assertedAt"])
        elif k == "alias":
            self.aliases[p["aliasId"]] = p["primaryId"]

    def resolve_alias(self, id_):
        seen = set()
        while id_ in self.aliases and id_ not in seen:
            seen.add(id_)
            id_ = self.aliases[id_]
        return id_

    def log_audit(self, kind, actor, summary, event_id=None, run_id=None, at=None):
        self.audit_seq += 1
        self.audit.append({"seq": self.audit_seq, "at": at or "1970-01-01T00:00:00.000Z", "kind": kind,
                           "actor": actor, "summary": summary, "eventId": event_id, "runId": run_id})

    def persist(self):
        if not self.file:
            return
        data = {"events": self.events, "audit": self.audit, "observations": self.observations,
                "stories": self.stories, "mergeQueue": self.merge_queue}
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.file) or ".", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, default=str)
        os.replace(tmp, self.file)  # atomic: a crash never leaves a half-written world


class WorldModel:
    def __init__(self, namespace, storage="memory", directory=None, clock=None, conflict_policy="most-recent-wins",
                 auto_merge_at=0.85, review_lower=0.82):
        if not namespace:
            raise ValueError("WorldModel: namespace is required")
        self._s = _Store(namespace, storage, directory, conflict_policy)
        self._lock = threading.RLock()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.conflict_policy = conflict_policy
        self.auto_merge_at, self.review_lower = auto_merge_at, review_lower

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _prov(source):
        if not source or not source.get("kind") or source.get("at") is None:
            raise ValueError("World Model: every write carries provenance (kind + at). No provenance, no write.")
        return {**source, "at": _iso(source["at"])}

    def _commit(self):
        self._s.persist()

    # ------------------------------------------------------------------ writes
    def upsert_entity(self, type, source, name=None, external_key=None, attrs=None, state=None):  # noqa: A002
        with self._lock:
            src = self._prov(source)
            name = name or (attrs or {}).get("name")
            if external_key:
                id_ = external_id(type, external_key["system"], external_key["id"])
            else:
                if not name:
                    raise ValueError("World Model: a name-keyed entity needs a name")
                id_ = entity_id_from_name(type, name)
                for other in list(self._s.entities.values()):
                    if other["type"] != type or other["id"] == id_ or not other.get("name"):
                        continue
                    sim = name_similarity(name, other["name"])
                    d = decide(sim, self.auto_merge_at, self.review_lower)
                    if d == "auto-merge":
                        self._s.append("alias", {"aliasId": id_, "primaryId": other["id"], "mergedAt": src["at"],
                                                 "mergedBy": "resolver", "reason": f"similarity {sim:.3f}"}, src["at"])
                        self._s.log_audit("auto-merge", "resolver", f"{name} -> {other['name']} ({sim:.3f})", at=src["at"])
                        return self._fold_entity(other["id"], type, attrs, src, name, state)
                    if d == "review" and not any({p["leftId"], p["rightId"]} == {id_, other["id"]} for p in self._s.merge_queue):
                        self._s.merge_queue.append({"id": f"merge_{len(self._s.merge_queue) + 1}", "leftId": id_,
                                                    "rightId": other["id"], "leftName": name, "rightName": other["name"],
                                                    "similarity": sim, "status": "pending", "proposedAt": src["at"]})
                        self._s.log_audit("merge-proposed", "resolver", f"{name} ~ {other['name']} ({sim:.3f})", at=src["at"])
            return self._fold_entity(id_, type, attrs, src, name, state)

    def _fold_entity(self, id_, type_, attrs, src, name, state):
        rid = self._s.resolve_alias(id_)
        ent = {"type": type_, "id": rid, "attrs": dict(attrs or {}), "schemaVersion": 1, "source": src,
               "createdAt": src["at"], "updatedAt": src["at"], "name": name, "aliases": [name] if name else [],
               "state": state or "active"}
        ev = self._s.append("upsertEntity", ent, src["at"])
        self._s.log_audit("upsertEntity", src.get("by", "system"), f"{rid} ({type_})", ev["id"], src.get("runId"), src["at"])
        self._commit()
        return copy.deepcopy(self._s.entities[rid])

    def upsert_context(self, type, source, external_key=None, attrs=None, parent=None):  # noqa: A002
        with self._lock:
            src = self._prov(source)
            id_ = (external_id(type, external_key["system"], external_key["id"]) if external_key
                   else context_id_from_attrs(type, attrs or {}, parent))
            ctx = {"type": type, "id": id_, "attrs": dict(attrs or {}), "parent": parent, "schemaVersion": 1,
                   "source": src, "createdAt": src["at"], "updatedAt": src["at"]}
            ev = self._s.append("upsertContext", ctx, src["at"])
            self._s.log_audit("upsertContext", src.get("by", "system"), f"{id_} ({type})", ev["id"], src.get("runId"), src["at"])
            self._commit()
            return copy.deepcopy(self._s.contexts[id_])

    def relate(self, from_, type, to, source, context=None, attrs=None, valid_from=None, valid_to=None):  # noqa: A002
        with self._lock:
            src = self._prov(source)
            f, t = self._s.resolve_alias(from_), self._s.resolve_alias(to)
            rid = relationship_id(f, type, t, context, valid_from)
            if rid in self._s.relationships:
                return copy.deepcopy(self._s.relationships[rid])  # content-addressed no-op
            rel = {"id": rid, "from": f, "type": type, "to": t, "context": context, "attrs": attrs,
                   "validFrom": valid_from or src["at"], "validTo": valid_to, "schemaVersion": 1, "source": src,
                   "createdAt": src["at"]}
            ev = self._s.append("relate", rel, src["at"])
            self._s.log_audit("relate", src.get("by", "system"), f"{f} -[{type}]-> {t}", ev["id"], src.get("runId"), src["at"])
            self._commit()
            return copy.deepcopy(self._s.relationships[rid])

    def assert_fact(self, about, predicate, value, source, confidence=0.5, context=None):
        """Facts are append-only claims; the conflict policy picks which value lands on the entity."""
        with self._lock:
            src = self._prov(source)
            about = self._s.resolve_alias(about)
            fid = fact_id(about, predicate, value, src["at"], src["kind"], src.get("url"))
            if fid in self._s.facts:
                return copy.deepcopy(self._s.facts[fid])
            if about not in self._s.entities:  # subject must exist before the fact folds onto it
                inferred = about.split(":")[0] if ":" in about else "entity"
                self._s.append("upsertEntity", {"type": inferred, "id": about, "attrs": {}, "schemaVersion": 1,
                                                "source": src, "createdAt": src["at"], "updatedAt": src["at"],
                                                "name": None, "aliases": [], "state": "emerging"}, src["at"])
            fact = {"id": fid, "about": about, "predicate": predicate, "value": value, "confidence": confidence,
                    "context": context, "source": src, "assertedAt": src["at"]}
            ev = self._s.append("assertFact", fact, src["at"])
            winner = self._s.facts[self._s.facts[fid].get("supersededBy") or fid]
            self._s.log_audit("assertFact", src.get("by", "system"),
                              f"{about}.{predicate} = {json.dumps(winner['value'], default=str)[:120]} ({self.conflict_policy})",
                              ev["id"], src.get("runId"), src["at"])
            self._commit()
            return copy.deepcopy(self._s.facts[fid])

    # ------------------------------------------------------------------ ingest (idempotent)
    def ingest(self, observations):
        """Idempotent ingest: re-running the same batch leaves the canonical state unchanged (changed=False).
        observations: [{externalId, title, text?, at, source{kind,at}, type?, entities:[{type,name}]}]"""
        with self._lock:
            before = self.state_shape_canonical()
            processed = skipped = 0
            for obs in observations:
                checksum = sha256_hex(canonical_json({"externalId": obs["externalId"], "title": obs.get("title"),
                                                      "text": obs.get("text") or "", "at": obs["at"]}))
                rec = self._s.observations.get(obs["externalId"])
                if rec and rec["checksum"] == checksum:
                    skipped += 1
                    continue
                src = self._prov({**obs["source"], "at": obs["at"]})
                otype = obs.get("type") or "item"
                ent = self._fold_entity(external_id(otype, "ingest", obs["externalId"]), otype,
                                        {"title": obs.get("title"), "text": obs.get("text")}, src, obs.get("title"), None)
                mentioned = []
                for m in obs.get("entities") or []:
                    mid = entity_id_from_name(m["type"], m["name"])
                    self._fold_entity(mid, m["type"], None, src, m["name"], None)
                    mid = self._s.resolve_alias(mid)
                    mentioned.append(mid)
                    rid = relationship_id(ent["id"], "mentions", mid)
                    if rid not in self._s.relationships:
                        self._s.append("relate", {"id": rid, "from": ent["id"], "type": "mentions", "to": mid,
                                                  "validFrom": src["at"], "schemaVersion": 1, "source": src,
                                                  "createdAt": src["at"]}, src["at"])
                self._s.observations[obs["externalId"]] = {
                    "id": "obs_" + sha256_hex(obs["externalId"])[:12], "externalId": obs["externalId"],
                    "entityId": ent["id"], "checksum": checksum, "at": obs["at"], "entityIds": sorted(mentioned)}
                processed += 1
            self._rebuild_stories_and_aggregates()
            after = self.state_shape_canonical()
            run_id = "run_" + sha256_hex(canonical_json([o["externalId"] for o in observations]))[:12]
            self._s.log_audit("ingest", "pipeline", f"run {run_id}: {processed} processed, {skipped} skipped",
                              run_id=run_id, at=observations[0]["at"] if observations else None)
            self._commit()
            return {"runId": run_id, "processed": processed, "skippedUnchanged": skipped, "changed": before != after}

    def _rebuild_stories_and_aggregates(self):
        allrec = sorted(self._s.observations.values(), key=lambda r: r["externalId"])
        used, stories = set(), {}
        for seed in allrec:
            if seed["externalId"] in used:
                continue
            group = [seed]
            used.add(seed["externalId"])
            for other in allrec:
                if other["externalId"] not in used and _jaccard(seed["entityIds"], other["entityIds"]) >= CLUSTER_JACCARD:
                    group.append(other)
                    used.add(other["externalId"])
            members = sorted(g["externalId"] for g in group)
            sid = "story_" + sha256_hex(canonical_json(members))[:12]
            times = sorted(g["at"] for g in group)
            stories[sid] = {"id": sid, "memberIds": members, "firstAt": times[0], "lastAt": times[-1],
                            "entityIds": sorted({e for g in group for e in g["entityIds"]})}
            for g in group:
                g["storyId"] = sid
        self._s.stories = stories
        for ent in self._s.entities.values():
            ms = [o for o in self._s.observations.values() if ent["id"] in o["entityIds"]]
            if ms:
                ts = sorted(m["at"] for m in ms)
                ent["aggregates"] = {"mentionCount": len(ms), "firstSeenAt": ts[0], "lastSeenAt": ts[-1],
                                     "linkedStoryIds": sorted({m.get("storyId") for m in ms if m.get("storyId")})}

    # ------------------------------------------------------------------ reads
    def entity(self, id_):
        e = self._s.entities.get(self._s.resolve_alias(id_))
        return copy.deepcopy(e) if e else None

    def context(self, id_):
        c = self._s.contexts.get(id_)
        return copy.deepcopy(c) if c else None

    def entities(self, type_=None):
        return [copy.deepcopy(e) for e in sorted(self._s.entities.values(), key=lambda e: e["id"])
                if type_ is None or e["type"] == type_]

    def neighbors_of(self, id_, via=None, at=None, in_context=None, direction="both"):
        rid = self._s.resolve_alias(id_)
        out = []
        for rel in self._s.relationships.values():
            if via and rel["type"] != via:
                continue
            if in_context and rel.get("context") != in_context:
                continue
            if at is not None:
                if rel.get("validFrom") and at < rel["validFrom"]:
                    continue
                if rel.get("validTo") and at >= rel["validTo"]:
                    continue
            elif rel.get("validTo"):
                continue  # closed edges are excluded from "now" queries
            if rel["from"] == rid and direction in ("both", "out"):
                other = rel["to"]
            elif rel["to"] == rid and direction in ("both", "in"):
                other = rel["from"]
            else:
                continue
            if other in self._s.entities:
                out.append(copy.deepcopy(self._s.entities[other]))
        return sorted(out, key=lambda e: e["id"])

    def facts_about(self, id_, predicate=None, include_superseded=False):
        rid = self._s.resolve_alias(id_)
        return sorted((copy.deepcopy(f) for f in self._s.facts.values()
                       if f["about"] == rid and (predicate is None or f["predicate"] == predicate)
                       and (include_superseded or not f.get("supersededBy"))), key=lambda f: f["assertedAt"])

    def merge_queue(self):
        return copy.deepcopy(self._s.merge_queue)

    def audit(self):
        return copy.deepcopy(self._s.audit)

    def events(self):
        return copy.deepcopy(self._s.events)

    def state_shape(self):
        key = lambda x: x["id"]  # noqa: E731
        return {"entities": sorted(self._s.entities.values(), key=key),
                "contexts": sorted(self._s.contexts.values(), key=key),
                "relationships": sorted(self._s.relationships.values(), key=key),
                "facts": sorted(self._s.facts.values(), key=key),
                "observations": sorted(self._s.observations.values(), key=lambda r: r["externalId"]),
                "stories": sorted(self._s.stories.values(), key=key)}

    def state_shape_canonical(self):
        return canonical_json(self.state_shape())

    def stats(self):
        return {"entities": len(self._s.entities), "contexts": len(self._s.contexts),
                "relationships": len(self._s.relationships), "facts": len(self._s.facts),
                "events": len(self._s.events), "audit": len(self._s.audit)}
