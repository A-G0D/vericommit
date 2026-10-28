# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Planned
- Container-backed test/build sandbox (Docker or bubblewrap) so the engine can run on untrusted repos.
- Calibration corpus: per-layer false-accept / false-alarm measurement on labeled real changes.
- PR review-comment prediction and reviewer-objection checklist output.
- Multi-language public-API contract checks (currently Python AST only).

## [0.1.0] - 2026-10-28

First public release. The engine is complete enough to gate real commits end to end.

### Added
- `run` command: context collection, parallel candidate fan-out across providers, bounded repair
  loop on hard-gate failures, and settlement of the best hard-passing candidate.
- `check` command: re-run the same gates on an existing patch (from a human or an agent) without
  calling a model.
- `profile` command: extracts a repository's contribution contract — AI policy, CI commands,
  commit style, PR template, DCO/CLA/changelog rules — as markdown + JSON + prompt fragment.
- `pr` command: executes a multi-commit plan on a new branch and renders a PR body package.
- `track` command: polls a GitHub PR for review feedback into a JSONL ledger.
- `batch` command: concurrent evaluation of a JSONL manifest of repo/task pairs.
- Verification layers: `repo_policy`, `git_hygiene`, `no_test_tamper`, `safety`, `build`, `tests`
  (hard) and `lint`, `contract`, `style`, `changelog`, `injection` (soft).
- Providers: OpenAI-compatible HTTP (DeepSeek, OpenRouter, local vLLM), Claude Code CLI headless,
  and a deterministic mock provider.
- Throwaway-worktree execution with scrubbed environment and timeouts; `--write-branch` commits an
  accepted patch on a new branch without touching `HEAD`.
- Report artifacts per run: `report.json`, `change.diff`, `commit_message.txt`, `REJECTED.md`.
- Deterministic end-to-end demo (`demo/run_mock_demo.sh`) that needs no API keys or network.
- Tests for the engine, layers, policy, providers, and repo-contract extraction.

### Security
- Secret and dangerous-call scanning on every added line before anything is executed.
- Test/CI/dependency files are protected twice: default forbidden paths plus an independent
  `no_test_tamper` layer.
- Provider keys are read from the environment or an optional keys file and never printed.

[Unreleased]: https://github.com/A-G0D/vericommit/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/A-G0D/vericommit/releases/tag/v0.1.0
