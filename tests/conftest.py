from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from reviewhound.metrics import MetricsLog, SQLiteMetrics
from reviewhound.queue import SQLiteQueue, WorkerQueue
from reviewhound.store import ReviewStore, Store
from reviewhound.vcs.base import PullRequest


class FakeClock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


PG_URL = os.environ.get("REVIEWHOUND_TEST_PG", "")
_PG_TABLES = ("jobs, reviews, findings, posts, feedback, metric_counters, comment_links, "
              "dismissals, feedback_events, review_findings, review_blockers")


@pytest.fixture(params=["sqlite", "postgres"])
def backend(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Backend]:
    """Every behavioural queue/store/metrics test runs once per backend, so they cannot drift."""
    if request.param == "sqlite":
        yield Backend("sqlite", tmp_path, None)
        return
    if not PG_URL:
        pytest.skip("set REVIEWHOUND_TEST_PG=postgresql://... to run the Postgres backend")
    import psycopg

    with psycopg.connect(PG_URL, autocommit=True) as c:
        c.execute(f"DROP TABLE IF EXISTS {_PG_TABLES}")
    yield Backend("postgres", tmp_path, PG_URL)


class Backend:
    def __init__(self, name: str, home: Path, url: str | None) -> None:
        self.name = name
        self.home = home
        self.url = url

    def queue(self, **kw: Any) -> WorkerQueue:
        if self.url is None:
            return SQLiteQueue(self.home / "q.db", **kw)
        from reviewhound.backends.postgres import PostgresQueue

        return PostgresQueue(self.url, **kw)

    def store(self, **kw: Any) -> ReviewStore:
        if self.url is None:
            return Store(self.home / "s.db", **kw)
        from reviewhound.backends.postgres import PostgresStore

        return PostgresStore(self.url, **kw)

    def metrics(self) -> MetricsLog:
        if self.url is None:
            return SQLiteMetrics(self.home / "q.db")
        from reviewhound.backends.postgres import PostgresMetrics

        return PostgresMetrics(self.url)


def make_pr(head: str = "h1", base: str = "b1", **kw: Any) -> PullRequest:
    return PullRequest(repo=kw.pop("repo", "acme/shop"), number=kw.pop("number", 7),
                       title=kw.pop("title", "Add delete endpoint"), body=kw.pop("body", ""),
                       base_sha=base, head_sha=head, **kw)


def answer(*findings: dict[str, Any]) -> str:
    return json.dumps({"findings": list(findings)})


class SpyConn:
    """Wraps a store connection: records each statement, and can fail one that matches."""

    def __init__(self, conn: Any, log: list[tuple[str, Any]], fail_on: tuple[str, ...]) -> None:
        self._c, self._log, self._fail_on = conn, log, fail_on

    def execute(self, sql: str, *a: Any, **k: Any) -> Any:
        if any(marker in sql for marker in self._fail_on):
            raise RuntimeError("injected failure")
        self._log.append((sql, a[0] if a else ()))
        return self._c.execute(sql, *a, **k)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._c, name)


def spy(monkeypatch: pytest.MonkeyPatch, store: Any,
        *fail_on: str) -> list[tuple[str, Any]]:
    log: list[tuple[str, Any]] = []
    real = store._tx

    @contextmanager
    def tx() -> Iterator[Any]:
        with real() as c:
            yield SpyConn(c, log, fail_on)
    monkeypatch.setattr(store, "_tx", tx)
    return log
