import io
import random
import numpy as np
import pytest
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import realtime.video_passage_detector as detector_module
from realtime.video_passage_detector import (
    FixedCameraLineCrossingDetector,
    LightweightVideoPassageDetector,
    VideoPassageCandidate,
    VideoPassageDetectorConfig,
    merge_candidates,
    merge_line_crossings,
    match_candidate_counts,
    reconcile_candidates,
    unmatched_passage_times,
    VideoPassageScanWorker,
    LiveReviewBatchScanWorker,
    review_batch_range,
    review_batch_start,
)
from realtime.finish_line import FinishLine


def test_review_batch_range_is_half_open_with_overlap():
    batch = review_batch_range(1_200_000, batch_ms=120_000, overlap_ms=2_000)
    assert batch.core_started_at_ms == 1_200_000
    assert batch.core_ended_at_ms == 1_320_000
    assert batch.scan_started_at_ms == 1_198_000
    assert batch.scan_ended_at_ms == 1_322_000
    assert review_batch_start(1_319_999, batch_ms=120_000) == 1_200_000
    assert review_batch_start(1_320_000, batch_ms=120_000) == 1_320_000


def test_live_batch_scanner_owns_boundary_candidate_once(tmp_path, monkeypatch):
    segments = []
    for index in range(0, 122):
        start = index * 2_000
        path = tmp_path / f"segment_{index:03d}.ts"
        path.write_bytes(b"video")
        segments.append(
            SimpleNamespace(
                segment_id=f"segment-{index}",
                camera_index=1,
                started_at_ms=start,
                ended_at_ms=start + 2_000,
                video_path=path.name,
            )
        )
    calls = []
    progress = []
    leases = []

    def fake_scan(path, **kwargs):
        calls.append(Path(path).read_text(encoding="utf-8"))
        # One event exactly at the 120-second boundary. Both batches see it
        # through overlap, but only the second core owns the half-open peak.
        return (
            VideoPassageCandidate(
                candidate_id="line-1",
                camera_index=1,
                started_at_ms=119_900,
                ended_at_ms=120_100,
                peak_at_ms=120_000,
                peak_score=1.0,
                changed_area=0.1,
            ),
        )

    monkeypatch.setattr(detector_module, "scan_video_file", fake_scan)
    worker = LiveReviewBatchScanWorker(
        lambda: tuple(segments),
        lambda _items: None,
        path_resolver=lambda item: tmp_path / item.video_path,
        manifest_dir=tmp_path,
        batch_ms=120_000,
        overlap_ms=2_000,
        progress_callback=progress.append,
        lease_callback=lambda items: leases.append(("acquire", len(items))) or True,
        release_callback=lambda items: leases.append(("release", len(items))),
    )
    result = worker.scan_once()

    assert len(calls) == 2
    assert len(result) == 1
    assert result[0].peak_at_ms == 120_000
    assert result[0].video_path.endswith("segment_060.ts")
    assert progress == [0, 120_000]
    assert [entry[0] for entry in leases] == [
        "acquire",
        "release",
        "acquire",
        "release",
    ]


def test_live_batch_scanner_commits_cursor_only_after_candidate_persists(
    tmp_path,
    monkeypatch,
):
    segments = []
    for index in range(61):
        start = index * 2_000
        path = tmp_path / f"segment_{index:03d}.ts"
        path.write_bytes(b"video")
        segments.append(
            SimpleNamespace(
                segment_id=f"segment-{index}",
                camera_index=1,
                started_at_ms=start,
                ended_at_ms=start + 2_000,
                video_path=path.name,
            )
        )
    candidate = VideoPassageCandidate(
        "line-1",
        1,
        59_900,
        60_100,
        60_000,
        1.0,
        0.1,
    )
    monkeypatch.setattr(
        detector_module,
        "scan_video_file",
        lambda *_args, **_kwargs: (candidate,),
    )
    callback_calls = []

    def persist(items):
        callback_calls.append(items)
        if len(callback_calls) == 1:
            raise RuntimeError("disk full")

    worker = LiveReviewBatchScanWorker(
        lambda: tuple(segments),
        persist,
        path_resolver=lambda item: tmp_path / item.video_path,
        manifest_dir=tmp_path,
    )

    with pytest.raises(RuntimeError, match="disk full"):
        worker.scan_once()
    assert worker._next_batch_start_ms == 0
    assert worker._emitted_keys == set()

    result = worker.scan_once()
    assert len(result) == 1
    assert worker._next_batch_start_ms == 120_000
    assert len(worker._emitted_keys) == 1


def test_live_batch_scans_core_despite_overlap_only_gap(tmp_path, monkeypatch):
    segments = []
    for index in range(60):
        path = tmp_path / f"core_{index:03d}.ts"
        path.write_bytes(b"video")
        segments.append(
            SimpleNamespace(
                segment_id=f"core-{index}",
                camera_index=1,
                started_at_ms=index * 2_000,
                ended_at_ms=(index + 1) * 2_000,
                video_path=path.name,
            )
        )
    right = tmp_path / "right.ts"
    right.write_bytes(b"video")
    segments.append(
        SimpleNamespace(
            segment_id="right",
            camera_index=1,
            started_at_ms=122_000,
            ended_at_ms=124_000,
            video_path=right.name,
        )
    )
    scan_calls = []
    monkeypatch.setattr(
        detector_module,
        "scan_video_file",
        lambda *args, **kwargs: scan_calls.append(args[0]) or (),
    )
    permanent_ranges = []
    worker = LiveReviewBatchScanWorker(
        lambda: tuple(segments),
        lambda _items: None,
        path_resolver=lambda item: tmp_path / item.video_path,
        manifest_dir=tmp_path,
        permanent_gap_callback=lambda start, end: permanent_ranges.append(
            (start, end)
        )
        or True,
    )

    assert worker.scan_once() == ()
    assert len(scan_calls) == 1
    assert permanent_ranges == []
    assert worker._next_batch_start_ms == 120_000


def test_live_batch_waits_for_initial_core_tail(tmp_path, monkeypatch):
    segments = []
    for index in range(59):
        path = tmp_path / f"prefix_{index:03d}.ts"
        path.write_bytes(b"video")
        segments.append(
            SimpleNamespace(
                segment_id=f"prefix-{index}",
                camera_index=1,
                started_at_ms=index * 2_000,
                ended_at_ms=(index + 1) * 2_000,
                video_path=path.name,
            )
        )
    future = tmp_path / "future.ts"
    future.write_bytes(b"video")
    segments.append(
        SimpleNamespace(
            segment_id="future",
            camera_index=1,
            started_at_ms=130_000,
            ended_at_ms=132_000,
            video_path=future.name,
        )
    )
    scan_calls = []
    monkeypatch.setattr(
        detector_module,
        "scan_video_file",
        lambda *args, **kwargs: scan_calls.append(args[0]) or (),
    )
    worker = LiveReviewBatchScanWorker(
        lambda: tuple(segments),
        lambda _items: None,
        path_resolver=lambda item: tmp_path / item.video_path,
        manifest_dir=tmp_path,
    )

    assert worker.scan_once() == ()
    assert scan_calls == []
    assert worker._next_batch_start_ms == 0


def test_live_batch_scanner_does_not_collapse_missing_segment_gap(tmp_path, monkeypatch):
    segments = []
    progress = []
    for index in range(61):
        if index == 30:
            continue
        start = index * 2_000
        path = tmp_path / f"segment_{index:03d}.ts"
        path.write_bytes(b"video")
        segments.append(
            SimpleNamespace(
                segment_id=f"segment-{index}",
                camera_index=1,
                started_at_ms=start,
                ended_at_ms=start + 2_000,
                video_path=path.name,
            )
        )
    scan_calls = []
    monkeypatch.setattr(
        detector_module,
        "scan_video_file",
        lambda *args, **kwargs: scan_calls.append(args[0]) or (),
    )
    worker = LiveReviewBatchScanWorker(
        lambda: tuple(segments),
        lambda _items: None,
        path_resolver=lambda item: tmp_path / item.video_path,
        manifest_dir=tmp_path,
        progress_callback=progress.append,
        permanent_gap_callback=lambda _start, _end: True,
    )

    assert worker.scan_once() == ()
    assert scan_calls == []
    assert worker._next_batch_start_ms == 120_000
    assert progress == [0, 120_000]


def test_live_batch_scanner_retries_temporary_segment_gap(tmp_path, monkeypatch):
    segments = []
    for index in range(61):
        start = index * 2_000
        path = tmp_path / f"segment_{index:03d}.ts"
        path.write_bytes(b"video")
        segments.append(
            SimpleNamespace(
                segment_id=f"segment-{index}",
                camera_index=1,
                started_at_ms=start,
                ended_at_ms=start + 2_000,
                video_path=path.name,
            )
        )
    visible = [segment for index, segment in enumerate(segments) if index != 30]
    scan_calls = []
    monkeypatch.setattr(
        detector_module,
        "scan_video_file",
        lambda *args, **kwargs: scan_calls.append(args[0]) or (),
    )
    worker = LiveReviewBatchScanWorker(
        lambda: tuple(visible),
        lambda _items: None,
        path_resolver=lambda item: tmp_path / item.video_path,
        manifest_dir=tmp_path,
    )

    assert worker.scan_once() == ()
    visible.append(segments[30])

    assert worker.scan_once() == ()
    assert len(scan_calls) == 1
    assert worker._next_batch_start_ms == 120_000


def test_live_batch_scanner_protects_initial_batch_before_it_is_ready(tmp_path):
    path = tmp_path / "segment_000.ts"
    path.write_bytes(b"video")
    segment = SimpleNamespace(
        segment_id="segment-0",
        camera_index=1,
        started_at_ms=0,
        ended_at_ms=2_000,
        video_path=path.name,
    )
    progress = []
    worker = LiveReviewBatchScanWorker(
        lambda: (segment,),
        lambda _items: None,
        path_resolver=lambda item: tmp_path / item.video_path,
        manifest_dir=tmp_path,
        progress_callback=progress.append,
    )

    assert worker.scan_once() == ()
    assert progress == [0]


def test_live_batch_scanner_ignores_late_progress_after_stop():
    progress = []
    worker = LiveReviewBatchScanWorker(
        lambda: (),
        lambda _items: None,
        progress_callback=progress.append,
    )
    worker._stop.set()

    worker._notify_progress(120_000)
    worker._notify_progress(None)

    assert progress == [None]


def test_live_batch_scanner_pause_terminates_active_ffmpeg():
    class _Process:
        def __init__(self):
            self.terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

    worker = LiveReviewBatchScanWorker(lambda: (), lambda _items: None)
    process = _Process()
    worker._set_active_process(process)

    worker.pause()

    assert process.terminated


def test_scan_video_file_hides_ffmpeg_console_on_windows(tmp_path, monkeypatch):
    video_path = tmp_path / "sample.mp4"
    video_path.write_bytes(b"video")
    calls = []

    class _Process:
        stdout = io.BytesIO()
        stderr = io.BytesIO()
        returncode = 0

        def wait(self, timeout=None):
            return 0

    def fake_popen(command, **kwargs):
        calls.append((command, kwargs))
        return _Process()

    monkeypatch.setattr(detector_module.os, "name", "nt")
    monkeypatch.setattr(detector_module.subprocess, "Popen", fake_popen)
    detector_module.scan_video_file(
        video_path,
        started_at_ms=0,
        width=2,
        height=2,
        ffmpeg_path="ffmpeg",
    )

    assert calls[0][1]["creationflags"] == (
        getattr(detector_module.subprocess, "CREATE_NO_WINDOW", 0)
        | getattr(
            detector_module.subprocess,
            "BELOW_NORMAL_PRIORITY_CLASS",
            0x00004000,
        )
    )


def test_scan_video_file_uses_concat_demuxer_for_batch_manifest(tmp_path, monkeypatch):
    manifest = tmp_path / "batch.ffconcat"
    manifest.write_text("ffconcat version 1.0\n", encoding="utf-8")
    calls = []

    class _Process:
        stdout = io.BytesIO()
        stderr = io.BytesIO()
        returncode = 0

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(
        detector_module.subprocess,
        "Popen",
        lambda command, **kwargs: (calls.append(command) or _Process()),
    )
    detector_module.scan_video_file(
        manifest,
        started_at_ms=0,
        width=2,
        height=2,
        ffmpeg_path="ffmpeg",
    )

    assert calls[0][1:5] == ["-hide_banner", "-loglevel", "error", "-nostdin"]
    assert calls[0][5:10] == ["-f", "concat", "-safe", "0", "-i"]


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
    blank = np.zeros((20, 40), dtype=np.uint8)
    right = blank.copy()
    right[:, 20:23] = 255
    left = blank.copy()
    left[:, 17:20] = 255

    assert detector.process_frame(0, blank) == ()
    assert detector.process_frame(100, right) == ()
    result = detector.process_frame(200, left)

    assert len(result) == 1
    assert result[0].candidate_id == "line-1-1"


def test_scan_worker_keeps_line_detector_across_adjacent_segments(
    tmp_path,
    monkeypatch,
):
    paths = []
    segments = []
    for index, start in enumerate((0, 1_000), 1):
        path = tmp_path / f"camera_{index}.ts"
        path.write_bytes(b"video")
        paths.append(path)
        segments.append(
            SimpleNamespace(
                segment_id=f"segment-{index}",
                camera_index=1,
                started_at_ms=start,
                ended_at_ms=start + 1_000,
                video_path=path.name,
            )
        )
    seen_detectors = []

    def fake_scan(*_args, **kwargs):
        seen_detectors.append(kwargs["detector"])
        return ()

    monkeypatch.setattr(detector_module, "scan_video_file", fake_scan)
    worker = VideoPassageScanWorker(
        lambda: tuple(segments),
        lambda _items: None,
        width=16,
        height=12,
        path_resolver=lambda item: tmp_path / item.video_path,
        finish_line=FinishLine(1, 0.5, 0.0, 0.5, 1.0),
    )

    worker.scan_once()

    assert len(seen_detectors) == 2
    assert seen_detectors[0] is seen_detectors[1]


def test_line_detector_resets_frame_state_between_adjacent_segments():
    detector = FixedCameraLineCrossingDetector(
        FinishLine(1, 0.5, 0.0, 0.5, 1.0),
        min_score=0.01,
        min_changed_area=0.01,
        cooldown_ms=0,
    )
    blank = np.zeros((20, 40), dtype=np.uint8)
    right = blank.copy()
    right[:, 20:23] = 255
    left = blank.copy()
    left[:, 17:20] = 255

    assert detector.process_frame(0, blank) == ()
    assert detector.process_frame(100, right) == ()
    detector.reset_segment_state()

    # The first frame of a new file establishes a baseline; it must not be
    # compared with the final frame of the previous file.
    assert detector.process_frame(1_000, left) == ()
    assert detector.process_frame(1_100, blank) == ()


def test_fixed_camera_line_detector_supports_scan_flush():
    detector = FixedCameraLineCrossingDetector(
        FinishLine(1, 0.5, 0.0, 0.5, 1.0)
    )
    assert detector.flush() == ()


def test_fixed_camera_line_detector_ignores_motion_outside_finish_line_roi():
    detector = FixedCameraLineCrossingDetector(
        FinishLine(1, 0.5, 0.4, 0.5, 0.6, band_width=0.08),
        min_score=0.01,
        min_changed_area=0.01,
        cooldown_ms=0,
    )
    blank = np.zeros((40, 40), dtype=np.uint8)
    upper_right = blank.copy()
    upper_right[2:12, 26:34] = 255
    upper_left = blank.copy()
    upper_left[2:12, 6:14] = 255

    assert detector.process_frame(0, blank) == ()
    assert detector.process_frame(100, upper_right) == ()
    assert detector.process_frame(200, blank) == ()
    assert detector.process_frame(300, upper_left) == ()


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


def test_reconciliation_applies_camera_specific_passage_offsets():
    camera_one = VideoPassageCandidate(
        "camera-1", 1, 8_000, 8_100, 8_050, 0.2, 0.1
    )
    camera_two = VideoPassageCandidate(
        "camera-2", 2, 9_000, 9_100, 9_050, 0.2, 0.1
    )

    result = reconcile_candidates(
        (camera_one, camera_two),
        (9_350, 11_450),
        passage_time_offset_by_camera={1: -1_300, 2: -2_400},
    )

    assert [item.chip_count for item in result] == [1, 1]


def test_unmatched_chip_times_are_kept_as_anomalies():
    candidate = VideoPassageCandidate("v1", 1, 1_000, 1_300, 1_100, 0.2, 0.1)
    assert unmatched_passage_times((candidate,), (1_100, 5_000)) == (5_000,)


def test_indexed_candidate_matching_matches_linear_reference():
    randomizer = random.Random(0)
    candidates = tuple(
        VideoPassageCandidate(
            f"candidate-{index}",
            1 + index % 2,
            (started := randomizer.randrange(0, 20_000)),
            started + randomizer.randrange(0, 1_000),
            started,
            0.2,
            0.1,
        )
        for index in range(100)
    )
    passage_times = tuple(
        sorted(randomizer.randrange(0, 20_000) for _ in range(300))
    )
    offsets = {1: -250, 2: 400}
    tolerance = 500

    indexed = match_candidate_counts(
        candidates,
        passage_times,
        tolerance_ms=tolerance,
        passage_time_offset_by_camera=offsets,
    )
    expected_counts = tuple(
        sum(
            candidate.started_at_ms - tolerance
            <= timestamp + offsets[candidate.camera_index]
            <= candidate.ended_at_ms + tolerance
            for timestamp in passage_times
        )
        for candidate in candidates
    )
    expected_unmatched = tuple(
        timestamp
        for timestamp in passage_times
        if not any(
            candidate.started_at_ms - tolerance
            <= timestamp + offsets[candidate.camera_index]
            <= candidate.ended_at_ms + tolerance
            for candidate in candidates
        )
    )

    assert tuple(count for _candidate, count in indexed) == expected_counts
    assert unmatched_passage_times(
        candidates,
        passage_times,
        tolerance_ms=tolerance,
        passage_time_offset_by_camera=offsets,
    ) == expected_unmatched


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


def test_scan_worker_publishes_each_segment_before_archive_scan_finishes(
    tmp_path,
    monkeypatch,
):
    paths = []
    segments = []
    for index, start in enumerate((10_000, 20_000), 1):
        path = tmp_path / f"camera_01_{index}.ts"
        path.write_bytes(b"video")
        paths.append(path)
        segments.append(
            SimpleNamespace(
                segment_id=f"segment-{index}",
                camera_index=1,
                started_at_ms=start,
                video_path=path.name,
            )
        )

    def fake_scan(path, **_kwargs):
        start = 10_000 if Path(path) == paths[0] else 20_000
        return (VideoPassageCandidate(
            f"raw-{start}", 1, start + 100, start + 200, start + 150, 0.2, 0.1
        ),)

    monkeypatch.setattr(detector_module, "scan_video_file", fake_scan)
    published = []
    worker = VideoPassageScanWorker(
        lambda: tuple(segments),
        published.append,
        width=16,
        height=12,
        path_resolver=lambda item: tmp_path / item.video_path,
    )

    result = worker.scan_once(result_callback=published.append)

    assert len(result) == 2
    assert [batch[0].segment_id for batch in published] == [
        "segment-1",
        "segment-2",
    ]


def test_scan_worker_holds_cleanup_lease_while_scanning(tmp_path, monkeypatch):
    video_path = tmp_path / "camera_01.ts"
    video_path.write_bytes(b"video")
    segment = SimpleNamespace(
        segment_id="segment-01",
        camera_index=1,
        started_at_ms=10_000,
        video_path="camera_01.ts",
    )
    lease_events = []

    def fake_scan(*_args, **_kwargs):
        lease_events.append("scan")
        return ()

    monkeypatch.setattr(detector_module, "scan_video_file", fake_scan)
    worker = VideoPassageScanWorker(
        lambda: (segment,),
        lambda _items: None,
        width=16,
        height=12,
        path_resolver=lambda item: tmp_path / item.video_path,
        lease_callback=lambda items: lease_events.append(
            ("acquire", items[0].segment_id)
        )
        or True,
        release_callback=lambda items: lease_events.append(
            ("release", items[0].segment_id)
        ),
    )

    worker.scan_once()

    assert lease_events == [
        ("acquire", "segment-01"),
        "scan",
        ("release", "segment-01"),
    ]


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


def test_scan_worker_pause_and_resume_preserve_pending_segments(
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
    scan_calls = []
    monkeypatch.setattr(
        detector_module,
        "scan_video_file",
        lambda *_args, **_kwargs: scan_calls.append(True) or (),
    )
    worker = VideoPassageScanWorker(
        lambda: (segment,),
        lambda _items: None,
        width=16,
        height=12,
        path_resolver=lambda item: tmp_path / item.video_path,
    )

    worker.pause()
    assert worker.scan_once() == ()
    assert scan_calls == []

    worker.resume()
    assert worker.scan_once() == ()
    assert scan_calls == [True]


def test_scan_worker_stop_terminates_active_ffmpeg_process(tmp_path, monkeypatch):
    video_path = tmp_path / "camera_01.ts"
    video_path.write_bytes(b"video")
    segment = SimpleNamespace(
        segment_id="segment-01",
        camera_index=1,
        started_at_ms=10_000,
        video_path="camera_01.ts",
    )
    started = threading.Event()
    release = threading.Event()

    class _Process:
        def __init__(self):
            self.terminated = False

        def poll(self):
            return None if not self.terminated else -15

        def terminate(self):
            self.terminated = True
            release.set()

    process = _Process()

    def blocking_scan(*_args, process_callback=None, **_kwargs):
        process_callback(process)
        started.set()
        release.wait(2.0)
        process_callback(None)
        return ()

    monkeypatch.setattr(detector_module, "scan_video_file", blocking_scan)
    worker = VideoPassageScanWorker(
        lambda: (segment,),
        lambda _items: None,
        width=16,
        height=12,
        path_resolver=lambda item: tmp_path / item.video_path,
    )
    worker.start()
    assert started.wait(1.0)

    worker.stop(timeout=1.0)

    assert process.terminated
    for _ in range(20):
        if worker._thread is None:  # noqa: SLF001 - lifecycle assertion
            break
        time.sleep(0.01)
    assert worker._thread is None  # noqa: SLF001 - lifecycle assertion
