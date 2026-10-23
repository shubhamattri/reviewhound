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
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx

from reviewhound.pipeline import Reviewer
from reviewhound.providers import ClaudeCliProvider, ProviderRegistry
from reviewhound.scanners import BuiltinScanner
from reviewhound.store import Store
from reviewhound.vcs.github import GitHubVCS

QA_HOME = Path.home() / ".reviewhound" / "qa"
LOGIN_HELP = ("No saved GitHub login for Playwright. Run once, log in, then close the window:\n"
              f"  uv run playwright open --save-storage={QA_HOME}/github-state.json "
              "https://github.com/login")


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


def _token(user: str | None) -> str:
    if os.environ.get("GITHUB_TOKEN"):
        return os.environ["GITHUB_TOKEN"]
    cmd = ["gh", "auth", "token"] + (["-u", user] if user else [])
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    if res.returncode != 0 or not res.stdout.strip():
        sys.exit("No GitHub token: set GITHUB_TOKEN or log in with gh.")
    return res.stdout.strip()


class GitHub:
    def __init__(self, token: str) -> None:
        self.http = httpx.Client(base_url="https://api.github.com", timeout=30, headers={
            "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})

    def call(self, method: str, url: str, **kw: Any) -> Any:
        r = self.http.request(method, url, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {url} -> HTTP {r.status_code}")
        return r.json() if r.content else None


def open_fresh_pr(gh: GitHub, repo: str, head_ref: str, base: str, tag: str) -> tuple[int, str, str]:
    sha = gh.call("GET", f"/repos/{repo}/git/ref/heads/{head_ref}")["object"]["sha"]
    branch = f"qa/{tag}"
    gh.call("POST", f"/repos/{repo}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": sha})
    pr = gh.call("POST", f"/repos/{repo}/pulls", json={
        "title": f"QA run {tag}", "head": branch, "base": base,
        "body": "Automated QA run for Reviewhound. Closed when the run ends."})
    return int(pr["number"]), pr["html_url"], branch


def close_pr(gh: GitHub, repo: str, number: int, branch: str) -> None:
    gh.call("PATCH", f"/repos/{repo}/pulls/{number}", json={"state": "closed"})
    gh.call("DELETE", f"/repos/{repo}/git/refs/heads/{branch}")


def api_checks(gh: GitHub, repo: str, number: int, expected: list[dict[str, Any]]
               ) -> tuple[list[Check], list[dict[str, Any]]]:
    comments = gh.call("GET", f"/repos/{repo}/pulls/{number}/comments", params={"per_page": 100})
    posted = [{"path": c["path"], "line": c.get("line"), "title": c["body"].split("\n", 1)[0]}
              for c in comments]
    checks = []
    for e in expected:
        hit = [p for p in posted if p["path"] == e["file"] and p["line"] is not None
               and e["lines"][0] <= p["line"] <= e["lines"][1]]
        checks.append(Check(f"API: comment for {e['bug']}", bool(hit),
                            f"{e['file']}:{e['lines']} -> {[h['line'] for h in hit]}"))
    unexpected = [p for p in posted if not any(
        p["path"] == e["file"] and p["line"] is not None and
        e["lines"][0] <= p["line"] <= e["lines"][1] for e in expected)]
    checks.append(Check("API: no comments outside planted bugs", not unexpected,
                        json.dumps(unexpected)))
    return checks, posted


def browser_checks(pr_url: str, posted: list[dict[str, Any]], out: Path,
                   result: QAResult) -> list[Check]:
    from playwright.sync_api import sync_playwright

    state = QA_HOME / "github-state.json"
    checks: list[Check] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(storage_state=str(state), viewport={"width": 1440, "height": 900},
                                  record_video_dir=str(out), record_video_size={"width": 1440,
                                                                                "height": 900})
        page = ctx.new_page()
        page.goto(pr_url, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        signed_in = page.locator("meta[name='user-login']").get_attribute("content") or ""
        checks.append(Check("Browser: signed in to GitHub", bool(signed_in), signed_in or
                            "not signed in; refresh the saved login"))
        summary = page.get_by_text("Reviewhound (").first
        checks.append(Check("Browser: review summary visible on Conversation tab",
                            summary.count() > 0 and summary.is_visible()))
        if summary.count():
            summary.scroll_into_view_if_needed()
            page.wait_for_timeout(1500)
        shot = out / "conversation.png"
        page.screenshot(path=str(shot), full_page=True)
        result.screenshots.append(str(shot))

        page.goto(pr_url + "/files", wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        for c in posted:
            title = c["title"].replace("**", "").split("] ", 1)[-1].strip()
            loc = page.get_by_text(title, exact=False).first
            visible = loc.count() > 0
            if visible:
                loc.scroll_into_view_if_needed()
                page.wait_for_timeout(1500)
                visible = loc.is_visible()
            checks.append(Check(f"Browser: '{title[:60]}' shown on {c['path']}:{c['line']}",
                                visible))
        shot = out / "files.png"
        page.screenshot(path=str(shot), full_page=True)
        result.screenshots.append(str(shot))
        video = page.video
        ctx.close()
        browser.close()
        if video is not None:
            final = out / "walkthrough.webm"
            Path(video.path()).rename(final)
            result.video = str(final)
    return checks


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

    if not (QA_HOME / "github-state.json").exists():
        sys.exit(LOGIN_HELP)
    tag = f"{args.milestone}-{time.strftime('%Y%m%d-%H%M%S')}"
    out = QA_HOME / "runs" / tag
    out.mkdir(parents=True)
    expected = json.loads(Path(args.expected).read_text())
    token = _token(args.gh_user)
    gh = GitHub(token)
    result = QAResult(milestone=args.milestone, repo=args.repo)

    number, url, branch = open_fresh_pr(gh, args.repo, args.head_ref, args.base, tag)
    result.pr, result.pr_url = number, url
    try:
        reviewer = Reviewer(GitHubVCS(token, QA_HOME / "checkouts"),
                            ProviderRegistry([ClaudeCliProvider()]), [BuiltinScanner()],
                            Store(out / "store.db"))
        review = reviewer.review(args.repo, number, dry_run=False)
        result.head, result.outcome, result.cost_usd = (review.pr.head_sha, review.outcome,
                                                        review.cost_usd)
        result.checks.append(Check("Pipeline: review posted", review.outcome == "posted",
                                   review.outcome + (f" ({review.error})" if review.error else "")))
        if review.outcome == "posted":
            time.sleep(3)  # GitHub can take a moment to list new review comments
            checks, posted = api_checks(gh, args.repo, number, expected)
            result.checks.extend(checks)
            result.checks.extend(browser_checks(url, posted, out, result))
    finally:
        if not args.keep_pr:
            close_pr(gh, args.repo, number, branch)

    (out / "result.json").write_text(json.dumps({**asdict(result), "passed": result.passed},
                                                indent=2))
    print(f"QA {args.milestone}: {'PASS' if result.passed else 'FAIL'}  PR {url}")
    for c in result.checks:
        print(f"  [{'PASS' if c.ok else 'FAIL'}] {c.name}" + (f"  ({c.detail})" if c.detail else ""))
    print(f"  video: {result.video or 'none'}\n  evidence: {out}")
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
