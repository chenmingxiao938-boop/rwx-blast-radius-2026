"""Focused tests for the isolated runtime tracer."""

import builtins
import io
import json
import os
from pathlib import Path
import tempfile
import unittest

from runtime_trace_pilot import RuntimeRecorder, select_registered_tests, run_one, FIXTURE_METHODS


class RuntimeTracePilotTests(unittest.TestCase):
    def test_registered_selection_is_unique_and_deterministic(self):
        tests = []
        number = 0
        for filename, count in (("test_clickhouse.py", 11), ("test_sqlite.py", 11),
                                ("test_bigquery.py", 9)):
            for index in range(count):
                tests.append({"number": number, "id": f"tests/dialects/{filename}::T::test_{index}",
                              "file": f"tests/dialects/{filename}", "seconds": index / 100})
                number += 1
        for method in FIXTURE_METHODS:
            tests.append({"number": number, "id": f"tests/test_optimizer.py::T::{method}",
                          "file": "tests/test_optimizer.py", "seconds": 0})
            number += 1
        expected = select_registered_tests({"tests": tests})
        self.assertEqual(len(expected), 32)
        self.assertEqual(len({item["number"] for item in expected}), 32)
        self.assertEqual(expected, select_registered_tests({"tests": list(reversed(tests))}))

    def test_missing_manifest_entries_fail_closed(self):
        with self.assertRaises(ValueError):
            select_registered_tests({"tests": []})

    def test_trace_separates_import_and_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "sqlglot" / "probe.py"
            source.parent.mkdir()
            code = compile("def probe():\n    return 7\nprobe()\n", str(source), "exec")
            with RuntimeRecorder(root) as trace:
                exec(code, {})
                trace.phase = "execution"
                exec(code, {})
            evidence = trace.evidence()
            self.assertIn("sqlglot/probe.py", evidence["calls"]["import"])
            self.assertIn("sqlglot/probe.py", evidence["calls"]["execution"])

    def test_fixture_reads_across_common_apis_and_restoration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = root / "tests" / "fixtures" / "example.sql"
            fixture.parent.mkdir(parents=True)
            fixture.write_text("select 1")
            original_open = builtins.open
            with RuntimeRecorder(root) as trace:
                with open(fixture) as stream:
                    stream.read()
                with io.open(fixture) as stream:
                    stream.read()
                fixture.read_text()
                descriptor = os.open(fixture, os.O_RDONLY)
                os.close(descriptor)
            self.assertIs(builtins.open, original_open)
            self.assertGreaterEqual(len(trace.reads), 4)
            self.assertTrue(all(item["path"] == "tests/fixtures/example.sql" for item in trace.reads))

    def test_failed_fixture_open_is_not_reported_as_a_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            missing = root / "tests" / "fixtures" / "missing.sql"
            with RuntimeRecorder(root) as trace:
                with self.assertRaises(FileNotFoundError):
                    open(missing)
            self.assertEqual(trace.reads, [])

    def test_missing_test_reports_import_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = run_one("tests/does_not_exist.py::Missing::test_absent", temporary)
            self.assertEqual(result["status"], "import_failed")
            json.dumps(result)


if __name__ == "__main__":
    unittest.main()
