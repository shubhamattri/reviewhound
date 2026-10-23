"""Model providers behind one interface, with a registry, fallback and circuit breaker."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

log = logging.getLogger(__name__)


@dataclass
class Completion:
    text: str
    provider: str
    model: str
    cost_usd: float = 0.0
    duration_s: float = 0.0


class ProviderError(RuntimeError):
    """The provider could not produce an answer (timeout, crash, bad envelope)."""


class Provider(Protocol):
    name: str

    def complete(self, prompt: str, *, model: str, timeout_s: float) -> Completion: ...

    def healthy(self) -> bool: ...


Runner = Callable[[list[str], str, float, dict[str, str]], "subprocess.CompletedProcess[str]"]


def _run(cmd: list[str], stdin: str, timeout_s: float,
         env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    # An empty working directory keeps any project instructions on this machine out of the review.
    with tempfile.TemporaryDirectory(prefix="reviewhound-") as cwd:
        return subprocess.run(cmd, input=stdin, capture_output=True, text=True,
                              timeout=timeout_s, check=False, env=env, cwd=cwd)


class ClaudeCliProvider:
    """Calls `claude -p` so reviews bill the user's subscription, not an API key."""

    name = "claude-cli"

    def __init__(self, binary: str = "claude", runner: Runner = _run) -> None:
        self.binary = binary
        self._run = runner

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.pop("ANTHROPIC_API_KEY", None)
        return env

    def complete(self, prompt: str, *, model: str, timeout_s: float) -> Completion:
        cmd = [self.binary, "-p", "--output-format", "json", "--model", model,
               "--tools", "", "--no-session-persistence", "--setting-sources", "local",
               "--strict-mcp-config"]
        started = time.monotonic()
        try:
            res = self._run(cmd, prompt, timeout_s, self._env())
        except subprocess.TimeoutExpired as exc:
            raise ProviderError(f"{self.name} timed out after {timeout_s}s") from exc
        except OSError as exc:
            raise ProviderError(f"{self.name} could not start: {type(exc).__name__}") from exc
        if res.returncode != 0:
            raise ProviderError(f"{self.name} exited {res.returncode}")
        try:
            envelope = json.loads(res.stdout)
        except json.JSONDecodeError as exc:
            raise ProviderError(f"{self.name} returned non-JSON envelope") from exc
        if not isinstance(envelope, dict) or envelope.get("is_error") or \
                not isinstance(envelope.get("result"), str):
            raise ProviderError(f"{self.name} returned an error envelope")
        return Completion(text=envelope["result"], provider=self.name, model=model,
                          cost_usd=float(envelope.get("total_cost_usd") or 0.0),
                          duration_s=time.monotonic() - started)

    def healthy(self) -> bool:
        try:
            res = self._run([self.binary, "--version"], "", 20, self._env())
        except (subprocess.TimeoutExpired, OSError):
            return False
        return res.returncode == 0


class FakeProvider:
    """Returns recorded answers. Raises on a prompt it has no answer for."""

    name = "fake"

    def __init__(self, answers: list[str] | Callable[[str], str]) -> None:
        self._answers = answers
        self.prompts: list[str] = []

    def complete(self, prompt: str, *, model: str, timeout_s: float) -> Completion:
        self.prompts.append(prompt)
        if callable(self._answers):
            return Completion(text=self._answers(prompt), provider=self.name, model=model)
        if not self._answers:
            raise ProviderError("fake provider has no recorded answer left")
        return Completion(text=self._answers.pop(0), provider=self.name, model=model)

    def healthy(self) -> bool:
        return True


@dataclass
class _Breaker:
    failures: int = 0
    opened_at: float | None = None


@dataclass
class RegistryResult:
    completion: Completion
    attempted: list[str] = field(default_factory=list)


class ProviderRegistry:
    """Try providers in order; a provider that keeps failing is skipped for a cool-off."""

    def __init__(self, providers: list[Provider], failure_threshold: int = 3,
                 cooloff_s: float = 300.0, clock: Callable[[], float] = time.monotonic) -> None:
        if not providers:
            raise ValueError("registry needs at least one provider")
        self.providers = providers
        self.failure_threshold = failure_threshold
        self.cooloff_s = cooloff_s
        self._clock = clock
        self._breakers: dict[str, _Breaker] = {p.name: _Breaker() for p in providers}

    def _open(self, name: str) -> bool:
        b = self._breakers[name]
        if b.opened_at is None:
            return False
        if self._clock() - b.opened_at >= self.cooloff_s:
            b.opened_at, b.failures = None, self.failure_threshold - 1  # half-open: one try
            return False
        return True

    def complete(self, prompt: str, *, model: str, timeout_s: float) -> RegistryResult:
        attempted: list[str] = []
        for p in self.providers:
            if self._open(p.name):
                attempted.append(f"{p.name}:skipped(circuit-open)")
                continue
            try:
                c = p.complete(prompt, model=model, timeout_s=timeout_s)
            except ProviderError as exc:
                b = self._breakers[p.name]
                b.failures += 1
                if b.failures >= self.failure_threshold:
                    b.opened_at = self._clock()
                attempted.append(f"{p.name}:failed")
                log.warning("provider failed", extra={"provider": p.name,
                                                      "error": type(exc).__name__})
                continue
            self._breakers[p.name] = _Breaker()
            attempted.append(f"{p.name}:ok")
            return RegistryResult(completion=c, attempted=attempted)
        raise ProviderError("all providers failed: " + ", ".join(attempted))
