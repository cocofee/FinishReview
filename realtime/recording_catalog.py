"""Recording coverage and file availability, independent of Qt widgets."""

from bisect import bisect_right
from dataclasses import dataclass, replace
import heapq
from pathlib import Path
import time

from .video_timeline import DEFAULT_CLOCK_SOURCE, DEFAULT_TIMING_ERROR_MS, PassageVideoLocation, RecordingSegment


@dataclass(frozen=True)
class FilmstripSource:
    location: PassageVideoLocation
    start_ms: int
    end_ms: int
    available: bool
    priority: int = 0

    @property
    def key(self):
        # Source identity must survive a live HLS playlist growing at its
        # right edge. Coverage end is tracked separately by ``set_sources``;
        # keeping it out of this key preserves already decoded thumbnails.
        return (str(self.location.video_path.absolute()), self.start_ms,
                self.location.segment.segment_id)


@dataclass(frozen=True)
class RecordingSpan:
    start_ms: int
    end_ms: int
    source: FilmstripSource | None


class RaceRecordingIndex:
    """Non-overlapping recording spans, including uncompressed real gaps."""

    def __init__(self, sources=()):
        self.sources = tuple(sorted(sources, key=lambda source: (source.start_ms, source.key)))
        events = {}
        for index, source in enumerate(self.sources):
            if source.end_ms <= source.start_ms:
                continue
            events.setdefault(source.start_ms, []).append((True, index))
            events.setdefault(source.end_ms, []).append((False, index))
        points = sorted(events)
        active = set()
        heap = []
        spans = []
        for point_index, point in enumerate(points[:-1]):
            for begins, index in events[point]:
                if begins:
                    active.add(index)
                    source = self.sources[index]
                    heapq.heappush(heap, (not source.available, source.priority,
                                         -(source.end_ms - source.start_ms), index))
                else:
                    active.discard(index)
            while heap and heap[0][-1] not in active:
                heapq.heappop(heap)
            source = self.sources[heap[0][-1]] if heap else None
            end = points[point_index + 1]
            if spans and spans[-1].source == source:
                spans[-1] = RecordingSpan(spans[-1].start_ms, end, source)
            else:
                spans.append(RecordingSpan(point, end, source))
        self.spans = tuple(spans)
        self.starts = tuple(span.start_ms for span in spans)
        self.start_ms = points[0] if points else 0
        self.end_ms = points[-1] if points else 0

    def span_at(self, timestamp):
        index = bisect_right(self.starts, timestamp) - 1
        if index >= 0 and timestamp < self.spans[index].end_ms:
            return self.spans[index]
        return None

    def sample_at(self, timestamp, interval_ms):
        span = self.span_at(timestamp)
        if span is not None and span.source is not None:
            return span.source, timestamp
        # Even a recording shorter than the selected spacing gets a tile.
        index = bisect_right(self.starts, timestamp)
        if index < len(self.spans):
            next_span = self.spans[index]
            if next_span.start_ms < timestamp + interval_ms and next_span.source is not None:
                return next_span.source, next_span.start_ms
        return None, timestamp


def recording_sources(
    store,
    camera_index: int,
    race_id: str = "",
    *,
    offset_for_location=None,
    live_location: PassageVideoLocation | None = None,
    live_locations=(),
    path_available=None,
):
    """Use recording coverage, never the roster or its filters, for the rail."""
    sources = []
    pending = 0
    for segment in store.segments():
        if segment.camera_index != camera_index or segment.clock_source != DEFAULT_CLOCK_SOURCE:
            continue
        if race_id and segment.race_id and segment.race_id != race_id:
            continue
        if segment.media_started_at_ms is None or segment.media_duration_ms is None:
            pending += 1
            continue
        start = int(segment.media_started_at_ms)
        end = start + int(segment.media_duration_ms)
        path = store.resolve_video_path(segment)
        available = path.is_file() if path_available is None else path_available(path, False)
        continuous = "archive" in path.stem or "archive" in segment.end_reason
        priority = 0 if continuous else 2 if path.suffix.lower() == ".m3u8" else 1
        location = PassageVideoLocation(segment, path, 0, 0, 0,
                                        segment.timing_error_ms, "located" if available else "missing_file")
        if offset_for_location is not None:
            location = replace(location, clock_offset_ms=offset_for_location(location))
        sources.append(FilmstripSource(location, start, end, available, priority))
    # The archive writer deliberately leaves the current five-minute file out
    # until it is sealed and duration-probed.  Keep the already published HLS
    # tail in the same chronological index so the operator can review the
    # newest arrivals without waiting for that seal.  Archive sources have a
    # higher priority and therefore replace this overlap automatically once
    # the long-form file is published.
    live_values = tuple(live_locations)
    if live_location is not None:
        live_values = (*live_values, live_location)
    for live_location in live_values:
        segment = live_location.segment
        if (
            segment.camera_index == camera_index
            and segment.media_started_at_ms is not None
            and segment.media_duration_ms is not None
            and (not race_id or not segment.race_id or segment.race_id == race_id)
        ):
            path = Path(live_location.video_path)
            # Each live source is one closed TS segment.  Validate only that
            # immutable file; validating a rolling playlist makes an otherwise
            # healthy tail fail when its old head has already been cleaned up.
            if path_available is not None:
                available = path_available(path, True)
            else:
                try:
                    available = path.is_file() and path.stat().st_size > 0
                except OSError:
                    available = False
            if path.suffix.lower() == ".m3u8":
                available = available and store.video_path_is_playable(path)
            location = live_location
            if offset_for_location is not None:
                location = replace(location, clock_offset_ms=offset_for_location(location))
            sources.append(
                FilmstripSource(
                    location,
                    int(segment.media_started_at_ms),
                    int(segment.media_started_at_ms + segment.media_duration_ms),
                    available,
                    2,
                )
            )
            # This is an active tail, not a missing interval.  The caller
            # renders a separate processing notice; keep ``pending`` reserved
            # for archive segments that have no temporary playback source.
    return tuple(sources), pending


@dataclass(frozen=True, slots=True)
class RecordingCatalogJob:
    catalog: object
    store: object
    camera_index: int
    race_id: str
    ring_buffers: tuple


@dataclass(frozen=True, slots=True)
class RecordingCatalogSnapshot:
    context: tuple
    sources: tuple
    pending: int
    revision: int = 0


class RecordingCatalog:
    """Worker-owned availability cache, scoped to the current source set."""

    def __init__(self):
        self._availability = {}

    def refresh(self, job, *, deleted_paths=()):
        for path in deleted_paths:
            self._availability.pop((str(path), False), None)
            self._availability.pop((str(path), True), None)
        used = set()
        now = time.monotonic()

        def available(path, require_nonempty):
            key = (str(path), require_nonempty)
            used.add(key)
            cached = self._availability.get(key)
            if cached is not None and now - cached[0] < 2.0:
                return cached[1]
            try:
                value = path.is_file() and (not require_nonempty or path.stat().st_size > 0)
            except OSError:
                value = False
            self._availability[key] = (now, value)
            return value

        live = []
        for ring in job.ring_buffers:
            if ring.camera_index != job.camera_index:
                continue
            for item in ring.filmstrip_segments():
                path = ring.resolve_path(item)
                segment = RecordingSegment(
                    segment_id=f"live-filmstrip-{ring.camera_index}-{item.segment_id}",
                    source_id=ring.source_id, camera_index=ring.camera_index,
                    video_path=str(path), started_at_ms=item.started_at_ms,
                    ended_at_ms=item.ended_at_ms, media_started_at_ms=item.started_at_ms,
                    media_duration_ms=item.duration_ms, clock_source=DEFAULT_CLOCK_SOURCE,
                    timing_error_ms=DEFAULT_TIMING_ERROR_MS,
                    end_reason="live_filmstrip_tail", race_id=job.race_id,
                )
                live.append(PassageVideoLocation(segment, path, 0, 0, 0,
                                                  DEFAULT_TIMING_ERROR_MS, "unverified"))
        sources, pending = recording_sources(job.store, job.camera_index, job.race_id,
                                              live_locations=live, path_available=available)
        self._availability = {key: value for key, value in self._availability.items() if key in used}
        return RecordingCatalogSnapshot(
            (str(job.store.journal_path.absolute()), job.race_id, job.camera_index), sources, pending,
            job.store.revision,
        )


