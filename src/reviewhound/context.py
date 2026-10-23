"""Build the review context for one PR inside a byte budget, most important first."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from reviewhound.diff import Diff, FileDiff
from reviewhound.index.treesitter import CodeIndex, Definition
from reviewhound.vcs.base import PullRequest

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_MAX_BODY_BYTES = 4_000


@dataclass
class Context:
    text: str
    included: list[str] = field(default_factory=list)
    omitted: list[str] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.text.encode("utf-8"))


def render_hunks(fd: FileDiff) -> str:
    """Diff with head-side line numbers so the model can cite exact lines."""
    out: list[str] = []
    line = 0
    for raw in fd.patch.splitlines():
        if raw.startswith("@@"):
            out.append(raw)
            m = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)", raw)
            line = int(m.group(1)) if m else 0
            continue
        if raw.startswith("\\"):
            continue
        if raw.startswith("-"):
            out.append(f"{'':>6} -{raw[1:]}")
        elif raw.startswith("+"):
            out.append(f"{line:>6} +{raw[1:]}")
            line += 1
        else:
            out.append(f"{line:>6}  {raw.removeprefix(' ')}")
            line += 1
    return "\n".join(out)


def numbered(source: str, start: int, end: int) -> str:
    lines = source.splitlines()
    return "\n".join(f"{n:>6}  {lines[n - 1]}" for n in range(start, min(end, len(lines)) + 1))


class _Budget:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.parts: list[str] = []
        self.used = 0

    def add(self, block: str) -> bool:
        size = len(block.encode("utf-8")) + 2
        if self.used + size > self.limit:
            return False
        self.parts.append(block)
        self.used += size
        return True


def changed_definitions(diff: Diff, index: CodeIndex) -> list[Definition]:
    seen: dict[tuple[str, str], Definition] = {}
    for path, fd in diff.files.items():
        for line in sorted(fd.added_lines):
            d = index.enclosing_definition(path, line)
            if d is not None:
                seen.setdefault((d.file, d.qualified), d)
    return list(seen.values())


def build_context(pr: PullRequest, diff: Diff, index: CodeIndex, budget_bytes: int,
                  reviewable: list[str], conventions: str = "", churn_note: str = "") -> Context:
    ctx = Context(text="")
    b = _Budget(budget_bytes)
    body = pr.body[:_MAX_BODY_BYTES]
    b.add(f"## Pull request\nTitle: {pr.title}\n\n{body}".rstrip())
    if conventions:
        b.add(f"## Repository conventions (learned)\n{conventions}")

    for path in reviewable:
        fd = diff.files[path]
        block = f"## Diff: {path} ({fd.status})\n{render_hunks(fd)}"
        if b.add(block):
            ctx.included.append(f"diff:{path}")
        else:
            ctx.omitted.append(f"diff:{path}")

    changed = changed_definitions(diff, index)
    for d in changed:
        src = index.sources.get(d.file, "")
        block = f"## Changed function at head: {d.qualified} ({d.file})\n" \
                f"{numbered(src, d.start_line, d.end_line)}"
        (ctx.included if b.add(block) else ctx.omitted).append(f"def:{d.file}:{d.qualified}")

    for d in changed:
        for caller in index.callers_of(d.name)[:5]:
            path, qualified = caller.split("::", 1)
            defs = [x for x in index.definitions(qualified) if x.file == path]
            if not defs or (path, qualified) == (d.file, d.qualified):
                continue
            c = defs[0]
            block = f"## Caller of {d.name}: {c.qualified} ({c.file})\n" \
                    f"{numbered(index.sources[c.file], c.start_line, c.end_line)}"
            (ctx.included if b.add(block) else ctx.omitted).append(f"caller:{c.file}:{c.qualified}")

    referenced: set[str] = set()
    for path in reviewable:
        src = index.sources.get(path, "")
        lines = src.splitlines()
        for n in diff.files[path].added_lines:
            if 0 < n <= len(lines):
                referenced.update(_IDENT.findall(lines[n - 1]))
    changed_names = {d.qualified for d in changed}
    for name in sorted(referenced):
        for d in index.definitions(name)[:1]:
            if d.qualified in changed_names:
                continue
            block = f"## Referenced {d.kind}: {d.qualified} ({d.file})\n" \
                    f"{numbered(index.sources[d.file], d.start_line, min(d.end_line, d.start_line + 60))}"
            (ctx.included if b.add(block) else ctx.omitted).append(f"ref:{d.file}:{d.qualified}")

    if churn_note:
        b.add(f"## Recent history\n{churn_note}")
    ctx.text = "\n\n".join(b.parts)
    return ctx
