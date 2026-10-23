"""One review, end to end: fetch, route, index, ask, verify, scan, apply policy, post or store."""

from __future__ import annotations

import logging
import time
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field

from reviewhound.checker import CheckOutcome, ClaimChecker
from reviewhound.config import CONFIG_FILENAME, PolicyConfig, RepoConfig, Severity, load_config
from reviewhound.context import build_context
from reviewhound.diff import Diff
from reviewhound.findings import Finding, FindingsParseError, ModelReview, parse_model_review
from reviewhound.github_feedback import after_post
from reviewhound.index.conventions import learn_conventions
from reviewhound.index.treesitter import CodeIndex
from reviewhound.paths import language_of, matches_any
from reviewhound.policy import PolicyDecision, apply_policy
from reviewhound.poster import SUMMARY_PREFIX, build_review, own_pr_review, render_dry_run
from reviewhound.prompt import build_prompt
from reviewhound.proof import DockerRunner, Prover, SandboxRunner, run_proof_stage, select_runner
from reviewhound.providers import ProviderError, ProviderRegistry
from reviewhound.queue import DEFAULT_LEASE_S
from reviewhound.router import Route, route
from reviewhound.scanners import Scanner
from reviewhound.store import Blocker, ReviewRecord, ReviewStore
from reviewhound.vcs.base import (
    VCS,
    PostedComment,
    PostedReview,
    PullRequest,
    ReviewPayload,
    ReviewThreads,
    Snapshot,
)
from reviewhound.vcs.github import GitHubError

log = logging.getLogger(__name__)
MAX_INDEX_FILES = 5_000
MAX_BODY_CHARS = 60_000  # GitHub refuses a review body over 65,536 characters
RETRY_IN_BODY = (400, 422)  # GitHub could not place an inline comment


RECOVERED = "recovered: an earlier attempt posted this review but did not record it"
ADOPTED = "adopted: a review of this head by this account was already on GitHub; not posted again"
CLAIM_BUSY = "another run holds the post claim for this head and nothing of it is on GitHub yet"
EVENTS = {"COMMENTED": "COMMENT", "CHANGES_REQUESTED": "REQUEST_CHANGES", "APPROVED": "APPROVE"}


class RecordAfterPostError(RuntimeError):
    """The review is on GitHub but its record is not: retry, and the retry records it."""


class PostRefused(GitHubError):
    """GitHub refused the review POST with a status the body retry cannot fix. Nothing landed."""


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
    snapshot: Snapshot | None = None  # the head this run reviewed, for follow-ups
    changed_paths: list[str] = field(default_factory=list)
    head_sources: Mapping[str, str] = field(default_factory=dict)  # the head index's files
    blockers: list[Blocker] = field(default_factory=list)  # as decided before any proof
    recovered_id: str = ""  # an earlier attempt's post of this head, recorded by this run
    recovered_event: str = ""  # the verdict that post really carried
    posted_event: str = ""  # the verdict of a landed review this run did not post itself
    read_back: list[PostedComment] | None = None  # that review's comments, as GitHub has them
    retry_after_s: float = 0.0  # claim-busy: when the other run's claim goes stale
    dry_run: bool = False  # a dry run writes nothing, whatever its outcome


@dataclass
class _Claim:
    keep: bool = False  # a POST was sent, so it may have landed: never free the head again


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
    index = CodeIndex(cfg.languages)
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


def not_reviewed(diff: Diff, head: Snapshot, ignore_paths: list[str]) -> list[tuple[str, str]]:
    """Changed files the review could not see: patch omitted by the host, or unreadable at head."""
    out = dict(diff.skipped)
    for path in diff.changed_paths:
        if path not in out:
            reason = head.skip_reason(path)
            if reason is not None:
                out[path] = reason
    return sorted((p, r) for p, r in out.items() if not matches_any(p, ignore_paths))


def _more(n: int) -> str:
    return f"\n\n{n} more findings not shown." if n else ""


def comments_in_body(review: ReviewPayload, limit: int = MAX_BODY_CHARS) -> ReviewPayload:
    """The same review with its inline comments moved into the body, for a refused anchor.

    The body stays within `limit` characters; comments that do not fit are counted instead."""
    blocks = []
    for c in review.comments:
        deleted = " (deleted line)" if c.side == "LEFT" else ""
        blocks.append(f"\n\n**`{c.path}:{c.line}`{deleted}**\n\n{c.body}")
    title = "\n\nGitHub could not place these comments on their lines:"
    body = review.body[:max(0, limit - len(title) - len(_more(len(blocks))))] + title
    for i, block in enumerate(blocks):
        if len(body) + sum(len(b) for b in blocks[i:]) <= limit:
            body += "".join(blocks[i:])
            break
        if len(body) + len(block) + len(_more(len(blocks) - i - 1)) > limit:
            body += _more(len(blocks) - i)
            break
        body += block
    return ReviewPayload(head_sha=review.head_sha, event=review.event, body=body, comments=[])


def _refused(exc: Exception) -> bool:
    """GitHub answered 4xx (429 included): nothing landed, so this head may be posted again."""
    return isinstance(exc, GitHubError) and exc.status is not None and 400 <= exc.status < 500


def blocking_snapshot(decision: PolicyDecision, policy: PolicyConfig) -> list[Blocker]:
    """The blockers behind a REQUEST_CHANGES verdict, copied before a proof can change them.
    apply_policy requests changes exactly when one of these exists."""
    blocking = Severity.parse(policy.blocking_severity)
    return [Blocker.of(f) for f in decision.post if f.severity >= blocking]


class Reviewer:
    def __init__(self, vcs: VCS, registry: ProviderRegistry, scanners: list[Scanner],
                 store: ReviewStore | None, verify: bool = True,
                 proof_runners: Mapping[str, SandboxRunner] | None = None,
                 stale_claim_s: float = DEFAULT_LEASE_S) -> None:
        self.verify = verify  # False only for the eval baseline that measures the checker
        self.stale_claim_s = stale_claim_s  # a claim with no post id this old was abandoned
        # Only runners the operator hands in can run; a repo's config cannot add one.
        self.proof_runners = proof_runners if proof_runners is not None else \
            {"docker": DockerRunner()}
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
               expected_head: str | None = None,
               should_post: Callable[[], bool] | None = None) -> ReviewResult:
        """`should_post` is asked right before each post; False (lease lost) posts nothing."""
        t = _Timer()
        with t.stage("fetch"):
            pr = self.vcs.get_pull(repo, number)
        result = ReviewResult(pr=pr, outcome="", dry_run=dry_run)
        if not dry_run:
            result.recovered_id, result.recovered_event = self._record_landed(
                repo, number, expected_head or pr.head_sha)
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
            result.snapshot = head = self.vcs.snapshot(repo, pr.head_sha)
            result.changed_paths = list(diff.changed_paths)
            index = build_index(head, diff, cfg)
            result.head_sources = index.sources
            churn = self.vcs.churn(repo, pr.head_sha)
            conv = learn_conventions(index, churn, cfg.auth.guard_functions)
            unseen = not_reviewed(diff, head, cfg.ignore_paths)
            if unseen:
                log.info("files not reviewed", extra={"pr": number, "files": unseen})
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
            checker = ClaimChecker(diff, index, cfg.policy.snap_distance,
                                   base_reader=lambda p: self.vcs.read_file(repo, pr.base_sha, p))
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
        known = self.store.known_state(repo, number) if self.store and not dry_run else {}
        decision = apply_policy(outcome.kept + scanned, cfg.policy, known)
        result.decision = decision
        result.blockers = blocking_snapshot(decision, cfg.policy)
        if cfg.proof.enabled:
            with t.stage("proof"):
                self._prove(decision, cfg, rt, head, index, result)
        result.review = build_review(pr.head_sha, decision, diff, rt.tier, result.reviewers,
                                     not_reviewed=unseen)
        result.rendered = render_dry_run(result.review, decision)

        with t.stage("post"):
            if dry_run:
                result.outcome = "dry-run"
            elif not cfg.posting:
                result.outcome = "shadow"
            elif should_post is not None and not should_post():
                result.outcome = "lease-lost"  # checked again right before each POST
            elif self.store is not None and not self.store.claim_post(repo, number, pr.head_sha):
                self._claim_taken(pr, result, should_post)
            else:
                self._post(pr, result, should_post)
        posted, stored = decision.post, []
        if result.read_back is not None:  # a review this run did not post: trust only GitHub
            on = {(c.path, c.side, c.line) for c in result.read_back}
            posted = [f for f in decision.post if (f.file, f.side, f.line) in on]
            stored = [f for f in decision.post if (f.file, f.side, f.line) not in on]
        done = self._finish(result, t, posted,
                            [(f, r) for f, r in decision.held if r != "duplicate"], stored)
        if done.outcome == "post-uncertain":
            log.warning("post outcome uncertain; comments not linked, verdicts not dismissed",
                        extra={"pr": number, "head": pr.head_sha})
        if done.outcome in ("posted", "recovered") and self.store is not None:
            after_post(self.vcs, self.store, done, cfg.policy)
        return done

    def _record_landed(self, repo: str, number: int, head: str) -> tuple[str, str]:
        """Before any model call: record a post an earlier attempt landed but never recorded,
        with the event and blockers saved when it landed. Returns (id, event), or empty."""
        if self.store is None:
            return "", ""
        landed = self.store.unrecorded_post(repo, number, head)
        if landed is None:
            return "", ""
        post_id, event = landed
        log.warning("recording a review an earlier attempt posted",
                    extra={"pr": number, "head": head, "post_id": post_id})
        try:
            self.store.record_review(ReviewRecord(
                repo=repo, pr=number, head_sha=head, outcome="posted", event=event, posted=True,
                post_id=post_id, error=RECOVERED), [], [])
        except Exception as exc:
            raise RecordAfterPostError(f"review {post_id} posted but not recorded: "
                                       f"{type(exc).__name__}") from exc
        return post_id, event

    def _claim_taken(self, pr: PullRequest, result: ReviewResult,
                     should_post: Callable[[], bool] | None) -> None:
        """Another run holds this head's claim. Recover or adopt a review that landed, take over
        an abandoned claim, or come back once it is stale. Never post twice."""
        assert self.store is not None
        if result.recovered_id:  # this run recorded that post at job start
            result.outcome, result.post_id, result.error = "recovered", result.recovered_id, \
                RECOVERED
            result.posted_event = result.recovered_event
            self._read_back(pr, result)
            return
        held = self.store.post_claim(pr.repo, pr.number, pr.head_sha)
        if held is None and self.store.claim_post(pr.repo, pr.number, pr.head_sha):
            self._post(pr, result, should_post)  # the other run freed it in between
            return
        if held is not None and held[0]:
            result.outcome = "already-posted"
            return
        age = held[1] if held is not None else self.stale_claim_s
        if self.store.take_stale_claim(pr.repo, pr.number, pr.head_sha, self.stale_claim_s):
            log.warning("taking over an abandoned post claim", extra={
                "pr": pr.number, "head": pr.head_sha, "age_s": round(age, 1)})
            self._post(pr, result, should_post)  # the remote check runs before its POST
            return
        landed = self._landed_review(pr)
        if landed is not None:
            self._adopt(pr, result, landed)
            return
        result.outcome, result.error = "claim-busy", CLAIM_BUSY
        result.retry_after_s = max(0.0, self.stale_claim_s - age)

    def _landed_review(self, pr: PullRequest) -> PostedReview | None:
        """This account's review of exactly this head, if one is on GitHub (the source of truth
        for "did it land"). An account the token cannot name matches any author."""
        viewer = self.vcs.viewer_login()
        for r in self.vcs.list_reviews(pr.repo, pr.number):
            if r.commit_id == pr.head_sha and r.body.startswith(SUMMARY_PREFIX) and \
                    (not viewer or r.author == viewer):
                return r
        return None

    def _adopt(self, pr: PullRequest, result: ReviewResult, landed: PostedReview) -> None:
        """Record a landed review instead of posting again: its id on the claim, then a posted
        record whose findings are only those GitHub shows."""
        event = EVENTS.get(landed.state, landed.state)
        log.warning("review already on GitHub; adopting it", extra={
            "pr": pr.number, "head": pr.head_sha, "post_id": landed.id})
        result.outcome, result.post_id, result.error = "posted", landed.id, ADOPTED
        result.posted_event = event
        self._read_back(pr, result)
        # Only blockers GitHub shows a comment for: this run's answer may differ from that post's.
        on = {(c.path, c.side, c.line) for c in result.read_back or []}
        shown = {f.fingerprint for f in (result.decision.post if result.decision else [])
                 if (f.file, f.side, f.line) in on}
        result.blockers = [b for b in result.blockers if b.fingerprint in shown]
        self._mark_posted(pr, landed.id, event, result.blockers)

    def _resolve_uncertain(self, pr: PullRequest, result: ReviewResult) -> None:
        """After a POST that may have landed, ask GitHub once: adopt it if it is there."""
        try:
            landed = self._landed_review(pr)
        except Exception as exc:  # noqa: BLE001 - stays post-uncertain, as before this check
            log.warning("could not check GitHub after an uncertain post",
                        extra={"pr": pr.number, "error": type(exc).__name__})
            return
        if landed is not None:
            self._adopt(pr, result, landed)

    def _read_back(self, pr: PullRequest, result: ReviewResult) -> None:
        """The landed review's comments. On failure none: its findings stay `stored`, unlinked."""
        result.read_back = []
        if not isinstance(self.vcs, ReviewThreads):
            log.warning("VCS cannot read back comments; findings stored", extra={"pr": pr.number})
            return
        try:
            result.read_back = self.vcs.review_comments(pr.repo, pr.number, result.post_id)
        except Exception as exc:  # noqa: BLE001 - nothing is marked posted without proof
            log.error("could not read back the landed review; findings stored, not linked",
                      extra={"pr": pr.number, "post_id": result.post_id,
                             "error": type(exc).__name__})

    def _post(self, pr: PullRequest, result: ReviewResult,
              should_post: Callable[[], bool] | None) -> None:
        """Holds the claim from claim_post: freed unless a POST was sent, whatever is raised."""
        claim = _Claim()
        try:
            self._send(pr, result, should_post, claim)
        finally:
            if not claim.keep and self.store is not None:
                self.store.release_post(pr.repo, pr.number, pr.head_sha)

    def _blocked(self, pr: PullRequest, result: ReviewResult,
                 should_post: Callable[[], bool] | None) -> str:
        """Checked right before every POST: a moved head, a review already on GitHub (adopted)
        or a lost lease posts nothing. The lease is read last, after the network calls."""
        if self.vcs.current_head(pr.repo, pr.number) != pr.head_sha:
            return "head-moved"
        landed = self._landed_review(pr)
        if landed is not None:
            self._adopt(pr, result, landed)
            return "adopted"
        if should_post is not None and not should_post():
            return "lease-lost"
        return ""

    def _send(self, pr: PullRequest, result: ReviewResult,
              should_post: Callable[[], bool] | None, claim: _Claim) -> None:
        """Post once; on a refusal of inline comments (400/422), once more with them in the body.
        Any other refusal raises its GitHubError for the worker to classify."""
        assert result.review is not None
        if result.review.event != "COMMENT" and pr.author and \
                pr.author == self.vcs.viewer_login():
            result.review = own_pr_review(result.review)
        payload = result.review
        status: int | None = None
        for attempt in (1, 2):
            blocked = self._blocked(pr, result, should_post)
            if blocked == "adopted":
                claim.keep = True  # it landed: never free the head again
                return
            if blocked:
                result.outcome, result.error = blocked, ""
                return
            claim.keep = True
            try:
                post_id = self.vcs.post_review(pr, payload)
            except Exception as exc:  # every outcome is classified below
                if not _refused(exc):
                    result.outcome = "post-uncertain"  # it may have landed
                    result.error = str(exc) if isinstance(exc, GitHubError) else \
                        type(exc).__name__
                    self._resolve_uncertain(pr, result)
                    return
                claim.keep = False  # GitHub refused it: nothing landed
                status = exc.status if isinstance(exc, GitHubError) else None
                if status not in RETRY_IN_BODY:
                    raise PostRefused(str(exc), status) from exc
                result.outcome, result.error = "post-rejected", str(exc)
                if attempt == 2 or not payload.comments:
                    return
                log.warning("inline comments refused; retrying with them in the body",
                            extra={"pr": pr.number, "status": status})
                payload = comments_in_body(payload)
                continue
            result.outcome, result.post_id, result.review = "posted", post_id, payload
            result.error = "" if attempt == 1 else (f"inline comments refused (HTTP {status}); "
                                                    "posted them in the review body")
            self._mark_posted(pr, post_id, payload.event, result.blockers)
            return

    def _mark_posted(self, pr: PullRequest, post_id: str, event: str,
                     blockers: list[Blocker]) -> None:
        if self.store is None:
            return
        try:
            self.store.mark_posted(pr.repo, pr.number, pr.head_sha, post_id, event, blockers)
        except Exception as exc:  # noqa: BLE001 - the review record below still carries the id
            log.error("could not record the post id on the claim",
                      extra={"pr": pr.number, "head": pr.head_sha, "post_id": post_id,
                             "error": type(exc).__name__})

    def _prove(self, decision: PolicyDecision, cfg: RepoConfig, rt: Route, head: Snapshot,
               index: CodeIndex, result: ReviewResult) -> None:
        runner = select_runner(cfg.proof, self.proof_runners)
        prover = Prover(self.registry, runner, cfg.proof, rt.model, rt.timeout_s) if runner \
            else None
        try:
            run_proof_stage(prover, decision, cfg, head, index)
        except Exception as exc:  # noqa: BLE001 - a proof failure never fails the review
            log.warning("proof stage failed", extra={"pr": result.pr.number,
                                                     "error": type(exc).__name__})
        if prover is not None:
            result.cost_usd += prover.cost_usd

    def _finish(self, result: ReviewResult, t: _Timer, posted: list[Finding],
                held: list[tuple[Finding, str]], stored: list[Finding] | None = None
                ) -> ReviewResult:
        result.timings = t.timings
        log.info("review finished", extra={"repo": result.pr.repo, "pr": result.pr.number,
                                           "head": result.pr.head_sha, "outcome": result.outcome,
                                           "timings": t.timings, "cost_usd": result.cost_usd})
        if self.store is None or result.dry_run:
            return result
        try:
            self._record(result, t, posted, held, stored or [])
        except Exception as exc:
            if result.outcome == "posted":
                raise RecordAfterPostError(f"review {result.post_id} posted but not recorded: "
                                           f"{type(exc).__name__}") from exc
            raise
        return result

    def _record(self, result: ReviewResult, t: _Timer, posted: list[Finding],
                held: list[tuple[Finding, str]], stored: list[Finding]) -> None:
        assert self.store is not None
        rt = result.route
        self.store.record_review(
            ReviewRecord(repo=result.pr.repo, pr=result.pr.number,
                         head_sha=result.pr.head_sha, outcome=result.outcome,
                         tier=rt.tier if rt else "", model=rt.model if rt else "",
                         providers=tuple(result.reviewers),
                         event=result.posted_event or (result.review.event if result.review
                                                       else ""),
                         # An uncertain post may have landed: never risk posting it twice.
                         posted=result.outcome in ("posted", "post-uncertain", "recovered"),
                         post_id=result.post_id, cost_usd=result.cost_usd,
                         timings=t.timings, error=result.error),
            posted, held, result.blockers, stored)
