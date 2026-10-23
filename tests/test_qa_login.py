import json
from pathlib import Path
from typing import Any

from qa.login import signed_in_user, wait_for_login

YES = [{"name": "logged_in", "value": "yes", "domain": ".github.com"},
       {"name": "dotcom_user", "value": "me", "domain": ".github.com"}]


def test_signed_in_needs_both_github_cookies() -> None:
    assert signed_in_user(YES) == "me"
    assert signed_in_user([{**YES[0], "value": "no"}, YES[1]]) == ""
    assert signed_in_user([YES[0]]) == ""
    assert signed_in_user([{**c, "domain": ".evil.example"} for c in YES]) == ""


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


def test_login_is_saved_only_once_github_says_signed_in(tmp_path: Path) -> None:
    states: list[dict[str, Any] | None] = [None, {"cookies": [{**YES[0], "value": "no"}]},
                                           {"cookies": YES, "origins": []}]
    clock = Clock()
    target = tmp_path / "state.json"
    user = wait_for_login(lambda: states.pop(0), target, timeout_s=60, sleep=clock.sleep,
                          clock=clock)
    assert user == "me"
    assert json.loads(target.read_text()) == {"cookies": YES, "origins": []}
    assert oct(target.stat().st_mode & 0o777) == "0o600"
    assert clock.t == 6


def test_no_login_before_timeout_saves_nothing(tmp_path: Path) -> None:
    clock = Clock()
    target = tmp_path / "state.json"
    assert wait_for_login(lambda: None, target, timeout_s=9, sleep=clock.sleep, clock=clock) == ""
    assert not target.exists()
