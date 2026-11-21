import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import selector
import shadow_mode
from dependency_map import build_dependency_map
from shadow_mode import analyze_patch, load_artifact, run_shadow


def inventory():
    paths = (
        "tests/test_optimizer.py",
        "tests/dialects/test_bigquery.py",
        "tests/dialects/test_clickhouse.py",
    )
    return [
        {"number": n, "file": path, "id": path + "::test_query", "seconds": 0.001}
        for n, path in enumerate(paths)
    ]


def graph():
    return build_dependency_map({
        "sqlglot/__init__.py": "def parse_one(sql, read=None): pass",
        "sqlglot/dialects/bigquery.py": "",
        "sqlglot/dialects/clickhouse.py": "",
        "tests/test_optimizer.py": (
            "from sqlglot import parse_one\n"
            "parse_one('SELECT 1', 'bigquery')\n"
        ),
        "tests/dialects/test_bigquery.py": "from sqlglot.dialects.bigquery import BigQuery",
        "tests/dialects/test_clickhouse.py": "from sqlglot.dialects.clickhouse import ClickHouse",
    })


def modified(path, old="old", new="new"):
    return (f"diff --git a/{path} b/{path}\n"
            f"--- a/{path}\n+++ b/{path}\n"
            f"@@ -1 +1 @@\n-{old}\n+{new}\n")


class ShadowModeTests(unittest.TestCase):
    def write_patch(self, root, name, content):
        path = root / f"{name}.patch"
        path.write_text(content, encoding="utf-8")
        return path

    def test_bigquery_optimizer_candidate_union_and_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_patch(Path(directory), "change",
                                    modified("sqlglot/dialects/bigquery.py"))
            report = analyze_patch(path, {"tests": inventory()}, graph())
        self.assertEqual(report["changed_source_files"], ("sqlglot/dialects/bigquery.py",))
        self.assertEqual(len(report["direct_candidate_ids"]), 1)
        self.assertEqual(len(report["dependency_candidate_ids"]), 2)
        self.assertEqual(len(report["combined_candidate_ids"]), 2)
        self.assertEqual(report["candidate_percent"], 66.667)
        self.assertTrue(report["fallback_required"])
        self.assertEqual(len(report["file_impacts"]), 1)

    def test_multiple_changes_rename_and_deduplication(self):
        rename = (
            "diff --git a/sqlglot/dialects/bigquery.py b/sqlglot/dialects/clickhouse.py\n"
            "similarity index 100%\n"
            "rename from sqlglot/dialects/bigquery.py\n"
            "rename to sqlglot/dialects/clickhouse.py\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_patch(Path(directory), "rename",
                                    rename + modified("sqlglot/dialects/bigquery.py"))
            report = analyze_patch(path, {"tests": inventory()}, graph())
        self.assertEqual(len(report["combined_candidate_ids"]), 3)
        self.assertEqual(len(set(report["combined_candidate_ids"])), 3)
        self.assertEqual(len(report["file_impacts"]), 2)
        self.assertEqual(report["file_impacts"][0]["old_path"],
                         "sqlglot/dialects/bigquery.py")
        self.assertTrue(report["fallback_required"])

    def test_unknown_and_missing_mapping_force_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unknown = analyze_patch(self.write_patch(root, "unknown", "not a diff\n"),
                                    {"tests": inventory()}, graph())
            missing = analyze_patch(self.write_patch(root, "missing",
                                                     modified("sqlglot/new_module.py")),
                                    {"tests": inventory()}, graph())
        self.assertTrue(unknown["fallback_required"])
        self.assertTrue(missing["fallback_required"])
        self.assertEqual(unknown["combined_candidate_ids"], ())
        self.assertTrue(missing["fallback_reasons"])

    def test_shared_fixture_and_unresolved_import(self):
        sources = {
            "sqlglot/__init__.py": "def parse_one(sql, read=None): pass",
            "sqlglot/dialects/bigquery.py": "",
            "tests/test_optimizer.py": (
                "from sqlglot import parse_one\n"
                "parse_one('SELECT 1', read=name)\n"
            ),
            "tests/dialects/test_bigquery.py": "",
            "tests/dialects/test_clickhouse.py": "",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_patch(
                Path(directory), "shared",
                modified("tests/fixtures/query.sql") +
                modified("sqlglot/dialects/bigquery.py", "import old", "import new"),
            )
            report = analyze_patch(path, {"tests": inventory()}, build_dependency_map(sources))
        self.assertIn("configuration_or_fixture", report["risk_categories"])
        self.assertIn("import_or_dependency", report["risk_categories"])
        self.assertTrue(any("unresolved dialect argument" in reason
                            for reason in report["unknown_or_unresolved"]))
        self.assertTrue(report["fallback_required"])

    def test_artifact_loads_fixed_source_map(self):
        loaded = load_artifact(Path(__file__).parent / "dependency-map.json.gz")
        self.assertIn("sqlglot/dialects/bigquery.py", loaded.direct_dependencies)

    def test_shadow_writes_only_stderr(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_patch(Path(directory), "change",
                                    modified("sqlglot/dialects/bigquery.py"))
            out, err = io.StringIO(), io.StringIO()
            with patch.object(shadow_mode, "load_artifact", return_value=graph()):
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    run_shadow([path], inventory())
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(len(err.getvalue().splitlines()), 2)
        self.assertIn("shadow_summary", json.loads(err.getvalue().splitlines()[-1]))

    def test_selector_keeps_full_suite_and_jsonl_stdout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "commits").mkdir()
            self.write_patch(root / "commits", "change",
                             modified("sqlglot/dialects/bigquery.py"))
            (root / "tests.json").write_text(json.dumps({"tests": inventory()}), encoding="utf-8")
            out, err = io.StringIO(), io.StringIO()
            with patch.object(selector, "DATA", root), patch.object(shadow_mode, "load_artifact",
                                                                    return_value=graph()):
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    selector.main()
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(set(record), {"commit", "workers"})
        self.assertEqual(sorted(n for worker in record["workers"] for n in worker), [0, 1, 2])
        self.assertIn("shadow_summary", err.getvalue())


if __name__ == "__main__":
    unittest.main()

