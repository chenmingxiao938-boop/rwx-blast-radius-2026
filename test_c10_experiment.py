import unittest

from c10_ast import analyze_region, call_index, caller_distances
from c10_experiment import is_fixture_only, pick, rank
from c10_semantic import patch_features, test_features as semantic_test_features
from patch_parser import parse_patch


def changed(path, hunk):
    return parse_patch(f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n" + hunk)


class C10ExperimentTests(unittest.TestCase):
    def test_branch_evidence_does_not_pick_sibling(self):
        before = "def f(flag):\n    if flag:\n        return 1\n    return 2\n"
        patch = changed("sqlglot/demo.py", "@@ -3 +3 @@\n-        return 1\n+        return 3\n")
        region = analyze_region(patch.files[0], before)
        self.assertEqual(region.symbols, {"f"})
        self.assertEqual(region.branch_lines, {3})
        self.assertFalse(region.structure_risk)

    def test_module_assignment_is_structural_and_requires_matching_source(self):
        patch = changed("sqlglot/demo.py", "@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 2\n")
        self.assertTrue(analyze_region(patch.files[0], "VALUE = 1\n").structure_risk)
        with self.assertRaisesRegex(ValueError, "pinned source"):
            analyze_region(patch.files[0], "VALUE = 0\n")

    def test_static_calls_bind_an_import_alias_and_one_wrapper(self):
        calls = call_index({"sqlglot/demo.py": "def f():\n    return 2\n",
                            "tests/test_example.py": "from sqlglot.demo import f as alias\n"
                            "def helper():\n    return alias()\n"
                            "def test_wrapper():\n    assert helper()\n"})
        self.assertEqual(caller_distances(calls, {("sqlglot/demo.py", "f")}),
                         {("tests/test_example.py", "helper"): 1,
                          ("tests/test_example.py", "test_wrapper"): 2})

    def test_cost_ranking_does_not_drop_required_evidence_silently(self):
        scores, seconds = {0: 5, 1: 5, 2: 1}, {0: 23.0, 1: 0.02, 2: 0.1}
        self.assertEqual(rank(scores, seconds), [1, 0, 2])
        self.assertEqual(pick(scores, {2}, seconds, 2), (1, 2))
        with self.assertRaisesRegex(ValueError, "exceeds_budget"):
            pick(scores, {1, 2}, seconds, 1)

    def test_fixture_only_is_not_a_python_change(self):
        hunk = "@@ -1 +1 @@\n-SELECT 1\n+SELECT 2\n"
        self.assertTrue(is_fixture_only(changed("tests/fixtures/one.sql", hunk)))
        self.assertFalse(is_fixture_only(changed("sqlglot/demo.py", hunk)))

    def test_sql_literal_supports_expression_feature(self):
        patch = changed("sqlglot/parsers/bigquery.py", "@@ -1 +1 @@\n-exp.Select\n+exp.Join\n")
        feature = patch_features(patch)
        tests = semantic_test_features("tests/test_demo.py::test_input",
                                       'def test_input():\n    parse_one("SELECT * FROM x JOIN y", read="bigquery")\n')
        self.assertTrue({"dialect:bigquery", "sql:join"}.issubset(feature.keys() & tests.keys()))


if __name__ == "__main__":
    unittest.main()
