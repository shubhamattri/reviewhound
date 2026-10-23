import hashlib

from reviewhound.config import RepoConfig, Severity
from reviewhound.findings import Finding
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


def test_two_secrets_on_same_shaped_lines_get_different_fingerprints_without_raw_values() -> None:
    a, b = "sk9Hf72kQpL0zXv3", "sk9Hq81mWnB4cYt6"
    head = {"s.py": f'STRIPE_SECRET_KEY = "{a}"\n', "t.py": f'STRIPE_SECRET_KEY = "{a}"\n',
            "u.py": f'STRIPE_SECRET_KEY = "{b}"\n'}
    found = {f.file: f for f in BuiltinScanner().scan(diff_file_maps({}, head),
                                                       DictSnapshot(head), RepoConfig())}
    moved = Finding(file="s.py", line=9, severity=Severity.BLOCKER, category="secret",
                    title="t", body="b", evidence_quote=found["s.py"].evidence_quote,
                    source="scanner:builtin", identity=found["u.py"].identity)
    assert found["s.py"].evidence_quote == found["u.py"].evidence_quote
    assert found["s.py"].identity == found["t.py"].identity
    assert found["s.py"].identity != found["u.py"].identity
    assert len(found["s.py"].identity) == 16
    assert moved.fingerprint != found["s.py"].fingerprint
    assert moved.fingerprint == Finding(**{**vars(moved), "line": 1}).fingerprint
    for f in found.values():
        assert a not in repr(f) and b not in repr(f)


def test_fingerprint_without_identity_is_unchanged() -> None:
    f = Finding(file="a.py", line=3, severity=Severity.MAJOR, category="c", title="T",
                body="b", evidence_quote="x  =  1", source="model:m")
    assert f.identity == ""
    assert f.fingerprint == hashlib.sha256(b"a.py\x00c\x00x = 1").hexdigest()[:16]


def test_secret_lookalikes_are_not_flagged() -> None:
    head = {"s.py": 'TOKEN_URL = "https://auth.example.com/token"\n'
                    'PASS' 'WORD = "change' 'me-please"\nAPI_KEY = os.environ["API_KEY"]\n'}
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


def test_real_secret_shapes_with_slash_or_dot_are_flagged() -> None:
    head = {"k.py": 'aws_secret = "wJalrXUtnFEMI/K7MDENGbPxRfiCYzzzzzzzzzzz"\n'
                    'session_token = "' + "e" + 'yJhbGciOiJIUzI1.e' + 'yJzdWIiOiIxMjM0.SflKxwRJSMeKKF2QT4"\n'}
    assert scan({}, head) == [("k.py", 1, "secret"), ("k.py", 2, "secret")]


def test_messages_that_mention_sql_verbs_are_not_sql() -> None:
    head = {"m.py": 'raise Error(f"Failed to update {name}")\n'
                    'log(f"Could not delete {path}")\n'
                    'msg = "Select an option: " + choice\n',
            "m.ts": "throw new Error(`Cannot update ${id}`);\n"}
    assert scan({}, head) == []


def test_sql_with_interpolation_before_from_is_flagged() -> None:
    head = {"q.py": 'q = f"SELECT {cols} FROM users"\n'}
    assert scan({}, head) == [("q.py", 1, "sql-injection")]


def test_constant_sql_and_url_values_are_not_flagged() -> None:
    head = {"c.py": 'q = db.execute(f"SELECT id FROM users")\n'
                    'OAUTH_TOKEN_ENDPOINT = "https://login.acme.io/oauth2/token"\n'}
    assert scan({}, head) == []
