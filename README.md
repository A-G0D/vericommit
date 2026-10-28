# VeriCommit

[![CI](https://github.com/A-G0D/vericommit/actions/workflows/ci.yml/badge.svg)](https://github.com/A-G0D/vericommit/actions/workflows/ci.yml)
[![CodeQL](https://github.com/A-G0D/vericommit/actions/workflows/codeql.yml/badge.svg)](https://github.com/A-G0D/vericommit/actions/workflows/codeql.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**Model-agnostic, verification-first commit engine.** Any model proposes a patch; a stack of
deterministic gates decides whether it is allowed to become a commit.

```
task + repo ──► context ──► candidates (DeepSeek / Claude / OpenRouter / mock)
                                │
                                ▼
     hard gates (fail fast): repo policy → git hygiene → no test tampering
                             → safety → build → tests
     soft layers (score):    lint · public-API contract · style · changelog · injection
                                │
                                ▼
              accepted patch + commit message + verification report
```

Agentic contributors made code generation cheap. The bottleneck moved to **verification**:
does the change build, do the repo's own tests pass, was a test or CI file quietly modified,
did a secret or a public-API break slip in? VeriCommit treats a model as an untrusted proposal
source and puts the evidence in charge.

Status: **alpha (0.1.0)**, measured below. Interfaces may change before 0.2.

## Why

Real commits need safety, not vibes. The engine never trusts the model: it writes the candidate
into a throwaway `git worktree`, runs the repo's own build/tests with a scrubbed environment and
timeouts, scans for secrets and dangerous calls, enforces path/diff policies, detects public-API
breakage and style drift, and only then produces a patch on a new branch. **Nothing touches your
working tree or current branch.**

## Install

```bash
pip install git+https://github.com/A-G0D/vericommit.git   # or: pip install -e ".[dev]"
vericommit --help
```

Requires Python 3.10+. The only runtime dependency is PyYAML (used to read repo CI workflows).

## Quickstart

```bash
# 1. list provider specs and whether keys are visible
vericommit providers

# 2. generate + verify a patch for a task (default provider: deepseek)
vericommit run --repo /path/to/repo \
  --task "fix the bug in app/calc.py: add() returns a-b" \
  --providers deepseek --candidates 2 --budget 0.5

# 3. verify an existing (human or agent) patch with the same gates
git -C /path/to/repo diff > change.diff
vericommit check --repo /path/to/repo --patch change.diff

# 4. deterministic demo, no keys, no network
bash demo/run_mock_demo.sh

# 5. tests
python3 -m pytest -q            # or: python3 -m unittest discover -s tests -v
```

Outputs land in `runs/<run-id>/`: `change.diff`, `commit_message.txt`, `report.json` (per-candidate
layers, tokens, cost), and `REJECTED.md` when nothing passes. `--write-branch` commits the accepted
patch on a **new** branch and never moves `HEAD`.

## Providers

| spec | what | credential |
|---|---|---|
| `deepseek` / `deepseek:reasoner` | DeepSeek chat / reasoner | `DEEPSEEK_API_KEY` |
| `claude` / `claude:sonnet` / `claude:opus` | Claude Code CLI, headless, `--max-turns 1` | existing CLI login |
| `openrouter:<model>` | any OpenRouter model | `OPENROUTER_API_KEY` |
| `mock` | deterministic stub for tests | – |

Keys are read from the environment, then from an optional
`~/.config/vericommit/keys.env` (one `NAME=value` per line). Values are never printed or logged.
Use whatever model you trust to *propose*; the gates decide what *lands*.

## Policy (`policy.json`, every key optional)

Allowed/forbidden globs, max diff lines/files, build/test/lint commands (auto-detected from the
repo otherwise), hard vs soft check lists, candidate count, repairs, providers, per-run USD budget,
test/build timeouts, branch/commit prefix, and whether autonomous-agent run is permitted. Defaults
in [`vericommit/policy.py`](vericommit/policy.py).

## Verification layers (ordered; hard gates fail fast)

| # | layer | kind | catches |
|---|---|---|---|
| 1 | `repo_policy` | hard | repos whose contribution docs forbid autonomous agents |
| 2 | `git_hygiene` | hard | forbidden paths (tests/CI/deps/env), path allow-list, diff/file caps, binary & oversized files |
| 3 | `no_test_tamper` | hard | test/CI edits even if the policy is loosened (defense in depth) |
| 4 | `safety` | hard | secrets/tokens/private keys, `shell=True`, `os.system`, eval/exec, pickle, `rm -rf /`, `chmod 777`, `curl \| sh` |
| 5 | `build` | hard | compile/build in a throwaway worktree, scrubbed env, timeout |
| 6 | `tests` | hard | the repo's own test command; flags "no tests ran" |
| 7 | `lint` | soft | repo linter/formatter if configured |
| 8 | `contract` | soft | public-API breakage via Python AST diff of changed modules |
| 9 | `style` | soft | repo line-length/indent profile, trailing whitespace, debug prints, TODO/FIXME, commit-message convention |
| 10 | `changelog` | soft | contribution docs require a changelog entry that is missing |
| 11 | `injection` | soft | prompt-injection signals in the context the model was given |

Hard failures trigger a bounded repair loop (failure text is fed back to the model), then
rejection. Every run writes a report, so an agent's work is auditable after the fact.

## Working with real repositories (profile → plan → PR)

Every run starts by reading the repo's own contract: `profile` walks README/CONTRIBUTING/AGENTS/
AI-policy docs, `.github/workflows/*.yml`, PR/issue templates, changelogs, style configs, and the
last 80 commit subjects. It writes `CONVENTIONS.md`, `conventions.json`, and a prompt fragment that
the engine injects into every generation.

```bash
vericommit profile --repo /path/to/any/repo        # -> repo-profiles/<name>/
vericommit pr --repo /path/to/repo --plan plan.json \
    --providers deepseek --candidates 2 --budget 0.5   # multi-commit branch + runs/PR.md
vericommit track --repo owner/repo --pr 1234       # poll reviews -> runs/pr_tracker.jsonl
```

The AI-policy gate is hard and non-negotiable: `repo_policy` refuses to run on repos whose
contribution docs forbid autonomous agents, and `pr`/`run` exit non-zero instead of producing a
patch. The human author still writes the PR narrative, discloses AI assistance, and answers review
comments in their own words.

## Batch mode

`batch` runs a JSONL manifest of `{repo, task}` pairs concurrently (`--workers N`), each with its
own candidate fan-out; `--candidates K` × `--workers N` provider calls are in flight at once.
Costs and acceptance are written to `runs/batch_report.json`.

## Measured status (2026-10-27, local run)

| Check | Result |
|---|---|
| Unit + integration tests | **26/26 pass** (`python3 -m pytest -q`, Python 3.10–3.13) |
| Mock end-to-end demo | accepted, minimal patch, report written (`demo/run_mock_demo.sh`) |
| Cross-provider run (deepseek + claude, 2 candidates) | both hard-pass, 6.6 s, **$0.1374**, chosen patch = 2-line diff, conventional commit |
| `check` on the produced patch | all configured hard + soft gates PASS → ACCEPT |
| `--write-branch` | commit on a new branch; `main` untouched |
| Batch (3 repos × DeepSeek, 3 workers) | 3/3 accepted |

These are single-machine measurements, not a benchmark; the repo contains no calibration corpus yet
(see limitations).

## What is deliberately not done yet

- **No container sandbox.** Tests run in a throwaway worktree with a scrubbed environment
  (`*_API_KEY`/`*_TOKEN` removed) and timeouts, but a hostile repo/test can still touch the
  filesystem. Add a container backend before running this on untrusted repos.
- **No review-comment prediction.** The style layer uses rules + repo statistics.
- **No calibration corpus.** False-accept / false-alarm rates per gate are the next milestone; until
  then treat the gates as necessary conditions, not sufficient proof of correctness.
- **Python-first contract checks.** The API-diff layer understands Python ASTs; other languages get
  build/test/lint only.

## Contributing

Issues and PRs are welcome — read [CONTRIBUTING.md](CONTRIBUTING.md) first. AI-assisted
contributions are allowed with disclosure; see [AGENTS.md](AGENTS.md) for the rules agents must
follow here. Security reports: [SECURITY.md](SECURITY.md). Community expectations:
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## License

[MIT](LICENSE) © 2026 A-G0D.
