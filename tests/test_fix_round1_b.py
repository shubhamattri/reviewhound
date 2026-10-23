"""Fix round 1, track B: anchoring, fingerprints, child env, store order, feedback, dismissal."""

from __future__ import annotations

import base64
import http.client
import json
import logging
import socket
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from reviewhound.checker import ClaimChecker
from reviewhound.config import RepoConfig, Severity
from reviewhound.findings import Finding, ModelFinding
from reviewhound.github_feedback import DISMISS_MESSAGE, open_reason
from reviewhound.index.treesitter import CodeIndex
from reviewhound.pipeline import comments_in_body
from reviewhound.procenv import child_env
from reviewhound.queue import SQLiteQueue
from reviewhound.scanners import SemgrepScanner, secret_identity
from reviewhound.store import ReviewRecord
from reviewhound.vcs.base import DictSnapshot, PullRequest, ReviewComment, ReviewPayload
from reviewhound.vcs.github import GitHubError, GitHubVCS
from reviewhound.vcs.memory import diff_file_maps
from reviewhound.webhook import allowed_owners_from, handle_event, make_server
from reviewhound.worker import is_retryable
from tests.conftest import PG_URL, Backend, answer, spy
from tests.test_github_feedback import FIXED, push, reviewer_on, state_of, thread
from tests.test_pipeline import HEAD, REAL
from tests.test_webhook import SECRET, sign

# B7: a blank line added or removed inside a quote must not anchor unchanged code.
SPAN_BASE = {"m.py": "def f():\n    first_call_here()\n    second_call_here()\n    return 1\n"}
SPAN_HEAD = {"m.py": "def f():\n    first_call_here()\n\n    second_call_here()\n    return 1\n"}
SPAN_QUOTE = "first_call_here()\nsecond_call_here()"


def span_finding(line: int) -> ModelFinding:
    return ModelFinding.model_validate({
        "file": "m.py", "line": line, "severity": "major", "title": "t", "body": "b",
        "evidence_quote": SPAN_QUOTE})


def test_blank_line_added_inside_a_quote_does_not_anchor_it() -> None:
    chk = ClaimChecker(diff_file_maps(SPAN_BASE, SPAN_HEAD), CodeIndex.build(SPAN_HEAD))
    _f, reason = chk.check(span_finding(3), "model:m")
    assert reason == "evidence quote is only in code this PR did not change"


def test_blank_line_deleted_inside_a_quote_does_not_anchor_it_on_the_left() -> None:
    chk = ClaimChecker(diff_file_maps(SPAN_HEAD, SPAN_BASE), CodeIndex.build(SPAN_BASE),
                       base_reader=SPAN_HEAD.get)
    f, reason = chk.check(span_finding(3), "model:m")
    assert (reason, f.side) == ("evidence quote is only in code this PR did not change", "RIGHT")


def test_a_real_change_inside_the_quote_still_anchors_on_its_non_blank_line() -> None:
    head = {"m.py": SPAN_HEAD["m.py"].replace("second_call_here()", "second_call_here()  # x")}
    chk = ClaimChecker(diff_file_maps(SPAN_BASE, head), CodeIndex.build(head))
    f, reason = chk.check(span_finding(3), "model:m")
    assert (reason, f.line, f.side) == (None, 4, "RIGHT")


# B8: LEFT and RIGHT findings with the same quote are different findings.

def _fp(side: str, **kw: Any) -> str:
    f = Finding(file="app/admin.py", line=11, severity=4,  # type: ignore[arg-type]
                category="auth", title="t", body="", evidence_quote="db.delete(uid)", source="",
                side=side, **kw)  # type: ignore[arg-type]
    return f.fingerprint


def test_left_side_is_part_of_the_fingerprint_and_right_stays_stable() -> None:
    assert _fp("RIGHT") == "68eb534220237c8b"  # the value before side existed
    assert _fp("LEFT") != _fp("RIGHT")
    assert len(_fp("LEFT")) == 16
    assert _fp("LEFT", identity="abc") != _fp("RIGHT", identity="abc")


# B9: child processes get PATH, HOME, LANG and what they need, never the service's tokens.

LEAKY = {"GITHUB_TOKEN": "ghp_x", "ANTHROPIC_API_KEY": "sk-x", "CLAUDE_CODE_OAUTH_TOKEN": "o",
         "REVIEWHOUND_WEBHOOK_SECRET": "w", "REVIEWHOUND_DATABASE_URL": "pg-url-with-password",
         "AWS_SECRET_ACCESS_KEY": "a"}
SAFE = {"PATH": "/usr/bin:/bin", "HOME": "/home/rh", "LANG": "C.UTF-8"}


def _leaky_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in {**LEAKY, **SAFE}.items():
        monkeypatch.setenv(k, v)


def _capture(monkeypatch: pytest.MonkeyPatch, module: Any,
             stdout: str = "") -> list[tuple[list[str], dict[str, str]]]:
    calls: list[tuple[list[str], dict[str, str]]] = []

    def run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append((cmd, dict(kw["env"])))
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")
    monkeypatch.setattr(module.subprocess, "run", run)
    return calls


def test_git_fetch_env_holds_only_the_auth_header(monkeypatch: pytest.MonkeyPatch,
                                                  tmp_path: Path) -> None:
    import reviewhound.vcs.github as gh_mod
    _leaky_env(monkeypatch)
    calls = _capture(monkeypatch, gh_mod)
    GitHubVCS("tok123", tmp_path).snapshot("acme/shop", "abc")
    basic = base64.b64encode(b"x-access-token:tok123").decode()
    want = {**SAFE, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}"}
    assert [c[0][1] for c in calls] == ["init", "remote", "fetch", "checkout"]
    assert [env for _cmd, env in calls] == [want] * 4


def test_git_log_env_has_no_tokens_at_all(monkeypatch: pytest.MonkeyPatch,
                                          tmp_path: Path) -> None:
    import reviewhound.vcs.github as gh_mod
    _leaky_env(monkeypatch)
    dest = tmp_path / "acme__shop" / "abc"
    dest.mkdir(parents=True)
    (dest / ".reviewhound-complete").write_text("abc")
    calls = _capture(monkeypatch, gh_mod, stdout="a.py\na.py\n")
    assert GitHubVCS("tok123", tmp_path).churn("acme/shop", "abc") == {"a.py": 2}
    assert calls == [(["git", "log", "--name-only", "--format="],
                      {**SAFE, "GIT_TERMINAL_PROMPT": "0"})]


def test_semgrep_env_has_no_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    import reviewhound.scanners as sc_mod
    _leaky_env(monkeypatch)
    calls = _capture(monkeypatch, sc_mod, stdout='{"results": []}')
    scanner = SemgrepScanner()
    scanner.binary = "/opt/semgrep"
    assert scanner.scan(diff_file_maps({}, {"a.py": "x = 1\n"}),
                        DictSnapshot({"a.py": "x = 1\n"}), RepoConfig()) == []
    assert [env for _cmd, env in calls] == [SAFE]


# B5: findings are upserted in fingerprint order, so two workers lock rows in the same order.

def _f(quote: str) -> Finding:
    return Finding(file="a.py", line=1, severity=Severity.MAJOR, category="c", title="t",
                   body="b", evidence_quote=quote, source="model:m", verified=True)


def test_findings_are_upserted_in_fingerprint_order(backend: Backend,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    store = backend.store()
    posted = [_f(f"posted_quote_{i}") for i in range(6)]
    held = [(_f(f"held_quote_{i}"), "unverified") for i in range(6)]
    log = spy(monkeypatch, store)
    store.record_review(ReviewRecord(repo="acme/shop", pr=7, head_sha="h1", outcome="posted",
                                     posted=True, post_id="r1"), posted, held)
    order = [params[2] for sql, params in log if sql.startswith("INSERT INTO findings")]
    assert order == sorted(f.fingerprint for f in posted + [h for h, _ in held])
    assert order != [f.fingerprint for f in posted + [h for h, _ in held]]  # input was unsorted
    states = {f["fingerprint"]: f["state"] for f in store.findings("acme/shop", 7)}
    assert states == {**{f.fingerprint: "posted" for f in posted},
                      **{h.fingerprint: "held" for h, _ in held}}


def test_same_fingerprint_posted_and_held_keeps_the_posted_state(backend: Backend) -> None:
    store = backend.store()
    f = _f("one_quote_here")
    store.record_review(ReviewRecord(repo="acme/shop", pr=7, head_sha="h1", outcome="posted",
                                     posted=True, post_id="r1"), [f], [(f, "duplicate")])
    assert [(r["state"], r["held_reason"]) for r in store.findings("acme/shop", 7)] == [
        ("posted", None)]
    # As before the sort: with nothing posted, the held row is written last and wins.
    store.record_review(ReviewRecord(repo="acme/shop", pr=8, head_sha="h1", outcome="shadow"),
                        [f], [(f, "duplicate")])
    assert [(r["state"], r["held_reason"]) for r in store.findings("acme/shop", 8)] == [
        ("held", "duplicate")]


def test_postgres_deadlock_and_serialization_failures_are_retryable() -> None:
    import psycopg.errors as pge
    assert is_retryable(pge.DeadlockDetected("d")) is True
    assert is_retryable(pge.SerializationFailure("s")) is True
    assert is_retryable(pge.UniqueViolation("u")) is False
    assert is_retryable(pge.CheckViolation("c")) is False


@pytest.mark.skipif(not PG_URL, reason="needs REVIEWHOUND_TEST_PG")
def test_a_real_postgres_deadlock_is_classified_retryable() -> None:
    import psycopg
    with psycopg.connect(PG_URL, autocommit=True) as c:
        c.execute("DROP TABLE IF EXISTS dl_probe")
        c.execute("CREATE TABLE dl_probe (id INT PRIMARY KEY, v INT)")
        c.execute("INSERT INTO dl_probe VALUES (1, 0), (2, 0)")
    a, b = psycopg.connect(PG_URL), psycopg.connect(PG_URL)
    errors: list[BaseException] = []
    try:
        a.execute("UPDATE dl_probe SET v=1 WHERE id=1")
        b.execute("UPDATE dl_probe SET v=1 WHERE id=2")

        def second() -> None:
            try:
                b.execute("UPDATE dl_probe SET v=2 WHERE id=1")
            except psycopg.Error as exc:
                errors.append(exc)
        t = threading.Thread(target=second)
        t.start()
        try:
            a.execute("UPDATE dl_probe SET v=2 WHERE id=2")
        except psycopg.Error as exc:
            errors.append(exc)
        a.rollback()
        t.join(10)
        b.rollback()
    finally:
        a.close()
        b.close()
    assert [type(e).__name__ for e in errors] == ["DeadlockDetected"]
    assert is_retryable(errors[0]) is True


def test_child_env_skips_unset_safe_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    _leaky_env(monkeypatch)
    monkeypatch.delenv("LANG")
    assert child_env(EXTRA="1") == {"PATH": SAFE["PATH"], "HOME": SAFE["HOME"], "EXTRA": "1"}


# B4: a feedback event is deduplicated by its signed body, not the unsigned delivery header.

def _post(port: int, body: bytes, event: str, headers: dict[str, str] | None = None,
          path: str = "/webhook") -> tuple[int, str]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("POST", path, body, {"X-Hub-Signature-256": sign(body), "X-GitHub-Event": event,
                                      "Content-Type": "application/json", **(headers or {})})
    resp = conn.getresponse()
    return resp.status, json.loads(resp.read())["message"]


@contextmanager
def serving(**kw: Any) -> Iterator[int]:
    server = make_server(kw.pop("queue"), SECRET, "127.0.0.1", 0, **kw)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def test_replayed_signed_body_with_a_new_delivery_id_is_applied_once(backend: Backend) -> None:
    _vcs, reviewer, store = reviewer_on(backend, [answer(REAL)])
    reviewer.review("acme/shop", 7, dry_run=False)
    resolve = json.dumps(thread("resolved", 1001)).encode()
    unresolve = json.dumps(thread("unresolved", 1001)).encode()
    ev = "pull_request_review_thread"
    with serving(queue=backend.queue(), store=store) as port:
        assert _post(port, resolve, ev, {"X-GitHub-Delivery": "d1"}) == (
            202, "recorded 1 resolved")
        assert _post(port, unresolve, ev, {"X-GitHub-Delivery": "d2"}) == (
            202, "recorded 1 reopened")
        assert _post(port, resolve, ev, {"X-GitHub-Delivery": "d3"}) == (
            202, "event already applied")
        assert _post(port, resolve, ev) == (202, "event already applied")
    assert state_of(store) == "reopened"
    assert [f["kind"] for f in store.feedback("acme/shop", 7)] == ["resolved", "reopened"]


# B2: an old "changes requested" is dismissed only when each of its blockers is positively gone.

SECOND = {"file": "app/admin.py", "line": 12, "severity": "blocker", "category": "logic",
          "title": "always reports success", "body": "...", "evidence_quote": "return True"}
ALL_FIXED = {"app/admin.py": FIXED["app/admin.py"].replace("    return True",
                                                           "    return deleted")}


def assert_kept(vcs: Any, store: Any) -> None:
    assert vcs.dismissed == []
    assert store.dismissals("acme/shop", 7) == []


def test_model_silence_on_unchanged_code_never_dismisses(backend: Backend) -> None:
    vcs, reviewer, store = reviewer_on(backend, [answer(REAL), answer()])
    assert reviewer.review("acme/shop", 7, dry_run=False).review.event == (  # type: ignore
        "REQUEST_CHANGES")
    push(vcs, "h2", {**HEAD, "app/other.py": "x = 1\n"})
    second = reviewer.review("acme/shop", 7, dry_run=False)
    assert (second.outcome, second.review.event) == ("posted", "COMMENT")  # type: ignore
    assert_kept(vcs, store)


def test_checker_dropping_the_blocker_never_dismisses(backend: Backend) -> None:
    wrong = {**REAL, "claims": [{"kind": "symbol_exists", "name": "audit_log"}]}
    vcs, reviewer, store = reviewer_on(backend, [answer(REAL), answer(wrong)])
    reviewer.review("acme/shop", 7, dry_run=False)
    push(vcs, "h2", HEAD)
    second = reviewer.review("acme/shop", 7, dry_run=False)
    assert [r for _, r in second.dropped] == ["false claim: symbol audit_log does not exist"]
    assert second.outcome == "posted"
    assert_kept(vcs, store)


def test_blocker_file_deleted_at_head_is_dismissed(backend: Backend) -> None:
    vcs, reviewer, store = reviewer_on(backend, [answer(REAL), answer()])
    reviewer.review("acme/shop", 7, dry_run=False)
    push(vcs, "h2", {"app/other.py": "x = 1\n"})
    assert reviewer.review("acme/shop", 7, dry_run=False).outcome == "posted"
    assert vcs.dismissed == [("memory-review-1", DISMISS_MESSAGE)]
    assert store.dismissals("acme/shop", 7) == [{"review_id": "memory-review-1", "ok": 1,
                                                 "error": ""}]


def test_blocker_quote_moved_to_another_file_keeps_the_verdict(backend: Backend) -> None:
    vcs, reviewer, store = reviewer_on(backend, [answer(REAL), answer()])
    reviewer.review("acme/shop", 7, dry_run=False)
    push(vcs, "h2", {"app/users.py": HEAD["app/admin.py"]})
    assert reviewer.review("acme/shop", 7, dry_run=False).outcome == "posted"
    assert_kept(vcs, store)


def test_every_blocker_of_the_old_review_must_be_gone(backend: Backend) -> None:
    vcs, reviewer, store = reviewer_on(backend, [answer(REAL, SECOND), answer(), answer()])
    first = reviewer.review("acme/shop", 7, dry_run=False)
    assert [c.line for c in first.review.comments] == [11, 12]  # type: ignore[union-attr]
    push(vcs, "h2", FIXED)  # the first blocker's line is rewritten, "return True" is not
    reviewer.review("acme/shop", 7, dry_run=False)
    assert_kept(vcs, store)
    push(vcs, "h3", ALL_FIXED)
    reviewer.review("acme/shop", 7, dry_run=False)
    assert vcs.dismissed == [("memory-review-1", DISMISS_MESSAGE)]


def test_head_moving_just_before_the_dismissal_keeps_the_verdict(
        backend: Backend, monkeypatch: pytest.MonkeyPatch) -> None:
    vcs, reviewer, store = reviewer_on(backend, [answer(REAL), answer()])
    reviewer.review("acme/shop", 7, dry_run=False)
    push(vcs, "h2", FIXED)
    real_post = vcs.post_review

    def post_then_push(pr: PullRequest, review: ReviewPayload) -> str:
        review_id = real_post(pr, review)
        vcs.head_sha_now = "h3"
        return review_id
    monkeypatch.setattr(vcs, "post_review", post_then_push)
    assert reviewer.review("acme/shop", 7, dry_run=False).outcome == "posted"
    assert_kept(vcs, store)


def test_an_old_review_with_no_recorded_findings_is_never_dismissed(backend: Backend) -> None:
    vcs, reviewer, store = reviewer_on(backend, [answer()])
    store.record_review(ReviewRecord(repo="acme/shop", pr=7, head_sha="h0", outcome="posted",
                                     event="REQUEST_CHANGES", posted=True, post_id="legacy-1"),
                        [], [])
    assert reviewer.review("acme/shop", 7, dry_run=False).outcome == "posted"
    assert store.blocking_reviews_to_dismiss("acme/shop", 7, "memory-review-1") == ["legacy-1"]
    assert_kept(vcs, store)


def test_posted_findings_are_stored_per_review_with_their_quote(backend: Backend) -> None:
    _vcs, reviewer, store = reviewer_on(backend, [answer(REAL, SECOND)])
    reviewer.review("acme/shop", 7, dry_run=False)
    rows = store.posted_findings("acme/shop", 7, "memory-review-1")
    assert [(r["file"], r["side"], r["severity"], r["evidence_quote"], r["identity"])
            for r in rows] == [("app/admin.py", "RIGHT", 4, "db.delete(uid)", ""),
                               ("app/admin.py", "RIGHT", 4, "return True", "")]
    assert store.posted_findings("acme/shop", 7, "other") == []
    assert store.posted_findings("acme/shop", 8, "memory-review-1") == []


class _Head(DictSnapshot):
    def skip_reason(self, path: str) -> str | None:
        return "too big" if path == "big.py" else None


AKIA = "AKIA" + "Q" * 16


def _row(**kw: Any) -> dict[str, Any]:
    return {"file": "a.py", "side": "RIGHT", "category": "auth",
            "evidence_quote": "do_the_thing(x)", "identity": "", **kw}


@pytest.mark.parametrize(("row", "files", "changed", "reason"), [
    (_row(), {"a.py": "y = do_the_thing(x)\n"}, [], "evidence quote is still in a.py"),
    (_row(), {"a.py": "y = 2\n"}, [], None),
    (_row(), {}, [], None),
    (_row(), {"b.py": "do_the_thing(x)\n"}, ["a.py", "b.py"], "evidence quote is now in b.py"),
    (_row(), {"b.py": "do_the_thing(x)\n"}, ["a.py"], None),
    (_row(file="big.py"), {}, [], "big.py could not be read at head (too big)"),
    (_row(side="LEFT"), {"a.py": "y = 2\n"}, [], "a deleted-line finding cannot be shown gone"),
    (_row(evidence_quote="x()"), {"a.py": "y = 2\n"}, [], "no evidence quote to look for"),
    (_row(category="secret", identity=secret_identity(AKIA), evidence_quote="AKIA[redacted]"),
     {"a.py": f'key = "{AKIA}"\n'}, [], "the secret is still in a.py"),
    (_row(category="secret", identity=secret_identity(AKIA), evidence_quote="AKIA[redacted]"),
     {"a.py": "key = env()\n"}, [], None),
])
def test_open_reason_needs_positive_evidence(row: dict[str, Any], files: dict[str, str],
                                             changed: list[str], reason: str | None) -> None:
    assert open_reason(row, _Head(files), changed) == reason


# B3: a refused post is retried once with every comment in the body; a lost lease posts nothing.

def scripted_posts(vcs: Any, monkeypatch: pytest.MonkeyPatch,
                   *outcomes: Exception | None) -> list[ReviewPayload]:
    """Each post raises the next outcome, or really posts when it is None."""
    real_post = vcs.post_review
    calls: list[ReviewPayload] = []

    def post(pr: PullRequest, review: ReviewPayload) -> str:
        calls.append(review)
        outcome = outcomes[len(calls) - 1]
        if outcome is not None:
            raise outcome
        return str(real_post(pr, review))
    monkeypatch.setattr(vcs, "post_review", post)
    return calls


REJECTED = GitHubError("POST /x -> HTTP 422: line could not be resolved", 422)


def test_rejected_anchor_is_retried_once_with_comments_in_the_body(
        backend: Backend, monkeypatch: pytest.MonkeyPatch) -> None:
    vcs, reviewer, store = reviewer_on(backend, [answer(REAL, SECOND)])
    calls = scripted_posts(vcs, monkeypatch, REJECTED, None)
    result = reviewer.review("acme/shop", 7, dry_run=False)
    assert (result.outcome, result.post_id) == ("posted", "memory-review-1")
    assert result.error == "inline comments refused (HTTP 422); posted them in the review body"
    assert [len(c.comments) for c in calls] == [2, 0]
    assert [c.event for c in calls] == ["REQUEST_CHANGES", "REQUEST_CHANGES"]
    first, retry = calls
    want = [first.body, "", "GitHub could not place these comments on their lines:",
            "", "**`app/admin.py:11`**", "", first.comments[0].body,
            "", "**`app/admin.py:12`**", "", first.comments[1].body]
    assert retry.body.split("\n") == "\n".join(want).split("\n")
    assert [(r["outcome"], r["posted"], r["post_id"]) for r in store.reviews("acme/shop", 7)] == [
        ("posted", 1, "memory-review-1")]
    assert store.claim_post("acme/shop", 7, "h1") is False


def test_left_comment_moved_to_the_body_says_it_is_a_deleted_line() -> None:
    review = ReviewPayload(head_sha="h", event="COMMENT", body="B", comments=[
        ReviewComment(path="a.py", line=3, body="gone", side="LEFT")])
    assert comments_in_body(review) == ReviewPayload(
        head_sha="h", event="COMMENT", comments=[],
        body="B\n\nGitHub could not place these comments on their lines:\n\n"
             "**`a.py:3` (deleted line)**\n\ngone")


def test_rejected_twice_is_rejected_and_frees_the_head(backend: Backend,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    vcs, reviewer, store = reviewer_on(backend, [answer(REAL)])
    calls = scripted_posts(vcs, monkeypatch, REJECTED, GitHubError("HTTP 403: nope", 403))
    result = reviewer.review("acme/shop", 7, dry_run=False)
    assert (result.outcome, result.error) == ("post-rejected", "HTTP 403: nope")
    assert [len(c.comments) for c in calls] == [1, 0]
    assert store.claim_post("acme/shop", 7, "h1") is True


def test_retry_that_times_out_is_uncertain_and_keeps_the_claim(
        backend: Backend, monkeypatch: pytest.MonkeyPatch) -> None:
    vcs, reviewer, store = reviewer_on(backend, [answer(REAL)])
    scripted_posts(vcs, monkeypatch, REJECTED, TimeoutError("socket"))
    result = reviewer.review("acme/shop", 7, dry_run=False)
    assert (result.outcome, result.error) == ("post-uncertain", "TimeoutError")
    assert [(r["outcome"], r["posted"]) for r in store.reviews("acme/shop", 7)] == [
        ("post-uncertain", 1)]
    assert store.claim_post("acme/shop", 7, "h1") is False


@pytest.mark.parametrize("first", [GitHubError("HTTP 502", 502), GitHubError("HTTP 429", 429),
                                   TimeoutError("socket")])
def test_only_a_refusal_is_retried(backend: Backend, monkeypatch: pytest.MonkeyPatch,
                                   first: Exception) -> None:
    vcs, reviewer, _store = reviewer_on(backend, [answer(REAL)])
    calls = scripted_posts(vcs, monkeypatch, first, None)
    assert reviewer.review("acme/shop", 7, dry_run=False).outcome == "post-uncertain"
    assert len(calls) == 1


def test_refusal_of_a_review_without_inline_comments_is_not_retried(
        backend: Backend, monkeypatch: pytest.MonkeyPatch) -> None:
    vcs, reviewer, store = reviewer_on(backend, [answer()])
    calls = scripted_posts(vcs, monkeypatch, REJECTED, None)
    assert reviewer.review("acme/shop", 7, dry_run=False).outcome == "post-rejected"
    assert len(calls) == 1
    assert store.claim_post("acme/shop", 7, "h1") is True


def test_uncertain_post_skips_linking_and_says_so(backend: Backend,
                                                  monkeypatch: pytest.MonkeyPatch,
                                                  caplog: pytest.LogCaptureFixture) -> None:
    vcs, reviewer, store = reviewer_on(backend, [answer(REAL)])
    scripted_posts(vcs, monkeypatch, TimeoutError("socket"))
    caplog.set_level(logging.WARNING, logger="reviewhound.pipeline")
    assert reviewer.review("acme/shop", 7, dry_run=False).outcome == "post-uncertain"
    assert [(r.msg, r.pr, r.head) for r in caplog.records] == [  # type: ignore[attr-defined]
        ("post outcome uncertain; comments not linked, verdicts not dismissed", 7, "h1")]
    assert store.linked_findings("acme/shop", [1001]) == {}


def test_lost_lease_posts_nothing_and_leaves_the_head_free(backend: Backend) -> None:
    vcs, reviewer, store = reviewer_on(backend, [answer(REAL)])
    result = reviewer.review("acme/shop", 7, dry_run=False, should_post=lambda: False)
    assert (result.outcome, vcs.posted) == ("lease-lost", [])
    assert [(r["outcome"], r["posted"]) for r in store.reviews("acme/shop", 7)] == [
        ("lease-lost", 0)]
    assert [f["state"] for f in store.findings("acme/shop", 7)] == ["stored"]
    assert store.claim_post("acme/shop", 7, "h1") is True


def test_lease_lost_after_a_refusal_skips_the_retry(backend: Backend,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    vcs, reviewer, store = reviewer_on(backend, [answer(REAL)])
    calls = scripted_posts(vcs, monkeypatch, REJECTED, None)
    answers = iter([True, False])
    result = reviewer.review("acme/shop", 7, dry_run=False, should_post=lambda: next(answers))
    assert (result.outcome, len(calls)) == ("lease-lost", 1)
    assert store.claim_post("acme/shop", 7, "h1") is True


def test_held_lease_posts_as_before(backend: Backend) -> None:
    vcs, reviewer, _store = reviewer_on(backend, [answer(REAL)])
    result = reviewer.review("acme/shop", 7, dry_run=False, should_post=lambda: True)
    assert (result.outcome, len(vcs.posted)) == ("posted", 1)


# B1: only allowed repository owners get jobs or feedback writes.

ALLOWED = frozenset({"acme"})


def pr_body(owner: str = "acme") -> dict[str, Any]:
    return {"action": "opened", "repository": {"full_name": f"{owner}/shop"},
            "pull_request": {"number": 7, "draft": False, "head": {"sha": "abc"}}}


def test_event_from_another_owner_is_ignored_and_queues_nothing(backend: Backend) -> None:
    q = backend.queue()
    assert handle_event(q, "pull_request", pr_body("mallory"), allowed_owners=ALLOWED) == (
        202, "ignored: owner not allowed")
    assert q.claim() is None
    assert handle_event(q, "pull_request", pr_body("ACME"), allowed_owners=ALLOWED) == (
        202, "job 1 queued")
    assert handle_event(q, "ping", {}, allowed_owners=ALLOWED) == (200, "pong")


def test_unset_allowlist_allows_every_owner(backend: Backend) -> None:
    q = backend.queue()
    assert handle_event(q, "pull_request", pr_body("anyone"), allowed_owners=None) == (
        202, "job 1 queued")
    assert handle_event(q, "pull_request", pr_body("other"), allowed_owners=frozenset()) == (
        202, "job 2 queued")


def test_feedback_from_another_owner_writes_nothing(backend: Backend) -> None:
    _vcs, reviewer, store = reviewer_on(backend, [answer(REAL)])
    reviewer.review("acme/shop", 7, dry_run=False)
    foreign = {**thread("resolved", 1001), "repository": {"full_name": "mallory/shop"}}
    assert handle_event(backend.queue(), "pull_request_review_thread", foreign, store,
                        allowed_owners=ALLOWED) == (202, "ignored: owner not allowed")
    assert store.feedback("mallory/shop", 7) == []
    assert state_of(store) == "posted"


def test_http_post_from_another_owner_creates_no_job(backend: Backend) -> None:
    q = backend.queue()
    with serving(queue=q, allowed_owners=ALLOWED) as port:
        assert _post(port, json.dumps(pr_body("mallory")).encode(), "pull_request") == (
            202, "ignored: owner not allowed")
        assert q.claim() is None
        assert _post(port, json.dumps(pr_body("Acme")).encode(), "pull_request") == (
            202, "job 1 queued")


def test_serve_passes_env_and_flag_owners_to_the_server(tmp_path: Path,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    import reviewhound.webhook as wh
    from reviewhound.cli import main
    seen: dict[str, Any] = {}

    class Idle:
        def serve_forever(self) -> None:
            pass

    def fake_make_server(*a: Any, **kw: Any) -> Idle:
        seen.update(kw)
        return Idle()
    monkeypatch.setattr(wh, "make_server", fake_make_server)
    monkeypatch.setenv("REVIEWHOUND_WEBHOOK_SECRET", "s")
    monkeypatch.setenv("REVIEWHOUND_ALLOWED_OWNERS", "Acme")
    monkeypatch.delenv("REVIEWHOUND_DATABASE_URL", raising=False)
    assert main(["--home", str(tmp_path), "serve", "--allowed-owner", "Beta"]) == 0
    assert seen["allowed_owners"] == frozenset({"acme", "beta"})


def test_allowed_owners_come_from_env_and_flags() -> None:
    assert allowed_owners_from(" Acme, beta ,,", ["Gamma"]) == frozenset({"acme", "beta", "gamma"})
    assert allowed_owners_from("", []) is None
    assert allowed_owners_from(" , ", []) is None


# B6: /metrics needs the bearer token when one is set; slow bodies time out.

def _get(port: int, headers: dict[str, str] | None = None) -> tuple[int, str]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/metrics", headers=headers or {})
    resp = conn.getresponse()
    return resp.status, resp.read().decode()


@pytest.mark.parametrize(("headers", "want"), [
    ({}, (401, '{"message": "metrics need a bearer token"}')),
    ({"Authorization": "Bearer nope"}, (401, '{"message": "metrics need a bearer token"}')),
    ({"Authorization": "bearer t0ken"}, (401, '{"message": "metrics need a bearer token"}')),
    ({"Authorization": "t0ken"}, (401, '{"message": "metrics need a bearer token"}')),
    ({"Authorization": "Bearer t0ken"}, (200, "m 1\n")),
])
def test_metrics_need_the_bearer_token_when_set(tmp_path: Path, headers: dict[str, str],
                                                want: tuple[int, str]) -> None:
    with serving(queue=SQLiteQueue(tmp_path / "q.db"), metrics=lambda: "m 1\n",
                 metrics_token="t0ken") as port:
        assert _get(port, headers) == want


def test_metrics_token_is_read_from_the_environment(tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVIEWHOUND_METRICS_TOKEN", "envtok")
    with serving(queue=SQLiteQueue(tmp_path / "q.db"), metrics=lambda: "m 1\n") as port:
        assert _get(port)[0] == 401
        assert _get(port, {"Authorization": "Bearer envtok"}) == (200, "m 1\n")


def test_unset_metrics_token_stays_open_and_warns_loudly(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.delenv("REVIEWHOUND_METRICS_TOKEN", raising=False)
    caplog.set_level(logging.WARNING, logger="reviewhound.webhook")
    with serving(queue=SQLiteQueue(tmp_path / "q.db"), metrics=lambda: "m 1\n") as port:
        assert _get(port) == (200, "m 1\n")
    assert [r.msg for r in caplog.records] == [
        "/metrics is UNAUTHENTICATED: set REVIEWHOUND_METRICS_TOKEN",
        ("no owner allowlist: events from ANY repository owner are accepted; "
         "set REVIEWHOUND_ALLOWED_OWNERS")]


def test_metrics_token_compare_is_constant_time(monkeypatch: pytest.MonkeyPatch,
                                                tmp_path: Path) -> None:
    import reviewhound.webhook as wh
    seen: list[tuple[bytes, bytes]] = []
    real = wh.hmac.compare_digest

    def spy_compare(a: bytes, b: bytes) -> bool:
        seen.append((a, b))
        return real(a, b)
    monkeypatch.setattr(wh.hmac, "compare_digest", spy_compare)
    with serving(queue=SQLiteQueue(tmp_path / "q.db"), metrics=lambda: "m 1\n",
                 metrics_token="t0ken") as port:
        assert _get(port, {"Authorization": "Bearer nope"})[0] == 401
    assert seen == [(b"Bearer nope", b"Bearer t0ken")]


def test_slow_body_times_out_and_queues_nothing(tmp_path: Path) -> None:
    q = SQLiteQueue(tmp_path / "q.db")
    with serving(queue=q, read_timeout_s=0.3) as port:
        sock = socket.create_connection(("127.0.0.1", port), timeout=3)
        sock.sendall(b"POST /webhook HTTP/1.1\r\nHost: x\r\nContent-Length: 100\r\n\r\n{")
        assert sock.recv(1024) == b""  # the server gave up and closed the connection
        sock.close()
    assert q.claim() is None


def test_default_read_timeout_is_ten_seconds(tmp_path: Path) -> None:
    server = make_server(SQLiteQueue(tmp_path / "q.db"), SECRET, "127.0.0.1", 0)
    try:
        assert server.RequestHandlerClass.timeout == 10.0
    finally:
        server.server_close()
