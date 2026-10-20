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
    KIND_CHIP_NO_CROSSING,
    KIND_NEAR_CROSSING,
    KIND_NO_CHIP_CROSSING,
    KIND_TWO_CHIPS_ONE_CROSSING,
    STATUS_CLOSED_UPSTREAM,
    STATUS_EXCLUDED,
    STATUS_INFO,
    ProblemItem,
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


def load_child_items(workspace: Path, group_id: str) -> list[dict]:
    """The group's current children, straight from the problem list.

    A draft or a saved record keeps its own signature, so the child list is
    read when a decision needs it: a child added, answered or re-analysed later
    must never be hidden by an earlier copy of the context.
    """

    path = Path(workspace) / PROBLEM_LIST_NAME
    payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    return [child for child in payload.get("items", [])
            if child.get("evidence", {}).get("group") == group_id]


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


def add_viewed(ranges, timestamp, start, end):
    """Only decoded frames contribute; seeking across a gap never fills it."""
    merged = []
    for left, right in sorted([*ranges, [max(start, timestamp-50), min(end, timestamp+50)]]):
        if right < left:
            continue
        if merged and left <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], right)
        else:
            merged.append([left, right])
    return merged


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

    人数相等不等于身份对上：读出来的号码不在本团芯片里，就是漏读、双芯片或备用
    芯片的迹象，总数相等也要留着待核。没认号的人按时间先后对芯片只作参考，不另设
    门槛。多出来的人必须有号码、前后都有参考过线者，才补得进 CycleRace。看没看
    完整团由 ``review_completion_issue`` 判断。
    """

    active = [mark for mark in marks if not is_excluded(mark)]
    if not active:
        return "请先在原画面点击每位压线者计数。"
    bibs = [m.get("bib", "").strip() for m in active if m.get("bib", "").strip()]
    if len(bibs) != len(set(bibs)):
        return "同一号码被填写在多人上，请先核对并修正。"
    if len(active) == len(chips):
        # A read number left unpaired has no chip in this group: an identity
        # difference that equal totals would otherwise hide.
        unknown = [extra["bib"].strip() for extra in result["extras"] if extra["bib"].strip()]
        if unknown:
            return (f"号码 {'、'.join(unknown)} 不在本团芯片里：人数相等也可能有漏读或双芯片，"
                    "请核对号码；无法确认时保存进度。")
        return ""
    if result["missing"]:
        return "仍有芯片没有对应的计数，请补查原帧；无法确认时保存进度，保留待处理。"
    if any(not e["bib"] or e["chip_time_ms"] is None for e in result["extras"]):
        return "多出的人须看清号码，且前后都有参考过线者才能估算补录时间；否则保存进度。"
    return ""


def review_completion_issue(context: BunchContext, marks: list[dict], result: dict,
                            viewed_ranges, fully_viewed: bool) -> str:
    """The one gate for confirming a group: both review surfaces, every saved
    completion and the export apply it.

    Confirming says "本集团已完整查看并完成人数对照": the recording has no gap,
    every moment from before the first to after the last crosser was on screen,
    the count owes nothing, and the operator said so. Equal totals never close a
    group by themselves. The doubts folded into the group are not answered by
    the confirmation; they stay open on their own (see ``open_doubts``).
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
        number, or on its own row. Children and answers are read from disk and
        re-read whenever one of those files changes.
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
            cached = self._doubts[key] = (record, stamp, doubts)
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
            if rider_count(record["marks"]) == len(context.chips):
                continue  # 数量相等的集团只完成点数，不把暂未对上的时间参考导出成漏读。
            for extra in result["extras"]:
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
