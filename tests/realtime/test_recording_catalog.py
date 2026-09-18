from pathlib import Path
from collections import deque

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


def test_catalog_applies_sealed_segment_delta_without_full_snapshot(tmp_path, monkeypatch):
    store = VideoTimelineStore(tmp_path / "timeline.jsonl")
    job = RecordingCatalogJob(RecordingCatalog(), store, 1, "race", ())
    job.catalog.refresh(job)
    monkeypatch.setattr(store, "segments", lambda: (_ for _ in ()).throw(AssertionError("full scan")))
    video = tmp_path / "camera.mkv"
    video.write_bytes(b"video")
    segment = store.start_segment(source_id="camera", camera_index=1, video_path=video,
                                  started_at_ms=1000, race_id="race")
    assert job.catalog.refresh(job).pending == 1
    store.finish_segment(segment.segment_id, ended_at_ms=2000, media_duration_ms=1000)
    snapshot = job.catalog.refresh(job)
    assert snapshot.pending == 0
    assert len(snapshot.sources) == 1
    assert snapshot.sources[0].available
    assert job.catalog.refresh(job) == snapshot


def test_catalog_bounds_periodic_file_checks_and_detects_external_changes(tmp_path, monkeypatch):
    store = VideoTimelineStore(tmp_path / "timeline.jsonl")
    paths = [tmp_path / f"camera-{index}.mkv" for index in range(40)]
    for index, path in enumerate(paths):
        path.write_bytes(b"video")
        store.add_completed_segment(source_id="camera", camera_index=1, video_path=path,
                                    media_started_at_ms=1000 + index * 1000, media_duration_ms=1000,
                                    race_id="race", clock_source=DEFAULT_CLOCK_SOURCE,
                                    timing_error_ms=0, end_reason="archive")
    job = RecordingCatalogJob(RecordingCatalog(), store, 1, "race", ())
    job.catalog.refresh(job)
    clock = [10.0]
    monkeypatch.setattr("realtime.recording_catalog.time.monotonic", lambda: clock[0])
    job.catalog._availability = type(job.catalog._availability)(
        (key, (0, value)) for key, (_, value) in job.catalog._availability.items()
    )
    paths[-1].unlink()
    before = job.catalog.metrics.snapshot("catalog.file_check").count
    job.catalog.refresh(job)
    assert job.catalog.metrics.snapshot("catalog.file_check").count - before == 32
    snapshot = job.catalog.refresh(job)
    assert not snapshot.sources[-1].available
    paths[-1].write_bytes(b"restored")
    clock[0] += 3
    job.catalog.refresh(job)
    assert job.catalog.refresh(job).sources[-1].available


def test_expired_timeline_delta_falls_back_to_current_snapshot(tmp_path):
    store = VideoTimelineStore(tmp_path / "timeline.jsonl")
    store._segment_changes = deque(maxlen=2)
    for index in range(3):
        store.start_segment(source_id="camera", camera_index=1, video_path=tmp_path / f"{index}.mkv",
                            started_at_ms=1000 + index)
    revision, reset, segments = store.segment_changes_since(0)
    assert reset and revision == 3 and len(segments) == 3
    assert store.segment_changes_since(revision) == (revision, False, ())
