"""Proof by failing test: ask for a pytest that shows the bug, run it in a sandbox, classify.

Running generated tests means running PR code. Proof is off by default; see README.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol

from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from reviewhound.config import ProofConfig, RepoConfig, Severity
from reviewhound.findings import Finding, _extract_json
from reviewhound.index.treesitter import CodeIndex
from reviewhound.paths import language_of
from reviewhound.policy import PolicyDecision
from reviewhound.providers import ProviderError, ProviderRegistry
from reviewhound.vcs.base import Snapshot

log = logging.getLogger(__name__)

ProofVerdict = Literal["proven", "not_proven", "inconclusive"]
# A test that dies on these never reached the code under test.
NOT_A_PROOF = frozenset({"ImportError", "ModuleNotFoundError", "SyntaxError", "IndentationError",
                         "TabError"})
DEFAULT_IMAGE = "python:3.12-slim"
MAX_SNAPSHOT_BYTES = 20_000_000
MAX_FILE_PROMPT_BYTES = 20_000
MAX_JUNIT_BYTES = 5_000_000
_PATH_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]*$")
_EXC_RE = re.compile(r"^([A-Za-z_][\w.]*)(?::|$)")
_LOC_RE = re.compile(r":\d+: ([A-Za-z_][\w.]*)$")


class ProofError(RuntimeError):
    """The proof could not be attempted (unsafe path, snapshot too large)."""


class ProofAnswer(BaseModel):
    model_config = ConfigDict(extra="ignore")
    test_path: str
    code: str = Field(min_length=1)
    expected_exception: str | None = None

    @field_validator("test_path")
    @classmethod
    def _safe_test_path(cls, v: str) -> str:
        p = PurePosixPath(v)
        if not _PATH_RE.match(v) or p.is_absolute() or ".." in p.parts or \
                not (p.name.startswith("test_") and p.suffix == ".py"):
            raise ValueError(f"test_path must be a relative test_*.py path, got {v!r}")
        return p.as_posix()


@dataclass
class RunResult:
    """What a sandbox run produced. `error` is set when the runner itself could not run."""

    returncode: int | None
    junit_xml: str | None
    timed_out: bool = False
    error: str = ""
    output_tail: str = ""


class SandboxRunner(Protocol):
    name: str

    def run(self, files: Mapping[str, str], test_path: str, timeout_s: float) -> RunResult: ...


@dataclass
class CaseOutcome:
    name: str
    status: Literal["passed", "failed", "error", "skipped"]
    exc_type: str | None = None
    message: str = ""


def _failure_type(text: str, message: str) -> str | None:
    # pytest ends the traceback with "path:line: ExcType"; the message drops the type for asserts.
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    m = _LOC_RE.search(lines[-1]) if lines else None
    if m is None:
        m = _EXC_RE.match(message)
    return m.group(1).rsplit(".", 1)[-1] if m else None


def parse_junit(xml_text: str) -> list[CaseOutcome]:
    """Read pytest's --junitxml output. Raises ValueError on XML that is not a test report."""
    try:
        root = SafeET.fromstring(xml_text)
    except (SafeET.ParseError, DefusedXmlException) as exc:
        raise ValueError(f"junit xml does not parse: {type(exc).__name__}") from exc
    if root.tag not in ("testsuites", "testsuite"):
        raise ValueError(f"unexpected junit root <{root.tag}>")
    out: list[CaseOutcome] = []
    for case in root.iter("testcase"):
        name = f"{case.get('classname', '')}::{case.get('name', '')}"
        failure, error, skipped = case.find("failure"), case.find("error"), case.find("skipped")
        if error is not None:
            out.append(CaseOutcome(name, "error", message=error.get("message", "")))
        elif failure is not None:
            message = failure.get("message", "")
            out.append(CaseOutcome(name, "failed", _failure_type(failure.text or "", message),
                                   message))
        elif skipped is not None:
            out.append(CaseOutcome(name, "skipped", message=skipped.get("message", "")))
        else:
            out.append(CaseOutcome(name, "passed"))
    return out


def classify(run: RunResult, expected_exception: str | None = None) -> tuple[ProofVerdict, str]:
    """Proven only when every test ran and failed on the claimed behaviour itself."""
    if run.error:
        return "inconclusive", run.error
    if run.timed_out:
        return "inconclusive", "test run timed out"
    if run.junit_xml is None:
        return "inconclusive", f"no junit report (exit {run.returncode})"
    try:
        cases = parse_junit(run.junit_xml)
    except ValueError as exc:
        return "inconclusive", str(exc)
    if not cases:
        return "inconclusive", "no tests collected"
    errors = [c for c in cases if c.status == "error"]
    if errors:
        return "inconclusive", f"test error: {errors[0].message[:200]}"
    if any(c.status == "skipped" for c in cases):
        return "inconclusive", "a test was skipped"
    failed = [c for c in cases if c.status == "failed"]
    if not failed:
        if run.returncode != 0:
            return "inconclusive", f"tests passed but pytest exited {run.returncode}"
        return "not_proven", "the generated test passes on the PR head"
    allowed = {"AssertionError"}
    claimed = expected_exception.rsplit(".", 1)[-1] if expected_exception else ""
    if claimed and claimed not in NOT_A_PROOF:
        allowed.add(claimed)
    for c in failed:
        if c.exc_type not in allowed:
            return "inconclusive", (f"test failed with {c.exc_type or 'an unknown error'}, "
                                   "not the claimed behaviour")
    if run.returncode != 1:
        return "inconclusive", f"tests failed but pytest exited {run.returncode}"
    if len(failed) < len(cases):
        return "inconclusive", "some generated tests passed and some failed"
    return "proven", f"the generated test fails on the PR head with {failed[0].exc_type}"


def _write_tree(files: Mapping[str, str], root: Path) -> None:
    resolved = root.resolve()
    for rel, text in files.items():
        target = (root / rel).resolve()
        if PurePosixPath(rel).is_absolute() or not target.is_relative_to(resolved):
            raise ProofError(f"refusing to write outside the sandbox: {rel!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def _remove_tree(path: Path) -> None:
    def report(func: Callable[..., object], p: str, exc: BaseException) -> None:
        log.warning("proof temp dir cleanup failed", extra={"path": p,
                                                           "error": type(exc).__name__})
    shutil.rmtree(path, onexc=report)


_MISSING, _UNREADABLE = "missing", "unreadable"


def _read_untrusted(path: Path, limit: int, tail: bool = False) -> bytes | str:
    """Read at most `limit` bytes of a file the code under test could have replaced.

    Returns the bytes, or _MISSING / _UNREADABLE. A symlink, FIFO or device is never opened for
    reading: no-follow and non-blocking open, then only a regular file is read.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return _MISSING
    except OSError:
        return _UNREADABLE
    with os.fdopen(fd, "rb") as fh:
        info = os.fstat(fh.fileno())
        if not stat.S_ISREG(info.st_mode):
            return _UNREADABLE
        if tail:
            fh.seek(max(0, info.st_size - limit))
        return fh.read(limit)


def _tail(path: Path, limit: int = 2_000) -> str:
    data = _read_untrusted(path, limit, tail=True)
    return data.decode("utf-8", "replace") if isinstance(data, bytes) else ""


def _read_junit(path: Path) -> str | None:
    """The report is written by code under test, so it is untrusted and size-capped."""
    data = _read_untrusted(path, MAX_JUNIT_BYTES + 1)
    if data == _MISSING:
        return None
    if not isinstance(data, bytes):
        return "<unreadable-report/>"
    if len(data) > MAX_JUNIT_BYTES:
        return "<oversized-report/>"
    return data.decode("utf-8", errors="replace")


_PYTEST_FLAGS = ["-q", "-p", "no:cacheprovider", "-o", "junit_family=xunit2"]
# Sets rlimits on itself, then becomes pytest; avoids preexec_fn, which is unsafe with threads.
_LIMITS_BOOTSTRAP = (
    "import os, resource, sys\n"
    "cpu, mem, fsize = (int(x) for x in sys.argv[1:4])\n"
    "resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 1))\n"
    "if mem > 0:\n"
    "    resource.setrlimit(resource.RLIMIT_AS, (mem, mem))\n"
    "resource.setrlimit(resource.RLIMIT_FSIZE, (fsize, fsize))\n"
    "os.execv(sys.argv[4], sys.argv[4:])\n"
)


class LocalRunner:
    """Runs the test as a plain subprocess. Not a sandbox: only for repos you trust."""

    name = "local"

    def __init__(self, python: str = sys.executable, memory_bytes: int = 2 << 30,
                 file_size_bytes: int = 50 << 20) -> None:
        self.python = python
        # macOS refuses RLIMIT_AS outright, so there the address-space cap is not applied.
        self.memory_bytes = 0 if sys.platform == "darwin" else memory_bytes
        self.file_size_bytes = file_size_bytes

    def _env(self, tmp: Path, work: Path) -> dict[str, str]:
        # An allowlist, not a scrub list: nothing from the server's environment leaks in.
        return {"PATH": os.defpath, "HOME": str(tmp / "home"), "TMPDIR": str(tmp / "tmp"),
                "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
                "PYTHONHASHSEED": "0", "PYTHONPATH": f"{work}{os.pathsep}{work / 'src'}"}

    def run(self, files: Mapping[str, str], test_path: str, timeout_s: float) -> RunResult:
        tmp = Path(tempfile.mkdtemp(prefix="reviewhound-proof-")).resolve()
        try:
            work, out = tmp / "work", tmp / "out"
            for d in (work, out, tmp / "home", tmp / "tmp"):
                d.mkdir()
            _write_tree(files, work)
            (out / "pytest.ini").write_text("[pytest]\n")
            cmd = [self.python, "-c", _LIMITS_BOOTSTRAP, str(max(1, int(timeout_s))),
                   str(self.memory_bytes), str(self.file_size_bytes), self.python, "-m", "pytest",
                   *_PYTEST_FLAGS, "-c", str(out / "pytest.ini"), f"--rootdir={work}",
                   f"--junitxml={out / 'junit.xml'}", "--", test_path]
            with (out / "output.txt").open("w") as sink:
                try:
                    proc = subprocess.Popen(cmd, cwd=work, env=self._env(tmp, work),
                                            stdin=subprocess.DEVNULL, stdout=sink,
                                            stderr=subprocess.STDOUT, start_new_session=True)
                except OSError as exc:
                    return RunResult(None, None, error=f"could not start python: "
                                                       f"{type(exc).__name__}")
                try:
                    rc = proc.wait(timeout=timeout_s)
                except subprocess.TimeoutExpired:
                    _kill_group(proc)
                    return RunResult(None, None, timed_out=True,
                                     output_tail=_tail(out / "output.txt"))
            return RunResult(rc, _read_junit(out / "junit.xml"),
                             output_tail=_tail(out / "output.txt"))
        finally:
            _remove_tree(tmp)


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(proc.pid, 9)
    except ProcessLookupError:
        pass  # already exited between the timeout and the kill
    proc.wait()


CommandRunner = Callable[[list[str], float], "subprocess.CompletedProcess[str]"]


def _run_command(cmd: list[str], timeout_s: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s, check=False,
                          stdin=subprocess.DEVNULL)


_DOCKER_SCRIPT = ('cp -r /src /tmp/work && cd /tmp/work && '
                  'PYTHONPATH=/tmp/work:/tmp/work/src exec python -m pytest '
                  + " ".join(_PYTEST_FLAGS) + ' -c /out/pytest.ini --rootdir=/tmp/work '
                  '--junitxml=/out/junit.xml -- "$1" > /out/output.txt 2>&1')


class DockerRunner:
    """Runs the test in a throwaway container with no network and a read-only root."""

    name = "docker"

    def __init__(self, image: str = DEFAULT_IMAGE, docker: str = "docker",
                 command: CommandRunner = _run_command, startup_grace_s: float = 30.0) -> None:
        self.image = image
        self.docker = docker
        self._cmd = command
        self.startup_grace_s = startup_grace_s

    def _unavailable(self) -> str:
        try:
            res = self._cmd([self.docker, "info", "--format", "{{.ServerVersion}}"], 15)
        except OSError:
            return "docker unavailable: the docker CLI is not installed"
        except subprocess.TimeoutExpired:
            return "docker unavailable: docker info timed out"
        if res.returncode != 0:
            return "docker unavailable: the daemon is not running"
        return ""

    def command(self, work: Path, out: Path, test_path: str, name: str) -> list[str]:
        return [self.docker, "run", "--rm", "--name", name, "--network", "none", "--read-only",
                "--memory", "512m", "--cpus", "1", "--pids-limit", "128", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges", "--tmpfs", "/tmp:rw,size=256m",
                "-v", f"{work}:/src:ro", "-v", f"{out}:/out:rw", "-e", "HOME=/tmp",
                "-e", "PYTHONDONTWRITEBYTECODE=1", "-w", "/tmp", self.image,
                "sh", "-c", _DOCKER_SCRIPT, "sh", test_path]

    def run(self, files: Mapping[str, str], test_path: str, timeout_s: float) -> RunResult:
        problem = self._unavailable()
        if problem:
            return RunResult(None, None, error=problem)
        tmp = Path(tempfile.mkdtemp(prefix="reviewhound-proof-")).resolve()
        try:
            work, out = tmp / "work", tmp / "out"
            work.mkdir()
            out.mkdir()
            _write_tree(files, work)
            (out / "pytest.ini").write_text("[pytest]\n")
            name = f"reviewhound-proof-{uuid.uuid4().hex[:12]}"
            try:
                res = self._cmd(self.command(work, out, test_path, name),
                                timeout_s + self.startup_grace_s)
            except subprocess.TimeoutExpired:
                self._kill(name)
                return RunResult(None, None, timed_out=True)
            except OSError as exc:
                return RunResult(None, None, error=f"docker could not start: "
                                                   f"{type(exc).__name__}")
            if res.returncode == 125:  # the docker CLI's own failure, e.g. a missing image
                return RunResult(125, None, error="docker run failed: "
                                                  + (res.stderr or "").strip()[-300:])
            return RunResult(res.returncode, _read_junit(out / "junit.xml"),
                             output_tail=_tail(out / "output.txt"))
        finally:
            _remove_tree(tmp)

    def _kill(self, name: str) -> None:
        # Killing the CLI on timeout leaves the container running; stop it by name.
        try:
            res = self._cmd([self.docker, "kill", name], 15)
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warning("could not kill timed-out proof container",
                        extra={"container": name, "error": type(exc).__name__})
            return
        if res.returncode != 0:
            log.warning("could not kill timed-out proof container",
                        extra={"container": name, "returncode": res.returncode})


@dataclass
class ProofResult:
    verdict: ProofVerdict
    reason: str
    test_path: str = ""
    code: str = ""


def _module_name(path: str) -> str:
    parts = list(PurePosixPath(path).with_suffix("").parts)
    if parts and parts[0] == "src" and len(parts) > 1:
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def build_proof_prompt(f: Finding, source: str, function_src: str) -> str:
    file_src = source if len(source) <= MAX_FILE_PROMPT_BYTES else \
        source[:MAX_FILE_PROMPT_BYTES] + "\n# ... (truncated)\n"
    return f"""You are proving a reported bug with a failing pytest test.

Reported bug in `{f.file}` line {f.line} ({f.severity.name.lower()}): {f.title}
{f.body}
Evidence: {f.evidence_quote}

The changed function:
```python
{function_src or "(no enclosing function found)"}
```

The whole file at the PR head (importable as module `{_module_name(f.file)}`):
```python
{file_src}
```

Write ONE pytest file that exercises exactly the claimed wrong behaviour. Assert the CORRECT
behaviour, so the test fails on this code if and only if the bug is real. Use only the repository
code and the Python standard library (pytest is available). No network, no sleeping, no writing
outside the working directory. If the bug shows as an exception rather than a wrong value, name
the exception class in expected_exception; otherwise use null.

Answer with JSON only, in this shape:
{{"test_path": "tests/test_reviewhound_proof.py", "code": "...", "expected_exception": null}}
"""


def snapshot_files(head: Snapshot, max_bytes: int | None = None) -> dict[str, str]:
    limit = MAX_SNAPSHOT_BYTES if max_bytes is None else max_bytes
    files: dict[str, str] = {}
    total = 0
    for path in head.paths():
        text = head.read(path)
        if text is None:
            continue
        total += len(text.encode("utf-8"))
        if total > limit:
            raise ProofError(f"head snapshot is over {limit} bytes")
        files[path] = text
    return files


class Prover:
    def __init__(self, registry: ProviderRegistry, runner: SandboxRunner, cfg: ProofConfig,
                 model: str, model_timeout_s: float) -> None:
        self.registry = registry
        self.runner = runner
        self.cfg = cfg
        self.model = model
        self.model_timeout_s = model_timeout_s
        self.cost_usd = 0.0

    def prove(self, f: Finding, files: Mapping[str, str], index: CodeIndex) -> ProofResult:
        source = files.get(f.file)
        if source is None:
            return ProofResult("inconclusive", "file is not in the head snapshot")
        d = index.enclosing_definition(f.file, f.line)
        function_src = "\n".join(source.splitlines()[d.start_line - 1:d.end_line]) if d else ""
        try:
            res = self.registry.complete(build_proof_prompt(f, source, function_src),
                                         model=self.model, timeout_s=self.model_timeout_s)
        except ProviderError as exc:
            return ProofResult("inconclusive", f"model call failed: {exc}")
        self.cost_usd += res.completion.cost_usd
        try:
            ans = ProofAnswer.model_validate(_extract_json(res.completion.text))
        except (ValueError, ValidationError) as exc:
            return ProofResult("inconclusive", f"malformed proof answer: {type(exc).__name__}")
        if ans.test_path in files:
            return ProofResult("inconclusive", f"test path {ans.test_path} would overwrite a repo "
                                               "file", ans.test_path, ans.code)
        run = self.runner.run({**files, ans.test_path: ans.code}, ans.test_path,
                              self.cfg.timeout_s)
        verdict, reason = classify(run, ans.expected_exception)
        return ProofResult(verdict, reason, ans.test_path, ans.code)



def eligible(pc: ProofConfig, findings: list[Finding]) -> list[Finding]:
    floor = Severity.parse(pc.min_severity)
    return [f for f in findings if f.verified and f.severity >= floor and
            f.source.startswith("model:") and language_of(f.file) == "python"
            and f.side == "RIGHT"][:pc.max_findings]


def select_runner(cfg: ProofConfig, runners: Mapping[str, SandboxRunner]) -> SandboxRunner | None:
    return runners.get(cfg.runner)


def run_proof_stage(prover: Prover | None, decision: PolicyDecision, cfg: RepoConfig,
                    head: Snapshot, index: CodeIndex) -> None:
    """Attach proofs to the findings about to be posted. Never raises."""
    pc = cfg.proof
    candidates = eligible(pc, decision.post)
    if not candidates:
        return
    if prover is None:
        for f in candidates:
            _attach(f, ProofResult("inconclusive", f"proof runner {pc.runner!r} is not enabled "
                                                   "on this server"))
        return
    try:
        files = snapshot_files(head)
    except ProofError as exc:
        for f in candidates:
            _attach(f, ProofResult("inconclusive", str(exc)))
        return
    try:
        _prove_each(prover, candidates, files, index, pc)
    finally:
        _reapply_floor_and_event(decision, cfg)


def _prove_each(prover: Prover, candidates: list[Finding], files: Mapping[str, str],
                index: CodeIndex, pc: ProofConfig) -> None:
    for f in candidates:
        try:
            result = prover.prove(f, files, index)
        except Exception as exc:  # noqa: BLE001 - a proof must never fail the review
            log.warning("proof crashed", extra={"file": f.file, "line": f.line,
                                                "error": type(exc).__name__})
            result = ProofResult("inconclusive", f"proof crashed: {type(exc).__name__}")
        _attach(f, result)
        log.info("proof finished", extra={"file": f.file, "line": f.line,
                                           "verdict": result.verdict})
        if result.verdict == "not_proven" and pc.downgrade_unproven and \
                f.severity == Severity.BLOCKER:
            f.severity = Severity.MAJOR
            f.notes.append("downgraded from blocker: the generated test did not reproduce it")


def _attach(f: Finding, r: ProofResult) -> None:
    f.proof = r.verdict
    f.proof_test = r.code
    f.notes.append(f"proof {r.verdict}: {r.reason}")


def _reapply_floor_and_event(decision: PolicyDecision, cfg: RepoConfig) -> None:
    floor = Severity.parse(cfg.policy.min_severity_to_post)
    keep: list[Finding] = []
    for f in decision.post:
        if f.severity < floor:
            decision.held.append((f, (f"below {cfg.policy.min_severity_to_post} after an "
                                     "unreproduced proof")))
        else:
            keep.append(f)
    decision.post = keep
    blocking = Severity.parse(cfg.policy.blocking_severity)
    decision.event = "REQUEST_CHANGES" if any(f.severity >= blocking for f in keep) \
        else "COMMENT"
