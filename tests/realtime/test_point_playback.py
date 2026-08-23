from pathlib import Path
from datetime import datetime, timezone

import pytest

import realtime.point_playback as point_playback_module
from realtime.point_playback import (
    POINT_PLAYBACK_AFTER_MS,
    POINT_PLAYBACK_BEFORE_MS,
    PointPlaybackUnavailable,
    prepare_point_playback,
)
from realtime.review_recorder import ReviewRingBuffer
from realtime.video_timeline import (
    DEFAULT_CLOCK_SOURCE,
    PassageVideoLocation,
    RecordingSegment,
    VideoTimelineStore,
)


def _add_archive(
    store: VideoTimelineStore,
    race_dir: Path,
    *,
    name: str,
    started_at_ms: int,
    duration_ms: int,
) -> RecordingSegment:
    path = race_dir / "videos" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"video")
    return store.add_completed_segment(
        source_id="camera_01_review",
        camera_index=1,
        video_path=path,
        media_started_at_ms=started_at_ms,
        media_duration_ms=duration_ms,
        clock_source=DEFAULT_CLOCK_SOURCE,
        timing_error_ms=2_000,
        end_reason="continuous_archive_fallback",
        race_id="race-1",
    )


def _location(segment: RecordingSegment, race_dir: Path, anchor_ms: int):
    media_start = int(segment.media_started_at_ms or segment.started_at_ms)
    position_ms = anchor_ms - media_start
    return PassageVideoLocation(
        segment=segment,
        video_path=(race_dir / segment.video_path).resolve(),
        passage_position_ms=position_ms,
        playback_position_ms=max(0, position_ms - 3_000),
        clock_offset_ms=0,
        timing_error_ms=segment.timing_error_ms,
        status="located",
    )


def test_point_playback_defaults_to_45_seconds_before_and_15_after():
    assert POINT_PLAYBACK_BEFORE_MS == 45_000
    assert POINT_PLAYBACK_AFTER_MS == 15_000


def test_point_playback_builds_one_range_across_adjacent_archives(tmp_path):
    store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    first = _add_archive(
        store,
        tmp_path,
        name="archive_0000.mkv",
        started_at_ms=0,
        duration_ms=50_000,
    )
    _add_archive(
        store,
        tmp_path,
        name="archive_0001.mkv",
        started_at_ms=50_000,
        duration_ms=50_000,
    )

    session = prepare_point_playback(
        store,
        _location(first, tmp_path, 60_000),
        anchor_time_ms=60_000,
        race_id="race-1",
        output_dir=tmp_path,
    )
    try:
        assert session.requested_started_at_ms == 15_000
        assert session.requested_ended_at_ms == 75_000
        assert session.available_started_at_ms == 15_000
        assert session.available_ended_at_ms == 75_000
        assert session.duration_ms == 60_000
        assert session.target_position_ms == 45_000
        manifest = session.manifest_path.read_text(encoding="utf-8")
        assert "file 'videos/archive_0000.mkv'" in manifest
        assert "inpoint 15.000000" in manifest
        assert "outpoint 50.000000" in manifest
        assert "file 'videos/archive_0001.mkv'" in manifest
        assert "outpoint 25.000000" in manifest
    finally:
        manifest_path = session.manifest_path
        session.cleanup()
    assert not manifest_path.exists()


def test_point_playback_clamps_to_contiguous_media_around_anchor(tmp_path):
    store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    first = _add_archive(
        store,
        tmp_path,
        name="before_gap.mkv",
        started_at_ms=0,
        duration_ms=30_000,
    )
    second = _add_archive(
        store,
        tmp_path,
        name="after_gap.mkv",
        started_at_ms=50_000,
        duration_ms=50_000,
    )

    session = prepare_point_playback(
        store,
        _location(second, tmp_path, 60_000),
        anchor_time_ms=60_000,
        race_id="race-1",
        output_dir=tmp_path,
    )
    try:
        assert session.available_started_at_ms == 50_000
        assert session.available_ended_at_ms == 75_000
        assert session.duration_ms == 25_000
        assert session.target_position_ms == 10_000
        manifest = session.manifest_path.read_text(encoding="utf-8")
        assert manifest.count("file '") == 1
        assert "file 'videos/after_gap.mkv'" in manifest
    finally:
        session.cleanup()


def test_point_playback_rejects_media_that_does_not_cover_anchor(tmp_path):
    store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    segment = _add_archive(
        store,
        tmp_path,
        name="too_early.mkv",
        started_at_ms=0,
        duration_ms=30_000,
    )

    with pytest.raises(PointPlaybackUnavailable, match="target point"):
        prepare_point_playback(
            store,
            _location(segment, tmp_path, 60_000),
            anchor_time_ms=60_000,
            race_id="race-1",
            output_dir=tmp_path,
        )


def test_point_playback_removes_staged_links_when_manifest_write_fails(
    tmp_path,
    monkeypatch,
):
    store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    segment = _add_archive(
        store,
        tmp_path,
        name="archive with space.mkv",
        started_at_ms=0,
        duration_ms=60_000,
    )
    monkeypatch.setattr(
        point_playback_module,
        "_write_manifest",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk error")),
    )

    with pytest.raises(OSError, match="disk error"):
        prepare_point_playback(
            store,
            _location(segment, tmp_path, 30_000),
            anchor_time_ms=30_000,
            race_id="race-1",
            output_dir=tmp_path,
        )

    assert not list(tmp_path.glob(".point_playback_*"))
    assert not list(tmp_path.glob("_point_playback_*"))


def test_point_playback_stages_only_unsafe_media_paths(tmp_path):
    store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    segment = _add_archive(
        store,
        tmp_path,
        name="archive with space.mkv",
        started_at_ms=0,
        duration_ms=60_000,
    )

    session = prepare_point_playback(
        store,
        _location(segment, tmp_path, 30_000),
        anchor_time_ms=30_000,
        race_id="race-1",
        output_dir=tmp_path,
    )
    staging_dirs = list(tmp_path.glob("_point_playback_*"))
    try:
        assert len(staging_dirs) == 1
        staged_path = staging_dirs[0] / "clip_0000.mkv"
        assert staged_path.samefile(tmp_path / "videos" / "archive with space.mkv")
        manifest = session.manifest_path.read_text(encoding="utf-8")
        assert f"file '{staging_dirs[0].name}/clip_0000.mkv'" in manifest
    finally:
        session.cleanup()

    assert not session.manifest_path.exists()
    assert not staging_dirs[0].exists()


def test_point_playback_uses_and_releases_live_ring_buffer_segments(tmp_path):
    anchor_ms = 1_787_450_000_000
    buffer_dir = tmp_path / "review_buffer" / "camera_01"
    buffer_dir.mkdir(parents=True)
    playlist_path = buffer_dir / "camera_01.m3u8"
    lines = ["#EXTM3U", "#EXT-X-VERSION:6"]
    segment_starts = range(anchor_ms - 46_000, anchor_ms + 16_000, 2_000)
    for index, started_at_ms in enumerate(segment_starts):
        name = f"segment_{index:03d}.ts"
        (buffer_dir / name).write_bytes(b"video")
        timestamp = datetime.fromtimestamp(
            started_at_ms / 1000.0,
            tz=timezone.utc,
        ).isoformat(timespec="milliseconds")
        lines.extend(
            (
                f"#EXT-X-PROGRAM-DATE-TIME:{timestamp}",
                "#EXTINF:2.000,",
                name,
            )
        )
    playlist_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ring_buffer = ReviewRingBuffer(
        playlist_path,
        camera_index=1,
        retention_seconds=360,
    )
    segment = RecordingSegment(
        segment_id="live-location",
        source_id="camera_01_review",
        camera_index=1,
        video_path="missing.m3u8",
        started_at_ms=anchor_ms - 3_000,
        ended_at_ms=anchor_ms + 3_000,
        media_started_at_ms=anchor_ms - 3_000,
        media_duration_ms=6_000,
        race_id="race-1",
    )
    location = PassageVideoLocation(
        segment=segment,
        video_path=tmp_path / "missing.m3u8",
        passage_position_ms=3_000,
        playback_position_ms=0,
        clock_offset_ms=0,
        timing_error_ms=2_000,
        status="located",
    )

    session = prepare_point_playback(
        VideoTimelineStore(tmp_path / "video_timeline.jsonl"),
        location,
        anchor_time_ms=anchor_ms,
        race_id="race-1",
        output_dir=tmp_path,
        ring_buffer=ring_buffer,
    )
    pinned_segment = ring_buffer.segments()[0]
    try:
        assert session.available_started_at_ms == anchor_ms - 46_000
        assert session.available_ended_at_ms == anchor_ms + 16_000
        assert session.target_position_ms == 46_000
        assert session.duration_ms == 62_000
        manifest = session.manifest_path.read_text(encoding="utf-8")
        assert manifest.count("file '") == 31
        assert "inpoint" not in manifest
        assert ring_buffer.pinned_event_ids(pinned_segment.segment_id)
    finally:
        session.cleanup()
    assert not ring_buffer.pinned_event_ids(pinned_segment.segment_id)
