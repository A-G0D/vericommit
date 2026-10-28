# Architecture

Status: implemented alpha (0.1.0). This document describes *how* the verification engine is put
together and which invariants it defends. It is not a research proposal and contains no benchmark
claims; see the measured status in the [README](README.md).

## 1. The contract

A commit is accepted only if it survives a fixed sequence of gates against a real repository:

```
task + repo
    │
    ▼
┌────────────────────────────────────────────────────────────────────────┐
│ L0  profile      read the repo's own contract: CONTRIBUTING/AGENTS/AI  │
│                  policy, workflows, templates, commit style, commands  │
├────────────────────────────────────────────────────────────────────────┤
│ L1  candidates   N providers propose JSON patches (files + message +   │
│                  rationale + risk). Cheap, parallel, replaceable.       │
├────────────────────────────────────────────────────────────────────────┤
│ L2  hard gates   repo_policy → git_hygiene → no_test_tamper → safety   │
│                  → build → tests.  Fail fast; bounded repair loop.      │
├────────────────────────────────────────────────────────────────────────┤
│ L3  soft layers  lint · contract · style · changelog · injection        │
│                  Score and report; never silently block.                │
├────────────────────────────────────────────────────────────────────────┤
│ L4  settle       best hard-passing candidate → change.diff, commit msg, │
│                  report.json (+ REJECTED.md if none passed)             │
└────────────────────────────────────────────────────────────────────────┘
```

The pipeline is deliberately **deterministic-first**. Model calls are the expensive, noisy part;
anything that can be answered by a subprocess, a regex, or a git command runs without a model.

## 2. Design rules

1. **Never touch the working tree.** Every candidate is written into a fresh
   `git worktree add --detach` checked out at `HEAD`. The only way a change reaches a branch is the
   explicit `--write-branch` path, which creates a *new* branch in a throwaway worktree.
2. **The repo is the oracle.** Build, test, and lint commands are taken from the repository's own
   CI workflows and contribution docs first; heuristics are only a fallback. A patch that passes our
   opinion but not the repo's own tests is not accepted.
3. **Fail fast, cheap before expensive.** `git_hygiene` (file paths, diff size) and `safety`
   (secrets, dangerous calls) run before anything executes the candidate. Tests never run on a patch
   that touched `.github/`, a lockfile, or a test file.
4. **Defense in depth on test tampering.** The default policy already forbids test/CI paths, but
   `no_test_tamper` re-checks independently so that loosening `allowed_paths` cannot let a
   self-grading patch through.
5. **Scrub the environment.** Test/build subprocesses get a copy of the environment with
   `*_API_KEY`, `*_TOKEN`, `*SECRET*`, `*PASSWORD*` removed, plus `CI=1`, `NO_COLOR=1`,
   `GIT_TERMINAL_PROMPT=0`, and a timeout.
6. **Refuse first, generate second.** `repo_policy` runs before any provider call when the repo
   profile says its contribution docs forbid autonomous agents. The engine raises instead of
   spending tokens to produce a patch it must never submit.
7. **Everything is auditable.** Each run writes `report.json` with per-candidate per-layer results,
   token counts and cost; rejected runs write `REJECTED.md` with the failing gate text.

## 3. Components

| module | responsibility |
|---|---|
| `vericommit/cli.py` | argument parsing and the user-facing commands (`run`, `check`, `profile`, `pr`, `track`, `batch`, `providers`) |
| `vericommit/engine.py` | context collection, candidate fan-out + repair loop, settlement, new-branch commit |
| `vericommit/layers.py` | the 11 verification layers and `run_checks` ordering |
| `vericommit/conventions.py` | repo contract extraction: AI policy, workflows → commands, commit style, PR template, DCO/CLA/changelog rules |
| `vericommit/policy.py` | defaults and glob matching for what a commit may touch |
| `vericommit/providers.py` | OpenAI-compatible HTTP, Claude Code CLI, mock; JSON extraction; key discovery |
| `vericommit/gitops.py` | worktree lifecycle, path-safe file application, diff/status helpers, scrubbed env, repo detection |
| `vericommit/testing.py` | deterministic demo repo fixtures used by tests and `demo/run_mock_demo.sh` |

## 4. Failure model

The gates target the failure classes that are *deterministically detectable*:

- the patch does not build or import;
- the repo's tests fail, error, or run zero tests;
- a test, CI workflow, lockfile, dependency manifest, or `.env` was modified;
- a secret or a dangerous call was introduced;
- the public API of a Python module was removed or its signature changed;
- the diff is out of scope (too many files/lines, forbidden paths, binary or oversized files);
- the commit message violates the repo's convention;
- the repo's contribution rules require a changelog/DCO handling that is missing;
- the model was fed prompt-injection text.

They do **not** prove semantic correctness (the repo's tests are the oracle, and the repo's tests can
be weak). That limitation is why the tool is framed as a *gate*, not a verifier of truth.

## 5. Extension points

- **Providers:** implement `Provider.complete()` returning a `Completion`; add a spec branch in
  `build_provider()`.
- **Layers:** add a function `f(ctx: Context) -> CheckResult`, register it in `LAYERS`, and add it to
  `hard_checks` or `soft_checks` in the policy. Hard layers must be deterministic and cheap enough
  to run before the build.
- **Sandboxing:** `build` and `tests` both shell out through `gitops.run`; swapping in a container
  backend is a single call-site change once a backend exists.
- **Calibration:** the report format is the input for measuring false-accept / false-alarm rate per
  layer on a labeled corpus. This is the next milestone before any layer is trusted as more than a
  necessary condition.
