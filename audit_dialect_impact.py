"""Compare source-derived dispatch evidence with the existing import graph."""

import argparse
import json
import resource
import sys
import time
from collections import Counter
from pathlib import Path

from dialect_impact_map import map_dialect_impacts
from shadow_mode import load_artifact


TARGETS = (
    "sqlglot/parsers/singlestore.py",
    "sqlglot/dialects/materialize.py",
    "sqlglot/parsers/bigquery.py",
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
    dynamic = map_dialect_impacts(TARGETS, metadata, args.pinned_source)
    id_by_number = {entry["number"]: entry["id"] for entry in metadata["tests"]}
    reports = []
    for impact in dynamic.file_impacts:
        static = graph.analyze([impact.path], metadata["tests"])
        static_ids = {id_by_number[number] for number in static.candidate_test_ids}
        dynamic_ids = set(impact.candidate_ids)
        added = sorted(dynamic_ids - static_ids)
        reports.append({
            "path": impact.path,
            "dialects": impact.referenced_dialects,
            "static_count": len(static_ids),
            "dynamic_count": len(dynamic_ids),
            "new_dynamic_count": len(added),
            "new_dynamic_ids": added,
            "combined_count": len(static_ids | dynamic_ids),
            "evidence_count": len(impact.evidence),
            "evidence_for_new": [
                {"test_id": evidence.test_id, "dialect": evidence.dialect,
                 "role": evidence.role, "source": f"{evidence.source_path}:{evidence.line}",
                 "mechanism": evidence.mechanism}
                for evidence in impact.evidence if evidence.test_id in added
            ],
            "unresolved_count": len(impact.unresolved),
            "global_unresolved_count": len(dynamic.global_unresolved),
            "global_unresolved_kinds": dict(sorted(Counter(
                reason.split(" in ", 1)[0].split(" at ", 1)[0]
                for reason in dynamic.global_unresolved
            ).items())),
            "unresolved_kinds": dict(sorted(Counter(
                reason.split(" in ", 1)[0].split(" at ", 1)[0]
                for reason in impact.unresolved
            ).items())),
            "unresolved_reasons": impact.unresolved,
            "fallback_required": dynamic.fallback_required,
        })
    print(json.dumps(reports, ensure_ascii=False, indent=2))
    print(json.dumps({"cpu_ms": round((time.process_time() - started) * 1000, 3),
                      "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}), file=sys.stderr)


if __name__ == "__main__":
    main()
