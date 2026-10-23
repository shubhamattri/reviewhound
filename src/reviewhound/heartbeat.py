"""Keep a running job's lease alive while a long review runs, so no other worker steals it."""

from __future__ import annotations

import logging
import threading
from types import TracebackType
from typing import Protocol, Self

from reviewhound.queue import Job, LeaseLost

log = logging.getLogger(__name__)


class LeaseQueue(Protocol):
    lease_s: float

    def extend_lease(self, job: Job) -> float: ...


class LeaseHeartbeat:
    """Context manager: renews the lease every `interval_s` (default lease_s / 3) in a thread."""

    def __init__(self, queue: LeaseQueue, job: Job, interval_s: float | None = None,
                 join_timeout_s: float = 60.0) -> None:
        self.queue = queue
        self.job = job
        self.interval_s = interval_s if interval_s is not None else queue.lease_s / 3
        self.join_timeout_s = join_timeout_s
        self.beats = 0
        self.lost = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def beat(self) -> bool:
        """Renew once. Returns False when the lease is gone and renewing must stop."""
        try:
            self.queue.extend_lease(self.job)
        except LeaseLost:
            self.lost = True
            log.error("lease lost while the job was running", extra={
                "event": "lease_lost", "job": self.job.id, "repo": self.job.repo,
                "pr": self.job.pr, "head": self.job.head_sha})
            return False
        except Exception as exc:  # noqa: BLE001 - a DB blip must not end renewal; next beat retries
            log.warning("lease renewal failed, will retry", extra={
                "job": self.job.id, "error": type(exc).__name__})
            return True
        self.beats += 1
        return True

    def _run(self) -> None:
        while not self._stop.wait(self.interval_s):
            if not self.beat():
                return

    def __enter__(self) -> Self:
        self._thread = threading.Thread(target=self._run, name=f"lease-{self.job.id}",
                                        daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 tb: TracebackType | None) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(self.join_timeout_s)
            if self._thread.is_alive():
                log.error("lease heartbeat did not stop", extra={"job": self.job.id})
