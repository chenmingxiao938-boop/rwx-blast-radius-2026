"""Three source-only choices between two unchanged ranked prefixes."""

from collections import Counter
from dataclasses import dataclass

from c10_budget import COVERAGE, STATIC

LOW, HIGH = 32, 40
POLICIES = ("coverage", "concentrated", "reach-risk")


@dataclass(frozen=True)
class BinarySignals:
    covered_tests: int
    largest_covered_module: int
    reached_tests: int
    impacted_modules: int
    structural: bool


def collect_signals(evidence, test_files, structural):
    covered_by_module = Counter()
    reached = set()
    for number, signals in evidence.items():
        if signals & COVERAGE:
            covered_by_module[test_files[number]] += 1
            reached.add(number)
        if signals & STATIC:
            reached.add(number)
    return BinarySignals(sum(covered_by_module.values()), max(covered_by_module.values(), default=0),
                         len(reached), len({test_files[number] for number in reached}), structural)


def binary_budget(policy, signals):
    if policy == "coverage":
        wide = signals.covered_tests > LOW
    elif policy == "concentrated":
        wide = signals.largest_covered_module > LOW
    elif policy == "reach-risk":
        wide = signals.reached_tests > LOW or (signals.structural and signals.impacted_modules > 1)
    else:
        raise ValueError(f"unknown binary budget policy: {policy}")
    return HIGH if wide else LOW
