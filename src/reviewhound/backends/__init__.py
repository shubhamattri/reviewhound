"""Pick the storage backend from REVIEWHOUND_DATABASE_URL: Postgres if set, SQLite files otherwise."""

from __future__ import annotations

import os
from pathlib import Path

from reviewhound.metrics import MetricsLog, SQLiteMetrics
from reviewhound.queue import SQLiteQueue, WorkerQueue
from reviewhound.store import ReviewStore, Store

ENV_URL = "REVIEWHOUND_DATABASE_URL"
_PG_SCHEMES = ("postgresql://", "postgres://")


class BackendError(ValueError):
    pass


def database_url(explicit: str | None = None) -> str | None:
    """The configured Postgres URL, or None for SQLite. Never echoes the URL: it holds a password."""
    url = (explicit if explicit is not None else os.environ.get(ENV_URL, "")).strip()
    if not url:
        return None
    if not url.startswith(_PG_SCHEMES):
        raise BackendError(f"{ENV_URL} must start with postgresql:// (or be empty for SQLite)")
    return url


def open_queue(home: Path, url: str | None = None) -> WorkerQueue:
    pg = database_url(url)
    if pg is None:
        home.mkdir(parents=True, exist_ok=True)
        return SQLiteQueue(home / "queue.db")
    from reviewhound.backends.postgres import PostgresQueue

    return PostgresQueue(pg)


def open_store(home: Path, url: str | None = None) -> ReviewStore:
    pg = database_url(url)
    if pg is None:
        home.mkdir(parents=True, exist_ok=True)
        return Store(home / "reviewhound.db")
    from reviewhound.backends.postgres import PostgresStore

    return PostgresStore(pg)


def open_metrics(home: Path, url: str | None = None) -> MetricsLog:
    # Kept beside the queue: the webhook server only opens the queue database.
    pg = database_url(url)
    if pg is None:
        home.mkdir(parents=True, exist_ok=True)
        return SQLiteMetrics(home / "queue.db")
    from reviewhound.backends.postgres import PostgresMetrics

    return PostgresMetrics(pg)
