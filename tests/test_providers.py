import json
import subprocess

import pytest

from reviewhound.providers import ClaudeCliProvider, FakeProvider, ProviderError, ProviderRegistry
from tests.conftest import FakeClock

Calls = list[tuple[list[str], str, dict[str, str]]]


def runner(stdout: str = "", code: int = 0, exc: Exception | None = None, calls: Calls | None = None):  # type: ignore[no-untyped-def]
    def run(cmd: list[str], stdin: str, timeout: float, env: dict[str, str]):  # type: ignore[no-untyped-def]
        if calls is not None:
            calls.append((cmd, stdin, env))
        if exc is not None:
            raise exc
        return subprocess.CompletedProcess(cmd, code, stdout, "")
    return run


def test_claude_cli_uses_subscription_not_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "should-not-leak")
    calls: Calls = []
    env = json.dumps({"result": '{"findings": []}', "is_error": False, "total_cost_usd": 0.01})
    p = ClaudeCliProvider(runner=runner(env, calls=calls))
    c = p.complete("PROMPT", model="sonnet", timeout_s=5)
    assert (c.text, c.model, c.cost_usd) == ('{"findings": []}', "sonnet", 0.01)
    cmd, stdin, sent_env = calls[0]
    assert cmd == ["claude", "-p", "--output-format", "json", "--model", "sonnet", "--tools", "",
                   "--no-session-persistence", "--setting-sources", "local",
                   "--strict-mcp-config"]
    assert stdin == "PROMPT"
    assert "ANTHROPIC_API_KEY" not in sent_env


@pytest.mark.parametrize("r, msg", [
    (runner("", code=1), "exited 1"),
    (runner("not json"), "non-JSON envelope"),
    (runner(json.dumps({"is_error": True, "result": "rate limited"})), "error envelope"),
    (runner(exc=subprocess.TimeoutExpired(["claude"], 5)), "timed out"),
    (runner(exc=FileNotFoundError()), "could not start"),
])
def test_claude_cli_failures_raise_provider_error(r, msg: str) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ProviderError, match=msg):
        ClaudeCliProvider(runner=r).complete("p", model="m", timeout_s=5)


class Flaky:
    def __init__(self, name: str, fail: bool) -> None:
        self.name, self.fail, self.calls = name, fail, 0

    def complete(self, prompt: str, *, model: str, timeout_s: float):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.fail:
            raise ProviderError("down")
        return FakeProvider(['{"findings": []}']).complete(prompt, model=model,
                                                          timeout_s=timeout_s)

    def healthy(self) -> bool:
        return not self.fail


def test_registry_falls_back_and_reports_who_ran() -> None:
    main, backup = Flaky("main", fail=True), Flaky("backup", fail=False)
    res = ProviderRegistry([main, backup]).complete("p", model="m", timeout_s=1)
    assert res.attempted == ["main:failed", "backup:ok"]


def test_circuit_opens_after_threshold_and_half_opens_after_cooloff(clock: FakeClock) -> None:
    main, backup = Flaky("main", fail=True), Flaky("backup", fail=False)
    reg = ProviderRegistry([main, backup], failure_threshold=2, cooloff_s=60, clock=clock)
    reg.complete("p", model="m", timeout_s=1)
    reg.complete("p", model="m", timeout_s=1)
    assert main.calls == 2
    assert reg.complete("p", model="m", timeout_s=1).attempted == [
        "main:skipped(circuit-open)", "backup:ok"]
    assert main.calls == 2
    clock.now += 60
    main.fail = False
    assert reg.complete("p", model="m", timeout_s=1).attempted == ["main:ok"]


def test_all_providers_down_is_an_error() -> None:
    with pytest.raises(ProviderError, match="all providers failed: main:failed"):
        ProviderRegistry([Flaky("main", True)]).complete("p", model="m", timeout_s=1)


def test_fake_provider_refuses_when_out_of_answers() -> None:
    fake = FakeProvider(["a"])
    assert fake.complete("x", model="m", timeout_s=1).text == "a"
    with pytest.raises(ProviderError):
        fake.complete("y", model="m", timeout_s=1)
