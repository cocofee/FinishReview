"""Publish immutable review segments without touching the active recorder.

Short files are retained for the race: problem IDs and exported frame references
must not change when a five-minute archive later becomes available.
"""

from .video_timeline import RecordingSegment, DEFAULT_CLOCK_SOURCE, DEFAULT_TIMING_ERROR_MS


def publish_sealed_segments(ring, timeline, race_id):
    published = []
    for item in ring.filmstrip_segments():
        path = ring.resolve_path(item)
        if timeline.find_segment_by_video_path(path) is not None:
            continue
        segment = RecordingSegment(
            segment_id=f"live-problem-{ring.camera_index}-{item.segment_id}",
            source_id=ring.source_id, camera_index=ring.camera_index,
            video_path=str(path), started_at_ms=item.started_at_ms,
            ended_at_ms=item.ended_at_ms, media_started_at_ms=item.started_at_ms,
            media_duration_ms=item.duration_ms, clock_source=DEFAULT_CLOCK_SOURCE,
            timing_error_ms=DEFAULT_TIMING_ERROR_MS, race_id=race_id,
            end_reason="live_filmstrip_tail",
        )
        published.append(ring.retain_filmstrip_segment(segment, timeline, end_reason="retained_problem_segment"))
    return tuple(published)
