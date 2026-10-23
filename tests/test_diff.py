from reviewhound.diff import parse_patch, parse_unified_diff

PATCH = """@@ -1,3 +1,4 @@
 a
-b
+B
+C
 d
@@ -10,2 +11,3 @@ def f():
 x
+y
 z"""


def test_parse_patch_tracks_added_and_context_lines_on_head_side() -> None:
    fd = parse_patch("f.py", PATCH)
    assert fd.added_lines == {2, 3, 12}
    assert fd.commentable_lines == {1, 2, 3, 4, 11, 12, 13}


def test_parse_patch_tracks_deleted_lines_on_base_side() -> None:
    patch = "@@ -4,6 +4,4 @@\n a\n-b\n-c\n d\n+D\n-e\n f\n@@ -20 +18,0 @@\n-z\n"
    fd = parse_patch("f.py", patch)
    assert fd.deleted_lines == {5, 6, 8, 20}
    assert fd.added_lines == {6}
    assert parse_patch("f.py", PATCH).deleted_lines == {2}


def test_nearest_added_prefers_closest_then_lower_and_respects_distance() -> None:
    fd = parse_patch("f.py", PATCH)
    assert fd.nearest_added(7, 4) == 3
    assert fd.nearest_added(7, 3) is None
    assert fd.nearest_added(12, 0) == 12


def test_unified_diff_reads_new_deleted_and_renamed_files() -> None:
    text = """diff --git a/new.py b/new.py
new file mode 100644
--- /dev/null
+++ b/new.py
@@ -0,0 +1,2 @@
+x = 1
+y = 2
diff --git a/old.py b/old.py
deleted file mode 100644
--- a/old.py
+++ /dev/null
@@ -1 +0,0 @@
-z = 3
diff --git a/a.py b/b.py
similarity index 90%
rename from a.py
rename to b.py
--- a/a.py
+++ b/b.py
@@ -1 +1 @@
-q = 1
+q = 2
"""
    d = parse_unified_diff(text)
    assert set(d.files) == {"new.py", "old.py", "b.py"}
    assert d.files["new.py"].status == "added"
    assert d.files["new.py"].added_lines == {1, 2}
    assert d.files["old.py"].status == "deleted"
    assert d.files["old.py"].deleted_lines == {1}
    assert d.files["b.py"].deleted_lines == {1}
    assert d.files["b.py"].status == "renamed"
    assert d.files["b.py"].old_path == "a.py"
    assert d.changed_paths == ["new.py", "b.py"]
    assert d.added_line_count() == 3
    assert d.removed_line_count() == 2


def test_parse_patch_records_the_head_line_after_each_deleted_run() -> None:
    patch = "@@ -4,6 +4,4 @@\n a\n-b\n-c\n d\n+D\n-e\n f\n@@ -20 +18,0 @@\n-z\n"
    fd = parse_patch("f.py", patch)
    assert [h.deleted_at for h in fd.hunks] == [[5, 7], [19]]
    assert [h.deleted_at for h in parse_patch("f.py", PATCH).hunks] == [[2], []]
    blank_runs = "@@ -1,6 +1,4 @@\n a\n-\n b\n-   \n-c\n d\n"
    assert parse_patch("f.py", blank_runs).hunks[0].deleted_at == [3]
