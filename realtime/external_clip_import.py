"""Import timestamped third-party clips into the race video timeline."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from realtime.passage_receiver import PassageEventStore, PassageJournalError
    from realtime.video_timeline import (
        RecordingSegment,
        VideoTimelineError,
        VideoTimelineStore,
        probe_video_duration_ms,
    )
else:
    from .passage_receiver import PassageEventStore, PassageJournalError
    from .video_timeline import (
        RecordingSegment,
        VideoTimelineError,
        VideoTimelineStore,
        probe_video_duration_ms,
    )


SIDECAR_SCHEMA_VERSION = 1
BEIJING_TIMEZONE = timezone(timedelta(hours=8))
EXTERNAL_CLOCK_SOURCE = "external_clip_sidecar_beijing"
EXTERNAL_END_REASON = "external_clip_import"


class ExternalClipImportError(VideoTimelineError):
    """Raised when an external clip sidecar cannot be imported safely."""


class ExternalClipImportCancelled(ExternalClipImportError):
    """Raised when an operator cancels media verification."""


@dataclass(frozen=True, slots=True)
class ExternalClip:
    video_path: Path
    source_id: str
    camera_index: int
    capture_timestamp: str
    timestamp_anchor: str
    timing_error_ms: int
    media_duration_ms: int
    media_started_at_ms: int
    media_ended_at_ms: int
    race_id: str


@dataclass(frozen=True, slots=True)
class ExternalClipImportResult:
    race_id: str
    created_count: int
    repaired_count: int
    duplicate_count: int
    segments: tuple[RecordingSegment, ...]


def _required_string(payload: Mapping[str, Any], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ExternalClipImportError(f"{name} must be a non-empty string")
    return value.strip()


def _required_integer(payload: Mapping[str, Any], name: str) -> int:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ExternalClipImportError(f"{name} must be an integer")
    return value


def _beijing_timestamp_ms(value: str) -> int:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ExternalClipImportError(
            "capture_timestamp must be an ISO 8601 timestamp"
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ExternalClipImportError(
            "capture_timestamp must include the Beijing +08:00 offset"
        )
    if parsed.utcoffset() != BEIJING_TIMEZONE.utcoffset(None):
        raise ExternalClipImportError(
            "capture_timestamp must use the Beijing +08:00 offset"
        )
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = parsed.astimezone(timezone.utc) - epoch
    return (
        delta.days * 86_400_000
        + delta.seconds * 1_000
        + delta.microseconds // 1_000
    )


def _absolute_path(path: Path) -> Path:
    return path.expanduser().absolute()


def _path_key(path: Path) -> str:
    return os.path.normcase(str(_absolute_path(path)))


def load_external_clip_sidecar(
    sidecar_path: str | Path,
    *,
    expected_race_id: str,
    duration_probe: Optional[Callable[[Path], Optional[int]]] = None,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> tuple[ExternalClip, ...]:
    """Validate a JSON sidecar and probe every referenced clip before import."""
    sidecar = _absolute_path(Path(sidecar_path))
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
    except OSError as error:
        raise ExternalClipImportError(f"failed to read sidecar: {sidecar}") from error
    except json.JSONDecodeError as error:
        raise ExternalClipImportError(f"invalid sidecar JSON: {error}") from error

    if not isinstance(payload, Mapping):
        raise ExternalClipImportError("sidecar root must be an object")
    if _required_integer(payload, "schema_version") != SIDECAR_SCHEMA_VERSION:
        raise ExternalClipImportError("unsupported sidecar schema_version")
    sidecar_race_id = _required_string(payload, "race_id")
    expected_race_id = str(expected_race_id).strip()
    if not expected_race_id:
        raise ExternalClipImportError("expected race_id is required")
    if sidecar_race_id != expected_race_id:
        raise ExternalClipImportError(
            "sidecar race_id does not match the active CycleRace race"
        )
    clip_payloads = payload.get("clips")
    if not isinstance(clip_payloads, list):
        raise ExternalClipImportError("clips must be an array")

    probe = probe_video_duration_ms if duration_probe is None else duration_probe
    clips: list[ExternalClip] = []
    seen_paths: set[str] = set()
    for index, item in enumerate(clip_payloads):
        if cancel_check is not None and cancel_check():
            raise ExternalClipImportCancelled("clip import cancelled")
        if not isinstance(item, Mapping):
            raise ExternalClipImportError(f"clips[{index}] must be an object")
        try:
            raw_path = _required_string(item, "video_path")
            source_id = _required_string(item, "source_id")
            camera_index = _required_integer(item, "camera_index")
            capture_timestamp = _required_string(item, "capture_timestamp")
            timestamp_anchor = _required_string(item, "timestamp_anchor")
            timing_error_ms = _required_integer(item, "timing_error_ms")
            if camera_index <= 0:
                raise ExternalClipImportError("camera_index must be positive")
            if timestamp_anchor not in {"start", "end"}:
                raise ExternalClipImportError(
                    "timestamp_anchor must be either 'start' or 'end'"
                )
            if timing_error_ms < 0:
                raise ExternalClipImportError(
                    "timing_error_ms must be non-negative"
                )
            capture_timestamp_ms = _beijing_timestamp_ms(capture_timestamp)
            video_path = Path(raw_path)
            if not video_path.is_absolute():
                video_path = sidecar.parent / video_path
            video_path = _absolute_path(video_path)
            path_key = _path_key(video_path)
            if path_key in seen_paths:
                raise ExternalClipImportError(
                    f"duplicate video_path in sidecar: {video_path}"
                )
            seen_paths.add(path_key)
            if not video_path.is_file() or video_path.stat().st_size <= 0:
                raise ExternalClipImportError(
                    f"video file is missing or empty: {video_path}"
                )
            media_duration_ms = probe(video_path)
            if media_duration_ms is None or int(media_duration_ms) <= 0:
                raise ExternalClipImportError(
                    f"failed to verify media duration: {video_path}"
                )
            media_duration_ms = int(media_duration_ms)
            media_started_at_ms = (
                capture_timestamp_ms
                if timestamp_anchor == "start"
                else capture_timestamp_ms - media_duration_ms
            )
            if media_started_at_ms < 0:
                raise ExternalClipImportError(
                    f"derived media start is invalid: {video_path}"
                )
        except ExternalClipImportError as error:
            raise ExternalClipImportError(f"clips[{index}]: {error}") from error

        clips.append(
            ExternalClip(
                video_path=video_path,
                source_id=source_id,
                camera_index=camera_index,
                capture_timestamp=capture_timestamp,
                timestamp_anchor=timestamp_anchor,
                timing_error_ms=timing_error_ms,
                media_duration_ms=media_duration_ms,
                media_started_at_ms=media_started_at_ms,
                media_ended_at_ms=media_started_at_ms + media_duration_ms,
                race_id=sidecar_race_id,
            )
        )
        if progress_callback is not None:
            progress_callback(index + 1, len(clip_payloads), str(video_path))
    return tuple(clips)


def race_id_from_passage_store(passage_store: PassageEventStore) -> str:
    race_ids = {event.race_id.strip() for event in passage_store.events()}
    race_ids.discard("")
    if not race_ids:
        raise ExternalClipImportError(
            "at least one CycleRace passage is required before clip import"
        )
    if len(race_ids) != 1:
        raise ExternalClipImportError(
            "the active passage journal contains multiple CycleRace race_id values"
        )
    return next(iter(race_ids))


def _validate_source_camera_mapping(
    timeline_store: VideoTimelineStore,
    clips: tuple[ExternalClip, ...],
) -> None:
    source_to_camera: dict[str, int] = {}
    camera_to_source: dict[int, str] = {}

    def register(source_id: str, camera_index: int, context: str) -> None:
        mapped_camera = source_to_camera.get(source_id)
        if mapped_camera is not None and mapped_camera != camera_index:
            raise ExternalClipImportError(
                f"source_id maps to multiple camera_index values: {context}"
            )
        mapped_source = camera_to_source.get(camera_index)
        if mapped_source is not None and mapped_source != source_id:
            raise ExternalClipImportError(
                f"camera_index maps to multiple source_id values: {context}"
            )
        source_to_camera[source_id] = camera_index
        camera_to_source[camera_index] = source_id

    for segment in timeline_store.segments():
        if segment.clock_source == EXTERNAL_CLOCK_SOURCE:
            register(segment.source_id, segment.camera_index, segment.video_path)
    for clip in clips:
        register(clip.source_id, clip.camera_index, str(clip.video_path))


def _completed_segment_matches(segment: RecordingSegment, clip: ExternalClip) -> bool:
    return (
        segment.race_id == clip.race_id
        and segment.source_id == clip.source_id
        and segment.camera_index == clip.camera_index
        and segment.started_at_ms == clip.media_started_at_ms
        and segment.ended_at_ms == clip.media_ended_at_ms
        and segment.media_started_at_ms == clip.media_started_at_ms
        and segment.media_duration_ms == clip.media_duration_ms
        and segment.clock_source == EXTERNAL_CLOCK_SOURCE
        and segment.timing_error_ms == clip.timing_error_ms
        and segment.end_reason == EXTERNAL_END_REASON
    )


def _open_segment_matches(segment: RecordingSegment, clip: ExternalClip) -> bool:
    return (
        segment.race_id == clip.race_id
        and segment.source_id == clip.source_id
        and segment.camera_index == clip.camera_index
        and segment.started_at_ms == clip.media_started_at_ms
        and segment.ended_at_ms is None
        and segment.media_started_at_ms is None
        and segment.media_duration_ms is None
        and segment.clock_source == EXTERNAL_CLOCK_SOURCE
        and segment.timing_error_ms == clip.timing_error_ms
    )


def import_verified_external_clips(
    timeline_store: VideoTimelineStore,
    clips: Sequence[ExternalClip],
    *,
    expected_race_id: str,
) -> ExternalClipImportResult:
    """Append previously verified clips without probing media on the UI thread."""
    expected_race_id = str(expected_race_id).strip()
    if not expected_race_id:
        raise ExternalClipImportError("expected race_id is required")
    clips = tuple(clips)
    if any(clip.race_id != expected_race_id for clip in clips):
        raise ExternalClipImportError(
            "verified clips do not match the active CycleRace race"
        )
    _validate_source_camera_mapping(timeline_store, clips)
    existing_by_path: dict[str, list[RecordingSegment]] = {}
    for segment in timeline_store.segments():
        path_key = _path_key(timeline_store.resolve_video_path(segment))
        existing_by_path.setdefault(path_key, []).append(segment)

    plan: list[tuple[str, ExternalClip, Optional[RecordingSegment]]] = []
    for clip in clips:
        existing = existing_by_path.get(_path_key(clip.video_path), [])
        if len(existing) > 1:
            raise ExternalClipImportError(
                f"multiple timeline segments already reference: {clip.video_path}"
            )
        if not existing:
            plan.append(("create", clip, None))
            continue
        segment = existing[0]
        if _completed_segment_matches(segment, clip):
            plan.append(("duplicate", clip, segment))
        elif _open_segment_matches(segment, clip):
            plan.append(("repair", clip, segment))
        else:
            raise ExternalClipImportError(
                f"timeline metadata conflicts with sidecar: {clip.video_path}"
            )

    created_count = 0
    repaired_count = 0
    duplicate_count = 0
    imported_segments: list[RecordingSegment] = []
    for action, clip, existing in plan:
        if action == "duplicate":
            if existing is None:
                raise ExternalClipImportError("invalid duplicate import plan")
            duplicate_count += 1
            imported_segments.append(existing)
            continue
        if action == "create":
            segment = timeline_store.add_completed_segment(
                source_id=clip.source_id,
                camera_index=clip.camera_index,
                video_path=clip.video_path,
                media_started_at_ms=clip.media_started_at_ms,
                media_duration_ms=clip.media_duration_ms,
                clock_source=EXTERNAL_CLOCK_SOURCE,
                timing_error_ms=clip.timing_error_ms,
                end_reason=EXTERNAL_END_REASON,
                race_id=clip.race_id,
            )
            created_count += 1
            imported_segments.append(segment)
            continue
        else:
            segment = existing
            repaired_count += 1
        if segment is None:
            raise ExternalClipImportError("invalid import plan")
        imported_segments.append(
            timeline_store.finish_segment(
                segment.segment_id,
                ended_at_ms=clip.media_ended_at_ms,
                end_reason=EXTERNAL_END_REASON,
                media_duration_ms=clip.media_duration_ms,
                media_started_at_ms=clip.media_started_at_ms,
            )
        )

    return ExternalClipImportResult(
        race_id=expected_race_id,
        created_count=created_count,
        repaired_count=repaired_count,
        duplicate_count=duplicate_count,
        segments=tuple(imported_segments),
    )


def import_external_clip_sidecar(
    timeline_store: VideoTimelineStore,
    sidecar_path: str | Path,
    *,
    expected_race_id: str,
    duration_probe: Optional[Callable[[Path], Optional[int]]] = None,
) -> ExternalClipImportResult:
    """Verify and import clips without changing or copying the source media."""
    clips = load_external_clip_sidecar(
        sidecar_path,
        expected_race_id=expected_race_id,
        duration_probe=duration_probe,
    )
    return import_verified_external_clips(
        timeline_store,
        clips,
        expected_race_id=expected_race_id,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Import timestamped third-party clips into a FinishReview timeline."
    )
    parser.add_argument("sidecar", type=Path, help="JSON sidecar path")
    parser.add_argument("timeline", type=Path, help="video_timeline.jsonl path")
    args = parser.parse_args(argv)
    try:
        timeline_path = args.timeline.expanduser().absolute()
        passage_store = PassageEventStore(
            timeline_path.parent / "cyclerace_passage_events.jsonl"
        )
        expected_race_id = race_id_from_passage_store(passage_store)
        result = import_external_clip_sidecar(
            VideoTimelineStore(timeline_path),
            args.sidecar,
            expected_race_id=expected_race_id,
        )
    except (PassageJournalError, VideoTimelineError) as error:
        parser.exit(2, f"error: {error}\n")
    print(
        json.dumps(
            {
                "race_id": result.race_id,
                "created": result.created_count,
                "repaired": result.repaired_count,
                "duplicates": result.duplicate_count,
            },
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BEIJING_TIMEZONE",
    "EXTERNAL_CLOCK_SOURCE",
    "EXTERNAL_END_REASON",
    "ExternalClip",
    "ExternalClipImportCancelled",
    "ExternalClipImportError",
    "ExternalClipImportResult",
    "import_external_clip_sidecar",
    "import_verified_external_clips",
    "load_external_clip_sidecar",
    "main",
    "race_id_from_passage_store",
]
