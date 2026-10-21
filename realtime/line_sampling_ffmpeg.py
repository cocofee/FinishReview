"""Decode a native-resolution line ROI without converting the whole frame to BGR."""

from __future__ import annotations

import re
import subprocess
import threading
import time
from pathlib import Path

import cv2
import numpy as np


_TIMEBASE = re.compile(r"config in time_base:\s*(\d+)/(\d+)")
_FRAME = re.compile(r"\bn:\s*(\d+)\s+pts:\s*(-?\d+)\s+pts_time:")


def sample_roi(path, geometry, executable, *, threads, checkpoint, cancelled, expected_frames=0, first_pts=0.0):
    """Keep every frame and its decoder PTS; raise rather than accept partial output."""
    mapx, mapy = geometry.remap_maps()
    # Even crop coordinates preserve chroma alignment of common 4:2:0 sources.
    x0 = max(0, int(np.floor(float(mapx.min()) - 4)) // 2 * 2)
    y0 = max(0, int(np.floor(float(mapy.min()) - 4)) // 2 * 2)
    right = min(geometry.width, (int(np.ceil(float(mapx.max()) + 5)) + 1) // 2 * 2)
    bottom = min(geometry.height, (int(np.ceil(float(mapy.max()) + 5)) + 1) // 2 * 2)
    width, height = right - x0, bottom - y0
    if width <= 0 or height <= 0 or width * height > geometry.width * geometry.height // 2:
        raise ValueError("line ROI is too large for the streaming fast path")
    command = [str(executable), "-hide_banner", "-nostdin", "-loglevel", "info",
               "-threads", str(max(1, int(threads))), "-i", str(Path(path)),
               "-map", "0:v:0", "-an", "-filter_threads", "1", "-vf", f"crop={width}:{height}:{x0}:{y0},format=bgr24,showinfo",
               "-fps_mode", "passthrough", "-threads", "1", "-pix_fmt", "bgr24", "-f", "rawvideo", "pipe:1"]
    started = time.perf_counter()
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                              | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)))
    timestamps, errors = [], []
    state = {"timebase": None, "error": None}

    def read_timing():
        try:
            for raw in process.stderr:
                line = raw.decode("utf-8", errors="replace")
                match = _TIMEBASE.search(line)
                if match:
                    state["timebase"] = 1000.0 * int(match[1]) / int(match[2])
                match = _FRAME.search(line)
                if match:
                    if state["timebase"] is None or int(match[1]) != len(timestamps):
                        state["error"] = "missing or discontinuous frame timing"
                    else:
                        timestamps.append(int(match[2]) * state["timebase"])
                if len(errors) >= 8:
                    errors.pop(0)
                errors.append(line.strip())
        except Exception as error:
            state["error"] = str(error)

    timing_thread = threading.Thread(target=read_timing, daemon=True)
    timing_thread.start()
    columns = []
    read_seconds, sample_seconds, checkpoint_seconds = 0.0, 0.0, 0.0
    frame_bytes = width * height * 3
    mapx, mapy = mapx - x0, mapy - y0
    try:
        while not cancelled():
            before = time.perf_counter()
            data = process.stdout.read(frame_bytes)
            read_seconds += time.perf_counter() - before
            if not data:
                break
            if len(data) != frame_bytes:
                raise ValueError("incomplete decoded ROI frame")
            before = time.perf_counter()
            frame = np.frombuffer(data, np.uint8).reshape(height, width, 3)
            columns.append(cv2.remap(frame, mapx, mapy, cv2.INTER_LINEAR).mean(axis=1).astype(np.uint8))
            sample_seconds += time.perf_counter() - before
            if checkpoint is not None:
                before = time.perf_counter()
                checkpoint()
                checkpoint_seconds += time.perf_counter() - before
        if cancelled():
            raise InterruptedError("line sampling cancelled")
        code = process.wait(timeout=10)
        timing_thread.join(timeout=10)
        if code or timing_thread.is_alive() or state["error"]:
            raise ValueError(state["error"] or "FFmpeg ROI decoder failed: " + " ".join(errors)[-500:])
        if not columns or len(columns) != len(timestamps) or (expected_frames > 0 and len(columns) != expected_frames):
            raise ValueError("ROI frame count or original timestamps do not agree")
        pts = np.asarray(timestamps, dtype=np.float64)
        if np.any(np.diff(pts) <= 0):
            raise ValueError("ROI timestamps are not strictly increasing")
        pts = pts - pts[0] + first_pts
        return np.stack(columns, axis=1), pts, {
            "read": round(read_seconds, 4), "sample": round(sample_seconds, 4),
            "checkpoint": round(checkpoint_seconds, 4), "ffmpeg_total": round(time.perf_counter() - started, 4),
        }
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
        timing_thread.join(timeout=10)
        process.stdout.close()
        process.stderr.close()
