"""Operator decisions on problem items, and the three CycleRace hand-off lists.

Every decision records the action, reason, evidence frame and calibration
version, bound to the bibs, chip reads and time it was made for. When the
item changes upstream (another bib, another read, a new time) the decision
is stale and the item asks again. Decisions never change CycleRace results;
the exports are suggestions for CycleRace to apply.
"""

from __future__ import annotations

import csv
import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from .chip_history import format_chip_time
from .durable_jsonl import append_jsonl_records
from .problem_items import (
    ACTION_CHANGE,
    ACTION_CONFIRM_CROSSING,
    ACTION_DELETE_CHIP,
    ACTION_DELETE_EXTRA_CHIP,
    ACTION_LABELS,
    ACTION_NOT_RIDER,
    ACTION_RESULT_LABELS,
    ACTION_RECORD_BIB,
    ACTION_RIDE_BACK,
    ACTION_SAME_CROSSING,
    ACTION_SYNCED,
    KIND_BUNCH,
    KIND_MANUAL_REVIEW,
    KIND_OBSTRUCTION,
    KIND_TITLES,
    ProblemItem,
)


SCHEMA_VERSION = 1
RESOLUTIONS_FILENAME = "problem_resolutions.jsonl"
EXPORT_MANUAL_ENTRIES = "补录清单.csv"
EXPORT_DELETIONS = "建议删除清单.csv"
EXPORT_CHANGES = "建议改号改圈清单.csv"
SAME_FRAME_DISTANCE = 0.03


class ProblemResolutionError(RuntimeError):
    """Raised when the decision journal cannot be read or written."""


@dataclass(frozen=True)
class ProblemBinding:
    """What the decision was made for."""

    bibs: tuple[str, ...]
    event_ids: tuple[str, ...]
    chip_time_ms: Optional[int]

    @classmethod
    def of(cls, item: ProblemItem) -> "ProblemBinding":
        return cls(tuple(item.bibs), tuple(item.event_ids), item.chip_time_ms)


@dataclass(frozen=True)
class ProblemResolution:
    item_id: str
    kind: str
    action: str
    binding: ProblemBinding
    bib: str = ""
    target_bib: str = ""
    reason: str = ""
    frame: Mapping[str, Any] = field(default_factory=dict)
    position: tuple[float, float] = ()
    chip_time_ms: Optional[int] = None
    calibration_version: int = 0
    resolved_at_ms: int = 0
    revision: int = 1
    analysis_signature: str = ""
    source_frame: Optional[Mapping[str, Any]] = None

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["binding"] = {
            "bibs": list(self.binding.bibs),
            "event_ids": list(self.binding.event_ids),
            "chip_time_ms": self.binding.chip_time_ms,
        }
        payload["position"] = list(self.position)
        payload["frame"] = dict(self.frame)
        payload["schema_version"] = SCHEMA_VERSION
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ProblemResolution":
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported problem resolution schema_version")
        binding = payload.get("binding") or {}
        return cls(
            item_id=str(payload["item_id"]),
            kind=str(payload.get("kind", "")),
            action=str(payload["action"]),
            binding=ProblemBinding(
                tuple(str(value) for value in binding.get("bibs", ())),
                tuple(str(value) for value in binding.get("event_ids", ())),
                None if binding.get("chip_time_ms") is None else int(binding["chip_time_ms"]),
            ),
            bib=str(payload.get("bib", "")),
            target_bib=str(payload.get("target_bib", "")),
            reason=str(payload.get("reason", "")),
            frame=dict(payload.get("frame", {})),
            position=tuple(float(value) for value in payload.get("position", ())),
            chip_time_ms=None if payload.get("chip_time_ms") is None else int(payload["chip_time_ms"]),
            calibration_version=int(payload.get("calibration_version", 0)),
            resolved_at_ms=int(payload.get("resolved_at_ms", 0)),
            revision=int(payload.get("revision", 1)),
            analysis_signature=str(payload.get("analysis_signature", "")),
            source_frame=None if payload.get("source_frame") is None else dict(payload["source_frame"]),
        )

    @property
    def label(self) -> str:
        return ACTION_RESULT_LABELS.get(self.action) or ACTION_LABELS.get(self.action, self.action)


class ProblemResolutionStore:
    """Append-only journal; the latest revision per item wins."""

    def __init__(self, journal_path: str | Path):
        self.journal_path = Path(journal_path).expanduser().absolute()
        self._lock = threading.RLock()
        self._latest: dict[str, ProblemResolution] = {}
        self._load()

    def _load(self) -> None:
        if not self.journal_path.exists():
            return
        try:
            lines = self.journal_path.read_bytes().splitlines(keepends=True)
        except OSError as error:
            raise ProblemResolutionError(f"failed to read {self.journal_path}") from error
        for number, raw in enumerate(lines, start=1):
            stripped = raw.rstrip(b"\r\n")
            if not stripped:
                continue
            try:
                resolution = ProblemResolution.from_payload(json.loads(stripped.decode("utf-8")))
            except (UnicodeDecodeError, ValueError, KeyError, TypeError) as error:
                if number == len(lines) and not raw.endswith((b"\n", b"\r")):
                    break
                raise ProblemResolutionError(f"invalid problem resolution line {number}: {error}") from error
            current = self._latest.get(resolution.item_id)
            if current is None or resolution.revision >= current.revision:
                self._latest[resolution.item_id] = resolution

    def get(self, item_id: str) -> Optional[ProblemResolution]:
        with self._lock:
            return self._latest.get(item_id)

    def resolutions(self) -> tuple[ProblemResolution, ...]:
        with self._lock:
            return tuple(self._latest.values())

    def resolve(
        self,
        item: ProblemItem,
        action: str,
        *,
        bib: str = "",
        target_bib: str = "",
        reason: str = "",
        position: Sequence[float] = (),
        chip_time_ms: Optional[int] = None,
        frame: Optional[Mapping[str, Any]] = None,
    ) -> ProblemResolution:
        if item.evidence.get("configuration_stale"):
            raise ValueError("配置已变更，等待重新分析后再处理")
        if item.evidence.get("batch_members"):
            raise ValueError("批次必须选择成员逐条处理，不能提交整批决定")
        previous, stale = self.state(item)
        acknowledge = action == ACTION_SYNCED and previous is not None and not stale and previous.action in (
            ACTION_RECORD_BIB, ACTION_DELETE_CHIP, ACTION_DELETE_EXTRA_CHIP, ACTION_CHANGE,
        )
        if action not in item.actions and action != "reopen" and not acknowledge:
            raise ValueError(f"action {action} is not available for {item.kind}")
        if action == ACTION_RECORD_BIB and not bib.strip():
            raise ValueError("认号补录需要号码")
        if action == ACTION_CONFIRM_CROSSING and not item.evidence.get("frame"):
            raise ValueError("确认过线需要保存对应原帧")
        if action in (ACTION_DELETE_CHIP, ACTION_DELETE_EXTRA_CHIP) and not reason.strip():
            raise ValueError("删除需要填写原因")
        if action in (ACTION_NOT_RIDER, ACTION_RIDE_BACK, ACTION_SAME_CROSSING) and not reason.strip():
            # A rider may be excluded, but never on "the number is unreadable":
            # the written reason is what the exclusion stays traceable by.
            raise ValueError("排除或确认同一次过线需要填写依据，请写明看到的画面")
        if action in (ACTION_DELETE_CHIP, ACTION_DELETE_EXTRA_CHIP):
            if len(item.event_ids) > 1 and len(item.bibs) != len(item.event_ids):
                raise ValueError("记录与号码对应不明确，请使用成员级处理")
            target = target_bib.strip() or (item.bibs[0] if len(item.event_ids) == 1 and item.bibs else "")
            if not target or sum(bib == target for bib in item.bibs) != 1:
                raise ValueError("必须指定本问题中唯一对应的目标记录；不能回退到首条")
            target_bib = target
        with self._lock:
            current = self._latest.get(item.item_id)
            resolution = ProblemResolution(
                item_id=item.item_id,
                kind=item.kind,
                action=action,
                binding=ProblemBinding.of(item),
                bib=bib.strip(),
                target_bib=target_bib.strip(),
                reason=reason.strip(),
                frame=dict(frame if frame is not None else item.evidence.get("frame") or {}),
                source_frame=dict(item.evidence.get("frame") or {}) if frame is not None else None,
                position=tuple(float(value) for value in position),
                chip_time_ms=item.chip_time_ms if chip_time_ms is None else int(chip_time_ms),
                calibration_version=int(item.calibration_version),
                analysis_signature=str(item.evidence.get("analysis_signature", "")),
                resolved_at_ms=int(time.time() * 1000),
                revision=(current.revision + 1) if current is not None else 1,
            )
            self.journal_path.parent.mkdir(parents=True, exist_ok=True)
            append_jsonl_records(
                self.journal_path,
                [json.dumps(resolution.to_payload(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")],
                description="problem resolution journal",
            )
            self._latest[item.item_id] = resolution
            return resolution

    def state(self, item: ProblemItem) -> tuple[Optional[ProblemResolution], bool]:
        """Return (decision, stale). A stale decision no longer matches the item."""

        resolution = self.get(item.item_id)
        if resolution is None or resolution.action == "reopen":
            return None, False
        return resolution, (resolution.binding != ProblemBinding.of(item)
                            or bool(item.evidence.get("configuration_stale"))
                            or resolution.calibration_version != item.calibration_version
                            or dict(resolution.source_frame if resolution.source_frame is not None else resolution.frame)
                            != dict(item.evidence.get("frame") or {})
                            or resolution.analysis_signature != str(item.evidence.get("analysis_signature", "")))


def same_frame_conflicts(
    frame: Mapping[str, Any],
    position: Sequence[float],
    bib: str,
    claims: Iterable[tuple[str, Mapping[str, Any], Sequence[float]]],
    *,
    distance: float = SAME_FRAME_DISTANCE,
) -> list[str]:
    """Other bibs already confirmed on the same frame and position.

    ``claims`` are (bib, frame, normalized position). The Dingbian errors
    (067 confirmed on 057, 051 on 029) were second bibs on one frame.
    """

    conflicts = []
    for other_bib, other_frame, other_position in claims:
        if not other_bib or other_bib == bib:
            continue
        same_frame = (
            str(other_frame.get("video_path", "")) == str(frame.get("video_path", ""))
            and int(other_frame.get("frame_index", -1)) == int(frame.get("frame_index", -2))
        ) or (
            other_frame.get("segment_id") and other_frame.get("segment_id") == frame.get("segment_id")
            and int(other_frame.get("frame_index", -1)) == int(frame.get("frame_index", -2))
        )
        if not same_frame:
            continue
        if position and other_position:
            dx = float(position[0]) - float(other_position[0])
            dy = float(position[1]) - float(other_position[1])
            if (dx * dx + dy * dy) ** 0.5 > distance:
                continue
        conflicts.append(other_bib)
    return conflicts


def _frame_text(frame: Mapping[str, Any]) -> str:
    if not frame:
        return ""
    name = Path(str(frame.get("video_path", ""))).name
    return f"{name} 第 {int(frame.get('frame_index', 0)) + 1} 帧（片内 {int(frame.get('position_ms', 0))} ms）"


def _passage_number(event_id: str) -> str:
    return event_id.rsplit("-passage-", 1)[-1] if "-passage-" in event_id else event_id


def _manual_time_source(item: ProblemItem) -> str:
    """Where the suggested chip time comes from; the operator checks it in CycleRace."""

    if item.kind == KIND_MANUAL_REVIEW or item.chip_time_ms is None:
        return "未换算：按原帧人工判断"
    if item.evidence.get("time_source"):
        return str(item.evidence["time_source"])
    if item.kind in (KIND_BUNCH, KIND_OBSTRUCTION):
        return "集团内前后过线芯片之间插值"
    return "校时换算（视频原帧）"


def export_lists(
    items: Sequence[ProblemItem],
    store: ProblemResolutionStore,
    output_dir: str | Path,
    *, events=(), manual_extra=(), deletions_extra=(),
) -> dict[str, int]:
    """Write 补录 / 建议删除 / 建议改号改圈 CSV lists for CycleRace."""

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    by_id = {item.item_id: item for item in items}
    manual, deletions, changes = [], [], []
    from .bunch_review import BunchReviewStore
    bunches = BunchReviewStore(store.journal_path.parent)
    group_bibs: dict[str, set] = {}  # group id -> numbers its own confirmed count exports
    for resolution in sorted(store.resolutions(), key=lambda value: value.chip_time_ms or 0):
        item = by_id.get(resolution.item_id)
        if item is None:
            continue
        parent = by_id.get(item.grouped)
        if parent is not None and parent.evidence.get("batch_members"):
            continue
        if bunches.latest_for(item) is not None:
            continue  # the per-rider bunch review supersedes an old group-level decision
        _decision, stale = store.state(item)
        if stale:
            continue
        title = KIND_TITLES.get(item.kind, item.kind)
        if resolution.action == ACTION_RECORD_BIB:
            if parent is not None and parent.kind in (KIND_BUNCH, KIND_OBSTRUCTION):
                if parent.item_id not in group_bibs:
                    group_bibs[parent.item_id] = {row[0] for row in bunches.manual_entries(
                        [parent], store.journal_path.parent.parent)}
                if resolution.bib in group_bibs[parent.item_id]:
                    # A doubt left open by its confirmed group, answered with a
                    # number the group count already exports: one rider, one entry.
                    continue
            manual.append((
                resolution.bib, format_chip_time(resolution.chip_time_ms), _frame_text(resolution.frame),
                resolution.frame.get("camera_index", ""), resolution.calibration_version,
                f"{title}；{resolution.reason}".rstrip("；"), _manual_time_source(item),
            ))
        elif resolution.action in (ACTION_DELETE_CHIP, ACTION_DELETE_EXTRA_CHIP):
            if item.evidence.get("batch_members") or len(item.bibs) != len(item.event_ids):
                continue
            target = resolution.target_bib or (item.bibs[0] if len(item.event_ids) == 1 and item.bibs else "")
            event_ids = [
                event_id for bib, event_id in zip(item.bibs, item.event_ids) if bib == target
            ]
            if len(event_ids) != 1:
                continue
            for event_id in event_ids:
                deletions.append((
                    target, format_chip_time(item.chip_time_ms), _passage_number(event_id),
                    title, resolution.reason, event_id, "", "", _frame_text(resolution.frame), resolution.calibration_version,
                    json.dumps(resolution.to_payload(), ensure_ascii=False),
                ))
        elif resolution.action == ACTION_CHANGE:
            changes.append((
                item.bibs[0] if item.bibs else "", resolution.target_bib, format_chip_time(item.chip_time_ms),
                "、".join(_passage_number(event_id) for event_id in item.event_ids), title, resolution.reason,
            ))
    from .batch_review import BatchDecisionStore, member_key
    from .bunch_review import group_member_batch, load_context
    batch_store = BatchDecisionStore(store.journal_path.parent)
    exported = set()
    for item in items:
        if item.kind in (KIND_BUNCH, KIND_OBSTRUCTION) and not item.grouped:
            try:
                record = bunches.latest_for(item)
                context = load_context(store.journal_path.parent.parent, item, record["camera"] if record else None)
                item = group_member_batch(context)
            except (ValueError, OSError):
                continue
        for member in item.evidence.get("batch_members", []):
            key = member_key(item, member)
            if key in exported or batch_store.state(item, member, events) != "待 CycleRace":
                continue
            decision = batch_store.latest[key]
            deletions.append((member["bib"], format_chip_time(member["chip_ms"]), _passage_number(member["event_id"]),
                              item.title, decision["reason"], member["event_id"], member["revision"], item.item_id,
                              _frame_text(decision["binding"]["frame"]), item.calibration_version,
                              json.dumps(decision["binding"], ensure_ascii=False)))
            exported.add(key)
    manual.extend(bunches.manual_entries(items, store.journal_path.parent.parent))
    # Rows the arrival list settled (a person without a chip, a second chip).
    manual.extend(manual_extra)
    deletions.extend(deletions_extra)
    # 携带人号码: whose second chip it is (the arrival list's 双芯片); blank elsewhere.
    deletions = [tuple(row) + ("",) * (12 - len(row)) for row in deletions]
    tables = (
        (EXPORT_MANUAL_ENTRIES, ("号码", "芯片域时间", "证据帧", "机位", "校时版本", "说明", "时间来源"), manual),
        (EXPORT_DELETIONS, ("号码", "通过时间", "CycleRace 记录编号", "问题类型", "原因", "事件ID", "记录修订", "批次ID", "证据帧", "校时版本", "证据快照JSON", "携带人号码"), deletions),
        (EXPORT_CHANGES, ("原号码", "建议号码或圈数", "通过时间", "CycleRace 记录编号", "问题类型", "原因"), changes),
    )
    counts = {}
    for name, header, rows in tables:
        path = output_dir / name
        temporary = path.with_name(path.name + ".tmp")
        with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(header)
            writer.writerows(rows)
        os.replace(temporary, path)
        counts[name] = len(rows)
    return counts


__all__ = [
    "EXPORT_CHANGES",
    "EXPORT_DELETIONS",
    "EXPORT_MANUAL_ENTRIES",
    "ProblemBinding",
    "ProblemResolution",
    "ProblemResolutionError",
    "ProblemResolutionStore",
    "RESOLUTIONS_FILENAME",
    "export_lists",
    "same_frame_conflicts",
]
