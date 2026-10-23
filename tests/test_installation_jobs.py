import sqlite3
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest

from reviewhound.config import ConfigError
from reviewhound.pipeline import Reviewer
from reviewhound.providers import FakeProvider, ProviderRegistry
from reviewhound.queue import Job, SQLiteQueue
from reviewhound.vcs.github import GitHubError
from reviewhound.vcs.github_app import AppNotInstalledError, GitHubApp, token_for_job
from reviewhound.vcs.memory import MemoryVCS
from reviewhound.webhook import handle_event
from reviewhound.worker import run_one
from tests.conftest import Backend, answer, make_pr
from tests.test_github_app import PEM, PUBLIC, T0, TokenServer
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


def test_enqueue_stores_installation_id(backend: Backend) -> None:
    q = backend.queue()
    q.enqueue("acme/shop", 7, "h1", installation_id=42)
    q.enqueue("acme/shop", 8, "h1")
    first, second = q.claim(), q.claim()
    assert first is not None and second is not None
    assert (first.pr, first.installation_id) == (7, 42)
    assert (second.pr, second.installation_id) == (8, None)


def test_redelivery_keeps_or_updates_installation_id(backend: Backend) -> None:
    q = backend.queue()
    job_id, _ = q.enqueue("acme/shop", 7, "h1")
    assert q.enqueue("acme/shop", 7, "h1", installation_id=42) == (job_id, False)
    assert q.enqueue("acme/shop", 7, "h1") == (job_id, False)
    job = q.get(job_id)
    assert job is not None and job.installation_id == 42
    q.enqueue("acme/shop", 7, "h1", installation_id=43)
    job = q.get(job_id)
    assert job is not None and job.installation_id == 43


def _old_jobs_table(backend: Backend) -> None:
    if backend.url is None:
        c = sqlite3.connect(backend.home / "q.db")
        c.executescript(OLD_SCHEMA)
        c.close()
        return
    import psycopg

    from reviewhound.backends.postgres import QUEUE_DDL
    with psycopg.connect(backend.url, autocommit=True) as pg:
        pg.execute(QUEUE_DDL[0])  # the jobs table as milestone 6 created it
        pg.execute("INSERT" + OLD_SCHEMA.split("INSERT", 1)[1])


def _job_columns(backend: Backend) -> list[str]:
    if backend.url is None:
        c = sqlite3.connect(backend.home / "q.db")
        cols = [r[1] for r in c.execute("PRAGMA table_info(jobs)")]
        c.close()
        return cols
    import psycopg
    with psycopg.connect(backend.url) as pg:
        rows = pg.execute("SELECT column_name FROM information_schema.columns "
                          "WHERE table_name = 'jobs'").fetchall()
    return [r[0] for r in rows]


def test_old_queue_db_is_migrated_in_place(backend: Backend) -> None:
    _old_jobs_table(backend)
    assert "installation_id" not in _job_columns(backend)
    q = backend.queue()
    backend.queue()  # a second process opening the same DB must not fail
    old = q.claim()
    assert old is not None and (old.head_sha, old.installation_id) == ("old", None)
    q.enqueue("acme/shop", 9, "h9", installation_id=5)
    new = q.claim()
    assert new is not None and (new.pr, new.installation_id) == (9, 5)
    assert _job_columns(backend).count("installation_id") == 1


def test_webhook_puts_installation_id_on_the_job(backend: Backend) -> None:
    q = backend.queue()
    assert handle_event(q, "pull_request", pr_event()) == (202, "job 1 queued")
    job = q.claim()
    assert job is not None and job.installation_id == 42
    assert handle_event(q, "pull_request", pr_event("def", installation=None)) == (
        202, "job 2 queued")
    job = q.claim()
    assert job is not None and job.installation_id is None


def test_webhook_rejects_malformed_installation_id(backend: Backend) -> None:
    q = backend.queue()
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
    assert token_for_job(None, job(None), static) == "ghp_static"
    assert len(calls) == 2


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
    monkeypatch.setattr(app, "installation_for_repo", lambda repo: {"acme/shop": 77}[repo])
    vcs = build(job(None)).vcs
    assert isinstance(vcs, GitHubVCS) and vcs._token() == "ghs_77"


class InstallationServer(TokenServer):
    """GET /repos/{r}/installation answers from `installs`; token exchange as TokenServer."""

    def __init__(self, installs: dict[str, int], lookup_statuses: list[int] | None = None) -> None:
        super().__init__()
        self.installs = installs
        self.lookup_statuses = list(lookup_statuses or [])
        self.lookups: list[httpx.Request] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path.endswith("/installation"):
            self.lookups.append(req)
            self.calls.append(req)
            if self.lookup_statuses:
                return httpx.Response(self.lookup_statuses.pop(0), json={"message": "nope"})
            repo = req.url.path.removeprefix("/repos/").removesuffix("/installation")
            if repo not in self.installs:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"id": self.installs[repo], "account": {}})
        return super().__call__(req)


def lookup_app(server: InstallationServer) -> GitHubApp:
    client = httpx.Client(transport=httpx.MockTransport(server), base_url="https://api.github.com")
    return GitHubApp("12345", PEM, client=client, clock=server.clock, sleep=lambda s: None)


def test_job_without_installation_looks_it_up_with_the_app_jwt() -> None:
    server = InstallationServer({"acme/shop": 77})
    provider = token_for_job(lookup_app(server), job(None), lambda: "ghp_static")
    assert not isinstance(provider, str) and provider() == "ghs_tok2"
    lookup, exchange = server.calls
    assert (lookup.method, lookup.url.path) == ("GET", "/repos/acme/shop/installation")
    bearer = lookup.headers["Authorization"].removeprefix("Bearer ")
    assert jwt.decode(bearer, PUBLIC, algorithms=["RS256"],
                      options={"verify_exp": False, "verify_iat": False}) == {
        "iss": "12345", "iat": int(T0) - 60, "exp": int(T0) + 540}
    assert exchange.url.path == "/app/installations/77/access_tokens"


def test_job_with_installation_id_never_looks_it_up() -> None:
    server = InstallationServer({"acme/shop": 77})
    provider = token_for_job(lookup_app(server), job(42), lambda: "ghp_static")
    assert not isinstance(provider, str) and provider() == "ghs_tok1"
    assert server.lookups == []
    assert [r.url.path for r in server.calls] == ["/app/installations/42/access_tokens"]


def test_app_not_installed_on_the_repo_is_a_permanent_clear_error() -> None:
    from reviewhound.worker import is_retryable
    server = InstallationServer({})
    with pytest.raises(AppNotInstalledError) as err:
        token_for_job(lookup_app(server), job(None), lambda: "ghp_static")
    assert str(err.value) == ("GitHub App 12345 is not installed on acme/shop "
                              "(GET /repos/acme/shop/installation -> 404)")
    assert isinstance(err.value, ConfigError) and is_retryable(err.value) is False
    assert len(server.lookups) == 1


def test_installation_lookup_retries_server_errors_but_not_auth_errors() -> None:
    from reviewhound.worker import is_retryable
    server = InstallationServer({"acme/shop": 77}, lookup_statuses=[502, 503])
    assert lookup_app(server).installation_for_repo("acme/shop") == 77
    assert len(server.lookups) == 3
    server = InstallationServer({"acme/shop": 77}, lookup_statuses=[500, 500, 500])
    with pytest.raises(GitHubError) as gone:
        lookup_app(server).installation_for_repo("acme/shop")
    assert gone.value.status == 500 and is_retryable(gone.value) is True
    server = InstallationServer({"acme/shop": 77}, lookup_statuses=[401])
    with pytest.raises(GitHubError) as denied:
        lookup_app(server).installation_for_repo("acme/shop")
    assert denied.value.status == 401 and is_retryable(denied.value) is False
    assert len(server.lookups) == 1


def test_malformed_installation_response_raises() -> None:
    class Bad(InstallationServer):
        def __call__(self, req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"id": "seven"})
    with pytest.raises(GitHubError, match="malformed installation response"):
        lookup_app(Bad({})).installation_for_repo("acme/shop")


def test_worker_marks_a_job_on_an_uninstalled_repo_dead_at_once(tmp_path: Path) -> None:
    q = SQLiteQueue(tmp_path / "q.db")
    q.enqueue("acme/shop", 7, "h1")
    app = lookup_app(InstallationServer({}))

    def factory(j: Job) -> Reviewer:
        token_for_job(app, j, lambda: "ghp_static")
        raise AssertionError("a reviewer must not be built without an installation")
    ran = run_one(q, factory)
    assert ran is not None
    dead = q.get(ran.id)
    assert dead is not None and (dead.state, dead.attempts) == ("dead", 1)
    assert dead.last_error == ("AppNotInstalledError: GitHub App 12345 is not installed on "
                               "acme/shop (GET /repos/acme/shop/installation -> 404)")
