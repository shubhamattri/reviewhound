import threading
from pathlib import Path

import pytest

from reviewhound.backends import BackendError, database_url, open_metrics, open_queue, open_store
from reviewhound.metrics import SQLiteMetrics
from reviewhound.queue import Job, LeaseLost, SQLiteQueue
from reviewhound.store import Store
from tests.conftest import Backend, FakeClock


def _race(n: int, fn: object) -> tuple[list[object], list[BaseException]]:
    barrier = threading.Barrier(n)
    out: list[object] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def run(i: int) -> None:
        try:
            barrier.wait(10)
            res = fn(i)  # type: ignore[operator]
        except BaseException as exc:  # noqa: BLE001 - surfaced by the caller's assert
            with lock:
                errors.append(exc)
            return
        with lock:
            out.append(res)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    return out, errors


def test_two_concurrent_claimers_claim_a_job_once(backend: Backend) -> None:
    for round_ in range(15):
        setup = backend.queue()
        job_id, _ = setup.enqueue("a/b", round_, "h1")
        claimers = [backend.queue(), backend.queue()]
        got, errors = _race(2, lambda i, cs=claimers: cs[i].claim())
        assert errors == []
        claimed = [j for j in got if j is not None]
        assert [j.id for j in claimed] == [job_id]  # type: ignore[attr-defined]
        assert claimers[0].complete(claimed[0]) is None  # type: ignore[arg-type]


def test_many_claimers_drain_the_queue_without_duplicates(backend: Backend) -> None:
    setup = backend.queue()
    ids = [setup.enqueue("a/b", pr, "h1")[0] for pr in range(30)]
    claimers = [backend.queue() for _ in range(6)]

    def drain(i: int) -> list[int]:
        mine: list[int] = []
        while (job := claimers[i].claim()) is not None:
            mine.append(job.id)
        return mine

    got, errors = _race(6, drain)
    assert errors == []
    flat = sorted(j for part in got for j in part)  # type: ignore[attr-defined]
    assert flat == sorted(ids)


def test_concurrent_enqueue_of_one_head_makes_one_job(backend: Backend) -> None:
    queues = [backend.queue() for _ in range(4)]
    got, errors = _race(4, lambda i: queues[i].enqueue("a/b", 1, "h1"))
    assert errors == []
    assert sorted(got) == [(1, False), (1, False), (1, False), (1, True)]  # type: ignore[type-var]


def test_extend_lease_keeps_a_running_job_from_being_reclaimed(backend: Backend,
                                                                clock: FakeClock) -> None:
    queue = backend.queue(clock=clock, lease_s=100)
    queue.enqueue("a/b", 1, "h1")
    job = queue.claim()
    assert job is not None
    clock.now += 90
    assert queue.extend_lease(job) == clock.now + 100
    clock.now += 60  # past the original lease, inside the extended one
    assert queue.claim() is None
    queue.complete(job)
    done = queue.get(job.id)
    assert done is not None and (done.state, done.attempts) == ("done", 1)


def test_extend_lease_refuses_a_job_another_worker_took(backend: Backend,
                                                        clock: FakeClock) -> None:
    queue = backend.queue(clock=clock, lease_s=100)
    queue.enqueue("a/b", 1, "h1")
    stale = queue.claim()
    assert stale is not None
    clock.now += 101
    fresh = queue.claim()
    assert fresh is not None and fresh.id == stale.id
    with pytest.raises(LeaseLost):
        queue.extend_lease(stale)
    assert queue.extend_lease(fresh) == clock.now + 100
    queue.complete(fresh)
    with pytest.raises(LeaseLost):
        queue.extend_lease(fresh)


def test_state_counts_and_oldest_queued_age(backend: Backend, clock: FakeClock) -> None:
    queue = backend.queue(clock=clock)
    assert queue.state_counts() == {}
    assert queue.oldest_queued_age() == 0.0
    queue.enqueue("a/b", 1, "h1")
    clock.now += 30
    queue.enqueue("a/b", 2, "h1")
    queue.enqueue("a/b", 2, "h2")  # supersedes h1 of PR 2
    job = queue.claim()
    assert job is not None and job.pr == 1
    clock.now += 15
    assert queue.state_counts() == {"running": 1, "queued": 1, "superseded": 1}
    assert queue.oldest_queued_age() == 15.0


def test_metrics_counters_add_up_across_writers(backend: Backend) -> None:
    logs = [backend.metrics() for _ in range(4)]
    _, errors = _race(4, lambda i: logs[i].add([("reviews_total", "posted", 1.0),
                                                ("review_cost_usd_total", "", 0.25)]))
    assert errors == []
    logs[0].add([("jobs_failed_total", "ProviderError", 1.0)])
    assert backend.metrics().counters() == {("reviews_total", "posted"): 4.0,
                                            ("review_cost_usd_total", ""): 1.0,
                                            ("jobs_failed_total", "ProviderError"): 1.0}


def test_lease_lost_job_has_the_token_it_was_claimed_with(backend: Backend) -> None:
    queue = backend.queue()
    queue.enqueue("a/b", 1, "h1")
    job = queue.claim()
    assert job is not None and job.lease_token is not None and len(job.lease_token) == 16
    forged = Job(id=job.id, repo=job.repo, pr=job.pr, head_sha=job.head_sha, state=job.state,
                 attempts=job.attempts, max_attempts=job.max_attempts, last_error=None,
                 lease_token="0" * 16)
    with pytest.raises(LeaseLost):
        queue.complete(forged)
    with pytest.raises(LeaseLost):
        queue.fail(forged, "x", retryable=True)


def test_database_url_selects_the_backend(monkeypatch: pytest.MonkeyPatch,
                                          tmp_path: Path) -> None:
    monkeypatch.delenv("REVIEWHOUND_DATABASE_URL", raising=False)
    assert database_url() is None
    monkeypatch.setenv("REVIEWHOUND_DATABASE_URL", "  ")
    assert database_url() is None
    monkeypatch.setenv("REVIEWHOUND_DATABASE_URL", "postgresql://db.internal/rh")
    assert database_url() == "postgresql://db.internal/rh"
    assert database_url("") is None  # an explicit empty value means SQLite
    marker = "not-a-real-password"
    monkeypatch.setenv("REVIEWHOUND_DATABASE_URL", "mysql://db.internal/rh?password=" + marker)
    with pytest.raises(BackendError) as err:
        database_url()
    assert marker not in str(err.value)
    monkeypatch.delenv("REVIEWHOUND_DATABASE_URL")
    home = tmp_path / "home"
    q = open_queue(home)
    assert isinstance(q, SQLiteQueue) and q.path == str(home / "queue.db")
    s = open_store(home)
    assert isinstance(s, Store) and s.path == str(home / "reviewhound.db")
    m = open_metrics(home)
    assert isinstance(m, SQLiteMetrics) and m.path == str(home / "queue.db")
