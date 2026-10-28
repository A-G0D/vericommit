"""Policy: what a commit is allowed to touch, and which checks are mandatory."""

from __future__ import annotations

import fnmatch
import json
import os
from typing import Any

DEFAULT_POLICY: dict[str, Any] = {
    "allowed_paths": ["**"],
    "forbidden_paths": [
        ".github/**",
        ".gitlab-ci.yml",
        "tests/**",
        "test/**",
        "test_*.py",
        "*_test.py",
        "spec/**",
        "__tests__/**",
        "package.json",
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "Cargo.toml",
        "go.mod",
        "go.sum",
        "requirements*.txt",
        "*.lock",
        "uv.lock",
        "poetry.lock",
        ".env*",
        "Dockerfile",
        "docker-compose*",
    ],
    "max_diff_lines": 400,
    "max_files_changed": 8,
    "max_file_bytes": 1_000_000,
    "build_cmd": None,  # None = auto-detect
    "test_cmd": None,  # None = auto-detect; "skip" disables the gate
    "lint_cmd": None,
    "require_tests": True,
    "hard_checks": ["repo_policy", "git_hygiene", "no_test_tamper", "safety", "build", "tests"],
    "soft_checks": ["lint", "contract", "style", "changelog", "injection"],
    "candidates": 2,
    "repairs": 1,
    "providers": ["deepseek"],
    "reviewer_provider": None,
    "budget_usd": 2.0,
    "test_timeout_s": 300,
    "build_timeout_s": 180,
    "commit_prefix": "fix",
    "allow_network_in_tests": False,
    "allow_autonomous_agent": False,
}


def load_policy(path: str | None) -> dict[str, Any]:
    policy = json.loads(json.dumps(DEFAULT_POLICY))  # deep copy
    if path:
        with open(path, encoding="utf-8") as fh:
            user = json.load(fh)
        policy.update(user)
    return policy


def glob_any(path: str, patterns: list[str]) -> bool:
    p = path.replace(os.sep, "/")
    return any(fnmatch.fnmatch(p, pat) or fnmatch.fnmatch("/" + p, pat) for pat in patterns)
