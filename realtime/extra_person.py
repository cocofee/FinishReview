"""Where in a wave is the person without a chip?

11-14, 古浪 6 号: the operator cannot read bibs, so people are counted by
clicking them as they cross.  One click more than chips used to be reported on
the last person of the wave.  The times say more: up to the person without a
chip every click sits on its chip's time, and from that person on every click
sits on the *next* chip's time ("出现了同样的秒差").  Leaving that one click
out puts every other click back on its own chip.

This only says where the extra person crossed.  Who it is cannot be told from
the times; riders closer together than SLACK_MS cannot be told apart either,
and are then all named for a referee to look at.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median

SLACK_MS = 250          # two people closer than this: the times cannot say which one has no chip
OFFSET_LIMIT_MS = 300   # how far a wave's clicks may sit off the chips as a whole
_FAR = float("inf")


@dataclass(frozen=True)
class ExtraPerson:
    click: int                    # index into the clicks, the likeliest person without a chip
    candidates: tuple[int, ...]   # every click that fits about as well, in order; holds CLICK

    @property
    def sure(self) -> bool:
        return len(self.candidates) == 1


@dataclass(frozen=True)
class WaveFit:
    chip_of_click: tuple[int | None, ...]   # per click: index of its chip, None for a person without one
    extras: tuple[ExtraPerson, ...]
    offset_ms: int                          # clicks minus chips over the wave, after the extras are left out
    worst_ms: int                           # the worst click-to-chip distance left


def _tables(clicks, chips, offset):
    """forward[i][j]: least total distance pairing the first i clicks with the first j chips in order."""
    n, m = len(clicks), len(chips)
    forward = [[_FAR] * (m + 1) for _ in range(n + 1)]
    forward[0][0] = 0.0
    for i in range(1, n + 1):
        forward[i][0] = 0.0                                    # all left out so far
        for j in range(1, min(i, m) + 1):
            paired = forward[i - 1][j - 1] + abs(clicks[i - 1] - chips[j - 1] - offset)
            forward[i][j] = min(paired, forward[i - 1][j])     # or click i-1 has no chip
    backward = [[_FAR] * (m + 2) for _ in range(n + 2)]
    for i in range(n, -1, -1):
        backward[i][m] = 0.0
        for j in range(m - 1, -1, -1):
            if i < n:
                paired = backward[i + 1][j + 1] + abs(clicks[i] - chips[j] - offset)
                backward[i][j] = min(paired, backward[i + 1][j])
    return forward, backward


def _pairing(clicks, chips, forward):
    i, j, chip_of_click = len(clicks), len(chips), [None] * len(clicks)
    while i > 0:
        if j > 0 and forward[i][j] != forward[i - 1][j]:
            chip_of_click[i - 1] = j - 1
            j -= 1
        i -= 1
    return chip_of_click


def fit_wave(click_times_ms, chip_times_ms) -> WaveFit | None:
    """Both in arrival order.  None unless there are more clicks than chips."""
    clicks, chips = list(click_times_ms), list(chip_times_ms)
    n, m = len(clicks), len(chips)
    if n <= m or sorted(clicks) != clicks or sorted(chips) != chips:
        return None
    offset = 0
    for _ in range(2):                                         # second pass: with the wave's own offset
        forward, backward = _tables(clicks, chips, offset)
        chip_of_click = _pairing(clicks, chips, forward)
        gaps = [clicks[i] - chips[j] for i, j in enumerate(chip_of_click) if j is not None]
        offset = int(max(-OFFSET_LIMIT_MS, min(OFFSET_LIMIT_MS, median(gaps)))) if gaps else 0
    forward, backward = _tables(clicks, chips, offset)
    chip_of_click = _pairing(clicks, chips, forward)
    best = forward[n][m]
    # What it costs to say click i is the one without a chip.
    without = [min(forward[i][j] + backward[i + 1][j] for j in range(m + 1)) for i in range(n)]
    extras = []
    for i, chip in enumerate(chip_of_click):
        if chip is not None:
            continue
        low = high = i
        while low > 0 and chip_of_click[low - 1] is not None and without[low - 1] <= best + SLACK_MS:
            low -= 1
        while high < n - 1 and chip_of_click[high + 1] is not None and without[high + 1] <= best + SLACK_MS:
            high += 1
        extras.append(ExtraPerson(i, tuple(range(low, high + 1))))
    worst = max((abs(clicks[i] - chips[j] - offset) for i, j in enumerate(chip_of_click) if j is not None),
                default=0)
    return WaveFit(tuple(chip_of_click), tuple(extras), offset, int(worst))
