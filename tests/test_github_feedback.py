import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest

from reviewhound.findings import Finding
from reviewhound.github_feedback import DISMISS_MESSAGE
from reviewhound.pipeline import Reviewer
from reviewhound.providers import FakeProvider, ProviderRegistry
from reviewhound.queue import SQLiteQueue
from reviewhound.store import Store
from reviewhound.vcs.base import PostedComment
from reviewhound.vcs.github import GitHubError, GitHubVCS
from reviewhound.vcs.memory import MemoryVCS
from reviewhound.webhook import handle_event
from tests.conftest import answer, make_pr
from tests.test_pipeline import BASE, HEAD, REAL

CFG = {".reviewhound.yml": "posting: true\n"}
FIXED = {"app/admin.py": HEAD["app/admin.py"].replace(
    "    db.delete(uid)", '    require_role(req, "admin")\n    db.delete(uid)')}
MINOR = {"file": "app/admin.py", "line": 12, "severity": "minor", "category": "style",
         "title": "returns a constant", "body": "...", "evidence_quote": "return True"}
FP = Finding(file="app/admin.py", line=11, severity=4, category="auth",  # type: ignore[arg-type]
             title="", body="", evidence_quote="db.delete(uid)", source="").fingerprint


def reviewer_on(tmp_path: Path, answers: list[str]) -> tuple[MemoryVCS, Reviewer, Store]:
    vcs = MemoryVCS(make_pr(head="h1"), {**BASE, **CFG}, {**HEAD, **CFG})
    store = Store(tmp_path / "s.db")
    return vcs, Reviewer(vcs, ProviderRegistry([FakeProvider(answers)]), [], store), store


def push(vcs: MemoryVCS, sha: str, files: dict[str, str]) -> None:
    vcs.pr = make_pr(head=sha)
    vcs.head = {**files, **CFG}
    vcs.head_sha_now = sha


def thread(action: str, *ids: int) -> dict[str, Any]:
    return {"action": action, "repository": {"full_name": "Acme/Shop"},
            "thread": {"node_id": "T1", "comments": [{"id": i} for i in ids]}}


def reply(parent: int | None, body: str = "intended", cid: int = 555,
          app: int | None = None) -> dict[str, Any]:
    comment: dict[str, Any] = {"id": cid, "in_reply_to_id": parent, "body": body,
                               "user": {"login": "dev"}}
    if app is not None:
        comment["performed_via_github_app"] = {"id": app}
    return {"action": "created", "repository": {"full_name": "acme/shop"}, "comment": comment}


def state_of(store: Store) -> str:
    return next(f["state"] for f in store.findings("acme/shop", 7) if f["fingerprint"] == FP)


def test_posted_comments_are_linked_to_findings(tmp_path: Path) -> None:
    _vcs, reviewer, store = reviewer_on(tmp_path, [answer(REAL, MINOR)])
    result = reviewer.review("acme/shop", 7, dry_run=False)
    assert (result.outcome, result.post_id) == ("posted", "memory-review-1")
    links = store.linked_findings("acme/shop", [1001, 1002, 9999])
    minor_fp = next(f.fingerprint for f in result.decision.post if f.line == 12)  # type: ignore
    assert links == {1001: (7, FP), 1002: (7, minor_fp)}


def test_comment_fetch_failure_never_fails_the_review(tmp_path: Path,
                                                      caplog: pytest.LogCaptureFixture) -> None:
    vcs, reviewer, store = reviewer_on(tmp_path, [answer(REAL)])
    vcs.comments_error = GitHubError("boom", 502)
    caplog.set_level(logging.ERROR)
    assert reviewer.review("acme/shop", 7, dry_run=False).outcome == "posted"
    assert store.linked_findings("acme/shop", [1001]) == {}
    assert [(r.msg, r.step, r.error) for r in caplog.records] == [  # type: ignore[attr-defined]
        ("post follow-up failed", "link_comments", "GitHubError")]


def test_thread_resolved_and_unresolved_become_feedback(tmp_path: Path) -> None:
    _vcs, reviewer, store = reviewer_on(tmp_path, [answer(REAL)])
    reviewer.review("acme/shop", 7, dry_run=False)
    q = SQLiteQueue(tmp_path / "q.db")
    assert handle_event(q, "pull_request_review_thread", thread("resolved", 1001, 1777),
                        store) == (202, "recorded 1 resolved")
    assert state_of(store) == "resolved"
    assert handle_event(q, "pull_request_review_thread", thread("unresolved", 1001),
                        store) == (202, "recorded 1 reopened")
    assert state_of(store) == "reopened"
    assert store.feedback("acme/shop", 7) == [
        {"fingerprint": FP, "kind": "resolved", "detail": ""},
        {"fingerprint": FP, "kind": "reopened", "detail": ""}]
    assert q.claim() is None


def test_resolved_finding_is_not_posted_again(tmp_path: Path) -> None:
    vcs, reviewer, store = reviewer_on(tmp_path, [answer(REAL), answer(REAL)])
    reviewer.review("acme/shop", 7, dry_run=False)
    handle_event(SQLiteQueue(tmp_path / "q.db"), "pull_request_review_thread",
                 thread("resolved", 1001), store)
    push(vcs, "h2", HEAD)
    second = reviewer.review("acme/shop", 7, dry_run=False)
    assert second.review is not None and second.review.comments == []
    assert second.decision is not None
    assert [r for _, r in second.decision.held] == ["developer resolved this earlier"]


def test_unknown_comment_ids_are_ignored_and_logged(tmp_path: Path,
                                                    caplog: pytest.LogCaptureFixture) -> None:
    store = Store(tmp_path / "s.db")
    q = SQLiteQueue(tmp_path / "q.db")
    caplog.set_level(logging.INFO, logger="reviewhound.github_feedback")
    assert handle_event(q, "pull_request_review_thread", thread("resolved", 42), store) == (
        202, "recorded 0 resolved")
    assert handle_event(q, "pull_request_review_comment", reply(43), store) == (
        202, "reply to unknown comment ignored")
    assert [(r.msg, getattr(r, "comment_ids", None) or getattr(r, "comment_id", None))
            for r in caplog.records] == [
        ("thread comments not posted by us, ignored", [42]),
        ("reply to a comment not posted by us, ignored", 43)]
    assert store.feedback("acme/shop", 7) == []


def test_reply_to_our_comment_is_recorded_once_and_truncated(tmp_path: Path) -> None:
    _vcs, reviewer, store = reviewer_on(tmp_path, [answer(REAL)])
    reviewer.review("acme/shop", 7, dry_run=False)
    q = SQLiteQueue(tmp_path / "q.db")
    body = "x" * 600
    assert handle_event(q, "pull_request_review_comment", reply(1001, body), store) == (
        202, "recorded reply")
    assert handle_event(q, "pull_request_review_comment", reply(1001, body), store) == (
        202, "reply already recorded")
    assert store.feedback("acme/shop", 7) == [{"fingerprint": FP, "kind": "reply",
                                               "detail": "x" * 500}]
    assert state_of(store) == "posted"


def test_replies_by_the_app_itself_and_non_replies_are_ignored(tmp_path: Path) -> None:
    _vcs, reviewer, store = reviewer_on(tmp_path, [answer(REAL)])
    reviewer.review("acme/shop", 7, dry_run=False)
    q = SQLiteQueue(tmp_path / "q.db")
    assert handle_event(q, "pull_request_review_comment", reply(1001, app=77), store,
                        app_id="77") == (202, "own reply ignored")
    assert handle_event(q, "pull_request_review_comment", reply(None), store, app_id="77") == (
        202, "not a reply")
    edited = {**reply(1001), "action": "edited"}
    assert handle_event(q, "pull_request_review_comment", edited, store) == (
        202, "ignored action edited")
    assert store.feedback("acme/shop", 7) == []
    assert handle_event(q, "pull_request_review_comment", reply(1001, app=78), store,
                        app_id="77") == (202, "recorded reply")


def test_feedback_store_failure_releases_the_event_for_redelivery(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _vcs, reviewer, store = reviewer_on(tmp_path, [answer(REAL)])
    reviewer.review("acme/shop", 7, dry_run=False)
    q = SQLiteQueue(tmp_path / "q.db")

    def broken(*a: Any, **k: Any) -> bool:
        raise RuntimeError("disk full")
    with monkeypatch.context() as m:
        m.setattr(store, "add_feedback", broken)
        with pytest.raises(RuntimeError):
            handle_event(q, "pull_request_review_comment", reply(1001), store)
    assert handle_event(q, "pull_request_review_comment", reply(1001), store) == (
        202, "recorded reply")


def test_malformed_feedback_payloads_get_400(tmp_path: Path) -> None:
    store = Store(tmp_path / "s.db")
    q = SQLiteQueue(tmp_path / "q.db")
    assert handle_event(q, "pull_request_review_thread", {"action": "resolved"}, store) == (
        400, "missing repository")
    bad_thread = {**thread("resolved"), "thread": {"comments": [{"id": "x"}]}}
    assert handle_event(q, "pull_request_review_thread", bad_thread, store) == (
        400, "malformed review thread payload")
    assert handle_event(q, "pull_request_review_comment",
                        {**reply(1), "comment": "nope"}, store) == (
        400, "malformed review comment payload")
    assert handle_event(q, "pull_request_review_thread", thread("resolved", 1)) == (
        202, "ignored event pull_request_review_thread: no store")


# Stale verdict

def test_clean_new_head_dismisses_the_earlier_request_changes(tmp_path: Path) -> None:
    vcs, reviewer, store = reviewer_on(tmp_path, [answer(REAL), answer(), answer()])
    first = reviewer.review("acme/shop", 7, dry_run=False)
    assert first.review is not None and first.review.event == "REQUEST_CHANGES"
    push(vcs, "h2", FIXED)
    second = reviewer.review("acme/shop", 7, dry_run=False)
    assert (second.outcome, second.review.event) == ("posted", "COMMENT")  # type: ignore
    assert vcs.dismissed == [("memory-review-1", DISMISS_MESSAGE)]
    assert store.dismissals("acme/shop", 7) == [{"review_id": "memory-review-1", "ok": 1,
                                                 "error": ""}]
    push(vcs, "h3", FIXED)
    reviewer.review("acme/shop", 7, dry_run=False)
    assert vcs.dismissed == [("memory-review-1", DISMISS_MESSAGE)]


def test_blocker_still_present_keeps_the_verdict(tmp_path: Path) -> None:
    vcs, reviewer, store = reviewer_on(tmp_path, [answer(REAL), answer(REAL)])
    reviewer.review("acme/shop", 7, dry_run=False)
    push(vcs, "h2", HEAD)
    second = reviewer.review("acme/shop", 7, dry_run=False)
    assert (second.outcome, second.review.event) == ("posted", "COMMENT")  # type: ignore
    assert [r for _, r in second.decision.held] == [  # type: ignore[union-attr]
        "already posted on an earlier head"]
    assert vcs.dismissed == []
    assert store.dismissals("acme/shop", 7) == []


def test_resolved_blocker_no_longer_holds_the_verdict(tmp_path: Path) -> None:
    vcs, reviewer, store = reviewer_on(tmp_path, [answer(REAL), answer(REAL)])
    reviewer.review("acme/shop", 7, dry_run=False)
    handle_event(SQLiteQueue(tmp_path / "q.db"), "pull_request_review_thread",
                 thread("resolved", 1001), store)
    push(vcs, "h2", HEAD)
    reviewer.review("acme/shop", 7, dry_run=False)
    assert vcs.dismissed == [("memory-review-1", DISMISS_MESSAGE)]


def test_new_blocking_review_dismisses_nothing(tmp_path: Path) -> None:
    vcs, reviewer, store = reviewer_on(tmp_path, [answer(REAL), answer(REAL)])
    reviewer.review("acme/shop", 7, dry_run=False)
    store.add_feedback("acme/shop", 7, FP, "fixed")  # comes back: posted again as a blocker
    push(vcs, "h2", HEAD)
    second = reviewer.review("acme/shop", 7, dry_run=False)
    assert second.review is not None and second.review.event == "REQUEST_CHANGES"
    assert vcs.dismissed == []


def test_failed_dismissal_is_recorded_and_never_fails_the_review(tmp_path: Path) -> None:
    vcs, reviewer, store = reviewer_on(tmp_path, [answer(REAL), answer(), answer()])
    reviewer.review("acme/shop", 7, dry_run=False)
    vcs.dismiss_error = GitHubError("PUT x -> HTTP 422", 422)
    push(vcs, "h2", FIXED)
    assert reviewer.review("acme/shop", 7, dry_run=False).outcome == "posted"
    assert store.dismissals("acme/shop", 7) == [{
        "review_id": "memory-review-1", "ok": 0, "error": "GitHubError: PUT x -> HTTP 422"}]
    vcs.dismiss_error = None
    push(vcs, "h3", FIXED)
    reviewer.review("acme/shop", 7, dry_run=False)
    assert vcs.dismissed == [("memory-review-1", DISMISS_MESSAGE)]


def test_shadow_and_dry_run_never_dismiss(tmp_path: Path) -> None:
    vcs, reviewer, _store = reviewer_on(tmp_path, [answer(REAL), answer(), answer()])
    reviewer.review("acme/shop", 7, dry_run=False)
    push(vcs, "h2", FIXED)
    assert reviewer.review("acme/shop", 7, dry_run=True).outcome == "dry-run"
    vcs.base = dict(BASE)  # posting off: shadow mode
    assert reviewer.review("acme/shop", 7, dry_run=False).outcome == "shadow"
    assert vcs.dismissed == []


# GitHubVCS endpoints

def test_github_review_comments_and_dismissal_requests(tmp_path: Path) -> None:
    seen: list[tuple[str, str, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, req.url.path, req.url.query.decode() or req.content.decode()))
        if req.method == "PUT":
            return httpx.Response(200, json={})
        page = int(req.url.params["page"])
        items = [{"id": page * 1000 + i, "path": "a.py", "line": i, "original_line": i}
                 for i in range(100 if page == 1 else 2)]
        if page == 2:
            items[1]["line"] = None
        return httpx.Response(200, json=items)
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.github.com")
    vcs = GitHubVCS("t", tmp_path, client=client)
    comments = vcs.review_comments("acme/shop", 7, "88")
    assert len(comments) == 102
    assert comments[-2:] == [PostedComment(2000, "a.py", 0), PostedComment(2001, "a.py", 1)]
    vcs.dismiss_review("acme/shop", 7, "88", "msg")
    assert seen == [
        ("GET", "/repos/acme/shop/pulls/7/reviews/88/comments", "per_page=100&page=1"),
        ("GET", "/repos/acme/shop/pulls/7/reviews/88/comments", "per_page=100&page=2"),
        ("PUT", "/repos/acme/shop/pulls/7/reviews/88/dismissals",
         json.dumps({"message": "msg", "event": "DISMISS"}, separators=(",", ":")))]
