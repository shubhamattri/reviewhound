import json
import logging
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from reviewhound.vcs.github import GitHubError, GitHubVCS
from reviewhound.vcs.github_app import GitHubApp, GitHubAppConfigError
from tests.conftest import FakeClock

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                        serialization.NoEncryption()).decode()
PUBLIC = KEY.public_key()
T0 = 1_800_000_000.0  # 2027-01-15T08:00:00Z


def iso(ts: float) -> str:
    import datetime as dt
    return dt.datetime.fromtimestamp(ts, dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class TokenServer:
    def __init__(self, ttl_s: float = 3600, statuses: list[int] | None = None) -> None:
        self.ttl_s = ttl_s
        self.statuses = list(statuses or [])
        self.calls: list[httpx.Request] = []
        self.clock = FakeClock(T0)

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        if self.statuses:
            return httpx.Response(self.statuses.pop(0), json={"message": "nope"})
        n = len(self.calls)
        return httpx.Response(201, json={"token": f"ghs_tok{n}",
                                         "expires_at": iso(self.clock() + self.ttl_s)})


def make_app(server: TokenServer) -> GitHubApp:
    client = httpx.Client(transport=httpx.MockTransport(server), base_url="https://api.github.com")
    return GitHubApp("12345", PEM, client=client, clock=server.clock, sleep=lambda s: None)


def test_jwt_is_rs256_signed_with_app_id_and_short_lived() -> None:
    server = TokenServer()
    token = make_app(server).jwt()
    assert jwt.get_unverified_header(token)["alg"] == "RS256"
    claims = jwt.decode(token, PUBLIC, algorithms=["RS256"],
                        options={"verify_exp": False, "verify_iat": False})
    assert claims == {"iss": "12345", "iat": int(T0) - 60, "exp": int(T0) + 540}


def test_installation_token_exchange_uses_jwt_bearer() -> None:
    server = TokenServer()
    assert make_app(server).installation_token(99) == "ghs_tok1"
    req = server.calls[0]
    assert (req.method, req.url.path) == ("POST", "/app/installations/99/access_tokens")
    bearer = req.headers["Authorization"].removeprefix("Bearer ")
    assert jwt.decode(bearer, PUBLIC, algorithms=["RS256"],
                      options={"verify_exp": False, "verify_iat": False})["iss"] == "12345"


def test_token_cached_until_five_minutes_before_expiry() -> None:
    server = TokenServer(ttl_s=3600)
    app = make_app(server)
    assert app.installation_token(99) == "ghs_tok1"
    server.clock.now = T0 + 3600 - 301
    assert app.installation_token(99) == "ghs_tok1"
    assert len(server.calls) == 1
    server.clock.now = T0 + 3600 - 300
    assert app.installation_token(99) == "ghs_tok2"
    assert len(server.calls) == 2


def test_tokens_are_cached_per_installation() -> None:
    server = TokenServer()
    app = make_app(server)
    assert (app.installation_token(1), app.installation_token(2)) == ("ghs_tok1", "ghs_tok2")
    assert app.installation_token(1) == "ghs_tok1"
    assert [r.url.path for r in server.calls] == ["/app/installations/1/access_tokens",
                                                  "/app/installations/2/access_tokens"]


def test_server_error_is_retried_and_auth_error_is_not() -> None:
    server = TokenServer(statuses=[502])
    assert make_app(server).installation_token(5) == "ghs_tok2"
    server = TokenServer(statuses=[401])
    with pytest.raises(GitHubError) as err:
        make_app(server).installation_token(5)
    assert err.value.status == 401
    assert len(server.calls) == 1


def test_bad_token_response_raises_and_caches_nothing() -> None:
    calls: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(201, json={"token": "ghs_x", "expires_at": "soon"})
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.github.com")
    app = GitHubApp("1", PEM, client=client, clock=FakeClock(T0), sleep=lambda s: None)
    for _ in range(2):
        with pytest.raises(GitHubError, match="malformed installation token response"):
            app.installation_token(3)
    assert len(calls) == 2


def test_secrets_never_reach_logs_or_repr(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    server = TokenServer(statuses=[503])
    app = make_app(server)
    app.installation_token(9)
    text = caplog.text + repr(app) + json.dumps([r.__dict__ for r in caplog.records], default=str)
    assert "PRIVATE KEY" not in text and "ghs_tok" not in text
    assert app.jwt() not in text


def test_from_env(tmp_path: Path) -> None:
    key = tmp_path / "app.pem"
    key.write_text(PEM)
    assert GitHubApp.from_env({}) is None
    app = GitHubApp.from_env({"REVIEWHOUND_APP_ID": "77",
                              "REVIEWHOUND_APP_PRIVATE_KEY_PATH": str(key)})
    assert app is not None and app.app_id == "77"
    with pytest.raises(GitHubAppConfigError, match="both"):
        GitHubApp.from_env({"REVIEWHOUND_APP_ID": "77"})
    with pytest.raises(GitHubAppConfigError, match="cannot read"):
        GitHubApp.from_env({"REVIEWHOUND_APP_ID": "77",
                            "REVIEWHOUND_APP_PRIVATE_KEY_PATH": str(tmp_path / "missing.pem")})
    key.write_text("not a key")
    with pytest.raises(GitHubAppConfigError, match="not a valid RSA private key"):
        GitHubApp.from_env({"REVIEWHOUND_APP_ID": "77",
                            "REVIEWHOUND_APP_PRIVATE_KEY_PATH": str(key)})


# GitHubVCS with a token provider

def vcs_with(provider: Any, tmp_path: Path, seen: list[str]) -> GitHubVCS:
    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.headers["Authorization"])
        return httpx.Response(200, json={"title": "t", "body": "", "base": {"sha": "b"},
                                         "head": {"sha": "h"}, "user": {"login": "u"}})
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.github.com")
    return GitHubVCS(provider, tmp_path, client=client)


def test_vcs_asks_the_provider_on_every_request(tmp_path: Path) -> None:
    tokens = iter(["ghs_a", "ghs_b"])
    seen: list[str] = []
    vcs = vcs_with(lambda: next(tokens), tmp_path, seen)
    vcs.get_pull("acme/shop", 1)
    vcs.get_pull("acme/shop", 1)
    assert seen == ["Bearer ghs_a", "Bearer ghs_b"]


def test_vcs_refreshes_an_expired_app_token_transparently(tmp_path: Path) -> None:
    server = TokenServer(ttl_s=600)
    app = make_app(server)
    seen: list[str] = []
    vcs = vcs_with(app.token_provider(42), tmp_path, seen)
    vcs.get_pull("acme/shop", 1)
    server.clock.now = T0 + 600
    vcs.get_pull("acme/shop", 1)
    assert seen == ["Bearer ghs_tok1", "Bearer ghs_tok2"]


def test_git_checkout_uses_the_current_token(tmp_path: Path) -> None:
    import base64
    tokens = iter(["ghs_one", "ghs_two"])
    vcs = vcs_with(lambda: next(tokens), tmp_path, [])

    def basic(env: dict[str, str]) -> str:
        return base64.b64decode(env["GIT_CONFIG_VALUE_0"].split()[-1]).decode()
    assert basic(vcs._git_env()) == "x-access-token:ghs_one"
    assert basic(vcs._git_env()) == "x-access-token:ghs_two"


def test_static_token_path_unchanged(tmp_path: Path) -> None:
    seen: list[str] = []
    vcs = vcs_with("ghp_static", tmp_path, seen)
    vcs.get_pull("acme/shop", 1)
    assert seen == ["Bearer ghp_static"]
    with pytest.raises(GitHubError, match="GitHub token is empty"):
        GitHubVCS("", tmp_path)


def test_provider_returning_empty_token_fails_loudly(tmp_path: Path) -> None:
    seen: list[str] = []
    vcs = vcs_with(lambda: "", tmp_path, seen)
    with pytest.raises(GitHubError, match="token provider returned an empty token"):
        vcs.get_pull("acme/shop", 1)
    assert seen == []
