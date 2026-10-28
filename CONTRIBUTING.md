# Contributing to VeriCommit

Thanks for considering a contribution. This project is about *verification-first* changes, so the
contribution rules apply to the project itself.

## Development setup

```bash
git clone https://github.com/A-G0D/vericommit.git
cd vericommit
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## Before you open a PR

Run the same checks CI runs:

```bash
ruff check .
ruff format --check .
python3 -m pytest -q
bash demo/run_mock_demo.sh          # deterministic end-to-end, no keys
```

All four must pass. If you changed a verification layer, add a test that fails without your change
and passes with it. If you changed provider or policy defaults, document the behavioral change in
`CHANGELOG.md`.

## Commit messages

Conventional commits, imperative mood, short subject ≤ 72 characters:

```
feat(providers): add a local vLLM provider spec
fix(layers): close worktree when build times out
docs(architecture): describe the settle step
```

One logical change per PR. No reformatting-only PRs, no dependency churn unrelated to the change.

## What we will not merge

- Changes that weaken a gate to make a test pass (for example, removing `no_test_tamper` from the
  default policy, or allowing test-file edits by default). Gates may be *configurable*, but the
  default must stay strict and the change must be justified with a test.
- Secrets, real API keys, customer data, or personal information anywhere in the tree or tests.
- Features that require sending repository content to a third party without an explicit, documented
  opt-in.
- Test-only "fixes" that special-case the demo repository. The demo is a fixture; the engine must
  work on arbitrary repositories.

## AI-assisted contributions

AI-assisted contributions are allowed, and common in this repo. Please:

1. Disclose it in the PR description with one line, e.g.
   `AI assistance: prepared with an AI coding agent; I reviewed and ran the checks above.`
2. Be able to explain every changed line and answer review comments yourself.
3. Include the verification output (commands + results) you actually ran.

See [AGENTS.md](AGENTS.md) for the specific expectations for autonomous agents operating in
this repository.

## Reporting bugs and requesting features

Use the issue templates in `.github/ISSUE_TEMPLATE/`. A good bug report includes the command you
ran, the repository you ran it against (or a minimal fixture), the expected result, and the actual
result. Never paste real tokens or private repository contents into an issue.

## Code of conduct

By participating you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).

## License

By contributing you agree that your contribution is licensed under the [MIT License](LICENSE).
