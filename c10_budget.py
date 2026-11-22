"""Label-free budget experiments over an unchanged C10 combined ranking."""

from collections.abc import Callable
from dataclasses import dataclass
from math import ceil, sqrt
from statistics import median


COVERAGE = {"line", "branch", "symbol"}
STATIC = {"caller", "import"}
LOCAL = {"line", "branch", "symbol", "caller"}
CAP = 128


@dataclass(frozen=True)
class Signals:
    coverage_breadth: int
    static_reach: int
    changed_files: int
    changed_symbols: int
    structural: bool


def breadth_budget(signals):
    observed = 6 * ceil(sqrt(signals.coverage_breadth))
    callers = 3 * ceil(sqrt(signals.static_reach))
    shape = min(24, 4 * max(0, signals.changed_files + signals.changed_symbols - 2))
    return min(CAP, max(16, observed + callers + shape + 16 * signals.structural))


def relevance_budget(order, required_count, strength, evidence, width):
    optional = order[required_count:CAP]
    if not optional:
        return required_count
    threshold = .5 * median(strength[number] for number in optional[:8])
    supported_tail = max((rank for rank, number in enumerate(optional, required_count + 1)
                          if strength[number] >= threshold and evidence.get(number, set()) & LOCAL),
                         default=required_count)
    return min(CAP, max(min(width, 32), supported_tail))


def choose_budget(policy: str, order, required_count, strength, evidence, signals: Signals,
                  schedule_ms: Callable[[list[int]], int]) -> int:
    width = breadth_budget(signals)
    if policy == "breadth":
        budget = width
    elif policy == "relevance":
        budget = relevance_budget(order, required_count, strength, evidence, width)
    elif policy == "cost":
        top = min(width, relevance_budget(order, required_count, strength, evidence, width))
        budget = min(len(order), max(required_count, min(width, 32 if signals.structural else 24)))
        cost = schedule_ms(order[:budget])
        while budget < min(len(order), top):
            number = order[budget]
            next_cost = schedule_ms(order[:budget + 1])
            if (next_cost - cost > 2000 and not evidence.get(number, set()) & {"line", "branch"}
                    and not signals.structural):
                break
            budget += 1
            cost = next_cost
    else:
        raise ValueError(f"unknown budget policy: {policy}")
    return min(len(order), max(required_count, budget))
