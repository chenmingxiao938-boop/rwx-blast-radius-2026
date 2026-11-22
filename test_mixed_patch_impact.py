import unittest

from impact_selector import PINNED_COMMIT, analyze_patch
from mixed_patch_impact import analyze_mixed_patch, is_comment_only
from patch_parser import parse_patch


PYTHON_CHANGE = """diff --git a/sqlglot/example.py b/sqlglot/example.py
--- a/sqlglot/example.py
+++ b/sqlglot/example.py
@@ -1,2 +1,3 @@
 import sys
+# Ordinary explanation
 x = 1
"""
FIXTURE_CHANGE = """diff --git a/tests/fixtures/demo.sql b/tests/fixtures/demo.sql
--- a/tests/fixtures/demo.sql
+++ b/tests/fixtures/demo.sql
@@ -1 +1 @@
-SELECT 1;
+SELECT 2;
"""
NODE_ID = "tests/test_sample.py::test_reader"


class MixedPatchTests(unittest.TestCase):
    def setUp(self):
        self.source = "import sys\nx = 1\n"
        self.snapshot = {"pinned_commit": PINNED_COMMIT, "sources": {"sqlglot/example.py": self.source}}
        self.manifest = {"tests": [
            {"id": NODE_ID, "file": "tests/test_sample.py", "number": 0},
            {"id": "tests/test_sample.py::test_other", "file": "tests/test_sample.py", "number": 1},
        ]}
        self.coverage = {"pinned_commit": PINNED_COMMIT, "manifest_count": 2,
                         "missing_ids": [], "extra_ids": [], "pytest_exit_code": 0,
                         "fixture_reads": {"tests/fixtures/demo.sql": {"test:" + NODE_ID: 1}}}

    def analyze(self, patch, mode="aggressive_fixture_only"):
        return analyze_mixed_patch(
            patch, self.manifest, self.coverage, {"pinned_commit": PINNED_COMMIT, "symbols": {}},
            {"commit": PINNED_COMMIT, "direct_dependencies": {}}, baseline_analyze=analyze_patch,
            fixture_mode=mode, method_mode="ast_method_body", source_snapshot=self.snapshot,
        )

    def test_comment_and_resolved_fixture_narrow_in_experiment(self):
        result = self.analyze(PYTHON_CHANGE + FIXTURE_CHANGE)
        self.assertEqual(result.test_numbers, (0,))
        self.assertTrue(any("introspection" in message for message in result.uncertainty))

    def test_baseline_fixture_policy_preserves_full_suite(self):
        self.assertTrue(self.analyze(PYTHON_CHANGE + FIXTURE_CHANGE, "baseline").fallback_required)

    def test_substantive_or_unverified_source_preserves_full_suite(self):
        for patch in (PYTHON_CHANGE.replace("+# Ordinary explanation", "+x = 2"),
                      PYTHON_CHANGE.replace(" import sys", " import os"),
                      PYTHON_CHANGE.replace("+# Ordinary explanation", "+# type: ignore"),
                      PYTHON_CHANGE.replace("+# Ordinary explanation", "+# vim: set fileencoding=latin-1")):
            with self.subTest(patch=patch):
                self.assertEqual(self.analyze(patch + FIXTURE_CHANGE).test_numbers, (0, 1))

    def test_missing_source_and_collection_fixture_reader_preserve_full_suite(self):
        document = PYTHON_CHANGE + FIXTURE_CHANGE
        self.coverage["fixture_reads"]["tests/fixtures/demo.sql"]["collection"] = 1
        self.assertTrue(self.analyze(document).fallback_required)
        del self.coverage["fixture_reads"]["tests/fixtures/demo.sql"]["collection"]
        self.snapshot["sources"].clear()
        self.assertTrue(self.analyze(document).fallback_required)

    def test_fixture_only_uses_v5_result(self):
        self.assertEqual(self.analyze(FIXTURE_CHANGE).test_numbers, (0,))

    def test_hash_inside_multiline_string_is_not_a_comment(self):
        change = parse_patch(PYTHON_CHANGE.replace(" import sys", ' value = """')
                             .replace(" x = 1", ' """')).files[0]
        self.assertFalse(is_comment_only(change, 'value = """\n"""\n'))

    def test_mode_changes_and_duplicate_paths_preserve_full_suite(self):
        mode = PYTHON_CHANGE.replace("--- a/", "old mode 100644\nnew mode 100755\n--- a/", 1)
        for changes in (mode, PYTHON_CHANGE + PYTHON_CHANGE):
            self.assertTrue(self.analyze(changes + FIXTURE_CHANGE).fallback_required)


if __name__ == "__main__":
    unittest.main()
