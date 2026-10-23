"""One review, end to end: fetch, route, index, ask, verify, scan, apply policy, post or store."""

from __future__ import annotations

import logging
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from reviewhound.checker import CheckOutcome, ClaimChecker
from reviewhound.config import CONFIG_FILENAME, RepoConfig, load_config
from reviewhound.context import build_context
from reviewhound.diff import Diff
from reviewhound.findings import Finding, FindingsParseError, ModelReview, parse_model_review
from reviewhound.index.conventions import learn_conventions
from reviewhound.index.treesitter import CodeIndex
from reviewhound.paths import language_of, matches_any
from reviewhound.policy import PolicyDecision, apply_policy
from reviewhound.poster import build_review, render_dry_run
from reviewhound.prompt import build_prompt
from reviewhound.providers import ProviderError, ProviderRegistry
from reviewhound.router import Route, route
from reviewhound.scanners import Scanner
from reviewhound.store import ReviewRecord, Store
from reviewhound.vcs.base import VCS, PullRequest, ReviewPayload, Snapshot

log = logging.getLogger(__name__)
MAX_INDEX_FILES = 5_000


@dataclass
class ReviewResult:
    pr: PullRequest
    outcome: str
    route: Route | None = None
    decision: PolicyDecision | None = None
    review: ReviewPayload | None = None
    dropped: list[tuple[Finding, str]] = field(default_factory=list)
    reviewers: list[str] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)
    cost_usd: float = 0.0
    rendered: str = ""
    error: str = ""
    post_id: str = ""


class _Timer:
    def __init__(self) -> None:
        self.timings: dict[str, float] = {}

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        start = time.monotonic()
        try:
            yield
        finally:
            self.timings[name] = round(time.monotonic() - start, 3)


def build_index(head: Snapshot, diff: Diff, cfg: RepoConfig) -> CodeIndex:
    index = CodeIndex()
    paths = [p for p in head.paths() if language_of(p) in cfg.languages
             and not matches_any(p, cfg.ignore_paths)]
    changed = set(diff.changed_paths)
    # Changed files always make it in, even past the cap.
    ordered = [p for p in paths if p in changed] + [p for p in paths if p not in changed]
    for path in ordered[:MAX_INDEX_FILES] + [p for p in ordered[MAX_INDEX_FILES:] if p in changed]:
        src = head.read(path)
        if src is not None:
            index.add(path, src)
    for path in changed:  # non-code changed files still need their text for the checker
        if path not in index.sources:
            src = head.read(path)
            if src is not None:
                index.sources[path] = src
    return index


class Reviewer:
    def __init__(self, vcs: VCS, registry: ProviderRegistry, scanners: list[Scanner],
                 store: Store | None, verify: bool = True) -> None:
        self.verify = verify  # False only for the eval baseline that measures the checker
        self.vcs = vcs
        self.registry = registry
        self.scanners = scanners
        self.store = store

    def _ask_model(self, prompt: str, rt: Route, result: ReviewResult) -> ModelReview:
        """Ask once; on malformed JSON ask once more, then give up loudly."""
        last: Exception | None = None
        for attempt in (1, 2):
            text = prompt if attempt == 1 else prompt + \
                "\n\nYour previous answer was not valid JSON for the schema. Answer with JSON only."
            res = self.registry.complete(text, model=rt.model, timeout_s=rt.timeout_s)
            result.reviewers.extend(res.attempted)
            result.cost_usd += res.completion.cost_usd
            try:
                return parse_model_review(res.completion.text)
            except FindingsParseError as exc:
                last = exc
                log.warning("model answer did not parse", extra={"attempt": attempt})
        raise ProviderError(f"model answers did not match the schema: {last}")

    def review(self, repo: str, number: int, *, dry_run: bool,
               expected_head: str | None = None) -> ReviewResult:
        t = _Timer()
        with t.stage("fetch"):
            pr = self.vcs.get_pull(repo, number)
        result = ReviewResult(pr=pr, outcome="")
        if pr.state != "open":
            result.outcome = "skipped"
            result.error = f"PR is {pr.state}"
            return self._finish(result, t, [], [])
        if expected_head is not None and pr.head_sha != expected_head:
            result.outcome = "superseded"
            result.error = f"PR head is now {pr.head_sha[:12]}"
            return self._finish(result, t, [], [])
        with t.stage("diff"):
            diff = self.vcs.get_diff(pr)
            cfg = load_config(self.vcs.read_file(repo, pr.base_sha, CONFIG_FILENAME))
        result.route = rt = route(diff, cfg)
        if rt.tier == "skip":
            result.outcome = "skipped"
            return self._finish(result, t, [], [])

        with t.stage("index"):
            head = self.vcs.snapshot(repo, pr.head_sha)
            index = build_index(head, diff, cfg)
            churn = self.vcs.churn(repo, pr.head_sha)
            conv = learn_conventions(index, churn, cfg.auth.guard_functions)
        reviewable = [p for p in diff.changed_paths if not matches_any(p, cfg.ignore_paths)]
        with t.stage("context"):
            touched = Counter({p: churn[p] for p in reviewable if churn.get(p)})
            churn_note = "\n".join(f"{p}: {n} recent commits" for p, n in touched.most_common(10))
            ctx = build_context(pr, diff, index, rt.context_bytes, reviewable, conv.summary(),
                                churn_note)
            prompt = build_prompt(ctx.text, cfg)
        with t.stage("model"):
            answer = self._ask_model(prompt, rt, result)
        with t.stage("verify"):
            checker = ClaimChecker(diff, index, cfg.policy.snap_distance)
            if self.verify:
                outcome = checker.check_all(answer.findings, f"model:{rt.model}")
            else:
                outcome = CheckOutcome(kept=[Finding.from_model(mf, f"model:{rt.model}")
                                             for mf in answer.findings])
                for f in outcome.kept:
                    f.verified = True
            result.dropped = outcome.dropped
            for f, reason in outcome.dropped:
                log.info("finding dropped", extra={"pr": number, "file": f.file,
                                                   "line": f.line, "reason": reason})
        with t.stage("scan"):
            scanned: list[Finding] = []
            for s in self.scanners:
                scanned.extend(s.scan(diff, head, cfg))
            result.reviewers.extend(f"scanner:{s.name}" for s in self.scanners)
        known = self.store.known_state(repo, number) if self.store else {}
        decision = apply_policy(outcome.kept + scanned, cfg.policy, known)
        result.decision = decision
        result.review = build_review(pr.head_sha, decision, diff, rt.tier, result.reviewers)
        result.rendered = render_dry_run(result.review, decision)

        with t.stage("post"):
            if dry_run:
                result.outcome = "dry-run"
            elif not cfg.posting:
                result.outcome = "shadow"
            elif self.vcs.current_head(repo, number) != pr.head_sha:
                result.outcome = "head-moved"
            else:
                try:
                    post_id = self.vcs.post_review(pr, result.review)
                except Exception as exc:  # noqa: BLE001 - the post may or may not have landed
                    result.outcome = "post-uncertain"
                    result.error = type(exc).__name__
                else:
                    result.outcome = "posted"
                    result.post_id = post_id
        return self._finish(result, t, decision.post,
                            [(f, r) for f, r in decision.held if r != "duplicate"])

    def _finish(self, result: ReviewResult, t: _Timer, posted: list[Finding],
                held: list[tuple[Finding, str]]) -> ReviewResult:
        result.timings = t.timings
        log.info("review finished", extra={"repo": result.pr.repo, "pr": result.pr.number,
                                           "head": result.pr.head_sha, "outcome": result.outcome,
                                           "timings": t.timings, "cost_usd": result.cost_usd})
        if self.store is not None and result.outcome != "dry-run":
            rt = result.route
            self.store.record_review(
                ReviewRecord(repo=result.pr.repo, pr=result.pr.number,
                             head_sha=result.pr.head_sha, outcome=result.outcome,
                             tier=rt.tier if rt else "", model=rt.model if rt else "",
                             providers=tuple(result.reviewers),
                             event=result.review.event if result.review else "",
                             posted=result.outcome == "posted",
                             post_id=result.post_id, cost_usd=result.cost_usd,
                             timings=t.timings, error=result.error),
                posted, held)
        return result
