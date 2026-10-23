"""Save a GitHub browser login for the QA run.

Opens your normal Google Chrome with its own empty profile. Nothing automates the window while you
sign in (Google refuses sign-in in automated browsers); the script only reads cookies over the
debugging port and closes Chrome once GitHub says you are signed in.

Run: uv run python -m qa.login
"""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from qa.github_qa import QA_HOME

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PORT = 9333


def signed_in_user(cookies: list[dict[str, Any]]) -> str:
    jar = {c["name"]: c["value"] for c in cookies if "github.com" in c.get("domain", "")}
    return jar.get("dotcom_user", "") if jar.get("logged_in") == "yes" else ""


def read_state(port: int) -> dict[str, Any] | None:
    """Storage state of the running Chrome, or None while it is not reachable yet."""
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=5000)
            state: dict[str, Any] = dict(browser.contexts[0].storage_state())
            return state
    except PlaywrightError:
        return None


def wait_for_login(read: Callable[[], dict[str, Any] | None], target: Path, timeout_s: float,
                   sleep: Callable[[float], None] = time.sleep,
                   clock: Callable[[], float] = time.monotonic) -> str:
    deadline = clock() + timeout_s
    while clock() < deadline:
        state = read()
        user = signed_in_user(state.get("cookies", [])) if state else ""
        if state and user:
            import json
            target.write_text(json.dumps(state))
            target.chmod(0o600)
            return user
        sleep(3)
    return ""


def main(timeout_s: float = 600) -> int:
    QA_HOME.mkdir(parents=True, exist_ok=True)
    profile = QA_HOME / "chrome-profile"
    chrome = subprocess.Popen([CHROME, f"--user-data-dir={profile}",
                               f"--remote-debugging-port={PORT}", "--no-first-run",
                               "--no-default-browser-check", "https://github.com/login"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("Sign in to GitHub in the Chrome window that opened (Google sign-in works there).\n"
          "It closes by itself once you are in. Waiting up to 10 minutes.")
    try:
        user = wait_for_login(lambda: read_state(PORT), QA_HOME / "github-state.json", timeout_s)
    finally:
        chrome.terminate()
        chrome.wait(timeout=15)
    if not user:
        print("Timed out without a login; nothing saved.", file=sys.stderr)
        return 1
    print(f"Saved login for {user} to {QA_HOME / 'github-state.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
