"""Unified diff parsing: which head-side lines a pull request added or touched."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class Hunk:
    new_start: int
    new_count: int
    added: list[int] = field(default_factory=list)
    context: list[int] = field(default_factory=list)


@dataclass
class FileDiff:
    path: str
    old_path: str | None
    status: str  # added | modified | deleted | renamed
    hunks: list[Hunk] = field(default_factory=list)
    patch: str = ""

    @property
    def added_lines(self) -> set[int]:
        return {n for h in self.hunks for n in h.added}

    @property
    def commentable_lines(self) -> set[int]:
        """Head-side lines GitHub accepts a review comment on: added plus context in hunks."""
        return {n for h in self.hunks for n in (*h.added, *h.context)}

    def nearest_added(self, line: int, max_distance: int) -> int | None:
        best: int | None = None
        for n in self.added_lines:
            d = abs(n - line)
            if d <= max_distance and (best is None or d < abs(best - line) or
                                      (d == abs(best - line) and n < best)):
                best = n
        return best


@dataclass
class Diff:
    files: dict[str, FileDiff] = field(default_factory=dict)

    @property
    def changed_paths(self) -> list[str]:
        return [p for p, f in self.files.items() if f.status != "deleted"]

    def added_line_count(self) -> int:
        return sum(len(f.added_lines) for f in self.files.values())

    def removed_line_count(self) -> int:
        removed = 0
        for f in self.files.values():
            removed += sum(
                1 for ln in f.patch.splitlines() if ln.startswith("-") and not ln.startswith("---")
            )
        return removed


def _strip_prefix(path: str) -> str:
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def parse_patch(path: str, patch: str, status: str = "modified",
                old_path: str | None = None) -> FileDiff:
    """Parse one file's hunks (the `patch` field GitHub returns per file)."""
    fd = FileDiff(path=path, old_path=old_path, status=status, patch=patch)
    hunk: Hunk | None = None
    new_line = 0
    for raw in patch.splitlines():
        m = _HUNK_RE.match(raw)
        if m:
            new_start = int(m.group(3))
            new_count = int(m.group(4)) if m.group(4) is not None else 1
            hunk = Hunk(new_start=new_start, new_count=new_count)
            fd.hunks.append(hunk)
            new_line = new_start
            continue
        if hunk is None or raw.startswith("\\"):
            continue
        if raw.startswith("+"):
            hunk.added.append(new_line)
            new_line += 1
        elif raw.startswith("-"):
            continue
        else:
            hunk.context.append(new_line)
            new_line += 1
    return fd


def parse_unified_diff(text: str) -> Diff:
    """Parse `git diff` output for a whole change set."""
    diff = Diff()
    blocks = re.split(r"^(?=diff --git )", text, flags=re.MULTILINE)
    for block in blocks:
        if not block.startswith("diff --git "):
            continue
        lines = block.splitlines()
        old_path: str | None = None
        new_path: str | None = None
        status = "modified"
        body_start = len(lines)
        for i, ln in enumerate(lines[1:], start=1):
            if ln.startswith("new file mode"):
                status = "added"
            elif ln.startswith("deleted file mode"):
                status = "deleted"
            elif ln.startswith("rename from "):
                old_path = ln[len("rename from "):]
                status = "renamed"
            elif ln.startswith("rename to "):
                new_path = ln[len("rename to "):]
            elif ln.startswith("--- "):
                src = ln[4:].strip()
                if src != "/dev/null":
                    old_path = old_path or _strip_prefix(src)
            elif ln.startswith("+++ "):
                dst = ln[4:].strip()
                if dst != "/dev/null":
                    new_path = _strip_prefix(dst)
            elif ln.startswith("@@"):
                body_start = i
                break
        if new_path is None:
            header = lines[0][len("diff --git "):].split(" b/", 1)
            new_path = header[1] if len(header) == 2 else _strip_prefix(header[0])
        patch = "\n".join(lines[body_start:])
        diff.files[new_path] = parse_patch(new_path, patch, status, old_path)
    return diff
