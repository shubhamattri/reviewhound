"""Command line: review a PR, run the webhook server or worker, run evals, record feedback."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from reviewhound.backends import open_metrics, open_queue, open_store
from reviewhound.evals import run_eval
from reviewhound.heartbeat import DEFAULT_MAX_JOB_SECONDS
from reviewhound.metrics import BacklogAlarm, collect, render_prometheus, render_stats
from reviewhound.pipeline import Reviewer
from reviewhound.proof import (
    DEFAULT_IMAGE,
    BudgetedRunner,
    DockerRunner,
    LocalRunner,
    SandboxRunner,
)
from reviewhound.providers import ClaudeCliProvider, FakeProvider, Provider, ProviderRegistry
from reviewhound.queue import Job
from reviewhound.scanners import BuiltinScanner, Scanner, SemgrepScanner

if TYPE_CHECKING:
    from reviewhound.vcs.github_app import GitHubApp

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
DEFAULT_HOME = Path(os.environ.get("REVIEWHOUND_HOME", Path.home() / ".reviewhound"))


class _JsonFormatter(logging.Formatter):
    _SKIP = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}

    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, Any] = {"level": record.levelname, "logger": record.name,
                                "msg": record.getMessage()}
        data.update({k: v for k, v in record.__dict__.items() if k not in self._SKIP})
        return json.dumps(data, default=str)


def _setup_logging(verbose: bool) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_JsonFormatter())
    logging.basicConfig(level=logging.INFO if verbose else logging.WARNING, handlers=[handler])


def _github_token(gh_user: str | None) -> str:
    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        return token
    cmd = ["gh", "auth", "token"] + (["-u", gh_user] if gh_user else [])
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    if res.returncode != 0 or not res.stdout.strip():
        raise SystemExit("No GitHub token: set GITHUB_TOKEN or log in with `gh auth login`.")
    return res.stdout.strip()


def _registry(args: argparse.Namespace) -> ProviderRegistry:
    providers: list[Provider] = []
    if args.fake_answer:
        providers.append(FakeProvider([Path(args.fake_answer).read_text()]))
    else:
        providers.append(ClaudeCliProvider())
    return ProviderRegistry(providers)


def _worker_reviewer_factory(args: argparse.Namespace) -> Callable[[Job], Reviewer]:
    """One reviewer per job: proof runners and their time budget are per repo and per review."""
    from reviewhound.vcs.github import GitHubVCS

    home = Path(args.home)
    home.mkdir(parents=True, exist_ok=True)
    vcs = GitHubVCS(_github_token(args.gh_user), home / "checkouts")
    store = open_store(home, args.database_url)
    registry = _registry(args)
    scanners: list[Scanner] = [BuiltinScanner(), SemgrepScanner()]

    def build(job: Job) -> Reviewer:
        return Reviewer(vcs, registry, scanners, store,
                        proof_runners=_proof_runners(args, job.repo))
    return build


def _reviewer(args: argparse.Namespace, with_store: bool) -> Reviewer:
    from reviewhound.vcs.github import GitHubVCS

    home = Path(args.home)
    home.mkdir(parents=True, exist_ok=True)
    vcs = GitHubVCS(_github_token(args.gh_user), home / "checkouts")
    store = open_store(home, args.database_url) if with_store else None
    return Reviewer(vcs, _registry(args), [BuiltinScanner(), SemgrepScanner()], store,
                    proof_runners=_proof_runners(args, args.repo))


def _proof_runners(args: argparse.Namespace, repo: str) -> dict[str, SandboxRunner]:
    """Fresh runners for one review. The local runner runs PR code on this machine, so only
    repos the operator named get it; a repo's own config can never add it."""
    total = args.max_proof_seconds
    runners: dict[str, SandboxRunner] = {
        "docker": BudgetedRunner(DockerRunner(image=args.proof_image), total)}
    if repo.lower() in {r.lower() for r in args.allow_local_proof_repo}:
        runners["local"] = BudgetedRunner(LocalRunner(), total)
    return runners


def _repo_name(value: str) -> str:
    if not _REPO_RE.match(value):
        raise argparse.ArgumentTypeError(f"expected owner/name, got {value!r}")
    return value


def _positive_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a number of seconds, got {value!r}") from None
    if not seconds > 0:
        raise argparse.ArgumentTypeError(f"must be above 0, got {value!r}")
    return seconds


def _github_app() -> GitHubApp | None:
    from reviewhound.vcs.github_app import GitHubApp, GitHubAppConfigError

    try:
        return GitHubApp.from_env()
    except GitHubAppConfigError as exc:
        raise SystemExit(str(exc)) from exc


def _app_reviewer_factory(args: argparse.Namespace, app: GitHubApp) -> Callable[[Job], Reviewer]:
    """One reviewer per job, authenticated as that job's installation."""
    import httpx

    from reviewhound.vcs.github import API, GitHubVCS
    from reviewhound.vcs.github_app import token_for_job

    home = Path(args.home)
    home.mkdir(parents=True, exist_ok=True)
    store = open_store(home, args.database_url)
    registry = _registry(args)
    scanners: list[Scanner] = [BuiltinScanner(), SemgrepScanner()]
    client = httpx.Client(base_url=API, timeout=httpx.Timeout(30.0))

    def build(job: Job) -> Reviewer:
        # With an App every job uses an installation token, looked up by repo when missing.
        vcs = GitHubVCS(token_for_job(app, job, lambda: ""), home / "checkouts", client=client)
        return Reviewer(vcs, registry, scanners, store,
                        proof_runners=_proof_runners(args, job.repo))
    return build


def cmd_review(args: argparse.Namespace) -> int:
    reviewer = _reviewer(args, with_store=not args.dry_run)
    result = reviewer.review(args.repo, args.pr, dry_run=args.dry_run)
    rt = result.route
    print(f"PR {result.pr.repo}#{result.pr.number} head {result.pr.head_sha[:12]}")
    if rt:
        print(f"route: {rt.tier} (model {rt.model or '-'}) " + "; ".join(rt.reasons))
    print(f"reviewers: {', '.join(result.reviewers) or 'none'}")
    print(f"timings: {json.dumps(result.timings)}  cost: ${result.cost_usd:.4f}")
    if result.dropped:
        print("=== Dropped by the claim checker ===")
        for f, reason in result.dropped:
            print(f"- {f.file}:{f.line} {f.title} -> {reason}")
    if result.rendered:
        print(result.rendered)
    print(f"outcome: {result.outcome}" + (f" ({result.error})" if result.error else ""))
    return 0 if result.outcome not in ("post-uncertain",) else 2


def cmd_eval(args: argparse.Namespace) -> int:
    verify = not args.no_checker
    if args.live:
        claude = ClaudeCliProvider()
        report = run_eval(Path(args.cases), lambda spec: claude, verify=verify)
    else:
        report = run_eval(Path(args.cases), verify=verify)
    print(report.render())
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from reviewhound.webhook import ALLOWED_OWNERS_ENV, allowed_owners_from, make_server

    secret = os.environ.get("REVIEWHOUND_WEBHOOK_SECRET", "")
    if not secret:
        raise SystemExit("Set REVIEWHOUND_WEBHOOK_SECRET to the GitHub App webhook secret.")
    owners_env = os.environ.get(ALLOWED_OWNERS_ENV, "")
    if owners_env and allowed_owners_from(owners_env, []) is None:
        # A set but empty allowlist is a mistake; read as "unset" it would accept every owner.
        raise SystemExit(f"{ALLOWED_OWNERS_ENV} is set but names no owner ({owners_env!r}). "
                         "List owners separated by commas, or unset it to accept every owner.")
    home = Path(args.home)
    queue = open_queue(home, args.database_url)
    metrics = open_metrics(home, args.database_url)
    owners = allowed_owners_from(owners_env, args.allowed_owner)
    server = make_server(queue, secret, args.host, args.port,
                         store=open_store(home, args.database_url),
                         app_id=os.environ.get("REVIEWHOUND_APP_ID") or None,
                         metrics=lambda: render_prometheus(
                             collect(queue, metrics, args.backlog_alarm_s)),
                         allowed_owners=owners)
    print(f"listening on {args.host}:{args.port}")
    server.serve_forever()
    return 0


def cmd_worker(args: argparse.Namespace) -> int:
    from reviewhound.worker import run_forever, run_one

    queue = open_queue(Path(args.home), args.database_url)
    metrics = open_metrics(Path(args.home), args.database_url)
    app = _github_app()
    reviewer = _worker_reviewer_factory(args) if app is None else _app_reviewer_factory(args, app)
    if args.once:
        job = run_one(queue, reviewer, metrics, args.max_job_seconds)
        print("no job" if job is None else f"ran job {job.id}")
        return 0
    run_forever(queue, reviewer, metrics=metrics,
                alarm=BacklogAlarm(queue, older_than_s=args.backlog_alarm_s,
                                   interval_s=args.backlog_alarm_interval_s),
                max_job_seconds=args.max_job_seconds)
    return 0


def cmd_enqueue(args: argparse.Namespace) -> int:
    """Manual enqueue, also what the missed-PR sweep calls."""
    from reviewhound.vcs.github import GitHubVCS

    queue = open_queue(Path(args.home), args.database_url)
    vcs = GitHubVCS(_github_token(args.gh_user), Path(args.home) / "checkouts")
    pr = vcs.get_pull(args.repo, args.pr)
    job_id, created = queue.enqueue(args.repo, args.pr, pr.head_sha)
    print(f"job {job_id} {'queued' if created else 'already exists'} for head {pr.head_sha[:12]}")
    return 0


def cmd_feedback(args: argparse.Namespace) -> int:
    store = open_store(Path(args.home), args.database_url)
    ok = store.add_feedback(args.repo, args.pr, args.fingerprint, args.kind, args.detail or "")
    print("recorded" if ok else "no such finding")
    return 0 if ok else 1


def cmd_stats(args: argparse.Namespace) -> int:
    home = Path(args.home)
    queue = open_queue(home, args.database_url)
    snap = collect(queue, open_metrics(home, args.database_url), args.backlog_alarm_s)
    print(render_prometheus(snap) if args.prometheus else render_stats(snap))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="reviewhound")
    p.add_argument("--home", default=str(DEFAULT_HOME), help="state directory")
    p.add_argument("--gh-user", default=None, help="gh account to take the token from")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--database-url", default=None,
                   help="postgresql://... (default: $REVIEWHOUND_DATABASE_URL, else SQLite in --home)")
    p.add_argument("--backlog-alarm-s", type=float, default=600.0,
                   help="queued jobs older than this count as stuck")
    p.add_argument("--allow-local-proof-repo", action="append", default=[], type=_repo_name,
                   metavar="OWNER/NAME",
                   help="let this repo's proof.runner: local run generated tests on this "
                        "machine (repeatable); no other repo can")
    p.add_argument("--max-proof-seconds", type=_positive_seconds, default=300.0,
                   help="total sandbox time for all proofs in one review")
    p.add_argument("--proof-image", default=DEFAULT_IMAGE,
                   help="docker image for proof runs (build it from Dockerfile.proof)")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("review", help="review one PR")
    r.add_argument("repo")
    r.add_argument("pr", type=int)
    r.add_argument("--dry-run", action="store_true", help="print the review, post nothing")
    r.add_argument("--fake-answer", help="file with a recorded model answer (no model call)")
    r.set_defaults(func=cmd_review)

    e = sub.add_parser("eval", help="replay the eval cases and print precision and recall")
    e.add_argument("--cases", default="evals/cases")
    e.add_argument("--live", action="store_true", help="ask the real model, not recordings")
    e.add_argument("--no-checker", action="store_true",
                   help="baseline: keep every model finding, to measure what the checker adds")
    e.set_defaults(func=cmd_eval)

    s = sub.add_parser("serve", help="run the webhook receiver")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8787)
    s.add_argument("--allowed-owner", action="append", default=[],
                   help="only accept events for repos of this owner (repeatable); "
                        "adds to REVIEWHOUND_ALLOWED_OWNERS")
    s.set_defaults(func=cmd_serve)

    w = sub.add_parser("worker", help="run the review worker")
    w.add_argument("--once", action="store_true")
    w.add_argument("--fake-answer", default=None)
    w.add_argument("--backlog-alarm-interval-s", type=float, default=300.0,
                   help="log the backlog alarm at most once per this many seconds")
    w.add_argument("--max-job-seconds", type=_positive_seconds, default=DEFAULT_MAX_JOB_SECONDS,
                   help="after this a review stops renewing its lease, posts nothing and is "
                        "retried")
    w.set_defaults(func=cmd_worker)

    q = sub.add_parser("enqueue", help="queue a review for a PR's current head")
    q.add_argument("repo")
    q.add_argument("pr", type=int)
    q.set_defaults(func=cmd_enqueue)

    st = sub.add_parser("stats", help="print review, cost, failure and backlog numbers")
    st.add_argument("--prometheus", action="store_true", help="print the /metrics text instead")
    st.set_defaults(func=cmd_stats)

    f = sub.add_parser("feedback", help="record a developer reaction to a finding")
    f.add_argument("repo")
    f.add_argument("pr", type=int)
    f.add_argument("fingerprint")
    f.add_argument("kind", choices=["resolved", "fixed", "declined", "reply", "reopened"])
    f.add_argument("--detail")
    f.set_defaults(func=cmd_feedback)

    args = p.parse_args(argv)
    _setup_logging(args.verbose)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
