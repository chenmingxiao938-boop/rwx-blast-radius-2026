"""Reproducible, answer-free audit of four pinned SQLGlot modules."""

import argparse
import json
import resource
import sys
import time
from collections import Counter
from pathlib import Path

from dialect_impact_map import map_dialect_impacts
from graph_precision import _SourceEvidence, analyze_graph_precision
from shadow_mode import load_artifact


TARGETS = (
    "sqlglot/generators/clickhouse.py",
    "sqlglot/generators/sqlite.py",
    "sqlglot/parsers/bigquery.py",
    "sqlglot/dialects/dialect.py",
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("pinned_source", type=Path)
    parser.add_argument("dependency_artifact", type=Path)
    args = parser.parse_args()
    started = time.process_time()
    metadata = json.loads(args.manifest.read_text(encoding="utf-8"))
    graph = load_artifact(args.dependency_artifact)
    dialect = map_dialect_impacts(TARGETS[:-1], metadata, args.pinned_source)
    dialect_by_path = {item.path: item for item in dialect.file_impacts}
    dynamic = {item.path: item.candidate_numbers for item in dialect.file_impacts}
    impacts = analyze_graph_precision(TARGETS, metadata, graph, args.pinned_source,
        dynamic_candidates=dynamic, dynamic_unresolved=dialect.global_unresolved)
    source = _SourceEvidence(args.pinned_source)
    rows = []
    id_by_number = {entry["number"]: entry["id"] for entry in metadata["tests"]}
    for item in impacts:
        propagated = graph.analyze([item.path], metadata["tests"]).uncertainties
        path_issues = set(item.path_uncertainties)
        new_dynamic = sorted(set(dynamic.get(item.path, ())) - set(item.current_candidate_numbers))
        rows.append({
            "path": item.path,
            "module_candidate_count": len(item.current_candidate_numbers),
            "dynamic_candidate_count": len(dynamic.get(item.path, ())),
            "new_dynamic_candidate_count": len(new_dynamic),
            "new_dynamic_examples": [id_by_number[number] for number in new_dynamic[:12]],
            "new_dynamic_evidence": [
                {"test_id": evidence.test_id, "source": f"{evidence.source_path}:{evidence.line}",
                 "dialect": evidence.dialect, "role": evidence.role, "mechanism": evidence.mechanism}
                for evidence in dialect_by_path.get(item.path, ()).evidence
                if evidence.test_number in new_dynamic
            ][:20] if item.path in dialect_by_path else [],
            "combined_candidate_count": len(item.combined_candidate_numbers),
            "direct_test_count": len(item.direct_candidate_numbers),
            "transitive_test_count": len(item.transitive_candidate_numbers),
            "evidence_subset_count": len(item.evidence_candidate_numbers),
            "conservative_rest_count": len(item.conservative_candidate_numbers),
            "direct_importer_count": len(item.direct_importers),
            "direct_importers": item.direct_importers,
            "transitive_importer_count": item.transitive_importers_count,
            "shared_base_importers": item.shared_base_importers,
            "first_hop_kinds": dict(sorted(Counter(edge.kind for edge in item.first_hop_edges).items())),
            "first_hop_edges": [edge.__dict__ for edge in item.first_hop_edges],
            "example_chains": item.example_chains,
            "example_chain_edges": [
                [source.edge(chain[index], chain[index + 1]).__dict__
                 for index in range(len(chain) - 1)]
                for chain in item.example_chains
            ],
            "global_graph_uncertainty_count": len(item.global_uncertainties),
            "propagated_uncertainty_count": len(propagated),
            "propagated_off_path_count": sum(issue not in path_issues for issue in propagated),
            "propagated_on_path_count": sum(issue in path_issues for issue in propagated),
            "known_path_uncertainty_count": len(item.path_uncertainties),
            "changed_module_uncertainty_count": len(item.changed_module_uncertainties),
            "global_dynamic_uncertainty_count": len(item.dynamic_unresolved),
            "fallback_required": item.fallback_required,
        })
    print(json.dumps(rows, indent=2))
    print(json.dumps({"cpu_ms": round((time.process_time() - started) * 1000, 3),
                      "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}), file=sys.stderr)


if __name__ == "__main__":
    main()
