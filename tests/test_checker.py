import difflib
from pathlib import Path
from typing import Any

import pytest

from reviewhound.checker import ClaimChecker, find_quote
from reviewhound.config import Severity
from reviewhound.diff import parse_unified_diff
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
    assert reason == "evidence quote does not exist at head or on deleted lines"


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


DOTTED_BASE = {"m.py": '''class Repo:
    def save(self, x):
        return x


class Cache:
    def get(self, k):
        return k
'''}
DOTTED_HEAD = {"m.py": DOTTED_BASE["m.py"] + '''

def run(c):
    return c.get(1)
'''}


def dotted_checker() -> ClaimChecker:
    return ClaimChecker(diff_file_maps(DOTTED_BASE, DOTTED_HEAD), CodeIndex.build(DOTTED_HEAD))


def dotted(claim: Claim) -> tuple[str, str | None]:
    f, reason = dotted_checker().check(mf(file="m.py", line=12, evidence_quote="return c.get(1)",
                                          claims=[claim]), "m")
    return f.claim_results[0].verdict, reason


@pytest.mark.parametrize("claim, verdict, reason", [
    (Claim(kind="symbol_exists", name="Repo.save"), "true", None),
    (Claim(kind="symbol_exists", name="save"), "true", None),
    (Claim(kind="symbol_exists", name="Cache.save"), "false",
     "false claim: Cache has no member save"),
    (Claim(kind="calls", caller="Cache.save", callee="get"), "false",
     "false claim: function Cache.save does not exist"),
    (Claim(kind="not_calls", caller="Repo.save", callee="get"), "true", None),
    (Claim(kind="symbol_exists", name="requests.get"), "unknown", None),
])
def test_dotted_symbol_must_match_its_qualified_definition(claim: Claim, verdict: str,
                                                            reason: str | None) -> None:
    assert dotted(claim) == (verdict, reason)


def test_dotted_field_named_inside_its_owner_is_unknown_not_false() -> None:
    go_src = "package c\n\ntype Config struct {\n\tPort int\n}\n\nfunc Load() {}\n"
    idx = CodeIndex.build({"c.go": go_src, "m.py": DOTTED_HEAD["m.py"]})
    c = ClaimChecker(diff_file_maps({}, {"c.go": go_src}), idx)
    verdicts = [c.check_claim(Claim(kind="symbol_exists", name=n), "c.go").verdict
                for n in ("Config.Port", "Config.Host", "Cache.k", "Cache.save")]
    assert verdicts == ["unknown", "false", "unknown", "false"]


def test_dotted_names_with_braces_from_the_model_do_not_crash() -> None:
    assert dotted(Claim(kind="calls", caller="Cache.{x}", callee="get")) == (
        "false", "false claim: function Cache.{x} does not exist")
    assert dotted(Claim(kind="symbol_exists", name="Cache.{0}")) == (
        "false", "false claim: Cache has no member {0}")


def test_dotted_symbol_matches_a_nested_qualified_suffix() -> None:
    idx = CodeIndex.build({"n.py": "class A:\n    class B:\n        def go(self):\n"
                                   "            pass\n"})
    c = ClaimChecker(diff_file_maps({}, {"n.py": idx.sources["n.py"]}), idx)
    assert c.check_claim(Claim(kind="symbol_exists", name="B.go"), "n.py").verdict == "true"
    assert c.check_claim(Claim(kind="symbol_exists", name="A.B.go"), "n.py").verdict == "true"
    assert c.check_claim(Claim(kind="symbol_exists", name="A.go"), "n.py").verdict == "unknown"
    assert c.check_claim(Claim(kind="symbol_exists", name="A.run"), "n.py").verdict == "false"


def test_quote_spanning_blank_lines_matches_and_locates_the_changed_line() -> None:
    src = "def f():\n    a = 1\n\n    return a\n"
    assert find_quote(src, "a = 1\n\nreturn a") == [2]
    assert find_quote(src, "a = 1\nreturn a") == [2]
    base = {"q.py": "def f():\n    a = 1\n\n    return 0\n"}
    head = {"q.py": src}
    c = ClaimChecker(diff_file_maps(base, head), CodeIndex.build(head))
    f, reason = c.check(mf(file="q.py", line=1, evidence_quote="a = 1\nreturn a"), "m")
    assert (reason, f.line, f.verified) == (None, 4, True)


def test_claims_in_languages_the_index_does_not_cover_are_unknown_not_false() -> None:
    base = {"lib/pay.rb": "def charge(amount)\n  gateway.charge(amount)\nend\n"}
    head = {"lib/pay.rb": "def charge(amount)\n  gateway.charge(amount)\n  rescue nil\nend\n"}
    c = ClaimChecker(diff_file_maps(base, head), CodeIndex.build(head))
    f, reason = c.check(mf(file="lib/pay.rb", line=3, evidence_quote="rescue nil", claims=[
        Claim(kind="symbol_exists", name="charge"),
        Claim(kind="calls", caller="charge", callee="charge"),
        Claim(kind="quote", file="lib/gateway.rb", text="def charge"),
        Claim(kind="quote", file="lib/pay.rb", text="gateway.charge(amount)")]), "m")
    assert reason is None
    assert [r.verdict for r in f.claim_results] == ["unknown", "unknown", "unknown", "true"]
    assert f.verified is False


def test_covered_language_still_returns_false_for_missing_things() -> None:
    _, reason = checker().check(mf(claims=[Claim(kind="quote", file="lib/x.py", text="abc")]),
                                "m")
    assert reason == "false claim: file lib/x.py not found at head"


def test_index_limited_to_configured_languages_treats_others_as_uncovered() -> None:
    idx = CodeIndex(languages={"python"})
    idx.add("a.go", "package a\n\nfunc Run() {}\n")
    assert (idx.covers("a.go"), idx.covers("a.py"), idx.is_defined("Run")) == (False, True, False)
    assert idx.sources["a.go"] == "package a\n\nfunc Run() {}\n"


DEL_BASE = {"h.py": '''def delete_user(req, uid):
    require_role(req, "admin")
    audit(uid)
    db.delete(uid)
''', "gone.py": "def legacy_guard(req):\n    require_role(req, \"admin\")\n"}
DEL_HEAD = {"h.py": '''def delete_user(req, uid):
    audit(uid)
    db.delete(uid)
'''}


def del_checker(reader: Any = None) -> ClaimChecker:
    return ClaimChecker(diff_file_maps(DEL_BASE, DEL_HEAD), CodeIndex.build(DEL_HEAD),
                        base_reader=reader or DEL_BASE.get)


def test_quote_only_on_deleted_base_lines_is_kept_on_the_left_side() -> None:
    f, reason = del_checker().check(mf(file="h.py", line=2, severity="blocker",
                                       evidence_quote='require_role(req, "admin")'), "m")
    assert (reason, f.side, f.line, f.verified) == (None, "LEFT", 2, True)
    assert f.notes == ["on deleted base line 2"]


def test_quote_in_a_deleted_file_is_kept_on_the_left_side() -> None:
    f, reason = del_checker().check(mf(file="gone.py", line=1,
                                       evidence_quote='require_role(req, "admin")'), "m")
    assert (reason, f.side, f.line) == (None, "LEFT", 2)


def test_quote_found_nowhere_is_still_dropped_and_head_quotes_stay_right() -> None:
    _, reason = del_checker().check(mf(file="h.py", line=2, evidence_quote="require_staff(req)"),
                                    "m")
    assert reason == "evidence quote does not exist at head or on deleted lines"
    _, reason = del_checker().check(mf(file="h.py", line=2, evidence_quote="db.delete(uid)"), "m")
    assert reason == "evidence quote is only in code this PR did not change"
    _, reason = del_checker().check(mf(file="gone.py", line=1, evidence_quote=""), "m")
    assert reason == "gone.py is deleted by this PR and the finding quotes nothing from it"


def test_base_quote_counts_only_where_it_touches_a_deleted_line() -> None:
    base = {"k.py": "alpha = 1\nbeta = 2\ngamma = 3\n"}
    head = {"k.py": "alpha = 1\ngamma = 3\ndelta = 4\n"}
    c = ClaimChecker(diff_file_maps(base, head), CodeIndex.build(head), base_reader=base.get)
    f, reason = c.check(mf(file="k.py", line=1, evidence_quote="alpha = 1\nbeta = 2"), "m")
    assert (reason, f.side, f.line) == (None, "LEFT", 2)
    base = {"j.py": "first = 1\nsecond = 2\n"}
    head = {"j.py": "first = 1\nmiddle = 0\nsecond = 2\n"}
    c = ClaimChecker(diff_file_maps(base, head), CodeIndex.build(head), base_reader=base.get)
    _, reason = c.check(mf(file="j.py", line=2, evidence_quote="first = 1\nsecond = 2"), "m")
    assert reason == "evidence quote is only in code this PR did not change"


def test_removed_guard_still_present_in_other_unchanged_code_is_kept_left() -> None:
    base = {"g.py": 'def a(req):\n    require_role(req, "admin")\n    return 1\n\n\n'
                    'def b(req):\n    require_role(req, "admin")\n    return 2\n'}
    head = {"g.py": base["g.py"].replace('    require_role(req, "admin")\n    return 2',
                                         "    return 2")}
    c = ClaimChecker(diff_file_maps(base, head), CodeIndex.build(head), base_reader=base.get)
    f, reason = c.check(mf(file="g.py", line=6, evidence_quote='require_role(req, "admin")'),
                        "m")
    assert (reason, f.side, f.line) == (None, "LEFT", 7)


def test_base_that_does_not_line_up_with_the_diff_is_not_trusted() -> None:
    moved = {"h.py": "# a line the base branch added later\n" + DEL_BASE["h.py"]}
    c = del_checker(moved.get)
    _, reason = c.check(mf(file="h.py", line=2, evidence_quote='require_role(req, "admin")'), "m")
    assert reason == "base version of h.py does not match the diff"


@pytest.mark.parametrize("name, verdict", [(".save", "true"), ("Repo.save.", "true"),
                                           (".Cache.save", "false")])
def test_dotted_names_with_stray_dots_are_normalised(name: str, verdict: str) -> None:
    assert dotted(Claim(kind="symbol_exists", name=name))[0] == verdict


def test_renamed_file_reads_the_base_under_its_old_path() -> None:
    diff = parse_unified_diff("diff --git a/old.py b/new.py\nrename from old.py\n"
                              "rename to new.py\n--- a/old.py\n+++ b/new.py\n@@ -1,2 +1 @@\n"
                              " keep_this = 1\n-check_owner(user)\n")
    head = {"new.py": "keep_this = 1\n"}
    c = ClaimChecker(diff, CodeIndex.build(head),
                     base_reader={"old.py": "keep_this = 1\ncheck_owner(user)\n"}.get)
    f, reason = c.check(mf(file="new.py", line=2, evidence_quote="check_owner(user)"), "m")
    assert (reason, f.side, f.line) == (None, "LEFT", 2)


def test_base_read_failure_drops_with_a_reason_and_reads_each_path_once() -> None:
    calls: list[str] = []

    def broken(path: str) -> str | None:
        calls.append(path)
        raise TimeoutError("network")
    c = del_checker(broken)
    for _ in range(2):
        _, reason = c.check(mf(file="h.py", line=2, evidence_quote='require_role(req, "admin")'),
                            "m")
        assert reason == "base version of h.py could not be read (TimeoutError)"
    assert calls == ["h.py"]


def test_without_a_base_reader_deleted_line_quotes_are_dropped() -> None:
    c = ClaimChecker(diff_file_maps(DEL_BASE, DEL_HEAD), CodeIndex.build(DEL_HEAD))
    _, reason = c.check(mf(file="h.py", line=2, evidence_quote='require_role(req, "admin")'), "m")
    assert reason == "evidence quote does not exist at head or on deleted lines"


def test_trivial_quote_counts_as_no_evidence() -> None:
    f, reason = checker().check(mf(line=11, evidence_quote="i)", severity="major"), "model:x")
    assert reason is None
    assert f.verified is False
    assert f.severity is Severity.MINOR


# Live answers (eval cases 29 and 32): the model quoted the code around a deleted guard.
EVAL_CASES = Path(__file__).parent.parent / "evals" / "cases"
BESIDE = "anchored on context line {}, next to lines this PR deleted"
ONLY_UNCHANGED = "evidence quote is only in code this PR did not change"


def case_checker(name: str, path: str, with_base: bool = True) -> ClaimChecker:
    base = {path: (EVAL_CASES / name / "base" / path).read_text()}
    head = {path: (EVAL_CASES / name / "head" / path).read_text()}
    return ClaimChecker(diff_file_maps(base, head), CodeIndex.build(head),
                        base_reader=base.get if with_base else None)


def test_live_answer_quoting_code_after_a_removed_role_check_is_kept_beside_it() -> None:
    c = case_checker("29-py-removed-role-check", "app/orders.py")
    f, reason = c.check(mf(file="app/orders.py", line=5, severity="blocker",
                           evidence_quote='    orders.cancel(order_id)\n    return {"ok": True}'),
                        "m")
    assert (reason, f.side, f.line, f.verified) == (None, "RIGHT", 5, True)
    assert f.notes == [BESIDE.format(5)]


def test_live_answer_quoting_code_around_a_removed_unlock_is_kept_beside_it() -> None:
    c = case_checker("32-go-removed-deferred-unlock", "cache/cache.go")
    f, reason = c.check(mf(file="cache/cache.go", line=11, severity="blocker",
                           evidence_quote="c.mu.Lock()\n\tc.data[k] = v\n}"), "m")
    assert (reason, f.side, f.line, f.verified) == (None, "RIGHT", 11, True)
    assert f.notes == [BESIDE.format(11)]


def test_quote_beside_a_deletion_anchors_on_the_line_nearest_the_gap() -> None:
    c = case_checker("32-go-removed-deferred-unlock", "cache/cache.go")
    f, reason = c.check(mf(file="cache/cache.go", line=13, evidence_quote="c.data[k] = v\n}"),
                        "m")
    assert (reason, f.side, f.line) == (None, "RIGHT", 12)
    assert f.notes == [BESIDE.format(12)]


def test_beside_a_deletion_needs_no_base_reader() -> None:
    c = case_checker("29-py-removed-role-check", "app/orders.py", with_base=False)
    f, reason = c.check(mf(file="app/orders.py", line=4, evidence_quote="orders.cancel(order_id)"),
                        "m")
    assert (reason, f.side, f.line) == (None, "RIGHT", 5)


def wide_checker(base: str, head: str, context: int) -> ClaimChecker:
    body = difflib.unified_diff(base.splitlines(), head.splitlines(), fromfile="a/w.py",
                                tofile="b/w.py", lineterm="", n=context)
    diff = parse_unified_diff("\n".join(["diff --git a/w.py b/w.py", *body]) + "\n")
    return ClaimChecker(diff, CodeIndex.build({"w.py": head}), base_reader={"w.py": base}.get)


SPREAD = "".join(f"value_{i} = {i}\n" for i in range(1, 15))


def test_quote_of_unchanged_code_ten_lines_from_the_deletion_is_dropped() -> None:
    c = wide_checker(SPREAD, SPREAD.replace("value_3 = 3\n", ""), context=3)
    _, reason = c.check(mf(file="w.py", line=12, evidence_quote="value_13 = 13"), "m")
    assert reason == ONLY_UNCHANGED


@pytest.mark.parametrize("blanks, reason", [(3, None), (4, ONLY_UNCHANGED),
                                            (10, ONLY_UNCHANGED)])
def test_only_blank_lines_within_snap_distance_may_separate_quote_and_deletion(
        blanks: int, reason: str | None) -> None:
    base = "first = 1\nguard_call()\n" + "\n" * blanks + "after_blank = 2\n"
    c = wide_checker(base, base.replace("guard_call()\n", ""), context=15)
    f, got = c.check(mf(file="w.py", line=2, evidence_quote="after_blank = 2"), "m")
    assert got == reason
    if reason is None:
        assert (f.side, f.line, f.notes) == ("RIGHT", 2 + blanks, [BESIDE.format(2 + blanks)])


def test_code_between_the_quote_and_the_deletion_drops_it() -> None:
    _, reason = del_checker().check(mf(file="h.py", line=3, evidence_quote="db.delete(uid)"), "m")
    assert reason == ONLY_UNCHANGED


def test_quote_next_to_an_addition_only_hunk_is_dropped() -> None:
    base = "first = 1\nsecond = 2\nthird = 3\n"
    c = wide_checker(base, "first = 1\nsecond = 2\nadded = 9\nthird = 3\n", context=3)
    _, reason = c.check(mf(file="w.py", line=2, evidence_quote="second = 2"), "m")
    assert reason == ONLY_UNCHANGED


def test_quote_running_past_the_hunk_context_is_dropped() -> None:
    c = wide_checker(SPREAD, SPREAD.replace("value_3 = 3\n", ""), context=3)
    in_hunk = "value_4 = 4\nvalue_5 = 5\nvalue_6 = 6"
    _, reason = c.check(mf(file="w.py", line=3, evidence_quote=in_hunk), "m")
    assert reason is None
    _, reason = c.check(mf(file="w.py", line=3, evidence_quote=in_hunk + "\nvalue_7 = 7"), "m")
    assert reason == ONLY_UNCHANGED


def test_hallucinated_quote_beside_a_deletion_is_dropped() -> None:
    c = case_checker("29-py-removed-role-check", "app/orders.py")
    _, reason = c.check(mf(file="app/orders.py", line=5,
                           evidence_quote="orders.cancel(order_id, force=True)"), "m")
    assert reason == "evidence quote does not exist at head or on deleted lines"
