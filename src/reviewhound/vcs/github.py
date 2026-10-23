"""GitHub REST adapter. The token comes from the environment, never from code or args."""

from __future__ import annotations

import base64
import logging
import os
import shutil
import subprocess
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from reviewhound.diff import Diff, parse_patch
from reviewhound.vcs.base import DirSnapshot, PostedComment, PullRequest, ReviewPayload, Snapshot

log = logging.getLogger(__name__)
API = "https://api.github.com"
_RETRY_STATUS = {429, 500, 502, 503, 504}


def _github_reason(resp: httpx.Response) -> str:
    """GitHub's own error text (never echoes credentials), so a refusal can be acted on."""
    try:
        data = resp.json()
    except ValueError:
        return ""
    if not isinstance(data, dict):
        return ""
    parts = [str(data.get("message") or "")]
    parts += [str(e.get("message") or e) if isinstance(e, dict) else str(e)
              for e in (data.get("errors") or [])]
    reason = "; ".join(p for p in parts if p)
    return f": {reason[:300]}" if reason else ""


class GitHubError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class GitHubVCS:
    def __init__(self, token: str | Callable[[], str], cache_dir: Path,
                 client: httpx.Client | None = None, max_attempts: int = 4,
                 history_depth: int = 50) -> None:
        """`token` is a static token, or a provider called per request (App installation tokens)."""
        if not token:
            raise GitHubError("GitHub token is empty; set GITHUB_TOKEN")
        self._token_source = token
        self.cache_dir = cache_dir
        self.max_attempts = max_attempts
        self.history_depth = history_depth
        self._viewer: str | None = None
        self._http = client or httpx.Client(base_url=API, timeout=httpx.Timeout(30.0))
        self._headers = {"Accept": "application/vnd.github+json",
                         "X-GitHub-Api-Version": "2022-11-28"}

    def _token(self) -> str:
        if isinstance(self._token_source, str):
            return self._token_source
        token = self._token_source()
        if not token:
            raise GitHubError("token provider returned an empty token")
        return token

    def _request(self, method: str, url: str, headers_extra: dict[str, str] | None = None,
                 **kw: Any) -> httpx.Response:
        headers = {**self._headers, "Authorization": f"Bearer {self._token()}",
                   **(headers_extra or {})}
        # A POST that timed out may have landed; retrying it could post a review twice.
        attempts = self.max_attempts if method == "GET" else 1
        delay = 1.0
        for attempt in range(1, attempts + 1):
            try:
                resp = self._http.request(method, url, headers=headers, **kw)
            except httpx.TransportError as exc:
                if attempt == attempts:
                    raise GitHubError(f"{method} {url} failed: {type(exc).__name__}") from exc
                log.warning("github transport error, retrying", extra={"url": url,
                                                                       "attempt": attempt})
            else:
                if resp.status_code < 400:
                    return resp
                if resp.status_code not in _RETRY_STATUS or attempt == attempts:
                    raise GitHubError(f"{method} {url} -> HTTP {resp.status_code}"
                                      f"{_github_reason(resp)}", resp.status_code)
            time.sleep(delay)
            delay *= 2
        raise GitHubError(f"{method} {url} exhausted retries")

    def get_pull(self, repo: str, number: int) -> PullRequest:
        d = self._request("GET", f"/repos/{repo}/pulls/{number}").json()
        return PullRequest(repo=repo, number=number, title=d.get("title") or "",
                           body=d.get("body") or "", base_sha=d["base"]["sha"],
                           head_sha=d["head"]["sha"], author=(d.get("user") or {}).get("login", ""),
                           draft=bool(d.get("draft")), state=d.get("state", "open"))

    def get_diff(self, pr: PullRequest) -> Diff:
        diff = Diff()
        page = 1
        while True:
            files = self._request("GET", f"/repos/{pr.repo}/pulls/{pr.number}/files",
                                  params={"per_page": 100, "page": page}).json()
            for f in files:
                status = {"removed": "deleted"}.get(f["status"], f["status"])
                fd = parse_patch(f["filename"], f.get("patch") or "", status,
                                 f.get("previous_filename"))
                diff.files[f["filename"]] = fd
            if len(files) < 100:
                return diff
            page += 1

    def current_head(self, repo: str, number: int) -> str:
        return self.get_pull(repo, number).head_sha

    def _git_env(self) -> dict[str, str]:
        basic = base64.b64encode(f"x-access-token:{self._token()}".encode()).decode()
        return {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.extraHeader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}"}

    def _checkout(self, repo: str, sha: str) -> Path:
        dest = self.cache_dir / repo.replace("/", "__") / sha
        if (dest / ".reviewhound-complete").exists():
            return dest
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)
        env = self._git_env()
        steps = [["git", "init", "-q"],
                 ["git", "remote", "add", "origin", f"https://github.com/{repo}.git"],
                 ["git", "fetch", "-q", f"--depth={self.history_depth}", "origin", sha],
                 ["git", "checkout", "-q", "FETCH_HEAD"]]
        for cmd in steps:
            res = subprocess.run(cmd, cwd=dest, env=env, capture_output=True, text=True, check=False,
                                 timeout=300)
            if res.returncode != 0:
                shutil.rmtree(dest, ignore_errors=True)
                raise GitHubError(f"checkout step {cmd[1]} failed with code {res.returncode}")
        (dest / ".reviewhound-complete").write_text(sha)
        return dest

    def snapshot(self, repo: str, sha: str) -> Snapshot:
        return DirSnapshot(self._checkout(repo, sha))

    def read_file(self, repo: str, sha: str, path: str) -> str | None:
        try:
            resp = self._request("GET", f"/repos/{repo}/contents/{path}", params={"ref": sha},
                                 headers_extra={"Accept": "application/vnd.github.raw+json"})
        except GitHubError as exc:
            if exc.status == 404:
                return None
            raise
        return resp.text

    def churn(self, repo: str, sha: str) -> Counter[str]:
        dest = self._checkout(repo, sha)
        res = subprocess.run(["git", "log", "--name-only", "--format="], cwd=dest, check=False,
                             capture_output=True, text=True, timeout=60)
        if res.returncode != 0:
            return Counter()
        return Counter(ln for ln in res.stdout.splitlines() if ln.strip())

    def viewer_login(self) -> str:
        """Login the token acts as, or "" when it cannot say (app installation tokens)."""
        if self._viewer is None:
            try:
                self._viewer = str(self._request("GET", "/user").json().get("login") or "")
            except GitHubError:
                self._viewer = ""
        return self._viewer

    def post_review(self, pr: PullRequest, review: ReviewPayload) -> str:
        payload = {"commit_id": review.head_sha, "event": review.event, "body": review.body,
                   "comments": [{"path": c.path, "line": c.line, "side": "RIGHT", "body": c.body}
                                for c in review.comments]}
        d = self._request("POST", f"/repos/{pr.repo}/pulls/{pr.number}/reviews",
                          json=payload).json()
        return str(d.get("id", ""))

    def review_comments(self, repo: str, number: int, review_id: str) -> list[PostedComment]:
        # The per-review endpoint omits `line`; the PR-wide list has it, so filter that by review.
        out: list[PostedComment] = []
        page = 1
        while True:
            items = self._request("GET", f"/repos/{repo}/pulls/{number}/comments",
                                  params={"per_page": 100, "page": page}).json()
            for c in items:
                if str(c.get("pull_request_review_id")) != str(review_id):
                    continue
                out.append(PostedComment(id=int(c["id"]), path=str(c.get("path") or ""),
                                         line=int(c.get("line") or c.get("original_line") or 0)))
            if len(items) < 100:
                return out
            page += 1

    def dismiss_review(self, repo: str, number: int, review_id: str, message: str) -> None:
        self._request("PUT", f"/repos/{repo}/pulls/{number}/reviews/{review_id}/dismissals",
                      json={"message": message, "event": "DISMISS"})
