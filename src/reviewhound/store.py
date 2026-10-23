"""Review state in SQLite: reviews, findings with stable identity, and developer feedback."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Iterator, Sequence
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
    post_id TEXT, event TEXT,
    PRIMARY KEY (repo, pr, head_sha)
);
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo TEXT NOT NULL, pr INTEGER NOT NULL, fingerprint TEXT NOT NULL,
    kind TEXT NOT NULL, detail TEXT, created_at REAL NOT NULL
);
"""

# Milestone 3: GitHub comment ids we posted, verdicts we dismissed, webhook events already applied.
_GITHUB_SCHEMA = """
CREATE TABLE IF NOT EXISTS comment_links (
    repo TEXT NOT NULL, comment_id INTEGER NOT NULL, pr INTEGER NOT NULL,
    fingerprint TEXT NOT NULL, review_id TEXT NOT NULL, created_at REAL NOT NULL,
    PRIMARY KEY (repo, comment_id)
);
CREATE TABLE IF NOT EXISTS dismissals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo TEXT NOT NULL, pr INTEGER NOT NULL, review_id TEXT NOT NULL,
    ok INTEGER NOT NULL, error TEXT, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS feedback_events (
    key TEXT PRIMARY KEY, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS review_findings (
    repo TEXT NOT NULL, pr INTEGER NOT NULL, review_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
    file TEXT NOT NULL, line INTEGER NOT NULL, side TEXT NOT NULL, severity INTEGER NOT NULL,
    category TEXT NOT NULL, evidence_quote TEXT NOT NULL, identity TEXT NOT NULL,
    PRIMARY KEY (repo, pr, review_id, fingerprint)
);
CREATE TABLE IF NOT EXISTS review_blockers (
    repo TEXT NOT NULL, pr INTEGER NOT NULL, review_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
    file TEXT NOT NULL, side TEXT NOT NULL, category TEXT NOT NULL,
    evidence_quote TEXT NOT NULL, identity TEXT NOT NULL, deleted_text TEXT, deleted_kept INTEGER,
    PRIMARY KEY (repo, pr, review_id, fingerprint)
);
"""

FEEDBACK_KINDS = {"resolved", "fixed", "declined", "reply", "reopened"}


@dataclass(frozen=True)
class ReviewRecord:
    repo: str
    pr: int
    head_sha: str
    outcome: str  # posted | shadow | skipped | head-moved | lease-lost | post-rejected | ...
    tier: str = ""
    model: str = ""
    providers: tuple[str, ...] = ()
    event: str = ""
    posted: bool = False
    post_id: str = ""
    cost_usd: float = 0.0
    timings: dict[str, float] | None = None
    error: str = ""


@dataclass(frozen=True)
class Blocker:
    """A blocking finding of a REQUEST_CHANGES review, as decided before any proof changed it."""

    fingerprint: str
    file: str
    side: str
    category: str
    evidence_quote: str
    identity: str
    deleted_text: str = ""  # a removed-guard blocker is gone once this text is back
    deleted_kept: int = 0  # copies of it the file still had, so another copy is not "back"

    @classmethod
    def of(cls, f: Finding) -> Blocker:
        return cls(f.fingerprint, f.file, f.side, f.category, f.evidence_quote, f.identity,
                   f.deleted_text, f.deleted_kept)


def blocker_rows(rec: ReviewRecord, blockers: Sequence[Blocker]) -> list[tuple[Any, ...]]:
    if rec.outcome != "posted" or not rec.post_id:
        return []
    return [(rec.repo.lower(), rec.pr, rec.post_id, b.fingerprint, b.file, b.side, b.category,
             b.evidence_quote, b.identity, b.deleted_text, b.deleted_kept) for b in blockers]


def upsert_order(rec: ReviewRecord, posted: list[Finding], held: list[tuple[Finding, str]],
                 stored: Sequence[Finding] = ()) -> list[tuple[Finding, str, str | None]]:
    """(finding, state, held reason) sorted by fingerprint, so concurrent writers lock rows in
    one order and cannot deadlock. The sort is stable: posted stays ahead of held on a tie.
    `stored`: findings a landed review was meant to carry but GitHub does not show."""
    new_state = "posted" if rec.posted else "stored"
    rows = [(f, new_state, None) for f in posted] + [(f, "stored", None) for f in stored] + \
        [(f, "held", r) for f, r in held]
    return sorted(rows, key=lambda row: row[0].fingerprint)


def review_finding_rows(rec: ReviewRecord, posted: list[Finding]) -> list[tuple[Any, ...]]:
    """What a later run needs to prove each finding of this posted review gone."""
    if rec.outcome != "posted" or not rec.post_id:
        return []
    return [(rec.repo.lower(), rec.pr, rec.post_id, f.fingerprint, f.file, f.line, f.side,
             int(f.severity), f.category, f.evidence_quote, f.identity) for f in posted]


def check_kinds(items: list[tuple[int, str, str, str]]) -> None:
    for _pr, _fingerprint, kind, _detail in items:
        if kind not in FEEDBACK_KINDS:
            raise ValueError(f"unknown feedback kind {kind!r}")


class ReviewStore(Protocol):
    """What the pipeline and CLI need from a store; SQLite and Postgres both provide it."""

    def known_state(self, repo: str, pr: int) -> dict[str, str]: ...

    def record_review(self, rec: ReviewRecord, posted: list[Finding],
                      held: list[tuple[Finding, str]],
                      blockers: Sequence[Blocker] = (), stored: Sequence[Finding] = ()) -> int: ...

    def claim_post(self, repo: str, pr: int, head_sha: str) -> bool: ...

    def post_claim(self, repo: str, pr: int, head_sha: str) -> tuple[str, float] | None: ...

    def take_stale_claim(self, repo: str, pr: int, head_sha: str, older_than_s: float) -> bool: ...

    def release_post(self, repo: str, pr: int, head_sha: str) -> None: ...

    def mark_posted(self, repo: str, pr: int, head_sha: str, post_id: str, event: str,
                    blockers: Sequence[Blocker] = ()) -> None: ...

    def unrecorded_post(self, repo: str, pr: int, head_sha: str) -> tuple[str, str] | None: ...

    def review_blockers(self, repo: str, pr: int, review_id: str) -> list[dict[str, Any]]: ...

    def add_feedback(self, repo: str, pr: int, fingerprint: str, kind: str,
                     detail: str = "") -> bool: ...

    def reviews(self, repo: str, pr: int) -> list[dict[str, Any]]: ...

    def findings(self, repo: str, pr: int) -> list[dict[str, Any]]: ...

    def link_comments(self, repo: str, pr: int, review_id: str,
                      links: list[tuple[int, str]]) -> None: ...

    def linked_findings(self, repo: str, comment_ids: list[int]) -> dict[int, tuple[int, str]]: ...

    def apply_feedback(self, event_key: str, repo: str,
                       items: list[tuple[int, str, str, str]]) -> list[bool] | None: ...

    def posted_findings(self, repo: str, pr: int, review_id: str) -> list[dict[str, Any]]: ...

    def blocking_reviews_to_dismiss(self, repo: str, pr: int,
                                    current_post_id: str) -> list[str]: ...

    def record_dismissal(self, repo: str, pr: int, review_id: str, ok: bool,
                         error: str = "", permanent: bool = False) -> None: ...

    def dismissals(self, repo: str, pr: int) -> list[dict[str, Any]]: ...

    def feedback(self, repo: str, pr: int) -> list[dict[str, Any]]: ...


class Store:
    def __init__(self, path: Path | str, clock: Callable[[], float] = time.time) -> None:
        self.path = str(path)
        self._clock = clock
        c = sqlite3.connect(self.path, timeout=30)
        try:
            # Switch to WAL once here: racing writers switching it later can fail with "locked".
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(_SCHEMA)
            c.executescript(_GITHUB_SCHEMA)
        finally:
            c.close()
        self._migrate()

    def _migrate(self) -> None:
        """Add columns newer than the DB file. BEGIN IMMEDIATE keeps two processes from racing."""
        with self._tx() as c:
            cols = {r["name"] for r in c.execute("PRAGMA table_info(posts)")}
            if "post_id" not in cols:
                c.execute("ALTER TABLE posts ADD COLUMN post_id TEXT")
            if "event" not in cols:
                c.execute("ALTER TABLE posts ADD COLUMN event TEXT")
            cols = {r["name"] for r in c.execute("PRAGMA table_info(review_blockers)")}
            if "deleted_text" not in cols:
                c.execute("ALTER TABLE review_blockers ADD COLUMN deleted_text TEXT")
            if "deleted_kept" not in cols:
                c.execute("ALTER TABLE review_blockers ADD COLUMN deleted_kept INTEGER")

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
                      held: list[tuple[Finding, str]],
                      blockers: Sequence[Blocker] = (), stored: Sequence[Finding] = ()) -> int:
        """Write the review, every finding and the review's blockers in one transaction."""
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
            for f, state, reason in upsert_order(rec, posted, held, stored):
                self._upsert(c, repo, rec.pr, rec.head_sha, f, state, reason, now)
            c.executemany("INSERT OR IGNORE INTO review_findings (repo, pr, review_id, fingerprint, "
                          "file, line, side, severity, category, evidence_quote, identity) "
                          "VALUES (?,?,?,?,?,?,?,?,?,?,?)", review_finding_rows(rec, posted))
            c.executemany("INSERT OR IGNORE INTO review_blockers (repo, pr, review_id, fingerprint, "
                          "file, side, category, evidence_quote, identity, deleted_text, deleted_kept) "
                          "VALUES (?,?,?,?,?,?,?,?,?,?,?)", blocker_rows(rec, blockers))
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

    def post_claim(self, repo: str, pr: int, head_sha: str) -> tuple[str, float] | None:
        """(post id or "", age in seconds) of this head's claim, or None when nobody holds it."""
        with self._tx() as c:
            row = c.execute("SELECT post_id, created_at FROM posts WHERE repo=? AND pr=? "
                            "AND head_sha=?", (repo.lower(), pr, head_sha)).fetchone()
        if row is None:
            return None
        return str(row["post_id"] or ""), self._clock() - float(row["created_at"])

    def take_stale_claim(self, repo: str, pr: int, head_sha: str, older_than_s: float) -> bool:
        """Take over a claim with no post id older than `older_than_s`; one taker wins."""
        now = self._clock()
        with self._tx() as c:
            cur = c.execute("UPDATE posts SET created_at=? WHERE repo=? AND pr=? AND head_sha=? "
                            "AND (post_id IS NULL OR post_id='') AND created_at < ?",
                            (now, repo.lower(), pr, head_sha, now - older_than_s))
            return cur.rowcount == 1

    def release_post(self, repo: str, pr: int, head_sha: str) -> None:
        """Undo claim_post after GitHub rejected the review, so a later run may post it."""
        with self._tx() as c:
            c.execute("DELETE FROM posts WHERE repo=? AND pr=? AND head_sha=?",
                      (repo.lower(), pr, head_sha))

    def mark_posted(self, repo: str, pr: int, head_sha: str, post_id: str, event: str,
                    blockers: Sequence[Blocker] = ()) -> None:
        """The moment the post lands: its id and event on the claim, and its blockers."""
        rec = ReviewRecord(repo=repo, pr=pr, head_sha=head_sha, outcome="posted", post_id=post_id)
        with self._tx() as c:
            c.execute("INSERT INTO posts (repo, pr, head_sha, created_at, post_id, event) "
                      "VALUES (?,?,?,?,?,?) ON CONFLICT (repo, pr, head_sha) DO UPDATE "
                      "SET post_id=excluded.post_id, event=excluded.event",
                      (repo.lower(), pr, head_sha, self._clock(), post_id, event))
            c.executemany("INSERT OR IGNORE INTO review_blockers (repo, pr, review_id, fingerprint, "
                          "file, side, category, evidence_quote, identity, deleted_text, deleted_kept) "
                          "VALUES (?,?,?,?,?,?,?,?,?,?,?)", blocker_rows(rec, blockers))

    def unrecorded_post(self, repo: str, pr: int, head_sha: str) -> tuple[str, str] | None:
        """(review id, event) of a post that landed for this head with no posted review record."""
        with self._tx() as c:
            row = c.execute(
                "SELECT p.post_id, p.event FROM posts p WHERE p.repo=? AND p.pr=? AND p.head_sha=? "
                "AND p.post_id IS NOT NULL AND p.post_id != '' AND NOT EXISTS (SELECT 1 FROM "
                "reviews r WHERE r.repo=p.repo AND r.pr=p.pr AND r.post_id=p.post_id "
                "AND r.outcome='posted')", (repo.lower(), pr, head_sha)).fetchone()
        return None if row is None else (str(row["post_id"]), str(row["event"] or ""))

    def review_blockers(self, repo: str, pr: int, review_id: str) -> list[dict[str, Any]]:
        with self._tx() as c:
            rows = c.execute("SELECT fingerprint, file, side, category, evidence_quote, identity, "
                             "COALESCE(deleted_text, '') AS deleted_text, "
                             "COALESCE(deleted_kept, 0) AS deleted_kept "
                             "FROM review_blockers WHERE repo=? AND pr=? AND review_id=? "
                             "ORDER BY file, fingerprint", (repo.lower(), pr, review_id)).fetchall()
        return [dict(r) for r in rows]

    def add_feedback(self, repo: str, pr: int, fingerprint: str, kind: str,
                     detail: str = "") -> bool:
        return self.apply_feedback("", repo, [(pr, fingerprint, kind, detail)]) == [True]

    def apply_feedback(self, event_key: str, repo: str,
                       items: list[tuple[int, str, str, str]]) -> list[bool] | None:
        """Claim `event_key` and record each (pr, fingerprint, kind, detail) in ONE transaction.

        None: the key was claimed before. Else, per item, whether its finding exists. """
        check_kinds(items)
        now = self._clock()
        repo = repo.lower()
        with self._tx() as c:
            if event_key:
                cur = c.execute("INSERT OR IGNORE INTO feedback_events (key, created_at) "
                                "VALUES (?,?)", (event_key, now))
                if cur.rowcount != 1:
                    return None
            done: list[bool] = []
            for pr, fingerprint, kind, detail in items:
                exists = c.execute("SELECT 1 FROM findings WHERE repo=? AND pr=? "
                                   "AND fingerprint=?", (repo, pr, fingerprint)).fetchone()
                done.append(exists is not None)
                if exists is None:
                    continue
                c.execute("INSERT INTO feedback (repo, pr, fingerprint, kind, detail, created_at) "
                          "VALUES (?,?,?,?,?,?)", (repo, pr, fingerprint, kind, detail, now))
                if kind != "reply":
                    c.execute("UPDATE findings SET state=?, updated_at=? WHERE repo=? AND pr=? "
                              "AND fingerprint=?", (kind, now, repo, pr, fingerprint))
            return done

    def posted_findings(self, repo: str, pr: int, review_id: str) -> list[dict[str, Any]]:
        with self._tx() as c:
            rows = c.execute("SELECT * FROM review_findings WHERE repo=? AND pr=? AND review_id=? "
                             "ORDER BY file, line, fingerprint",
                             (repo.lower(), pr, review_id)).fetchall()
        return [dict(r) for r in rows]

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

    def link_comments(self, repo: str, pr: int, review_id: str,
                      links: list[tuple[int, str]]) -> None:
        """Remember which GitHub comment carries which finding, so thread events map back."""
        now = self._clock()
        with self._tx() as c:
            c.executemany(
                "INSERT OR REPLACE INTO comment_links (repo, comment_id, pr, fingerprint, "
                "review_id, created_at) VALUES (?,?,?,?,?,?)",
                [(repo.lower(), cid, pr, fp, review_id, now) for cid, fp in links])

    def linked_findings(self, repo: str, comment_ids: list[int]) -> dict[int, tuple[int, str]]:
        """comment id -> (PR, fingerprint), for the ids we posted; others are absent."""
        if not comment_ids:
            return {}
        marks = ",".join("?" * len(comment_ids))
        with self._tx() as c:
            rows = c.execute(f"SELECT comment_id, pr, fingerprint FROM comment_links "
                             f"WHERE repo=? AND comment_id IN ({marks})",
                             (repo.lower(), *comment_ids)).fetchall()
        return {int(r["comment_id"]): (int(r["pr"]), r["fingerprint"]) for r in rows}

    def blocking_reviews_to_dismiss(self, repo: str, pr: int, current_post_id: str) -> list[str]:
        """Our REQUEST_CHANGES reviews posted before `current_post_id`, not dismissed or given up.

        Only older rows: a late follow-up must never dismiss a newer head's verdict."""
        with self._tx() as c:
            rows = c.execute(
                "SELECT post_id FROM reviews WHERE repo=? AND pr=? AND outcome='posted' "
                "AND event='REQUEST_CHANGES' AND post_id != '' "
                "AND id < (SELECT MIN(id) FROM reviews WHERE repo=? AND pr=? AND post_id=?) "
                "AND post_id NOT IN (SELECT review_id FROM dismissals WHERE repo=? AND pr=? "
                "AND ok != 0) ORDER BY id",
                (repo.lower(), pr, repo.lower(), pr, current_post_id, repo.lower(), pr)
            ).fetchall()
        return [r["post_id"] for r in rows]

    def record_dismissal(self, repo: str, pr: int, review_id: str, ok: bool,
                         error: str = "", permanent: bool = False) -> None:
        """ok is stored as 1 (dismissed), 0 (failed, retry later) or -1 (failed, give up)."""
        status = 1 if ok else (-1 if permanent else 0)
        with self._tx() as c:
            c.execute("INSERT INTO dismissals (repo, pr, review_id, ok, error, created_at) "
                      "VALUES (?,?,?,?,?,?)",
                      (repo.lower(), pr, review_id, status, error[:500], self._clock()))

    def dismissals(self, repo: str, pr: int) -> list[dict[str, Any]]:
        with self._tx() as c:
            rows = c.execute("SELECT review_id, ok, error FROM dismissals WHERE repo=? AND pr=? "
                             "ORDER BY id", (repo.lower(), pr)).fetchall()
        return [dict(r) for r in rows]

    def feedback(self, repo: str, pr: int) -> list[dict[str, Any]]:
        with self._tx() as c:
            rows = c.execute("SELECT fingerprint, kind, detail FROM feedback WHERE repo=? AND pr=? "
                             "ORDER BY id", (repo.lower(), pr)).fetchall()
        return [dict(r) for r in rows]
