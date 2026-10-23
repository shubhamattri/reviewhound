from pathlib import Path
from typing import Any

import httpx

from reviewhound.diff import Diff
from reviewhound.pipeline import Reviewer, not_reviewed
from reviewhound.policy import PolicyDecision
from reviewhound.poster import build_review
from reviewhound.providers import FakeProvider, ProviderRegistry
from reviewhound.vcs.base import DictSnapshot, DirSnapshot, PullRequest, Snapshot
from reviewhound.vcs.github import GitHubVCS
from reviewhound.vcs.memory import MemoryVCS, diff_file_maps
from tests.conftest import answer, make_pr


def gh_files(files: list[dict[str, Any]], tmp_path: Path) -> GitHubVCS:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=files)
    client = httpx.Client(base_url="https://api.github.com",
                          transport=httpx.MockTransport(handler))
    return GitHubVCS("t0ken", tmp_path, client=client)


def test_github_files_without_a_patch_are_recorded_with_a_reason(tmp_path: Path) -> None:
    files = [
        {"filename": "big.py", "status": "modified", "changes": 9000},
        {"filename": "logo.png", "status": "added", "changes": 0},
        {"filename": "moved.py", "status": "renamed", "changes": 0,
         "previous_filename": "old.py"},
        {"filename": "gone.bin", "status": "removed", "changes": 0},
        {"filename": "ok.py", "status": "modified", "changes": 1,
         "patch": "@@ -1 +1 @@\n-a = 1\n+a = 2"},
    ]
    diff = gh_files(files, tmp_path).get_diff(make_pr())
    assert diff.skipped == {"big.py": "diff too large for GitHub to return",
                            "logo.png": "binary file, no text diff"}
    assert set(diff.files) == {"big.py", "logo.png", "moved.py", "gone.bin", "ok.py"}
    assert diff.files["ok.py"].added_lines == {1}


def test_dir_snapshot_explains_why_it_refused_a_file(tmp_path: Path) -> None:
    (tmp_path / "big.py").write_text("x" * 50)
    (tmp_path / "bin.dat").write_bytes(b"\xff\xfe\x00")
    (tmp_path / "ok.py").write_text("x = 1\n")
    snap = DirSnapshot(tmp_path, max_file_bytes=10)
    assert snap.read("big.py") is None
    assert snap.skip_reason("big.py") == "over the 10-byte snapshot size cap"
    assert snap.skip_reason("bin.dat") == "not UTF-8 text"
    assert snap.skip_reason("ok.py") is None
    assert snap.skip_reason("missing.py") is None
    assert DictSnapshot({"a": "b"}).skip_reason("a") is None


def test_not_reviewed_merges_omitted_patches_and_unreadable_files(tmp_path: Path) -> None:
    (tmp_path / "huge.py").write_text("y" * 50)
    (tmp_path / "small.py").write_text("y = 1\n")
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor" / "lib.py").write_text("z" * 50)
    diff = diff_file_maps({}, {"huge.py": "y\n", "small.py": "y = 1\n", "vendor/lib.py": "z\n",
                               "logo.png": "p\n"})
    diff.skipped["logo.png"] = "binary file, no text diff"
    got = not_reviewed(diff, DirSnapshot(tmp_path, max_file_bytes=10), ["vendor/**"])
    assert got == [("huge.py", "over the 10-byte snapshot size cap"),
                   ("logo.png", "binary file, no text diff")]


def test_review_body_lists_files_that_were_not_reviewed() -> None:
    review = build_review("h1", PolicyDecision(), Diff(), "light", ["fake:ok"],
                          not_reviewed=[("big.py", "diff too large for GitHub to return")])
    assert review.body.splitlines() == [
        "Reviewhound (light review). Reviewers that ran: fake:ok.",
        "No verified issues found.",
        "- not reviewed: big.py (diff too large for GitHub to return)"]
    assert build_review("h1", PolicyDecision(), Diff(), "light", []).body.splitlines() == [
        "Reviewhound (light review). Reviewers that ran: none.", "No verified issues found."]


class _CappedVCS(MemoryVCS):
    def __init__(self, pr: PullRequest, base: dict[str, str], head: dict[str, str],
                 root: Path) -> None:
        super().__init__(pr, base, head)
        self.root = root

    def get_diff(self, pr: PullRequest) -> Diff:
        diff = super().get_diff(pr)
        diff.skipped["assets/logo.png"] = "binary file, no text diff"
        return diff

    def snapshot(self, repo: str, sha: str) -> Snapshot:
        return DirSnapshot(self.root, max_file_bytes=30)


def test_pipeline_puts_skipped_files_in_the_review_body(tmp_path: Path) -> None:
    head = {"app.py": "def f():\n    return 1\n", "gen.py": "x = '" + "a" * 40 + "'\n"}
    for path, text in head.items():
        (tmp_path / path).write_text(text)
    vcs = _CappedVCS(make_pr(head="h1"), {}, head, tmp_path)
    result = Reviewer(vcs, ProviderRegistry([FakeProvider([answer()])]), [], None).review(
        "acme/shop", 7, dry_run=True)
    assert result.review is not None
    assert [ln for ln in result.review.body.splitlines() if "not reviewed" in ln] == [
        "- not reviewed: assets/logo.png (binary file, no text diff)",
        "- not reviewed: gen.py (over the 30-byte snapshot size cap)"]
