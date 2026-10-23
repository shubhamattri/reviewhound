"""End-to-end QA on GitHub: open a fresh PR in a sandbox repo, let Reviewhound post a real review,
check it through the API, then check it in the browser with Playwright and record a video.

Run: uv run python qa/github_qa.py --milestone m1
The sandbox repo needs `.reviewhound.yml` with `posting: true` on its base branch, and
`expected.json` (see qa/expected.json) lists the bugs planted on the head branch.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx

QA_HOME = Path.home() / ".reviewhound" / "qa"
SUMMARY_PREFIX = "Reviewhound ("
LOGIN_HELP = ("No saved GitHub login for Playwright. Run this once and log in:\n"
              "  uv run python -m qa.login")


def saved_login_user(state_file: Path) -> str:
    """The GitHub user in a saved browser login, or "" when it is missing or signed out."""
    try:
        cookies = {c["name"]: c["value"] for c in json.loads(state_file.read_text())["cookies"]}
    except (OSError, ValueError, KeyError, TypeError):
        return ""
    return cookies.get("dotcom_user", "") if cookies.get("logged_in") == "yes" else ""


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class QAResult:
    milestone: str
    repo: str
    pr: int = 0
    pr_url: str = ""
    head: str = ""
    outcome: str = ""
    cost_usd: float = 0.0
    checks: list[Check] = field(default_factory=list)
    video: str = ""
    screenshots: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(c.ok for c in self.checks)


class Api(Protocol):
    def call(self, method: str, url: str, **kw: Any) -> Any: ...


class GitHub:
    def __init__(self, token: str) -> None:
        self.http = httpx.Client(base_url="https://api.github.com", timeout=30, headers={
            "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})

    def call(self, method: str, url: str, **kw: Any) -> Any:
        r = self.http.request(method, url, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {url} -> HTTP {r.status_code}")
        return r.json() if r.content else None


def _token(user: str | None) -> str:
    if os.environ.get("GITHUB_TOKEN"):
        return os.environ["GITHUB_TOKEN"]
    cmd = ["gh", "auth", "token"] + (["-u", user] if user else [])
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    if res.returncode != 0 or not res.stdout.strip():
        sys.exit("No GitHub token: set GITHUB_TOKEN or log in with gh.")
    return res.stdout.strip()


def _in_range(c: dict[str, Any], e: dict[str, Any]) -> bool:
    return (c["path"] == e["file"] and c["line"] is not None
            and e["lines"][0] <= c["line"] <= e["lines"][1])


def _describes(c: dict[str, Any], e: dict[str, Any]) -> bool:
    """A planted bug counts as found only if a comment in its range actually talks about it."""
    text = f"{c['title']} {c.get('body', '')}".lower()
    return _in_range(c, e) and any(w.lower() in text for w in e.get("mentions", [""]))


def fetch_bot_review(gh: Api, repo: str, number: int, head: str,
                     timeout_s: float = 60, sleep: Callable[[float], None] = time.sleep
                     ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """The review this run posted (on `head`, with our summary) and only its comments.

    GitHub can list a new review a few seconds late, so poll until it shows up.
    """
    waited = 0.0
    while True:
        reviews = gh.call("GET", f"/repos/{repo}/pulls/{number}/reviews",
                          params={"per_page": 100})
        ours = [r for r in reviews if r.get("commit_id") == head
                and (r.get("body") or "").startswith(SUMMARY_PREFIX)]
        if ours:
            review = ours[-1]
            # Only the PR-wide list carries `line`; keep this review's comments.
            comments = [c for c in gh.call("GET", f"/repos/{repo}/pulls/{number}/comments",
                                           params={"per_page": 100})
                        if c.get("pull_request_review_id") == review["id"]]
            posted = [{"id": c["id"], "path": c["path"], "line": c.get("line"),
                       "title": c["body"].split("\n", 1)[0].replace("**", "").strip(),
                       "body": c["body"]}
                      for c in comments]
            return review, posted
        if waited >= timeout_s:
            return None, []
        sleep(5)
        waited += 5


def api_checks(review: dict[str, Any] | None, posted: list[dict[str, Any]],
               expected: list[dict[str, Any]]) -> list[Check]:
    checks = [Check("API: review by Reviewhound on this head", review is not None,
                    str(review["id"]) if review else "no review on this head")]
    for e in expected:
        hit = [c for c in posted if _describes(c, e)]
        checks.append(Check(f"API: comment for {e['bug']}", bool(hit),
                            f"{e['file']}:{e['lines']} -> {[h['line'] for h in hit]}"))
    stray = [c for c in posted if not any(_in_range(c, e) for e in expected)]
    checks.append(Check("API: no comments outside planted bugs", not stray, json.dumps(stray)))
    untitled = [c["id"] for c in posted if not c["title"]]
    checks.append(Check("API: every comment has a title", not untitled, str(untitled)))
    return checks


def browser_checks(pr_url: str, review: dict[str, Any], posted: list[dict[str, Any]],
                   out: Path, result: QAResult) -> list[Check]:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    checks: list[Check] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(storage_state=str(QA_HOME / "github-state.json"),
                                  viewport={"width": 1440, "height": 900},
                                  record_video_dir=str(out),
                                  record_video_size={"width": 1440, "height": 900})
        page = ctx.new_page()
        try:
            page.goto(f"{pr_url}#pullrequestreview-{review['id']}", wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            login = page.locator("meta[name='user-login']")
            user = login.get_attribute("content", timeout=5000) if login.count() else ""
            checks.append(Check("Browser: signed in to GitHub", bool(user),
                                user or "saved login expired; run the login command again"))
            summary_line = (review.get("body") or "").splitlines()[0]
            block = page.locator(f"#pullrequestreview-{review['id']}")
            shown = block.count() > 0 and summary_line in (block.first.inner_text() or "")
            if shown:
                block.first.scroll_into_view_if_needed()
                page.wait_for_timeout(1500)
            checks.append(Check("Browser: review summary shown in its own review block", shown,
                                summary_line))
            page.screenshot(path=str(out / "conversation.png"))
            result.screenshots.append(str(out / "conversation.png"))
            for c in posted:
                checks.append(_comment_check(page, pr_url, c, out, result))
        except PlaywrightError as exc:
            checks.append(Check("Browser: run finished without errors", False, type(exc).__name__))
            page.screenshot(path=str(out / "error.png"))
            result.screenshots.append(str(out / "error.png"))
        finally:
            video = page.video
            ctx.close()  # finalises the video file
            browser.close()
            if video is not None:
                final = out / "walkthrough.webm"
                Path(video.path()).rename(final)
                result.video = str(final)
    return checks


def _comment_check(page: Any, pr_url: str, c: dict[str, Any], out: Path,
                   result: QAResult) -> Check:
    anchor = f"discussion_r{c['id']}"
    page.goto(f"{pr_url}#{anchor}", wait_until="domcontentloaded")
    page.wait_for_timeout(2500)
    el = page.locator(f"#{anchor}")
    if el.count() == 0 or not el.first.is_visible():
        return Check(f"Browser: comment {c['id']} visible", False, "anchor not on page")
    el.first.scroll_into_view_if_needed()
    has_title = c["title"] in el.first.inner_text()
    # The file path sits in the thread header a few levels above the comment body.
    has_path = el.first.evaluate(
        "(e, path) => { for (let x = e, i = 0; x && i < 8; x = x.parentElement, i++)"
        " { if ((x.innerText || '').includes(path)) return true; } return false; }", c["path"])
    shot = out / f"comment-{c['id']}.png"
    page.screenshot(path=str(shot))
    result.screenshots.append(str(shot))
    return Check(f"Browser: '{c['title'][:60]}' shown on {c['path']}:{c['line']}",
                 bool(has_title and has_path), f"title={has_title} path={has_path}")


def _cleanup(gh: Api, repo: str, number: int, branch: str) -> list[Check]:
    checks = []
    if number:
        try:
            gh.call("PATCH", f"/repos/{repo}/pulls/{number}", json={"state": "closed"})
        except RuntimeError as exc:
            checks.append(Check("Cleanup: QA PR closed", False, str(exc)))
    try:
        gh.call("DELETE", f"/repos/{repo}/git/refs/heads/{branch}")
    except RuntimeError as exc:
        checks.append(Check("Cleanup: QA branch deleted", False, str(exc)))
    return checks


def run(args: argparse.Namespace, gh: Api, token: str, out: Path,
        review_pr: Callable[[str, int, str], Any]) -> QAResult:
    expected = json.loads(Path(args.expected).read_text())
    result = QAResult(milestone=args.milestone, repo=args.repo)
    branch = f"qa/{out.name}"
    created_branch = False
    try:
        sha = gh.call("GET", f"/repos/{args.repo}/git/ref/heads/{args.head_ref}")["object"]["sha"]
        gh.call("POST", f"/repos/{args.repo}/git/refs",
                json={"ref": f"refs/heads/{branch}", "sha": sha})
        created_branch = True
        pr = gh.call("POST", f"/repos/{args.repo}/pulls", json={
            "title": f"QA run {out.name}", "head": branch, "base": args.base,
            "body": "Automated QA run for Reviewhound. Closed when the run ends."})
        result.pr, result.pr_url = int(pr["number"]), pr["html_url"]
        review = review_pr(args.repo, result.pr, token)
        result.head, result.outcome = review.pr.head_sha, review.outcome
        result.cost_usd = review.cost_usd
        result.checks.append(Check("Pipeline: review posted", review.outcome == "posted",
                                   review.outcome + (f" ({review.error})" if review.error else "")))
        if review.outcome == "posted":
            bot_review, posted = fetch_bot_review(gh, args.repo, result.pr, result.head)
            result.checks.extend(api_checks(bot_review, posted, expected))
            if bot_review is not None:
                result.checks.extend(browser_checks(result.pr_url, bot_review, posted, out,
                                                    result))
    except Exception as exc:  # noqa: BLE001 - any failure is a FAIL in the report, never a crash
        result.checks.append(Check("Run finished without errors", False,
                                   f"{type(exc).__name__}: {exc}"))
    finally:
        if created_branch and not args.keep_pr:
            result.checks.extend(_cleanup(gh, args.repo, result.pr, branch))
        (out / "result.json").write_text(json.dumps({**asdict(result), "passed": result.passed},
                                                    indent=2))
    return result


def _review_with_reviewhound(repo: str, number: int, token: str) -> Any:
    from reviewhound.pipeline import Reviewer
    from reviewhound.providers import ClaudeCliProvider, ProviderRegistry
    from reviewhound.scanners import BuiltinScanner
    from reviewhound.store import Store
    from reviewhound.vcs.github import GitHubVCS

    store = Store(QA_HOME / "runs" / f"pr-{number}-store.db")
    reviewer = Reviewer(GitHubVCS(token, QA_HOME / "checkouts"),
                        ProviderRegistry([ClaudeCliProvider()]), [BuiltinScanner()], store)
    return reviewer.review(repo, number, dry_run=False)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--milestone", required=True)
    ap.add_argument("--repo", default="shubhamattri/reviewhound-sandbox")
    ap.add_argument("--head-ref", default="feature/refunds")
    ap.add_argument("--base", default="main")
    ap.add_argument("--gh-user", default="shubhamattri")
    ap.add_argument("--expected", default=str(Path(__file__).parent / "expected.json"))
    ap.add_argument("--keep-pr", action="store_true", help="leave the QA PR open")
    args = ap.parse_args()

    if not saved_login_user(QA_HOME / "github-state.json"):
        sys.exit(LOGIN_HELP)
    out = QA_HOME / "runs" / f"{args.milestone}-{time.strftime('%Y%m%d-%H%M%S')}"
    out.mkdir(parents=True)
    token = _token(args.gh_user)
    result = run(args, GitHub(token), token, out, _review_with_reviewhound)
    print(f"QA {args.milestone}: {'PASS' if result.passed else 'FAIL'}  PR {result.pr_url}")
    for c in result.checks:
        print(f"  [{'PASS' if c.ok else 'FAIL'}] {c.name}" + (f"  ({c.detail})" if c.detail else ""))
    print(f"  video: {result.video or 'none'}\n  evidence: {out}")
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
