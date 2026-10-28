# AGENTS.md — rules for AI agents working in this repository

This file is the contract for any autonomous or semi-autonomous coding agent (Claude Code, Codex,
Cursor, OpenHands, custom harnesses, ...) that contributes to VeriCommit. Humans get the same rules
via [CONTRIBUTING.md](CONTRIBUTING.md). If your harness supports a project instruction file, point it
at this file.

## TL;DR

1. Read `README.md`, `ARCHITECTURE.md`, and this file before editing.
2. Run the full check suite before proposing a change.
3. **Never weaken a gate, a test, or a CI workflow to make your change pass.** Do not edit
   `.github/`, `tests/`, or the default gate lists unless the task is explicitly about those files.
4. No secrets, credentials, personal data, or third-party repository content in code, tests, logs,
   or commit messages.
5. Disclose AI assistance in the PR description and answer review questions yourself.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## The one command that defines "done"

```bash
ruff check . && ruff format --check . && python3 -m pytest -q && bash demo/run_mock_demo.sh
```

All four steps must pass. Paste the results (or the failing output) in your PR.

## What you may edit

- `vericommit/` — the engine. Small, focused diffs. One logical change per PR.
- `tests/` — only to add or extend coverage for a behavior change. Do not delete, skip, or loosen
  an existing test to make a change pass. If a test is wrong, explain why in the PR and change the
  behavior deliberately.
- `docs`/`README.md`/`ARCHITECTURE.md` — keep them honest; do not invent benchmark numbers.

## What you must not do

- Do not modify `.github/workflows/*` to make CI pass on your change. CI is the oracle. If CI is
  genuinely wrong, say so explicitly in the PR and explain the evidence.
- Do not add a new runtime dependency without updating `pyproject.toml` and the changelog.
- Do not add network calls, telemetry, or analytics.
- Do not print, log, commit, or paste provider keys. Test with `mock` — no API key is needed for
  any test or demo.
- Do not run the engine against untrusted third-party repositories; there is no sandbox yet (see
  `SECURITY.md`).
- Do not impersonate the maintainer or other contributors in commit messages, comments, or issues.

## Code style

- Python 3.10+, type hints on public functions, `from __future__ import annotations`.
- Ruff with the repo config; format with `ruff format`.
- Comments explain *why*, not *what*. No debug prints, no TODOs in shipped code.
- New verification layer? Add a unit test that fails without it, register it in `LAYERS`, and decide
  explicitly whether it is hard (deterministic, cheap, pre-execution) or soft (scored).
- Conventional commit messages (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`).

## Repository layout

```
vericommit/        package: cli, engine, layers, conventions, policy, providers, gitops, testing
tests/             unit + integration tests (pytest / unittest compatible)
demo/              deterministic mock-provider demo, no network
.github/           CI, CodeQL, Dependabot, issue templates
```

## Reporting

If your change touches security-sensitive behavior (path handling, subprocess execution, environment
scrubbing, secret detection), call it out in the PR under a "Security" heading. If you find a
vulnerability, follow [SECURITY.md](SECURITY.md) instead of opening a public PR with details.
