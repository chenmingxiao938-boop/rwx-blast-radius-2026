"""Offline static test selection experiments. This module never reads labels."""

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
import gzip
import json
import math
from pathlib import Path, PurePosixPath
import sys
import time

from c10_ast import analyze_region, call_index, caller_distances, _insertion_anchors
from c10_binary_budget import POLICIES, binary_budget, collect_signals
from c10_budget import COVERAGE, STATIC, Signals, choose_budget
from impact_selector import PINNED_COMMIT, analyze_patch, resolve_fixture_readers
from mixed_patch_impact import analyze_mixed_patch
from patch_parser import parse_patch
from selector import best_schedule


@dataclass(frozen=True)
class Mode:
    ranking: str
    budget: int | None = None
    fallback: str = "evidence"
    fixture: str = "shared"
    adaptive: str | None = None
    binary: str | None = None


MODES = {
    "C5": Mode("c5"),
    "A": Mode("symbol"),
    "B": Mode("ast"),
    "B-guarded": Mode("ast", fallback="structural"),
    "C": Mode("semantic", 64),
    "D8": Mode("combined", 8, fixture="path"),
    "D32": Mode("combined", 32, fixture="path"),
    "D128": Mode("combined", 128, fixture="path"),
    "D32-shared": Mode("combined", 32),
    "D32-guarded": Mode("combined", 32, fallback="structural"),
}
MODES.update({f"D{budget}": Mode("combined", budget, fixture="path")
              for budget in (40, 48, 64, 80, 96, 112)})
for _policy in ("breadth", "relevance", "cost"):
    MODES[f"E-{_policy}"] = Mode("combined", fixture="path", adaptive=_policy)
    MODES[f"E-{_policy}-shared"] = Mode("combined", adaptive=_policy)
MODES.update({f"F-{policy}": Mode("combined", fixture="path", binary=policy) for policy in POLICIES})

WEIGHTS = {"line": 12, "branch": 7, "symbol": 6, "caller": 5, "dialect": 4,
           "import": 3, "module": 1, "direct_test": 30,
           "fixture_observed": 30, "fixture_source": 26, "fixture_initializer": 30,
           "fixture_shared": 12}


def fixture_path(path):
    return path.startswith("tests/fixtures/") and path.endswith(".sql")


def is_fixture_only(patch):
    return not patch.unknown and bool(patch.files) and all(
        change.kind == "modified" and change.old_path == change.new_path
        and fixture_path(change.old_path) for change in patch.files
    )


def rank(scores, durations):
    # Use duration as a soft penalty so short tests do not crowd out strong evidence.
    return sorted(scores, key=lambda n: (-strength(scores[n], durations[n]), durations[n], n))


def strength(score, duration):
    return score / (1 + .22 * math.log1p(duration))


def pick(scores, required, durations, budget):
    if budget is not None and len(required) > budget:
        raise ValueError("required_evidence_exceeds_budget")
    ordered = rank({n: value for n, value in scores.items() if n not in required}, durations)
    kept = ordered if budget is None else ordered[:budget - len(required)]
    return tuple(sorted(required | set(kept)))


def _read_gzip(root, name):
    with gzip.open(root / name, "rt", encoding="utf-8") as stream:
        return json.load(stream)


class Experiment:
    def __init__(self, manifest, assets, source, fixture_map, mode):
        self.mode = mode
        self.manifest = manifest
        self.tests = {test["number"]: test for test in manifest["tests"]}
        if sorted(self.tests) != list(range(len(manifest["tests"]))):
            raise ValueError("invalid official test IDs")
        self.durations = {n: test["seconds"] for n, test in self.tests.items()}
        self.test_files = {n: test["file"] for n, test in self.tests.items()}
        self.by_file = defaultdict(set)
        for n, test in self.tests.items():
            self.by_file[test["file"]].add(n)
        self.coverage = _read_gzip(assets, "coverage_index_b31.json.gz")
        self.ranges = _read_gzip(assets, "source_ranges.json.gz")
        self.deps = _read_gzip(assets, "dependency-map.json.gz")
        self.snapshot = _read_gzip(assets, "source_snapshot.json.gz")
        self.shared = json.loads((assets / "shared_fixture_map.json").read_text(encoding="utf-8"))
        self.fixture_map = json.loads(fixture_map.read_text(encoding="utf-8"))
        if self.fixture_map.get("pinned_commit") != PINNED_COMMIT:
            raise ValueError("fixture evidence is from a different SQLGlot commit")
        self.source_files = self.snapshot["sources"]
        self.test_sources = {}
        if mode.ranking in {"ast", "semantic", "combined"}:
            marker = source / "SOURCE_COMMIT"
            if not marker.is_file() or marker.read_text(encoding="utf-8").strip() != PINNED_COMMIT:
                raise ValueError("provide a verified pinned SQLGlot archive")
            self.test_sources = {file: (source / file).read_text(encoding="utf-8") for file in sorted(self.by_file)}
        if mode.ranking in {"ast", "combined"}:
            self.calls = call_index(self.source_files | self.test_sources)
        if mode.ranking in {"semantic", "combined"}:
            from c10_semantic import test_features
            self.features = {n: test_features(test["id"], self.test_sources[test["file"]])
                             for n, test in self.tests.items()}
            self.frequency = Counter(feature for features in self.features.values() for feature in features)

    def baseline(self, text):
        return analyze_mixed_patch(text, self.manifest, self.coverage, self.ranges, self.deps,
                                   baseline_analyze=analyze_patch,
                                   fixture_mode="aggressive_fixture_only", method_mode="baseline",
                                   source_snapshot=self.snapshot, fixture_safety_mode="shared_loader",
                                   shared_fixture_map=self.shared)

    def fixture_evidence(self, path, add, required):
        readers = resolve_fixture_readers(self.coverage["fixture_reads"].get(path, {}), self.manifest)
        unresolved = (not readers.numbers or readers.unresolved or readers.ambiguous or readers.collection_seen)
        if unresolved:
            return f"fixture_reader_unresolved:{path}"
        add(readers.numbers, "fixture_observed")
        required.update(readers.numbers)
        raw_edges = self.fixture_map["path_edges"].get(path, ())
        initializer_ids = set(self.shared["initializers"].get(path, ()))
        source_ids = set()
        for edge in raw_edges:
            n = edge["test_number"]
            if n not in self.tests or self.tests[n]["id"] != edge["test_id"]:
                raise ValueError("fixture edge disagrees with the official manifest")
            if edge["classification"] == "proven":
                source_ids.add(n)
        add(source_ids, "fixture_source")
        add(initializer_ids, "fixture_initializer")
        required.update(source_ids | initializer_ids)
        if self.mode.fixture == "shared" or not source_ids:
            for n in readers.numbers:
                file = self.tests[n]["file"]
                cohort = set(self.shared["file_cohorts"].get(file, ()))
                if file in self.shared["unresolved_files"] or not (cohort or initializer_ids):
                    return f"shared_loader_unresolved:{file}"
                add(cohort, "fixture_shared")
                required.update(cohort)
        return None

    def select(self, text, patch, baseline, budget_details=None, ranking_details=None):
        if self.mode.ranking == "c5":
            return baseline.test_numbers, baseline.fallback_reasons, []
        if patch.unknown or not patch.files or any(c.kind != "modified" for c in patch.files):
            return tuple(self.tests), ("unsupported_patch_shape",), []

        evidence = defaultdict(set)
        required, risks, targets = set(), set(), set()

        def add(numbers, reason):
            for number in numbers:
                evidence[number].add(reason)

        pure_fixture = is_fixture_only(patch)
        for change in patch.files:
            path = change.new_path
            if fixture_path(path):
                if reason := self.fixture_evidence(path, add, required):
                    return tuple(self.tests), (reason,), []
                continue
            if path in self.by_file:
                add(self.by_file[path], "direct_test")
                required.update(self.by_file[path])
                continue
            if path not in self.source_files:
                return tuple(self.tests), (f"unsupported_path:{path}",), []

            lines = set().union(*(set(h.old_changed_lines) or _insertion_anchors(h) for h in change.hunks))
            spans = self.ranges["symbols"].get(path, ())
            symbols = set()
            for line in lines:
                matching = [span for span in spans if span[1] <= line <= span[2]]
                if matching:
                    symbols.add(min(matching, key=lambda span: span[2] - span[1])[0])
            branch = set()
            if self.mode.ranking in {"ast", "combined"}:
                try:
                    region = analyze_region(change, self.source_files[path])
                except (ValueError, SyntaxError):
                    return tuple(self.tests), (f"unverified_ast:{path}",), []
                symbols = region.symbols or symbols
                lines = region.old_lines
                branch = region.branch_lines
                if region.structure_risk:
                    risks.add(f"ast_structure:{path}")
                if region.text_only:
                    risks.add(f"same_ast_text_changed:{path}")
            collection = set(self.coverage["collection_lines"].get(path, ()))
            if lines & collection:
                risks.add(f"collection_line:{path}")
            line_index = self.coverage["line_index"].get(path, {})
            line_hits = {n for line in lines for n in line_index.get(str(line), ())}
            branch_hits = {n for line in branch for n in line_index.get(str(line), ())}
            owner_hits = {n for name in symbols for n in self.coverage["symbol_index"].get(path, {}).get(name, ())}
            add(line_hits, "line")
            add(branch_hits, "branch")
            # B is an explicit ablation: use the owner when no old statement has observations.
            if self.mode.ranking != "ast" or not (line_hits or branch_hits):
                add(owner_hits, "symbol")
            targets.update((path, name) for name in symbols)
            for importer, imports in self.deps["direct_dependencies"].items():
                if path in imports:
                    add(self.by_file.get(importer, ()), "import")
            if path.startswith(("sqlglot/parsers/", "sqlglot/generators/", "sqlglot/dialects/")):
                dialect = PurePosixPath(path).stem
                add(self.by_file.get(f"tests/dialects/test_{dialect}.py", ()), "dialect")
            if not (line_hits or branch_hits or owner_hits) or self.mode.ranking == "combined":
                add({n for row in line_index.values() for n in row}, "module")

        if self.mode.fallback == "structural":
            guards = sorted(r for r in risks if r.startswith(("ast_structure:", "collection_line:")))
            if guards:
                return tuple(self.tests), tuple(guards), sorted(risks)
        if not pure_fixture and self.mode.ranking in {"ast", "combined"}:
            for (path, name), distance in caller_distances(self.calls, targets).items():
                add(self.coverage["symbol_index"].get(path, {}).get(name, ()), "caller")
                for n in self.by_file.get(path, ()):
                    test_owner = self.tests[n]["id"].split("::", 1)[-1].split("[", 1)[0].replace("::", ".")
                    if test_owner == name:
                        add((n,), "caller")

        scores = {n: sum(WEIGHTS[signal] for signal in signals) for n, signals in evidence.items()}
        if not pure_fixture and self.mode.ranking in {"semantic", "combined"}:
            from c10_semantic import patch_features
            features = patch_features(patch)
            rarity = {key: math.log1p(len(self.tests) / self.frequency[key])
                      for key in features if self.frequency[key]}
            if self.mode.ranking == "semantic":
                scores = {n: .15 * value for n, value in scores.items()}
            for n, observed in self.features.items():
                overlap = sum(min(observed[key], features[key]) * weight for key, weight in rarity.items())
                if overlap:
                    weight = 3 if self.mode.ranking == "semantic" else 2
                    scores[n] = scores.get(n, 0) + weight * math.log1p(overlap)
        if not scores:
            return tuple(self.tests), ("no_positive_evidence",), sorted(risks)

        budget, order = self.mode.budget, None
        if self.mode.adaptive or ranking_details is not None:
            mandatory = rank({n: scores[n] for n in required}, self.durations)
            optional = rank({n: value for n, value in scores.items() if n not in required}, self.durations)
            order = mandatory + optional
            if ranking_details is not None:
                ranking_details.extend(order)
        if self.mode.adaptive:
            signals = Signals(
                coverage_breadth=sum(bool(items & COVERAGE) for items in evidence.values()),
                static_reach=sum(bool(items & STATIC) for items in evidence.values()),
                changed_files=len(patch.files), changed_symbols=len(targets),
                structural=any(reason.startswith(("ast_structure:", "collection_line:")) for reason in risks),
            )
            values = {n: strength(score, self.durations[n]) for n, score in scores.items()}

            def schedule_ms(numbers):
                return best_schedule([self.tests[n] for n in numbers])[1]

            budget = choose_budget(self.mode.adaptive, order, len(required), values, evidence,
                                   signals, schedule_ms)
            if budget_details is not None:
                budget_details.update(vars(signals))
        if self.mode.binary:
            structural = any(r.startswith(("ast_structure:", "collection_line:")) for r in risks)
            signals = collect_signals(evidence, self.test_files, structural)
            budget = binary_budget(self.mode.binary, signals)
            if budget_details is not None:
                budget_details.update(vars(signals))
        if budget_details is not None:
            budget_details.update(budget=budget, pool_count=len(scores), required_count=len(required))
        if budget is not None and len(required) > budget:
            return tuple(self.tests), ("required_evidence_exceeds_budget",), sorted(risks)
        if order is None:
            selected = pick(scores, required, self.durations, budget)
        else:
            selected = tuple(sorted(order[:budget]))
        return selected, (), sorted(risks)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", default="D40", choices=MODES)
    parser.add_argument("--data", type=Path, default=Path("/data"))
    parser.add_argument("--source", type=Path, default=Path("/opt/sqlglot"))
    parser.add_argument("--fixture-map", type=Path)
    parser.add_argument("--runs", type=Path, help="write offline experiment artifacts instead of stdout")
    parser.add_argument("--assets", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--record-ranks", action="store_true")
    args = parser.parse_args()
    if args.record_ranks and args.runs is None:
        parser.error("--record-ranks requires --runs")
    started = time.process_time()
    manifest = json.loads((args.data / "tests.json").read_text(encoding="utf-8"))
    fixture_map = args.fixture_map or args.assets / "fixture_path_shadow_map.json"
    initialization = Experiment(manifest, args.assets, args.source, fixture_map, MODES[args.mode])
    startup_cpu = time.process_time() - started
    output, diagnostics, schedules, rankings = [], [], {}, []
    for file in sorted((args.data / "commits").glob("*.patch")):
        text = file.read_text(encoding="utf-8")
        patch = parse_patch(text)
        baseline = initialization.baseline(text) if args.mode == "C5" else None
        budget_details = {}
        ranked_ids = [] if args.record_ranks else None
        selected, fallback_reasons, risks = initialization.select(text, patch, baseline,
                                                                   budget_details, ranked_ids)
        if initialization.mode.binary and fallback_reasons:
            raise ValueError(f"no supported 32/40 decision for {file.name}: {fallback_reasons}")
        if selected not in schedules:
            schedules[selected] = best_schedule([initialization.tests[n] for n in selected])
        workers, cost = schedules[selected]
        output.append({"commit": file.stem, "workers": workers})
        diagnostics.append({"commit": file.stem, "fixture_only": is_fixture_only(patch),
                            "paths": sorted(patch.changed_paths), "fallback": bool(fallback_reasons),
                            "fallback_reasons": list(fallback_reasons), "risks": risks,
                            "schedule_cost_ms": cost, "selected_count": len(selected),
                            **budget_details})
        if ranked_ids is not None:
            rankings.append({"commit": file.stem, "ranked": ranked_ids,
                             "fallback_reasons": list(fallback_reasons)})
    jsonl = "".join(json.dumps(row) + "\n" for row in output)
    if args.runs is None:
        sys.stdout.write(jsonl)
        sys.stdout.flush()
        print(f"selector_cpu_ms={(time.process_time() - started) * 1000:.3f}", file=sys.stderr)
        return
    args.runs.mkdir(parents=True, exist_ok=True)
    (args.runs / f"{args.mode}.jsonl").write_text(jsonl, encoding="utf-8")
    cpu_seconds = time.process_time() - started
    (args.runs / f"{args.mode}.metrics.json").write_text(json.dumps(
        {"cpu_seconds": cpu_seconds, "startup_cpu_seconds": startup_cpu, "patches": diagnostics},
        indent=2) + "\n", encoding="utf-8")
    if args.record_ranks:
        (args.runs / f"{args.mode}.ranks.json").write_text(json.dumps(rankings) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
