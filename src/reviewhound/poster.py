"""Turn a policy decision into the exact GitHub review payload, and render it for dry runs."""

from __future__ import annotations

import json

from reviewhound.diff import Diff
from reviewhound.findings import Finding
from reviewhound.policy import PolicyDecision
from reviewhound.vcs.base import ReviewComment, ReviewPayload


def _comment_body(f: Finding) -> str:
    tag = f.severity.name.lower()
    body = f"**[{tag}] {f.title}**\n\n{f.body}"
    if not f.verified:
        body += "\n\n_Not fully verified against the code._"
    return body


def build_review(head_sha: str, decision: PolicyDecision, diff: Diff, tier: str,
                 reviewers: list[str]) -> ReviewPayload:
    comments: list[ReviewComment] = []
    body_only: list[Finding] = []
    for f in decision.post:
        fd = diff.files.get(f.file)
        if fd is not None and f.line in fd.commentable_lines:
            comments.append(ReviewComment(path=f.file, line=f.line, body=_comment_body(f)))
        else:
            body_only.append(f)
    ran = ", ".join(reviewers) if reviewers else "none"
    lines = [f"Reviewhound ({tier} review). Reviewers that ran: {ran}."]
    if not decision.post:
        lines.append("No verified issues found.")
    else:
        lines.append(f"{len(decision.post)} issue(s) found.")
    for f in body_only:
        lines.append(f"- `{f.file}:{f.line}` [{f.severity.name.lower()}] {f.title}")
    return ReviewPayload(head_sha=head_sha, event=decision.event, body="\n".join(lines),
                         comments=comments)


def render_dry_run(review: ReviewPayload, decision: PolicyDecision) -> str:
    payload = {"commit_id": review.head_sha, "event": review.event, "body": review.body,
               "comments": [{"path": c.path, "line": c.line, "side": "RIGHT", "body": c.body}
                            for c in review.comments]}
    out = ["=== Review that would be posted ===", json.dumps(payload, indent=2)]
    if decision.held:
        out.append("=== Held back (not posted) ===")
        for f, reason in decision.held:
            out.append(f"- {f.file}:{f.line} [{f.severity.name.lower()}] {f.title} -> {reason}")
    return "\n".join(out)


def own_pr_review(review: ReviewPayload) -> ReviewPayload:
    """GitHub forbids approving or requesting changes on your own PR: post the verdict as text."""
    verdict = {"REQUEST_CHANGES": "changes requested", "APPROVE": "approved"}[review.event]
    note = (f"Verdict: {verdict}. Posted as a comment because this account opened the pull "
            "request, and GitHub does not let an account give a verdict on its own pull request.")
    return ReviewPayload(head_sha=review.head_sha, event="COMMENT",
                         body=f"{review.body}\n{note}", comments=review.comments)
