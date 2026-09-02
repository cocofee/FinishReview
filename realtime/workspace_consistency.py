"""Cross-journal validation and rebuildable workspace projection."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator

from .passage_evidence import (
    HIGH_SPEED_SOURCE,
    PassageEvidenceAssociationStore,
    VideoClockCalibrationStore,
)
from .passage_receiver import PassageEventStore
from .review_clip import PassageReviewBindingStore
from .video_timeline import VideoTimelineStore


PROJECTION_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class ConsistencyIssue:
    severity: str
    code: str
    journal: str
    entity_id: str
    message: str


@dataclass(frozen=True, slots=True)
class WorkspaceConsistencyReport:
    issues: tuple[ConsistencyIssue, ...]
    event_count: int
    segment_count: int
    association_count: int
    calibration_count: int
    clip_count: int
    binding_count: int

    @property
    def is_consistent(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)


class WorkspaceConsistencyError(RuntimeError):
    """Raised when an inconsistent workspace cannot be projected safely."""


class WorkspaceConsistencyService:
    """Validate references across append-only workspace journals.

    ``rebuild_projection`` writes only a disposable derived file. Source JSONL
    journals remain untouched and continue to be the audit authority.
    """

    def __init__(
        self,
        passage_store: PassageEventStore,
        timeline_store: VideoTimelineStore,
        association_store: PassageEvidenceAssociationStore,
        calibration_store: VideoClockCalibrationStore,
        binding_store: PassageReviewBindingStore | None = None,
    ) -> None:
        self.passage_store = passage_store
        self.timeline_store = timeline_store
        self.association_store = association_store
        self.calibration_store = calibration_store
        self.binding_store = binding_store

    @classmethod
    def open_read_only(
        cls,
        *,
        passage_journal: str | Path,
        timeline_journal: str | Path,
        association_journal: str | Path,
        calibration_journal: str | Path,
        binding_journal: str | Path | None = None,
    ) -> "WorkspaceConsistencyService":
        """Open journals without truncating a recoverable incomplete tail."""

        return cls(
            PassageEventStore(
                passage_journal,
                recover_incomplete_tail=False,
            ),
            VideoTimelineStore(
                timeline_journal,
                recover_incomplete_tail=False,
            ),
            PassageEvidenceAssociationStore(
                association_journal,
                recover_incomplete_tail=False,
            ),
            VideoClockCalibrationStore(
                calibration_journal,
                recover_incomplete_tail=False,
            ),
            PassageReviewBindingStore(
                binding_journal,
                recover_incomplete_tail=False,
            )
            if binding_journal is not None
            else None,
        )

    @contextmanager
    def _locked_stores(self) -> Iterator[None]:
        stores = [
            self.passage_store,
            self.timeline_store,
            self.association_store,
            self.calibration_store,
        ]
        if self.binding_store is not None:
            stores.append(self.binding_store)
        with ExitStack() as stack:
            for store in sorted(stores, key=lambda value: id(value._lock)):
                stack.enter_context(store._lock)
            yield

    def check(self) -> WorkspaceConsistencyReport:
        with self._locked_stores():
            return self._check_locked()

    def _check_locked(self) -> WorkspaceConsistencyReport:
        events = self.passage_store.events(include_inactive=True)
        segments = self.timeline_store.segments()
        associations = self.association_store.associations()
        calibrations = self.calibration_store.calibrations()
        clips = self.binding_store.clips() if self.binding_store is not None else ()
        bindings = (
            self.binding_store.bindings()
            if self.binding_store is not None
            else ()
        )
        events_by_id = {event.event_id: event for event in events}
        segments_by_id = {segment.segment_id: segment for segment in segments}
        clips_by_id = {clip.clip_id: clip for clip in clips}
        issues: list[ConsistencyIssue] = []
        stores = (
            ("passages", self.passage_store),
            ("timeline", self.timeline_store),
            ("associations", self.association_store),
            ("calibrations", self.calibration_store),
            ("review_clips", self.binding_store),
        )
        for journal, store in stores:
            if store is not None and getattr(store, "_recovered_incomplete_tail", False):
                issues.append(
                    ConsistencyIssue(
                        "warning",
                        "incomplete_tail_ignored",
                        journal,
                        "",
                        "日志末尾存在未完成记录；只读检查已忽略且未修改源文件",
                    )
                )

        for association in associations:
            event = events_by_id.get(association.passage_event_id)
            if event is None:
                issues.append(
                    self._error(
                        "association_event_missing",
                        "passage_evidence_associations.jsonl",
                        association.passage_event_id,
                        "判罚关联引用了不存在的通过事件",
                    )
                )
                continue
            if association.bib != event.bib:
                issues.append(
                    self._error(
                        "association_bib_mismatch",
                        "passage_evidence_associations.jsonl",
                        association.passage_event_id,
                        f"关联号码 {association.bib!r} 与事件号码 {event.bib!r} 不一致",
                    )
                )
            if (
                association.confirmed_source != HIGH_SPEED_SOURCE
                and association.segment_id not in segments_by_id
            ):
                issues.append(
                    self._error(
                        "association_segment_missing",
                        "passage_evidence_associations.jsonl",
                        association.passage_event_id,
                        f"普通视频关联引用了不存在的录像段 {association.segment_id}",
                    )
                )

        for calibration in calibrations:
            event = events_by_id.get(calibration.anchor_event_id)
            if event is None:
                issues.append(
                    self._error(
                        "calibration_event_missing",
                        "video_clock_calibrations.jsonl",
                        calibration.anchor_event_id,
                        "校时锚点引用了不存在的通过事件",
                    )
                )
            elif calibration.anchor_bib != event.bib:
                issues.append(
                    self._error(
                        "calibration_bib_mismatch",
                        "video_clock_calibrations.jsonl",
                        calibration.anchor_event_id,
                        "校时锚点号码与通过事件号码不一致",
                    )
                )

        for clip in clips:
            segment = segments_by_id.get(clip.timeline_segment_id)
            if segment is None:
                issues.append(
                    self._error(
                        "clip_segment_missing",
                        "review_clips.jsonl",
                        clip.clip_id,
                        f"判读片段引用了不存在的时间轴段 {clip.timeline_segment_id}",
                    )
                )
            elif segment.camera_index != clip.camera_index:
                issues.append(
                    self._error(
                        "clip_camera_mismatch",
                        "review_clips.jsonl",
                        clip.clip_id,
                        "判读片段机位与时间轴段机位不一致",
                    )
                )

        for binding in bindings:
            event = events_by_id.get(binding.event_id)
            clip = clips_by_id.get(binding.clip_id)
            if event is None:
                issues.append(
                    self._error(
                        "binding_event_missing",
                        "review_clips.jsonl",
                        binding.event_id,
                        "判读绑定引用了不存在的通过事件",
                    )
                )
            else:
                if binding.revision != event.revision:
                    issues.append(
                        ConsistencyIssue(
                            "warning",
                            "binding_revision_stale",
                            "review_clips.jsonl",
                            binding.event_id,
                            f"绑定版本 {binding.revision} 与事件版本 {event.revision} 不同",
                        )
                    )
                if binding.passage_timestamp_ms != event.timeline_timestamp_ms:
                    issues.append(
                        self._error(
                            "binding_timestamp_mismatch",
                            "review_clips.jsonl",
                            binding.event_id,
                            "判读绑定时间与通过事件时间不一致",
                        )
                    )
            if clip is None:
                issues.append(
                    self._error(
                        "binding_clip_missing",
                        "review_clips.jsonl",
                        binding.event_id,
                        f"判读绑定引用了不存在的片段 {binding.clip_id}",
                    )
                )
            elif clip.camera_index != binding.camera_index:
                issues.append(
                    self._error(
                        "binding_camera_mismatch",
                        "review_clips.jsonl",
                        binding.event_id,
                        "判读绑定机位与片段机位不一致",
                    )
                )

        return WorkspaceConsistencyReport(
            issues=tuple(issues),
            event_count=len(events),
            segment_count=len(segments),
            association_count=len(associations),
            calibration_count=len(calibrations),
            clip_count=len(clips),
            binding_count=len(bindings),
        )

    def rebuild_projection(self, output_path: str | Path) -> Path:
        """Atomically rebuild a disposable latest-state JSON projection."""

        with self._locked_stores():
            return self._rebuild_projection_locked(output_path)

    def _rebuild_projection_locked(self, output_path: str | Path) -> Path:
        report = self._check_locked()
        if not report.is_consistent:
            codes = ", ".join(
                sorted({issue.code for issue in report.issues if issue.severity == "error"})
            )
            raise WorkspaceConsistencyError(
                f"workspace projection rebuild blocked by consistency errors: {codes}"
            )

        events = self.passage_store.events(include_inactive=True)
        segments = self.timeline_store.segments()
        associations = self.association_store.associations()
        calibrations = self.calibration_store.calibrations()
        clips = self.binding_store.clips() if self.binding_store is not None else ()
        bindings = self.binding_store.bindings() if self.binding_store is not None else ()
        payload: dict[str, Any] = {
            "schema_version": PROJECTION_SCHEMA_VERSION,
            "generated_at_ms": int(time.time() * 1000),
            "sources": self._source_fingerprints(),
            "events": {event.event_id: event.to_payload() for event in events},
            "segments": {segment.segment_id: asdict(segment) for segment in segments},
            "associations": {
                f"{value.passage_event_id}:{value.confirmed_source}": value.to_payload()
                for value in associations
            },
            "calibrations": {
                f"{value.camera_index}:{value.session_key}": value.to_payload()
                for value in calibrations
            },
            "clips": {clip.clip_id: asdict(clip) for clip in clips},
            "bindings": {
                f"{binding.event_id}:{binding.camera_index}": asdict(binding)
                for binding in bindings
            },
        }
        target = Path(output_path).expanduser().absolute()
        target.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{target.name}.",
                suffix=".tmp",
                dir=target.parent,
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(data)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, target)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()
        return target

    @staticmethod
    def _error(code: str, journal: str, entity_id: str, message: str) -> ConsistencyIssue:
        return ConsistencyIssue("error", code, journal, entity_id, message)

    def _source_fingerprints(self) -> dict[str, dict[str, Any]]:
        stores = {
            "passages": self.passage_store,
            "timeline": self.timeline_store,
            "associations": self.association_store,
            "calibrations": self.calibration_store,
        }
        if self.binding_store is not None:
            stores["review_clips"] = self.binding_store
        result = {}
        for name, store in stores.items():
            path = store.journal_path
            if not path.exists():
                result[name] = {"path": path.name, "size": 0, "sha256": ""}
                continue
            content = path.read_bytes()
            result[name] = {
                "path": path.name,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        return result
