from __future__ import annotations

import json
from typing import Any

import pytest

from reviewhound.vcs.base import PullRequest


class FakeClock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def make_pr(head: str = "h1", base: str = "b1", **kw: Any) -> PullRequest:
    return PullRequest(repo=kw.pop("repo", "acme/shop"), number=kw.pop("number", 7),
                       title=kw.pop("title", "Add delete endpoint"), body=kw.pop("body", ""),
                       base_sha=base, head_sha=head, **kw)


def answer(*findings: dict[str, Any]) -> str:
    return json.dumps({"findings": list(findings)})
