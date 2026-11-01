import json
from dataclasses import replace

import pytest

from realtime.analysis_workspace import AnalysisWorkspace, session_id_for_path
from realtime.passage_evidence import VideoClockCalibrationStore
from realtime.passage_receiver import PassageEvent
from realtime.video_timeline import VideoTimelineStore


DAY = 1_789_747_200_000  # 2026-09-19 00:00 Beijing time


def _event(race_id, index, chip_ms, *, active=True, revision=1):
    return PassageEvent(
        event_id=f"{race_id}-stage-1-passage-{index}", race_id=race_id, stage_id="stage-1",
        group_id="1", sequence=index, bib=str(index), passage_time_ms=(chip_ms - DAY) % 86_400_000,
        lap=0, passage_timestamp_ms=chip_ms, received_at_ms=chip_ms - 400,
        is_active=active, revision=revision,
    )


def _write_events(root, events):
    (root / "cyclerace_passage_events.jsonl").write_text(
        "".join(json.dumps(event.to_payload(), ensure_ascii=False) + "\n" for event in events),
        encoding="utf-8",
    )


def test_scope_prefers_saved_event_scope_over_majority(tmp_path):
    root = tmp_path / "race"
    root.mkdir()
    t = DAY + 10 * 3_600_000
    _write_events(root, [_event("other", 1, t), _event("other", 2, t + 1), _event("main", 3, t + 2)])
    (root / "review_event_scope.json").write_text(
        json.dumps({"schema_version": 1, "race_id": "main", "stage_id": "stage-1"}), encoding="utf-8",
    )

    workspace = AnalysisWorkspace(root)

    assert workspace.race_id == "main"
    assert [read.bib for read in workspace.reads] == ["3"]
    assert workspace.race_date == "2026-09-19"


def test_metadata_stage_date_is_not_a_passage_filter(tmp_path):
    _write_events(tmp_path, [_event("main", 1, DAY + 36_000_000)])
    (tmp_path / "cyclerace_race_metadata.json").write_text(json.dumps({
        "schema_version": 1, "message_type": "race_metadata", "race_id": "main",
        "stage_id": "stage-1", "revision": 1, "emitted_at_ms": 1,
        "stage_date": "2026-10-18",
    }), encoding="utf-8")
    workspace = AnalysisWorkspace(tmp_path)
    assert workspace.race_date == "2026-10-18"
    assert [read.bib for read in workspace.reads] == ["1"]


def test_archive_segments_skip_clips_missing_files_and_other_races(tmp_path):
    root = tmp_path / "race"
    (root / "videos").mkdir(parents=True)
    t = DAY + 10 * 3_600_000
    _write_events(root, [_event("main", 1, t)])
    timeline = VideoTimelineStore(root / "video_timeline.jsonl")

    def add(name, start, *, race_id="main", create=True, camera=1):
        path = root / "videos" / name
        if create:
            path.write_bytes(b"\0")
        timeline.add_completed_segment(
            source_id=f"camera_0{camera}_review", camera_index=camera, video_path=path,
            media_started_at_ms=start, media_duration_ms=300_000,
            clock_source="videopipe_system_clock", timing_error_ms=3_000,
            end_reason="continuous_archive_fallback", race_id=race_id,
        )

    add("camera_01_20260919_101635_843599_archive_0001.mkv", t + 300_000)
    add("camera_01_20260919_101635_843599_archive_0000.mkv", t)
    add("camera_01_20260919_101635_843599_review_0000000001.ts", t)
    add("camera_01_20260919_113420_780632_archive_0000.mkv", t + 900_000, create=False)
    add("camera_01_20260919_090000_000000_archive_0000.mkv", t - 900_000, race_id="other")
    add("camera_02_20260919_101635_900000_archive_0000.mkv", t, camera=2)
    before = (root / "video_timeline.jsonl").read_bytes()

    workspace = AnalysisWorkspace(root)
    segments = workspace.archive_segments(1)

    assert [item.path.name for item in segments] == [
        "camera_01_20260919_101635_843599_archive_0000.mkv",
        "camera_01_20260919_101635_843599_archive_0001.mkv",
    ]
    assert {item.session_id for item in segments} == {"camera_01_20260919_101635_843599"}
    assert workspace.camera_indexes() == (1, 2)
    assert (root / "video_timeline.jsonl").read_bytes() == before


def test_finish_lines_fall_back_to_long_image_lines(tmp_path):
    root = tmp_path / "race"
    root.mkdir()
    _write_events(root, [_event("main", 1, DAY + 36_000_000)])
    (root / "long_image_lines.json").write_text(json.dumps({
        "schema_version": 1,
        "finish_lines": [{"camera_index": 1, "x1": 0.23, "y1": 0.11, "x2": 0.21, "y2": 0.92, "band_width": 0.08}],
    }), encoding="utf-8")

    assert AnalysisWorkspace(root).finish_lines()[1]["x2"] == 0.21
    assert session_id_for_path(root / "camera_01_x_archive_0012.mkv") == "camera_01_x"


def _archived(root, name, start, duration=300_000, camera=1):
    path = root / "videos" / name
    path.write_bytes(b"\0")
    VideoTimelineStore(root / "video_timeline.jsonl").add_completed_segment(
        source_id=f"camera_0{camera}_review", camera_index=camera, video_path=path,
        media_started_at_ms=start, media_duration_ms=duration,
        clock_source="videopipe_system_clock", timing_error_ms=3_000,
        end_reason="continuous_archive_fallback", race_id="",
    )


def test_archive_segments_leave_out_another_visit_of_the_same_folder(tmp_path):
    """The race folder also held an indoor test a month later (real case)."""

    root = tmp_path / "race"
    (root / "videos").mkdir(parents=True)
    t = DAY + 36_000_000
    _write_events(root, [_event("main", 1, t), _event("main", 2, t + 3_600_000)])
    _archived(root, "camera_01_20260919_094900_000000_archive_0000.mkv", t - 300_000)
    _archived(root, "camera_01_20260919_101635_000000_archive_0000.mkv", t + 3_600_000)
    _archived(root, "camera_01_20261015_161337_133871_archive_0000.mkv", t + 26 * 3_600_000)

    names = [item.path.name for item in AnalysisWorkspace(root).archive_segments(1)]

    assert names == ["camera_01_20260919_094900_000000_archive_0000.mkv",
                     "camera_01_20260919_101635_000000_archive_0000.mkv"]


def test_archive_segments_keep_a_night_race_and_a_folder_without_chips(tmp_path):
    root = tmp_path / "race"
    (root / "videos").mkdir(parents=True)
    late = DAY + 23 * 3_600_000 + 1_800_000
    _write_events(root, [_event("main", 1, late), _event("main", 2, late + 3_600_000)])
    _archived(root, "camera_01_20260919_235000_000000_archive_0000.mkv", late - 600_000)
    _archived(root, "camera_01_20260920_003000_000000_archive_0000.mkv", late + 3_600_000)
    assert len(AnalysisWorkspace(root).archive_segments(1)) == 2

    quiet = tmp_path / "quiet"
    (quiet / "videos").mkdir(parents=True)
    (quiet / "review_event_scope.json").write_text('{"race_id": "main", "stage_id": "stage-1"}')
    _archived(quiet, "camera_01_20260919_235000_000000_archive_0000.mkv", DAY + 36_000_000)
    (quiet / "cyclerace_passage_events.jsonl").write_text("", encoding="utf-8")

    assert len(AnalysisWorkspace(quiet).archive_segments(1)) == 1


def test_overlapping_recordings_of_one_camera_cover_each_moment_once(tmp_path):
    """古浪 2026-10-18: a second instance recorded beside the first for 40 minutes."""

    root = tmp_path / "race"
    (root / "videos").mkdir(parents=True)
    t = DAY + 36_000_000
    _write_events(root, [_event("main", 1, t + 60_000)])
    first, second, third = (f"camera_01_20260919_{clock}_000000" for clock in ("100000", "100200", "101140"))
    _archived(root, f"{first}_archive_0000.mkv", t, duration=150_000)
    _archived(root, f"{first}_archive_0001.mkv", t + 150_000, duration=150_000)
    _archived(root, f"{first}_archive_0002.mkv", t + 300_000)
    _archived(root, f"{first}_archive_0003.mkv", t + 600_000, duration=100_000)
    _archived(root, f"{second}_archive_0000.mkv", t + 120_000)
    # Back-to-back sessions whose clocks touch are not a second recording.
    _archived(root, f"{third}_archive_0000.mkv", t + 699_900)

    segments = AnalysisWorkspace(root).archive_segments(1)

    # The later session takes over; the earlier one keeps what nobody else covers.
    assert [(item.path.name, item.excluded_ranges) for item in segments] == [
        (f"{first}_archive_0000.mkv", ((t + 120_000, t + 150_000),)),
        (f"{second}_archive_0000.mkv", ()),
        (f"{first}_archive_0002.mkv", ((t + 300_000, t + 420_000),)),
        (f"{first}_archive_0003.mkv", ()),
        (f"{third}_archive_0000.mkv", ()),
    ]


SESSION = "camera_01_20260919_100000_000000"


def _record_anchor(root, event, *, offset_ms=1_250, session=SESSION, camera=1, **provenance):
    return VideoClockCalibrationStore(root / "video_clock_calibrations.jsonl").record(
        camera_index=camera,
        session_key=f"camera_01_review|{root / 'videos'}|{session}",
        offset_ms=offset_ms,
        anchor_event_id=event.event_id,
        anchor_bib=event.bib,
        calibrated_at_ms=DAY,
        **provenance,
    )


def _provenance(event, **overrides):
    fields = {
        "anchor_event_revision": event.revision,
        "anchor_chip_time_ms": event.timeline_timestamp_ms,
        "anchor_frame": {"segment_id": "segment-1", "position_ms": 3_700},
        "anchor_race_id": event.race_id,
        "anchor_stage_id": event.stage_id,
    }
    fields.update(overrides)
    return fields


def test_manual_anchors_use_the_recorded_originals(tmp_path):
    root = tmp_path / "race"
    root.mkdir()
    t = DAY + 10 * 3_600_000
    first = _event("main", 1, t)
    _write_events(root, [first])
    _record_anchor(root, first, offset_ms=-1_300, **_provenance(first))

    anchor, = AnalysisWorkspace(root).manual_anchors(1)

    assert anchor.chip_ms == t and anchor.offset_ms == -1_300
    assert anchor.label == first.bib and anchor.reference == SESSION
    assert anchor.kind == "manual_calibration"


def test_manual_anchors_accept_a_legacy_record_on_its_current_event(tmp_path):
    root = tmp_path / "race"
    root.mkdir()
    t = DAY + 10 * 3_600_000
    first = _event("main", 1, t)
    _write_events(root, [first])
    _record_anchor(root, first)  # no provenance: the shape older records have

    anchor, = AnalysisWorkspace(root).manual_anchors(1)

    assert anchor.chip_ms == t and anchor.reference == SESSION


@pytest.mark.parametrize("change", ["revision", "time", "race", "stage", "inactive"])
def test_manual_anchors_reject_an_anchor_that_no_longer_stands(tmp_path, change):
    root = tmp_path / change
    root.mkdir()
    t = DAY + 10 * 3_600_000
    first = _event("main", 1, t)
    events = [first]
    provenance = _provenance(first)
    if change == "revision":  # CycleRace revised the exact anchor event
        events.append(_event("main", 1, t, revision=2))
    elif change == "time":  # the stored original chip time moved
        provenance["anchor_chip_time_ms"] = t - 5_000
    elif change == "race":  # the record belongs to another race (reused folder)
        provenance["anchor_race_id"] = "other-race"
    elif change == "stage":
        provenance["anchor_stage_id"] = "stage-9"
    elif change == "inactive":  # the event was voided
        events = [replace(first, is_active=False)]
    _write_events(root, events)
    (root / "review_event_scope.json").write_text(
        json.dumps({"schema_version": 1, "race_id": "main", "stage_id": "stage-1"}),
        encoding="utf-8",
    )
    _record_anchor(root, first, **_provenance(first, **provenance))
    # The legacy record on the same anchor would still stand: provenance is
    # what makes the difference, so both records exist side by side.
    _record_anchor(root, first, session="camera_01_20260919_100100_000000")

    workspace = AnalysisWorkspace(root)

    anchors = workspace.manual_anchors(1)
    if change == "inactive":
        assert anchors == ()
    else:
        assert [anchor.reference for anchor in anchors] == ["camera_01_20260919_100100_000000"]
