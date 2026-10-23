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
from reviewhound.store import ReviewRecord, ReviewStore
from reviewhound.vcs.base import PostedComment
from reviewhound.vcs.github import GitHubError, GitHubVCS
from reviewhound.vcs.memory import MemoryVCS
from reviewhound.webhook import handle_event
from tests.conftest import Backend, answer, make_pr
from tests.test_pipeline import BASE, HEAD, REAL

CFG = {".reviewhound.yml": "posting: true\n"}
FIXED = {"app/admin.py": HEAD["app/admin.py"].replace(
    "    db.delete(uid)", '    require_role(req, "admin")\n    db.delete(uid)')}
MINOR = {"file": "app/admin.py", "line": 12, "severity": "minor", "category": "style",
         "title": "returns a constant", "body": "...", "evidence_quote": "return True"}
FP = Finding(file="app/admin.py", line=11, severity=4, category="auth",  # type: ignore[arg-type]
             title="", body="", evidence_quote="db.delete(uid)", source="").fingerprint


def reviewer_on(backend: Backend,
                answers: list[str]) -> tuple[MemoryVCS, Reviewer, ReviewStore]:
    vcs = MemoryVCS(make_pr(head="h1"), {**BASE, **CFG}, {**HEAD, **CFG})
    store = backend.store()
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


def state_of(store: ReviewStore) -> str:
    return next(f["state"] for f in store.findings("acme/shop", 7) if f["fingerprint"] == FP)


def test_posted_comments_are_linked_to_findings(backend: Backend) -> None:
    _vcs, reviewer, store = reviewer_on(backend, [answer(REAL, MINOR)])
    result = reviewer.review("acme/shop", 7, dry_run=False)
    assert (result.outcome, result.post_id) == ("posted", "memory-review-1")
    links = store.linked_findings("acme/shop", [1001, 1002, 9999])
    minor_fp = next(f.fingerprint for f in result.decision.post if f.line == 12)  # type: ignore
    assert links == {1001: (7, FP), 1002: (7, minor_fp)}


def test_comment_fetch_failure_never_fails_the_review(backend: Backend,
                                                      caplog: pytest.LogCaptureFixture) -> None:
    vcs, reviewer, store = reviewer_on(backend, [answer(REAL)])
    vcs.comments_error = GitHubError("boom", 502)
    caplog.set_level(logging.ERROR)
    assert reviewer.review("acme/shop", 7, dry_run=False).outcome == "posted"
    assert store.linked_findings("acme/shop", [1001]) == {}
    assert [(r.msg, r.step, r.error) for r in caplog.records] == [  # type: ignore[attr-defined]
        ("post follow-up failed", "link_comments", "GitHubError")]


def test_thread_resolved_and_unresolved_become_feedback(backend: Backend) -> None:
    _vcs, reviewer, store = reviewer_on(backend, [answer(REAL)])
    reviewer.review("acme/shop", 7, dry_run=False)
    q = backend.queue()
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


def test_resolved_finding_is_not_posted_again(backend: Backend) -> None:
    vcs, reviewer, store = reviewer_on(backend, [answer(REAL), answer(REAL)])
    reviewer.review("acme/shop", 7, dry_run=False)
    handle_event(backend.queue(), "pull_request_review_thread",
                 thread("resolved", 1001), store)
    push(vcs, "h2", HEAD)
    second = reviewer.review("acme/shop", 7, dry_run=False)
    assert second.review is not None and second.review.comments == []
    assert second.decision is not None
    assert [r for _, r in second.decision.held] == ["developer resolved this earlier"]


def test_unknown_comment_ids_are_ignored_and_logged(backend: Backend,
                                                    caplog: pytest.LogCaptureFixture) -> None:
    store = backend.store()
    q = backend.queue()
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


def test_reply_to_our_comment_is_recorded_once_and_truncated(backend: Backend) -> None:
    _vcs, reviewer, store = reviewer_on(backend, [answer(REAL)])
    reviewer.review("acme/shop", 7, dry_run=False)
    q = backend.queue()
    body = "x" * 600
    assert handle_event(q, "pull_request_review_comment", reply(1001, body), store) == (
        202, "recorded reply")
    assert handle_event(q, "pull_request_review_comment", reply(1001, body), store) == (
        202, "reply already recorded")
    assert store.feedback("acme/shop", 7) == [{"fingerprint": FP, "kind": "reply",
                                               "detail": "x" * 500}]
    assert state_of(store) == "posted"


def test_replies_by_the_app_itself_and_non_replies_are_ignored(backend: Backend) -> None:
    _vcs, reviewer, store = reviewer_on(backend, [answer(REAL)])
    reviewer.review("acme/shop", 7, dry_run=False)
    q = backend.queue()
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
        backend: Backend, monkeypatch: pytest.MonkeyPatch) -> None:
    _vcs, reviewer, store = reviewer_on(backend, [answer(REAL)])
    reviewer.review("acme/shop", 7, dry_run=False)
    q = backend.queue()

    def broken(*a: Any, **k: Any) -> bool:
        raise RuntimeError("disk full")
    with monkeypatch.context() as m:
        m.setattr(store, "add_feedback", broken)
        with pytest.raises(RuntimeError):
            handle_event(q, "pull_request_review_comment", reply(1001), store)
    assert handle_event(q, "pull_request_review_comment", reply(1001), store) == (
        202, "recorded reply")


def test_malformed_feedback_payloads_get_400(backend: Backend) -> None:
    store = backend.store()
    q = backend.queue()
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

def test_clean_new_head_dismisses_the_earlier_request_changes(backend: Backend) -> None:
    vcs, reviewer, store = reviewer_on(backend, [answer(REAL), answer(), answer()])
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


def test_blocker_still_present_keeps_the_verdict(backend: Backend) -> None:
    vcs, reviewer, store = reviewer_on(backend, [answer(REAL), answer(REAL)])
    reviewer.review("acme/shop", 7, dry_run=False)
    push(vcs, "h2", HEAD)
    second = reviewer.review("acme/shop", 7, dry_run=False)
    assert (second.outcome, second.review.event) == ("posted", "COMMENT")  # type: ignore
    assert [r for _, r in second.decision.held] == [  # type: ignore[union-attr]
        "already posted on an earlier head"]
    assert vcs.dismissed == []
    assert store.dismissals("acme/shop", 7) == []


def test_resolved_blocker_no_longer_holds_the_verdict(backend: Backend) -> None:
    vcs, reviewer, store = reviewer_on(backend, [answer(REAL), answer(REAL)])
    reviewer.review("acme/shop", 7, dry_run=False)
    handle_event(backend.queue(), "pull_request_review_thread",
                 thread("resolved", 1001), store)
    push(vcs, "h2", HEAD)
    reviewer.review("acme/shop", 7, dry_run=False)
    assert vcs.dismissed == [("memory-review-1", DISMISS_MESSAGE)]


def test_new_blocking_review_dismisses_nothing(backend: Backend) -> None:
    vcs, reviewer, store = reviewer_on(backend, [answer(REAL), answer(REAL)])
    reviewer.review("acme/shop", 7, dry_run=False)
    store.add_feedback("acme/shop", 7, FP, "fixed")  # comes back: posted again as a blocker
    push(vcs, "h2", HEAD)
    second = reviewer.review("acme/shop", 7, dry_run=False)
    assert second.review is not None and second.review.event == "REQUEST_CHANGES"
    assert vcs.dismissed == []


def test_failed_dismissal_is_recorded_and_never_fails_the_review(backend: Backend) -> None:
    vcs, reviewer, store = reviewer_on(backend, [answer(REAL), answer(), answer()])
    reviewer.review("acme/shop", 7, dry_run=False)
    vcs.dismiss_error = GitHubError("PUT x -> HTTP 502", 502)
    push(vcs, "h2", FIXED)
    assert reviewer.review("acme/shop", 7, dry_run=False).outcome == "posted"
    assert store.dismissals("acme/shop", 7) == [{
        "review_id": "memory-review-1", "ok": 0, "error": "GitHubError: PUT x -> HTTP 502"}]
    vcs.dismiss_error = None
    push(vcs, "h3", FIXED)
    reviewer.review("acme/shop", 7, dry_run=False)
    assert vcs.dismissed == [("memory-review-1", DISMISS_MESSAGE)]


def test_shadow_and_dry_run_never_dismiss(backend: Backend) -> None:
    vcs, reviewer, _store = reviewer_on(backend, [answer(REAL), answer(), answer()])
    reviewer.review("acme/shop", 7, dry_run=False)
    push(vcs, "h2", FIXED)
    assert reviewer.review("acme/shop", 7, dry_run=True).outcome == "dry-run"
    vcs.base = dict(BASE)  # posting off: shadow mode
    assert reviewer.review("acme/shop", 7, dry_run=False).outcome == "shadow"
    assert vcs.dismissed == []


# GitHubVCS endpoints

def test_github_review_comments_and_dismissal_requests(tmp_path: Path) -> None:
    """Shaped like real GitHub: only the PR-wide comment list carries `line`."""
    seen: list[tuple[str, str, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, req.url.path, req.url.query.decode() or req.content.decode()))
        if req.method == "PUT":
            return httpx.Response(200, json={})
        page = int(req.url.params["page"])
        items = [{"id": page * 1000 + i, "path": "a.py", "line": i, "original_line": i,
                  "pull_request_review_id": 88 if i % 2 == 0 else 99}
                 for i in range(100 if page == 1 else 3)]
        if page == 2:
            items[2]["line"] = None
            items[2]["side"] = "LEFT"
        return httpx.Response(200, json=items)
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.github.com")
    vcs = GitHubVCS("t", tmp_path, client=client)
    comments = vcs.review_comments("acme/shop", 7, "88")
    assert len(comments) == 52
    assert comments[-2:] == [PostedComment(2000, "a.py", 0),
                             PostedComment(2002, "a.py", 2, "LEFT")]
    vcs.dismiss_review("acme/shop", 7, "88", "msg")
    assert seen == [
        ("GET", "/repos/acme/shop/pulls/7/comments", "per_page=100&page=1"),
        ("GET", "/repos/acme/shop/pulls/7/comments", "per_page=100&page=2"),
        ("PUT", "/repos/acme/shop/pulls/7/reviews/88/dismissals",
         json.dumps({"message": "msg", "event": "DISMISS"}, separators=(",", ":")))]


def test_late_follow_up_never_dismisses_a_newer_review(backend: Backend) -> None:
    store = backend.store()
    for head, event, post_id in [("h1", "REQUEST_CHANGES", "r1"), ("h2", "COMMENT", "r2"),
                                 ("h3", "REQUEST_CHANGES", "r3")]:
        store.record_review(ReviewRecord(repo="acme/shop", pr=7, head_sha=head, outcome="posted",
                                         event=event, posted=True, post_id=post_id), [], [])
    assert store.blocking_reviews_to_dismiss("acme/shop", 7, "r2") == ["r1"]
    assert store.blocking_reviews_to_dismiss("acme/shop", 7, "unknown") == []


def test_redelivered_thread_event_is_applied_once(backend: Backend) -> None:
    _vcs, reviewer, store = reviewer_on(backend, [answer(REAL)])
    reviewer.review("acme/shop", 7, dry_run=False)
    q = backend.queue()
    assert handle_event(q, "pull_request_review_thread", thread("resolved", 1001), store,
                        delivery="d1") == (202, "recorded 1 resolved")
    assert handle_event(q, "pull_request_review_thread", thread("unresolved", 1001), store,
                        delivery="d2") == (202, "recorded 1 reopened")
    assert handle_event(q, "pull_request_review_thread", thread("resolved", 1001), store,
                        delivery="d1") == (202, "event already applied")
    assert state_of(store) == "reopened"
    assert [f["kind"] for f in store.feedback("acme/shop", 7)] == ["resolved", "reopened"]


def test_thread_write_failure_releases_the_delivery(backend: Backend,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    _vcs, reviewer, store = reviewer_on(backend, [answer(REAL)])
    reviewer.review("acme/shop", 7, dry_run=False)
    q = backend.queue()

    def broken(*a: Any, **k: Any) -> bool:
        raise RuntimeError("disk full")
    with monkeypatch.context() as m:
        m.setattr(store, "add_feedback", broken)
        with pytest.raises(RuntimeError):
            handle_event(q, "pull_request_review_thread", thread("resolved", 1001), store,
                         delivery="d1")
    assert handle_event(q, "pull_request_review_thread", thread("resolved", 1001), store,
                        delivery="d1") == (202, "recorded 1 resolved")


def test_permanent_dismissal_failure_is_not_retried(backend: Backend) -> None:
    vcs, reviewer, store = reviewer_on(backend, [answer(REAL), answer(), answer()])
    reviewer.review("acme/shop", 7, dry_run=False)
    vcs.dismiss_error = GitHubError("PUT x -> HTTP 404", 404)
    push(vcs, "h2", FIXED)
    reviewer.review("acme/shop", 7, dry_run=False)
    vcs.dismiss_error = None
    push(vcs, "h3", FIXED)
    reviewer.review("acme/shop", 7, dry_run=False)
    assert vcs.dismissed == []
    assert store.dismissals("acme/shop", 7) == [{
        "review_id": "memory-review-1", "ok": -1, "error": "GitHubError: PUT x -> HTTP 404"}]


def test_comment_links_hold_github_sized_ids_and_stay_per_repo(backend: Backend) -> None:
    store = backend.store()
    big = 2_900_000_001  # GitHub comment ids no longer fit in 32 bits
    store.link_comments("Acme/Shop", 7, "r1", [(big, "fp1"), (5, "fp2")])
    store.link_comments("acme/other", 9, "r9", [(5, "fpX")])
    assert store.linked_findings("acme/shop", [big, 5, 6]) == {big: (7, "fp1"), 5: (7, "fp2")}
    store.link_comments("acme/shop", 8, "r2", [(5, "fp3")])  # re-link replaces the row
    assert store.linked_findings("ACME/SHOP", [5]) == {5: (8, "fp3")}
    assert store.linked_findings("acme/other", [5, big]) == {5: (9, "fpX")}
    assert store.linked_findings("acme/shop", []) == {}
    store.link_comments("acme/shop", 7, "r3", [])


def test_event_keys_are_claimed_once_until_released(backend: Backend) -> None:
    store = backend.store()
    assert store.claim_event("thread:d1") is True
    assert store.claim_event("thread:d1") is False
    assert store.claim_event("thread:d2") is True
    store.release_event("thread:d1")
    assert store.claim_event("thread:d1") is True


def test_dismissal_states_decide_what_is_retried(backend: Backend) -> None:
    store = backend.store()
    for head, post_id in [("h1", "r1"), ("h2", "r2"), ("h3", "r3"), ("h4", "r4")]:
        store.record_review(ReviewRecord(repo="acme/shop", pr=7, head_sha=head, outcome="posted",
                                         event="REQUEST_CHANGES", posted=True, post_id=post_id),
                            [], [])
    store.record_review(ReviewRecord(repo="acme/shop", pr=7, head_sha="h5", outcome="posted",
                                     event="COMMENT", posted=True, post_id="r5"), [], [])
    store.record_dismissal("acme/shop", 7, "r1", ok=True)
    store.record_dismissal("acme/shop", 7, "r2", ok=False, error="x" * 600)
    store.record_dismissal("acme/shop", 7, "r3", ok=False, error="404", permanent=True)
    assert store.blocking_reviews_to_dismiss("acme/shop", 7, "r5") == ["r2", "r4"]
    assert store.blocking_reviews_to_dismiss("acme/shop", 8, "r5") == []
    assert [(d["review_id"], d["ok"], len(d["error"] or "")) for d in
            store.dismissals("acme/shop", 7)] == [("r1", 1, 0), ("r2", 0, 500), ("r3", -1, 3)]


def test_left_and_right_comments_on_the_same_line_number_link_to_their_own_findings(
        backend: Backend) -> None:
    from types import SimpleNamespace

    from reviewhound.config import PolicyConfig
    from reviewhound.github_feedback import link_comments
    from reviewhound.vcs.base import ReviewComment, ReviewPayload
    left = Finding(file="app/admin.py", line=6, severity=4,  # type: ignore[arg-type]
                   category="auth", title="", body="", evidence_quote="require_role(req)",
                   source="", side="LEFT")
    right = Finding(file="app/admin.py", line=6, severity=4,  # type: ignore[arg-type]
                    category="auth", title="", body="", evidence_quote="db.delete(uid)",
                    source="")
    assert left.fingerprint != right.fingerprint
    vcs = MemoryVCS(make_pr(head="h1"), BASE, HEAD)
    vcs.posted.append(ReviewPayload(head_sha="h1", event="COMMENT", body="", comments=[
        ReviewComment(path="app/admin.py", line=6, body="r", side="RIGHT"),
        ReviewComment(path="app/admin.py", line=6, body="l", side="LEFT")]))
    result: Any = SimpleNamespace(pr=make_pr(head="h1"), post_id="memory-review-1",
                                  decision=SimpleNamespace(post=[left, right], held=[]))
    store = backend.store()
    link_comments(vcs, store, result, PolicyConfig())
    assert store.linked_findings("acme/shop", [1001, 1002]) == {
        1001: (7, right.fingerprint), 1002: (7, left.fingerprint)}
