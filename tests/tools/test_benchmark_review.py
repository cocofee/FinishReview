from tools.benchmark_review import _populate_timeline_store, _timeline_lookup_benchmark
from realtime.video_timeline import RecordingSegment, VideoTimelineStore


def test_benchmark_fixture_uses_indexed_production_loader(tmp_path):
    store = VideoTimelineStore(tmp_path / "timeline.jsonl")
    segment = RecordingSegment(
        segment_id="last", source_id="camera-1", camera_index=1,
        video_path="last.mp4", started_at_ms=2000, ended_at_ms=2900,
        media_started_at_ms=2000, media_duration_ms=900, race_id="benchmark-race",
    )
    _populate_timeline_store(store, [segment])
    lookup = store.locate_passage(2450, race_id="benchmark-race")
    assert lookup.status == "missing_file"
    assert lookup.locations[0].segment == segment
    assert store.find_segment_by_video_path(tmp_path / "last.mp4") == segment
    assert store.revision == 2


def test_timeline_benchmark_checks_real_candidate_path():
    result = _timeline_lookup_benchmark(100, 2)
    assert result["expected_status"] == "missing_file"
    assert result["lookup"]["min_ms"] >= 0
