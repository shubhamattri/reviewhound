"""Reviewing a PR as it stood at one of its own past commits: dry run only, closed PRs too."""

from pathlib import Path
from typing import Any

import httpx
import pytest

from reviewhound import cli
from reviewhound.pipeline import Reviewer
from reviewhound.providers import FakeProvider, ProviderRegistry
from reviewhound.scanners import BuiltinScanner
from reviewhound.vcs.base import ShaNotOnPull
from reviewhound.vcs.github import GitHubError, GitHubVCS
from reviewhound.vcs.memory import MemoryVCS
from tests.conftest import answer, make_pr
from tests.test_pipeline import BASE, HEAD, REAL

FULL = "a1b2c3d4" + "0" * 32
OTHER = "ffee0011" + "0" * 32
PATCH = {"filename": "app/x.py", "status": "modified", "changes": 1,
         "patch": "@@ -1 +1 @@\n-a = 1\n+a = 2"}


def gh(commits: list[str], files: list[dict[str, Any]], seen: list[str],
       tmp_path: Path) -> GitHubVCS:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.url.path}?{request.url.query.decode()}")
        if request.url.path == "/repos/acme/shop/pulls/7/commits":
            page = int(request.url.params["page"])
            batch = commits[(page - 1) * 100:page * 100]
            return httpx.Response(200, json=[{"sha": s} for s in batch])
        if request.url.path.startswith("/repos/acme/shop/compare/"):
            return httpx.Response(200, json={"merge_base_commit": {"sha": "mb1"},
                                             "files": files})
        return httpx.Response(404, json={"message": "Not Found"})
    client = httpx.Client(base_url="https://api.github.com",
                          transport=httpx.MockTransport(handler))
    return GitHubVCS("t0ken", tmp_path, client=client)


def test_pull_at_resolves_a_short_sha_and_diffs_it_from_the_merge_base(tmp_path: Path) -> None:
    seen: list[str] = []
    pr = make_pr(head=OTHER, base="b1", state="closed")
    pinned, diff = gh([FULL, OTHER], [PATCH], seen, tmp_path).pull_at(pr, "A1B2C3D")
    assert pinned == make_pr(head=FULL, base="mb1", state="closed")
    assert seen[-1] == f"/repos/acme/shop/compare/b1...{FULL}?"
    assert set(diff.files) == {"app/x.py"}
    assert diff.files["app/x.py"].added_lines == {1}


def test_pull_at_finds_a_commit_on_the_second_page_of_pr_commits(tmp_path: Path) -> None:
    seen: list[str] = []
    commits = [f"{i:040x}" for i in range(1, 101)] + [FULL]
    pinned, _ = gh(commits, [PATCH], seen, tmp_path).pull_at(make_pr(), FULL[:12])
    assert pinned.head_sha == FULL
    assert seen[:2] == ["/repos/acme/shop/pulls/7/commits?per_page=100&page=1",
                        "/repos/acme/shop/pulls/7/commits?per_page=100&page=2"]


def test_pull_at_refuses_a_sha_that_is_not_on_the_pr_and_fetches_no_diff(tmp_path: Path) -> None:
    seen: list[str] = []
    with pytest.raises(ShaNotOnPull, match="is not a commit on acme/shop#7"):
        gh([OTHER], [PATCH], seen, tmp_path).pull_at(make_pr(), FULL[:7])
    assert not [u for u in seen if "/compare/" in u]


def test_pull_at_refuses_a_short_sha_that_matches_two_commits(tmp_path: Path) -> None:
    twin = "a1b2c3d4" + "1" * 32
    with pytest.raises(ShaNotOnPull, match="is ambiguous on acme/shop#7"):
        gh([FULL, twin], [PATCH], [], tmp_path).pull_at(make_pr(), "a1b2c3d")


def test_pull_at_names_githubs_commit_cap_when_the_sha_is_missing(tmp_path: Path) -> None:
    commits = [f"{i:040x}" for i in range(1, 251)]
    with pytest.raises(ShaNotOnPull, match="lists only its first 250 commits"):
        gh(commits, [PATCH], [], tmp_path).pull_at(make_pr(), FULL)


def test_pull_at_refuses_a_compare_that_names_no_merge_base(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/commits"):
            return httpx.Response(200, json=[{"sha": FULL}])
        return httpx.Response(200, json={"files": [PATCH]})
    client = httpx.Client(base_url="https://api.github.com",
                          transport=httpx.MockTransport(handler))
    with pytest.raises(GitHubError, match="named no merge base"):
        GitHubVCS("t0ken", tmp_path, client=client).pull_at(make_pr(), FULL)


def test_pull_at_refuses_a_compare_cut_short_at_githubs_file_cap(tmp_path: Path) -> None:
    files = [{**PATCH, "filename": f"f{i}.py"} for i in range(300)]
    with pytest.raises(GitHubError, match="300 files"):
        gh([FULL], files, [], tmp_path).pull_at(make_pr(), FULL)


def pinned_setup(answers: list[str]) -> tuple[MemoryVCS, Reviewer, FakeProvider]:
    vcs = MemoryVCS(make_pr(head="h2", state="closed"), dict(BASE), dict(HEAD))
    vcs.commits = ["h1", "h2"]
    vcs.merge_base = "mb1"
    provider = FakeProvider(answers)
    return vcs, Reviewer(vcs, ProviderRegistry([provider]), [BuiltinScanner()], None), provider


def test_merged_pr_is_reviewed_in_a_dry_run_at_the_pinned_commit() -> None:
    vcs, reviewer, provider = pinned_setup([answer(REAL)])
    result = reviewer.review("acme/shop", 7, dry_run=True, at_sha="h1")
    assert result.outcome == "dry-run"
    assert result.pr.head_sha == "h1"
    assert result.review is not None and result.review.head_sha == "h1"
    assert [(c.path, c.line) for c in result.review.comments] == [("app/admin.py", 11)]
    assert len(provider.prompts) == 1
    assert vcs.posted == []
    assert result.pr.base_sha == "mb1"
    assert {("snapshot", "h1"), ("churn", "h1"), ("read_file", "mb1")} <= set(vcs.reads)
    assert {sha for _, sha in vcs.reads} <= {"h1", "mb1"}


def test_closed_pr_without_a_pinned_commit_is_still_skipped_in_a_dry_run() -> None:
    vcs, reviewer, provider = pinned_setup([])
    result = reviewer.review("acme/shop", 7, dry_run=True)
    assert (result.outcome, result.error) == ("skipped", "PR is closed")
    assert provider.prompts == []


def test_pinned_review_is_refused_outside_a_dry_run_before_any_fetch() -> None:
    vcs, reviewer, provider = pinned_setup([answer(REAL)])
    vcs.get_pull = None  # type: ignore[method-assign,assignment]
    with pytest.raises(ValueError, match="only in a dry run"):
        reviewer.review("acme/shop", 7, dry_run=False, at_sha="h1")
    assert provider.prompts == []
    assert vcs.posted == []


def test_pinned_review_of_a_commit_not_on_the_pr_raises_and_asks_no_model() -> None:
    vcs, reviewer, provider = pinned_setup([answer(REAL)])
    with pytest.raises(ShaNotOnPull):
        reviewer.review("acme/shop", 7, dry_run=True, at_sha="h9")
    assert provider.prompts == []


def test_cli_refuses_at_sha_without_dry_run_before_any_github_call() -> None:
    with pytest.raises(SystemExit, match="needs --dry-run"):
        cli.main(["review", "acme/shop", "7", "--at-sha", "abc1234"])


@pytest.mark.parametrize("bad", ["abc123", "xyz1234", "a" * 41])
def test_cli_rejects_a_value_that_is_not_a_short_or_full_sha(bad: str) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["review", "acme/shop", "7", "--dry-run", "--at-sha", bad])
    assert exc.value.code == 2


def test_cli_turns_a_sha_not_on_the_pr_into_a_one_line_exit(
        monkeypatch: pytest.MonkeyPatch) -> None:
    _, reviewer, _ = pinned_setup([])
    monkeypatch.setattr(cli, "_reviewer", lambda args, with_store: reviewer)
    with pytest.raises(SystemExit, match="^--at-sha: abc1234 is not exactly one commit"):
        cli.main(["review", "acme/shop", "7", "--dry-run", "--at-sha", "ABC1234"])


def test_cli_turns_a_github_refusal_of_the_pinned_review_into_a_one_line_exit(
        monkeypatch: pytest.MonkeyPatch) -> None:
    vcs, reviewer, _ = pinned_setup([])

    def too_big(pr: object, sha: str) -> None:
        raise GitHubError("compare lists 300 files, GitHub's cap, so the diff may be cut short")
    vcs.pull_at = too_big  # type: ignore[method-assign,assignment]
    monkeypatch.setattr(cli, "_reviewer", lambda args, with_store: reviewer)
    with pytest.raises(SystemExit, match="^--at-sha abcdef1: compare lists 300 files"):
        cli.main(["review", "acme/shop", "7", "--dry-run", "--at-sha", "ABCDEF1"])
