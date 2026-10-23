"""GitHub App auth: sign a JWT with the app key, trade it for short-lived installation tokens."""

from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Protocol

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from reviewhound.vcs.github import API, GitHubError

log = logging.getLogger(__name__)
APP_ID_ENV = "REVIEWHOUND_APP_ID"
KEY_PATH_ENV = "REVIEWHOUND_APP_PRIVATE_KEY_PATH"
REFRESH_MARGIN_S = 300
_RETRY_STATUS = {429, 500, 502, 503, 504}


class GitHubAppConfigError(ValueError):
    """App credentials are missing, half-set, or unreadable."""


class GitHubApp:
    def __init__(self, app_id: str, private_key_pem: str, client: httpx.Client | None = None,
                 clock: Callable[[], float] = time.time, max_attempts: int = 3,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        if not app_id or not private_key_pem:
            raise GitHubAppConfigError("GitHub App id and private key are both required")
        try:
            key = load_pem_private_key(private_key_pem.encode(), password=None)
        except (ValueError, TypeError) as exc:
            raise GitHubAppConfigError("GitHub App key is not a valid RSA private key") from exc
        if not isinstance(key, RSAPrivateKey):
            raise GitHubAppConfigError("GitHub App key is not a valid RSA private key")
        self.app_id = app_id
        self._key = key
        self._http = client or httpx.Client(base_url=API, timeout=httpx.Timeout(30.0))
        self._clock = clock
        self._sleep = sleep
        self.max_attempts = max_attempts
        self._cache: dict[int, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return f"GitHubApp(app_id={self.app_id!r})"

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> GitHubApp | None:
        """None when no App is configured; an error when it is half-configured or unreadable."""
        app_id, key_path = env.get(APP_ID_ENV, ""), env.get(KEY_PATH_ENV, "")
        if not app_id and not key_path:
            return None
        if not app_id or not key_path:
            raise GitHubAppConfigError(f"set both {APP_ID_ENV} and {KEY_PATH_ENV}")
        try:
            pem = Path(key_path).read_text()
        except OSError as exc:
            raise GitHubAppConfigError(
                f"cannot read the key file in {KEY_PATH_ENV}: {type(exc).__name__}") from exc
        return cls(app_id, pem)

    def jwt(self) -> str:
        # iat is backdated 60s for clock drift; GitHub rejects an exp more than 10 minutes out.
        now = int(self._clock())
        return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": self.app_id}, self._key,
                          algorithm="RS256")

    def installation_token(self, installation_id: int) -> str:
        with self._lock:
            cached = self._cache.get(installation_id)
            if cached is not None and self._clock() < cached[1] - REFRESH_MARGIN_S:
                return cached[0]
            token, expires_at = self._exchange(installation_id)
            self._cache[installation_id] = (token, expires_at)
            return token

    def token_provider(self, installation_id: int) -> Callable[[], str]:
        return lambda: self.installation_token(installation_id)

    def _exchange(self, installation_id: int) -> tuple[str, float]:
        url = f"/app/installations/{installation_id}/access_tokens"
        for attempt in range(1, self.max_attempts + 1):
            try:
                resp = self._http.post(url, headers={"Authorization": f"Bearer {self.jwt()}",
                                                     "Accept": "application/vnd.github+json",
                                                     "X-GitHub-Api-Version": "2022-11-28"})
            except httpx.TransportError as exc:
                if attempt == self.max_attempts:
                    raise GitHubError(f"POST {url} failed: {type(exc).__name__}") from exc
            else:
                if resp.status_code < 400:
                    return self._parse(resp, url)
                if resp.status_code not in _RETRY_STATUS or attempt == self.max_attempts:
                    raise GitHubError(f"POST {url} -> HTTP {resp.status_code}", resp.status_code)
            log.warning("installation token request failed, retrying",
                        extra={"installation": installation_id, "attempt": attempt})
            self._sleep(2.0 ** (attempt - 1))
        raise GitHubError(f"POST {url} exhausted retries")

    @staticmethod
    def _parse(resp: httpx.Response, url: str) -> tuple[str, float]:
        try:
            d = resp.json()
            token = str(d["token"])
            expires = dt.datetime.fromisoformat(str(d["expires_at"])).timestamp()
        except (ValueError, KeyError, TypeError) as exc:
            raise GitHubError(f"POST {url}: malformed installation token response") from exc
        if not token:
            raise GitHubError(f"POST {url}: malformed installation token response")
        return token, expires


class _HasInstallation(Protocol):
    @property
    def installation_id(self) -> int | None: ...


def token_for_job(app: GitHubApp | None, job: _HasInstallation,
                  static_token: Callable[[], str]) -> str | Callable[[], str]:
    """The job's installation token when an App is configured, else the static token."""
    if app is not None and job.installation_id is not None:
        return app.token_provider(job.installation_id)
    return static_token()
