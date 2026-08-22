import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from realtime.external_clip_import import (
    EXTERNAL_CLOCK_SOURCE,
    ExternalClipImportCancelled,
    ExternalClipImportError,
    import_external_clip_sidecar,
    load_external_clip_sidecar,
    race_id_from_passage_store,
)
from realtime.video_timeline import VideoTimelineStore


def _timestamp_ms(value):
    return int(datetime.fromisoformat(value).timestamp() * 1000)


def _write_sidecar(tmp_path, clips, *, race_id="race-1"):
    sidecar = tmp_path / "clips.json"
    sidecar.write_text(
        json.dumps({"schema_version": 1, "race_id": race_id, "clips": clips}),
        encoding="utf-8",
    )
    return sidecar


def _clip(tmp_path, name, **overrides):
    video_path = tmp_path / name
    video_path.write_bytes(b"video")
    payload = {
        "video_path": name,
        "source_id": "high_speed_01",
        "camera_index": 1,
        "capture_timestamp": "2026-08-10T10:00:00.500+08:00",
        "timestamp_anchor": "start",
        "timing_error_ms": 20,
    }
    payload.update(overrides)
    return payload


def test_imports_start_anchored_clip_and_locates_passage(tmp_path):
    clip = _clip(tmp_path, "start.mkv")
    store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")

    result = import_external_clip_sidecar(
        store,
        _write_sidecar(tmp_path, [clip]),
        expected_race_id="race-1",
        duration_probe=lambda _path: 4_000,
    )

    started_at_ms = _timestamp_ms(clip["capture_timestamp"])
    segment = result.segments[0]
    assert result.created_count == 1
    assert segment.started_at_ms == started_at_ms
    assert segment.ended_at_ms == started_at_ms + 4_000
    assert segment.media_started_at_ms == started_at_ms
    assert segment.clock_source == EXTERNAL_CLOCK_SOURCE
    assert segment.race_id == "race-1"
    assert (
        store.locate_passage(
            started_at_ms + 2_000,
            race_id="race-1",
        ).status
        == "located"
    )
    assert (
        store.locate_passage(
            started_at_ms + 2_000,
            race_id="race-2",
        ).status
        == "race_mismatch"
    )


def test_imports_end_anchored_clip(tmp_path):
    clip = _clip(
        tmp_path,
        "end.mkv",
        capture_timestamp="2026-08-10T10:00:10.000+08:00",
        timestamp_anchor="end",
    )
    store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")

    result = import_external_clip_sidecar(
        store,
        _write_sidecar(tmp_path, [clip]),
        expected_race_id="race-1",
        duration_probe=lambda _path: 2_500,
    )

    ended_at_ms = _timestamp_ms(clip["capture_timestamp"])
    segment = result.segments[0]
    assert segment.started_at_ms == ended_at_ms - 2_500
    assert segment.ended_at_ms == ended_at_ms
    assert segment.media_started_at_ms == ended_at_ms - 2_500


def test_overlapping_cameras_return_one_location_each(tmp_path):
    first = _clip(tmp_path, "first.mkv")
    second = _clip(
        tmp_path,
        "second.mkv",
        source_id="high_speed_02",
        camera_index=2,
    )
    store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    import_external_clip_sidecar(
        store,
        _write_sidecar(tmp_path, [first, second]),
        expected_race_id="race-1",
        duration_probe=lambda _path: 5_000,
    )

    lookup = store.locate_passage(
        _timestamp_ms(first["capture_timestamp"]) + 1_000
    )

    assert lookup.status == "located"
    assert [item.segment.camera_index for item in lookup.locations] == [1, 2]


def test_sparse_clips_preserve_recording_gap(tmp_path):
    first = _clip(tmp_path, "first.mkv")
    second = _clip(
        tmp_path,
        "second.mkv",
        capture_timestamp="2026-08-10T10:00:10.500+08:00",
    )
    store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    import_external_clip_sidecar(
        store,
        _write_sidecar(tmp_path, [first, second]),
        expected_race_id="race-1",
        duration_probe=lambda _path: 2_000,
    )

    gap_time_ms = _timestamp_ms(first["capture_timestamp"]) + 5_000

    assert store.locate_passage(gap_time_ms).status == "recording_gap"


def test_duplicate_import_is_idempotent(tmp_path):
    clip = _clip(tmp_path, "clip.mkv")
    sidecar = _write_sidecar(tmp_path, [clip])
    journal = tmp_path / "video_timeline.jsonl"
    store = VideoTimelineStore(journal)

    first = import_external_clip_sidecar(
        store,
        sidecar,
        expected_race_id="race-1",
        duration_probe=lambda _path: 3_000,
    )
    second = import_external_clip_sidecar(
        store,
        sidecar,
        expected_race_id="race-1",
        duration_probe=lambda _path: 3_000,
    )

    assert first.created_count == 1
    assert second.created_count == 0
    assert second.duplicate_count == 1
    assert len(store.segments()) == 1
    assert len(journal.read_text(encoding="utf-8").splitlines()) == 2


def test_reimport_repairs_matching_open_segment(tmp_path):
    clip = _clip(tmp_path, "clip.mkv")
    sidecar = _write_sidecar(tmp_path, [clip])
    store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    started_at_ms = _timestamp_ms(clip["capture_timestamp"])
    store.start_segment(
        source_id=clip["source_id"],
        camera_index=clip["camera_index"],
        video_path=tmp_path / clip["video_path"],
        started_at_ms=started_at_ms,
        clock_source=EXTERNAL_CLOCK_SOURCE,
        timing_error_ms=clip["timing_error_ms"],
        race_id="race-1",
    )

    result = import_external_clip_sidecar(
        store,
        sidecar,
        expected_race_id="race-1",
        duration_probe=lambda _path: 3_000,
    )

    assert result.repaired_count == 1
    assert len(store.segments()) == 1
    assert store.segments()[0].ended_at_ms == started_at_ms + 3_000


@pytest.mark.parametrize(
    "capture_timestamp",
    [None, "2026-08-10T10:00:00.500", "2026-08-10T02:00:00.500+00:00"],
)
def test_rejects_missing_or_non_beijing_timestamp_without_mutating_timeline(
    tmp_path,
    capture_timestamp,
):
    valid = _clip(tmp_path, "valid.mkv")
    invalid = _clip(tmp_path, "invalid.mkv")
    if capture_timestamp is None:
        invalid.pop("capture_timestamp")
    else:
        invalid["capture_timestamp"] = capture_timestamp
    store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")

    with pytest.raises(ExternalClipImportError, match="capture_timestamp"):
        import_external_clip_sidecar(
            store,
            _write_sidecar(tmp_path, [valid, invalid]),
            expected_race_id="race-1",
            duration_probe=lambda _path: 2_000,
        )

    assert store.segments() == ()


def test_rejects_changed_metadata_for_an_imported_path(tmp_path):
    clip = _clip(tmp_path, "clip.mkv")
    sidecar = _write_sidecar(tmp_path, [clip])
    store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    import_external_clip_sidecar(
        store,
        sidecar,
        expected_race_id="race-1",
        duration_probe=lambda _path: 3_000,
    )
    clip["timing_error_ms"] = 50
    _write_sidecar(tmp_path, [clip])

    with pytest.raises(ExternalClipImportError, match="conflicts"):
        import_external_clip_sidecar(
            store,
            sidecar,
            expected_race_id="race-1",
            duration_probe=lambda _path: 3_000,
        )

    assert len(store.segments()) == 1


def test_rejects_sidecar_for_another_race_without_mutating_timeline(tmp_path):
    clip = _clip(tmp_path, "clip.mkv")
    store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")

    with pytest.raises(ExternalClipImportError, match="race_id"):
        import_external_clip_sidecar(
            store,
            _write_sidecar(tmp_path, [clip], race_id="race-2"),
            expected_race_id="race-1",
            duration_probe=lambda _path: 3_000,
        )

    assert store.segments() == ()


@pytest.mark.parametrize(
    "second_overrides, message",
    [
        ({"camera_index": 2}, "source_id maps"),
        ({"source_id": "high_speed_02"}, "camera_index maps"),
    ],
)
def test_rejects_inconsistent_source_camera_mapping(
    tmp_path,
    second_overrides,
    message,
):
    first = _clip(tmp_path, "first.mkv")
    second = _clip(tmp_path, "second.mkv", **second_overrides)
    store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")

    with pytest.raises(ExternalClipImportError, match=message):
        import_external_clip_sidecar(
            store,
            _write_sidecar(tmp_path, [first, second]),
            expected_race_id="race-1",
            duration_probe=lambda _path: 3_000,
        )

    assert store.segments() == ()


def test_external_camera_index_can_match_regular_video_camera_index(tmp_path):
    clip = _clip(tmp_path, "external.mkv")
    store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    live_path = tmp_path / "live.mkv"
    live_path.write_bytes(b"video")
    segment = store.start_segment(
        source_id="camera_01",
        camera_index=1,
        video_path=live_path,
        started_at_ms=1_000,
    )
    store.finish_segment(segment.segment_id, ended_at_ms=2_000)

    result = import_external_clip_sidecar(
        store,
        _write_sidecar(tmp_path, [clip]),
        expected_race_id="race-1",
        duration_probe=lambda _path: 3_000,
    )

    assert result.created_count == 1
    assert [segment.camera_index for segment in store.segments()] == [1, 1]
    assert [segment.source_id for segment in store.segments()] == [
        "camera_01",
        "high_speed_01",
    ]


def test_external_camera_index_remains_unique_within_high_speed_sources(tmp_path):
    store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    existing_path = tmp_path / "existing.mkv"
    existing_path.write_bytes(b"video")
    store.add_completed_segment(
        source_id="high_speed_01",
        camera_index=1,
        video_path=existing_path,
        media_started_at_ms=1_000,
        media_duration_ms=3_000,
        clock_source=EXTERNAL_CLOCK_SOURCE,
        timing_error_ms=20,
        end_reason="external_clip_import",
        race_id="race-1",
    )
    clip = _clip(
        tmp_path,
        "new.mkv",
        source_id="high_speed_02",
        camera_index=1,
    )

    with pytest.raises(ExternalClipImportError, match="camera_index maps"):
        import_external_clip_sidecar(
            store,
            _write_sidecar(tmp_path, [clip]),
            expected_race_id="race-1",
            duration_probe=lambda _path: 3_000,
        )


def test_new_clip_is_appended_as_one_completed_journal_write(
    tmp_path,
    monkeypatch,
):
    clip = _clip(tmp_path, "clip.mkv")
    store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    append_calls = []
    original_append = store._append_records

    def _append(payloads):
        append_calls.append(payloads)
        original_append(payloads)

    monkeypatch.setattr(store, "_append_records", _append)

    import_external_clip_sidecar(
        store,
        _write_sidecar(tmp_path, [clip]),
        expected_race_id="race-1",
        duration_probe=lambda _path: 3_000,
    )

    assert len(append_calls) == 1
    assert [item["record_type"] for item in append_calls[0]] == [
        "segment_started",
        "segment_ended",
    ]


def test_incomplete_external_segment_is_not_mtime_recovered_or_located(tmp_path):
    clip = _clip(tmp_path, "clip.mkv")
    sidecar = _write_sidecar(tmp_path, [clip])
    store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    started_at_ms = _timestamp_ms(clip["capture_timestamp"])
    store.start_segment(
        source_id=clip["source_id"],
        camera_index=clip["camera_index"],
        video_path=tmp_path / clip["video_path"],
        started_at_ms=started_at_ms,
        clock_source=EXTERNAL_CLOCK_SOURCE,
        timing_error_ms=clip["timing_error_ms"],
        race_id="race-1",
    )

    assert store.recover_open_segments() == 0
    assert store.locate_passage(started_at_ms + 1_000).status == "no_segments"

    result = import_external_clip_sidecar(
        store,
        sidecar,
        expected_race_id="race-1",
        duration_probe=lambda _path: 3_000,
    )

    assert result.repaired_count == 1
    assert store.locate_passage(started_at_ms + 1_000).status == "located"


def test_resolves_single_race_id_from_passage_store():
    store = SimpleNamespace(
        events=lambda: (
            SimpleNamespace(race_id="race-1"),
            SimpleNamespace(race_id="race-1"),
        )
    )

    assert race_id_from_passage_store(store) == "race-1"


@pytest.mark.parametrize(
    "race_ids, message",
    [([], "at least one"), (["race-1", "race-2"], "multiple")],
)
def test_rejects_missing_or_mixed_passage_race_identity(race_ids, message):
    store = SimpleNamespace(
        events=lambda: tuple(SimpleNamespace(race_id=value) for value in race_ids)
    )

    with pytest.raises(ExternalClipImportError, match=message):
        race_id_from_passage_store(store)


def test_imported_race_identity_survives_timeline_reload(tmp_path):
    clip = _clip(tmp_path, "clip.mkv")
    journal = tmp_path / "video_timeline.jsonl"
    import_external_clip_sidecar(
        VideoTimelineStore(journal),
        _write_sidecar(tmp_path, [clip]),
        expected_race_id="race-1",
        duration_probe=lambda _path: 3_000,
    )

    restored = VideoTimelineStore(journal)
    segment = restored.segments()[0]
    passage_time_ms = _timestamp_ms(clip["capture_timestamp"]) + 1_000

    assert segment.race_id == "race-1"
    assert restored.locate_passage(passage_time_ms, race_id="race-1").status == "located"
    assert (
        restored.locate_passage(passage_time_ms, race_id="race-2").status
        == "race_mismatch"
    )


def test_sidecar_validation_can_be_cancelled_before_media_probe(tmp_path):
    clip = _clip(tmp_path, "clip.mkv")
    sidecar = _write_sidecar(tmp_path, [clip])
    probe_calls = []

    with pytest.raises(ExternalClipImportCancelled):
        load_external_clip_sidecar(
            sidecar,
            expected_race_id="race-1",
            duration_probe=lambda path: probe_calls.append(path) or 3_000,
            cancel_check=lambda: True,
        )

    assert probe_calls == []
