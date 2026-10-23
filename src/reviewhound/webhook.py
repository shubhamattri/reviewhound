"""GitHub webhook receiver: verify the signature, enqueue one job per PR head, return fast."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from reviewhound.queue import JobQueue

log = logging.getLogger(__name__)
PR_ACTIONS = {"opened", "synchronize", "reopened", "ready_for_review"}
MAX_BODY = 5 * 1024 * 1024


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def handle_event(queue: JobQueue, event: str, payload: dict[str, Any]) -> tuple[int, str]:
    """Return (HTTP status, message). Pure so it can be tested without a socket."""
    if event == "ping":
        return 200, "pong"
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
    job_id, created = queue.enqueue(repo, number, head)
    log.info("webhook enqueue", extra={"repo": repo, "pr": number, "head": head,
                                       "job": job_id, "new_job": created})
    return 202, f"job {job_id} {'queued' if created else 'already queued'}"


def make_server(queue: JobQueue, secret: str, host: str, port: int) -> ThreadingHTTPServer:
    if not secret:
        raise ValueError("webhook secret is required")

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, status: int, message: str) -> None:
            data = json.dumps({"message": message}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            self._reply(200 if self.path == "/healthz" else 404, "ok" if self.path == "/healthz"
                        else "not found")

        def do_POST(self) -> None:
            if self.path != "/webhook":
                self._reply(404, "not found")
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY:
                self._reply(413 if length > MAX_BODY else 400, "bad body length")
                return
            body = self.rfile.read(length)
            if not verify_signature(secret, body, self.headers.get("X-Hub-Signature-256")):
                self._reply(401, "bad signature")
                return
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                self._reply(400, "body is not JSON")
                return
            status, message = handle_event(queue, self.headers.get("X-GitHub-Event", ""),
                                           payload if isinstance(payload, dict) else {})
            self._reply(status, message)

        def log_message(self, format: str, *args: Any) -> None:
            log.debug("http " + format, *args)

    return ThreadingHTTPServer((host, port), Handler)
