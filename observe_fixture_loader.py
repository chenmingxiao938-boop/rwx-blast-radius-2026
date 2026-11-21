"""Observe pinned helper file opens without running the full sqlglot tests."""

import argparse
import builtins
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

from patch_parser import parse_patch


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("sample", type=Path)
    parser.add_argument("pinned_source", type=Path)
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location("pinned_test_helpers", args.pinned_source / "tests/helpers.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load pinned helper")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fixture_paths = sorted({
        path for patch_file in (args.sample / "commits").glob("*.patch")
        for path in parse_patch(patch_file.read_text(encoding="utf-8")).changed_paths
        if path.startswith("tests/fixtures/") and path.endswith(".sql")
    })
    original_open = builtins.open
    observed = []

    def traced_open(file, *args, **kwargs):
        observed.append(str(Path(file).resolve()))
        return original_open(file, *args, **kwargs)

    with patch("builtins.open", traced_open):
        for path in fixture_paths:
            relative = path.removeprefix("tests/fixtures/")
            loader = module.load_sql_fixtures if relative.endswith("__invalid.sql") else module.load_sql_fixture_pairs
            next(loader(relative), None)
    print(json.dumps({"fixture_count": len(fixture_paths), "observed_paths": sorted(set(observed))}, indent=2))


if __name__ == "__main__":
    main()
