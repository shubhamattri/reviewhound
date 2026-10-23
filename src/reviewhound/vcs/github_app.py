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

from reviewhound.config import ConfigError
from reviewhound.vcs.github import API, GitHubError

log = logging.getLogger(__name__)
APP_ID_ENV = "REVIEWHOUND_APP_ID"
KEY_PATH_ENV = "REVIEWHOUND_APP_PRIVATE_KEY_PATH"
REFRESH_MARGIN_S = 300
_RETRY_STATUS = {429, 500, 502, 503, 504}


class GitHubAppConfigError(ValueError):
    """App credentials are missing, half-set, or unreadable."""


class AppNotInstalledError(ConfigError):
    """The App has no installation on the repository: retrying will not help."""


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
        self._login: str | None = None
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

    def bot_login(self) -> str:
        """The login the App's reviews carry: "<slug>[bot]", looked up once with the App JWT."""
        with self._lock:
            if self._login is None:
                try:
                    slug = self._call("GET", "/app").json()["slug"]
                except (ValueError, KeyError, TypeError) as exc:
                    raise GitHubError("GET /app: malformed app response") from exc
                if not isinstance(slug, str) or not slug:
                    raise GitHubError("GET /app: malformed app response")
                self._login = f"{slug}[bot]"
            return self._login

    def token_provider(self, installation_id: int) -> Callable[[], str]:
        return lambda: self.installation_token(installation_id)

    def installation_for_repo(self, repo: str) -> int:
        """The App's installation id on `repo`, for jobs queued without one (manual, sweep)."""
        url = f"/repos/{repo}/installation"
        try:
            resp = self._call("GET", url)
        except GitHubError as exc:
            if exc.status == 404:
                raise AppNotInstalledError(f"GitHub App {self.app_id} is not installed on {repo} "
                                           f"(GET {url} -> 404)") from exc
            raise
        try:
            installation = resp.json()["id"]
        except (ValueError, KeyError, TypeError) as exc:
            raise GitHubError(f"GET {url}: malformed installation response") from exc
        if isinstance(installation, bool) or not isinstance(installation, int):
            raise GitHubError(f"GET {url}: malformed installation response")
        log.info("installation looked up", extra={"repo": repo, "installation": installation})
        return installation

    def _exchange(self, installation_id: int) -> tuple[str, float]:
        url = f"/app/installations/{installation_id}/access_tokens"
        return self._parse(self._call("POST", url), url)

    def _call(self, method: str, url: str) -> httpx.Response:
        """An App-JWT request; 429/5xx and transport errors are retried, other 4xx are not."""
        for attempt in range(1, self.max_attempts + 1):
            try:
                resp = self._http.request(method, url, headers={
                    "Authorization": f"Bearer {self.jwt()}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28"})
            except httpx.TransportError as exc:
                if attempt == self.max_attempts:
                    raise GitHubError(f"{method} {url} failed: {type(exc).__name__}") from exc
            else:
                if resp.status_code < 400:
                    return resp
                if resp.status_code not in _RETRY_STATUS or attempt == self.max_attempts:
                    raise GitHubError(f"{method} {url} -> HTTP {resp.status_code}",
                                      resp.status_code)
            log.warning("GitHub App request failed, retrying",
                        extra={"method": method, "url": url, "attempt": attempt})
            self._sleep(2.0 ** (attempt - 1))
        raise GitHubError(f"{method} {url} exhausted retries")

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
    def repo(self) -> str: ...

    @property
    def installation_id(self) -> int | None: ...


def token_for_job(app: GitHubApp | None, job: _HasInstallation,
                  static_token: Callable[[], str]) -> str | Callable[[], str]:
    """With an App, the job's installation token (looked up by repo if the job has no id);
    without one, the static token."""
    if app is None:
        return static_token()
    installation = job.installation_id
    if installation is None:
        installation = app.installation_for_repo(job.repo)
    return app.token_provider(installation)
