"""Durable job queue. One job per (repo, PR, head commit), so a repeated event is a no-op."""

from __future__ import annotations

import secrets
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    repo TEXT NOT NULL,
    pr INTEGER NOT NULL,
    head_sha TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('queued','running','done','dead','superseded')),
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL,
    run_after REAL NOT NULL,
    lease_until REAL,
    lease_token TEXT,
    last_error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_ready ON jobs(state, run_after);
CREATE INDEX IF NOT EXISTS jobs_pr ON jobs(repo, pr);
"""


@dataclass(frozen=True)
class Job:
    id: int
    repo: str
    pr: int
    head_sha: str
    state: str
    attempts: int
    max_attempts: int
    last_error: str | None
    lease_token: str | None = None


class JobQueue(Protocol):
    def enqueue(self, repo: str, pr: int, head_sha: str) -> tuple[int, bool]: ...

    def claim(self) -> Job | None: ...

    def complete(self, job: Job) -> None: ...

    def fail(self, job: Job, error: str, retryable: bool) -> str: ...

    def dead_jobs(self) -> list[Job]: ...


class LeaseLost(RuntimeError):
    """This worker no longer owns the job; its result must not be recorded."""


def job_key(repo: str, pr: int, head_sha: str) -> str:
    return f"{repo.lower()}#{pr}@{head_sha}"


class SQLiteQueue:
    def __init__(self, path: Path | str, max_attempts: int = 4, lease_s: float = 1800,
                 backoff_base_s: float = 30, clock: Callable[[], float] = time.time) -> None:
        self.path = str(path)
        self.max_attempts = max_attempts
        self.lease_s = lease_s
        self.backoff_base_s = backoff_base_s
        self._clock = clock
        with self._conn() as c:
            c.executescript(_SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        try:
            c.execute("PRAGMA journal_mode=WAL")
            yield c
        finally:
            c.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
            except BaseException:
                c.execute("ROLLBACK")
                raise
            c.execute("COMMIT")

    @staticmethod
    def _job(row: sqlite3.Row) -> Job:
        return Job(id=row["id"], repo=row["repo"], pr=row["pr"], head_sha=row["head_sha"],
                   state=row["state"], attempts=row["attempts"],
                   max_attempts=row["max_attempts"], last_error=row["last_error"],
                   lease_token=row["lease_token"])

    def enqueue(self, repo: str, pr: int, head_sha: str) -> tuple[int, bool]:
        """Return (job id, created). Older queued jobs for the same PR are superseded."""
        repo = repo.lower()
        now = self._clock()
        key = job_key(repo, pr, head_sha)
        with self._tx() as c:
            c.execute("UPDATE jobs SET state='superseded', updated_at=? "
                      "WHERE repo=? AND pr=? AND state='queued' AND key != ?",
                      (now, repo, pr, key))
            row = c.execute("SELECT id, state FROM jobs WHERE key = ?", (key,)).fetchone()
            if row is not None:
                if row["state"] != "superseded":
                    return int(row["id"]), False
                # The PR came back to a head that was skipped earlier: review it now.
                c.execute("UPDATE jobs SET state='queued', attempts=0, run_after=?, updated_at=? "
                          "WHERE id=?", (now, now, row["id"]))
                return int(row["id"]), True
            cur = c.execute(
                "INSERT INTO jobs (key, repo, pr, head_sha, state, max_attempts, run_after, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?)",
                (key, repo, pr, head_sha, self.max_attempts, now, now, now))
            return int(cur.lastrowid or 0), True

    def claim(self) -> Job | None:
        """Take the next ready job, or one whose worker died (lease expired)."""
        now = self._clock()
        with self._tx() as c:
            c.execute("UPDATE jobs SET state='dead', last_error='lease expired on final attempt', "
                      "lease_until=NULL, updated_at=? WHERE state='running' AND lease_until < ? "
                      "AND attempts >= max_attempts", (now, now))
            row = c.execute(
                "SELECT id FROM jobs WHERE (state='queued' AND run_after <= ?) "
                "OR (state='running' AND lease_until < ?) ORDER BY run_after, id LIMIT 1",
                (now, now)).fetchone()
            if row is None:
                return None
            c.execute("UPDATE jobs SET state='running', attempts=attempts+1, lease_until=?, "
                      "lease_token=?, updated_at=? WHERE id=?",
                      (now + self.lease_s, secrets.token_hex(8), now, row["id"]))
            claimed = c.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
            return self._job(claimed)

    def complete(self, job: Job) -> None:
        """Mark done. Raises LeaseLost if another worker took the job after our lease ran out."""
        with self._tx() as c:
            cur = c.execute("UPDATE jobs SET state='done', lease_until=NULL, updated_at=? "
                            "WHERE id=? AND state='running' AND lease_token=?",
                            (self._clock(), job.id, job.lease_token))
            if cur.rowcount != 1:
                raise LeaseLost(job.id)

    def fail(self, job: Job, error: str, retryable: bool) -> str:
        """Record a failure. Returns the new state: 'queued' (will retry) or 'dead'."""
        now = self._clock()
        job_id = job.id
        with self._tx() as c:
            row = c.execute("SELECT attempts, max_attempts FROM jobs WHERE id=? "
                            "AND state='running' AND lease_token=?",
                            (job_id, job.lease_token)).fetchone()
            if row is None:
                raise LeaseLost(job_id)
            if retryable and row["attempts"] < row["max_attempts"]:
                state = "queued"
                run_after = now + self.backoff_base_s * (2 ** (row["attempts"] - 1))
            else:
                state, run_after = "dead", now
            c.execute("UPDATE jobs SET state=?, run_after=?, lease_until=NULL, last_error=?, "
                      "updated_at=? WHERE id=?", (state, run_after, error[:500], now, job_id))
            return state

    def get(self, job_id: int) -> Job | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._job(row) if row else None

    def dead_jobs(self) -> list[Job]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM jobs WHERE state='dead' ORDER BY id").fetchall()
        return [self._job(r) for r in rows]

    def requeue(self, job_id: int) -> bool:
        with self._tx() as c:
            cur = c.execute("UPDATE jobs SET state='queued', attempts=0, run_after=?, "
                            "updated_at=? WHERE id=? AND state='dead'",
                            (self._clock(), self._clock(), job_id))
        return cur.rowcount == 1

    def stuck_count(self, older_than_s: float) -> int:
        """Queued jobs waiting longer than `older_than_s`: the backlog alarm reads this."""
        with self._conn() as c:
            row = c.execute("SELECT COUNT(*) AS n FROM jobs WHERE state='queued' "
                            "AND created_at < ?", (self._clock() - older_than_s,)).fetchone()
        return int(row["n"])
