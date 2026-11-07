"""Build the review context for one PR inside a byte budget, most important first."""

from __future__ import annotations

import re
import secrets
from collections.abc import Sequence
from dataclasses import dataclass, field

from reviewhound.diff import Diff, FileDiff
from reviewhound.index.treesitter import CodeIndex, Definition
from reviewhound.vcs.base import PullRequest

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_MAX_BODY_BYTES = 4_000
_MAX_RULES_BYTES = 16_000
_RULES_SHARE = 5  # rules get at most a fifth of the budget, ahead of the diff
_MIN_RULES_BYTES = 1_000
_CUT = "\n[rules file cut here to fit the review budget]"
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")


def header_safe(text: str) -> str:
    """`text` on one line: control characters and line breaks shown as escapes, so a file
    name cannot open a section of its own."""
    return _CONTROL.sub(lambda m: m.group(0).encode("unicode_escape").decode("ascii"), text)


@dataclass
class Context:
    text: str
    included: list[str] = field(default_factory=list)
    omitted: list[str] = field(default_factory=list)
    rules_marker: str = ""  # names the block holding the real rules files; empty when none

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

    @property
    def free(self) -> int:
        return self.limit - self.used - 2

    def add(self, block: str) -> bool:
        size = len(block.encode("utf-8")) + 2
        if self.used + size > self.limit:
            return False
        self.parts.append(block)
        self.used += size
        return True


def _cut_to(text: str, limit: int) -> str:
    """`text` within `limit` UTF-8 bytes, cut at a line end and marked when cut."""
    if len(text.encode("utf-8")) <= limit:
        return text
    room = limit - len(_CUT.encode("utf-8"))
    kept = text.encode("utf-8")[:max(room, 0)].decode("utf-8", errors="ignore")
    if "\n" in kept:  # a file with no line break in reach is cut mid-line instead
        kept = kept[:kept.rfind("\n")].rstrip("\n")
    return kept + _CUT if kept else ""


def _rules_block(marker: str, sections: list[str]) -> str:
    return f"<rules-{marker}>\n" + "\n\n".join(sections) + f"\n</rules-{marker}>"


def _diff_block(fd: FileDiff) -> str:
    return f"## Diff: {header_safe(fd.path)} ({fd.status})\n{render_hunks(fd)}"


def _bytes_taken(blocks: list[str], room: int) -> int:
    """Bytes `_Budget.add` would spend on `blocks`, in order, with `room` bytes left."""
    used = 0
    for block in blocks:
        size = len(block.encode("utf-8")) + 2
        if used + size <= room:
            used += size
    return used


def changed_definitions(diff: Diff, index: CodeIndex) -> list[Definition]:
    seen: dict[tuple[str, str], Definition] = {}
    for path, fd in diff.files.items():
        for line in sorted(fd.added_lines):
            d = index.enclosing_definition(path, line)
            if d is not None:
                seen.setdefault((d.file, d.qualified), d)
    return list(seen.values())


def build_context(pr: PullRequest, diff: Diff, index: CodeIndex, budget_bytes: int,
                  reviewable: list[str], conventions: str = "", churn_note: str = "",
                  house_rules: Sequence[tuple[str, str]] = ()) -> Context:
    """`house_rules` is (path, text) of the repo's own rules files, root first."""
    ctx = Context(text="")
    b = _Budget(budget_bytes)
    body = pr.body[:_MAX_BODY_BYTES]
    b.add(f"## Pull request\nTitle: {header_safe(pr.title)}\n\n{body}".rstrip())
    if conventions:
        b.add(f"## Repository conventions (learned)\n{conventions}")

    diff_blocks = [(path, _diff_block(diff.files[path])) for path in reviewable]
    reserved = _bytes_taken([block for _, block in diff_blocks], b.free + 2)
    rules_left = min(_MAX_RULES_BYTES, budget_bytes // _RULES_SHARE, b.free - reserved)
    marker = secrets.token_hex(8)
    room = min(rules_left, b.free) - len(_rules_block(marker, [""]).encode("utf-8"))
    sections: list[tuple[str, str]] = []
    for path, text in house_rules:
        title = f"## Repository rules: {header_safe(path)} (the team's own rules for this repo)\n"
        gap = 2 if sections else 0  # the blank line between two rules files
        size = room - gap - len(title.encode("utf-8"))
        rules_body = _cut_to(text.strip(), size) if size >= _MIN_RULES_BYTES else ""
        if rules_body:
            room -= gap + len((title + rules_body).encode("utf-8"))
            sections.append((path, title + rules_body))
        else:
            ctx.omitted.append(f"rules:{path}")
    if sections and b.add(_rules_block(marker, [section for _, section in sections])):
        ctx.rules_marker = marker
        ctx.included.extend(f"rules:{path}" for path, _ in sections)
    else:
        ctx.omitted.extend(f"rules:{path}" for path, _ in sections)

    for path, block in diff_blocks:
        if b.add(block):
            ctx.included.append(f"diff:{path}")
        else:
            ctx.omitted.append(f"diff:{path}")

    changed = changed_definitions(diff, index)
    for d in changed:
        src = index.sources.get(d.file, "")
        block = f"## Changed function at head: {d.qualified} ({header_safe(d.file)})\n" \
                f"{numbered(src, d.start_line, d.end_line)}"
        (ctx.included if b.add(block) else ctx.omitted).append(f"def:{d.file}:{d.qualified}")

    for d in changed:
        for caller in index.callers_of(d.name)[:5]:
            path, qualified = caller.split("::", 1)
            defs = [x for x in index.definitions(qualified) if x.file == path]
            if not defs or (path, qualified) == (d.file, d.qualified):
                continue
            c = defs[0]
            block = f"## Caller of {d.name}: {c.qualified} ({header_safe(c.file)})\n" \
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
            block = f"## Referenced {d.kind}: {d.qualified} ({header_safe(d.file)})\n" \
                    f"{numbered(index.sources[d.file], d.start_line, min(d.end_line, d.start_line + 60))}"
            (ctx.included if b.add(block) else ctx.omitted).append(f"ref:{d.file}:{d.qualified}")

    if churn_note:
        b.add(f"## Recent history\n{churn_note}")
    ctx.text = "\n\n".join(b.parts)
    return ctx
