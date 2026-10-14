"""Order-preserving one-to-one pairing of video crossings with chip reads.

Crossings and expected chip positions (chip time mapped through the clock
model) are aligned like two sequences: each crossing pairs with at most one
chip, order is kept, and unmatched items on either side cost a fixed gap
penalty. A pair costs ``0.5 * (residual / sigma)^2`` and is only allowed
within ``window_ms``. The alignment never pairs a crossing with "the nearest
chip" on its own; every pair competes with skipping both.

The problem is split into independent blocks wherever the merged timeline
has a gap longer than the window, so long races stay fast.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Sequence, TypeVar

import numpy as np


SIGMA_MS = 150.0
GAP_COST = 1.6
WINDOW_MS = 700.0

C = TypeVar("C")
R = TypeVar("R")


@dataclass(frozen=True)
class Pairing(Generic[C, R]):
    matched: tuple[tuple[C, R, float], ...]
    video_only: tuple[C, ...]
    chip_only: tuple[R, ...]


def _blocks(crossing_ms: np.ndarray, expected_ms: np.ndarray, window_ms: float) -> list[tuple[list[int], list[int]]]:
    items = [(float(t), 0, index) for index, t in enumerate(crossing_ms)]
    items += [(float(t), 1, index) for index, t in enumerate(expected_ms)]
    items.sort()
    blocks: list[tuple[list[int], list[int]]] = []
    previous = None
    for time_ms, kind, index in items:
        if previous is None or time_ms - previous > window_ms:
            blocks.append(([], []))
        blocks[-1][kind].append(index)
        previous = time_ms
    return blocks


def _align(
    crossing_ms: np.ndarray,
    expected_ms: np.ndarray,
    sigma_ms: float,
    gap_cost: float,
    window_ms: float,
) -> tuple[list[tuple[int, int]], list[int], list[int]]:
    n, m = crossing_ms.size, expected_ms.size
    cost = np.full((n + 1, m + 1), np.inf)
    cost[0, :] = np.arange(m + 1) * gap_cost
    cost[:, 0] = np.arange(n + 1) * gap_cost
    move = np.zeros((n + 1, m + 1), np.int8)
    for i in range(1, n + 1):
        crossing = crossing_ms[i - 1]
        for j in range(1, m + 1):
            best, step = cost[i - 1, j] + gap_cost, 1
            if cost[i, j - 1] + gap_cost < best:
                best, step = cost[i, j - 1] + gap_cost, 2
            residual = crossing - expected_ms[j - 1]
            if abs(residual) <= window_ms:
                value = cost[i - 1, j - 1] + 0.5 * (residual / sigma_ms) ** 2
                if value < best:
                    best, step = value, 3
            cost[i, j], move[i, j] = best, step
    matched, video_only, chip_only = [], [], []
    i, j = n, m
    while i > 0 or j > 0:
        step = move[i, j] if i > 0 and j > 0 else (1 if i > 0 else 2)
        if step == 3:
            matched.append((i - 1, j - 1))
            i, j = i - 1, j - 1
        elif step == 1:
            video_only.append(i - 1)
            i -= 1
        else:
            chip_only.append(j - 1)
            j -= 1
    return matched[::-1], video_only[::-1], chip_only[::-1]


def pair_crossings(
    crossings: Sequence[C],
    crossing_ms: Sequence[float],
    chips: Sequence[R],
    expected_ms: Sequence[float],
    *,
    sigma_ms: float = SIGMA_MS,
    gap_cost: float = GAP_COST,
    window_ms: float = WINDOW_MS,
) -> Pairing[C, R]:
    """Align crossings (recorder time) with chips at their expected position."""

    crossing_times = np.asarray(crossing_ms, dtype=np.float64)
    expected = np.asarray(expected_ms, dtype=np.float64)
    if crossing_times.size != len(crossings) or expected.size != len(chips):
        raise ValueError("times must match the items")
    crossing_order = np.argsort(crossing_times, kind="stable")
    chip_order = np.argsort(expected, kind="stable")
    sorted_crossings = crossing_times[crossing_order]
    sorted_expected = expected[chip_order]
    matched, video_only, chip_only = [], [], []
    for crossing_indexes, chip_indexes in _blocks(sorted_crossings, sorted_expected, window_ms):
        a = sorted_crossings[crossing_indexes]
        b = sorted_expected[chip_indexes]
        pairs, lonely_crossings, lonely_chips = _align(a, b, sigma_ms, gap_cost, window_ms)
        for i, j in pairs:
            ci = int(crossing_order[crossing_indexes[i]])
            cj = int(chip_order[chip_indexes[j]])
            matched.append((crossings[ci], chips[cj], float(crossing_times[ci] - expected[cj])))
        video_only += [crossings[int(crossing_order[crossing_indexes[i]])] for i in lonely_crossings]
        chip_only += [chips[int(chip_order[chip_indexes[j]])] for j in lonely_chips]
    return Pairing(tuple(matched), tuple(video_only), tuple(chip_only))


__all__ = ["GAP_COST", "Pairing", "SIGMA_MS", "WINDOW_MS", "pair_crossings"]
