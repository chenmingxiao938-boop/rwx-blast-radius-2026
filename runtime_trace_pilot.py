"""Bounded, offline runtime evidence pilot; never used by the selector."""

import argparse
import builtins
import contextlib
import io
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import time
import unittest


GROUPS = ("test_clickhouse.py", "test_sqlite.py", "test_bigquery.py", "test_optimizer.py")
LIMITS = (10, 10, 8, 4)
FIXTURE_METHODS = ("test_normalize", "test_simplify", "test_canonicalize", "test_qualify_columns__invalid")


def select_registered_tests(manifest):
    """Choose short, diverse registered methods using metadata, never outcomes."""
    tests = manifest["tests"]
    chosen = []
    for name, limit in zip(GROUPS, LIMITS):
        matching = [t for t in tests if t["file"].endswith("/" + name) or t["file"] == "tests/" + name]
        if name == "test_optimizer.py":
            matching = [t for t in matching if t["id"].rsplit("::", 1)[-1] in FIXTURE_METHODS]
        else:
            matching.sort(key=lambda t: (float(t["seconds"]), t["id"]))
        chosen.extend(sorted(matching[:limit], key=lambda t: t["number"]))
    if len(chosen) != 32 or len({t["number"] for t in chosen}) != 32:
        raise ValueError("pilot manifest does not contain 32 unique registered tests")
    return chosen


class RuntimeRecorder:
    def __init__(self, source_root):
        self.root = Path(source_root).resolve()
        self.phase = "import"
        self.lines = {"import": {}, "execution": {}}
        self.calls = {"import": {}, "execution": {}}
        self.reads = []
        self._originals = {}
        self._path_cache = {}

    def _relative(self, path):
        try:
            return Path(path).resolve().relative_to(self.root).as_posix()
        except (OSError, TypeError, ValueError):
            return None

    def trace(self, frame, event, arg):
        if event not in ("call", "line"):
            return self.trace
        filename = frame.f_code.co_filename
        if filename not in self._path_cache:
            self._path_cache[filename] = self._relative(filename)
        path = self._path_cache[filename]
        if path and (path.startswith("sqlglot/") or path.startswith("tests/")):
            table = self.calls if event == "call" else self.lines
            by_file = table[self.phase].setdefault(path, {})
            key = frame.f_code.co_qualname
            if event == "call":
                by_file[key] = by_file.get(key, 0) + 1
            else:
                by_file.setdefault(key, set()).add(frame.f_lineno)
        return self.trace

    def _record_read(self, file, mode, caller):
        if isinstance(file, int):
            return
        path = self._relative(file)
        if path and path.startswith("tests/fixtures/"):
            site = self._relative(caller.f_code.co_filename)
            self.reads.append({"phase": self.phase, "path": path, "mode": str(mode),
                               "site": f"{site}:{caller.f_lineno}"})

    def __enter__(self):
        self._originals = {"open": builtins.open, "io.open": io.open,
                           "os.open": os.open, "Path.open": Path.open}
        recorder = self

        def wrapped_open(file, mode="r", *args, **kwargs):
            handle = recorder._originals["open"](file, mode, *args, **kwargs)
            if "r" in mode or "+" in mode:
                recorder._record_read(file, mode, sys._getframe(1))
            return handle

        def wrapped_io_open(file, mode="r", *args, **kwargs):
            handle = recorder._originals["io.open"](file, mode, *args, **kwargs)
            if "r" in mode or "+" in mode:
                recorder._record_read(file, mode, sys._getframe(1))
            return handle

        def wrapped_os_open(file, flags, *args, **kwargs):
            descriptor = recorder._originals["os.open"](file, flags, *args, **kwargs)
            if flags & os.O_ACCMODE != os.O_WRONLY:
                recorder._record_read(file, flags, sys._getframe(1))
            return descriptor

        def wrapped_path_open(path, mode="r", *args, **kwargs):
            handle = recorder._originals["Path.open"](path, mode, *args, **kwargs)
            if "r" in mode or "+" in mode:
                recorder._record_read(path, mode, sys._getframe(1))
            return handle

        builtins.open = wrapped_open
        io.open = wrapped_io_open
        os.open = wrapped_os_open
        Path.open = wrapped_path_open
        sys.settrace(self.trace)
        return self

    def __exit__(self, *_):
        sys.settrace(None)
        builtins.open = self._originals["open"]
        io.open = self._originals["io.open"]
        os.open = self._originals["os.open"]
        Path.open = self._originals["Path.open"]

    def evidence(self):
        return {"calls": self.calls,
                "lines": {phase: {path: {name: sorted(lines) for name, lines in funcs.items()}
                                  for path, funcs in files.items()}
                          for phase, files in self.lines.items()},
                "reads": sorted(self.reads, key=lambda r: (r["phase"], r["path"], r["site"], r["mode"]))}


def run_one(test_id, source_root):
    module_id = test_id.replace("/", ".").replace(".py::", ".").replace("::", ".")
    before = set(sys.modules)
    cpu_start = time.process_time()
    wall_start = time.perf_counter()
    record = {"id": test_id}
    with RuntimeRecorder(source_root) as trace, contextlib.redirect_stdout(io.StringIO()) as stdout:
        try:
            suite = unittest.defaultTestLoader.loadTestsFromName(module_id)
            trace.phase = "execution"
            result = unittest.TestResult()
            suite.run(result)
            import_error = any(case.__class__.__name__ == "_FailedTest" for case, _ in result.errors)
            record["status"] = "import_failed" if import_error else "passed" if result.wasSuccessful() else "failed"
            record["failures"] = [f"{case}: {detail.strip().splitlines()[-1]}"
                                  for case, detail in result.failures + result.errors]
            record["skipped"] = [str(case) for case, _ in result.skipped]
        except Exception as exc:
            record["status"] = "import_failed" if trace.phase == "import" else "failed"
            record["error"] = f"{type(exc).__name__}: {exc}"
    record["stdout_bytes"] = len(stdout.getvalue().encode())
    record["imported_modules"] = sorted(x for x in set(sys.modules) - before if x.startswith(("sqlglot", "tests")))
    record["cpu_ms"] = round((time.process_time() - cpu_start) * 1000, 3)
    record["wall_ms"] = round((time.perf_counter() - wall_start) * 1000, 3)
    record["peak_rss_kib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    record.update(trace.evidence())
    return record


def run_pilot(manifest_path, source_root):
    with open(manifest_path, encoding="utf-8") as handle:
        selected = select_registered_tests(json.load(handle))
    output = []
    for item in selected:
        command = [sys.executable, "-B", __file__, "single", item["id"], source_root]
        start = time.perf_counter()
        process = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
        try:
            record = json.loads(process.stdout)
        except json.JSONDecodeError:
            record = {"id": item["id"], "status": "child_failed", "stderr": process.stderr[-500:]}
        record["number"] = item["number"]
        record["process_wall_ms"] = round((time.perf_counter() - start) * 1000, 3)
        output.append(record)
    return {"selected": len(selected), "records": output}


def run_controlled_probes(source_root):
    """Exercise real pinned dispatch and fixture loader without external test extras."""
    import importlib

    before = set(sys.modules)
    with RuntimeRecorder(source_root) as trace, contextlib.redirect_stdout(io.StringIO()):
        importlib.import_module("tests.dialects.test_dialect")
        importlib.import_module("tests.helpers")
        imported_at_collection = set(sys.modules) - before
        trace.phase = "execution"
        from sqlglot import parse_one
        for dialect in ("clickhouse", "sqlite", "bigquery"):
            parse_one("SELECT 1", read=dialect).sql(dialect=dialect)
        from tests.helpers import load_sql_fixture_pairs
        fixture = next(load_sql_fixture_pairs("optimizer/normalize.sql"), None)
    evidence = trace.evidence()
    evidence["imported_at_collection"] = sorted(m for m in imported_at_collection if m.startswith("sqlglot"))
    evidence["fixture_loaded"] = fixture is not None
    evidence["peak_rss_kib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return evidence


def run_baseline(manifest_path):
    """Use the same child-process isolation without instrumentation."""
    with open(manifest_path, encoding="utf-8") as handle:
        selected = select_registered_tests(json.load(handle))
    records = []
    for item in selected:
        if item["file"].endswith(("test_bigquery.py", "test_optimizer.py")):
            continue  # Their imports are unavailable in the pilot image.
        name = item["id"].replace("/", ".").replace(".py::", ".").replace("::", ".")
        before = resource.getrusage(resource.RUSAGE_CHILDREN)
        start = time.perf_counter()
        process = subprocess.run([sys.executable, "-B", "-m", "unittest", "-q", name],
                                 capture_output=True, text=True, timeout=30, check=False)
        after = resource.getrusage(resource.RUSAGE_CHILDREN)
        records.append({"number": item["number"], "passed": process.returncode == 0,
                        "wall_ms": round((time.perf_counter() - start) * 1000, 3),
                        "cpu_ms": round(((after.ru_utime + after.ru_stime) -
                                          (before.ru_utime + before.ru_stime)) * 1000, 3)})
    return {"selected": len(records), "records": records}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("single", "pilot", "probe", "baseline"))
    parser.add_argument("first")
    parser.add_argument("source_root")
    args = parser.parse_args()
    result = (run_one(args.first, args.source_root) if args.mode == "single" else
              run_pilot(args.first, args.source_root) if args.mode == "pilot" else
              run_baseline(args.first) if args.mode == "baseline" else
              run_controlled_probes(args.source_root))
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
