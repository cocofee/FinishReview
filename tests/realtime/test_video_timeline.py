import json
import os

import cv2
import realtime.video_timeline as video_timeline
from realtime.video_timeline import VideoTimelineStore


def test_probes_media_duration_from_frame_count_and_fps(tmp_path, monkeypatch):
    video_path = tmp_path / "camera.mkv"
    video_path.write_bytes(b"video")

    class _Capture:
        def isOpened(self):
            return True

        def get(self, property_id):
            if property_id == cv2.CAP_PROP_FPS:
                return 50.0
            if property_id == cv2.CAP_PROP_FRAME_COUNT:
                return 625
            return 0

        def release(self):
            pass

    monkeypatch.setattr(cv2, "VideoCapture", lambda _path: _Capture())

    assert video_timeline.probe_video_duration_ms(video_path) == 12_500


def test_media_probe_failure_keeps_timeline_unverified(tmp_path, monkeypatch):
    video_path = tmp_path / "camera.mkv"
    video_path.write_bytes(b"video")

    def _raise(_path):
        raise RuntimeError("decoder unavailable")

    monkeypatch.setattr(cv2, "VideoCapture", _raise)

    assert video_timeline.probe_video_duration_ms(video_path) is None


def _segment(
    store,
    race_dir,
    *,
    camera_index=1,
    name="camera.mkv",
    started_at_ms=10_000,
    ended_at_ms=20_000,
    timing_error_ms=1_500,
    verify_media=True,
    clock_source=video_timeline.DEFAULT_CLOCK_SOURCE,
    race_id="",
):
    path = race_dir / "videos" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"video")
    segment = store.start_segment(
        source_id=f"camera_{camera_index:02d}",
        camera_index=camera_index,
        video_path=path,
        started_at_ms=started_at_ms,
        clock_source=clock_source,
        timing_error_ms=timing_error_ms,
        race_id=race_id,
    )
    if ended_at_ms is not None:
        finish_kwargs = {}
        if verify_media:
            finish_kwargs = {
                "media_duration_ms": ended_at_ms - started_at_ms,
                "media_started_at_ms": started_at_ms,
            }
        store.finish_segment(
            segment.segment_id,
            ended_at_ms=ended_at_ms,
            **finish_kwargs,
        )
    return segment, path


def test_locates_passage_with_clock_offset_and_pre_roll(tmp_path):
    store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    _, path = _segment(store, tmp_path)

    lookup = store.locate_passage(
        14_000,
        clock_offset_ms=500,
        pre_roll_ms=3_000,
    )

    assert lookup.status == "located"
    assert len(lookup.locations) == 1
    location = lookup.locations[0]
    assert location.video_path == path.absolute()
    assert location.passage_position_ms == 4_500
    assert location.playback_position_ms == 1_500
    assert location.clock_offset_ms == 500
    assert location.timing_error_ms == 1_500


def test_reports_before_after_and_restart_gap(tmp_path):
    store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    _segment(
        store,
        tmp_path,
        name="first.mkv",
        started_at_ms=10_000,
        ended_at_ms=12_000,
    )
    _segment(
        store,
        tmp_path,
        name="second.mkv",
        started_at_ms=13_000,
        ended_at_ms=20_000,
    )

    assert store.locate_passage(9_000).status == "before_recording"
    assert store.locate_passage(12_500).status == "recording_gap"
    assert store.locate_passage(21_000).status == "after_recording"
    lookup = store.locate_passage(14_000)
    assert lookup.locations[0].video_path.name == "second.mkv"


def test_reports_hls_playlist_with_missing_media_segment_as_missing(tmp_path):
    store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    playlist = tmp_path / "review_buffer" / "evidence.m3u8"
    playlist.parent.mkdir(parents=True)
    playlist.write_text(
        "#EXTM3U\n#EXTINF:2.000,\nmissing.ts\n#EXT-X-ENDLIST\n",
        encoding="utf-8",
    )
    store.add_completed_segment(
        source_id="camera_01_review",
        camera_index=1,
        video_path=playlist,
        media_started_at_ms=10_000,
        media_duration_ms=2_000,
        clock_source=video_timeline.DEFAULT_CLOCK_SOURCE,
        timing_error_ms=0,
        end_reason="passage_review_window",
    )

    lookup = store.locate_passage(11_000)

    assert lookup.locations[0].status == "missing_file"


def test_prefers_playable_archive_over_broken_hls_for_same_source(tmp_path):
    store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    archive = tmp_path / "videos" / "archive.mkv"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"video")
    store.add_completed_segment(
        source_id="camera_01_review",
        camera_index=1,
        video_path=archive,
        media_started_at_ms=10_000,
        media_duration_ms=20_000,
        clock_source=video_timeline.DEFAULT_CLOCK_SOURCE,
        timing_error_ms=0,
        end_reason="archive_segment",
    )
    playlist = tmp_path / "review_buffer" / "evidence.m3u8"
    playlist.parent.mkdir(parents=True)
    playlist.write_text(
        "#EXTM3U\n#EXTINF:2.000,\nmissing.ts\n#EXT-X-ENDLIST\n",
        encoding="utf-8",
    )
    store.add_completed_segment(
        source_id="camera_01_review",
        camera_index=1,
        video_path=playlist,
        media_started_at_ms=14_000,
        media_duration_ms=4_000,
        clock_source=video_timeline.DEFAULT_CLOCK_SOURCE,
        timing_error_ms=0,
        end_reason="passage_review_window",
    )

    lookup = store.locate_passage(15_000)

    assert lookup.locations[0].status == "located"
    assert lookup.locations[0].video_path == archive.absolute()


def test_reports_verified_clip_within_timing_error_as_near_boundary(tmp_path):
    store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    _segment(
        store,
        tmp_path,
        name="external-near-boundary.mkv",
        started_at_ms=10_000,
        ended_at_ms=20_000,
        timing_error_ms=100,
        clock_source="external_test_clock",
    )

    before = store.locate_passage(9_950)
    after = store.locate_passage(20_050)

    assert before.status == "near_boundary"
    assert before.locations[0].status == "near_boundary"
    assert before.locations[0].passage_position_ms == 0
    assert after.status == "near_boundary"
    assert after.locations[0].passage_position_ms == 10_000
    assert store.locate_passage(9_899).status == "before_recording"


def test_external_segment_is_filtered_by_race_identity(tmp_path):
    store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    _segment(
        store,
        tmp_path,
        name="race-1.mkv",
        clock_source="external_test_clock",
        race_id="race-1",
    )

    assert store.locate_passage(15_000, race_id="race-1").status == "located"
    assert store.locate_passage(15_000, race_id="race-2").status == "race_mismatch"


def test_schema_v1_live_segment_without_race_id_remains_compatible(tmp_path):
    video_path = tmp_path / "videos" / "legacy.mkv"
    video_path.parent.mkdir()
    video_path.write_bytes(b"video")
    journal = tmp_path / "video_timeline.jsonl"
    records = [
        {
            "schema_version": 1,
            "record_type": "segment_started",
            "segment_id": "legacy-segment",
            "source_id": "camera_01",
            "camera_index": 1,
            "video_path": "videos/legacy.mkv",
            "started_at_ms": 10_000,
            "clock_source": video_timeline.DEFAULT_CLOCK_SOURCE,
            "timing_error_ms": 2_000,
        },
        {
            "schema_version": 1,
            "record_type": "segment_ended",
            "segment_id": "legacy-segment",
            "ended_at_ms": 20_000,
            "end_reason": "stopped",
            "media_duration_ms": 10_000,
            "media_started_at_ms": 10_000,
        },
    ]
    journal.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )

    store = VideoTimelineStore(journal)

    assert store.segments()[0].race_id == ""
    assert store.locate_passage(15_000, race_id="race-2").status == "located"


def test_returns_one_location_per_camera(tmp_path):
    store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    _segment(store, tmp_path, camera_index=1, name="camera_01.mkv")
    _segment(store, tmp_path, camera_index=2, name="camera_02.mkv")

    lookup = store.locate_passage(15_000)

    assert [item.segment.camera_index for item in lookup.locations] == [1, 2]


def test_legacy_segment_without_media_duration_is_openable_but_unverified(tmp_path):
    store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    _segment(store, tmp_path, verify_media=False)

    lookup = store.locate_passage(15_000)

    assert lookup.status == "unverified"
    assert lookup.locations[0].status == "unverified"
    assert lookup.locations[0].passage_position_ms == 5_000


def test_reports_wall_clock_hit_outside_verified_media_range(tmp_path):
    store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    path = tmp_path / "videos" / "camera.mkv"
    path.parent.mkdir()
    path.write_bytes(b"video")
    segment = store.start_segment(
        source_id="camera_01",
        camera_index=1,
        video_path=path,
        started_at_ms=10_000,
    )
    store.finish_segment(
        segment.segment_id,
        ended_at_ms=20_000,
        media_duration_ms=6_000,
        media_started_at_ms=12_000,
    )

    outside = store.locate_passage(11_000)
    located = store.locate_passage(15_000)

    assert outside.status == "outside_media"
    assert outside.locations[0].status == "outside_media"
    assert located.status == "located"
    assert located.locations[0].passage_position_ms == 3_000


def test_open_segment_is_not_located_beyond_current_time(tmp_path):
    store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    _segment(
        store,
        tmp_path,
        started_at_ms=10_000,
        ended_at_ms=None,
    )

    active = store.locate_passage(15_000, current_time_ms=16_000)
    future = store.locate_passage(17_000, current_time_ms=16_000)

    assert active.status == "recording"
    assert active.locations[0].status == "recording"
    assert future.status == "after_recording"


def test_restart_restores_segment_boundaries_and_relative_paths(tmp_path):
    journal = tmp_path / "video_timeline.jsonl"
    store = VideoTimelineStore(journal)
    segment, path = _segment(store, tmp_path)

    restored = VideoTimelineStore(journal)
    restored_segment = restored.segments()[0]

    assert restored_segment.segment_id == segment.segment_id
    assert restored_segment.ended_at_ms == 20_000
    assert restored_segment.media_duration_ms == 10_000
    assert restored_segment.media_started_at_ms == 10_000
    assert restored.resolve_video_path(restored_segment) == path.absolute()
    assert not restored_segment.video_path.startswith(str(tmp_path))


def test_recovers_only_an_incomplete_final_record(tmp_path):
    journal = tmp_path / "video_timeline.jsonl"
    store = VideoTimelineStore(journal)
    _segment(store, tmp_path)
    with journal.open("ab") as output:
        output.write(b'{"schema_version":1,"record_type":"segment_started"')

    restored = VideoTimelineStore(journal)

    assert restored.recovered_incomplete_tail is True
    assert len(restored.segments()) == 1
    for line in journal.read_text(encoding="utf-8").splitlines():
        json.loads(line)


def test_restart_closes_segment_left_open_by_previous_process(
    tmp_path,
    monkeypatch,
):
    started_at_ms = 1_787_217_100_000
    written_at_ms = 1_787_217_118_000
    journal = tmp_path / "video_timeline.jsonl"
    store = VideoTimelineStore(journal)
    _, path = _segment(
        store,
        tmp_path,
        started_at_ms=started_at_ms,
        ended_at_ms=None,
    )
    os.utime(path, (written_at_ms / 1000.0, written_at_ms / 1000.0))
    monkeypatch.setattr(video_timeline, "probe_video_duration_ms", lambda _path: 5_000)

    restored = VideoTimelineStore(journal)

    assert restored.recover_open_segments() == 1
    segment = restored.segments()[0]
    assert segment.ended_at_ms == written_at_ms
    assert segment.media_duration_ms == 5_000
    assert segment.media_started_at_ms == written_at_ms - 5_000
    assert segment.end_reason == "recovered_after_restart"
    assert restored.locate_passage(started_at_ms + 5_000).status == "outside_media"
    assert restored.locate_passage(started_at_ms + 15_000).status == "located"
