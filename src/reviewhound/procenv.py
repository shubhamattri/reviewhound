"""The environment child processes (git, semgrep) run with: nothing that carries a credential."""

from __future__ import annotations

import os

SAFE_KEYS = ("PATH", "HOME", "LANG")


def child_env(**extra: str) -> dict[str, str]:
    """PATH, HOME and LANG from our environment, plus exactly what the caller adds."""
    env = {k: os.environ[k] for k in SAFE_KEYS if k in os.environ}
    env.update(extra)
    return env
