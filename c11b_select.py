"""C11-B research runner, separate from the official D40 entrypoint."""

import argparse
import json
from pathlib import Path
import sys
import time

import c10_experiment as c10
from c11a_select import capture_rank, choose
from c11b_evidence import scoped_evidence
from c11b_policy import prune
from patch_parser import parse_patch
from selector import best_schedule


def run(mode, data, source, assets, runs):
    started = time.process_time()
    manifest = json.loads((data / "tests.json").read_text(encoding="utf-8"))
    experiment = c10.Experiment(manifest, assets, source, assets / "fixture_path_shadow_map.json",
                                c10.Mode("combined", fixture="path", adaptive="breadth"))
    startup = time.process_time() - started
    schedules, outputs, diagnostics, audit = {}, [], [], []

    def schedule(numbers):
        key = tuple(sorted(numbers))
        if key not in schedules:
            schedules[key] = best_schedule([experiment.tests[n] for n in key])
        return schedules[key]

    for patch_file in sorted((data / "commits").glob("*.patch")):
        text = patch_file.read_text(encoding="utf-8")
        patch = parse_patch(text)
        ranking, selected, reasons, risks = capture_rank(experiment, text, patch)
        if ranking is None:
            mandatory = set(selected)
            scoped = {}
        else:
            selected, _ = choose("P-cliff", ranking, experiment.tests, lambda ids: schedule(ids)[1])
            mandatory = set(ranking.order[:ranking.required_count])
            scoped = scoped_evidence(experiment, patch, ranking, selected)
        baseline_cost = schedule(selected)[1]
        removed = []
        if mode == "evidence" and ranking is not None:
            selected, removed = prune(selected, mandatory, ranking, scoped, experiment.tests, risks,
                                      patch, lambda ids: schedule(ids)[1])
            if not mandatory <= set(selected):
                raise ValueError("C11-B dropped a mandatory test")
        optional = set(selected) - mandatory
        workers, cost = schedule(selected)
        outputs.append({"commit": patch_file.stem, "workers": workers})
        diagnostics.append({"commit": patch_file.stem, "fixture_only": c10.is_fixture_only(patch),
                            "fallback": bool(reasons), "fallback_reasons": reasons, "risks": risks,
                            "selected_count": len(selected), "required_count": len(mandatory),
                            "optional_count": len(optional), "schedule_cost_ms": cost,
                            "baseline_schedule_ms": baseline_cost, "saving_ms": baseline_cost - cost,
                            "removed_tests": removed})
        if mode == "audit":
            expensive = sorted(optional, key=lambda n: (-experiment.tests[n]["seconds"], n))[:5]
            audit.append({"commit": patch_file.stem, "paths": sorted(patch.changed_paths),
                          "current_schedule_ms": cost, "mandatory_schedule_ms": schedule(mandatory)[1],
                          "mandatory_count": len(mandatory), "optional_count": len(optional),
                          "expensive_optional": [
                              {"number": n, "id": experiment.tests[n]["id"], "seconds": experiment.tests[n]["seconds"],
                               "evidence": sorted(ranking.evidence.get(n, ())),
                               "scoped_evidence": sorted(scoped.get(n, ())),
                               "marginal_ms": cost - schedule(set(selected) - {n})[1]}
                              for n in expensive]})

    jsonl = "".join(json.dumps(row) + "\n" for row in outputs)
    if runs is None:
        sys.stdout.write(jsonl)
        sys.stdout.flush()
        print(f"selector_cpu_ms={(time.process_time() - started) * 1000:.3f}", file=sys.stderr)
        return
    runs.mkdir(parents=True, exist_ok=True)
    (runs / f"{mode}.jsonl").write_text(jsonl, encoding="utf-8")
    (runs / f"{mode}.metrics.json").write_text(json.dumps({
        "cpu_seconds": time.process_time() - started, "startup_cpu_seconds": startup, "patches": diagnostics,
    }, indent=2) + "\n", encoding="utf-8")
    if mode == "audit":
        (runs / "source-audit.json").write_text(json.dumps(audit, indent=2) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", default="evidence", choices=("audit", "evidence"))
    parser.add_argument("--data", type=Path, default=Path("/data"))
    parser.add_argument("--source", type=Path, default=Path("/opt/sqlglot"))
    parser.add_argument("--assets", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--runs", type=Path, help="write offline experiment artifacts instead of stdout")
    args = parser.parse_args()
    if args.mode == "audit" and args.runs is None:
        parser.error("--mode audit requires --runs")
    run(args.mode, args.data, args.source, args.assets, args.runs)


if __name__ == "__main__":
    main()
