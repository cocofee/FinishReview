"""Lightweight ordinary-video passage candidate detection.

This module deliberately detects motion batches near a configured finish-line ROI.
It does not identify athletes, create official timing records, or require one
candidate per athlete.  It is intended to narrow the manual-review search area
on low-resource field computers.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass, replace
import os
import subprocess
from pathlib import Path
import threading
import time
from typing import Callable, Iterable, Mapping, Optional, Sequence

import numpy as np

from .finish_line import FinishLine


# Python's subprocess module does not expose this Windows creation flag on
# every supported version. Keep the numeric fallback local so candidate scans
# remain below the recorder even on older Python builds.
_BELOW_NORMAL_PRIORITY_CLASS = getattr(
    subprocess,
    "BELOW_NORMAL_PRIORITY_CLASS",
    0x00004000,
)


@dataclass(frozen=True, slots=True)
class VideoPassageCandidate:
    """One continuous visual passage batch, not an official passage record."""

    candidate_id: str
    camera_index: int
    started_at_ms: int
    ended_at_ms: int
    peak_at_ms: int
    peak_score: float
    changed_area: float
    kind: str = "passage"
    status: str = "待人工复核"
    segment_id: str = ""
    video_path: str = ""
    video_position_ms: int = 0

    @property
    def duration_ms(self) -> int:
        return max(0, self.ended_at_ms - self.started_at_ms)

    @property
    def is_group(self) -> bool:
        return self.kind == "group"

    @property
    def is_camera_motion(self) -> bool:
        return self.kind == "camera_motion"


@dataclass(frozen=True, slots=True)
class VideoPassageReconciliation:
    """Anomaly summary used by the review UI."""

    candidate: VideoPassageCandidate
    chip_count: int
    anomaly: str
    review_status: str = "pending"

    @property
    def needs_review(self) -> bool:
        return self.anomaly != "正常匹配"


@dataclass(frozen=True, slots=True)
class VideoPassageDetectorConfig:
    """Conservative defaults for a CPU-only detector."""

    camera_index: int = 1
    min_score: float = 0.018
    min_changed_area: float = 0.010
    start_confirm_frames: int = 2
    end_confirm_frames: int = 3
    merge_gap_ms: int = 350
    group_duration_ms: int = 650
    max_event_ms: int = 2_000
    global_motion_area: float = 0.35
    global_motion_confirm_frames: int = 4

    def __post_init__(self) -> None:
        if self.camera_index <= 0:
            raise ValueError("camera_index must be positive")
        if self.min_score < 0 or self.min_changed_area < 0:
            raise ValueError("detection thresholds must be non-negative")
        if self.start_confirm_frames <= 0 or self.end_confirm_frames <= 0:
            raise ValueError("confirmation frame counts must be positive")
        if self.merge_gap_ms < 0 or self.group_duration_ms < 0:
            raise ValueError("durations must be non-negative")
        if self.max_event_ms <= 0:
            raise ValueError("max_event_ms must be positive")
        if not 0 < self.global_motion_area <= 1:
            raise ValueError("global_motion_area must be between zero and one")
        if self.global_motion_confirm_frames <= 0:
            raise ValueError("global_motion_confirm_frames must be positive")


@dataclass(frozen=True, slots=True)
class ReviewBatchRange:
    """A core review batch and the small overlap used for detector context."""

    core_started_at_ms: int
    core_ended_at_ms: int
    scan_started_at_ms: int
    scan_ended_at_ms: int


def review_batch_range(
    core_started_at_ms: int,
    *,
    batch_ms: int = 120_000,
    overlap_ms: int = 2_000,
) -> ReviewBatchRange:
    """Return one half-open batch range with bounded detector overlap."""

    batch_ms = int(batch_ms)
    overlap_ms = max(0, int(overlap_ms))
    if batch_ms <= 0:
        raise ValueError("batch_ms must be positive")
    core_started_at_ms = int(core_started_at_ms)
    core_ended_at_ms = core_started_at_ms + batch_ms
    return ReviewBatchRange(
        core_started_at_ms=core_started_at_ms,
        core_ended_at_ms=core_ended_at_ms,
        scan_started_at_ms=max(0, core_started_at_ms - overlap_ms),
        scan_ended_at_ms=core_ended_at_ms + overlap_ms,
    )


def review_batch_start(timestamp_ms: int, *, batch_ms: int = 120_000) -> int:
    """Align an absolute timeline timestamp to a deterministic batch start."""

    batch_ms = int(batch_ms)
    if batch_ms <= 0:
        raise ValueError("batch_ms must be positive")
    timestamp_ms = int(timestamp_ms)
    return timestamp_ms // batch_ms * batch_ms


@dataclass(slots=True)
class _OpenBatch:
    started_at_ms: int
    last_motion_at_ms: int
    peak_at_ms: int
    peak_score: float
    changed_area: float
    quiet_frames: int = 0
    global_motion_frames: int = 0


class LightweightVideoPassageDetector:
    """State machine consuming sampled grayscale/colour frames.

    Frames should already be downsampled and cropped to the finish-line ROI.
    The detector stores only two small grayscale frames and one open batch.
    """

    def __init__(self, config: VideoPassageDetectorConfig | None = None):
        self.config = config or VideoPassageDetectorConfig()
        self._previous: Optional[np.ndarray] = None
        self._open: Optional[_OpenBatch] = None
        self._positive_frames = 0
        self._candidate_number = 0

    @staticmethod
    def _gray(frame: np.ndarray) -> np.ndarray:
        array = np.asarray(frame)
        if array.ndim == 3:
            array = array.astype(np.float32).mean(axis=2)
        if array.ndim != 2 or not array.size:
            raise ValueError("frame must be a non-empty 2D or 3D array")
        if not np.issubdtype(array.dtype, np.floating):
            array = array.astype(np.float32)
        else:
            array = array.astype(np.float32, copy=False)
        scale = max(1.0, float(array.max()))
        if scale > 1.0:
            array = array / 255.0
        return array

    def _measure(self, frame: np.ndarray) -> tuple[float, float]:
        gray = self._gray(frame)
        if self._previous is None:
            self._previous = gray.copy()
            return 0.0, 0.0
        if gray.shape != self._previous.shape:
            raise ValueError("frame shape cannot change during detection")
        difference = np.abs(gray - self._previous)
        self._previous = gray.copy()
        # A small dead band suppresses camera noise and compression shimmer.
        changed = difference > 0.08
        return float(difference.mean()), float(changed.mean())

    def _is_motion(self, score: float, area: float) -> bool:
        return (
            score >= self.config.min_score
            and area >= self.config.min_changed_area
        )

    def _new_candidate(self, batch: _OpenBatch) -> VideoPassageCandidate:
        self._candidate_number += 1
        kind = (
            "camera_motion"
            if getattr(batch, "global_motion_frames", 0)
            >= self.config.global_motion_confirm_frames
            else "group"
            if batch.last_motion_at_ms - batch.started_at_ms
            >= self.config.group_duration_ms
            else "passage"
        )
        return VideoPassageCandidate(
            candidate_id=f"video-{self.config.camera_index}-{self._candidate_number}",
            camera_index=self.config.camera_index,
            started_at_ms=batch.started_at_ms,
            ended_at_ms=batch.last_motion_at_ms,
            peak_at_ms=batch.peak_at_ms,
            peak_score=batch.peak_score,
            changed_area=batch.changed_area,
            kind=kind,
        )

    def process_frame(
        self,
        timestamp_ms: int,
        frame: np.ndarray,
    ) -> tuple[VideoPassageCandidate, ...]:
        """Consume one sampled frame and return batches closed by this frame."""

        timestamp_ms = int(timestamp_ms)
        score, area = self._measure(frame)
        motion = self._is_motion(score, area)
        emitted: list[VideoPassageCandidate] = []

        if motion:
            self._positive_frames += 1
            if self._open is None:
                if self._positive_frames >= self.config.start_confirm_frames:
                    self._open = _OpenBatch(
                        started_at_ms=timestamp_ms,
                        last_motion_at_ms=timestamp_ms,
                        peak_at_ms=timestamp_ms,
                    peak_score=score,
                    changed_area=area,
                    global_motion_frames=(
                        1 if area >= self.config.global_motion_area else 0
                    ),
                )
            else:
                self._open.last_motion_at_ms = timestamp_ms
                self._open.quiet_frames = 0
                if area >= self.config.global_motion_area:
                    self._open.global_motion_frames += 1
                if score >= self._open.peak_score:
                    self._open.peak_score = score
                    self._open.peak_at_ms = timestamp_ms
                self._open.changed_area = max(self._open.changed_area, area)
                if (
                    timestamp_ms - self._open.started_at_ms
                    >= self.config.max_event_ms
                ):
                    emitted.append(self._new_candidate(self._open))
                    self._open = None
                    self._positive_frames = 0
            return tuple(emitted)

        self._positive_frames = 0
        if self._open is None:
            return ()
        self._open.quiet_frames += 1
        if self._open.quiet_frames >= self.config.end_confirm_frames:
            emitted.append(self._new_candidate(self._open))
            self._open = None
        return tuple(emitted)

    def flush(self) -> tuple[VideoPassageCandidate, ...]:
        """Close the current batch at end-of-file."""

        if self._open is None:
            return ()
        candidate = self._new_candidate(self._open)
        self._open = None
        return (candidate,)


class FixedCameraLineCrossingDetector:
    """Conservative detector for a stable camera and configured finish line."""

    def __init__(
        self,
        finish_line: FinishLine,
        *,
        camera_index: int | None = None,
        min_score: float = 0.015,
        min_changed_area: float = 0.006,
        cooldown_ms: int = 500,
    ):
        self.finish_line = finish_line
        self.camera_index = max(1, int(camera_index or finish_line.camera_index))
        self.min_score = float(min_score)
        self.min_changed_area = float(min_changed_area)
        self.cooldown_ms = max(0, int(cooldown_ms))
        self._previous: np.ndarray | None = None
        self._previous_center: tuple[float, float] | None = None
        self._last_crossing_ms = -10**12
        self._candidate_number = 0

    def reset_segment_state(self) -> None:
        """Forget frame-to-frame motion state at a video segment boundary."""

        self._previous = None
        self._previous_center = None

    def process_frame(
        self,
        timestamp_ms: int,
        frame: np.ndarray,
    ) -> tuple[VideoPassageCandidate, ...]:
        gray = LightweightVideoPassageDetector._gray(frame)
        if self._previous is None:
            self._previous = gray.copy()
            return ()
        if gray.shape != self._previous.shape:
            raise ValueError("frame shape cannot change during detection")
        difference = np.abs(gray - self._previous)
        self._previous = gray.copy()
        roi_left, roi_top, roi_right, roi_bottom = self.finish_line.roi
        x1 = max(0, min(gray.shape[1] - 1, int(gray.shape[1] * roi_left)))
        y1 = max(0, min(gray.shape[0] - 1, int(gray.shape[0] * roi_top)))
        x2 = max(x1 + 1, min(gray.shape[1], int(gray.shape[1] * roi_right)))
        y2 = max(y1 + 1, min(gray.shape[0], int(gray.shape[0] * roi_bottom)))
        roi_difference = difference[y1:y2, x1:x2]
        mask = roi_difference > 0.08
        area = float(mask.mean())
        score = float(roi_difference.mean())
        if score < self.min_score or area < self.min_changed_area:
            self._previous_center = None
            return ()
        ys, xs = np.nonzero(mask)
        if len(xs) == 0:
            self._previous_center = None
            return ()
        height, width = gray.shape
        center = (
            float(xs.mean() + x1) / width,
            float(ys.mean() + y1) / height,
        )
        previous_center = self._previous_center
        self._previous_center = center
        if previous_center is None or int(timestamp_ms) - self._last_crossing_ms < self.cooldown_ms:
            return ()
        if not self.finish_line.contains_crossing(previous_center, center):
            return ()
        self._last_crossing_ms = int(timestamp_ms)
        self._candidate_number += 1
        return (
            VideoPassageCandidate(
                candidate_id=f"line-{self.camera_index}-{self._candidate_number}",
                camera_index=self.camera_index,
                started_at_ms=int(timestamp_ms),
                ended_at_ms=int(timestamp_ms),
                peak_at_ms=int(timestamp_ms),
                peak_score=score,
                changed_area=area,
            ),
        )

    def flush(self) -> tuple[VideoPassageCandidate, ...]:
        """Close the detector using the common scanner interface."""
        return ()


def merge_candidates(
    candidates: Iterable[VideoPassageCandidate],
    *,
    merge_gap_ms: int = 350,
) -> tuple[VideoPassageCandidate, ...]:
    """Merge nearby detections into batches without guessing athlete count."""

    ordered = sorted(candidates, key=lambda item: (item.started_at_ms, item.candidate_id))
    if not ordered:
        return ()
    merged: list[VideoPassageCandidate] = [ordered[0]]
    for current in ordered[1:]:
        previous = merged[-1]
        if current.started_at_ms - previous.ended_at_ms > merge_gap_ms:
            merged.append(current)
            continue
        peak = current if current.peak_score > previous.peak_score else previous
        merged[-1] = VideoPassageCandidate(
            candidate_id=previous.candidate_id,
            camera_index=previous.camera_index,
            started_at_ms=previous.started_at_ms,
            ended_at_ms=max(previous.ended_at_ms, current.ended_at_ms),
            peak_at_ms=peak.peak_at_ms,
            peak_score=peak.peak_score,
            changed_area=max(previous.changed_area, current.changed_area),
            kind="group",
        )
    return tuple(merged)


def merge_line_crossings(
    candidates: Iterable[VideoPassageCandidate],
    *,
    batch_gap_ms: int = 1_500,
) -> tuple[VideoPassageCandidate, ...]:
    """Merge jitter and tightly spaced crossings into operator review batches."""
    if batch_gap_ms < 0:
        raise ValueError("batch_gap_ms must be non-negative")
    ordered = sorted(candidates, key=lambda item: (item.peak_at_ms, item.candidate_id))
    if not ordered:
        return ()
    batches: list[VideoPassageCandidate] = [ordered[0]]
    for current in ordered[1:]:
        previous = batches[-1]
        if current.started_at_ms - previous.ended_at_ms > batch_gap_ms:
            batches.append(current)
            continue
        peak = current if current.peak_score > previous.peak_score else previous
        batches[-1] = VideoPassageCandidate(
            candidate_id=previous.candidate_id,
            camera_index=previous.camera_index,
            started_at_ms=min(previous.started_at_ms, current.started_at_ms),
            ended_at_ms=max(previous.ended_at_ms, current.ended_at_ms),
            peak_at_ms=peak.peak_at_ms,
            peak_score=peak.peak_score,
            changed_area=max(previous.changed_area, current.changed_area),
            kind="group",
            status=previous.status,
            segment_id=previous.segment_id,
            video_path=previous.video_path,
            video_position_ms=previous.video_position_ms,
        )
    return tuple(batches)


def scan_video_file(
    video_path: str | Path,
    *,
    started_at_ms: int,
    width: int,
    height: int,
    ffmpeg_path: str | Path = "ffmpeg",
    sample_fps: float = 8.0,
    roi: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0),
    detector: LightweightVideoPassageDetector | None = None,
    process_callback: Callable[[subprocess.Popen | None], None] | None = None,
) -> tuple[VideoPassageCandidate, ...]:
    """Scan a video at low FPS through FFmpeg without retaining video frames.

    ``roi`` is normalized as ``(left, top, right, bottom)``. The caller should
    provide the actual segment start time from the video timeline. This helper
    intentionally uses raw grayscale frames and a pipe, so it does not create a
    second encoded video or a large in-memory cache.
    """

    video_path = Path(video_path).absolute()
    if width <= 0 or height <= 0:
        raise ValueError("width and height must be positive")
    if sample_fps <= 0:
        raise ValueError("sample_fps must be positive")
    left, top, right, bottom = (float(value) for value in roi)
    if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
        raise ValueError("roi must be normalized and non-empty")
    crop_left, crop_right = int(width * left), max(int(width * right), 1)
    crop_top, crop_bottom = int(height * top), max(int(height * bottom), 1)
    crop_width = max(1, crop_right - crop_left)
    crop_height = max(1, crop_bottom - crop_top)
    input_args = ["-i", str(video_path)]
    if video_path.suffix.lower() == ".ffconcat":
        input_args = ["-f", "concat", "-safe", "0", *input_args]
    command = [
        str(ffmpeg_path), "-hide_banner", "-loglevel", "error", "-nostdin",
        *input_args, "-vf",
        f"scale={int(width)}:{int(height)},fps={float(sample_fps):g}",
        "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1",
    ]
    process_kwargs = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
    }
    if os.name == "nt":
        # Keep candidate scanning below the recorder and the foreground judge.
        # ``BELOW_NORMAL_PRIORITY_CLASS`` is unavailable on non-Windows test
        # doubles, so the fallback remains the existing no-console flag.
        process_kwargs["creationflags"] = getattr(
            subprocess,
            "CREATE_NO_WINDOW",
            0,
        ) | _BELOW_NORMAL_PRIORITY_CLASS
    process = subprocess.Popen(command, **process_kwargs)
    if process_callback is not None:
        process_callback(process)
    active_detector = detector or LightweightVideoPassageDetector()
    frame_size = width * height
    frame_index = 0
    candidates: list[VideoPassageCandidate] = []
    try:
        assert process.stdout is not None
        while True:
            payload = process.stdout.read(frame_size)
            if not payload:
                break
            if len(payload) != frame_size:
                break
            frame = np.frombuffer(payload, dtype=np.uint8).reshape((height, width))
            # Finish-line coordinates are normalized against the full frame;
            # ordinary ROI detection can use the smaller cropped array.
            cropped = (
                frame
                if isinstance(active_detector, FixedCameraLineCrossingDetector)
                else frame[crop_top:crop_bottom, crop_left:crop_right]
            )
            timestamp_ms = int(round(int(started_at_ms) + frame_index * 1000.0 / sample_fps))
            candidates.extend(active_detector.process_frame(timestamp_ms, cropped))
            frame_index += 1
        candidates.extend(active_detector.flush())
    finally:
        if process_callback is not None:
            process_callback(None)
        if process.stdout is not None:
            process.stdout.close()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
    if process.returncode:
        detail = ""
        if process.stderr is not None:
            detail = process.stderr.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail or f"ffmpeg failed with exit code {process.returncode}")
    return tuple(candidates)


def reconcile_candidates(
    candidates: Sequence[VideoPassageCandidate],
    passage_times_ms: Iterable[int],
    *,
    tolerance_ms: int = 500,
    passage_time_offset_ms: int = 0,
    passage_time_offset_by_camera: Mapping[int, int] | None = None,
) -> tuple[VideoPassageReconciliation, ...]:
    """Classify only suspicious batches for manual review.

    A batch with one nearby chip is considered normal. Multiple chips in one
    batch are deliberately classified as a group instead of being split.
    """

    matched = match_candidate_counts(
        candidates,
        passage_times_ms,
        tolerance_ms=tolerance_ms,
        passage_time_offset_ms=passage_time_offset_ms,
        passage_time_offset_by_camera=passage_time_offset_by_camera,
    )
    result = []
    for candidate, chip_count in matched:
        if candidate.is_camera_motion:
            anomaly = "机位运动过大，无法可靠判读"
        elif chip_count == 0:
            anomaly = "视频候选无芯片记录"
        elif candidate.is_group or chip_count > 1:
            anomaly = "多人批次，芯片记录多于视频事件"
        else:
            anomaly = "正常匹配"
        result.append(VideoPassageReconciliation(candidate, chip_count, anomaly))
    return tuple(result)


def unmatched_passage_times(
    candidates: Sequence[VideoPassageCandidate],
    passage_times_ms: Iterable[int],
    *,
    tolerance_ms: int = 500,
    passage_time_offset_ms: int = 0,
    passage_time_offset_by_camera: Mapping[int, int] | None = None,
) -> tuple[int, ...]:
    """Return chip times with no nearby visual batch."""

    offset = int(passage_time_offset_ms)
    ranges = sorted(
        (
            item.started_at_ms
            - tolerance_ms
            - int(
                passage_time_offset_by_camera.get(
                    int(item.camera_index), offset
                )
                if passage_time_offset_by_camera is not None
                else offset
            ),
            item.ended_at_ms
            + tolerance_ms
            - int(
                passage_time_offset_by_camera.get(
                    int(item.camera_index), offset
                )
                if passage_time_offset_by_camera is not None
                else offset
            ),
        )
        for item in candidates
    )
    merged: list[list[int]] = []
    for start, end in ranges:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    unmatched = []
    interval_index = 0
    for timestamp in sorted(int(value) for value in passage_times_ms):
        while (
            interval_index < len(merged)
            and merged[interval_index][1] < timestamp
        ):
            interval_index += 1
        if (
            interval_index >= len(merged)
            or timestamp < merged[interval_index][0]
        ):
            unmatched.append(timestamp)
    return tuple(unmatched)


def match_candidate_counts(
    candidates: Sequence[VideoPassageCandidate],
    passage_times_ms: Iterable[int],
    *,
    tolerance_ms: int = 500,
    passage_time_offset_ms: int = 0,
    passage_time_offset_by_camera: Mapping[int, int] | None = None,
) -> tuple[tuple[VideoPassageCandidate, int], ...]:
    """Count chip events inside each visual batch for anomaly-first review."""

    if tolerance_ms < 0:
        raise ValueError("tolerance_ms must be non-negative")
    offset = int(passage_time_offset_ms)
    raw_times = tuple(sorted(int(value) for value in passage_times_ms))
    result = []
    for candidate in candidates:
        candidate_offset = offset
        if passage_time_offset_by_camera is not None:
            candidate_offset = int(
                passage_time_offset_by_camera.get(
                    int(candidate.camera_index),
                    candidate_offset,
                )
            )
        lower = candidate.started_at_ms - tolerance_ms - candidate_offset
        upper = candidate.ended_at_ms + tolerance_ms - candidate_offset
        count = bisect_right(raw_times, upper) - bisect_left(raw_times, lower)
        result.append((candidate, count))
    return tuple(result)


class VideoPassageScanWorker:
    """Cooperative background scanner for completed ordinary-video segments."""

    def __init__(
        self,
        segment_provider: Callable[[], Iterable[object]],
        result_callback: Callable[[tuple[VideoPassageCandidate, ...]], None],
        *,
        camera_index: int = 1,
        width: int,
        height: int,
        ffmpeg_path: str | Path = "ffmpeg",
        sample_fps: float = 8.0,
        roi: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0),
        interval_seconds: float = 2.0,
        path_resolver: Callable[[object], str | Path] | None = None,
        finish_line: FinishLine | None = None,
        line_batch_gap_ms: int = 1_500,
        lease_callback: Callable[[Sequence[object]], bool] | None = None,
        release_callback: Callable[[Sequence[object]], None] | None = None,
        progress_callback: Callable[[int | None], None] | None = None,
    ):
        self.segment_provider = segment_provider
        self.result_callback = result_callback
        self.camera_index = max(1, int(camera_index))
        self.width = int(width)
        self.height = int(height)
        self.ffmpeg_path = ffmpeg_path
        self.sample_fps = float(sample_fps)
        self.roi = roi
        self.interval_seconds = max(0.5, float(interval_seconds))
        self.path_resolver = path_resolver or (
            lambda segment: str(getattr(segment, "video_path", ""))
        )
        self.finish_line = finish_line
        self.line_batch_gap_ms = max(0, int(line_batch_gap_ms))
        self.lease_callback = lease_callback
        self.release_callback = release_callback
        self.progress_callback = progress_callback
        self._stop = threading.Event()
        self._pause_requested = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._scanned_ids: set[str] = set()
        self._retained_leases: dict[str, tuple[object, ...]] = {}
        self._line_detector: FixedCameraLineCrossingDetector | None = None
        self._line_detector_last_end_ms: int | None = None
        self._process_lock = threading.Lock()
        self._active_process: subprocess.Popen | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="video-passage-scan",
            daemon=True,
        )
        self._thread.start()

    def request_scan(self) -> None:
        self._wake.set()

    def pause(self) -> None:
        """Temporarily stop starting new FFmpeg scans without losing state."""

        self._pause_requested.set()
        self._wake.set()

    def resume(self) -> None:
        """Resume scanning after a temporary playback pause."""

        self._pause_requested.clear()
        self._wake.set()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self._wake.set()
        with self._process_lock:
            process = self._active_process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0.1, float(timeout)))
        stopped = thread is None or not thread.is_alive()
        if stopped:
            self._thread = None
            retained = tuple(self._retained_leases.values())
            self._retained_leases.clear()
            if self.release_callback is not None:
                for lease_values in retained:
                    self.release_callback(lease_values)
            self._notify_progress(None)

    def _set_active_process(self, process: subprocess.Popen | None) -> None:
        with self._process_lock:
            self._active_process = process

    def _notify_progress(self, value: int | None) -> None:
        if value is not None and self._stop.is_set():
            return
        if self.progress_callback is not None:
            self.progress_callback(value)

    def _update_scan_watermark(self, segments: Sequence[object]) -> None:
        pending_starts = [
            int(getattr(segment, "started_at_ms"))
            for segment in segments
            if str(getattr(segment, "segment_id", "")) not in self._scanned_ids
        ]
        self._notify_progress(min(pending_starts) if pending_starts else None)

    def scan_once(
        self,
        *,
        result_callback: Callable[
            [tuple[VideoPassageCandidate, ...]], None
        ] | None = None,
    ) -> tuple[VideoPassageCandidate, ...]:
        """Scan pending segments and optionally publish each segment result.

        The optional callback keeps archive review responsive: a long archive
        is scanned segment by segment, so the operator can navigate to the
        first discovered candidates before the remaining files finish.
        """
        found: list[VideoPassageCandidate] = []
        segments = tuple(self.segment_provider())
        self._update_scan_watermark(segments)
        for segment in segments:
            if self._stop.is_set() or self._pause_requested.is_set():
                break
            segment_id = str(getattr(segment, "segment_id", ""))
            path = Path(self.path_resolver(segment))
            if not segment_id or segment_id in self._scanned_ids or not path.is_file():
                continue
            lease_values = self._retained_leases.get(segment_id, (segment,))
            if segment_id not in self._retained_leases:
                if (
                    self.lease_callback is not None
                    and not self.lease_callback(lease_values)
                ):
                    continue
            try:
                try:
                    segment_start_ms = int(getattr(segment, "started_at_ms"))
                    camera_index = int(
                        getattr(segment, "camera_index", self.camera_index)
                    )
                    detector = None
                    if self.finish_line is not None:
                        if (
                            self._line_detector is None
                            or self._line_detector_last_end_ms is None
                            or segment_start_ms - self._line_detector_last_end_ms > 1_000
                            or self._line_detector.camera_index != max(1, camera_index)
                        ):
                            self._line_detector = FixedCameraLineCrossingDetector(
                                self.finish_line,
                                camera_index=camera_index,
                            )
                        else:
                            self._line_detector.reset_segment_state()
                        detector = self._line_detector
                    segment_candidates = scan_video_file(
                        path,
                        started_at_ms=segment_start_ms,
                        width=self.width,
                        height=self.height,
                        ffmpeg_path=self.ffmpeg_path,
                        sample_fps=self.sample_fps,
                        roi=self.roi,
                        detector=detector,
                        process_callback=self._set_active_process,
                    )
                    segment_candidates = (
                        merge_line_crossings(
                            segment_candidates,
                            batch_gap_ms=self.line_batch_gap_ms,
                        )
                        if self.finish_line is not None
                        else merge_candidates(segment_candidates)
                    )
                    segment_started_at_ms = segment_start_ms
                    found.extend(
                        replace(
                            candidate,
                            candidate_id=f"{segment_id}:{candidate.candidate_id}",
                            camera_index=camera_index,
                            segment_id=segment_id,
                            video_path=str(path),
                            video_position_ms=max(
                                0,
                                int(candidate.peak_at_ms - segment_started_at_ms),
                            ),
                        )
                        for candidate in segment_candidates
                    )
                    segment_found = (
                        tuple(found[-len(segment_candidates):])
                        if segment_candidates
                        else ()
                    )
                except (OSError, RuntimeError, ValueError):
                    # A bad or removed segment must not stop recording.
                    continue
                if (
                    segment_found
                    and result_callback is not None
                    and not self._stop.is_set()
                ):
                    try:
                        result_callback(segment_found)
                    except Exception:
                        self._retained_leases[segment_id] = lease_values
                        raise
                if self.finish_line is not None:
                    self._line_detector_last_end_ms = int(
                        getattr(segment, "ended_at_ms", segment_started_at_ms)
                    )
                self._scanned_ids.add(segment_id)
                self._retained_leases.pop(segment_id, None)
            finally:
                if (
                    segment_id not in self._retained_leases
                    and self.release_callback is not None
                ):
                    self.release_callback(lease_values)
                self._update_scan_watermark(segments)
        return tuple(found)

    def _run(self) -> None:
        while not self._stop.is_set():
            if self._pause_requested.is_set():
                self._wake.wait(0.1)
                self._wake.clear()
                continue
            try:
                self.scan_once(result_callback=self.result_callback)
            except (OSError, RuntimeError, ValueError):
                # Persistence and mutable media failures are retried next poll.
                pass
            self._wake.wait(self.interval_seconds)
            self._wake.clear()


class LiveReviewBatchScanWorker:
    """Low-priority scanner for one continuously recorded camera.

    Completed HLS segments are joined into one temporary concat manifest per
    120-second core batch. A two-second overlap gives the detector enough
    context at both edges while half-open core ownership and absolute-time
    de-duplication ensure that a crossing is published once.
    """

    def __init__(
        self,
        segment_provider: Callable[[], Iterable[object]],
        result_callback: Callable[[tuple[VideoPassageCandidate, ...]], None],
        *,
        camera_index: int = 1,
        width: int = 480,
        height: int = 270,
        ffmpeg_path: str | Path = "ffmpeg",
        sample_fps: float = 3.0,
        roi: tuple[float, float, float, float] = (0.35, 0.15, 0.65, 0.95),
        interval_seconds: float = 2.0,
        batch_ms: int = 120_000,
        overlap_ms: int = 2_000,
        path_resolver: Callable[[object], str | Path] | None = None,
        finish_line: FinishLine | None = None,
        line_batch_gap_ms: int = 1_500,
        manifest_dir: str | Path | None = None,
        segment_gap_tolerance_ms: int = 250,
        progress_callback: Callable[[int | None], None] | None = None,
        lease_callback: Callable[[Sequence[object]], bool] | None = None,
        release_callback: Callable[[Sequence[object]], None] | None = None,
        permanent_gap_callback: Callable[[int, int], bool] | None = None,
        cursor_reader: Callable[[str], int | None] | None = None,
        cursor_committer: Callable[[str, int], int] | None = None,
        scanner_id: str | None = None,
    ):
        self.segment_provider = segment_provider
        self.result_callback = result_callback
        self.camera_index = max(1, int(camera_index))
        self.width = int(width)
        self.height = int(height)
        self.ffmpeg_path = ffmpeg_path
        self.sample_fps = float(sample_fps)
        self.roi = roi
        self.interval_seconds = max(0.5, float(interval_seconds))
        self.batch_ms = int(batch_ms)
        self.overlap_ms = max(0, int(overlap_ms))
        if self.batch_ms <= 0:
            raise ValueError("batch_ms must be positive")
        self.path_resolver = path_resolver or (
            lambda segment: str(getattr(segment, "video_path", ""))
        )
        self.finish_line = finish_line
        self.line_batch_gap_ms = max(0, int(line_batch_gap_ms))
        self.manifest_dir = Path(manifest_dir).resolve() if manifest_dir else None
        self.segment_gap_tolerance_ms = max(0, int(segment_gap_tolerance_ms))
        self.progress_callback = progress_callback
        self.lease_callback = lease_callback
        self.release_callback = release_callback
        self.permanent_gap_callback = permanent_gap_callback
        self.cursor_reader = cursor_reader
        self.cursor_committer = cursor_committer
        self.scanner_id = str(scanner_id or f"camera-{self.camera_index}:live_batch_v2")
        self._stop = threading.Event()
        self._pause_requested = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._next_batch_start_ms: int | None = None
        if self.cursor_reader is not None:
            self._next_batch_start_ms = self.cursor_reader(self.scanner_id)
        self._emitted_keys: set[tuple[int, int]] = set()
        self._retained_leases: dict[int, tuple[object, ...]] = {}
        self._process_lock = threading.Lock()
        self._active_process: subprocess.Popen | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="live-review-batch-scan",
            daemon=True,
        )
        self._thread.start()

    def request_scan(self) -> None:
        self._wake.set()

    def pause(self) -> None:
        self._pause_requested.set()
        self._terminate_active_process()
        self._wake.set()

    def resume(self) -> None:
        self._pause_requested.clear()
        self._wake.set()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self._wake.set()
        self._terminate_active_process()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0.1, float(timeout)))
        stopped = thread is None or not thread.is_alive()
        if stopped:
            self._thread = None
            retained = tuple(self._retained_leases.values())
            self._retained_leases.clear()
            if self.release_callback is not None:
                for lease_values in retained:
                    self.release_callback(lease_values)
            self._notify_progress(None)

    def _set_active_process(self, process: subprocess.Popen | None) -> None:
        with self._process_lock:
            self._active_process = process

    def _terminate_active_process(self) -> None:
        with self._process_lock:
            process = self._active_process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass

    def _notify_progress(self, value: int | None) -> None:
        if value is not None and self._stop.is_set():
            return
        callback = self.progress_callback
        if callback is not None:
            try:
                callback(value)
            except Exception:  # noqa: BLE001 - progress must not stop scanning.
                pass

    def _segments_are_contiguous(self, segments: Sequence[object]) -> bool:
        previous_end: int | None = None
        for segment in segments:
            started = int(getattr(segment, "started_at_ms"))
            ended = int(getattr(segment, "ended_at_ms"))
            if ended <= started:
                return False
            if (
                previous_end is not None
                and started - previous_end > self.segment_gap_tolerance_ms
            ):
                return False
            previous_end = ended
        return True

    def _batch_has_core_coverage(
        self,
        batch: ReviewBatchRange,
        segments: Sequence[object],
        ordered: Sequence[object],
    ) -> bool:
        """Reject gaps at a batch edge without rejecting an initial partial batch."""
        if not segments:
            return False
        initial_start = review_batch_start(
            int(getattr(ordered[0], "started_at_ms")), batch_ms=self.batch_ms
        )
        tolerance = self.segment_gap_tolerance_ms
        tail_covered = int(getattr(segments[-1], "ended_at_ms")) >= (
            batch.core_ended_at_ms - tolerance
        )
        if batch.core_started_at_ms == initial_start:
            return tail_covered
        return (
            int(getattr(segments[0], "started_at_ms"))
            <= batch.core_started_at_ms + tolerance
            and tail_covered
        )

    @staticmethod
    def _manifest_reference(path: Path) -> str:
        # FFconcat accepts POSIX separators and single-quote escaping for
        # Windows paths. The manifest uses absolute paths to avoid staging or
        # linking large HLS segments on an 8 GB field computer.
        value = path.resolve().as_posix()
        return value.replace("'", "'\\''")

    def _write_manifest(
        self,
        batch: ReviewBatchRange,
        segments: Sequence[object],
    ) -> Path:
        directory = self.manifest_dir
        if directory is None:
            first_path = Path(self.path_resolver(segments[0])).resolve()
            directory = first_path.parent
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (
            f".live_review_{self.camera_index}_{batch.core_started_at_ms}.ffconcat"
        )
        lines = ["ffconcat version 1.0"]
        for segment in segments:
            lines.append(f"file '{self._manifest_reference(Path(self.path_resolver(segment)))}'")
            lines.append(
                f"duration {max(1, int(getattr(segment, 'ended_at_ms') - getattr(segment, 'started_at_ms'))) / 1000.0:.6f}"
            )
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(temporary, path)
        return path

    def _ready_batch(self, segments: Sequence[object]) -> tuple[ReviewBatchRange, ...]:
        if not segments:
            return ()
        ordered = tuple(
            sorted(segments, key=lambda item: (int(getattr(item, "started_at_ms")), str(getattr(item, "segment_id", ""))))
        )
        if self._next_batch_start_ms is None:
            self._next_batch_start_ms = review_batch_start(
                int(getattr(ordered[0], "started_at_ms")), batch_ms=self.batch_ms
            )
            # Protect the first batch while it is still accumulating coverage.
            self._notify_progress(self._next_batch_start_ms)
        latest_end = max(int(getattr(item, "ended_at_ms")) for item in ordered)
        ready = []
        cursor = self._next_batch_start_ms
        while cursor + self.batch_ms + self.overlap_ms <= latest_end:
            ready.append(
                review_batch_range(
                    cursor,
                    batch_ms=self.batch_ms,
                    overlap_ms=self.overlap_ms,
                )
            )
            cursor += self.batch_ms
        return tuple(ready)

    def _core_segments(
        self,
        batch: ReviewBatchRange,
        selected: Sequence[object],
    ) -> tuple[object, ...]:
        return tuple(
            item
            for item in selected
            if int(getattr(item, "ended_at_ms")) > batch.core_started_at_ms
            and int(getattr(item, "started_at_ms")) < batch.core_ended_at_ms
        )

    def _connected_components(
        self,
        batch: ReviewBatchRange,
        selected: Sequence[object],
    ) -> tuple[tuple[object, ...], ...]:
        values = tuple(
            sorted(
                selected,
                key=lambda item: int(getattr(item, "started_at_ms")),
            )
        )
        tolerance = self.segment_gap_tolerance_ms
        components: list[list[object]] = []
        for item in values:
            if not components:
                components.append([item])
                continue
            previous = components[-1][-1]
            if int(getattr(item, "started_at_ms")) - int(
                getattr(previous, "ended_at_ms")
            ) <= tolerance:
                components[-1].append(item)
            else:
                components.append([item])
        return tuple(
            tuple(component)
            for component in components
            if any(
                int(getattr(item, "ended_at_ms")) > batch.core_started_at_ms
                and int(getattr(item, "started_at_ms"))
                < batch.core_ended_at_ms
                for item in component
            )
        )

    def _incomplete_ranges(
        self,
        batch: ReviewBatchRange,
        selected: Sequence[object],
        ordered: Sequence[object],
    ) -> tuple[tuple[int, int], ...]:
        if not selected:
            return ((batch.core_started_at_ms, batch.core_ended_at_ms),)
        values = tuple(
            sorted(selected, key=lambda item: int(getattr(item, "started_at_ms")))
        )
        ranges = []
        initial_start = review_batch_start(
            int(getattr(ordered[0], "started_at_ms")),
            batch_ms=self.batch_ms,
        )
        first_start = int(getattr(values[0], "started_at_ms"))
        if (
            batch.core_started_at_ms != initial_start
            and first_start
            > batch.core_started_at_ms + self.segment_gap_tolerance_ms
        ):
            ranges.append((batch.core_started_at_ms, first_start))
        for previous, following in zip(values, values[1:]):
            previous_end = int(getattr(previous, "ended_at_ms"))
            following_start = int(getattr(following, "started_at_ms"))
            if following_start - previous_end > self.segment_gap_tolerance_ms:
                ranges.append((previous_end, following_start))
        last_end = int(getattr(values[-1], "ended_at_ms"))
        if last_end < batch.core_ended_at_ms - self.segment_gap_tolerance_ms:
            ranges.append((last_end, batch.core_ended_at_ms))
        return tuple(ranges)

    @staticmethod
    def _map_concat_timestamp(
        timestamp_ms: int,
        selected: Sequence[object],
    ) -> int:
        if not selected:
            return int(timestamp_ms)
        playback_offset = max(
            0,
            int(timestamp_ms) - int(getattr(selected[0], "started_at_ms")),
        )
        concat_offset = 0
        for segment in selected:
            duration = max(
                1,
                int(getattr(segment, "ended_at_ms"))
                - int(getattr(segment, "started_at_ms")),
            )
            if playback_offset < concat_offset + duration:
                return int(getattr(segment, "started_at_ms")) + max(
                    0,
                    playback_offset - concat_offset,
                )
            concat_offset += duration
        last = selected[-1]
        return int(getattr(last, "ended_at_ms"))

    def _scan_ready_batch(
        self,
        batch: ReviewBatchRange,
        selected: Sequence[object],
    ) -> tuple[
        bool,
        tuple[VideoPassageCandidate, ...],
        tuple[tuple[int, int], ...],
    ]:
        try:
            manifest = self._write_manifest(batch, selected)
        except (OSError, ValueError):
            return False, (), ()
        detector = (
            FixedCameraLineCrossingDetector(
                self.finish_line,
                camera_index=self.camera_index,
            )
            if self.finish_line is not None
            else LightweightVideoPassageDetector(
                VideoPassageDetectorConfig(camera_index=self.camera_index)
            )
        )
        try:
            candidates = scan_video_file(
                manifest,
                started_at_ms=int(getattr(selected[0], "started_at_ms")),
                width=self.width,
                height=self.height,
                ffmpeg_path=self.ffmpeg_path,
                sample_fps=self.sample_fps,
                roi=self.roi,
                detector=detector,
                process_callback=self._set_active_process,
            )
        except (OSError, RuntimeError, ValueError):
            return False, (), ()
        finally:
            try:
                manifest.unlink(missing_ok=True)
            except OSError:
                pass
        merged = (
            merge_line_crossings(candidates, batch_gap_ms=self.line_batch_gap_ms)
            if self.finish_line is not None
            else merge_candidates(candidates)
        )
        found = []
        found_keys = []
        for candidate in merged:
            mapped_candidate = replace(
                candidate,
                started_at_ms=self._map_concat_timestamp(
                    candidate.started_at_ms,
                    selected,
                ),
                ended_at_ms=self._map_concat_timestamp(
                    candidate.ended_at_ms,
                    selected,
                ),
                peak_at_ms=self._map_concat_timestamp(
                    candidate.peak_at_ms,
                    selected,
                ),
            )
            if not (
                batch.core_started_at_ms
                <= mapped_candidate.peak_at_ms
                < batch.core_ended_at_ms
            ):
                continue
            key = (
                int(mapped_candidate.camera_index),
                int(mapped_candidate.peak_at_ms),
            )
            if key in self._emitted_keys:
                continue
            owner = next(
                (
                    item
                    for item in selected
                    if int(getattr(item, "started_at_ms"))
                    <= mapped_candidate.peak_at_ms
                    < int(getattr(item, "ended_at_ms"))
                ),
                selected[0],
            )
            segment_id = str(getattr(owner, "segment_id", ""))
            path = Path(self.path_resolver(owner)).resolve()
            found.append(
                replace(
                    mapped_candidate,
                    candidate_id=(
                        f"batch:{batch.core_started_at_ms}:{candidate.candidate_id}"
                    ),
                    camera_index=self.camera_index,
                    segment_id=segment_id,
                    video_path=str(path),
                    video_position_ms=max(
                        0,
                        int(
                            mapped_candidate.peak_at_ms
                            - int(getattr(owner, "started_at_ms"))
                        ),
                    ),
                )
            )
            found_keys.append(key)
        return True, tuple(found), tuple(found_keys)

    def scan_once(self) -> tuple[VideoPassageCandidate, ...]:
        if self._stop.is_set() or self._pause_requested.is_set():
            return ()
        segments = tuple(self.segment_provider())
        ordered = tuple(
            sorted(segments, key=lambda item: (int(getattr(item, "started_at_ms")), str(getattr(item, "segment_id", ""))))
        )
        found: list[VideoPassageCandidate] = []
        next_progress_ms: int | None = None
        result_protection_start_ms: int | None = None
        for batch in self._ready_batch(ordered):
            if self._stop.is_set() or self._pause_requested.is_set():
                break
            selected = tuple(
                item
                for item in ordered
                if int(getattr(item, "ended_at_ms")) > batch.scan_started_at_ms
                and int(getattr(item, "started_at_ms")) < batch.scan_ended_at_ms
                and Path(self.path_resolver(item)).is_file()
            )
            core_selected = self._core_segments(batch, selected)
            incomplete = (
                not core_selected
                or not self._batch_has_core_coverage(
                    batch,
                    core_selected,
                    ordered,
                )
                or not self._segments_are_contiguous(core_selected)
            )
            if incomplete:
                missing_ranges = self._incomplete_ranges(
                    batch,
                    core_selected,
                    ordered,
                )
                permanent_gap = bool(
                    missing_ranges
                    and self.permanent_gap_callback is not None
                    and all(
                        self.permanent_gap_callback(start, end)
                        for start, end in missing_ranges
                    )
                )
                if not permanent_gap:
                    break
                self._next_batch_start_ms = batch.core_ended_at_ms
                next_progress_ms = batch.core_ended_at_ms
                continue
            components = self._connected_components(batch, selected)
            selected_for_scan = tuple(
                item for component in components for item in component
            )
            retained_lease = self._retained_leases.get(batch.core_started_at_ms)
            lease_values = retained_lease or selected_for_scan
            if (
                retained_lease is None
                and self.lease_callback is not None
                and not self.lease_callback(lease_values)
            ):
                break
            batch_found_all: list[VideoPassageCandidate] = []
            batch_keys_all: list[tuple[int, int]] = []
            scan_succeeded = True
            try:
                for component in components:
                    component_ok, component_found, component_keys = (
                        self._scan_ready_batch(batch, component)
                    )
                    if not component_ok:
                        scan_succeeded = False
                        break
                    batch_found_all.extend(component_found)
                    batch_keys_all.extend(component_keys)
                if (
                    scan_succeeded
                    and batch_found_all
                    and not self._stop.is_set()
                    and self.result_callback is not None
                ):
                    self.result_callback(tuple(batch_found_all))
                if (
                    scan_succeeded
                    and self.cursor_committer is not None
                ):
                    self.cursor_committer(
                        self.scanner_id,
                        batch.core_ended_at_ms,
                    )
            except Exception:
                self._retained_leases[batch.core_started_at_ms] = lease_values
                raise
            else:
                self._retained_leases.pop(batch.core_started_at_ms, None)
                if self.release_callback is not None:
                    self.release_callback(lease_values)
            if not scan_succeeded:
                break
            self._emitted_keys.update(batch_keys_all)
            found.extend(batch_found_all)
            self._next_batch_start_ms = batch.core_ended_at_ms
            next_progress_ms = self._next_batch_start_ms
            if batch_found_all:
                result_protection_start_ms = (
                    batch.core_started_at_ms
                    if result_protection_start_ms is None
                    else min(result_protection_start_ms, batch.core_started_at_ms)
                )
            # Yield between FFmpeg runs when several batches are waiting, so
            # recording and the foreground judge remain responsive on older
            # single-camera machines.
            time.sleep(0)
        result = tuple(found)
        if not self._stop.is_set() and next_progress_ms is not None:
            self._notify_progress(
                result_protection_start_ms
                if result_protection_start_ms is not None
                else next_progress_ms
            )
        return result

    def _run(self) -> None:
        while not self._stop.is_set():
            if not self._pause_requested.is_set():
                try:
                    self.scan_once()
                except (OSError, RuntimeError, ValueError):
                    # HLS playlists and segment files can change underneath a
                    # polling pass. Keep the recorder independent and retry
                    # on the next wake-up instead of killing the scanner.
                    pass
            self._wake.wait(self.interval_seconds)
            self._wake.clear()


__all__ = [
    "LightweightVideoPassageDetector",
    "FixedCameraLineCrossingDetector",
    "VideoPassageCandidate",
    "VideoPassageDetectorConfig",
    "merge_candidates",
    "merge_line_crossings",
    "scan_video_file",
    "VideoPassageScanWorker",
    "ReviewBatchRange",
    "review_batch_range",
    "review_batch_start",
    "LiveReviewBatchScanWorker",
    "match_candidate_counts",
    "VideoPassageReconciliation",
    "reconcile_candidates",
    "unmatched_passage_times",
]
