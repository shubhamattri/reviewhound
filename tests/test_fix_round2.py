"""Fix round 2: the posting path as one guarded unit, durable post ids, dismissal evidence."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from reviewhound.config import Severity
from reviewhound.findings import Finding
from reviewhound.github_feedback import DISMISS_MESSAGE, open_reason
from reviewhound.pipeline import (
    MAX_BODY_CHARS,
    PostRefused,
    RecordAfterPostError,
    Reviewer,
    ReviewResult,
    comments_in_body,
)
from reviewhound.providers import FakeProvider, ProviderRegistry
from reviewhound.queue import WorkerQueue
from reviewhound.store import Blocker, ReviewRecord, ReviewStore
from reviewhound.vcs.base import DictSnapshot, PullRequest, ReviewComment, ReviewPayload
from reviewhound.vcs.github import GitHubError
from reviewhound.vcs.memory import MemoryVCS
from reviewhound.worker import is_retryable, run_one
from tests.conftest import Backend, answer, make_pr, spy
from tests.test_fix_round1_b import REJECTED, SECOND, scripted_posts
from tests.test_github_feedback import CFG, FIXED, FP, push, reviewer_on
from tests.test_pipeline import BASE, HEAD, REAL

REPO, PR, H1 = "acme/shop", 7, "h1"


def claim_free(store: ReviewStore) -> bool:
    """True when the head is free again (and leaves it claimed, as a later run would)."""
    return store.claim_post(REPO, PR, H1)


def on_claim(store: ReviewStore, monkeypatch: pytest.MonkeyPatch, then: Callable[[], None]) -> None:
    """Run `then` right after the store hands out the post claim."""
    real = store.claim_post

    def claim(repo: str, pr: int, head: str) -> bool:
        won = real(repo, pr, head)
        then()
        return won
    monkeypatch.setattr(store, "claim_post", claim)


# --- P1: the lease and the head are checked right before every POST --------------------------

def test_head_moving_after_the_claim_posts_nothing_and_frees_the_claim(
        backend: Backend, monkeypatch: pytest.MonkeyPatch) -> None:
    vcs, reviewer, store = reviewer_on(backend, [answer(REAL)])
    on_claim(store, monkeypatch, lambda: setattr(vcs, "head_sha_now", "h2"))
    result = reviewer.review(REPO, PR, dry_run=False)
    assert (result.outcome, result.post_id, vcs.posted) == ("head-moved", "", [])
    assert [(r["outcome"], r["posted"]) for r in store.reviews(REPO, PR)] == [("head-moved", 0)]
    assert [f["state"] for f in store.findings(REPO, PR)] == ["stored"]
    assert claim_free(store) is True


def test_lease_lost_after_the_claim_posts_nothing_and_frees_the_claim(
        backend: Backend, monkeypatch: pytest.MonkeyPatch) -> None:
    vcs, reviewer, store = reviewer_on(backend, [answer(REAL)])
    lease = {"held": True}
    on_claim(store, monkeypatch, lambda: lease.update(held=False))
    result = reviewer.review(REPO, PR, dry_run=False, should_post=lambda: lease["held"])
    assert (result.outcome, vcs.posted) == ("lease-lost", [])
    assert [r["outcome"] for r in store.reviews(REPO, PR)] == ["lease-lost"]
    assert claim_free(store) is True


def test_body_retry_never_posts_against_a_superseded_head(
        backend: Backend, monkeypatch: pytest.MonkeyPatch) -> None:
    vcs, reviewer, store = reviewer_on(backend, [answer(REAL)])
    real_post = vcs.post_review
    calls: list[ReviewPayload] = []

    def refuse_then_push(pr: PullRequest, review: ReviewPayload) -> str:
        calls.append(review)
        if len(calls) == 1:
            vcs.head_sha_now = "h2"  # a push lands while GitHub refuses the first try
            raise REJECTED
        return real_post(pr, review)
    monkeypatch.setattr(vcs, "post_review", refuse_then_push)
    result = reviewer.review(REPO, PR, dry_run=False)
    assert (result.outcome, len(calls), vcs.posted) == ("head-moved", 1, [])
    assert claim_free(store) is True


def test_head_is_read_after_the_claim_not_before(backend: Backend,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    vcs, reviewer, store = reviewer_on(backend, [answer(REAL)])
    order: list[str] = []
    on_claim(store, monkeypatch, lambda: order.append("claim"))
    real_head = vcs.current_head

    def head(repo: str, number: int) -> str:
        order.append("head")
        return real_head(repo, number)
    monkeypatch.setattr(vcs, "current_head", head)
    real_post = vcs.post_review

    def post(pr: PullRequest, review: ReviewPayload) -> str:
        order.append("POST")
        return real_post(pr, review)
    monkeypatch.setattr(vcs, "post_review", post)
    assert reviewer.review(REPO, PR, dry_run=False).outcome == "posted"
    assert order[-3:] == ["claim", "head", "POST"]


# --- P2: anything raised between the claim and the POST frees the claim and re-raises --------

def test_viewer_login_failure_frees_the_claim_and_reraises(backend: Backend,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    vcs, reviewer, store = reviewer_on(backend, [answer(REAL)])
    vcs.pr = make_pr(head=H1, author="alice")
    boom = GitHubError("token lookup failed")

    def viewer() -> str:
        raise boom
    monkeypatch.setattr(vcs, "viewer_login", viewer)
    with pytest.raises(GitHubError) as raised:
        reviewer.review(REPO, PR, dry_run=False)
    assert raised.value is boom
    assert (vcs.posted, store.reviews(REPO, PR)) == ([], [])
    assert claim_free(store) is True


def test_head_lookup_failure_frees_the_claim_and_reraises(backend: Backend,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    vcs, reviewer, store = reviewer_on(backend, [answer(REAL)])

    def head(repo: str, number: int) -> str:
        raise GitHubError("GET /pulls/7 -> HTTP 502", 502)
    monkeypatch.setattr(vcs, "current_head", head)
    with pytest.raises(GitHubError, match="HTTP 502"):
        reviewer.review(REPO, PR, dry_run=False)
    assert vcs.posted == []
    assert claim_free(store) is True


def test_failure_before_the_post_requeues_the_job_and_the_retry_posts(
        backend: Backend, monkeypatch: pytest.MonkeyPatch) -> None:
    vcs, reviewer, store = reviewer_on(backend, [answer(REAL), answer(REAL)])
    vcs.pr = make_pr(head=H1, author="alice")
    q = backend.queue(backoff_base_s=0)
    q.enqueue(REPO, PR, H1)
    logins = iter([GitHubError("token lookup failed"), "reviewhound-bot"])

    def viewer() -> str:
        nxt = next(logins)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt
    monkeypatch.setattr(vcs, "viewer_login", viewer)
    job = run_one(q, reviewer)
    assert job is not None
    after = q.get(job.id)
    assert after is not None and (after.state, after.last_error) == (
        "queued", "GitHubError: token lookup failed")
    run_one(q, reviewer)
    done = q.get(job.id)
    assert done is not None and done.state == "done"
    assert len(vcs.posted) == 1


def test_interrupt_during_the_post_keeps_the_claim(backend: Backend,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    vcs, reviewer, store = reviewer_on(backend, [answer(REAL)])
    scripted_posts(vcs, monkeypatch, KeyboardInterrupt())  # type: ignore[arg-type]
    with pytest.raises(KeyboardInterrupt):
        reviewer.review(REPO, PR, dry_run=False)
    assert claim_free(store) is False  # it may have landed: never post this head twice


# --- P3: only 400 and 422 get the body retry; other refusals raise and free the claim -------

def test_http_400_is_retried_in_the_body(backend: Backend, monkeypatch: pytest.MonkeyPatch) -> None:
    vcs, reviewer, _store = reviewer_on(backend, [answer(REAL)])
    calls = scripted_posts(vcs, monkeypatch, GitHubError("HTTP 400: bad line", 400), None)
    result = reviewer.review(REPO, PR, dry_run=False)
    assert (result.outcome, [len(c.comments) for c in calls]) == ("posted", [1, 0])
    assert result.error == "inline comments refused (HTTP 400); posted them in the review body"


@pytest.mark.parametrize(("status", "retryable"), [
    (401, True), (403, True), (408, True), (404, False), (409, False), (410, False),
])
def test_other_refusals_raise_free_the_claim_and_never_retry_in_the_body(
        backend: Backend, monkeypatch: pytest.MonkeyPatch, status: int, retryable: bool) -> None:
    vcs, reviewer, store = reviewer_on(backend, [answer(REAL)])
    calls = scripted_posts(vcs, monkeypatch, GitHubError(f"HTTP {status}: no", status), None)
    with pytest.raises(PostRefused) as raised:
        reviewer.review(REPO, PR, dry_run=False)
    assert (raised.value.status, str(raised.value), len(calls)) == (status, f"HTTP {status}: no", 1)
    assert is_retryable(raised.value) is retryable
    assert store.reviews(REPO, PR) == []
    assert claim_free(store) is True


def test_refusal_of_the_body_retry_with_403_raises_and_frees_the_claim(
        backend: Backend, monkeypatch: pytest.MonkeyPatch) -> None:
    vcs, reviewer, store = reviewer_on(backend, [answer(REAL)])
    calls = scripted_posts(vcs, monkeypatch, REJECTED, GitHubError("HTTP 403: nope", 403))
    with pytest.raises(PostRefused, match="HTTP 403: nope"):
        reviewer.review(REPO, PR, dry_run=False)
    assert [len(c.comments) for c in calls] == [1, 0]
    assert claim_free(store) is True


@pytest.mark.parametrize(("status", "state", "error"), [
    (401, "queued", "PostRefused: HTTP 401: no"),
    (403, "queued", "PostRefused: HTTP 403: no"),
    (408, "queued", "PostRefused: HTTP 408: no"),
    (404, "dead", "PostRefused: HTTP 404: no"),
])
def test_worker_requeues_auth_and_timeout_refusals_and_kills_the_rest(
        backend: Backend, monkeypatch: pytest.MonkeyPatch, status: int, state: str,
        error: str) -> None:
    vcs, reviewer, _store = reviewer_on(backend, [answer(REAL)])
    scripted_posts(vcs, monkeypatch, GitHubError(f"HTTP {status}: no", status))
    q = backend.queue(backoff_base_s=0)
    q.enqueue(REPO, PR, H1)
    job = run_one(q, reviewer)
    assert job is not None
    after = q.get(job.id)
    assert after is not None and (after.state, after.last_error) == (state, error)


def test_installation_lookup_401_stays_permanent() -> None:
    assert is_retryable(GitHubError("GET /installation -> HTTP 401", 401)) is False
    assert is_retryable(PostRefused("POST -> HTTP 401", 401)) is True


def _comments(n: int, size: int) -> ReviewPayload:
    return ReviewPayload(head_sha="h", event="COMMENT", body="B", comments=[
        ReviewComment(path="a.py", line=i, body="x" * size) for i in range(1, n + 1)])


def _block(i: int, size: int) -> str:
    return f"\n\n**`a.py:{i}`**\n\n" + "x" * size


TITLE = "B\n\nGitHub could not place these comments on their lines:"


def test_body_retry_is_capped_and_counts_what_it_left_out() -> None:
    out = comments_in_body(_comments(10, 9_990))
    assert out.body == TITLE + "".join(_block(i, 9_990) for i in range(1, 6)) + \
        "\n\n5 more findings not shown."
    assert len(out.body) <= MAX_BODY_CHARS == 60_000
    assert out.comments == []


def test_body_retry_keeps_every_comment_that_fits() -> None:
    out = comments_in_body(_comments(5, 9_990))
    assert out.body == TITLE + "".join(_block(i, 9_990) for i in range(1, 6))


def test_body_retry_fills_up_to_the_exact_limit() -> None:
    review = _comments(2, 10)
    exact = len(TITLE) + 2 * len(_block(1, 10))
    assert comments_in_body(review, limit=exact).body == TITLE + _block(1, 10) + _block(2, 10)
    # The two blocks no longer fit, and one block plus "1 more" is longer than the limit.
    assert comments_in_body(review, limit=exact - 1).body == \
        TITLE + "\n\n2 more findings not shown."
    wide = _comments(3, 100)
    roomy = len(TITLE) + 2 * len(_block(1, 100)) + len("\n\n1 more findings not shown.")
    assert comments_in_body(wide, limit=roomy).body == \
        TITLE + _block(1, 100) + _block(2, 100) + "\n\n1 more findings not shown."


def test_body_retry_trims_an_oversized_body_itself() -> None:
    review = ReviewPayload(head_sha="h", event="COMMENT", body="y" * 70_000, comments=[
        ReviewComment(path="a.py", line=i, body="c" * 100) for i in (1, 2, 3)])
    out = comments_in_body(review)
    tail = TITLE[1:] + "\n\n3 more findings not shown."
    assert out.body == "y" * (MAX_BODY_CHARS - len(tail)) + tail
    assert len(out.body) == MAX_BODY_CHARS
    small = ReviewPayload(head_sha="h", event="COMMENT", body="y" * 70_000,
                          comments=[ReviewComment(path="a.py", line=1, body="c")])
    assert comments_in_body(small).body.endswith("lines:\n\n**`a.py:1`**\n\nc")
    assert len(comments_in_body(small).body) <= MAX_BODY_CHARS


def test_capped_retry_is_what_gets_posted(backend: Backend,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    long = {**REAL, "body": "z" * 70_000}
    vcs, reviewer, _store = reviewer_on(backend, [answer(long)])
    calls = scripted_posts(vcs, monkeypatch, REJECTED, None)
    assert reviewer.review(REPO, PR, dry_run=False).outcome == "posted"
    assert len(calls[1].body) <= MAX_BODY_CHARS
    assert calls[1].body.endswith("\n\n1 more findings not shown.")


# --- P4: the job's state follows the review outcome ---------------------------------------

def _worker_run(backend: Backend, reviewer: Reviewer) -> tuple[str, str | None]:
    q: WorkerQueue = backend.queue(backoff_base_s=0)
    q.enqueue(REPO, PR, H1)
    job = run_one(q, reviewer)
    assert job is not None
    after = q.get(job.id)
    assert after is not None
    return after.state, after.last_error


def test_post_rejected_after_the_body_retry_is_a_dead_job_with_its_reason(
        backend: Backend, monkeypatch: pytest.MonkeyPatch) -> None:
    vcs, reviewer, _store = reviewer_on(backend, [answer(REAL)])
    scripted_posts(vcs, monkeypatch, REJECTED, GitHubError("HTTP 422: body too long", 422))
    assert _worker_run(backend, reviewer) == ("dead", "post-rejected: HTTP 422: body too long")


@pytest.mark.parametrize("outcome", ["head-moved", "already-posted"])
def test_head_moved_and_already_posted_complete_the_job(
        backend: Backend, monkeypatch: pytest.MonkeyPatch, outcome: str) -> None:
    vcs, reviewer, store = reviewer_on(backend, [answer(REAL)])
    if outcome == "head-moved":
        vcs.head_sha_now = "h2"
    else:
        store.claim_post(REPO, PR, H1)
    assert _worker_run(backend, reviewer) == ("done", None)
    assert [r["outcome"] for r in store.reviews(REPO, PR)] == [outcome]


def test_lease_lost_still_fails_retryable(backend: Backend,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    _vcs, reviewer, _store = reviewer_on(backend, [answer(REAL)])
    real = reviewer.review

    def lost(*a: Any, **kw: Any) -> ReviewResult:
        return real(*a, **{**kw, "should_post": lambda: False})
    monkeypatch.setattr(reviewer, "review", lost)
    state, error = _worker_run(backend, reviewer)
    assert (state, error is not None and error.startswith("LeaseLost")) == ("queued", True)


# --- P5: the post id is durable the moment the POST succeeds; a retry recovers ------------

def test_post_id_is_written_before_the_review_record(backend: Backend,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    _vcs, reviewer, store = reviewer_on(backend, [answer(REAL)])
    log = spy(monkeypatch, store)
    assert reviewer.review(REPO, PR, dry_run=False).outcome == "posted"
    sql = [s for s, _ in log]
    mark = next(i for i, s in enumerate(sql) if "SET post_id" in s)
    record = next(i for i, s in enumerate(sql) if s.startswith("INSERT INTO reviews"))
    assert mark < record
    assert log[mark][1][-1] == "memory-review-1"


def test_record_failure_after_the_post_is_retryable_and_the_retry_recovers(
        backend: Backend, monkeypatch: pytest.MonkeyPatch) -> None:
    vcs, reviewer, store = reviewer_on(backend, [answer(REAL), answer(REAL)])
    real_tx = store._tx  # type: ignore[attr-defined]
    spy(monkeypatch, store, "INSERT INTO reviews")
    with pytest.raises(RecordAfterPostError) as raised:
        reviewer.review(REPO, PR, dry_run=False)
    assert str(raised.value) == "review memory-review-1 posted but not recorded: RuntimeError"
    assert is_retryable(raised.value) is True
    assert store.unrecorded_post(REPO, PR, H1) == "memory-review-1"
    monkeypatch.setattr(store, "_tx", real_tx)

    again = reviewer.review(REPO, PR, dry_run=False)
    assert (again.outcome, again.post_id, len(vcs.posted)) == ("posted", "memory-review-1", 1)
    assert again.error == "recovered: an earlier attempt posted this review but did not record it"
    assert [(r["outcome"], r["posted"], r["post_id"]) for r in store.reviews(REPO, PR)] == [
        ("posted", 1, "memory-review-1")]
    assert store.linked_findings(REPO, [1001]) == {1001: (PR, FP)}
    assert store.unrecorded_post(REPO, PR, H1) is None
    assert [f["state"] for f in store.findings(REPO, PR)] == ["posted"]


def test_recovery_runs_the_dismissal_too(backend: Backend,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    vcs, reviewer, store = reviewer_on(backend, [answer(REAL), answer(), answer()])
    reviewer.review(REPO, PR, dry_run=False)
    push(vcs, "h2", FIXED)
    real_tx = store._tx  # type: ignore[attr-defined]
    spy(monkeypatch, store, "INSERT INTO reviews")
    with pytest.raises(RecordAfterPostError):
        reviewer.review(REPO, PR, dry_run=False)
    assert vcs.dismissed == []
    monkeypatch.setattr(store, "_tx", real_tx)
    again = reviewer.review(REPO, PR, dry_run=False)
    assert (again.outcome, again.post_id, len(vcs.posted)) == ("posted", "memory-review-2", 2)
    assert vcs.dismissed == [("memory-review-1", DISMISS_MESSAGE)]


def test_worker_retries_a_record_failure_and_completes_without_posting_twice(
        backend: Backend, monkeypatch: pytest.MonkeyPatch) -> None:
    vcs, reviewer, store = reviewer_on(backend, [answer(REAL), answer(REAL)])
    real_tx = store._tx  # type: ignore[attr-defined]
    spy(monkeypatch, store, "INSERT INTO reviews")
    q = backend.queue(backoff_base_s=0)
    q.enqueue(REPO, PR, H1)
    job = run_one(q, reviewer)
    assert job is not None
    after = q.get(job.id)
    assert after is not None and after.state == "queued"
    assert after.last_error == ("RecordAfterPostError: review memory-review-1 posted but not "
                                "recorded: RuntimeError")
    monkeypatch.setattr(store, "_tx", real_tx)
    run_one(q, reviewer)
    done = q.get(job.id)
    assert done is not None and done.state == "done"
    assert len(vcs.posted) == 1
    assert [r["outcome"] for r in store.reviews(REPO, PR)] == ["posted"]


def test_mark_failure_is_logged_and_the_review_still_records(
        backend: Backend, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture) -> None:
    _vcs, reviewer, store = reviewer_on(backend, [answer(REAL)])
    spy(monkeypatch, store, "SET post_id")
    caplog.set_level(logging.ERROR, logger="reviewhound.pipeline")
    assert reviewer.review(REPO, PR, dry_run=False).outcome == "posted"
    assert [(r.msg, r.post_id, r.error) for r in caplog.records] == [  # type: ignore[attr-defined]
        ("could not record the post id on the claim", "memory-review-1", "RuntimeError")]
    assert [(r["outcome"], r["post_id"]) for r in store.reviews(REPO, PR)] == [
        ("posted", "memory-review-1")]


def test_claim_without_a_post_id_stays_already_posted(backend: Backend) -> None:
    vcs, reviewer, store = reviewer_on(backend, [answer(REAL)])
    store.claim_post(REPO, PR, H1)  # an uncertain post: no id was ever recorded
    result = reviewer.review(REPO, PR, dry_run=False)
    assert (result.outcome, result.post_id, vcs.posted) == ("already-posted", "", [])


def test_recorded_post_is_not_recovered_twice(backend: Backend) -> None:
    vcs, reviewer, store = reviewer_on(backend, [answer(REAL), answer(REAL)])
    reviewer.review(REPO, PR, dry_run=False)
    again = reviewer.review(REPO, PR, dry_run=False)
    assert (again.outcome, len(vcs.posted)) == ("already-posted", 1)
    assert [r["outcome"] for r in store.reviews(REPO, PR)] == ["posted", "already-posted"]


def test_unrecorded_post_store_contract(backend: Backend) -> None:
    store = backend.store()
    assert store.unrecorded_post(REPO, PR, H1) is None
    assert store.claim_post("Acme/Shop", PR, H1) is True
    assert store.unrecorded_post(REPO, PR, H1) is None
    store.mark_posted("ACME/shop", PR, H1, "r9")
    assert store.unrecorded_post("acme/SHOP", PR, H1) == "r9"
    assert store.unrecorded_post(REPO, PR, "h2") is None
    assert store.claim_post(REPO, PR, H1) is False
    store.record_review(ReviewRecord(repo=REPO, pr=PR, head_sha=H1, outcome="post-uncertain",
                                     posted=True, post_id="r9"), [], [])
    assert store.unrecorded_post(REPO, PR, H1) == "r9"  # only a posted record counts
    store.record_review(ReviewRecord(repo=REPO, pr=PR, head_sha=H1, outcome="posted",
                                     posted=True, post_id="r9"), [], [])
    assert store.unrecorded_post(REPO, PR, H1) is None


def test_mark_posted_without_a_claim_still_records_the_id(backend: Backend) -> None:
    store = backend.store()
    store.mark_posted(REPO, PR, H1, "r1")
    assert store.claim_post(REPO, PR, H1) is False
    assert store.unrecorded_post(REPO, PR, H1) == "r1"


def test_old_sqlite_posts_table_gets_the_post_id_column(tmp_path: Any) -> None:
    from reviewhound.store import Store
    db = tmp_path / "s.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE posts (repo TEXT NOT NULL, pr INTEGER NOT NULL, head_sha TEXT NOT NULL,"
              " created_at REAL NOT NULL, PRIMARY KEY (repo, pr, head_sha))")
    c.execute("INSERT INTO posts VALUES ('acme/shop', 7, 'h0', 1.0)")
    c.commit()
    c.close()
    store = Store(db)
    Store(db)  # a second start must not try to add it again
    assert store.claim_post(REPO, PR, "h0") is False  # the old claim survived
    store.mark_posted(REPO, PR, "h0", "r1")
    assert store.unrecorded_post(REPO, PR, "h0") == "r1"


def test_old_postgres_posts_table_gets_the_post_id_column(backend: Backend) -> None:
    if backend.url is None:
        pytest.skip("Postgres only")
    import psycopg
    with psycopg.connect(backend.url, autocommit=True) as c:
        c.execute("CREATE TABLE posts (repo TEXT NOT NULL, pr INTEGER NOT NULL, "
                  "head_sha TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL, "
                  "PRIMARY KEY (repo, pr, head_sha))")
        c.execute("INSERT INTO posts VALUES ('acme/shop', 7, 'h0', 1.0)")
    store = backend.store()
    backend.store()
    assert store.claim_post(REPO, PR, "h0") is False
    store.mark_posted(REPO, PR, "h0", "r1")
    assert store.unrecorded_post(REPO, PR, "h0") == "r1"


# --- D1: blocking evidence is kept as decided before any proof downgrade or hold ----------

def _proof_moves(monkeypatch: pytest.MonkeyPatch, how: str) -> None:
    """Stand in for a proof stage that downgrades or holds the blocker (verdict kept)."""
    def prove(self: Reviewer, decision: Any, *a: Any) -> None:
        for f in list(decision.post):
            if how == "downgrade":
                f.severity = Severity.MAJOR
            else:
                decision.post.remove(f)
                decision.held.append((f, "below blocker (a generated test passed)"))
    monkeypatch.setattr(Reviewer, "_prove", prove)


def _proof_on(vcs: MemoryVCS) -> None:
    cfg = "posting: true\nproof:\n  enabled: true\n"
    vcs.base = {**vcs.base, ".reviewhound.yml": cfg}
    vcs.head = {**vcs.head, ".reviewhound.yml": cfg}


@pytest.mark.parametrize("how", ["downgrade", "hold"])
def test_blocker_changed_by_a_proof_is_still_dismissed_once_gone(
        backend: Backend, monkeypatch: pytest.MonkeyPatch, how: str) -> None:
    vcs, reviewer, store = reviewer_on(backend, [answer(REAL), answer()])
    _proof_on(vcs)
    _proof_moves(monkeypatch, how)
    first = reviewer.review(REPO, PR, dry_run=False)
    assert (first.outcome, first.review.event) == ("posted", "REQUEST_CHANGES")  # type: ignore
    assert store.review_blockers(REPO, PR, "memory-review-1") == [{
        "fingerprint": FP, "file": "app/admin.py", "side": "RIGHT", "category": "auth",
        "evidence_quote": "db.delete(uid)", "identity": ""}]
    push(vcs, "h2", FIXED)
    _proof_on(vcs)
    assert reviewer.review(REPO, PR, dry_run=False).outcome == "posted"
    assert vcs.dismissed == [("memory-review-1", DISMISS_MESSAGE)]


@pytest.mark.parametrize("how", ["downgrade", "hold"])
def test_blocker_changed_by_a_proof_keeps_the_verdict_while_present(
        backend: Backend, monkeypatch: pytest.MonkeyPatch, how: str) -> None:
    vcs, reviewer, store = reviewer_on(backend, [answer(REAL), answer()])
    _proof_on(vcs)
    _proof_moves(monkeypatch, how)
    reviewer.review(REPO, PR, dry_run=False)
    push(vcs, "h2", {**HEAD, "app/other.py": "x = 1\n"})
    _proof_on(vcs)
    reviewer.review(REPO, PR, dry_run=False)
    assert (vcs.dismissed, store.dismissals(REPO, PR)) == ([], [])


def test_only_request_changes_reviews_keep_blockers(backend: Backend) -> None:
    minor = {**REAL, "severity": "minor"}
    _vcs, reviewer, store = reviewer_on(backend, [answer(minor)])
    result = reviewer.review(REPO, PR, dry_run=False)
    assert (result.outcome, result.review.event) == ("posted", "COMMENT")  # type: ignore
    assert (result.blockers, store.review_blockers(REPO, PR, "memory-review-1")) == ([], [])


def test_blockers_are_written_only_for_a_posted_review_with_an_id(backend: Backend) -> None:
    store = backend.store()
    b = Blocker("fp1", "a.py", "RIGHT", "auth", "do_it(x)", "")
    for outcome, post_id in [("shadow", ""), ("post-uncertain", ""), ("posted", "")]:
        store.record_review(ReviewRecord(repo=REPO, pr=PR, head_sha=H1, outcome=outcome,
                                         post_id=post_id), [], [], [b])
    store.record_review(ReviewRecord(repo="Acme/Shop", pr=PR, head_sha=H1, outcome="posted",
                                     posted=True, post_id="r1"), [], [], [b])
    assert store.review_blockers(REPO, PR, "") == []
    assert store.review_blockers("ACME/shop", PR, "r1") == [{
        "fingerprint": "fp1", "file": "a.py", "side": "RIGHT", "category": "auth",
        "evidence_quote": "do_it(x)", "identity": ""}]
    assert store.review_blockers(REPO, 8, "r1") == []


def test_legacy_review_without_blocker_rows_falls_back_to_posted_findings(
        backend: Backend) -> None:
    vcs, reviewer, store = reviewer_on(backend, [answer(), answer()])
    f = Finding(file="app/admin.py", line=11, severity=Severity.BLOCKER, category="auth",
                title="t", body="b", evidence_quote="db.delete(uid)", source="model:m")
    store.record_review(ReviewRecord(repo=REPO, pr=PR, head_sha="h0", outcome="posted",
                                     event="REQUEST_CHANGES", posted=True, post_id="legacy-1"),
                        [f], [])
    assert reviewer.review(REPO, PR, dry_run=False).outcome == "posted"
    assert vcs.dismissed == []  # the quote is still in app/admin.py
    push(vcs, "h2", FIXED)
    reviewer.review(REPO, PR, dry_run=False)
    assert vcs.dismissed == [("legacy-1", DISMISS_MESSAGE)]


# --- D2: "gone" means gone from its file AND every other file in the head index -----------

def test_quote_still_in_an_unchanged_file_keeps_the_verdict(backend: Backend) -> None:
    legacy = {"app/legacy.py": "def purge(uid):\n    db.delete(uid)\n"}
    vcs = MemoryVCS(make_pr(head=H1), {**BASE, **CFG, **legacy}, {**HEAD, **CFG, **legacy})
    store = backend.store()
    reviewer = Reviewer(vcs, ProviderRegistry([FakeProvider([answer(REAL), answer()])]), [],
                        store)
    reviewer.review(REPO, PR, dry_run=False)
    push(vcs, "h2", {**FIXED, **legacy})
    second = reviewer.review(REPO, PR, dry_run=False)
    assert "app/legacy.py" not in second.changed_paths
    assert second.head_sources["app/legacy.py"] == legacy["app/legacy.py"]
    assert (vcs.dismissed, store.dismissals(REPO, PR)) == ([], [])


class _Head(DictSnapshot):
    def skip_reason(self, path: str) -> str | None:
        return "too big" if path == "big.py" else None


ROW = {"file": "a.py", "side": "RIGHT", "category": "auth", "evidence_quote": "do_the_thing(x)",
       "identity": ""}


@pytest.mark.parametrize(("files", "changed", "sources", "reason"), [
    ({"a.py": "y = 2\n", "b.py": "do_the_thing(x)\n"}, [], {"b.py": "do_the_thing(x)\n"},
     "evidence quote is now in b.py"),
    ({"a.py": "y = 2\n"}, [], {"b.py": "do_the_thing(x)\n"}, "evidence quote is now in b.py"),
    ({}, [], {"b.py": "do_the_thing(x)\n"}, "evidence quote is now in b.py"),
    ({"a.py": "y = 2\n"}, [], {"a.py": "y = 2\n", "b.py": "z = 3\n"}, None),
    ({"a.py": "y = 2\n"}, ["big.py"], {}, "big.py could not be read at head (too big)"),
    ({"a.py": "y = do_the_thing(x)\n"}, [], {"b.py": "do_the_thing(x)\n"},
     "evidence quote is still in a.py"),
])
def test_open_reason_searches_every_indexed_file(files: dict[str, str], changed: list[str],
                                                 sources: dict[str, str],
                                                 reason: str | None) -> None:
    assert open_reason(ROW, _Head(files), changed, sources) == reason


# --- H5: a Postgres write transaction retries once on deadlock or serialization failure ----

class _Flaky:
    """A connection whose first `fails` statements matching `marker` raise `error`."""

    def __init__(self, conn: Any, marker: str, error: Exception, state: dict[str, int]) -> None:
        self._c, self._marker, self._error, self._state = conn, marker, error, state

    def execute(self, sql: str, *a: Any, **k: Any) -> Any:
        if self._marker in sql:
            self._state["seen"] += 1
            if self._state["seen"] <= self._state["fails"]:
                raise self._error
        return self._c.execute(sql, *a, **k)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._c, name)


def flaky(monkeypatch: pytest.MonkeyPatch, obj: Any, marker: str, error: Exception,
          fails: int) -> dict[str, int]:
    state = {"seen": 0, "fails": fails}
    real = obj._tx

    @contextmanager
    def tx() -> Iterator[Any]:
        with real() as c:
            yield _Flaky(c, marker, error, state)
    monkeypatch.setattr(obj, "_tx", tx)
    return state


def _pg_errors() -> list[Exception]:
    import psycopg.errors as pge
    return [pge.DeadlockDetected("deadlock detected"),
            pge.SerializationFailure("could not serialize access")]


def _pg_only(backend: Backend) -> None:
    if backend.url is None:
        pytest.skip("Postgres only")


@pytest.mark.parametrize("which", [0, 1])
def test_postgres_write_retries_once_and_commits(backend: Backend,
                                                 monkeypatch: pytest.MonkeyPatch,
                                                 which: int) -> None:
    _pg_only(backend)
    q = backend.queue()
    state = flaky(monkeypatch, q, "INSERT INTO jobs", _pg_errors()[which], fails=1)
    assert q.enqueue(REPO, PR, H1) == (1, True)
    assert state["seen"] == 2
    job = q.claim()
    assert job is not None and (job.repo, job.head_sha) == (REPO, H1)


@pytest.mark.parametrize("which", [0, 1])
def test_postgres_write_failing_twice_raises_and_writes_nothing(
        backend: Backend, monkeypatch: pytest.MonkeyPatch, which: int) -> None:
    _pg_only(backend)
    store = backend.store()
    err = _pg_errors()[which]
    state = flaky(monkeypatch, store, "INSERT INTO feedback_events", err, fails=2)
    with pytest.raises(type(err)):
        store.apply_feedback("thread:abc", REPO, [])
    assert state["seen"] == 2
    monkeypatch.undo()
    assert store.apply_feedback("thread:abc", REPO, []) == []  # the key was never committed


def test_postgres_write_does_not_retry_other_errors(backend: Backend,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    _pg_only(backend)
    import psycopg.errors as pge
    store = backend.store()
    state = flaky(monkeypatch, store, "INSERT INTO posts", pge.UniqueViolation("dup"), fails=2)
    with pytest.raises(pge.UniqueViolation):
        store.claim_post(REPO, PR, H1)
    assert state["seen"] == 1


def test_webhook_enqueue_rides_out_one_deadlock(backend: Backend,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    _pg_only(backend)
    from reviewhound.webhook import handle_event
    q = backend.queue()
    flaky(monkeypatch, q, "pg_advisory_xact_lock(hashtext", _pg_errors()[0], fails=1)
    body = {"action": "opened", "repository": {"full_name": REPO},
            "pull_request": {"number": PR, "draft": False, "head": {"sha": H1}}}
    assert handle_event(q, "pull_request", body) == (202, "job 1 queued")
