"""The shipped Dockerfile and compose files: pinned versions, App mode, one published port."""

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).parent.parent


def compose(name: str) -> dict[str, Any]:
    data = yaml.safe_load((ROOT / name).read_text())
    assert isinstance(data, dict)
    return data


def test_dockerfile_pins_uv_and_the_claude_cli() -> None:
    text = (ROOT / "Dockerfile").read_text()
    assert re.search(r"^COPY --from=ghcr\.io/astral-sh/uv:\d+\.\d+\.\d+ ", text, re.MULTILINE)
    [version] = re.findall(r"^ARG CLAUDE_CODE_VERSION=(.*)$", text, re.MULTILINE)
    assert re.fullmatch(r"\d+\.\d+\.\d+", version), version
    assert "latest" not in text


def test_only_the_webhook_port_is_published() -> None:
    services = compose("docker-compose.yml")["services"]
    published = {name: svc.get("ports", []) for name, svc in services.items()}
    assert published == {"serve": ["${REVIEWHOUND_PORT:-8787}:8787"], "worker": [],
                         "postgres": []}


def test_token_mode_needs_no_app_and_app_mode_needs_no_token() -> None:
    base = compose("docker-compose.yml")["services"]
    worker_env = base["worker"]["environment"]
    assert worker_env["GITHUB_TOKEN"] == "${GITHUB_TOKEN:-}"  # optional: App mode has none
    assert base["serve"]["environment"]["REVIEWHOUND_METRICS_TOKEN"] == \
        "${REVIEWHOUND_METRICS_TOKEN:-}"
    app = compose("docker-compose.app.yml")
    for svc in ("serve", "worker"):
        env = app["services"][svc]["environment"]
        assert env["REVIEWHOUND_APP_ID"] == "${REVIEWHOUND_APP_ID:?set REVIEWHOUND_APP_ID}"
    assert app["services"]["worker"]["environment"]["REVIEWHOUND_APP_PRIVATE_KEY_PATH"] == \
        "/run/secrets/github_app_key"
    assert app["services"]["worker"]["secrets"] == ["github_app_key"]
    assert app["secrets"] == {"github_app_key": {
        "file": "${REVIEWHOUND_APP_PRIVATE_KEY_FILE:?set REVIEWHOUND_APP_PRIVATE_KEY_FILE}"}}
    assert "ports" not in app["services"]["serve"] and "ports" not in app["services"]["worker"]


def test_no_secret_value_is_written_into_the_compose_files() -> None:
    for name in ("docker-compose.yml", "docker-compose.app.yml"):
        for svc in compose(name)["services"].values():
            for key, value in (svc.get("environment") or {}).items():
                if re.search(r"TOKEN|SECRET|PASSWORD|_KEY|DATABASE_URL", key):
                    assert value.startswith(("${", "/run/secrets/")), \
                        (name, key)
