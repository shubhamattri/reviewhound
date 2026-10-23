from pathlib import Path
from typing import Any

import pytest

from qa import github_qa
from qa.github_qa import api_checks

EXPECTED = [{"bug": "no role check", "file": "shop/orders.py", "lines": [20, 21]},
            {"bug": "assignment", "file": "web/cart.ts", "lines": [12, 12]}]


class FakeGitHub:
    def __init__(self, comments: list[dict[str, Any]]) -> None:
        self.comments = comments
        self.calls: list[tuple[str, str]] = []

    def call(self, method: str, url: str, **kw: Any) -> Any:
        self.calls.append((method, url))
        return self.comments


def comment(path: str, line: int | None, title: str = "**[major] t**") -> dict[str, Any]:
    return {"path": path, "line": line, "body": f"{title}\n\nbody"}


def test_every_planted_bug_needs_a_comment_inside_its_line_range() -> None:
    gh = FakeGitHub([comment("shop/orders.py", 21), comment("web/cart.ts", 13)])
    checks, posted = api_checks(gh, "o/r", 5, EXPECTED)  # type: ignore[arg-type]
    assert [(c.name, c.ok) for c in checks] == [
        ("API: comment for no role check", True),
        ("API: comment for assignment", False),
        ("API: no comments outside planted bugs", False)]
    assert posted[0] == {"path": "shop/orders.py", "line": 21, "title": "**[major] t**"}
    assert gh.calls == [("GET", "/repos/o/r/pulls/5/comments")]


def test_outdated_comment_without_a_line_does_not_count() -> None:
    gh = FakeGitHub([comment("shop/orders.py", None), comment("web/cart.ts", 12)])
    checks, _ = api_checks(gh, "o/r", 5, EXPECTED)  # type: ignore[arg-type]
    assert [c.ok for c in checks] == [False, True, False]


def test_all_bugs_found_and_nothing_else_passes() -> None:
    gh = FakeGitHub([comment("shop/orders.py", 20), comment("web/cart.ts", 12)])
    checks, _ = api_checks(gh, "o/r", 5, EXPECTED)  # type: ignore[arg-type]
    assert all(c.ok for c in checks)


def test_missing_browser_login_stops_before_opening_a_pr(tmp_path: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github_qa, "QA_HOME", tmp_path)
    monkeypatch.setattr("sys.argv", ["github_qa", "--milestone", "m0"])
    opened: list[str] = []
    monkeypatch.setattr(github_qa, "open_fresh_pr", lambda *a: opened.append("pr"))
    with pytest.raises(SystemExit) as exc:
        github_qa.main()
    assert "No saved GitHub login" in str(exc.value)
    assert opened == []


def test_qa_result_passes_only_when_every_check_passes() -> None:
    r = github_qa.QAResult(milestone="m", repo="o/r")
    assert r.passed is False
    r.checks = [github_qa.Check("a", True), github_qa.Check("b", True)]
    assert r.passed is True
    r.checks.append(github_qa.Check("c", False))
    assert r.passed is False
