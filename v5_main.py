"""V5 experimental JSONL selector; all diagnostics go to stderr."""

import gzip
import json
import math
import os
from pathlib import Path
import resource
import sys
import time

from impact_selector import FIXTURE_MODES, METHOD_MODES, analyze_patch
from mixed_patch_impact import MIXED_MODES, analyze_mixed_patch
from selector import best_schedule

DATA = Path("/data")
APP = Path("/app")
COVERAGE_INDEXES = {"baseline": "coverage_index.json.gz",
                    "subprocess": "coverage_index_b31.json.gz"}


def read_gzip(name):
    with gzip.open(APP / name, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def main():
    start = time.process_time()
    fixture_mode = os.environ.get("V5_FIXTURE_MODE", "baseline")
    if fixture_mode not in FIXTURE_MODES:
        raise ValueError(f"unknown fixture mode: {fixture_mode}")
    coverage_mode = os.environ.get("V5_COVERAGE_MODE", "baseline")
    if coverage_mode not in COVERAGE_INDEXES:
        raise ValueError(f"unknown coverage mode: {coverage_mode}")
    method_mode = os.environ.get("V5_METHOD_MODE", "baseline")
    if method_mode not in METHOD_MODES:
        raise ValueError(f"unknown method mode: {method_mode}")
    mixed_mode = os.environ.get("V5_MIXED_MODE", "baseline")
    if mixed_mode not in MIXED_MODES:
        raise ValueError(f"unknown mixed mode: {mixed_mode}")
    manifest = json.loads((DATA / "tests.json").read_text(encoding="utf-8"))
    tests = manifest["tests"]
    coverage = read_gzip(COVERAGE_INDEXES[coverage_mode])
    ranges = read_gzip("source_ranges.json.gz")
    dependencies = read_gzip("dependency-map.json.gz")
    snapshot = read_gzip("source_snapshot.json.gz") if method_mode == "ast_method_body" else None
    schedules = {}
    for patch in sorted((DATA / "commits").glob("*.patch")):
        patch_text = patch.read_text(encoding="utf-8")
        options = dict(fixture_mode=fixture_mode, method_mode=method_mode,
                       source_snapshot=snapshot)
        if mixed_mode == "comment_only_fixture":
            decision = analyze_mixed_patch(
                patch_text, manifest, coverage, ranges, dependencies,
                baseline_analyze=analyze_patch, **options)
        else:
            decision = analyze_patch(patch_text, manifest, coverage, ranges,
                                     dependencies, **options)
        selected = decision.test_numbers
        if selected not in schedules:
            chosen = [tests[number] for number in selected]
            schedules[selected] = best_schedule(chosen)
        workers, cost = schedules[selected]
        print(json.dumps({"commit": patch.stem, "workers": workers}, separators=(",", ":")))
        print(json.dumps({"commit": patch.stem, "fixture_mode": fixture_mode,
                          "coverage_mode": coverage_mode,
                          "method_mode": method_mode,
                          "mixed_mode": mixed_mode,
                          "candidate_count": len(selected),
                          "selected_duration_ms": sum(math.floor(tests[n]["seconds"] * 1000 + 0.5)
                                                      for n in selected),
                          "workers": len(workers), "schedule_cost_ms": cost,
                          "fallback_required": decision.fallback_required,
                          "fallback_reasons": decision.fallback_reasons,
                          "uncertainty": decision.uncertainty}, separators=(",", ":")), file=sys.stderr)
    print(f"selector_cpu_ms={(time.process_time() - start) * 1000:.3f}", file=sys.stderr)
    print(f"selector_peak_rss_kib={resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
