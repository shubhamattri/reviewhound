"""The proof stage inside a full review, with FakeProvider and a real LocalRunner."""

import json
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest

from reviewhound.config import Severity
from reviewhound.pipeline import Reviewer, ReviewResult
from reviewhound.proof import DockerRunner, LocalRunner, RunResult, SandboxRunner
from reviewhound.providers import FakeProvider, ProviderRegistry
from reviewhound.store import Store
from reviewhound.vcs.base import ReviewPayload
from reviewhound.vcs.memory import MemoryVCS
from tests.conftest import answer, make_pr

BASE = {"calc/stats.py": "def total(xs):\n    return sum(xs)\n"}
BUGGY = "def total(xs):\n    result = 0\n    for i in range(1, len(xs)):\n" \
        "        result += xs[i]\n    return result\n"
CORRECT = "def total(xs):\n    result = 0\n    for x in xs:\n" \
          "        result += x\n    return result\n"
FINDING = {"file": "calc/stats.py", "line": 3, "severity": "blocker", "category": "correctness",
           "title": "First item is skipped", "body": "range starts at 1.",
           "evidence_quote": "for i in range(1, len(xs)):",
           "claims": [{"kind": "quote", "file": "calc/stats.py",
                       "text": "result = 0"}]}
TEST_CODE = "from calc.stats import total\n\n\ndef test_total_counts_every_item():\n" \
            "    assert total([1, 2, 3]) == 6\n"
PROOF = json.dumps({"test_path": "tests/test_reviewhound_proof.py", "code": TEST_CODE})
ON = "proof:\n  enabled: true\n  runner: local\n"


def review(head_src: str, cfg: str, answers: list[str],
           runners: Mapping[str, SandboxRunner] | None = None, tmp: Path | None = None,
           posting: bool = False) -> tuple[ReviewResult, FakeProvider, MemoryVCS]:
    base = {**BASE, ".reviewhound.yml": cfg + ("posting: true\n" if posting else "")}
    vcs = MemoryVCS(make_pr(head="h1"), base, {"calc/stats.py": head_src})
    fake = FakeProvider(answers)
    store = Store(tmp / "s.db") if tmp else None
    reviewer = Reviewer(vcs, ProviderRegistry([fake]), [], store,
                        proof_runners=runners if runners is not None else
                        {"local": LocalRunner()})
    return reviewer.review("acme/shop", 7, dry_run=not posting), fake, vcs


def review_of(result: ReviewResult) -> ReviewPayload:
    assert result.review is not None
    return result.review


def test_proof_off_by_default_changes_nothing() -> None:
    result, fake, _ = review(BUGGY, "", [answer(FINDING)])
    assert result.decision is not None
    [f] = result.decision.post
    assert (f.proof, f.proof_test, f.severity) == (None, "", Severity.BLOCKER)
    assert "proof" not in result.timings
    assert len(fake.prompts) == 1
    assert "Reproduced" not in review_of(result).comments[0].body


def test_real_bug_is_proven_posted_with_the_test_and_timed(tmp_path: Path) -> None:
    result, fake, vcs = review(BUGGY, ON, [answer(FINDING), PROOF], tmp=tmp_path, posting=True)
    assert result.outcome == "posted"
    assert result.decision is not None
    [f] = result.decision.post
    assert (f.proof, f.proof_test) == ("proven", TEST_CODE)
    assert f.notes == [("proof proven: the generated test fails on the PR head with "
                       "AssertionError")]
    assert "proof" in result.timings
    [posted] = vcs.posted
    assert posted.event == "REQUEST_CHANGES"
    body = posted.comments[0].body
    assert "**Reproduced by a generated test:** it fails on this PR's head." in body
    assert f"<details><summary>Failing test</summary>\n\n```python\n{TEST_CODE}```" in body
    assert "First item is skipped" in fake.prompts[1]


def test_unproven_blocker_is_downgraded_but_the_verdict_is_kept() -> None:
    # PR code runs in the sandbox and can make any test pass, so a proof never relaxes a review.
    finding = {**FINDING, "evidence_quote": "for x in xs:"}
    result, _, _ = review(CORRECT, ON + "  downgrade_unproven: true\n", [answer(finding), PROOF])
    assert result.decision is not None
    [f] = result.decision.post
    assert (f.proof, f.severity) == ("not_proven", Severity.MAJOR)
    assert review_of(result).event == "REQUEST_CHANGES"
    assert "Reproduced" not in review_of(result).comments[0].body
    assert "[major]" in review_of(result).comments[0].body


def test_unproven_blocker_stays_a_blocker_by_default() -> None:
    finding = {**FINDING, "evidence_quote": "for x in xs:"}
    result, _, _ = review(CORRECT, ON, [answer(finding), PROOF])
    assert result.decision is not None
    [f] = result.decision.post
    assert (f.proof, f.severity) == ("not_proven", Severity.BLOCKER)
    assert review_of(result).event == "REQUEST_CHANGES"


class Exploding:
    name = "exploding"

    def run(self, files: Mapping[str, str], test_path: str, timeout_s: float) -> RunResult:
        raise RuntimeError("sandbox crashed")


def test_a_proof_failure_never_fails_the_review(tmp_path: Path) -> None:
    result, _, vcs = review(BUGGY, ON, [answer(FINDING), PROOF], runners={"local": Exploding()},
                            tmp=tmp_path, posting=True)
    assert result.outcome == "posted"
    assert result.decision is not None
    [f] = result.decision.post
    assert (f.proof, f.severity) == ("inconclusive", Severity.BLOCKER)
    assert [len(p.comments) for p in vcs.posted] == [1]


def test_a_crash_outside_the_prover_is_logged_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    import reviewhound.pipeline as pl

    def boom(*a: object, **k: object) -> None:
        raise MemoryError("whole stage")
    monkeypatch.setattr(pl, "run_proof_stage", boom)
    result, _, _ = review(BUGGY, ON, [answer(FINDING), PROOF])
    assert result.outcome == "dry-run"
    assert result.decision is not None
    assert result.decision.post[0].proof is None


def test_local_runner_needs_the_operator_not_just_repo_config() -> None:
    vcs =MemoryVCS(make_pr(head="h1"), {**BASE, ".reviewhound.yml": ON},
                    {"calc/stats.py": BUGGY})
    fake = FakeProvider([answer(FINDING), PROOF])
    res = Reviewer(vcs, ProviderRegistry([fake]), [], None).review("acme/shop", 7, dry_run=True)
    assert res.decision is not None
    [f] = res.decision.post
    assert f.proof == "inconclusive"
    assert f.notes == ["proof inconclusive: proof runner 'local' is not enabled on this server"]
    assert len(fake.prompts) == 1


def test_default_docker_runner_with_daemon_down_is_inconclusive() -> None:
    def no_daemon(cmd: list[str], timeout_s: float) -> subprocess.CompletedProcess[str]:
        assert cmd[1] == "info"
        return subprocess.CompletedProcess(cmd, 1, "", "Cannot connect to the Docker daemon")
    result, _, _ = review(BUGGY, "proof:\n  enabled: true\n", [answer(FINDING), PROOF],
                          runners={"docker": DockerRunner(command=no_daemon)})
    assert result.decision is not None
    [f] = result.decision.post
    assert (f.proof, f.severity) == ("inconclusive", Severity.BLOCKER)
    assert f.notes == ["proof inconclusive: docker unavailable: the daemon is not running"]


def test_malformed_proof_answer_is_inconclusive() -> None:
    result, _, _ = review(BUGGY, ON, [answer(FINDING), "I cannot write a test"])
    assert result.decision is not None
    [f] = result.decision.post
    assert f.proof == "inconclusive"
    assert f.notes == ["proof inconclusive: malformed proof answer: JSONDecodeError"]


def test_findings_below_min_severity_are_not_sent_for_proof() -> None:
    major = {**FINDING, "severity": "major"}
    result, fake, _ = review(BUGGY, ON, [answer(major)])
    assert result.decision is not None
    [f] = result.decision.post
    assert f.proof is None
    assert len(fake.prompts) == 1


def test_held_unreproduced_blocker_keeps_the_verdict_and_is_named_in_the_body() -> None:
    finding = {**FINDING, "evidence_quote": "for x in xs:"}
    strict = "policy:\n  min_severity_to_post: blocker\n" + ON + "  downgrade_unproven: true\n"
    result, _, _ = review(CORRECT, strict, [answer(finding), PROOF])
    assert result.decision is not None
    assert result.decision.post == []
    [(f, reason)] = result.decision.held
    assert (f.severity, reason) == (Severity.MAJOR, "below blocker after an unreproduced proof")
    payload = review_of(result)
    assert payload.event == "REQUEST_CHANGES"
    assert payload.body.splitlines()[1:] == [
        "No verified issues found.",
        ("- `calc/stats.py:3` [major] First item is skipped (held: a generated test passed on "
         "this head; the verdict was set before the proof and is kept)")]
