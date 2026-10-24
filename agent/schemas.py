"""Validation of every structured model reply (planner, investigator, critic, fact-checker).

Each validator returns a cleaned copy that the rest of the code can index safely, or raises SchemaError when nothing
usable is left. Rules: ids are normalised (H1.., C1..), enums are enforced (unknown values become a safe default or
are dropped), items missing required fields are dropped, and no code ever indexes raw model output.
"""
import re

STANCES = {"supports", "refutes", "unclear"}
STATUSES = {"supported", "refuted", "unclear"}
VERDICTS = {"approve", "revise"}


class SchemaError(ValueError):
    pass


class Inconsistent(SchemaError):
    """A reply that parses but contradicts itself. The model is asked once to fix it; if it does not, the cleaned
    `value` is used as it stands (llm.chat_json)."""

    def __init__(self, message, value):
        super().__init__(message)
        self.value = value


def _text(v, limit=2000):
    return v.strip()[:limit] if isinstance(v, str) else ""


def _obj(o):
    if not isinstance(o, dict):
        raise SchemaError("the reply must be a JSON object")
    return o


def _checks(raw, hyp_ids, first):
    out, n = [], first
    for c in raw if isinstance(raw, list) else []:
        if not isinstance(c, dict):
            continue
        h = _text(c.get("hypothesis"), 10).upper()
        purpose = _text(c.get("purpose"), 500)
        if h not in hyp_ids or not purpose:
            continue
        out.append({"id": f"C{n}", "hypothesis": h, "purpose": purpose, "how": _text(c.get("how"), 500)})
        n += 1
    return out


def planner(o):
    o = _obj(o)
    hyps, seen = [], set()
    for h in o.get("hypotheses") if isinstance(o.get("hypotheses"), list) else []:
        if not isinstance(h, dict):
            continue
        hid, claim = _text(h.get("id"), 10).upper(), _text(h.get("claim"), 800)
        if not re.fullmatch(r"H\d+", hid) or not claim or hid in seen:
            continue
        seen.add(hid)
        hyps.append({"id": hid, "claim": claim})
    if not hyps:
        raise SchemaError('"hypotheses" must contain objects with "id" (H1, H2, ...) and "claim"')
    return {"restated_question": _text(o.get("restated_question"), 800), "hypotheses": hyps,
            "checks": _checks(o.get("checks"), seen, 1)}


def sql(o):
    o = _obj(o)
    q = _text(o.get("sql"), 5000)
    if not q:
        raise SchemaError('the reply must have a non-empty "sql" string')
    return {"sql": q, "purpose": _text(o.get("purpose"), 500)}


def interpret(o):
    o = _obj(o)
    finding = _text(o.get("finding"), 1500)
    if not finding:
        raise SchemaError('the reply must have a non-empty "finding" string')
    stance = _text(o.get("stance"), 20).lower()
    return {"finding": finding, "stance": stance if stance in STANCES else "unclear"}


def critic(o, hyp_ids, next_check):
    o = _obj(o)
    verdict = _text(o.get("verdict"), 20).lower()
    raw = o.get("hypothesis_status")
    if isinstance(raw, list):  # [{"id": "H1", "status": "..."}] is accepted too
        raw = {(_text(x.get("id"), 10) if isinstance(x, dict) else ""): (x.get("status") if isinstance(x, dict) else None)
               for x in raw}
    status = {}
    for k, v in (raw.items() if isinstance(raw, dict) else []):
        k, v = _text(k, 10).upper(), _text(v, 20).lower()
        if k in hyp_ids and v in STATUSES:
            status[k] = v
    issues = o.get("issues")
    issues = [issues] if isinstance(issues, str) else issues if isinstance(issues, list) else []
    out = {"verdict": verdict if verdict in VERDICTS else "revise", "hypothesis_status": status,
           "issues": [_text(i, 1000) for i in issues if _text(i, 1000)][:10],
           "new_checks": _checks(o.get("new_checks"), hyp_ids, next_check)}
    settled = bool(hyp_ids) and all(status.get(h) in ("supported", "refuted") for h in hyp_ids)
    if out["verdict"] == "revise" and settled and not out["new_checks"]:
        raise Inconsistent("verdict is 'revise', but every hypothesis is marked supported or refuted and no new check "
                           "is requested. Either set verdict to 'approve', or mark what is still uncertain as 'unclear' "
                           "and add the new_checks that would settle it", out)
    return out


def fact_check(o):
    o = _obj(o)
    out = []
    for c in o.get("corrections") if isinstance(o.get("corrections"), list) else []:
        if isinstance(c, dict) and _text(c.get("wrong"), 2000):
            out.append({"wrong": _text(c.get("wrong"), 2000), "right": _text(c.get("right"), 2000),
                        "why": _text(c.get("why"), 500)})
    return {"corrections": out}


def river_card(o):
    o = _obj(o)
    return {k: _text(o.get(k), 600) for k in ("headline", "trust", "pattern", "plan")}
