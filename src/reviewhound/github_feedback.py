"""The GitHub feedback loop: link posted comments to findings, turn thread events into feedback,
and dismiss our own stale "changes requested" verdict once a new head has no open blocker."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from reviewhound.config import PolicyConfig, Severity
from reviewhound.store import ReviewStore
from reviewhound.vcs.base import VCS, ReviewThreads
from reviewhound.vcs.github import GitHubError

if TYPE_CHECKING:
    from reviewhound.pipeline import ReviewResult

log = logging.getLogger(__name__)
FEEDBACK_EVENTS = {"pull_request_review_thread", "pull_request_review_comment"}
THREAD_ACTIONS = {"resolved": "resolved", "unresolved": "reopened"}
REPLY_MAX_CHARS = 500
DISMISS_MESSAGE = "Superseded: Reviewhound found no blocking issues on the latest commit."
# Held findings with these reasons are not open problems on the new head.
_NOT_OPEN = ("duplicate", "unverified", "developer ")


def handle_feedback_event(store: ReviewStore, event: str, payload: dict[str, Any],
                          app_id: str | None = None, delivery: str = "") -> tuple[int, str]:
    """DB writes only: this runs inside the webhook request, so no GitHub calls here."""
    repo = (payload.get("repository") or {}).get("full_name")
    if not isinstance(repo, str) or not repo:
        return 400, "missing repository"
    if event == "pull_request_review_thread":
        return _thread_event(store, repo, payload, delivery)
    return _comment_event(store, repo, payload, app_id)


def _thread_event(store: ReviewStore, repo: str, payload: dict[str, Any],
                  delivery: str) -> tuple[int, str]:
    action = payload.get("action")
    kind = THREAD_ACTIONS.get(str(action))
    if kind is None:
        return 202, f"ignored action {action}"
    try:
        ids = [int(c["id"]) for c in (payload["thread"]["comments"] or [])]
    except (KeyError, TypeError, ValueError):
        return 400, "malformed review thread payload"
    # A late redelivery of an old resolve must not undo a newer unresolve.
    key = f"thread:{delivery}" if delivery else ""
    if key and not store.claim_event(key):
        return 202, "event already applied"
    try:
        return _apply_thread(store, repo, str(action), kind, ids)
    except BaseException:
        if key:
            store.release_event(key)
        raise


def _apply_thread(store: ReviewStore, repo: str, action: str, kind: str,
                  ids: list[int]) -> tuple[int, str]:
    links = store.linked_findings(repo, ids)
    unknown = [i for i in ids if i not in links]
    if unknown:
        log.info("thread comments not posted by us, ignored",
                 extra={"repo": repo, "comment_ids": unknown, "action": action})
    recorded = 0
    for pr, fingerprint in sorted(set(links.values())):
        if store.add_feedback(repo, pr, fingerprint, kind):
            recorded += 1
        else:
            log.warning("linked finding missing", extra={"repo": repo, "pr": pr,
                                                          "fingerprint": fingerprint})
    return 202, f"recorded {recorded} {kind}"


def _comment_event(store: ReviewStore, repo: str, payload: dict[str, Any],
                   app_id: str | None) -> tuple[int, str]:
    action = payload.get("action")
    if action != "created":
        return 202, f"ignored action {action}"
    comment = payload.get("comment")
    if not isinstance(comment, dict):
        return 400, "malformed review comment payload"
    parent = comment.get("in_reply_to_id")
    if parent is None:
        return 202, "not a reply"
    app = comment.get("performed_via_github_app") or {}
    if app_id is not None and str(app.get("id")) == app_id:
        return 202, "own reply ignored"
    try:
        parent_id, reply_id = int(parent), int(comment["id"])
    except (KeyError, TypeError, ValueError):
        return 400, "malformed review comment payload"
    link = store.linked_findings(repo, [parent_id]).get(parent_id)
    if link is None:
        log.info("reply to a comment not posted by us, ignored",
                 extra={"repo": repo, "comment_id": parent_id})
        return 202, "reply to unknown comment ignored"
    key = f"reply:{repo.lower()}:{reply_id}"
    if not store.claim_event(key):
        return 202, "reply already recorded"
    pr, fingerprint = link
    try:
        ok = store.add_feedback(repo, pr, fingerprint, "reply",
                                str(comment.get("body") or "")[:REPLY_MAX_CHARS])
    except BaseException:
        store.release_event(key)
        raise
    if not ok:
        log.warning("linked finding missing", extra={"repo": repo, "pr": pr,
                                                      "fingerprint": fingerprint})
        return 202, "linked finding missing"
    return 202, "recorded reply"


def after_post(vcs: VCS, store: ReviewStore, result: ReviewResult, policy: PolicyConfig) -> None:
    """Follow-ups once a review is posted and recorded. They never fail the review."""
    if not isinstance(vcs, ReviewThreads):
        log.info("VCS cannot read back comments; feedback loop off", extra={"pr": result.pr.number})
        return
    for step in (link_comments, dismiss_stale_verdicts):
        try:
            step(vcs, store, result, policy)
        except Exception as exc:  # noqa: BLE001 - the review already posted; log and move on
            log.error("post follow-up failed", extra={"step": step.__name__, "repo": result.pr.repo,
                                                      "pr": result.pr.number,
                                                      "error": type(exc).__name__})


def link_comments(vcs: ReviewThreads, store: ReviewStore, result: ReviewResult,
                  policy: PolicyConfig) -> None:
    pr = result.pr
    posted = result.decision.post if result.decision else []
    by_line = {(f.file, f.line): f.fingerprint for f in posted}
    links: list[tuple[int, str]] = []
    for c in vcs.review_comments(pr.repo, pr.number, result.post_id):
        fingerprint = by_line.get((c.path, c.line))
        if fingerprint is None:
            log.warning("posted comment matches no finding", extra={
                "repo": pr.repo, "pr": pr.number, "comment_id": c.id, "line": c.line})
        else:
            links.append((c.id, fingerprint))
    store.link_comments(pr.repo, pr.number, result.post_id, links)


def has_open_blocker(result: ReviewResult, policy: PolicyConfig) -> bool:
    """A blocker posted now, or posted earlier and still present, keeps the old verdict."""
    if result.decision is None:
        return True
    blocking = Severity.parse(policy.blocking_severity)
    if any(f.severity >= blocking for f in result.decision.post):
        return True
    return any(f.severity >= blocking and not reason.startswith(_NOT_OPEN)
               for f, reason in result.decision.held)


def dismiss_stale_verdicts(vcs: ReviewThreads, store: ReviewStore, result: ReviewResult,
                           policy: PolicyConfig) -> None:
    if has_open_blocker(result, policy):
        return
    pr = result.pr
    for review_id in store.blocking_reviews_to_dismiss(pr.repo, pr.number, result.post_id):
        try:
            vcs.dismiss_review(pr.repo, pr.number, review_id, DISMISS_MESSAGE)
        except Exception as exc:  # noqa: BLE001 - recorded and logged; retried after the next post
            log.warning("dismissal failed", extra={"repo": pr.repo, "pr": pr.number,
                                                   "review_id": review_id,
                                                   "error": type(exc).__name__})
            # 4xx other than 429 (already dismissed, no permission) will not get better.
            permanent = (isinstance(exc, GitHubError) and exc.status is not None
                         and 400 <= exc.status < 500 and exc.status != 429)
            store.record_dismissal(pr.repo, pr.number, review_id, ok=False,
                                   error=f"{type(exc).__name__}: {exc}", permanent=permanent)
        else:
            log.info("stale verdict dismissed", extra={"repo": pr.repo, "pr": pr.number,
                                                       "review_id": review_id})
            store.record_dismissal(pr.repo, pr.number, review_id, ok=True)
