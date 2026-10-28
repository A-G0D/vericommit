"""Read a repo's contribution contract: docs, AI policy, workflows, templates, style.

This is what turns "generic agent" into "contributor who read the instructions".
It is also a safety boundary: repos that forbid autonomous agents are marked and
the engine refuses to act on them.
"""

from __future__ import annotations

import os
import re
import subprocess
from typing import Any

import yaml

DOC_NAME_PATTERNS = [
    (r"(?i)readme", "readme"),
    (r"(?i)contributing", "contributing"),
    (r"(?i)^agents\.md$", "agents"),
    (r"(?i)^ai_policy", "ai_policy"),
    (r"(?i)code_of_conduct", "code_of_conduct"),
    (r"(?i)^security", "security"),
    (r"(?i)pull_request_template", "pr_template"),
    (r"(?i)issue_template", "issue_template"),
    (r"(?i)^changelog|^changes", "changelog"),
    (r"(?i)support", "support"),
]
AI_FORBIDDEN = [
    r"autonomous agents? (are|is) not allowed",
    r"not allowed to be used for contributing",
    r"AI-generated (pull requests?|prs?|contributions?) (are|is) (not allowed|prohibited|not accepted)",
    r"do not (submit|open|create) (pull requests?|prs?|issues?) (with|using|from) AI",
    r"fully (automated|autonomous) (agents?|contributions?) (are|will be) (not )?(accepted|allowed|closed)",
]
AI_HUMAN = [
    r"human in the loop",
    r"responsible for any code",
    r"AI[- ]assisted",
    r"should be able to (explain|describe)",
    r"written by humans?",
]
AI_DISCLOSE = [r"disclos", r"quote block", r"in your own words"]
CMD_TOKENS = [
    r"\bpytest\b",
    r"\btox\b",
    r"\bnox\b",
    r"\bhatch\b",
    r"\bpre-commit\b",
    r"\bruff\b",
    r"\bblack\b",
    r"\bflake8\b",
    r"\bmypy\b",
    r"\bpyright\b",
    r"\bnpm\b",
    r"\bpnpm\b",
    r"\byarn\b",
    r"\bnode\b",
    r"\bjest\b",
    r"\bvitest\b",
    r"\beslint\b",
    r"\bprettier\b",
    r"\btsc\b",
    r"\bcargo\b",
    r"\bclippy\b",
    r"\brustfmt\b",
    r"\bgo test\b",
    r"\bgo vet\b",
    r"\bgo build\b",
    r"\bmake\b",
]
TEST_HINT = r"(test|pytest|tox|nox|jest|vitest|cargo test|go test)"
LINT_HINT = (
    r"(lint|ruff|black|flake8|eslint|prettier|clippy|mypy|pyright|rustfmt|go vet|pre-commit)"
)
BUILD_HINT = r"(build|tsc|cargo build|go build|compile)"


def _read(path: str, limit: int = 60_000) -> str:
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            return fh.read(limit)
    except OSError:
        return ""


def _walk(root: str, max_files: int = 4000) -> list[str]:
    out = []
    for base, dirs, files in os.walk(root):
        dirs[:] = [
            d
            for d in dirs
            if d
            not in (
                ".git",
                "node_modules",
                ".venv",
                "venv",
                "__pycache__",
                "target",
                "dist",
                "build",
            )
        ]
        for f in files:
            out.append(os.path.relpath(os.path.join(base, f), root))
            if len(out) >= max_files:
                return out
    return out


def find_docs(repo: str) -> dict[str, str]:
    docs: dict[str, str] = {}
    for rel in _walk(repo):
        base = os.path.basename(rel)
        for pat, kind in DOC_NAME_PATTERNS:
            if re.search(pat, base) or re.search(pat, rel.replace(os.sep, "/")):
                if kind == "readme" and "readme" in docs:
                    continue
                if kind == "contributing" and "contributing" in docs:
                    continue
                docs[rel if kind not in ("readme", "contributing") else f"{kind}:{rel}"] = rel
                break
    return docs


def detect_ai_policy(repo: str, docs: dict[str, str]) -> dict[str, Any]:
    sources = [
        rel
        for key, rel in docs.items()
        if "ai_policy" in key.lower() or "contributing" in key.lower()
    ]
    text = "\n\n".join(f"## {rel}\n" + _read(os.path.join(repo, rel)) for rel in sources)
    if not text.strip():
        for rel in docs.values():
            if re.search(r"(?i)ai.policy", rel):
                text += _read(os.path.join(repo, rel))
    cat, evidence = "unknown", ""
    for pat in AI_FORBIDDEN:
        m = re.search(pat, text, re.I)
        if m:
            cat, evidence = "forbidden_autonomous", m.group(0)
            break
    if cat == "unknown":
        for p in AI_HUMAN:
            m = re.search(p, text, re.I)
            if m:
                cat, evidence = "human_in_loop", m.group(0)
                break
    if cat == "unknown" and re.search(r"(?i)\bAI\b|\bLLMs?\b", text):
        cat = "ai_mentioned"
    disclosure = any(re.search(p, text, re.I) for p in AI_DISCLOSE)
    return {
        "category": cat,
        "evidence": evidence.strip()[:400],
        "requires_disclosure": disclosure,
        "sources": sources,
    }


def parse_workflows(repo: str) -> list[dict[str, Any]]:
    out = []
    wf_dir = os.path.join(repo, ".github", "workflows")
    if not os.path.isdir(wf_dir):
        return out
    for fn in sorted(os.listdir(wf_dir)):
        if not fn.endswith((".yml", ".yaml")):
            continue
        path = os.path.join(wf_dir, fn)
        try:
            data = yaml.safe_load(_read(path)) or {}
        except yaml.YAMLError:
            out.append({"file": fn, "error": "yaml parse error", "commands": [], "jobs": []})
            continue
        jobs = data.get("jobs") or {}
        commands: list[str] = []
        checks: list[str] = []
        for job_name, job in jobs.items():
            if not isinstance(job, dict):
                continue
            checks.append(str(job.get("name") or job_name))
            for step in job.get("steps") or []:
                if not isinstance(step, dict):
                    continue
                run = step.get("run")
                if isinstance(run, str):
                    for line in run.splitlines():
                        line = line.strip()
                        if line and not line.startswith("#") and "|" not in line:
                            commands.append(line)
                if step.get("uses"):
                    commands.append("uses:" + str(step["uses"]))
        out.append({"file": fn, "jobs": checks, "commands": commands, "on": data.get("on")})
    return out


CMD_START = (
    "pytest",
    "uv",
    "tox",
    "nox",
    "hatch",
    "pre-commit",
    "ruff",
    "black",
    "flake8",
    "mypy",
    "pyright",
    "npm",
    "pnpm",
    "yarn",
    "node",
    "jest",
    "vitest",
    "eslint",
    "prettier",
    "tsc",
    "cargo",
    "clippy",
    "rustfmt",
    "go",
    "make",
    "python",
    "python3",
)


def _looks_like_command(cmd: str) -> bool:
    parts = cmd.split()
    if not parts:
        return False
    if "${{" in cmd or "$(" in cmd or cmd.endswith("\\"):
        return False  # unresolved CI expressions / continuations
    return parts[0] in CMD_START


def infer_commands(workflows: list[dict], docs_text: str) -> dict[str, list[str]]:
    candidates: list[str] = []
    for wf in workflows:
        for cmd in wf.get("commands", []):
            if _looks_like_command(cmd) and any(re.search(t, cmd, re.I) for t in CMD_TOKENS):
                candidates.append(cmd)
    for line in docs_text.splitlines():
        s = line.strip().strip("`$ ")
        if (
            _looks_like_command(s)
            and 3 < len(s) < 200
            and any(re.search(t, s, re.I) for t in CMD_TOKENS)
        ):
            candidates.append(s)

    def pick(hint: str) -> list[str]:
        seen, out = set(), []
        for c in candidates:
            cl = c.lower()
            if re.search(hint, cl) and c not in seen and not cl.startswith("uses:"):
                seen.add(c)
                out.append(c)
        return out[:6]

    return {"test": pick(TEST_HINT), "lint": pick(LINT_HINT), "build": pick(BUILD_HINT)}


def detect_commit_style(repo: str) -> dict[str, Any]:
    rc = subprocess.run(
        ["git", "log", "--format=%s", "-80"], cwd=repo, capture_output=True, text=True
    )
    msgs = [m for m in rc.stdout.splitlines() if m.strip()] if rc.returncode == 0 else []
    conventional = sum(
        1
        for m in msgs
        if re.match(
            r"^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)(\(.+\))?!?:\s", m
        )
    )
    imperative = sum(1 for m in msgs if re.match(r"^[A-Z][a-z]+(\s|$)", m))
    avg_len = round(sum(len(m) for m in msgs) / len(msgs), 1) if msgs else 0
    style = "unknown"
    if msgs:
        if conventional / len(msgs) >= 0.6:
            style = "conventional"
        elif imperative / len(msgs) >= 0.6:
            style = "imperative"
        else:
            style = "freeform"
    return {
        "style": style,
        "samples": msgs[:8],
        "n": len(msgs),
        "conventional_ratio": round(conventional / len(msgs), 2) if msgs else None,
        "avg_length": avg_len,
    }


def detect_configs(repo: str) -> dict[str, Any]:
    found = {}
    for rel in (
        ".editorconfig",
        "ruff.toml",
        ".ruff.toml",
        ".flake8",
        "mypy.ini",
        "rustfmt.toml",
        ".prettierrc",
        ".prettierrc.json",
        ".eslintrc",
        ".eslintrc.json",
        ".eslintrc.js",
        ".pre-commit-config.yaml",
        "pyproject.toml",
        "package.json",
        "Cargo.toml",
        "go.mod",
        "tox.ini",
    ):
        if os.path.exists(os.path.join(repo, rel)):
            found[rel] = True
    tools = {}
    pyproject = os.path.join(repo, "pyproject.toml")
    if os.path.exists(pyproject):
        txt = _read(pyproject)
        for tool in ("ruff", "black", "mypy", "pytest", "coverage", "isort"):
            if re.search(rf"\[tool\.{tool}[\.\]]", txt):
                tools[tool] = True
    hooks = []
    pre = os.path.join(repo, ".pre-commit-config.yaml")
    if os.path.exists(pre):
        try:
            data = yaml.safe_load(_read(pre)) or {}
            hooks = [
                h.get("id")
                for r in data.get("repos", [])
                for h in (r.get("hooks") or [])
                if isinstance(h, dict)
            ]
        except yaml.YAMLError:
            pass
    return {"files": sorted(found), "pyproject_tools": sorted(tools), "pre_commit_hooks": hooks}


def detect_pr_and_signoff(repo: str, docs: dict[str, str]) -> dict[str, Any]:
    pr_template, headings = "", []
    for key, rel in docs.items():
        kl = key.lower()
        if "pr_template" in kl or "pull_request_template" in kl:
            pr_template = _read(os.path.join(repo, rel))
            break
    instructions = []
    for line in pr_template.splitlines():
        s = line.strip()
        if s.startswith("#"):
            headings.append(s.lstrip("# ").strip())
        elif s and "<!--" not in s and "-->" not in s:
            instructions.append(s)
    instructions = [x for x in instructions if len(x) > 15][:15]
    all_text = "\n".join(_read(os.path.join(repo, rel)) for rel in docs.values())
    return {
        "template": pr_template[:4000],
        "sections": headings[:12],
        "instructions": instructions,
        "requires_dco_signoff": bool(
            re.search(r"(?i)signed-off-by|DCO\b|developer certificate", all_text)
        ),
        "requires_cla": bool(re.search(r"(?i)\bCLA\b|contributor license agreement", all_text)),
        "requires_changelog": bool(
            re.search(r"(?i)changelog|CHANGES\.[a-z]+|release notes", all_text)
        ),
        "issue_required": bool(
            re.search(
                r"(?i)(before opening a (pr|pull request)[^.]*(ticket|issue)|"
                r"open (a|an) (ticket|issue)|file (a|an) issue|create (a|an) issue)",
                all_text,
            )
        ),
    }


def build_profile(repo: str) -> dict[str, Any]:
    repo = os.path.abspath(repo)
    docs = find_docs(repo)
    docs_text = "\n".join(_read(os.path.join(repo, rel)) for rel in docs.values())
    workflows = parse_workflows(repo)
    profile = {
        "repo": repo,
        "name": os.path.basename(repo),
        "docs": docs,
        "remote": subprocess.run(
            ["git", "-C", repo, "remote", "get-url", "origin"], capture_output=True, text=True
        ).stdout.strip(),
        "ai_policy": detect_ai_policy(repo, docs),
        "workflows": workflows,
        "commands": infer_commands(workflows, docs_text),
        "commit_style": detect_commit_style(repo),
        "configs": detect_configs(repo),
        "pr": detect_pr_and_signoff(repo, docs),
        "changelog_files": [
            rel for rel in docs if "changelog" in rel.lower() or "changes." in rel.lower()
        ],
    }
    profile["ready_for_agent_pr"] = profile["ai_policy"]["category"] != "forbidden_autonomous"
    return profile


def render_markdown(p: dict[str, Any]) -> str:
    ai = p["ai_policy"]
    lines = [
        f"# Repo profile: {p['name']}",
        "",
        f"- repo: `{p['repo']}`",
        f"- remote: `{p['remote']}`",
        f"- **agent PR allowed: {'YES' if p['ready_for_agent_pr'] else 'NO'}**"
        f"  (AI policy: `{ai['category']}`)",
        f"- AI policy evidence: {ai['evidence'] or '(none found)'}",
        f"- disclosure required: {ai['requires_disclosure']}",
        f"- commit style: `{p['commit_style']['style']}` "
        f"(conventional ratio {p['commit_style']['conventional_ratio']}, "
        f"avg len {p['commit_style']['avg_length']})",
        "",
    ]
    if any(p["commands"].values()):
        lines += ["## CI commands found", ""]
        for kind in ("test", "lint", "build"):
            for cmd in p["commands"][kind]:
                lines.append(f"- {kind}: `{cmd}`")
        lines.append("")
    lines += ["## Workflow jobs", ""]
    for wf in p["workflows"]:
        lines.append(
            f"- `{wf['file']}`: jobs = {', '.join(wf.get('jobs') or []) or '?'}"
            + (f" (ERROR: {wf.get('error')})" if wf.get("error") else "")
        )
    lines += ["", "## Contribution rules", ""]
    pr = p["pr"]
    lines += [
        f"- issue required before PR: {pr['issue_required']}",
        f"- DCO sign-off required: {pr['requires_dco_signoff']}",
        f"- CLA required: {pr['requires_cla']}",
        f"- changelog entry required: {pr['requires_changelog']}",
    ]
    if pr["sections"]:
        lines.append(f"- PR template sections: {', '.join(pr['sections'])}")
    if pr.get("instructions"):
        lines.append("- PR template instructions:")
        lines += [f"  - {x}" for x in pr["instructions"][:10]]
    cfg = p["configs"]
    if cfg["pyproject_tools"] or cfg["pre_commit_hooks"]:
        lines += [
            "",
            f"- style tooling: pyproject[{', '.join(cfg['pyproject_tools'])}] "
            f"pre-commit[{', '.join(cfg['pre_commit_hooks'][:8])}]",
        ]
    lines += ["", "## Docs read", ""] + [f"- `{rel}`" for rel in p["docs"].values()]
    lines += ["", f"## Commit style samples ({p['commit_style']['style']})", ""]
    lines += [f"- `{m}`" for m in p["commit_style"]["samples"]]
    return "\n".join(lines) + "\n"


def load_docs_text(repo: str, profile: dict[str, Any], max_chars: int = 12000) -> str:
    chunks = []
    for key, rel in profile["docs"].items():
        if any(k in key for k in ("contributing", "agents", "ai_policy", "pr_template")):
            chunks.append(f"### {rel}\n" + _read(os.path.join(repo, rel))[:4000])
    return "\n\n".join(chunks)[:max_chars]


def prompt_fragment(p: dict[str, Any], docs_text: str = "", max_chars: int = 6000) -> str:
    ai = p["ai_policy"]
    parts = [
        f"REPOSITORY CONTRIBUTION RULES ({p['name']})",
        f"- AI policy category: {ai['category']} (evidence: {ai['evidence'][:200]})",
        f"- commit style: {p['commit_style']['style']}",
        f"- issue required before PR: {p['pr']['issue_required']}",
        f"- DCO sign-off: {p['pr']['requires_dco_signoff']}; CLA: {p['pr']['requires_cla']}; "
        f"changelog entry: {p['pr']['requires_changelog']}",
    ]
    for kind in ("test", "lint", "build"):
        if p["commands"][kind]:
            parts.append(f"- {kind} command(s): {'; '.join(p['commands'][kind][:3])}")
    if p["pr"]["sections"]:
        parts.append(f"- PR template sections to fill: {', '.join(p['pr']['sections'])}")
    if p["pr"].get("instructions"):
        parts.append("- PR template instructions: " + " | ".join(p["pr"]["instructions"][:6]))
    if docs_text:
        parts.append(
            "\nEXCERPTS FROM CONTRIBUTION DOCS (authoritative; follow them):\n"
            + docs_text[:max_chars]
        )
    return "\n".join(parts)
