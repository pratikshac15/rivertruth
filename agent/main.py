"""CLI:  python3 -m agent.main "Why did ... ?"   (add SAMPLE_MODE=true to replay without Zetaris/Nightshift)"""
import json
import os
import sys

from . import config, log
from .cases import CASES, site_question
from .orchestrator import run

DEFAULT_Q = ("Tonight (Oct 22-23, 2026) USGS measured Black Warrior River flow at Northport jumping from about 180 cfs "
             "to over 4,000 cfs, while the National Water Model showed about 200 cfs and the NWS gauge at Oliver Lock and Dam "
             "showed about 1,000-1,200 cfs. Why do these sources disagree, and what actually happened?")


def main():
    args = sys.argv[1:]
    site_lid = None
    if "--site" in args:
        i = args.index("--site")
        site_lid = args[i + 1].upper()
        del args[i:i + 2]
    if site_lid and site_lid != "TODA1" and not args:
        args = [site_question(f"NWS gauge {site_lid}")]
    if args[:1] == ["--case"]:
        case_key = args[1] if len(args) > 1 else "1"
        name, q = CASES[case_key]
        print(f"Test case {case_key}: {name}")
    elif args[:1] == ["--list"]:
        for k, (name, q) in CASES.items():
            print(f"{k}  {name}: {q}")
        return
    else:
        q = " ".join(args).strip() or CASES["1"][1]
    case = run(q, site_lid)
    print("\n" + "=" * 70 + "\n" + case.report + "\n" + "=" * 70)
    os.makedirs("output_examples", exist_ok=True)
    base = os.path.join("output_examples", os.path.basename(log.path()).replace(".jsonl", ""))
    with open(base + "_report.md", "w", encoding="utf-8") as f:
        f.write(f"# RiverTruth report\n\n**Question:** {q}\n\n{case.report}\n\n## Evidence\n")
        for e in case.evidence:
            f.write(f"\n### {e.id} ({e.check_id}) {e.purpose}\n```sql\n{e.sql}\n```\n")
            f.write(f"Error: {e.error}\n" if e.error else f"Rows: {len(e.rows)}; first rows: {e.rows[:10]}\n")
    with open(base + "_case.json", "w", encoding="utf-8") as f:
        json.dump(case.to_dict(), f, indent=1, default=str)
    print(f"\nSaved: {base}_report.md, {base}_case.json, log {log.path()}"
          f"{'  (SAMPLE MODE)' if config.SAMPLE_MODE else ''}")


if __name__ == "__main__":
    main()
