"""LocalRunner end to end: real pytest subprocesses on tiny projects in a temp dir."""

import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest

from reviewhound.proof import LocalRunner, ProofError, classify

BUGGY = {"calc/stats.py": "def total(xs):\n    result = 0\n    for i in range(1, len(xs)):\n"
                          "        result += xs[i]\n    return result\n"}
CORRECT = {"calc/stats.py": "def total(xs):\n    return sum(xs)\n"}
PROOF = "from calc.stats import total\n\n\ndef test_total_counts_every_item():\n" \
        "    assert total([1, 2, 3]) == 6\n"
TEST = "tests/test_proof_total.py"


@pytest.fixture
def sandbox_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "tmps"
    root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    return root


def test_real_bug_is_proven_and_temp_dir_removed(sandbox_tmp: Path) -> None:
    res = LocalRunner().run({**BUGGY, TEST: PROOF}, TEST, 30)
    assert classify(res) == ("proven",
                             "the generated test fails on the PR head with AssertionError")
    assert res.returncode == 1
    assert list(sandbox_tmp.iterdir()) == []


def test_correct_function_is_not_proven(sandbox_tmp: Path) -> None:
    res = LocalRunner().run({**CORRECT, TEST: PROOF}, TEST, 30)
    assert classify(res) == ("not_proven", "the generated test passes on the PR head")
    assert list(sandbox_tmp.iterdir()) == []


def test_syntax_error_in_generated_test_is_inconclusive(sandbox_tmp: Path) -> None:
    res = LocalRunner().run({**BUGGY, TEST: "def test_a(:\n    pass\n"}, TEST, 30)
    assert classify(res) == ("inconclusive", "test error: collection failure")
    assert list(sandbox_tmp.iterdir()) == []


def test_infinite_loop_hits_the_timeout_and_is_killed(sandbox_tmp: Path) -> None:
    looping = {"spin.py": "def spin():\n    while True:\n        pass\n"}
    test = "from spin import spin\n\n\ndef test_spin():\n    spin()\n"
    started = time.monotonic()
    res = LocalRunner().run({**looping, "test_spin.py": test}, "test_spin.py", 2)
    assert classify(res) == ("inconclusive", "test run timed out")
    assert time.monotonic() - started < 10
    assert list(sandbox_tmp.iterdir()) == []


def test_environment_is_scrubbed_and_home_is_the_temp_dir(
        sandbox_tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    check = ("import os\n\n\ndef test_env():\n"
             "    assert 'GITHUB_TOKEN' not in os.environ\n"
             "    assert 'ANTHROPIC_API_KEY' not in os.environ\n"
             f"    assert os.environ['HOME'].startswith({str(sandbox_tmp)!r})\n"
             "    assert os.getcwd().endswith('/work')\n")
    res = LocalRunner().run({"test_env.py": check}, "test_env.py", 30)
    assert classify(res) == ("not_proven", "the generated test passes on the PR head")


def test_path_escaping_the_sandbox_is_refused_and_cleaned_up(sandbox_tmp: Path,
                                                             tmp_path: Path) -> None:
    with pytest.raises(ProofError, match="outside the sandbox"):
        LocalRunner().run({"../../evil.py": "x", TEST: PROOF}, TEST, 30)
    assert not (sandbox_tmp / "evil.py").exists()
    assert list(sandbox_tmp.iterdir()) == []


def test_missing_python_is_inconclusive_and_cleans_up(sandbox_tmp: Path) -> None:
    res = LocalRunner(python="/nonexistent/python3").run({**BUGGY, TEST: PROOF}, TEST, 30)
    assert classify(res) == ("inconclusive", "could not start python: FileNotFoundError")
    assert list(sandbox_tmp.iterdir()) == []


def test_timed_out_process_is_killed_and_reaped(sandbox_tmp: Path,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen

    def recording_popen(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        proc = real_popen(*args, **kwargs)
        started.append(proc)
        return proc
    monkeypatch.setattr(subprocess, "Popen", recording_popen)
    sleeper = "import time\n\n\ndef test_sleep():\n    time.sleep(600)\n"
    res = LocalRunner().run({"test_sleep.py": sleeper}, "test_sleep.py", 2)
    assert classify(res) == ("inconclusive", "test run timed out")
    [proc] = started
    assert proc.returncode == -9
    assert list(sandbox_tmp.iterdir()) == []
