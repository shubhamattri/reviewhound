from reviewhound.config import RepoConfig, load_config
from reviewhound.router import route
from reviewhound.vcs.memory import diff_file_maps


def test_docs_only_change_gets_light_pass_on_cheap_model() -> None:
    r = route(diff_file_maps({}, {"docs/guide.md": "hello\n"}), RepoConfig())
    assert (r.tier, r.model, r.context_bytes) == ("light", "haiku", 20_000)


def test_auth_path_gets_deep_review() -> None:
    r = route(diff_file_maps({}, {"app/auth/session.py": "x = 1\n"}), RepoConfig())
    assert (r.tier, r.model) == ("deep", "opus")
    assert "app/auth/session.py matches risky path **/auth/**" in r.reasons


def test_ordinary_code_is_standard_until_it_is_large() -> None:
    small = route(diff_file_maps({}, {"app/cart.py": "x = 1\n"}), RepoConfig())
    assert small.tier == "standard"
    cfg = load_config("budget:\n  large_pr_lines: 5\n")
    big = route(diff_file_maps({}, {"app/cart.py": "".join(f"x{i} = 1\n" for i in range(5))}), cfg)
    assert big.tier == "deep"


def test_only_ignored_files_are_skipped() -> None:
    r = route(diff_file_maps({}, {"web/package-lock.json": "{}\n"}), RepoConfig())
    assert r.tier == "skip"
