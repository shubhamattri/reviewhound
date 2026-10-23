"""The reviewer prompt. Generic: everything team-specific arrives through config."""

from __future__ import annotations

import json

from reviewhound.config import RepoConfig
from reviewhound.findings import findings_json_schema

_INSTRUCTIONS = """You are reviewing a pull request. Report only real defects that the change
introduces: wrong behaviour, crashes, security holes, data loss, broken contracts, missing
checks. Do not report style, naming, or anything in lines this PR did not change.

Every finding must be provable from the code shown:
- `line` is a head-side line number from the diff (the number printed before `+`).
- `evidence_quote` is copied exactly from that line or the lines around it.
- `reachability` says how execution reaches the line.
- `ruled_out` names the most likely reason this is NOT a bug and why it does not apply.
- `claims` lists the facts your finding depends on, each one checkable:
    {"kind": "quote", "file": "...", "text": "<exact code>"}
    {"kind": "symbol_exists", "name": "<function, class or variable>"}
    {"kind": "calls", "caller": "<function>", "callee": "<function>"}
    {"kind": "not_calls", "caller": "<function>", "callee": "<function>"}
  Every claim is checked against the code. One false claim discards the finding.

Severity: blocker (will break production or open a security hole), major (wrong behaviour
users will hit), minor (edge case or robustness), nit (rarely worth reporting).
Report each defect once, at the line that causes it, with its own severity: never bundle
unrelated defects into one finding, and never split one defect into several. In `body`, state only what the code shown proves: no guesses about
compilers, linters, frameworks or code you cannot see.
If there is nothing real to report, return {"findings": []}. Fewer, certain findings beat
many guesses.

Answer with JSON only, matching this schema:"""


def build_prompt(context_text: str, cfg: RepoConfig) -> str:
    parts = [_INSTRUCTIONS, json.dumps(findings_json_schema(), separators=(",", ":"))]
    if cfg.auth.guard_functions:
        parts.append("Team rule: protected entry points must call one of: "
                     + ", ".join(cfg.auth.guard_functions)
                     + (f" (roles: {', '.join(cfg.auth.roles)})" if cfg.auth.roles else ""))
    if cfg.tone_examples:
        parts.append("Write comment bodies in this tone:\n" + "\n".join(
            f"- {t}" for t in cfg.tone_examples[:5]))
    parts.append("# Pull request context\n\n" + context_text)
    return "\n\n".join(parts)
