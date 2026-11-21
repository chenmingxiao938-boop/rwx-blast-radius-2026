"""Optional build-time probe for a file-to-pytest-node coverage map.

Requires pytest and coverage.py in the build environment. The runtime artifact
is gzip JSON and can be read using only the Python standard library.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
from collections import defaultdict
from pathlib import Path


def collect(root: Path, output: Path, pytest_args: list[str]) -> int:
    import coverage
    import pytest

    root = root.resolve()
    output = output.resolve()
    if not (root / "sqlglot" / "__init__.py").is_file() or not (root / "tests").is_dir():
        raise ValueError("expected the root of the pinned sqlglot checkout")
    recorder = coverage.Coverage(source=[str(root / "sqlglot")], data_file=None, config_file=False)

    class Collector:
        def __init__(self):
            self.collected = set()
            self.passed = set()
            self.incomplete = set()

        def pytest_collection_finish(self, session):
            self.collected.update(item.nodeid for item in session.items)

        @pytest.hookimpl(hookwrapper=True, tryfirst=True)
        def pytest_runtest_protocol(self, item):
            recorder.switch_context(item.nodeid)
            try:
                yield
            finally:
                recorder.switch_context("")

        def pytest_runtest_logreport(self, report):
            if report.when == "call" and report.passed:
                self.passed.add(report.nodeid)
            if not report.passed or hasattr(report, "wasxfail"):
                self.incomplete.add(report.nodeid)

    plugin = Collector()
    previous_dir = Path.cwd()
    sys.path.insert(0, str(root))
    try:
        os.chdir(root)
        with recorder.collect():
            status = pytest.main([*pytest_args, "tests"], plugins=[plugin])
    finally:
        os.chdir(previous_dir)
        sys.path.remove(str(root))
    if status != pytest.ExitCode.OK:
        return int(status)

    data = recorder.get_data()
    startup_files = []
    files_to_tests = defaultdict(set)
    for file in sorted(data.measured_files()):
        relative = Path(file).resolve().relative_to(root).as_posix()
        contexts = {context for labels in data.contexts_by_lineno(file).values() for context in labels}
        if "" in contexts:
            startup_files.append(relative)
        files_to_tests[relative].update(contexts - {""})
    document = {
        "files_to_test_node_ids": {file: sorted(nodes) for file, nodes in files_to_tests.items()},
        "startup_files": sorted(startup_files),
        "collected_test_node_ids": sorted(plugin.collected),
        "incomplete_test_node_ids": sorted(plugin.collected - plugin.passed | plugin.incomplete),
    }
    payload = json.dumps(document, separators=(",", ":"), sort_keys=True).encode("utf-8")
    output.write_bytes(gzip.compress(payload, mtime=0))
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    raise SystemExit(collect(args.root, args.output, args.pytest_args))
