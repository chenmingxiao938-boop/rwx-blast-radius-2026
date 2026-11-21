"""Deterministic LPT scheduling with the Blast Radius per-worker charge."""

from __future__ import annotations

import heapq
import math

MAX_WORKERS = 8
WORKER_COST_SECONDS = 2
DEFAULT_DURATION_SECONDS = 1


def _duration_units(seconds: list[int | float]) -> tuple[list[int], int]:
    """Use exact milliseconds for official inputs, or exact binary units otherwise."""
    milliseconds = []
    for duration in seconds:
        if isinstance(duration, int) or duration.is_integer():
            milliseconds.append(int(duration) * 1000)
        else:
            rounded = round(duration * 1000)
            if rounded / 1000 != duration:
                break
            milliseconds.append(rounded)
    else:
        return milliseconds, 1000

    # Every float denominator is a power of two, so the largest one is a
    # common denominator. Integer loads avoid overflow and unstable ties.
    ratios = [duration.as_integer_ratio() for duration in seconds]
    units_per_second = max(denominator for _, denominator in ratios)
    return [
        numerator * (units_per_second // denominator)
        for numerator, denominator in ratios
    ], units_per_second


def assign_workers(
    test_ids: list[int], durations: dict[int, float]
) -> list[list[int]]:
    """Return the lowest-cost LPT allocation using up to eight workers.

    Duplicate IDs are collapsed. IDs must be integers (excluding bool).
    Missing, negative, non-finite or non-numeric durations use one second.
    Zero is valid. Ties favor lower test IDs, lower occupied worker indices,
    and fewer workers when two allocations have the same total cost.
    """
    unique_ids = set()
    for test_id in test_ids:
        if type(test_id) is not int:
            raise TypeError("test IDs must be integers")
        unique_ids.add(test_id)
    if not unique_ids:
        return []

    ids = list(unique_ids)
    seconds = []
    for test_id in ids:
        duration = durations.get(test_id, DEFAULT_DURATION_SECONDS)
        if (
            type(duration) not in (int, float)
            or duration < 0
            or (isinstance(duration, float) and not math.isfinite(duration))
        ):
            duration = DEFAULT_DURATION_SECONDS
        seconds.append(duration)

    loads, units_per_second = _duration_units(seconds)
    tasks = sorted(zip(loads, ids), key=lambda task: (-task[0], task[1]))
    worker_charge = WORKER_COST_SECONDS * units_per_second
    best_workers = []
    best_cost = None
    for count in range(1, min(MAX_WORKERS, len(tasks)) + 1):
        # Empty workers have the lightest load. Seed them first so zero-time
        # tasks also produce exactly `count` occupied workers in each trial.
        workers = [[test_id] for _, test_id in tasks[:count]]
        queue = [(load, index) for index, (load, _) in enumerate(tasks[:count])]
        heapq.heapify(queue)
        for load, test_id in tasks[count:]:
            worker_load, index = queue[0]
            workers[index].append(test_id)
            heapq.heapreplace(queue, (worker_load + load, index))

        cost = max(load for load, _ in queue) + worker_charge * count
        if best_cost is None or cost < best_cost:
            best_cost = cost
            best_workers = workers

    return best_workers
