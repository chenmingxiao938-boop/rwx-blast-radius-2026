import tempfile
import unittest
from pathlib import Path

from shared_fixture_map import build
from impact_selector import PINNED_COMMIT, analyze_patch


class SharedMapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "tests").mkdir()
        self.file = self.root / "tests" / "test_demo.py"
        self.manifest = {"tests": [
            {"number": 0, "id": "tests/test_demo.py::Demo::test_one", "file": "tests/test_demo.py"},
            {"number": 1, "id": "tests/test_demo.py::Demo::test_two", "file": "tests/test_demo.py"},
        ]}
        self.source = (
            "import unittest\n"
            "from tests.helpers import load_sql_fixtures as lf\n"
            "class Demo(unittest.TestCase):\n"
            "    def read(self, path):\n"
            "        return list(lf(path))\n"
            "    def test_one(self):\n"
            "        self.read('a.sql')\n"
            "    def test_two(self):\n"
            "        pass\n"
        )

    def test_indirect_loader_alias_import_resolves_call_target(self):
        self.file.write_text(self.source)
        mapping = build(self.root, self.manifest)
        self.assertEqual(mapping["file_cohorts"]["tests/test_demo.py"], [0])
        self.assertNotIn("tests/test_demo.py", mapping["unresolved_files"])

    def test_setup_class_initialization_includes_class_consumers(self):
        self.file.write_text(self.source.replace(
            "    def read(self, path):",
            "    @classmethod\n"
            "    def setUpClass(cls):\n"
            "        cls.sqls = list(lf('a.sql'))\n"
            "    def read(self, path):",
        ))
        mapping = build(self.root, self.manifest)
        self.assertEqual(mapping["initializers"]["tests/fixtures/a.sql"], [0, 1])

    def test_dynamic_initializer_path_is_unresolved(self):
        self.file.write_text(self.source.replace(
            "    def read(self, path):",
            "    @classmethod\n"
            "    def setUpClass(cls):\n"
            "        cls.sqls = list(lf(cls.fixture_name))\n"
            "    def read(self, path):",
        ))
        self.assertIn("tests/test_demo.py", build(self.root, self.manifest)["unresolved_files"])
    def test_alias_assignment_is_unresolved(self):
        self.file.write_text(self.source + "\nreader = lf\n")
        self.assertIn("tests/test_demo.py", build(self.root, self.manifest)["unresolved_files"])

    def test_unknown_inherited_wrapper_is_unresolved(self):
        self.file.write_text(self.source.replace("self.read('a.sql')", "self.other_reader('a.sql')"))
        self.assertIn("tests/test_demo.py", build(self.root, self.manifest)["unresolved_files"])

    def test_runtime_adds_cohort_and_falls_back_for_unresolved_file(self):
        self.file.write_text(self.source)
        mapping = build(self.root, self.manifest)
        coverage = {"pinned_commit": PINNED_COMMIT, "manifest_count": 2,
                    "missing_ids": [], "extra_ids": [], "pytest_exit_code": 0,
                    "fixture_reads": {"tests/fixtures/a.sql": {
                        "tests/test_demo.py::Demo::test_one": [["open", "tests/helpers.py:1"]]}},
                    "collection_lines": {}}
        patch = ("diff --git a/tests/fixtures/a.sql b/tests/fixtures/a.sql\n"
                 "--- a/tests/fixtures/a.sql\n+++ b/tests/fixtures/a.sql\n"
                 "@@ -1 +1 @@\n-old\n+new\n")
        def decide():
            return analyze_patch(
                patch, self.manifest, coverage,
                {"pinned_commit": PINNED_COMMIT, "symbols": {}},
                {"commit": PINNED_COMMIT, "direct_dependencies": {}},
                fixture_mode="aggressive_fixture_only", fixture_safety_mode="shared_loader",
                shared_fixture_map=mapping,
            )
        self.assertEqual(decide().test_numbers, (0,))
        mapping["unresolved_files"] = ["tests/test_demo.py"]
        self.assertEqual(decide().test_numbers, (0, 1))
        self.assertTrue(decide().fallback_required)


if __name__ == "__main__":
    unittest.main()