"""GitHub webhook receiver: verify the signature, enqueue one job per PR head, return fast."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from collections.abc import Callable, Iterable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from reviewhound.github_feedback import FEEDBACK_EVENTS, handle_feedback_event
from reviewhound.queue import JobQueue
from reviewhound.store import ReviewStore

log = logging.getLogger(__name__)
PR_ACTIONS = {"opened", "synchronize", "reopened", "ready_for_review"}
MAX_BODY = 5 * 1024 * 1024
READ_TIMEOUT_S = 10.0
BODY_DEADLINE_S = 30.0
METRICS_TOKEN_ENV = "REVIEWHOUND_METRICS_TOKEN"
ALLOWED_OWNERS_ENV = "REVIEWHOUND_ALLOWED_OWNERS"


def allowed_owners_from(env_value: str, flags: Iterable[str]) -> frozenset[str] | None:
    """Owners from the comma-separated env value plus --allowed-owner flags; None allows all."""
    owners = {o.strip().lower() for o in [*env_value.split(","), *flags] if o.strip()}
    return frozenset(owners) or None


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode(), header.encode("utf-8", "replace"))


def handle_event(queue: JobQueue, event: str, payload: dict[str, Any],
                 store: ReviewStore | None = None, app_id: str | None = None,
                 body_sha256: str = "",
                 allowed_owners: frozenset[str] | None = None) -> tuple[int, str]:
    """Return (HTTP status, message). Pure so it can be tested without a socket."""
    if event == "ping":
        return 200, "pong"
    full_name = (payload.get("repository") or {}).get("full_name")
    if allowed_owners and isinstance(full_name, str) and \
            full_name.split("/", 1)[0].lower() not in allowed_owners:
        log.info("event from an owner not on the allowlist ignored",
                 extra={"repo": full_name, "event": event})
        return 202, "ignored: owner not allowed"
    if event in FEEDBACK_EVENTS:
        if store is None:
            return 202, f"ignored event {event}: no store"
        return handle_feedback_event(store, event, payload, app_id, body_sha256)
    if event != "pull_request":
        return 202, f"ignored event {event}"
    action = payload.get("action")
    pr = payload.get("pull_request") or {}
    repo = (payload.get("repository") or {}).get("full_name")
    if action not in PR_ACTIONS:
        return 202, f"ignored action {action}"
    if pr.get("draft"):
        return 202, "draft PR ignored"
    try:
        number = int(pr["number"])
        head = str(pr["head"]["sha"])
    except (KeyError, TypeError, ValueError):
        return 400, "malformed pull_request payload"
    if not repo:
        return 400, "missing repository"
    inst = payload.get("installation")
    installation = inst.get("id") if isinstance(inst, dict) else inst
    if installation is not None and (isinstance(installation, bool)
                                     or not isinstance(installation, int)):
        return 400, "malformed installation id"
    job_id, created = queue.enqueue(repo, number, head, installation_id=installation)
    log.info("webhook enqueue", extra={"repo": repo, "pr": number, "head": head,
                                       "job": job_id, "new_job": created,
                                       "installation": installation})
    return 202, f"job {job_id} {'queued' if created else 'already queued'}"


def make_server(queue: JobQueue, secret: str, host: str, port: int,
                store: ReviewStore | None = None, app_id: str | None = None,
                metrics: Callable[[], str] | None = None,
                metrics_token: str | None = None,
                allowed_owners: frozenset[str] | None = None,
                read_timeout_s: float = READ_TIMEOUT_S,
                body_deadline_s: float = BODY_DEADLINE_S) -> ThreadingHTTPServer:
    """`metrics_token` None reads REVIEWHOUND_METRICS_TOKEN; empty leaves /metrics open."""
    if not secret:
        raise ValueError("webhook secret is required")
    token = os.environ.get(METRICS_TOKEN_ENV, "") if metrics_token is None else metrics_token
    expected_auth = f"Bearer {token}".encode() if token else None
    if metrics is not None and expected_auth is None:
        log.warning(f"/metrics is UNAUTHENTICATED: set {METRICS_TOKEN_ENV}")
    if not allowed_owners:
        log.warning("no owner allowlist: events from ANY repository owner are accepted; "
                    f"set {ALLOWED_OWNERS_ENV}")

    class Handler(BaseHTTPRequestHandler):
        timeout = read_timeout_s  # a client that stalls mid-body cannot hold a thread forever

        def _reply(self, status: int, message: str) -> None:
            data = json.dumps({"message": message}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            if self.path == "/metrics" and metrics is not None:
                self._metrics(metrics)
                return
            self._reply(200 if self.path == "/healthz" else 404, "ok" if self.path == "/healthz"
                        else "not found")

        def _metrics(self, render: Callable[[], str]) -> None:
            if expected_auth is not None:
                got = self.headers.get("Authorization", "").encode("utf-8", "replace")
                if not hmac.compare_digest(got, expected_auth):
                    log.warning("metrics request without the right bearer token")
                    self._reply(401, "metrics need a bearer token")
                    return
            try:
                data = render().encode()
            except Exception as exc:  # noqa: BLE001 - a scrape failure is a 500, not a crash
                log.error("metrics render failed", extra={"error": type(exc).__name__})
                self._reply(500, "metrics unavailable")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:
            if self.path != "/webhook":
                self._reply(404, "not found")
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0 or length > MAX_BODY:
                self._reply(413 if length > MAX_BODY else 400, "bad body length")
                return
            body = self._read_body(length)
            if body is None:
                log.warning("webhook body not received within the deadline",
                            extra={"deadline_s": body_deadline_s, "length": length})
                self.close_connection = True
                self._reply(408, "body not received in time")
                return
            if not verify_signature(secret, body, self.headers.get("X-Hub-Signature-256")):
                self._reply(401, "bad signature")
                return
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                self._reply(400, "body is not JSON")
                return
            status, message = handle_event(queue, self.headers.get("X-GitHub-Event", ""),
                                           payload if isinstance(payload, dict) else {},
                                           store, app_id, hashlib.sha256(body).hexdigest(),
                                           allowed_owners)
            self._reply(status, message)

        def _read_body(self, length: int) -> bytes | None:
            """`length` bytes, or None once `body_deadline_s` has passed, however the client
            paces its bytes. A stall longer than the per-read timeout still raises TimeoutError."""
            deadline = time.monotonic() + body_deadline_s
            chunks: list[bytes] = []
            got = 0
            while got < length:
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                self.connection.settimeout(min(read_timeout_s, left))
                try:
                    chunk = self.rfile.read1(min(65_536, length - got))
                except TimeoutError:
                    if time.monotonic() >= deadline:
                        return None
                    raise
                if not chunk:
                    break  # the client hung up: a short body fails the signature check
                chunks.append(chunk)
                got += len(chunk)
            return b"".join(chunks)

        def log_message(self, format: str, *args: Any) -> None:
            log.debug("http " + format, *args)

    return ThreadingHTTPServer((host, port), Handler)
