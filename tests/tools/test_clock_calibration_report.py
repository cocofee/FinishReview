import json

import numpy as np

from realtime.clock_sync import ClockModelStore
from realtime.passage_evidence import PassageEvidenceAssociationStore, VideoClockCalibrationStore
from realtime.passage_receiver import PassageEvent
from realtime.video_timeline import VideoTimelineStore
from tools.clock_calibration_report import run_report


RACE = "event-a-competition-b"
DAY = 1_789_747_200_000  # 2026-09-19 00:00 Beijing time
START = DAY + 10 * 3_600_000


def _workspace(tmp_path, *, manual_offset_error=0):
    root = tmp_path / "event"
    (root / "videos").mkdir(parents=True)
    rng = np.random.default_rng(4)
    chips = np.sort(START + rng.uniform(60_000, 3_000_000, 220)).astype(np.int64)
    offset = lambda chip: 1_200 - 334 * (chip - START) / 3_600_000
    events = []
    for index, chip in enumerate(chips, start=1):
        events.append(PassageEvent(
            event_id=f"{RACE}-stage-1-passage-{index}", race_id=RACE, stage_id="stage-1", group_id="1",
            sequence=index, bib=f"{index:03d}", passage_time_ms=int(chip - DAY), lap=0,
            passage_timestamp_ms=int(chip), received_at_ms=int(chip) - 500,
        ))
    (root / "cyclerace_passage_events.jsonl").write_text(
        "".join(json.dumps(event.to_payload(), ensure_ascii=False) + "\n" for event in events), encoding="utf-8",
    )
    video = root / "videos" / "camera_01_20260919_100000_000000_archive_0000.mkv"
    video.write_bytes(b"\0" * 32)
    timeline = VideoTimelineStore(root / "video_timeline.jsonl")
    segment = timeline.add_completed_segment(
        source_id="camera_01_review", camera_index=1, video_path=video,
        media_started_at_ms=START, media_duration_ms=3_200_000,
        clock_source="videopipe_system_clock", timing_error_ms=3_000,
        end_reason="continuous_archive_fallback", race_id=RACE,
    )
    crossings = [
        {"recorder_ms": int(chip + offset(chip) + rng.normal(0, 20)),
         "session_id": "camera_01_20260919_100000_000000"}
        for chip in chips
    ]
    (tmp_path / "crossings.json").write_text(
        json.dumps({"camera_index": 1, "crossings": crossings}), encoding="utf-8",
    )
    anchor = events[0]
    VideoClockCalibrationStore(root / "video_clock_calibrations.jsonl").record(
        camera_index=1,
        session_key=f"camera_01_review|{root / 'videos'}|camera_01_20260919_100000_000000",
        offset_ms=int(offset(chips[0])) + manual_offset_error,
        anchor_event_id=anchor.event_id, anchor_bib=anchor.bib, calibrated_at_ms=int(chips[0]) + 10_000,
    )
    confirmed = events[5]
    store = PassageEvidenceAssociationStore(root / "passage_evidence_associations.jsonl")
    position = int(chips[5] + offset(chips[5]) - START - 190)
    store.confirm(passage_event_id=confirmed.event_id, bib=confirmed.bib, confirmed_source="regular",
                  segment_id=segment.segment_id, frame_index=position // 50, position_ms=position,
                  marker_x_normalized=0.5, marker_y_normalized=0.5, confirmed_at_ms=int(chips[5]) + 5_000)
    return root


def test_report_calibrates_automatically_and_flags_a_wrong_manual_anchor(tmp_path):
    root = _workspace(tmp_path, manual_offset_error=-58_000)
    before = {path.name: path.read_bytes() for path in root.iterdir() if path.is_file()}

    report = run_report(root, tmp_path / "crossings.json", tmp_path / "out")

    model = report["model"]
    assert abs(model["slope_ms_per_hour"] + 334) < 30
    assert model["residual_median_abs_ms"] < 30
    assert report["coarse"]["distinct"]
    anchor, = report["manual_anchors"]
    assert not anchor["within_tolerance"]
    assert anchor["difference_ms"] < -57_000
    assert report["operator_confirmations"]["count"] == 1
    assert ClockModelStore(tmp_path / "out" / "video_clock_models.jsonl").latest(1).version == 1
    after = {path.name: path.read_bytes() for path in root.iterdir() if path.is_file()}
    assert after == before
    text = (tmp_path / "out" / "clock_report_camera1.txt").read_text(encoding="utf-8")
    assert "请核对" in text


def test_report_accepts_a_good_manual_anchor(tmp_path):
    root = _workspace(tmp_path, manual_offset_error=40)

    report = run_report(root, tmp_path / "crossings.json", tmp_path / "out")

    assert report["manual_anchors"][0]["within_tolerance"]
