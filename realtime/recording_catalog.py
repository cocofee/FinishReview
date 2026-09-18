"""Recording coverage and file availability, independent of Qt widgets."""

from bisect import bisect_right
from collections import OrderedDict
from dataclasses import dataclass, replace
import heapq
from itertools import islice
from pathlib import Path
import time
from .runtime_metrics import RuntimeMetrics

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
    segments=None,
):
    """Use recording coverage, never the roster or its filters, for the rail."""
    sources = []
    pending = 0
    for segment in store.segments() if segments is None else segments:
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
    """Worker-owned delta projection with bounded rolling file revalidation."""

    RECHECK_BATCH = 32

    def __init__(self):
        self._availability = OrderedDict()
        self._store = None
        self._context = None
        self._revision = -1
        self._entries = {}
        self._path_segments = {}
        self._sources = ()
        self._pending = 0
        self.metrics = RuntimeMetrics()

    def refresh(self, job, *, deleted_paths=()):
        context = (str(job.store.journal_path.absolute()), job.race_id, job.camera_index)
        if job.store is not self._store or context != self._context:
            self._store, self._context = job.store, context
            self._revision = -1
            self._availability.clear()
            self._entries.clear()
            self._path_segments.clear()
        revision, reset, changed = job.store.segment_changes_since(self._revision)
        if reset:
            self._entries.clear()
            self._path_segments.clear()
        dirty = reset or bool(changed)
        now = time.monotonic()
        invalidated = set()
        for path in deleted_paths:
            for nonempty in (False, True):
                key = (str(path), nonempty)
                self._availability.pop(key, None)
                invalidated.add(key)

        def available(path, require_nonempty):
            key = (str(path), require_nonempty)
            cached = self._availability.get(key)
            if cached is not None:
                return cached[1]
            started = time.perf_counter()
            try:
                value = path.is_file() and (not require_nonempty or path.stat().st_size > 0)
            except OSError:
                value = False
            self.metrics.observe("catalog.file_check", (time.perf_counter() - started) * 1000,
                                 item_count=1)
            self._availability[key] = (now, value)
            return value

        # Files may be removed/restored outside the recorder. Sweep a bounded
        # slice each refresh, rather than stat every file whenever a TTL expires.
        for key in tuple(islice(self._availability, self.RECHECK_BATCH)):
            checked_at, previous = self._availability[key]
            if now - checked_at < 2.0:
                break
            self._availability.pop(key)
            if available(Path(key[0]), key[1]) != previous:
                invalidated.add(key)

        for segment in changed:
            sources, pending = recording_sources(
                job.store, job.camera_index, job.race_id, segments=(segment,), path_available=available,
            )
            self._entries[segment.segment_id] = (sources, pending)
            if sources:
                key = (str(sources[0].location.video_path), False)
                self._path_segments.setdefault(key, set()).add(segment.segment_id)
        for key in invalidated:
            for segment_id in self._path_segments.get(key, ()):
                sources, pending = self._entries[segment_id]
                value = available(Path(key[0]), key[1])
                self._entries[segment_id] = (tuple(
                    replace(source, available=value, location=replace(
                        source.location, status="located" if value else "missing_file"))
                    for source in sources
                ), pending)
                dirty = True
        if dirty:
            self._sources = tuple(source for sources, _ in self._entries.values() for source in sources)
            self._pending = sum(pending for _, pending in self._entries.values())
        self._revision = revision

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
        live_sources, _ = recording_sources(job.store, job.camera_index, job.race_id,
                                           segments=(), live_locations=live, path_available=available)
        used = set(self._path_segments)
        used.update((str(source.location.video_path), True) for source in live_sources)
        self._availability = OrderedDict((key, value) for key, value in self._availability.items() if key in used)
        return RecordingCatalogSnapshot(
            context, self._sources + live_sources, self._pending, revision,
        )
