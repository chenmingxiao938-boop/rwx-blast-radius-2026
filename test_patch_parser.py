import unittest
from difflib import unified_diff
from random import Random

from patch_parser import parse_patch


class PatchParserTests(unittest.TestCase):
    def test_empty_patch(self):
        patch = parse_patch(" \n\t\n")
        self.assertEqual(patch.files, ())
        self.assertFalse(patch.unknown)
        self.assertEqual(patch.changed_paths, frozenset())

    def test_modified_file_and_multiple_hunks(self):
        patch = parse_patch("""diff --git a/sqlglot/parser.py b/sqlglot/parser.py
index abc1234..def5678 100644
--- a/sqlglot/parser.py
+++ b/sqlglot/parser.py
@@ -10,3 +10,4 @@ def parse():
 context
-old
+new
+extra
 context
@@ -20,0 +22,2 @@ def other():
+first
+second
""")
        self.assertFalse(patch.unknown)
        file = patch.files[0]
        self.assertEqual((file.kind, file.old_path, file.new_path), ("modified", "sqlglot/parser.py", "sqlglot/parser.py"))
        self.assertEqual(len(file.hunks), 2)
        self.assertEqual((file.hunks[0].old_changed_lines, file.hunks[0].new_changed_lines), ((11,), (11, 12)))
        self.assertEqual((file.hunks[1].old_changed_lines, file.hunks[1].new_changed_lines), ((), (22, 23)))
        self.assertEqual(file.hunks[0].context, " def parse():")

    def test_multiple_files_addition_and_deletion(self):
        patch = parse_patch("""diff --git a/new.py b/new.py
new file mode 100644
index 0000000..abc1234
--- /dev/null
+++ b/new.py
@@ -0,0 +1,2 @@
+one
+two
diff --git a/old.py b/old.py
deleted file mode 100644
index abc1234..0000000
--- a/old.py
+++ /dev/null
@@ -1,2 +0,0 @@
-one
-two
""")
        self.assertFalse(patch.unknown)
        self.assertEqual([(f.kind, f.old_path, f.new_path) for f in patch.files], [
            ("added", None, "new.py"), ("deleted", "old.py", None),
        ])
        self.assertEqual(patch.changed_paths, frozenset({"new.py", "old.py"}))
        self.assertEqual(patch.files[1].hunks[0].old_changed_lines, (1, 2))

    def test_rename_only_and_rename_with_edits(self):
        pure = parse_patch("""diff --git a/old.py b/new.py
similarity index 100%
rename from old.py
rename to new.py
""")
        self.assertFalse(pure.unknown)
        self.assertEqual((pure.files[0].kind, pure.files[0].old_path, pure.files[0].new_path),
                         ("renamed", "old.py", "new.py"))
        changed = parse_patch("""diff --git a/old.py b/new.py
similarity index 80%
rename from old.py
rename to new.py
index abc1234..def5678 100644
--- a/old.py
+++ b/new.py
@@ -1 +1 @@
-old
+new
""")
        self.assertFalse(changed.unknown)
        self.assertEqual(changed.files[0].kind, "renamed")
        self.assertEqual(changed.files[0].hunks[0].new_changed_lines, (1,))

    def test_copy_and_quoted_paths(self):
        copy = parse_patch("""diff --git \"a/old name.py\" \"b/new name.py\"
similarity index 100%
copy from old name.py
copy to new name.py
""")
        self.assertFalse(copy.unknown)
        self.assertEqual((copy.files[0].kind, copy.files[0].old_path, copy.files[0].new_path),
                         ("copied", "old name.py", "new name.py"))
        quoted = parse_patch(r'''diff --git "a/caf\303\251.py" "b/caf\303\251.py"
index abc1234..def5678 100644
--- "a/caf\303\251.py"
+++ "b/caf\303\251.py"
@@ -1 +1 @@
-old
+new
''')
        self.assertFalse(quoted.unknown)
        self.assertEqual(quoted.files[0].new_path, "café.py")

    def test_no_newline_marker(self):
        patch = parse_patch("""diff --git a/x.py b/x.py
--- a/x.py
+++ b/x.py
@@ -1 +1 @@
-old
\\ No newline at end of file
+new
\\ No newline at end of file
""")
        self.assertFalse(patch.unknown)
        self.assertEqual(patch.files[0].hunks[0].old_changed_lines, (1,))

    def test_binary_and_mode_only_are_explicitly_unknown(self):
        binary = parse_patch("""diff --git a/x.png b/x.png
index abc1234..def5678 100644
Binary files a/x.png and b/x.png differ
""")
        mode = parse_patch("""diff --git a/x.py b/x.py
old mode 100644
new mode 100755
""")
        self.assertTrue(binary.unknown)
        self.assertTrue(mode.unknown)
        self.assertEqual(binary.files[0].kind, "modified")
        self.assertEqual(mode.files[0].kind, "modified")

    def test_malformed_or_unsupported_input_is_unknown(self):
        cases = (
            "not a diff\n",
            "diff --cc a/x.py\n",
            "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n+new\n",
            "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/y.py\n@@ -1 +1 @@\n-old\n+new\n",
            "diff --git a/x.py b/x.py\nindex abc1234..def5678 160000\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-Subproject commit abc\n+Subproject commit def\n",
            "diff --git a/x.py b/x.py\n+++ b/x.py\n--- a/x.py\n@@ -1 +1 @@\n-old\n+new\n",
            "diff --git a/x.py b/y.py\nrename from x.py\nrename to y.py\n--- /dev/null\n+++ b/y.py\n@@ -0,0 +1 @@\n+new\n",
            "diff --git a/x.py b/x.py\nnew file mode 100644\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n",
        )
        for text in cases:
            with self.subTest(text=text.splitlines()[0]):
                self.assertTrue(parse_patch(text).unknown)

    def test_known_file_survives_unknown_second_file(self):
        patch = parse_patch("""diff --git a/x.py b/x.py
--- a/x.py
+++ b/x.py
@@ -1 +1 @@
-old
+new
diff --git a/y.py b/y.py
unsupported marker
""")
        self.assertTrue(patch.unknown)
        self.assertEqual(len(patch.files), 2)
        self.assertEqual(patch.files[0].kind, "modified")
        self.assertEqual(patch.files[1].kind, "unknown")

    def test_added_and_deleted_null_sides_reject_contradictory_hunks(self):
        additions = (
            "@@ -1 +1 @@\n-old\n+new\n",
            "@@ -1,0 +1 @@\n+new\n",
            "@@ -0,1 +1 @@\n-old\n+new\n",
        )
        for hunk in additions:
            with self.subTest(kind="added", hunk=hunk.splitlines()[0]):
                text = "diff --git a/x.py b/x.py\nnew file mode 100644\n--- /dev/null\n+++ b/x.py\n" + hunk
                self.assertTrue(parse_patch(text).unknown)
        deletions = (
            "@@ -1 +1 @@\n-old\n+new\n",
            "@@ -1 +1,0 @@\n-old\n",
            "@@ -1 +0,1 @@\n-old\n+new\n",
        )
        for hunk in deletions:
            with self.subTest(kind="deleted", hunk=hunk.splitlines()[0]):
                text = "diff --git a/x.py b/x.py\ndeleted file mode 100644\n--- a/x.py\n+++ /dev/null\n" + hunk
                self.assertTrue(parse_patch(text).unknown)

    def test_invalid_coordinates_overlap_and_gap_mismatch_are_unknown(self):
        header = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
        cases = (
            "@@ -0 +1 @@\n-old\n+new\n",
            "@@ -1 +0 @@\n-old\n+new\n",
            "@@ -10 +10 @@\n-a\n+b\n@@ -10 +10 @@\n-c\n+d\n",
            "@@ -10 +10 @@\n-a\n+b\n@@ -9 +9 @@\n-c\n+d\n",
            "@@ -10 +10 @@\n-a\n+b\n@@ -20 +30 @@\n-c\n+d\n",
            "@@ -1 +1 @@\n-old\n+new\n@@ -2 +1 @@\n-old2\n+new2\n",
            "@@ -1 +1 @@\n\\ No newline at end of file\n-old\n+new\n",
        )
        for hunks in cases:
            with self.subTest(hunks=hunks):
                self.assertTrue(parse_patch(header + hunks).unknown)

    def test_valid_zero_length_ranges_and_multi_hunk_progression(self):
        insertion = parse_patch("""diff --git a/x.py b/x.py
--- a/x.py
+++ b/x.py
@@ -0,0 +1 @@
+z
@@ -5,0 +7 @@
+y
""")
        deletion = parse_patch("""diff --git a/x.py b/x.py
--- a/x.py
+++ b/x.py
@@ -1 +0,0 @@
-a
@@ -6 +4,0 @@
-f
""")
        self.assertFalse(insertion.unknown)
        self.assertFalse(deletion.unknown)
        self.assertEqual(insertion.files[0].hunks[0].new_changed_lines, (1,))
        self.assertEqual(deletion.files[0].hunks[1].old_changed_lines, (6,))

    def test_declared_counts_must_match_body(self):
        header = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
        for old_count in range(4):
            for new_count in range(4):
                hunk = f"@@ -1,{old_count} +1,{new_count} @@\n-old\n+new\n"
                parsed = parse_patch(header + hunk)
                with self.subTest(old_count=old_count, new_count=new_count):
                    self.assertEqual(parsed.unknown, (old_count, new_count) != (1, 1))

    def test_octal_escapes_fail_safely_and_valid_escapes_survive(self):
        for value in range(512):
            escaped = f"\\{value:03o}"
            text = f'diff --git "a/x{escaped}.py" "b/x{escaped}.py"\n'
            parsed = parse_patch(text)
            if value > 255:
                self.assertTrue(parsed.unknown)
        valid = parse_patch(r'''diff --git "a/x\040y.py" "b/x\040y.py"
--- "a/x\040y.py"
+++ "b/x\040y.py"
@@ -1 +1 @@
-old
+new
''')
        self.assertFalse(valid.unknown)
        self.assertEqual(valid.files[0].new_path, "x y.py")
        for text in (r'diff --git "a/x\3.py" "b/x\3.py"',
                     r'diff --git "a/x\q.py" "b/x\q.py"',
                     'diff --git "a/x.py" "b/x.py\n',
                     'diff --git "a/\ud800.py" "b/\ud800.py"\n'):
            with self.subTest(path=text):
                self.assertTrue(parse_patch(text).unknown)

    def test_malformed_second_path_preserves_first_file_and_is_deterministic(self):
        text = ("diff --git a/ok.py b/ok.py\n--- a/ok.py\n+++ b/ok.py\n"
                "@@ -1 +1 @@\n-old\n+new\n"
                'diff --git "a/bad\\400.py" "b/bad\\400.py"\n')
        first = parse_patch(text)
        self.assertEqual(first, parse_patch(text))
        self.assertTrue(first.unknown)
        self.assertEqual(first.files[0].kind, "modified")
        self.assertEqual(first.files[0].new_path, "ok.py")
        self.assertEqual(first.files[1].kind, "unknown")
        self.assertEqual(first.changed_paths, frozenset({"ok.py"}))
    def test_generated_valid_unified_diffs_are_not_unknown(self):
        random = Random(21)
        checked = 0
        for case in range(200):
            before = [f"line {n}\n" for n in range(random.randrange(1, 30))]
            after = before.copy()
            for edit in range(random.randrange(1, 5)):
                operation = random.choice(("insert", "delete", "replace"))
                index = random.randrange(len(after) + 1)
                if operation == "insert" or not after:
                    after.insert(index, f"added {case}-{edit}\n")
                elif operation == "delete":
                    del after[min(index, len(after) - 1)]
                else:
                    after[min(index, len(after) - 1)] = f"changed {case}-{edit}\n"
            diff = "".join(unified_diff(before, after, fromfile="a/x.py", tofile="b/x.py", n=0))
            if not diff:
                continue
            with self.subTest(case=case):
                self.assertFalse(parse_patch("diff --git a/x.py b/x.py\n" + diff).unknown)
            checked += 1
        self.assertGreater(checked, 100)

    def test_hunk_body_preserves_import_lines_and_context(self):
        patch = parse_patch("""diff --git a/sqlglot/x.py b/sqlglot/x.py
--- a/sqlglot/x.py
+++ b/sqlglot/x.py
@@ -1,3 +1,3 @@
 from pkg import (
-    Old,
+    New,
 )
""")
        self.assertFalse(patch.unknown)
        self.assertEqual(patch.files[0].hunks[0].body, (
            " from pkg import (", "-    Old,", "+    New,", " )",
        ))
if __name__ == "__main__":
    unittest.main()
