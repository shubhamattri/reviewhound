from pathlib import Path

import pytest

from reviewhound.config import Severity
from reviewhound.findings import Finding
from reviewhound.store import ReviewRecord, Store
from tests.conftest import FakeClock


def fnd(quote: str) -> Finding:
    return Finding(file="a.py", line=3, severity=Severity.MAJOR, category="c", title="t",
                   body="b", evidence_quote=quote, source="model:x", verified=True)


def test_review_and_findings_written_together(tmp_path: Path, clock: FakeClock) -> None:
    s = Store(tmp_path / "s.db", clock=clock)
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


def test_developer_feedback_survives_a_re_review(tmp_path: Path, clock: FakeClock) -> None:
    s = Store(tmp_path / "s.db", clock=clock)
    f = fnd("x")
    s.record_review(ReviewRecord(repo="a/b", pr=1, head_sha="h1", outcome="posted", posted=True),
                    [f], [])
    assert s.add_feedback("a/b", 1, f.fingerprint, "declined", "intended") is True
    s.record_review(ReviewRecord(repo="a/b", pr=1, head_sha="h2", outcome="posted", posted=True),
                    [f], [])
    assert s.known_state("a/b", 1) == {f.fingerprint: "declined"}
    row = s.findings("a/b", 1)[0]
    assert (row["first_head"], row["last_head"]) == ("h1", "h2")


def test_shadow_finding_can_be_posted_later(tmp_path: Path, clock: FakeClock) -> None:
    s = Store(tmp_path / "s.db", clock=clock)
    f = fnd("x")
    s.record_review(ReviewRecord(repo="a/b", pr=1, head_sha="h1", outcome="shadow"), [f], [])
    assert s.known_state("a/b", 1) == {f.fingerprint: "stored"}
    s.record_review(ReviewRecord(repo="a/b", pr=1, head_sha="h2", outcome="posted", posted=True),
                    [f], [])
    assert s.known_state("a/b", 1) == {f.fingerprint: "posted"}


def test_feedback_rejects_unknown_kind_and_unknown_finding(tmp_path: Path) -> None:
    s = Store(tmp_path / "s.db")
    with pytest.raises(ValueError):
        s.add_feedback("a/b", 1, "f", "liked")
    assert s.add_feedback("a/b", 1, "nope", "resolved") is False
