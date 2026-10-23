"""Policy as data: which verified findings get posted, and what the review verdict is."""

from __future__ import annotations

from dataclasses import dataclass, field

from reviewhound.config import PolicyConfig, Severity
from reviewhound.findings import Finding


@dataclass
class PolicyDecision:
    post: list[Finding] = field(default_factory=list)
    held: list[tuple[Finding, str]] = field(default_factory=list)
    event: str = "COMMENT"


def apply_policy(findings: list[Finding], policy: PolicyConfig,
                 known_state: dict[str, str] | None = None) -> PolicyDecision:
    """`known_state` maps fingerprint -> thread state from earlier reviews of this PR."""
    known = known_state or {}
    decision = PolicyDecision()
    floor = Severity.parse(policy.min_severity_to_post)
    blocking = Severity.parse(policy.blocking_severity)

    seen: set[str] = set()
    seen_lines: set[tuple[str, int, str]] = set()
    candidates: list[Finding] = []
    for f in sorted(findings, key=lambda x: (-x.severity, not x.verified, x.file, x.line)):
        line_key = (f.file, f.line, f.category)
        if f.fingerprint in seen or line_key in seen_lines:
            decision.held.append((f, "duplicate"))
            continue
        seen.add(f.fingerprint)
        seen_lines.add(line_key)
        state = known.get(f.fingerprint)
        if state in ("posted", "reopened"):
            decision.held.append((f, "already posted on an earlier head"))
        elif state in ("declined", "resolved"):
            decision.held.append((f, f"developer {state} this earlier"))
        elif f.severity < floor:
            decision.held.append((f, f"below {policy.min_severity_to_post}"))
        elif policy.drop_unverified and not f.verified:
            decision.held.append((f, "unverified"))
        else:
            candidates.append(f)

    decision.post = candidates[:policy.max_comments]
    for f in candidates[policy.max_comments:]:
        decision.held.append((f, f"over the {policy.max_comments}-comment limit"))
    if any(f.severity >= blocking for f in decision.post):
        decision.event = "REQUEST_CHANGES"
    return decision
