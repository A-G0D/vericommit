"""Tests for repo convention extraction and AI-policy refusal."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vericommit.conventions import build_profile, prompt_fragment, render_markdown
from vericommit.engine import Engine
from vericommit.policy import load_policy
from vericommit.providers import MockProvider
from vericommit.testing import FIXED, make_demo_repo

FORBIDDEN = """# AI Policy

Autonomous agents are not allowed to be used for contributing to this project.
Pull requests that appear in violation of this will be closed.
"""
CONTRIB = """# Contributing

## Use of AI
All use of AI must follow the AI Policy.
Add an entry in CHANGES.md for every change.
"""
WORKFLOW = """name: tests
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: |
          python3 -m pytest -q
          python3 -m ruff check .
"""


def make_repo(tmp: str, ai_policy: str | None) -> str:
    repo = make_demo_repo(os.path.join(tmp, "repo"))
    os.makedirs(os.path.join(repo, ".github", "workflows"), exist_ok=True)
    with open(os.path.join(repo, ".github", "workflows", "ci.yml"), "w") as fh:
        fh.write(WORKFLOW)
    with open(os.path.join(repo, "CONTRIBUTING.md"), "w") as fh:
        fh.write(CONTRIB)
    with open(os.path.join(repo, "CHANGES.md"), "w") as fh:
        fh.write("# Changes\n")
    with open(os.path.join(repo, ".github", "pull_request_template.md"), "w") as fh:
        fh.write("# Summary\n\n# Tests\n\nDescribe tests.\n")
    if ai_policy:
        with open(os.path.join(repo, "AI_POLICY.md"), "w") as fh:
            fh.write(ai_policy)
    import subprocess

    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=d",
            "-c",
            "user.email=d@d",
            "commit",
            "-qm",
            "docs: add contribution docs",
        ],
        cwd=repo,
        check=True,
    )
    return repo


class TestConventions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vc-conv-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_forbidden_ai_policy_detected(self):
        repo = make_repo(self.tmp, FORBIDDEN)
        p = build_profile(repo)
        self.assertEqual(p["ai_policy"]["category"], "forbidden_autonomous")
        self.assertFalse(p["ready_for_agent_pr"])
        self.assertIn("Autonomous agents", p["ai_policy"]["evidence"])

    def test_allowed_repo_commands_and_template(self):
        repo = make_repo(self.tmp, None)
        p = build_profile(repo)
        self.assertTrue(p["ready_for_agent_pr"])
        self.assertIn("python3 -m pytest -q", p["commands"]["test"])
        self.assertTrue(p["pr"]["requires_changelog"])
        self.assertIn("Summary", p["pr"]["sections"])

    def test_prompt_fragment_mentions_rules(self):
        repo = make_repo(self.tmp, FORBIDDEN)
        p = build_profile(repo)
        frag = prompt_fragment(p, "use AI policy")
        self.assertIn("forbidden_autonomous", frag)
        self.assertIn("commit style", frag)
        self.assertTrue(render_markdown(p).startswith("# Repo profile"))

    def test_engine_refuses_forbidden_repo(self):
        repo = make_repo(self.tmp, FORBIDDEN)
        policy = load_policy(None)
        policy.update({"candidates": 1, "providers": ["mock"], "budget_usd": 0.1})
        good = json.dumps(
            {
                "files": {"app/calc.py": FIXED},
                "commit_message": "fix(calc): sum",
                "rationale": "x",
                "risk": "y",
            }
        )
        engine = Engine(
            repo,
            policy=policy,
            providers=[("mock", MockProvider([good]))],
            out_dir=os.path.join(self.tmp, "runs"),
            verbose=False,
        )
        with self.assertRaises(RuntimeError) as ctx:
            engine.run("fix add")
        self.assertIn("autonomous", str(ctx.exception).lower())


if __name__ == "__main__":
    unittest.main()
