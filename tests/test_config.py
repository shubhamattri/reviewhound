import pytest

from reviewhound.config import ConfigError, RepoConfig, Severity, load_config


def test_missing_file_gives_shadow_mode_defaults() -> None:
    cfg = load_config(None)
    assert cfg == RepoConfig()
    assert cfg.posting is False
    assert cfg.policy.max_comments == 10
    assert cfg.policy.min_severity_to_post == "minor"


def test_overrides_merge_over_defaults() -> None:
    cfg = load_config("posting: true\npolicy:\n  max_comments: 3\n"
                      "auth:\n  guard_functions: [ensure_staff]\n")
    assert cfg.posting is True
    assert cfg.policy.max_comments == 3
    assert cfg.policy.blocking_severity == "blocker"
    assert cfg.auth.guard_functions == ["ensure_staff"]


@pytest.mark.parametrize("text, fragment", [
    ("posting: [", "not valid YAML"),
    ("- a\n- b\n", "mapping"),
    ("postng: true\n", "invalid"),
    ("policy:\n  min_severity_to_post: urgent\n", "invalid"),
    ("languages: [cobol]\n", "invalid"),
    ("policy:\n  max_comments: -1\n", "invalid"),
])
def test_bad_config_is_rejected_not_guessed(text: str, fragment: str) -> None:
    with pytest.raises(ConfigError, match=fragment):
        load_config(text)


def test_javascript_and_go_are_allowed_and_on_by_default() -> None:
    assert RepoConfig().languages == ["python", "typescript", "javascript", "go"]
    assert load_config("languages: [javascript, go]\n").languages == ["javascript", "go"]


def test_severity_parse_and_order() -> None:
    assert Severity.parse(" Major ") is Severity.MAJOR
    assert Severity.BLOCKER > Severity.MAJOR > Severity.MINOR > Severity.NIT
    with pytest.raises(ValueError):
        Severity.parse("huge")
