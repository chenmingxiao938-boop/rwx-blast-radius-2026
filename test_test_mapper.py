import json
import unittest
from pathlib import Path

from patch_parser import FileChange, Patch, parse_patch
from test_mapper import map_tests


TESTS = {
    "tests": [
        {"number": 0, "id": "tests/dialects/test_bigquery.py::TestBigQuery::test_a",
         "file": "tests/dialects/test_bigquery.py", "seconds": 0.01},
        {"number": 1, "id": "tests/dialects/test_bigquery.py::TestBigQuery::test_b",
         "file": "tests/dialects/test_bigquery.py", "seconds": 0.02},
        {"number": 2, "id": "tests/dialects/test_clickhouse.py::TestClickHouse::test_a",
         "file": "tests/dialects/test_clickhouse.py", "seconds": 0.03},
        {"number": 3, "id": "tests/dialects/test_dialect.py::TestDialect::test_a",
         "file": "tests/dialects/test_dialect.py", "seconds": 0.04},
        {"number": 4, "id": "tests/test_optimizer.py::TestOptimizer::test_a",
         "file": "tests/test_optimizer.py", "seconds": 0.05},
    ]
}


def change(path, kind="modified", old=None):
    return FileChange(kind, path if kind != "added" else None,
                      path if kind != "deleted" else None, (), None)


def ids(result):
    return set(result.candidate_ids)


class MapperTests(unittest.TestCase):
    def test_direct_test_file(self):
        result = map_tests(Patch((change("tests/dialects/test_bigquery.py"),)), TESTS)
        self.assertEqual(len(result.candidates), 2)
        self.assertEqual(result.confidence, "direct")
        self.assertIn("direct_test_file", result.categories)
        self.assertTrue(all("Changed registered test file" in c.reasons[0] for c in result.candidates))
        self.assertTrue(result.fallback_required)

    def test_source_dialect_parser_and_generator_links(self):
        for path in ("sqlglot/dialects/bigquery.py", "sqlglot/parsers/bigquery.py",
                     "sqlglot/generators/bigquery.py"):
            with self.subTest(path=path):
                result = map_tests(Patch((change(path),)), TESTS)
                self.assertEqual(len(result.candidates), 2)
                self.assertEqual(result.confidence, "structural")
                self.assertTrue(result.fallback_required)

    def test_multiple_files_and_duplicate_candidate_ids(self):
        patch = Patch((change("sqlglot/dialects/bigquery.py"),
                       change("sqlglot/parsers/bigquery.py"),
                       change("sqlglot/dialects/clickhouse.py")))
        result = map_tests(patch, TESTS)
        self.assertEqual(len(result.candidate_ids), 3)
        bigquery = [c for c in result.candidates if "bigquery" in c.test_id]
        self.assertTrue(all(len(c.reasons) == 2 for c in bigquery))
        self.assertEqual(len(result.candidate_ids), len(set(result.candidate_ids)))

    def test_rename_uses_both_paths(self):
        patch = Patch((FileChange("renamed", "tests/dialects/test_bigquery.py",
                                  "tests/dialects/test_clickhouse.py", ()),))
        result = map_tests(patch, TESTS)
        self.assertEqual(len(result.candidate_ids), 3)
        self.assertEqual(result.confidence, "direct")
        self.assertTrue(result.fallback_required)

    def test_addition_and_deletion(self):
        added = map_tests(Patch((change("tests/dialects/test_bigquery.py", "added"),)), TESTS)
        deleted = map_tests(Patch((change("tests/dialects/test_bigquery.py", "deleted"),)), TESTS)
        self.assertEqual(added.candidate_ids, deleted.candidate_ids)
        self.assertEqual(len(added.candidate_ids), 2)
        self.assertTrue(added.fallback_required and deleted.fallback_required)

    def test_shared_utility_and_public_api(self):
        shared = map_tests(Patch((change("sqlglot/helper.py"),)), TESTS)
        public = map_tests(Patch((change("sqlglot/dialects/dialect.py"),)), TESTS)
        test_helper = map_tests(Patch((change("tests/dialects/test_dialect.py"),)), TESTS)
        self.assertIn("shared_utility", shared.categories)
        self.assertIn("public_api", public.categories)
        self.assertIn("shared_utility", test_helper.categories)
        self.assertEqual(len(test_helper.candidate_ids), 1)
        self.assertTrue(all(x.fallback_required for x in (shared, public, test_helper)))

    def test_configuration_and_fixture(self):
        for path in ("pyproject.toml", "tests/fixtures/optimizer/input.sql",
                     "tests/conftest.py", "requirements.lock"):
            with self.subTest(path=path):
                result = map_tests(Patch((change(path),)), TESTS)
                self.assertIn("configuration_or_fixture", result.categories)
                self.assertTrue(result.fallback_required)
                self.assertTrue(any(path in reason for reason in result.fallback_reasons))

    def test_unknown_patch_preserves_known_candidates(self):
        text = ("diff --git a/tests/dialects/test_bigquery.py b/tests/dialects/test_bigquery.py\n"
                "--- a/tests/dialects/test_bigquery.py\n"
                "+++ b/tests/dialects/test_bigquery.py\n"
                "@@ -1 +1 @@\n-old\n+new\n"
                "diff --git a/other.py b/other.py\nunsupported marker\n")
        result = map_tests(parse_patch(text), TESTS)
        self.assertEqual(len(result.candidate_ids), 2)
        self.assertIn("unknown_or_unsupported", result.categories)
        self.assertEqual(result.confidence, "unknown")
        self.assertTrue(result.fallback_required)

    def test_missing_mapping_and_unsupported_path(self):
        missing = map_tests(Patch((change("sqlglot/dialects/new_dialect.py"),)), TESTS)
        unsupported = map_tests(Patch((change("docs/guide.md"),)), TESTS)
        self.assertEqual(missing.candidate_ids, ())
        self.assertEqual(unsupported.candidate_ids, ())
        self.assertEqual(missing.confidence, "unknown")
        self.assertIn("unknown_or_unsupported", unsupported.categories)
        self.assertTrue(missing.fallback_required and unsupported.fallback_required)

    def test_deterministic_independent_of_input_order(self):
        files = (change("sqlglot/parsers/bigquery.py"), change("sqlglot/dialects/clickhouse.py"),
                 change("tests/dialects/test_bigquery.py"))
        first = map_tests(Patch(files), TESTS)
        second = map_tests(Patch(tuple(reversed(files))), TESTS)
        self.assertEqual(first, second)
        self.assertEqual(first.candidate_ids, tuple(sorted(first.candidate_ids)))

    def test_indirect_dependency_is_never_certified_safe(self):
        for path in ("tests/dialects/test_bigquery.py", "sqlglot/dialects/bigquery.py",
                     "sqlglot/optimizer/scope.py"):
            with self.subTest(path=path):
                result = map_tests(Patch((change(path),)), TESTS)
                self.assertTrue(result.fallback_required)
                self.assertTrue(any("Dependency and coverage" in x for x in result.fallback_reasons))

    def test_empty_patch_recommends_full_suite(self):
        result = map_tests(Patch(()), TESTS)
        self.assertEqual(result.candidate_ids, ())
        self.assertEqual(result.confidence, "unknown")
        self.assertTrue(result.fallback_required)

    def test_manifest_validation_fails_fast(self):
        for metadata in ({}, {"tests": [{}]},
                         {"tests": [TESTS["tests"][0], TESTS["tests"][0]]}):
            with self.subTest(metadata=metadata):
                with self.assertRaises(ValueError):
                    map_tests(Patch(()), metadata)


    def test_import_dependency_and_copy_classification(self):
        import_change = map_tests(Patch((change("sqlglot/optimizer/__init__.py"),)), TESTS)
        self.assertIn("import_or_dependency", import_change.categories)
        self.assertTrue(import_change.fallback_required)
        copied = map_tests(Patch((FileChange("copied", "tests/dialects/test_bigquery.py",
                                            "tests/dialects/test_clickhouse.py", ()),)), TESTS)
        self.assertEqual(len(copied.candidate_ids), 3)
        self.assertTrue(copied.fallback_required)

    def test_malformed_patch_has_no_false_safe_result(self):
        result = map_tests(parse_patch('diff --git "a/bad\\400.py" "b/bad\\400.py"\n'), TESTS)
        self.assertEqual(result.candidate_ids, ())
        self.assertEqual(result.confidence, "unknown")
        self.assertTrue(result.fallback_required)
    def test_import_change_is_dependency_risk(self):
        patch = parse_patch("""diff --git a/sqlglot/dialects/bigquery.py b/sqlglot/dialects/bigquery.py
--- a/sqlglot/dialects/bigquery.py
+++ b/sqlglot/dialects/bigquery.py
@@ -1 +1 @@
-from sqlglot.parsers.bigquery import BigQueryParser
+from sqlglot.parsers.base import BaseParser
""")
        result = map_tests(patch, TESTS)
        self.assertIn("import_or_dependency", result.categories)
        self.assertEqual(len(result.candidate_ids), 2)
        self.assertIn("Import dependents have not been resolved",
                      result.file_impacts[0].unresolved_reasons)
        self.assertTrue(result.fallback_required)

    def test_multiline_import_with_context_and_without_context(self):
        with_context = parse_patch("""diff --git a/sqlglot/dialects/bigquery.py b/sqlglot/dialects/bigquery.py
--- a/sqlglot/dialects/bigquery.py
+++ b/sqlglot/dialects/bigquery.py
@@ -1,3 +1,3 @@
 from sqlglot.parsers.bigquery import (
-    OldParser,
+    NewParser,
 )
""")
        without_context = parse_patch("""diff --git a/sqlglot/dialects/bigquery.py b/sqlglot/dialects/bigquery.py
--- a/sqlglot/dialects/bigquery.py
+++ b/sqlglot/dialects/bigquery.py
@@ -2 +2 @@
-    OldParser,
+    NewParser,
""")
        self.assertFalse(with_context.unknown)
        self.assertFalse(without_context.unknown)
        detected = map_tests(with_context, TESTS)
        unresolved = map_tests(without_context, TESTS)
        self.assertIn("import_or_dependency", detected.categories)
        self.assertIn("dependency_uncertain", unresolved.categories)
        self.assertTrue(detected.fallback_required and unresolved.fallback_required)

    def test_reject_noncanonical_manifest_paths(self):
        path_cases = (
            "tests/../tests/test_a.py", "tests/./test_a.py", "tests//test_a.py",
            "tests/test_a.py/", "/tests/test_a.py", "C:/tests/test_a.py",
            r"tests\test_a.py", "", "tests/test_a.py\n", "tests/\ud800.py",
        )
        for path in path_cases:
            with self.subTest(path=path):
                metadata = {"tests": [{"number": 0, "file": path,
                                       "id": path + "::test_one", "seconds": 0.001}]}
                with self.assertRaises(ValueError):
                    map_tests(Patch(()), metadata)

    def test_manifest_numbering_is_ordered_and_gapless(self):
        valid = {"tests": [{"number": 0, "id": "tests/test_a.py::test_a",
                            "file": "tests/test_a.py", "seconds": 0.001},
                           {"number": 1, "id": "tests/test_a.py::test_b",
                            "file": "tests/test_a.py", "seconds": 0.001}]}
        self.assertEqual(len(map_tests(Patch(()), valid).candidate_ids), 0)
        for numbers in ((7,), (0, 2), (1, 0), (0, 0), (True,)):
            metadata = {"tests": [
                {"number": number, "id": f"tests/test_a.py::test_{i}",
                 "file": "tests/test_a.py", "seconds": 0.001}
                for i, number in enumerate(numbers)
            ]}
            with self.subTest(numbers=numbers), self.assertRaises(ValueError):
                map_tests(Patch(()), metadata)

    def test_real_sample_manifest_is_valid(self):
        metadata = json.loads((Path(__file__).parent / "sample" / "tests.json").read_text(encoding="utf-8"))
        result = map_tests(Patch(()), metadata)
        self.assertTrue(result.fallback_required)

    def test_file_impacts_preserve_rename_paths_and_candidate_union(self):
        patch = Patch((FileChange("renamed", "tests/dialects/test_bigquery.py",
                                  "tests/dialects/test_clickhouse.py", ()),))
        result = map_tests(patch, TESTS)
        impact = result.file_impacts[0]
        self.assertEqual((impact.kind, impact.old_path, impact.new_path),
                         ("renamed", "tests/dialects/test_bigquery.py",
                          "tests/dialects/test_clickhouse.py"))
        self.assertEqual(set(impact.candidate_ids), set(result.candidate_ids))
        self.assertTrue(impact.unresolved_reasons)
        self.assertTrue(result.fallback_required)

    def test_multiple_impacts_keep_unresolved_separate(self):
        patch = Patch((change("sqlglot/dialects/bigquery.py"),
                       change("tests/fixtures/query.sql")))
        result = map_tests(patch, TESTS)
        self.assertEqual(len(result.file_impacts), 2)
        self.assertTrue(any("Configuration or fixture" in reason
                            for impact in result.file_impacts
                            for reason in impact.unresolved_reasons))
        self.assertTrue(result.fallback_required)
    def test_duplicate_file_sections_have_deterministic_impacts(self):
        first = parse_patch("""diff --git a/sqlglot/dialects/bigquery.py b/sqlglot/dialects/bigquery.py
--- a/sqlglot/dialects/bigquery.py
+++ b/sqlglot/dialects/bigquery.py
@@ -1 +1 @@
-import old
+import new
""").files[0]
        second = parse_patch("""diff --git a/sqlglot/dialects/bigquery.py b/sqlglot/dialects/bigquery.py
--- a/sqlglot/dialects/bigquery.py
+++ b/sqlglot/dialects/bigquery.py
@@ -2 +2 @@
-old
+new
""").files[0]
        self.assertEqual(map_tests(Patch((first, second)), TESTS),
                         map_tests(Patch((second, first)), TESTS))
if __name__ == "__main__":
    unittest.main()

