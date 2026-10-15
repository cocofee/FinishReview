"""Manual bunch counts, provisional alignment and durable review drafts.

Only a plate read by the operator identifies a rider. Untyped counts are
aligned in time order as a reference; no CycleRace record is changed here.
"""

from __future__ import annotations

import hashlib
import json
import statistics
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from .analysis_workspace import AnalysisWorkspace
from .clock_sync import CLOCK_MODEL_FILENAME, ClockModel, ClockModelStore
from .durable_jsonl import append_jsonl_records
from .problem_items import ProblemItem


JOURNAL_NAME = "bunch_reviews.jsonl"


@dataclass(frozen=True)
class BunchChip:
    event_id: str
    bib: str
    chip_ms: int
    recorder_ms: int


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

    @property
    def signature(self) -> str:
        payload = {
            "item": self.item.to_payload(), "camera": self.camera,
            "model": self.model.to_payload(), "chips": [asdict(chip) for chip in self.chips],
            "segments": [(s.segment.segment_id, str(s.path), s.start_ms, s.end_ms) for s in self.segments],
            "line": self.line,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def load_context(workspace: Path, item: ProblemItem, camera: int | None = None) -> BunchContext:
    from .problem_analysis import config_from_workspace, line_for_segment

    workspace = Path(workspace)
    source = AnalysisWorkspace(workspace)
    frame = item.evidence.get("frame") or {}
    camera = camera or int(frame.get("camera_index", 1))
    clocks = ClockModelStore(workspace / CLOCK_MODEL_FILENAME)
    model = (clocks.get(camera, item.calibration_version) if camera == frame.get("camera_index")
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
        chips.append(BunchChip(event_id, bib, read.chip_ms, int(model.to_recorder(read.chip_ms, session))))
    chips.sort(key=lambda c: c.chip_ms)
    if not chips:
        raise ValueError("本团没有可核对的芯片记录。")
    start, end = chips[0].recorder_ms - 1500, chips[-1].recorder_ms + 1500
    if camera == frame.get("camera_index"):
        start = min(start, int(item.evidence.get("recorder_start_ms", start)))
        end = max(end, int(item.evidence.get("recorder_end_ms", end)))
    segments = tuple(s for s in archives if s.start_ms <= end and s.end_ms >= start)
    if not segments:
        raise ValueError(f"机位 {camera} 的本团原录像缺失；未打开其他时段录像。")
    config = config_from_workspace(workspace)
    anchor = next((segment for segment in segments if segment.segment.segment_id == frame.get("segment_id")), segments[0])
    line = dict((frame.get("line_config") if camera == frame.get("camera_index") else None)
                or line_for_segment(config, source, anchor) or {})
    return BunchContext(workspace, item, camera, model, chips, segments, start, end, line)


def is_excluded(mark: dict) -> bool:
    """Old drafts are rider counts; an explicit reason records manual exclusion."""
    return bool(mark.get("exclusion_reason", "").strip())


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
        if before and after:
            va, ca = max(before)
            vb, cb = min(after)
            if cb >= ca:
                chip_time = round(ca + (t - va) / (vb - va) * (cb - ca))
        extras.append({"mark": i, "bib": marks[i].get("bib", ""), "chip_time_ms": chip_time})
    ambiguous = [i for i, q in pairs.items() if not marks[i].get("bib") and any(
        p != q and abs(chip.recorder_ms - chips[q].recorder_ms) <= 150 for p, chip in enumerate(chips)
    )]
    return {"pairs": pairs, "extras": extras, "missing": [q for q in range(len(chips)) if q not in used_chips],
            "bias_ms": bias, "lone_count": len(lone), "ambiguous": ambiguous}


def completion_issue(chips: list[BunchChip], marks: list[dict], result: dict, fully_viewed: bool) -> str:
    if not fully_viewed:
        return "请逐帧查看完整团段后，勾选“已看完整团”。"
    active = [mark for mark in marks if not is_excluded(mark)]
    if not active:
        return "请先在原画面点击每位压线者计数。"
    bibs = [m.get("bib", "").strip() for m in active if m.get("bib", "").strip()]
    if len(bibs) != len(set(bibs)):
        return "同一号码被填写在多人上，请先核对并修正。"
    if result["missing"]:
        return "仍有芯片没有对应的计数，请补查原帧；无法确认时保存进度，保留待处理。"
    if any(not e["bib"] or e["chip_time_ms"] is None for e in result["extras"]):
        return "多出的人须看清号码，且前后都有参考过线者才能估算补录时间；否则保存进度。"
    return ""


class BunchReviewStore:
    def __init__(self, directory: Path):
        self.path = Path(directory) / JOURNAL_NAME
        self.latest = {}
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
        record = self.latest.get((context.item.item_id, context.camera))
        return record if record and record.get("signature") == context.signature else None

    def latest_for(self, item: ProblemItem) -> dict | None:
        records = [record for (item_id, _camera), record in self.latest.items() if item_id == item.item_id]
        return records[-1] if records else None

    def save(self, context: BunchContext, marks: list[dict], seconds: float, *, completed=False,
             fully_viewed=False, fallback_bias_ms=0) -> dict:
        result = align_counts(context.chips, marks, fallback_bias_ms)
        if completed:
            issue = completion_issue(context.chips, marks, result, fully_viewed)
            if issue:
                raise ValueError(issue)
        record = {"schema_version": 1, "item_id": context.item.item_id, "signature": context.signature,
                  "item_snapshot": context.item.to_payload(), "chips": [asdict(chip) for chip in context.chips],
                  "camera": context.camera, "calibration_version": context.model.version,
                  "marks": marks, "seconds": round(seconds, 1), "completed": completed,
                  "fully_viewed": fully_viewed, "fallback_bias_ms": fallback_bias_ms,
                  "extras": result["extras"]}
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
            if self.get(context) is None:
                continue  # changed chips, calibration, geometry or evidence: ask again
            # Recompute from the saved evidence so stale cached extras cannot
            # reintroduce an excluded person or an incomplete group's suggestions.
            result = align_counts(context.chips, record["marks"], record.get("fallback_bias_ms", 0))
            if completion_issue(context.chips, record["marks"], result, record.get("fully_viewed", False)):
                continue
            for extra in result["extras"]:
                mark = record["marks"][extra["mark"]]
                rows.append((extra["bib"], format_chip_time(extra["chip_time_ms"]), _frame_text(mark["frame"]),
                             record["camera"], record["calibration_version"],
                             f"集团人工认号；前后过线插值估算；本团用时 {record['seconds']} 秒"))
        return rows
