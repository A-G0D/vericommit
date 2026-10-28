"""The pipeline: context -> candidates (any provider) -> verification -> select -> artifact."""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from .conventions import build_profile, load_docs_text, prompt_fragment
from .gitops import (
    Worktree,
    apply_files,
    changed_paths,
    detect_repo,
    diff_text,
    make_worktree,
    remove_worktree,
    run,
    sanitized_env,
)
from .layers import CheckResult, Context, run_checks
from .policy import load_policy
from .providers import Completion, Provider, ProviderError, extract_json

SYSTEM_PROMPT = """You are a senior software engineer making ONE minimal, production-ready commit.

Output ONLY a JSON object with exactly these keys:
{
  "files": {"relative/path.py": "FULL new file content", ...},
  "commit_message": "type(scope): summary in conventional-commit form",
  "rationale": "2-4 sentences: what changed and why",
  "risk": "one sentence on the riskiest part of the change"
}

Rules:
- Return FULL file contents for every file you change (the engine writes the files verbatim).
- Change as few files and lines as possible. No drive-by refactors, no renames, no formatting churn.
- NEVER modify tests, CI config, dependency manifests, lockfiles, or .env files.
- Follow the repository's existing style: naming, imports, error handling, line length.
- Do not add secrets, tokens, passwords, or credentials.
- Do not add debug prints, TODOs, or commented-out code.
- If the task is impossible or ambiguous, return {"files": {}, "commit_message": "", "rationale": "why", "risk": ""}.
No markdown fences. No prose outside the JSON object."""


@dataclass
class Candidate:
    idx: int
    provider_spec: str
    completion: Completion | None = None
    files: dict[str, str] = field(default_factory=dict)
    commit_message: str = ""
    rationale: str = ""
    risk: str = ""
    patch: str = ""
    changed: list[str] = field(default_factory=list)
    checks: list[CheckResult] = field(default_factory=list)
    hard_pass: bool = False
    score: float = 0.0
    attempts: int = 0
    error: str | None = None
    history: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "idx": self.idx,
            "provider": self.provider_spec,
            "hard_pass": self.hard_pass,
            "score": round(self.score, 3),
            "changed": self.changed,
            "attempts": self.attempts,
            "error": self.error,
            "commit_message": self.commit_message,
            "rationale": self.rationale,
            "risk": self.risk,
            "completion": self.completion.as_dict() if self.completion else None,
            "checks": [c.as_dict() for c in self.checks],
            "history": self.history,
        }


@dataclass
class RunResult:
    run_id: str
    repo: str
    task: str
    candidates: list[Candidate]
    accepted: Candidate | None
    total_cost_usd: float
    out_dir: str
    elapsed_s: float
    settled: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "repo": self.repo,
            "task": self.task,
            "accepted_candidate": self.accepted.idx if self.accepted else None,
            "total_cost_usd": round(self.total_cost_usd, 6),
            "elapsed_s": round(self.elapsed_s, 2),
            "out_dir": self.out_dir,
            "candidates": [c.as_dict() for c in self.candidates],
            "settled": self.settled,
        }


def _read_text(path: str, limit: int | None = None) -> str | None:
    """Read a text file, returning None when it cannot be read."""
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            return fh.read() if limit is None else fh.read(limit)
    except OSError:
        return None


def _git(repo: str, *args: str) -> tuple[int, str, str]:
    return run(["git", *args], repo, env=sanitized_env())


def collect_context(
    repo: str, task: str, max_files: int = 6, per_file: int = 4000, total: int = 14000
) -> tuple[str, list[str]]:
    rc, out, _ = _git(repo, "ls-files")
    files = [f for f in out.splitlines() if f.strip()] if rc == 0 else []
    code = [
        f
        for f in files
        if f.endswith(
            (".py", ".js", ".ts", ".rs", ".go", ".java", ".md", ".toml", ".json", ".yml", ".yaml")
        )
    ]
    words = set(re.findall(r"[a-zA-Z_]{3,}", task.lower()))

    def score(path: str) -> int:
        s = sum(3 for w in words if w in path.lower())
        head = _read_text(os.path.join(repo, path), 3000)
        if head:
            s += sum(1 for w in words if w in head.lower())
        return s

    ranked = sorted(code, key=score, reverse=True)[:max_files]
    chunks, used = [], 0
    for path in ranked:
        if score(path) == 0 and not path.lower().startswith(("readme", "agents")):
            continue
        text = _read_text(os.path.join(repo, path), per_file)
        if text is None:
            continue
        if used + len(text) > total:
            text = text[: max(0, total - used)]
        if not text:
            break
        chunks.append(f"### FILE {path}\n{text}")
        used += len(text)
    return ("\n\n".join(chunks), ranked)


def build_user_prompt(task: str, repo: str, context: str, tree: list[str], policy: dict) -> str:
    tree_text = "\n".join(tree[:300])
    return f"""TASK
{task}

REPOSITORY TREE (truncated)
{tree_text}

RELEVANT FILES
{context}

CONSTRAINTS
- allowed paths: {policy.get("allowed_paths")}
- forbidden paths: {policy.get("forbidden_paths")}
- max files changed: {policy["max_files_changed"]}, max diff lines: {policy["max_diff_lines"]}
Return the JSON object now."""


class Engine:
    def __init__(
        self,
        repo: str,
        policy: dict[str, Any] | None = None,
        providers: list[tuple[str, Provider]] | None = None,
        out_dir: str = "runs",
        verbose: bool = True,
    ):
        self.repo = os.path.abspath(repo)
        self.policy = policy or load_policy(None)
        self.providers = providers or []
        self.out_dir = out_dir
        self.verbose = verbose
        self.info = detect_repo(self.repo)
        self.total_cost = 0.0
        # Read the repo's contribution contract (AI policy, workflows, style).
        self.profile = policy.get("repo_conventions") or build_profile(self.repo)
        self.policy["repo_conventions"] = self.profile
        cmds = self.profile.get("commands") or {}
        if not self.policy.get("test_cmd") and cmds.get("test"):
            self.policy["test_cmd"] = cmds["test"][0]
        if not self.policy.get("lint_cmd") and cmds.get("lint"):
            self.policy["lint_cmd"] = cmds["lint"][0]
        if not self.policy.get("build_cmd") and cmds.get("build"):
            self.policy["build_cmd"] = cmds["build"][0]
        self.docs_text = load_docs_text(self.repo, self.profile)
        self.system_prompt = (
            SYSTEM_PROMPT + "\n\n" + prompt_fragment(self.profile, self.docs_text)
            if self.docs_text
            else SYSTEM_PROMPT
        )
        ai = self.profile.get("ai_policy") or {}
        self.log(
            f"[profile] {self.profile['name']}: ai_policy={ai.get('category')} "
            f"agent_pr_allowed={self.profile.get('ready_for_agent_pr')} "
            f"commit_style={self.profile['commit_style']['style']} "
            f"cmd(test/lint/build)={bool(cmds.get('test'))}/{bool(cmds.get('lint'))}/{bool(cmds.get('build'))}"
        )

    def log(self, msg: str) -> None:
        if self.verbose:
            print(msg, flush=True)

    def _budget_left(self) -> float:
        return float(self.policy["budget_usd"]) - self.total_cost

    def _make_context(self, task: str, wt: Worktree, provider_spec: str) -> tuple[Context, str]:
        context, picked = collect_context(self.repo, task)
        rc, tree_out, _ = _git(self.repo, "ls-files")
        tree = tree_out.splitlines() if rc == 0 else []
        user = build_user_prompt(task, self.repo, context, tree, self.policy)
        ctx = Context(
            repo=self.repo,
            worktree=wt.path,
            changed=[],
            patch="",
            policy=self.policy,
            info=self.info,
            task=task,
            evidence_text=context,
        )
        return ctx, user

    def _evaluate_once(
        self, provider: Provider, spec: str, system: str, user: str, attempt_idx: int
    ) -> tuple[dict[str, Any], Worktree | None, str, list[str], str]:
        completion = provider.complete(system, user)
        self.total_cost += completion.cost_usd
        parsed = extract_json(completion.text)
        files = parsed.get("files") or {}
        if not isinstance(files, dict):
            raise ProviderError("files must be an object")
        wt = make_worktree(self.repo)
        try:
            if files:
                apply_files(wt, files)
            patch = diff_text(wt)
            changed = changed_paths(wt)
        except Exception:
            remove_worktree(wt)
            raise
        return parsed, wt, patch, changed, completion

    def _attempt(
        self, cand: Candidate, provider: Provider, spec: str, system: str, task: str
    ) -> None:
        wt = make_worktree(self.repo)
        try:
            ctx, user = self._make_context(task, wt, spec)
            ctx.commit_message = ""
            feedback = ""
            for attempt in range(1 + int(self.policy["repairs"])):
                cand.attempts = attempt + 1
                if self._budget_left() <= 0:
                    cand.error = "budget exhausted"
                    return
                prompt = user + (
                    "\n\nPREVIOUS ATTEMPT FAILED VERIFICATION:\n" + feedback if feedback else ""
                )
                try:
                    parsed, wt2, patch, changed, completion = self._evaluate_once(
                        provider, spec, system, prompt, attempt
                    )
                except ProviderError as e:
                    cand.error = f"provider error: {e}"
                    cand.history.append({"attempt": attempt + 1, "error": str(e)})
                    feedback = f"Your previous output was rejected: {e}"
                    continue
                try:
                    remove_worktree(wt)
                finally:
                    wt = wt2  # reuse the fresh worktree for checks
                cand.completion = completion
                cand.files = parsed.get("files") or {}
                cand.commit_message = str(parsed.get("commit_message") or "")
                cand.rationale = str(parsed.get("rationale") or "")
                cand.risk = str(parsed.get("risk") or "")
                cand.patch, cand.changed = patch, changed
                ctx.worktree, ctx.patch, ctx.changed = wt.path, patch, changed
                ctx.commit_message = cand.commit_message
                checks = run_checks(ctx)
                cand.checks = checks
                hard_fail = [c for c in checks if c.severity == "hard" and not c.ok]
                cand.hard_pass = not hard_fail and bool(cand.files) and bool(changed)
                soft = [c for c in checks if c.severity == "soft"]
                cand.score = (
                    sum(1.0 for c in soft if c.ok)
                    - 2.0 * sum(1 for c in soft if not c.ok)
                    + (2.0 if cand.hard_pass else 0.0)
                )
                cand.history.append(
                    {
                        "attempt": attempt + 1,
                        "hard_pass": cand.hard_pass,
                        "failing": [c.name for c in hard_fail],
                        "cost_usd": round(
                            getattr(cand.completion or Completion("", "", ""), "cost_usd", 0), 6
                        ),
                    }
                )
                if cand.hard_pass:
                    return
                if not hard_fail:
                    cand.error = "no files changed"
                    return
                feedback = "\n".join(f"- {c.name}: {c.detail[:600]}" for c in hard_fail)
                cand.error = "verification failed: " + ", ".join(c.name for c in hard_fail)
        finally:
            remove_worktree(wt)

    def run(self, task: str) -> RunResult:
        if not self.profile.get("ready_for_agent_pr", True) and not self.policy.get(
            "allow_autonomous_agent"
        ):
            raise RuntimeError(
                "refusing to run: repo AI policy forbids autonomous agents "
                f"({self.profile['ai_policy'].get('evidence')})"
            )
        t0 = time.time()
        run_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        run_dir = os.path.join(self.out_dir, run_id)
        os.makedirs(run_dir, exist_ok=True)
        n = int(self.policy["candidates"])
        specs = self.policy["providers"]
        pairs: list[tuple[str, Provider]] = []
        for i in range(n):
            if i < len(self.providers):
                pairs.append(self.providers[i])
            else:
                spec = specs[i % len(specs)] if specs else "mock"
                from .providers import build_provider

                pairs.append((spec, build_provider(spec)))
        candidates = [Candidate(idx=i, provider_spec=spec) for i, (spec, _) in enumerate(pairs)]
        self.log(
            f"[engine] repo={self.repo} ({self.info.kind}) candidates={len(pairs)} "
            f"budget=${self.policy['budget_usd']}"
        )

        def work(cand: Candidate, spec: str, provider: Provider) -> None:
            try:
                self._attempt(cand, provider, spec, SYSTEM_PROMPT, task)
            except Exception as e:  # never let one candidate kill the run
                cand.error = f"{type(e).__name__}: {e}"
            self.log(
                f"[candidate {cand.idx}] {spec}: hard_pass={cand.hard_pass} "
                f"score={cand.score:.1f} changed={cand.changed} err={cand.error}"
            )

        with ThreadPoolExecutor(max_workers=min(len(pairs), 8)) as pool:
            futures = [pool.submit(work, c, s, p) for c, (s, p) in zip(candidates, pairs)]
            for f in futures:
                f.result()

        accepted = None
        for cand in sorted(candidates, key=lambda c: (c.hard_pass, c.score), reverse=True):
            if cand.hard_pass:
                accepted = cand
                break
        settled = self._settle(run_dir, accepted, candidates)
        result = RunResult(
            run_id=run_id,
            repo=self.repo,
            task=task,
            candidates=candidates,
            accepted=accepted,
            total_cost_usd=self.total_cost,
            out_dir=run_dir,
            elapsed_s=time.time() - t0,
            settled=settled,
        )
        with open(os.path.join(run_dir, "report.json"), "w", encoding="utf-8") as fh:
            json.dump(result.as_dict(), fh, indent=2)
        self.log(
            f"[engine] done in {result.elapsed_s:.1f}s cost=${result.total_cost_usd:.4f} "
            f"accepted={accepted.idx if accepted else None} -> {run_dir}"
        )
        return result

    def _settle(
        self, run_dir: str, accepted: Candidate | None, candidates: list[Candidate]
    ) -> dict[str, Any]:
        if accepted is None:
            with open(os.path.join(run_dir, "REJECTED.md"), "w", encoding="utf-8") as fh:
                fh.write("# No candidate passed the hard gates\n\n")
                for c in candidates:
                    fh.write(f"## candidate {c.idx} ({c.provider_spec})\n\n")
                    for chk in c.checks:
                        fh.write(
                            f"- {chk.name} [{'PASS' if chk.ok else 'FAIL'}]: {chk.detail[:500]}\n"
                        )
                    fh.write("\n")
            return {"status": "rejected"}
        patch_path = os.path.join(run_dir, "change.diff")
        with open(patch_path, "w", encoding="utf-8") as fh:
            fh.write(accepted.patch)
        with open(os.path.join(run_dir, "commit_message.txt"), "w", encoding="utf-8") as fh:
            fh.write(accepted.commit_message + "\n")
        meta = {
            "status": "accepted",
            "candidate": accepted.idx,
            "patch": patch_path,
            "changed": accepted.changed,
        }
        if self.policy.get("write_branch"):
            branch = self.policy.get("branch") or f"vericommit/{run_dir.split(os.sep)[-1]}"
            meta["branch"] = write_branch(
                self.repo, accepted.files, accepted.commit_message, branch
            )
        return meta


def write_branch(repo: str, files: dict[str, str], message: str, branch: str) -> dict[str, Any]:
    """Create a real commit on a NEW branch via a throwaway worktree; never touches HEAD."""
    import tempfile

    tmp = tempfile.mkdtemp(prefix="vc-commit-")
    os.rmdir(tmp)
    rc, _, err = run(["git", "worktree", "add", "--detach", tmp, "HEAD"], repo, env=sanitized_env())
    if rc != 0:
        raise RuntimeError(f"could not create commit worktree: {err}")
    try:
        wt = Worktree(repo, tmp)
        apply_files(wt, files)
        wt.git("checkout", "-b", branch)
        wt.git("add", "-A")
        rc, out, err = run(
            [
                "git",
                "-c",
                "user.name=vericommit",
                "-c",
                "user.email=vericommit@localhost",
                "commit",
                "-m",
                message,
            ],
            tmp,
            env=sanitized_env(),
        )
        if rc != 0:
            raise RuntimeError(f"git commit failed: {err}")
        rc, sha, _ = run(["git", "rev-parse", "HEAD"], tmp, env=sanitized_env())
        return {"branch": branch, "commit": sha.strip()}
    finally:
        run(["git", "worktree", "remove", "--force", tmp], repo, env=sanitized_env())
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
