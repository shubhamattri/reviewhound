"""Deterministic check: a hard-coded spacing or radius length where the repo has a token for it.

Tokens come from CSS custom properties in the repo (`--sp-1: 4px;`) and from token tables in
the repo's rules files (`--sp-1: 4px   --sp-2: 8px`), since canonical tokens often live in a
package outside the repo. Only an exact value match in the same family is flagged."""

from __future__ import annotations

import re
from collections import defaultdict

from reviewhound.config import RepoConfig, Severity
from reviewhound.diff import Diff
from reviewhound.findings import Finding
from reviewhound.house_rules import read_rules_files
from reviewhound.paths import matches_any
from reviewhound.vcs.base import Snapshot

STYLE_EXT = (".css", ".scss", ".sass", ".less")
INLINE_EXT = (".jsx", ".tsx", ".js", ".ts", ".html", ".vue", ".svelte")
MAX_STYLE_FILES = 3_000
MAX_FINDINGS = 5  # so a new stylesheet cannot crowd out the model's findings
SPACING, RADIUS = "spacing", "radius"
_STYLE_ATTR, _DELETED = "style=", "deleted"

_FAMILY_TOKEN = {SPACING: re.compile(r"^--(?:sp|space|spacing)-[A-Za-z0-9_-]+$"),
                 RADIUS: re.compile(r"^--(?:r|radius)-[A-Za-z0-9_-]+$")}
_TOKENISH = re.compile(r"^--(?:sp|space|spacing|r|radius)(?:-|$)")
_NAME = r"--(?:sp|space|spacing|r|radius)-[A-Za-z0-9_-]+"
_LEN = r"(\d*\.?\d+)(px|rem|em)"
# CSS: one plain length per token. Rules file: several per line.
_CSS_DEF = re.compile(rf"({_NAME})\s*:\s*{_LEN}\s*(?:!important\s*)?(?:;|\}}|$)", re.IGNORECASE)
_RULES_DEF = re.compile(rf"({_NAME})`?\s*:\s*`?{_LEN}(?![\w.%(])", re.IGNORECASE)
_DECL = re.compile(r"(?:^|(?<=[\s;{]))(--[A-Za-z][\w-]*|[a-z][a-z-]*)\s*:\s*([^;{}]+)")
_INLINE = re.compile(r"""\b([a-zA-Z]+)\s*:\s*['"]([^'"]+)['"]""")
_LITERAL = re.compile(rf"(?<![\w.#$@-]){_LEN}(?![\w%(])", re.IGNORECASE)
_PARENS = re.compile(r"\([^()]*\)")
_COMMENT = re.compile(r"/\*.*?\*/")
_SPACING_PROPS = {"padding", "margin", "gap", "row-gap", "column-gap", "grid-gap", "grid-row-gap",
                  "grid-column-gap", "inset", "top", "left", "right", "bottom"}

Tokens = dict[tuple[str, str], list[str]]


def _norm(number: str, unit: str) -> str:
    return f"{float(number):g}{unit.lower()}"


def family_of(prop: str) -> str | None:
    p = prop.lower()
    if p.startswith("--"):
        if _TOKENISH.match(p):  # defines a token or alias, not a use of one
            return None
        if re.search(r"padding|margin|gap|spacing", p):
            return SPACING
        return RADIUS if RADIUS in p else None
    if p.startswith("border") and p.endswith(RADIUS):
        return RADIUS
    if p in _SPACING_PROPS or p.startswith(("padding-", "margin-", "inset-")):
        return SPACING
    return None


def load_tokens(rules: list[tuple[str, str]], styles: list[tuple[str, str]]) -> Tokens:
    """(family, value) -> token names, rules-file tokens first."""
    found: dict[tuple[str, str], list[str]] = defaultdict(list)
    for texts, pattern in ((rules, _RULES_DEF), (styles, _CSS_DEF)):
        for _path, text in texts:
            for line in text.splitlines():
                for m in pattern.finditer(line):
                    name = m.group(1).lower()
                    family = next(f for f, rx in _FAMILY_TOKEN.items() if rx.match(name))
                    key = (family, _norm(m.group(2), m.group(3)))
                    if name not in found[key]:
                        found[key].append(name)
    return dict(found)


def _declarations(path: str, text: str) -> list[tuple[str, str]]:
    if path.endswith(STYLE_EXT):
        line = _COMMENT.sub("", text).strip()
        if line.startswith(("//", "/*", "*")):
            return []
        return [(m.group(1), m.group(2)) for m in _DECL.finditer(line)]
    if _STYLE_ATTR not in text:
        return []
    out = [(re.sub(r"[A-Z]", lambda c: "-" + c.group(0).lower(), m.group(1)), m.group(2))
           for m in _INLINE.finditer(text)]
    for attr in re.finditer(r"""style=["']([^"']+)["']""", text):
        out.extend((m.group(1), m.group(2)) for m in _DECL.finditer(attr.group(1)))
    return out


def _replacements(decls: list[tuple[str, str]], tokens: Tokens) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for prop, value in decls:
        family = family_of(prop)
        if family is None:
            continue
        bare = value
        while _PARENS.search(bare):  # calc(), var() fallbacks and the like stay as written
            bare = _PARENS.sub(" ", bare)
        for m in _LITERAL.finditer(bare):
            literal = _norm(m.group(1), m.group(2))
            if literal in ("0px", "0rem", "0em", "1px"):
                continue
            names = tokens.get((family, literal))
            if names and (names[0], m.group(0)) not in out:
                out.append((names[0], m.group(0)))
    return out


class DesignTokenScanner:
    name = "design-tokens"

    def scan(self, diff: Diff, head: Snapshot, cfg: RepoConfig) -> list[Finding]:
        if not cfg.design_tokens:
            return []
        targets = [p for p, fd in diff.files.items()
                   if fd.status != _DELETED and p.endswith(STYLE_EXT + INLINE_EXT)
                   and not matches_any(p, cfg.ignore_paths)]
        if not targets:
            return []
        tokens = self._tokens(head, targets, cfg)
        if not tokens:
            return []
        out: list[Finding] = []
        for path in targets:
            src = head.read(path)
            if src is None:
                continue
            lines = src.splitlines()
            for n in sorted(diff.files[path].added_lines):
                if not 0 < n <= len(lines):
                    continue
                swaps = _replacements(_declarations(path, lines[n - 1]), tokens)
                if swaps:
                    out.append(self._finding(path, n, lines[n - 1], swaps))
        if len(out) > MAX_FINDINGS:
            rest = len(out) - MAX_FINDINGS
            out = out[:MAX_FINDINGS]
            out[-1].body += f" {rest} more added lines in this PR have the same issue."
        return out

    def _tokens(self, head: Snapshot, targets: list[str], cfg: RepoConfig) -> Tokens:
        rules = read_rules_files(targets, lambda p: head.read(p) is not None, head.read)
        styles: list[tuple[str, str]] = []
        for path in head.paths():
            if len(styles) >= MAX_STYLE_FILES:
                break
            if path.endswith(STYLE_EXT) and not matches_any(path, cfg.ignore_paths):
                text = head.read(path)
                if text is not None:
                    styles.append((path, text))
        return load_tokens(rules, styles)

    @staticmethod
    def _finding(path: str, n: int, line: str, swaps: list[tuple[str, str]]) -> Finding:
        pairs = ", ".join(f"var({name}) instead of {lit}" for name, lit in swaps)
        return Finding(
            file=path, line=n, severity=Severity.MINOR, category="design-token",
            title=f"Use {pairs}",
            body=f"The repo defines a design token with this exact value. Use {pairs}, so the "
                 "value follows the design system when the token changes.",
            evidence_quote=line.strip(), source="scanner:design-tokens", verified=True)
