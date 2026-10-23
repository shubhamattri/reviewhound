import hashlib
import hmac
import http.client
import json
import threading
from pathlib import Path
from typing import Any

from reviewhound.queue import SQLiteQueue
from reviewhound.webhook import handle_event, make_server, verify_signature

SECRET = "s3cret"


def pr_event(action: str = "opened", draft: bool = False, sha: str = "abc") -> dict[str, Any]:
    return {"action": action, "repository": {"full_name": "acme/shop"},
            "pull_request": {"number": 7, "draft": draft, "head": {"sha": sha}}}


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def test_signature_check() -> None:
    body = b'{"a":1}'
    assert verify_signature(SECRET, body, sign(body)) is True
    assert verify_signature(SECRET, body + b" ", sign(body)) is False
    assert verify_signature(SECRET, body, None) is False
    assert verify_signature("", body, sign(body)) is False


def test_redelivered_event_makes_one_job(tmp_path: Path) -> None:
    q = SQLiteQueue(tmp_path / "q.db")
    assert handle_event(q, "pull_request", pr_event()) == (202, "job 1 queued")
    assert handle_event(q, "pull_request", pr_event("synchronize")) == (202, "job 1 already queued")
    assert handle_event(q, "pull_request", pr_event("synchronize", sha="def")) == (202,
                                                                                  "job 2 queued")


def test_irrelevant_events_enqueue_nothing(tmp_path: Path) -> None:
    q = SQLiteQueue(tmp_path / "q.db")
    assert handle_event(q, "ping", {}) == (200, "pong")
    assert handle_event(q, "issues", {}) == (202, "ignored event issues")
    assert handle_event(q, "pull_request", pr_event("closed")) == (202, "ignored action closed")
    assert handle_event(q, "pull_request", pr_event(draft=True)) == (202, "draft PR ignored")
    assert handle_event(q, "pull_request", {"action": "opened", "pull_request": {}}) == (
        400, "malformed pull_request payload")
    assert q.claim() is None


def test_http_server_rejects_bad_signature_and_accepts_good_one(tmp_path: Path) -> None:
    q = SQLiteQueue(tmp_path / "q.db")
    server = make_server(q, SECRET, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        body = json.dumps(pr_event()).encode()

        def post(sig: str) -> tuple[int, str]:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("POST", "/webhook", body, {"X-Hub-Signature-256": sig,
                                                    "X-GitHub-Event": "pull_request",
                                                    "Content-Type": "application/json"})
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read())["message"]

        assert post("sha256=bad") == (401, "bad signature")
        assert q.claim() is None
        assert post(sign(body)) == (202, "job 1 queued")
    finally:
        server.shutdown()
        server.server_close()
