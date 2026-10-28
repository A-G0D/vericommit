"""Git worktrees, safe file application, diff extraction, sanitized subprocess env."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Any

SECRET_ENV_RE = re.compile(r"(API_KEY|_TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)", re.I)


class GitError(RuntimeError):
    pass


def run(
    cmd: list[str], cwd: str, timeout: float = 120.0, env: dict[str, str] | None = None
) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s: {' '.join(cmd)}"
    except FileNotFoundError as e:
        return 127, "", str(e)


def sanitized_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not SECRET_ENV_RE.search(k)}
    env.update(
        {
            "CI": "1",
            "NO_COLOR": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return env


@dataclass
class Worktree:
    repo: str
    path: str
    ephemeral_copy: bool = False

    def git(self, *args: str, timeout: float = 120.0) -> tuple[int, str, str]:
        return run(["git", *args], self.path, timeout=timeout, env=sanitized_env())


def make_worktree(repo: str) -> Worktree:
    repo = os.path.abspath(repo)
    tmp = tempfile.mkdtemp(prefix="vc-wt-")
    os.rmdir(tmp)
    rc, _, err = run(["git", "worktree", "add", "--detach", tmp, "HEAD"], repo, env=sanitized_env())
    if rc == 0:
        return Worktree(repo, tmp, ephemeral_copy=False)
    # fallback: copy (repo may have no commits)
    shutil.copytree(repo, tmp, ignore=shutil.ignore_patterns(".git"))
    return Worktree(repo, tmp, ephemeral_copy=True)


def remove_worktree(wt: Worktree) -> None:
    if wt.ephemeral_copy:
        shutil.rmtree(wt.path, ignore_errors=True)
    else:
        run(["git", "worktree", "remove", "--force", wt.path], wt.repo, env=sanitized_env())
        shutil.rmtree(wt.path, ignore_errors=True)


def safe_join(root: str, relpath: str) -> str:
    root_abs = os.path.abspath(root)
    full = os.path.abspath(os.path.join(root_abs, relpath))
    if not (full == root_abs or full.startswith(root_abs + os.sep)):
        raise GitError(f"path escapes worktree: {relpath}")
    return full


def apply_files(wt: Worktree, files: dict[str, str]) -> list[str]:
    written = []
    for rel, content in files.items():
        full = safe_join(wt.path, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True) if os.path.dirname(full) else None
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(content)
        written.append(rel)
    return written


def reset_worktree(wt: Worktree) -> None:
    if not wt.ephemeral_copy:
        wt.git("reset", "--hard", "HEAD")
        wt.git("clean", "-fd")
    else:
        # rebuild the copy is cheaper than tracking edits: caller discards it
        pass


def changed_paths(wt: Worktree) -> list[str]:
    rc, out, err = wt.git("status", "--porcelain")
    if rc != 0:
        raise GitError(f"git status failed: {err}")
    paths = []
    for line in out.splitlines():
        if not line.strip():
            continue
        path = line[3:].strip()
        if " -> " in path:  # rename
            path = path.split(" -> ", 1)[1]
        path = path.strip('"')
        paths.append(path)
    return sorted(set(paths))


def diff_text(wt: Worktree) -> str:
    wt.git("add", "-A")
    rc, out, err = wt.git("diff", "--cached", "--no-color")
    if rc != 0:
        raise GitError(f"git diff failed: {err}")
    return out


@dataclass
class RepoInfo:
    kind: str
    build_cmd: list[str] | None = None
    test_cmd: list[str] | None = None
    lint_cmd: list[str] | None = None
    details: dict[str, Any] = field(default_factory=dict)


def detect_repo(repo: str) -> RepoInfo:
    """Heuristic build/test/lint detection for python, node, rust, go."""
    pyproject = os.path.exists(os.path.join(repo, "pyproject.toml"))
    pkg = os.path.join(repo, "package.json")
    has_py = (
        any(f.endswith(".py") for r, d, fs in os.walk(repo) if ".git" not in r for f in fs)
        if os.path.isdir(repo)
        else False
    )
    if os.path.exists(os.path.join(repo, "Cargo.toml")):
        return RepoInfo(
            "rust",
            ["cargo", "check", "--quiet"],
            ["cargo", "test", "--quiet"],
            ["cargo", "clippy", "--quiet"],
            {"manifest": "Cargo.toml"},
        )
    if os.path.exists(os.path.join(repo, "go.mod")):
        return RepoInfo(
            "go",
            ["go", "build", "./..."],
            ["go", "test", "./..."],
            ["go", "vet", "./..."],
            {"manifest": "go.mod"},
        )
    if os.path.exists(pkg):
        try:
            scripts = _json_load(pkg).get("scripts") or {}
        except (OSError, ValueError):
            scripts = {}
        test_cmd = ["npm", "test", "--silent"] if "test" in scripts else None
        build_cmd = ["npm", "run", "build", "--silent"] if "build" in scripts else None
        lint_cmd = ["npm", "run", "lint", "--silent"] if "lint" in scripts else None
        if os.path.exists(os.path.join(repo, "tsconfig.json")) and not build_cmd:
            build_cmd = ["npx", "tsc", "--noEmit"]
        return RepoInfo("node", build_cmd, test_cmd, lint_cmd, {"scripts": sorted(scripts)})
    if has_py or pyproject:
        test_cmd = None
        if os.path.isdir(os.path.join(repo, "tests")) or os.path.isdir(os.path.join(repo, "test")):
            test_cmd = ["python3", "-m", "unittest", "discover", "-s", "tests", "-q"]
        lint_cmd = None
        if os.path.exists(os.path.join(repo, "ruff.toml")) or os.path.exists(
            os.path.join(repo, ".ruff.toml")
        ):
            lint_cmd = ["python3", "-m", "ruff", "check", "."]
        return RepoInfo(
            "python",
            ["python3", "-m", "compileall", "-q", "."],
            test_cmd,
            lint_cmd,
            {"pyproject": pyproject},
        )
    return RepoInfo("unknown")


def _json_load(path: str) -> dict:
    import json

    with open(path, encoding="utf-8") as fh:
        return json.load(fh)
