"""Manual bunch counts, provisional alignment and durable review drafts.

Only a plate read by the operator identifies a rider. Untyped counts are
aligned in time order as a reference; no CycleRace record is changed here.
"""

from __future__ import annotations

import hashlib
import json
import statistics
from dataclasses import asdict, dataclass, field
from functools import cached_property
from pathlib import Path

import numpy as np

from .analysis_workspace import AnalysisWorkspace
from .clock_sync import CLOCK_MODEL_FILENAME, ClockModel, ClockModelStore
from .durable_jsonl import append_jsonl_records
from .problem_items import (
    ACTION_DELETE_CHIP,
    ACTION_DELETE_EXTRA_CHIP,
    ACTION_INSUFFICIENT,
    ACTION_KEEP,
    ACTION_NOT_RIDER,
    ACTION_RECORD_BIB,
    ACTION_RIDE_BACK,
    ACTION_SAME_CROSSING,
    ACTION_SIDE_BY_SIDE,
    CATEGORY_VIDEO,
    KIND_CHIP_NO_CROSSING,
    KIND_NEAR_CROSSING,
    KIND_NO_CHIP_CROSSING,
    KIND_TWO_CHIPS_ONE_CROSSING,
    STATUS_CLOSED_UPSTREAM,
    STATUS_EXCLUDED,
    STATUS_INFO,
    STATUS_OPEN,
    ProblemItem,
    stable_item_id,
)


JOURNAL_NAME = "bunch_reviews.jsonl"
PROBLEM_LIST_NAME = "finish_problems/problem_list.json"
CROSSINGS_NAME = "finish_problems/crossings_camera{}.json"

# The rule a saved completion was confirmed under. Version 2: the whole period
# from before the first to after the last crosser was on screen and the operator
# confirmed it; equal totals alone no longer close a group. A completion saved
# under an older rule is reopened in memory only — the journal keeps it as saved.
COMPLETION_POLICY_VERSION = 2

# A counted person is on the frame when the camera and the moment agree; a
# retained copy of the same actual frame (extra metadata, a rebuilt stamp) also
# counts. Neighboring frames are 50 ms apart, so the moment stays unambiguous.
MARK_TIME_TOLERANCE_MS = 20

# The doubts a group's children can carry, and the operator's own answers to
# them. Only the answers below close one: equal totals never can, and "confirm
# the crossing, number later" leaves the question open on purpose.
CHILD_ANSWERS = {
    KIND_TWO_CHIPS_ONE_CROSSING: (ACTION_DELETE_EXTRA_CHIP, ACTION_SIDE_BY_SIDE),
    KIND_CHIP_NO_CROSSING: (ACTION_DELETE_CHIP, ACTION_KEEP, ACTION_INSUFFICIENT),
    KIND_NO_CHIP_CROSSING: (ACTION_RECORD_BIB, ACTION_NOT_RIDER, ACTION_RIDE_BACK),
    KIND_NEAR_CROSSING: (ACTION_RECORD_BIB, ACTION_SAME_CROSSING, ACTION_NOT_RIDER, ACTION_RIDE_BACK,
                         ACTION_INSUFFICIENT),
}
RESOLVED_STATUSES = (STATUS_CLOSED_UPSTREAM, STATUS_EXCLUDED, STATUS_INFO)

# One parse per version of the problem list: every confirmed group asks for its
# children on every refresh, and the list of a whole race is large.
_CHILDREN: dict[tuple, dict[str, list[dict]]] = {}


def load_child_items(workspace: Path, group_id: str) -> list[dict]:
    """The group's current children, straight from the problem list.

    A draft or a saved record keeps its own signature, so the child list is
    read when a decision needs it: a child added, answered or re-analysed later
    must never be hidden by an earlier copy of the context. The parsed list is
    reused only while the file is unchanged; callers must not modify it.
    """

    path = Path(workspace) / PROBLEM_LIST_NAME
    try:
        stamp = (str(path), path.stat().st_mtime_ns, path.stat().st_size)
    except OSError:
        return []
    if stamp not in _CHILDREN:
        payload = json.loads(path.read_text(encoding="utf-8"))
        groups: dict[str, list[dict]] = {}
        for child in payload.get("items", []):
            group = child.get("evidence", {}).get("group")
            if group:
                groups.setdefault(group, []).append(child)
        _CHILDREN.clear()
        _CHILDREN[stamp] = groups
    return list(_CHILDREN[stamp].get(group_id, ()))


# The detection run writes one file per camera; it is the only place that keeps
# every person seen on the line, so both reviews read it back. Parsing is worth
# a cache: the file holds a whole session and every opened problem asks again.
_DETECTIONS: dict[tuple, list[dict] | None] = {}


def detected_crossings(workspace: Path, camera: int) -> list[dict] | None:
    """Every rider-like crossing the analysis detected on this camera's line.

    ``None`` means this workspace has no detection output at all (a group that
    was never analysed, or a file that can no longer be read) — different from
    an empty list, which is a line where nobody came over.
    """

    path = Path(workspace) / CROSSINGS_NAME.format(int(camera))
    try:
        stamp = (str(path), path.stat().st_mtime_ns, path.stat().st_size)
    except OSError:
        return None
    if stamp not in _DETECTIONS:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            crossings: list[dict] | None = [entry for entry in payload["crossings"] if isinstance(entry, dict)]
        except (OSError, ValueError, TypeError, KeyError):
            crossings = None
        if len(_DETECTIONS) > 4:
            _DETECTIONS.clear()
        _DETECTIONS[stamp] = crossings
    return _DETECTIONS[stamp]


@dataclass(frozen=True)
class BunchChip:
    event_id: str
    bib: str
    chip_ms: int
    recorder_ms: int
    revision: int = 0
    chip_id: str = ""


@dataclass
class BunchContext:
    workspace: Path
    item: ProblemItem
    camera: int
    model: ClockModel
    chips: list[BunchChip]
    segments: tuple
    start_ms: int
    end_ms: int
    line: dict
    lines: dict = field(default_factory=dict)
    gaps: list = field(default_factory=list)
    settings_token: str = ""

    @cached_property
    def child_items(self):
        """Navigation hints for this context; decisions read the list fresh."""

        return load_child_items(self.workspace, self.item.item_id)

    @property
    def detected(self) -> list[dict]:
        """The people detection found inside this group's own period.

        Detection is the only record of everyone the camera saw on the line:
        the chips only account for the riders wearing one, and the operator's
        counts only for the ones they clicked. Each entry keeps the moment and
        whether the view was blocked when it happened. Read fresh — a running
        analysis may be writing this file while the list is already open.
        """

        people = []
        for entry in detected_crossings(self.workspace, self.camera) or []:
            try:
                recorder_ms = int(entry["recorder_ms"])
            except (KeyError, TypeError, ValueError):
                continue
            if self.start_ms <= recorder_ms <= self.end_ms:
                people.append({"recorder_ms": recorder_ms,
                               "in_obstruction": bool(entry.get("in_obstruction"))})
        people.sort(key=lambda entry: entry["recorder_ms"])
        return people

    @property
    def signature(self) -> str:
        payload = {
            "item": self.item.to_payload(), "camera": self.camera,
            "model": self.model.to_payload(), "chips": [asdict(chip) for chip in self.chips],
            "segments": [(s.segment.segment_id, str(s.path), s.start_ms, s.end_ms) for s in self.segments],
            "line": self.line,
            "lines": self.lines, "gaps": self.gaps, "settings_token": self.settings_token,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def load_context(workspace: Path, item: ProblemItem, camera: int | None = None) -> BunchContext:
    from .problem_analysis import config_from_workspace, line_for_segment

    workspace = Path(workspace)
    if item.evidence.get("configuration_stale") or item.evidence.get("superseded"):
        raise ValueError("本团证据已变化，请重新分析后再核对。")
    source = AnalysisWorkspace(workspace, include_review_segments=True)
    frame = item.evidence.get("frame") or {}
    clocks = ClockModelStore(workspace / CLOCK_MODEL_FILENAME)
    primary = frame.get("camera_index") or item.evidence.get("camera_index")
    if camera is None and primary is None:
        candidates = [index for index in source.camera_indexes() if clocks.get(index, item.calibration_version)]
        if len(candidates) != 1:
            raise ValueError("旧集团缺少机位证据，无法唯一确定原机位；请重新分析生成集团入口。")
        primary = candidates[0]
    camera = int(camera or primary)
    model = (clocks.get(camera, item.calibration_version) if primary is None or camera == int(primary)
             else clocks.latest(camera))
    if model is None:
        raise ValueError(f"机位 {camera} 缺少本条问题对应的校时版本，请先校时或重新分析。")
    archives = source.archive_segments(camera)
    chips = []
    for event_id, bib in zip(item.event_ids, item.bibs):
        candidates = [read for read in source.reads if read.event_id == event_id and read.bib == bib]
        if not candidates:
            raise ValueError(f"芯片 {bib} 的记录已变化，请重新分析问题清单。")
        read = min(candidates, key=lambda r: abs(r.chip_ms - (item.chip_time_ms or r.chip_ms)))
        if not read.active_now or read.changed_upstream:
            raise ValueError(f"芯片 {bib} 已在上游修改或作废，请刷新问题清单。")
        sessions = [segment for segment in archives
                    if segment.start_ms <= model.to_recorder(read.chip_ms, segment.session_id) <= segment.end_ms]
        session = next((segment.session_id for segment in sessions if segment.session_id == frame.get('session_id')),
                       sessions[0].session_id if sessions else '')
        latest = source.latest_event(event_id)
        if latest is not None and (latest.lap != read.lap or latest.bib != bib):
            raise ValueError(f"芯片 {bib} 的身份或圈次已变化，请重新分析。")
        chips.append(BunchChip(event_id, bib, read.chip_ms, int(model.to_recorder(read.chip_ms, session)),
                               latest.revision if latest else read.latest_revision, read.chip_id))
    chips.sort(key=lambda c: c.chip_ms)
    if not chips:
        raise ValueError("本团没有可核对的芯片记录。")
    start, end = chips[0].recorder_ms - 1500, chips[-1].recorder_ms + 1500
    if primary is not None and camera == int(primary):
        start = min(start, int(item.evidence.get("recorder_start_ms", start)))
        end = max(end, int(item.evidence.get("recorder_end_ms", end)))
    segments = tuple(s for s in archives if s.start_ms <= end and s.end_ms >= start)
    if not segments:
        raise ValueError(f"机位 {camera} 的本团原录像缺失；未打开其他时段录像。")
    config = config_from_workspace(workspace)
    anchor = next((segment for segment in segments if segment.segment.segment_id == frame.get("segment_id")), segments[0])
    line = dict((frame.get("line_config") if camera == frame.get("camera_index") else None)
                or line_for_segment(config, source, anchor) or {})
    lines = {s.segment.segment_id: dict(line_for_segment(config, source, s) or {}) for s in segments}
    if frame.get("line_config") and camera == frame.get("camera_index"):
        lines[anchor.segment.segment_id] = dict(frame["line_config"])
    gaps = uncovered_intervals(start, end, [(s.start_ms, s.end_ms) for s in segments], tolerance=100)
    return BunchContext(workspace, item, camera, model, chips, segments, start, end, line,
                        lines, gaps, config.settings_token)


def uncovered_intervals(start, end, ranges, tolerance=0):
    cursor, gaps = start, []
    for left, right in sorted(ranges):
        if right < cursor or left > end:
            continue
        if left > cursor + tolerance:
            gaps.append([cursor, min(left, end)])
        cursor = max(cursor, min(right, end))
    if cursor < end - tolerance:
        gaps.append([cursor, end])
    return gaps


def add_viewed(ranges, timestamp, start, end, reach=50):
    """Only decoded frames contribute; seeking across a gap never fills it.

    A frame covers ``reach`` either side of it: a group is looked at frame by
    frame, an obstruction period may be swept through quickly (``view_reach``).
    """
    merged = []
    for left, right in sorted([*ranges, [max(start, timestamp-reach), min(end, timestamp+reach)]]):
        if right < left:
            continue
        if merged and left <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], right)
        else:
            merged.append([left, right])
    return merged


def view_reach(item) -> int:
    """How far either side of one decoded frame counts as seen.

    A group of riders is followed frame by frame (50 ms covers a 20 fps frame).
    A blocked or crowded period can last minutes; the operator decided it may
    be swept through quickly and confirmed by hand, so one frame every half
    second while dragging is enough there.
    """
    return 250 if item.kind == "obstruction" else 50


def review_candidates(context, marks, result):
    """Explainable navigation hints, never a claim to have found every rider."""
    candidates = {}
    def add(key, start, end, reason):
        start, end = max(context.start_ms, int(start)), min(context.end_ms, int(end))
        if start <= end:
            candidates[str(key)] = {"id": str(key), "start_ms": start, "end_ms": end, "reason": reason}
    item = context.item
    if item.kind == "obstruction":
        add("obstruction", context.start_ms, context.end_ms, "遮挡 / 拥挤：仍须检查完整时段")
    count = item.evidence.get("video_count")
    if count is not None and count != len(context.chips):
        add("count_difference", context.start_ms, context.end_ms, f"自动候选 {count} / 芯片 {len(context.chips)}：数量差仅定位本团")
    for child in context.child_items:
        evidence = child.get("evidence", {})
        child_frame = evidence.get("frame") or {}
        if child_frame.get("camera_index", context.camera) != context.camera:
            continue
        timestamp = evidence.get("recorder_ms", evidence.get("expected_recorder_ms"))
        if timestamp is not None:
            add(child["item_id"], timestamp-250, timestamp+250, child.get("title", child["kind"]))
    for a, b in zip(context.chips, context.chips[1:]):
        if b.recorder_ms-a.recorder_ms <= 150:
            add("dense:"+a.event_id, a.recorder_ms-250, b.recorder_ms+250, "密集 / 可能并排：时间刻度不代表身份")
    if marks:
        for extra in result["extras"]:
            index = extra["mark"]
            timestamp = marks[index]["recorder_ms"]
            add(f"extra:{index}", timestamp-150, timestamp+150, "人工计数未对应芯片；号码可暂留空")
        for index in result["missing"]:
            chip = context.chips[index]
            add("chip:"+chip.event_id, chip.recorder_ms-250, chip.recorder_ms+250, "芯片尚无人工计数对应："+chip.bib)
    for index, (left, right) in enumerate(context.gaps):
        add(f"gap:{index}", left, right, "原录像缺口：不能确认完整团段")
    return sorted(candidates.values(), key=lambda c: (c["start_ms"], c["end_ms"], c["id"]))


def group_member_batch(context):
    """Use the same member decision contract for extra chips within a group."""
    from .batch_review import read_snapshots
    from .problem_items import KIND_BATCH_READS
    from dataclasses import replace
    source = AnalysisWorkspace(context.workspace)
    snapshots = read_snapshots(source.reads, source.metadata(), source.latest_event)
    members = [snapshots[c.event_id] for c in context.chips if c.event_id in snapshots]
    return replace(context.item, kind=KIND_BATCH_READS,
                   evidence=dict(context.item.evidence, batch_scope=[source.race_id, source.stage_id],
                                 batch_members=members, analysis_signature=context.signature))


def pending_member_decisions(context):
    from .batch_review import BatchDecisionStore
    batch = group_member_batch(context)
    decisions = BatchDecisionStore(context.workspace / "finish_problems")
    return any(decisions.state(batch, member) in ("待 CycleRace", "需要重查", "暂存")
               for member in batch.evidence["batch_members"])


def counted_person_on_child_frame(child, marks) -> bool:
    """A person was counted on the child's own frame; the crossing is accounted.

    The number may stay unread: one counted person at that moment is the
    operator's answer that the line change was a rider, not a total that happens
    to match. The frame comparison tolerates the extra metadata and retained
    copies that saved marks carry.
    """

    frame = child.evidence.get("frame") or {}
    stamp = child.evidence.get("recorder_ms")
    if stamp is None:
        stamp = child.evidence.get("expected_recorder_ms")
    if stamp is None:
        return False
    return any(not is_excluded(mark) and mark_on_frame(mark, frame, int(stamp)) for mark in marks)


def chip_answered_by_count(context, child, marks, result) -> bool:
    """The chip's own number was read off a counted person in this group.

    Only the number read on the rider himself answers "chip without a crossing":
    a time-order match to a bibbless count is a reference, not an identification,
    so it never stands in for the answer.
    """

    positions = {chip.event_id: index for index, chip in enumerate(context.chips)}
    target = next((positions[event_id] for event_id in child.event_ids if event_id in positions), None)
    if target is None:
        return False
    for mark_index, chip_index in result["pairs"].items():
        if chip_index != target or mark_index >= len(marks):
            continue
        mark = marks[mark_index]
        if not is_excluded(mark) and mark.get("bib", "").strip():
            return True
    return False


def group_member_states(context) -> dict[str, str]:
    """The member flow's verified state for each chip record of this group.

    The per-chip keep/delete decisions live in the batch journal, bound to the
    group's own evidence; a changed record reads as "需要重查" and never answers.
    """

    from .batch_review import BatchDecisionStore

    try:
        batch = group_member_batch(context)
        store = BatchDecisionStore(context.workspace / "finish_problems")
        return {member["event_id"]: store.state(batch, member) for member in batch.evidence["batch_members"]}
    except (KeyError, OSError, ValueError):
        return {}


def chip_answered_by_members(child, states) -> bool:
    """The operator's own per-chip answers for the chips this doubt is about.

    A chip kept verified or already suggested for deletion answers the doubt; a
    hold, an undecided record or a re-check state does not, and a decision whose
    binding no longer matches the record never stands in for the answer.
    """

    answers = {states.get(event_id) for event_id in child.event_ids}
    if child.kind == KIND_TWO_CHIPS_ONE_CROSSING:
        # One chip of the pair being deleted settles the crossing; two separate
        # riders need both records kept, nothing looser.
        return "待 CycleRace" in answers or answers == {"已确认有效"}
    return not answers.isdisjoint({"已确认有效", "待 CycleRace"})


def unresolved_crossing_conflicts(context, marks, result):
    """Explicit child doubts cannot disappear just because the totals happen to match.

    A double chip on one crossing is answered on the child itself or per member
    chip, never by the count: two physical chips are not two wrong records by
    themselves. A chip whose crossing was not seen needs its number read on a
    counted person, or the chip kept/deleted in the member flow, and a crossing
    with no chip is answered by a counted person on its own frame. Children are
    read from the current problem list, so a conflict that appeared after the
    draft was opened still blocks.
    """

    from .problem_resolutions import ProblemResolutionStore

    store = ProblemResolutionStore(context.workspace / "finish_problems/problem_resolutions.jsonl")
    member_states = None
    conflicts = []
    for value in load_child_items(context.workspace, context.item.item_id):
        child = ProblemItem.from_payload(value)
        answers = CHILD_ANSWERS.get(child.kind)
        if answers is None or child.status in RESOLVED_STATUSES:
            continue
        decision, stale = store.state(child)
        if decision is not None and not stale and decision.action in answers:
            continue
        if child.kind in (KIND_CHIP_NO_CROSSING, KIND_TWO_CHIPS_ONE_CROSSING):
            if child.kind == KIND_CHIP_NO_CROSSING and chip_answered_by_count(context, child, marks, result):
                continue
            if member_states is None:
                member_states = group_member_states(context)
            if member_states and chip_answered_by_members(child, member_states):
                continue
        elif counted_person_on_child_frame(child, marks):
            continue
        conflicts.append(child)
    return conflicts


def unanswered_differences(context, rows) -> list[ProblemItem]:
    """The differences of a confirmed count that nobody has answered yet.

    Only the operator's own answer on the row closes one — for a chip, a keep
    or delete given in the member flow too. The count that produced the row
    never answers it: the counted person is on that very frame.
    """

    from .problem_resolutions import ProblemResolutionStore

    store = ProblemResolutionStore(context.workspace / "finish_problems/problem_resolutions.jsonl")
    member_states = None
    unanswered = []
    for row in rows:
        decision, stale = store.state(row)
        if decision is not None and not stale and decision.action in CHILD_ANSWERS[row.kind]:
            continue
        if row.kind == KIND_CHIP_NO_CROSSING:
            if member_states is None:
                member_states = group_member_states(context)
            if member_states and chip_answered_by_members(row, member_states):
                continue
        unanswered.append(row)
    return unanswered


def is_excluded(mark: dict) -> bool:
    """Old drafts are rider counts; an explicit reason records manual exclusion."""
    return bool(mark.get("exclusion_reason", "").strip())


def mark_on_frame(mark, frame, recorder_ms):
    """A counted person belongs to one decoded frame, including rebound archives.

    The stored reference is a retained copy: it may carry extra metadata, and
    only the camera, the moment and the actual frame identify it. Two references
    are the same frame when the camera and the moment agree, or when they name
    the same decoded frame exactly — a rebuilt stamp on one copy cannot detach
    the mark from the frame it was counted on.
    """

    ref = mark.get("frame") or {}
    if (ref.get("camera_index") is not None and frame.get("camera_index") is not None
            and int(ref["camera_index"]) != int(frame["camera_index"])):
        return False
    left, right = ref.get("frame_index"), frame.get("frame_index")
    if (ref.get("segment_id") and ref.get("segment_id") == frame.get("segment_id")
            and left is not None and right is not None and int(left) == int(right)):
        return True
    stamped = mark.get("recorder_ms")
    if stamped is None or recorder_ms is None:
        return False
    return abs(int(stamped) - int(recorder_ms)) <= MARK_TIME_TOLERANCE_MS


def rider_count(marks):
    return sum(not is_excluded(mark) for mark in marks)


def align_counts(chips: list[BunchChip], marks: list[dict], fallback_bias_ms: int = 0) -> dict:
    """Prototype's plate-first / ordered alignment, with explicit uncertainty.

    Extra riders only get a suggested chip time when bracketed by two counted
    riders. At the ends of a bunch there is no invented extrapolation.
    """
    # Keep journal indices: UI and exported evidence refer to the original marks.
    order = sorted((i for i, mark in enumerate(marks) if not is_excluded(mark)),
                   key=lambda i: marks[i]["recorder_ms"])
    pairs, used_chips = {}, set()
    for i in order:
        bib = marks[i].get("bib", "").strip()
        same = [q for q, chip in enumerate(chips) if bib and chip.bib == bib and q not in used_chips]
        if same:
            q = min(same, key=lambda q: abs(chips[q].recorder_ms - marks[i]["recorder_ms"]))
            pairs[i] = q
            used_chips.add(q)
    lone = []
    for i in order:
        t = marks[i]["recorder_ms"]
        nearby = [c for c in chips if abs(c.recorder_ms - t) <= 300]
        if len(nearby) == 1 and not any(j != i and abs(marks[j]["recorder_ms"] - t) <= 300 for j in order):
            lone.append(t - nearby[0].recorder_ms)
    bias = round(statistics.median(lone)) if len(lone) >= 3 else fallback_bias_ms
    remaining_marks = [i for i in order if i not in pairs and not marks[i].get("bib", "").strip()]
    remaining_chips = [q for q in range(len(chips)) if q not in used_chips]
    n, k = len(remaining_marks), len(remaining_chips)
    costs = np.full((n + 1, k + 1), np.inf)
    costs[0, :] = np.arange(k + 1)
    costs[:, 0] = np.arange(n + 1)

    def cost(i, j):
        distance = abs(marks[remaining_marks[i]]["recorder_ms"] - bias - chips[remaining_chips[j]].recorder_ms)
        return (distance / 150.0) ** 2 if distance <= 400 else np.inf

    for i in range(1, n + 1):
        for j in range(1, k + 1):
            costs[i, j] = min(costs[i - 1, j] + 1, costs[i, j - 1] + 1, costs[i - 1, j - 1] + cost(i - 1, j - 1))
    i, j = n, k
    while i > 0 and j > 0:
        if np.isclose(costs[i, j], costs[i - 1, j - 1] + cost(i - 1, j - 1)):
            pairs[remaining_marks[i - 1]] = remaining_chips[j - 1]
            i, j = i - 1, j - 1
        elif np.isclose(costs[i, j], costs[i - 1, j] + 1):
            i -= 1
        else:
            j -= 1
    used_chips = set(pairs.values())
    extras = []
    for i in order:
        if i in pairs:
            continue
        t = marks[i]["recorder_ms"]
        before = [(marks[p]["recorder_ms"], chips[q].chip_ms) for p, q in pairs.items() if marks[p]["recorder_ms"] <= t]
        after = [(marks[p]["recorder_ms"], chips[q].chip_ms) for p, q in pairs.items() if marks[p]["recorder_ms"] > t]
        chip_time = None
        bracket_ms = None
        if before and after:
            va, ca = max(before)
            vb, cb = min(after)
            if cb >= ca:
                chip_time = round(ca + (t - va) / (vb - va) * (cb - ca))
                bracket_ms = int(cb - ca)
        extras.append({"mark": i, "bib": marks[i].get("bib", ""), "chip_time_ms": chip_time,
                       "bracket_ms": bracket_ms})
    ambiguous = [i for i, q in pairs.items() if not marks[i].get("bib") and any(
        p != q and abs(chip.recorder_ms - chips[q].recorder_ms) <= 150 for p, chip in enumerate(chips)
    )]
    return {"pairs": pairs, "extras": extras, "missing": [q for q in range(len(chips)) if q not in used_chips],
            "bias_ms": bias, "lone_count": len(lone), "ambiguous": ambiguous}


def completion_issue(chips: list[BunchChip], marks: list[dict], result: dict) -> str:
    """What the count itself still owes before the group can be confirmed.

    Only a count that cannot be read blocks: nobody counted, or one number on
    two people. A difference between the people and the chips never does —
    confirming says the whole group was seen, and every difference becomes a row
    of its own at its own time (``count_differences``). Whether the whole group
    was seen is ``review_completion_issue``'s question.
    """

    active = [mark for mark in marks if not is_excluded(mark)]
    if not active:
        return "请先在原画面点击每位压线者计数。"
    bibs = [m.get("bib", "").strip() for m in active if m.get("bib", "").strip()]
    if len(bibs) != len(set(bibs)):
        return "同一号码被填写在多人上，请先核对并修正。"
    return ""


def group_exports(extra: dict, counted: int, chip_count: int) -> bool:
    """A counted person the confirmed group itself hands on as a 补录.

    The number was read and two counted riders bracket the moment, so the chip
    time is interpolated between theirs. With equal totals nothing is exported:
    a number outside the group there may as well be a misread or a double chip.
    """

    return bool(extra["bib"].strip()) and extra["chip_time_ms"] is not None and counted != chip_count


def count_differences(item: ProblemItem, camera: int, chips: list[BunchChip], marks: list[dict], result: dict,
                      *, children=(), model: ClockModel | None = None, version: int = 0) -> list[ProblemItem]:
    """What a confirmed count left unsettled: one row per person or chip.

    A number read on a person but not among the group's chips, or one person
    more than there are chips, is a suspected missed read on that person's own
    frame — unless the group already exports them (``group_exports``). A chip
    no counted person stands for is a suspected double or spare chip, or a
    rider overlooked. How many rows there are follows from the totals and the
    numbers read, never from crossing times alone: a clock offset must not
    turn an equal count into differences. Which unread person or chip is the
    surplus is a guess from crossing times, and the row says so. A frame or a
    chip already carrying a doubt of its own is taken first, so one difference
    is asked once.
    """

    def read(index):
        return marks[index].get("bib", "").strip()

    active = [index for index, mark in enumerate(marks) if not is_excluded(mark)]
    unread = [index for index in active if not read(index)]
    by_number = {chip for index, chip in result["pairs"].items() if read(index)}
    free = [chip for chip in range(len(chips)) if chip not in by_number]
    bias = result.get("bias_ms", 0)
    extras = {extra["mark"]: extra for extra in result["extras"]}
    rows = []

    def gap(index, chip):
        return abs(marks[index]["recorder_ms"] - bias - chips[chip].recorder_ms)

    lines = [child for child in children if child.kind in (KIND_NO_CHIP_CROSSING, KIND_NEAR_CROSSING)]
    surplus = max(0, len(unread) - len(free))
    guessed = sorted((index for index in extras if not read(index)), key=lambda index: (
        not any(counted_person_on_child_frame(child, [marks[index]]) for child in lines),
        -min((gap(index, chip) for chip in free), default=0), marks[index]["recorder_ms"]))
    for index in [index for index in extras if read(index)] + guessed[:surplus]:
        extra, mark = extras[index], marks[index]
        if group_exports(extra, len(active), len(chips)):
            continue
        frame = dict(mark.get("frame") or {})
        evidence = {"group": item.item_id, "camera_index": camera, "recorder_ms": int(mark["recorder_ms"]),
                    "position": [mark.get("x"), mark.get("y")], "count_difference": "extra",
                    "read_bib": read(index)}
        if frame:
            evidence["frame"] = frame
        chip_time = extra["chip_time_ms"]
        if chip_time is not None:
            evidence["time_source"] = (f"集团内前后过线芯片之间插值（前后参考相隔 {extra['bracket_ms']} ms）"
                                       if extra.get("bracket_ms") is not None else "集团内前后过线芯片之间插值")
        elif model is not None:
            chip_time = int(model.to_chip(mark["recorder_ms"], frame.get("session_id", "")))
        detail = (f"{read(index)} 号不在本团芯片里：疑似漏读，也可能号码看错或一人两个芯片；核对后认号补录"
                  if read(index) else
                  "本团点出的人比芯片多，这一位没有对应芯片：疑似漏读（未认号，按过线时间推测是这一位）；"
                  "认得出号码就补录，看不清就保留待证据")
        rows.append(ProblemItem(
            stable_item_id(KIND_NO_CHIP_CROSSING, "count", item.item_id, mark["recorder_ms"], mark.get("x"), mark.get("y")),
            KIND_NO_CHIP_CROSSING, CATEGORY_VIDEO, detail, chip_time_ms=chip_time, status=STATUS_OPEN,
            priority=25, evidence=evidence, calibration_version=version))

    doubted = {event_id for child in children if child.kind in (KIND_CHIP_NO_CROSSING, KIND_TWO_CHIPS_ONE_CROSSING)
               for event_id in child.event_ids}
    missing = set(result["missing"])
    shortfall = max(0, len(free) - len(unread))
    candidates = sorted(free, key=lambda chip: (
        chips[chip].event_id not in doubted, chip not in missing,
        -min((gap(index, chip) for index in unread), default=0), chips[chip].chip_ms))
    for chip in candidates[:shortfall]:
        reading = chips[chip]
        if reading.event_id in doubted:
            continue
        rows.append(ProblemItem(
            stable_item_id(KIND_CHIP_NO_CROSSING, "count", item.item_id, reading.event_id),
            KIND_CHIP_NO_CROSSING, CATEGORY_VIDEO,
            f"芯片 {reading.bib} 没有对应到本团点出的人"
            + ("（未认号，按过线时间推测是这一条）" if unread else "")
            + "：疑似一人两个芯片或备用芯片，也可能漏看了一人",
            chip_time_ms=reading.chip_ms, bibs=(reading.bib,), event_ids=(reading.event_id,),
            status=STATUS_OPEN, priority=25, calibration_version=version,
            evidence={"group": item.item_id, "camera_index": camera, "expected_recorder_ms": reading.recorder_ms,
                      "count_difference": "missing"}))
    return sorted(rows, key=lambda row: row.evidence.get("recorder_ms", row.evidence.get("expected_recorder_ms")))


def review_completion_issue(context: BunchContext, marks: list[dict], result: dict,
                            viewed_ranges, fully_viewed: bool) -> str:
    """The one gate for confirming a group: both review surfaces, every saved
    completion and the export apply it.

    Confirming says "本集团已完整查看并完成人数对照": the recording has no gap,
    every moment from before the first to after the last crosser was on screen,
    the count can be read, and the operator said so. Equal totals never close a
    group by themselves, and unequal ones never keep it open: the doubts folded
    into the group and the differences the count leaves are not answered by the
    confirmation; they stay open on rows of their own (see ``open_doubts``).
    """

    if context.gaps:
        return "原录像存在缺口，不能确认本团已完整核对。"
    unseen = uncovered_intervals(context.start_ms, context.end_ms, viewed_ranges or [], tolerance=100)
    if unseen:
        seconds = sum(right - left for left, right in unseen) / 1000
        return f"本团还有 {seconds:.1f} 秒画面没看过：请从团首看到团尾再确认，或先保存进度。"
    issue = completion_issue(context.chips, marks, result)
    if issue:
        return issue
    if not fully_viewed:
        return "请确认已看完整团，范围内没有未计入的过线者。"
    return ""


def _decision_stamp(workspace: Path) -> tuple:
    """When a group's children, or the answers given to them, last changed on disk."""

    from .batch_review import BATCH_DECISIONS
    from .problem_resolutions import RESOLUTIONS_FILENAME

    stamps = []
    for path in (Path(workspace) / PROBLEM_LIST_NAME, Path(workspace) / "finish_problems" / RESOLUTIONS_FILENAME,
                 Path(workspace) / "finish_problems" / BATCH_DECISIONS):
        try:
            stat = path.stat()
            stamps.append((stat.st_mtime_ns, stat.st_size))
        except OSError:
            stamps.append(None)
    return tuple(stamps)


class BunchReviewStore:
    def __init__(self, directory: Path):
        self.path = Path(directory) / JOURNAL_NAME
        self.latest = {}
        # (item_id, camera) -> (record, decision stamp, open doubt ids)
        self._doubts = {}
        # item_id -> (record, problem list stamp, difference rows)
        self._differences = {}
        if self.path.is_file():
            lines = self.path.read_bytes().splitlines(keepends=True)
            for number, line in enumerate(lines):
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("schema_version") != 1:
                        raise ValueError("unsupported bunch review schema")
                    key = (entry["item_id"], entry["camera"])
                    self.latest.pop(key, None)
                    self.latest[key] = entry
                except (ValueError, KeyError):
                    if number == len(lines) - 1 and not line.endswith(b"\n"):
                        break
                    raise ValueError("集团核对记录损坏，未覆盖原记录。")

    def get(self, context: BunchContext) -> dict | None:
        """The review saved for exactly this evidence, its completion re-checked.

        A completion saved under an older rule, or one the gate no longer
        accepts, comes back reopened: the marks and viewed ranges are kept and
        ``completion_issue`` says why. Only this copy changes; the journal keeps
        what the operator saved.
        """

        record = self.latest.get((context.item.item_id, context.camera))
        if not record or record.get("signature") != context.signature:
            return None
        if not record.get("completed"):
            return record
        if record.get("completion_policy_version") != COMPLETION_POLICY_VERSION:
            issue = "这一团按旧规则（人数相等即完成）关闭过；原点数保留，请看完整团后重新确认。"
        else:
            result = align_counts(context.chips, record["marks"], record.get("fallback_bias_ms", 0))
            issue = review_completion_issue(context, record["marks"], result, record.get("viewed_ranges"),
                                            record.get("fully_viewed", False))
        return dict(record, completed=False, completion_issue=issue) if issue else record

    def open_doubts(self, context: BunchContext) -> list[str]:
        """The doubts folded into this group that its confirmation left open.

        Confirming a group settles its count, not its children: a double chip, a
        chip nobody was seen with, or a crossing without a chip stays a question
        until the operator answers it — on the counted person's own frame or
        number, or on its own row. So does every difference the count itself
        left (``count_differences``). Children and answers are read from disk
        and re-read whenever one of those files changes.
        """

        record = self.get(context)
        if not record or not record.get("completed"):
            return []
        key = (context.item.item_id, context.camera)
        stamp = _decision_stamp(context.workspace)
        cached = self._doubts.get(key)
        if cached is None or cached[0] is not record or cached[1] != stamp:
            result = align_counts(context.chips, record["marks"], record.get("fallback_bias_ms", 0))
            doubts = [child.item_id for child in unresolved_crossing_conflicts(context, record["marks"], result)]
            doubts += [row.item_id for row in unanswered_differences(
                context, self.differences(context.item, context.workspace))]
            cached = self._doubts[key] = (record, stamp, doubts)
        return list(cached[2])

    def differences(self, item: ProblemItem, workspace: Path) -> list[ProblemItem]:
        """The rows the group's confirmed count leaves (``count_differences``).

        None while the group is open, or when its confirmation belongs to another
        version of the item or of the rule. Worked out from the saved record
        alone, so listing them never opens a recording.
        """

        record = self.latest_for(item)
        if (not record or not record.get("completed")
                or record.get("completion_policy_version") != COMPLETION_POLICY_VERSION
                or record.get("item_snapshot") != item.to_payload()):
            return []
        path = Path(workspace) / PROBLEM_LIST_NAME
        try:
            stamp = (path.stat().st_mtime_ns, path.stat().st_size)
        except OSError:
            stamp = None
        cached = self._differences.get(item.item_id)
        if cached is None or cached[0] is not record or cached[1] != stamp:
            chips = [BunchChip(**chip) for chip in record.get("chips", [])]
            result = align_counts(chips, record["marks"], record.get("fallback_bias_ms", 0))
            children = [ProblemItem.from_payload(value) for value in load_child_items(workspace, item.item_id)]
            camera = int(record["camera"])
            version = int(record.get("calibration_version") or item.calibration_version)
            model = ClockModelStore(Path(workspace) / CLOCK_MODEL_FILENAME).get(camera, version)
            rows = count_differences(item, camera, chips, record["marks"], result,
                                     children=children, model=model, version=version)
            cached = self._differences[item.item_id] = (record, stamp, rows)
        return list(cached[2])

    def latest_for(self, item: ProblemItem) -> dict | None:
        records = [record for (item_id, _camera), record in self.latest.items() if item_id == item.item_id]
        return records[-1] if records else None

    def save(self, context: BunchContext, marks: list[dict], seconds: float, *, completed=False,
             fully_viewed=False, fallback_bias_ms=0, viewed_ranges=None, visited_candidates=()) -> dict:
        result = align_counts(context.chips, marks, fallback_bias_ms)
        if completed:
            issue = review_completion_issue(context, marks, result, viewed_ranges, fully_viewed)
            if issue:
                raise ValueError(issue)
        record = {"schema_version": 1, "item_id": context.item.item_id, "signature": context.signature,
                  "item_snapshot": context.item.to_payload(), "chips": [asdict(chip) for chip in context.chips],
                  "camera": context.camera, "calibration_version": context.model.version,
                  "marks": marks, "seconds": round(seconds, 1), "completed": completed,
                  "completion_policy_version": COMPLETION_POLICY_VERSION,
                  "fully_viewed": fully_viewed, "fallback_bias_ms": fallback_bias_ms,
                  "extras": result["extras"]}
        if viewed_ranges is not None:
            record["viewed_ranges"] = viewed_ranges
            record["visited_candidates"] = list(visited_candidates)
        record["counted_riders"] = sum(not is_excluded(m) for m in marks)
        record["identified_riders"] = sum(not is_excluded(m) and bool(m.get("bib", "").strip()) for m in marks)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        append_jsonl_records(self.path, [json.dumps(record, ensure_ascii=False).encode("utf-8")],
                             description="bunch review journal")
        key = (context.item.item_id, context.camera)
        self.latest.pop(key, None)
        self.latest[key] = record
        return record

    def manual_entries(self, items: list[ProblemItem], workspace: Path) -> list[tuple]:
        from .chip_history import format_chip_time
        from .problem_resolutions import _frame_text

        rows = []
        by_id = {item.item_id: item for item in items}
        for item_id, item in by_id.items():
            record = self.latest_for(item)
            if not record or not record.get("completed"):
                continue
            try:
                context = load_context(workspace, by_id[item_id], record["camera"])
            except (ValueError, OSError):
                continue
            checked = self.get(context)
            if checked is None or not checked.get("completed"):
                continue  # changed evidence, or a completion the current gate reopens: ask again
            if pending_member_decisions(context):
                continue
            # Recompute from the saved evidence so stale cached extras cannot
            # reintroduce an excluded person or an incomplete group's suggestions.
            result = align_counts(context.chips, record["marks"], record.get("fallback_bias_ms", 0))
            counted = rider_count(record["marks"])
            for extra in result["extras"]:
                if not group_exports(extra, counted, len(context.chips)):
                    continue  # a row of its own (count_differences), answered there
                mark = record["marks"][extra["mark"]]
                # Every missed rider keeps its own frame, time and bracketing
                # reference; two riders never share one card or one time.
                source = "集团内前后过线芯片之间插值"
                if extra.get("bracket_ms") is not None:
                    source += f"（前后参考相隔 {extra['bracket_ms']} ms）"
                rows.append((extra["bib"], format_chip_time(extra["chip_time_ms"]), _frame_text(mark["frame"]),
                             record["camera"], record["calibration_version"],
                             f"集团人工认号；前后过线插值估算；本团用时 {record['seconds']} 秒", source))
        return rows
