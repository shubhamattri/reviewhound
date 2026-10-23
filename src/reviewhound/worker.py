"""Worker: take jobs off the queue and run reviews. Failures are classified, never dropped."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from reviewhound.config import ConfigError
from reviewhound.pipeline import Reviewer
from reviewhound.providers import ProviderError
from reviewhound.queue import Job, LeaseLost, SQLiteQueue
from reviewhound.vcs.github import GitHubError

log = logging.getLogger(__name__)


def is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, ProviderError):
        return True
    if isinstance(exc, GitHubError):
        return exc.status is None or exc.status >= 500 or exc.status == 429
    if isinstance(exc, ConfigError):
        return False
    return isinstance(exc, (TimeoutError, ConnectionError, OSError))


def run_one(queue: SQLiteQueue, reviewer: Reviewer) -> Job | None:
    job = queue.claim()
    if job is None:
        return None
    log.info("job claimed", extra={"job": job.id, "repo": job.repo, "pr": job.pr,
                                   "head": job.head_sha, "attempt": job.attempts})
    try:
        result = reviewer.review(job.repo, job.pr, dry_run=False, expected_head=job.head_sha)
    except Exception as exc:  # noqa: BLE001 - every failure must be classified, not lost
        retry = is_retryable(exc)
        try:
            state = queue.fail(job, f"{type(exc).__name__}: {exc}", retryable=retry)
        except LeaseLost:
            log.warning("lease lost before recording failure", extra={"job": job.id})
            return job
        log.error("job failed", extra={"job": job.id, "error": type(exc).__name__,
                                       "new_state": state})
        return job
    try:
        queue.complete(job)
    except LeaseLost:
        log.warning("lease lost before completion", extra={"job": job.id,
                                                           "outcome": result.outcome})
    return job


def run_forever(queue: SQLiteQueue, reviewer: Reviewer, idle_sleep_s: float = 5.0,
                should_stop: Callable[[], bool] = lambda: False) -> None:
    while not should_stop():
        if run_one(queue, reviewer) is None:
            time.sleep(idle_sleep_s)
