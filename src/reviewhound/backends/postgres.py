"""Postgres queue, store and metrics with the same behaviour as the SQLite ones.

Every read-then-write runs in one transaction. Claims use FOR UPDATE SKIP LOCKED, so two workers
never take the same job. Times are epoch seconds from the injected clock, as in SQLite.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from typing import Any, TypeVar

import psycopg
from psycopg.rows import DictRow, dict_row

from reviewhound.findings import Finding
from reviewhound.queue import Job, LeaseLost, job_key
from reviewhound.store import (
    Blocker,
    ReviewRecord,
    blocker_rows,
    check_kinds,
    review_finding_rows,
    upsert_order,
)

log = logging.getLogger(__name__)
T = TypeVar("T")
Conn = psycopg.Connection[DictRow]
# deadlock_detected, serialization_failure: Postgres rolled the transaction back; rerunning is safe.
RETRY_SQLSTATES = ("40P01", "40001")

# Separate DDL lists so a later column or table is one more statement, not an edit to a blob.
QUEUE_DDL: list[str] = [
    """CREATE TABLE IF NOT EXISTS jobs (
        id BIGSERIAL PRIMARY KEY,
        key TEXT NOT NULL UNIQUE,
        repo TEXT NOT NULL,
        pr INTEGER NOT NULL,
        head_sha TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('queued','running','done','dead','superseded')),
        attempts INTEGER NOT NULL DEFAULT 0,
        max_attempts INTEGER NOT NULL,
        run_after DOUBLE PRECISION NOT NULL,
        lease_until DOUBLE PRECISION,
        lease_token TEXT,
        last_error TEXT,
        created_at DOUBLE PRECISION NOT NULL,
        updated_at DOUBLE PRECISION NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS jobs_ready ON jobs(state, run_after)",
    "CREATE INDEX IF NOT EXISTS jobs_pr ON jobs(repo, pr)",
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS installation_id BIGINT",
]

STORE_DDL: list[str] = [
    """CREATE TABLE IF NOT EXISTS reviews (
        id BIGSERIAL PRIMARY KEY,
        repo TEXT NOT NULL, pr INTEGER NOT NULL, head_sha TEXT NOT NULL,
        outcome TEXT NOT NULL,
        tier TEXT, model TEXT, providers TEXT, event TEXT,
        posted INTEGER NOT NULL DEFAULT 0, post_id TEXT,
        cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0, timings TEXT, error TEXT,
        created_at DOUBLE PRECISION NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS reviews_pr ON reviews(repo, pr)",
    """CREATE TABLE IF NOT EXISTS findings (
        id BIGSERIAL PRIMARY KEY,
        repo TEXT NOT NULL, pr INTEGER NOT NULL, fingerprint TEXT NOT NULL,
        first_head TEXT NOT NULL, last_head TEXT NOT NULL,
        file TEXT NOT NULL, line INTEGER NOT NULL, severity INTEGER NOT NULL,
        category TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
        source TEXT NOT NULL, verified INTEGER NOT NULL,
        state TEXT NOT NULL CHECK (state IN
            ('stored','held','posted','resolved','declined','fixed','reopened')),
        held_reason TEXT,
        created_at DOUBLE PRECISION NOT NULL, updated_at DOUBLE PRECISION NOT NULL,
        UNIQUE (repo, pr, fingerprint))""",
    """CREATE TABLE IF NOT EXISTS posts (
        repo TEXT NOT NULL, pr INTEGER NOT NULL, head_sha TEXT NOT NULL,
        created_at DOUBLE PRECISION NOT NULL,
        PRIMARY KEY (repo, pr, head_sha))""",
    "ALTER TABLE posts ADD COLUMN IF NOT EXISTS post_id TEXT",
    """CREATE TABLE IF NOT EXISTS feedback (
        id BIGSERIAL PRIMARY KEY,
        repo TEXT NOT NULL, pr INTEGER NOT NULL, fingerprint TEXT NOT NULL,
        kind TEXT NOT NULL, detail TEXT, created_at DOUBLE PRECISION NOT NULL)""",
    # GitHub comment ids we posted, verdicts we dismissed, webhook events already applied.
    """CREATE TABLE IF NOT EXISTS comment_links (
        repo TEXT NOT NULL, comment_id BIGINT NOT NULL, pr INTEGER NOT NULL,
        fingerprint TEXT NOT NULL, review_id TEXT NOT NULL,
        created_at DOUBLE PRECISION NOT NULL,
        PRIMARY KEY (repo, comment_id))""",
    """CREATE TABLE IF NOT EXISTS dismissals (
        id BIGSERIAL PRIMARY KEY,
        repo TEXT NOT NULL, pr INTEGER NOT NULL, review_id TEXT NOT NULL,
        ok INTEGER NOT NULL, error TEXT, created_at DOUBLE PRECISION NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS feedback_events (
        key TEXT PRIMARY KEY, created_at DOUBLE PRECISION NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS review_findings (
        repo TEXT NOT NULL, pr INTEGER NOT NULL, review_id TEXT NOT NULL,
        fingerprint TEXT NOT NULL, file TEXT NOT NULL, line INTEGER NOT NULL,
        side TEXT NOT NULL, severity INTEGER NOT NULL, category TEXT NOT NULL,
        evidence_quote TEXT NOT NULL, identity TEXT NOT NULL,
        PRIMARY KEY (repo, pr, review_id, fingerprint))""",
    """CREATE TABLE IF NOT EXISTS review_blockers (
        repo TEXT NOT NULL, pr INTEGER NOT NULL, review_id TEXT NOT NULL,
        fingerprint TEXT NOT NULL, file TEXT NOT NULL, side TEXT NOT NULL,
        category TEXT NOT NULL, evidence_quote TEXT NOT NULL, identity TEXT NOT NULL,
        PRIMARY KEY (repo, pr, review_id, fingerprint))""",
]

METRICS_DDL: list[str] = [
    """CREATE TABLE IF NOT EXISTS metric_counters (
        name TEXT NOT NULL, label TEXT NOT NULL, value DOUBLE PRECISION NOT NULL,
        PRIMARY KEY (name, label))""",
]

_DDL_LOCK = 0x7265766877  # any constant: serialises CREATE IF NOT EXISTS across processes


class _Pg:
    def __init__(self, url: str, connect_timeout_s: int = 10) -> None:
        self.url = url
        self.connect_timeout_s = connect_timeout_s

    @contextmanager
    def _tx(self) -> Iterator[Conn]:
        """One transaction: committed on success, rolled back on any exception."""
        with psycopg.connect(self.url, row_factory=dict_row,
                             connect_timeout=self.connect_timeout_s) as conn:
            yield conn

    def _write(self, body: Callable[[Conn], T]) -> T:
        """Run `body` in one transaction, and once more if Postgres aborted it for a deadlock or a
        serialization failure. A second such failure, or any other error, raises."""
        for attempt in (1, 2):
            try:
                with self._tx() as c:
                    return body(c)
            except psycopg.Error as exc:
                if exc.sqlstate not in RETRY_SQLSTATES or attempt == 2:
                    raise
                log.warning("postgres write aborted, retrying once",
                            extra={"sqlstate": exc.sqlstate, "error": type(exc).__name__})
        raise AssertionError("unreachable")

    def _migrate(self, ddl: Sequence[str]) -> None:
        def body(c: Conn) -> None:
            c.execute("SELECT pg_advisory_xact_lock(%s)", (_DDL_LOCK,))
            for stmt in ddl:
                c.execute(stmt)
        self._write(body)


class PostgresQueue(_Pg):
    def __init__(self, url: str, max_attempts: int = 4, lease_s: float = 1800,
                 backoff_base_s: float = 30, clock: Callable[[], float] = time.time) -> None:
        super().__init__(url)
        self.max_attempts = max_attempts
        self.lease_s = lease_s
        self.backoff_base_s = backoff_base_s
        self._clock = clock
        self._migrate(QUEUE_DDL)

    @staticmethod
    def _job(row: DictRow) -> Job:
        return Job(id=row["id"], repo=row["repo"], pr=row["pr"], head_sha=row["head_sha"],
                   state=row["state"], attempts=row["attempts"],
                   max_attempts=row["max_attempts"], last_error=row["last_error"],
                   lease_token=row["lease_token"], installation_id=row["installation_id"])

    def enqueue(self, repo: str, pr: int, head_sha: str,
                installation_id: int | None = None) -> tuple[int, bool]:
        """Return (job id, created). Older queued jobs for the same PR are superseded."""
        repo = repo.lower()
        now = self._clock()
        key = job_key(repo, pr, head_sha)

        def body(c: Conn) -> tuple[int, bool]:
            # Serialise enqueues for one PR, as SQLite's BEGIN IMMEDIATE does.
            c.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"{repo}#{pr}",))
            c.execute("UPDATE jobs SET state='superseded', updated_at=%s "
                      "WHERE repo=%s AND pr=%s AND state='queued' AND key != %s",
                      (now, repo, pr, key))
            row = c.execute("SELECT id, state FROM jobs WHERE key=%s FOR UPDATE",
                            (key,)).fetchone()
            if row is not None and installation_id is not None:
                c.execute("UPDATE jobs SET installation_id=%s WHERE id=%s",
                          (installation_id, row["id"]))
            if row is not None:
                if row["state"] != "superseded":
                    return int(row["id"]), False
                c.execute("UPDATE jobs SET state='queued', attempts=0, run_after=%s, "
                          "updated_at=%s WHERE id=%s", (now, now, row["id"]))
                return int(row["id"]), True
            new = c.execute(
                "INSERT INTO jobs (key, repo, pr, head_sha, state, max_attempts, run_after, "
                "created_at, updated_at, installation_id) "
                "VALUES (%s, %s, %s, %s, 'queued', %s, %s, %s, %s, %s) RETURNING id",
                (key, repo, pr, head_sha, self.max_attempts, now, now, now, installation_id)
            ).fetchone()
            if new is None:
                raise RuntimeError("INSERT ... RETURNING returned no row")
            return int(new["id"]), True
        return self._write(body)

    def claim(self) -> Job | None:
        """Take the next ready job, or one whose worker died (lease expired)."""
        now = self._clock()

        def body(c: Conn) -> Job | None:
            c.execute("UPDATE jobs SET state='dead', last_error='lease expired on final attempt', "
                      "lease_until=NULL, updated_at=%s WHERE id IN (SELECT id FROM jobs "
                      "WHERE state='running' AND lease_until < %s AND attempts >= max_attempts "
                      "FOR UPDATE SKIP LOCKED)", (now, now))
            row = c.execute(
                "SELECT id FROM jobs WHERE (state='queued' AND run_after <= %s) "
                "OR (state='running' AND lease_until < %s) ORDER BY run_after, id LIMIT 1 "
                "FOR UPDATE SKIP LOCKED", (now, now)).fetchone()
            if row is None:
                return None
            claimed = c.execute(
                "UPDATE jobs SET state='running', attempts=attempts+1, lease_until=%s, "
                "lease_token=%s, updated_at=%s WHERE id=%s RETURNING *",
                (now + self.lease_s, secrets.token_hex(8), now, row["id"])).fetchone()
            if claimed is None:
                raise RuntimeError(f"locked job {row['id']} vanished during claim")
            return self._job(claimed)
        return self._write(body)

    def complete(self, job: Job) -> None:
        """Mark done. Raises LeaseLost if another worker took the job after our lease ran out."""
        def body(c: Conn) -> None:
            cur = c.execute("UPDATE jobs SET state='done', lease_until=NULL, updated_at=%s "
                            "WHERE id=%s AND state='running' AND lease_token=%s",
                            (self._clock(), job.id, job.lease_token))
            if cur.rowcount != 1:
                raise LeaseLost(job.id)
        self._write(body)

    def fail(self, job: Job, error: str, retryable: bool) -> str:
        """Record a failure. Returns the new state: 'queued' (will retry) or 'dead'."""
        now = self._clock()

        def body(c: Conn) -> str:
            row = c.execute("SELECT attempts, max_attempts FROM jobs WHERE id=%s "
                            "AND state='running' AND lease_token=%s FOR UPDATE",
                            (job.id, job.lease_token)).fetchone()
            if row is None:
                raise LeaseLost(job.id)
            if retryable and row["attempts"] < row["max_attempts"]:
                state = "queued"
                run_after = now + self.backoff_base_s * (2 ** (row["attempts"] - 1))
            else:
                state, run_after = "dead", now
            c.execute("UPDATE jobs SET state=%s, run_after=%s, lease_until=NULL, last_error=%s, "
                      "updated_at=%s WHERE id=%s", (state, run_after, error[:500], now, job.id))
            return state
        return self._write(body)

    def extend_lease(self, job: Job) -> float:
        """Push the lease out by lease_s. Raises LeaseLost if the job is no longer ours."""
        now = self._clock()

        def body(c: Conn) -> None:
            cur = c.execute("UPDATE jobs SET lease_until=%s, updated_at=%s WHERE id=%s "
                            "AND state='running' AND lease_token=%s",
                            (now + self.lease_s, now, job.id, job.lease_token))
            if cur.rowcount != 1:
                raise LeaseLost(job.id)
        self._write(body)
        return now + self.lease_s

    def get(self, job_id: int) -> Job | None:
        with self._tx() as c:
            row = c.execute("SELECT * FROM jobs WHERE id=%s", (job_id,)).fetchone()
        return self._job(row) if row else None

    def dead_jobs(self) -> list[Job]:
        with self._tx() as c:
            rows = c.execute("SELECT * FROM jobs WHERE state='dead' ORDER BY id").fetchall()
        return [self._job(r) for r in rows]

    def requeue(self, job_id: int) -> bool:
        now = self._clock()

        def body(c: Conn) -> bool:
            cur = c.execute("UPDATE jobs SET state='queued', attempts=0, run_after=%s, "
                            "updated_at=%s WHERE id=%s AND state='dead'", (now, now, job_id))
            return cur.rowcount == 1
        return self._write(body)

    def stuck_count(self, older_than_s: float) -> int:
        """Queued jobs waiting longer than `older_than_s`: the backlog alarm reads this."""
        with self._tx() as c:
            row = c.execute("SELECT COUNT(*) AS n FROM jobs WHERE state='queued' "
                            "AND created_at < %s", (self._clock() - older_than_s,)).fetchone()
        return int(row["n"]) if row else 0

    def state_counts(self) -> dict[str, int]:
        with self._tx() as c:
            rows = c.execute("SELECT state, COUNT(*) AS n FROM jobs GROUP BY state").fetchall()
        return {r["state"]: int(r["n"]) for r in rows}

    def oldest_queued_age(self) -> float:
        """Seconds the oldest queued job has waited; 0 when nothing is queued."""
        with self._tx() as c:
            row = c.execute("SELECT MIN(created_at) AS t FROM jobs WHERE state='queued'"
                            ).fetchone()
        if row is None or row["t"] is None:
            return 0.0
        return max(0.0, self._clock() - float(row["t"]))


class PostgresStore(_Pg):
    def __init__(self, url: str, clock: Callable[[], float] = time.time) -> None:
        super().__init__(url)
        self._clock = clock
        self._migrate(STORE_DDL)

    def known_state(self, repo: str, pr: int) -> dict[str, str]:
        with self._tx() as c:
            rows = c.execute("SELECT fingerprint, state FROM findings WHERE repo=%s AND pr=%s",
                             (repo.lower(), pr)).fetchall()
        return {r["fingerprint"]: r["state"] for r in rows}

    def record_review(self, rec: ReviewRecord, posted: list[Finding],
                      held: list[tuple[Finding, str]],
                      blockers: Sequence[Blocker] = ()) -> int:
        """Write the review, every finding and the review's blockers in one transaction."""
        now = self._clock()
        repo = rec.repo.lower()

        def body(c: Conn) -> int:
            row = c.execute(
                "INSERT INTO reviews (repo, pr, head_sha, outcome, tier, model, providers, event, "
                "posted, post_id, cost_usd, timings, error, created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (repo, rec.pr, rec.head_sha, rec.outcome, rec.tier, rec.model,
                 json.dumps(list(rec.providers)), rec.event, int(rec.posted), rec.post_id,
                 rec.cost_usd, json.dumps(rec.timings or {}), rec.error, now)).fetchone()
            if row is None:
                raise RuntimeError("INSERT ... RETURNING returned no row")
            for f, state, reason in upsert_order(rec, posted, held):
                self._upsert(c, repo, rec.pr, rec.head_sha, f, state, reason, now)
            with c.cursor() as cur:
                rows = review_finding_rows(rec, posted)
                if rows:
                    cur.executemany(
                        "INSERT INTO review_findings (repo, pr, review_id, fingerprint, file, "
                        "line, side, severity, category, evidence_quote, identity) VALUES "
                        "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING", rows)
                brows = blocker_rows(rec, blockers)
                if brows:
                    cur.executemany(
                        "INSERT INTO review_blockers (repo, pr, review_id, fingerprint, file, "
                        "side, category, evidence_quote, identity) VALUES "
                        "(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING", brows)
            return int(row["id"])
        return self._write(body)

    @staticmethod
    def _upsert(c: Conn, repo: str, pr: int, head: str, f: Finding,
                state: str, reason: str | None, now: float) -> None:
        # Same protected states as Store._upsert: feedback and live comments are never overwritten.
        c.execute(
            "INSERT INTO findings (repo, pr, fingerprint, first_head, last_head, file, line, "
            "severity, category, title, body, source, verified, state, held_reason, created_at, "
            "updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
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
        def body(c: Conn) -> bool:
            cur = c.execute("INSERT INTO posts (repo, pr, head_sha, created_at) "
                            "VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                            (repo.lower(), pr, head_sha, self._clock()))
            return cur.rowcount == 1
        return self._write(body)

    def release_post(self, repo: str, pr: int, head_sha: str) -> None:
        """Undo claim_post after GitHub rejected the review, so a later run may post it."""
        def body(c: Conn) -> None:
            c.execute("DELETE FROM posts WHERE repo=%s AND pr=%s AND head_sha=%s",
                      (repo.lower(), pr, head_sha))
        self._write(body)

    def mark_posted(self, repo: str, pr: int, head_sha: str, post_id: str) -> None:
        """Record the GitHub review id on the claim the moment the post lands."""
        def body(c: Conn) -> None:
            c.execute("INSERT INTO posts (repo, pr, head_sha, created_at, post_id) "
                      "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (repo, pr, head_sha) DO UPDATE "
                      "SET post_id=excluded.post_id",
                      (repo.lower(), pr, head_sha, self._clock(), post_id))
        self._write(body)

    def unrecorded_post(self, repo: str, pr: int, head_sha: str) -> str | None:
        """The review id of a post that landed for this head but has no posted review record."""
        with self._tx() as c:
            row = c.execute(
                "SELECT p.post_id FROM posts p WHERE p.repo=%s AND p.pr=%s AND p.head_sha=%s "
                "AND p.post_id IS NOT NULL AND p.post_id != '' AND NOT EXISTS (SELECT 1 FROM "
                "reviews r WHERE r.repo=p.repo AND r.pr=p.pr AND r.post_id=p.post_id "
                "AND r.outcome='posted')", (repo.lower(), pr, head_sha)).fetchone()
        return None if row is None else str(row["post_id"])

    def review_blockers(self, repo: str, pr: int, review_id: str) -> list[dict[str, Any]]:
        with self._tx() as c:
            rows = c.execute("SELECT fingerprint, file, side, category, evidence_quote, identity "
                             "FROM review_blockers WHERE repo=%s AND pr=%s AND review_id=%s "
                             "ORDER BY file, fingerprint", (repo.lower(), pr, review_id)).fetchall()
        return [dict(r) for r in rows]

    def add_feedback(self, repo: str, pr: int, fingerprint: str, kind: str,
                     detail: str = "") -> bool:
        return self.apply_feedback("", repo, [(pr, fingerprint, kind, detail)]) == [True]

    def apply_feedback(self, event_key: str, repo: str,
                       items: list[tuple[int, str, str, str]]) -> list[bool] | None:
        """Same contract as Store.apply_feedback: the claim and the writes commit together."""
        check_kinds(items)
        now = self._clock()
        repo = repo.lower()

        def body(c: Conn) -> list[bool] | None:
            if event_key:
                cur = c.execute("INSERT INTO feedback_events (key, created_at) VALUES (%s,%s) "
                                "ON CONFLICT DO NOTHING", (event_key, now))
                if cur.rowcount != 1:
                    return None
            done: list[bool] = []
            for pr, fingerprint, kind, detail in items:
                exists = c.execute("SELECT 1 FROM findings WHERE repo=%s AND pr=%s "
                                   "AND fingerprint=%s FOR UPDATE",
                                   (repo, pr, fingerprint)).fetchone()
                done.append(exists is not None)
                if exists is None:
                    continue
                c.execute("INSERT INTO feedback (repo, pr, fingerprint, kind, detail, created_at) "
                          "VALUES (%s,%s,%s,%s,%s,%s)",
                          (repo, pr, fingerprint, kind, detail, now))
                if kind != "reply":
                    c.execute("UPDATE findings SET state=%s, updated_at=%s WHERE repo=%s "
                              "AND pr=%s AND fingerprint=%s", (kind, now, repo, pr, fingerprint))
            return done
        return self._write(body)

    def posted_findings(self, repo: str, pr: int, review_id: str) -> list[dict[str, Any]]:
        with self._tx() as c:
            rows = c.execute("SELECT * FROM review_findings WHERE repo=%s AND pr=%s "
                             "AND review_id=%s ORDER BY file, line, fingerprint",
                             (repo.lower(), pr, review_id)).fetchall()
        return [dict(r) for r in rows]

    def reviews(self, repo: str, pr: int) -> list[dict[str, Any]]:
        with self._tx() as c:
            rows = c.execute("SELECT * FROM reviews WHERE repo=%s AND pr=%s ORDER BY id",
                             (repo.lower(), pr)).fetchall()
        return [dict(r) for r in rows]

    def findings(self, repo: str, pr: int) -> list[dict[str, Any]]:
        with self._tx() as c:
            rows = c.execute("SELECT * FROM findings WHERE repo=%s AND pr=%s ORDER BY id",
                             (repo.lower(), pr)).fetchall()
        return [dict(r) for r in rows]

    def link_comments(self, repo: str, pr: int, review_id: str,
                      links: list[tuple[int, str]]) -> None:
        """Remember which GitHub comment carries which finding, so thread events map back."""
        if not links:
            return
        now = self._clock()

        def body(c: Conn) -> None:
            with c.cursor() as cur:
                cur.executemany(
                    "INSERT INTO comment_links (repo, comment_id, pr, fingerprint, review_id, "
                    "created_at) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (repo, comment_id) "
                    "DO UPDATE SET pr=excluded.pr, fingerprint=excluded.fingerprint, "
                    "review_id=excluded.review_id, created_at=excluded.created_at",
                    [(repo.lower(), cid, pr, fp, review_id, now) for cid, fp in links])
        self._write(body)

    def linked_findings(self, repo: str, comment_ids: list[int]) -> dict[int, tuple[int, str]]:
        """comment id -> (PR, fingerprint), for the ids we posted; others are absent."""
        if not comment_ids:
            return {}
        with self._tx() as c:
            rows = c.execute("SELECT comment_id, pr, fingerprint FROM comment_links "
                             "WHERE repo=%s AND comment_id = ANY(%s)",
                             (repo.lower(), list(comment_ids))).fetchall()
        return {int(r["comment_id"]): (int(r["pr"]), r["fingerprint"]) for r in rows}

    def blocking_reviews_to_dismiss(self, repo: str, pr: int, current_post_id: str) -> list[str]:
        """Same rule as Store: only older REQUEST_CHANGES rows, not dismissed or given up."""
        repo = repo.lower()
        with self._tx() as c:
            rows = c.execute(
                "SELECT post_id FROM reviews WHERE repo=%s AND pr=%s AND outcome='posted' "
                "AND event='REQUEST_CHANGES' AND post_id != '' "
                "AND id < (SELECT MIN(id) FROM reviews WHERE repo=%s AND pr=%s AND post_id=%s) "
                "AND post_id NOT IN (SELECT review_id FROM dismissals WHERE repo=%s AND pr=%s "
                "AND ok != 0) ORDER BY id",
                (repo, pr, repo, pr, current_post_id, repo, pr)).fetchall()
        return [r["post_id"] for r in rows]

    def record_dismissal(self, repo: str, pr: int, review_id: str, ok: bool,
                         error: str = "", permanent: bool = False) -> None:
        """ok is stored as 1 (dismissed), 0 (failed, retry later) or -1 (failed, give up)."""
        status = 1 if ok else (-1 if permanent else 0)

        def body(c: Conn) -> None:
            c.execute("INSERT INTO dismissals (repo, pr, review_id, ok, error, created_at) "
                      "VALUES (%s,%s,%s,%s,%s,%s)",
                      (repo.lower(), pr, review_id, status, error[:500], self._clock()))
        self._write(body)

    def dismissals(self, repo: str, pr: int) -> list[dict[str, Any]]:
        with self._tx() as c:
            rows = c.execute("SELECT review_id, ok, error FROM dismissals WHERE repo=%s "
                             "AND pr=%s ORDER BY id", (repo.lower(), pr)).fetchall()
        return [dict(r) for r in rows]

    def feedback(self, repo: str, pr: int) -> list[dict[str, Any]]:
        with self._tx() as c:
            rows = c.execute("SELECT fingerprint, kind, detail FROM feedback WHERE repo=%s "
                             "AND pr=%s ORDER BY id", (repo.lower(), pr)).fetchall()
        return [dict(r) for r in rows]


class PostgresMetrics(_Pg):
    def __init__(self, url: str) -> None:
        super().__init__(url)
        self._migrate(METRICS_DDL)

    def add(self, increments: Sequence[tuple[str, str, float]]) -> None:
        # Sorted so two workers lock the same rows in the same order and cannot deadlock.
        def body(c: Conn) -> None:
            for name, label, value in sorted(increments):
                c.execute("INSERT INTO metric_counters (name, label, value) VALUES (%s,%s,%s) "
                          "ON CONFLICT (name, label) DO UPDATE "
                          "SET value = metric_counters.value + excluded.value",
                          (name, label, value))
        self._write(body)

    def counters(self) -> dict[tuple[str, str], float]:
        with self._tx() as c:
            rows = c.execute("SELECT name, label, value FROM metric_counters").fetchall()
        return {(r["name"], r["label"]): float(r["value"]) for r in rows}
