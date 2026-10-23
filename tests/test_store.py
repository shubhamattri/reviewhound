import sqlite3
from pathlib import Path

import pytest

from reviewhound.config import Severity
from reviewhound.findings import Finding
from reviewhound.store import ReviewRecord, Store
from tests.conftest import Backend, FakeClock


def fnd(quote: str) -> Finding:
    return Finding(file="a.py", line=3, severity=Severity.MAJOR, category="c", title="t",
                   body="b", evidence_quote=quote, source="model:x", verified=True)


def test_review_and_findings_written_together(backend: Backend, clock: FakeClock) -> None:
    s = backend.store(clock=clock)
    rid = s.record_review(ReviewRecord(repo="A/B", pr=1, head_sha="h1", outcome="posted",
                                       tier="standard", providers=("claude-cli:ok",),
                                       posted=True, cost_usd=0.02, timings={"model": 1.5}),
                          [fnd("x")], [(fnd("y"), "unverified")])
    assert rid == 1
    rows = s.reviews("a/b", 1)
    assert [(r["outcome"], r["posted"], r["providers"], r["cost_usd"]) for r in rows] == [
        ("posted", 1, '["claude-cli:ok"]', 0.02)]
    assert {(f["state"], f["held_reason"]) for f in s.findings("a/b", 1)} == {
        ("posted", None), ("held", "unverified")}


def test_developer_feedback_survives_a_re_review(backend: Backend, clock: FakeClock) -> None:
    s = backend.store(clock=clock)
    f = fnd("x")
    s.record_review(ReviewRecord(repo="a/b", pr=1, head_sha="h1", outcome="posted", posted=True),
                    [f], [])
    assert s.add_feedback("a/b", 1, f.fingerprint, "declined", "intended") is True
    s.record_review(ReviewRecord(repo="a/b", pr=1, head_sha="h2", outcome="posted", posted=True),
                    [f], [])
    assert s.known_state("a/b", 1) == {f.fingerprint: "declined"}
    row = s.findings("a/b", 1)[0]
    assert (row["first_head"], row["last_head"]) == ("h1", "h2")


def test_shadow_finding_can_be_posted_later(backend: Backend, clock: FakeClock) -> None:
    s = backend.store(clock=clock)
    f = fnd("x")
    s.record_review(ReviewRecord(repo="a/b", pr=1, head_sha="h1", outcome="shadow"), [f], [])
    assert s.known_state("a/b", 1) == {f.fingerprint: "stored"}
    s.record_review(ReviewRecord(repo="a/b", pr=1, head_sha="h2", outcome="posted", posted=True),
                    [f], [])
    assert s.known_state("a/b", 1) == {f.fingerprint: "posted"}


def test_feedback_rejects_unknown_kind_and_unknown_finding(backend: Backend) -> None:
    s = backend.store()
    with pytest.raises(ValueError):
        s.add_feedback("a/b", 1, "f", "liked")
    assert s.add_feedback("a/b", 1, "nope", "resolved") is False


@pytest.mark.parametrize("state", ["resolved", "posted", "reopened", "declined"])
def test_feedback_and_posted_states_survive_a_held_re_review(backend: Backend, state: str) -> None:
    s = backend.store()
    f = fnd("x")
    s.record_review(ReviewRecord(repo="a/b", pr=1, head_sha="h1", outcome="posted", posted=True),
                    [f], [])
    if state != "posted":
        s.add_feedback("a/b", 1, f.fingerprint, state)
    s.record_review(ReviewRecord(repo="a/b", pr=1, head_sha="h2", outcome="posted", posted=True),
                    [], [(f, "already posted on an earlier head")])
    assert s.known_state("a/b", 1) == {f.fingerprint: state}


def test_fixed_finding_that_comes_back_is_posted_once_then_remembered(backend: Backend) -> None:
    s = backend.store()
    f = fnd("x")
    s.record_review(ReviewRecord(repo="a/b", pr=1, head_sha="h1", outcome="posted", posted=True),
                    [f], [])
    s.add_feedback("a/b", 1, f.fingerprint, "fixed")
    s.record_review(ReviewRecord(repo="a/b", pr=1, head_sha="h2", outcome="posted", posted=True),
                    [f], [])
    assert s.known_state("a/b", 1) == {f.fingerprint: "posted"}


def test_only_one_post_per_head(backend: Backend) -> None:
    s = backend.store()
    assert s.claim_post("A/b", 1, "h1") is True
    assert s.claim_post("a/b", 1, "h1") is False
    assert s.claim_post("a/b", 1, "h2") is True


def test_new_sqlite_store_is_in_wal_mode_once_constructed(tmp_path: Path) -> None:
    # Before any transaction: writers racing to switch a fresh file to WAL can hit "locked".
    Store(tmp_path / "s.db")
    c = sqlite3.connect(tmp_path / "s.db")
    try:
        assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        c.close()
