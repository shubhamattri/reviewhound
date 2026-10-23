"""Worker: take jobs off the queue and run reviews. Failures are classified, never dropped."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import cast

from reviewhound.config import ConfigError
from reviewhound.heartbeat import DEFAULT_MAX_JOB_SECONDS, LeaseHeartbeat
from reviewhound.metrics import (
    BacklogAlarm,
    Increment,
    MetricsLog,
    failure_increments,
    review_increments,
)
from reviewhound.pipeline import PostRefused, RecordAfterPostError, Reviewer
from reviewhound.providers import ProviderError
from reviewhound.queue import Job, LeaseLost, WorkerQueue
from reviewhound.vcs.github import GitHubError

log = logging.getLogger(__name__)


# An expired token, a timeout or a rate limit can pass on a later try.
POST_RETRYABLE_HTTP = (401, 403, 408, 429)


def is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, (ProviderError, RecordAfterPostError)):
        return True
    if isinstance(exc, PostRefused):
        return exc.status in POST_RETRYABLE_HTTP
    if isinstance(exc, GitHubError):
        return exc.status is None or exc.status >= 500 or exc.status == 429
    if isinstance(exc, ConfigError):
        return False
    if getattr(exc, "sqlstate", None) in ("40P01", "40001"):  # Postgres deadlock, serialization
        return True
    return isinstance(exc, (TimeoutError, ConnectionError, OSError))


def _record(metrics: MetricsLog | None, increments: list[Increment], job: Job) -> None:
    # Runs after the job state is written; a metrics outage must not fail or retry the job.
    if metrics is None:
        return
    try:
        metrics.add(increments)
    except Exception as exc:  # noqa: BLE001 - logged, and the job outcome is already durable
        log.error("metrics write failed", extra={"job": job.id, "error": type(exc).__name__})


def _reviewer_for(reviewer: Reviewer | Callable[[Job], Reviewer], job: Job) -> Reviewer:
    # Duck-typed: anything with .review() is a reviewer, anything else is a per-job factory.
    if hasattr(reviewer, "review"):
        return cast(Reviewer, reviewer)
    return reviewer(job)


LEASE_LOST_ERROR = ("LeaseLost: review ran past --max-job-seconds or lost its lease; "
                    "nothing was posted")


def _fail(queue: WorkerQueue, job: Job, error: str, retryable: bool, kind: str,
          delay_s: float = 0.0) -> None:
    try:
        state = queue.fail(job, error, retryable=retryable, delay_s=delay_s)
    except LeaseLost:
        log.warning("lease lost before recording failure", extra={"job": job.id})
    else:
        log.error("job failed", extra={"job": job.id, "error": kind, "new_state": state})


POST_UNCERTAIN_RETRY_S = 60.0


def run_one(queue: WorkerQueue, reviewer: Reviewer | Callable[[Job], Reviewer],
            metrics: MetricsLog | None = None,
            max_job_seconds: float = DEFAULT_MAX_JOB_SECONDS) -> Job | None:
    """`reviewer` may be a factory, so each job can use its own installation's token."""
    job = queue.claim()
    if job is None:
        return None
    log.info("job claimed", extra={"job": job.id, "repo": job.repo, "pr": job.pr,
                                   "head": job.head_sha, "attempt": job.attempts})
    try:
        runner = _reviewer_for(reviewer, job)
        with LeaseHeartbeat(queue, job, max_job_seconds=max_job_seconds) as hb:
            result = runner.review(job.repo, job.pr, dry_run=False, expected_head=job.head_sha,
                                   should_post=lambda: not hb.lost)
    except Exception as exc:  # noqa: BLE001 - every failure must be classified, not lost
        _fail(queue, job, f"{type(exc).__name__}: {exc}", is_retryable(exc), type(exc).__name__)
        _record(metrics, failure_increments(type(exc).__name__), job)
        return job
    if result.outcome == "lease-lost":
        # Nothing was posted: hand the job back (or to dead after max attempts), never done.
        _fail(queue, job, LEASE_LOST_ERROR, True, "LeaseLost")
        _record(metrics, review_increments(result.outcome, result.timings, result.cost_usd), job)
        return job
    if result.outcome == "claim-busy":
        # Another run may still post this head: look again once its claim can be taken over.
        _fail(queue, job, f"claim-busy: {result.error}", True, "ClaimBusy",
              delay_s=result.retry_after_s)
        _record(metrics, review_increments(result.outcome, result.timings, result.cost_usd), job)
        return job
    if result.outcome == "post-uncertain":
        # It may or may not have landed: never call it done. The retry checks GitHub first.
        _fail(queue, job, f"post-uncertain: {result.error}", True, "PostUncertain",
              delay_s=POST_UNCERTAIN_RETRY_S)
        _record(metrics, review_increments(result.outcome, result.timings, result.cost_usd), job)
        return job
    if result.outcome == "post-rejected":
        # GitHub refused the review even with its comments in the body: a retry cannot help.
        _fail(queue, job, f"post-rejected: {result.error}", False, "PostRejected")
        _record(metrics, review_increments(result.outcome, result.timings, result.cost_usd), job)
        return job
    try:
        queue.complete(job)
    except LeaseLost:
        log.warning("lease lost before completion", extra={"job": job.id,
                                                           "outcome": result.outcome})
    _record(metrics, review_increments(result.outcome, result.timings, result.cost_usd), job)
    return job


def run_forever(queue: WorkerQueue, reviewer: Reviewer | Callable[[Job], Reviewer],
                idle_sleep_s: float = 5.0,
                should_stop: Callable[[], bool] = lambda: False,
                metrics: MetricsLog | None = None, alarm: BacklogAlarm | None = None,
                max_job_seconds: float = DEFAULT_MAX_JOB_SECONDS) -> None:
    while not should_stop():
        if alarm is not None:
            try:
                alarm.check()
            except Exception as exc:  # noqa: BLE001 - the alarm must never stop the worker
                log.error("backlog check failed", extra={"error": type(exc).__name__})
        if run_one(queue, reviewer, metrics, max_job_seconds) is None:
            time.sleep(idle_sleep_s)
