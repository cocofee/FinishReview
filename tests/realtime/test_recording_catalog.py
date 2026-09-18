from pathlib import Path

from realtime.recording_catalog import RecordingCatalog, RecordingCatalogJob
from realtime.video_timeline import DEFAULT_CLOCK_SOURCE, VideoTimelineStore


def test_catalog_reuses_availability_and_invalidates_deleted_paths(tmp_path, monkeypatch):
    video = tmp_path / "archive.mkv"
    video.write_bytes(b"video")
    store = VideoTimelineStore(tmp_path / "timeline.jsonl")
    store.add_completed_segment(source_id="camera", camera_index=1, video_path=video,
                                media_started_at_ms=1000, media_duration_ms=10000,
                                race_id="race", clock_source=DEFAULT_CLOCK_SOURCE,
                                timing_error_ms=0, end_reason="continuous_archive")
    job = RecordingCatalogJob(RecordingCatalog(), store, 1, "race", ())
    checks = []
    original = Path.is_file

    def check(path):
        checks.append(path)
        return original(path)

    monkeypatch.setattr(Path, "is_file", check)
    first = job.catalog.refresh(job)
    second = job.catalog.refresh(job)
    assert first == second and first.sources[0].available
    assert checks == [video]
    video.unlink()
    third = job.catalog.refresh(job, deleted_paths=(video,))
    assert not third.sources[0].available
    assert checks == [video, video]
