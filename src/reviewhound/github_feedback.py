"""The GitHub feedback loop: link posted comments to findings, turn thread events into feedback,
and dismiss our own stale "changes requested" verdict once a new head has no open blocker."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from reviewhound.checker import find_quote, is_meaningful
from reviewhound.config import PolicyConfig, Severity
from reviewhound.scanners import secret_identities
from reviewhound.store import ReviewStore
from reviewhound.vcs.base import VCS, ReviewThreads, Snapshot, ThreadedVCS
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
                          app_id: str | None = None, body_sha256: str = "") -> tuple[int, str]:
    """DB writes only: this runs inside the webhook request, so no GitHub calls here.

    `body_sha256` identifies the signed delivery; the unsigned X-GitHub-Delivery id never does."""
    repo = (payload.get("repository") or {}).get("full_name")
    if not isinstance(repo, str) or not repo:
        return 400, "missing repository"
    if event == "pull_request_review_thread":
        return _thread_event(store, repo, payload, body_sha256)
    return _comment_event(store, repo, payload, app_id)


def _thread_event(store: ReviewStore, repo: str, payload: dict[str, Any],
                  body_sha256: str) -> tuple[int, str]:
    action = payload.get("action")
    kind = THREAD_ACTIONS.get(str(action))
    if kind is None:
        return 202, f"ignored action {action}"
    try:
        ids = [int(c["id"]) for c in (payload["thread"]["comments"] or [])]
    except (KeyError, TypeError, ValueError):
        return 400, "malformed review thread payload"
    links = store.linked_findings(repo, ids)
    unknown = [i for i in ids if i not in links]
    if unknown:
        log.info("thread comments not posted by us, ignored",
                 extra={"repo": repo, "comment_ids": unknown, "action": action})
    targets = sorted(set(links.values()))
    # A replayed old resolve must not undo a newer unresolve; the claim commits with the writes.
    done = store.apply_feedback(f"thread:{body_sha256}" if body_sha256 else "", repo,
                                [(pr, fingerprint, kind, "") for pr, fingerprint in targets])
    if done is None:
        return 202, "event already applied"
    for (pr, fingerprint), ok in zip(targets, done, strict=True):
        if not ok:
            log.warning("linked finding missing", extra={"repo": repo, "pr": pr,
                                                          "fingerprint": fingerprint})
    return 202, f"recorded {sum(done)} {kind}"


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
    pr, fingerprint = link
    done = store.apply_feedback(f"reply:{repo.lower()}:{reply_id}", repo, [
        (pr, fingerprint, "reply", str(comment.get("body") or "")[:REPLY_MAX_CHARS])])
    if done is None:
        return 202, "reply already recorded"
    if done != [True]:
        log.warning("linked finding missing", extra={"repo": repo, "pr": pr,
                                                      "fingerprint": fingerprint})
        return 202, "linked finding missing"
    return 202, "recorded reply"


def after_post(vcs: VCS, store: ReviewStore, result: ReviewResult, policy: PolicyConfig) -> None:
    """Follow-ups once a review is posted and recorded. They never fail the review."""
    if not isinstance(vcs, ThreadedVCS):
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
    # A LEFT (deleted line) and a RIGHT comment can share a line number.
    by_line: dict[tuple[str, str, int], str] = {(f.file, f.side, f.line): f.fingerprint
                                               for f in posted}
    links: list[tuple[int, str]] = []
    comments = result.read_back if result.read_back is not None else \
        vcs.review_comments(pr.repo, pr.number, result.post_id)
    for c in comments:
        fingerprint = by_line.get((c.path, c.side, c.line))
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


def open_reason(row: Mapping[str, Any], head: Snapshot, changed: list[str],
                sources: Mapping[str, str]) -> str | None:
    """Why an old blocker may still be in the code at `head`; None only when it is positively gone.

    A removed-guard blocker (it carries the deleted text) is gone once that text is back in its
    file. Any other is gone when its quote (or, for a secret, the secret's identity) is absent
    from its own file, every other file in the head index (`sources`) and every changed file."""
    path, quote = str(row["file"]), str(row["evidence_quote"])
    deleted = str(row.get("deleted_text") or "")
    if deleted:
        return _still_deleted(path, deleted, int(row.get("deleted_kept") or 0), head)
    if row["side"] == "LEFT":
        return "a deleted-line finding cannot be shown gone"
    secret = row["category"] == "secret" and bool(row["identity"])
    if not secret and not is_meaningful(quote):
        return "no evidence quote to look for"
    what = "the secret" if secret else "evidence quote"

    def present(text: str) -> bool:
        return row["identity"] in secret_identities(text) if secret else bool(find_quote(text, quote))
    src = head.read(path)
    if src is not None and present(src):
        return f"{what} is still in {path}"
    others = sorted((set(sources) | set(changed)) - {path})
    for other in ([] if src is not None else [path]) + others:
        text = sources.get(other) if other in sources else head.read(other)
        skip = head.skip_reason(other) if text is None else None
        if skip is not None:
            return f"{other} could not be read at head ({skip})"
        if text is not None and present(text):
            return f"{what} is now in {other}"
    return None


def _still_deleted(path: str, deleted: str, kept: int, head: Snapshot) -> str | None:
    """Back means more copies of the deleted code than the file kept when it was flagged."""
    if not is_meaningful(deleted):
        return "the deleted code is too short to look for"
    src = head.read(path)
    if src is None:
        skip = head.skip_reason(path)
        return f"{path} could not be read at head ({skip})" if skip else \
            f"{path} is not in the head"
    if len(find_quote(src, deleted)) > kept:
        return None
    return f"the code this PR deleted is still missing from {path}"


def dismiss_stale_verdicts(vcs: ThreadedVCS, store: ReviewStore, result: ReviewResult,
                           policy: PolicyConfig) -> None:
    """Dismiss an older REQUEST_CHANGES only when each of its blockers is positively gone."""
    if has_open_blocker(result, policy) or result.snapshot is None:
        return
    pr = result.pr
    blocking = Severity.parse(policy.blocking_severity)
    for review_id in store.blocking_reviews_to_dismiss(pr.repo, pr.number, result.post_id):
        # Reviews recorded before review_blockers existed fall back to their posted findings.
        blockers = store.review_blockers(pr.repo, pr.number, review_id) or [
            r for r in store.posted_findings(pr.repo, pr.number, review_id)
            if r["severity"] >= blocking]
        reasons = [open_reason(r, result.snapshot, result.changed_paths, result.head_sources)
                   for r in blockers]
        kept = [r for r in reasons if r is not None] if blockers else [
            "no blocking finding recorded for it"]
        if kept:
            log.info("stale verdict kept", extra={"repo": pr.repo, "pr": pr.number,
                                                  "review_id": review_id, "reason": kept[0]})
            continue
        # A newer head may have brought the problem back: never dismiss on its behalf.
        if vcs.current_head(pr.repo, pr.number) != pr.head_sha:
            log.info("head moved; stale verdict kept", extra={"repo": pr.repo, "pr": pr.number,
                                                             "review_id": review_id})
            return
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
