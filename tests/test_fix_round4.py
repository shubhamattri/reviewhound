"""Fix round 4: GitHub decides whether a review landed, 429 is a refusal, recovery marks only what
GitHub shows, removed-guard blockers can be dismissed, beside-deletion findings need a true claim,
and dry runs write nothing."""

from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest

from reviewhound.checker import UNCONFIRMED_BESIDE, ClaimChecker
from reviewhound.config import PolicyConfig
from reviewhound.diff import parse_patch
from reviewhound.findings import ModelFinding
from reviewhound.github_feedback import DISMISS_MESSAGE, open_reason
from reviewhound.index.treesitter import CodeIndex
from reviewhound.pipeline import ADOPTED, CLAIM_BUSY, RECOVERED, PostRefused, Reviewer
from reviewhound.policy import apply_policy
from reviewhound.providers import FakeProvider, ProviderRegistry
from reviewhound.queue import WorkerQueue
from reviewhound.store import Blocker, ReviewRecord, ReviewStore
from reviewhound.vcs.base import (
    DictSnapshot,
    PostedComment,
    PostedReview,
    PullRequest,
    ReviewComment,
    ReviewPayload,
)
from reviewhound.vcs.github import GitHubError, GitHubVCS
from reviewhound.vcs.github_app import GitHubApp
from reviewhound.vcs.memory import MemoryVCS, diff_file_maps
from reviewhound.worker import is_retryable, run_one
from tests.conftest import Backend, FakeClock, answer, make_pr
from tests.test_fix_round1_b import REJECTED, SECOND, scripted_posts
from tests.test_github_app import PEM
from tests.test_github_feedback import CFG, FP
from tests.test_pipeline import BASE, HEAD, REAL

REPO, PR, H1 = "acme/shop", 7, "h1"
STALE = 600.0
SUMMARY = "Reviewhound (standard review). Reviewers that ran: model:x."


def rig(backend: Backend, answers: list[str], clock: FakeClock | None = None
        ) -> tuple[MemoryVCS, Reviewer, ReviewStore]:
    vcs = MemoryVCS(make_pr(head=H1), {**BASE, **CFG}, {**HEAD, **CFG})
    store = backend.store(clock=clock) if clock is not None else backend.store()
    return vcs, Reviewer(vcs, ProviderRegistry([FakeProvider(answers)]), [], store,
                         stale_claim_s=STALE), store


def again(vcs: MemoryVCS, store: ReviewStore, answers: list[str]) -> Reviewer:
    """A later run (another worker, or the retry) on the same VCS and store."""
    return Reviewer(vcs, ProviderRegistry([FakeProvider(answers)]), [], store, stale_claim_s=STALE)


def landed(comments: list[ReviewComment], event: str = "REQUEST_CHANGES",
           body: str = SUMMARY, head: str = H1) -> ReviewPayload:
    return ReviewPayload(head_sha=head, event=event, body=body, comments=comments)


def at_line_11() -> ReviewComment:
    return ReviewComment(path="app/admin.py", line=11, body="x")


def rows(store: ReviewStore) -> list[tuple[Any, ...]]:
    return [(r["outcome"], r["post_id"], r["event"], r["error"]) for r in store.reviews(REPO, PR)]


def states(store: ReviewStore) -> dict[int, str]:
    return {f["line"]: f["state"] for f in store.findings(REPO, PR)}


def crash_after_the_post(vcs: MemoryVCS, monkeypatch: pytest.MonkeyPatch) -> None:
    """The POST lands, then the process dies before mark_posted."""
    real = vcs.post_review

    def post(pr: PullRequest, review: ReviewPayload) -> str:
        real(pr, review)
        raise KeyboardInterrupt
    monkeypatch.setattr(vcs, "post_review", post)


# --- N1: GitHub is the source of truth for "did it land" -----------------------------------

def test_crash_after_the_post_is_adopted_by_the_next_run_not_posted_again(
        backend: Backend, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakeClock()
    vcs, reviewer, store = rig(backend, [answer(REAL)], clock)
    crash_after_the_post(vcs, monkeypatch)
    with pytest.raises(KeyboardInterrupt):
        reviewer.review(REPO, PR, dry_run=False, expected_head=H1)
    assert store.post_claim(REPO, PR, H1) == ("", 0.0)  # held, and no id was ever recorded
    monkeypatch.undo()
    clock.now += 60
    result = again(vcs, store, [answer(REAL)]).review(REPO, PR, dry_run=False, expected_head=H1)
    assert (result.outcome, result.post_id, result.error) == ("posted", "memory-review-1", ADOPTED)
    assert len(vcs.posted) == 1
    assert store.post_claim(REPO, PR, H1) == ("memory-review-1", 60.0)
    assert rows(store) == [("posted", "memory-review-1", "REQUEST_CHANGES", ADOPTED)]
    assert states(store) == {11: "posted"}
    assert store.linked_findings(REPO, [1001]) == {1001: (PR, FP)}
    assert store.review_blockers(REPO, PR, "memory-review-1")[0]["fingerprint"] == FP


def test_review_already_on_github_is_adopted_before_the_first_post(backend: Backend) -> None:
    vcs, reviewer, store = rig(backend, [answer(REAL)])
    vcs.posted.append(landed([at_line_11()], event="COMMENT"))  # the store lost its claim row
    result = reviewer.review(REPO, PR, dry_run=False)
    assert (result.outcome, result.post_id, result.error) == ("posted", "memory-review-1", ADOPTED)
    assert len(vcs.posted) == 1
    assert store.post_claim(REPO, PR, H1) is not None
    assert store.post_claim(REPO, PR, H1)[0] == "memory-review-1"  # type: ignore[index]
    assert rows(store) == [("posted", "memory-review-1", "COMMENT", ADOPTED)]  # GitHub's verdict


def test_review_landing_during_the_body_retry_is_adopted_not_posted_again(
        backend: Backend, monkeypatch: pytest.MonkeyPatch) -> None:
    vcs, reviewer, store = rig(backend, [answer(REAL)])
    calls: list[ReviewPayload] = []

    def refuse_while_another_lands(pr: PullRequest, review: ReviewPayload) -> str:
        calls.append(review)
        vcs.posted.append(landed([at_line_11()]))
        raise REJECTED
    monkeypatch.setattr(vcs, "post_review", refuse_while_another_lands)
    result = reviewer.review(REPO, PR, dry_run=False)
    assert (result.outcome, result.post_id, len(calls)) == ("posted", "memory-review-1", 1)
    assert store.post_claim(REPO, PR, H1)[0] == "memory-review-1"  # type: ignore[index]


@pytest.mark.parametrize("review, adopted", [
    (PostedReview("r9", "reviewhound-bot", H1, SUMMARY, "COMMENTED"), True),
    (PostedReview("r9", "someone-else", H1, SUMMARY, "COMMENTED"), False),
    (PostedReview("r9", "reviewhound-bot", "h0", SUMMARY, "COMMENTED"), False),
    (PostedReview("r9", "reviewhound-bot", H1, "LGTM. " + SUMMARY, "COMMENTED"), False),
])
def test_only_this_accounts_review_of_this_head_with_our_summary_counts(
        backend: Backend, monkeypatch: pytest.MonkeyPatch, review: PostedReview,
        adopted: bool) -> None:
    vcs, reviewer, _store = rig(backend, [answer(REAL)])
    monkeypatch.setattr(vcs, "list_reviews", lambda repo, number: [review])
    monkeypatch.setattr(vcs, "review_comments", lambda repo, number, rid: [])
    result = reviewer.review(REPO, PR, dry_run=False)
    assert (result.post_id, len(vcs.posted)) == (("r9", 0) if adopted else ("memory-review-1", 1))


def test_an_unnamed_account_adopts_nothing_so_a_lookalike_review_cannot_silence_it(
        backend: Backend, monkeypatch: pytest.MonkeyPatch) -> None:
    vcs, reviewer, _store = rig(backend, [answer(REAL)])
    vcs.viewer = ""
    monkeypatch.setattr(vcs, "list_reviews", lambda repo, number: [
        PostedReview("r9", "pr-author", H1, SUMMARY, "COMMENTED")])
    monkeypatch.setattr(vcs, "review_comments", lambda repo, number, rid: [])
    result = reviewer.review(REPO, PR, dry_run=False)
    assert result.outcome == "posted"
    assert result.post_id != "r9"
    assert len(vcs.posted) == 1


def test_remote_check_failure_frees_the_claim_posts_nothing_and_reraises(
        backend: Backend, monkeypatch: pytest.MonkeyPatch) -> None:
    vcs, reviewer, store = rig(backend, [answer(REAL)])

    def down(repo: str, number: int) -> list[PostedReview]:
        raise GitHubError("GET /reviews -> HTTP 502", 502)
    monkeypatch.setattr(vcs, "list_reviews", down)
    with pytest.raises(GitHubError, match="HTTP 502"):
        reviewer.review(REPO, PR, dry_run=False)
    assert (vcs.posted, store.reviews(REPO, PR)) == ([], [])
    assert store.post_claim(REPO, PR, H1) is None


def test_remote_check_runs_before_every_post_and_the_lease_is_read_after_it(
        backend: Backend, monkeypatch: pytest.MonkeyPatch) -> None:
    vcs, reviewer, _store = rig(backend, [answer(REAL)])
    order: list[str] = []
    real_list = vcs.list_reviews
    monkeypatch.setattr(vcs, "list_reviews",
                        lambda repo, n: order.append("list") or real_list(repo, n))  # type: ignore[func-returns-value]
    scripted = scripted_posts(vcs, monkeypatch, REJECTED, None)
    lease = lambda: order.append("lease") or True  # type: ignore[func-returns-value]
    real_post = vcs.post_review
    monkeypatch.setattr(vcs, "post_review",
                        lambda pr, r: order.append("POST") or real_post(pr, r))  # type: ignore[func-returns-value]
    assert reviewer.review(REPO, PR, dry_run=False, should_post=lease).outcome == "posted"
    assert len(scripted) == 2
    assert order == ["lease", "list", "lease", "POST", "list", "lease", "POST"]


# --- N1: stale claims ------------------------------------------------------------------------

def test_stale_claim_without_an_id_is_taken_over_and_posted(backend: Backend) -> None:
    clock = FakeClock()
    vcs, reviewer, store = rig(backend, [answer(REAL)], clock)
    assert store.claim_post(REPO, PR, H1) is True  # a run that died before its POST
    clock.now += STALE + 1
    result = reviewer.review(REPO, PR, dry_run=False)
    assert (result.outcome, result.post_id, result.error) == ("posted", "memory-review-1", "")
    assert len(vcs.posted) == 1
    assert store.post_claim(REPO, PR, H1) == ("memory-review-1", 0.0)


def test_claim_exactly_one_lease_old_is_not_stale(backend: Backend) -> None:
    clock = FakeClock()
    vcs, reviewer, store = rig(backend, [answer(REAL)], clock)
    store.claim_post(REPO, PR, H1)
    clock.now += STALE
    result = reviewer.review(REPO, PR, dry_run=False)
    assert (result.outcome, result.retry_after_s, vcs.posted) == ("claim-busy", 0.0, [])


def test_stale_claim_whose_review_is_on_github_is_adopted_not_posted(
        backend: Backend, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakeClock()
    vcs, reviewer, store = rig(backend, [answer(REAL)], clock)
    crash_after_the_post(vcs, monkeypatch)
    with pytest.raises(KeyboardInterrupt):
        reviewer.review(REPO, PR, dry_run=False)
    monkeypatch.undo()
    clock.now += STALE + 1
    result = again(vcs, store, [answer(REAL)]).review(REPO, PR, dry_run=False)
    assert (result.outcome, result.post_id, result.error) == ("posted", "memory-review-1", ADOPTED)
    assert len(vcs.posted) == 1


def test_takeover_that_fails_before_the_post_frees_the_claim(
        backend: Backend, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakeClock()
    vcs, reviewer, store = rig(backend, [answer(REAL)], clock)
    store.claim_post(REPO, PR, H1)
    clock.now += STALE + 1

    def head(repo: str, number: int) -> str:
        raise GitHubError("GET /pulls/7 -> HTTP 502", 502)
    monkeypatch.setattr(vcs, "current_head", head)
    with pytest.raises(GitHubError):
        reviewer.review(REPO, PR, dry_run=False)
    assert (vcs.posted, store.post_claim(REPO, PR, H1)) == ([], None)


def test_fresh_claim_with_nothing_on_github_is_busy_and_posts_nothing(backend: Backend) -> None:
    clock = FakeClock()
    vcs, reviewer, store = rig(backend, [answer(REAL)], clock)
    store.claim_post(REPO, PR, H1)
    clock.now += 100
    result = reviewer.review(REPO, PR, dry_run=False)
    assert (result.outcome, result.error, result.retry_after_s) == (
        "claim-busy", CLAIM_BUSY, STALE - 100)
    assert vcs.posted == []
    assert store.post_claim(REPO, PR, H1) == ("", 100.0)  # still the other run's
    assert rows(store) == [("claim-busy", "", "REQUEST_CHANGES", CLAIM_BUSY)]
    assert states(store) == {11: "stored"}


def test_fresh_claim_whose_review_is_on_github_is_adopted(
        backend: Backend, monkeypatch: pytest.MonkeyPatch) -> None:
    vcs, reviewer, store = rig(backend, [answer(REAL)])
    store.claim_post(REPO, PR, H1)
    vcs.posted.append(landed([at_line_11()]))
    result = reviewer.review(REPO, PR, dry_run=False)
    assert (result.outcome, result.post_id, result.error) == ("posted", "memory-review-1", ADOPTED)
    assert store.post_claim(REPO, PR, H1)[0] == "memory-review-1"  # type: ignore[index]
    assert len(vcs.posted) == 1


def test_worker_retries_a_busy_claim_once_it_can_be_taken_over(backend: Backend) -> None:
    clock = FakeClock()
    vcs, reviewer, store = rig(backend, [answer(REAL), answer(REAL)], clock)
    q: WorkerQueue = backend.queue(clock=clock, backoff_base_s=1)
    q.enqueue(REPO, PR, H1)
    store.claim_post(REPO, PR, H1)
    clock.now += 100
    job = run_one(q, reviewer)
    assert job is not None
    after = q.get(job.id)
    assert after is not None and (after.state, after.last_error) == ("queued", f"claim-busy: {CLAIM_BUSY}")
    clock.now += STALE - 100 - 1
    assert q.claim() is None  # not before the claim can go stale
    clock.now += 2
    assert run_one(q, reviewer) is not None
    done = q.get(job.id)
    assert done is not None and done.state == "done"
    assert len(vcs.posted) == 1


@pytest.mark.parametrize("delay, backoff, wait", [(500.0, 30.0, 500.0), (5.0, 30.0, 30.0)])
def test_fail_waits_the_longer_of_backoff_and_delay(backend: Backend, delay: float,
                                                    backoff: float, wait: float) -> None:
    clock = FakeClock()
    q = backend.queue(clock=clock, backoff_base_s=backoff)
    q.enqueue(REPO, PR, H1)
    job = q.claim()
    assert job is not None
    assert q.fail(job, "busy", retryable=True, delay_s=delay) == "queued"
    clock.now += wait - 1
    assert q.claim() is None
    clock.now += 1
    assert q.claim() is not None


def test_stale_claim_store_contract(backend: Backend) -> None:
    clock = FakeClock()
    store = backend.store(clock=clock)
    assert store.post_claim(REPO, PR, H1) is None
    assert store.take_stale_claim(REPO, PR, H1, STALE) is False  # nothing to take
    store.claim_post(REPO, PR, H1)
    clock.now += STALE
    assert store.take_stale_claim(REPO, PR, H1, STALE) is False  # not older than the lease
    clock.now += 1
    assert store.post_claim("ACME/shop", PR, H1) == ("", STALE + 1)
    assert store.take_stale_claim(REPO, PR, H1, STALE) is True
    assert store.take_stale_claim(REPO, PR, H1, 0.0) is False  # the claim is fresh again
    assert store.post_claim(REPO, PR, H1) == ("", 0.0)
    clock.now += STALE + 1
    store.mark_posted(REPO, PR, H1, "r1", "COMMENT")
    assert store.take_stale_claim(REPO, PR, H1, STALE) is False  # a landed post is never taken
    assert store.post_claim(REPO, PR, H1) == ("r1", STALE + 1)


def test_two_takers_of_one_stale_claim_only_one_wins(backend: Backend) -> None:
    clock = FakeClock()
    first, second = backend.store(clock=clock), backend.store(clock=clock)
    first.claim_post(REPO, PR, H1)
    clock.now += STALE + 1
    assert [first.take_stale_claim(REPO, PR, H1, STALE),
            second.take_stale_claim(REPO, PR, H1, STALE)] == [True, False]


# --- N1b: 429 on the POST is a refusal ---------------------------------------------------------

RATE_LIMITED = GitHubError("POST /x -> HTTP 429: rate limited", 429)


def test_429_on_the_post_frees_the_claim_and_raises_retryable(
        backend: Backend, monkeypatch: pytest.MonkeyPatch) -> None:
    vcs, reviewer, store = rig(backend, [answer(REAL)])
    scripted_posts(vcs, monkeypatch, RATE_LIMITED)
    with pytest.raises(PostRefused) as raised:
        reviewer.review(REPO, PR, dry_run=False)
    assert (raised.value.status, is_retryable(raised.value)) == (429, True)
    assert store.post_claim(REPO, PR, H1) is None
    assert store.reviews(REPO, PR) == []  # never post-uncertain


def test_429_on_the_body_retry_frees_the_claim_and_raises(
        backend: Backend, monkeypatch: pytest.MonkeyPatch) -> None:
    vcs, reviewer, store = rig(backend, [answer(REAL)])
    calls = scripted_posts(vcs, monkeypatch, REJECTED, RATE_LIMITED)
    with pytest.raises(PostRefused):
        reviewer.review(REPO, PR, dry_run=False)
    assert (len(calls), store.post_claim(REPO, PR, H1)) == (2, None)


def test_worker_requeues_a_429_and_the_retry_posts(backend: Backend,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    vcs, reviewer, store = rig(backend, [answer(REAL), answer(REAL)])
    scripted_posts(vcs, monkeypatch, RATE_LIMITED, None)
    q = backend.queue(backoff_base_s=0)
    q.enqueue(REPO, PR, H1)
    job = run_one(q, reviewer)
    assert job is not None
    after = q.get(job.id)
    assert after is not None and (after.state, after.last_error) == (
        "queued", "PostRefused: POST /x -> HTTP 429: rate limited")
    run_one(q, reviewer)
    done = q.get(job.id)
    assert done is not None and done.state == "done"
    assert [r["outcome"] for r in store.reviews(REPO, PR)] == ["posted"]


# --- N2: only findings GitHub shows are marked posted ------------------------------------------

def _recorded_post_then_crash(backend: Backend, monkeypatch: pytest.MonkeyPatch
                              ) -> tuple[MemoryVCS, ReviewStore]:
    """The post lands with two comments and its id is saved; the review record fails."""
    vcs, reviewer, store = rig(backend, [answer(REAL, SECOND)])
    real = store.record_review

    def fail(*a: Any, **k: Any) -> int:
        raise RuntimeError("db down")
    monkeypatch.setattr(store, "record_review", fail)
    with pytest.raises(Exception, match="posted but not recorded"):
        reviewer.review(REPO, PR, dry_run=False, expected_head=H1)
    monkeypatch.setattr(store, "record_review", real)
    assert [len(p.comments) for p in vcs.posted] == [2]
    return vcs, store


def test_recovery_marks_posted_only_the_comments_github_shows(
        backend: Backend, monkeypatch: pytest.MonkeyPatch) -> None:
    vcs, store = _recorded_post_then_crash(backend, monkeypatch)
    monkeypatch.setattr(vcs, "review_comments", lambda repo, n, rid: [
        PostedComment(id=4242, path="app/admin.py", line=12)])
    result = again(vcs, store, [answer(REAL, SECOND)]).review(REPO, PR, dry_run=False,
                                                              expected_head=H1)
    assert (result.outcome, result.error) == ("recovered", RECOVERED)
    assert states(store) == {11: "stored", 12: "posted"}
    assert store.linked_findings(REPO, [4242, 1001, 1002]) == {
        4242: (PR, store.findings(REPO, PR)[1]["fingerprint"])}


def test_recovery_that_cannot_read_back_marks_nothing_posted(
        backend: Backend, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture) -> None:
    vcs, store = _recorded_post_then_crash(backend, monkeypatch)
    vcs.comments_error = GitHubError("GET /comments -> HTTP 502", 502)
    caplog.set_level(logging.ERROR, logger="reviewhound.pipeline")
    result = again(vcs, store, [answer(REAL, SECOND)]).review(REPO, PR, dry_run=False,
                                                              expected_head=H1)
    assert result.outcome == "recovered"
    assert states(store) == {11: "stored", 12: "stored"}
    assert store.linked_findings(REPO, [1001, 1002]) == {}
    assert [r.msg for r in caplog.records] == [
        "could not read back the landed review; findings stored, not linked"]


def test_adopted_review_without_inline_comments_marks_nothing_posted(backend: Backend) -> None:
    vcs, reviewer, store = rig(backend, [answer(REAL, SECOND)])
    vcs.posted.append(landed([]))  # its comments went into the body
    result = reviewer.review(REPO, PR, dry_run=False)
    assert (result.outcome, result.error) == ("posted", ADOPTED)
    assert states(store) == {11: "stored", 12: "stored"}
    assert store.posted_findings(REPO, PR, "memory-review-1") == []


def test_a_review_this_run_posted_itself_marks_its_findings_posted(backend: Backend) -> None:
    _vcs, reviewer, store = rig(backend, [answer(REAL, SECOND)])
    result = reviewer.review(REPO, PR, dry_run=False)
    assert (result.outcome, result.read_back) == ("posted", None)
    assert states(store) == {11: "posted", 12: "posted"}


# --- N3: a removed-guard blocker is dismissed once the deleted code is back --------------------

GUARD_BASE = ("from app.auth import require_role\n\n\ndef cancel_order(request, order_id):\n"
              '    require_role(request, "support")\n    orders.cancel(order_id)\n'
              '    return {"ok": True}\n')
GUARD_HEAD = GUARD_BASE.replace('    require_role(request, "support")\n', "")
GUARD = 'require_role(request, "support")'


def test_runs_are_split_by_added_lines_and_keep_their_blank_lines() -> None:
    fd = parse_patch("a.py", "@@ -1,6 +1,4 @@\n keep()\n-gone_one()\n-\n-gone_two()\n+new()\n"
                     "-gone_three()\n tail()\n")
    assert fd.run_text(at=2) == "gone_one()\n\ngone_two()"
    assert fd.run_text(at=3) == "gone_three()"
    assert fd.run_text(base_line=4) == "gone_one()\n\ngone_two()"
    assert fd.run_text(base_line=5) == "gone_three()"
    assert fd.run_text(at=9) == ""


def guard_checker() -> ClaimChecker:
    base, head = {"app/orders.py": GUARD_BASE}, {"app/orders.py": GUARD_HEAD}
    return ClaimChecker(diff_file_maps(base, head), CodeIndex.build(head), base_reader=base.get)


def mfind(**kw: Any) -> ModelFinding:
    base: dict[str, Any] = {"file": "app/orders.py", "line": 5, "severity": "blocker",
                            "category": "auth", "title": "role check removed", "body": "b",
                            "evidence_quote": "orders.cancel(order_id)", "claims": []}
    base.update(kw)
    return ModelFinding.model_validate(base)


NOT_CALLS = {"kind": "not_calls", "caller": "cancel_order", "callee": "require_role"}


def test_left_and_beside_findings_carry_the_deleted_run_and_others_do_not() -> None:
    c = guard_checker()
    left, _ = c.check(mfind(evidence_quote=GUARD, claims=[NOT_CALLS]), "m")
    beside, _ = c.check(mfind(claims=[NOT_CALLS]), "m")
    assert (left.side, left.deleted_text) == ("LEFT", '    require_role(request, "support")')
    assert (beside.side, beside.deleted_text) == ("RIGHT", '    require_role(request, "support")')
    base = {"a.py": "def f(x):\n    guard_call(x)\n    return x\n"}
    head = {"a.py": "def f(x):\n    return x + 1\n"}
    plain, reason = ClaimChecker(diff_file_maps(base, head), CodeIndex.build(head)).check(
        ModelFinding.model_validate({"file": "a.py", "line": 2, "severity": "major", "title": "t",
                                     "body": "b", "evidence_quote": "return x + 1"}), "m")
    assert (reason, plain.side, plain.deleted_text) == (None, "RIGHT", "")


def test_blocker_deleted_text_is_stored_and_read_back(backend: Backend) -> None:
    store = backend.store()
    b = Blocker("fp1", "a.py", "RIGHT", "auth", "do_it(x)", "", "guard(x)", 2)
    store.mark_posted(REPO, PR, H1, "r1", "REQUEST_CHANGES", [b])
    store.record_review(ReviewRecord(repo=REPO, pr=PR, head_sha="h2", outcome="posted",
                                     post_id="r2", posted=True), [], [], [b])
    want = {"fingerprint": "fp1", "file": "a.py", "side": "RIGHT", "category": "auth",
            "evidence_quote": "do_it(x)", "identity": "", "deleted_text": "guard(x)",
            "deleted_kept": 2}
    assert store.review_blockers(REPO, PR, "r1") == [want]
    assert store.review_blockers(REPO, PR, "r2") == [want]


def test_old_sqlite_review_blockers_table_gets_the_deleted_text_column(tmp_path: Any) -> None:
    import sqlite3

    from reviewhound.store import Store
    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE review_blockers (repo TEXT NOT NULL, pr INTEGER NOT NULL, "
              "review_id TEXT NOT NULL, fingerprint TEXT NOT NULL, file TEXT NOT NULL, "
              "side TEXT NOT NULL, category TEXT NOT NULL, evidence_quote TEXT NOT NULL, "
              "identity TEXT NOT NULL, PRIMARY KEY (repo, pr, review_id, fingerprint))")
    c.execute("INSERT INTO review_blockers VALUES ('acme/shop', 7, 'r0', 'fp0', 'a.py', 'RIGHT', "
              "'auth', 'q', '')")
    c.commit()
    c.close()
    store = Store(path)
    old = store.review_blockers(REPO, PR, "r0")[0]
    assert (old["deleted_text"], old["deleted_kept"]) == ("", 0)
    store.mark_posted(REPO, PR, H1, "r1", "REQUEST_CHANGES",
                      [Blocker("fp1", "a.py", "LEFT", "auth", "q", "", "guard(x)", 1)])
    new = store.review_blockers(REPO, PR, "r1")[0]
    assert (new["deleted_text"], new["deleted_kept"]) == ("guard(x)", 1)


def test_old_postgres_review_blockers_table_gets_the_deleted_text_column(backend: Backend) -> None:
    if backend.url is None:
        pytest.skip("Postgres only")
    import psycopg
    with psycopg.connect(backend.url, autocommit=True) as c:
        c.execute("CREATE TABLE review_blockers (repo TEXT NOT NULL, pr INTEGER NOT NULL, "
                  "review_id TEXT NOT NULL, fingerprint TEXT NOT NULL, file TEXT NOT NULL, "
                  "side TEXT NOT NULL, category TEXT NOT NULL, evidence_quote TEXT NOT NULL, "
                  "identity TEXT NOT NULL, PRIMARY KEY (repo, pr, review_id, fingerprint))")
        c.execute("INSERT INTO review_blockers VALUES ('acme/shop', 7, 'r0', 'fp0', 'a.py', "
                  "'RIGHT', 'auth', 'q', '')")
    store = backend.store()
    old = store.review_blockers(REPO, PR, "r0")[0]
    assert (old["deleted_text"], old["deleted_kept"]) == ("", 0)
    store.mark_posted(REPO, PR, H1, "r1", "REQUEST_CHANGES",
                      [Blocker("fp1", "a.py", "LEFT", "auth", "q", "", "guard(x)", 1)])
    new = store.review_blockers(REPO, PR, "r1")[0]
    assert (new["deleted_text"], new["deleted_kept"]) == ("guard(x)", 1)


def removed_guard_row(side: str = "RIGHT", deleted: str = '    require_role(request, "support")'
                      ) -> dict[str, str]:
    return {"file": "app/orders.py", "side": side, "category": "auth",
            "evidence_quote": "orders.cancel(order_id)", "identity": "", "deleted_text": deleted}


RESTORED_SPACED = GUARD_HEAD.replace("def cancel_order(request, order_id):\n",
                                     "def cancel_order(request, order_id):\n"
                                     '        require_role( request,  "support" )\n')


@pytest.mark.parametrize("side", ["RIGHT", "LEFT"])
@pytest.mark.parametrize("files, reason", [
    ({"app/orders.py": GUARD_BASE}, None),
    ({"app/orders.py": RESTORED_SPACED.replace("( request,  ", "(request, ")
      .replace(' )', ')')}, None),
    ({"app/orders.py": GUARD_HEAD},
     "the code this PR deleted is still missing from app/orders.py"),
    ({"app/orders.py": GUARD_HEAD, "app/other.py": GUARD_BASE},
     "the code this PR deleted is still missing from app/orders.py"),
    ({"app/other.py": GUARD_BASE}, "app/orders.py is not in the head"),
])
def test_removed_guard_blocker_is_gone_only_once_the_deleted_code_is_back(
        side: str, files: dict[str, str], reason: str | None) -> None:
    row = removed_guard_row(side)
    assert open_reason(row, DictSnapshot(files), list(files), files) == reason


def test_restoring_with_other_whitespace_counts_as_back() -> None:
    files = {"app/orders.py": GUARD_HEAD.replace(
        "    orders.cancel", '    require_role(request,   "support")\n    orders.cancel')}
    assert open_reason(removed_guard_row(), DictSnapshot(files), [], files) is None


def test_deleted_text_too_short_to_look_for_keeps_the_verdict() -> None:
    files = {"app/orders.py": GUARD_BASE}
    assert open_reason(removed_guard_row(deleted="  }\n"), DictSnapshot(files), [], files) == \
        "the deleted code is too short to look for"


def test_ordinary_blocker_keeps_the_quote_rule() -> None:
    files = {"app/orders.py": GUARD_BASE}
    assert open_reason(removed_guard_row(deleted=""), DictSnapshot(files), [], files) == \
        "evidence quote is still in app/orders.py"
    assert open_reason(removed_guard_row("LEFT", ""), DictSnapshot(files), [], files) == \
        "a deleted-line finding cannot be shown gone"


def _guard_pr(backend: Backend, finding: dict[str, Any]) -> tuple[MemoryVCS, ReviewStore, Any]:
    base = {"app/orders.py": GUARD_BASE, **CFG}
    vcs = MemoryVCS(make_pr(head=H1), base, {"app/orders.py": GUARD_HEAD, **CFG})
    store = backend.store()
    provider = FakeProvider([answer(finding), answer()])
    reviewer = Reviewer(vcs, ProviderRegistry([provider]), [], store)
    return vcs, store, reviewer


@pytest.mark.parametrize("quote", ["orders.cancel(order_id)", GUARD])
def test_removed_guard_verdict_is_dismissed_when_the_guard_comes_back(
        backend: Backend, quote: str) -> None:
    finding = {"file": "app/orders.py", "line": 5, "severity": "blocker", "category": "auth",
               "title": "role check removed", "body": "b", "evidence_quote": quote,
               "claims": [NOT_CALLS]}
    vcs, store, reviewer = _guard_pr(backend, finding)
    first = reviewer.review(REPO, PR, dry_run=False)
    assert (first.outcome, vcs.posted[0].event) == ("posted", "REQUEST_CHANGES")
    assert store.review_blockers(REPO, PR, "memory-review-1")[0]["deleted_text"] == \
        '    require_role(request, "support")'
    vcs.pr = make_pr(head="h2")
    vcs.head = {"app/orders.py": GUARD_BASE + "\n\ndef noop():\n    pass\n", **CFG}
    vcs.head_sha_now = "h2"
    assert reviewer.review(REPO, PR, dry_run=False).outcome == "posted"
    assert vcs.dismissed == [("memory-review-1", DISMISS_MESSAGE)]


def test_removed_guard_verdict_is_kept_while_the_guard_is_still_missing(backend: Backend) -> None:
    finding = {"file": "app/orders.py", "line": 5, "severity": "blocker", "category": "auth",
               "title": "role check removed", "body": "b",
               "evidence_quote": "orders.cancel(order_id)", "claims": [NOT_CALLS]}
    vcs, _store, reviewer = _guard_pr(backend, finding)
    reviewer.review(REPO, PR, dry_run=False)
    vcs.pr = make_pr(head="h2")
    vcs.head = {"app/orders.py": GUARD_HEAD.replace("orders.cancel(order_id)",
                                                    "orders.cancel_now(order_id)"), **CFG}
    vcs.head_sha_now = "h2"
    assert reviewer.review(REPO, PR, dry_run=False).outcome == "posted"
    assert vcs.dismissed == []


# --- N4: next to a deletion, verified needs a claim checked true -------------------------------

def test_beside_with_a_true_claim_is_verified() -> None:
    f, reason = guard_checker().check(mfind(claims=[NOT_CALLS]), "m")
    assert (reason, f.side, f.verified, UNCONFIRMED_BESIDE in f.notes) == (None, "RIGHT", True,
                                                                          False)


def test_beside_with_a_false_claim_is_dropped() -> None:
    _f, reason = guard_checker().check(mfind(claims=[
        {"kind": "calls", "caller": "cancel_order", "callee": "require_role"}]), "m")
    assert reason == "false claim: cancel_order never calls require_role"


def test_beside_with_no_claim_is_unverified_and_the_default_policy_holds_it() -> None:
    f, reason = guard_checker().check(mfind(), "m")
    assert (reason, f.line, f.verified) == (None, 5, False)
    assert f.notes == ["anchored on context line 5, next to lines this PR deleted",
                       UNCONFIRMED_BESIDE]
    decision = apply_policy([f], PolicyConfig())
    assert (decision.post, [r for _, r in decision.held]) == ([], ["unverified"])


def test_beside_with_only_an_unknown_claim_is_unverified() -> None:
    f, reason = guard_checker().check(mfind(claims=[{"kind": "symbol_exists"}]), "m")
    assert (reason, f.verified, f.claim_results[0].verdict) == (None, False, "unknown")


def test_beside_with_a_quote_of_the_deleted_guard_is_verified() -> None:
    f, reason = guard_checker().check(mfind(claims=[
        {"kind": "quote", "text": GUARD}]), "m")
    assert (reason, f.verified) == (None, True)
    assert f.claim_results[0].reason == "quote found on the lines this PR deleted"


@pytest.mark.parametrize("claim", [
    {"kind": "quote", "text": "require_admin(request)"},
    {"kind": "quote", "file": "app/auth.py", "text": GUARD},
    {"kind": "quote", "text": "support"},
])
def test_beside_with_a_quote_that_is_not_the_deleted_code_is_dropped(claim: dict[str, str]) -> None:
    _, reason = guard_checker().check(mfind(claims=[claim]), "m")
    assert reason is not None and reason.startswith("false claim:")


def test_quote_of_deleted_code_does_not_hold_for_a_finding_on_an_added_line() -> None:
    base = {"a.py": "def f(x):\n    guard_call(x)\n    return x\n"}
    head = {"a.py": "def f(x):\n    return x + 1\n"}
    c = ClaimChecker(diff_file_maps(base, head), CodeIndex.build(head), base_reader=base.get)
    _, reason = c.check(ModelFinding.model_validate({
        "file": "a.py", "line": 2, "severity": "major", "title": "t", "body": "b",
        "evidence_quote": "return x + 1", "claims": [{"kind": "quote", "text": "guard_call(x)"}]}),
        "m")
    assert reason == "false claim: quoted text not found in a.py"


# --- N5: dry runs write nothing ----------------------------------------------------------------

class NoStore:
    """Any use of the store fails the test."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"dry run touched the store: {name}")


def test_dry_run_never_touches_the_store() -> None:
    vcs = MemoryVCS(make_pr(head=H1), {**BASE, **CFG}, {**HEAD, **CFG})
    reviewer = Reviewer(vcs, ProviderRegistry([FakeProvider([answer(REAL)])]), [],
                        NoStore())  # type: ignore[arg-type]
    result = reviewer.review(REPO, PR, dry_run=True, expected_head=H1)
    assert (result.outcome, vcs.posted) == ("dry-run", [])
    assert result.decision is not None and [f.line for f in result.decision.post] == [11]


@pytest.mark.parametrize("state, expected_head, outcome", [
    ("closed", H1, "skipped"), ("open", "h0", "superseded")])
def test_dry_run_still_checks_the_pr_state_and_head_first(state: str, expected_head: str,
                                                          outcome: str) -> None:
    vcs = MemoryVCS(make_pr(head=H1, state=state), BASE, HEAD)
    reviewer = Reviewer(vcs, ProviderRegistry([FakeProvider([])]), [],
                        NoStore())  # type: ignore[arg-type]
    assert reviewer.review(REPO, PR, dry_run=True, expected_head=expected_head).outcome == outcome


def test_dry_run_of_a_pr_with_nothing_to_review_writes_nothing() -> None:
    vcs = MemoryVCS(make_pr(head=H1), {"old.py": "x = 1\n"}, {})  # only a deletion
    reviewer = Reviewer(vcs, ProviderRegistry([FakeProvider([])]), [],
                        NoStore())  # type: ignore[arg-type]
    assert reviewer.review(REPO, PR, dry_run=True).outcome == "skipped"


def test_dry_run_leaves_an_unrecorded_post_for_the_real_run(backend: Backend) -> None:
    _vcs, reviewer, store = rig(backend, [answer(REAL)])
    store.mark_posted(REPO, PR, H1, "r1", "COMMENT")
    assert reviewer.review(REPO, PR, dry_run=True).outcome == "dry-run"
    assert store.reviews(REPO, PR) == []
    assert store.unrecorded_post(REPO, PR, H1) == ("r1", "COMMENT")


# --- GitHub adapter: listing reviews, the App's own login ---------------------------------------

def _vcs(handler: Any, **kw: Any) -> GitHubVCS:
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.github.com")
    return GitHubVCS("tok", cache_dir=None, client=client, **kw)  # type: ignore[arg-type]


def test_list_reviews_reads_every_page() -> None:
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url.params))
        page = int(req.url.params["page"])
        n = 100 if page == 1 else 1
        return httpx.Response(200, json=[
            {"id": page * 1000 + i, "user": {"login": "bot"}, "commit_id": "h1", "body": "b",
             "state": "COMMENTED"} for i in range(n)])
    got = _vcs(handler).list_reviews("acme/shop", 7)
    assert seen == ["per_page=100&page=1", "per_page=100&page=2"]
    assert (len(got), got[-1]) == (101, PostedReview("2000", "bot", "h1", "b", "COMMENTED"))


def test_list_reviews_tolerates_a_deleted_user_and_an_empty_body() -> None:
    got = _vcs(lambda req: httpx.Response(200, json=[
        {"id": 5, "user": None, "commit_id": None, "body": None, "state": "COMMENTED"}])
    ).list_reviews("acme/shop", 7)
    assert got == [PostedReview("5", "", "", "", "COMMENTED")]


def test_a_login_source_names_the_account_and_its_errors_raise() -> None:
    vcs = _vcs(lambda req: httpx.Response(500), login=lambda: "app[bot]")
    assert vcs.viewer_login() == "app[bot]"

    def broken() -> str:
        raise GitHubError("GET /app -> HTTP 401", 401)
    with pytest.raises(GitHubError):
        _vcs(lambda req: httpx.Response(500), login=broken).viewer_login()


def test_app_bot_login_is_its_slug_looked_up_once() -> None:
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        return httpx.Response(200, json={"slug": "reviewhound-acme"})
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.github.com")
    app = GitHubApp("123", PEM, client=client)
    assert [app.bot_login(), app.bot_login()] == ["reviewhound-acme[bot]"] * 2
    assert calls == ["/app"]


# --- wiring: the worker's stale threshold is its queue lease; the App posts as its bot ----------

def _args(tmp_path: Any) -> Any:
    import argparse
    return argparse.Namespace(home=str(tmp_path), gh_user=None, fake_answer=None,
                              database_url=None, allow_local_proof_repo=[],
                              proof_image="img:1", max_proof_seconds=300.0)


def _job() -> Any:
    from reviewhound.queue import Job
    return Job(1, REPO, PR, H1, "running", 1, 4, None, "t", installation_id=42)


def test_worker_factories_use_the_given_stale_threshold_and_the_app_login(
        tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from reviewhound.cli import _app_reviewer_factory, _worker_reviewer_factory
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
    assert _worker_reviewer_factory(_args(tmp_path), 42.0)(_job()).stale_claim_s == 42.0
    app = GitHubApp("1", PEM)
    monkeypatch.setattr(app, "installation_token", lambda iid: f"ghs_{iid}")
    monkeypatch.setattr(app, "bot_login", lambda: "reviewhound-acme[bot]")
    reviewer = _app_reviewer_factory(_args(tmp_path), app, 43.0)(_job())
    assert (reviewer.stale_claim_s, reviewer.vcs.viewer_login()) == (43.0,
                                                                    "reviewhound-acme[bot]")


def test_worker_command_passes_its_queue_lease_as_the_stale_threshold(
        tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from reviewhound import cli
    for k in ("REVIEWHOUND_DATABASE_URL", "REVIEWHOUND_APP_ID", "REVIEWHOUND_APP_PRIVATE_KEY_PATH"):
        monkeypatch.delenv(k, raising=False)
    seen: list[float] = []

    def factory(args: Any, stale_claim_s: float) -> Any:
        seen.append(stale_claim_s)
        return lambda job: None
    monkeypatch.setattr(cli, "_worker_reviewer_factory", factory)
    assert cli.main(["--home", str(tmp_path), "worker", "--once"]) == 0
    assert seen == [1800.0]


def test_left_finding_quote_claim_of_its_deleted_line_is_still_checked_at_head() -> None:
    _, reason = guard_checker().check(mfind(evidence_quote=GUARD, claims=[
        {"kind": "quote", "text": GUARD}]), "m")
    assert reason == "false claim: quoted text not found in app/orders.py"


@pytest.mark.parametrize("body", [{"name": "x"}, {"slug": ""}, {"slug": 5}, ["slug"]])
def test_app_bot_login_refuses_a_malformed_answer(body: Any) -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=body)),
                          base_url="https://api.github.com")
    with pytest.raises(GitHubError, match="malformed app response"):
        GitHubApp("123", PEM, client=client).bot_login()


# --- review fixes: a second copy of the guard, adopted blockers, uncertain posts, a freed claim --

TWO_GUARDS_BASE = GUARD_BASE + ('\n\ndef refund_order(request, order_id):\n'
                                '    require_role(request, "support")\n'
                                '    orders.refund(order_id)\n')
TWO_GUARDS_HEAD = TWO_GUARDS_BASE.replace('    require_role(request, "support")\n', "", 1)


def test_a_copy_the_file_kept_is_counted_when_the_finding_is_made() -> None:
    base, head = {"app/orders.py": TWO_GUARDS_BASE}, {"app/orders.py": TWO_GUARDS_HEAD}
    c = ClaimChecker(diff_file_maps(base, head), CodeIndex.build(head), base_reader=base.get)
    beside, _ = c.check(mfind(claims=[NOT_CALLS]), "m")
    left, _ = c.check(mfind(evidence_quote=GUARD, claims=[NOT_CALLS]), "m")
    assert (beside.deleted_kept, left.deleted_kept) == (1, 1)
    plain, _ = guard_checker().check(mfind(claims=[NOT_CALLS]), "m")
    assert plain.deleted_kept == 0


@pytest.mark.parametrize("head, reason", [
    (TWO_GUARDS_HEAD, "the code this PR deleted is still missing from app/orders.py"),
    (TWO_GUARDS_BASE, None),
])
def test_a_copy_of_the_guard_elsewhere_in_the_file_is_not_a_restore(
        head: str, reason: str | None) -> None:
    row = {**removed_guard_row(), "deleted_kept": 1}
    files = {"app/orders.py": head}
    assert open_reason(row, DictSnapshot(files), [], files) == reason


def test_verdict_on_one_of_two_guards_is_kept_while_that_guard_is_missing(
        backend: Backend) -> None:
    finding = {"file": "app/orders.py", "line": 5, "severity": "blocker", "category": "auth",
               "title": "role check removed", "body": "b",
               "evidence_quote": "orders.cancel(order_id)", "claims": [NOT_CALLS]}
    vcs = MemoryVCS(make_pr(head=H1), {"app/orders.py": TWO_GUARDS_BASE, **CFG},
                    {"app/orders.py": TWO_GUARDS_HEAD, **CFG})
    store = backend.store()
    reviewer = Reviewer(vcs, ProviderRegistry([FakeProvider([answer(finding), answer()])]), [],
                        store)
    assert reviewer.review(REPO, PR, dry_run=False).outcome == "posted"
    assert store.review_blockers(REPO, PR, "memory-review-1")[0]["deleted_kept"] == 1
    vcs.pr = make_pr(head="h2")
    vcs.head = {"app/orders.py": TWO_GUARDS_HEAD + "\n\ndef noop():\n    pass\n", **CFG}
    vcs.head_sha_now = "h2"
    assert reviewer.review(REPO, PR, dry_run=False).outcome == "posted"
    assert vcs.dismissed == []


def test_adopted_review_keeps_only_the_blockers_github_shows(backend: Backend) -> None:
    vcs, reviewer, store = rig(backend, [answer(REAL, SECOND)])
    vcs.posted.append(landed([ReviewComment(path="app/admin.py", line=12, body="x")]))
    result = reviewer.review(REPO, PR, dry_run=False)
    assert result.error == ADOPTED
    second = next(f["fingerprint"] for f in store.findings(REPO, PR) if f["line"] == 12)
    assert [b["fingerprint"] for b in store.review_blockers(REPO, PR, "memory-review-1")] == [
        second]


def test_adopted_review_with_no_comments_keeps_no_blockers(backend: Backend) -> None:
    vcs, reviewer, store = rig(backend, [answer(REAL, SECOND)])
    vcs.posted.append(landed([]))
    reviewer.review(REPO, PR, dry_run=False)
    assert store.review_blockers(REPO, PR, "memory-review-1") == []


def test_uncertain_post_that_landed_is_adopted_at_once(
        backend: Backend, monkeypatch: pytest.MonkeyPatch) -> None:
    vcs, reviewer, store = rig(backend, [answer(REAL)])
    real = vcs.post_review

    def lands_then_times_out(pr: PullRequest, review: ReviewPayload) -> str:
        real(pr, review)
        raise TimeoutError("socket")
    monkeypatch.setattr(vcs, "post_review", lands_then_times_out)
    result = reviewer.review(REPO, PR, dry_run=False)
    assert (result.outcome, result.post_id, result.error) == ("posted", "memory-review-1", ADOPTED)
    assert store.post_claim(REPO, PR, H1)[0] == "memory-review-1"  # type: ignore[index]
    assert states(store) == {11: "posted"}


def test_uncertain_post_stays_uncertain_when_github_cannot_be_asked(
        backend: Backend, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture) -> None:
    vcs, reviewer, store = rig(backend, [answer(REAL)], FakeClock())
    scripted_posts(vcs, monkeypatch, TimeoutError("socket"))
    lists = iter([[], GitHubError("GET /reviews -> HTTP 502", 502)])

    def list_reviews(repo: str, number: int) -> list[PostedReview]:
        nxt = next(lists)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt
    monkeypatch.setattr(vcs, "list_reviews", list_reviews)
    caplog.set_level(logging.WARNING, logger="reviewhound.pipeline")
    result = reviewer.review(REPO, PR, dry_run=False)
    assert (result.outcome, result.error) == ("post-uncertain", "TimeoutError")
    assert store.post_claim(REPO, PR, H1) == ("", 0.0)  # kept: it may have landed
    assert "could not check GitHub after an uncertain post" in [r.msg for r in caplog.records]


def test_claim_freed_by_the_other_run_in_between_is_claimed_and_posted(
        backend: Backend, monkeypatch: pytest.MonkeyPatch) -> None:
    vcs, reviewer, store = rig(backend, [answer(REAL)])
    real = store.claim_post
    calls: list[str] = []

    def claim(repo: str, pr: int, head: str) -> bool:
        calls.append(head)
        return False if len(calls) == 1 else real(repo, pr, head)  # held, then freed
    monkeypatch.setattr(store, "claim_post", claim)
    result = reviewer.review(REPO, PR, dry_run=False)
    assert (result.outcome, result.post_id, len(vcs.posted)) == ("posted", "memory-review-1", 1)
    assert calls == [H1, H1]
