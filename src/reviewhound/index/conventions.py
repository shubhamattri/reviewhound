"""Learn a repo's habits from its code so a new team gets value with zero config."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from reviewhound.index.treesitter import CodeIndex

_AUTH_NAME = re.compile(
    r"(auth|login_required|permission|authori[sz]e|^require_|^ensure_|has_role|admin|can_access|"
    r"check_access|guard)", re.IGNORECASE)
_LOG_CALL = re.compile(r"\b(logger|log|logging|console)\.(debug|info|warn|warning|error|exception)\b")
_RAISE_TYPE = re.compile(r"\b(?:raise|throw new)\s+([A-Z][A-Za-z0-9_]*)")


@dataclass
class Conventions:
    auth_helpers: list[str] = field(default_factory=list)
    log_styles: list[str] = field(default_factory=list)
    error_types: list[str] = field(default_factory=list)
    hot_folders: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = []
        if self.auth_helpers:
            parts.append("auth helpers: " + ", ".join(self.auth_helpers))
        if self.log_styles:
            parts.append("logging: " + ", ".join(self.log_styles))
        if self.error_types:
            parts.append("error types: " + ", ".join(self.error_types))
        if self.hot_folders:
            parts.append("high-churn folders: " + ", ".join(self.hot_folders))
        return "\n".join(parts)


def learn_conventions(index: CodeIndex, churn: Counter[str] | None = None,
                      configured_guards: list[str] | None = None,
                      top: int = 5) -> Conventions:
    """`churn` maps path -> number of recent commits touching it (optional)."""
    defined_guards = Counter[str]()
    for d in index.iter_definitions():
        if d.kind == "function" and _AUTH_NAME.search(d.name):
            defined_guards[d.name] = len(index.callers_of(d.name))
    auth = [n for n, uses in defined_guards.most_common() if uses > 0][:top]
    for g in configured_guards or []:
        if g not in auth:
            auth.insert(0, g)

    log_styles = Counter[str]()
    errors = Counter[str]()
    for src in index.sources.values():
        for m in _LOG_CALL.finditer(src):
            log_styles[f"{m.group(1)}.{m.group(2)}"] += 1
        for m in _RAISE_TYPE.finditer(src):
            errors[m.group(1)] += 1

    hot: list[str] = []
    if churn:
        folders = Counter[str]()
        for path, n in churn.items():
            folders[path.rsplit("/", 1)[0] if "/" in path else "."] += n
        hot = [f for f, _ in folders.most_common(top)]
    return Conventions(auth_helpers=auth,
                       log_styles=[s for s, _ in log_styles.most_common(top)],
                       error_types=[e for e, _ in errors.most_common(top)],
                       hot_folders=hot)
