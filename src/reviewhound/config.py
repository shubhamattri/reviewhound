"""Per-repo configuration: `.reviewhound.yml` merged over defaults, validated."""

from __future__ import annotations

from enum import IntEnum
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from reviewhound.paths import LANGUAGES

CONFIG_FILENAME = ".reviewhound.yml"


class Severity(IntEnum):
    NIT = 1
    MINOR = 2
    MAJOR = 3
    BLOCKER = 4

    @classmethod
    def parse(cls, value: str) -> Severity:
        try:
            return cls[value.strip().upper()]
        except KeyError as exc:
            raise ValueError(f"unknown severity {value!r}") from exc


class ConfigError(ValueError):
    """The repo's config file is present but invalid; the review must not guess."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RiskPathRule(_Strict):
    pattern: str
    tier: Literal["light", "standard", "deep"]


class AuthConfig(_Strict):
    guard_functions: list[str] = Field(default_factory=list)
    roles: list[str] = Field(default_factory=list)


class PolicyConfig(_Strict):
    min_severity_to_post: str = "minor"
    max_comments: int = Field(default=10, ge=0, le=100)
    blocking_severity: str = "blocker"
    drop_unverified: bool = True
    snap_distance: int = Field(default=3, ge=0, le=20)

    @field_validator("min_severity_to_post", "blocking_severity")
    @classmethod
    def _valid_severity(cls, v: str) -> str:
        Severity.parse(v)
        return v.lower()


class BudgetConfig(_Strict):
    context_bytes_light: int = Field(default=20_000, gt=0)
    context_bytes_standard: int = Field(default=60_000, gt=0)
    context_bytes_deep: int = Field(default=120_000, gt=0)
    large_pr_lines: int = Field(default=600, gt=0)


class RepoConfig(_Strict):
    version: int = 1
    posting: bool = False  # shadow mode until a repo opts in
    languages: list[str] = Field(default_factory=lambda: list(LANGUAGES))
    ignore_paths: list[str] = Field(
        default_factory=lambda: ["**/*.lock", "**/package-lock.json", "**/dist/**", "**/vendor/**",
                                 "**/node_modules/**", "**/.venv/**", "**/*.min.js"]
    )
    risk_paths: list[RiskPathRule] = Field(
        default_factory=lambda: [
            RiskPathRule(pattern="**/auth/**", tier="deep"),
            RiskPathRule(pattern="**/*auth*", tier="deep"),
            RiskPathRule(pattern="**/payment*/**", tier="deep"),
            RiskPathRule(pattern="**/migrations/**", tier="deep"),
            RiskPathRule(pattern="**/*.md", tier="light"),
            RiskPathRule(pattern="docs/**", tier="light"),
        ]
    )
    auth: AuthConfig = Field(default_factory=AuthConfig)
    personal_data_terms: list[str] = Field(
        default_factory=lambda: ["email", "phone", "password", "ssn", "dob", "date_of_birth",
                                 "address", "passport"]
    )
    ticket_pattern: str | None = None
    tone_examples: list[str] = Field(default_factory=list)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)

    @field_validator("languages")
    @classmethod
    def _known_languages(cls, v: list[str]) -> list[str]:
        unknown = sorted(set(v) - set(LANGUAGES))
        if unknown:
            raise ValueError(f"unsupported languages: {unknown}")
        return v


def load_config(text: str | None) -> RepoConfig:
    """Parse the repo's config text. None (no file) means defaults; bad content raises."""
    if text is None or not text.strip():
        return RepoConfig()
    try:
        raw: Any = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{CONFIG_FILENAME} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{CONFIG_FILENAME} must be a mapping at the top level")
    try:
        return RepoConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"{CONFIG_FILENAME} is invalid: {exc}") from exc
