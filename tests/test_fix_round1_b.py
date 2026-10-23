"""Fix round 1, track B: checker anchoring, fingerprints, child env, store ordering."""

from __future__ import annotations

from typing import Any

from reviewhound.checker import ClaimChecker
from reviewhound.findings import Finding, ModelFinding
from reviewhound.index.treesitter import CodeIndex
from reviewhound.vcs.memory import diff_file_maps

# B7: a blank line added or removed inside a quote must not anchor unchanged code.
SPAN_BASE = {"m.py": "def f():\n    first_call_here()\n    second_call_here()\n    return 1\n"}
SPAN_HEAD = {"m.py": "def f():\n    first_call_here()\n\n    second_call_here()\n    return 1\n"}
SPAN_QUOTE = "first_call_here()\nsecond_call_here()"


def span_finding(line: int) -> ModelFinding:
    return ModelFinding.model_validate({
        "file": "m.py", "line": line, "severity": "major", "title": "t", "body": "b",
        "evidence_quote": SPAN_QUOTE})


def test_blank_line_added_inside_a_quote_does_not_anchor_it() -> None:
    chk = ClaimChecker(diff_file_maps(SPAN_BASE, SPAN_HEAD), CodeIndex.build(SPAN_HEAD))
    _f, reason = chk.check(span_finding(3), "model:m")
    assert reason == "evidence quote is only in code this PR did not change"


def test_blank_line_deleted_inside_a_quote_does_not_anchor_it_on_the_left() -> None:
    chk = ClaimChecker(diff_file_maps(SPAN_HEAD, SPAN_BASE), CodeIndex.build(SPAN_BASE),
                       base_reader=SPAN_HEAD.get)
    f, reason = chk.check(span_finding(3), "model:m")
    assert (reason, f.side) == ("evidence quote is only in code this PR did not change", "RIGHT")


def test_a_real_change_inside_the_quote_still_anchors_on_its_non_blank_line() -> None:
    head = {"m.py": SPAN_HEAD["m.py"].replace("second_call_here()", "second_call_here()  # x")}
    chk = ClaimChecker(diff_file_maps(SPAN_BASE, head), CodeIndex.build(head))
    f, reason = chk.check(span_finding(3), "model:m")
    assert (reason, f.line, f.side) == (None, 4, "RIGHT")


# B8: LEFT and RIGHT findings with the same quote are different findings.

def _fp(side: str, **kw: Any) -> str:
    f = Finding(file="app/admin.py", line=11, severity=4,  # type: ignore[arg-type]
                category="auth", title="t", body="", evidence_quote="db.delete(uid)", source="",
                side=side, **kw)  # type: ignore[arg-type]
    return f.fingerprint


def test_left_side_is_part_of_the_fingerprint_and_right_stays_stable() -> None:
    assert _fp("RIGHT") == "68eb534220237c8b"  # the value before side existed
    assert _fp("LEFT") != _fp("RIGHT")
    assert len(_fp("LEFT")) == 16
    assert _fp("LEFT", identity="abc") != _fp("RIGHT", identity="abc")
