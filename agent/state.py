"""The shared case file every agent reads and writes (the agents' common world state)."""
from dataclasses import asdict, dataclass, field


@dataclass
class Evidence:
    id: str
    check_id: str
    purpose: str
    sql: str
    columns: list
    rows: list
    error: str = ""


@dataclass
class Finding:
    check_id: str
    hypothesis: str
    finding: str
    evidence_id: str
    stance: str  # supports | refutes | unclear


@dataclass
class CaseFile:
    question: str
    snapshot: str = ""
    memory: str = ""
    catalog: str = ""
    view: str = "water.hourly_compare"
    site: str = "TODA1"
    run_id: str = ""
    run_ctx: str = ""
    hypotheses: list = field(default_factory=list)   # [{id, claim, status}]
    checks: list = field(default_factory=list)       # [{id, hypothesis, purpose, how, status}]
    evidence: list = field(default_factory=list)     # [Evidence]
    findings: list = field(default_factory=list)     # [Finding]
    critiques: list = field(default_factory=list)    # [{round, verdict, issues}]
    report: str = ""

    def next_evidence_id(self):
        return f"E{len(self.evidence) + 1}"

    def evidence_brief(self, max_rows=12):
        out = []
        for e in self.evidence:
            if e.error:
                out.append(f"{e.id} [{e.check_id}] {e.purpose}: QUERY FAILED: {e.error[:200]}")
                continue
            rows = e.rows[:max_rows]
            more = f" (+{len(e.rows) - max_rows} more rows)" if len(e.rows) > max_rows else ""
            out.append(f"{e.id} [{e.check_id}] {e.purpose}\n  columns={e.columns}\n  rows={rows}{more}")
        return "\n".join(out) or "(no evidence yet)"

    def findings_brief(self):
        return "\n".join(f"- {f.check_id} ({f.hypothesis}, {f.stance}, {f.evidence_id}): {f.finding}"
                         for f in self.findings) or "(no findings yet)"

    def to_dict(self):
        d = asdict(self)
        return d
