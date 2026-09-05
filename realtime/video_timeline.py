"""Durable mapping from wall-clock passage times to recorded video segments."""

from __future__ import annotations

import json
import math
import os
import threading
import time
import uuid
from bisect import bisect_right
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Optional

from .time_domain import (
    ClockOffsetMs,
    DurationMs,
    MediaPositionMs,
    WallClockMs,
)


SCHEMA_VERSION = 1
DEFAULT_CLOCK_SOURCE = "videopipe_system_clock"
DEFAULT_TIMING_ERROR_MS = 2_000
_PLAYABILITY_CACHE_TTL_SECONDS = 2.0


class VideoTimelineError(RuntimeError):
    """Raised when the recording timeline cannot be read or updated."""


def _video_path_is_playable(video_path: Path) -> bool:
    try:
        if not video_path.is_file() or video_path.stat().st_size <= 0:
            return False
        if video_path.suffix.lower() != ".m3u8":
            return True
        lines = video_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False

    media_paths = [
        line.strip()
        for line in lines
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not media_paths:
        return False
    for value in media_paths:
        segment_path = Path(value)
        if not segment_path.is_absolute():
            segment_path = video_path.parent / segment_path
        try:
            if not segment_path.is_file() or segment_path.stat().st_size <= 0:
                return False
        except OSError:
            return False
    return True


@dataclass(frozen=True, slots=True)
class RecordingSegment:
    segment_id: str
    source_id: str
    camera_index: int
    video_path: str
    started_at_ms: WallClockMs
    ended_at_ms: Optional[WallClockMs] = None
    media_duration_ms: Optional[DurationMs] = None
    media_started_at_ms: Optional[WallClockMs] = None
    clock_source: str = DEFAULT_CLOCK_SOURCE
    timing_error_ms: DurationMs = DurationMs(DEFAULT_TIMING_ERROR_MS)
    end_reason: str = ""
    race_id: str = ""

    def __post_init__(self) -> None:
        if not self.segment_id.strip():
            raise VideoTimelineError("segment_id is required")
        if not self.source_id.strip():
            raise VideoTimelineError("source_id is required")
        if self.camera_index <= 0:
            raise VideoTimelineError("camera_index must be positive")
        if not self.video_path.strip():
            raise VideoTimelineError("video_path is required")
        if self.started_at_ms < 0:
            raise VideoTimelineError("started_at_ms must be non-negative")
        if self.ended_at_ms is not None and self.ended_at_ms < self.started_at_ms:
            raise VideoTimelineError("ended_at_ms cannot precede started_at_ms")
        if self.media_duration_ms is not None and self.media_duration_ms <= 0:
            raise VideoTimelineError("media_duration_ms must be positive")
        if self.media_started_at_ms is not None and self.media_started_at_ms < 0:
            raise VideoTimelineError("media_started_at_ms must be non-negative")
        if (self.media_duration_ms is None) != (self.media_started_at_ms is None):
            raise VideoTimelineError(
                "media_duration_ms and media_started_at_ms must be provided together"
            )
        if not self.clock_source.strip():
            raise VideoTimelineError("clock_source is required")
        if self.timing_error_ms < 0:
            raise VideoTimelineError("timing_error_ms must be non-negative")
        if not isinstance(self.race_id, str):
            raise VideoTimelineError("race_id must be a string")


@dataclass(frozen=True, slots=True)
class PassageVideoLocation:
    segment: RecordingSegment
    video_path: Path
    passage_position_ms: MediaPositionMs
    playback_position_ms: MediaPositionMs
    clock_offset_ms: ClockOffsetMs
    timing_error_ms: DurationMs
    status: str
    media_locator: str = ""


@dataclass(frozen=True, slots=True)
class PassageVideoLookup:
    status: str
    target_time_ms: WallClockMs
    locations: tuple[PassageVideoLocation, ...] = ()


class _SegmentIntervalIndex:
    """Incremental closed-segment interval index preserving journal order."""

    def __init__(self) -> None:
        self._entries: list[tuple[int, int, int, str]] = []
        self._starts: list[int] = []
        self._prefix_max_ends: list[int] = []

    def add(
        self,
        *,
        started_at_ms: int,
        ended_at_ms: int,
        order: int,
        segment_id: str,
    ) -> None:
        entry = (int(started_at_ms), int(ended_at_ms), int(order), str(segment_id))
        index = bisect_right(self._entries, entry)
        self._entries.insert(index, entry)
        self._starts.insert(index, entry[0])
        previous_max = self._prefix_max_ends[index - 1] if index else -1
        if index == len(self._prefix_max_ends):
            self._prefix_max_ends.append(max(previous_max, entry[1]))
            return
        self._prefix_max_ends.insert(index, max(previous_max, entry[1]))
        for cursor in range(index + 1, len(self._entries)):
            self._prefix_max_ends[cursor] = max(
                self._prefix_max_ends[cursor - 1],
                self._entries[cursor][1],
            )

    def candidates(self, target_time_ms: int) -> tuple[str, ...]:
        cursor = bisect_right(self._starts, int(target_time_ms)) - 1
        segment_ids = []
        while cursor >= 0 and self._prefix_max_ends[cursor] >= target_time_ms:
            entry = self._entries[cursor]
            if entry[1] >= target_time_ms:
                segment_ids.append(entry[3])
            cursor -= 1
        return tuple(segment_ids)


@dataclass(slots=True)
class _ClosedTimelineBounds:
    count: int = 0
    earliest_start_ms: Optional[WallClockMs] = None
    latest_end_ms: Optional[WallClockMs] = None

    def add(self, segment: RecordingSegment) -> None:
        if segment.ended_at_ms is None:
            raise ValueError("closed timeline bounds require a completed segment")
        started_at_ms = (
            segment.media_started_at_ms
            if segment.media_started_at_ms is not None
            else segment.started_at_ms
        )
        ended_at_ms = (
            segment.media_started_at_ms + segment.media_duration_ms
            if segment.media_started_at_ms is not None
            and segment.media_duration_ms is not None
            else segment.ended_at_ms
        )
        self.count += 1
        self.earliest_start_ms = (
            started_at_ms
            if self.earliest_start_ms is None
            else min(self.earliest_start_ms, started_at_ms)
        )
        self.latest_end_ms = (
            ended_at_ms
            if self.latest_end_ms is None
            else max(self.latest_end_ms, ended_at_ms)
        )


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise VideoTimelineError(f"{name} must be an integer")
    return value


def _string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise VideoTimelineError(f"{name} must be a string")
    return value


def _optional_integer(payload: Mapping[str, Any], name: str) -> Optional[int]:
    value = payload.get(name)
    return None if value is None else _integer(value, name)


def probe_video_duration_ms(video_path: str | Path) -> Optional[int]:
    """Return the decoded media duration when OpenCV can verify it."""
    path = Path(video_path)
    try:
        if not path.is_file() or path.stat().st_size <= 0:
            return None
        import cv2
    except (ImportError, OSError):
        return None

    capture = None
    try:
        capture = cv2.VideoCapture(str(path))
        if not capture.isOpened():
            return None
        fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
        frame_count = float(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0)
    except Exception:
        return None
    finally:
        if capture is not None:
            try:
                capture.release()
            except Exception:
                pass
    if not math.isfinite(fps) or not math.isfinite(frame_count):
        return None
    if fps <= 0.0 or frame_count <= 0.0:
        return None
    return max(1, int(round(frame_count * 1000.0 / fps)))


def _looks_like_incomplete_json(value: str) -> bool:
    in_string = False
    escaped = False
    nesting = 0
    for character in value:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            nesting += 1
        elif character in "]}":
            if nesting == 0:
                return False
            nesting -= 1
    return in_string or nesting > 0


class VideoTimelineStore:
    """Append-only recording-segment journal scoped to one race directory."""

    def __init__(
        self,
        journal_path: str | Path,
        *,
        recover_incomplete_tail: bool = True,
    ):
        self.journal_path = Path(journal_path).expanduser().absolute()
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._segments: dict[str, RecordingSegment] = {}
        self._segment_order: list[str] = []
        self._segment_order_index: dict[str, int] = {}
        self._segment_ids_by_race: dict[str, list[str]] = {}
        self._closed_indexes_by_race: dict[str, _SegmentIntervalIndex] = {}
        self._open_segment_ids_by_race: dict[str, set[str]] = {}
        self._closed_bounds_by_race: dict[str, _ClosedTimelineBounds] = {}
        self._all_closed_bounds = _ClosedTimelineBounds()
        self._legacy_default_closed_bounds = _ClosedTimelineBounds()
        self._segments_by_path: dict[str, list[str]] = {}
        self._playability_cache: dict[str, tuple[int, int, float, bool]] = {}
        self._revision = 0
        self._recovered_incomplete_tail = False
        self._recover_incomplete_tail = bool(recover_incomplete_tail)
        self._load_existing()
        self._rebuild_indexes()

    @property
    def recovered_incomplete_tail(self) -> bool:
        with self._lock:
            return self._recovered_incomplete_tail

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    def _load_existing(self) -> None:
        if not self.journal_path.exists():
            return
        try:
            content = self.journal_path.read_bytes()
        except OSError as error:
            raise VideoTimelineError(
                f"failed to read video timeline: {self.journal_path}"
            ) from error

        offset = 0
        lines = content.splitlines(keepends=True)
        for line_number, raw_line in enumerate(lines, start=1):
            terminated = raw_line.endswith(b"\n") or raw_line.endswith(b"\r")
            stripped = raw_line.rstrip(b"\r\n")
            if not stripped:
                offset += len(raw_line)
                continue
            try:
                payload = json.loads(stripped.decode("utf-8"))
                self._merge_record(payload)
            except (UnicodeDecodeError, json.JSONDecodeError, VideoTimelineError) as error:
                is_tail = line_number == len(lines) and not terminated
                candidate = ""
                if is_tail:
                    try:
                        candidate = stripped.decode("utf-8")
                    except UnicodeDecodeError:
                        pass
                if candidate and _looks_like_incomplete_json(candidate):
                    if self._recover_incomplete_tail:
                        self._truncate(offset)
                    self._recovered_incomplete_tail = True
                    return
                raise VideoTimelineError(
                    f"invalid video timeline line {line_number}: {error}"
                ) from error
            offset += len(raw_line)

    def _merge_record(self, payload: Mapping[str, Any]) -> None:
        if not isinstance(payload, Mapping):
            raise VideoTimelineError("video timeline record must be an object")
        if _integer(payload.get("schema_version"), "schema_version") != SCHEMA_VERSION:
            raise VideoTimelineError("unsupported video timeline schema_version")
        record_type = _string(payload.get("record_type"), "record_type")
        if record_type == "segment_started":
            segment = RecordingSegment(
                segment_id=_string(payload.get("segment_id"), "segment_id"),
                source_id=_string(payload.get("source_id"), "source_id"),
                camera_index=_integer(payload.get("camera_index"), "camera_index"),
                video_path=_string(payload.get("video_path"), "video_path"),
                started_at_ms=_integer(payload.get("started_at_ms"), "started_at_ms"),
                clock_source=_string(payload.get("clock_source"), "clock_source"),
                timing_error_ms=_integer(
                    payload.get("timing_error_ms"), "timing_error_ms"
                ),
                race_id=_string(payload.get("race_id", ""), "race_id"),
            )
            if segment.segment_id in self._segments:
                raise VideoTimelineError(
                    f"duplicate segment_started record: {segment.segment_id}"
                )
            self._segments[segment.segment_id] = segment
            self._segment_order.append(segment.segment_id)
            self._revision += 1
            return
        if record_type == "segment_ended":
            segment_id = _string(payload.get("segment_id"), "segment_id")
            current = self._segments.get(segment_id)
            if current is None:
                raise VideoTimelineError(f"unknown segment_id: {segment_id}")
            if current.ended_at_ms is not None:
                raise VideoTimelineError(f"duplicate segment_ended record: {segment_id}")
            ended_at_ms = _integer(payload.get("ended_at_ms"), "ended_at_ms")
            media_duration_ms = _optional_integer(payload, "media_duration_ms")
            media_started_at_ms = _optional_integer(payload, "media_started_at_ms")
            if media_duration_ms is not None and media_started_at_ms is None:
                media_started_at_ms = max(0, ended_at_ms - media_duration_ms)
            self._segments[segment_id] = replace(
                current,
                ended_at_ms=ended_at_ms,
                media_duration_ms=media_duration_ms,
                media_started_at_ms=media_started_at_ms,
                end_reason=_string(payload.get("end_reason", ""), "end_reason"),
            )
            self._revision += 1
            return
        raise VideoTimelineError(f"unsupported video timeline record_type: {record_type}")

    def _truncate(self, size: int) -> None:
        try:
            with self.journal_path.open("r+b") as journal:
                journal.truncate(size)
                journal.flush()
                os.fsync(journal.fileno())
        except OSError as error:
            raise VideoTimelineError(
                f"failed to recover video timeline: {self.journal_path}"
            ) from error

    def _append_records(self, payloads: tuple[Mapping[str, Any], ...]) -> None:
        records = b"".join(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
            + b"\n"
            for payload in payloads
        )
        if not records:
            return
        original_size = (
            self.journal_path.stat().st_size if self.journal_path.exists() else 0
        )
        separator = b""
        if original_size:
            try:
                with self.journal_path.open("rb") as journal:
                    journal.seek(-1, os.SEEK_END)
                    if journal.read(1) not in {b"\n", b"\r"}:
                        separator = b"\n"
            except OSError as error:
                raise VideoTimelineError(
                    f"failed to inspect video timeline: {self.journal_path}"
                ) from error
        try:
            with self.journal_path.open("ab") as journal:
                journal.write(separator + records)
                journal.flush()
                os.fsync(journal.fileno())
        except OSError as error:
            try:
                self._truncate(original_size)
            except VideoTimelineError:
                pass
            raise VideoTimelineError(
                f"failed to append video timeline: {self.journal_path}"
            ) from error

    def _append_record(self, payload: Mapping[str, Any]) -> None:
        self._append_records((payload,))

    def _portable_video_path(self, video_path: str | Path) -> str:
        resolved = Path(video_path).expanduser().absolute()
        try:
            return resolved.relative_to(self.journal_path.parent).as_posix()
        except ValueError:
            return str(resolved)

    def resolve_video_path(self, segment: RecordingSegment) -> Path:
        path = Path(segment.video_path)
        if path.is_absolute():
            return path
        return (self.journal_path.parent / path).absolute()

    @staticmethod
    def _path_key(path: Path) -> str:
        return os.path.normcase(str(path.absolute()))

    @staticmethod
    def _segment_interval(segment: RecordingSegment) -> tuple[int, int] | None:
        if segment.ended_at_ms is None:
            return None
        started_at_ms = segment.started_at_ms
        ended_at_ms = segment.ended_at_ms
        if (
            segment.media_started_at_ms is not None
            and segment.media_duration_ms is not None
        ):
            media_started_at_ms = segment.media_started_at_ms
            media_ended_at_ms = media_started_at_ms + segment.media_duration_ms
            boundary_error_ms = (
                segment.timing_error_ms
                if segment.clock_source != DEFAULT_CLOCK_SOURCE
                else 0
            )
            started_at_ms = min(
                started_at_ms,
                max(0, media_started_at_ms - boundary_error_ms),
            )
            ended_at_ms = max(
                ended_at_ms,
                media_ended_at_ms + boundary_error_ms,
            )
        return started_at_ms, ended_at_ms

    def _index_segment(self, segment: RecordingSegment, order: int) -> None:
        segment_id = segment.segment_id
        self._segment_order_index[segment_id] = int(order)
        self._segment_ids_by_race.setdefault(segment.race_id, []).append(segment_id)
        path_key = self._path_key(self.resolve_video_path(segment))
        self._segments_by_path.setdefault(path_key, []).append(segment_id)
        interval = self._segment_interval(segment)
        if interval is None:
            self._open_segment_ids_by_race.setdefault(segment.race_id, set()).add(
                segment_id
            )
            return
        index = self._closed_indexes_by_race.setdefault(
            segment.race_id,
            _SegmentIntervalIndex(),
        )
        index.add(
            started_at_ms=interval[0],
            ended_at_ms=interval[1],
            order=order,
            segment_id=segment_id,
        )
        self._index_closed_bounds(segment)

    def _index_closed_bounds(self, segment: RecordingSegment) -> None:
        self._closed_bounds_by_race.setdefault(
            segment.race_id,
            _ClosedTimelineBounds(),
        ).add(segment)
        self._all_closed_bounds.add(segment)
        if not segment.race_id and segment.clock_source == DEFAULT_CLOCK_SOURCE:
            self._legacy_default_closed_bounds.add(segment)

    def _rebuild_indexes(self) -> None:
        self._segment_order_index.clear()
        self._segment_ids_by_race.clear()
        self._closed_indexes_by_race.clear()
        self._open_segment_ids_by_race.clear()
        self._closed_bounds_by_race.clear()
        self._all_closed_bounds = _ClosedTimelineBounds()
        self._legacy_default_closed_bounds = _ClosedTimelineBounds()
        self._segments_by_path.clear()
        for order, segment_id in enumerate(self._segment_order):
            self._index_segment(self._segments[segment_id], order)

    def find_segment_by_video_path(
        self,
        video_path: str | Path,
    ) -> RecordingSegment | None:
        path_key = self._path_key(Path(video_path).expanduser().absolute())
        with self._lock:
            segment_ids = self._segments_by_path.get(path_key, ())
            return self._segments[segment_ids[-1]] if segment_ids else None

    def get_segment(self, segment_id: str) -> RecordingSegment | None:
        with self._lock:
            return self._segments.get(str(segment_id))

    def video_path_is_playable(self, video_path: str | Path) -> bool:
        return self._video_path_is_playable(Path(video_path).expanduser().absolute())

    def _video_path_is_playable(self, video_path: Path) -> bool:
        path_key = self._path_key(video_path)
        now = time.monotonic()
        cached = self._playability_cache.get(path_key)
        if cached is not None and now - cached[2] <= _PLAYABILITY_CACHE_TTL_SECONDS:
            return cached[3]
        try:
            stat = video_path.stat()
            signature = (int(stat.st_size), int(stat.st_mtime_ns))
        except OSError:
            signature = (-1, -1)
        playable = _video_path_is_playable(video_path)
        self._playability_cache[path_key] = (*signature, now, playable)
        return playable

    def _candidate_segment_ids(
        self,
        target_time_ms: int,
        expected_race_id: str,
    ) -> tuple[str, ...]:
        if expected_race_id:
            race_keys = (expected_race_id, "")
        else:
            race_keys = tuple(self._segment_ids_by_race)
        segment_ids: set[str] = set()
        for race_key in race_keys:
            index = self._closed_indexes_by_race.get(race_key)
            if index is not None:
                segment_ids.update(index.candidates(target_time_ms))
            segment_ids.update(self._open_segment_ids_by_race.get(race_key, ()))
        return tuple(
            sorted(
                segment_ids,
                key=lambda segment_id: self._segment_order_index[segment_id],
            )
        )

    @staticmethod
    def _segment_is_relevant(
        segment: RecordingSegment,
        expected_race_id: str,
    ) -> bool:
        if segment.ended_at_ms is None and segment.clock_source != DEFAULT_CLOCK_SOURCE:
            return False
        if not expected_race_id:
            return True
        return segment.race_id == expected_race_id or (
            not segment.race_id and segment.clock_source == DEFAULT_CLOCK_SOURCE
        )

    def _relevant_timeline_bounds(
        self,
        expected_race_id: str,
        now_ms: int,
    ) -> tuple[int, Optional[int], Optional[int]]:
        if expected_race_id:
            closed_bounds = (
                self._closed_bounds_by_race.get(expected_race_id),
                self._legacy_default_closed_bounds,
            )
            open_race_ids = (expected_race_id, "")
        else:
            closed_bounds = (self._all_closed_bounds,)
            open_race_ids = tuple(self._open_segment_ids_by_race)

        count = 0
        earliest_start_ms: Optional[int] = None
        latest_end_ms: Optional[int] = None
        for bounds in closed_bounds:
            if bounds is None or not bounds.count:
                continue
            count += bounds.count
            if bounds.earliest_start_ms is not None:
                earliest_start_ms = (
                    bounds.earliest_start_ms
                    if earliest_start_ms is None
                    else min(earliest_start_ms, bounds.earliest_start_ms)
                )
            if bounds.latest_end_ms is not None:
                latest_end_ms = (
                    bounds.latest_end_ms
                    if latest_end_ms is None
                    else max(latest_end_ms, bounds.latest_end_ms)
                )

        for race_id in open_race_ids:
            for segment_id in self._open_segment_ids_by_race.get(race_id, ()):
                segment = self._segments[segment_id]
                if not self._segment_is_relevant(segment, expected_race_id):
                    continue
                count += 1
                earliest_start_ms = (
                    segment.started_at_ms
                    if earliest_start_ms is None
                    else min(earliest_start_ms, segment.started_at_ms)
                )
                latest_end_ms = (
                    now_ms
                    if latest_end_ms is None
                    else max(latest_end_ms, now_ms)
                )
        return count, earliest_start_ms, latest_end_ms

    def _has_any_eligible_segment(self) -> bool:
        if self._all_closed_bounds.count:
            return True
        return any(
            self._segments[segment_id].clock_source == DEFAULT_CLOCK_SOURCE
            for segment_ids in self._open_segment_ids_by_race.values()
            for segment_id in segment_ids
        )

    def start_segment(
        self,
        *,
        source_id: str,
        camera_index: int,
        video_path: str | Path,
        started_at_ms: int,
        clock_source: str = DEFAULT_CLOCK_SOURCE,
        timing_error_ms: int = DEFAULT_TIMING_ERROR_MS,
        race_id: str = "",
    ) -> RecordingSegment:
        segment = RecordingSegment(
            segment_id=uuid.uuid4().hex,
            source_id=str(source_id),
            camera_index=int(camera_index),
            video_path=self._portable_video_path(video_path),
            started_at_ms=int(started_at_ms),
            clock_source=str(clock_source),
            timing_error_ms=int(timing_error_ms),
            race_id=str(race_id),
        )
        payload = {
            "schema_version": SCHEMA_VERSION,
            "record_type": "segment_started",
            "segment_id": segment.segment_id,
            "source_id": segment.source_id,
            "camera_index": segment.camera_index,
            "video_path": segment.video_path,
            "started_at_ms": segment.started_at_ms,
            "clock_source": segment.clock_source,
            "timing_error_ms": segment.timing_error_ms,
            "race_id": segment.race_id,
        }
        with self._lock:
            self._append_record(payload)
            self._segments[segment.segment_id] = segment
            self._segment_order.append(segment.segment_id)
            self._index_segment(segment, len(self._segment_order) - 1)
            self._revision += 1
        return segment

    def finish_segment(
        self,
        segment_id: str,
        *,
        ended_at_ms: int,
        end_reason: str = "stopped",
        media_duration_ms: Optional[int] = None,
        media_started_at_ms: Optional[int] = None,
    ) -> RecordingSegment:
        with self._lock:
            current = self._segments.get(str(segment_id))
            if current is None:
                raise VideoTimelineError(f"unknown segment_id: {segment_id}")
            if current.ended_at_ms is not None:
                return current
            ended_at_ms = max(current.started_at_ms, int(ended_at_ms))
            if media_duration_ms is not None:
                media_duration_ms = int(media_duration_ms)
                if media_started_at_ms is None:
                    media_started_at_ms = max(0, ended_at_ms - media_duration_ms)
            elif media_started_at_ms is not None:
                raise VideoTimelineError(
                    "media_started_at_ms requires media_duration_ms"
                )
            updated = replace(
                current,
                ended_at_ms=ended_at_ms,
                media_duration_ms=media_duration_ms,
                media_started_at_ms=media_started_at_ms,
                end_reason=str(end_reason),
            )
            payload = {
                "schema_version": SCHEMA_VERSION,
                "record_type": "segment_ended",
                "segment_id": current.segment_id,
                "ended_at_ms": ended_at_ms,
                "end_reason": str(end_reason),
            }
            if media_duration_ms is not None:
                payload["media_duration_ms"] = media_duration_ms
                payload["media_started_at_ms"] = int(media_started_at_ms)
            self._append_record(payload)
            self._segments[current.segment_id] = updated
            self._open_segment_ids_by_race.get(current.race_id, set()).discard(
                current.segment_id
            )
            interval = self._segment_interval(updated)
            if interval is not None:
                index = self._closed_indexes_by_race.setdefault(
                    updated.race_id,
                    _SegmentIntervalIndex(),
                )
                index.add(
                    started_at_ms=interval[0],
                    ended_at_ms=interval[1],
                    order=self._segment_order_index[updated.segment_id],
                    segment_id=updated.segment_id,
                )
                self._index_closed_bounds(updated)
            self._revision += 1
            return updated

    def add_completed_segment(
        self,
        *,
        source_id: str,
        camera_index: int,
        video_path: str | Path,
        media_started_at_ms: int,
        media_duration_ms: int,
        clock_source: str,
        timing_error_ms: int,
        end_reason: str,
        race_id: str = "",
    ) -> RecordingSegment:
        """Append a complete external segment as one journal write."""
        media_started_at_ms = int(media_started_at_ms)
        media_duration_ms = int(media_duration_ms)
        media_ended_at_ms = media_started_at_ms + media_duration_ms
        segment = RecordingSegment(
            segment_id=uuid.uuid4().hex,
            source_id=str(source_id),
            camera_index=int(camera_index),
            video_path=self._portable_video_path(video_path),
            started_at_ms=media_started_at_ms,
            ended_at_ms=media_ended_at_ms,
            media_duration_ms=media_duration_ms,
            media_started_at_ms=media_started_at_ms,
            clock_source=str(clock_source),
            timing_error_ms=int(timing_error_ms),
            end_reason=str(end_reason),
            race_id=str(race_id),
        )
        started_payload = {
            "schema_version": SCHEMA_VERSION,
            "record_type": "segment_started",
            "segment_id": segment.segment_id,
            "source_id": segment.source_id,
            "camera_index": segment.camera_index,
            "video_path": segment.video_path,
            "started_at_ms": segment.started_at_ms,
            "clock_source": segment.clock_source,
            "timing_error_ms": segment.timing_error_ms,
            "race_id": segment.race_id,
        }
        ended_payload = {
            "schema_version": SCHEMA_VERSION,
            "record_type": "segment_ended",
            "segment_id": segment.segment_id,
            "ended_at_ms": media_ended_at_ms,
            "end_reason": segment.end_reason,
            "media_duration_ms": media_duration_ms,
            "media_started_at_ms": media_started_at_ms,
        }
        with self._lock:
            self._append_records((started_payload, ended_payload))
            self._segments[segment.segment_id] = segment
            self._segment_order.append(segment.segment_id)
            self._index_segment(segment, len(self._segment_order) - 1)
            self._revision += 2
        return segment

    def segments(self) -> tuple[RecordingSegment, ...]:
        with self._lock:
            return tuple(self._segments[item] for item in self._segment_order)

    def recover_open_segments(self) -> int:
        """Close segments left open by a previous FinishReview process."""
        open_segments = [
            segment
            for segment in self.segments()
            if segment.ended_at_ms is None
            and segment.clock_source == DEFAULT_CLOCK_SOURCE
        ]
        recovered = 0
        for segment in open_segments:
            video_path = self.resolve_video_path(segment)
            try:
                ended_at_ms = int(round(video_path.stat().st_mtime * 1000.0))
            except OSError:
                ended_at_ms = segment.started_at_ms
            media_duration_ms = probe_video_duration_ms(video_path)
            self.finish_segment(
                segment.segment_id,
                ended_at_ms=max(segment.started_at_ms, ended_at_ms),
                end_reason="recovered_after_restart",
                media_duration_ms=media_duration_ms,
            )
            recovered += 1
        return recovered

    def locate_passage(
        self,
        passage_time_ms: int,
        *,
        clock_offset_ms: int = 0,
        pre_roll_ms: int = 3_000,
        current_time_ms: Optional[int] = None,
        race_id: Optional[str] = None,
        prefer_continuous_media: bool = False,
    ) -> PassageVideoLookup:
        target_time_ms = int(passage_time_ms) + int(clock_offset_ms)
        pre_roll_ms = max(0, int(pre_roll_ms))
        now_ms = int(time.time() * 1000.0) if current_time_ms is None else int(current_time_ms)
        expected_race_id = str(race_id or "").strip()
        with self._lock:
            candidate_ids = set(
                self._candidate_segment_ids(target_time_ms, expected_race_id)
            )
            candidate_ids.update(
                self._candidate_segment_ids(
                    target_time_ms - 5_000,
                    expected_race_id,
                )
            )
            segments = tuple(
                self._segments[segment_id]
                for segment_id in sorted(
                    candidate_ids,
                    key=lambda value: self._segment_order_index[value],
                )
                if self._segment_is_relevant(
                    self._segments[segment_id],
                    expected_race_id,
                )
            )
            eligible_count, earliest_start, latest_end = (
                self._relevant_timeline_bounds(expected_race_id, now_ms)
            )
            any_eligible = self._has_any_eligible_segment()
        if not eligible_count:
            return PassageVideoLookup(
                "race_mismatch" if expected_race_id and any_eligible else "no_segments",
                target_time_ms,
            )

        segments_by_source: dict[tuple[str, int], list[RecordingSegment]] = {}
        for segment in segments:
            segments_by_source.setdefault(
                (segment.source_id, int(segment.camera_index)), []
            ).append(segment)
        split_previous_ids: set[str] = set()
        split_boundary_ids: set[str] = set()
        for source_segments in segments_by_source.values():
            source_segments.sort(key=lambda item: (item.started_at_ms, item.segment_id))
            for previous, following in zip(source_segments, source_segments[1:]):
                previous_end = (
                    previous.media_started_at_ms + previous.media_duration_ms
                    if previous.media_started_at_ms is not None
                    and previous.media_duration_ms is not None
                    else previous.ended_at_ms
                )
                following_start = (
                    following.media_started_at_ms
                    if following.media_started_at_ms is not None
                    else following.started_at_ms
                )
                if previous_end is None or abs(
                    int(following_start) - int(previous_end)
                ) > max(
                    int(previous.timing_error_ms),
                    int(following.timing_error_ms),
                ):
                    continue
                split_previous_ids.add(following.segment_id)
                split_boundary_ids.update((previous.segment_id, following.segment_id))

        candidates: dict[
            str,
            tuple[tuple[int, int, int, int, int], RecordingSegment],
        ] = {}
        for segment in segments:
            process_end = (
                segment.ended_at_ms if segment.ended_at_ms is not None else now_ms
            )
            process_hit = segment.started_at_ms <= target_time_ms <= process_end
            candidate_key: Optional[tuple[int, int, int]] = None
            if (
                segment.media_started_at_ms is not None
                and segment.media_duration_ms is not None
            ):
                media_start = segment.media_started_at_ms
                media_end = media_start + segment.media_duration_ms
                if media_start <= target_time_ms <= media_end:
                    near_split_start = (
                        segment.segment_id in split_previous_ids
                        and 0 < target_time_ms - media_start
                        < min(1_000, int(segment.timing_error_ms))
                    )
                    candidate_key = (
                        (1, target_time_ms - media_start, segment.started_at_ms)
                        if near_split_start
                        else (0, 0, -segment.started_at_ms)
                    )
                else:
                    boundary_distance = min(
                        abs(target_time_ms - media_start),
                        abs(target_time_ms - media_end),
                    )
                    if (
                        boundary_distance <= segment.timing_error_ms
                        and (
                            segment.clock_source != DEFAULT_CLOCK_SOURCE
                            or segment.segment_id in split_boundary_ids
                        )
                    ):
                        candidate_key = (
                            1,
                            boundary_distance,
                            segment.started_at_ms,
                        )
                    elif process_hit:
                        candidate_key = (
                            3,
                            boundary_distance,
                            -segment.started_at_ms,
                        )
            elif process_hit:
                candidate_key = (2, 0, -segment.started_at_ms)
            if candidate_key is None:
                continue
            video_path = self.resolve_video_path(segment)
            playable_rank = int(not self._video_path_is_playable(video_path))
            continuous_rank = int(
                bool(prefer_continuous_media)
                and video_path.suffix.lower() == ".m3u8"
            )
            ranked_candidate_key = (
                playable_rank,
                candidate_key[0],
                continuous_rank,
                *candidate_key[1:],
            )
            current = candidates.get(segment.source_id)
            if current is None or ranked_candidate_key < current[0]:
                candidates[segment.source_id] = (ranked_candidate_key, segment)

        locations = []
        selected_segments = [item[1] for item in candidates.values()]
        for segment in sorted(selected_segments, key=lambda item: item.camera_index):
            video_path = self.resolve_video_path(segment)
            if segment.ended_at_ms is None:
                status = "recording"
            elif not self._video_path_is_playable(video_path):
                status = "missing_file"
            elif (
                segment.media_started_at_ms is None
                or segment.media_duration_ms is None
            ):
                status = "unverified"
            else:
                media_end_at_ms = (
                    segment.media_started_at_ms + segment.media_duration_ms
                )
                if segment.media_started_at_ms <= target_time_ms <= media_end_at_ms:
                    status = "located"
                elif (
                    min(
                        abs(target_time_ms - segment.media_started_at_ms),
                        abs(target_time_ms - media_end_at_ms),
                    )
                    <= segment.timing_error_ms
                    and (
                        segment.clock_source != DEFAULT_CLOCK_SOURCE
                        or segment.segment_id in split_boundary_ids
                    )
                ):
                    status = "near_boundary"
                else:
                    status = "outside_media"
            position_origin_ms = (
                segment.media_started_at_ms
                if segment.media_started_at_ms is not None
                else segment.started_at_ms
            )
            passage_position_ms = max(0, target_time_ms - position_origin_ms)
            if segment.media_duration_ms is not None:
                passage_position_ms = min(
                    passage_position_ms,
                    segment.media_duration_ms,
                )
            locations.append(
                PassageVideoLocation(
                    segment=segment,
                    video_path=video_path,
                    passage_position_ms=passage_position_ms,
                    playback_position_ms=max(0, passage_position_ms - pre_roll_ms),
                    clock_offset_ms=int(clock_offset_ms),
                    timing_error_ms=segment.timing_error_ms,
                    status=status,
                )
            )
        if locations:
            if any(item.status == "located" for item in locations):
                status = "located"
            elif any(item.status == "near_boundary" for item in locations):
                status = "near_boundary"
            elif any(item.status == "unverified" for item in locations):
                status = "unverified"
            elif any(item.status == "recording" for item in locations):
                status = "recording"
            elif any(item.status == "outside_media" for item in locations):
                status = "outside_media"
            else:
                status = "missing_file"
            return PassageVideoLookup(status, target_time_ms, tuple(locations))

        assert earliest_start is not None
        assert latest_end is not None
        if target_time_ms < earliest_start:
            status = "before_recording"
        else:
            status = "after_recording" if target_time_ms > latest_end else "recording_gap"
        return PassageVideoLookup(status, target_time_ms)


__all__ = [
    "DEFAULT_CLOCK_SOURCE",
    "DEFAULT_TIMING_ERROR_MS",
    "PassageVideoLocation",
    "PassageVideoLookup",
    "RecordingSegment",
    "VideoTimelineError",
    "VideoTimelineStore",
    "probe_video_duration_ms",
]
