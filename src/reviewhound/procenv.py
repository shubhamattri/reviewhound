"""The environments child processes run with: allowlists, so no service credential leaks in."""

from __future__ import annotations

import os

# git and semgrep: a shell basics plus what a proxied or private-CA network needs.
SAFE_KEYS = ("PATH", "HOME", "LANG",
             "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy",
             "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE")
# `claude -p`: its login and config, never GITHUB_TOKEN, the DB URL, the webhook secret or an API key.
CLAUDE_KEYS = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "TMPDIR", "SHELL",
               "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR")


def _pick(keys: tuple[str, ...]) -> dict[str, str]:
    return {k: os.environ[k] for k in keys if k in os.environ}


def child_env(**extra: str) -> dict[str, str]:
    """SAFE_KEYS from our environment, plus exactly what the caller adds."""
    env = _pick(SAFE_KEYS)
    env.update(extra)
    return env


def claude_env() -> dict[str, str]:
    """CLAUDE_KEYS from our environment and nothing else."""
    return _pick(CLAUDE_KEYS)
