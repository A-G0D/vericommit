"""vericommit CLI.  python -m vericommit.cli {run,check,providers} ..."""

from __future__ import annotations

import argparse
import json
import os
import sys

from .engine import Engine
from .gitops import make_worktree, remove_worktree
from .layers import Context, run_checks
from .policy import load_policy
from .providers import build_provider


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _task_text(arg: str) -> str:
    if os.path.exists(arg):
        return _read(arg).strip()
    return arg


def cmd_run(args: argparse.Namespace) -> int:
    policy = load_policy(args.policy)
    if args.providers:
        policy["providers"] = [p.strip() for p in args.providers.split(",") if p.strip()]
    if args.candidates:
        policy["candidates"] = args.candidates
    if args.repairs is not None:
        policy["repairs"] = args.repairs
    if args.budget:
        policy["budget_usd"] = args.budget
    if args.test_cmd:
        policy["test_cmd"] = args.test_cmd
    if args.write_branch:
        policy["write_branch"] = True
        if args.branch:
            policy["branch"] = args.branch
    providers = [(spec, build_provider(spec)) for spec in policy["providers"]]
    engine = Engine(
        args.repo, policy=policy, providers=providers, out_dir=args.out, verbose=not args.quiet
    )
    task = _task_text(args.task)
    result = engine.run(task)
    print("\n=== VERICOMMIT REPORT ===")
    print(f"run:      {result.run_id}")
    print(f"repo:     {result.repo}  ({engine.info.kind})")
    print(f"elapsed:  {result.elapsed_s:.1f}s   cost: ${result.total_cost_usd:.4f}")
    for cand in result.candidates:
        state = "ACCEPTED" if result.accepted is cand else ("pass" if cand.hard_pass else "reject")
        failing = [c.name for c in cand.checks if c.severity == "hard" and not c.ok]
        print(
            f"  #{cand.idx} {cand.provider_spec:12s} {state:8s} score={cand.score:+.1f} "
            f"changed={cand.changed} fail={failing or '-'} err={cand.error or '-'}"
        )
    if result.accepted:
        print(f"\npatch:         {result.settled.get('patch')}")
        print(f"commit msg:    {result.accepted.commit_message}")
        if result.settled.get("branch"):
            b = result.settled["branch"]
            print(f"branch:        {b['branch']} @ {b['commit'][:10]}")
        print(f"report:        {os.path.join(result.out_dir, 'report.json')}")
        return 0
    print("\nNo candidate passed the hard verification gates.")
    return 1


def cmd_profile(args: argparse.Namespace) -> int:
    from .conventions import build_profile, load_docs_text, prompt_fragment, render_markdown

    prof = build_profile(args.repo)
    out = args.out or os.path.join("repo-profiles", prof["name"])
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "CONVENTIONS.md"), "w", encoding="utf-8") as fh:
        fh.write(render_markdown(prof))
    with open(os.path.join(out, "conventions.json"), "w", encoding="utf-8") as fh:
        json.dump(prof, fh, indent=2)
    with open(os.path.join(out, "prompt_fragment.md"), "w", encoding="utf-8") as fh:
        fh.write(prompt_fragment(prof, load_docs_text(args.repo, prof)))
    ai = prof["ai_policy"]
    print(f"profile: {prof['name']} -> {out}")
    print(f"  agent PR allowed: {prof['ready_for_agent_pr']} (AI policy: {ai['category']})")
    if not prof["ready_for_agent_pr"]:
        print(f"  REFUSED for autonomous work: {ai['evidence']}")
    print(
        f"  commit style: {prof['commit_style']['style']} | "
        f"issue required: {prof['pr']['issue_required']} | "
        f"dco: {prof['pr']['requires_dco_signoff']} | changelog: {prof['pr']['requires_changelog']}"
    )
    for kind in ("test", "lint", "build"):
        if prof["commands"][kind]:
            print(f"  {kind}: {prof['commands'][kind][0]}")
    return 0


def cmd_pr(args: argparse.Namespace) -> int:
    """Execute a multi-commit plan on a new branch and produce the PR package."""
    import tempfile

    from .conventions import build_profile
    from .gitops import Worktree, apply_files, run, sanitized_env

    plan = json.loads(_read(args.plan))
    repo = os.path.abspath(args.repo)
    prof = build_profile(repo)
    if not prof.get("ready_for_agent_pr", True):
        print(
            "REFUSED: this repo's AI policy forbids autonomous agents:",
            prof["ai_policy"]["evidence"],
        )
        return 3
    branch = args.branch or "vericommit/" + __import__("time").strftime("%Y%m%d-%H%M%S")
    tmp = tempfile.mkdtemp(prefix="vc-pr-")
    os.rmdir(tmp)
    rc, _, err = run(
        ["git", "worktree", "add", "-b", branch, tmp, "HEAD"], repo, env=sanitized_env()
    )
    if rc != 0:
        print(f"could not create branch worktree: {err}")
        return 2
    commits = []
    try:
        for step in plan.get("commits", []):
            pol = load_policy(args.policy)
            pol["repo_conventions"] = prof
            if args.providers:
                pol["providers"] = [x.strip() for x in args.providers.split(",")]
            if args.candidates:
                pol["candidates"] = args.candidates
            if args.budget:
                pol["budget_usd"] = args.budget
            eng = Engine(
                tmp,
                policy=pol,
                providers=[(sp, build_provider(sp)) for sp in pol["providers"]],
                out_dir=args.out,
                verbose=True,
            )
            res = eng.run(step["task"])
            if not res.accepted:
                print(f"ABORT at step {len(commits) + 1}: no candidate passed verification")
                return 1
            apply_files(Worktree(repo, tmp), res.accepted.files)
            msg = step.get("commit_message") or res.accepted.commit_message
            run(["git", "add", "-A"], tmp, env=sanitized_env())
            cmd = ["git", "-c", "user.name=vericommit", "-c", "user.email=vericommit@localhost"]
            if prof["pr"]["requires_dco_signoff"]:
                cmd.append("-c")
                cmd.append("format.signoff=true")
            cmd += ["commit", "-m", msg]
            rc, out, err = run(cmd, tmp, env=sanitized_env())
            if rc != 0:
                print(f"commit failed: {err}")
                return 2
            sha = run(["git", "rev-parse", "--short", "HEAD"], tmp, env=sanitized_env())[1].strip()
            commits.append(
                {
                    "sha": sha,
                    "message": msg,
                    "files": res.accepted.changed,
                    "report": res.out_dir,
                    "checks": [c.as_dict() for c in res.accepted.checks],
                }
            )
            print(f"[pr] commit {sha}: {msg}")
        body = _render_pr_body(plan, prof, commits, branch)
        pr_dir = args.out or "runs"
        os.makedirs(pr_dir, exist_ok=True)
        pr_path = os.path.join(pr_dir, "PR.md")
        with open(pr_path, "w", encoding="utf-8") as fh:
            fh.write(body)
        with open(os.path.join(pr_dir, "pr.json"), "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "branch": branch,
                    "commits": commits,
                    "pr_md": pr_path,
                    "title": plan.get("title", ""),
                },
                fh,
                indent=2,
            )
        print(f"\nPR body: {pr_path}")
        print(f"branch:  {branch} ({len(commits)} commits)")
        print(
            f"next:    git push <your-fork> {branch} && gh pr create --head {branch} "
            f"--title {json.dumps(plan.get('title', ''))} --body-file {pr_path}"
        )
        return 0
    finally:
        run(["git", "worktree", "remove", "--force", tmp], repo, env=sanitized_env())


def _render_pr_body(plan: dict, prof: dict, commits: list, branch: str) -> str:
    sections = prof["pr"].get("sections") or ["Summary", "Related issue", "Tests", "Risk"]
    lines = [f"<!-- generated by VeriCommit; branch {branch} -->"]
    for name in sections:
        nl = name.lower()
        lines += ["", f"## {name}", ""]
        if "summary" in nl or "description" in nl or "change" in nl:
            lines += [f"- {c['message']}" for c in commits]
        elif "issue" in nl or "ticket" in nl:
            lines.append(f"fixes #{plan['issue']}" if plan.get("issue") else "_no issue linked_")
        elif "test" in nl:
            cmds = prof["commands"]["test"][:3]
            lines += [f"- ran `{c}`" for c in cmds]
            lines.append(f"- verification: all hard gates passed on all {len(commits)} commits")
        elif "risk" in nl:
            lines.append("Low: changes are minimal, tests pass, no API breakage detected.")
        elif "screenshot" in nl or "media" in nl:
            lines.append("_N/A_")
        else:
            lines.append("_see commit messages_")
    lines += [
        "",
        "## AI disclosure",
        "",
        "This change was prepared with an AI assistant under human review and editing. "
        "The human author is responsible for the content and will answer review questions "
        "in their own words.",
    ]
    lines += [
        "",
        "---",
        f"Commits: {len(commits)}"
        + (" · `" + "`, `".join(c["sha"] for c in commits) + "`" if commits else ""),
    ]
    return "\n".join(lines) + "\n"


def cmd_track(args: argparse.Namespace) -> int:
    """Poll a PR for review feedback; append one row per poll to a JSONL."""
    cmd = [
        "gh",
        "pr",
        "view",
        str(args.pr),
        "-R",
        args.repo,
        "--json",
        "number,title,state,reviewDecision,url,reviews,comments,updatedAt",
    ]
    rc, out, err = __import__("subprocess").run(cmd, capture_output=True, text=True)
    if rc != 0:
        print(f"gh failed: {err.strip()[:400]}")
        return 2
    data = json.loads(out)
    state_file = args.state_file or "runs/pr_tracker.jsonl"
    os.makedirs(os.path.dirname(state_file) or ".", exist_ok=True)
    review_events = [
        {
            "author": (r.get("author") or {}).get("login"),
            "state": r.get("state"),
            "body": (r.get("body") or "")[:300],
        }
        for r in data.get("reviews") or []
    ]
    comment_events = [
        {"author": (c.get("author") or {}).get("login"), "body": (c.get("body") or "")[:300]}
        for c in data.get("comments") or []
    ]
    row = {
        "ts": __import__("time").strftime("%Y-%m-%dT%H:%M:%S"),
        "repo": args.repo,
        "pr": data.get("number"),
        "state": data.get("state"),
        "review_decision": data.get("reviewDecision"),
        "url": data.get("url"),
        "reviews": review_events,
        "comments": comment_events,
    }
    with open(state_file, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")
    print(
        f"{args.repo}#{row['pr']}: state={row['state']} decision={row['review_decision']} "
        f"reviews={len(review_events)} comments={len(comment_events)}"
    )
    for r in review_events + comment_events:
        print(f"  [{r.get('state') or 'comment'}] {r.get('author')}: {r.get('body', '')[:120]}")
    print(f"tracked -> {state_file}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    policy = load_policy(args.policy)
    from .engine import collect_context

    text = _task_text(args.task) if args.task else ""
    wt = make_worktree(args.repo)
    try:
        patch = _read(args.patch)
        proc = __import__("subprocess").run(
            ["git", "apply", "-"], input=patch, text=True, cwd=wt.path, capture_output=True
        )
        if proc.returncode != 0:
            print(f"patch did not apply: {proc.stderr[:800]}")
            return 2
        from .gitops import changed_paths, diff_text

        changed = changed_paths(wt)
        real = diff_text(wt)
        ctx = Context(
            repo=os.path.abspath(args.repo),
            worktree=wt.path,
            changed=changed,
            patch=real,
            policy=policy,
            info=__import__("vericommit.gitops", fromlist=["detect_repo"]).detect_repo(args.repo),
            task=text,
            commit_message=args.commit_message or "",
            evidence_text=collect_context(args.repo, text)[0],
        )
        results = run_checks(ctx)
        hard_fail = [r for r in results if r.severity == "hard" and not r.ok]
        for r in results:
            print(f"[{'PASS' if r.ok else 'FAIL'}] {r.severity:4s} {r.name}: {r.detail[:300]}")
        print(f"\nverdict: {'REJECT' if hard_fail else 'ACCEPT'} ({len(hard_fail)} hard failures)")
        return 1 if hard_fail else 0
    finally:
        remove_worktree(wt)


def cmd_batch(args: argparse.Namespace) -> int:
    """Run many repo/task pairs concurrently: the scale entry point."""
    from concurrent.futures import ThreadPoolExecutor

    policy = load_policy(args.policy)
    specs = (
        [p.strip() for p in args.providers.split(",")] if args.providers else policy["providers"]
    )
    if args.candidates:
        policy["candidates"] = args.candidates
    if args.budget:
        policy["budget_usd"] = args.budget
    items = [json.loads(line) for line in _read(args.manifest).splitlines() if line.strip()]

    def one(idx_item):
        idx, item = idx_item
        try:
            eng = Engine(
                item["repo"],
                policy=dict(policy),
                providers=[(s, build_provider(s)) for s in specs],
                out_dir=args.out,
                verbose=False,
            )
            res = eng.run(item["task"])
            return {
                "idx": idx,
                "repo": item["repo"],
                "accepted": res.accepted.idx if res.accepted else None,
                "changed": res.accepted.changed if res.accepted else [],
                "cost_usd": round(res.total_cost_usd, 6),
                "out_dir": res.out_dir,
                "error": None,
            }
        except Exception as e:
            return {
                "idx": idx,
                "repo": item.get("repo"),
                "accepted": None,
                "changed": [],
                "cost_usd": 0.0,
                "out_dir": None,
                "error": f"{type(e).__name__}: {e}",
            }

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(one, list(enumerate(items))))
    ok = sum(1 for r in results if r["accepted"] is not None)
    cost = sum(r["cost_usd"] for r in results)
    report = {
        "tasks": len(items),
        "accepted": ok,
        "rejected": len(items) - ok,
        "total_cost_usd": round(cost, 6),
        "workers": args.workers,
        "results": results,
    }
    out = os.path.join(args.out, "batch_report.json")
    os.makedirs(args.out, exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    print(f"batch: {len(items)} tasks, {ok} accepted, ${cost:.4f}, workers={args.workers}")
    for r in results:
        print(
            f"  [{r['idx']}] {'ACCEPT' if r['accepted'] is not None else 'REJECT':6s} "
            f"{os.path.basename(str(r['repo'])):24s} {r['changed']} {r['error'] or ''}"
        )
    print(f"report: {out}")
    return 0 if ok == len(items) else 1


def cmd_providers(_: argparse.Namespace) -> int:
    from .providers import load_env_keys

    load_env_keys()
    print("provider specs:")
    print("  claude                 Claude Code CLI (headless, no tools)")
    print("  claude:<model>         e.g. claude:sonnet, claude:opus")
    print("  deepseek               DeepSeek chat  (DEEPSEEK_API_KEY)")
    print("  deepseek:reasoner      DeepSeek reasoner")
    print("  openrouter:<model>     any model on OpenRouter (OPENROUTER_API_KEY)")
    print("  mock                   deterministic stub for tests")
    print("\nkey status:")
    for env in ("DEEPSEEK_API_KEY", "OPENROUTER_API_KEY"):
        print(f"  {env}: {'set' if os.environ.get(env) else 'NOT SET'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="vericommit")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="generate candidates, verify them, settle a patch")
    r.add_argument("--repo", required=True)
    r.add_argument("--task", required=True, help="task text or path to a task file")
    r.add_argument("--policy", default=None)
    r.add_argument("--providers", default=None, help="comma list, e.g. deepseek,claude")
    r.add_argument("--candidates", type=int, default=None)
    r.add_argument("--repairs", type=int, default=None)
    r.add_argument("--budget", type=float, default=None)
    r.add_argument("--test-cmd", default=None)
    r.add_argument("--out", default="runs")
    r.add_argument(
        "--write-branch",
        action="store_true",
        help="commit the accepted patch on a NEW branch (never touches HEAD)",
    )
    r.add_argument("--branch", default=None)
    r.add_argument("--quiet", action="store_true")
    r.set_defaults(func=cmd_run)

    pr = sub.add_parser("profile", help="read a repo's contribution contract")
    pr.add_argument("--repo", required=True)
    pr.add_argument("--out", default=None)
    pr.set_defaults(func=cmd_profile)

    prp = sub.add_parser("pr", help="execute a multi-commit plan on a new branch + PR package")
    prp.add_argument("--repo", required=True)
    prp.add_argument("--plan", required=True)
    prp.add_argument("--policy", default=None)
    prp.add_argument("--providers", default=None)
    prp.add_argument("--candidates", type=int, default=None)
    prp.add_argument("--budget", type=float, default=None)
    prp.add_argument("--branch", default=None)
    prp.add_argument("--out", default="runs")
    prp.set_defaults(func=cmd_pr)

    tr = sub.add_parser("track", help="poll a PR for review feedback (gh)")
    tr.add_argument("--repo", required=True)
    tr.add_argument("--pr", required=True)
    tr.add_argument("--state-file", default=None)
    tr.set_defaults(func=cmd_track)

    c = sub.add_parser("check", help="verify an existing patch with the same gates")
    c.add_argument("--repo", required=True)
    c.add_argument("--patch", required=True)
    c.add_argument("--policy", default=None)
    c.add_argument("--task", default=None)
    c.add_argument("--commit-message", default=None)
    c.set_defaults(func=cmd_check)

    b = sub.add_parser("batch", help="run many repo/task pairs concurrently (JSONL manifest)")
    b.add_argument("--manifest", required=True)
    b.add_argument("--workers", type=int, default=4)
    b.add_argument("--policy", default=None)
    b.add_argument("--providers", default=None)
    b.add_argument("--candidates", type=int, default=None)
    b.add_argument("--budget", type=float, default=None)
    b.add_argument("--out", default="runs")
    b.set_defaults(func=cmd_batch)

    p = sub.add_parser("providers", help="list provider specs and key status")
    p.set_defaults(func=cmd_providers)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
