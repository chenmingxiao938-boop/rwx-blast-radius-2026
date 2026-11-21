import tempfile
import unittest
from pathlib import Path

from dependency_map import SelectionReason, build_dependency_map, scan_dependency_map


def inventory(*paths):
    return [{"number": index, "file": path} for index, path in enumerate(paths)]


class DependencyMapTests(unittest.TestCase):
    def setUp(self):
        self.changed = "sqlglot/core.py"
        self.test = "tests/test_core.py"
        self.other = "tests/test_other.py"
        self.helper = "tests/helpers.py"
        self.sources = {
            "sqlglot/__init__.py": "",
            self.changed: "VALUE = 1",
            self.helper: "from sqlglot import core",
            self.test: "from .helpers import check",
            self.other: "import unittest",
        }

    def test_direct_and_transitive_relationships(self):
        self.sources[self.other] = "import sqlglot.core as core"
        graph = build_dependency_map(self.sources)
        self.assertIn(self.changed, graph.direct_dependencies[self.other])
        self.assertIn(self.changed, graph.direct_dependencies[self.helper])
        result = graph.analyze([self.changed], inventory(self.test, self.other, self.test))
        self.assertEqual(result.candidate_test_ids, (0, 1, 2))
        self.assertEqual(result.reasons[0], (SelectionReason("indirect", (self.test, self.helper, self.changed)),))
        self.assertEqual(result.reasons[1], (SelectionReason("direct", (self.other, self.changed)),))
        self.assertEqual(result.reasons[0], result.reasons[2])
        self.assertFalse(result.full_suite_fallback)

    def test_cycle_terminates_with_shortest_chain(self):
        self.sources[self.changed] = "from . import middle"
        self.sources["sqlglot/middle.py"] = "from .core import VALUE"
        self.sources[self.helper] = "from sqlglot import middle"
        graph = build_dependency_map(self.sources)
        impact = graph.analyze([self.changed], inventory(self.test))
        self.assertEqual(impact.reasons[0][0].chain, (self.test, self.helper, "sqlglot/middle.py", self.changed))
        self.assertIn(self.changed, graph.transitive_dependencies(self.test))
        self.assertNotIn(self.test, graph.transitive_dependencies(self.test))
        self.assertEqual(graph.transitive_dependencies(self.changed), ("sqlglot/__init__.py", "sqlglot/middle.py"))
        self.assertFalse(impact.full_suite_fallback)

    def test_package_initializer_and_changed_test_file(self):
        graph = build_dependency_map(self.sources)
        impact = graph.analyze(["sqlglot/__init__.py", self.test], inventory(self.test, self.other))
        self.assertEqual(impact.candidate_test_ids, (0,))
        self.assertIn(SelectionReason("same-file", (self.test,)), impact.reasons[0])
        self.assertFalse(impact.full_suite_fallback)

    def test_fixture_scopes_and_plugin_dependencies(self):
        nested_test = "tests/nested/test_area.py"
        self.sources[nested_test] = ""
        self.sources["tests/nested/conftest.py"] = 'pytest_plugins = ["tests.fixtures"]'
        self.sources["tests/fixtures.py"] = "from sqlglot.core import VALUE"
        # An ancestor conftest is loaded implicitly for each test in its tree.
        self.sources["conftest.py"] = ""
        graph = build_dependency_map(self.sources)
        result = graph.analyze([self.changed], inventory(nested_test, self.other))
        self.assertEqual(result.candidate_test_ids, (0,))
        self.assertEqual(result.reasons[0][0].chain, (nested_test, "tests/nested/conftest.py", "tests/fixtures.py", self.changed))
        self.assertTrue(result.full_suite_fallback)
        self.assertEqual(graph.analyze(["conftest.py"], inventory(nested_test, self.other)).candidate_test_ids, (0, 1))

    def test_fixture_imports_are_scoped_without_global_plugin_registration(self):
        nested_test = "tests/nested/test_area.py"
        self.sources[nested_test] = ""
        self.sources["tests/nested/conftest.py"] = "from sqlglot.core import VALUE"
        result = build_dependency_map(self.sources).analyze([self.changed], inventory(nested_test, self.other))
        self.assertEqual(result.candidate_test_ids, (0,))
        self.assertFalse(result.full_suite_fallback)

    def test_literal_dynamic_imports_are_resolved(self):
        self.sources[self.test] = 'from importlib import import_module as load\nload("sqlglot.core")'
        self.sources[self.other] = '__import__("sqlglot", fromlist=["core"])'
        graph = build_dependency_map(self.sources)
        result = graph.analyze([self.changed], inventory(self.test, self.other))
        self.assertEqual(result.candidate_test_ids, (0, 1))
        self.assertFalse(result.full_suite_fallback)

    def test_dynamic_imports_raise_fallback_even_with_known_candidates(self):
        self.sources[self.other] = 'import importlib as lib\nlib.import_module(f"sqlglot.{name}")'
        graph = build_dependency_map(self.sources)
        result = graph.analyze([self.changed], inventory(self.test, self.other))
        self.assertEqual(result.candidate_test_ids, (0,))
        self.assertTrue(result.full_suite_fallback)
        self.assertEqual(result.uncertainties[0].path, self.other)
        self.assertIn("nonliteral target", result.uncertainties[0].detail)

    def test_unresolved_local_import_in_indirect_helper(self):
        self.sources[self.helper] += "\nimport sqlglot.missing"
        result = build_dependency_map(self.sources).analyze([self.changed], inventory(self.test))
        self.assertTrue(result.full_suite_fallback)
        self.assertEqual(result.uncertainties[0].path, self.helper)

    def test_unknown_paths_and_unmapped_changes_force_fallback(self):
        self.sources["sqlglot/isolated.py"] = ""
        graph = build_dependency_map(self.sources)
        for path in ("sqlglot/new.py", "sqlglot/isolated.py", "tests/fixtures/cases.sql", "/dev/null"):
            with self.subTest(path=path):
                result = graph.analyze([self.changed, path], inventory(self.test))
                self.assertEqual(result.candidate_test_ids, (0,))
                self.assertTrue(result.full_suite_fallback)

    def test_missing_test_source_and_empty_change_list_force_fallback(self):
        graph = build_dependency_map(self.sources)
        self.assertTrue(graph.analyze([], inventory(self.test)).full_suite_fallback)
        result = graph.analyze([self.changed], inventory(self.test, "tests/not_indexed.py"))
        self.assertTrue(result.full_suite_fallback)
        self.assertEqual(result.uncertainties[0].path, "tests/not_indexed.py")

    def test_unparseable_or_unreadable_sources_are_visible(self):
        for broken in (None, "def )"):
            with self.subTest(broken=broken):
                self.sources[self.helper] = broken
                graph = build_dependency_map(self.sources)
                result = graph.analyze([self.helper], inventory(self.test))
                self.assertEqual(result.candidate_test_ids, (0,))
                self.assertTrue(result.full_suite_fallback)
                self.assertEqual(result.uncertainties[0].path, self.helper)

    def test_deterministic_results_across_orders_and_path_spelling(self):
        tests = inventory(self.test, self.other)
        first = build_dependency_map(self.sources)
        second = build_dependency_map(dict(reversed(tuple(self.sources.items()))))
        self.assertEqual(first.direct_dependencies, second.direct_dependencies)
        self.assertEqual(
            first.analyze([self.changed, self.helper], tests),
            second.analyze([self.helper, self.changed.replace("/", "\\"), self.helper], reversed(tests)),
        )

    def test_checkout_scanner_respects_python_source_encoding(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for path, source in self.sources.items():
                file = root / path
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_bytes(("# coding: latin-1\n# café\n" + source).encode("latin-1"))
            graph = scan_dependency_map(root)
            impact = graph.analyze([self.changed], inventory(self.test))
            self.assertEqual(impact.candidate_test_ids, (0,))
            self.assertFalse(impact.full_suite_fallback)


if __name__ == "__main__":
    unittest.main()
