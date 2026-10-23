"""In-memory VCS for tests and the eval harness: a PR is a pair of file maps."""

from __future__ import annotations

import difflib
from collections import Counter

from reviewhound.diff import Diff, parse_unified_diff
from reviewhound.vcs.base import (
    DictSnapshot,
    PostedComment,
    PostedReview,
    PullRequest,
    ReviewPayload,
    Snapshot,
)

STATES = {"COMMENT": "COMMENTED", "REQUEST_CHANGES": "CHANGES_REQUESTED", "APPROVE": "APPROVED"}


def diff_file_maps(base: dict[str, str], head: dict[str, str]) -> Diff:
    chunks: list[str] = []
    for path in sorted(set(base) | set(head)):
        old, new = base.get(path), head.get(path)
        if old == new:
            continue
        header = [f"diff --git a/{path} b/{path}"]
        if old is None:
            header.append("new file mode 100644")
        elif new is None:
            header.append("deleted file mode 100644")
        body = difflib.unified_diff(
            (old or "").splitlines(), (new or "").splitlines(),
            fromfile=f"a/{path}" if old is not None else "/dev/null",
            tofile=f"b/{path}" if new is not None else "/dev/null", lineterm="", n=3)
        chunks.append("\n".join([*header, *body]))
    return parse_unified_diff("\n".join(chunks) + "\n")


class MemoryVCS:
    def __init__(self, pr: PullRequest, base: dict[str, str], head: dict[str, str]) -> None:
        self.pr = pr
        self.base = base
        self.head = head
        self.head_sha_now = pr.head_sha
        self.posted: list[ReviewPayload] = []
        self.viewer = "reviewhound-bot"
        self.dismissed: list[tuple[str, str]] = []
        self.dismiss_error: Exception | None = None
        self.comments_error: Exception | None = None

    def get_pull(self, repo: str, number: int) -> PullRequest:
        return self.pr

    def get_diff(self, pr: PullRequest) -> Diff:
        return diff_file_maps(self.base, self.head)

    def snapshot(self, repo: str, sha: str) -> Snapshot:
        return DictSnapshot(self.head if sha == self.pr.head_sha else self.base)

    def read_file(self, repo: str, sha: str, path: str) -> str | None:
        return (self.head if sha == self.pr.head_sha else self.base).get(path)

    def churn(self, repo: str, sha: str) -> Counter[str]:
        return Counter()

    def current_head(self, repo: str, number: int) -> str:
        return self.head_sha_now

    def viewer_login(self) -> str:
        return self.viewer

    def post_review(self, pr: PullRequest, review: ReviewPayload) -> str:
        self.posted.append(review)
        return f"memory-review-{len(self.posted)}"

    def list_reviews(self, repo: str, number: int) -> list[PostedReview]:
        return [PostedReview(id=f"memory-review-{i}", author=self.viewer, commit_id=r.head_sha,
                             body=r.body, state=STATES[r.event])
                for i, r in enumerate(self.posted, start=1)]

    def review_comments(self, repo: str, number: int, review_id: str) -> list[PostedComment]:
        if self.comments_error is not None:
            raise self.comments_error
        n = int(review_id.rsplit("-", 1)[1])
        return [PostedComment(id=n * 1000 + i, path=c.path, line=c.line, side=c.side)
                for i, c in enumerate(self.posted[n - 1].comments, start=1)]

    def dismiss_review(self, repo: str, number: int, review_id: str, message: str) -> None:
        if self.dismiss_error is not None:
            raise self.dismiss_error
        self.dismissed.append((review_id, message))
