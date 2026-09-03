"""Explicit millisecond domains used at video and timing boundaries.

The aliases intentionally remain ``int`` at runtime. They therefore preserve
Qt signal compatibility and the existing JSONL schema while allowing static
analysis to reject accidental arithmetic between wall-clock timestamps, media
positions, durations, and clock offsets.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NewType


WallClockMs = NewType("WallClockMs", int)
"""Milliseconds since the Unix epoch (or another declared absolute clock)."""

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
    "ClockOffsetMs",
    "DurationMs",
    "MediaPositionMs",
    "MediaWindow",
    "WallClockMs",
    "WallClockWindow",
    "clock_offset_ms",
    "duration_ms",
    "media_position_ms",
    "wall_clock_ms",
]
