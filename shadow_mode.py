"""Offline dependency artifact and non-selecting Shadow Mode diagnostics."""

from __future__ import annotations

import argparse
import gzip
import json
import resource
import subprocess
import sys
import time
from collections import Counter, defaultdict
from dataclasses import asdict
from pathlib import Path
from types import MappingProxyType

from dependency_map import DependencyMap, Uncertainty, analyze_with_mapper, scan_dependency_map
from patch_parser import parse_patch

PINNED_COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
ARTIFACT = Path("/app/dependency-map.json.gz")


def write_artifact(source_root: Path, output: Path) -> None:
    """Build only from the official pinned source and tests checkout."""
    if not (source_root / "sqlglot" / "__init__.py").is_file() or not (source_root / "tests").is_dir():
        raise ValueError("expected pinned sqlglot source and tests")
    commit = subprocess.run(
        ["git", "-C", str(source_root), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    if commit != PINNED_COMMIT:
        raise ValueError("source checkout is not the official pinned commit")
    graph = scan_dependency_map(source_root)
    document = {
        "commit": PINNED_COMMIT,
        "direct_dependencies": {path: list(edges) for path, edges in graph.direct_dependencies.items()},
        "uncertainties": [list(issue) for issue in graph.uncertainties],
    }
    payload = json.dumps(document, separators=(",", ":"), sort_keys=True).encode("utf-8")
    output.write_bytes(gzip.compress(payload, mtime=0))


def load_artifact(path: Path = ARTIFACT) -> DependencyMap:
    document = json.loads(gzip.decompress(path.read_bytes()))
    if document.get("commit") != PINNED_COMMIT:
        raise ValueError("dependency map is not from the pinned sqlglot commit")
    edges = document["direct_dependencies"]
    if not isinstance(edges, dict) or not all(
        isinstance(path, str) and isinstance(deps, list) and
        all(isinstance(dep, str) for dep in deps)
        for path, deps in edges.items()
    ):
        raise ValueError("invalid dependency map")
    reverse: dict[str, set[str]] = defaultdict(set)
    for path, deps in edges.items():
        for dep in deps:
            if dep not in edges:
                raise ValueError(f"dependency map has missing module: {dep}")
            reverse[dep].add(path)
    uncertainties = tuple(Uncertainty(*entry) for entry in document["uncertainties"])
    return DependencyMap(
        MappingProxyType({path: tuple(deps) for path, deps in edges.items()}),
        MappingProxyType({path: tuple(sorted(importers)) for path, importers in reverse.items()}),
        uncertainties,
    )


def analyze_patch(path: Path, metadata: dict, graph: DependencyMap) -> dict:
    """Return complete positive evidence; never a test-selection decision."""
    patch = parse_patch(path.read_text(encoding="utf-8"))
    impact = analyze_with_mapper(graph, patch, metadata)
    mapping, dependency = impact.mapping, impact.dependency
    id_by_number = {test["number"]: test["id"] for test in metadata["tests"]}
    dependency_ids = tuple(sorted(id_by_number[n] for n in dependency.candidate_test_ids))
    unresolved = tuple(sorted({
        f"{issue.path}:{issue.line}: {issue.detail}" for issue in dependency.uncertainties
    }))
    fallback = set(mapping.fallback_reasons)
    fallback.update(unresolved)
    total = len(metadata["tests"])
    changed_paths = tuple(sorted(patch.changed_paths))
    return {
        "commit": path.stem,
        "changed_paths": changed_paths,
        "changed_source_files": tuple(p for p in changed_paths if p.startswith("sqlglot/")),
        "direct_candidate_ids": mapping.candidate_ids,
        "dependency_candidate_ids": dependency_ids,
        "combined_candidate_ids": impact.candidate_test_ids,
        "candidate_count": len(impact.candidate_test_ids),
        "candidate_percent": round(100 * len(impact.candidate_test_ids) / total, 3) if total else 0.0,
        "risk_categories": mapping.categories,
        "unknown_or_unresolved": unresolved,
        "fallback_required": impact.fallback_required,
        "fallback_reasons": tuple(sorted(fallback)),
        "file_impacts": tuple(asdict(item) for item in mapping.file_impacts),
    }


def aggregate(reports: list[dict]) -> dict:
    counts = sorted(report["candidate_count"] for report in reports)
    reasons = Counter(
        issue.split(": ", 1)[-1]
        for report in reports
        for issue in report["unknown_or_unresolved"]
    )
    return {
        "patches": len(reports),
        "candidate_count_min": counts[0] if counts else 0,
        "candidate_count_max": counts[-1] if counts else 0,
        "candidate_count_mean": round(sum(counts) / len(counts), 2) if counts else 0,
        "fallback_count": sum(report["fallback_required"] for report in reports),
        "top_uncertainties": reasons.most_common(10),
    }


def run_shadow(patches: list[Path], tests: list[dict], artifact: Path | None = None,
               *, full: bool = False) -> None:
    """Write JSON diagnostics to stderr while leaving stdout to the selector."""
    start_cpu = time.process_time_ns()
    start_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    metadata = {"tests": tests}
    reports: list[dict] = []
    try:
        graph = load_artifact(artifact or ARTIFACT)
        for patch in patches:
            try:
                report = analyze_patch(patch, metadata, graph)
                reports.append(report)
                record = report if full else {
                    key: report[key] for key in (
                        "commit", "changed_source_files", "candidate_count",
                        "candidate_percent", "risk_categories", "fallback_required",

                    )
                }
                if not full:
                    record["fallback_reason_count"] = len(report["fallback_reasons"])
                    record["fallback_reason_examples"] = report["fallback_reasons"][:3]
                    record["unresolved_count"] = len(report["unknown_or_unresolved"])
            except Exception as error:
                record = {
                    "commit": patch.stem, "fallback_required": True,
                    "shadow_error": f"{type(error).__name__}: {error}",
                }
            print(json.dumps({"shadow": record}, sort_keys=True), file=sys.stderr)
    except Exception as error:
        for patch in patches:
            print(json.dumps({"shadow": {
                "commit": patch.stem, "fallback_required": True,
                "shadow_error": f"{type(error).__name__}: {error}",
            }}, sort_keys=True), file=sys.stderr)
    summary = aggregate(reports)
    summary["shadow_cpu_ms"] = round((time.process_time_ns() - start_cpu) / 1_000_000, 3)
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    summary["process_peak_rss_kib"] = peak
    summary["shadow_peak_rss_delta_kib"] = max(0, peak - start_rss)
    print(json.dumps({"shadow_summary": summary}, sort_keys=True), file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("source_root", type=Path)
    build.add_argument("artifact", type=Path)
    analyze = commands.add_parser("analyze")
    analyze.add_argument("data", type=Path)
    analyze.add_argument("artifact", type=Path)
    analyze.add_argument("--full", action="store_true")
    args = parser.parse_args()
    if args.command == "build":
        write_artifact(args.source_root, args.artifact)
    else:
        tests = json.loads((args.data / "tests.json").read_text(encoding="utf-8"))["tests"]
        run_shadow(sorted((args.data / "commits").glob("*.patch")), tests, args.artifact,
                   full=args.full)


if __name__ == "__main__":
    main()

