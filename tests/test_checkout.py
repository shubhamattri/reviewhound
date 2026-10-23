import threading
from pathlib import Path

import pytest

from reviewhound.vcs.github import GitHubError, GitHubVCS

SHA = "abc123"


class FakeFetchVCS(GitHubVCS):
    """GitHubVCS whose fetch writes files instead of running git, and can be paused mid-way."""

    def __init__(self, cache: Path, label: str, gate: threading.Event | None = None,
                 started: threading.Event | None = None, fail: bool = False) -> None:
        super().__init__("token", cache)
        self.label, self.gate, self.started, self.fail = label, gate, started, fail
        self.fetch_dirs: list[Path] = []

    def _fetch_into(self, path: Path, repo: str, sha: str) -> None:
        self.fetch_dirs.append(path)
        (path / "app.py").write_text(f"written by {self.label}\n")
        if self.started is not None:
            self.started.set()
        if self.gate is not None:
            assert self.gate.wait(5)
        if self.fail:
            raise GitHubError("checkout step fetch failed with code 128")


def dest(cache: Path) -> Path:
    return cache / "acme__shop" / SHA


def leftovers(cache: Path) -> list[str]:
    """Temp checkout dirs still on disk (the lock file is kept on purpose)."""
    return sorted(p.name for p in (cache / "acme__shop").iterdir()
                  if p.name.startswith(f".{SHA}.") and p.is_dir())


def test_second_worker_does_not_delete_a_checkout_in_progress(tmp_path: Path) -> None:
    gate, started = threading.Event(), threading.Event()
    slow = FakeFetchVCS(tmp_path, "A", gate=gate, started=started)
    fast = FakeFetchVCS(tmp_path, "B")
    result: dict[str, Path] = {}
    t = threading.Thread(target=lambda: result.update(a=slow._checkout("acme/shop", SHA)))
    t.start()
    assert started.wait(5)
    in_progress = slow.fetch_dirs[0]
    assert fast._checkout("acme/shop", SHA) == dest(tmp_path)
    assert (in_progress / "app.py").read_text() == "written by A\n"  # B left A's work alone
    gate.set()
    t.join(5)
    assert result["a"] == dest(tmp_path)
    assert (dest(tmp_path) / "app.py").read_text() == "written by B\n"
    assert (dest(tmp_path) / ".reviewhound-complete").read_text() == SHA
    assert leftovers(tmp_path) == []


def test_concurrent_checkouts_of_one_sha_both_succeed(tmp_path: Path) -> None:
    barrier = threading.Barrier(4)
    results: list[Path] = []
    errors: list[BaseException] = []

    def run(i: int) -> None:
        vcs = FakeFetchVCS(tmp_path, f"w{i}")
        try:
            barrier.wait(5)
            results.append(vcs._checkout("acme/shop", SHA))
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assert below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert errors == []
    assert results == [dest(tmp_path)] * 4
    assert (dest(tmp_path) / ".reviewhound-complete").read_text() == SHA
    assert leftovers(tmp_path) == []


def test_complete_checkout_is_reused_without_fetching(tmp_path: Path) -> None:
    first = FakeFetchVCS(tmp_path, "A")
    first._checkout("acme/shop", SHA)
    second = FakeFetchVCS(tmp_path, "B")
    assert second._checkout("acme/shop", SHA) == dest(tmp_path)
    assert second.fetch_dirs == []
    assert (dest(tmp_path) / "app.py").read_text() == "written by A\n"


def test_failed_fetch_leaves_nothing_behind(tmp_path: Path) -> None:
    vcs = FakeFetchVCS(tmp_path, "A", fail=True)
    with pytest.raises(GitHubError, match="fetch failed"):
        vcs._checkout("acme/shop", SHA)
    assert not dest(tmp_path).exists()
    assert leftovers(tmp_path) == []


def test_half_done_checkout_from_an_older_version_is_replaced(tmp_path: Path) -> None:
    stale = dest(tmp_path)
    stale.mkdir(parents=True)
    (stale / "partial.txt").write_text("interrupted")
    assert FakeFetchVCS(tmp_path, "A")._checkout("acme/shop", SHA) == stale
    assert sorted(p.name for p in stale.iterdir()) == [".reviewhound-complete", "app.py"]
