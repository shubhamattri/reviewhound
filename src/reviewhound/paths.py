"""Glob matching with `**` for repo-relative paths (one implementation for every caller)."""

from __future__ import annotations

import re
from functools import lru_cache


@lru_cache(maxsize=512)
def _compile(pattern: str) -> re.Pattern[str]:
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def path_matches(path: str, pattern: str) -> bool:
    return _compile(pattern).match(path) is not None


def matches_any(path: str, patterns: list[str]) -> bool:
    return any(path_matches(path, p) for p in patterns)


_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "python": (".py",),
    "typescript": (".ts", ".tsx", ".mts", ".cts"),
    "javascript": (".js", ".jsx", ".mjs", ".cjs"),
    "go": (".go",),
}
LANGUAGES: tuple[str, ...] = tuple(_EXTENSIONS)


def language_of(path: str) -> str | None:
    for lang, exts in _EXTENSIONS.items():
        if path.endswith(exts):
            return lang
    return None
