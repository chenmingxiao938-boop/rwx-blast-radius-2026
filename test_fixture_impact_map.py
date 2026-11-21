"""Focused tests for the research-only fixture reader map."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from fixture_impact_map import map_fixture_impacts


class FixtureMapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "tests/fixtures/optimizer").mkdir(parents=True)
        (self.root / "tests").mkdir(exist_ok=True)
        self.source = """class TestOptimizer:
    def check_file(self, file):
        pass
    def test_a(self):
        self.check_file('simplify')
    def test_b(self):
        self.check_file('simplify')
        load_sql_fixtures('optimizer/direct.sql')
    def test_dynamic(self):
        self.check_file(name)
"""
        self.write("tests/test_optimizer.py", self.source)
        self.write("tests/fixtures/optimizer/simplify.sql", "SELECT 1")
        self.write("tests/fixtures/optimizer/direct.sql", "SELECT 2")
        self.metadata = {"tests": [
            {"number": number, "file": "tests/test_optimizer.py",
             "id": f"tests/test_optimizer.py::TestOptimizer::{name}", "seconds": 0.001}
            for number, name in enumerate(("test_a", "test_b", "test_dynamic"))
        ]}

    def write(self, path, content):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def mapping(self, *paths):
        return map_fixture_impacts(paths, self.metadata, self.root)

    def test_direct_fixture_read(self):
        result = self.mapping("tests/fixtures/optimizer/direct.sql")
        self.assertEqual(result.candidate_numbers, (1,))
        self.assertEqual(result.file_impacts[0].evidence[0].method, "load_sql_fixtures")

    def test_shared_fixture(self):
        result = self.mapping("tests/fixtures/optimizer/simplify.sql")
        self.assertEqual(result.candidate_numbers, (0, 1))
        self.assertEqual(len(result.file_impacts[0].evidence), 2)

    def test_class_setup_fixture_reaches_each_registered_method(self):
        self.write("tests/test_optimizer.py", """class TestOptimizer:
    @classmethod
    def setUpClass(cls):
        load_sql_fixture_pairs('optimizer/simplify.sql')
    def test_a(self):
        pass
    def test_b(self):
        pass
""")
        result = self.mapping("tests/fixtures/optimizer/simplify.sql")
        self.assertEqual(result.candidate_numbers, (0, 1, 2))

    def test_dynamic_path_is_unresolved(self):
        result = self.mapping("tests/fixtures/optimizer/simplify.sql")
        self.assertTrue(any("Dynamic" in item for item in result.unresolved))
        self.assertTrue(result.fallback_required)

    def test_missing_fixture(self):
        result = self.mapping("tests/fixtures/optimizer/missing.sql")
        self.assertEqual(result.candidate_numbers, ())
        self.assertTrue(any("absent" in item for item in result.unresolved))

    def test_unknown_fixture_type(self):
        result = self.mapping("tests/fixtures/optimizer/data.json")
        self.assertTrue(any("Unsupported" in item for item in result.unresolved))

    def test_multiple_fixtures(self):
        result = self.mapping("tests/fixtures/optimizer/direct.sql", "tests/fixtures/optimizer/simplify.sql")
        self.assertEqual(result.candidate_numbers, (0, 1))
        self.assertEqual(len(result.file_impacts), 2)

    def test_duplicate_paths_do_not_duplicate_candidates(self):
        result = self.mapping("tests/fixtures/optimizer/simplify.sql", "tests/fixtures/optimizer/simplify.sql")
        self.assertEqual(result.candidate_numbers, (0, 1))
        self.assertEqual(len(result.file_impacts), 1)

    def test_order_is_deterministic(self):
        paths = ("tests/fixtures/optimizer/direct.sql", "tests/fixtures/optimizer/simplify.sql")
        self.assertEqual(self.mapping(*paths), self.mapping(*reversed(paths)))

    def test_fallback_always_propagates(self):
        self.assertTrue(self.mapping("tests/fixtures/optimizer/direct.sql").fallback_required)
        self.assertTrue(self.mapping().fallback_required)

    def test_official_numbers_follow_metadata(self):
        result = self.mapping("tests/fixtures/optimizer/simplify.sql")
        self.assertEqual({e.test_id: e.test_number for e in result.file_impacts[0].evidence}, {
            "tests/test_optimizer.py::TestOptimizer::test_a": 0,
            "tests/test_optimizer.py::TestOptimizer::test_b": 1,
        })

    def test_malformed_manifest_rejected(self):
        self.metadata["tests"][1]["number"] = 5
        with self.assertRaises(ValueError):
            self.mapping("tests/fixtures/optimizer/simplify.sql")

    def test_noncanonical_path_unresolved(self):
        result = self.mapping("tests/fixtures/../fixtures/optimizer/simplify.sql")
        self.assertEqual(result.candidate_numbers, ())
        self.assertTrue(any("Unsupported" in item for item in result.unresolved))


class PinnedManifestTests(unittest.TestCase):
    def test_registered_number_for_pinned_fixture(self):
        source = os.environ.get("RWX_PINNED_SOURCE")
        sample = os.environ.get("RWX_SAMPLE")
        if not source or not sample:
            self.skipTest("pinned source and sample manifest not mounted")
        metadata = json.loads((Path(sample) / "tests.json").read_text(encoding="utf-8"))
        fixture = "tests/fixtures/optimizer/simplify.sql"
        expected_id = "tests/test_optimizer.py::TestOptimizer::test_simplify"
        expected_number = next(entry["number"] for entry in metadata["tests"] if entry["id"] == expected_id)
        result = map_fixture_impacts([fixture], metadata, source)
        self.assertEqual(result.candidate_ids, (expected_id,))
        self.assertEqual(result.candidate_numbers, (expected_number,))
        self.assertTrue(result.fallback_required)


if __name__ == "__main__":
    unittest.main()
