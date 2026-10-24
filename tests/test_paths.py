import pytest

from reviewhound.paths import LANGUAGES, language_of, matches_any, path_matches


@pytest.mark.parametrize("path, lang", [
    ("a/b.py", "python"), ("x.ts", "typescript"), ("x.tsx", "typescript"),
    ("x.mts", "typescript"), ("x.js", "javascript"), ("x.jsx", "javascript"),
    ("x.mjs", "javascript"), ("x.cjs", "javascript"), ("cmd/main.go", "go"),
    ("README.md", None), ("x.pyc", None), ("go.mod", None),
])
def test_language_of_every_supported_extension(path: str, lang: str | None) -> None:
    assert language_of(path) == lang


def test_languages_list_is_the_single_source_of_truth() -> None:
    assert LANGUAGES == ("python", "typescript", "javascript", "go")


def test_globs_handle_double_star_and_single_segment() -> None:
    assert path_matches("src/auth/login.py", "**/auth/**") is True
    assert path_matches("auth/login.py", "**/auth/**") is True
    assert path_matches("src/authz.py", "**/auth/**") is False
    assert path_matches("a/b.md", "*.md") is False
    assert matches_any("web/package-lock.json", ["**/package-lock.json"]) is True
