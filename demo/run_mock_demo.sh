#!/usr/bin/env bash
# Deterministic end-to-end demo: no API keys, no network, no model calls.
#
# A fake model proposes a one-line fix for a deliberately broken demo repo.
# The engine applies the candidate to a throwaway git worktree, runs the
# repo's own tests, and only writes a patch/commit message when every hard
# gate passes. Run from anywhere: `bash demo/run_mock_demo.sh`.
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
tmp="$(mktemp -d)"

PYTHONPATH="$here${PYTHONPATH:+:$PYTHONPATH}" python3 - "$tmp" <<'PY'
import json
import os
import sys

from vericommit.engine import Engine
from vericommit.policy import load_policy
from vericommit.providers import MockProvider
from vericommit.testing import FIXED, make_demo_repo

tmp = sys.argv[1]
repo = make_demo_repo(os.path.join(tmp, "repo"))

policy = load_policy(None)
policy.update({"candidates": 1, "repairs": 0, "providers": ["mock"], "budget_usd": 1.0})

response = json.dumps({
    "files": {"app/calc.py": FIXED},
    "commit_message": "fix(calc): return the sum from add()",
    "rationale": "subtraction typo; docstring and tests expect a sum",
    "risk": "none: pure arithmetic, public signature unchanged",
})
engine = Engine(repo, policy=policy, providers=[("mock", MockProvider([response]))],
                out_dir=os.path.join(tmp, "runs"), verbose=True)
result = engine.run("add(a, b) returns the difference instead of the sum. Fix it.")

print()
print("ACCEPTED:", bool(result.accepted), "| patch:", result.settled.get("patch"))
print("commit:  ", result.accepted.commit_message if result.accepted else "-")
print("report:  ", os.path.join(result.out_dir, "report.json"))
sys.exit(0 if result.accepted else 1)
PY

echo "demo dir: $tmp"
