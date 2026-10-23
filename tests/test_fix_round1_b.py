"""Fix round 1, track B: checker anchoring, fingerprints, child env, store ordering."""

from __future__ import annotations

import base64
import subprocess
from pathlib import Path
from typing import Any

import pytest

from reviewhound.checker import ClaimChecker
from reviewhound.config import RepoConfig
from reviewhound.findings import Finding, ModelFinding
from reviewhound.index.treesitter import CodeIndex
from reviewhound.procenv import child_env
from reviewhound.scanners import SemgrepScanner
from reviewhound.vcs.base import DictSnapshot
from reviewhound.vcs.github import GitHubVCS
from reviewhound.vcs.memory import diff_file_maps

# B7: a blank line added or removed inside a quote must not anchor unchanged code.
SPAN_BASE = {"m.py": "def f():\n    first_call_here()\n    second_call_here()\n    return 1\n"}
SPAN_HEAD = {"m.py": "def f():\n    first_call_here()\n\n    second_call_here()\n    return 1\n"}
SPAN_QUOTE = "first_call_here()\nsecond_call_here()"


def span_finding(line: int) -> ModelFinding:
    return ModelFinding.model_validate({
        "file": "m.py", "line": line, "severity": "major", "title": "t", "body": "b",
        "evidence_quote": SPAN_QUOTE})


def test_blank_line_added_inside_a_quote_does_not_anchor_it() -> None:
    chk = ClaimChecker(diff_file_maps(SPAN_BASE, SPAN_HEAD), CodeIndex.build(SPAN_HEAD))
    _f, reason = chk.check(span_finding(3), "model:m")
    assert reason == "evidence quote is only in code this PR did not change"


def test_blank_line_deleted_inside_a_quote_does_not_anchor_it_on_the_left() -> None:
    chk = ClaimChecker(diff_file_maps(SPAN_HEAD, SPAN_BASE), CodeIndex.build(SPAN_BASE),
                       base_reader=SPAN_HEAD.get)
    f, reason = chk.check(span_finding(3), "model:m")
    assert (reason, f.side) == ("evidence quote is only in code this PR did not change", "RIGHT")


def test_a_real_change_inside_the_quote_still_anchors_on_its_non_blank_line() -> None:
    head = {"m.py": SPAN_HEAD["m.py"].replace("second_call_here()", "second_call_here()  # x")}
    chk = ClaimChecker(diff_file_maps(SPAN_BASE, head), CodeIndex.build(head))
    f, reason = chk.check(span_finding(3), "model:m")
    assert (reason, f.line, f.side) == (None, 4, "RIGHT")


# B8: LEFT and RIGHT findings with the same quote are different findings.

def _fp(side: str, **kw: Any) -> str:
    f = Finding(file="app/admin.py", line=11, severity=4,  # type: ignore[arg-type]
                category="auth", title="t", body="", evidence_quote="db.delete(uid)", source="",
                side=side, **kw)  # type: ignore[arg-type]
    return f.fingerprint


def test_left_side_is_part_of_the_fingerprint_and_right_stays_stable() -> None:
    assert _fp("RIGHT") == "68eb534220237c8b"  # the value before side existed
    assert _fp("LEFT") != _fp("RIGHT")
    assert len(_fp("LEFT")) == 16
    assert _fp("LEFT", identity="abc") != _fp("RIGHT", identity="abc")


# B9: child processes get PATH, HOME, LANG and what they need, never the service's tokens.

LEAKY = {"GITHUB_TOKEN": "ghp_x", "ANTHROPIC_API_KEY": "sk-x", "CLAUDE_CODE_OAUTH_TOKEN": "o",
         "REVIEWHOUND_WEBHOOK_SECRET": "w", "REVIEWHOUND_DATABASE_URL": "pg-url-with-password",
         "AWS_SECRET_ACCESS_KEY": "a"}
SAFE = {"PATH": "/usr/bin:/bin", "HOME": "/home/rh", "LANG": "C.UTF-8"}


def _leaky_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in {**LEAKY, **SAFE}.items():
        monkeypatch.setenv(k, v)


def _capture(monkeypatch: pytest.MonkeyPatch, module: Any,
             stdout: str = "") -> list[tuple[list[str], dict[str, str]]]:
    calls: list[tuple[list[str], dict[str, str]]] = []

    def run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append((cmd, dict(kw["env"])))
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")
    monkeypatch.setattr(module.subprocess, "run", run)
    return calls


def test_git_fetch_env_holds_only_the_auth_header(monkeypatch: pytest.MonkeyPatch,
                                                  tmp_path: Path) -> None:
    import reviewhound.vcs.github as gh_mod
    _leaky_env(monkeypatch)
    calls = _capture(monkeypatch, gh_mod)
    GitHubVCS("tok123", tmp_path).snapshot("acme/shop", "abc")
    basic = base64.b64encode(b"x-access-token:tok123").decode()
    want = {**SAFE, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}"}
    assert [c[0][1] for c in calls] == ["init", "remote", "fetch", "checkout"]
    assert [env for _cmd, env in calls] == [want] * 4


def test_git_log_env_has_no_tokens_at_all(monkeypatch: pytest.MonkeyPatch,
                                          tmp_path: Path) -> None:
    import reviewhound.vcs.github as gh_mod
    _leaky_env(monkeypatch)
    dest = tmp_path / "acme__shop" / "abc"
    dest.mkdir(parents=True)
    (dest / ".reviewhound-complete").write_text("abc")
    calls = _capture(monkeypatch, gh_mod, stdout="a.py\na.py\n")
    assert GitHubVCS("tok123", tmp_path).churn("acme/shop", "abc") == {"a.py": 2}
    assert calls == [(["git", "log", "--name-only", "--format="],
                      {**SAFE, "GIT_TERMINAL_PROMPT": "0"})]


def test_semgrep_env_has_no_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    import reviewhound.scanners as sc_mod
    _leaky_env(monkeypatch)
    calls = _capture(monkeypatch, sc_mod, stdout='{"results": []}')
    scanner = SemgrepScanner()
    scanner.binary = "/opt/semgrep"
    assert scanner.scan(diff_file_maps({}, {"a.py": "x = 1\n"}),
                        DictSnapshot({"a.py": "x = 1\n"}), RepoConfig()) == []
    assert [env for _cmd, env in calls] == [SAFE]


def test_child_env_skips_unset_safe_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    _leaky_env(monkeypatch)
    monkeypatch.delenv("LANG")
    assert child_env(EXTRA="1") == {"PATH": SAFE["PATH"], "HOME": SAFE["HOME"], "EXTRA": "1"}
