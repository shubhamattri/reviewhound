"""Command line: review a PR, run the webhook server or worker, run evals, record feedback."""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from reviewhound.backends import open_metrics, open_queue, open_store
from reviewhound.evals import run_eval
from reviewhound.metrics import BacklogAlarm, collect, render_prometheus, render_stats
from reviewhound.pipeline import Reviewer
from reviewhound.proof import DockerRunner, LocalRunner, SandboxRunner
from reviewhound.providers import ClaudeCliProvider, FakeProvider, Provider, ProviderRegistry
from reviewhound.queue import Job
from reviewhound.scanners import BuiltinScanner, Scanner, SemgrepScanner

if TYPE_CHECKING:
    from reviewhound.vcs.github_app import GitHubApp

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


def _reviewer(args: argparse.Namespace, with_store: bool) -> Reviewer:
    from reviewhound.vcs.github import GitHubVCS

    home = Path(args.home)
    home.mkdir(parents=True, exist_ok=True)
    vcs = GitHubVCS(_github_token(args.gh_user), home / "checkouts")
    store = open_store(home, args.database_url) if with_store else None
    return Reviewer(vcs, _registry(args), [BuiltinScanner(), SemgrepScanner()], store,
                    proof_runners=_proof_runners(args))


def _proof_runners(args: argparse.Namespace) -> dict[str, SandboxRunner]:
    runners: dict[str, SandboxRunner] = {"docker": DockerRunner(image=args.proof_image)}
    if args.allow_local_proof:  # runs PR code on this machine: trusted repos only
        runners["local"] = LocalRunner()
    return runners


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
    runners = _proof_runners(args)
    client = httpx.Client(base_url=API, timeout=httpx.Timeout(30.0))

    def build(job: Job) -> Reviewer:
        # With an App every job uses an installation token, looked up by repo when missing.
        vcs = GitHubVCS(token_for_job(app, job, lambda: ""), home / "checkouts", client=client)
        return Reviewer(vcs, registry, scanners, store, proof_runners=runners)
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
    from reviewhound.webhook import make_server

    secret = os.environ.get("REVIEWHOUND_WEBHOOK_SECRET", "")
    if not secret:
        raise SystemExit("Set REVIEWHOUND_WEBHOOK_SECRET to the GitHub App webhook secret.")
    home = Path(args.home)
    queue = open_queue(home, args.database_url)
    metrics = open_metrics(home, args.database_url)
    server = make_server(queue, secret, args.host, args.port,
                         store=open_store(home, args.database_url),
                         app_id=os.environ.get("REVIEWHOUND_APP_ID") or None,
                         metrics=lambda: render_prometheus(
                             collect(queue, metrics, args.backlog_alarm_s)))
    print(f"listening on {args.host}:{args.port}")
    server.serve_forever()
    return 0


def cmd_worker(args: argparse.Namespace) -> int:
    from reviewhound.worker import run_forever, run_one

    queue = open_queue(Path(args.home), args.database_url)
    metrics = open_metrics(Path(args.home), args.database_url)
    app = _github_app()
    reviewer: Reviewer | Callable[[Job], Reviewer] = (
        _reviewer(args, with_store=True) if app is None else _app_reviewer_factory(args, app))
    if args.once:
        job = run_one(queue, reviewer, metrics)
        print("no job" if job is None else f"ran job {job.id}")
        return 0
    run_forever(queue, reviewer, metrics=metrics,
                alarm=BacklogAlarm(queue, older_than_s=args.backlog_alarm_s,
                                   interval_s=args.backlog_alarm_interval_s))
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
    p.add_argument("--allow-local-proof", action="store_true",
                   help="let repos with proof.runner: local run generated tests on this machine")
    p.add_argument("--proof-image", default="python:3.12-slim",
                   help="docker image for proof runs; it must have pytest installed")
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
    s.set_defaults(func=cmd_serve)

    w = sub.add_parser("worker", help="run the review worker")
    w.add_argument("--once", action="store_true")
    w.add_argument("--fake-answer", default=None)
    w.add_argument("--backlog-alarm-interval-s", type=float, default=300.0,
                   help="log the backlog alarm at most once per this many seconds")
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
