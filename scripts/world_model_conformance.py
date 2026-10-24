"""World Model conformance scorecard (no dependencies).   Run:  python3 scripts/world_model_conformance.py

Exit code 0 = all checks pass (the spec's definition of done), 1 = at least one failed.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests import test_world_model as T  # noqa: E402

tests = [(n, f) for n, f in vars(T).items() if n.startswith("test_") and callable(f)]
passed = 0
print("\nWorld Model conformance (Python implementation: world_model/)\n")
for name, fn in tests:
    desc = (fn.__doc__ or "").strip().splitlines()[0]
    try:
        fn()
        passed += 1
        print(f"  PASS {name[5:11]}  {desc}")
    except Exception as e:  # noqa: BLE001
        print(f"  FAIL {name[5:11]}  {desc}\n        {type(e).__name__}: {e}")
print(f"\n  {passed}/{len(tests)} checks passed\n")
sys.exit(0 if passed == len(tests) else 1)
