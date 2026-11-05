"""Chip-only self checks (plan 7.3): no video needed, usable during the race.

Every check works on chip reads as they arrived. When CycleRace later voids
or changes a read, the item stays visible but is closed upstream, so the
list shows both what was caught and what has been fixed.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional, Sequence

import numpy as np

from .chip_history import EXCLUDED_SPARE, ChipRead, format_chip_time
from .problem_items import (
    CATEGORY_CHIP,
    CATEGORY_COVERAGE,
    CATEGORY_SYNC,
    KIND_BATCH_READS,
    KIND_BEFORE_GUN,
    KIND_DUPLICATE_MANUAL,
    KIND_DUPLICATE_READ,
    KIND_EARLY_READ,
    KIND_LAP_MISSING,
    KIND_LAP_OVER,
    KIND_LAP_REGRESS,
    KIND_LAP_REPEAT,
    KIND_LAP_TIME,
    KIND_NOT_ARRIVED,
    KIND_PAIRED_CHIPS,
    KIND_REPEATED_PASSAGE,
    KIND_SYNC,
    KIND_TEST_CHIP,
    KIND_UNKNOWN_BIB,
    STATUS_CLOSED_UPSTREAM,
    STATUS_INFO,
    STATUS_OPEN,
    ProblemItem,
    sort_items,
    stable_item_id,
)
from .sync_check import SyncIssue


DUPLICATE_WINDOW_MS = 10_000
BATCH_WINDOW_MS = 1_000
BATCH_TIGHT_WINDOW_MS = 200
BATCH_TIGHT_COUNT = 6
DEFAULT_MAX_SPEED_KMH = 60.0
EARLY_FRACTION = 0.5
EARLY_REFERENCE_MIN_READS = 5
LAP_TIME_LOW = 0.5
# A lap read missed in between shows up as one lap of about twice the usual
# time.  Real 现代五项 laps stay within 0.86-1.39x the group median, so 1.6x
# still catches a fast athlete's doubled lap (about 1.7x).
LAP_TIME_HIGH = 1.6
_FINISHED_STATUSES = {"", "OK"}
# A group is settled -- every athlete has stopped -- once it has been quiet for
# this many median laps.  Only then are short or extra laps judged mid-race.
SETTLE_LAP_FACTOR = 1.5
# The lap most bibs ended on is the target when only a few bibs went beyond it.
TARGET_MIN_SHARE = 0.6
OVER_MAX_FRACTION = 0.15
# Two bibs together on every lap are one athlete carrying two chips, or two
# athletes running as a pair: a picture decides.  Seven 现代五项 races had no
# such pair within 1 s; two laps together happened by chance, three did once.
PAIR_WINDOW_MS = 1_000
PAIR_MIN_LAPS = 3


@dataclass(frozen=True, slots=True)
class ChipCheckContext:
    """Race facts that CycleRace does not send today; empty means unknown."""

    gun_starts: Mapping[str, int] = field(default_factory=dict)
    mass_start_ms: Optional[int] = None
    distance_km: Optional[float] = None
    max_speed_kmh: float = DEFAULT_MAX_SPEED_KMH
    roster: Mapping[str, str] = field(default_factory=dict)
    statuses: Mapping[str, str] = field(default_factory=dict)
    sync_issues: Sequence[SyncIssue] = ()
    race_finished: bool = True
    # Wall clock of a live check, so a group that has gone quiet can be judged
    # before the whole race is declared finished.  ``None`` waits for the end.
    now_ms: Optional[int] = None

    def gun_for(self, group_name: str) -> Optional[int]:
        if group_name in self.gun_starts:
            return int(self.gun_starts[group_name])
        return None if self.mass_start_ms is None else int(self.mass_start_ms)

    def missing_inputs(self) -> tuple[str, ...]:
        missing = []
        if not self.gun_starts and self.mass_start_ms is None:
            missing.append("各组发枪时间（过早读数检查）")
        if self.distance_km is None:
            missing.append("赛段距离（过早读数的绝对下限）")
        if not self.roster:
            missing.append("报名名单（名单外号码、应到未到）")
        if not self.statuses:
            missing.append("退赛 / OTL / DNS 状态（应到未到、末圈缺读）")
        return tuple(missing)


def _status(reads: Iterable[ChipRead], *, minimum_active: int = 1) -> tuple[str, str]:
    reads = tuple(reads)
    active = sum(read.active_now for read in reads)
    if active >= minimum_active:
        return STATUS_OPEN, ""
    voided = [read.bib for read in reads if not read.active_now]
    return STATUS_CLOSED_UPSTREAM, "CycleRace 已作废 " + "、".join(voided)


def _read_text(read: ChipRead) -> str:
    source = "补录" if read.manual_entry else "芯片"
    return f"{read.bib}（{source} {format_chip_time(read.chip_ms)}）"


def _excluded_items(reads: Sequence[ChipRead]) -> list[ProblemItem]:
    items = []
    for read in reads:
        if not read.excluded:
            continue
        group = "备用组" if read.excluded == EXCLUDED_SPARE else "测试组"
        status, note = _status((read,))
        items.append(ProblemItem(
            item_id=stable_item_id(KIND_TEST_CHIP, read.event_id, read.revision),
            kind=KIND_TEST_CHIP, category=CATEGORY_CHIP,
            detail=f"{group} {read.group_name}：{_read_text(read)}，不应出现在比赛读数中",
            chip_time_ms=read.chip_ms, bibs=(read.bib,), event_ids=(read.event_id,),
            status=status, status_note=note, priority=80,
        ))
    return items


def _unknown_bib_items(reads: Sequence[ChipRead], roster: Mapping[str, str]) -> list[ProblemItem]:
    if not roster:
        return []
    items = []
    for read in reads:
        if read.excluded or read.bib in roster:
            continue
        status, note = _status((read,))
        items.append(ProblemItem(
            item_id=stable_item_id(KIND_UNKNOWN_BIB, read.event_id, read.revision),
            kind=KIND_UNKNOWN_BIB, category=CATEGORY_CHIP,
            detail=f"{_read_text(read)} 不在报名名单中",
            chip_time_ms=read.chip_ms, bibs=(read.bib,), event_ids=(read.event_id,),
            status=status, status_note=note, priority=40,
        ))
    return items


def _duplicate_items(reads: Sequence[ChipRead], *, multi_lap: bool, window_ms: int) -> list[ProblemItem]:
    by_bib: dict[str, list[ChipRead]] = defaultdict(list)
    for read in reads:
        if not read.excluded and read.bib:
            by_bib[read.bib].append(read)
    items = []
    for bib, bib_reads in by_bib.items():
        if len(bib_reads) < 2:
            continue
        bib_reads.sort(key=lambda read: read.chip_ms)
        clusters: list[list[ChipRead]] = [[bib_reads[0]]]
        for read in bib_reads[1:]:
            if read.chip_ms - clusters[-1][-1].chip_ms <= window_ms:
                clusters[-1].append(read)
            else:
                clusters.append([read])
        for cluster in clusters:
            if len(cluster) < 2:
                continue
            kind = KIND_DUPLICATE_MANUAL if all(read.manual_entry for read in cluster) else KIND_DUPLICATE_READ
            status, note = _status(cluster, minimum_active=2)
            gap = cluster[-1].chip_ms - cluster[0].chip_ms
            items.append(ProblemItem(
                item_id=stable_item_id(kind, *sorted(read.read_id for read in cluster)),
                kind=kind, category=CATEGORY_CHIP,
                detail=f"号码 {bib} 在 {gap / 1000:.1f} 秒内有 {len(cluster)} 条有效记录："
                       + "；".join(_read_text(read) for read in cluster),
                chip_time_ms=cluster[0].chip_ms, bibs=(bib,),
                event_ids=tuple(read.event_id for read in cluster),
                status=status, status_note=note, priority=20,
            ))
        if not multi_lap and len(clusters) > 1:
            firsts = [cluster[0] for cluster in clusters]
            status, note = _status(firsts, minimum_active=2)
            items.append(ProblemItem(
                item_id=stable_item_id(KIND_REPEATED_PASSAGE, *sorted(read.read_id for read in firsts)),
                kind=KIND_REPEATED_PASSAGE, category=CATEGORY_CHIP,
                detail=f"单圈赛中号码 {bib} 有 {len(firsts)} 次有效通过："
                       + "；".join(_read_text(read) for read in firsts),
                chip_time_ms=firsts[0].chip_ms, bibs=(bib,),
                event_ids=tuple(read.event_id for read in firsts),
                status=status, status_note=note, priority=25,
            ))
    return items


def _batch_items(reads: Sequence[ChipRead], roster: Mapping[str, str]) -> list[ProblemItem]:
    """Bursts that riders cannot produce, e.g. a box of chips at the antenna.

    Bunch sprints legitimately put 5-10 reads into one second, so a burst is
    flagged only when it is very tight or contains test, spare or unlisted chips.
    """

    latest = {read.event_id: read for read in sorted(reads, key=lambda r: (r.latest_revision, r.revision, r.active_now))}
    ordered = sorted(latest.values(), key=lambda read: read.chip_ms)
    items = []
    clusters = []
    start = 0
    for end, read in enumerate(ordered):
        while ordered[start].chip_ms < read.chip_ms - BATCH_WINDOW_MS:
            start += 1
        window = ordered[start:end + 1]
        tight = [item for item in window if item.chip_ms >= read.chip_ms - BATCH_TIGHT_WINDOW_MS]
        suspicious = [
            item for item in window
            if item.excluded or (roster and item.bib not in roster)
        ]
        flagged = None
        if len(tight) >= BATCH_TIGHT_COUNT:
            flagged = tight
        elif len(window) >= 3 and suspicious:
            flagged = window
        if not flagged:
            continue
        earlier = {item.event_id for cluster in clusters[:-1] for item in cluster}
        flagged = [item for item in flagged if item.event_id not in earlier]
        if not flagged:
            continue
        if clusters and {item.event_id for item in clusters[-1]} & {item.event_id for item in flagged}:
            combined = {item.event_id: item for item in clusters[-1] + flagged}
            merged = sorted(combined.values(), key=lambda item: item.chip_ms)
            if merged[-1].chip_ms - merged[0].chip_ms <= BATCH_WINDOW_MS:
                clusters[-1] = merged
                continue
        used = {item.event_id for cluster in clusters for item in cluster}
        remaining = [item for item in flagged if item.event_id not in used]
        if len(remaining) >= 2:
            clusters.append(remaining)
    for flagged in clusters:
        status, note = _status(flagged, minimum_active=1)
        span = flagged[-1].chip_ms - flagged[0].chip_ms
        items.append(ProblemItem(
            item_id=stable_item_id(KIND_BATCH_READS, *sorted(item.read_id for item in flagged)),
            kind=KIND_BATCH_READS, category=CATEGORY_CHIP,
            detail=f"{span} ms 内 {len(flagged)} 条独立通过记录，人数待视频核实：" + "、".join(item.bib for item in flagged),
            chip_time_ms=flagged[0].chip_ms, bibs=tuple(item.bib for item in flagged),
            event_ids=tuple(item.event_id for item in flagged),
            status=status, status_note=note, priority=60,
        ))
    return items


def _early_items(reads: Sequence[ChipRead], context: ChipCheckContext) -> list[ProblemItem]:
    by_group: dict[str, list[int]] = defaultdict(list)
    for read in reads:
        gun = context.gun_for(read.group_name)
        if gun is not None and not read.excluded:
            by_group[read.group_name].append(read.chip_ms - gun)
    absolute = None
    if context.distance_km and context.max_speed_kmh > 0:
        absolute = context.distance_km / context.max_speed_kmh * 3_600_000
    items = []
    for read in reads:
        gun = context.gun_for(read.group_name)
        if gun is None or read.excluded:
            continue
        elapsed = read.chip_ms - gun
        status, note = _status((read,))
        base = dict(
            chip_time_ms=read.chip_ms, bibs=(read.bib,), event_ids=(read.event_id,),
            status=status, status_note=note, priority=15,
        )
        if elapsed < 0:
            items.append(ProblemItem(
                item_id=stable_item_id(KIND_BEFORE_GUN, read.event_id, read.revision),
                kind=KIND_BEFORE_GUN, category=CATEGORY_CHIP,
                detail=f"{_read_text(read)} 早于 {read.group_name} 发枪 {format_chip_time(gun)}",
                **base,
            ))
            continue
        others = sorted(value for value in by_group[read.group_name] if value != elapsed)
        relative = None
        if len(others) >= EARLY_REFERENCE_MIN_READS:
            relative = EARLY_FRACTION * float(np.percentile(others, 10))
        limits = [value for value in (absolute, relative) if value is not None]
        if not limits or elapsed >= max(limits):
            continue
        items.append(ProblemItem(
            item_id=stable_item_id(KIND_EARLY_READ, read.event_id, read.revision),
            kind=KIND_EARLY_READ, category=CATEGORY_CHIP,
            detail=(
                f"{_read_text(read)} 距 {read.group_name} 发枪仅 {elapsed / 60_000:.1f} 分钟，"
                f"不可能这么早到达（下限 {max(limits) / 60_000:.0f} 分钟）"
            ),
            **base,
        ))
    return items


@dataclass(frozen=True, slots=True)
class LapTarget:
    """How many laps a group is judged against, and whether it can be judged yet."""

    target: int
    settled: bool
    # When a group still running would settle if no further read arrived.
    settle_at_ms: Optional[int] = None
    # Bibs beyond a target most of the group agrees on: an extra read, not a
    # standard that would make everyone else a lap short.
    over_bibs: tuple[str, ...] = ()


def _lap_reference(reads: Sequence[ChipRead]) -> dict[str, list[int]]:
    by_bib: dict[str, list[ChipRead]] = defaultdict(list)
    for read in reads:
        by_bib[read.bib].append(read)
    lap_times: dict[str, list[int]] = defaultdict(list)
    for bib_reads in by_bib.values():
        bib_reads = sorted(bib_reads, key=lambda read: read.chip_ms)
        for previous, current in zip(bib_reads, bib_reads[1:]):
            if current.lap == previous.lap + 1:
                lap_times[current.group_name].append(current.chip_ms - previous.chip_ms)
    return lap_times


def lap_targets(reads: Sequence[ChipRead], context: ChipCheckContext = ChipCheckContext()) -> dict[str, LapTarget]:
    """Per group: the lap count to judge against and whether the group has stopped.

    The target is the lap most bibs ended on when only a few bibs went beyond
    it; otherwise the highest lap, as before.  One extra read must not make
    every other athlete look a lap short.
    """

    active = [read for read in reads if read.lap > 0 and not read.excluded and read.active_now]
    lap_times = _lap_reference([read for read in reads if read.lap > 0 and not read.excluded])
    finals: dict[str, dict[str, int]] = defaultdict(dict)
    last_ms: dict[str, int] = {}
    for read in active:
        group = finals[read.group_name]
        group[read.bib] = max(group.get(read.bib, 0), read.lap)
        last_ms[read.group_name] = max(last_ms.get(read.group_name, read.chip_ms), read.chip_ms)
    targets = {}
    for group_name, bib_laps in finals.items():
        reference = lap_times.get(group_name, [])
        median = float(np.median(reference)) if len(reference) >= EARLY_REFERENCE_MIN_READS else None
        settle_at = int(last_ms[group_name] + SETTLE_LAP_FACTOR * median) if median else None
        settled = context.race_finished or (
            context.now_ms is not None and settle_at is not None and context.now_ms >= settle_at)
        counts: dict[int, int] = defaultdict(int)
        for lap in bib_laps.values():
            counts[lap] += 1
        most = max(counts.values())
        mode = max(lap for lap, count in counts.items() if count == most)
        above = tuple(sorted((bib for bib, lap in bib_laps.items() if lap > mode), key=lambda b: (len(b), b)))
        allowed = max(1, int(OVER_MAX_FRACTION * len(bib_laps)))
        if len(bib_laps) >= 3 and most >= TARGET_MIN_SHARE * len(bib_laps) and 0 < len(above) <= allowed:
            target, over = mode, above
        else:
            target, over = max(counts), ()
        targets[group_name] = LapTarget(target, settled, None if settled else settle_at, over)
    return targets


def _lap_items(reads: Sequence[ChipRead], context: ChipCheckContext) -> list[ProblemItem]:
    lapped = [read for read in reads if read.lap > 0 and not read.excluded]
    if not lapped:
        return []
    by_bib: dict[str, list[ChipRead]] = defaultdict(list)
    for read in lapped:
        by_bib[read.bib].append(read)
    lap_times = _lap_reference(lapped)
    targets = lap_targets(reads, context)
    items = []
    for bib, bib_reads in by_bib.items():
        bib_reads.sort(key=lambda read: read.chip_ms)
        for previous, current in zip(bib_reads, bib_reads[1:]):
            pair = (previous, current)
            status, note = _status(pair, minimum_active=2)
            common = dict(
                category=CATEGORY_CHIP, chip_time_ms=current.chip_ms, bibs=(bib,),
                event_ids=(previous.event_id, current.event_id),
                status=status, status_note=note, priority=30,
            )
            if current.lap == previous.lap:
                items.append(ProblemItem(
                    item_id=stable_item_id(KIND_LAP_REPEAT, previous.read_id, current.read_id),
                    kind=KIND_LAP_REPEAT,
                    detail=f"号码 {bib} 两次记为第 {current.lap} 圈：{_read_text(previous)}；{_read_text(current)}",
                    **common,
                ))
            elif current.lap < previous.lap:
                items.append(ProblemItem(
                    item_id=stable_item_id(KIND_LAP_REGRESS, previous.read_id, current.read_id),
                    kind=KIND_LAP_REGRESS,
                    detail=f"号码 {bib} 圈号从 {previous.lap} 倒退到 {current.lap}",
                    **common,
                ))
            elif current.lap == previous.lap + 1:
                reference = lap_times.get(current.group_name, [])
                if len(reference) >= EARLY_REFERENCE_MIN_READS:
                    median = float(np.median(reference))
                    lap_ms = current.chip_ms - previous.chip_ms
                    if lap_ms < LAP_TIME_LOW * median or lap_ms > LAP_TIME_HIGH * median:
                        hint = (f"，约 {lap_ms / median:.1f} 圈的时间，可能中间漏读一次"
                                if lap_ms > LAP_TIME_HIGH * median else "")
                        items.append(ProblemItem(
                            item_id=stable_item_id(KIND_LAP_TIME, previous.read_id, current.read_id),
                            kind=KIND_LAP_TIME,
                            detail=(
                                f"号码 {bib} 第 {current.lap} 圈用时 {lap_ms / 60_000:.1f} 分钟，"
                                f"组内中位 {median / 60_000:.1f} 分钟{hint}"
                            ),
                            **common,
                        ))
        active = [read for read in bib_reads if read.active_now]
        if not active:
            continue
        target = targets.get(active[0].group_name)
        if target is None or not target.settled:
            continue
        last = max(read.lap for read in active)
        if bib in target.over_bibs:
            for read in active:
                if read.lap <= target.target:
                    continue
                before = [other for other in active if other.chip_ms < read.chip_ms]
                gap = f"，距上一次 {(read.chip_ms - before[-1].chip_ms) / 60_000:.1f} 分钟" if before else ""
                items.append(ProblemItem(
                    item_id=stable_item_id(KIND_LAP_OVER, read.read_id),
                    kind=KIND_LAP_OVER, category=CATEGORY_CHIP,
                    detail=(f"号码 {bib} 记到第 {read.lap} 圈，同组多数人是 {target.target} 圈；"
                            f"多出的一次 {_read_text(read)}{gap}。可能跑完后又经过读点或重复读数"),
                    chip_time_ms=read.chip_ms, bibs=(bib,), event_ids=(read.event_id,),
                    status=STATUS_OPEN, priority=30,
                ))
            continue
        status_text = context.statuses.get(bib, "")
        if last < target.target and status_text in _FINISHED_STATUSES:
            items.append(ProblemItem(
                item_id=stable_item_id(KIND_LAP_MISSING, bib, last),
                kind=KIND_LAP_MISSING, category=CATEGORY_CHIP,
                detail=f"号码 {bib} 只到第 {last} 圈，同组应为 {target.target} 圈，且没有退赛状态",
                chip_time_ms=active[-1].chip_ms, bibs=(bib,), event_ids=(active[-1].event_id,),
                status=STATUS_OPEN, priority=35,
            ))
    return items


def _paired_items(reads: Sequence[ChipRead]) -> list[ProblemItem]:
    """Two bibs that crossed together on every lap, at least ``PAIR_MIN_LAPS`` of them.

    Withdrawn reads still count, so a pair CycleRace already fixed stays visible
    as closed upstream.  A re-identified read (same event under a new bib) is
    one passage, never a pair with itself.
    """

    by_group: dict[str, dict[str, dict[int, ChipRead]]] = defaultdict(lambda: defaultdict(dict))
    for read in sorted(reads, key=lambda read: (read.active_now, read.revision)):
        if read.lap > 0 and not read.excluded and read.bib:
            by_group[read.group_name][read.bib][read.lap] = read
    items = []
    for bibs in by_group.values():
        names = sorted(bibs, key=lambda bib: (len(bib), bib))
        for index, first in enumerate(names):
            for second in names[index + 1:]:
                common = sorted(set(bibs[first]) & set(bibs[second]))
                if len(common) < PAIR_MIN_LAPS:
                    continue
                pairs = [(bibs[first][lap], bibs[second][lap]) for lap in common]
                if any(a.event_id == b.event_id for a, b in pairs):
                    continue
                together = next((index for index, (a, b) in enumerate(pairs)
                                 if abs(a.chip_ms - b.chip_ms) > PAIR_WINDOW_MS), len(pairs))
                if together < PAIR_MIN_LAPS:
                    continue
                anchor = pairs[:PAIR_MIN_LAPS]
                members = [read for pair in anchor for read in pair]
                status, note = _status(members, minimum_active=len(members))
                widest = max(abs(a.chip_ms - b.chip_ms) for a, b in pairs[:together])
                if together < len(pairs) and status == STATUS_OPEN:
                    # Two athletes pacing each other split up at last; one
                    # athlete's two chips never do.  The row stays, answered.
                    a, b = pairs[together]
                    status = STATUS_INFO
                    note = f"第 {common[together]} 圈相差 {abs(a.chip_ms - b.chip_ms) / 1000:.1f} 秒，已经分开：是两个人"
                items.append(ProblemItem(
                    item_id=stable_item_id(KIND_PAIRED_CHIPS, first, second),
                    kind=KIND_PAIRED_CHIPS, category=CATEGORY_CHIP,
                    detail=(f"号码 {first} 和 {second} 连续 {together} 圈都一起过线（最大相差 {widest / 1000:.2f} 秒）："
                            "可能是一人带两个芯片，也可能是两人结伴跑，看原帧是 1 人还是 2 人"),
                    chip_time_ms=anchor[0][0].chip_ms, bibs=(first, second),
                    # The first laps bind the decision; later laps must not reopen it.
                    event_ids=tuple(read.event_id for read in members),
                    status=status, status_note=note, priority=20,
                    evidence={"laps": common, "lap_times": [[a.chip_ms, b.chip_ms] for a, b in pairs]},
                ))
    return items


def _not_arrived_items(reads: Sequence[ChipRead], context: ChipCheckContext) -> list[ProblemItem]:
    if not context.roster or not context.race_finished:
        return []
    arrived = {read.bib for read in reads if read.active_now and not read.excluded}
    missing: dict[str, list[str]] = defaultdict(list)
    for bib, group in context.roster.items():
        if bib in arrived or context.statuses.get(bib, "") not in _FINISHED_STATUSES:
            continue
        if any(keyword in group for keyword in ("测试", "备用")):
            continue
        missing[group].append(bib)
    items = []
    for group, bibs in sorted(missing.items()):
        bibs = sorted(bibs, key=lambda value: (len(value), value))
        items.append(ProblemItem(
            item_id=stable_item_id(KIND_NOT_ARRIVED, group),
            kind=KIND_NOT_ARRIVED, category=CATEGORY_COVERAGE,
            detail=f"{group} 应到未到 {len(bibs)} 人（名单减已到、减退赛）：" + "、".join(bibs[:60])
                   + ("……" if len(bibs) > 60 else ""),
            bibs=tuple(bibs), status=STATUS_INFO, priority=90,
            evidence={"group": group, "count": len(bibs)},
        ))
    return items


def _sync_items(issues: Sequence[SyncIssue], race_date_ms: int = 0) -> list[ProblemItem]:
    items = []
    for issue in issues:
        event_ids = (issue.event.event_id,) if issue.event is not None else ()
        chip_time = (
            int(issue.event.timeline_timestamp_ms) if issue.event is not None
            else (race_date_ms + int(issue.passage_time_ms) if race_date_ms else None)
        )
        items.append(ProblemItem(
            item_id=stable_item_id(KIND_SYNC, issue.kind, issue.passage_id),
            kind=KIND_SYNC, category=CATEGORY_SYNC, detail=issue.message,
            chip_time_ms=chip_time, bibs=(issue.bib,) if issue.bib else (),
            event_ids=event_ids, status=STATUS_OPEN, priority=10,
            evidence={"sync_kind": issue.kind, "passage_id": issue.passage_id, "is_manual": issue.is_manual},
        ))
    return items


def next_recheck_ms(reads: Sequence[ChipRead], context: ChipCheckContext) -> Optional[int]:
    """When a live check must run again even if no new read arrives: a group settles."""

    pending = [target.settle_at_ms for target in lap_targets(reads, context).values()
               if not target.settled and target.settle_at_ms is not None]
    return min(pending) if pending else None


def chip_check_items(
    reads: Sequence[ChipRead],
    context: ChipCheckContext = ChipCheckContext(),
    *,
    duplicate_window_ms: int = DUPLICATE_WINDOW_MS,
    race_date_ms: int = 0,
) -> tuple[ProblemItem, ...]:
    """Run all chip-only checks and return the items, pending ones first."""

    multi_lap = any(read.lap > 0 for read in reads)
    items: list[ProblemItem] = []
    items += _excluded_items(reads)
    items += _unknown_bib_items(reads, context.roster)
    items += _duplicate_items(reads, multi_lap=multi_lap, window_ms=duplicate_window_ms)
    items += _batch_items(reads, context.roster)
    items += _early_items(reads, context)
    items += _lap_items(reads, context)
    items += _paired_items(reads)
    items += _not_arrived_items(reads, context)
    items += _sync_items(context.sync_issues, race_date_ms)
    return tuple(sort_items(items))


__all__ = [
    "ChipCheckContext",
    "DUPLICATE_WINDOW_MS",
    "LapTarget",
    "chip_check_items",
    "lap_targets",
    "next_recheck_ms",
]
