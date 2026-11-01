"""Merge rules that turn pairing results into finish problem items (plan 7.2/7.3).

Rules, in order:
1. an unpaired crossing near a chip-only read is that read arriving late
   (0.8 s; 1.5 s when both sides are isolated);
2. an extra crossing beside a chip that is already matched to another crossing
   is only merged when it belongs to the same passage: the same moment or an
   adjacent frame of the same session. The merged piece stays in that rider's
   evidence. Anything else becomes one "附近可能另有过线" item per chip, never
   a silent drop and never a task per fragment (plan step 5);
3. repeated activity at the same spot within 90 s becomes one "standing" item;
4. remaining bicycle-like crossings are "crossing without chip";
5. chips within 1 s of a group's first chip are one group, never chained on to
   the next rider. A pair of chips on one crossing, with the line in view and
   fewer than six riders within 3 s, is "two chips, one crossing"; every other
   group of two or more gets one manual check row, also when video and chips
   agree (two riders side by side are counted once by the line). A chip-only
   read in a group is "chip without crossing" inside that row;
6. three or more crossings within 1 s with fewer chips, and long obstructed
   periods, get a manual check.

Paired riders are auto-passed and listed separately; they are never turned
into manual confirmations. No rule assigns a crossing to a nearby chip as a
result; items only describe what the operator should look at. Chip or video
counts never decide identity: only the operator reads a number.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Mapping, Optional, Sequence

from .bike_check import (
    DECISION_DISPUTED,
    DECISION_EXCLUDE,
    LABEL_BICYCLE,
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
    KIND_NEAR_CROSSING,
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
SIMULTANEOUS_MS = 300
SAME_CROSSING_MS = 150
SAME_CROSSING_FRAMES = 2
DENSE_WINDOW_MS = 2_000
SMALL_GROUP_MIN = 3
BUNCH_MARGIN_MS = 700
# A group is first-to-last chip time within 1 s, anchored on the first chip.
GROUP_SPAN_MS = 1_000
# Unpaired crossings this close to a member's expected time count for the group.
GROUP_VIDEO_MS = 300
# The video count of a group is trusted only with fewer than six riders (chips
# or crossings) within 3 s of it and the line in view the whole time.
QUIET_MS = 3_000
BUSY_RIDERS = 6
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


@dataclass(frozen=True)
class NearCrossing:
    """An extra crossing beside a chip that is already matched elsewhere.

    ``read`` is the nearby chip the operator should compare against; it is a
    candidate association, never a decision about who crossed.
    """

    crossing: CrossingRecord
    read: ChipRead
    anchor: Optional[CrossingRecord]
    gap_ms: int
    verdict: Optional[BikeVerdict] = None


@dataclass
class _ChipGroup:
    """Riders whose chips fall within 1 s of the group's first chip."""

    members: list[tuple[ChipRead, bool]] = field(default_factory=list)  # (read, manual entry)
    expected: list[int] = field(default_factory=list)  # recorder time of each member
    events: dict[str, CrossingRecord] = field(default_factory=dict)
    reliable: bool = True

    @property
    def reads(self) -> list[ChipRead]:
        return [read for read, _manual in self.members]

    @property
    def manual(self) -> list[ChipRead]:
        return [read for read, manual in self.members if manual]

    @property
    def start(self) -> int:
        return min(self.expected)

    @property
    def end(self) -> int:
        return max(self.expected)


@dataclass
class RuleOutcome:
    items: list[ProblemItem] = field(default_factory=list)
    auto_passed: list[dict] = field(default_factory=list)
    late_pairs: list[tuple[str, int]] = field(default_factory=list)
    late_pair_frames: list[dict] = field(default_factory=list)
    duplicates: int = 0
    fragments: list[dict] = field(default_factory=list)
    near_crossings: list[dict] = field(default_factory=list)
    stats: dict = field(default_factory=dict)


def _chip_text(read: ChipRead) -> str:
    return f"{read.bib}（{format_chip_time(read.chip_ms)}）"


def _closed_note(reads: Sequence[ChipRead]) -> tuple[str, str]:
    inactive = [read for read in reads if not read.active_now]
    if not inactive:
        return STATUS_OPEN, ""
    return STATUS_CLOSED_UPSTREAM, "CycleRace 已作废 " + "、".join(read.bib for read in inactive)


def same_passage(extra: CrossingRecord, anchor: Optional[CrossingRecord]) -> tuple[bool, str]:
    """Whether an extra change is part of an already detected crossing.

    Only the geometry the detector itself merges on: the same moment, or an
    adjacent frame of the same session. Rows along the line are not used, so a
    second rider is never merged just because the chip time is close; at the
    same moment one rider cannot be two riders, and the strip already merges
    pieces within ``same_start_ms``. Everything wider stays a candidate.
    """

    if anchor is None or extra.session_id != anchor.session_id:
        return False, ""
    if abs(extra.recorder_ms - anchor.recorder_ms) <= SAME_CROSSING_MS:
        return True, "同一时刻的两处变化属于同一次过线"
    if (extra.video_path == anchor.video_path
            and abs(extra.frame_index - anchor.frame_index) <= SAME_CROSSING_FRAMES):
        return True, "相邻帧上的两处变化属于同一次过线（前后轮或影子）"
    return False, ""


def _nearest_expected(
    crossing: CrossingRecord, expected: Mapping[str, int], window_ms: int,
) -> Optional[tuple[str, int]]:
    """Closest already matched chip time, used as a candidate association only."""

    best: Optional[tuple[str, int]] = None
    for read_id, value in expected.items():
        gap = int(crossing.recorder_ms - value)
        if abs(gap) <= window_ms and (best is None or abs(gap) < abs(best[1])):
            best = (read_id, gap)
    return best


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
    periods = merge_obstructions(obstructions)

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
        if crossing.in_obstruction and crossing.key not in verdicts:
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
    late_crossing: dict[str, CrossingRecord] = {}
    rest: list[tuple[CrossingRecord, Optional[BikeVerdict]]] = []
    near_candidates: list[NearCrossing] = []
    anchor_of: dict[str, CrossingRecord] = {read.read_id: crossing for crossing, read, _residual in matched}
    read_of: dict[str, ChipRead] = {read.read_id: read for read in [*chip_only, *(item[1] for item in matched)]}
    expected_values = sorted(expected.values())
    for crossing, verdict in sorted(candidates, key=lambda item: item[0].recorder_ms):
        if verdict is not None and verdict.label in ("pedestrian", "vehicle"):
            rest.append((crossing, verdict))
            continue  # A nearby chip cannot turn a non-bicycle target into a rider.
        late = [
            read for read in chip_only
            if read.read_id not in resolved and (
                abs(crossing.recorder_ms - expected[read.read_id]) <= LATE_PAIR_MS
                or (
                    abs(crossing.recorder_ms - expected[read.read_id]) <= ISOLATED_LATE_PAIR_MS
                    and isolated_chip(read) and isolated_crossing(crossing)
                )
            )
        ]
        if late:
            read = min(late, key=lambda item: abs(crossing.recorder_ms - expected[item.read_id]))
            resolved.add(read.read_id)
            late_crossing[read.read_id] = crossing
            residual = int(crossing.recorder_ms - expected[read.read_id])
            outcome.late_pairs.append((read.bib, residual))
            outcome.late_pair_frames.append(dict(_auto_passed(read, crossing, residual, version, late=True)))
            outcome.auto_passed.append(_auto_passed(read, crossing, residual, version, late=True))
            continue
        neighbor = _nearest_expected(crossing, expected, DUPLICATE_MS)
        if neighbor is None:
            rest.append((crossing, verdict))
            continue
        read = read_of[neighbor[0]]
        anchor = anchor_of.get(read.read_id)
        same, reason = same_passage(crossing, anchor)
        if same:
            # Same rider, not a dropped crossing: the piece stays in that
            # rider's evidence so the merge has a traceable reason.
            outcome.duplicates += 1
            outcome.fragments.append({
                "bib": read.bib, "event_id": read.event_id, "gap_ms": int(neighbor[1]), "reason": reason,
                "recorder_ms": int(crossing.recorder_ms), "frame": crossing.frame_payload(version),
            })
            continue
        near_candidates.append(NearCrossing(crossing, read, anchor, int(neighbor[1]), verdict))

    spots: list[dict] = []
    for crossing, verdict in rest:
        for spot in spots:
            if abs(crossing.row - spot["row"]) <= STANDING_ROWS and crossing.recorder_ms - spot["last"] <= STANDING_WINDOW_MS:
                spot["members"].append((crossing, verdict))
                spot["last"] = crossing.recorder_ms
                break
        else:
            spots.append({"row": crossing.row, "last": crossing.recorder_ms, "members": [(crossing, verdict)]})

    singles: list[tuple[CrossingRecord, Optional[BikeVerdict]]] = []
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
        singles.extend(members)

    # Changes at several places on the line in the same moment are one item:
    # a rider can split into two parts on the line, and one card shows all.
    clusters: list[list[tuple[CrossingRecord, Optional[BikeVerdict]]]] = []
    for member in sorted(singles, key=lambda item: item[0].recorder_ms):
        if clusters and member[0].recorder_ms - clusters[-1][-1][0].recorder_ms <= SIMULTANEOUS_MS:
            clusters[-1].append(member)
        else:
            clusters.append([member])
    for cluster in clusters:
        crossing, verdict = min(cluster, key=_evidence_rank)
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
        evidence = _crossing_evidence(crossing, version, verdict)
        # Group size is not read from chips: a stretch with several crossings and
        # fewer chips is kept visible even when no chip is missing entirely.
        dense_start = crossing.recorder_ms - DENSE_WINDOW_MS
        dense_end = crossing.recorder_ms + DENSE_WINDOW_MS
        dense_video = sum(dense_start <= value <= dense_end for value in crossing_times)
        dense_chips = sum(dense_start <= value <= dense_end for value in expected_values)
        dense = dense_video >= SMALL_GROUP_MIN and dense_video > dense_chips
        if dense:
            evidence = dict(evidence, dense_window={"video_count": dense_video, "chip_count": dense_chips,
                                                    "start_ms": int(dense_start), "end_ms": int(dense_end)})
        outcome.items.append(ProblemItem(
            item_id=stable_item_id(KIND_NO_CHIP_CROSSING, camera_index, crossing.key),
            kind=KIND_NO_CHIP_CROSSING, category=CATEGORY_VIDEO,
            detail=(
                f"{format_chip_time(chip_time)} 有人过线，附近没有芯片读数（认车：{LABEL_TEXT.get(label, label)}）"
                + (f"；同一时刻线上另有 {len(cluster) - 1} 处变化" if len(cluster) > 1 else "")
                + (f"；前后 2 秒内共 {dense_video} 处过线、附近只有 {dense_chips} 条芯片，请一并核对"
                   if dense else "")
            ),
            chip_time_ms=chip_time, bibs=tuple(read.bib for read in manual),
            event_ids=tuple(read.event_id for read in manual),
            status=status, status_note=note, priority=10,
            evidence=evidence, calibration_version=version,
        ))

    # Extra changes beside a chip time that is already used by another crossing.
    # They are kept as one traceable question per chip; the nearby chip is an
    # association for the operator, not a reason to decide who crossed.
    near_groups: list[list[NearCrossing]] = []
    for entry in sorted(near_candidates, key=lambda value: value.crossing.recorder_ms):
        if (near_groups and entry.read.read_id == near_groups[-1][-1].read.read_id
                and entry.crossing.recorder_ms - near_groups[-1][-1].crossing.recorder_ms <= SIMULTANEOUS_MS):
            near_groups[-1].append(entry)
        else:
            near_groups.append([entry])
    for group in near_groups:
        best = min(group, key=lambda value: _evidence_rank((value.crossing, value.verdict)))
        crossing, verdict, read = best.crossing, best.verdict, best.read
        chip_time = model.to_chip(crossing.recorder_ms, crossing.session_id)
        label = verdict.label if verdict is not None else LABEL_UNCHECKED
        status, note = STATUS_OPEN, ""
        if verdict is not None and verdict.decision == DECISION_DISPUTED:
            status, note = STATUS_DISPUTED, f"有争议：{LABEL_TEXT.get(label, label)}，{verdict.reason}"
        elif crowded(crossing.recorder_ms):
            note = "终点线拥挤时段"
        manual = [
            entry for entry in manual_entries
            if entry.active_now and abs(entry.chip_ms - chip_time) <= MANUAL_MATCH_MS
        ]
        if manual:
            status = STATUS_CLOSED_UPSTREAM
            note = "CycleRace 已补录 " + "、".join(_chip_text(entry) for entry in manual)
        members = [{
            "frame": value.crossing.frame_payload(version),
            "recorder_ms": int(value.crossing.recorder_ms),
            "chip_time_ms": model.to_chip(value.crossing.recorder_ms, value.crossing.session_id),
            "gap_to_chip_ms": int(value.gap_ms),
            "row": int(value.crossing.row),
            "bike": ({"label": value.verdict.label, "score": round(value.verdict.score, 3),
                      "decision": value.verdict.decision, "reason": value.verdict.reason}
                     if value.verdict is not None else None),
        } for value in sorted(group, key=lambda value: value.crossing.recorder_ms)]
        outcome.near_crossings.append({
            "bib": read.bib, "event_id": read.event_id, "gap_ms": int(best.gap_ms),
            "recorder_ms": int(crossing.recorder_ms), "members": len(group), "bike": members[0]["bike"],
        })
        outcome.items.append(ProblemItem(
            item_id=stable_item_id(KIND_NEAR_CROSSING, camera_index, read.read_id, crossing.key),
            kind=KIND_NEAR_CROSSING, category=CATEGORY_VIDEO,
            detail=(
                f"{format_chip_time(chip_time)} 线上另有一处变化，距芯片 {_chip_text(read)} 的过线 "
                f"{best.gap_ms:+d} ms（认车：{LABEL_TEXT.get(label, label)}）；"
                "可能另有一位漏读选手，也可能只是同一人的后轮或影子，请按原帧判断"
                + (f"；同一芯片附近另有 {len(group) - 1} 处变化，见证据" if len(group) > 1 else "")
            ),
            chip_time_ms=chip_time, status=status, status_note=note, priority=15,
            evidence=dict(
                _crossing_evidence(crossing, version, verdict),
                near_read={"bib": read.bib, "event_id": read.event_id, "chip_ms": read.chip_ms,
                           "expected_recorder_ms": int(expected[read.read_id])},
                gap_ms=int(best.gap_ms), members=members,
            ),
            calibration_version=version,
        ))

    paired_crossings = [m[0] for m in matched]
    crossing_of = {read.read_id: crossing for crossing, read, _residual in matched}
    crossing_of.update(late_crossing)
    groups = _chip_groups([m[1] for m in matched] + list(chip_only), manual_entries, expected, model)
    _assign_group_events(groups, crossing_of,
                         [crossing for crossing, _verdict in singles] + [entry.crossing for entry in near_candidates])
    chip_marks = sorted(value for group in groups for value in group.expected)

    def view_is_clear(start: int, end: int, crossings) -> bool:
        """The video count can be trusted: few riders around and the line in view."""

        low, high = start - QUIET_MS, end + QUIET_MS
        busy = max(sum(low <= value <= high for value in chip_marks),
                   sum(low <= value <= high for value in crossing_times))
        return (busy < BUSY_RIDERS and not crowded(start) and not crowded(end)
                and not any(crossing.in_obstruction for crossing in crossings))

    claimed: set[str] = set()
    group_of: dict[str, _ChipGroup] = {}
    for group in groups:
        if len(group.members) < 2:
            continue
        group.reliable = view_is_clear(group.start, group.end, group.events.values())
        for read in group.reads:
            group_of[read.read_id] = group
        if len(group.members) != 2 or group.manual or not group.reliable or len(group.events) != 1:
            continue
        # Two chips within 1 s and one crossing, the view clear: the plainest
        # double-chip case, asked as one question about the pair.
        crossing = next(iter(group.events.values()))
        partner = min(group.reads, key=lambda value: (crossing_of.get(value.read_id) is not crossing,
                                                      abs(expected[value.read_id] - crossing.recorder_ms)))
        read = next(value for value in group.reads if value is not partner)
        if abs(expected[read.read_id] - crossing.recorder_ms) > TWO_CHIP_MS:
            continue  # too far from that crossing to be on the same rider: the group is counted
        claimed.update((read.read_id, partner.read_id))
        status, note = _closed_note((read, partner))
        outcome.items.append(ProblemItem(
            item_id=stable_item_id(KIND_TWO_CHIPS_ONE_CROSSING, camera_index, read.read_id, partner.read_id),
            kind=KIND_TWO_CHIPS_ONE_CROSSING, category=CATEGORY_VIDEO,
            detail=f"芯片 {_chip_text(read)} 与 {_chip_text(partner)} 对应同一次过线；线上没被挡，画面只看到一次",
            chip_time_ms=read.chip_ms, bibs=(read.bib, partner.bib),
            event_ids=(read.event_id, partner.event_id),
            status=status, status_note=note, priority=20,
            evidence=dict(_crossing_evidence(crossing, version), chip_count=2, video_count=1,
                          span_ms=abs(read.chip_ms - partner.chip_ms)),
            calibration_version=version,
        ))

    for read in chip_only:
        if read.read_id in resolved or read.read_id in claimed:
            continue
        expected_ms = expected[read.read_id]
        partners = [
            (crossing, chip) for crossing, chip, _residual in matched
            if abs(crossing.recorder_ms - expected_ms) <= TWO_CHIP_MS
        ]
        group = group_of.get(read.read_id)
        if partners and group is None:
            crossing, partner = min(partners, key=lambda item: abs(item[0].recorder_ms - expected_ms))
            if view_is_clear(expected_ms, expected_ms, (crossing,)):
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
            detail=f"芯片 {_chip_text(read)} 附近没有看到过线"
                   + (f"；{TWO_CHIP_MS} ms 内另有一次过线，可能是并排被并成一次，也可能一人两个芯片"
                      if partners else ""),
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
            chip_time_ms=chip_time, status=STATUS_OPEN,
            status_note="待人工确认",
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
    fragments_of: dict[str, list[dict]] = {}
    for fragment in outcome.fragments:
        fragments_of.setdefault(fragment["event_id"], []).append(fragment)
    unverified: set[str] = set()
    for crossing, read, residual in matched:
        if read.event_id in open_bibs:
            continue
        if crossing.in_obstruction and crowded(expected[read.read_id]):
            # Only motion inside a blocked, crowded stretch matches this chip;
            # no rider is seen, so it is checked with that period instead.
            unverified.add(read.read_id)
            continue
        entry = _auto_passed(read, crossing, int(residual), version)
        if read.event_id in fragments_of:
            entry["merged_fragments"] = fragments_of[read.event_id]
        outcome.auto_passed.append(entry)

    records = sorted([m[0] for m in matched] + [crossing for crossing, _verdict in rest]
                     + [value.crossing for value in near_candidates], key=lambda crossing: crossing.recorder_ms)
    bunches = _bunch_items(camera_index, groups, claimed, records, version)
    bunches += _video_cluster_items(camera_index, model, groups, records, version)
    race_periods = [
        (start, end) for start, end in periods if race_start_ms is None or end >= race_start_ms
    ]
    records = sorted([m[0] for m in matched] + list(video_only), key=lambda crossing: crossing.recorder_ms)
    crowds = _obstruction_items(
        camera_index, model, race_periods, matched, chip_only, version, unverified, records,
        manual_entries=manual_entries,
    )
    outcome.items = _group_members(outcome.items, bunches, crowds, expected)
    outcome.items += _no_video_items(camera_index, no_video)
    outcome.stats = {
        "candidates": len(candidates),
        "excluded": 0,
        "suggested_exclusions": len(excluded),
        "late_pairs": len(outcome.late_pairs),
        "duplicates": outcome.duplicates,
        "merged_fragments": len(outcome.fragments),
        "near_crossings": len(outcome.near_crossings),
        "standing": sum(len(spot["members"]) >= STANDING_MIN for spot in spots),
        "before_race": len(before_race),
        "paired_in_crowd": len(unverified),
        "chip_groups": sum(len(group.members) >= 2 for group in groups),
        "unclear_groups": sum(len(group.members) >= 2 and not group.reliable for group in groups),
    }
    return outcome


def _item_recorder_ms(item: ProblemItem, expected: Mapping[str, int]) -> Optional[int]:
    if "recorder_ms" in item.evidence:
        return int(item.evidence["recorder_ms"])
    if "expected_recorder_ms" in item.evidence:
        return int(item.evidence["expected_recorder_ms"])
    return None


BUNCH_CHILD_KINDS = {KIND_TWO_CHIPS_ONE_CROSSING, KIND_CHIP_NO_CROSSING, KIND_NO_CHIP_CROSSING, KIND_NEAR_CROSSING}
CROWD_CHILD_KINDS = {KIND_NO_CHIP_CROSSING, KIND_NEAR_CROSSING, KIND_STANDING}


def _group_members(items, bunches, crowds, expected) -> list[ProblemItem]:
    """Fold items inside a group or a crowded period into that group item.

    A group is counted as a whole (plan 7.4), and a crowded line period is
    checked as a whole (plan 7.1). An item about one of a group's chips belongs
    to that group; an item seen on the line belongs to the group whose chip is
    closest. Members stay in the list with a link to their group, so nothing
    is dropped or excluded.
    """

    owner_of = {event_id: bunch for bunch in bunches for event_id in bunch.event_ids}
    windows = [(bunch, bunch.evidence["recorder_start_ms"], bunch.evidence["recorder_end_ms"]) for bunch in bunches]
    crowd_windows = [(crowd, crowd.evidence["recorder_start_ms"] - 5_000, crowd.evidence["recorder_end_ms"] + 5_000)
                     for crowd in crowds]
    members: dict[str, list[str]] = {}
    result = []
    for item in items:
        group = None
        position = _item_recorder_ms(item, expected)
        if item.pending and item.kind in BUNCH_CHILD_KINDS:
            # An item about chips belongs to the group of those chips, or to
            # none: a neighbouring rider is checked separately.
            group = next((owner_of[event_id] for event_id in item.event_ids if event_id in owner_of), None)
            if group is None and not item.event_ids and position is not None:
                inside = [bunch for bunch, start, end in windows if start <= position <= end]
                group = min(inside, default=None, key=lambda bunch: min(
                    abs(value - position) for value in bunch.evidence["member_recorder_ms"]))
        if group is None and item.pending and item.kind in CROWD_CHILD_KINDS and position is not None:
            if item.kind != KIND_NO_CHIP_CROSSING or item.status == STATUS_DISPUTED:
                # a likely rider in a crowd stays a separate item
                group = next((crowd for crowd, start, end in crowd_windows if start <= position <= end), None)
        if group is None:
            result.append(item)
            continue
        members.setdefault(group.item_id, []).append(item.item_id)
        result.append(replace(item, evidence=dict(item.evidence, group=group.item_id)))
    for candidate in [*bunches, *crowds]:
        ids = members.get(candidate.item_id, [])
        if ids:
            candidate = replace(
                candidate,
                detail=candidate.detail + f"；组内另有 {len(ids)} 条待核对问题",
                evidence=dict(candidate.evidence, members=ids),
            )
        result.append(candidate)
    return result


def _evidence_rank(member: tuple[CrossingRecord, Optional[BikeVerdict]]) -> tuple[int, float, int]:
    crossing, verdict = member
    if verdict is not None and verdict.label == LABEL_BICYCLE:
        rank = 0
    elif verdict is None or verdict.decision != DECISION_DISPUTED:
        rank = 1
    else:
        rank = 2
    return rank, -(verdict.score if verdict is not None else 0.0), crossing.recorder_ms


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
        # Recorder time of the crossing, so a later pass can tell whether this
        # match sits inside a period that still needs a whole-group check.
        "recorder_ms": int(crossing.recorder_ms),
        "contact": list(crossing.contact),
    }


def merge_obstructions(obstructions: Sequence[Obstruction]) -> list[tuple[int, int]]:
    """Crowded line periods: blockages less than 30 s apart, at least 10 s in total."""

    periods: list[list[int]] = []
    for item in sorted(obstructions, key=lambda value: value.start_ms):
        if periods and item.start_ms - periods[-1][1] <= OBSTRUCTION_MERGE_MS:
            periods[-1][1] = max(periods[-1][1], item.end_ms)
            periods[-1][2] += item.end_ms - item.start_ms
        else:
            periods.append([item.start_ms, item.end_ms, item.end_ms - item.start_ms])
    return [(start, end) for start, end, total in periods if total >= OBSTRUCTION_MIN_MS]


def _chip_groups(automatic, manual_entries, expected, model) -> list[_ChipGroup]:
    """Active chips grouped by 1 s from each group's first chip, never chained.

    A manual entry is a rider too: the group count must include the one the
    chip system missed and an operator entered later, or the operator is told
    to look for one more rider than actually exists. It never replaces an
    automatic read of the same bib in the same group.
    """

    people = [(read, False, int(expected[read.read_id])) for read in automatic if read.active_now]
    people += [(read, True, int(model.to_recorder(read.chip_ms)))
               for read in manual_entries if read.active_now and not read.excluded]
    groups: list[_ChipGroup] = []
    for read, is_manual, recorder_ms in sorted(people, key=lambda person: (person[0].chip_ms, person[1])):
        if not groups or read.chip_ms > groups[-1].members[0][0].chip_ms + GROUP_SPAN_MS:
            groups.append(_ChipGroup())
        groups[-1].members.append((read, is_manual))
        groups[-1].expected.append(recorder_ms)
    for group in groups:
        automatic_bibs = {read.bib for read, is_manual in group.members if not is_manual}
        kept = [(member, value) for member, value in zip(group.members, group.expected)
                if not (member[1] and member[0].bib in automatic_bibs)]
        group.members = [member for member, _value in kept]
        group.expected = [value for _member, value in kept]
    return groups


def _assign_group_events(groups, crossing_of, loose) -> None:
    """Each group's crossings: its own chips' matches, then nearby unpaired ones.

    An unpaired crossing counts for one group only, the one with the closest
    member, so two neighbouring groups never both count it.
    """

    multi = [group for group in groups if len(group.members) >= 2]
    for group in multi:
        for read, is_manual in group.members:
            crossing = None if is_manual else crossing_of.get(read.read_id)
            if crossing is not None:
                group.events[crossing.key] = crossing
    for crossing in loose:
        inside = [group for group in multi
                  if group.start - GROUP_VIDEO_MS <= crossing.recorder_ms <= group.end + GROUP_VIDEO_MS]
        if inside:
            nearest = min(inside, key=lambda group: min(abs(value - crossing.recorder_ms) for value in group.expected))
            nearest.events.setdefault(crossing.key, crossing)


def _bunch_items(camera_index, groups, claimed, records, version) -> list[ProblemItem]:
    """One row per group of two or more riders, counted by the operator as a whole.

    Equal counts still ask for a look: two riders side by side are one change
    on the line, and an extra chip can hide a missed read. Where the line was
    blocked or other riders are close, the video count is not used as a hint.
    """

    items = []
    for index, group in enumerate(groups):
        reads = group.reads
        if len(reads) < 2 or any(read.read_id in claimed for read in reads):
            continue
        chips, video = len(reads), len(group.events)
        first, last = reads[0], reads[-1]
        start, end = group.start - BUNCH_MARGIN_MS, group.end + BUNCH_MARGIN_MS
        detail = f"{format_chip_time(first.chip_ms)} 起 {chips} 条芯片相隔 1 秒以内：自动计数 {video} 次"
        if video == chips:
            detail += "，与芯片一样多，仍请看一眼有没有并排或被挡的人"
        elif video > chips:
            detail += f"（不一致）；画面多 {video - chips} 次，可能有漏读，请整团点数"
        elif group.reliable:
            detail += f"（不一致）；画面少 {chips - video} 次，可能并排或一人多芯片，请整团点数"
        else:
            detail += "（不一致）；附近过线密集或线被挡，画面计数不可信，请整团点数"
        manual_reads = group.manual
        if manual_reads:
            detail += f"；含补录 {'、'.join(read.bib for read in manual_reads)}，请核对补录是否对应其中一次过线"
        before = groups[index - 1].reads[-1].chip_ms if index > 0 else None
        after = groups[index + 1].reads[0].chip_ms if index + 1 < len(groups) else None
        if ((before is not None and first.chip_ms - before <= GROUP_SPAN_MS)
                or (after is not None and after - last.chip_ms <= GROUP_SPAN_MS)):
            detail += "；前后 1 秒内还有别的芯片，视频连着看、分开计数"
        evidence = {"camera_index": camera_index, "recorder_start_ms": int(start), "recorder_end_ms": int(end),
                    "video_count": video, "chip_count": chips, "source": "chip",
                    "span_ms": int(last.chip_ms - first.chip_ms), "video_reliable": group.reliable,
                    "member_recorder_ms": sorted(int(value) for value in group.expected)}
        if manual_reads:
            evidence["manual_bibs"] = [read.bib for read in manual_reads]
            evidence["manual_event_ids"] = [read.event_id for read in manual_reads]
        events = sorted(group.events.values(), key=lambda crossing: crossing.recorder_ms)
        evidence.update(_period_anchor(events, start, end, version) or _period_anchor(records, start, end, version))
        items.append(ProblemItem(
            item_id=stable_item_id(KIND_BUNCH, camera_index, *(read.read_id for read in reads)),
            kind=KIND_BUNCH, category=CATEGORY_VIDEO, detail=detail,
            chip_time_ms=first.chip_ms, bibs=tuple(read.bib for read in reads),
            event_ids=tuple(read.event_id for read in reads), status=STATUS_OPEN,
            priority=55 if video == chips else 50, evidence=evidence, calibration_version=version,
        ))
    return items


def _video_cluster_items(camera_index, model, groups, records, version) -> list[ProblemItem]:
    """The other direction: three or more crossings within 1 s and fewer chips.

    Riders whose chips are each on their own are not a chip group, and a missed
    read among them must not wait for a second chip (plan step 5).
    """

    windows = [(group.start - BUNCH_MARGIN_MS, group.end + BUNCH_MARGIN_MS) for group in groups if len(group.members) >= 2]
    people = [(read, value) for group in groups for read, value in zip(group.reads, group.expected)]
    clusters: list[list[CrossingRecord]] = []
    for record in records:
        if clusters and record.recorder_ms <= clusters[-1][0].recorder_ms + GROUP_SPAN_MS:
            clusters[-1].append(record)
        else:
            clusters.append([record])
    items = []
    for cluster in clusters:
        if len(cluster) < SMALL_GROUP_MIN:
            continue
        start, end = cluster[0].recorder_ms - BUNCH_MARGIN_MS, cluster[-1].recorder_ms + BUNCH_MARGIN_MS
        if any(not (end < left or start > right) for left, right in windows):
            continue  # already covered by the chip-side group
        window_reads = [read for read, value in people if start <= value <= end]
        if not window_reads or len(cluster) <= len(window_reads):
            continue
        first = cluster[0]
        evidence = {"camera_index": camera_index, "recorder_start_ms": int(start), "recorder_end_ms": int(end),
                    "video_count": len(cluster), "chip_count": len(window_reads), "source": "video",
                    "member_recorder_ms": [int(record.recorder_ms) for record in cluster]}
        evidence.update(_period_anchor(records, start, end, version))
        items.append(ProblemItem(
            item_id=stable_item_id(KIND_BUNCH, camera_index, *(record.key for record in cluster)),
            kind=KIND_BUNCH, category=CATEGORY_VIDEO,
            detail=(
                f"{format_chip_time(model.to_chip(first.recorder_ms, first.session_id))} 起 1 秒内自动计数 "
                f"{len(cluster)} 次过线，只对上 {len(window_reads)} 条芯片（"
                + "、".join(read.bib for read in window_reads[:20])
                + ("……" if len(window_reads) > 20 else "")
                + "）：小组人数不能只按芯片判断，请整团点数"
            ),
            chip_time_ms=model.to_chip(first.recorder_ms, first.session_id),
            bibs=tuple(read.bib for read in window_reads), event_ids=tuple(read.event_id for read in window_reads),
            status=STATUS_OPEN, priority=48, evidence=evidence, calibration_version=version,
        ))
    return items


def _period_anchor(records: Sequence[CrossingRecord], start: int, end: int, version: int) -> dict:
    """Source frame of the first crossing in a bunch or crowded period, for its card and jump."""

    first = next((record for record in records if start <= record.recorder_ms <= end), None)
    return {"frame": first.frame_payload(version), "recorder_ms": first.recorder_ms} if first is not None else {}


def _obstruction_items(
    camera_index, model, periods, matched, chip_only, version, unverified=frozenset(), records=(),
    manual_entries=(),
) -> list[ProblemItem]:
    items = []
    reads = [m[1] for m in matched] + list(chip_only) + [
        read for read in manual_entries if read.active_now and not read.excluded
    ]
    for start, end in periods:
        inside = [read for read in reads if start - 5_000 <= model.to_recorder(read.chip_ms) <= end + 5_000]
        weak = [read.bib for read in inside if read.read_id in unverified]
        detail = (
            f"{format_chip_time(model.to_chip(start))}–{format_chip_time(model.to_chip(end))} 终点线被挡或拥挤"
            f"（{(end - start) / 1000:.0f} 秒），期间芯片 {len(inside)} 个"
        )
        if weak:
            detail += f"，其中 {len(weak)} 个只对上遮挡中的晃动、未自动通过：" + "、".join(weak[:30])
            detail += "……" if len(weak) > 30 else ""
        items.append(ProblemItem(
            item_id=stable_item_id(KIND_OBSTRUCTION, camera_index, start),
            kind=KIND_OBSTRUCTION, category=CATEGORY_VIDEO,
            detail=detail + "；证据不足，请核对该时段",
            chip_time_ms=model.to_chip(start), bibs=tuple(read.bib for read in inside),
            event_ids=tuple(read.event_id for read in inside), status=STATUS_OPEN, priority=60,
            evidence={
                "camera_index": camera_index, "recorder_start_ms": int(start), "recorder_end_ms": int(end), "unverified": weak,
                **_period_anchor(records, start, end, version),
            },
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
    "NearCrossing",
    "Obstruction",
    "RuleOutcome",
    "build_video_items",
    "merge_obstructions",
    "same_passage",
]
