from typing import Any

import pytest

from reviewhound.checker import ClaimChecker, find_quote
from reviewhound.config import Severity
from reviewhound.findings import Claim, ModelFinding
from reviewhound.index.treesitter import CodeIndex
from reviewhound.vcs.memory import diff_file_maps

BASE = {"app.py": '''def guard(req):
    pass


def get_item(req, i):
    guard(req)
    return i
'''}
HEAD = {"app.py": BASE["app.py"] + '''

def delete_item(req, i):
    store.remove(i)
    return True
'''}
# head lines 1-7 as base; added: 8-9 blank, 10 def delete_item, 11 store.remove(i), 12 return


def checker() -> ClaimChecker:
    return ClaimChecker(diff_file_maps(BASE, HEAD), CodeIndex.build(HEAD), snap_distance=3)


def mf(**kw: Any) -> ModelFinding:
    base: dict[str, Any] = {"file": "app.py", "line": 11, "severity": "major", "title": "t",
                            "body": "b", "evidence_quote": "store.remove(i)", "claims": []}
    base.update(kw)
    return ModelFinding.model_validate(base)


def test_find_quote_ignores_whitespace_and_needs_contiguous_lines() -> None:
    src = "a = 1\nif  x :\n    y()\nz = 2\n"
    assert find_quote(src, "if x :\n y()") == [2]
    assert find_quote(src, "if x :\nz = 2") == []
    assert find_quote(src, "y()") == [3]
    assert find_quote(src, "   ") == []


def test_true_finding_survives_and_is_verified() -> None:
    f, reason = checker().check(mf(claims=[
        Claim(kind="not_calls", caller="delete_item", callee="guard"),
        Claim(kind="calls", caller="get_item", callee="guard"),
        Claim(kind="symbol_exists", name="get_item"),
        Claim(kind="quote", file="app.py", text="guard(req)")]), "model:x")
    assert reason is None
    assert f.verified is True
    assert [r.verdict for r in f.claim_results] == ["true", "true", "true", "true"]


@pytest.mark.parametrize("claim, why", [
    (Claim(kind="not_calls", caller="get_item", callee="guard"), "false claim: get_item does call guard"),
    (Claim(kind="calls", caller="delete_item", callee="guard"), "false claim: delete_item never calls guard"),
    (Claim(kind="symbol_exists", name="authorize"), "false claim: symbol authorize does not exist"),
    (Claim(kind="quote", text="guard(request)"), "false claim: quoted text not found in app.py"),
    (Claim(kind="calls", caller="remove_all", callee="x"), "false claim: function remove_all does not exist"),
])
def test_one_false_claim_drops_the_finding(claim: Claim, why: str) -> None:
    _, reason = checker().check(mf(claims=[claim]), "model:x")
    assert reason == why


def test_invented_evidence_quote_drops_the_finding() -> None:
    _, reason = checker().check(mf(evidence_quote="store.purge(i)"), "model:x")
    assert reason == "evidence quote does not exist at head"


def test_evidence_only_in_unchanged_code_is_dropped_even_if_line_is_changed() -> None:
    _, reason = checker().check(mf(line=11, evidence_quote="guard(req)\n    return i"), "model:x")
    assert reason == "evidence quote is only in code this PR did not change"


def test_finding_moves_to_the_quoted_changed_line() -> None:
    f, reason = checker().check(mf(line=12, evidence_quote="store.remove(i)"), "model:x")
    assert reason is None
    assert f.line == 11
    assert f.notes == ["moved to quoted line 11"]


def test_no_evidence_lowers_severity_and_is_not_verified() -> None:
    f, reason = checker().check(mf(line=12, evidence_quote="", severity="blocker"), "model:x")
    assert reason is None
    assert f.severity is Severity.MAJOR
    assert f.verified is False


def test_no_evidence_on_unchanged_line_far_from_diff_is_dropped() -> None:
    _, reason = checker().check(mf(line=2, evidence_quote=""), "model:x")
    assert reason == "line 2 is not changed by this PR"


def test_no_evidence_near_diff_snaps_to_changed_line() -> None:
    f, reason = checker().check(mf(line=5, evidence_quote=""), "model:x")
    assert reason is None
    assert f.line == 8


def test_finding_in_file_outside_the_pr_is_dropped() -> None:
    _, reason = checker().check(mf(file="other.py"), "model:x")
    assert reason == "other.py is not a changed file in this PR"


def test_unknown_claim_keeps_finding_but_unverified() -> None:
    idx = CodeIndex.build({**HEAD, "b.py": "def get_item():\n    pass\n"})
    c = ClaimChecker(diff_file_maps(BASE, HEAD), idx)
    f, reason = c.check(mf(claims=[Claim(kind="calls", caller="get_item", callee="guard")]), "m")
    assert reason is None
    assert f.claim_results[0].verdict == "unknown"
    assert f.verified is False


def test_trivial_quote_counts_as_no_evidence() -> None:
    f, reason = checker().check(mf(line=11, evidence_quote="i)", severity="major"), "model:x")
    assert reason is None
    assert f.verified is False
    assert f.severity is Severity.MINOR
