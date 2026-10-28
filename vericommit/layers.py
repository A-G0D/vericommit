"""Verification layers. Each layer is a pure function of the commit context.

Hard layers can reject a candidate; soft layers only score it. Order matters:
cheap deterministic checks run before anything that executes the patch.
"""

from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass, field
from typing import Any

from .gitops import RepoInfo, run, sanitized_env

SECRET_PATTERNS = [
    (r"AKIA[0-9A-Z]{16}", "AWS access key"),
    (
        r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*['\"][A-Za-z0-9_\-]{16,}['\"]",
        "hardcoded credential",
    ),
    (r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----", "private key"),
    (r"ghp_[A-Za-z0-9]{30,}", "GitHub token"),
    (r"sk-[A-Za-z0-9]{20,}", "API secret key"),
    (r"(?i)bearer\s+[A-Za-z0-9._\-]{20,}", "bearer token"),
]
DANGEROUS = [
    (r"\bsubprocess\.[a-z_]+\([^)]*shell\s*=\s*True", "shell=True subprocess"),
    (r"\bos\.system\s*\(", "os.system"),
    (r"\b(eval|exec)\s*\(", "eval/exec"),
    (r"\bpickle\.loads?\s*\(", "pickle deserialization"),
    (r"rm\s+-rf\s+/", "rm -rf on absolute path"),
    (r"chmod\s+777", "chmod 777"),
    (r"curl[^|]*\|\s*(ba)?sh", "curl | sh"),
]
INJECTION = [
    r"(?i)ignore (all )?(previous|prior|above) instructions",
    r"(?i)disregard (the )?(system|previous) prompt",
    r"(?i)you are now\s+\w+",
    r"(?i)exfiltrate|send .* to https?://|curl .*\$\(env\)",
    r"(?i)do not (report|tell|mention) this",
]


@dataclass
class Context:
    repo: str
    worktree: str
    changed: list[str]
    patch: str
    policy: dict[str, Any]
    info: RepoInfo
    task: str
    commit_message: str = ""
    evidence_text: str = ""
    files: dict[str, str] = field(default_factory=dict)


@dataclass
class CheckResult:
    name: str
    ok: bool
    severity: str  # "hard" | "soft" | "info"
    detail: str
    meta: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "severity": self.severity,
            "detail": self.detail,
            **({"meta": self.meta} if self.meta else {}),
        }


def added_lines(patch: str) -> list[str]:
    return [ln[1:] for ln in patch.splitlines() if ln.startswith("+") and not ln.startswith("+++")]


def removed_lines(patch: str) -> list[str]:
    return [ln[1:] for ln in patch.splitlines() if ln.startswith("-") and not ln.startswith("---")]


def diff_line_count(patch: str) -> int:
    return sum(
        1
        for ln in patch.splitlines()
        if (ln.startswith("+") and not ln.startswith("+++"))
        or (ln.startswith("-") and not ln.startswith("---"))
    )


# --------------------------------------------------------------------------- #
# Hard gates
# --------------------------------------------------------------------------- #
def repo_policy(ctx: Context) -> CheckResult:
    """Refuse to act on repos whose AI policy forbids autonomous agents."""
    conv = ctx.policy.get("repo_conventions") or {}
    ai = conv.get("ai_policy") or {}
    cat = ai.get("category", "unknown")
    if cat == "forbidden_autonomous" and not ctx.policy.get("allow_autonomous_agent"):
        return CheckResult(
            "repo_policy",
            False,
            "hard",
            f"repo AI policy forbids autonomous agents ({ai.get('evidence')}); "
            "do not submit agent-generated PRs here",
            {"category": cat},
        )
    return CheckResult(
        "repo_policy",
        True,
        "hard",
        f"repo AI policy allows agent-assisted work (category={cat})",
        {"category": cat},
    )


def changelog(ctx: Context) -> CheckResult:
    """Soft: contribution docs demand a changelog entry for code changes."""
    conv = ctx.policy.get("repo_conventions") or {}
    need = (conv.get("pr") or {}).get("requires_changelog")
    files = conv.get("changelog_files") or []
    if not need or not files:
        return CheckResult("changelog", True, "soft", "no changelog requirement detected")
    touched = [p for p in ctx.changed if p in files]
    return CheckResult(
        "changelog",
        bool(touched),
        "soft",
        f"changelog entry {'present' if touched else 'MISSING'} ({files[:2]})",
        {"changelog_files": files},
    )


def git_hygiene(ctx: Context) -> CheckResult:
    from .policy import glob_any

    policy = ctx.policy
    problems = []
    if not ctx.changed:
        problems.append("no files changed")
    forbidden = [p for p in ctx.changed if glob_any(p, policy["forbidden_paths"])]
    if forbidden:
        problems.append(f"forbidden paths touched: {forbidden}")
    not_allowed = [
        p
        for p in ctx.changed
        if policy.get("allowed_paths") and not glob_any(p, policy["allowed_paths"])
    ]
    if not_allowed:
        problems.append(f"outside allowed_paths: {not_allowed}")
    if len(ctx.changed) > policy["max_files_changed"]:
        problems.append(f"{len(ctx.changed)} files changed > {policy['max_files_changed']}")
    n = diff_line_count(ctx.patch)
    if n > policy["max_diff_lines"]:
        problems.append(f"diff {n} lines > {policy['max_diff_lines']}")
    for rel in ctx.changed:
        full = os.path.join(ctx.worktree, rel)
        if not os.path.isfile(full):
            continue
        size = os.path.getsize(full)
        if size > policy["max_file_bytes"]:
            problems.append(f"{rel} is {size} bytes")
        try:
            with open(full, "rb") as fh:
                if b"\x00" in fh.read(4096):
                    problems.append(f"{rel} looks binary")
        except OSError:
            pass
    return CheckResult(
        "git_hygiene",
        not problems,
        "hard",
        "; ".join(problems) if problems else f"{len(ctx.changed)} files, {n} diff lines",
        {"changed": ctx.changed, "diff_lines": n},
    )


def safety(ctx: Context) -> CheckResult:
    added = added_lines(ctx.patch)
    findings = []
    for i, line in enumerate(added, 1):
        for pat, label in SECRET_PATTERNS:
            if re.search(pat, line):
                findings.append(f"secret({label})")
        for pat, label in DANGEROUS:
            if re.search(pat, line):
                findings.append(f"dangerous({label})")
    unique = sorted(set(findings))
    return CheckResult(
        "safety",
        not unique,
        "hard",
        ", ".join(unique) if unique else "no secrets or dangerous calls added",
        {"findings": unique},
    )


def no_test_tamper(ctx: Context) -> CheckResult:
    """Defense in depth: test/CI files may never be modified, regardless of allowed_paths."""
    test_like = re.compile(
        r"(^|/)(tests?|__tests__|spec)(/|$)|(^|/)test_[^/]*$|_test\.[a-z]+$|"
        r"^\.github/|(^|/)(conftest\.py|pytest\.ini|tox\.ini)$"
    )
    touched = [p for p in ctx.changed if test_like.search(p)]
    return CheckResult(
        "no_test_tamper",
        not touched,
        "hard",
        f"test/CI files modified: {touched}" if touched else "no test or CI files touched",
        {"touched": touched},
    )


def build(ctx: Context) -> CheckResult:
    cmd = ctx.policy.get("build_cmd") or ctx.info.build_cmd
    if not cmd:
        return CheckResult(
            "build", True, "hard", "no build command detected (skipped)", {"skipped": True}
        )
    rc, out, err = run(
        cmd, ctx.worktree, timeout=float(ctx.policy["build_timeout_s"]), env=sanitized_env()
    )
    tail = (out + err).strip()[-1200:]
    return CheckResult(
        "build",
        rc == 0,
        "hard",
        f"{' '.join(cmd)} -> exit {rc}" + (f"\n{tail}" if rc else ""),
        {"cmd": cmd, "exit": rc},
    )


def tests(ctx: Context) -> CheckResult:
    if ctx.policy.get("test_cmd") == "skip":
        return CheckResult(
            "tests", True, "hard", "disabled by policy (test_cmd=skip)", {"skipped": True}
        )
    cmd = ctx.policy.get("test_cmd") or ctx.info.test_cmd
    if not cmd:
        ok = not ctx.policy.get("require_tests", True)
        return CheckResult(
            "tests",
            ok,
            "hard",
            "no test command detected" + ("" if ok else " (require_tests=true)"),
            {"skipped": True},
        )
    rc, out, err = run(
        cmd, ctx.worktree, timeout=float(ctx.policy["test_timeout_s"]), env=sanitized_env()
    )
    blob = out + err
    no_tests = bool(re.search(r"(?i)(no tests ran|0 tests|ran 0 tests|no test)", blob))
    ok = rc == 0 and not (ctx.policy.get("require_tests", True) and no_tests)
    return CheckResult(
        "tests",
        ok,
        "hard",
        f"{' '.join(cmd)} -> exit {rc}"
        + ("; suspicious: no tests detected" if no_tests and rc == 0 else "")
        + (f"\n{blob.strip()[-1200:]}" if rc != 0 else ""),
        {"cmd": cmd, "exit": rc, "no_tests_detected": no_tests},
    )


# --------------------------------------------------------------------------- #
# Soft layers
# --------------------------------------------------------------------------- #
def lint(ctx: Context) -> CheckResult:
    cmd = ctx.policy.get("lint_cmd") or ctx.info.lint_cmd
    if not cmd:
        return CheckResult(
            "lint", True, "soft", "no linter configured (skipped)", {"skipped": True}
        )
    rc, out, err = run(cmd, ctx.worktree, timeout=120, env=sanitized_env())
    return CheckResult(
        "lint",
        rc == 0,
        "soft",
        f"{' '.join(cmd)} -> exit {rc}" + (f"\n{(out + err).strip()[-800:]}" if rc else ""),
        {"cmd": cmd, "exit": rc},
    )


def _public_api(source: str) -> dict[str, str]:
    """name -> signature string for top-level public defs/classes."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    api: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith(
            "_"
        ):
            args = [a.arg for a in node.args.args]
            api[node.name] = f"fn({', '.join(args)})"
        elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            methods = [
                n.name
                for n in node.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and not n.name.startswith("_")
            ]
            api[node.name] = f"class[{', '.join(methods)}]"
    return api


def contract(ctx: Context) -> CheckResult:
    """Detect accidental public-API breakage in changed Python modules."""
    issues = []
    for rel in ctx.changed:
        if not rel.endswith(".py"):
            continue
        rc, before, _ = run(["git", "show", f"HEAD:{rel}"], ctx.worktree, env=sanitized_env())
        full = os.path.join(ctx.worktree, rel)
        try:
            with open(full, encoding="utf-8", errors="ignore") as fh:
                after = fh.read()
        except OSError:
            continue
        if rc != 0:
            continue  # new file
        old_api, new_api = _public_api(before), _public_api(after)
        for name, sig in old_api.items():
            if name not in new_api:
                issues.append(f"{rel}: removed public {name}")
            elif new_api[name] != sig:
                issues.append(f"{rel}: signature changed {name}: {sig} -> {new_api[name]}")
    return CheckResult(
        "contract",
        not issues,
        "soft",
        "; ".join(issues) if issues else "public API unchanged",
        {"issues": issues},
    )


def _repo_line_profile(ctx: Context) -> tuple[int, str]:
    lens, tabs = [], 0
    count = 0
    for root, dirs, files in os.walk(ctx.worktree):
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", ".venv", "__pycache__")]
        for f in files:
            if not f.endswith((".py", ".js", ".ts", ".rs", ".go")):
                continue
            count += 1
            if count > 200:
                break
            try:
                with open(os.path.join(root, f), encoding="utf-8", errors="ignore") as fh:
                    for line in fh:
                        lens.append(len(line.rstrip("\n")))
                        if line.startswith("\t"):
                            tabs += 1
            except OSError:
                pass
    if not lens:
        return 100, "space"
    lens.sort()
    p95 = lens[int(len(lens) * 0.95)]
    style = "tab" if tabs > len(lens) * 0.3 else "space"
    return max(80, min(p95, 240)), style


def style(ctx: Context) -> CheckResult:
    max_len, indent_style = _repo_line_profile(ctx)
    issues = []
    for line in added_lines(ctx.patch):
        if len(line) > max_len + 10:
            issues.append(f"long line ({len(line)} > {max_len + 10})")
        if line != line.rstrip():
            issues.append("trailing whitespace")
        if indent_style == "space" and line.startswith("\t"):
            issues.append("tab indentation in a space-indented repo")
        if re.match(r"\s*(print\(|console\.log\(|dbg!\(|fmt\.Println\()", line):
            issues.append(f"debug print added: {line.strip()[:60]}")
        if re.match(r"\s*(#|//)\s*(TODO|FIXME|XXX)", line):
            issues.append("TODO/FIXME left in diff")
    msg = ctx.commit_message.strip()
    conv = ctx.policy.get("repo_conventions") or {}
    style = (conv.get("commit_style") or {}).get("style", "unknown")
    if msg:
        if style == "conventional":
            if not re.match(
                r"^(feat|fix|chore|docs|refactor|test|perf|build|ci|style|revert)(\(.+\))?!?:\s+\S.{8,}",
                msg,
            ):
                issues.append("repo uses conventional commits; message is not conventional")
        elif style == "imperative":
            if not re.match(r"^[A-Z][a-z]+\b.{8,}", msg) or msg.endswith("."):
                issues.append("repo uses imperative commit subjects (e.g. 'Fix the ...')")
        elif len(msg) < 12:
            issues.append("commit message too short")
    unique = sorted(set(issues))
    return CheckResult(
        "style",
        not unique,
        "soft",
        "; ".join(unique[:8])
        if unique
        else f"style consistent (max line {max_len}, {indent_style} indent)",
        {"issues": unique, "max_line": max_len},
    )


def injection(ctx: Context) -> CheckResult:
    hits = []
    for pat in INJECTION:
        for m in re.finditer(pat, ctx.evidence_text or ""):
            start = max(0, m.start() - 40)
            hits.append(
                f"{m.group(0)[:60]!r} near: ...{ctx.evidence_text[start : m.end() + 40]!r}..."
            )
            if len(hits) >= 5:
                break
    for pat in INJECTION:
        if re.search(pat, ctx.task or ""):
            hits.append(f"task text matches injection pattern {pat!r}")
    unique = sorted(set(hits))
    return CheckResult(
        "injection",
        not unique,
        "soft",
        "prompt-injection signals in provided context: " + "; ".join(unique[:3])
        if unique
        else "no prompt-injection signals in context",
        {"hits": unique},
    )


LAYERS = {
    "repo_policy": repo_policy,
    "changelog": changelog,
    "git_hygiene": git_hygiene,
    "no_test_tamper": no_test_tamper,
    "safety": safety,
    "build": build,
    "tests": tests,
    "lint": lint,
    "contract": contract,
    "style": style,
    "injection": injection,
}


def run_checks(ctx: Context) -> list[CheckResult]:
    results: list[CheckResult] = []
    for name in ctx.policy["hard_checks"]:
        res = LAYERS[name](ctx)
        results.append(res)
        if not res.ok:
            return results  # fail fast: don't execute a patch that already broke a cheap gate
    for name in ctx.policy["soft_checks"]:
        results.append(LAYERS[name](ctx))
    return results
