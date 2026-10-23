from pathlib import Path

from reviewhound.evals import run_eval, score

CASES = Path(__file__).parent.parent / "evals" / "cases"


def test_score_matches_each_expected_once_within_tolerance() -> None:
    expected = [{"file": "a.py", "line": 10}, {"file": "b.py", "line": 3}]
    tp, misses, false_alarms = score(expected, [("a.py", 12), ("a.py", 11), ("c.py", 1)])
    assert tp == 1
    assert misses == ["b.py:3"]
    assert false_alarms == ["a.py:11", "c.py:1"]


def test_recorded_eval_set_scores_and_checker_earns_its_keep() -> None:
    with_checker = run_eval(CASES)
    assert len(with_checker.cases) == 12
    assert (round(with_checker.precision, 2), round(with_checker.recall, 2)) == (1.0, 1.0)
    assert sum(c.dropped_by_checker for c in with_checker.cases) == 8
    baseline = run_eval(CASES, verify=False)
    assert round(baseline.precision, 2) == 0.67
