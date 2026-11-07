from pathlib import Path

import pytest

from reviewhound.config import RepoConfig, Severity
from reviewhound.design_tokens import DesignTokenScanner, load_tokens
from reviewhound.findings import Finding
from reviewhound.pipeline import Reviewer
from reviewhound.providers import FakeProvider, ProviderRegistry
from reviewhound.store import Store
from reviewhound.vcs.base import DictSnapshot
from reviewhound.vcs.memory import MemoryVCS, diff_file_maps
from tests.conftest import answer, make_pr

RULES = """## DS TOKEN QUICK REFERENCE
### Border Radius
--r-none: 0    --r-xs: 3px   --r-sm: 6px   --r-md: 8px
### Spacing
--sp-1: 4px   --sp-2: 8px   --sp-3: 12px  --sp-4: 14px  --sp-5: 16px
"""
CSS_BASE = ".popover {\n  color: red;\n}\n"
ON = RepoConfig(design_tokens=True)


def scan(added: str, extra: dict[str, str] | None = None, path: str = "src/PortalTable.css",
         cfg: RepoConfig = ON) -> list[Finding]:
    base = {"CLAUDE.md": RULES, path: CSS_BASE, **(extra or {})}
    head = {**base, path: CSS_BASE.replace("  color: red;\n", "  color: red;\n" + added)}
    return DesignTokenScanner().scan(diff_file_maps(base, head), DictSnapshot(head), cfg,
                                     DictSnapshot(base))


def test_check_is_off_unless_the_repo_config_turns_it_on() -> None:
    assert RepoConfig().design_tokens is False
    assert scan("  --ds-popover-padding: 4px;\n", cfg=RepoConfig()) == []
    assert [f.line for f in scan("  --ds-popover-padding: 4px;\n", cfg=ON)] == [3]


def test_spacing_custom_property_with_a_token_value_is_flagged_from_the_rules_table() -> None:
    found = scan("  --ds-popover-padding: 4px;\n")
    assert [(f.file, f.line, f.severity, f.category) for f in found] == [
        ("src/PortalTable.css", 3, Severity.MINOR, "design-token")]
    assert found[0].title == "Use var(--sp-1) instead of 4px"
    assert found[0].evidence_quote == "--ds-popover-padding: 4px;"
    assert found[0].verified and found[0].source == "scanner:design-tokens"


def test_values_with_no_token_or_outside_a_token_family_are_not_flagged() -> None:
    lines = ("  width: 280px;\n  border: 1px solid var(--line);\n  padding: 7px;\n"
             "  margin: 0;\n  padding: 1px;\n  height: 8px;\n  margin-left: 50%;\n")
    assert scan(lines) == []


def test_shorthand_names_each_literal_and_family_picks_the_radius_token() -> None:
    found = scan("  padding: 4px 8px 7px;\n  border-radius: 8px;\n  border-top-left-radius: 6px;\n")
    assert [(f.line, f.title) for f in found] == [
        (3, "Use var(--sp-1) instead of 4px, var(--sp-2) instead of 8px"),
        (4, "Use var(--r-md) instead of 8px"),
        (5, "Use var(--r-sm) instead of 6px")]


def test_negatives_function_arguments_and_token_definitions_are_left_alone() -> None:
    lines = ("  margin-top: -4px;\n  padding: calc(100% - 8px);\n"
             "  padding: var(--sp-x, 4px);\n  --sp-1: 4px;\n  --radius: 8px;\n"
             "  /* padding: 4px; */\n  // padding: 4px;\n")
    assert scan(lines, path="src/x.scss") == []


def test_tokens_come_from_css_custom_properties_in_the_repo_too() -> None:
    tokens = {"src/styles/tokens.css": ":root {\n  --space-2: 0.5rem;\n  --sp-alias: var(--sp-1);\n}\n"}
    found = scan("  gap: 0.5rem;\n  top: 4px;\n", extra=tokens)
    assert [(f.line, f.title) for f in found] == [
        (3, "Use var(--space-2) instead of 0.5rem"), (4, "Use var(--sp-1) instead of 4px")]


def test_no_token_definitions_anywhere_means_no_findings() -> None:
    base = {"a.css": CSS_BASE}
    head = {"a.css": CSS_BASE.replace("  color: red;\n", "  color: red;\n  padding: 4px;\n")}
    assert DesignTokenScanner().scan(diff_file_maps(base, head), DictSnapshot(head),
                                     ON, DictSnapshot(base)) == []


def test_only_added_lines_are_checked() -> None:
    base = {"CLAUDE.md": RULES, "a.css": ".a {\n  padding: 4px;\n}\n"}
    head = {**base, "a.css": ".a {\n  padding: 4px;\n  color: red;\n}\n"}
    assert DesignTokenScanner().scan(diff_file_maps(base, head), DictSnapshot(head),
                                     ON, DictSnapshot(base)) == []


def test_inline_style_on_a_jsx_line_is_checked() -> None:
    base = {"CLAUDE.md": RULES, "A.jsx": "export const A = () => null;\n"}
    head = {**base, "A.jsx": "export const A = () => (\n"
            "  <div style={{ paddingTop: '8px', width: '280px', borderRadius: 4 }} />\n);\n"}
    found = DesignTokenScanner().scan(diff_file_maps(base, head), DictSnapshot(head),
                                      ON, DictSnapshot(base))
    assert [(f.file, f.line, f.title) for f in found] == [
        ("A.jsx", 2, "Use var(--sp-2) instead of 8px")]


def test_load_tokens_reads_several_tokens_per_rules_line_and_skips_unitless() -> None:
    tokens = load_tokens([("CLAUDE.md", RULES)], [])
    assert tokens[("spacing", "4px")] == ["--sp-1"]
    assert tokens[("radius", "8px")] == ["--r-md"]
    assert ("radius", "0") not in tokens


def test_zero_and_one_pixel_are_never_flagged_even_when_a_token_has_that_value() -> None:
    found = scan("  padding: 1px;\n  margin: 0px;\n",
                 extra={"t.css": ":root {\n  --sp-hair: 1px;\n  --sp-none: 0px;\n}\n"})
    assert found == []


def test_a_review_gets_at_most_five_token_comments_and_the_last_counts_the_rest() -> None:
    found = scan("".join(f"  .x{n} {{ gap: 8px; }}\n" for n in range(7)))
    assert [f.line for f in found] == [3, 4, 5, 6, 7]
    assert found[-1].body.endswith("2 more added lines in this PR have the same issue.")
    assert "more added lines" not in found[0].body


def _pipeline_findings(base_cfg: str | None, head_cfg: str | None, tmp_path: Path) -> list[str]:
    css = "src/PortalTable.css"
    base = {"CLAUDE.md": RULES, css: CSS_BASE}
    head = {**base, css: CSS_BASE.replace("  color: red;\n",
                                          "  color: red;\n  --ds-popover-padding: 4px;\n")}
    if base_cfg is not None:
        base[".reviewhound.yml"] = base_cfg
    if head_cfg is not None:
        head[".reviewhound.yml"] = head_cfg
    vcs = MemoryVCS(make_pr(head="h1"), base, head)
    reviewer = Reviewer(vcs, ProviderRegistry([FakeProvider([answer()])]),
                        [DesignTokenScanner()], Store(tmp_path / "s.db"))
    decision = reviewer.review("acme/shop", 7, dry_run=True).decision
    assert decision is not None
    return [f"{f.file}:{f.line}" for f in decision.post]


def test_pipeline_runs_the_check_only_when_the_base_config_turns_it_on(tmp_path: Path) -> None:
    assert _pipeline_findings(None, None, tmp_path) == []
    assert _pipeline_findings("design_tokens: true\n", "design_tokens: true\n",
                              tmp_path) == ["src/PortalTable.css:3"]


def test_a_pr_cannot_turn_the_check_on_from_its_own_head_config(tmp_path: Path) -> None:
    assert _pipeline_findings(None, "design_tokens: true\n", tmp_path) == []


def test_cli_reviewer_runs_the_design_token_scanner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import argparse

    from reviewhound import cli

    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    args = argparse.Namespace(home=str(tmp_path), gh_user=None, database_url=None, fake_answer=None,
                              max_proof_seconds=60, proof_image="img", allow_local_proof_repo=[],
                              repo="o/r")
    reviewer = cli._reviewer(args, with_store=False)
    assert [type(s).__name__ for s in reviewer.scanners].count("DesignTokenScanner") == 1


def _scan_maps(base: dict[str, str], head: dict[str, str]) -> list[tuple[str, int, str]]:
    found = DesignTokenScanner().scan(diff_file_maps(base, head), DictSnapshot(head), ON,
                                      DictSnapshot(base))
    return [(f.file, f.line, f.title) for f in found]


def test_tokens_come_from_the_base_so_the_pr_cannot_add_or_remove_them() -> None:
    css = ".a {\n  color: red;\n}\n"
    added = ".a {\n  color: red;\n  padding: 8px;\n}\n"
    assert _scan_maps({"a.css": css}, {"a.css": added, "CLAUDE.md": "--sp-x: 8px\n"}) == []
    assert _scan_maps({"a.css": css}, {"a.css": added, "t.css": ":root { --sp-x: 8px; }\n"}) == []
    base = {"a.css": css, "CLAUDE.md": RULES, "t.css": ":root { --sp-y: 8px; }\n"}
    want = [("a.css", 3, "Use var(--sp-2) instead of 8px")]
    assert _scan_maps(base, {**base, "a.css": added, "CLAUDE.md": "nothing\n"}) == want
    assert _scan_maps(base, {"a.css": added, "t.css": base["t.css"]}) == want
    no_rules = {"a.css": css, "t.css": ":root { --sp-y: 8px; }\n"}
    for head_tokens in ({"t.css": ":root { --sp-y: 9px; }\n"}, {}):
        assert _scan_maps(no_rules, {"a.css": added, **head_tokens}) == [
            ("a.css", 3, "Use var(--sp-y) instead of 8px")]


def test_without_a_base_view_the_scanner_finds_nothing() -> None:
    head = {"CLAUDE.md": RULES, "a.css": ".a {\n  padding: 8px;\n}\n"}
    assert DesignTokenScanner().scan(diff_file_maps({}, head), DictSnapshot(head), ON) == []


def test_non_ascii_token_names_are_ignored_not_a_crash() -> None:
    styles = [("t.css", ":root {\n  --sp-ſ: 4px;\n  --ſp-1: 4px;\n  --sp-ok: 8px;\n}\n")]
    assert load_tokens([("CLAUDE.md", "--sp-ſ: 6px\n")], styles) == {
        ("spacing", "8px"): ["--sp-ok"]}


def test_lines_inside_a_multi_line_comment_are_not_flagged() -> None:
    found = scan("  /*\n  padding: 8px;\n  * { margin: 4px; }\n  */\n  * { margin: 4px; }\n")
    assert [(f.line, f.title) for f in found] == [(7, "Use var(--sp-1) instead of 4px")]


def test_style_blocks_in_vue_svelte_and_html_files_are_checked() -> None:
    for path in ("A.vue", "A.svelte", "a.html"):
        base = {"CLAUDE.md": RULES, path: "<template/>\n"}
        head = {**base, path: "<template/>\n<style scoped>\n.a {\n  padding: 8px;\n}\n"
                "/* gap: 4px; */\n</style>\n<style>.b { gap: 4px; }</style>\n<p>gap: 4px;</p>\n"}
        assert _scan_maps(base, head) == [(path, 4, "Use var(--sp-2) instead of 8px"),
                                          (path, 8, "Use var(--sp-1) instead of 4px")]


def test_letter_and_word_spacing_custom_properties_are_not_spacing_tokens() -> None:
    lines = "  --heading-letter-spacing: 4px;\n  --body-word-spacing: 4px;\n  --ds-spacing-x: 4px;\n"
    assert [f.line for f in scan(lines)] == [5]


def test_stacked_pr_leaves_the_token_check_off(tmp_path: Path) -> None:
    css = "src/PortalTable.css"
    base = {"CLAUDE.md": RULES, css: CSS_BASE, ".reviewhound.yml": "design_tokens: true\n"}
    head = {**base, css: CSS_BASE.replace("  color: red;\n", "  color: red;\n  gap: 4px;\n")}
    posted = []
    for n, pr in enumerate((make_pr(head="h1"), make_pr(head="h1", base_ref="feat/parent"))):
        vcs = MemoryVCS(pr, base, head)
        reviewer = Reviewer(vcs, ProviderRegistry([FakeProvider([answer()])]),
                            [DesignTokenScanner()], Store(tmp_path / f"{n}.db"))
        decision = reviewer.review("acme/shop", 7, dry_run=True).decision
        assert decision is not None
        posted.append([f"{f.file}:{f.line}" for f in decision.post])
    assert posted == [["src/PortalTable.css:3"], []]
