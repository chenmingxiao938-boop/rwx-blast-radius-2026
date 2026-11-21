"""Offline research report for fixture-only patches; never reads answers.json."""

import argparse
import json
import resource
import sys
import time
from pathlib import Path

from fixture_impact_map import map_fixture_impacts
from patch_parser import parse_patch


def main() -> None:
    started = time.process_time()
    parser = argparse.ArgumentParser()
    parser.add_argument("sample", type=Path)
    parser.add_argument("pinned_source", type=Path)
    args = parser.parse_args()
    metadata = json.loads((args.sample / "tests.json").read_text(encoding="utf-8"))
    fixture_patches = []
    for patch_file in sorted((args.sample / "commits").glob("*.patch")):
        patch = parse_patch(patch_file.read_text(encoding="utf-8"))
        paths = sorted(patch.changed_paths)
        if not paths or not all(path.startswith("tests/fixtures/") for path in paths):
            continue
        fixture_patches.append((patch_file.stem, paths, bool(patch.unknown_reasons)))
    all_paths = sorted({path for _, paths, _ in fixture_patches for path in paths})
    all_impacts = map_fixture_impacts(all_paths, metadata, args.pinned_source)
    impact_by_path = {item.path: item for item in all_impacts.file_impacts}
    reports = []
    for commit, paths, parser_unknown in fixture_patches:
        impacts = [impact_by_path[path] for path in paths]
        reports.append({
            "commit": commit,
            "paths": paths,
            "parser_unknown": parser_unknown,
            "candidate_numbers": sorted({number for item in impacts for number in item.candidate_numbers}),
            "candidate_ids": sorted({test_id for item in impacts for test_id in item.candidate_ids}),
            "evidence": [
                {"path": item.path, "reader": evidence.test_id,
                 "source": f"{evidence.source_path}:{evidence.line}", "method": evidence.method}
                for item in impacts for evidence in item.evidence
            ],
            "unresolved": sorted({reason for item in impacts for reason in item.unresolved}),
            "fallback_required": all_impacts.fallback_required,
        })
    print(json.dumps(reports, ensure_ascii=False, indent=2))
    print(json.dumps({"cpu_ms": round((time.process_time() - started) * 1000, 3),
                      "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}), file=sys.stderr)


if __name__ == "__main__":
    main()
