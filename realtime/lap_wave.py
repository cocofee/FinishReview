"""Lap-count hints and conservative chip-to-person matching for an arrival wave.

Every lap remains visible. Lap roles describe progress only, never which
passages are hidden. A wave's people are matched to its chips in order only
when every check holds; otherwise the whole wave is looked at by hand.
Nothing here decides a result: labels are a shortcut for the operator,
and CycleRace keeps the official time.
"""

from __future__ import annotations

from dataclasses import dataclass

from .crossing_pairing import pair_crossings

FINAL = "final"
INTERMEDIATE = "intermediate"
OVER = "over"  # more laps than the group's target: 圈数异常, only a hint

# Two people across the line closer than this may have swapped chips.
MIN_GAP_MS = 300


@dataclass(frozen=True)
class Passage:
    """One chip read of a rider: its lap (CycleRace's count) and the group's target laps."""

    key: str
    rider: str
    time_ms: int
    lap: int
    target_laps: int


@dataclass(frozen=True)
class WaveMatch:
    """The chip-to-person pairs, in order; REASONS are empty only when the wave is labelled."""

    pairs: tuple[tuple[str, str], ...]
    reasons: tuple[str, ...]


def lap_role(lap: int, target_laps: int) -> str:
    """FINAL for the last lap (or any lap of a group that does not run laps), INTERMEDIATE before it.

    A lap of 0 is not known (a read without lap counting): listed, as before.
    """
    if target_laps <= 1 or lap <= 0:
        return FINAL
    if lap < target_laps:
        return INTERMEDIATE
    if lap == target_laps:
        return FINAL
    return OVER


def short_riders(passages: list[Passage]) -> frozenset[str]:
    """Riders whose last passage is short of the group's target laps (圈数不足: a hint, never a judgement)."""
    last: dict[str, Passage] = {}
    for passage in passages:
        if passage.target_laps <= 1 or passage.lap <= 0:
            continue
        if passage.rider not in last or passage.time_ms > last[passage.rider].time_ms:
            last[passage.rider] = passage
    return frozenset(rider for rider, passage in last.items() if passage.lap < passage.target_laps)


def match_wave(
    chips: list[tuple[str, int]],
    crossings: list[tuple[str, int]],
    *,
    clock_ok: bool,
    short_rider_count: int = 0,
    maybe_several: bool = False,
    near_counted: bool = False,
    obstructed: bool = False,
    min_gap_ms: int = MIN_GAP_MS,
) -> WaveMatch:
    """Pair all passage chips of a wave with the people on its picture, or say why not.

    CHIPS: (key, chip time ms) of every passage in the wave.
    CROSSINGS: (key, time ms) of the people detected crossing on the picture.

    The chips and crossings go through the same time-ordered pairing as the
    chip/video matching (crossing_pairing.pair_crossings): every pair must be
    unique, keep the order and beat skipping both sides on time.  Equal counts
    alone are not enough: if one chip and one person are offset enough for the
    pairing to prefer dropping them (including some double-chip/missed-read
    cancellations), the incomplete pairing leaves the whole wave to hand.
    Time agreement does not prove identity or rule out every cancellation.
    """
    reasons = []
    if not chips:
        reasons.append("本波没有芯片记录")
    if not clock_ok:
        reasons.append("录像未校时")
    if len(crossings) != len(chips):
        reasons.append(f"人数 {len(crossings)} 与芯片 {len(chips)} 不一致")
    if short_rider_count:
        reasons.append("本波有圈数不足的选手")
    if maybe_several:
        reasons.append("有可能多人")
    if near_counted:
        reasons.append("有已点附近的过线")
    if obstructed:
        reasons.append("过线时视线被挡")
    ordered = sorted(crossings, key=lambda crossing: crossing[1])
    if any(later[1] - earlier[1] < min_gap_ms for earlier, later in zip(ordered, ordered[1:])):
        reasons.append(f"相邻过线不到 {min_gap_ms} 毫秒，顺序可能对错")
    if reasons:
        return WaveMatch(pairs=(), reasons=tuple(reasons))
    pairing = pair_crossings(
        [key for key, _time in crossings], [time for _key, time in crossings],
        [key for key, _time in chips], [time for _key, time in chips],
    )
    if len(pairing.matched) != len(chips) or pairing.video_only or pairing.chip_only:
        return WaveMatch(pairs=(), reasons=("芯片与过线时间对不上，可能有个别错位",))
    return WaveMatch(pairs=tuple((chip, crossing) for crossing, chip, _residual in pairing.matched), reasons=())
