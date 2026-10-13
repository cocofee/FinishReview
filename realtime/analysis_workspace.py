"""Read-only view of an event workspace for offline finish analysis.

Race folders used for analysis may be read-only archives. Every journal is
parsed without tail recovery, so opening a workspace never writes to it.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .chip_history import (
    ChipRead,
    chip_reads,
    dominant_race_date,
    local_date,
    read_passage_history,
)
from .clock_sync import ClockAnchor
from .passage_evidence import (
    REGULAR_SOURCE,
    PassageEvidenceAssociationStore,
    VideoClockCalibrationStore,
)
from .passage_receiver import PassageEvent
from .race_metadata import RaceMetadata
from .sync_check import latest_events
from .video_timeline import DEFAULT_CLOCK_SOURCE, RecordingSegment, VideoTimelineStore


PASSAGE_JOURNAL = "cyclerace_passage_events.jsonl"
DELETIONS_FILE = "cyclerace_passage_events_deleted.json"
TIMELINE_JOURNAL = "video_timeline.jsonl"
CALIBRATION_JOURNAL = "video_clock_calibrations.jsonl"
ASSOCIATION_JOURNAL = "passage_evidence_associations.jsonl"
METADATA_FILE = "cyclerace_race_metadata.json"
SCOPE_FILE = "review_event_scope.json"
FINISH_LINE_FILES = ("finish_lines.json", "long_image_lines.json")
_ARCHIVE_SUFFIX_RE = re.compile(r"_archive_\d+$", re.IGNORECASE)
_VIDEO_SUFFIXES = {".mkv", ".mp4", ".mov", ".avi", ".ts"}


@dataclass(frozen=True, slots=True)
class ArchiveSegment:
    """One closed, continuous recording file with a verified media range."""

    segment: RecordingSegment
    path: Path
    session_id: str

    @property
    def camera_index(self) -> int:
        return int(self.segment.camera_index)

    @property
    def start_ms(self) -> int:
        return int(self.segment.media_started_at_ms)

    @property
    def end_ms(self) -> int:
        return int(self.segment.media_started_at_ms) + int(self.segment.media_duration_ms)


def session_id_for_path(path: Path) -> str:
    return _ARCHIVE_SUFFIX_RE.sub("", Path(path).stem)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _deleted_event_ids(root: Path) -> tuple[str, ...]:
    payload = _read_json(root / DELETIONS_FILE)
    if isinstance(payload, list):
        return tuple(str(item) for item in payload if isinstance(item, str))
    if isinstance(payload, dict):
        records = payload.get("deletions", [])
        return tuple(str(item.get("event_id")) for item in records if isinstance(item, dict) and item.get("event_id"))
    return ()


class AnalysisWorkspace:
    """Passages, recordings and operator anchors of one race stage."""

    def __init__(
        self,
        root: str | Path,
        *,
        race_id: str = "",
        stage_id: str = "",
        race_date: str = "",
        as_of_ms: Optional[int] = None,
    ):
        self.root = Path(root).expanduser().absolute()
        self.history: tuple[PassageEvent, ...] = read_passage_history(self.root / PASSAGE_JOURNAL)
        if as_of_ms is not None:
            # Replay: only what FinishReview had received by then.
            self.history = tuple(
                event for event in self.history
                if 0 < int(event.received_at_ms) <= int(as_of_ms)
            )
        self.race_id, self.stage_id = self._scope(race_id, stage_id)
        scoped = [
            event for event in self.history
            if event.race_id == self.race_id and (not self.stage_id or event.stage_id == self.stage_id)
        ]
        metadata = self.metadata()
        self.race_date = race_date or (
            metadata.stage_date if metadata is not None and metadata.race_id == self.race_id
            and metadata.stage_date else dominant_race_date(scoped)
        )
        self.deleted_event_ids = _deleted_event_ids(self.root)
        self.reads: tuple[ChipRead, ...] = chip_reads(
            scoped, race_id=self.race_id, stage_id=self.stage_id,
            race_date=self.race_date, deleted_event_ids=self.deleted_event_ids,
        )
        self._latest = {
            event_id: event for event_id, event in latest_events(scoped).items()
        }
        self._timeline: Optional[VideoTimelineStore] = None

    def _scope(self, race_id: str, stage_id: str) -> tuple[str, str]:
        if race_id:
            return race_id, stage_id
        scope = _read_json(self.root / SCOPE_FILE)
        if isinstance(scope, dict) and scope.get("race_id"):
            return str(scope["race_id"]), str(scope.get("stage_id", ""))
        metadata = self.metadata()
        if metadata is not None:
            return metadata.race_id, metadata.stage_id
        counter = Counter((event.race_id, event.stage_id) for event in self.history if event.is_active)
        if not counter:
            raise ValueError(f"no CycleRace passages in {self.root}")
        return counter.most_common(1)[0][0]

    def metadata(self) -> Optional[RaceMetadata]:
        payload = _read_json(self.root / METADATA_FILE)
        if not isinstance(payload, dict):
            return None
        try:
            payload = dict(payload)
            payload.setdefault("schema_version", 1)
            payload.setdefault("message_type", "race_metadata")
            return RaceMetadata.from_payload(payload)
        except (ValueError, TypeError):
            return None

    def latest_event(self, event_id: str) -> Optional[PassageEvent]:
        return self._latest.get(event_id)

    def timeline(self) -> VideoTimelineStore:
        if self._timeline is None:
            self._timeline = VideoTimelineStore(
                self.root / TIMELINE_JOURNAL, recover_incomplete_tail=False,
            )
        return self._timeline

    def archive_segments(self, camera_index: Optional[int] = None) -> tuple[ArchiveSegment, ...]:
        """Closed long-form recordings of this race, oldest first."""

        segments = []
        if not (self.root / TIMELINE_JOURNAL).is_file():
            return ()
        store = self.timeline()
        seen = set()
        for segment in store.segments():
            if camera_index is not None and int(segment.camera_index) != int(camera_index):
                continue
            if segment.clock_source != DEFAULT_CLOCK_SOURCE:
                continue
            if segment.media_started_at_ms is None or segment.media_duration_ms is None:
                continue
            if segment.race_id and segment.race_id != self.race_id:
                continue
            path = store.resolve_video_path(segment)
            if path.suffix.lower() not in _VIDEO_SUFFIXES or "archive" not in path.stem.lower():
                continue
            key = str(path).lower()
            if key in seen or not path.is_file():
                continue
            seen.add(key)
            segments.append(ArchiveSegment(segment, path, session_id_for_path(path)))
        segments.sort(key=lambda item: (item.camera_index, item.start_ms))
        return tuple(segments)

    def camera_indexes(self) -> tuple[int, ...]:
        return tuple(sorted({item.camera_index for item in self.archive_segments()}))

    def finish_lines(self) -> dict:
        for name in FINISH_LINE_FILES:
            payload = _read_json(self.root / name)
            if isinstance(payload, dict) and isinstance(payload.get("finish_lines"), list):
                return {
                    int(item["camera_index"]): item
                    for item in payload["finish_lines"]
                    if isinstance(item, dict) and "camera_index" in item
                }
        return {}

    def manual_anchors(self, camera_index: int) -> tuple[ClockAnchor, ...]:
        """Manual clock calibrations recorded by the existing first-rider workflow."""

        path = self.root / CALIBRATION_JOURNAL
        if not path.is_file():
            return ()
        store = VideoClockCalibrationStore(path, recover_incomplete_tail=False)
        anchors = []
        for calibration in store.calibrations():
            if calibration.camera_index != int(camera_index):
                continue
            event = self.latest_event(calibration.anchor_event_id)
            if event is None or event.timeline_timestamp_ms <= 0:
                continue
            if self.race_date and local_date(event.timeline_timestamp_ms) != self.race_date:
                continue
            anchors.append(
                ClockAnchor(
                    camera_index=int(camera_index),
                    chip_ms=int(event.timeline_timestamp_ms),
                    offset_ms=int(calibration.offset_ms),
                    kind="manual_calibration",
                    label=calibration.anchor_bib,
                    reference=calibration.session_key.rsplit("|", 1)[-1],
                )
            )
        return tuple(anchors)

    def operator_anchors(self, camera_index: int, *, archive_only: bool = True) -> tuple[ClockAnchor, ...]:
        """Offsets implied by operator-confirmed frames (one per confirmation)."""

        path = self.root / ASSOCIATION_JOURNAL
        if not path.is_file() or not (self.root / TIMELINE_JOURNAL).is_file():
            return ()
        store = PassageEvidenceAssociationStore(path, recover_incomplete_tail=False)
        timeline = self.timeline()
        anchors = []
        for association in store.associations():
            if association.confirmed_source != REGULAR_SOURCE or association.confirmation_status != "confirmed":
                continue
            segment = timeline.get_segment(association.segment_id)
            if segment is None or segment.media_started_at_ms is None or int(segment.camera_index) != int(camera_index):
                continue
            video = timeline.resolve_video_path(segment)
            if archive_only and "archive" not in video.stem.lower():
                continue
            event = self.latest_event(association.passage_event_id)
            if event is None or event.timeline_timestamp_ms <= 0:
                continue
            recorder = int(segment.media_started_at_ms) + int(association.position_ms)
            anchors.append(
                ClockAnchor(
                    camera_index=int(camera_index),
                    chip_ms=int(event.timeline_timestamp_ms),
                    offset_ms=recorder - int(event.timeline_timestamp_ms),
                    kind="operator_confirmation",
                    label=association.bib,
                    reference=association.passage_event_id,
                )
            )
        return tuple(anchors)


__all__ = [
    "AnalysisWorkspace",
    "ArchiveSegment",
    "session_id_for_path",
]
