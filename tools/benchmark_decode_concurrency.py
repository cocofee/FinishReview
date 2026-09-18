"""Repeat single-video reverse playback with thumbnail, activity and duration workers.

This measures decoder competition and Qt delivery, optionally including the
playback label's painting. It does not simulate a live recorder or the entire
review window. OS file caches are deliberately not flushed.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import threading
import time

import cv2
from PyQt5.QtCore import QCoreApplication, QEventLoop, QTimer
from PyQt5.QtWidgets import QApplication

from realtime.decode_resources import DECODE_RESOURCES, ImageCache, THUMBNAIL
from realtime.race_filmstrip import RaceThumbnailWorker
from realtime.recording_catalog import FilmstripSource
from realtime.video_activity import VideoActivityWorker
from realtime.video_playback import PlaybackVideoLabel, VideoPlaybackWorker
from realtime.video_timeline import PassageVideoLocation, RecordingSegment, probe_video_duration_ms
if __package__:
    from .benchmark_video_playback import _latency_summary, _reverse_sequence_summary
else:
    from benchmark_video_playback import _latency_summary, _reverse_sequence_summary


def _private_memory_reader():
    if os.name != "nt":
        return lambda: None
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
            (name, ctypes.c_size_t) for name in (
                "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage", "PrivateUsage",
            )
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    process = kernel.GetCurrentProcess()
    get_memory = ctypes.WinDLL("psapi", use_last_error=True).GetProcessMemoryInfo
    get_memory.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    get_memory.restype = wintypes.BOOL

    def read():
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        return int(counters.PrivateUsage) if get_memory(process, ctypes.byref(counters), counters.cb) else None

    return read


def measure(videos, *, frames=40, timeout_seconds=90, reverse_prefetch=True,
            background=True, render=False, prefetch_threads=0):
    app_class = QApplication if render else QCoreApplication
    app = app_class.instance() or app_class([])
    loop = QEventLoop()
    players, activities, thumbnails, rows = [], [], [], []
    labels = []
    errors, samples, ticks = [], [], []
    caches = []
    read_private_bytes = _private_memory_reader()
    probe_stop = threading.Event()
    probe_threads = []
    for camera_index, path in enumerate(videos, 1):
        capture = cv2.VideoCapture(str(path))
        try:
            if not capture.isOpened():
                raise ValueError(f"Cannot open {path}")
            fps = float(capture.get(cv2.CAP_PROP_FPS))
            count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        finally:
            capture.release()
        start_frame = int(count * 0.75)
        requested = min(frames, start_frame + 1)
        row = dict(path=str(path), fps=fps, count=count, requested=requested,
                   frame_indexes=[], delivery_ms=[], thumbnail_indexes=[],
                   activity_completed=False, activity_points=0, probe_attempts=0,
                   duration_ms=None)
        rows.append(row)
        label = None
        if render:
            class MeasuredLabel(PlaybackVideoLabel):
                current_index = None

                def paintEvent(self, event, row=row):
                    super().paintEvent(event)
                    if self.current_index is not None and (
                        not row["painted_indexes"] or row["painted_indexes"][-1] != self.current_index
                    ):
                        row["painted_indexes"].append(self.current_index)
                        row["painted_ms"].append(round((time.perf_counter() - started) * 1000, 3))

            row.update(painted_indexes=[], painted_ms=[])
            label = MeasuredLabel()
            label.resize(1280, 720)
            label.show()
            labels.append(label)
        factory = cv2.VideoCapture
        if prefetch_threads:
            def factory(path):
                if threading.current_thread().name.startswith("reverse-prefetch-"):
                    return cv2.VideoCapture(str(path), cv2.CAP_FFMPEG,
                                            [cv2.CAP_PROP_N_THREADS, prefetch_threads])
                return cv2.VideoCapture(str(path))
        player = VideoPlaybackWorker(path, reverse_prefetch=reverse_prefetch,
                                     idle_prefetch=False, capture_factory=factory)
        player.set_presentation_ack_enabled(render)
        player._fps, player._frame_count = fps, count
        player.seek_and_play(round(start_frame * 1000 / fps), -1.0)

        def on_frame(_image, _position, index, row=row, player=player, label=label):
            if len(row["frame_indexes"]) >= row["requested"]:
                return
            row["frame_indexes"].append(int(index))
            row["delivery_ms"].append(round((time.perf_counter() - started) * 1000, 3))
            if label is not None:
                label.current_index = int(index)
                label.set_frame(_image)
                label.repaint()
                player.acknowledge_presented_frame(int(index))
            if len(row["frame_indexes"]) == row["requested"]:
                player.stop()

        player.frame_ready.connect(on_frame)
        player.playback_error.connect(errors.append)
        players.append(player)
        if not background:
            row.update(activity_completed=True, expected_thumbnail_indexes=[],
                       duration_ms=round(count * 1000 / fps))
            continue
        end_ms = round((count - 1) * 1000 / fps)
        activity = VideoActivityWorker(path, 0, end_ms)
        activity.failed.connect(errors.append)
        activity.completed.connect(lambda row=row: row.update(activity_completed=True))
        activity.points_ready.connect(lambda points, row=row: row.update(
            activity_points=row["activity_points"] + len(points),
        ))
        activities.append(activity)
        segment = RecordingSegment(str(camera_index), str(camera_index), camera_index,
                                   str(path), 0, ended_at_ms=end_ms + 1)
        location = PassageVideoLocation(segment, path, 0, 0, 0, 0, "located")
        source = FilmstripSource(location, 0, end_ms + 1, True)
        times = [round(index * end_ms / 7) for index in range(8)]
        row["expected_thumbnail_indexes"] = [int(stamp * fps / 1000) for stamp in times]
        thumbnail = RaceThumbnailWorker([(source, stamp) for stamp in times])
        cache = ImageCache(priority=THUMBNAIL)
        caches.append(cache)

        def on_thumbnail(frame, row=row, cache=cache):
            row["thumbnail_indexes"].append(frame.frame_index)
            cache[frame.key] = frame.image

        thumbnail.frame_ready.connect(on_thumbnail)
        thumbnail.failed.connect(lambda _key, error: errors.append(error))
        thumbnails.append(thumbnail)

        def probe(path=path, row=row):
            while not probe_stop.is_set():
                row["probe_attempts"] += 1
                duration = probe_video_duration_ms(path, cancelled=probe_stop.is_set)
                if duration is not None:
                    row["duration_ms"] = duration
                    return
                probe_stop.wait(0.1)

        probe_threads.append(threading.Thread(target=probe))

    timer = QTimer()
    timer.setInterval(10)
    timed_out = False
    started = time.perf_counter()

    def tick():
        nonlocal timed_out
        elapsed = time.perf_counter() - started
        ticks.append(round(elapsed * 1000, 3))
        samples.append(dict(elapsed_ms=ticks[-1], private_bytes=read_private_bytes(),
                            **asdict(DECODE_RESOURCES.snapshot())))
        done = all(len(row["frame_indexes"]) == row["requested"]
                   and row["activity_completed"] and row["thumbnail_indexes"] == row["expected_thumbnail_indexes"]
                   and row["duration_ms"] is not None for row in rows)
        timed_out = elapsed >= timeout_seconds
        if done or timed_out or errors:
            loop.quit()

    timer.timeout.connect(tick)
    timer.start()
    workers = players + activities + thumbnails
    stopped = []
    try:
        # Launch analysis before foreground work to exercise cooperative yield.
        for worker in activities + players + thumbnails:
            worker.start()
        for thread in probe_threads:
            thread.start()
        loop.exec_()
    finally:
        timer.stop()
        probe_stop.set()
        for worker in workers:
            worker.request_stop() if hasattr(worker, "request_stop") else worker.stop()
        for worker in workers:
            stopped.append(worker.wait(5000))
            if not stopped[-1]:
                worker.wait()
        for thread in probe_threads:
            thread.join()
        app.processEvents()
        for label in labels:
            label.close()
        for cache in caches:
            cache.clear()
    final = asdict(DECODE_RESOURCES.snapshot())
    for row in rows:
        stamps = row["delivery_ms"]
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        row.update(_reverse_sequence_summary(row["frame_indexes"], row["requested"]))
        row["frame_gap_latency"] = _latency_summary(gaps)
        if render:
            painted = row["painted_ms"]
            row["paint_gap_latency"] = _latency_summary([b - a for a, b in zip(painted, painted[1:])])
            row["paint_sequence_valid"] = row["painted_indexes"] == row["frame_indexes"]
        row["display_fps"] = round(1000 * (len(stamps) - 1) / (stamps[-1] - stamps[0]), 3) if len(stamps) > 1 else 0
    valid = (not timed_out and not errors and all(stopped)
             and final["captures"] == final["waiting"] == final["cache_bytes"] == 0
             and all(row["sequence_valid"] and row["activity_completed"]
                     and row.get("paint_sequence_valid", True)
                     and row["thumbnail_indexes"] == row["expected_thumbnail_indexes"]
                     and row["duration_ms"] == round(row["count"] * 1000 / row["fps"])
                     for row in rows)
             and all(sample["captures"] <= 6 and sample["background_captures"] <= 2
                     and sample["cache_bytes"] <= 256 * 1024**2 for sample in samples))
    return dict(valid=valid, timed_out=timed_out, errors=errors, streams=rows,
                resources_final=final, resource_samples=samples,
                private_bytes_peak=max((sample["private_bytes"] or 0 for sample in samples), default=0),
                private_bytes_final=read_private_bytes(),
                qt_tick_gap_ms=_latency_summary([b - a for a, b in zip(ticks, ticks[1:])]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("videos", nargs="+", type=Path, help="Run each video separately in single-video mode")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--frames", type=int, default=40)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reverse-prefetch", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--playback-only", action="store_true")
    parser.add_argument("--render", action="store_true", help="Include actual playback-label painting")
    parser.add_argument("--prefetch-threads", type=int, default=0, help="Override secondary decoder threads for A/B measurement")
    args = parser.parse_args()
    if args.rounds < 1 or args.frames < 2:
        parser.error("rounds must be positive; frames must be at least 2")
    root = Path(__file__).resolve().parents[1]
    videos = [path.resolve() for path in args.videos]
    sources = ("realtime/video_playback.py", "realtime/decode_resources.py",
               "realtime/video_activity.py", "realtime/race_filmstrip.py",
               "realtime/video_timeline.py", "realtime/passage_review.py",
               "tools/benchmark_decode_concurrency.py")
    result = dict(environment=dict(platform=platform.platform(), python=platform.python_version(),
        opencv=cv2.__version__, processor=platform.processor(), logical_cpus=os.cpu_count(),
        base_revision=subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        working_tree_dirty=bool(subprocess.check_output(["git", "status", "--porcelain"])),
        sources_sha256={name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in sources},
        media_sha256={str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in videos},
        cache="fresh workers each round; OS file cache not flushed; no live recorder",
        render_label=args.render, qt_platform=os.environ.get("QT_QPA_PLATFORM", "native"),
        reverse_prefetch=args.reverse_prefetch, background=not args.playback_only,
        prefetch_threads=args.prefetch_threads,
        mode="single-video"),
        rounds=[measure([video], frames=args.frames, reverse_prefetch=args.reverse_prefetch,
                        background=not args.playback_only, render=args.render,
                        prefetch_threads=args.prefetch_threads)
                for video in videos for _ in range(args.rounds)])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps([dict(valid=row["valid"], errors=row["errors"],
                          fps=[stream["display_fps"] for stream in row["streams"]],
                          resources_final=row["resources_final"]) for row in result["rounds"]], indent=2))
    return 0 if all(row["valid"] for row in result["rounds"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
