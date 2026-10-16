"""Items of the finish problem list shared by chip checks and video analysis.

An item only flags something for the operator. It never changes CycleRace
results; resolutions are exported as suggestions for CycleRace to apply.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional


CATEGORY_CHIP = "chip"
CATEGORY_VIDEO = "video"
CATEGORY_SYNC = "sync"
CATEGORY_COVERAGE = "coverage"

STATUS_OPEN = "open"
STATUS_DISPUTED = "disputed"
STATUS_CLOSED_UPSTREAM = "closed_upstream"
STATUS_EXCLUDED = "excluded"
STATUS_INFO = "info"
STATUS_WAITING = "waiting"

# Chip-only checks (plan 7.3, second part).
KIND_TEST_CHIP = "test_chip"
KIND_UNKNOWN_BIB = "unknown_bib"
KIND_DUPLICATE_READ = "duplicate_read"
KIND_DUPLICATE_MANUAL = "duplicate_manual"
KIND_REPEATED_PASSAGE = "repeated_passage"
KIND_BATCH_READS = "batch_reads"
KIND_BEFORE_GUN = "before_gun"
KIND_EARLY_READ = "early_read"
KIND_LAP_REPEAT = "lap_repeat"
KIND_LAP_REGRESS = "lap_regress"
KIND_LAP_TIME = "lap_time"
KIND_LAP_MISSING = "lap_missing"
KIND_NOT_ARRIVED = "not_arrived"
KIND_SYNC = "sync"
# Video problems (plan 7.3, first part).
KIND_NO_CHIP_CROSSING = "no_chip_crossing"
KIND_NEAR_CROSSING = "near_crossing"
KIND_TWO_CHIPS_ONE_CROSSING = "two_chips_one_crossing"
KIND_CHIP_NO_CROSSING = "chip_no_crossing"
KIND_BUNCH = "bunch_check"
KIND_OBSTRUCTION = "obstruction"
KIND_STANDING = "standing"
KIND_NO_VIDEO = "no_video"
KIND_MANUAL_REVIEW = "manual_review"
KIND_WAITING = "evidence_pending"

# Operator task classes (plan step 6). Every kind belongs to exactly one, so the
# counting row can never hide an item behind a fallback: the three classes the
# operator works through, the chip-record checks that were always in the queue,
# and the items that only wait for evidence.
TYPE_MULTI_CHIP = "multi_chip"
TYPE_BUNCH = "bunch"
TYPE_MISSED = "missed"
TYPE_RECORD = "record"
TYPE_EVIDENCE = "evidence"
TYPE_OTHER = "other"

TYPE_TITLES = {
    TYPE_MULTI_CHIP: "多芯片疑点",
    TYPE_BUNCH: "集团待核",
    TYPE_MISSED: "漏读疑点",
    TYPE_RECORD: "记录与圈数",
    TYPE_EVIDENCE: "待证据",
    TYPE_OTHER: "其他疑点",
}

# Counting-row order; the last class only appears when it holds items.
TYPE_ORDER = (TYPE_MULTI_CHIP, TYPE_BUNCH, TYPE_MISSED, TYPE_RECORD, TYPE_EVIDENCE, TYPE_OTHER)

KIND_TYPES = {
    KIND_TWO_CHIPS_ONE_CROSSING: TYPE_MULTI_CHIP,
    KIND_DUPLICATE_READ: TYPE_MULTI_CHIP,
    KIND_DUPLICATE_MANUAL: TYPE_MULTI_CHIP,
    KIND_REPEATED_PASSAGE: TYPE_MULTI_CHIP,
    KIND_BATCH_READS: TYPE_MULTI_CHIP,
    KIND_TEST_CHIP: TYPE_MULTI_CHIP,
    KIND_UNKNOWN_BIB: TYPE_MULTI_CHIP,
    KIND_BUNCH: TYPE_BUNCH,
    KIND_OBSTRUCTION: TYPE_BUNCH,
    KIND_STANDING: TYPE_BUNCH,
    KIND_NO_CHIP_CROSSING: TYPE_MISSED,
    KIND_NEAR_CROSSING: TYPE_MISSED,
    KIND_CHIP_NO_CROSSING: TYPE_MISSED,
    KIND_MANUAL_REVIEW: TYPE_MISSED,
    KIND_BEFORE_GUN: TYPE_RECORD,
    KIND_EARLY_READ: TYPE_RECORD,
    KIND_LAP_REPEAT: TYPE_RECORD,
    KIND_LAP_REGRESS: TYPE_RECORD,
    KIND_LAP_TIME: TYPE_RECORD,
    KIND_LAP_MISSING: TYPE_RECORD,
    KIND_NOT_ARRIVED: TYPE_RECORD,
    KIND_SYNC: TYPE_RECORD,
    KIND_NO_VIDEO: TYPE_EVIDENCE,
    KIND_WAITING: TYPE_EVIDENCE,
}

# The class the operator opens first for that type; the panel shows exactly one.
TYPE_ENTRY_LABELS = {
    TYPE_MULTI_CHIP: "按成员处理批次",
    TYPE_BUNCH: "打开集团逐帧核对",
    TYPE_MISSED: "查看候选证据",
}

KIND_TITLES = {
    KIND_MANUAL_REVIEW: "人工发现 · 待核实",
    KIND_WAITING: "等待录像 / 校时",
    KIND_TEST_CHIP: "测试或备用芯片读数",
    KIND_UNKNOWN_BIB: "名单外的号码",
    KIND_DUPLICATE_READ: "同号重复读数",
    KIND_DUPLICATE_MANUAL: "重复补录",
    KIND_REPEATED_PASSAGE: "同号多次有效通过",
    KIND_BATCH_READS: "成批读数",
    KIND_BEFORE_GUN: "早于发枪的读数",
    KIND_EARLY_READ: "发枪后过早的读数",
    KIND_LAP_REPEAT: "圈号重复",
    KIND_LAP_REGRESS: "圈号倒退",
    KIND_LAP_TIME: "单圈用时异常",
    KIND_LAP_MISSING: "末圈缺读",
    KIND_NOT_ARRIVED: "应到未到",
    KIND_SYNC: "两边记录不一致",
    KIND_NO_CHIP_CROSSING: "有人过线，没有芯片",
    KIND_NEAR_CROSSING: "附近可能另有过线",
    KIND_TWO_CHIPS_ONE_CROSSING: "一次过线，两个芯片",
    KIND_CHIP_NO_CROSSING: "有芯片，没看到过线",
    KIND_BUNCH: "集团核对",
    KIND_OBSTRUCTION: "线被挡 / 拥挤时段",
    KIND_STANDING: "有人停留",
    KIND_NO_VIDEO: "录像缺口内的芯片",
}

# Operator actions; the value is the button text.
ACTION_RECORD_BIB = "record_bib"
ACTION_NOT_RIDER = "not_rider"
ACTION_RIDE_BACK = "ride_back"
ACTION_SAME_CROSSING = "same_crossing"
ACTION_DELETE_EXTRA_CHIP = "delete_extra_chip"
ACTION_SIDE_BY_SIDE = "side_by_side"
ACTION_DELETE_CHIP = "delete_chip"
ACTION_INSUFFICIENT = "insufficient"
ACTION_KEEP = "keep"
ACTION_CHANGE = "suggest_change"
ACTION_SYNCED = "synced"
ACTION_CHECKED = "checked"

ACTION_LABELS = {
    ACTION_RECORD_BIB: "认号补录",
    ACTION_NOT_RIDER: "非选手",
    ACTION_RIDE_BACK: "往回骑或推车",
    ACTION_SAME_CROSSING: "确认是同一次过线",
    ACTION_DELETE_EXTRA_CHIP: "删除多余芯片",
    ACTION_SIDE_BY_SIDE: "确认是并排两人",
    ACTION_DELETE_CHIP: "建议删除芯片",
    ACTION_INSUFFICIENT: "证据不足",
    ACTION_KEEP: "确认有效",
    ACTION_CHANGE: "建议改号改圈",
    ACTION_SYNCED: "已在 CycleRace 处理",
    ACTION_CHECKED: "已核对",
}

# Standing reasons for the decisions that must carry one: the operator looks at
# the frame and confirms what it shows instead of typing it out, and the field
# stays editable for anything the frame shows differently.
ACTION_REASON_PRESETS = {
    ACTION_NOT_RIDER: "原帧是行人或工作人员，不是选手过线",
    ACTION_RIDE_BACK: "原帧该选手往回骑或推车，不是正常过线",
    ACTION_SAME_CROSSING: "原帧是同一人的后轮或影子，前轮已在同一帧压线",
    ACTION_DELETE_CHIP: "同一车两块芯片，同一帧压线",
    ACTION_DELETE_EXTRA_CHIP: "同一车两块芯片，同一帧压线",
}

KIND_ACTIONS = {
    KIND_MANUAL_REVIEW: (ACTION_RECORD_BIB, ACTION_NOT_RIDER, ACTION_CHECKED),
    KIND_WAITING: (),
    KIND_TEST_CHIP: (ACTION_DELETE_CHIP, ACTION_KEEP),
    KIND_UNKNOWN_BIB: (ACTION_CHANGE, ACTION_DELETE_CHIP, ACTION_KEEP),
    KIND_DUPLICATE_READ: (ACTION_DELETE_EXTRA_CHIP, ACTION_KEEP),
    KIND_DUPLICATE_MANUAL: (ACTION_DELETE_EXTRA_CHIP, ACTION_KEEP),
    KIND_REPEATED_PASSAGE: (ACTION_DELETE_EXTRA_CHIP, ACTION_CHANGE, ACTION_KEEP),
    KIND_BATCH_READS: (ACTION_DELETE_CHIP, ACTION_KEEP),
    KIND_BEFORE_GUN: (ACTION_DELETE_CHIP, ACTION_KEEP),
    KIND_EARLY_READ: (ACTION_DELETE_CHIP, ACTION_KEEP),
    KIND_LAP_REPEAT: (ACTION_CHANGE, ACTION_DELETE_CHIP, ACTION_KEEP),
    KIND_LAP_REGRESS: (ACTION_CHANGE, ACTION_KEEP),
    KIND_LAP_TIME: (ACTION_CHANGE, ACTION_KEEP),
    KIND_LAP_MISSING: (ACTION_CHECKED,),
    KIND_NOT_ARRIVED: (ACTION_CHECKED,),
    KIND_SYNC: (ACTION_SYNCED, ACTION_CHECKED),
    KIND_NO_CHIP_CROSSING: (ACTION_RECORD_BIB, ACTION_NOT_RIDER, ACTION_RIDE_BACK),
    KIND_NEAR_CROSSING: (ACTION_RECORD_BIB, ACTION_SAME_CROSSING, ACTION_NOT_RIDER, ACTION_RIDE_BACK, ACTION_INSUFFICIENT),
    KIND_TWO_CHIPS_ONE_CROSSING: (ACTION_DELETE_EXTRA_CHIP, ACTION_SIDE_BY_SIDE),
    KIND_CHIP_NO_CROSSING: (ACTION_DELETE_CHIP, ACTION_INSUFFICIENT, ACTION_KEEP),
    KIND_BUNCH: (ACTION_CHECKED, ACTION_RECORD_BIB),
    KIND_OBSTRUCTION: (ACTION_CHECKED, ACTION_RECORD_BIB),
    KIND_STANDING: (ACTION_NOT_RIDER, ACTION_RECORD_BIB),
    KIND_NO_VIDEO: (ACTION_CHECKED,),
}


def stable_item_id(kind: str, *parts: object) -> str:
    """Identity that survives reruns as long as the underlying records do."""

    digest = hashlib.sha1("|".join(str(part) for part in parts).encode("utf-8")).hexdigest()[:12]
    return f"{kind}:{digest}"


def task_type(kind: str) -> str:
    """Operator class of a kind; unknown kinds stay visible as 其他疑点."""

    return KIND_TYPES.get(kind, TYPE_OTHER)


@dataclass(frozen=True, slots=True)
class ProblemItem:
    item_id: str
    kind: str
    category: str
    detail: str
    chip_time_ms: Optional[int] = None
    bibs: tuple[str, ...] = ()
    event_ids: tuple[str, ...] = ()
    status: str = STATUS_OPEN
    status_note: str = ""
    priority: int = 50
    evidence: Mapping[str, Any] = field(default_factory=dict)
    calibration_version: int = 0

    @property
    def title(self) -> str:
        if self.evidence.get("batch_members"):
            return "多芯片批次核对" if self.category == CATEGORY_VIDEO else "密集读数，人数待核"
        return KIND_TITLES.get(self.kind, self.kind)

    @property
    def task_type(self) -> str:
        return task_type(self.kind)

    @property
    def actions(self) -> tuple[str, ...]:
        return KIND_ACTIONS.get(self.kind, (ACTION_CHECKED,))

    @property
    def pending(self) -> bool:
        """Needs a decision at the top level (group members are handled in the group)."""

        return self.status in (STATUS_OPEN, STATUS_DISPUTED) and not self.grouped

    @property
    def grouped(self) -> str:
        return str(self.evidence.get("group", "")) if self.evidence else ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "kind": self.kind,
            "category": self.category,
            "title": self.title,
            "detail": self.detail,
            "chip_time_ms": self.chip_time_ms,
            "bibs": list(self.bibs),
            "event_ids": list(self.event_ids),
            "status": self.status,
            "status_note": self.status_note,
            "priority": int(self.priority),
            "evidence": dict(self.evidence),
            "calibration_version": int(self.calibration_version),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ProblemItem":
        if not isinstance(payload, Mapping):
            raise ValueError("problem item must be an object")
        chip_time = payload.get("chip_time_ms")
        return cls(
            item_id=str(payload["item_id"]),
            kind=str(payload["kind"]),
            category=str(payload.get("category", "")),
            detail=str(payload.get("detail", "")),
            chip_time_ms=None if chip_time is None else int(chip_time),
            bibs=tuple(str(item) for item in payload.get("bibs", ())),
            event_ids=tuple(str(item) for item in payload.get("event_ids", ())),
            status=str(payload.get("status", STATUS_OPEN)),
            status_note=str(payload.get("status_note", "")),
            priority=int(payload.get("priority", 50)),
            evidence=dict(payload.get("evidence", {})),
            calibration_version=int(payload.get("calibration_version", 0)),
        )


def sort_items(items) -> list[ProblemItem]:
    """Pending first, then by time; closed and excluded items go last."""

    order = {STATUS_OPEN: 0, STATUS_DISPUTED: 0, STATUS_INFO: 1, STATUS_CLOSED_UPSTREAM: 2, STATUS_EXCLUDED: 3}
    return sorted(
        items,
        key=lambda item: (
            order.get(item.status, 1),
            item.chip_time_ms if item.chip_time_ms is not None else 2**62,
            item.priority,
            item.item_id,
        ),
    )


__all__ = [name for name in globals() if name.isupper() or name in {
    "ProblemItem", "sort_items", "stable_item_id", "task_type",
}]
