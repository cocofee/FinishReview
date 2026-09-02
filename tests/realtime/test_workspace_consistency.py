import json

import pytest

from realtime.passage_evidence import (
    REGULAR_SOURCE,
    PassageEvidenceAssociationStore,
    VideoClockCalibrationStore,
)
from realtime.passage_receiver import PassageEvent, PassageEventStore
from realtime.review_clip import PassageReviewBindingStore
from realtime.video_timeline import VideoTimelineStore
from realtime.workspace_consistency import (
    WorkspaceConsistencyError,
    WorkspaceConsistencyService,
)


def _workspace(tmp_path):
    passages = PassageEventStore(tmp_path / "passages.jsonl")
    timeline = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    associations = PassageEvidenceAssociationStore(tmp_path / "associations.jsonl")
    calibrations = VideoClockCalibrationStore(tmp_path / "calibrations.jsonl")
    bindings = PassageReviewBindingStore(tmp_path / "review_clips.jsonl")
    event = PassageEvent(
        "event-1", "race-1", "stage-1", "group-1", 1, "chip-7", "7",
        13_000, 1, "cyclerace", 14_000, 1, 1, "passage",
    )
    passages.append(event)
    video = tmp_path / "camera.mkv"
    video.write_bytes(b"video")
    segment = timeline.start_segment(
        source_id="camera_01",
        camera_index=1,
        video_path=video,
        started_at_ms=10_000,
        race_id="race-1",
    )
    timeline.finish_segment(segment.segment_id, ended_at_ms=18_000)
    associations.confirm(
        passage_event_id=event.event_id,
        bib=event.bib,
        confirmed_source=REGULAR_SOURCE,
        segment_id=segment.segment_id,
        frame_index=75,
        position_ms=3_000,
        marker_x_normalized=0.5,
        marker_y_normalized=0.5,
        confirmed_at_ms=15_000,
    )
    calibrations.record(
        camera_index=1,
        session_key="session-1",
        offset_ms=0,
        anchor_event_id=event.event_id,
        anchor_bib=event.bib,
        calibrated_at_ms=15_000,
    )
    clip = bindings.get_or_add_clip(
        race_id="race-1",
        camera_index=1,
        source_id="camera_01",
        started_at_ms=10_000,
        ended_at_ms=18_000,
        playlist_path=video,
        segment_signature="signature-1",
        timeline_segment_id=segment.segment_id,
    )
    bindings.bind(
        event_id=event.event_id,
        revision=event.revision,
        camera_index=1,
        clip_id=clip.clip_id,
        passage_timestamp_ms=event.timeline_timestamp_ms,
        passage_offset_ms=3_000,
    )
    return WorkspaceConsistencyService(
        passages, timeline, associations, calibrations, bindings
    )


def test_consistency_report_accepts_linked_journals_and_is_read_only(tmp_path):
    service = _workspace(tmp_path)
    journal_paths = (
        service.passage_store.journal_path,
        service.timeline_store.journal_path,
        service.association_store.journal_path,
        service.calibration_store.journal_path,
        service.binding_store.journal_path,
    )
    before = {path: path.read_bytes() for path in journal_paths}

    report = service.check()

    assert report.is_consistent
    assert report.issues == ()
    assert (report.event_count, report.segment_count, report.association_count) == (1, 1, 1)
    assert {path: path.read_bytes() for path in journal_paths} == before


def test_consistency_report_finds_orphan_and_identity_conflicts(tmp_path):
    service = _workspace(tmp_path)
    service.association_store.confirm(
        passage_event_id="missing-event",
        bib="99",
        confirmed_source=REGULAR_SOURCE,
        segment_id="missing-segment",
        frame_index=0,
        position_ms=0,
        marker_x_normalized=0.5,
        marker_y_normalized=0.5,
        confirmed_at_ms=16_000,
    )
    service.calibration_store.record(
        camera_index=1,
        session_key="bad",
        offset_ms=0,
        anchor_event_id="event-1",
        anchor_bib="wrong",
        calibrated_at_ms=16_000,
    )

    report = service.check()

    assert not report.is_consistent
    assert {issue.code for issue in report.issues} >= {
        "association_event_missing",
        "calibration_bib_mismatch",
    }


def test_projection_rebuild_is_explicit_atomic_and_idempotent(tmp_path):
    service = _workspace(tmp_path)
    output = tmp_path / "derived" / "workspace_projection.json"

    assert not output.exists()
    service.rebuild_projection(output)
    first = json.loads(output.read_text(encoding="utf-8"))
    service.rebuild_projection(output)
    second = json.loads(output.read_text(encoding="utf-8"))

    assert first["events"] == second["events"]
    assert first["segments"] == second["segments"]
    assert first["sources"] == second["sources"]
    assert set(first["events"]) == {"event-1"}
    assert not tuple(output.parent.glob("*.tmp"))


def test_read_only_open_reports_incomplete_tail_without_truncating(tmp_path):
    service = _workspace(tmp_path)
    passage_path = service.passage_store.journal_path
    timeline_path = service.timeline_store.journal_path
    association_path = service.association_store.journal_path
    calibration_path = service.calibration_store.journal_path
    binding_path = service.binding_store.journal_path
    with passage_path.open("ab") as journal:
        journal.write(b'{"schema_version":1,"event_id":"partial')
    before = passage_path.read_bytes()

    readonly = WorkspaceConsistencyService.open_read_only(
        passage_journal=passage_path,
        timeline_journal=timeline_path,
        association_journal=association_path,
        calibration_journal=calibration_path,
        binding_journal=binding_path,
    )
    report = readonly.check()

    assert passage_path.read_bytes() == before
    assert any(issue.code == "incomplete_tail_ignored" for issue in report.issues)
    assert report.is_consistent


def test_projection_rebuild_refuses_inconsistent_sources(tmp_path):
    service = _workspace(tmp_path)
    service.association_store.confirm(
        passage_event_id="orphan",
        bib="9",
        confirmed_source=REGULAR_SOURCE,
        segment_id="missing",
        frame_index=0,
        position_ms=0,
        marker_x_normalized=0.5,
        marker_y_normalized=0.5,
        confirmed_at_ms=20_000,
    )
    output = tmp_path / "projection.json"

    with pytest.raises(WorkspaceConsistencyError):
        service.rebuild_projection(output)

    assert not output.exists()
