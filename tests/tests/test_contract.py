import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


REPOSITORY = Path(__file__).resolve().parents[1]
MAX_STDOUT_BYTES = 2_000_000


def reject_json_constant(value):
    raise ValueError(f"non-standard JSON constant: {value}")


class OutputContractTests(unittest.TestCase):
    def assert_valid_stdout(self, stdout, data):
        self.assertLessEqual(len(stdout), MAX_STDOUT_BYTES, "stdout exceeds 2 MB")
        test_numbers = {
            case["number"] for case in json.loads((data / "tests.json").read_bytes())["tests"]
        }
        expected_commits = {patch.stem for patch in (data / "commits").glob("*.patch")}
        try:
            lines = stdout.decode("utf-8").removesuffix("\n").split("\n") if stdout else []
        except UnicodeDecodeError as error:
            self.fail(f"stdout isn't UTF-8: {error}")
        seen_commits = set()
        for line_number, line in enumerate(lines, start=1):
            try:
                record = json.loads(line, parse_constant=reject_json_constant)
            except ValueError as error:
                self.fail(f"invalid JSON on stdout line {line_number}: {error}")
            self.assertIsInstance(record, dict)
            self.assertEqual(set(record), {"commit", "workers"})
            commit = record["commit"]
            self.assertIsInstance(commit, str)
            self.assertIn(commit, expected_commits)
            self.assertNotIn(commit, seen_commits)
            seen_commits.add(commit)
            workers = record["workers"]
            self.assertIsInstance(workers, list)
            seen_tests = set()
            occupied = 0
            for worker in workers:
                self.assertIsInstance(worker, list)
                occupied += bool(worker)
                for test_id in worker:
                    # The official scorer treats JSON 3.0 as the integer 3.
                    self.assertTrue(
                        type(test_id) is int or (type(test_id) is float and test_id.is_integer())
                    )
                    self.assertIn(test_id, test_numbers)
                    self.assertNotIn(test_id, seen_tests)
                    seen_tests.add(test_id)
            self.assertLessEqual(occupied, 8)
        self.assertEqual(seen_commits, expected_commits)

    def make_data(self, commits=("a1", "b2"), times=(2.5, 1, 0.125)):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        data = Path(scratch.name)
        (data / "commits").mkdir()
        for commit in commits:
            (data / "commits" / f"{commit}.patch").touch()
        tests = [
            {"number": i, "id": f"tests/test_sample.py::test_{i}",
             "file": "tests/test_sample.py", "seconds": seconds}
            for i, seconds in enumerate(times)
        ]
        (data / "tests.json").write_text(json.dumps({"tests": tests}), encoding="utf-8")
        return data

    def run_selector(self, data):
        # Exercise the existing entry point with its data directory redirected
        # in a fresh process. stdout and stderr remain separate byte streams.
        return subprocess.run(
            [sys.executable, "-c",
             "import sys; from pathlib import Path; import selector; "
             "selector.DATA = Path(sys.argv[1]); selector.main()", str(data)],
            cwd=REPOSITORY,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            timeout=15,
        )

    def test_selector_stdout_obeys_contract_with_stderr_captured_separately(self):
        data = self.make_data()
        result = self.run_selector(data)
        self.assert_valid_stdout(result.stdout, data)
        for line in result.stdout.splitlines():
            workers = json.loads(line)["workers"]
            self.assertLessEqual(len(workers), 8)
            self.assertTrue(all(type(test_id) is int for worker in workers for test_id in worker))
        # Starter diagnostics, when present, must stay off the JSON stream.
