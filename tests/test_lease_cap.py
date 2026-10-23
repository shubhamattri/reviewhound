"""A wedged or lease-less job must stop renewing, skip posting and become reclaimable."""

import logging
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from reviewhound.heartbeat import LeaseHeartbeat
from reviewhound.queue import Job, SQLiteQueue
from reviewhound.worker import run_one
from tests.conftest import FakeClock, answer
from tests.test_pipeline import REAL, setup


class CountingQueue:
    lease_s = 30.0

    def __init__(self) -> None:
        self.calls = 0

    def extend_lease(self, job: Job) -> float:
        self.calls += 1
        return 0.0


JOB = Job(1, "a/b", 1, "h1", "running", 1, 3, None, "t")


def test_heartbeat_stops_renewing_at_the_wall_clock_cap(
        clock: FakeClock, caplog: pytest.LogCaptureFixture) -> None:
    q = CountingQueue()
    caplog.set_level(logging.ERROR, logger="reviewhound.heartbeat")
    hb = LeaseHeartbeat(q, JOB, max_job_seconds=100, clock=clock)
    clock.now += 99
    assert (hb.tick(), hb.lost, q.calls) == (True, False, 1)
    clock.now += 1
    assert hb.lost is True  # visible at once, not only at the next beat
    assert (hb.tick(), q.calls) == (False, 1)
    assert [(r.getMessage(), getattr(r, "event", None), getattr(r, "job", None))
            for r in caplog.records] == [("job ran past its time cap; lease released",
                                          "lease_lost", 1)]


def test_heartbeat_thread_ends_by_itself_after_the_cap() -> None:
    q = CountingQueue()
    with LeaseHeartbeat(q, JOB, interval_s=0.01, max_job_seconds=0.2) as hb:
        deadline = time.monotonic() + 5
        while not hb.lost and time.monotonic() < deadline:
            time.sleep(0.01)
        assert hb._thread is not None
        hb._thread.join(2)
        assert not hb._thread.is_alive()
        calls = q.calls
        time.sleep(0.1)
        assert q.calls == calls  # no renewal after the cap
    assert hb.lost is True


def test_cap_is_measured_from_entering_not_from_construction(clock: FakeClock) -> None:
    hb = LeaseHeartbeat(CountingQueue(), JOB, interval_s=60, max_job_seconds=10, clock=clock)
    clock.now += 50
    with hb:
        assert hb.lost is False
        clock.now += 10
        assert hb.lost is True


class ShouldPostReviewer:
    def __init__(self, sleep_s: float) -> None:
        self.sleep_s = sleep_s
        self.seen: list[bool] = []

    def review(self, repo: str, number: int, *, dry_run: bool, expected_head: str | None = None,
               should_post: Callable[[], bool] | None = None) -> SimpleNamespace:
        assert should_post is not None
        self.seen.append(should_post())
        time.sleep(self.sleep_s)
        ok = should_post()
        self.seen.append(ok)
        return SimpleNamespace(outcome="posted" if ok else "lease-lost", timings={},
                               cost_usd=0.0)


def test_worker_past_the_cap_skips_posting_and_requeues_the_job(tmp_path: Path) -> None:
    queue = SQLiteQueue(tmp_path / "q.db", backoff_base_s=0)
    queue.enqueue("a/b", 1, "h1")
    reviewer = ShouldPostReviewer(sleep_s=0.3)
    job = run_one(queue, reviewer, max_job_seconds=0.1)  # type: ignore[arg-type]
    assert job is not None
    assert reviewer.seen == [True, False]
    after = queue.get(job.id)
    assert after is not None
    assert (after.state, after.attempts, after.last_error) == (
        "queued", 1, ("LeaseLost: review ran past --max-job-seconds or lost its lease; "
                      "nothing was posted"))


def test_worker_within_the_cap_completes(tmp_path: Path) -> None:
    queue = SQLiteQueue(tmp_path / "q.db")
    queue.enqueue("a/b", 1, "h1")
    reviewer = ShouldPostReviewer(sleep_s=0)
    job = run_one(queue, reviewer)  # type: ignore[arg-type]
    assert job is not None
    assert reviewer.seen == [True, True]
    after = queue.get(job.id)
    assert after is not None and after.state == "done"


def test_pipeline_posts_nothing_when_should_post_says_no(tmp_path: Path) -> None:
    vcs, reviewer, store = setup([answer(REAL)], posting=True, tmp_path=tmp_path)
    result = reviewer.review("acme/shop", 7, dry_run=False, should_post=lambda: False)
    assert result.outcome == "lease-lost"
    assert vcs.posted == []
    assert [r["outcome"] for r in store.reviews("acme/shop", 7)] == ["lease-lost"]
    assert store.claim_post("acme/shop", 7, "h1") is True  # the post slot was never taken
