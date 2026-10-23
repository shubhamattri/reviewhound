"""Version-control interface: what the pipeline needs from GitHub (or anything else)."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from reviewhound.diff import Diff


@dataclass(frozen=True)
class PullRequest:
    repo: str  # owner/name
    number: int
    title: str
    body: str
    base_sha: str
    head_sha: str
    author: str = ""
    draft: bool = False
    state: str = "open"


@dataclass
class ReviewComment:
    path: str
    line: int
    body: str


@dataclass(frozen=True)
class PostedComment:
    """A review comment as GitHub stored it, read back after posting."""

    id: int
    path: str
    line: int


@dataclass
class ReviewPayload:
    head_sha: str
    event: str  # COMMENT | REQUEST_CHANGES | APPROVE
    body: str
    comments: list[ReviewComment] = field(default_factory=list)


class Snapshot(Protocol):
    def read(self, path: str) -> str | None: ...

    def paths(self) -> list[str]: ...


class DictSnapshot:
    def __init__(self, files: dict[str, str]) -> None:
        self._files = dict(files)

    def read(self, path: str) -> str | None:
        return self._files.get(path)

    def paths(self) -> list[str]:
        return sorted(self._files)


class DirSnapshot:
    """A checked-out tree on disk."""

    def __init__(self, root: Path, max_file_bytes: int = 1_000_000) -> None:
        self.root = root
        self.max_file_bytes = max_file_bytes

    def read(self, path: str) -> str | None:
        p = (self.root / path).resolve()
        if not p.is_relative_to(self.root.resolve()) or not p.is_file():
            return None
        if p.stat().st_size > self.max_file_bytes:
            return None
        try:
            return p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return None

    def paths(self) -> list[str]:
        out = []
        for p in self.root.rglob("*"):
            rel = p.relative_to(self.root).as_posix()
            if p.is_file() and not rel.startswith(".git/"):
                out.append(rel)
        return sorted(out)


class VCS(Protocol):
    def get_pull(self, repo: str, number: int) -> PullRequest: ...

    def get_diff(self, pr: PullRequest) -> Diff: ...

    def snapshot(self, repo: str, sha: str) -> Snapshot: ...

    def read_file(self, repo: str, sha: str, path: str) -> str | None: ...

    def churn(self, repo: str, sha: str) -> Counter[str]: ...

    def current_head(self, repo: str, number: int) -> str: ...

    def post_review(self, pr: PullRequest, review: ReviewPayload) -> str: ...

    def viewer_login(self) -> str: ...


@runtime_checkable
class ReviewThreads(Protocol):
    """Optional VCS ability: read back posted comments and dismiss an earlier review."""

    def review_comments(self, repo: str, number: int, review_id: str) -> list[PostedComment]: ...

    def dismiss_review(self, repo: str, number: int, review_id: str, message: str) -> None: ...
