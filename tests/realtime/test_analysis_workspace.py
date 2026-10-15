import json

from realtime.analysis_workspace import AnalysisWorkspace, session_id_for_path
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
