"""CLI-level tests: the commands users actually run must work from a clean install.

Kept ``unittest``-compatible so `python3 -m unittest discover -s tests` works without pytest.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vericommit import cli
from vericommit.cli import main
from vericommit.policy import load_policy
from vericommit.providers import MockProvider
from vericommit.testing import BUGGY, make_demo_repo

FIX_PATCH = """\
diff --git a/app/calc.py b/app/calc.py
--- a/app/calc.py
+++ b/app/calc.py
@@ -1,3 +1,3 @@
 def add(a, b):
     \"\"\"Return the sum of a and b.\"\"\"
-    return a - b
+    return a + b
"""

# A patch that adds a test file (test tampering) instead of fixing the product code.
TAMPER_PATCH = """\
diff --git a/tests/test_sneaky.py b/tests/test_sneaky.py
new file mode 100644
--- /dev/null
+++ b/tests/test_sneaky.py
@@ -0,0 +1,2 @@
+import unittest
+
"""


class TestCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vc-cli-")
        self.repo = make_demo_repo(os.path.join(self.tmp, "repo"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_providers_command_exits_zero(self):
        self.assertEqual(main(["providers"]), 0)

    def test_profile_writes_contract_files(self):
        out = os.path.join(self.tmp, "profile")
        self.assertEqual(main(["profile", "--repo", self.repo, "--out", out]), 0)
        for name in ("CONVENTIONS.md", "conventions.json", "prompt_fragment.md"):
            self.assertTrue(os.path.isfile(os.path.join(out, name)), name)

    def test_check_accepts_a_good_patch(self):
        patch = os.path.join(self.tmp, "change.diff")
        with open(patch, "w", encoding="utf-8") as fh:
            fh.write(FIX_PATCH)
        rc = main(
            [
                "check",
                "--repo",
                self.repo,
                "--patch",
                patch,
                "--commit-message",
                "fix(calc): return the sum",
            ]
        )
        self.assertEqual(rc, 0)

    def test_check_rejects_test_tampering(self):
        patch = os.path.join(self.tmp, "change.diff")
        with open(patch, "w", encoding="utf-8") as fh:
            fh.write(TAMPER_PATCH)
        self.assertEqual(main(["check", "--repo", self.repo, "--patch", patch]), 1)

    def test_run_with_mock_provider_accepts_and_writes_artifacts(self):
        policy_path = os.path.join(self.tmp, "policy.json")
        policy = load_policy(None)
        policy.update({"candidates": 1, "repairs": 0, "providers": ["mock"], "budget_usd": 1.0})
        with open(policy_path, "w", encoding="utf-8") as fh:
            json.dump(policy, fh)
        response = json.dumps(
            {
                "files": {"app/calc.py": BUGGY.replace("a - b", "a + b")},
                "commit_message": "fix(calc): return the sum",
                "rationale": "typo",
                "risk": "none",
            }
        )
        real_build = cli.build_provider
        cli.build_provider = lambda spec: MockProvider([response])
        try:
            rc = main(
                [
                    "run",
                    "--repo",
                    self.repo,
                    "--task",
                    "fix add",
                    "--policy",
                    policy_path,
                    "--out",
                    os.path.join(self.tmp, "runs"),
                    "--quiet",
                ]
            )
        finally:
            cli.build_provider = real_build
        self.assertEqual(rc, 0)
        runs = os.listdir(os.path.join(self.tmp, "runs"))
        self.assertEqual(len(runs), 1)
        run_dir = os.path.join(self.tmp, "runs", runs[0])
        self.assertTrue(os.path.isfile(os.path.join(run_dir, "change.diff")))
        self.assertTrue(os.path.isfile(os.path.join(run_dir, "report.json")))

    def test_demo_script_runs_from_any_directory(self):
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = dict(os.environ)
        env["PYTHONPATH"] = repo_root + (
            os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
        )
        proc = subprocess.run(
            ["bash", os.path.join(repo_root, "demo", "run_mock_demo.sh")],
            cwd=self.tmp,
            capture_output=True,
            text=True,
            timeout=180,
            env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("ACCEPTED: True", proc.stdout)


if __name__ == "__main__":
    unittest.main()
