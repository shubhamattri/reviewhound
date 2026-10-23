from pathlib import Path

import pytest

from reviewhound.config import ConfigError
from reviewhound.pipeline import Reviewer
from reviewhound.providers import FakeProvider, ProviderError, ProviderRegistry
from reviewhound.scanners import BuiltinScanner
from reviewhound.store import Store
from reviewhound.vcs.memory import MemoryVCS
from tests.conftest import answer, make_pr

BASE = {"app/admin.py": '''def require_role(req, role):
    pass


def get_user(req, uid):
    require_role(req, "admin")
    return uid
'''}
HEAD = {"app/admin.py": BASE["app/admin.py"] + '''

def delete_user(req, uid):
    db.delete(uid)
    return True
'''}
REAL = {"file": "app/admin.py", "line": 11, "severity": "blocker", "category": "auth",
        "title": "delete_user skips the role check", "body": "Anyone can delete users.",
        "evidence_quote": "db.delete(uid)",
        "claims": [{"kind": "not_calls", "caller": "delete_user", "callee": "require_role"}]}
FAKE = {"file": "app/admin.py", "line": 11, "severity": "major", "category": "auth",
        "title": "uses missing helper", "body": "...", "evidence_quote": "db.delete(uid)",
        "claims": [{"kind": "symbol_exists", "name": "audit_log"}]}


def setup(answers: list[str], posting: bool, tmp_path: Path, head_now: str = "h1"
          ) -> tuple[MemoryVCS, Reviewer, Store]:
    base = dict(BASE)
    if posting:
        base[".reviewhound.yml"] = "posting: true\n"
    head = {**HEAD, **({".reviewhound.yml": "posting: true\n"} if posting else {})}
    vcs = MemoryVCS(make_pr(head="h1"), base, head)
    vcs.head_sha_now = head_now
    store = Store(tmp_path / "s.db")
    reviewer = Reviewer(vcs, ProviderRegistry([FakeProvider(answers)]), [BuiltinScanner()], store)
    return vcs, reviewer, store


def test_dry_run_prints_review_and_posts_and_stores_nothing(tmp_path: Path) -> None:
    vcs, reviewer, store = setup([answer(REAL, FAKE)], posting=True, tmp_path=tmp_path)
    result = reviewer.review("acme/shop", 7, dry_run=True)
    assert result.outcome == "dry-run"
    assert vcs.posted == []
    assert store.reviews("acme/shop", 7) == []
    assert result.review is not None and result.review.event == "REQUEST_CHANGES"
    assert [(c.path, c.line) for c in result.review.comments] == [("app/admin.py", 11)]
    assert [r for _, r in result.dropped] == ["false claim: symbol audit_log does not exist"]
    assert '"line": 11' in result.rendered
    assert result.reviewers == ["fake:ok", "scanner:builtin"]
    assert set(result.timings) == {"fetch", "diff", "index", "context", "model", "verify",
                                   "scan", "post"}


def test_shadow_mode_stores_findings_but_posts_nothing(tmp_path: Path) -> None:
    vcs, reviewer, store = setup([answer(REAL)], posting=False, tmp_path=tmp_path)
    result = reviewer.review("acme/shop", 7, dry_run=False)
    assert result.outcome == "shadow"
    assert vcs.posted == []
    assert [(f["state"], f["line"]) for f in store.findings("acme/shop", 7)] == [("stored", 11)]


def test_posting_on_posts_once_and_never_repeats_on_next_head(tmp_path: Path) -> None:
    vcs, reviewer, _store = setup([answer(REAL), answer(REAL)], posting=True, tmp_path=tmp_path)
    first = reviewer.review("acme/shop", 7, dry_run=False)
    assert first.outcome == "posted"
    assert [len(p.comments) for p in vcs.posted] == [1]
    vcs.pr = make_pr(head="h2")
    vcs.head_sha_now = "h2"
    second = reviewer.review("acme/shop", 7, dry_run=False)
    assert second.outcome == "posted"
    assert [len(p.comments) for p in vcs.posted] == [1, 0]
    assert second.decision is not None
    assert [r for _, r in second.decision.held] == ["already posted on an earlier head"]


def test_head_moved_during_review_posts_nothing(tmp_path: Path) -> None:
    vcs, reviewer, store = setup([answer(REAL)], posting=True, tmp_path=tmp_path, head_now="h2")
    result = reviewer.review("acme/shop", 7, dry_run=False)
    assert result.outcome == "head-moved"
    assert vcs.posted == []
    assert [r["outcome"] for r in store.reviews("acme/shop", 7)] == ["head-moved"]


def test_job_for_an_old_head_is_superseded_without_model_call(tmp_path: Path) -> None:
    vcs, reviewer, _store = setup([], posting=True, tmp_path=tmp_path)
    result = reviewer.review("acme/shop", 7, dry_run=False, expected_head="h0")
    assert result.outcome == "superseded"
    assert vcs.posted == []


def test_malformed_model_answer_is_retried_once_then_fails(tmp_path: Path) -> None:
    _, reviewer, _ = setup(["nope", answer(REAL)], posting=False, tmp_path=tmp_path)
    assert reviewer.review("acme/shop", 7, dry_run=True).outcome == "dry-run"
    _, reviewer, _ = setup(["nope", "still nope"], posting=False, tmp_path=tmp_path)
    with pytest.raises(ProviderError, match="did not match the schema"):
        reviewer.review("acme/shop", 7, dry_run=True)


def test_config_comes_from_base_so_a_pr_cannot_turn_posting_on(tmp_path: Path) -> None:
    vcs = MemoryVCS(make_pr(head="h1"), BASE, {**HEAD, ".reviewhound.yml": "posting: true\n"})
    reviewer = Reviewer(vcs, ProviderRegistry([FakeProvider([answer(REAL)])]), [],
                        Store(tmp_path / "s.db"))
    assert reviewer.review("acme/shop", 7, dry_run=False).outcome == "shadow"
    assert vcs.posted == []


def test_invalid_config_fails_loudly(tmp_path: Path) -> None:
    vcs = MemoryVCS(make_pr(head="h1"), {**BASE, ".reviewhound.yml": "postng: true\n"}, HEAD)
    reviewer = Reviewer(vcs, ProviderRegistry([FakeProvider([])]), [], None)
    with pytest.raises(ConfigError):
        reviewer.review("acme/shop", 7, dry_run=True)


def test_post_failure_is_recorded_as_uncertain(tmp_path: Path) -> None:
    vcs, reviewer, store = setup([answer(REAL)], posting=True, tmp_path=tmp_path)

    def boom(pr, review):  # type: ignore[no-untyped-def]
        raise TimeoutError("socket")
    vcs.post_review = boom  # type: ignore[method-assign]
    result = reviewer.review("acme/shop", 7, dry_run=False)
    assert (result.outcome, result.error) == ("post-uncertain", "TimeoutError")
    assert [(r["outcome"], r["posted"]) for r in store.reviews("acme/shop", 7)] == [
        ("post-uncertain", 1)]


def test_language_turned_off_in_config_gives_unknown_claims_not_dropped_findings() -> None:
    base = {".reviewhound.yml": "languages: [python]\n", "svc/a.go": "package svc\n"}
    head = {**base, "svc/a.go": "package svc\n\nfunc Drop() {\n\tdb.Exec(q)\n}\n"}
    go = {"file": "svc/a.go", "line": 4, "severity": "major", "category": "c", "title": "t",
          "body": "b", "evidence_quote": "db.Exec(q)",
          "claims": [{"kind": "symbol_exists", "name": "Drop"}]}
    vcs = MemoryVCS(make_pr(head="h1"), base, head)
    reviewer = Reviewer(vcs, ProviderRegistry([FakeProvider([answer(go)])]), [], None)
    result = reviewer.review("acme/shop", 7, dry_run=True)
    assert result.dropped == []
    assert result.decision is not None
    assert [(f.file, r, f.claim_results[0].verdict) for f, r in result.decision.held] == [
        ("svc/a.go", "unverified", "unknown")]


def test_closed_pr_is_skipped_without_model_call(tmp_path: Path) -> None:
    vcs = MemoryVCS(make_pr(head="h1", state="closed"), BASE, HEAD)
    reviewer = Reviewer(vcs, ProviderRegistry([FakeProvider([])]), [], None)
    assert reviewer.review("acme/shop", 7, dry_run=False).outcome == "skipped"


def test_second_worker_on_same_head_does_not_post_again(tmp_path: Path) -> None:
    vcs, reviewer, store = setup([answer(REAL), answer(REAL)], posting=True, tmp_path=tmp_path)
    assert store.claim_post("acme/shop", 7, "h1") is True  # another worker got there first
    result = reviewer.review("acme/shop", 7, dry_run=False)
    assert result.outcome == "already-posted"
    assert vcs.posted == []


def test_uncertain_post_is_remembered_as_posted_so_it_is_never_doubled(tmp_path: Path) -> None:
    vcs, reviewer, store = setup([answer(REAL), answer(REAL)], posting=True, tmp_path=tmp_path)
    real_post = vcs.post_review

    def boom(pr, review):  # type: ignore[no-untyped-def]
        raise TimeoutError("socket")
    vcs.post_review = boom  # type: ignore[method-assign]
    assert reviewer.review("acme/shop", 7, dry_run=False).outcome == "post-uncertain"
    assert [f["state"] for f in store.findings("acme/shop", 7)] == ["posted"]
    vcs.post_review = real_post  # type: ignore[method-assign]
    vcs.pr = make_pr(head="h2")
    vcs.head_sha_now = "h2"
    reviewer.review("acme/shop", 7, dry_run=False)
    assert [len(p.comments) for p in vcs.posted] == [0]
