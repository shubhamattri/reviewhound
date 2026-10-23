"""Operator-side limits on proofs: which repos may run code locally, and total proof time."""

import argparse
from collections.abc import Mapping
from pathlib import Path

import pytest

from reviewhound.cli import _proof_runners, _worker_reviewer_factory, main
from reviewhound.config import load_config
from reviewhound.index.treesitter import CodeIndex
from reviewhound.pipeline import Reviewer
from reviewhound.proof import BudgetedRunner, DockerRunner, LocalRunner, Prover, RunResult
from reviewhound.providers import FakeProvider, ProviderRegistry
from reviewhound.queue import Job
from reviewhound.vcs.memory import MemoryVCS
from tests.conftest import FakeClock, answer, make_pr
from tests.test_proof import GOOD, HEAD, RecordingRunner, finding, sample
from tests.test_proof_pipeline import BUGGY, FINDING, PROOF


def ns(*repos: str, max_proof_seconds: float = 300.0) -> argparse.Namespace:
    return argparse.Namespace(allow_local_proof_repo=list(repos), proof_image="img:1",
                              max_proof_seconds=max_proof_seconds)


def inner(runner: object) -> object:
    assert isinstance(runner, BudgetedRunner)
    return runner.inner


# --- local runner is a per-repo, operator-side opt-in -----------------------------------------

def test_local_runner_only_for_repos_the_operator_listed() -> None:
    runners = _proof_runners(ns("Acme/Shop"), "acme/shop")
    assert sorted(runners) == ["docker", "local"]
    assert isinstance(inner(runners["local"]), LocalRunner)
    docker = inner(runners["docker"])
    assert isinstance(docker, DockerRunner) and docker.image == "img:1"
    assert sorted(_proof_runners(ns("acme/shop"), "acme/other")) == ["docker"]
    assert sorted(_proof_runners(ns(), "acme/shop")) == ["docker"]


def test_repo_config_alone_cannot_get_the_local_runner() -> None:
    cfg = "proof:\n  enabled: true\n  runner: local\n"
    vcs = MemoryVCS(make_pr(head="h1"), {"calc/stats.py": "def total(xs):\n    return 0\n",
                                         ".reviewhound.yml": cfg}, {"calc/stats.py": BUGGY})
    fake = FakeProvider([answer(FINDING), PROOF])
    reviewer = Reviewer(vcs, ProviderRegistry([fake]), [], None,
                        proof_runners=_proof_runners(ns("acme/trusted"), "acme/shop"))
    res = reviewer.review("acme/shop", 7, dry_run=True)
    assert res.decision is not None
    [f] = res.decision.post
    assert (f.proof, f.notes) == ("inconclusive", [
        "proof inconclusive: proof runner 'local' is not enabled on this server"])
    assert len(fake.prompts) == 1  # no proof prompt, so no PR code ran


@pytest.mark.parametrize("argv", [
    ["--allow-local-proof", "stats"],
    ["--allow-local-proof-repo", "acme", "stats"],
    ["--allow-local-proof-repo", "acme/shop/x", "stats"],
    ["--max-proof-seconds", "0", "stats"],
    ["--max-proof-seconds", "-5", "stats"],
])
def test_bad_proof_flags_are_refused(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2


def test_worker_factory_gives_each_job_its_own_runners_and_budget(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
    args = argparse.Namespace(home=str(tmp_path), gh_user=None, fake_answer=None,
                              database_url=None, allow_local_proof_repo=["acme/shop"],
                              proof_image="img:1", max_proof_seconds=120.0)
    build = _worker_reviewer_factory(args)

    def job(repo: str) -> Job:
        return Job(1, repo, 7, "h", "running", 1, 4, None, "t")
    first, second, other = build(job("acme/shop")), build(job("acme/shop")), build(job("x/y"))
    assert sorted(first.proof_runners) == ["docker", "local"]
    assert sorted(other.proof_runners) == ["docker"]
    a, b = first.proof_runners["local"], second.proof_runners["local"]
    assert isinstance(a, BudgetedRunner) and isinstance(b, BudgetedRunner)
    assert a is not b and a.total_s == 120.0
    assert first.vcs is second.vcs and first.store is second.store  # shared, built once


# --- total proof time per review ---------------------------------------------------------------

class TimedRunner:
    name = "timed"

    def __init__(self, clock: FakeClock, costs: list[float]) -> None:
        self.clock, self.costs = clock, costs
        self.timeouts: list[float] = []

    def run(self, files: Mapping[str, str], test_path: str, timeout_s: float) -> RunResult:
        self.timeouts.append(timeout_s)
        self.clock.now += self.costs.pop(0)
        return sample("assert", 1)


def test_budget_shrinks_each_timeout_and_then_refuses(clock: FakeClock) -> None:
    timed = TimedRunner(clock, [70, 29.5])
    runner = BudgetedRunner(timed, total_s=100, clock=clock)
    assert runner.name == "timed"
    runner.run({}, "t.py", 60)
    runner.run({}, "t.py", 60)
    refused = runner.run({}, "t.py", 60)
    assert timed.timeouts == [60, 30]
    assert (refused.returncode, refused.junit_xml, refused.error) == (
        None, None, "proof time budget of 100s for this review is used up")
    assert runner.exhausted() is True


def test_time_spent_by_a_crashing_run_still_counts(clock: FakeClock) -> None:
    class Crashing:
        name = "crash"

        def run(self, files: Mapping[str, str], test_path: str, timeout_s: float) -> RunResult:
            clock.now += 40
            raise RuntimeError("boom")
    runner = BudgetedRunner(Crashing(), total_s=50, clock=clock)
    with pytest.raises(RuntimeError):
        runner.run({}, "t.py", 60)
    assert runner.remaining() == 10


def test_exhausted_budget_skips_the_model_call(clock: FakeClock) -> None:
    rec = RecordingRunner(sample("assert", 1))
    runner = BudgetedRunner(rec, total_s=0.5, clock=clock)
    fake = FakeProvider([GOOD])
    cfg = load_config("proof:\n  enabled: true\n")
    p = Prover(ProviderRegistry([fake]), runner, cfg.proof, "sonnet", 30)
    res = p.prove(finding(), HEAD, CodeIndex.build(HEAD))
    assert (res.verdict, res.reason) == ("inconclusive",
                                         "proof time budget of 0.5s for this review is used up")
    assert (fake.prompts, rec.runs) == ([], [])
