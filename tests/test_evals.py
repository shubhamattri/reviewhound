from pathlib import Path

from reviewhound.evals import run_eval, score

CASES = Path(__file__).parent.parent / "evals" / "cases"


def test_score_matches_each_expected_once_within_tolerance() -> None:
    expected = [{"file": "a.py", "line": 10}, {"file": "b.py", "line": 3}]
    tp, misses, false_alarms = score(expected, [("a.py", 12, "RIGHT"), ("a.py", 11, "RIGHT"),
                                                ("c.py", 1, "RIGHT")])
    assert tp == 1
    assert misses == ["b.py:3"]
    assert false_alarms == ["a.py:11", "c.py:1"]


def test_score_requires_the_same_side() -> None:
    expected = [{"file": "a.py", "line": 5, "side": "LEFT"}]
    assert score(expected, [("a.py", 5, "RIGHT")]) == (0, ["a.py:5:LEFT"], ["a.py:5"])
    assert score(expected, [("a.py", 6, "LEFT")]) == (1, [], [])


def test_recorded_eval_set_scores_and_checker_earns_its_keep() -> None:
    with_checker = run_eval(CASES)
    assert len(with_checker.cases) == 32
    assert sum(c.expected for c in with_checker.cases) == 27
    assert (round(with_checker.precision, 2), round(with_checker.recall, 2)) == (1.0, 1.0)
    assert sum(c.dropped_by_checker for c in with_checker.cases) == 32
    assert [c.error for c in with_checker.cases if c.error] == []
    baseline = run_eval(CASES, verify=False)
    assert (sum(c.reported for c in baseline.cases),
            sum(c.true_positives for c in baseline.cases)) == (53, 25)
    assert round(baseline.precision, 2) == 0.47


def test_committed_cases_match_what_the_generator_writes(tmp_path: Path) -> None:
    from evals import build_cases
    build_cases.main(tmp_path / "cases")

    def tree(root: Path) -> dict[str, str]:
        return {p.relative_to(root).as_posix(): p.read_text() for p in root.rglob("*")
                if p.is_file()}
    assert tree(tmp_path / "cases") == tree(CASES)


def test_eval_set_covers_each_language_and_trap_kind() -> None:
    names = sorted(p.name for p in CASES.iterdir() if (p / "case.json").exists())
    seeded = {lang: [n for n in names if f"-{lang}-" in n and "clean" not in n]
              for lang in ("js", "go")}
    assert (len(seeded["js"]), len(seeded["go"])) == (6, 7)
    assert len([n for n in names if "clean" in n]) == 6
    assert {"18-js-two-secrets-same-shape", "29-py-removed-role-check",
            "32-go-removed-deferred-unlock", "27-py-clean-dotted-symbol-trap"} <= set(names)
