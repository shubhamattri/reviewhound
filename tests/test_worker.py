from pathlib import Path

from reviewhound.config import ConfigError
from reviewhound.pipeline import Reviewer
from reviewhound.providers import FakeProvider, ProviderError, ProviderRegistry
from reviewhound.queue import SQLiteQueue
from reviewhound.vcs.github import GitHubError
from reviewhound.vcs.memory import MemoryVCS
from reviewhound.worker import is_retryable, run_one
from tests.conftest import answer, make_pr
from tests.test_pipeline import BASE, HEAD


def test_failure_classification() -> None:
    assert is_retryable(ProviderError("x")) is True
    assert is_retryable(GitHubError("x", 502)) is True
    assert is_retryable(GitHubError("x", 429)) is True
    assert is_retryable(GitHubError("x", 404)) is False
    assert is_retryable(ConfigError("x")) is False
    assert is_retryable(ValueError("x")) is False


def test_worker_completes_job_for_current_head(tmp_path: Path) -> None:
    q = SQLiteQueue(tmp_path / "q.db")
    q.enqueue("acme/shop", 7, "h1")
    vcs = MemoryVCS(make_pr(head="h1"), BASE, HEAD)
    reviewer = Reviewer(vcs, ProviderRegistry([FakeProvider([answer()])]), [], None)
    job = run_one(q, reviewer)
    assert job is not None
    done = q.get(job.id)
    assert done is not None and done.state == "done"
    assert run_one(q, reviewer) is None


def test_worker_retries_model_outage_and_kills_config_errors(tmp_path: Path) -> None:
    q = SQLiteQueue(tmp_path / "q.db", backoff_base_s=0)
    q.enqueue("acme/shop", 7, "h1")
    vcs = MemoryVCS(make_pr(head="h1"), BASE, HEAD)
    down = Reviewer(vcs, ProviderRegistry([FakeProvider([])]), [], None)
    job = run_one(q, down)
    assert job is not None
    after = q.get(job.id)
    assert after is not None and (after.state, after.attempts) == ("queued", 1)
    assert after.last_error is not None and after.last_error.startswith("ProviderError")

    bad = MemoryVCS(make_pr(head="h1"), {**BASE, ".reviewhound.yml": "nope: 1\n"}, HEAD)
    run_one(q, Reviewer(bad, ProviderRegistry([FakeProvider([])]), [], None))
    dead = q.get(job.id)
    assert dead is not None and dead.state == "dead"
