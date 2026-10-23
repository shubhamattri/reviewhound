import sqlite3
from pathlib import Path
from typing import Any

import pytest

from reviewhound.config import ConfigError
from reviewhound.pipeline import Reviewer
from reviewhound.providers import FakeProvider, ProviderRegistry
from reviewhound.queue import Job, SQLiteQueue
from reviewhound.vcs.github_app import GitHubApp, token_for_job
from reviewhound.vcs.memory import MemoryVCS
from reviewhound.webhook import handle_event
from reviewhound.worker import run_one
from tests.conftest import answer, make_pr
from tests.test_github_app import PEM
from tests.test_pipeline import BASE, HEAD

# The jobs table exactly as milestone 1 and 2 created it, before installation_id existed.
OLD_SCHEMA = """
CREATE TABLE jobs (
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
INSERT INTO jobs (key, repo, pr, head_sha, state, max_attempts, run_after, created_at, updated_at)
VALUES ('acme/shop#7@old', 'acme/shop', 7, 'old', 'queued', 4, 0, 0, 0);
"""


def pr_event(sha: str = "abc", installation: Any = 42) -> dict[str, Any]:
    event: dict[str, Any] = {"action": "opened", "repository": {"full_name": "acme/shop"},
                             "pull_request": {"number": 7, "draft": False, "head": {"sha": sha}}}
    if installation is not None:
        event["installation"] = {"id": installation}
    return event


def test_enqueue_stores_installation_id(tmp_path: Path) -> None:
    q = SQLiteQueue(tmp_path / "q.db")
    q.enqueue("acme/shop", 7, "h1", installation_id=42)
    q.enqueue("acme/shop", 8, "h1")
    first, second = q.claim(), q.claim()
    assert first is not None and second is not None
    assert (first.pr, first.installation_id) == (7, 42)
    assert (second.pr, second.installation_id) == (8, None)


def test_redelivery_keeps_or_updates_installation_id(tmp_path: Path) -> None:
    q = SQLiteQueue(tmp_path / "q.db")
    job_id, _ = q.enqueue("acme/shop", 7, "h1")
    assert q.enqueue("acme/shop", 7, "h1", installation_id=42) == (job_id, False)
    assert q.enqueue("acme/shop", 7, "h1") == (job_id, False)
    job = q.get(job_id)
    assert job is not None and job.installation_id == 42
    q.enqueue("acme/shop", 7, "h1", installation_id=43)
    job = q.get(job_id)
    assert job is not None and job.installation_id == 43


def test_old_queue_db_is_migrated_in_place(tmp_path: Path) -> None:
    path = tmp_path / "q.db"
    c = sqlite3.connect(path)
    c.executescript(OLD_SCHEMA)
    c.close()
    q = SQLiteQueue(path)
    SQLiteQueue(path)  # a second process opening the same DB must not fail
    old = q.claim()
    assert old is not None and (old.head_sha, old.installation_id) == ("old", None)
    q.enqueue("acme/shop", 9, "h9", installation_id=5)
    new = q.claim()
    assert new is not None and (new.pr, new.installation_id) == (9, 5)
    c = sqlite3.connect(path)
    cols = [r[1] for r in c.execute("PRAGMA table_info(jobs)")]
    c.close()
    assert cols.count("installation_id") == 1


def test_webhook_puts_installation_id_on_the_job(tmp_path: Path) -> None:
    q = SQLiteQueue(tmp_path / "q.db")
    assert handle_event(q, "pull_request", pr_event()) == (202, "job 1 queued")
    job = q.claim()
    assert job is not None and job.installation_id == 42
    assert handle_event(q, "pull_request", pr_event("def", installation=None)) == (
        202, "job 2 queued")
    job = q.claim()
    assert job is not None and job.installation_id is None


def test_webhook_rejects_malformed_installation_id(tmp_path: Path) -> None:
    q = SQLiteQueue(tmp_path / "q.db")
    assert handle_event(q, "pull_request", pr_event(installation="x")) == (
        400, "malformed installation id")
    assert handle_event(q, "pull_request", pr_event(installation=True)) == (
        400, "malformed installation id")
    assert handle_event(q, "pull_request", {**pr_event(), "installation": "x"}) == (
        400, "malformed installation id")
    assert q.claim() is None


def job(installation_id: int | None) -> Job:
    return Job(id=1, repo="acme/shop", pr=7, head_sha="h", state="running", attempts=1,
               max_attempts=4, last_error=None, installation_id=installation_id)


def test_token_choice_per_job() -> None:
    app = GitHubApp("1", PEM)
    calls: list[int] = []

    def static() -> str:
        calls.append(1)
        return "ghp_static"
    provider = token_for_job(app, job(42), static)
    assert callable(provider) and calls == []
    assert token_for_job(None, job(42), static) == "ghp_static"
    assert token_for_job(app, job(None), static) == "ghp_static"
    assert token_for_job(None, job(None), static) == "ghp_static"
    assert len(calls) == 3


def test_app_provider_is_bound_to_the_jobs_installation(monkeypatch: pytest.MonkeyPatch) -> None:
    app = GitHubApp("1", PEM)
    asked: list[int] = []
    monkeypatch.setattr(app, "installation_token", lambda iid: asked.append(iid) or f"t{iid}")
    provider = token_for_job(app, job(42), lambda: "ghp_static")
    assert not isinstance(provider, str) and provider() == "t42"
    assert asked == [42]


def test_worker_builds_a_reviewer_per_job(tmp_path: Path) -> None:
    q = SQLiteQueue(tmp_path / "q.db")
    q.enqueue("acme/shop", 7, "h1", installation_id=42)
    seen: list[int | None] = []

    def factory(j: Job) -> Reviewer:
        seen.append(j.installation_id)
        vcs = MemoryVCS(make_pr(head="h1"), BASE, HEAD)
        return Reviewer(vcs, ProviderRegistry([FakeProvider([answer()])]), [], None)
    done = run_one(q, factory)
    assert done is not None and seen == [42]
    after = q.get(done.id)
    assert after is not None and after.state == "done"


def test_worker_factory_failure_is_classified_not_lost(tmp_path: Path) -> None:
    q = SQLiteQueue(tmp_path / "q.db")
    q.enqueue("acme/shop", 7, "h1")

    def factory(j: Job) -> Reviewer:
        raise ConfigError("job has no installation id and no static token")
    done = run_one(q, factory)
    assert done is not None
    after = q.get(done.id)
    assert after is not None and after.state == "dead"
    assert after.last_error == "ConfigError: job has no installation id and no static token"


def test_cli_app_factory_uses_installation_or_static_token(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import argparse

    from reviewhound.cli import _app_reviewer_factory
    from reviewhound.vcs.github import GitHubVCS
    app = GitHubApp("1", PEM)
    monkeypatch.setattr(app, "installation_token", lambda iid: f"ghs_{iid}")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    build = _app_reviewer_factory(argparse.Namespace(home=str(tmp_path), fake_answer=None, database_url=None), app)
    vcs = build(job(42)).vcs
    assert isinstance(vcs, GitHubVCS) and vcs._token() == "ghs_42"
    with pytest.raises(ConfigError, match="no installation id and GITHUB_TOKEN is not set"):
        build(job(None))
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_static")
    vcs = build(job(None)).vcs
    assert isinstance(vcs, GitHubVCS) and vcs._token() == "ghp_static"
