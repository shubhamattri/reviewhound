import http.client
import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from reviewhound.cli import main
from reviewhound.heartbeat import LeaseHeartbeat
from reviewhound.metrics import (
    BacklogAlarm,
    Snapshot,
    SQLiteMetrics,
    collect,
    render_prometheus,
    render_stats,
)
from reviewhound.providers import ProviderError
from reviewhound.queue import Job, LeaseLost, SQLiteQueue, WorkerQueue
from reviewhound.webhook import make_server
from reviewhound.worker import run_forever, run_one
from tests.conftest import Backend, FakeClock


class StubReviewer:
    def __init__(self, outcome: str = "posted", timings: dict[str, float] | None = None,
                 cost: float = 0.0, raises: Exception | None = None, sleep_s: float = 0.0,
                 during: Any = None) -> None:
        self.outcome, self.timings, self.cost = outcome, timings or {}, cost
        self.raises, self.sleep_s, self.during = raises, sleep_s, during
        self.calls = 0

    def review(self, repo: str, number: int, *, dry_run: bool,
               expected_head: str | None = None,
               should_post: Callable[[], bool] | None = None) -> SimpleNamespace:
        self.calls += 1
        time.sleep(self.sleep_s)
        if self.during is not None:
            self.during()
        if self.raises is not None:
            raise self.raises
        return SimpleNamespace(outcome=self.outcome, timings=self.timings, cost_usd=self.cost)


# --- lease heartbeat -------------------------------------------------------------------------

def test_heartbeat_beat_extends_the_lease(backend: Backend, clock: FakeClock) -> None:
    queue = backend.queue(clock=clock, lease_s=90)
    queue.enqueue("a/b", 1, "h1")
    job = queue.claim()
    assert job is not None
    hb = LeaseHeartbeat(queue, job)
    assert hb.interval_s == 30.0
    for _ in range(4):  # 4 beats, 30s apart: 120s in, well past the first 90s lease
        clock.now += 30
        assert hb.beat() is True
    assert (hb.beats, hb.lost) == (4, False)
    assert queue.claim() is None
    queue.complete(job)


def test_heartbeat_stops_and_logs_when_the_lease_is_lost(backend: Backend, clock: FakeClock,
                                                         caplog: pytest.LogCaptureFixture) -> None:
    queue = backend.queue(clock=clock, lease_s=10)
    queue.enqueue("a/b", 1, "h1")
    job = queue.claim()
    assert job is not None
    clock.now += 11
    assert queue.claim() is not None  # another worker takes it
    caplog.set_level(logging.ERROR, logger="reviewhound.heartbeat")
    with LeaseHeartbeat(queue, job, interval_s=0.01) as hb:
        deadline = time.monotonic() + 5
        while not hb.lost and time.monotonic() < deadline:
            time.sleep(0.01)
        assert hb._thread is not None
        hb._thread.join(2)
        assert not hb._thread.is_alive()  # stopped by itself, before __exit__
    assert (hb.lost, hb.beats) == (True, 0)
    events = [(r.getMessage(), getattr(r, "event", None), getattr(r, "job", None))
              for r in caplog.records]
    assert events == [("lease lost while the job was running", "lease_lost", job.id)]


class FlakyLeaseQueue:
    lease_s = 30.0

    def __init__(self, errors: list[BaseException]) -> None:
        self.errors = errors
        self.calls = 0

    def extend_lease(self, job: Job) -> float:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return 0.0


def test_heartbeat_keeps_going_after_a_transient_db_error() -> None:
    job = Job(1, "a/b", 1, "h1", "running", 1, 3, None, "t")
    q = FlakyLeaseQueue([OSError("db busy")])
    hb = LeaseHeartbeat(q, job)
    assert hb.beat() is True
    assert hb.beat() is True
    assert (q.calls, hb.beats, hb.lost) == (2, 1, False)
    q.errors = [LeaseLost(1)]
    assert hb.beat() is False and hb.lost is True


def test_long_review_keeps_its_lease_against_a_second_worker(backend: Backend) -> None:
    queue = backend.queue(lease_s=0.6)
    other = backend.queue(lease_s=0.6)
    queue.enqueue("a/b", 1, "h1")
    stolen: list[Job | None] = []
    reviewer = StubReviewer(sleep_s=1.5, during=lambda: stolen.append(other.claim()))
    job = run_one(queue, reviewer)  # type: ignore[arg-type]
    assert job is not None
    assert stolen == [None]
    done = queue.get(job.id)
    assert done is not None and (done.state, done.attempts) == ("done", 1)


# --- metrics recorded by the worker ------------------------------------------------------------

def test_worker_records_stage_time_cost_outcome_and_failures(backend: Backend) -> None:
    queue = backend.queue(backoff_base_s=0)
    metrics = backend.metrics()
    queue.enqueue("a/b", 1, "h1")
    queue.enqueue("a/b", 2, "h1")
    ok = StubReviewer("posted", {"fetch": 0.25, "model": 1.5}, cost=0.02)
    assert run_one(queue, ok, metrics) is not None  # type: ignore[arg-type]
    down = StubReviewer(raises=ProviderError("model down"))
    failed = run_one(queue, down, metrics)  # type: ignore[arg-type]
    assert failed is not None
    after = queue.get(failed.id)
    assert after is not None and after.state == "queued"
    shadow = StubReviewer("shadow", {"model": 0.5}, cost=0.01)
    assert run_one(queue, shadow, metrics) is not None  # type: ignore[arg-type]
    assert metrics.counters() == {
        ("reviews_total", "posted"): 1.0, ("reviews_total", "shadow"): 1.0,
        ("review_cost_usd_total", ""): pytest.approx(0.03),
        ("review_stage_seconds_sum", "fetch"): 0.25, ("review_stage_seconds_count", "fetch"): 1.0,
        ("review_stage_seconds_sum", "model"): 2.0, ("review_stage_seconds_count", "model"): 2.0,
        ("jobs_failed_total", "ProviderError"): 1.0}


class BrokenMetrics:
    def add(self, increments: Any) -> None:
        raise OSError("disk full")

    def counters(self) -> dict[tuple[str, str], float]:
        return {}


def test_metrics_outage_does_not_fail_the_job(tmp_path: Path,
                                              caplog: pytest.LogCaptureFixture) -> None:
    queue = SQLiteQueue(tmp_path / "q.db")
    queue.enqueue("a/b", 1, "h1")
    caplog.set_level(logging.ERROR, logger="reviewhound.worker")
    job = run_one(queue, StubReviewer(), BrokenMetrics())  # type: ignore[arg-type]
    assert job is not None
    done = queue.get(job.id)
    assert done is not None and done.state == "done"
    assert [(r.getMessage(), getattr(r, "error", None)) for r in caplog.records] == [
        ("metrics write failed", "OSError")]


# --- prometheus text, /metrics, stats -------------------------------------------------------

SNAP = Snapshot(
    counters={("reviews_total", "posted"): 3.0, ("reviews_total", "shadow"): 1.0,
              ("review_stage_seconds_sum", "model"): 4.5,
              ("review_stage_seconds_count", "model"): 3.0,
              ("review_cost_usd_total", ""): 0.125,
              ("jobs_failed_total", 'Weird"Error'): 2.0},
    backlog={"queued": 2, "running": 1, "done": 4, "dead": 0, "superseded": 0},
    oldest_queued_age_s=700.0, stuck=1, stuck_after_s=600.0)


def test_prometheus_text_is_exact() -> None:
    assert render_prometheus(SNAP) == (
        "# HELP reviews_total Review jobs finished, by outcome.\n"
        "# TYPE reviews_total counter\n"
        'reviews_total{outcome="posted"} 3\n'
        'reviews_total{outcome="shadow"} 1\n'
        "# HELP review_stage_seconds Wall time per review stage.\n"
        "# TYPE review_stage_seconds summary\n"
        'review_stage_seconds_sum{stage="model"} 4.5\n'
        'review_stage_seconds_count{stage="model"} 3\n'
        "# HELP review_cost_usd_total Model cost of all reviews in US dollars.\n"
        "# TYPE review_cost_usd_total counter\n"
        "review_cost_usd_total 0.125\n"
        "# HELP jobs_failed_total Failed job attempts, by error type.\n"
        "# TYPE jobs_failed_total counter\n"
        'jobs_failed_total{error="Weird\\"Error"} 2\n'
        "# HELP queue_backlog Jobs in the queue, by state.\n"
        "# TYPE queue_backlog gauge\n"
        'queue_backlog{state="queued"} 2\n'
        'queue_backlog{state="running"} 1\n'
        'queue_backlog{state="done"} 4\n'
        'queue_backlog{state="dead"} 0\n'
        'queue_backlog{state="superseded"} 0\n'
        "# HELP oldest_queued_age_seconds Age of the oldest queued job.\n"
        "# TYPE oldest_queued_age_seconds gauge\n"
        "oldest_queued_age_seconds 700\n"
        "# HELP queue_stuck_jobs Queued jobs older than the backlog alarm threshold.\n"
        "# TYPE queue_stuck_jobs gauge\n"
        "queue_stuck_jobs 1\n"
        "# HELP backlog_alarm 1 when any queued job is older than the threshold.\n"
        "# TYPE backlog_alarm gauge\n"
        "backlog_alarm 1\n")


def test_stats_text_shows_the_same_numbers() -> None:
    assert render_stats(SNAP).splitlines() == [
        "reviews by outcome:",
        "  posted           3",
        "  shadow           1",
        "time per stage (seconds): total / count / mean",
        "  model            4.500 / 3 / 1.500",
        "cost (USD): 0.1250",
        "failed attempts by error:",
        '  Weird"Error      2',
        "queue: queued=2, running=1, done=4, dead=0, superseded=0",
        "oldest queued age (s): 700.0",
        "stuck (queued > 600s): 1  BACKLOG ALARM"]


def test_collect_reads_queue_and_counters(backend: Backend, clock: FakeClock) -> None:
    queue = backend.queue(clock=clock)
    metrics = backend.metrics()
    queue.enqueue("a/b", 1, "h1")
    clock.now += 601
    queue.enqueue("a/b", 2, "h1")
    metrics.add([("reviews_total", "posted", 1.0)])
    snap = collect(queue, metrics, stuck_after_s=600)
    assert snap == Snapshot(counters={("reviews_total", "posted"): 1.0},
                            backlog={"queued": 2, "running": 0, "done": 0, "dead": 0,
                                     "superseded": 0},
                            oldest_queued_age_s=601.0, stuck=1, stuck_after_s=600)


def _get(port: int, path: str) -> tuple[int, str, str]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path)
    resp = conn.getresponse()
    return resp.status, resp.getheader("Content-Type") or "", resp.read().decode()


def _serve(tmp_path: Path, render: Any) -> Any:
    server = make_server(SQLiteQueue(tmp_path / "q.db"), "s", "127.0.0.1", 0, metrics=render)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_metrics_endpoint_serves_prometheus_text(tmp_path: Path) -> None:
    server = _serve(tmp_path, lambda: render_prometheus(SNAP))
    try:
        status, ctype, body = _get(server.server_address[1], "/metrics")
        assert (status, ctype) == (200, "text/plain; version=0.0.4; charset=utf-8")
        assert body == render_prometheus(SNAP)
        assert _get(server.server_address[1], "/healthz")[0] == 200
    finally:
        server.shutdown()
        server.server_close()


def test_metrics_endpoint_failure_is_a_500_and_server_keeps_serving(tmp_path: Path) -> None:
    def boom() -> str:
        raise OSError("db gone")

    server = _serve(tmp_path, boom)
    try:
        status, _, body = _get(server.server_address[1], "/metrics")
        assert (status, body) == (500, '{"message": "metrics unavailable"}')
        assert _get(server.server_address[1], "/healthz")[0] == 200
    finally:
        server.shutdown()
        server.server_close()


def test_metrics_endpoint_absent_without_a_renderer(tmp_path: Path) -> None:
    server = make_server(SQLiteQueue(tmp_path / "q.db"), "s", "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert _get(server.server_address[1], "/metrics")[0] == 404
    finally:
        server.shutdown()
        server.server_close()


def test_stats_command_prints_recorded_numbers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                               capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.delenv("REVIEWHOUND_DATABASE_URL", raising=False)
    SQLiteQueue(tmp_path / "queue.db").enqueue("a/b", 1, "h1")
    SQLiteMetrics(tmp_path / "queue.db").add([("reviews_total", "posted", 2.0),
                                              ("review_cost_usd_total", "", 0.5)])
    assert main(["--home", str(tmp_path), "stats"]) == 0
    out = capsys.readouterr().out
    assert "  posted           2\n" in out
    assert "cost (USD): 0.5000\n" in out
    assert "queue: queued=1, running=0, done=0, dead=0, superseded=0\n" in out
    assert main(["--home", str(tmp_path), "stats", "--prometheus"]) == 0
    prom = capsys.readouterr().out
    assert 'reviews_total{outcome="posted"} 2\n' in prom
    assert 'queue_backlog{state="queued"} 1\n' in prom


# --- backlog alarm ----------------------------------------------------------------------------

def test_backlog_alarm_fires_once_per_interval(backend: Backend, clock: FakeClock,
                                               caplog: pytest.LogCaptureFixture) -> None:
    queue = backend.queue(clock=clock)
    mono = FakeClock(0.0)
    alarm = BacklogAlarm(queue, older_than_s=600, interval_s=300, clock=mono)
    caplog.set_level(logging.ERROR, logger="reviewhound.metrics")
    queue.enqueue("a/b", 1, "h1")
    assert (alarm.check(), alarm.stuck, alarm.active) == (False, 0, False)
    clock.now += 601
    assert (alarm.check(), alarm.stuck, alarm.active) == (True, 1, True)
    mono.now += 299
    assert alarm.check() is False  # still stuck, but inside the interval
    assert alarm.active is True
    mono.now += 1
    assert alarm.check() is True
    fired = [(r.getMessage(), r.levelname, getattr(r, "event", None), getattr(r, "stuck", None),
              getattr(r, "older_than_s", None)) for r in caplog.records]
    assert fired == [("backlog_alarm", "ERROR", "backlog_alarm", 1, 600)] * 2
    job = queue.claim()
    assert job is not None
    assert (alarm.check(), alarm.stuck, alarm.active) == (False, 0, False)


class CountingAlarm:
    def __init__(self, fail: bool) -> None:
        self.fail, self.calls = fail, 0

    def check(self) -> bool:
        self.calls += 1
        if self.fail:
            raise OSError("db gone")
        return False


def test_run_forever_checks_the_alarm_and_survives_its_errors(tmp_path: Path) -> None:
    queue: WorkerQueue = SQLiteQueue(tmp_path / "q.db")
    for fail in (False, True):
        alarm = CountingAlarm(fail)
        loops = iter([False, False, True])
        run_forever(queue, StubReviewer(), idle_sleep_s=0,  # type: ignore[arg-type]
                    should_stop=lambda it=loops: next(it), alarm=alarm)  # type: ignore[arg-type]
        assert alarm.calls == 2
