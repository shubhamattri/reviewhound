"""The claim checker: every finding is a set of facts, and each fact is checked against code.

A false fact drops the finding. A finding with no evidence is downgraded. There are no
wording-specific rules here: only general checks against the index and the PR head.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from reviewhound.config import Severity
from reviewhound.diff import Diff
from reviewhound.findings import Claim, ClaimResult, Finding, ModelFinding
from reviewhound.index.treesitter import CodeIndex

MIN_EVIDENCE_CHARS = 8  # a quote like "x" or "}" matches anywhere and proves nothing


def is_meaningful(quote: str) -> bool:
    return len("".join(quote.split())) >= MIN_EVIDENCE_CHARS


def _norm(line: str) -> str:
    return " ".join(line.split())


def find_quote_spans(source: str, quote: str) -> list[tuple[int, int]]:
    """1-based (first, last) file lines of each place `quote` appears.

    Lines compare with whitespace collapsed; blank lines are skipped on both sides.
    """
    want = [_norm(q) for q in quote.strip().splitlines() if q.strip()]
    if not want:
        return []
    have = [(n, _norm(ln)) for n, ln in enumerate(source.splitlines(), start=1) if ln.strip()]
    hits: list[tuple[int, int]] = []
    for i in range(len(have) - len(want) + 1):
        window = have[i:i + len(want)]
        if all(w in h if j in (0, len(want) - 1) else w == h
               for j, (w, (_, h)) in enumerate(zip(want, window))):
            hits.append((window[0][0], window[-1][0]))
    return hits


def find_quote(source: str, quote: str) -> list[int]:
    """1-based start lines where `quote` appears."""
    return [start for start, _ in find_quote_spans(source, quote)]


@dataclass
class CheckOutcome:
    kept: list[Finding] = field(default_factory=list)
    dropped: list[tuple[Finding, str]] = field(default_factory=list)


class ClaimChecker:
    def __init__(self, diff: Diff, index: CodeIndex, snap_distance: int = 3) -> None:
        self.diff = diff
        self.index = index
        self.snap_distance = snap_distance

    def _uncovered(self, claim: Claim, path: str) -> ClaimResult:
        return ClaimResult(claim, "unknown", f"{path} is not in a language the index covers")

    def _dotted(self, claim: Claim, dotted: str, missing: str) -> ClaimResult | None:
        """Verdict for `Owner.member`, or None when a definition qualified that way exists."""
        if self.index.qualified_matches(dotted):
            return None
        owner = dotted.rsplit(".", 1)[0]
        if self.index.qualified_matches(owner):
            return ClaimResult(claim, "false", missing)
        return ClaimResult(claim, "unknown", f"{owner} is not defined in indexed code")

    def check_claim(self, claim: Claim, default_file: str) -> ClaimResult:
        if claim.kind == "quote":
            path = claim.file or default_file
            src = self.index.sources.get(path)
            if src is None:
                if not self.index.covers(path):
                    return self._uncovered(claim, path)
                return ClaimResult(claim, "false", f"file {path} not found at head")
            if not claim.text or not find_quote(src, claim.text):
                return ClaimResult(claim, "false", f"quoted text not found in {path}")
            return ClaimResult(claim, "true", "quote found")
        context_file = claim.file or default_file
        if claim.kind == "symbol_exists":
            if not claim.name:
                return ClaimResult(claim, "unknown", "no symbol name given")
            if "." in claim.name.strip("."):
                owner, member = claim.name.strip(".").rsplit(".", 1)
                miss = self._dotted(claim, f"{owner}.{member}", f"{owner} has no member {member}")
                return miss or ClaimResult(claim, "true", "qualified definition found")
            if self.index.has_symbol(claim.name):
                return ClaimResult(claim, "true", "symbol found")
            if not self.index.covers(context_file):
                return self._uncovered(claim, context_file)
            return ClaimResult(claim, "false", f"symbol {claim.name} does not exist")
        if claim.kind in ("calls", "not_calls"):
            if not claim.caller or not claim.callee:
                return ClaimResult(claim, "unknown", "caller or callee missing")
            caller = claim.caller.strip(".")
            if "." in caller:
                miss = self._dotted(claim, caller, f"function {claim.caller} does not exist")
                if miss is not None:
                    return miss
            elif not self.index.is_defined(caller):
                if not self.index.covers(context_file):
                    return self._uncovered(claim, context_file)
                return ClaimResult(claim, "false", f"function {claim.caller} does not exist")
            callees = self.index.calls_of(caller)
            if callees is None:
                return ClaimResult(claim, "unknown", f"{claim.caller} is ambiguous")
            callee = claim.callee.rsplit(".", 1)[-1]
            present = callee in callees
            if claim.kind == "calls":
                return ClaimResult(claim, "true" if present else "false",
                                   f"{claim.caller} {'calls' if present else 'never calls'} {callee}")
            return ClaimResult(claim, "false" if present else "true",
                               f"{claim.caller} {'does call' if present else 'never calls'} {callee}")
        return ClaimResult(claim, "unknown", f"unsupported claim kind {claim.kind}")

    def _locate(self, finding: Finding,
                quote_spans: list[tuple[int, int]]) -> tuple[int | None, str]:
        """Pick the changed line the finding belongs to, or explain why there is none."""
        added = self.diff.files[finding.file].added_lines
        if quote_spans:
            # Evidence must sit on changed code; the occurrence nearest the cited line wins.
            for start, end in sorted(quote_spans, key=lambda q: abs(q[0] - finding.line)):
                changed = [n for n in range(start, end + 1) if n in added]
                if changed:
                    best = min(changed, key=lambda n: abs(n - finding.line))
                    return best, f"moved to quoted line {best}"
            return None, "evidence quote is only in code this PR did not change"
        if finding.line in added:
            return finding.line, "line is changed by the PR"
        snapped = self.diff.files[finding.file].nearest_added(finding.line, self.snap_distance)
        if snapped is not None:
            return snapped, f"snapped to changed line {snapped}"
        return None, f"line {finding.line} is not changed by this PR"

    def check(self, mf: ModelFinding, source: str) -> tuple[Finding, str | None]:
        """Return the finding and a drop reason (None when it survives)."""
        f = Finding.from_model(mf, source)
        fd = self.diff.files.get(f.file)
        if fd is None or fd.status == "deleted":
            return f, f"{f.file} is not a changed file in this PR"
        src = self.index.sources.get(f.file)
        if src is None:
            return f, f"{f.file} could not be read at head"

        quote_spans: list[tuple[int, int]] = []
        if not is_meaningful(f.evidence_quote):
            f.evidence_quote = ""
        if f.evidence_quote:
            quote_spans = find_quote_spans(src, f.evidence_quote)
            if not quote_spans:
                return f, "evidence quote does not exist at head"

        line, how = self._locate(f, quote_spans)
        if line is None:
            return f, how
        if line != f.line:
            f.notes.append(how)
            f.line = line

        for claim in mf.claims:
            r = self.check_claim(claim, f.file)
            f.claim_results.append(r)
            if r.verdict == "false":
                return f, f"false claim: {r.reason}"

        has_evidence = bool(quote_spans)
        all_true = all(r.verdict == "true" for r in f.claim_results)
        f.verified = has_evidence and all_true
        if not has_evidence:
            f.severity = Severity(max(Severity.NIT, f.severity - 1))
            f.notes.append("no evidence quote: severity lowered one step")
        return f, None

    def check_all(self, findings: list[ModelFinding], source: str) -> CheckOutcome:
        out = CheckOutcome()
        for mf in findings:
            f, reason = self.check(mf, source)
            if reason is None:
                out.kept.append(f)
            else:
                out.dropped.append((f, reason))
        return out
