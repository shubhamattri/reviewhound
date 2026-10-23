import json
from pathlib import Path

import httpx

from reviewhound.config import PolicyConfig, Severity
from reviewhound.findings import Finding
from reviewhound.policy import PolicyDecision, apply_policy
from reviewhound.poster import build_review, render_dry_run
from reviewhound.vcs.base import ReviewComment, ReviewPayload
from reviewhound.vcs.github import GitHubVCS
from reviewhound.vcs.memory import diff_file_maps
from tests.conftest import make_pr

BASE = {"h.py": "a = 1\nguard()\nb = 2\n"}
HEAD = {"h.py": "a = 1\nb = 2\nc = 3\n"}


def finding(line: int, side: str = "RIGHT", quote: str = "q") -> Finding:
    return Finding(file="h.py", line=line, severity=Severity.MAJOR, category="auth",
                   title=f"t{line}{side}", body="b", evidence_quote=quote, source="model:m",
                   verified=True, side=side)


def test_left_finding_on_a_deleted_line_becomes_a_left_comment() -> None:
    diff = diff_file_maps(BASE, HEAD)
    decision = PolicyDecision(post=[finding(2, "LEFT", "guard()"), finding(3, quote="c = 3"),
                                    finding(1, "LEFT", "a = 1")])
    review = build_review("h1", decision, diff, "standard", [])
    assert [(c.path, c.line, c.side) for c in review.comments] == [("h.py", 2, "LEFT"),
                                                                   ("h.py", 3, "RIGHT")]
    assert "- `h.py:1` [major] t1LEFT" in review.body.splitlines()
    rendered = json.loads(render_dry_run(review, decision).split("\n", 1)[1])
    assert [(c["line"], c["side"]) for c in rendered["comments"]] == [(2, "LEFT"), (3, "RIGHT")]


def test_left_and_right_findings_on_the_same_number_are_not_duplicates() -> None:
    decision = apply_policy([finding(2, "LEFT", "guard()"), finding(2, quote="b = 2")],
                            PolicyConfig())
    assert [(f.line, f.side) for f in decision.post] == [(2, "LEFT"), (2, "RIGHT")]
    assert decision.held == []


def test_github_post_sends_side_and_defaults_to_right(tmp_path: Path) -> None:
    sent: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"id": 99})
    client = httpx.Client(base_url="https://api.github.com",
                          transport=httpx.MockTransport(handler))
    gh = GitHubVCS("t0ken", tmp_path, client=client)
    review = ReviewPayload(head_sha="h1", event="COMMENT", body="x", comments=[
        ReviewComment(path="h.py", line=2, body="gone", side="LEFT"),
        ReviewComment(path="h.py", line=3, body="new")])
    assert gh.post_review(make_pr(), review) == "99"
    assert sent[0]["comments"] == [
        {"path": "h.py", "line": 2, "side": "LEFT", "body": "gone"},
        {"path": "h.py", "line": 3, "side": "RIGHT", "body": "new"}]
