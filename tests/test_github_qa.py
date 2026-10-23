import argparse
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from qa import github_qa
from qa.github_qa import api_checks, fetch_bot_review, run

EXPECTED = [{"bug": "no role check", "file": "shop/orders.py", "lines": [20, 21]},
            {"bug": "assignment", "file": "web/cart.ts", "lines": [12, 12]}]
HEAD = "abc123"


class FakeGitHub:
    """Answers by (method, url). Raises on anything it was not told about."""

    def __init__(self, routes: dict[tuple[str, str], Any]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str]] = []

    def call(self, method: str, url: str, **kw: Any) -> Any:
        self.calls.append((method, url))
        answer = self.routes[(method, url)]
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, list) and answer and isinstance(answer[0], list):
            return answer.pop(0) if len(answer) > 1 else answer[0]
        return answer


def review(rid: int, commit: str, body: str = "Reviewhound (standard review). x") -> dict[str, Any]:
    return {"id": rid, "commit_id": commit, "body": body}


def comment(cid: int, path: str, line: int | None, title: str = "**[major] t**") -> dict[str, Any]:
    return {"id": cid, "path": path, "line": line, "body": f"{title}\n\nbody"}


def test_only_the_bot_review_on_this_head_and_its_comments_are_used() -> None:
    gh = FakeGitHub({
        ("GET", "/repos/o/r/pulls/5/reviews"): [
            review(1, "old-head"), review(3, HEAD), review(4, HEAD, body="LGTM from a human")],
        ("GET", "/repos/o/r/pulls/5/comments"): [
            {**comment(30, "web/cart.ts", 12), "pull_request_review_id": 3},
            {**comment(31, "web/cart.ts", 15), "pull_request_review_id": 1}],
    })
    found, posted = fetch_bot_review(gh, "o/r", 5, HEAD, sleep=lambda s: None)
    assert found is not None and found["id"] == 3
    assert posted == [{"id": 30, "path": "web/cart.ts", "line": 12, "title": "[major] t",
                       "body": "**[major] t**\n\nbody"}]


def test_review_that_never_appears_times_out_as_none() -> None:
    gh = FakeGitHub({("GET", "/repos/o/r/pulls/5/reviews"): [review(1, "old-head")]})
    slept: list[float] = []
    found, posted = fetch_bot_review(gh, "o/r", 5, HEAD, timeout_s=10, sleep=slept.append)
    assert (found, posted) == (None, [])
    assert slept == [5, 5]


def test_review_that_appears_late_is_found_by_polling() -> None:
    gh = FakeGitHub({
        ("GET", "/repos/o/r/pulls/5/reviews"): [[], [review(3, HEAD)]],
        ("GET", "/repos/o/r/pulls/5/comments"): [],
    })
    found, _ = fetch_bot_review(gh, "o/r", 5, HEAD, sleep=lambda s: None)
    assert found is not None and found["id"] == 3


def posted(*cs: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"id": c["id"], "path": c["path"], "line": c["line"],
             "title": c["body"].split("\n")[0].replace("**", "")} for c in cs]


def test_every_planted_bug_needs_a_comment_inside_its_line_range() -> None:
    checks = api_checks(review(3, HEAD), posted(comment(1, "shop/orders.py", 21),
                                               comment(2, "web/cart.ts", 13)), EXPECTED)
    assert [(c.name, c.ok) for c in checks] == [
        ("API: review by Reviewhound on this head", True),
        ("API: comment for no role check", True),
        ("API: comment for assignment", False),
        ("API: no comments outside planted bugs", False),
        ("API: every comment has a title", True)]


def test_outdated_untitled_or_missing_review_fails() -> None:
    checks = api_checks(None, posted(comment(1, "shop/orders.py", None),
                                     comment(2, "web/cart.ts", 12, title="")), EXPECTED)
    assert [c.ok for c in checks] == [False, False, True, False, False]


def test_all_bugs_found_and_nothing_else_passes() -> None:
    checks = api_checks(review(3, HEAD), posted(comment(1, "shop/orders.py", 20),
                                               comment(2, "web/cart.ts", 12)), EXPECTED)
    assert all(c.ok for c in checks)


def args(tmp_path: Path) -> argparse.Namespace:
    exp = tmp_path / "expected.json"
    exp.write_text(json.dumps(EXPECTED))
    return argparse.Namespace(milestone="m0", repo="o/r", head_ref="feat", base="main",
                              expected=str(exp), keep_pr=False)


def base_routes(branch: str) -> dict[tuple[str, str], Any]:
    return {("GET", "/repos/o/r/git/ref/heads/feat"): {"object": {"sha": HEAD}},
            ("POST", "/repos/o/r/git/refs"): {},
            ("POST", "/repos/o/r/pulls"): {"number": 9, "html_url": "https://x/pull/9"},
            ("PATCH", "/repos/o/r/pulls/9"): {},
            ("DELETE", f"/repos/o/r/git/refs/heads/{branch}"): None}


def test_pipeline_crash_is_a_fail_and_still_cleans_up_and_writes_result(tmp_path: Path) -> None:
    out = tmp_path / "m0-run"
    out.mkdir()
    gh = FakeGitHub(base_routes("qa/m0-run"))

    def crash(repo: str, number: int, token: str) -> Any:
        raise RuntimeError("model down")
    result = run(args(tmp_path), gh, "t", out, crash)
    assert result.passed is False
    assert [(c.name, c.ok) for c in result.checks] == [("Run finished without errors", False)]
    assert gh.calls[-2:] == [("PATCH", "/repos/o/r/pulls/9"),
                             ("DELETE", "/repos/o/r/git/refs/heads/qa/m0-run")]
    assert json.loads((out / "result.json").read_text())["passed"] is False


def test_pr_creation_failure_still_deletes_the_branch(tmp_path: Path) -> None:
    out = tmp_path / "m0-run"
    out.mkdir()
    routes = base_routes("qa/m0-run")
    routes[("POST", "/repos/o/r/pulls")] = RuntimeError("POST pulls -> HTTP 422")
    gh = FakeGitHub(routes)
    result = run(args(tmp_path), gh, "t", out, lambda *a: pytest.fail("must not review"))
    assert ("PATCH", "/repos/o/r/pulls/9") not in gh.calls
    assert gh.calls[-1] == ("DELETE", "/repos/o/r/git/refs/heads/qa/m0-run")
    assert result.passed is False


def test_failed_close_is_reported_and_branch_delete_still_runs(tmp_path: Path) -> None:
    out = tmp_path / "m0-run"
    out.mkdir()
    routes = base_routes("qa/m0-run")
    routes[("PATCH", "/repos/o/r/pulls/9")] = RuntimeError("PATCH -> HTTP 500")
    gh = FakeGitHub(routes)
    skipped = SimpleNamespace(pr=SimpleNamespace(head_sha=HEAD), outcome="shadow", cost_usd=0.0,
                              error="")
    result = run(args(tmp_path), gh, "t", out, lambda *a: skipped)
    assert [(c.name, c.ok) for c in result.checks] == [
        ("Pipeline: review posted", False), ("Cleanup: QA PR closed", False)]
    assert gh.calls[-1] == ("DELETE", "/repos/o/r/git/refs/heads/qa/m0-run")


def test_missing_browser_login_stops_before_opening_a_pr(tmp_path: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github_qa, "QA_HOME", tmp_path)
    monkeypatch.setattr("sys.argv", ["github_qa", "--milestone", "m0"])
    monkeypatch.setattr(github_qa, "run", lambda *a: pytest.fail("must not run"))
    with pytest.raises(SystemExit) as exc:
        github_qa.main()
    assert "No saved GitHub login" in str(exc.value)


def test_qa_result_passes_only_when_every_check_passes() -> None:
    r = github_qa.QAResult(milestone="m", repo="o/r")
    assert r.passed is False
    r.checks = [github_qa.Check("a", True), github_qa.Check("b", True)]
    assert r.passed is True
    r.checks.append(github_qa.Check("c", False))
    assert r.passed is False


@pytest.mark.parametrize("content, user", [
    ('{"cookies": [{"name": "logged_in", "value": "yes"}, {"name": "dotcom_user", "value": "me"}]}',
     "me"),
    ('{"cookies": [{"name": "logged_in", "value": "no"}, {"name": "dotcom_user", "value": "me"}]}',
     ""),
    ('{"cookies": [{"name": "logged_in", "value": "yes"}]}', ""),
    ("not json", ""),
])
def test_saved_login_must_be_signed_in(tmp_path: Path, content: str, user: str) -> None:
    f = tmp_path / "state.json"
    f.write_text(content)
    assert github_qa.saved_login_user(f) == user
    assert github_qa.saved_login_user(tmp_path / "missing.json") == ""


def test_a_comment_in_range_about_something_else_does_not_count() -> None:
    expected = [{"bug": "crash on missing order", "file": "shop/orders.py", "lines": [21, 24],
                 "mentions": ["none", "missing order"]}]
    cap = {"id": 1, "path": "shop/orders.py", "line": 22, "title": "[major] Refund cap",
           "body": "Cap ignores the refunded total."}
    crash = {**cap, "id": 2, "body": "ORDERS.get returns None for an unknown id."}
    assert [c.ok for c in api_checks(review(3, HEAD), [cap], expected)][1] is False
    assert [c.ok for c in api_checks(review(3, HEAD), [cap, crash], expected)][1:3] == [True,
                                                                                         True]
