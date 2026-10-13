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
    KIND_LAP_REGRESS,
    KIND_LAP_REPEAT,
    KIND_LAP_TIME,
    KIND_NOT_ARRIVED,
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
LAP_TIME_HIGH = 2.0
_FINISHED_STATUSES = {"", "OK"}


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

    ordered = sorted(reads, key=lambda read: read.chip_ms)
    items = []
    used: set[str] = set()
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
        if not flagged or all(item.read_id in used for item in flagged):
            continue
        used.update(item.read_id for item in flagged)
        status, note = _status(flagged, minimum_active=1)
        span = flagged[-1].chip_ms - flagged[0].chip_ms
        items.append(ProblemItem(
            item_id=stable_item_id(KIND_BATCH_READS, *sorted(item.read_id for item in flagged)),
            kind=KIND_BATCH_READS, category=CATEGORY_CHIP,
            detail=f"{span} ms 内 {len(flagged)} 条读数：" + "、".join(item.bib for item in flagged),
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


def _lap_items(reads: Sequence[ChipRead], context: ChipCheckContext) -> list[ProblemItem]:
    lapped = [read for read in reads if read.lap > 0 and not read.excluded]
    if not lapped:
        return []
    by_bib: dict[str, list[ChipRead]] = defaultdict(list)
    for read in lapped:
        by_bib[read.bib].append(read)
    lap_times: dict[str, list[int]] = defaultdict(list)
    for bib_reads in by_bib.values():
        bib_reads.sort(key=lambda read: read.chip_ms)
        for previous, current in zip(bib_reads, bib_reads[1:]):
            if current.lap == previous.lap + 1:
                lap_times[current.group_name].append(current.chip_ms - previous.chip_ms)
    group_max = defaultdict(int)
    for read in lapped:
        if read.active_now:
            group_max[read.group_name] = max(group_max[read.group_name], read.lap)
    items = []
    for bib, bib_reads in by_bib.items():
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
                        items.append(ProblemItem(
                            item_id=stable_item_id(KIND_LAP_TIME, previous.read_id, current.read_id),
                            kind=KIND_LAP_TIME,
                            detail=(
                                f"号码 {bib} 第 {current.lap} 圈用时 {lap_ms / 60_000:.1f} 分钟，"
                                f"组内中位 {median / 60_000:.1f} 分钟"
                            ),
                            **common,
                        ))
        if not context.race_finished:
            continue
        active = [read for read in bib_reads if read.active_now]
        if not active:
            continue
        last = max(read.lap for read in active)
        status_text = context.statuses.get(bib, "")
        if last < group_max[active[0].group_name] and status_text in _FINISHED_STATUSES:
            items.append(ProblemItem(
                item_id=stable_item_id(KIND_LAP_MISSING, bib, last),
                kind=KIND_LAP_MISSING, category=CATEGORY_CHIP,
                detail=f"号码 {bib} 只到第 {last} 圈，同组最多 {group_max[active[0].group_name]} 圈，且没有退赛状态",
                chip_time_ms=active[-1].chip_ms, bibs=(bib,), event_ids=(active[-1].event_id,),
                status=STATUS_OPEN, priority=35,
            ))
    return items


def _not_arrived_items(reads: Sequence[ChipRead], context: ChipCheckContext) -> list[ProblemItem]:
    if not context.roster:
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
    items += _not_arrived_items(reads, context)
    items += _sync_items(context.sync_issues, race_date_ms)
    return tuple(sort_items(items))


__all__ = [
    "ChipCheckContext",
    "DUPLICATE_WINDOW_MS",
    "chip_check_items",
]
