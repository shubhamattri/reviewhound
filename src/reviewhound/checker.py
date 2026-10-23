"""The claim checker: every finding is a set of facts, and each fact is checked against code.

A false fact drops the finding. A finding with no evidence is downgraded. There are no
wording-specific rules here: only general checks against the index and the PR head.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from reviewhound.config import Severity
from reviewhound.diff import Diff, FileDiff
from reviewhound.findings import Claim, ClaimResult, Finding, ModelFinding
from reviewhound.index.treesitter import CodeIndex

log = logging.getLogger(__name__)
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


class _BaseReadError(Exception):
    pass


class ClaimChecker:
    def __init__(self, diff: Diff, index: CodeIndex, snap_distance: int = 3,
                 base_reader: Callable[[str], str | None] | None = None) -> None:
        self.diff = diff
        self.index = index
        self.snap_distance = snap_distance
        self.base_reader = base_reader  # reads a file at the PR base; None disables LEFT findings
        self._base: dict[str, str | None | _BaseReadError] = {}

    def _base_source(self, path: str) -> str | None:
        """Base text of `path`, read at most once. Raises _BaseReadError when the read failed."""
        if self.base_reader is None:
            return None
        if path not in self._base:
            try:
                self._base[path] = self.base_reader(path)
            except Exception as exc:  # noqa: BLE001 - any read failure only costs this finding
                log.warning("base file read failed", extra={"path": path,
                                                            "error": type(exc).__name__})
                self._base[path] = _BaseReadError(type(exc).__name__)
        got = self._base[path]
        if isinstance(got, _BaseReadError):
            raise got
        return got

    def _locate_deleted(self, f: Finding, fd: FileDiff) -> tuple[int | None, str]:
        """Find the quote on base lines this PR deleted: (base line, note) or (None, reason)."""
        nowhere = "evidence quote does not exist at head or on deleted lines"
        try:
            base = self._base_source(fd.old_path or f.file)
        except _BaseReadError as exc:
            return None, f"base version of {f.file} could not be read ({exc})"
        spans = find_quote_spans(base, f.evidence_quote) if base is not None else []
        if not spans:
            return None, nowhere
        # The base must agree with the patch, or its line numbers mean nothing here.
        lines = base.splitlines() if base is not None else []
        deleted = {n for n, t in fd.deleted_text.items()
                   if n <= len(lines) and _norm(lines[n - 1]) == _norm(t)}
        for start, end in sorted(spans, key=lambda q: abs(q[0] - f.line)):
            hit = [n for n in range(start, end + 1) if n in deleted and lines[n - 1].strip()]
            if hit:
                best = min(hit, key=lambda n: abs(n - f.line))
                return best, f"on deleted base line {best}"
        if len(deleted) < len(fd.deleted_text):
            return None, f"base version of {f.file} does not match the diff"
        return None, "evidence quote is only in code this PR did not change"

    def _uncovered(self, claim: Claim, path: str) -> ClaimResult:
        return ClaimResult(claim, "unknown", f"{path} is not in a language the index covers")

    def _dotted(self, claim: Claim, dotted: str, missing: str) -> ClaimResult | None:
        """Verdict for `Owner.member`, or None when a definition qualified that way exists."""
        if self.index.qualified_matches(dotted):
            return None
        owner, member = dotted.rsplit(".", 1)
        owners = self.index.qualified_matches(owner)
        if not owners:
            return ClaimResult(claim, "unknown", f"{owner} is not defined in indexed code")
        word = re.compile(rf"\b{re.escape(member)}\b")
        for d in owners:
            body = self.index.sources.get(d.file, "").splitlines()[d.start_line - 1:d.end_line]
            if any(word.search(ln) for ln in body):
                # Fields and attributes are not indexed as definitions, so this cannot be judged.
                return ClaimResult(claim, "unknown", f"{member} is named inside {owner}")
        return ClaimResult(claim, "false", missing)

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
            name = claim.name.strip(".")
            if "." in name:
                owner, member = name.rsplit(".", 1)
                miss = self._dotted(claim, f"{owner}.{member}", f"{owner} has no member {member}")
                return miss or ClaimResult(claim, "true", "qualified definition found")
            if self.index.has_symbol(name):
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

    def _locate(self, finding: Finding, quote_spans: list[tuple[int, int]],
                src: str) -> tuple[int | None, str]:
        """Pick the changed line the finding belongs to, or explain why there is none."""
        added = self.diff.files[finding.file].added_lines
        if quote_spans:
            # Evidence must sit on a changed line of the quote itself, never on a blank one.
            lines = src.splitlines()
            for start, end in sorted(quote_spans, key=lambda q: abs(q[0] - finding.line)):
                changed = [n for n in range(start, end + 1) if n in added and lines[n - 1].strip()]
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
        if fd is None:
            return f, f"{f.file} is not a changed file in this PR"
        if not is_meaningful(f.evidence_quote):
            f.evidence_quote = ""
        src = self.index.sources.get(f.file) if fd.status != "deleted" else ""
        if src is None:
            return f, f"{f.file} could not be read at head"
        if fd.status == "deleted" and not f.evidence_quote:
            return f, f"{f.file} is deleted by this PR and the finding quotes nothing from it"

        quote_spans: list[tuple[int, int]] = []
        if f.evidence_quote:
            quote_spans = find_quote_spans(src, f.evidence_quote)
        line: int | None = None
        how = ""
        has_evidence = bool(quote_spans)
        if quote_spans or not f.evidence_quote:
            line, how = self._locate(f, quote_spans, src)
        if line is None and f.evidence_quote:
            # A removed line often still exists elsewhere at head, so try the deletions too.
            left, left_how = self._locate_deleted(f, fd)
            if left is not None:
                line, how, has_evidence = left, left_how, True
                f.side = "LEFT"
            elif not quote_spans:
                how = left_how
        if line is None:
            return f, how
        if line != f.line or f.side == "LEFT":
            f.notes.append(how)
            f.line = line

        for claim in mf.claims:
            r = self.check_claim(claim, f.file)
            f.claim_results.append(r)
            if r.verdict == "false":
                return f, f"false claim: {r.reason}"

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
