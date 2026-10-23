"""Job metrics: durable counters per job, queue gauges, Prometheus text and a backlog alarm.

Counters live in the database, not in process memory, so the webhook server can serve numbers
that the worker recorded.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)

JOB_STATES = ("queued", "running", "done", "dead", "superseded")
REVIEWS = "reviews_total"
STAGE_SUM = "review_stage_seconds_sum"
STAGE_COUNT = "review_stage_seconds_count"
COST = "review_cost_usd_total"
FAILED = "jobs_failed_total"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS metric_counters (
    name TEXT NOT NULL, label TEXT NOT NULL, value REAL NOT NULL,
    PRIMARY KEY (name, label)
);
"""

Increment = tuple[str, str, float]


class MetricsLog(Protocol):
    def add(self, increments: Sequence[Increment]) -> None: ...

    def counters(self) -> dict[tuple[str, str], float]: ...


class BacklogSource(Protocol):
    def state_counts(self) -> dict[str, int]: ...

    def oldest_queued_age(self) -> float: ...

    def stuck_count(self, older_than_s: float) -> int: ...


def review_increments(outcome: str, timings: dict[str, float], cost_usd: float) -> list[Increment]:
    """Counters for one job that produced a review result (any outcome)."""
    out: list[Increment] = [(REVIEWS, outcome, 1.0), (COST, "", float(cost_usd))]
    for stage, seconds in timings.items():
        out += [(STAGE_SUM, stage, float(seconds)), (STAGE_COUNT, stage, 1.0)]
    return out


def failure_increments(error_type: str) -> list[Increment]:
    return [(FAILED, error_type, 1.0)]


class SQLiteMetrics:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        with self._tx() as c:
            c.executescript(_SCHEMA)

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
            except BaseException:
                c.execute("ROLLBACK")
                raise
            c.execute("COMMIT")
        finally:
            c.close()

    def add(self, increments: Sequence[Increment]) -> None:
        with self._tx() as c:
            c.executemany("INSERT INTO metric_counters (name, label, value) VALUES (?,?,?) "
                          "ON CONFLICT (name, label) DO UPDATE "
                          "SET value = metric_counters.value + excluded.value",
                          sorted(increments))

    def counters(self) -> dict[tuple[str, str], float]:
        with self._tx() as c:
            rows = c.execute("SELECT name, label, value FROM metric_counters").fetchall()
        return {(str(n), str(lbl)): float(v) for n, lbl, v in rows}


@dataclass(frozen=True)
class Snapshot:
    counters: dict[tuple[str, str], float]
    backlog: dict[str, int]
    oldest_queued_age_s: float
    stuck: int
    stuck_after_s: float

    def series(self, name: str) -> dict[str, float]:
        return {lbl: v for (n, lbl), v in sorted(self.counters.items()) if n == name}


def collect(queue: BacklogSource, metrics: MetricsLog, stuck_after_s: float) -> Snapshot:
    counts = queue.state_counts()
    return Snapshot(counters=metrics.counters(),
                    backlog={s: counts.get(s, 0) for s in JOB_STATES},
                    oldest_queued_age_s=queue.oldest_queued_age(),
                    stuck=queue.stuck_count(stuck_after_s), stuck_after_s=stuck_after_s)


def _num(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else repr(float(v))


def _esc(label: str) -> str:
    return label.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def render_prometheus(s: Snapshot) -> str:
    lines: list[str] = []

    def family(name: str, kind: str, help_: str) -> None:
        lines.extend([f"# HELP {name} {help_}", f"# TYPE {name} {kind}"])

    family(REVIEWS, "counter", "Review jobs finished, by outcome.")
    lines += [f'{REVIEWS}{{outcome="{_esc(k)}"}} {_num(v)}' for k, v in s.series(REVIEWS).items()]
    family("review_stage_seconds", "summary", "Wall time per review stage.")
    for stage, total in s.series(STAGE_SUM).items():
        lines.append(f'{STAGE_SUM}{{stage="{_esc(stage)}"}} {_num(total)}')
        lines.append(f'{STAGE_COUNT}{{stage="{_esc(stage)}"}} '
                     f'{_num(s.counters.get((STAGE_COUNT, stage), 0.0))}')
    family(COST, "counter", "Model cost of all reviews in US dollars.")
    lines.append(f"{COST} {_num(s.counters.get((COST, ''), 0.0))}")
    family(FAILED, "counter", "Failed job attempts, by error type.")
    lines += [f'{FAILED}{{error="{_esc(k)}"}} {_num(v)}' for k, v in s.series(FAILED).items()]
    family("queue_backlog", "gauge", "Jobs in the queue, by state.")
    lines += [f'queue_backlog{{state="{st}"}} {n}' for st, n in s.backlog.items()]
    family("oldest_queued_age_seconds", "gauge", "Age of the oldest queued job.")
    lines.append(f"oldest_queued_age_seconds {_num(round(s.oldest_queued_age_s, 3))}")
    family("queue_stuck_jobs", "gauge", "Queued jobs older than the backlog alarm threshold.")
    lines.append(f"queue_stuck_jobs {s.stuck}")
    family("backlog_alarm", "gauge", "1 when any queued job is older than the threshold.")
    lines.append(f"backlog_alarm {1 if s.stuck > 0 else 0}")
    return "\n".join(lines) + "\n"


def render_stats(s: Snapshot) -> str:
    out = ["reviews by outcome:"]
    out += [f"  {k:<16} {_num(v)}" for k, v in s.series(REVIEWS).items()] or ["  (none)"]
    out.append("time per stage (seconds): total / count / mean")
    stages = s.series(STAGE_SUM)
    for stage, total in stages.items():
        n = s.counters.get((STAGE_COUNT, stage), 0.0)
        mean = total / n if n else 0.0
        out.append(f"  {stage:<16} {total:.3f} / {_num(n)} / {mean:.3f}")
    if not stages:
        out.append("  (none)")
    out.append(f"cost (USD): {s.counters.get((COST, ''), 0.0):.4f}")
    out.append("failed attempts by error:")
    out += [f"  {k:<16} {_num(v)}" for k, v in s.series(FAILED).items()] or ["  (none)"]
    out.append("queue: " + ", ".join(f"{k}={v}" for k, v in s.backlog.items()))
    out.append(f"oldest queued age (s): {s.oldest_queued_age_s:.1f}")
    out.append(f"stuck (queued > {_num(s.stuck_after_s)}s): {s.stuck}"
               + ("  BACKLOG ALARM" if s.stuck else ""))
    return "\n".join(out)


class BacklogAlarm:
    """Log one `backlog_alarm` error per interval while queued jobs are older than the limit."""

    def __init__(self, queue: BacklogSource, older_than_s: float = 600, interval_s: float = 300,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.queue = queue
        self.older_than_s = older_than_s
        self.interval_s = interval_s
        self._clock = clock
        self._last_fired: float | None = None
        self.stuck = 0  # gauge: last observed count

    @property
    def active(self) -> bool:
        return self.stuck > 0

    def check(self) -> bool:
        """Read the backlog; return True when this call logged the alarm."""
        self.stuck = self.queue.stuck_count(self.older_than_s)
        if self.stuck == 0:
            return False
        now = self._clock()
        if self._last_fired is not None and now - self._last_fired < self.interval_s:
            return False
        self._last_fired = now
        log.error("backlog_alarm", extra={"event": "backlog_alarm", "stuck": self.stuck,
                                          "older_than_s": self.older_than_s})
        return True
