"""Focused tests for graph provenance without test exclusion."""

import tempfile
import unittest
from pathlib import Path

from dependency_map import build_dependency_map
from graph_precision import analyze_graph_precision


class GraphPrecisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sources = {
            "pkg/__init__.py": "",
            "pkg/target.py": "class Base: pass\nVALUE = 1\n",
            "pkg/a.py": "from pkg.target import VALUE\nresult = VALUE\n",
            "pkg/b.py": "from pkg.a import result\nother = result\n",
            "pkg/child.py": "from pkg.target import Base\nclass Child(Base): pass\n",
            "tests/__init__.py": "",
            "tests/test_direct.py": "from pkg.target import VALUE\nassert VALUE\n",
            "tests/test_transitive.py": "from pkg.b import other\nassert other\n",
            "tests/test_unused.py": "from pkg.target import VALUE\n",
            "tests/test_child.py": "from pkg.child import Child\nassert Child\n",
        }
        self.metadata = {"tests": [
            {"number": number, "id": f"{path}::Test::test_case", "file": path, "seconds": 0.001}
            for number, path in enumerate(("tests/test_direct.py", "tests/test_transitive.py",
                                           "tests/test_unused.py", "tests/test_child.py"))
        ]}
        self.build()

    def build(self):
        for path, content in self.sources.items():
            if content is None:
                continue
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        self.graph = build_dependency_map(self.sources)

    def analyze(self, *paths, dynamic=None):
        return analyze_graph_precision(paths, self.metadata, self.graph, self.root,
                                       dynamic_candidates=dynamic)

    def test_direct_dependency(self):
        result = self.analyze("pkg/target.py")[0]
        self.assertIn(0, result.direct_candidate_numbers)
        self.assertIn("tests/test_direct.py", result.direct_importers)

    def test_transitive_dependency(self):
        result = self.analyze("pkg/target.py")[0]
        self.assertIn(1, result.transitive_candidate_numbers)
        self.assertTrue(any(chain[0] == "tests/test_transitive.py" for chain in result.example_chains))

    def test_shared_imports(self):
        result = self.analyze("pkg/target.py")[0]
        self.assertEqual(set(result.current_candidate_numbers), {0, 1, 2, 3})
        self.assertIn("pkg/a.py", result.direct_importers)
        self.assertIn("pkg/child.py", result.direct_importers)

    def test_cyclic_imports_terminate(self):
        self.sources["pkg/a.py"] += "from pkg.b import other\n"
        self.build()
        result = self.analyze("pkg/target.py")[0]
        self.assertEqual(set(result.current_candidate_numbers), {0, 1, 2, 3})

    def test_class_inheritance_provenance(self):
        result = self.analyze("pkg/target.py")[0]
        self.assertIn("pkg/child.py", result.shared_base_importers)
        self.assertIn(3, result.evidence_candidate_numbers)

    def test_dynamic_literal_import(self):
        self.sources["tests/test_direct.py"] = "import importlib\nimportlib.import_module('pkg.target')\n"
        self.build()
        result = self.analyze("pkg/target.py")[0]
        edge = next(item for item in result.first_hop_edges if item.importer == "tests/test_direct.py")
        self.assertEqual(edge.kind, "dynamic_literal")

    def test_dialect_literal_is_function_scoped_evidence(self):
        self.sources["sqlglot/__init__.py"] = "def parse_one(sql, read=None): pass\n"
        self.sources["sqlglot/dialects/__init__.py"] = ""
        self.sources["sqlglot/dialects/clickhouse.py"] = "class ClickHouse: pass\n"
        self.sources["tests/test_direct.py"] = (
            "from sqlglot import parse_one\n"
            "def test_one(): parse_one('SELECT 1', read='clickhouse')\n"
        )
        self.build()
        result = self.analyze("sqlglot/dialects/clickhouse.py")[0]
        edge = next(item for item in result.first_hop_edges if item.importer == "tests/test_direct.py")
        self.assertEqual(edge.kind, "dialect_literal")
        self.assertIn(0, result.current_candidate_numbers)
        self.assertIn(0, result.conservative_candidate_numbers)

    def test_dialect_keyword_on_imported_sqlglot_symbol(self):
        self.sources["sqlglot/__init__.py"] = "class exp: pass\n"
        self.sources["sqlglot/dialects/__init__.py"] = ""
        self.sources["sqlglot/dialects/sqlite.py"] = "class SQLite: pass\n"
        self.sources["tests/test_direct.py"] = (
            "from sqlglot import exp\n"
            "def test_one(): exp.func('x', dialect='sqlite')\n"
        )
        self.build()
        result = self.analyze("sqlglot/dialects/sqlite.py")[0]
        edge = next(item for item in result.first_hop_edges if item.importer == "tests/test_direct.py")
        self.assertEqual(edge.kind, "dialect_literal")

    def test_lazy_dialect_export_import(self):
        self.sources["sqlglot/dialects/__init__.py"] = ""
        self.sources["sqlglot/dialects/bigquery.py"] = "class BigQuery: pass\n"
        self.sources["tests/test_direct.py"] = "from sqlglot.dialects import BigQuery\nassert BigQuery\n"
        self.build()
        result = self.analyze("sqlglot/dialects/bigquery.py")[0]
        # The hand-built graph does not model __getattr__ lazy export, but the
        # provenance scanner records the source relationship when asked.
        from graph_precision import _SourceEvidence
        edge = _SourceEvidence(self.root).edge("tests/test_direct.py", "sqlglot/dialects/bigquery.py")
        self.assertEqual(edge.kind, "lazy_export_import")
        self.assertTrue(result.fallback_required)

    def test_symbol_reference_vs_import_only(self):
        result = self.analyze("pkg/target.py")[0]
        edge = next(item for item in result.first_hop_edges if item.importer == "tests/test_unused.py")
        self.assertEqual(edge.kind, "explicit_import_only")
        self.assertIn(2, result.conservative_candidate_numbers)
        self.assertIn(2, result.current_candidate_numbers)

    def test_missing_source_is_conservative(self):
        (self.root / "tests/test_unused.py").unlink()
        result = self.analyze("pkg/target.py")[0]
        edge = next(item for item in result.first_hop_edges if item.importer == "tests/test_unused.py")
        self.assertEqual(edge.kind, "unknown_source")
        self.assertIn(2, result.current_candidate_numbers)

    def test_malformed_source_is_conservative(self):
        self.sources["tests/test_unused.py"] = "def broken(:\n"
        self.build()
        result = self.analyze("pkg/target.py")[0]
        self.assertNotIn(2, result.current_candidate_numbers)
        self.assertTrue(any(issue.path == "tests/test_unused.py" for issue in result.global_uncertainties))
        self.assertTrue(result.fallback_required)

    def test_unknown_symbol_stays_candidate(self):
        self.sources["tests/test_unused.py"] = "from pkg.target import NOT_PRESENT\n"
        self.build()
        result = self.analyze("pkg/target.py")[0]
        self.assertIn(2, result.current_candidate_numbers)
        self.assertIn(2, result.conservative_candidate_numbers)

    def test_dynamic_candidates_are_additive(self):
        result = self.analyze("pkg/target.py", dynamic={"pkg/target.py": [3]})[0]
        self.assertIn(3, result.combined_candidate_numbers)
        self.assertIn(3, result.evidence_candidate_numbers)
        with self.assertRaises(ValueError):
            self.analyze("pkg/target.py", dynamic={"pkg/target.py": [999]})

    def test_deterministic_output_and_fallback(self):
        first = self.analyze("pkg/target.py", "pkg/a.py")
        second = self.analyze("pkg/a.py", "pkg/target.py")
        self.assertEqual(first, second)
        self.assertTrue(all(item.fallback_required for item in first))

    def test_missing_metadata_rejected(self):
        with self.assertRaises(ValueError):
            analyze_graph_precision(["pkg/target.py"], {}, self.graph, self.root)


if __name__ == "__main__":
    unittest.main()
