"""Risk router: size and touched paths pick the review tier, model and budget."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from reviewhound.config import RepoConfig
from reviewhound.diff import Diff
from reviewhound.paths import language_of, matches_any, path_matches

Tier = Literal["skip", "light", "standard", "deep"]
_ORDER: dict[Tier, int] = {"skip": 0, "light": 1, "standard": 2, "deep": 3}


@dataclass(frozen=True)
class Route:
    tier: Tier
    model: str
    context_bytes: int
    timeout_s: float
    reasons: tuple[str, ...]


_MODELS: dict[Tier, str] = {"skip": "", "light": "haiku", "standard": "sonnet", "deep": "opus"}
_TIMEOUTS: dict[Tier, float] = {"skip": 0, "light": 180, "standard": 420, "deep": 900}


def route(diff: Diff, cfg: RepoConfig) -> Route:
    reasons: list[str] = []
    reviewable = [p for p in diff.changed_paths if not matches_any(p, cfg.ignore_paths)]
    if not reviewable:
        return Route("skip", "", 0, 0, ("no reviewable files",))

    tier: Tier = "light"
    for path in reviewable:
        path_tier: Tier = "standard" if language_of(path) else "light"
        for rule in cfg.risk_paths:
            if path_matches(path, rule.pattern):
                path_tier = rule.tier
                if rule.tier != "light":
                    reasons.append(f"{path} matches risky path {rule.pattern}")
                break
        if _ORDER[path_tier] > _ORDER[tier]:
            tier = path_tier

    changed = diff.added_line_count() + diff.removed_line_count()
    if changed >= cfg.budget.large_pr_lines and tier == "standard":
        tier = "deep"
        reasons.append(f"{changed} changed lines >= {cfg.budget.large_pr_lines}")
    if tier == "light":
        reasons.append("only docs/config or non-code files")

    budget = {"light": cfg.budget.context_bytes_light,
              "standard": cfg.budget.context_bytes_standard,
              "deep": cfg.budget.context_bytes_deep}[tier]
    return Route(tier, _MODELS[tier], budget, _TIMEOUTS[tier], tuple(reasons))
