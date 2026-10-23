from reviewhound.config import PolicyConfig, Severity
from reviewhound.findings import Finding
from reviewhound.policy import apply_policy


def fnd(line: int, sev: Severity, verified: bool = True, quote: str = "", cat: str = "c") -> Finding:
    return Finding(file="a.py", line=line, severity=sev, category=cat, title=f"t{line}", body="b",
                   evidence_quote=quote or f"code {line}", source="model:x", verified=verified)


def test_floor_unverified_and_cap_are_applied_in_severity_order() -> None:
    fs = [fnd(1, Severity.NIT), fnd(2, Severity.MINOR), fnd(3, Severity.MAJOR, verified=False),
          fnd(4, Severity.MAJOR), fnd(5, Severity.BLOCKER)]
    d = apply_policy(fs, PolicyConfig(max_comments=2))
    assert [f.line for f in d.post] == [5, 4]
    assert sorted((f.line, r) for f, r in d.held) == [
        (1, "below minor"), (2, "over the 2-comment limit"), (3, "unverified")]
    assert d.event == "REQUEST_CHANGES"


def test_without_blocker_the_review_is_a_comment() -> None:
    assert apply_policy([fnd(4, Severity.MAJOR)], PolicyConfig()).event == "COMMENT"


def test_posted_and_declined_are_held_but_a_fixed_finding_that_returns_is_posted() -> None:
    posted, declined, fixed = fnd(1, Severity.MAJOR), fnd(2, Severity.MAJOR), fnd(3, Severity.MAJOR)
    known = {posted.fingerprint: "posted", declined.fingerprint: "declined",
             fixed.fingerprint: "fixed"}
    d = apply_policy([posted, declined, fixed], PolicyConfig(), known)
    assert [f.line for f in d.post] == [3]
    assert sorted((f.line, r) for f, r in d.held) == [
        (1, "already posted on an earlier head"), (2, "developer declined this earlier")]


def test_same_issue_twice_is_posted_once() -> None:
    a, b = fnd(1, Severity.MAJOR, quote="x = y"), fnd(9, Severity.MAJOR, quote="x  =  y")
    d = apply_policy([a, b], PolicyConfig())
    assert [f.line for f in d.post] == [1]
    assert [(f.line, r) for f, r in d.held] == [(9, "duplicate")]


def test_fingerprint_is_stable_across_line_moves_but_not_content() -> None:
    assert fnd(1, Severity.MAJOR, quote="x = y").fingerprint == \
        fnd(40, Severity.MAJOR, quote="x = y").fingerprint
    assert fnd(1, Severity.MAJOR, quote="x = y").fingerprint != \
        fnd(1, Severity.MAJOR, quote="x = z").fingerprint


def test_scanner_and_model_on_the_same_line_post_once_preferring_the_scanner() -> None:
    model = fnd(4, Severity.MAJOR, quote="q = f'...'", cat="injection")
    scanner = Finding(file="a.py", line=4, severity=Severity.MAJOR, category="sql-injection",
                      title="SQL built by string joining", body="b", evidence_quote="q = f'...'",
                      source="scanner:builtin", verified=True)
    d = apply_policy([model, scanner], PolicyConfig())
    assert [(f.source, f.line) for f in d.post] == [("scanner:builtin", 4)]
    assert [(f.source, r) for f, r in d.held] == [("model:x", "duplicate")]
