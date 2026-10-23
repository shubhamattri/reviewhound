"""Eval harness: replay recorded PRs through the full pipeline and score the findings.

A reported finding matches an expected one when it is in the same file, on the same side
(RIGHT: head line, LEFT: base line the PR deleted) and within `line_tolerance` lines.
Each expected finding can be matched once.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from reviewhound.config import CONFIG_FILENAME, load_config
from reviewhound.pipeline import Reviewer
from reviewhound.proof import DockerRunner, LocalRunner, SandboxRunner
from reviewhound.providers import FakeProvider, Provider, ProviderRegistry
from reviewhound.scanners import BuiltinScanner
from reviewhound.vcs.base import PullRequest
from reviewhound.vcs.memory import MemoryVCS


@dataclass
class CaseResult:
    name: str
    expected: int
    reported: int
    true_positives: int
    dropped_by_checker: int
    cost_usd: float
    misses: list[str] = field(default_factory=list)
    false_alarms: list[str] = field(default_factory=list)
    error: str = ""
    proof_mismatches: list[str] = field(default_factory=list)
    proof_skipped: list[str] = field(default_factory=list)


@dataclass
class EvalReport:
    cases: list[CaseResult]

    @property
    def precision(self) -> float:
        reported = sum(c.reported for c in self.cases)
        return 1.0 if reported == 0 else sum(c.true_positives for c in self.cases) / reported

    @property
    def recall(self) -> float:
        expected = sum(c.expected for c in self.cases)
        return 1.0 if expected == 0 else sum(c.true_positives for c in self.cases) / expected

    @property
    def cost_per_pr(self) -> float:
        return sum(c.cost_usd for c in self.cases) / max(1, len(self.cases))

    def render(self) -> str:
        rows = [f"{'case':<38} {'exp':>3} {'rep':>3} {'tp':>3} {'drop':>4}  notes"]
        for c in self.cases:
            notes = "; ".join([*(f"miss {m}" for m in c.misses),
                               *(f"false alarm {f}" for f in c.false_alarms),
                               *([f"error {c.error}"] if c.error else []),
                               *c.proof_mismatches, *c.proof_skipped])
            rows.append(f"{c.name:<38} {c.expected:>3} {c.reported:>3} {c.true_positives:>3} "
                        f"{c.dropped_by_checker:>4}  {notes}")
        dropped = sum(c.dropped_by_checker for c in self.cases)
        rows.append("")
        rows.append(f"cases: {len(self.cases)}  precision: {self.precision:.2f}  "
                    f"recall: {self.recall:.2f}  dropped by claim checker: {dropped}  "
                    f"cost per PR: ${self.cost_per_pr:.4f}")
        return "\n".join(rows)


def _read_tree(root: Path) -> dict[str, str]:
    if not root.exists():
        return {}
    return {p.relative_to(root).as_posix(): p.read_text() for p in sorted(root.rglob("*"))
            if p.is_file()}


def _label(path: str, line: int, side: str) -> str:
    return f"{path}:{line}" if side == "RIGHT" else f"{path}:{line}:{side}"


def score(expected: list[dict[str, Any]], reported: Sequence[tuple[str, int, str]],
          line_tolerance: int = 3) -> tuple[int, list[str], list[str]]:
    """Return (true positives, missed expected, false alarms)."""
    unmatched = list(expected)
    tp = 0
    false_alarms: list[str] = []
    for path, line, side in reported:
        hit = next((e for e in unmatched if e["file"] == path and any(
                    pos.get("side", "RIGHT") == side and
                    abs(int(pos["line"]) - line) <= line_tolerance
                    for pos in [e, *e.get("also", [])])), None)
        if hit is None:
            false_alarms.append(_label(path, line, side))
        else:
            unmatched.remove(hit)
            tp += 1
    return tp, [_label(e["file"], int(e["line"]), e.get("side", "RIGHT"))
                for e in unmatched], false_alarms


def default_runners(live: bool) -> dict[str, SandboxRunner]:
    # Live proofs run tests the model wrote, so they only ever get the container.
    if live:
        return {"docker": DockerRunner()}
    # Recorded cases and their recorded proofs are our own code, so they may run locally.
    return {"local": LocalRunner(), "docker": DockerRunner()}


def sandbox_missing(base: Mapping[str, str], runners: Mapping[str, SandboxRunner]) -> bool:
    """True when the runner the case's config asks for is absent or cannot start."""
    runner = runners.get(load_config(base.get(CONFIG_FILENAME)).proof.runner)
    if runner is None:
        return True
    return isinstance(runner, DockerRunner) and bool(runner.unavailable())


def run_case(case_dir: Path, provider_factory: Callable[[dict[str, Any]], Provider],
             verify: bool = True, live: bool = False,
             runners: Mapping[str, SandboxRunner] | None = None) -> CaseResult:
    spec = json.loads((case_dir / "case.json").read_text())
    base, head = _read_tree(case_dir / "base"), _read_tree(case_dir / "head")
    pr = PullRequest(repo=f"eval/{case_dir.name}", number=1, title=spec.get("title", ""),
                     body=spec.get("body", ""), base_sha="base", head_sha="head")
    vcs = MemoryVCS(pr, base, head)
    registry = ProviderRegistry([provider_factory(spec)])
    runners = default_runners(live) if runners is None else runners
    reviewer = Reviewer(vcs, registry, [BuiltinScanner()], store=None, verify=verify,
                        proof_runners=runners)
    try:
        result = reviewer.review(pr.repo, 1, dry_run=True)
    except Exception as exc:  # noqa: BLE001 - one broken case must not stop the run
        return CaseResult(case_dir.name, len(spec["expected"]), 0, 0, 0, 0.0,
                          misses=[_label(e["file"], int(e["line"]), e.get("side", "RIGHT"))
                                  for e in spec["expected"]],
                          error=type(exc).__name__)
    posted = [(f.file, f.line, f.side)
              for f in (result.decision.post if result.decision else [])]
    tp, misses, false_alarms = score(spec["expected"], posted)
    seen = [*result.decision.post, *(f for f, _ in result.decision.held)] \
        if result.decision else []
    proofs = {(f.file, f.line): f.proof for f in seen}
    expected_proofs = spec.get("expected_proofs", [])
    if live and expected_proofs and sandbox_missing(base, runners):
        # A live model cannot be judged on proofs that had nowhere to run.
        skipped = [f"proof {e['file']}:{e['line']} skipped (no sandbox)" for e in expected_proofs]
        return CaseResult(case_dir.name, len(spec["expected"]), len(posted), tp,
                          len(result.dropped), result.cost_usd, misses, false_alarms,
                          proof_skipped=skipped)
    mismatches = [f"proof {e['file']}:{e['line']} expected {e['proof']} got "
                  f"{proofs.get((e['file'], int(e['line'])))}"
                  for e in expected_proofs
                  if proofs.get((e["file"], int(e["line"]))) != e["proof"]]
    return CaseResult(case_dir.name, len(spec["expected"]), len(posted), tp,
                      len(result.dropped), result.cost_usd, misses, false_alarms,
                      proof_mismatches=mismatches)


def recorded_provider(spec: dict[str, Any]) -> Provider:
    return FakeProvider([json.dumps(spec["recorded_answer"]),
                         *(json.dumps(a) for a in spec.get("recorded_proofs", []))])


def run_eval(cases_root: Path, provider_factory: Callable[[dict[str, Any]], Provider]
             = recorded_provider, verify: bool = True, live: bool = False,
             runners: Mapping[str, SandboxRunner] | None = None) -> EvalReport:
    cases = sorted(p for p in cases_root.iterdir() if (p / "case.json").exists())
    return EvalReport([run_case(c, provider_factory, verify, live, runners) for c in cases])
