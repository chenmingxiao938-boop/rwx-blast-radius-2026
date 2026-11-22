"""Official diagnostic entrypoint for the frozen C12-A experiment."""

import json
from pathlib import Path
import sys
import time

import c10_experiment as c10
from c11a_select import capture_rank, choose
from c11b_evidence import scoped_evidence
from c11b_policy import prune
from c12_noop import assess_patch
from patch_parser import parse_patch
from selector import best_schedule


def main():
    start = time.process_time()
    data, assets, source = Path("/data"), Path("/app"), Path("/opt/sqlglot")
    manifest = json.loads((data / "tests.json").read_text(encoding="utf-8"))
    experiment = c10.Experiment(manifest, assets, source, assets / "fixture_path_shadow_map.json",
                                c10.Mode("combined", fixture="path", adaptive="breadth"))
    graph = experiment.deps["direct_dependencies"]
    schedules = {}

    def schedule(numbers):
        key = tuple(sorted(numbers))
        if key not in schedules:
            schedules[key] = best_schedule([experiment.tests[n] for n in key])
        return schedules[key]

    for path in sorted((data / "commits").glob("*.patch")):
        raw = path.read_text(encoding="utf-8")
        parsed = parse_patch(raw)
        ranking, selected, reasons, risks = capture_rank(experiment, raw, parsed)
        mandatory = set(selected)
        if ranking is not None:
            selected, _ = choose("P-cliff", ranking, experiment.tests, lambda ns: schedule(ns)[1])
            mandatory = set(ranking.order[:ranking.required_count])
            scoped = scoped_evidence(experiment, parsed, ranking, selected)
            selected, _ = prune(selected, mandatory, ranking, scoped, experiment.tests, risks,
                                parsed, lambda ns: schedule(ns)[1])

        assessment = assess_patch(parsed, experiment.source_files, experiment.test_sources, graph, patch_text=raw)
        if assessment["zero_candidate"] and not mandatory and not reasons:
            selected = set()
        if not mandatory.issubset(selected):
            raise ValueError("C12-A lost a mandatory test")
        workers, _ = schedule(selected)
        print(json.dumps({"commit": path.stem, "workers": workers}))

    sys.stdout.flush()
    print(f"selector_cpu_ms={(time.process_time() - start) * 1000:.3f}", file=sys.stderr)


if __name__ == "__main__":
    main()
