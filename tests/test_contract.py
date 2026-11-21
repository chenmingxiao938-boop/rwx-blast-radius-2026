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
        try:
            result = subprocess.run(
                [sys.executable, "-E", "-B", "-c",
                 "import sys; from pathlib import Path; import selector; "
                 "selector.DATA = Path(sys.argv[1]); selector.main()", str(data)],
                cwd=REPOSITORY,
                capture_output=True,
                timeout=15,
            )
        except subprocess.TimeoutExpired as error:
            stderr = (error.stderr or b"").decode("utf-8", errors="replace")
            self.fail(f"selector timed out after {error.timeout} seconds\nstderr:\n{stderr}")
        self.assertEqual(
            result.returncode, 0,
            f"selector exited unsuccessfully\nstderr:\n{result.stderr.decode('utf-8', errors='replace')}",
        )
        return result

    def test_selector_stdout_obeys_contract_with_stderr_captured_separately(self):
        data = self.make_data()
        result = self.run_selector(data)
        self.assert_valid_stdout(result.stdout, data)
        for line in result.stdout.splitlines():
            workers = json.loads(line)["workers"]
            self.assertLessEqual(len(workers), 8)
            self.assertTrue(all(type(test_id) is int for worker in workers for test_id in worker))
        # Starter diagnostics, when present, must stay off the JSON stream.
        starter_log = b"selected 3 tests on every commit"
        if starter_log in result.stdout or starter_log in result.stderr:
            self.assertIn(starter_log, result.stderr)

    def test_selector_output_with_empty_test_inventory(self):
        data = self.make_data(times=())
        self.assert_valid_stdout(self.run_selector(data).stdout, data)

    def test_selector_failure_reports_stderr(self):
        data = self.make_data()
        (data / "tests.json").write_bytes(b"{")
        with self.assertRaisesRegex(AssertionError, r"(?s)stderr:\n.*JSONDecodeError"):
            self.run_selector(data)

    def test_empty_workers_and_whole_number_json_are_valid(self):
        data = self.make_data()
        self.assert_valid_stdout(
            b'{"commit":"b2","workers":[[2.0],[],[],[],[],[],[],[],[]]}\n'
            b'{"commit":"a1","workers":[]}', data
        )

    def test_empty_batch_has_no_stdout(self):
        data = self.make_data(commits=())
        self.assert_valid_stdout(self.run_selector(data).stdout, data)

    def test_wrong_shapes_and_test_numbers_are_rejected(self):
        data = self.make_data(commits=("a1",), times=(1,) * 9)
        invalid = (
            b'{"commit":"a1"}',
            b'{"commit":"a1","workers":[],"extra":0}',
            b'{"commit":"a1","workers":{}}',
            b'{"commit":"a1","workers":[0]}',
            b'{"commit":"a1","workers":[[true]]}',
            b'{"commit":"a1","workers":[["1"]]}',
            b'{"commit":"a1","workers":[[1.25]]}',
            b'{"commit":"a1","workers":[[NaN]]}',
            b'{"commit":"a1","workers":[[Infinity]]}',
            b'{"commit":"a1","workers":[[9]]}',
            b'{"commit":"a1","workers":[[1,1]]}',
            b'{"commit":"a1","workers":[[1],[1]]}',
            b'{"commit":"a1","workers":[[0],[1],[2],[3],[4],[5],[6],[7],[8]]}',
        )
        for stdout in invalid:
            with self.subTest(stdout=stdout), self.assertRaises(AssertionError):
                self.assert_valid_stdout(stdout, data)

    def test_invalid_commit_coverage_and_extra_stdout_are_rejected(self):
        data = self.make_data(commits=("a1",))
        answer = b'{"commit":"a1","workers":[]}\n'
        invalid = (b"", answer * 2, answer.replace(b"a1", b"bad"),
                   b"DEBUG: loaded tests\n" + answer, answer + b"\n",
                   b"\xef\xbb\xbf" + answer, answer + b"\xff")
        for stdout in invalid:
            with self.subTest(stdout=stdout), self.assertRaises(AssertionError):
                self.assert_valid_stdout(stdout, data)

    def test_batch_stdout_cap_is_in_bytes(self):
        data = self.make_data(commits=("café", "b2"))
        answers = ('{"commit":"café","workers":[]}\n'
                   '{"commit":"b2","workers":[]}').encode("utf-8")
        at_limit = b" " * (MAX_STDOUT_BYTES - len(answers)) + answers
        self.assert_valid_stdout(at_limit, data)
        with self.assertRaises(AssertionError):
            self.assert_valid_stdout(b" " + at_limit, data)


if __name__ == "__main__":
    unittest.main()
