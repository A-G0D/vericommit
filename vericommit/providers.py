"""Model-agnostic providers: Claude CLI, any OpenAI-compatible endpoint (DeepSeek,
OpenRouter, vLLM, ...), and a deterministic Mock for tests.

Everything returns a Completion; nothing here knows about verification.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

# prices in USD per 1M tokens (in, out); overridable per provider spec / policy
DEFAULT_PRICES: dict[str, tuple[float, float]] = {
    "deepseek-chat": (0.28, 0.42),
    "deepseek-reasoner": (0.55, 2.19),
    "mock": (0.0, 0.0),
}


@dataclass
class Completion:
    text: str
    provider: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    latency_s: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "latency_s": round(self.latency_s, 3),
        }


class ProviderError(RuntimeError):
    pass


class Provider:
    name = "base"

    def complete(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 6000,
        temperature: float = 0.2,
        timeout: float = 240.0,
    ) -> Completion:
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# OpenAI-compatible HTTP provider (DeepSeek, OpenRouter, local vLLM, ...)
# --------------------------------------------------------------------------- #
class OpenAICompatProvider(Provider):
    def __init__(
        self,
        name: str,
        base_url: str,
        api_key_env: str,
        model: str,
        extra_headers: dict[str, str] | None = None,
        prices: tuple[float, float] | None = None,
    ):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self.model = model
        self.extra_headers = extra_headers or {}
        self.prices = prices or DEFAULT_PRICES.get(model, (0.0, 0.0))

    def complete(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 6000,
        temperature: float = 0.2,
        timeout: float = 240.0,
    ) -> Completion:
        key = os.environ.get(self.api_key_env, "")
        if not key:
            raise ProviderError(f"{self.api_key_env} is not set")
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
                **self.extra_headers,
            },
            method="POST",
        )
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:800]
            raise ProviderError(f"{self.name} HTTP {e.code}: {detail}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            raise ProviderError(f"{self.name} network error: {e}") from e
        latency = time.time() - t0
        try:
            text = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as e:
            raise ProviderError(f"{self.name} bad response: {json.dumps(body)[:500]}") from e
        usage = body.get("usage") or {}
        pt = int(usage.get("prompt_tokens") or 0)
        ct = int(usage.get("completion_tokens") or 0)
        cost = pt / 1e6 * self.prices[0] + ct / 1e6 * self.prices[1]
        return Completion(
            text=text,
            provider=self.name,
            model=self.model,
            prompt_tokens=pt,
            completion_tokens=ct,
            cost_usd=cost,
            latency_s=latency,
            raw=body,
        )


# --------------------------------------------------------------------------- #
# Claude Code CLI (headless, no tools, one turn)
# --------------------------------------------------------------------------- #
class ClaudeCLIProvider(Provider):
    def __init__(
        self,
        model: str | None = None,
        cli: str = "claude",
        prices: tuple[float, float] = (0.0, 0.0),
    ):
        self.name = "claude-cli"
        self.model = model or "default"
        self.cli = cli
        self.prices = prices

    def complete(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 6000,
        temperature: float = 0.2,
        timeout: float = 240.0,
    ) -> Completion:
        prompt = f"{system}\n\n---\n\n{user}"
        cmd = [self.cli, "-p", "--output-format", "json", "--max-turns", "1"]
        if self.model not in ("default", "", None):
            cmd += ["--model", self.model]
        t0 = time.time()
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout)
        latency = time.time() - t0
        if proc.returncode != 0:
            raise ProviderError(
                f"claude CLI exit {proc.returncode}: {(proc.stderr or proc.stdout)[:800]}"
            )
        try:
            body = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise ProviderError(f"claude CLI non-JSON output: {proc.stdout[:500]}") from e
        if body.get("is_error"):
            raise ProviderError(f"claude CLI error: {str(body)[:500]}")
        text = body.get("result", "")
        usage = body.get("usage") or {}
        pt = int(usage.get("input_tokens") or 0)
        ct = int(usage.get("output_tokens") or 0)
        cost = float(body.get("total_cost_usd") or 0.0)
        return Completion(
            text=text,
            provider=self.name,
            model=self.model,
            prompt_tokens=pt,
            completion_tokens=ct,
            cost_usd=cost,
            latency_s=latency,
            raw=body,
        )


# --------------------------------------------------------------------------- #
# Mock (tests / dry runs): deterministic responses
# --------------------------------------------------------------------------- #
class MockProvider(Provider):
    def __init__(self, responses: list[str] | Callable[[str], str]):
        self.name = "mock"
        self.model = "mock"
        self._responses = responses
        self._calls = 0

    def complete(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 6000,
        temperature: float = 0.2,
        timeout: float = 240.0,
    ) -> Completion:
        if callable(self._responses):
            text = self._responses(user)
        else:
            idx = min(self._calls, len(self._responses) - 1)
            text = self._responses[idx]
        self._calls += 1
        return Completion(
            text=text,
            provider=self.name,
            model=self.model,
            prompt_tokens=len(user) // 4,
            completion_tokens=len(text) // 4,
        )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def extract_json(text: str) -> dict[str, Any]:
    """Pull the first JSON object out of a model response (fences tolerated)."""
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    candidates = [fenced.group(1)] if fenced else []
    candidates.append(text)
    for blob in candidates:
        start = blob.find("{")
        while start != -1:
            depth, in_str, esc = 0, False, False
            for i in range(start, len(blob)):
                c = blob[i]
                if in_str:
                    if esc:
                        esc = False
                    elif c == "\\":
                        esc = True
                    elif c == '"':
                        in_str = False
                elif c == '"':
                    in_str = True
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(blob[start : i + 1])
                        except json.JSONDecodeError:
                            break
            start = blob.find("{", start + 1)
    raise ProviderError("response did not contain a JSON object")


def load_env_keys(extra_paths: tuple[str, ...] = ()) -> list[str]:
    """Make provider keys available to non-interactive shells.

    Reads from the environment first and then from an optional key file
    (``~/.config/vericommit/keys.env`` by default, one ``NAME=value`` per line).
    Values are never printed or logged. Idempotent: safe to call repeatedly.
    """
    if getattr(load_env_keys, "_done", False):
        return []
    load_env_keys._done = True  # type: ignore[attr-defined]
    found = []
    paths = [os.path.expanduser(p) for p in ("~/.config/vericommit/keys.env",)]
    paths += [os.path.expanduser(p) for p in extra_paths]
    pat = re.compile(r'^\s*(?:export\s+)?([A-Z][A-Z0-9_]*_API_KEY)\s*=\s*["\']?([^"\'\s]+)')
    for path in paths:
        try:
            text = open(path, encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        for line in text.splitlines():
            if line.lstrip().startswith("#"):
                continue
            m = pat.match(line)
            if m and m.group(1) not in os.environ:
                os.environ[m.group(1)] = m.group(2)
                found.append(m.group(1))
    return found


def build_provider(spec: str) -> Provider:
    """spec forms: claude | claude:<model> | deepseek | deepseek:reasoner |
    openrouter:<model> | mock"""
    spec = spec.strip()
    load_env_keys()
    if spec == "claude" or spec.startswith("claude:"):
        model = spec.split(":", 1)[1] if ":" in spec else None
        return ClaudeCLIProvider(model=model)
    if spec == "deepseek" or spec.startswith("deepseek:"):
        variant = spec.split(":", 1)[1] if ":" in spec else "chat"
        model = "deepseek-reasoner" if variant in ("reasoner", "r1") else "deepseek-chat"
        return OpenAICompatProvider(
            "deepseek", "https://api.deepseek.com", "DEEPSEEK_API_KEY", model
        )
    if spec.startswith("openrouter:"):
        model = spec.split(":", 1)[1]
        return OpenAICompatProvider(
            "openrouter",
            "https://openrouter.ai/api/v1",
            "OPENROUTER_API_KEY",
            model,
            extra_headers={"HTTP-Referer": "https://localhost", "X-Title": "vericommit"},
        )
    if spec == "mock":
        return MockProvider(["{}"])
    raise ProviderError(f"unknown provider spec: {spec}")
