"""C11-A research runner. Read C10's ranking without changing the D40 entrypoint."""

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
from statistics import median
import time
from unittest.mock import patch

import c10_experiment as c10
from c10_budget import COVERAGE, Signals
from patch_parser import parse_patch
from selector import best_schedule


FIXED = {f"D{number}": number for number in range(8, 41, 4)}
MODES = (*FIXED, "P-direct", "P-cliff", "P-substitute")
DIRECT_LINES = {"line", "branch"}
STRONG = COVERAGE | {"caller"}
CAP = 40


@dataclass
class Ranking:
    order: list[int]
    required_count: int
    scores: dict[int, float]
    evidence: dict[int, set[str]]
    signals: Signals


def capture_rank(experiment, text, parsed):
    ranking = None

    def observe(_policy, order, required_count, scores, evidence, signals, _schedule):
        nonlocal ranking
        ranking = Ranking(order, required_count, scores, evidence, signals)
        return len(order)

    # This seam is used only by the single-threaded research runner. The same
    # C10 function computes evidence, scores and the complete original order.
    with patch.object(c10, "choose_budget", observe):
        selected, fallback, risks = experiment.select(text, parsed, None)
    if ranking is None and not fallback:
        raise ValueError("C10 returned no ranking or fallback")
    return ranking, selected, fallback, risks


def _last_supported(ranking, limit, signals):
    return max((i for i, n in enumerate(ranking.order[:limit], 1)
                if ranking.evidence.get(n, set()) & signals), default=0)


def direct_budget(ranking, limit):
    if ranking.signals.structural:
        return limit
    last = _last_supported(ranking, limit, COVERAGE)
    return max(min(8, limit), ranking.required_count, last)


def cliff_budget(ranking, limit):
    if ranking.signals.structural:
        return limit
    floor = max(min(4, limit), ranking.required_count, _last_supported(ranking, limit, DIRECT_LINES))
    # Do not discard a broad observed cohort when multiple tests share the
    # changed symbol but line attribution is sparse.
    if ranking.signals.coverage_breadth > (CAP // 2):
        floor = max(floor, _last_supported(ranking, limit, COVERAGE))
    recent = ranking.order[ranking.required_count:ranking.required_count + 8]
    if not recent:
        return floor
    top = median(ranking.scores[n] for n in recent)
    for stop in range(floor, limit):
        previous, following = ranking.order[stop - 1:stop + 1]
        if ranking.scores[following] < min(.7 * ranking.scores[previous], .5 * top):
            return stop
    return max(floor, direct_budget(ranking, limit))


def substitute(ranking, picked, tests, schedule_cost):
    """Try replacing weak evidence with strictly stronger evidence in one test family."""
    if ranking.signals.structural:
        return picked, []
    mandatory = set(ranking.order[:ranking.required_count])
    selected = set(picked)
    extras = [n for n in ranking.order if n not in selected and ranking.evidence.get(n, set()) & STRONG]
    if not extras:
        return picked, []
    family = {n: tests[n]["id"].rsplit("::", 1)[0] for n in ranking.order}
    swaps = []
    cost = schedule_cost(selected)
    for old in sorted(picked, key=lambda n: (-tests[n]["seconds"], n)):
        old_evidence = ranking.evidence.get(old, set())
        if old in mandatory or old_evidence & STRONG:
            continue
        alternatives = [n for n in extras if n not in selected and family[n] == family[old]
                        and tests[n]["seconds"] < tests[old]["seconds"]
                        and old_evidence < ranking.evidence[n]]
        for new in alternatives:
            trial = selected - {old} | {new}
            new_cost = schedule_cost(trial)
            if new_cost < cost:
                swaps.append({"old": old, "new": new, "schedule_saving_ms": cost - new_cost})
                selected = trial
                cost = new_cost
                break
    return sorted(selected), swaps


def choose(mode, ranking, tests, schedule_cost):
    cap = min(len(ranking.order), max(CAP, ranking.required_count))
    if mode in FIXED:
        budget = max(FIXED[mode], ranking.required_count)
    elif mode == "P-direct":
        budget = direct_budget(ranking, cap)
    elif mode == "P-cliff":
        budget = cliff_budget(ranking, cap)
    elif mode == "P-substitute":
        picked = ranking.order[:cap]
        return substitute(ranking, picked, tests, schedule_cost)
    else:
        raise ValueError(f"unknown C11 mode: {mode}")
    return ranking.order[:budget], []


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True, choices=MODES)
    parser.add_argument("--data", type=Path, default=Path("/data"))
    parser.add_argument("--source", type=Path, default=Path("/opt/sqlglot"))
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--runs", type=Path, required=True)
    parser.add_argument("--record-ranks", action="store_true")
    args = parser.parse_args()

    started = time.process_time()
    manifest = json.loads((args.data / "tests.json").read_text(encoding="utf-8"))
    experiment = c10.Experiment(manifest, args.assets, args.source,
                                args.assets / "fixture_path_shadow_map.json",
                                c10.Mode("combined", fixture="path", adaptive="breadth"))
    startup = time.process_time() - started
    records, diagnostics, ranks, schedules = [], [], [], {}

    def schedule(numbers):
        key = tuple(sorted(numbers))
        if key not in schedules:
            schedules[key] = best_schedule([experiment.tests[n] for n in key])
        return schedules[key]

    for file in sorted((args.data / "commits").glob("*.patch")):
        text = file.read_text(encoding="utf-8")
        parsed = parse_patch(text)
        ranking, selected, reasons, risks = capture_rank(experiment, text, parsed)
        swaps = []
        if ranking is not None:
            selected, swaps = choose(args.mode, ranking, experiment.tests, lambda ns: schedule(ns)[1])
            if not set(ranking.order[:ranking.required_count]).issubset(selected):
                raise ValueError("C11 policy dropped a mandatory test")
        workers, cost = schedule(selected)
        records.append({"commit": file.stem, "workers": workers})
        diagnostics.append({"commit": file.stem, "fallback": bool(reasons),
                            "fallback_reasons": list(reasons), "fixture_only": c10.is_fixture_only(parsed),
                            "selected_count": len(selected), "schedule_cost_ms": cost, "risks": risks,
                            "required_count": ranking.required_count if ranking else 0, "swaps": swaps})
        if args.record_ranks:
            ranks.append({"commit": file.stem, "ranked": ranking.order if ranking else [],
                          "fallback_reasons": list(reasons)})

    args.runs.mkdir(parents=True, exist_ok=True)
    (args.runs / f"{args.mode}.jsonl").write_text("".join(json.dumps(row) + "\n" for row in records), encoding="utf-8")
    cpu = time.process_time() - started
    (args.runs / f"{args.mode}.metrics.json").write_text(json.dumps({
        "cpu_seconds": cpu, "startup_cpu_seconds": startup, "patches": diagnostics,
    }, indent=2) + "\n", encoding="utf-8")
    if args.record_ranks:
        (args.runs / f"{args.mode}.ranks.json").write_text(json.dumps(ranks) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
