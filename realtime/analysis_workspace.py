"""Read-only view of an event workspace for offline finish analysis.

Race folders used for analysis may be read-only archives. Every journal is
parsed without tail recovery, so opening a workspace never writes to it.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional

from .chip_history import (
    ChipRead,
    chip_reads,
    dominant_race_date,
    read_passage_history,
)
from .clock_sync import ClockAnchor
from .passage_evidence import (
    REGULAR_SOURCE,
    PassageEvidenceAssociationStore,
    VideoClockCalibrationStore,
)
from .passage_receiver import PassageEvent
from .passage_deletions import decode_deletions
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
RACE_CLOCK_MARGIN_MS = 3 * 60 * 60 * 1000
# Clock jitter between back-to-back sessions is not a second recording.
SESSION_OVERLAP_TOLERANCE_MS = 200


def race_clock_window(reads) -> Optional[tuple[int, int]]:
    """The clock span this race can occupy, from its own chip reads only.

    A workspace folder often holds recordings of several visits (the race and
    a later test in the same room). Only the race clock, widened generously,
    tells them apart; without any chip read there is nothing to compare, and
    the caller keeps everything.
    """

    times = [int(read.chip_ms) for read in reads if int(read.chip_ms) > 0]
    if not times:
        return None
    return min(times) - RACE_CLOCK_MARGIN_MS, max(times) + RACE_CLOCK_MARGIN_MS


@dataclass(frozen=True, slots=True)
class ArchiveSegment:
    """One closed, continuous recording file with a verified media range."""

    segment: RecordingSegment
    path: Path
    session_id: str
    excluded_ranges: tuple[tuple[int, int], ...] = ()

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
    from .runtime_paths import recording_session_path
    return re.sub(r"_review_\d+$", "", _ARCHIVE_SUFFIX_RE.sub("", recording_session_path(Path(path)).stem))


def uncovered_ranges(start, end, excluded):
    cursor = start
    for left, right in sorted(excluded):
        if right <= cursor or left >= end:
            continue
        if left > cursor:
            yield cursor, min(left, end)
        cursor = max(cursor, right)
    if cursor < end:
        yield cursor, end


def _one_recording_at_a_time(segments: list[ArchiveSegment]) -> list[ArchiveSegment]:
    """Two recorder instances on one camera must not report every rider twice.

    When recordings overlap (a second instance, or a build switch while the
    first still records), the session that started later takes over from its
    start; an earlier session keeps only the moments no later one covers.
    """

    started: dict[tuple[int, str], int] = {}
    for item in segments:
        key = (item.camera_index, item.session_id)
        started[key] = min(started.get(key, item.start_ms), item.start_ms)
    claimed: dict[int, list[tuple[int, int]]] = {}
    kept = []
    for key in sorted(started, key=lambda key: (started[key], key[1]), reverse=True):
        taken = claimed.setdefault(key[0], [])
        mine = []
        for item in segments:
            if (item.camera_index, item.session_id) != key:
                continue
            overlap = [(max(item.start_ms, left), min(item.end_ms, right)) for left, right in taken
                       if min(item.end_ms, right) - max(item.start_ms, left) > SESSION_OVERLAP_TOLERANCE_MS]
            if overlap:
                excluded = tuple(sorted(set(item.excluded_ranges) | set(overlap)))
                if next(uncovered_ranges(item.start_ms, item.end_ms, excluded), None) is None:
                    continue
                item = replace(item, excluded_ranges=excluded)
            mine.append(item)
        kept.extend(mine)
        spans = sorted([*taken, *(span for item in mine
                                  for span in uncovered_ranges(item.start_ms, item.end_ms, item.excluded_ranges))])
        taken.clear()
        for left, right in spans:
            if taken and left <= taken[-1][1]:
                taken[-1] = (taken[-1][0], max(taken[-1][1], right))
            else:
                taken.append((left, right))
    return kept


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _deletion_marks(root: Path) -> tuple[dict, ...]:
    path = root / DELETIONS_FILE
    if not path.exists():
        return ()
    try:
        return decode_deletions(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as error:
        raise ValueError(f"无法读取通过记录删除标记：{path}\n{error}") from error


def _applied_deletions(marks, latest: dict) -> tuple[str, ...]:
    """Deletions bound to bib/chip/time apply only while the record is unchanged."""

    applied = []
    for mark in marks:
        event = latest.get(mark["event_id"])
        if event is None:
            continue
        identity = {
            "bib": event.bib.strip(),
            "chip_id": event.chip_id.strip(),
            "passage_timestamp_ms": int(event.timeline_timestamp_ms),
        }
        if all(mark.get(key, value) == value for key, value in identity.items()):
            applied.append(mark["event_id"])
    return tuple(applied)


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
        include_review_segments: bool = False,
        allow_empty_scope: bool = False,
    ):
        self.root = Path(root).expanduser().absolute()
        self.include_review_segments = include_review_segments
        self.allow_empty_scope = bool(allow_empty_scope)
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
        self._latest = {
            event_id: event for event_id, event in latest_events(scoped).items()
        }
        self.deleted_event_ids = _applied_deletions(_deletion_marks(self.root), self._latest)
        self.reads: tuple[ChipRead, ...] = chip_reads(
            scoped, race_id=self.race_id, stage_id=self.stage_id,
            race_date=race_date, deleted_event_ids=self.deleted_event_ids,
        )
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
            # Drawing the finish line before the race is allowed: the geometry
            # belongs to the recording session, not to a chip event.  The race
            # scope is filled in later by the callers that need one.
            if self.allow_empty_scope:
                return "", ""
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

    def archive_segments(self, camera_index: Optional[int] = None, *, for_configuration: bool = False) -> tuple[ArchiveSegment, ...]:
        """Closed long-form recordings of this race, oldest first.

        A recording outside the race clock window belongs to another visit of
        the same folder and stays out: analysing it would invent riders this
        race never had.
        """

        segments = []
        if not (self.root / TIMELINE_JOURNAL).is_file():
            return ()
        store = self.timeline()
        window = race_clock_window(self.reads)
        problem_sessions = {
            (segment.camera_index, session_id_for_path(store.resolve_video_path(segment)))
            for segment in store.segments() if segment.end_reason == "retained_problem_segment"
        }
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
            short_session = session_id_for_path(path)
            short = self.include_review_segments and (
                segment.end_reason == "retained_problem_segment" or (
                    segment.end_reason == "retained_filmstrip_segment"
                    and (for_configuration or (segment.camera_index, short_session) in problem_sessions)
                )
            )
            if path.suffix.lower() not in _VIDEO_SUFFIXES or (not short and "archive" not in path.stem.lower()):
                continue
            key = str(path).lower()
            if key in seen or not path.is_file():
                continue
            seen.add(key)
            session = session_id_for_path(path)
            segments.append(ArchiveSegment(segment, path, session))
        # Prefer retained immutable short segments; never detect a rider twice
        # when their long archive is published. Non-overlapping history remains.
        shorts = [item for item in segments if item.segment.end_reason in ("retained_filmstrip_segment", "retained_problem_segment")]
        if self.include_review_segments and shorts:
            selected = list(shorts)
            for item in segments:
                if item in shorts:
                    continue
                overlap = sorted((max(item.start_ms, other.start_ms), min(item.end_ms, other.end_ms))
                                 for other in shorts if other.camera_index == item.camera_index
                                 and other.start_ms < item.end_ms and other.end_ms > item.start_ms)
                cursor = item.start_ms
                for start, end in overlap:
                    if start > cursor:
                        break
                    cursor = max(cursor, end)
                if cursor < item.end_ms:
                    # A late application start or a lost short segment must not
                    # discard the rest of an otherwise usable archive.
                    selected.append(replace(item, excluded_ranges=tuple(overlap)))
            segments = selected
        segments = _one_recording_at_a_time(segments)
        segments.sort(key=lambda item: (item.camera_index, item.start_ms))
        if window is not None:
            inside = [item for item in segments
                      if item.end_ms > window[0] and item.start_ms < window[1]]
            # A clock that disagrees with the recorder for a whole day would
            # empty the race; an empty race is worse than a doubtful one.
            if inside:
                segments = inside
        return tuple(segments)

    def camera_indexes(self) -> tuple[int, ...]:
        return tuple(sorted({item.camera_index for item in self.archive_segments()}))

    def finish_lines(self) -> dict:
        result = {}
        for name in reversed(FINISH_LINE_FILES):
            payload = _read_json(self.root / name)
            records = payload.get("finish_lines") if isinstance(payload, dict) else payload
            if isinstance(records, list):
                result.update({
                    int(item["camera_index"]): item
                    for item in records
                    if isinstance(item, dict) and "camera_index" in item
                })
        return result

    def manual_anchors(self, camera_index: int) -> tuple[ClockAnchor, ...]:
        """Manual clock calibrations recorded by the existing first-rider workflow.

        A record only counts while its exact anchor event still stands: the
        same event id, still active and not deleted, of the recorded race and
        stage. A record carrying provenance is only used while the event
        revision and original chip time are unchanged — the stored originals
        are compared against the latest event instead of being reconstituted
        from it. Legacy records without provenance fall back to the current
        active event, the way they were recorded.
        """

        path = self.root / CALIBRATION_JOURNAL
        if not path.is_file():
            return ()
        store = VideoClockCalibrationStore(path, recover_incomplete_tail=False)
        anchors = []
        for calibration in store.calibrations():
            if calibration.camera_index != int(camera_index):
                continue
            event = self.latest_event(calibration.anchor_event_id)
            if event is None or not event.is_active or event.timeline_timestamp_ms <= 0:
                continue
            if event.event_id in self.deleted_event_ids:
                continue
            if calibration.anchor_race_id and calibration.anchor_race_id != event.race_id:
                continue
            if calibration.anchor_stage_id and calibration.anchor_stage_id != event.stage_id:
                continue
            if (
                calibration.anchor_event_revision is not None
                and int(calibration.anchor_event_revision) != int(event.revision)
            ):
                continue
            if (
                calibration.anchor_chip_time_ms is not None
                and int(calibration.anchor_chip_time_ms) != int(event.timeline_timestamp_ms)
            ):
                continue
            chip_ms = (
                int(calibration.anchor_chip_time_ms)
                if calibration.anchor_chip_time_ms is not None
                else int(event.timeline_timestamp_ms)
            )
            anchors.append(
                ClockAnchor(
                    camera_index=int(camera_index),
                    chip_ms=chip_ms,
                    offset_ms=int(calibration.offset_ms),
                    kind="manual_calibration",
                    label=calibration.anchor_bib,
                    reference=session_id_for_path(Path(calibration.session_key.split("|", 2)[1]) /
                                                  (calibration.session_key.rsplit("|", 1)[-1] + ".m3u8"))
                    if calibration.session_key.count("|") == 2 else calibration.session_key.rsplit("|", 1)[-1],
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
