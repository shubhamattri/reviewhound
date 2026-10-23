import argparse
import dataclasses
import json
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest

from reviewhound.config import ConfigError, ProofConfig, RepoConfig, Severity, load_config
from reviewhound.findings import Finding
from reviewhound.index.treesitter import CodeIndex
from reviewhound.policy import PolicyDecision
from reviewhound.poster import _comment_body
from reviewhound.proof import (
    DockerRunner,
    ProofAnswer,
    ProofError,
    Prover,
    RunResult,
    _write_tree,
    classify,
    eligible,
    parse_junit,
    run_proof_stage,
)
from reviewhound.providers import FakeProvider, ProviderRegistry
from reviewhound.vcs.base import DictSnapshot

JUNIT = Path(__file__).parent / "fixtures" / "junit"


def sample(name: str, rc: int) -> RunResult:
    return RunResult(returncode=rc, junit_xml=(JUNIT / f"{name}.xml").read_text())


# --- config -----------------------------------------------------------------------------------

def test_proof_config_defaults_are_off_and_conservative() -> None:
    assert RepoConfig().proof == ProofConfig(enabled=False, min_severity="blocker",
                                             runner="docker", timeout_s=60, max_findings=3,
                                             downgrade_unproven=False)


def test_proof_config_overrides_merge() -> None:
    cfg = load_config("proof:\n  enabled: true\n  runner: local\n  min_severity: Major\n")
    assert (cfg.proof.enabled, cfg.proof.runner, cfg.proof.min_severity,
            cfg.proof.timeout_s) == (True, "local", "major", 60)


@pytest.mark.parametrize("text", [
    "proof:\n  runner: podman\n",
    "proof:\n  min_severity: urgent\n",
    "proof:\n  timeout_s: 0\n",
    "proof:\n  timeout_s: 601\n",
    "proof:\n  max_findings: -1\n",
    "proof:\n  enabeld: true\n",
])
def test_bad_proof_config_is_rejected(text: str) -> None:
    with pytest.raises(ConfigError, match="invalid"):
        load_config(text)


# --- junit parsing and classification on real pytest reports --------------------------------

def test_parse_junit_reads_exception_type_from_failure_message() -> None:
    [case] = parse_junit((JUNIT / "type.xml").read_text())
    assert (case.name, case.status, case.exc_type) == ("test_type::test_a", "failed", "TypeError")


@pytest.mark.parametrize("name, rc, expected_exc, verdict, reason", [
    ("assert", 1, None, "proven", "the generated test fails on the PR head with AssertionError"),
    ("assert_stripped", 1, None, "proven",
     "the generated test fails on the PR head with AssertionError"),
    ("assertmsg", 1, None, "proven",
     "the generated test fails on the PR head with AssertionError"),
    ("type", 1, "TypeError", "proven", "the generated test fails on the PR head with TypeError"),
    ("type", 1, None, "inconclusive",
     "test failed with TypeError, not the claimed behaviour"),
    ("type", 1, "KeyError", "inconclusive",
     "test failed with TypeError, not the claimed behaviour"),
    ("import", 1, None, "inconclusive",
     "test failed with ModuleNotFoundError, not the claimed behaviour"),
    ("import", 1, "ModuleNotFoundError", "inconclusive",
     "test failed with ModuleNotFoundError, not the claimed behaviour"),
    ("collect", 2, None, "inconclusive", "test error: collection failure"),
    ("syntax", 2, None, "inconclusive", "test error: collection failure"),
    ("fixture", 1, None, "inconclusive", 'test error: failed on setup with "RuntimeError: s"'),
    ("pass", 0, None, "not_proven", "the generated test passes on the PR head"),
    ("pass", 3, None, "inconclusive", "tests passed but pytest exited 3"),
    ("assert", 2, None, "inconclusive", "tests failed but pytest exited 2"),
    ("skip", 0, None, "inconclusive", "a test was skipped"),
    ("none", 5, None, "inconclusive", "no tests collected"),
])
def test_classify_real_junit_samples(name: str, rc: int, expected_exc: str | None, verdict: str,
                                     reason: str) -> None:
    assert classify(sample(name, rc), expected_exc) == (verdict, reason)


def test_missing_fixture_is_an_error_not_a_proof() -> None:
    verdict, reason = classify(sample("nofixture", 1))
    assert verdict == "inconclusive"
    assert reason.startswith("test error: failed on setup with")


def test_one_failing_and_one_passing_test_is_inconclusive() -> None:
    xml = ('<testsuites><testsuite><testcase classname="t" name="a">'
           '<failure message="AssertionError: x"/></testcase>'
           '<testcase classname="t" name="b"/></testsuite></testsuites>')
    assert classify(RunResult(1, xml)) == ("inconclusive",
                                           "some generated tests passed and some failed")


@pytest.mark.parametrize("run, reason", [
    (RunResult(None, None, error="docker unavailable: the daemon is not running"),
     "docker unavailable: the daemon is not running"),
    (RunResult(None, None, timed_out=True), "test run timed out"),
    (RunResult(2, None), "no junit report (exit 2)"),
    (RunResult(1, "<not xml"), "junit xml does not parse: ParseError"),
    (RunResult(1, "<html/>"), "unexpected junit root <html>"),
    (RunResult(1, '<!DOCTYPE x [<!ENTITY a "aaaa">]><testsuites>&a;</testsuites>'),
     "junit xml does not parse: EntitiesForbidden"),
])
def test_runner_problems_are_inconclusive(run: RunResult, reason: str) -> None:
    assert classify(run) == ("inconclusive", reason)


# --- model answer validation ---------------------------------------------------------------

@pytest.mark.parametrize("path", ["../test_x.py", "/tmp/test_x.py", "tests/x.py",
                                  "tests/test_x.txt", "tests/../../test_x.py", "-p/test_x.py",
                                  "tests/test x.py"])
def test_unsafe_or_non_test_paths_are_rejected(path: str) -> None:
    with pytest.raises(ValueError):
        ProofAnswer(test_path=path, code="def test_a(): pass")


def test_write_tree_refuses_to_escape_the_sandbox(tmp_path: Path) -> None:
    with pytest.raises(ProofError, match="outside the sandbox"):
        _write_tree({"../escape.py": "x"}, tmp_path / "work")
    assert not (tmp_path / "escape.py").exists()


# --- Docker runner with a fake command runner ----------------------------------------------

class FakeDocker:
    def __init__(self, info_rc: int = 0, run_rc: int = 1, junit: str | None = "assert",
                 info_exc: Exception | None = None, run_exc: Exception | None = None) -> None:
        self.calls: list[list[str]] = []
        self.timeouts: list[float] = []
        self.info_rc, self.run_rc, self.junit = info_rc, run_rc, junit
        self.info_exc, self.run_exc = info_exc, run_exc
        self.workdirs: list[Path] = []

    def __call__(self, cmd: list[str], timeout_s: float) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        self.timeouts.append(timeout_s)
        if cmd[1] == "info":
            if self.info_exc:
                raise self.info_exc
            return subprocess.CompletedProcess(cmd, self.info_rc, "27.0.1\n", "")
        if cmd[1] == "kill":
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if self.run_exc:
            raise self.run_exc
        mounts = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-v"]
        src, out = (Path(m.split(":")[0]) for m in mounts)
        self.workdirs.append(src.parent)
        assert (src / "pkg" / "mod.py").read_text() == "X = 1\n"
        assert (src / "tests" / "test_p.py").read_text() == "def test_a(): assert 0\n"
        if self.junit:
            (out / "junit.xml").write_text((JUNIT / f"{self.junit}.xml").read_text())
        return subprocess.CompletedProcess(cmd, self.run_rc, "", "boom" if self.run_rc else "")


FILES = {"pkg/mod.py": "X = 1\n", "tests/test_p.py": "def test_a(): assert 0\n"}


def test_docker_runner_uses_a_locked_down_container_and_cleans_up() -> None:
    fake = FakeDocker()
    res = DockerRunner(command=fake, startup_grace_s=5).run(FILES, "tests/test_p.py", 60)
    assert classify(res) == ("proven",
                             "the generated test fails on the PR head with AssertionError")
    run_cmd = fake.calls[1]
    for flag in (["--rm"], ["--network", "none"], ["--read-only"], ["--memory", "512m"],
                 ["--cpus", "1"], ["--pids-limit", "128"], ["--cap-drop", "ALL"],
                 ["--tmpfs", "/tmp:rw,size=256m"]):
        assert any(run_cmd[i:i + len(flag)] == flag for i in range(len(run_cmd))), flag
    assert "python:3.12-slim" in run_cmd
    assert [m.split(":", 1)[1] for m in (run_cmd[i + 1] for i, a in enumerate(run_cmd)
                                         if a == "-v")] == ["/src:ro", "/out:rw"]
    assert run_cmd[-2:] == ["sh", "tests/test_p.py"]  # the path is an argv, never shell text
    assert fake.timeouts == [15, 65]
    assert not fake.workdirs[0].exists()


@pytest.mark.parametrize("fake, reason", [
    (FakeDocker(info_rc=1), "docker unavailable: the daemon is not running"),
    (FakeDocker(info_exc=FileNotFoundError("docker")),
     "docker unavailable: the docker CLI is not installed"),
    (FakeDocker(info_exc=subprocess.TimeoutExpired("docker", 15)),
     "docker unavailable: docker info timed out"),
])
def test_docker_down_is_inconclusive_without_running_anything(fake: FakeDocker,
                                                              reason: str) -> None:
    res = DockerRunner(command=fake).run(FILES, "tests/test_p.py", 60)
    assert classify(res) == ("inconclusive", reason)
    assert [c[1] for c in fake.calls] == ["info"]


def test_docker_timeout_kills_the_container_by_name() -> None:
    fake = FakeDocker(run_exc=subprocess.TimeoutExpired("docker", 90))
    res = DockerRunner(command=fake).run(FILES, "tests/test_p.py", 60)
    assert classify(res) == ("inconclusive", "test run timed out")
    name = fake.calls[1][fake.calls[1].index("--name") + 1]
    assert name.startswith("reviewhound-proof-")
    assert fake.calls[2] == ["docker", "kill", name]


def test_oversized_junit_written_by_the_test_is_not_parsed(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("reviewhound.proof.MAX_JUNIT_BYTES", 100)
    res = DockerRunner(command=FakeDocker()).run(FILES, "tests/test_p.py", 60)
    assert classify(res) == ("inconclusive", "unexpected junit root <oversized-report>")


def test_docker_own_failure_is_inconclusive_not_a_test_result() -> None:
    res = DockerRunner(command=FakeDocker(run_rc=125, junit=None)).run(FILES, "tests/test_p.py",
                                                                       60)
    assert classify(res) == ("inconclusive", "docker run failed: boom")


# --- prover and the proof stage with fakes -------------------------------------------------

SRC = "def total(xs):\n    result = 0\n    for i in range(1, len(xs)):\n" \
      "        result += xs[i]\n    return result\n"
HEAD = {"calc/stats.py": SRC, "README.md": "hi\n"}
GOOD = json.dumps({"test_path": "tests/test_proof_total.py",
                   "code": "from calc.stats import total\n\ndef test_t():\n"
                           "    assert total([1, 2]) == 3\n"})


def finding(sev: Severity = Severity.BLOCKER, file: str = "calc/stats.py", verified: bool = True,
            source: str = "model:sonnet") -> Finding:
    return Finding(file=file, line=3, severity=sev, category="correctness",
                   title="first element skipped", body="range starts at 1",
                   evidence_quote="for i in range(1, len(xs)):", source=source,
                   verified=verified)


class RecordingRunner:
    name = "fake"

    def __init__(self, result: RunResult | Exception) -> None:
        self.result = result
        self.runs: list[tuple[dict[str, str], str, float]] = []

    def run(self, files: Mapping[str, str], test_path: str, timeout_s: float) -> RunResult:
        self.runs.append((dict(files), test_path, timeout_s))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def prover(answers: list[str], runner: RecordingRunner, **cfg: object) -> tuple[Prover,
                                                                                FakeProvider]:
    fake = FakeProvider(answers)
    pc = ProofConfig(enabled=True, **cfg)  # type: ignore[arg-type]
    return Prover(ProviderRegistry([fake]), runner, pc, "sonnet", 30), fake


def test_prover_sends_finding_function_and_file_and_runs_head_plus_test() -> None:
    runner = RecordingRunner(sample("assert", 1))
    p, fake = prover([GOOD], runner)
    res = p.prove(finding(), HEAD, CodeIndex.build(HEAD))
    assert (res.verdict, res.test_path) == ("proven", "tests/test_proof_total.py")
    assert res.code.startswith("from calc.stats import total")
    [prompt] = fake.prompts
    assert "first element skipped" in prompt and "range starts at 1" in prompt
    assert "importable as module `calc.stats`" in prompt
    assert "```python\ndef total(xs):\n    result = 0" in prompt
    [(files, path, timeout)] = runner.runs
    assert (path, timeout) == ("tests/test_proof_total.py", 60)
    assert files == {**HEAD, "tests/test_proof_total.py": json.loads(GOOD)["code"]}


@pytest.mark.parametrize("answer, reason", [
    ("not json", "malformed proof answer: JSONDecodeError"),
    (json.dumps({"code": "x"}), "malformed proof answer: ValidationError"),
    (json.dumps({"test_path": "../t.py", "code": "x"}), "malformed proof answer: ValidationError"),
    (json.dumps({"test_path": "tests/test_a.py", "code": ""}),
     "malformed proof answer: ValidationError"),
])
def test_malformed_model_answer_is_inconclusive_and_runs_nothing(answer: str,
                                                                  reason: str) -> None:
    runner = RecordingRunner(sample("assert", 1))
    p, _ = prover([answer], runner)
    res = p.prove(finding(), HEAD, CodeIndex.build(HEAD))
    assert (res.verdict, res.reason) == ("inconclusive", reason)
    assert runner.runs == []


def test_model_failure_is_inconclusive() -> None:
    runner = RecordingRunner(sample("assert", 1))
    p, _ = prover([], runner)
    res = p.prove(finding(), HEAD, CodeIndex.build(HEAD))
    assert res.verdict == "inconclusive"
    assert res.reason.startswith("model call failed: all providers failed")
    assert runner.runs == []


def test_a_test_path_that_would_overwrite_repo_code_is_refused() -> None:
    runner = RecordingRunner(sample("pass", 0))
    head = {**HEAD, "tests/test_proof_total.py": "def test_real(): pass\n"}
    p, _ = prover([GOOD], runner)
    res = p.prove(finding(), head, CodeIndex.build(head))
    assert (res.verdict, res.reason) == (
        "inconclusive", "test path tests/test_proof_total.py would overwrite a repo file")
    assert runner.runs == []


def test_eligibility_filters_and_caps() -> None:
    keep = [finding(), finding()]
    out = [finding(Severity.MAJOR), finding(file="src/a.ts"), finding(verified=False),
           finding(source="scanner:builtin")]
    assert eligible(ProofConfig(enabled=True, max_findings=1), keep + out) == keep[:1]
    assert eligible(ProofConfig(enabled=True, max_findings=20), out + keep) == keep
    assert [eligible(ProofConfig(enabled=True, max_findings=20), [f]) for f in out] == \
        [[], [], [], []]
    assert eligible(ProofConfig(enabled=True, min_severity="major"),
                    [out[0]]) == [out[0]]


def test_findings_on_deleted_lines_are_not_sent_for_proof() -> None:
    # A LEFT finding's line is a base line, so the head function and line in the prompt are wrong.
    left = dataclasses.replace(finding(), side="LEFT")
    right = finding()
    assert eligible(ProofConfig(enabled=True), [left, right]) == [right]


def stage(findings: list[Finding], runner: RecordingRunner | None, answers: list[str],
          cfg_text: str) -> tuple[PolicyDecision, FakeProvider]:
    cfg = load_config(cfg_text)
    fake = FakeProvider(answers)
    p = Prover(ProviderRegistry([fake]), runner, cfg.proof, "sonnet", 30) if runner else None
    decision = PolicyDecision(post=list(findings), event="REQUEST_CHANGES")
    run_proof_stage(p, decision, cfg, DictSnapshot(HEAD), CodeIndex.build(HEAD))
    return decision, fake


def test_not_proven_blocker_is_downgraded_only_when_configured() -> None:
    f = finding()
    decision, _ = stage([f], RecordingRunner(sample("pass", 0)), [GOOD],
                        "proof:\n  enabled: true\n  downgrade_unproven: true\n")
    assert (f.proof, f.severity, decision.event) == ("not_proven", Severity.MAJOR,
                                                     "REQUEST_CHANGES")
    assert decision.post == [f]
    g = finding()
    decision, _ = stage([g], RecordingRunner(sample("pass", 0)), [GOOD],
                        "proof:\n  enabled: true\n")
    assert (g.proof, g.severity, decision.event) == ("not_proven", Severity.BLOCKER,
                                                     "REQUEST_CHANGES")


def test_downgrade_below_the_posting_floor_holds_the_finding() -> None:
    f = finding()
    decision, _ = stage([f], RecordingRunner(sample("pass", 0)), [GOOD],
                        "policy:\n  min_severity_to_post: blocker\n"
                        "proof:\n  enabled: true\n  downgrade_unproven: true\n")
    assert decision.post == []
    assert decision.held == [(f, "below blocker after an unreproduced proof")]
    assert decision.event == "REQUEST_CHANGES"


def test_inconclusive_and_proven_never_change_severity() -> None:
    f = finding()
    stage([f], RecordingRunner(sample("collect", 2)), [GOOD],
          "proof:\n  enabled: true\n  downgrade_unproven: true\n")
    assert (f.proof, f.severity) == ("inconclusive", Severity.BLOCKER)


def test_a_crashing_runner_never_fails_the_stage() -> None:
    f, g = finding(), finding()
    decision, _ = stage([f, g], RecordingRunner(RuntimeError("disk full")), [GOOD, GOOD],
                        "proof:\n  enabled: true\n")
    assert [x.proof for x in (f, g)] == ["inconclusive", "inconclusive"]
    assert f.notes == ["proof inconclusive: proof crashed: RuntimeError"]
    assert decision.post == [f, g]


def test_missing_runner_marks_inconclusive_without_asking_the_model() -> None:
    f = finding()
    _, fake = stage([f], None, [GOOD], "proof:\n  enabled: true\n  runner: local\n")
    assert f.proof == "inconclusive"
    assert f.notes == ["proof inconclusive: proof runner 'local' is not enabled on this server"]
    assert fake.prompts == []


def test_oversized_snapshot_is_inconclusive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("reviewhound.proof.MAX_SNAPSHOT_BYTES", 10)
    f = finding()
    runner = RecordingRunner(sample("assert", 1))
    _, fake = stage([f], runner, [GOOD], "proof:\n  enabled: true\n")
    assert f.notes == ["proof inconclusive: head snapshot is over 10 bytes"]
    assert (fake.prompts, runner.runs) == ([], [])


# --- the posted comment --------------------------------------------------------------------

def test_proven_comment_says_so_and_folds_the_test_in_details() -> None:
    f = finding()
    f.proof, f.proof_test = "proven", "def test_x():\n    s = '```'\n    assert s == ''\n"
    body = _comment_body(f)
    assert body == ("**[blocker] first element skipped**\n\nrange starts at 1\n\n"
                    "**Reproduced by a generated test:** it fails on this PR's head.\n\n"
                    "<details><summary>Failing test</summary>\n\n````python\n"
                    "def test_x():\n    s = '```'\n    assert s == ''\n````\n\n</details>")


def test_body_only_findings_say_proven_and_skipped_files_follow() -> None:
    from reviewhound.diff import Diff
    from reviewhound.poster import build_review
    proven, plain = finding(), dataclasses.replace(finding(), title="other")
    proven.proof = "proven"
    decision = PolicyDecision(post=[proven, plain], event="REQUEST_CHANGES")
    review = build_review("h1", decision, Diff(), "standard", ["fake"],
                          not_reviewed=[("big.bin", "diff too large for GitHub to return")])
    assert review.comments == []
    assert review.body.splitlines()[2:] == [
        "- `calc/stats.py:3` [blocker] first element skipped (reproduced by a generated test)",
        "- `calc/stats.py:3` [blocker] other",
        "- not reviewed: big.bin (diff too large for GitHub to return)"]


@pytest.mark.parametrize("verdict", ["not_proven", "inconclusive", None])
def test_unproven_comment_has_no_proof_section(verdict: str | None) -> None:
    f = finding()
    f.proof, f.proof_test = verdict, "def test_x(): pass"  # type: ignore[assignment]
    assert _comment_body(f) == "**[blocker] first element skipped**\n\nrange starts at 1"


def test_cli_adds_the_local_runner_only_when_the_operator_opts_in() -> None:
    from reviewhound.cli import _proof_runners

    runners = _proof_runners(argparse.Namespace(allow_local_proof=False, proof_image="img:1"))
    assert sorted(runners) == ["docker"]
    assert isinstance(runners["docker"], DockerRunner) and runners["docker"].image == "img:1"
    runners = _proof_runners(argparse.Namespace(allow_local_proof=True, proof_image="img:1"))
    assert sorted(runners) == ["docker", "local"]


@pytest.mark.parametrize("claimed", ["builtins.ImportError", "builtins.ModuleNotFoundError"])
def test_dotted_import_error_claim_is_still_not_a_proof(claimed: str) -> None:
    assert classify(sample("import", 1), claimed) == (
        "inconclusive", "test failed with ModuleNotFoundError, not the claimed behaviour")


def test_planted_fifo_or_symlink_report_is_not_read(tmp_path: Path) -> None:
    from reviewhound.proof import _read_junit

    fifo = tmp_path / "fifo.xml"
    os.mkfifo(fifo)
    link = tmp_path / "link.xml"
    link.symlink_to("/dev/zero")
    assert (_read_junit(fifo), _read_junit(link)) == ("<unreadable-report/>",
                                                      "<unreadable-report/>")
    assert _read_junit(tmp_path / "missing.xml") is None


def test_floor_and_verdict_are_reapplied_even_if_proving_crashes(
        monkeypatch: pytest.MonkeyPatch) -> None:
    def downgrade_then_crash(prover: object, candidates: list[Finding], *a: object) -> None:
        candidates[0].severity = Severity.MAJOR
        raise MemoryError("mid-stage")
    monkeypatch.setattr("reviewhound.proof._prove_each", downgrade_then_crash)
    f = finding()
    cfg = load_config("proof:\n  enabled: true\n")
    p = Prover(ProviderRegistry([FakeProvider([GOOD])]), RecordingRunner(sample("pass", 0)),
               cfg.proof, "sonnet", 30)
    decision = PolicyDecision(post=[f], event="REQUEST_CHANGES")
    with pytest.raises(MemoryError):
        run_proof_stage(p, decision, cfg, DictSnapshot(HEAD), CodeIndex.build(HEAD))
    assert (decision.post, decision.event) == ([f], "REQUEST_CHANGES")


def test_report_symlinked_to_a_regular_file_outside_is_not_followed(tmp_path: Path) -> None:
    from reviewhound.proof import _read_junit

    secret = tmp_path / "server_secret.xml"
    secret.write_text((JUNIT / "assert.xml").read_text())
    link = tmp_path / "junit.xml"
    link.symlink_to(secret)
    assert _read_junit(link) == "<unreadable-report/>"


def test_output_tail_refuses_fifo_and_symlink_and_caps_size(tmp_path: Path) -> None:
    import threading

    from reviewhound.proof import _tail

    fifo = tmp_path / "fifo.txt"
    os.mkfifo(fifo)
    got: list[str] = []
    reader = threading.Thread(target=lambda: got.append(_tail(fifo)), daemon=True)
    reader.start()
    reader.join(3)
    assert got == [""], "reading a planted FIFO must not block"
    secret = tmp_path / "server_secret.txt"
    secret.write_text("TOKEN=abc")
    link = tmp_path / "link.txt"
    link.symlink_to(secret)
    big = tmp_path / "big.txt"
    big.write_text("a" * 5000 + "END")
    assert (_tail(link), _tail(tmp_path / "missing.txt")) == ("", "")
    assert _tail(big, limit=10) == "aaaaaaaEND"
