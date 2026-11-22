import ast
import difflib
import unittest

from impact_selector import PINNED_COMMIT, analyze_patch
from method_boundary import _outside_body, method_body_symbol, requires_structural_fallback
from patch_parser import parse_patch


PATH = "sqlglot/foo.py"
SOURCE = ("class A:\n"
          "    token = 1\n"
          "    def m(self, x):\n"
          "        value = x + 1\n"
          "        return value\n"
          "    def n(self):\n"
          "        return 2\n")


def patch(old, new):
    diff = "".join(difflib.unified_diff(old.splitlines(keepends=True),
                                        new.splitlines(keepends=True),
                                        fromfile="a/" + PATH, tofile="b/" + PATH))
    return f"diff --git a/{PATH} b/{PATH}\nindex 1111111..2222222 100644\n" + diff


def classify(old, new, pinned=None):
    text = patch(old, new)
    parsed = parse_patch(text)
    assert not parsed.unknown, parsed.unknown_reasons
    return method_body_symbol(parsed.files[0], pinned if pinned is not None else old, text)


class MethodBoundaryTests(unittest.TestCase):
    def test_body_mask_preserves_original_and_other_syntax(self):
        source = ("@decorate\nclass A(Parent):\n"
                  "    @trace\n    async def m(self, arg=1):\n"
                  "        def nested():\n            return arg\n"
                  "        return nested()\n    def sibling(self):\n        return 2\n"
                  "class B:\n    def m(self):\n        return 3\n")
        tree = ast.parse(source)
        snapshot = ast.dump(tree, include_attributes=True)
        expected = source.replace("        def nested():\n            return arg\n"
                                  "        return nested()\n", "        pass\n")
        self.assertEqual(_outside_body(tree, ("A", "m")), ast.dump(ast.parse(expected)))
        self.assertEqual(ast.dump(tree, include_attributes=True), snapshot)

    def test_body_replacement_and_first_body_comment(self):
        changed = SOURCE.replace("x + 1", "x + 2")
        self.assertEqual(classify(SOURCE, changed), "A.m")
        comment = SOURCE.replace("    def m(self, x):\n", "    def m(self, x):\n        # inside\n")
        self.assertEqual(classify(SOURCE, comment), "A.m")

    def test_two_hunks_in_one_method(self):
        old = ("class A:\n    def m(self):\n        a = 1\n" +
               "        gap = 0\n" * 12 + "        return a\n")
        new = old.replace("a = 1", "a = 2").replace("return a", "return a + 1")
        self.assertEqual(classify(old, new), "A.m")

    def test_two_methods_are_not_one_method(self):
        new = SOURCE.replace("x + 1", "x + 2").replace("return 2", "return 3")
        self.assertIsNone(classify(SOURCE, new))

    def test_before_next_method_is_not_prior_method_body(self):
        new = SOURCE.replace("    def n(self):", "    # between\n    def n(self):")
        self.assertIsNone(classify(SOURCE, new))

    def test_imports_including_multiline_imports_are_rejected(self):
        old = ("class A:\n    def m(self):\n        from math import (\n"
               "            sin,\n        )\n        return 1\n")
        self.assertIsNone(classify(old, old.replace("sin,", "cos,")))
        self.assertIsNone(classify(SOURCE, SOURCE.replace("value = x + 1",
                                                      "import math\n        value = x + 1")))

    def test_decorator_signature_class_and_inheritance_changes_are_rejected(self):
        decorated = SOURCE.replace("    def m", "    @staticmethod\n    def m")
        for old, new in ((SOURCE, decorated),
                         (SOURCE, SOURCE.replace("def m(self, x)", "def m(self, x=1)")),
                         (SOURCE, SOURCE.replace("token = 1", "token = 2")),
                         (SOURCE, SOURCE.replace("class A:", "class A(object):"))):
            with self.subTest(new=new):
                self.assertIsNone(classify(old, new))

    def test_module_initialization_and_top_level_function_are_rejected(self):
        self.assertIsNone(classify(SOURCE, SOURCE + "VALUE = 2\n"))
        old = "def f():\n    return 1\n"
        self.assertIsNone(classify(old, old.replace("return 1", "return 2")))

    def test_bad_replay_and_syntax_error_are_rejected(self):
        changed = SOURCE.replace("x + 1", "x + 2")
        self.assertIsNone(classify(SOURCE, changed, pinned=SOURCE.replace("x + 1", "x + 3")))
        self.assertIsNone(classify(SOURCE, SOURCE.replace("value = x + 1", "value = (")))

    def test_structural_risks_remain_full_suite_in_experiment(self):
        old = ("class A:\n    def m(self):\n        from math import (\n"
               "            sin,\n        )\n        return 1\n")
        for before, after, expected in (
            (old, old.replace("sin,", "cos,"), True),
            (SOURCE, SOURCE.replace("token = 1", "token = 2"), True),
            (SOURCE, SOURCE.replace("value = x + 1", "value = ("), True),
            (SOURCE, SOURCE.replace("x + 1", "x + 2"), False),
        ):
            with self.subTest(after=after):
                parsed = parse_patch(patch(before, after))
                self.assertEqual(requires_structural_fallback(parsed.files[0], before), expected)

    def test_baseline_and_optional_mode_remain_distinct(self):
        manifest = {"tests": [
            {"number": 0, "id": "tests/test_a.py::A::test_one", "file": "tests/test_a.py", "seconds": 0.1},
            {"number": 1, "id": "tests/test_b.py::B::test_two", "file": "tests/test_b.py", "seconds": 0.2}]}
        coverage = {"pinned_commit": PINNED_COMMIT, "manifest_count": 2,
                    "missing_ids": [], "extra_ids": [], "pytest_exit_code": 0,
                    "symbol_index": {PATH: {"A.m": [0]}}, "collection_lines": {},
                    "fixture_reads": {}}
        ranges = {"pinned_commit": PINNED_COMMIT,
                  "symbols": {PATH: [["A", 1, 7, 1, "class"],
                                     ["A.m", 3, 5, 3, "method"],
                                     ["A.n", 6, 7, 6, "method"]]}}
        graph = {"commit": PINNED_COMMIT, "direct_dependencies": {}}
        snapshot = {"pinned_commit": PINNED_COMMIT, "sources": {PATH: SOURCE}}
        text = patch(SOURCE, SOURCE.replace("    def m(self, x):\n",
                                            "    def m(self, x):\n        # inside\n"))
        baseline = analyze_patch(text, manifest, coverage, ranges, graph)
        experiment = analyze_patch(text, manifest, coverage, ranges, graph,
                                   method_mode="ast_method_body", source_snapshot=snapshot)
        self.assertTrue(baseline.fallback_required)
        self.assertFalse(experiment.fallback_required)
        self.assertEqual(experiment.test_numbers, (0,))
        old_import = ("class A:\n    def m(self):\n        from math import (\n"
                      "            sin,\n        )\n        return 1\n")
        snapshot["sources"][PATH] = old_import
        import_edit = patch(old_import, old_import.replace("sin,", "cos,"))
        unsafe = analyze_patch(import_edit, manifest, coverage, ranges, graph,
                               method_mode="ast_method_body", source_snapshot=snapshot)
        self.assertTrue(unsafe.fallback_required)
        self.assertEqual(unsafe.test_numbers, (0, 1))
        snapshot["sources"].clear()
        missing = analyze_patch(text, manifest, coverage, ranges, graph,
                                method_mode="ast_method_body", source_snapshot=snapshot)
        self.assertTrue(missing.fallback_required)
        self.assertEqual(missing.test_numbers, (0, 1))
        snapshot["sources"][PATH] = SOURCE.replace("value = x + 1", "value = x + 3")
        mismatch = analyze_patch(text, manifest, coverage, ranges, graph,
                                 method_mode="ast_method_body", source_snapshot=snapshot)
        self.assertTrue(mismatch.fallback_required)
        with self.assertRaises(ValueError):
            analyze_patch(text, manifest, coverage, ranges, graph,
                          method_mode="ast_method_body",
                          source_snapshot={"pinned_commit": "wrong", "sources": {}})


if __name__ == "__main__":
    unittest.main()
