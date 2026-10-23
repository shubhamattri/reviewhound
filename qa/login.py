"""Save a GitHub browser login for the QA run. Opens a window; log in; it saves and closes itself.

Run: uv run python qa/login.py
"""

from __future__ import annotations

import sys
import time

from qa.github_qa import QA_HOME


def main(timeout_s: float = 300) -> int:
    from playwright.sync_api import sync_playwright

    QA_HOME.mkdir(parents=True, exist_ok=True)
    target = QA_HOME / "github-state.json"
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto("https://github.com/login")
        print("Log in to GitHub in the window that opened. It closes by itself when you are in.")
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            cookies = {c["name"]: c["value"] for c in ctx.cookies("https://github.com")}
            if cookies.get("logged_in") == "yes" and cookies.get("dotcom_user"):
                ctx.storage_state(path=str(target))
                target.chmod(0o600)
                print(f"Saved login for {cookies['dotcom_user']} to {target}")
                browser.close()
                return 0
            page.wait_for_timeout(2000)
        browser.close()
    print("Timed out without a login; nothing saved.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
