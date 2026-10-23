from reviewhound.config import RepoConfig, Severity
from reviewhound.scanners import BuiltinScanner
from reviewhound.vcs.base import DictSnapshot
from reviewhound.vcs.memory import diff_file_maps


def scan(base: dict[str, str], head: dict[str, str]) -> list[tuple[str, int, str]]:
    found = BuiltinScanner().scan(diff_file_maps(base, head), DictSnapshot(head), RepoConfig())
    return [(f.file, f.line, f.category) for f in found]


def test_hard_coded_secret_is_a_blocker_and_redacted_in_the_comment() -> None:
    head = {"s.py": 'A = 1\nSTRIPE_SECRET_KEY = "sk9Hf72kQpL0zXv3"\n'}
    found = BuiltinScanner().scan(diff_file_maps({"s.py": "A = 1\n"}, head), DictSnapshot(head),
                                  RepoConfig())
    assert [(f.line, f.category, f.severity) for f in found] == [(2, "secret", Severity.BLOCKER)]
    assert "sk9Hf72kQpL0zXv3" not in found[0].evidence_quote
    assert found[0].evidence_quote == 'STRIPE_SECRET_KEY = "sk9H[redacted]"'


def test_secret_lookalikes_are_not_flagged() -> None:
    head = {"s.py": 'TOKEN_URL = "https://auth.example.com/token"\n'
                    'PASSWORD = "changeme-please"\nAPI_KEY = os.environ["API_KEY"]\n'}
    assert scan({}, head) == []


def test_sql_built_from_variables_is_flagged_but_bound_parameters_are_not() -> None:
    head = {"q.py": 'a = db.execute(f"SELECT * FROM t WHERE n = \'{name}\'")\n'
                    'b = db.execute("SELECT * FROM t WHERE n = " + name)\n'
                    'c = db.execute("SELECT * FROM t WHERE n = ?", (name,))\n',
            "q.ts": "const r = await db.query(`DELETE FROM t WHERE id = ${id}`);\n"}
    assert sorted(scan({}, head)) == [("q.py", 1, "sql-injection"), ("q.py", 2, "sql-injection"),
                                      ("q.ts", 1, "sql-injection")]


def test_personal_data_in_logs_uses_configured_terms() -> None:
    head = {"l.py": 'logger.info("signup %s", user.email)\nlogger.info("signup %s", user.id)\n'}
    assert scan({}, head) == [("l.py", 1, "personal-data-in-logs")]


def test_only_lines_added_by_the_pr_are_scanned() -> None:
    old = 'db.execute(f"SELECT * FROM t WHERE n = {n}")\n'
    assert scan({"q.py": old}, {"q.py": old + "x = 1\n"}) == []


def test_semgrep_results_outside_added_lines_are_ignored(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import json
    import stat

    from reviewhound.scanners import SemgrepScanner
    results = {"results": [
        {"path": "ROOT/app.py", "start": {"line": line}, "check_id": "rule.x",
         "extra": {"severity": "ERROR", "message": f"bad {line}", "lines": "x"}}
        for line in (1, 2)]}
    fake = tmp_path / "semgrep"
    fake.write_text("#!/bin/sh\nfor a; do root=$a; done\n"
                    f"echo '{json.dumps(results)}' | sed \"s#ROOT#$root#g\"\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    scanner = SemgrepScanner()
    scanner.binary = str(fake)
    head = {"app.py": "old = 1\nnew = 2\n"}
    found = scanner.scan(diff_file_maps({"app.py": "old = 1\n"}, head), DictSnapshot(head),
                         RepoConfig())
    assert [(f.file, f.line, f.category, f.severity) for f in found] == [
        ("app.py", 2, "semgrep:rule.x", Severity.MAJOR)]
