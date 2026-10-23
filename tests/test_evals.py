import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from reviewhound import evals, proof
from reviewhound.evals import recorded_provider, run_case, run_eval, score
from reviewhound.proof import DockerRunner, LocalRunner, SandboxRunner

CASES = Path(__file__).parent.parent / "evals" / "cases"


def test_score_matches_each_expected_once_within_tolerance() -> None:
    expected = [{"file": "a.py", "line": 10}, {"file": "b.py", "line": 3}]
    tp, misses, false_alarms = score(expected, [("a.py", 12, "RIGHT"), ("a.py", 11, "RIGHT"),
                                                ("c.py", 1, "RIGHT")])
    assert tp == 1
    assert misses == ["b.py:3"]
    assert false_alarms == ["a.py:11", "c.py:1"]


def test_score_requires_the_same_side() -> None:
    expected = [{"file": "a.py", "line": 5, "side": "LEFT"}]
    assert score(expected, [("a.py", 5, "RIGHT")]) == (0, ["a.py:5:LEFT"], ["a.py:5"])
    assert score(expected, [("a.py", 6, "LEFT")]) == (1, [], [])


def test_recorded_eval_set_scores_and_checker_earns_its_keep() -> None:
    with_checker = run_eval(CASES)
    assert len(with_checker.cases) == 36
    assert sum(c.expected for c in with_checker.cases) == 30
    assert (round(with_checker.precision, 2), round(with_checker.recall, 2)) == (1.0, 1.0)
    assert sum(c.dropped_by_checker for c in with_checker.cases) == 34
    assert [c.error for c in with_checker.cases if c.error] == []
    baseline = run_eval(CASES, verify=False)
    assert (sum(c.reported for c in baseline.cases),
            sum(c.true_positives for c in baseline.cases)) == (58, 30)
    assert round(baseline.precision, 2) == 0.52


def test_committed_cases_match_what_the_generator_writes(tmp_path: Path) -> None:
    from evals import build_cases
    build_cases.main(tmp_path / "cases")

    def tree(root: Path) -> dict[str, str]:
        return {p.relative_to(root).as_posix(): p.read_text() for p in root.rglob("*")
                if p.is_file()}
    assert tree(tmp_path / "cases") == tree(CASES)


def test_eval_set_covers_each_language_and_trap_kind() -> None:
    names = sorted(p.name for p in CASES.iterdir() if (p / "case.json").exists())
    seeded = {lang: [n for n in names if f"-{lang}-" in n and "clean" not in n]
              for lang in ("js", "go")}
    assert (len(seeded["js"]), len(seeded["go"])) == (6, 8)
    assert len([n for n in names if "clean" in n]) == 6
    assert {"18-js-two-secrets-same-shape", "29-py-removed-role-check",
            "32-go-removed-deferred-unlock", "27-py-clean-dotted-symbol-trap",
            "35-py-removed-role-check-quoted-around",
            "36-go-removed-unlock-quoted-around"} <= set(names)


def test_proof_eval_cases_prove_the_real_bug_and_hold_the_unreproduced_blocker() -> None:
    report = run_eval(CASES)
    by_name = {c.name: c for c in report.cases}
    proven, held = by_name["33-py-proof-proven-discount"], by_name["34-py-proof-not-reproduced"]
    assert (proven.reported, proven.true_positives, proven.proof_mismatches) == (1, 1, [])
    assert (held.reported, held.false_alarms, held.proof_mismatches) == (0, [], [])
    assert [c.proof_mismatches for c in report.cases if c.proof_mismatches] == []


def test_a_wrong_proof_verdict_is_reported_as_a_mismatch(tmp_path: Path) -> None:
    case = tmp_path / "33-copy"
    shutil.copytree(CASES / "33-py-proof-proven-discount", case)
    spec = json.loads((case / "case.json").read_text())
    spec["expected_proofs"][0]["proof"] = "not_proven"
    (case / "case.json").write_text(json.dumps(spec))
    result = run_case(case, recorded_provider)
    assert result.proof_mismatches == ["proof pricing.py:3 expected not_proven got proven"]


# Live eval: with no usable sandbox, proof expectations are skipped, not counted as misses.

def fake_docker(daemon_up: bool) -> Any:
    def run(cmd: list[str], timeout_s: float) -> subprocess.CompletedProcess[str]:
        up = daemon_up and cmd[1] == "info"
        return subprocess.CompletedProcess(cmd, 0 if up else 1, "27.0" if up else "", "")
    return run


def docker_only(daemon_up: bool) -> dict[str, SandboxRunner]:
    return {"docker": DockerRunner(command=fake_docker(daemon_up))}


def test_live_eval_without_a_sandbox_skips_proof_expectations() -> None:
    report = run_eval(CASES, live=True, runners=docker_only(daemon_up=False))
    by_name = {c.name: c for c in report.cases}
    proven, held = by_name["33-py-proof-proven-discount"], by_name["34-py-proof-not-reproduced"]
    assert (proven.reported, proven.true_positives, proven.proof_mismatches,
            proven.proof_skipped) == (1, 1, [], ["proof pricing.py:3 skipped (no sandbox)"])
    assert (held.proof_mismatches, held.proof_skipped) == (
        [], ["proof clamp.py:3 skipped (no sandbox)"])
    assert [c.name for c in report.cases if c.proof_skipped or c.proof_mismatches] == [
        "33-py-proof-proven-discount", "34-py-proof-not-reproduced"]
    assert "skipped (no sandbox)" in report.render()


def test_live_eval_gives_no_local_runner_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(evals, "DockerRunner",
                        lambda: proof.DockerRunner(command=fake_docker(daemon_up=False)))
    result = run_case(CASES / "33-py-proof-proven-discount", recorded_provider, live=True)
    assert (result.proof_mismatches, result.proof_skipped) == (
        [], ["proof pricing.py:3 skipped (no sandbox)"])


def test_live_eval_with_a_usable_runner_still_checks_proofs(tmp_path: Path) -> None:
    case = tmp_path / "33-copy"
    shutil.copytree(CASES / "33-py-proof-proven-discount", case)
    spec = json.loads((case / "case.json").read_text())
    spec["expected_proofs"][0]["proof"] = "not_proven"
    (case / "case.json").write_text(json.dumps(spec))
    result = run_case(case, recorded_provider, live=True, runners={"local": LocalRunner()})
    assert (result.proof_mismatches, result.proof_skipped) == (
        ["proof pricing.py:3 expected not_proven got proven"], [])


def test_live_eval_with_docker_up_checks_proofs_and_with_it_down_skips(tmp_path: Path) -> None:
    case = tmp_path / "33-docker"
    shutil.copytree(CASES / "33-py-proof-proven-discount", case)
    for side in ("base", "head"):
        (case / side / ".reviewhound.yml").write_text("proof:\n  enabled: true\n"
                                                      "  runner: docker\n")
    up = run_case(case, recorded_provider, live=True, runners=docker_only(daemon_up=True))
    assert (up.proof_skipped, len(up.proof_mismatches)) == ([], 1)
    assert up.proof_mismatches[0].startswith("proof pricing.py:3 expected proven got ")
    down = run_case(case, recorded_provider, live=True, runners=docker_only(daemon_up=False))
    assert (down.proof_mismatches, down.proof_skipped) == (
        [], ["proof pricing.py:3 skipped (no sandbox)"])


def test_recorded_eval_without_a_sandbox_still_counts_proof_mismatches() -> None:
    result = run_case(CASES / "33-py-proof-proven-discount", recorded_provider,
                      runners=docker_only(daemon_up=False))
    assert (result.proof_mismatches, result.proof_skipped) == (
        ["proof pricing.py:3 expected proven got inconclusive"], [])


def test_an_alternative_fair_anchor_counts_once_and_only_on_its_own_side() -> None:
    from reviewhound.evals import score
    exp = [{"file": "c.go", "line": 12, "side": "LEFT", "also": [{"line": 11, "side": "RIGHT"}]}]
    assert score(exp, [("c.go", 11, "RIGHT")])[0] == 1
    assert score(exp, [("c.go", 11, "RIGHT"), ("c.go", 12, "LEFT")]) == (1, [], ["c.go:12:LEFT"])
    assert score(exp, [("c.go", 20, "RIGHT")]) == (0, ["c.go:12:LEFT"], ["c.go:20"])
    assert score([{"file": "c.go", "line": 12, "side": "LEFT"}], [("c.go", 11, "RIGHT")])[0] == 0
