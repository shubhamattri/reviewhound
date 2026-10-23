from pathlib import Path

import pytest

from reviewhound.queue import LeaseLost, SQLiteQueue
from tests.conftest import FakeClock


def q(tmp_path: Path, clock: FakeClock, **kw: float) -> SQLiteQueue:
    return SQLiteQueue(tmp_path / "q.db", clock=clock, max_attempts=int(kw.get("max", 3)),
                       lease_s=kw.get("lease", 100), backoff_base_s=kw.get("backoff", 10))


def test_same_head_twice_is_one_job(tmp_path: Path, clock: FakeClock) -> None:
    queue = q(tmp_path, clock)
    first = queue.enqueue("Acme/Shop", 7, "h1")
    second = queue.enqueue("acme/shop", 7, "h1")
    assert first == (1, True)
    assert second == (1, False)


def test_new_head_supersedes_queued_old_head(tmp_path: Path, clock: FakeClock) -> None:
    queue = q(tmp_path, clock)
    queue.enqueue("a/b", 1, "h1")
    queue.enqueue("a/b", 1, "h2")
    job = queue.claim()
    assert job is not None and job.head_sha == "h2"
    assert queue.claim() is None
    old = queue.get(1)
    assert old is not None and old.state == "superseded"


def test_force_push_back_to_skipped_head_reviews_it(tmp_path: Path, clock: FakeClock) -> None:
    queue = q(tmp_path, clock)
    queue.enqueue("a/b", 1, "h1")
    queue.enqueue("a/b", 1, "h2")
    assert queue.enqueue("a/b", 1, "h1") == (1, True)
    job = queue.claim()
    assert job is not None and (job.id, job.head_sha) == (1, "h1")


def test_retry_uses_exponential_backoff_then_dies(tmp_path: Path, clock: FakeClock) -> None:
    queue = q(tmp_path, clock, max=3, backoff=10)
    queue.enqueue("a/b", 1, "h1")
    job = queue.claim()
    assert job is not None
    assert queue.fail(job, "boom", retryable=True) == "queued"
    assert queue.claim() is None  # backoff 10s not elapsed
    clock.now += 10
    job = queue.claim()
    assert job is not None and job.attempts == 2
    assert queue.fail(job, "boom", retryable=True) == "queued"
    clock.now += 19
    assert queue.claim() is None  # second backoff is 20s
    clock.now += 1
    job = queue.claim()
    assert job is not None and job.attempts == 3
    assert queue.fail(job, "boom again", retryable=True) == "dead"
    dead = queue.dead_jobs()
    assert [(d.id, d.last_error) for d in dead] == [(1, "boom again")]


def test_permanent_failure_is_dead_at_once(tmp_path: Path, clock: FakeClock) -> None:
    queue = q(tmp_path, clock)
    queue.enqueue("a/b", 1, "h1")
    job = queue.claim()
    assert job is not None
    assert queue.fail(job, "bad config", retryable=False) == "dead"
    assert queue.requeue(1) is True
    again = queue.claim()
    assert again is not None and again.attempts == 1


def test_crashed_worker_lease_expires_and_old_worker_cannot_finish(tmp_path: Path,
                                                                    clock: FakeClock) -> None:
    queue = q(tmp_path, clock, lease=100)
    queue.enqueue("a/b", 1, "h1")
    stale = queue.claim()
    assert stale is not None
    clock.now += 101
    fresh = queue.claim()
    assert fresh is not None and fresh.id == stale.id and fresh.attempts == 2
    with pytest.raises(LeaseLost):
        queue.complete(stale)
    with pytest.raises(LeaseLost):
        queue.fail(stale, "late", retryable=True)
    queue.complete(fresh)
    done = queue.get(1)
    assert done is not None and done.state == "done"


def test_lease_expiry_on_last_attempt_goes_dead_not_forever(tmp_path: Path,
                                                             clock: FakeClock) -> None:
    queue = q(tmp_path, clock, max=1, lease=100)
    queue.enqueue("a/b", 1, "h1")
    assert queue.claim() is not None
    clock.now += 101
    assert queue.claim() is None
    assert [(j.id, j.last_error) for j in queue.dead_jobs()] == [
        (1, "lease expired on final attempt")]


def test_backlog_alarm_counts_old_queued_jobs(tmp_path: Path, clock: FakeClock) -> None:
    queue = q(tmp_path, clock)
    queue.enqueue("a/b", 1, "h1")
    clock.now += 600
    queue.enqueue("a/b", 2, "h1")
    assert queue.stuck_count(older_than_s=300) == 1
