"""Discovery history for "附近可能另有过线" candidates (plan step 5).

A crossing beside a chip that is already matched to another crossing is a
candidate while nothing claims it. When the late chip finally arrives, the
candidate leaves the problem list on its own, so the discovery is journalled
here: the analysis run that closes it also reports it as reference information,
and the journal stays as the durable trace. Chip and video counts never decide
who crossed; closing a candidate only means a chip now claims that crossing.

Only the video analysis process writes this journal; it is append-only and the
latest record per candidate wins.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence

from .chip_history import format_chip_time
from .durable_jsonl import append_jsonl_records
from .problem_items import CATEGORY_VIDEO, KIND_NEAR_CROSSING, STATUS_CLOSED_UPSTREAM, STATUS_INFO, ProblemItem


HISTORY_FILENAME = "near_crossing_history.jsonl"
SCHEMA_VERSION = 1


def _frame_key(frame: Mapping[str, object]) -> tuple[str, int, int]:
    frame = frame or {}
    return (
        str(frame.get("video_path", "")),
        int(frame.get("camera_index", 0) or 0),
        int(frame.get("frame_index", -1)),
    )


def load_history(path: str | Path) -> dict[str, dict]:
    """Latest record per candidate; a torn last line is ignored, not repaired."""

    journal = Path(path)
    if not journal.is_file():
        return {}
    latest: dict[str, dict] = {}
    lines = journal.read_bytes().splitlines(keepends=True)
    for number, raw in enumerate(lines):
        stripped = raw.rstrip(b"\r\n")
        if not stripped:
            continue
        try:
            record = json.loads(stripped.decode("utf-8"))
            if record.get("schema_version") != SCHEMA_VERSION or not record.get("item_id"):
                raise ValueError("unsupported candidate history record")
        except (UnicodeDecodeError, ValueError, TypeError):
            if number == len(lines) - 1 and not raw.endswith((b"\n", b"\r")):
                break
            continue
        latest[str(record["item_id"])] = record
    return latest


def candidate_records(items: Sequence[ProblemItem]) -> dict[str, ProblemItem]:
    """Candidate items of this run, excluding items replayed from the journal."""

    return {
        item.item_id: item
        for item in items
        if item.kind == KIND_NEAR_CROSSING and not item.evidence.get("recorded_history")
    }


def update_history(
    path: str | Path,
    items: Sequence[ProblemItem],
    late_pairs: Iterable[Mapping[str, object]] = (),
    *,
    now_ms: int,
    race_id: str = "",
    stage_id: str = "",
    analysed_cameras: Optional[Iterable[int]] = None,
) -> list[ProblemItem]:
    """Append discovery and closure records; return the closures of this run.

    A candidate closes when a late chip is matched to its crossing, when the
    operator's manual entry already closed it, or when a rerun no longer
    reports it. Closed candidates come back as reference items once, so the
    list shows what happened instead of the candidate disappearing.

    ``analysed_cameras`` limits "no longer reported" to cameras this run really
    analysed: a camera skipped for a missing line or unsealed footage reports
    nothing, which is not evidence that its candidates were resolved.
    """

    journal = Path(path)
    history = load_history(journal)
    late_bibs = {_frame_key(entry.get("frame") or {}): str(entry.get("bib", ""))
                 for entry in late_pairs if entry.get("frame")}
    fresh = candidate_records(items)
    present = {item.item_id for item in items if item.kind == KIND_NEAR_CROSSING}
    appended: list[dict] = []
    closed: list[ProblemItem] = []
    for item_id, item in fresh.items():
        previous = history.get(item_id) or {}
        if previous.get("state") == "open":
            continue  # already journalled; the journal records changes, not each run
        frame = dict(item.evidence.get("frame") or {})
        record = {
            "schema_version": SCHEMA_VERSION, "item_id": item_id, "state": "open",
            "race_id": race_id, "stage_id": stage_id,
            "camera_index": int(frame.get("camera_index", 0) or 0),
            "chip_time_ms": item.chip_time_ms, "recorder_ms": item.evidence.get("recorder_ms"),
            "frame": frame, "near_read": dict(item.evidence.get("near_read") or {}),
            "gap_ms": item.evidence.get("gap_ms"), "detail": item.detail,
            "first_seen_ms": now_ms,
        }
        appended.append(record)
        if item.status == STATUS_CLOSED_UPSTREAM:
            reason = item.status_note or "CycleRace 已补录"
            closed.append(_closed_item(item, record, reason))
            appended.append(dict(record, state="closed", closed_at_ms=now_ms, closed_reason=reason))
    cameras = None if analysed_cameras is None else {int(value) for value in analysed_cameras}
    for item_id, record in history.items():
        if item_id in present or record.get("state") != "open":
            continue
        if (race_id or stage_id) and (str(record.get("race_id", "")), str(record.get("stage_id", ""))) != (race_id, stage_id):
            continue
        if cameras is not None and int(record.get("camera_index", 0) or 0) not in cameras:
            continue  # not analysed this run: keep it open, nothing was decided
        bib = late_bibs.get(_frame_key(record.get("frame") or {}))
        reason = (f"迟到芯片 {bib} 已匹配该过线" if bib else "本轮重分析未再出现该候选（可能已由芯片或补录匹配）")
        appended.append(dict(record, state="closed", closed_at_ms=now_ms, closed_reason=reason))
        closed.append(_closed_item(item=None, record=record, reason=reason))
    if appended:
        journal.parent.mkdir(parents=True, exist_ok=True)
        append_jsonl_records(journal, [json.dumps(record, ensure_ascii=False).encode("utf-8") for record in appended],
                             description="near crossing candidate history")
    return closed


def _closed_item(item: Optional[ProblemItem], record: Mapping[str, object], reason: str) -> ProblemItem:
    """Reference item for a candidate that no longer needs a decision."""

    note = f"发现历史：{format_chip_time(record.get('first_seen_ms'), date=True)} 标记的漏读候选；{reason}"
    evidence = {"frame": dict(record.get("frame") or {}), "near_read": dict(record.get("near_read") or {}),
                "gap_ms": record.get("gap_ms"), "recorded_history": True,
                "closed_reason": reason, "first_seen_ms": record.get("first_seen_ms")}
    if item is not None:
        return replace(item, status=STATUS_INFO, status_note=note, priority=85,
                       evidence=dict(item.evidence, **evidence))
    return ProblemItem(
        item_id=str(record.get("item_id", "")), kind=KIND_NEAR_CROSSING, category=CATEGORY_VIDEO,
        detail=str(record.get("detail", "附近可能另有过线")), chip_time_ms=record.get("chip_time_ms"),
        status=STATUS_INFO, status_note=note, priority=85, evidence=evidence,
    )


__all__ = ["HISTORY_FILENAME", "candidate_records", "load_history", "update_history"]
