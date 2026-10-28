# Security Policy

## Supported versions

| Version | Supported |
|---|---|
| 0.1.x | ✅ |
| < 0.1 | not released |

The project is alpha; fixes land on `main` and are released as patch versions.

## Reporting a vulnerability

Please **do not open a public issue** for a security problem. Use GitHub's private vulnerability
reporting on this repository:

1. Go to <https://github.com/A-G0D/vericommit/security/advisories/new>.
2. Describe the issue, the affected version/commit, and a minimal reproduction.

You will get an acknowledgement when the maintainer is available. This is a volunteer project, so
please allow a reasonable amount of time before disclosing publicly.

If private reporting is unavailable to you, open a normal issue that says only "security report —
please contact me for details" without any exploit details, and the maintainer will follow up.

## Threat model

VeriCommit executes repository code (`build_cmd`, `test_cmd`, `lint_cmd`) and therefore treats the
repository under test as **semi-trusted at best**:

- Subprocesses run with a defensive environment: `*_API_KEY`, `*_TOKEN`, `*SECRET*`,
  `*PASSWORD*` variables are removed; `GIT_TERMINAL_PROMPT=0` and timeouts are set.
- Candidates are applied to a throwaway git worktree and are rejected if they touch tests, CI,
  dependency manifests, or `.env` files.
- Added lines are scanned for credential-shaped strings, `shell=True`, `os.system`, `eval`/`exec`,
  `pickle` loading, and several destructive shell patterns.

**Known limitation:** there is no container or OS-level sandbox. Tests run as your user. A hostile
repository or hostile test suite can still read files your user can read, make network calls, or
write outside the worktree. Do not point VeriCommit at untrusted repositories until a container
backend exists. This limitation is documented in the README and tracked as roadmap work.

## What is in scope

- Secret or credential leakage caused by the tool (for example, keys in logs or report files).
- Path traversal in `apply_files`/`safe_join` that escapes the worktree.
- Command injection through policy/config values.
- Bypass of the test-tampering or safety gates by a crafted model response.
- Prompt-injection handling that causes the engine to execute untrusted instructions.

## Out of scope

- The engine running a repository's own test suite and that suite being malicious — see the sandbox
  limitation above; report it as a known limitation, not a new vulnerability.
- Vulnerabilities in model providers or their SDKs.
- Attacks that require the user to intentionally run `--write-branch` against a repository they
  do not trust.
