"""Merge rules that turn pairing results into finish problem items (plan 7.2/7.3).

Rules, in order:
1. an unpaired crossing near a chip-only read is that read arriving late
   (0.8 s; 1.5 s when both sides are isolated);
2. an extra crossing within 0.8 s of any expected chip time is the same
   rider (shadows, a second wheel) and is merged;
3. repeated activity at the same spot within 90 s becomes one "standing" item;
4. remaining bicycle-like crossings are "crossing without chip";
5. a chip-only read next to a paired crossing is "two chips, one crossing",
   otherwise "chip without crossing";
6. 4+ riders within 2 s and long obstructed periods get a manual check.

Paired riders are auto-passed and listed separately; they are never turned
into manual confirmations. No rule assigns a crossing to a nearby chip as a
result; items only describe what the operator should look at.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Mapping, Optional, Sequence

from .bike_check import (
    DECISION_DISPUTED,
    DECISION_EXCLUDE,
    LABEL_TEXT,
    LABEL_UNCHECKED,
    BikeVerdict,
)
from .chip_history import ChipRead, format_chip_time
from .clock_sync import ClockModel
from .problem_items import (
    CATEGORY_COVERAGE,
    CATEGORY_VIDEO,
    KIND_BUNCH,
    KIND_CHIP_NO_CROSSING,
    KIND_NO_CHIP_CROSSING,
    KIND_NO_VIDEO,
    KIND_OBSTRUCTION,
    KIND_STANDING,
    KIND_TWO_CHIPS_ONE_CROSSING,
    STATUS_CLOSED_UPSTREAM,
    STATUS_DISPUTED,
    STATUS_EXCLUDED,
    STATUS_INFO,
    STATUS_OPEN,
    ProblemItem,
    stable_item_id,
)


DUPLICATE_MS = 800
LATE_PAIR_MS = 800
ISOLATED_LATE_PAIR_MS = 1_500
ISOLATION_MS = 5_000
CROSSING_ISOLATION_MS = 1_500
TWO_CHIP_MS = 600
STANDING_ROWS = 50
STANDING_WINDOW_MS = 90_000
STANDING_MIN = 3
BUNCH_GAP_MS = 2_000
BUNCH_MIN = 4
BUNCH_MARGIN_MS = 700
OBSTRUCTION_MERGE_MS = 30_000
OBSTRUCTION_MIN_MS = 10_000
MANUAL_MATCH_MS = 1_500


@dataclass(frozen=True)
class CrossingRecord:
    """A detected crossing pinned to its source frame."""

    camera_index: int
    session_id: str
    segment_id: str
    video_path: str
    recorder_ms: int
    frame_index: int
    position_ms: int
    row: int
    r0: int
    r1: int
    in_obstruction: bool = False
    contact: tuple[float, float] = (0.0, 0.0)
    segment_points: tuple[tuple[float, float], ...] = ()

    @property
    def key(self) -> str:
        return f"{self.camera_index}|{self.video_path}|{self.frame_index}|{self.row}"

    def frame_payload(self, calibration_version: int) -> dict:
        return {
            "camera_index": self.camera_index,
            "segment_id": self.segment_id,
            "video_path": self.video_path,
            "frame_index": self.frame_index,
            "position_ms": self.position_ms,
            "session_id": self.session_id,
            "calibration_version": calibration_version,
        }


@dataclass(frozen=True)
class Obstruction:
    camera_index: int
    start_ms: int
    end_ms: int


@dataclass
class RuleOutcome:
    items: list[ProblemItem] = field(default_factory=list)
    auto_passed: list[dict] = field(default_factory=list)
    late_pairs: list[tuple[str, int]] = field(default_factory=list)
    duplicates: int = 0
    stats: dict = field(default_factory=dict)


def _chip_text(read: ChipRead) -> str:
    return f"{read.bib}（{format_chip_time(read.chip_ms)}）"


def _closed_note(reads: Sequence[ChipRead]) -> tuple[str, str]:
    inactive = [read for read in reads if not read.active_now]
    if not inactive:
        return STATUS_OPEN, ""
    return STATUS_CLOSED_UPSTREAM, "CycleRace 已作废 " + "、".join(read.bib for read in inactive)


def _crossing_evidence(crossing: CrossingRecord, version: int, verdict: Optional[BikeVerdict] = None) -> dict:
    evidence = {
        "frame": crossing.frame_payload(version),
        "contact": list(crossing.contact),
        "segment_points": [list(point) for point in crossing.segment_points],
        "recorder_ms": crossing.recorder_ms,
        "row": crossing.row,
    }
    if verdict is not None:
        evidence["bike"] = {
            "label": verdict.label, "score": round(verdict.score, 3), "decision": verdict.decision,
            "reason": verdict.reason, "vehicle": verdict.vehicle,
        }
    return evidence


def build_video_items(
    *,
    camera_index: int,
    model: ClockModel,
    matched: Sequence[tuple[CrossingRecord, ChipRead, float]],
    video_only: Sequence[CrossingRecord],
    chip_only: Sequence[ChipRead],
    no_video: Sequence[ChipRead],
    obstructions: Sequence[Obstruction] = (),
    verdicts: Mapping[str, BikeVerdict] = {},
    manual_entries: Sequence[ChipRead] = (),
    chip_locator=None,
    expected_ms: Optional[Mapping[str, int]] = None,
    race_start_ms: Optional[int] = None,
) -> RuleOutcome:
    """Apply the merge rules; ``chip_locator(read)`` pins a chip to a frame."""

    version = int(model.version)
    outcome = RuleOutcome()
    expected = {
        read.read_id: (expected_ms or {}).get(read.read_id, model.to_recorder(read.chip_ms))
        for read in [*chip_only, *(m[1] for m in matched)]
    }
    all_chip_times = sorted(read.chip_ms for read in [*chip_only, *(m[1] for m in matched)])
    crossing_times = sorted([m[0].recorder_ms for m in matched] + [c.recorder_ms for c in video_only])
    periods = _merge_obstructions(obstructions)

    def crowded(recorder_ms: int) -> bool:
        return any(start - 5_000 <= recorder_ms <= end + 5_000 for start, end in periods)

    def isolated_chip(read: ChipRead) -> bool:
        return sum(abs(t - read.chip_ms) < ISOLATION_MS for t in all_chip_times) <= 1

    def isolated_crossing(crossing: CrossingRecord) -> bool:
        return sum(abs(t - crossing.recorder_ms) < CROSSING_ISOLATION_MS for t in crossing_times) <= 1

    candidates: list[tuple[CrossingRecord, Optional[BikeVerdict]]] = []
    excluded = []
    before_race = []
    for crossing in video_only:
        if crossing.in_obstruction:
            continue
        if race_start_ms is not None and crossing.recorder_ms < race_start_ms:
            before_race.append(crossing)
            continue
        verdict = verdicts.get(crossing.key)
        if verdict is not None and verdict.decision == DECISION_EXCLUDE:
            excluded.append((crossing, verdict))
            continue
        candidates.append((crossing, verdict))

    resolved: set[str] = set()
    rest: list[tuple[CrossingRecord, Optional[BikeVerdict]]] = []
    expected_values = sorted(expected.values())
    for crossing, verdict in sorted(candidates, key=lambda item: item[0].recorder_ms):
        near = [
            read for read in chip_only
            if read.read_id not in resolved and (
                abs(crossing.recorder_ms - expected[read.read_id]) <= LATE_PAIR_MS
                or (
                    abs(crossing.recorder_ms - expected[read.read_id]) <= ISOLATED_LATE_PAIR_MS
                    and isolated_chip(read) and isolated_crossing(crossing)
                )
            )
        ]
        if near:
            read = min(near, key=lambda item: abs(crossing.recorder_ms - expected[item.read_id]))
            resolved.add(read.read_id)
            residual = int(crossing.recorder_ms - expected[read.read_id])
            outcome.late_pairs.append((read.bib, residual))
            outcome.auto_passed.append(_auto_passed(read, crossing, residual, version, late=True))
            continue
        if any(abs(crossing.recorder_ms - value) <= DUPLICATE_MS for value in expected_values):
            outcome.duplicates += 1
            continue
        rest.append((crossing, verdict))

    spots: list[dict] = []
    for crossing, verdict in rest:
        for spot in spots:
            if abs(crossing.row - spot["row"]) <= STANDING_ROWS and crossing.recorder_ms - spot["last"] <= STANDING_WINDOW_MS:
                spot["members"].append((crossing, verdict))
                spot["last"] = crossing.recorder_ms
                break
        else:
            spots.append({"row": crossing.row, "last": crossing.recorder_ms, "members": [(crossing, verdict)]})

    for spot in spots:
        members = spot["members"]
        first = members[0][0]
        chip_time = model.to_chip(first.recorder_ms, first.session_id)
        if len(members) >= STANDING_MIN:
            last = members[-1][0]
            outcome.items.append(ProblemItem(
                item_id=stable_item_id(KIND_STANDING, camera_index, first.key),
                kind=KIND_STANDING, category=CATEGORY_VIDEO,
                detail=(
                    f"线上同一位置 {format_chip_time(chip_time)}–{format_chip_time(model.to_chip(last.recorder_ms, last.session_id))} "
                    f"反复出现 {len(members)} 次活动，可能有人停留"
                ),
                chip_time_ms=chip_time, status=STATUS_OPEN, priority=40,
                evidence=dict(_crossing_evidence(first, version), count=len(members)),
                calibration_version=version,
            ))
            continue
        for crossing, verdict in members:
            chip_time = model.to_chip(crossing.recorder_ms, crossing.session_id)
            label = verdict.label if verdict is not None else LABEL_UNCHECKED
            status, note = STATUS_OPEN, ""
            if verdict is not None and verdict.decision == DECISION_DISPUTED:
                status, note = STATUS_DISPUTED, f"有争议：{LABEL_TEXT.get(label, label)}，{verdict.reason}"
            elif verdict is not None and verdict.decision != DECISION_EXCLUDE and crowded(crossing.recorder_ms):
                note = "终点线拥挤时段"
            manual = [
                read for read in manual_entries
                if read.active_now and abs(read.chip_ms - chip_time) <= MANUAL_MATCH_MS
            ]
            if manual:
                status = STATUS_CLOSED_UPSTREAM
                note = "CycleRace 已补录 " + "、".join(_chip_text(read) for read in manual)
            outcome.items.append(ProblemItem(
                item_id=stable_item_id(KIND_NO_CHIP_CROSSING, camera_index, crossing.key),
                kind=KIND_NO_CHIP_CROSSING, category=CATEGORY_VIDEO,
                detail=f"{format_chip_time(chip_time)} 有人过线，附近没有芯片读数（认车：{LABEL_TEXT.get(label, label)}）",
                chip_time_ms=chip_time, bibs=tuple(read.bib for read in manual),
                event_ids=tuple(read.event_id for read in manual),
                status=status, status_note=note, priority=10,
                evidence=_crossing_evidence(crossing, version, verdict),
                calibration_version=version,
            ))

    paired_crossings = [m[0] for m in matched]
    for read in chip_only:
        if read.read_id in resolved:
            continue
        expected_ms = expected[read.read_id]
        partners = [
            (crossing, chip) for crossing, chip, _residual in matched
            if abs(crossing.recorder_ms - expected_ms) <= TWO_CHIP_MS
        ]
        if partners:
            crossing, partner = min(partners, key=lambda item: abs(item[0].recorder_ms - expected_ms))
            status, note = _closed_note((read, partner))
            outcome.items.append(ProblemItem(
                item_id=stable_item_id(KIND_TWO_CHIPS_ONE_CROSSING, camera_index, read.read_id, partner.read_id),
                kind=KIND_TWO_CHIPS_ONE_CROSSING, category=CATEGORY_VIDEO,
                detail=f"芯片 {_chip_text(read)} 与 {_chip_text(partner)} 对应同一次过线",
                chip_time_ms=read.chip_ms, bibs=(read.bib, partner.bib),
                event_ids=(read.event_id, partner.event_id),
                status=status, status_note=note, priority=20,
                evidence=_crossing_evidence(crossing, version),
                calibration_version=version,
            ))
            continue
        status, note = _closed_note((read,))
        if status == STATUS_OPEN and crowded(expected_ms):
            note = "线被挡，证据不足"
        evidence = {"expected_recorder_ms": int(expected_ms)}
        if chip_locator is not None:
            located = chip_locator(read, expected_ms)
            if located:
                evidence.update(located)
        if paired_crossings:
            nearest = min(paired_crossings, key=lambda crossing: abs(crossing.recorder_ms - expected_ms))
            evidence["nearest_crossing_ms"] = int(nearest.recorder_ms - expected_ms)
        outcome.items.append(ProblemItem(
            item_id=stable_item_id(KIND_CHIP_NO_CROSSING, camera_index, read.read_id),
            kind=KIND_CHIP_NO_CROSSING, category=CATEGORY_VIDEO,
            detail=f"芯片 {_chip_text(read)} 附近没有看到过线",
            chip_time_ms=read.chip_ms, bibs=(read.bib,), event_ids=(read.event_id,),
            status=status, status_note=note, priority=30, evidence=evidence,
            calibration_version=version,
        ))

    for crossing, verdict in excluded:
        chip_time = model.to_chip(crossing.recorder_ms, crossing.session_id)
        outcome.items.append(ProblemItem(
            item_id=stable_item_id(KIND_NO_CHIP_CROSSING, camera_index, crossing.key),
            kind=KIND_NO_CHIP_CROSSING, category=CATEGORY_VIDEO,
            detail=f"{format_chip_time(chip_time)} 线上活动，认车为{LABEL_TEXT.get(verdict.label, verdict.label)}"
                   + (f"（{verdict.vehicle}）" if verdict.vehicle else ""),
            chip_time_ms=chip_time, status=STATUS_EXCLUDED,
            status_note=f"已排除：{LABEL_TEXT.get(verdict.label, verdict.label)} {verdict.score:.2f}",
            priority=90, evidence=_crossing_evidence(crossing, version, verdict),
            calibration_version=version,
        ))

    for crossing in before_race:
        chip_time = model.to_chip(crossing.recorder_ms, crossing.session_id)
        outcome.items.append(ProblemItem(
            item_id=stable_item_id(KIND_NO_CHIP_CROSSING, camera_index, crossing.key),
            kind=KIND_NO_CHIP_CROSSING, category=CATEGORY_VIDEO,
            detail=f"{format_chip_time(chip_time)} 首位选手到达前的线上活动",
            chip_time_ms=chip_time, status=STATUS_EXCLUDED, status_note="已排除：赛前活动",
            priority=95, evidence=_crossing_evidence(crossing, version),
            calibration_version=version,
        ))

    open_bibs = {
        read_id
        for item in outcome.items if item.pending
        for read_id in item.event_ids
    }
    for crossing, read, residual in matched:
        if read.event_id in open_bibs:
            continue
        outcome.auto_passed.append(_auto_passed(read, crossing, int(residual), version))

    bunches = _bunch_items(camera_index, model, matched, chip_only, rest, version)
    race_periods = [
        (start, end) for start, end in periods if race_start_ms is None or end >= race_start_ms
    ]
    crowds = _obstruction_items(camera_index, model, race_periods, matched, chip_only, version)
    outcome.items = _group_members(outcome.items, bunches, crowds, expected)
    outcome.items += _no_video_items(camera_index, no_video)
    outcome.stats = {
        "candidates": len(candidates),
        "excluded": len(excluded),
        "late_pairs": len(outcome.late_pairs),
        "duplicates": outcome.duplicates,
        "standing": sum(len(spot["members"]) >= STANDING_MIN for spot in spots),
        "before_race": len(before_race),
    }
    return outcome


def _item_recorder_ms(item: ProblemItem, expected: Mapping[str, int]) -> Optional[int]:
    if "recorder_ms" in item.evidence:
        return int(item.evidence["recorder_ms"])
    if "expected_recorder_ms" in item.evidence:
        return int(item.evidence["expected_recorder_ms"])
    return None


def _group_members(items, bunches, crowds, expected) -> list[ProblemItem]:
    """Fold items inside a bunch or a crowded period into that group item.

    Bunches (4+ riders within 2 s) need a head count anyway (plan 7.4), and a
    crowded line period is checked as a whole (plan 7.1). Members stay in the
    list with a link to their group, so nothing is dropped or excluded.
    """

    groups = []
    for bunch in bunches:
        window = (bunch.evidence["recorder_start_ms"], bunch.evidence["recorder_end_ms"])
        groups.append((bunch, window, {KIND_TWO_CHIPS_ONE_CROSSING, KIND_CHIP_NO_CROSSING, KIND_NO_CHIP_CROSSING}))
    for crowd in crowds:
        window = (crowd.evidence["recorder_start_ms"] - 5_000, crowd.evidence["recorder_end_ms"] + 5_000)
        groups.append((crowd, window, {KIND_NO_CHIP_CROSSING, KIND_STANDING}))
    members: dict[str, list[str]] = {}
    result = []
    for item in items:
        group = None
        position = _item_recorder_ms(item, expected)
        if item.pending and position is not None:
            for candidate, (start, end), kinds in groups:
                if item.kind not in kinds or not start <= position <= end:
                    continue
                if candidate.kind == KIND_OBSTRUCTION and item.kind == KIND_NO_CHIP_CROSSING and item.status != STATUS_DISPUTED:
                    continue  # a likely rider in a crowd stays a separate item
                group = candidate
                break
        if group is None:
            result.append(item)
            continue
        members.setdefault(group.item_id, []).append(item.item_id)
        result.append(replace(item, evidence=dict(item.evidence, group=group.item_id)))
    for candidate, _window, _kinds in groups:
        ids = members.get(candidate.item_id, [])
        if ids:
            candidate = replace(
                candidate,
                detail=candidate.detail + f"；组内另有 {len(ids)} 条待核对问题",
                evidence=dict(candidate.evidence, members=ids),
            )
        result.append(candidate)
    return result


def _auto_passed(read: ChipRead, crossing: CrossingRecord, residual: int, version: int, *, late: bool = False) -> dict:
    return {
        "bib": read.bib,
        "event_id": read.event_id,
        "chip_time_ms": read.chip_ms,
        "active_now": read.active_now,
        "residual_ms": int(residual),
        "late_pair": late,
        "source": "auto",
        "frame": crossing.frame_payload(version),
        "contact": list(crossing.contact),
    }


def _merge_obstructions(obstructions: Sequence[Obstruction]) -> list[tuple[int, int]]:
    periods: list[list[int]] = []
    for item in sorted(obstructions, key=lambda value: value.start_ms):
        if periods and item.start_ms - periods[-1][1] <= OBSTRUCTION_MERGE_MS:
            periods[-1][1] = max(periods[-1][1], item.end_ms)
            periods[-1][2] += item.end_ms - item.start_ms
        else:
            periods.append([item.start_ms, item.end_ms, item.end_ms - item.start_ms])
    return [(start, end) for start, end, total in periods if total >= OBSTRUCTION_MIN_MS]


def _bunch_items(camera_index, model, matched, chip_only, rest, version) -> list[ProblemItem]:
    reads = sorted([m[1] for m in matched] + list(chip_only), key=lambda read: read.chip_ms)
    groups: list[list[ChipRead]] = []
    for read in reads:
        if groups and read.chip_ms - groups[-1][-1].chip_ms <= BUNCH_GAP_MS:
            groups[-1].append(read)
        else:
            groups.append([read])
    crossings = [m[0].recorder_ms for m in matched] + [crossing.recorder_ms for crossing, _verdict in rest]
    items = []
    for group in groups:
        if len(group) < BUNCH_MIN:
            continue
        start = model.to_recorder(group[0].chip_ms) - BUNCH_MARGIN_MS
        end = model.to_recorder(group[-1].chip_ms) + BUNCH_MARGIN_MS
        video = sum(start <= value <= end for value in crossings)
        items.append(ProblemItem(
            item_id=stable_item_id(KIND_BUNCH, camera_index, *(read.read_id for read in group)),
            kind=KIND_BUNCH, category=CATEGORY_VIDEO,
            detail=(
                f"{format_chip_time(group[0].chip_ms)} 起 {len(group)} 人相隔 2 秒以内过线：芯片 {len(group)} 个，"
                f"自动计数 {video} 次" + ("（不一致）" if video != len(group) else "")
            ),
            chip_time_ms=group[0].chip_ms, bibs=tuple(read.bib for read in group),
            event_ids=tuple(read.event_id for read in group), status=STATUS_OPEN, priority=50,
            evidence={"recorder_start_ms": int(start), "recorder_end_ms": int(end), "video_count": video,
                      "chip_count": len(group)},
            calibration_version=version,
        ))
    return items


def _obstruction_items(camera_index, model, periods, matched, chip_only, version) -> list[ProblemItem]:
    items = []
    for start, end in periods:
        inside = [
            read for read in [m[1] for m in matched] + list(chip_only)
            if start <= model.to_recorder(read.chip_ms) <= end
        ]
        items.append(ProblemItem(
            item_id=stable_item_id(KIND_OBSTRUCTION, camera_index, start),
            kind=KIND_OBSTRUCTION, category=CATEGORY_VIDEO,
            detail=(
                f"{format_chip_time(model.to_chip(start))}–{format_chip_time(model.to_chip(end))} 终点线被挡或拥挤"
                f"（{(end - start) / 1000:.0f} 秒），期间芯片 {len(inside)} 个；证据不足，请核对该时段"
            ),
            chip_time_ms=model.to_chip(start), bibs=tuple(read.bib for read in inside),
            event_ids=tuple(read.event_id for read in inside), status=STATUS_OPEN, priority=60,
            evidence={"recorder_start_ms": int(start), "recorder_end_ms": int(end)},
            calibration_version=version,
        ))
    return items


def _no_video_items(camera_index, no_video) -> list[ProblemItem]:
    reads = sorted((read for read in no_video if read.active_now), key=lambda read: read.chip_ms)
    if not reads:
        return []
    groups: list[list[ChipRead]] = [[reads[0]]]
    for read in reads[1:]:
        if read.chip_ms - groups[-1][-1].chip_ms <= 60_000:
            groups[-1].append(read)
        else:
            groups.append([read])
    return [
        ProblemItem(
            item_id=stable_item_id(KIND_NO_VIDEO, camera_index, group[0].read_id),
            kind=KIND_NO_VIDEO, category=CATEGORY_COVERAGE,
            detail=(
                f"{format_chip_time(group[0].chip_ms)}–{format_chip_time(group[-1].chip_ms)} 没有录像覆盖，"
                f"{len(group)} 个芯片无法核实，不算通过：" + "、".join(read.bib for read in group[:40])
                + ("……" if len(group) > 40 else "")
            ),
            chip_time_ms=group[0].chip_ms, bibs=tuple(read.bib for read in group),
            event_ids=tuple(read.event_id for read in group), status=STATUS_INFO, priority=95,
        )
        for group in groups
    ]


__all__ = [
    "CrossingRecord",
    "Obstruction",
    "RuleOutcome",
    "build_video_items",
]
