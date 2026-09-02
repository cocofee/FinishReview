"""Lightweight ordinary-video passage candidate detection.

This module deliberately detects motion batches near a configured finish-line ROI.
It does not identify athletes, create official timing records, or require one
candidate per athlete.  It is intended to narrow the manual-review search area
on low-resource field computers.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import os
import subprocess
from pathlib import Path
import threading
import time
from typing import Callable, Iterable, Mapping, Optional, Sequence

import numpy as np

from .finish_line import FinishLine


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
    command = [
        str(ffmpeg_path), "-hide_banner", "-loglevel", "error", "-nostdin",
        "-i", str(video_path), "-vf",
        f"scale={int(width)}:{int(height)},fps={float(sample_fps):g}",
        "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1",
    ]
    process_kwargs = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
    }
    if os.name == "nt":
        process_kwargs["creationflags"] = getattr(
            subprocess,
            "CREATE_NO_WINDOW",
            0,
        )
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
    ranges = tuple(
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
    return tuple(
        int(timestamp)
        for timestamp in sorted(int(value) for value in passage_times_ms)
        if not any(start <= timestamp <= end for start, end in ranges)
    )


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
        times = tuple(value + candidate_offset for value in raw_times)
        count = sum(
            candidate.started_at_ms - tolerance_ms <= timestamp <= candidate.ended_at_ms + tolerance_ms
            for timestamp in times
        )
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
        self._stop = threading.Event()
        self._pause_requested = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._scanned_ids: set[str] = set()
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
        if thread is None or not thread.is_alive():
            self._thread = None

    def _set_active_process(self, process: subprocess.Popen | None) -> None:
        with self._process_lock:
            self._active_process = process

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
        for segment in tuple(self.segment_provider()):
            if self._stop.is_set() or self._pause_requested.is_set():
                break
            segment_id = str(getattr(segment, "segment_id", ""))
            path = Path(self.path_resolver(segment))
            if not segment_id or segment_id in self._scanned_ids or not path.is_file():
                continue
            try:
                segment_start_ms = int(getattr(segment, "started_at_ms"))
                camera_index = int(getattr(segment, "camera_index", self.camera_index))
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
                segment_found = tuple(found[-len(segment_candidates):]) if segment_candidates else ()
            except (OSError, RuntimeError, ValueError):
                # A bad or still-being-removed segment must not stop recording.
                continue
            if segment_found and result_callback is not None and not self._stop.is_set():
                result_callback(segment_found)
            if self.finish_line is not None:
                self._line_detector_last_end_ms = int(
                    getattr(segment, "ended_at_ms", segment_started_at_ms)
                )
            self._scanned_ids.add(segment_id)
        return tuple(found)

    def _run(self) -> None:
        while not self._stop.is_set():
            if self._pause_requested.is_set():
                self._wake.wait(0.1)
                self._wake.clear()
                continue
            self.scan_once(result_callback=self.result_callback)
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
    "match_candidate_counts",
    "VideoPassageReconciliation",
    "reconcile_candidates",
    "unmatched_passage_times",
]
