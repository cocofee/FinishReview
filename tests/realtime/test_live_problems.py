from datetime import datetime, timezone

from realtime.live_problems import publish_sealed_segments
from realtime.review_recorder import ReviewRingBuffer
from realtime.video_timeline import VideoTimelineStore


def test_only_published_segments_are_retained_and_evidence_survives_cleanup(tmp_path):
    start = 1_789_600_000_000
    closed = tmp_path / "camera_01_session_review_0000000000000000.ts"
    writing = tmp_path / "camera_01_session_review_0000000000000001.ts"
    closed.write_bytes(b"sealed")
    writing.write_bytes(b"writing")
    playlist = tmp_path / "camera_01_session.m3u8"
    playlist.write_text(
        "#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:2.000,\n"
        f"#EXT-X-PROGRAM-DATE-TIME:{datetime.fromtimestamp(start / 1000, timezone.utc).isoformat()}\n"
        f"{closed.name}\n", encoding="utf-8",
    )
    ring = ReviewRingBuffer(playlist, camera_index=1, retention_seconds=1)
    ring.scan()
    timeline = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    published = publish_sealed_segments(ring, timeline, "race-1")
    assert len(published) == 1
    assert timeline.resolve_video_path(published[0]) == closed
    assert published[0].media_duration_ms == 2000
    assert published[0].end_reason == "retained_problem_segment"
    assert timeline.locate_passage(start + 1000, race_id="race-1").locations == ()
    assert publish_sealed_segments(ring, timeline, "race-1") == ()
    ring.cleanup(current_time_ms=start + 60_000)
    assert closed.exists()
    assert timeline.find_segment_by_video_path(writing) is None
