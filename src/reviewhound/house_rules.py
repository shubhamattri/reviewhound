"""The reviewed repo's own rules files (CLAUDE.md, AGENTS.md): at the root and in changed folders."""

from __future__ import annotations

from collections.abc import Callable, Iterable

RULES_FILENAMES = ("CLAUDE.md", "AGENTS.md")
MAX_RULES_FILES = 8
MAX_FOLDER_DEPTH = 10  # rules deeper than this in a changed file's path are not looked for
MAX_RULES_FOLDERS = 200  # folders looked in per review, root included


def rules_file_candidates(changed_paths: Iterable[str]) -> list[str]:
    """Root first, then each folder holding a changed file, shallowest first, down to
    MAX_FOLDER_DEPTH levels and MAX_RULES_FOLDERS folders in all."""
    folders = {""}
    for path in changed_paths:
        parts = path.split("/", MAX_FOLDER_DEPTH)[:-1]
        folders.update("/".join(parts[:i]) for i in range(1, len(parts) + 1))
    ordered = sorted(folders, key=lambda f: (f.count("/") + bool(f), f))[:MAX_RULES_FOLDERS]
    return [f"{f}/{name}" if f else name for f in ordered for name in RULES_FILENAMES]


def read_rules_files(changed_paths: Iterable[str], exists: Callable[[str], bool],
                     read: Callable[[str], str | None]) -> list[tuple[str, str]]:
    """(path, text) for each rules file found, identical texts kept once."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for path in rules_file_candidates(changed_paths):
        if len(out) >= MAX_RULES_FILES or not exists(path):
            continue
        text = read(path)
        if text is None or not text.strip() or text.strip() in seen:
            continue
        seen.add(text.strip())
        out.append((path, text))
    return out
