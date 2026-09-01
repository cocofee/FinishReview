"""Build operator-facing finish-line review batches.

The batch is a video-review unit, not a timing or ranking unit.  Events from
different race groups may share one finish-line camera window and therefore
belong to the same review batch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence


DEFAULT_REVIEW_GAP_MS = 5_000
DEFAULT_SUBWAVE_GAP_MS = 3_000


@dataclass(frozen=True, slots=True)
class PassageReviewBatch:
    """One continuous finish-line video review window."""

    batch_id: str
    event_ids: tuple[str, ...]
    started_at_ms: int
    ended_at_ms: int
    subwave_breaks: tuple[int, ...] = ()

    @property
    def size(self) -> int:
        return len(self.event_ids)

    @property
    def is_large(self) -> bool:
        return self.size >= 10

    @property
    def is_extra_large(self) -> bool:
        return self.size >= 20


def build_review_batches(
    events: Iterable[object],
    *,
    review_gap_ms: int = DEFAULT_REVIEW_GAP_MS,
    subwave_gap_ms: int = DEFAULT_SUBWAVE_GAP_MS,
) -> tuple[PassageReviewBatch, ...]:
    """Group active passage events into global video-review windows.

    The input is sorted by the timeline timestamp.  A batch is split only
    when the gap between adjacent events exceeds ``review_gap_ms``.  Internal
    gaps above ``subwave_gap_ms`` are retained as subwave boundaries so the
    operator can see structure without reopening the video.
    """

    review_gap_ms = max(0, int(review_gap_ms))
    subwave_gap_ms = max(0, int(subwave_gap_ms))
    ordered = sorted(
        (
            event
            for event in events
            if bool(getattr(event, "is_active", True))
            and str(getattr(event, "event_id", "")).strip()
        ),
        key=lambda event: (
            int(getattr(event, "timeline_timestamp_ms", 0)),
            int(getattr(event, "sequence", 0)),
            str(getattr(event, "event_id", "")),
        ),
    )
    if not ordered:
        return ()

    result: list[PassageReviewBatch] = []
    current: list[object] = [ordered[0]]
    subwave_breaks: list[int] = []
    previous_time = int(getattr(ordered[0], "timeline_timestamp_ms", 0))
    for event in ordered[1:]:
        timestamp = int(getattr(event, "timeline_timestamp_ms", 0))
        gap = timestamp - previous_time
        if gap > review_gap_ms:
            result.append(_make_batch(current, subwave_breaks))
            current = [event]
            subwave_breaks = []
        else:
            if gap > subwave_gap_ms:
                subwave_breaks.append(len(current))
            current.append(event)
        previous_time = timestamp
    result.append(_make_batch(current, subwave_breaks))
    return tuple(result)


def _make_batch(
    events: Sequence[object],
    subwave_breaks: Sequence[int],
) -> PassageReviewBatch:
    event_ids = tuple(str(getattr(event, "event_id")) for event in events)
    started_at_ms = int(getattr(events[0], "timeline_timestamp_ms", 0))
    ended_at_ms = int(getattr(events[-1], "timeline_timestamp_ms", started_at_ms))
    # The first event is stable while a live batch grows with later chip reads.
    # This keeps operator annotations attached when new records arrive.
    batch_id = f"batch:{event_ids[0]}"
    return PassageReviewBatch(
        batch_id=batch_id,
        event_ids=event_ids,
        started_at_ms=started_at_ms,
        ended_at_ms=ended_at_ms,
        subwave_breaks=tuple(int(index) for index in subwave_breaks),
    )


__all__ = [
    "DEFAULT_REVIEW_GAP_MS",
    "DEFAULT_SUBWAVE_GAP_MS",
    "PassageReviewBatch",
    "build_review_batches",
]
