"""Measure real compressed-video seek and playback costs used by FinishReview."""

from __future__ import annotations

import argparse
import json
import platform
import random
import statistics
import time
from pathlib import Path
from typing import Callable, Iterable

import cv2
from PyQt5.QtCore import QCoreApplication, QEventLoop, QTimer

from realtime.video_playback import VideoPlaybackWorker


CaptureFactory = Callable[[str], cv2.VideoCapture]


def _percentile(samples: list[float], percentile: float) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    index = round((len(ordered) - 1) * percentile)
    return ordered[max(0, min(index, len(ordered) - 1))]


def _latency_summary(samples: list[float]) -> dict[str, float]:
    if not samples:
        return {
            "median_ms": 0.0,
            "p95_ms": 0.0,
            "min_ms": 0.0,
            "max_ms": 0.0,
        }
    return {
        "median_ms": round(statistics.median(samples), 3),
        "p95_ms": round(_percentile(samples, 0.95), 3),
        "min_ms": round(min(samples), 3),
        "max_ms": round(max(samples), 3),
    }


def _reverse_sequence_summary(
    frame_indexes: list[int],
    requested_frames: int,
) -> dict[str, object]:
    frame_deltas = [
        previous - current
        for previous, current in zip(frame_indexes, frame_indexes[1:])
    ]
    skipped_frames = sum(max(0, delta - 1) for delta in frame_deltas)
    out_of_order_pairs = sum(delta <= 0 for delta in frame_deltas)
    duplicate_frames = len(frame_indexes) - len(set(frame_indexes))
    return {
        "skipped_frames": skipped_frames,
        "out_of_order_pairs": out_of_order_pairs,
        "duplicate_frames": duplicate_frames,
        "strictly_descending": bool(frame_indexes)
        and out_of_order_pairs == 0
        and duplicate_frames == 0,
        "sequence_valid": len(frame_indexes) == requested_frames
        and skipped_frames == 0
        and out_of_order_pairs == 0
        and duplicate_frames == 0,
        "first_frame_index": frame_indexes[0] if frame_indexes else -1,
        "last_frame_index": frame_indexes[-1] if frame_indexes else -1,
    }


def _open_capture(video_path: Path, capture_factory: CaptureFactory):
    capture = capture_factory(str(video_path))
    if not capture or not capture.isOpened():
        raise RuntimeError(f"could not open video: {video_path}")
    return capture


def _measure_sequential(
    video_path: Path,
    frame_limit: int,
    capture_factory: CaptureFactory,
) -> dict[str, object]:
    capture = _open_capture(video_path, capture_factory)
    decoded = 0
    started = time.perf_counter()
    try:
        while decoded < frame_limit:
            ok, _frame = capture.read()
            if not ok:
                break
            decoded += 1
    finally:
        elapsed = max(time.perf_counter() - started, 1e-9)
        capture.release()
    return {
        "requested_frames": frame_limit,
        "decoded_frames": decoded,
        "elapsed_ms": round(elapsed * 1000.0, 3),
        "decode_fps": round(decoded / elapsed, 3),
    }


def _measure_seek_targets(
    video_path: Path,
    targets: Iterable[int],
    capture_factory: CaptureFactory,
) -> dict[str, object]:
    capture = _open_capture(video_path, capture_factory)
    latencies = []
    decoded = 0
    target_list = [max(0, int(target)) for target in targets]
    try:
        for target in target_list:
            started = time.perf_counter()
            positioned = capture.set(cv2.CAP_PROP_POS_FRAMES, target)
            ok, _frame = capture.read() if positioned else (False, None)
            latencies.append((time.perf_counter() - started) * 1000.0)
            if ok:
                decoded += 1
    finally:
        capture.release()
    elapsed_ms = sum(latencies)
    return {
        "requested_frames": len(target_list),
        "decoded_frames": decoded,
        "elapsed_ms": round(elapsed_ms, 3),
        "effective_fps": round(decoded * 1000.0 / max(elapsed_ms, 1e-9), 3),
        "latency": _latency_summary(latencies),
    }


def _measure_reverse_window(
    video_path: Path,
    *,
    frame_count: int,
    fps: float,
    width: int,
    height: int,
    reverse_frames: int,
    capture_factory: CaptureFactory,
) -> dict[str, object]:
    capture = _open_capture(video_path, capture_factory)
    worker = VideoPlaybackWorker(video_path, capture_factory=capture_factory)
    worker._fps = fps if fps > 0.1 else 25.0
    worker._frame_count = max(0, int(frame_count))
    worker._configure_reverse_window(width, height)
    usable_frames = max(1, int(frame_count))
    reverse_start = min(usable_frames - 1, max(0, int(usable_frames * 0.75)))
    targets = range(
        reverse_start,
        max(-1, reverse_start - max(1, int(reverse_frames))),
        -1,
    )
    decoded = 0
    capture_next_frame = 0
    latencies = []
    boundary_latencies = []
    started = time.perf_counter()
    try:
        for target in targets:
            with worker._cache_lock:
                was_cached = target in worker._frame_cache
            target_started = time.perf_counter()
            ok, capture_next_frame = worker._decode_target(
                capture,
                target,
                capture_next_frame,
                generation=worker._request_generation,
                reverse_window=True,
            )
            latency_ms = (time.perf_counter() - target_started) * 1000.0
            latencies.append(latency_ms)
            if not was_cached:
                boundary_latencies.append(latency_ms)
            if not ok:
                break
            decoded += 1
    finally:
        elapsed = max(time.perf_counter() - started, 1e-9)
        capture.release()
    return {
        "requested_frames": max(1, int(reverse_frames)),
        "decoded_frames": decoded,
        "elapsed_ms": round(elapsed * 1000.0, 3),
        "effective_fps": round(decoded / elapsed, 3),
        "first_frame_ms": round(latencies[0], 3) if latencies else 0.0,
        "latency": _latency_summary(latencies),
        "boundary_latency": _latency_summary(boundary_latencies),
        "window_frames": worker._reverse_window_frames,
        "cache_bytes": worker._frame_cache_bytes,
        "cache_limit_bytes": worker._max_cache_bytes,
    }


def _measure_reverse_prefetch_playback(
    video_path: Path,
    *,
    frame_count: int,
    fps: float,
    reverse_frames: int,
    capture_factory: CaptureFactory,
) -> dict[str, object]:
    app = QCoreApplication.instance() or QCoreApplication([])
    loop = QEventLoop()
    worker = VideoPlaybackWorker(
        video_path,
        capture_factory=capture_factory,
        reverse_prefetch=True,
    )
    worker._fps = fps if fps > 0.1 else 25.0
    worker._frame_count = max(0, int(frame_count))
    usable_frames = max(1, int(frame_count))
    start_frame = min(usable_frames - 1, max(0, int(usable_frames * 0.75)))
    requested_frames = min(max(1, int(reverse_frames)), start_frame + 1)
    frame_indexes = []
    timestamps = []
    errors = []
    finished = False

    def finish() -> None:
        nonlocal finished
        if finished:
            return
        finished = True
        worker.stop()
        loop.quit()

    def on_frame(_image, _position_ms: int, frame_index: int) -> None:
        timestamps.append(time.perf_counter())
        frame_indexes.append(int(frame_index))
        if len(frame_indexes) >= requested_frames:
            finish()

    worker.frame_ready.connect(on_frame)
    worker.playback_error.connect(lambda message: (errors.append(message), finish()))
    timeout = QTimer()
    timeout.setSingleShot(True)
    timeout.timeout.connect(finish)
    timeout_ms = max(
        3_000,
        int(requested_frames * 1000.0 / max(worker._fps, 0.1) * 2.5 + 2_000),
    )
    worker.seek_and_play(int(start_frame * 1000.0 / worker._fps), -1.0)
    timeout.start(timeout_ms)
    worker.start()
    loop.exec_()
    timeout.stop()
    worker.stop()
    worker.wait(3_000)
    app.processEvents()

    gaps_ms = [
        (current - previous) * 1000.0
        for previous, current in zip(timestamps, timestamps[1:])
    ]
    elapsed = (
        max(timestamps[-1] - timestamps[0], 1e-9)
        if len(timestamps) > 1
        else 0.0
    )
    return {
        "requested_frames": requested_frames,
        "displayed_frames": len(frame_indexes),
        **_reverse_sequence_summary(frame_indexes, requested_frames),
        "display_fps": round((len(frame_indexes) - 1) / elapsed, 3)
        if elapsed > 0
        else 0.0,
        "frame_gap_latency": _latency_summary(gaps_ms),
        "cache_limit_bytes": worker._max_cache_bytes,
        "error": errors[0] if errors else "",
    }


def benchmark_video(
    video_path: str | Path,
    *,
    sequential_frames: int = 250,
    seek_count: int = 40,
    reverse_frames: int = 100,
    capture_factory: CaptureFactory = cv2.VideoCapture,
) -> dict[str, object]:
    path = Path(video_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)

    capture = _open_capture(path, capture_factory)
    try:
        fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
        frame_count = max(0, int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0))
        width = max(0, int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0))
        height = max(0, int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0))
    finally:
        capture.release()

    usable_frames = max(1, frame_count)
    randomizer = random.Random(0)
    random_targets = [
        randomizer.randrange(usable_frames)
        for _ in range(max(1, int(seek_count)))
    ]
    reverse_start = min(
        usable_frames - 1,
        max(0, int(usable_frames * 0.75)),
    )
    reverse_targets = range(
        reverse_start,
        max(-1, reverse_start - max(1, int(reverse_frames))),
        -1,
    )

    return {
        "path": str(path),
        "size_bytes": path.stat().st_size,
        "metadata": {
            "fps": round(fps, 3),
            "frame_count": frame_count,
            "width": width,
            "height": height,
        },
        "sequential": _measure_sequential(
            path,
            max(1, int(sequential_frames)),
            capture_factory,
        ),
        "random_seek": _measure_seek_targets(
            path,
            random_targets,
            capture_factory,
        ),
        "reverse_random_seek": _measure_seek_targets(
            path,
            reverse_targets,
            capture_factory,
        ),
        "reverse_window": _measure_reverse_window(
            path,
            frame_count=frame_count,
            fps=fps,
            width=width,
            height=height,
            reverse_frames=reverse_frames,
            capture_factory=capture_factory,
        ),
        "reverse_prefetch_playback": _measure_reverse_prefetch_playback(
            path,
            frame_count=frame_count,
            fps=fps,
            reverse_frames=reverse_frames,
            capture_factory=capture_factory,
        ),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("videos", nargs="+", type=Path)
    parser.add_argument("--sequential-frames", type=int, default=250)
    parser.add_argument("--seek-count", type=int, default=40)
    parser.add_argument("--reverse-frames", type=int, default=100)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = {
        "environment": {
            "platform": platform.platform(),
            "python_opencv": cv2.__version__,
        },
        "videos": [
            benchmark_video(
                video,
                sequential_frames=args.sequential_frames,
                seek_count=args.seek_count,
                reverse_frames=args.reverse_frames,
            )
            for video in args.videos
        ],
    }
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output is not None:
        output_path = args.output.expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(encoded, encoding="utf-8", newline="\n")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
