"""Run every test on every commit with the cheapest LPT worker schedule."""

import json
import math
import sys
from pathlib import Path

from shadow_mode import run_shadow

DATA = Path("/data")
MAX_WORKERS = 8
WORKER_COST_MS = 2_000


def ordered_tests(tests: list[dict]) -> list[tuple[int, int]]:
    """Convert durations exactly as the scorer does, then sort once for LPT."""
    return sorted(
        (
            (test["number"], math.floor(test["seconds"] * 1000 + 0.5))
            for test in tests
        ),
        key=lambda item: (-item[1], item[0]),
    )


def split(ordered: list[tuple[int, int]], worker_count: int) -> tuple[list[list[int]], int]:
    """Put each longest remaining test on the least loaded worker."""
    workers: list[list[int]] = [[] for _ in range(worker_count)]
    loads = [0] * worker_count
    for number, milliseconds in ordered:
        lightest = loads.index(min(loads))
        workers[lightest].append(number)
        loads[lightest] += milliseconds
    return workers, max(loads) + WORKER_COST_MS * worker_count


def best_schedule(tests: list[dict]) -> tuple[list[list[int]], int]:
    ordered = ordered_tests(tests)
    if not ordered:
        return [], 0
    best_workers, best_cost = split(ordered, 1)
    for worker_count in range(2, min(MAX_WORKERS, len(ordered)) + 1):
        workers, cost = split(ordered, worker_count)
        # Counts increase, so equal costs keep the smaller worker count.
        if cost < best_cost:
            best_workers, best_cost = workers, cost
    return best_workers, best_cost


def main() -> None:
    tests = json.loads((DATA / "tests.json").read_text(encoding="utf-8"))["tests"]
    workers, cost = best_schedule(tests)
    patches = sorted((DATA / "commits").glob("*.patch"))
    for patch in patches:
        print(json.dumps({"commit": patch.stem, "workers": workers}))
    run_shadow(patches, tests)
    print(f"selected {len(tests)} tests, {len(workers)} workers, {cost} ms per commit", file=sys.stderr)


if __name__ == "__main__":
    main()
