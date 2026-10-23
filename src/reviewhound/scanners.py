"""Rule-based scanners for known patterns. These never go to the model."""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Protocol

from reviewhound.config import RepoConfig, Severity
from reviewhound.diff import Diff
from reviewhound.findings import Finding
from reviewhound.vcs.base import Snapshot

log = logging.getLogger(__name__)


class Scanner(Protocol):
    name: str

    def scan(self, diff: Diff, head: Snapshot, cfg: RepoConfig) -> list[Finding]: ...


_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("private key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("hard-coded secret", re.compile(
        r"""(?i)\b\w*(?:api[_-]?key|secret|passwd|password|token)\w*["']?\s*[:=]\s*"""
        r"""["'](?P<value>[^"'\s]{12,})["']""")),
]
_SQL_VERB = r"(?:SELECT|INSERT|UPDATE|DELETE)\b"
_SQL_BUILD = [
    # f-string or template literal with an interpolation inside the SQL text
    re.compile(rf"""(?i)\bf"[^"]*{_SQL_VERB}[^"]*\{{"""),
    re.compile(rf"""(?i)\bf'[^']*{_SQL_VERB}[^']*\{{"""),
    re.compile(rf"""(?i)`[^`]*{_SQL_VERB}[^`]*\$\{{"""),
    # SQL literal joined with +, % or .format()
    re.compile(rf"""(?i)"[^"]*{_SQL_VERB}[^"]*"\s*(?:\+|%|\.format\()"""),
    re.compile(rf"""(?i)'[^']*{_SQL_VERB}[^']*'\s*(?:\+|%|\.format\()"""),
]
_LOG_CALL = re.compile(
    r"\b(?:logger|logging|log|console)\.(?:debug|info|warn|warning|error|exception|log)\s*\((.*)")
_PLACEHOLDER = re.compile(
    r"(?i)(example|dummy|placeholder|changeme|xxx+|<[^>]+>|\$\{|^https?://|/|\.)")


class BuiltinScanner:
    name = "builtin"

    def scan(self, diff: Diff, head: Snapshot, cfg: RepoConfig) -> list[Finding]:
        out: list[Finding] = []
        pii = [t.lower() for t in cfg.personal_data_terms]
        for path, fd in diff.files.items():
            if fd.status == "deleted":
                continue
            src = head.read(path)
            if src is None:
                continue
            lines = src.splitlines()
            for n in sorted(fd.added_lines):
                if not 0 < n <= len(lines):
                    continue
                text = lines[n - 1]
                out.extend(self._line(path, n, text, pii))
        return out

    def _line(self, path: str, n: int, text: str, pii: list[str]) -> list[Finding]:
        found: list[Finding] = []
        quote = text.strip()
        for label, pat in _SECRET_PATTERNS:
            m = pat.search(text)
            if m is None:
                continue
            value = m.groupdict().get("value") or m.group(0)
            if not _PLACEHOLDER.search(value):
                redacted = quote.replace(value, value[:4] + "[redacted]")
                found.append(Finding(
                    file=path, line=n, severity=Severity.BLOCKER, category="secret",
                    title=f"Possible {label} committed",
                    body="This looks like a real credential in source. Move it to the "
                         "environment or a secret store and rotate it.",
                    evidence_quote=redacted, source="scanner:builtin", verified=True))
                break
        if any(p.search(text) for p in _SQL_BUILD):
            found.append(Finding(
                file=path, line=n, severity=Severity.MAJOR, category="sql-injection",
                title="SQL built by string joining",
                body="The query text is built from variables. Use bound parameters so input "
                     "cannot change the query.",
                evidence_quote=quote, source="scanner:builtin", verified=True))
        m = _LOG_CALL.search(text)
        if m:
            args = m.group(1).lower()
            hits = [t for t in pii if re.search(rf"\b{re.escape(t)}\b", args)]
            if hits:
                found.append(Finding(
                    file=path, line=n, severity=Severity.MAJOR, category="personal-data-in-logs",
                    title="Personal data written to logs",
                    body=f"This log line includes {', '.join(hits)}. Log an id instead.",
                    evidence_quote=quote, source="scanner:builtin", verified=True))
        return found


class SemgrepScanner:
    """Runs Semgrep when it is installed. Absent binary means no findings, logged once."""

    name = "semgrep"

    def __init__(self, config: str = "p/default", timeout_s: float = 300) -> None:
        self.config = config
        self.timeout_s = timeout_s
        self.binary = shutil.which("semgrep")

    def scan(self, diff: Diff, head: Snapshot, cfg: RepoConfig) -> list[Finding]:
        if self.binary is None:
            log.info("semgrep not installed; skipping")
            return []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for path in diff.changed_paths:
                src = head.read(path)
                if src is not None:
                    (root / path).parent.mkdir(parents=True, exist_ok=True)
                    (root / path).write_text(src)
            try:
                res = subprocess.run([self.binary, "--json", "--quiet", "--config", self.config,  # noqa: PLW1510
                                      str(root)], capture_output=True, text=True,
                                     timeout=self.timeout_s)
                data = json.loads(res.stdout or "{}")
            except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
                log.warning("semgrep failed", extra={"error": type(exc).__name__})
                return []
        out: list[Finding] = []
        sev = {"ERROR": Severity.MAJOR, "WARNING": Severity.MINOR, "INFO": Severity.NIT}
        for r in data.get("results", []):
            rel = Path(r["path"]).relative_to(root).as_posix() if Path(r["path"]).is_absolute() \
                else r["path"]
            line = int(r["start"]["line"])
            fd = diff.files.get(rel)
            if fd is None or line not in fd.added_lines:
                continue
            extra = r.get("extra", {})
            out.append(Finding(file=rel, line=line,
                               severity=sev.get(extra.get("severity", ""), Severity.MINOR),
                               category=f"semgrep:{r.get('check_id', 'rule')}",
                               title=extra.get("message", "Semgrep finding")[:120],
                               body=extra.get("message", ""), evidence_quote=extra.get("lines", ""),
                               source="scanner:semgrep", verified=True))
        return out
