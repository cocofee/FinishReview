"""Explicit millisecond domains used at video and timing boundaries.

The aliases intentionally remain ``int`` at runtime. They therefore preserve
Qt signal compatibility and the existing JSONL schema while allowing static
analysis to reject accidental arithmetic between wall-clock timestamps, media
positions, durations, and clock offsets.

Time contract for the finish problem list:

- chip time (:data:`ChipClockMs`) is the CycleRace reader clock and the only
  official timing domain;
- recorder time (:data:`RecorderClockMs`) is the FinishReview recording clock,
  i.e. a segment's media start plus a decoded media position;
- display time is chip-domain time formatted for the operator.

The recorder offset is applied exactly once by a calibration model. Evidence
is pinned to its decoded source frame (:class:`FrameRef`) instead of being
re-derived from a time value later.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, NewType


WallClockMs = NewType("WallClockMs", int)
"""Milliseconds since the Unix epoch (or another declared absolute clock)."""

ChipClockMs = NewType("ChipClockMs", int)
"""CycleRace chip-reader clock, epoch milliseconds; the official domain."""

RecorderClockMs = NewType("RecorderClockMs", int)
"""FinishReview recorder clock: segment media start plus decoded position."""

MediaPositionMs = NewType("MediaPositionMs", int)
"""Non-negative position measured from the start of a media resource."""

DurationMs = NewType("DurationMs", int)
"""Non-negative elapsed duration."""

ClockOffsetMs = NewType("ClockOffsetMs", int)
"""Signed correction added to an absolute passage timestamp."""


def wall_clock_ms(value: int) -> WallClockMs:
    value = int(value)
    if value < 0:
        raise ValueError("wall-clock milliseconds must be non-negative")
    return WallClockMs(value)


def media_position_ms(value: int) -> MediaPositionMs:
    value = int(value)
    if value < 0:
        raise ValueError("media position must be non-negative")
    return MediaPositionMs(value)


def duration_ms(value: int) -> DurationMs:
    value = int(value)
    if value < 0:
        raise ValueError("duration must be non-negative")
    return DurationMs(value)


def clock_offset_ms(value: int) -> ClockOffsetMs:
    return ClockOffsetMs(int(value))


def chip_clock_ms(value: int) -> ChipClockMs:
    value = int(value)
    if value < 0:
        raise ValueError("chip clock milliseconds must be non-negative")
    return ChipClockMs(value)


def recorder_clock_ms(value: int) -> RecorderClockMs:
    value = int(value)
    if value < 0:
        raise ValueError("recorder clock milliseconds must be non-negative")
    return RecorderClockMs(value)


@dataclass(frozen=True, slots=True)
class FrameRef:
    """One decoded source frame used as evidence.

    ``frame_index`` is the zero-based decode order inside ``video_path`` and
    ``position_ms`` is the decoder's media position of that same frame. The
    calibration version records which clock model mapped chip time to this
    frame; reopening the evidence never re-applies an offset.
    """

    camera_index: int
    segment_id: str
    video_path: str
    frame_index: int
    position_ms: MediaPositionMs
    session_id: str = ""
    calibration_version: int = 0

    def __post_init__(self) -> None:
        if int(self.camera_index) <= 0:
            raise ValueError("camera_index must be positive")
        if not str(self.video_path).strip():
            raise ValueError("video_path is required")
        if int(self.frame_index) < 0:
            raise ValueError("frame_index must be non-negative")
        if int(self.position_ms) < 0:
            raise ValueError("position_ms must be non-negative")
        if int(self.calibration_version) < 0:
            raise ValueError("calibration_version must be non-negative")

    def recorder_time_ms(self, media_started_at_ms: int) -> RecorderClockMs:
        return RecorderClockMs(int(media_started_at_ms) + int(self.position_ms))

    def to_payload(self) -> dict[str, Any]:
        return {
            "camera_index": int(self.camera_index),
            "segment_id": str(self.segment_id),
            "video_path": str(self.video_path),
            "frame_index": int(self.frame_index),
            "position_ms": int(self.position_ms),
            "session_id": str(self.session_id),
            "calibration_version": int(self.calibration_version),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "FrameRef":
        if not isinstance(payload, Mapping):
            raise ValueError("frame reference must be an object")
        return cls(
            camera_index=int(payload["camera_index"]),
            segment_id=str(payload.get("segment_id", "")),
            video_path=str(payload["video_path"]),
            frame_index=int(payload["frame_index"]),
            position_ms=MediaPositionMs(int(payload["position_ms"])),
            session_id=str(payload.get("session_id", "")),
            calibration_version=int(payload.get("calibration_version", 0)),
        )


@dataclass(frozen=True, slots=True)
class MediaWindow:
    start: MediaPositionMs
    end: MediaPositionMs

    def __post_init__(self) -> None:
        if int(self.start) < 0:
            raise ValueError("media window start must be non-negative")
        if int(self.end) < int(self.start):
            raise ValueError("media window end cannot precede start")

    @classmethod
    def from_milliseconds(cls, start: int, end: int) -> "MediaWindow":
        return cls(media_position_ms(start), media_position_ms(end))

    @property
    def duration(self) -> DurationMs:
        return DurationMs(int(self.end) - int(self.start))


@dataclass(frozen=True, slots=True)
class WallClockWindow:
    start: WallClockMs
    end: WallClockMs

    def __post_init__(self) -> None:
        if int(self.start) < 0:
            raise ValueError("wall-clock window start must be non-negative")
        if int(self.end) < int(self.start):
            raise ValueError("wall-clock window end cannot precede start")

    @classmethod
    def from_milliseconds(cls, start: int, end: int) -> "WallClockWindow":
        return cls(wall_clock_ms(start), wall_clock_ms(end))

    @property
    def duration(self) -> DurationMs:
        return DurationMs(int(self.end) - int(self.start))


__all__ = [
    "ChipClockMs",
    "ClockOffsetMs",
    "DurationMs",
    "FrameRef",
    "MediaPositionMs",
    "MediaWindow",
    "RecorderClockMs",
    "WallClockMs",
    "WallClockWindow",
    "chip_clock_ms",
    "clock_offset_ms",
    "duration_ms",
    "media_position_ms",
    "recorder_clock_ms",
    "wall_clock_ms",
]
