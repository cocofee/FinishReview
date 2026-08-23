"""Durable mapping from wall-clock passage times to recorded video segments."""

from __future__ import annotations

import json
import math
import os
import threading
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Optional


SCHEMA_VERSION = 1
DEFAULT_CLOCK_SOURCE = "videopipe_system_clock"
DEFAULT_TIMING_ERROR_MS = 2_000


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
    started_at_ms: int
    ended_at_ms: Optional[int] = None
    media_duration_ms: Optional[int] = None
    media_started_at_ms: Optional[int] = None
    clock_source: str = DEFAULT_CLOCK_SOURCE
    timing_error_ms: int = DEFAULT_TIMING_ERROR_MS
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
    passage_position_ms: int
    playback_position_ms: int
    clock_offset_ms: int
    timing_error_ms: int
    status: str
    media_locator: str = ""


@dataclass(frozen=True, slots=True)
class PassageVideoLookup:
    status: str
    target_time_ms: int
    locations: tuple[PassageVideoLocation, ...] = ()


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

    def __init__(self, journal_path: str | Path):
        self.journal_path = Path(journal_path).expanduser().absolute()
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._segments: dict[str, RecordingSegment] = {}
        self._segment_order: list[str] = []
        self._recovered_incomplete_tail = False
        self._load_existing()

    @property
    def recovered_incomplete_tail(self) -> bool:
        with self._lock:
            return self._recovered_incomplete_tail

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
            updated = replace(
                current,
                ended_at_ms=ended_at_ms,
                media_duration_ms=media_duration_ms,
                media_started_at_ms=media_started_at_ms,
                end_reason=str(end_reason),
            )
            self._segments[current.segment_id] = updated
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
    ) -> PassageVideoLookup:
        target_time_ms = int(passage_time_ms) + int(clock_offset_ms)
        pre_roll_ms = max(0, int(pre_roll_ms))
        now_ms = int(time.time() * 1000.0) if current_time_ms is None else int(current_time_ms)
        eligible_segments = tuple(
            segment
            for segment in self.segments()
            if segment.ended_at_ms is not None
            or segment.clock_source == DEFAULT_CLOCK_SOURCE
        )
        if not eligible_segments:
            return PassageVideoLookup("no_segments", target_time_ms)

        expected_race_id = str(race_id or "").strip()
        segments = tuple(
            segment
            for segment in eligible_segments
            if not expected_race_id
            or segment.race_id == expected_race_id
            or (
                not segment.race_id
                and segment.clock_source == DEFAULT_CLOCK_SOURCE
            )
        )
        if not segments:
            return PassageVideoLookup("race_mismatch", target_time_ms)

        candidates: dict[
            str,
            tuple[tuple[int, int, int, int], RecordingSegment],
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
                    candidate_key = (0, 0, -segment.started_at_ms)
                else:
                    boundary_distance = min(
                        abs(target_time_ms - media_start),
                        abs(target_time_ms - media_end),
                    )
                    if (
                        segment.clock_source != DEFAULT_CLOCK_SOURCE
                        and boundary_distance <= segment.timing_error_ms
                    ):
                        candidate_key = (
                            1,
                            boundary_distance,
                            -segment.started_at_ms,
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
            playable_rank = int(
                not _video_path_is_playable(self.resolve_video_path(segment))
            )
            ranked_candidate_key = (playable_rank, *candidate_key)
            current = candidates.get(segment.source_id)
            if current is None or ranked_candidate_key < current[0]:
                candidates[segment.source_id] = (ranked_candidate_key, segment)

        locations = []
        selected_segments = [item[1] for item in candidates.values()]
        for segment in sorted(selected_segments, key=lambda item: item.camera_index):
            video_path = self.resolve_video_path(segment)
            if segment.ended_at_ms is None:
                status = "recording"
            elif not _video_path_is_playable(video_path):
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
                    segment.clock_source != DEFAULT_CLOCK_SOURCE
                    and min(
                        abs(target_time_ms - segment.media_started_at_ms),
                        abs(target_time_ms - media_end_at_ms),
                    )
                    <= segment.timing_error_ms
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

        earliest_start = min(
            item.media_started_at_ms
            if item.media_started_at_ms is not None
            else item.started_at_ms
            for item in segments
        )
        if target_time_ms < earliest_start:
            status = "before_recording"
        else:
            latest_end = max(
                (
                    item.media_started_at_ms + item.media_duration_ms
                    if item.media_started_at_ms is not None
                    and item.media_duration_ms is not None
                    else item.ended_at_ms
                    if item.ended_at_ms is not None
                    else now_ms
                )
                for item in segments
            )
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
