from pathlib import Path
from typing import Any, Self

import pytest

from qa import login


class FakePage:
    def goto(self, url: str) -> None:
        self.url = url

    def wait_for_timeout(self, ms: int) -> None:
        pass


class FakeContext:
    def __init__(self, cookie_rounds: list[list[dict[str, str]]]) -> None:
        self.rounds = cookie_rounds
        self.saved: list[str] = []

    def new_page(self) -> FakePage:
        return FakePage()

    def cookies(self, url: str) -> list[dict[str, str]]:
        return self.rounds.pop(0) if len(self.rounds) > 1 else self.rounds[0]

    def storage_state(self, path: str) -> None:
        Path(path).write_text("{}")
        self.saved.append(path)


class FakePlaywright:
    def __init__(self, ctx: FakeContext) -> None:
        self.ctx = ctx
        self.chromium = self

    def launch(self, headless: bool) -> Any:
        return self

    def new_context(self) -> FakeContext:
        return self.ctx

    def close(self) -> None:
        pass

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *a: object) -> None:
        pass


def patch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ctx: FakeContext) -> None:
    import playwright.sync_api
    monkeypatch.setattr(playwright.sync_api, "sync_playwright", lambda: FakePlaywright(ctx))
    monkeypatch.setattr(login, "QA_HOME", tmp_path)


def test_login_is_saved_only_after_github_says_signed_in(tmp_path: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = FakeContext([[{"name": "logged_in", "value": "no"}],
                       [{"name": "logged_in", "value": "yes"}, {"name": "dotcom_user", "value": "me"}]])
    patch(monkeypatch, tmp_path, ctx)
    assert login.main(timeout_s=60) == 0
    target = tmp_path / "github-state.json"
    assert ctx.saved == [str(target)]
    assert oct(target.stat().st_mode & 0o777) == "0o600"


def test_no_login_before_timeout_saves_nothing(tmp_path: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = FakeContext([[{"name": "logged_in", "value": "no"}]])
    patch(monkeypatch, tmp_path, ctx)
    assert login.main(timeout_s=0) == 1
    assert ctx.saved == []
    assert not (tmp_path / "github-state.json").exists()
