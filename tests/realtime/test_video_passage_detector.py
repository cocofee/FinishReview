import numpy as np
from types import SimpleNamespace

import realtime.video_passage_detector as detector_module
from realtime.video_passage_detector import (
    FixedCameraLineCrossingDetector,
    LightweightVideoPassageDetector,
    VideoPassageCandidate,
    VideoPassageDetectorConfig,
    merge_candidates,
    merge_line_crossings,
    reconcile_candidates,
    unmatched_passage_times,
    VideoPassageScanWorker,
)
from realtime.finish_line import FinishLine


def _frame(value: int = 0) -> np.ndarray:
    return np.full((12, 16), value, dtype=np.uint8)


def test_detector_requires_consecutive_motion_and_emits_one_candidate():
    detector = LightweightVideoPassageDetector(
        VideoPassageDetectorConfig(
            start_confirm_frames=2,
            end_confirm_frames=2,
            min_score=0.01,
            min_changed_area=0.01,
        )
    )
    assert detector.process_frame(0, _frame()) == ()
    assert detector.process_frame(100, _frame(100)) == ()
    assert detector.process_frame(200, _frame(200)) == ()
    assert detector.process_frame(300, _frame(255)) == ()
    assert detector.process_frame(400, _frame(255)) == ()
    result = detector.process_frame(500, _frame(255))
    assert len(result) == 1
    assert result[0].started_at_ms == 200
    assert result[0].ended_at_ms == 300
    assert result[0].status == "待人工复核"


def test_flush_closes_open_batch_without_inventing_people():
    detector = LightweightVideoPassageDetector(
        VideoPassageDetectorConfig(
            start_confirm_frames=1,
            min_score=0.01,
            min_changed_area=0.01,
        )
    )
    detector.process_frame(0, _frame())
    detector.process_frame(100, _frame(255))
    result = detector.flush()
    assert len(result) == 1
    assert not result[0].is_group


def test_large_continuous_motion_is_marked_as_camera_motion():
    detector = LightweightVideoPassageDetector(
        VideoPassageDetectorConfig(
            start_confirm_frames=1,
            end_confirm_frames=2,
            min_score=0.01,
            min_changed_area=0.01,
            global_motion_area=0.3,
            global_motion_confirm_frames=2,
        )
    )
    detector.process_frame(0, _frame())
    detector.process_frame(100, _frame(255))
    detector.process_frame(200, _frame(0))
    detector.process_frame(300, _frame(255))
    detector.process_frame(400, _frame(255))
    result = detector.process_frame(500, _frame(255))

    assert len(result) == 1
    assert result[0].is_camera_motion
    assert reconcile_candidates(result, (200,))[0].needs_review


def test_fixed_camera_line_detector_emits_only_on_crossing():
    detector = FixedCameraLineCrossingDetector(
        FinishLine(1, 0.5, 0.0, 0.5, 1.0),
        min_score=0.01,
        min_changed_area=0.01,
        cooldown_ms=0,
    )
    blank = np.zeros((20, 20), dtype=np.uint8)
    right = blank.copy()
    right[:, 12:14] = 255
    left = blank.copy()
    left[:, 4:6] = 255

    assert detector.process_frame(0, blank) == ()
    assert detector.process_frame(100, right) == ()
    assert detector.process_frame(200, blank) == ()
    result = detector.process_frame(300, left)

    assert len(result) == 1
    assert result[0].candidate_id == "line-1-1"


def test_fixed_camera_line_detector_supports_scan_flush():
    detector = FixedCameraLineCrossingDetector(
        FinishLine(1, 0.5, 0.0, 0.5, 1.0)
    )
    assert detector.flush() == ()


def test_long_motion_is_split_at_max_event_duration():
    detector = LightweightVideoPassageDetector(
        VideoPassageDetectorConfig(
            start_confirm_frames=1,
            end_confirm_frames=2,
            min_score=0.01,
            min_changed_area=0.01,
            max_event_ms=200,
        )
    )
    detector.process_frame(0, _frame())
    detector.process_frame(100, _frame(100))
    emitted = detector.process_frame(300, _frame(200))
    assert len(emitted) == 1
    assert emitted[0].started_at_ms == 100


def test_reconciliation_only_marks_anomalous_batches_for_review():
    candidate = VideoPassageCandidate(
        "v1", 1, 1_000, 1_300, 1_100, 0.2, 0.1
    )
    result = reconcile_candidates((candidate,), (1_100,))
    assert result[0].anomaly == "正常匹配"
    assert not result[0].needs_review

    group = VideoPassageCandidate(
        "v2", 1, 2_000, 2_900, 2_300, 0.3, 0.2, kind="group"
    )
    result = reconcile_candidates((group,), (2_100, 2_400))
    assert result[0].anomaly == "多人批次，芯片记录多于视频事件"
    assert result[0].needs_review


def test_unmatched_chip_times_are_kept_as_anomalies():
    candidate = VideoPassageCandidate("v1", 1, 1_000, 1_300, 1_100, 0.2, 0.1)
    assert unmatched_passage_times((candidate,), (1_100, 5_000)) == (5_000,)


def test_nearby_candidates_are_reported_as_one_group_batch():
    detector = LightweightVideoPassageDetector()
    first = detector._new_candidate(  # noqa: SLF001 - focused merge fixture
        type("Batch", (), {
            "started_at_ms": 1000,
            "last_motion_at_ms": 1100,
            "peak_at_ms": 1050,
            "peak_score": 0.2,
            "changed_area": 0.1,
        })()
    )
    second = detector._new_candidate(
        type("Batch", (), {
            "started_at_ms": 1200,
            "last_motion_at_ms": 1300,
            "peak_at_ms": 1250,
            "peak_score": 0.3,
            "changed_area": 0.2,
        })()
    )
    result = merge_candidates((first, second), merge_gap_ms=200)
    assert len(result) == 1
    assert result[0].is_group
    assert result[0].started_at_ms == 1000
    assert result[0].ended_at_ms == 1300


def test_line_crossing_jitter_is_merged_into_one_review_batch():
    crossings = tuple(
        VideoPassageCandidate(
            f"line-{index}", 1, timestamp, timestamp, timestamp, 0.2, 0.1
        )
        for index, timestamp in enumerate((10_000, 10_500, 11_250), 1)
    )

    result = merge_line_crossings(crossings, batch_gap_ms=1_000)

    assert len(result) == 1
    assert result[0].is_group
    assert result[0].started_at_ms == 10_000
    assert result[0].ended_at_ms == 11_250


def test_scan_worker_merges_one_segment_and_preserves_video_location(
    tmp_path,
    monkeypatch,
):
    video_path = tmp_path / "camera_02.ts"
    video_path.write_bytes(b"video")
    segment = SimpleNamespace(
        segment_id="segment-02",
        camera_index=2,
        started_at_ms=10_000,
        video_path="camera_02.ts",
    )
    first = VideoPassageCandidate(
        "raw-1", 1, 10_100, 10_300, 10_200, 0.2, 0.1
    )
    second = VideoPassageCandidate(
        "raw-2", 1, 10_500, 10_700, 10_600, 0.3, 0.2
    )
    monkeypatch.setattr(
        detector_module,
        "scan_video_file",
        lambda *_args, **_kwargs: (first, second),
    )
    worker = VideoPassageScanWorker(
        lambda: (segment,),
        lambda _items: None,
        camera_index=2,
        width=16,
        height=12,
        path_resolver=lambda item: tmp_path / item.video_path,
    )

    result = worker.scan_once()

    assert len(result) == 1
    assert result[0].candidate_id == "segment-02:raw-1"
    assert result[0].camera_index == 2
    assert result[0].segment_id == "segment-02"
    assert result[0].video_path == str(video_path)
    assert result[0].video_position_ms == 600
    assert result[0].is_group


def test_scan_worker_uses_line_batch_merge_for_fixed_camera(
    tmp_path,
    monkeypatch,
):
    video_path = tmp_path / "camera_01.ts"
    video_path.write_bytes(b"video")
    segment = SimpleNamespace(
        segment_id="segment-01",
        camera_index=1,
        started_at_ms=10_000,
        video_path="camera_01.ts",
    )
    first = VideoPassageCandidate(
        "raw-1", 1, 10_100, 10_100, 10_100, 0.2, 0.1
    )
    second = VideoPassageCandidate(
        "raw-2", 1, 10_900, 10_900, 10_900, 0.3, 0.2
    )
    monkeypatch.setattr(
        detector_module,
        "scan_video_file",
        lambda *_args, **_kwargs: (first, second),
    )
    worker = VideoPassageScanWorker(
        lambda: (segment,),
        lambda _items: None,
        camera_index=1,
        width=16,
        height=12,
        path_resolver=lambda item: tmp_path / item.video_path,
        finish_line=FinishLine(1, 0.5, 0.0, 0.5, 1.0),
    )

    result = worker.scan_once()

    assert len(result) == 1
    assert result[0].is_group
    assert result[0].started_at_ms == 10_100
    assert result[0].ended_at_ms == 10_900
