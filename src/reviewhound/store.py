"""Review state in SQLite: reviews, findings with stable identity, and developer feedback."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from reviewhound.findings import Finding

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo TEXT NOT NULL, pr INTEGER NOT NULL, head_sha TEXT NOT NULL,
    outcome TEXT NOT NULL,
    tier TEXT, model TEXT, providers TEXT, event TEXT,
    posted INTEGER NOT NULL DEFAULT 0, post_id TEXT,
    cost_usd REAL NOT NULL DEFAULT 0, timings TEXT, error TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS reviews_pr ON reviews(repo, pr);
CREATE TABLE IF NOT EXISTS findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo TEXT NOT NULL, pr INTEGER NOT NULL, fingerprint TEXT NOT NULL,
    first_head TEXT NOT NULL, last_head TEXT NOT NULL,
    file TEXT NOT NULL, line INTEGER NOT NULL, severity INTEGER NOT NULL,
    category TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
    source TEXT NOT NULL, verified INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN
        ('stored','held','posted','resolved','declined','fixed','reopened')),
    held_reason TEXT,
    created_at REAL NOT NULL, updated_at REAL NOT NULL,
    UNIQUE (repo, pr, fingerprint)
);
CREATE TABLE IF NOT EXISTS posts (
    repo TEXT NOT NULL, pr INTEGER NOT NULL, head_sha TEXT NOT NULL, created_at REAL NOT NULL,
    PRIMARY KEY (repo, pr, head_sha)
);
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo TEXT NOT NULL, pr INTEGER NOT NULL, fingerprint TEXT NOT NULL,
    kind TEXT NOT NULL, detail TEXT, created_at REAL NOT NULL
);
"""

FEEDBACK_KINDS = {"resolved", "fixed", "declined", "reply", "reopened"}


@dataclass(frozen=True)
class ReviewRecord:
    repo: str
    pr: int
    head_sha: str
    outcome: str  # posted | shadow | dry-run | skipped | head-moved | failed | post-uncertain
    tier: str = ""
    model: str = ""
    providers: tuple[str, ...] = ()
    event: str = ""
    posted: bool = False
    post_id: str = ""
    cost_usd: float = 0.0
    timings: dict[str, float] | None = None
    error: str = ""


class ReviewStore(Protocol):
    """What the pipeline and CLI need from a store; SQLite and Postgres both provide it."""

    def known_state(self, repo: str, pr: int) -> dict[str, str]: ...

    def record_review(self, rec: ReviewRecord, posted: list[Finding],
                      held: list[tuple[Finding, str]]) -> int: ...

    def claim_post(self, repo: str, pr: int, head_sha: str) -> bool: ...

    def add_feedback(self, repo: str, pr: int, fingerprint: str, kind: str,
                     detail: str = "") -> bool: ...

    def reviews(self, repo: str, pr: int) -> list[dict[str, Any]]: ...

    def findings(self, repo: str, pr: int) -> list[dict[str, Any]]: ...


class Store:
    def __init__(self, path: Path | str, clock: Callable[[], float] = time.time) -> None:
        self.path = str(path)
        self._clock = clock
        c = sqlite3.connect(self.path, timeout=30)
        try:
            c.executescript(_SCHEMA)
        finally:
            c.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
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

    def known_state(self, repo: str, pr: int) -> dict[str, str]:
        with self._tx() as c:
            rows = c.execute("SELECT fingerprint, state FROM findings WHERE repo=? AND pr=?",
                             (repo.lower(), pr)).fetchall()
        return {r["fingerprint"]: r["state"] for r in rows}

    def record_review(self, rec: ReviewRecord, posted: list[Finding],
                      held: list[tuple[Finding, str]]) -> int:
        """Write the review and every finding in one transaction."""
        now = self._clock()
        repo = rec.repo.lower()
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO reviews (repo, pr, head_sha, outcome, tier, model, providers, event, "
                "posted, post_id, cost_usd, timings, error, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (repo, rec.pr, rec.head_sha, rec.outcome, rec.tier, rec.model,
                 json.dumps(list(rec.providers)), rec.event, int(rec.posted), rec.post_id,
                 rec.cost_usd, json.dumps(rec.timings or {}), rec.error, now))
            new_state = "posted" if rec.posted else "stored"
            for f in posted:
                self._upsert(c, repo, rec.pr, rec.head_sha, f, new_state, None, now)
            for f, reason in held:
                self._upsert(c, repo, rec.pr, rec.head_sha, f, "held", reason, now)
            return int(cur.lastrowid or 0)

    @staticmethod
    def _upsert(c: sqlite3.Connection, repo: str, pr: int, head: str, f: Finding, state: str,
                reason: str | None, now: float) -> None:
        # A re-review never overwrites developer feedback or the fact that a comment is live.
        # 'fixed' is not kept: a fixed finding that comes back is posted again, once.
        c.execute(
            "INSERT INTO findings (repo, pr, fingerprint, first_head, last_head, file, line, "
            "severity, category, title, body, source, verified, state, held_reason, created_at, "
            "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT (repo, pr, fingerprint) DO UPDATE SET last_head=excluded.last_head, "
            "line=excluded.line, updated_at=excluded.updated_at, "
            "state=CASE WHEN findings.state IN ('declined','resolved','posted','reopened') "
            "THEN findings.state ELSE excluded.state END, "
            "held_reason=CASE WHEN findings.state IN ('declined','resolved','posted','reopened') "
            "THEN findings.held_reason ELSE excluded.held_reason END",
            (repo, pr, f.fingerprint, head, head, f.file, f.line, int(f.severity), f.category,
             f.title, f.body, f.source, int(f.verified), state, reason, now, now))

    def claim_post(self, repo: str, pr: int, head_sha: str) -> bool:
        """At most one post per head, even if two workers or a retry reach the post step."""
        with self._tx() as c:
            cur = c.execute("INSERT OR IGNORE INTO posts (repo, pr, head_sha, created_at) "
                            "VALUES (?,?,?,?)", (repo.lower(), pr, head_sha, self._clock()))
            return cur.rowcount == 1

    def add_feedback(self, repo: str, pr: int, fingerprint: str, kind: str,
                     detail: str = "") -> bool:
        if kind not in FEEDBACK_KINDS:
            raise ValueError(f"unknown feedback kind {kind!r}")
        now = self._clock()
        with self._tx() as c:
            exists = c.execute("SELECT 1 FROM findings WHERE repo=? AND pr=? AND fingerprint=?",
                               (repo.lower(), pr, fingerprint)).fetchone()
            if exists is None:
                return False
            c.execute("INSERT INTO feedback (repo, pr, fingerprint, kind, detail, created_at) "
                      "VALUES (?,?,?,?,?,?)", (repo.lower(), pr, fingerprint, kind, detail, now))
            if kind != "reply":
                c.execute("UPDATE findings SET state=?, updated_at=? WHERE repo=? AND pr=? "
                          "AND fingerprint=?", (kind, now, repo.lower(), pr, fingerprint))
            return True

    def reviews(self, repo: str, pr: int) -> list[dict[str, Any]]:
        with self._tx() as c:
            rows = c.execute("SELECT * FROM reviews WHERE repo=? AND pr=? ORDER BY id",
                             (repo.lower(), pr)).fetchall()
        return [dict(r) for r in rows]

    def findings(self, repo: str, pr: int) -> list[dict[str, Any]]:
        with self._tx() as c:
            rows = c.execute("SELECT * FROM findings WHERE repo=? AND pr=? ORDER BY id",
                             (repo.lower(), pr)).fetchall()
        return [dict(r) for r in rows]
