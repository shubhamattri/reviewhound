"""The structured contract between the model and the verifier."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from reviewhound.config import Severity

ClaimKind = Literal["quote", "symbol_exists", "calls", "not_calls"]


class Claim(BaseModel):
    """One checkable statement of fact about the code at the PR head."""

    model_config = ConfigDict(extra="ignore")
    kind: ClaimKind
    file: str | None = None
    text: str | None = None
    name: str | None = None
    caller: str | None = None
    callee: str | None = None


class ModelFinding(BaseModel):
    model_config = ConfigDict(extra="ignore")
    file: str
    line: int = Field(ge=1)
    severity: Literal["nit", "minor", "major", "blocker"]
    category: str = "correctness"
    title: str
    body: str
    evidence_quote: str = ""
    reachability: str = ""
    ruled_out: str = ""
    claims: list[Claim] = Field(default_factory=list)


class ModelReview(BaseModel):
    model_config = ConfigDict(extra="ignore")
    findings: list[ModelFinding] = Field(default_factory=list)


class FindingsParseError(ValueError):
    """The model answer was not JSON matching the schema."""


def _extract_json(text: str) -> Any:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        stripped = stripped.rsplit("```", 1)[0]
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        start, end = stripped.find("{"), stripped.rfind("}")
        if start == -1 or end <= start:
            raise
        return json.loads(stripped[start:end + 1])


def parse_model_review(text: str) -> ModelReview:
    try:
        return ModelReview.model_validate(_extract_json(text))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise FindingsParseError(f"model output does not match schema: {type(exc).__name__}") \
            from exc


def findings_json_schema() -> dict[str, Any]:
    return ModelReview.model_json_schema()


@dataclass
class ClaimResult:
    claim: Claim
    verdict: Literal["true", "false", "unknown"]
    reason: str


@dataclass
class Finding:
    """A finding after verification, ready for policy."""

    file: str
    line: int
    severity: Severity
    category: str
    title: str
    body: str
    evidence_quote: str
    source: str  # model:<provider> or scanner:<name>
    verified: bool = False
    claim_results: list[ClaimResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    identity: str = ""  # scanner-set short hash that tells apart same-looking findings
    side: Literal["LEFT", "RIGHT"] = "RIGHT"  # LEFT: `line` is a base line this PR deleted
    proof: Literal["proven", "not_proven", "inconclusive"] | None = None
    proof_test: str = ""  # the generated test code, when the model produced one

    @property
    def fingerprint(self) -> str:
        """Stable identity across re-reviews: survives line shifts, not content changes."""
        anchor = self.identity or " ".join(self.evidence_quote.split()) or self.title.lower()
        raw = f"{self.file}\x00{self.category}\x00{anchor}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    @classmethod
    def from_model(cls, mf: ModelFinding, source: str) -> Finding:
        return cls(file=mf.file, line=mf.line, severity=Severity.parse(mf.severity),
                   category=mf.category, title=mf.title, body=mf.body,
                   evidence_quote=mf.evidence_quote, source=source)
