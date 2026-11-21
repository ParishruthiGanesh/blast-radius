"""Split selected tests across workers to minimise busiest worker + 2 s per worker."""

from __future__ import annotations

import heapq

WORKER_MS = 2000
MAX_WORKERS = 8


def _pack(durations: list[tuple[int, int]], count: int) -> tuple[int, list[list[int]]]:
    """Longest test first onto the least loaded worker. `durations` is sorted descending."""
    heap = [(0, index) for index in range(count)]
    workers: list[list[int]] = [[] for _ in range(count)]
    for milliseconds, number in durations:
        load, index = heap[0]
        workers[index].append(number)
        heapq.heapreplace(heap, (load + milliseconds, index))
    return max(load for load, _ in heap), workers


def plan(tests: dict[int, int]) -> tuple[int, list[list[int]]]:
    """`tests` maps test number -> milliseconds. Returns (cost in ms, workers)."""
    if not tests:
        return 0, []
    durations = sorted(((ms, number) for number, ms in tests.items()), reverse=True)
    best: tuple[int, list[list[int]]] | None = None
    for count in range(1, min(MAX_WORKERS, len(durations)) + 1):
        busiest, workers = _pack(durations, count)
        cost = busiest + WORKER_MS * count
        if best is None or cost < best[0]:
            best = (cost, workers)
        if busiest == durations[0][0]:
            break  # more workers cannot lower the busiest load any further
    assert best is not None
    return best[0], [worker for worker in best[1] if worker]


def cost(tests: dict[int, int]) -> int:
    return plan(tests)[0]
