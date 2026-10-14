"""Sample pixels along the finish line in every decoded frame.

Each frame contributes one column: ``samples`` points along the configured
line, each the mean of a 4-pixel band across the line. Stacked over time the
columns form a synthetic finish-line image ("strip"). No model runs here; the
cost is decoding plus one ``cv2.remap`` per frame.

A line may be a straight segment (``x1, y1, x2, y2``) or a polyline
(``points``) for wide-angle lenses. With ``snap`` enabled the line is moved,
per recording file, onto the nearest painted white band within a small
search distance; this follows slow camera drift during long races.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

import cv2
import numpy as np


DEFAULT_SAMPLES = 500
BAND_OFFSETS = (-2.0, -1.0, 0.0, 1.0)
DEFAULT_DECODE_THREADS = 2
SNAP_SEARCH_PX = 80
SNAP_FRAMES = 7
STRIP_SCHEMA_VERSION = 1


class LineSamplingError(RuntimeError):
    """Raised when a recording cannot be opened or sampled."""


@dataclass(frozen=True)
class LineGeometry:
    """Sample points (pixels) along the line and their unit normals."""

    width: int
    height: int
    xs: np.ndarray
    ys: np.ndarray
    nx: np.ndarray
    ny: np.ndarray
    source: str = "configured"
    snap_offset_px: float = 0.0

    @property
    def samples(self) -> int:
        return int(self.xs.size)

    @property
    def length_px(self) -> float:
        return float(np.hypot(np.diff(self.xs), np.diff(self.ys)).sum())

    @classmethod
    def from_points(
        cls,
        points: np.ndarray,
        width: int,
        height: int,
        *,
        samples: int = DEFAULT_SAMPLES,
        source: str = "configured",
    ) -> "LineGeometry":
        points = np.asarray(points, dtype=np.float64)
        if points.ndim != 2 or points.shape[0] < 2 or points.shape[1] != 2:
            raise ValueError("a finish line needs at least two points")
        steps = np.hypot(np.diff(points[:, 0]), np.diff(points[:, 1]))
        if steps.sum() <= 1.0:
            raise ValueError("finish line is too short")
        arc = np.concatenate([[0.0], np.cumsum(steps)])
        positions = np.linspace(0.0, arc[-1], int(samples))
        xs = np.interp(positions, arc, points[:, 0])
        ys = np.interp(positions, arc, points[:, 1])
        dx, dy = np.gradient(xs), np.gradient(ys)
        norm = np.hypot(dx, dy)
        norm[norm == 0] = 1.0
        return cls(int(width), int(height), xs, ys, dy / norm, -dx / norm, source)

    @classmethod
    def from_config(
        cls,
        config: Mapping[str, Any],
        width: int,
        height: int,
        *,
        samples: int = DEFAULT_SAMPLES,
    ) -> "LineGeometry":
        """Build from a ``finish_lines.json`` entry (normalized coordinates)."""

        raw = config.get("points")
        if raw:
            normalized = [(float(x), float(y)) for x, y in raw]
        else:
            normalized = [
                (float(config["x1"]), float(config["y1"])),
                (float(config["x2"]), float(config["y2"])),
            ]
        points = np.asarray([(x * width, y * height) for x, y in normalized], dtype=np.float64)
        return cls.from_points(points, width, height, samples=samples)

    def shifted(self, offsets_px: np.ndarray, *, source: str) -> "LineGeometry":
        offsets = np.asarray(offsets_px, dtype=np.float64)
        return LineGeometry(
            self.width, self.height,
            self.xs + self.nx * offsets, self.ys + self.ny * offsets,
            self.nx, self.ny, source, float(np.mean(np.abs(offsets))),
        )

    def remap_maps(self, band: tuple[float, ...] = BAND_OFFSETS) -> tuple[np.ndarray, np.ndarray]:
        offsets = np.asarray(band, dtype=np.float64)[None, :]
        mapx = (self.xs[:, None] + self.nx[:, None] * offsets).astype(np.float32)
        mapy = (self.ys[:, None] + self.ny[:, None] * offsets).astype(np.float32)
        return mapx, mapy

    def point(self, row: int) -> tuple[float, float]:
        row = int(np.clip(row, 0, self.samples - 1))
        return float(self.xs[row]), float(self.ys[row])


def line_config_digest(config: Mapping[str, Any]) -> str:
    keys = ("x1", "y1", "x2", "y2", "points", "snap")
    payload = json.dumps({key: config.get(key) for key in keys}, sort_keys=True)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def snap_to_paint(
    frames: list[np.ndarray],
    geometry: LineGeometry,
    *,
    search_px: int = SNAP_SEARCH_PX,
    step: int = 10,
    min_support: float = 0.4,
) -> Optional[LineGeometry]:
    """Move the line onto the painted white band near it, if one is found.

    Every ``step``-th sample point is searched along its normal for bright,
    unsaturated pixels in each frame. Points found in at least half of the
    frames give an offset; a robust quadratic along the line smooths them.
    Returns ``None`` when the band is not found reliably.
    """

    if not frames:
        return None
    rows = np.arange(0, geometry.samples, max(1, int(step)))
    offsets = np.arange(-int(search_px), int(search_px) + 1, dtype=np.float64)
    mapx = (geometry.xs[rows][:, None] + geometry.nx[rows][:, None] * offsets[None, :]).astype(np.float32)
    mapy = (geometry.ys[rows][:, None] + geometry.ny[rows][:, None] * offsets[None, :]).astype(np.float32)
    found = np.full((len(frames), rows.size), np.nan)
    for index, frame in enumerate(frames):
        patch = cv2.remap(frame, mapx, mapy, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
        white = (hsv[:, :, 2] > 200) & (hsv[:, :, 1] < 40)
        for position in range(rows.size):
            hits = np.nonzero(white[position])[0]
            if hits.size >= 3:
                found[index, position] = float(np.median(offsets[hits]))
    support = np.sum(~np.isnan(found), axis=0) >= max(1, len(frames) // 2)
    if support.mean() < min_support:
        return None
    s = rows[support] / max(1, geometry.samples - 1)
    y = np.nanmedian(found[:, support], axis=0)
    keep = np.ones(s.size, dtype=bool)
    coefficients = np.polyfit(s, y, 2)
    for _ in range(3):
        residual = np.abs(y - np.polyval(coefficients, s))
        keep = residual <= max(6.0, 2.5 * float(np.median(residual[keep])))
        if keep.sum() < 5:
            return None
        coefficients = np.polyfit(s[keep], y[keep], 2)
    full = np.clip(
        np.polyval(coefficients, np.arange(geometry.samples) / max(1, geometry.samples - 1)),
        -search_px, search_px,
    )
    return geometry.shifted(full, source="snapped")


@dataclass
class LineStrip:
    """One recording file sampled along the finish line."""

    strip: np.ndarray
    pts_ms: np.ndarray
    geometry: LineGeometry
    video_path: str
    fps: float
    decode_seconds: float = 0.0
    cpu_seconds: float = 0.0
    meta: Optional[dict] = None

    @property
    def frame_count(self) -> int:
        return int(self.pts_ms.size)


def _open_capture(path: Path, threads: int):
    capture = cv2.VideoCapture(str(path), cv2.CAP_FFMPEG, [cv2.CAP_PROP_N_THREADS, int(threads)])
    if not capture.isOpened():
        capture.release()
        raise LineSamplingError(f"无法打开录像：{path}")
    return capture


def _snap_frames(path: Path, threads: int, count: int) -> list[np.ndarray]:
    capture = _open_capture(path, threads)
    try:
        total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        frames = []
        for index in range(count):
            if total > 0:
                capture.set(cv2.CAP_PROP_POS_FRAMES, int((index + 0.5) / count * max(1, total - 1)))
            ok, frame = capture.read()
            if ok and frame is not None:
                frames.append(frame)
        return frames
    finally:
        capture.release()


def sample_video_file(
    path: str | Path,
    line_config: Mapping[str, Any],
    *,
    samples: int = DEFAULT_SAMPLES,
    threads: int = DEFAULT_DECODE_THREADS,
    snap: bool = False,
    checkpoint: Optional[Callable[[], None]] = None,
    cancelled: Callable[[], bool] = lambda: False,
) -> LineStrip:
    """Decode every frame once and keep only the finish-line band."""

    path = Path(path)
    capture = _open_capture(path, threads)
    started_wall, started_cpu = time.perf_counter(), time.process_time()
    try:
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
        geometry = LineGeometry.from_config(line_config, width, height, samples=samples)
        if snap:
            capture.release()
            snapped = snap_to_paint(_snap_frames(path, threads, SNAP_FRAMES), geometry)
            geometry = snapped or geometry
            capture = _open_capture(path, threads)
        mapx, mapy = geometry.remap_maps()
        columns: list[np.ndarray] = []
        pts: list[float] = []
        while not cancelled():
            ok, frame = capture.read()
            if not ok or frame is None:
                break
            pts.append(float(capture.get(cv2.CAP_PROP_POS_MSEC)))
            columns.append(cv2.remap(frame, mapx, mapy, cv2.INTER_LINEAR).mean(axis=1).astype(np.uint8))
            if checkpoint is not None:
                checkpoint()
    finally:
        capture.release()
    if not columns:
        raise LineSamplingError(f"录像没有可解码的画面：{path}")
    strip = np.stack(columns, axis=1)
    return LineStrip(
        strip=strip,
        pts_ms=np.asarray(pts, dtype=np.float64),
        geometry=geometry,
        video_path=str(path),
        fps=fps,
        decode_seconds=time.perf_counter() - started_wall,
        cpu_seconds=time.process_time() - started_cpu,
    )


def save_strip(path: str | Path, strip: LineStrip, meta: Mapping[str, Any]) -> None:
    """Write one strip atomically (compressed npz plus JSON metadata)."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(meta)
    payload.update(
        schema_version=STRIP_SCHEMA_VERSION,
        video_path=strip.video_path,
        fps=strip.fps,
        width=strip.geometry.width,
        height=strip.geometry.height,
        line_source=strip.geometry.source,
        snap_offset_px=strip.geometry.snap_offset_px,
        decode_seconds=round(strip.decode_seconds, 2),
        cpu_seconds=round(strip.cpu_seconds, 2),
    )
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as handle:
        np.savez_compressed(
            handle,
            strip=strip.strip,
            pts_ms=strip.pts_ms,
            xs=strip.geometry.xs,
            ys=strip.geometry.ys,
            nx=strip.geometry.nx,
            ny=strip.geometry.ny,
            meta=np.frombuffer(json.dumps(payload, ensure_ascii=False).encode("utf-8"), dtype=np.uint8),
        )
    os.replace(temporary, path)


def load_strip(path: str | Path) -> LineStrip:
    with np.load(Path(path)) as data:
        meta = json.loads(bytes(data["meta"]).decode("utf-8"))
        geometry = LineGeometry(
            int(meta["width"]), int(meta["height"]),
            data["xs"].copy(), data["ys"].copy(), data["nx"].copy(), data["ny"].copy(),
            str(meta.get("line_source", "configured")), float(meta.get("snap_offset_px", 0.0)),
        )
        return LineStrip(
            strip=data["strip"].copy(),
            pts_ms=data["pts_ms"].copy(),
            geometry=geometry,
            video_path=str(meta.get("video_path", "")),
            fps=float(meta.get("fps", 0.0)),
            decode_seconds=float(meta.get("decode_seconds", 0.0)),
            cpu_seconds=float(meta.get("cpu_seconds", 0.0)),
            meta=meta,
        )


def read_strip_meta(path: str | Path) -> Optional[dict]:
    try:
        with np.load(Path(path)) as data:
            return json.loads(bytes(data["meta"]).decode("utf-8"))
    except (OSError, KeyError, ValueError):
        return None


__all__ = [
    "BAND_OFFSETS",
    "DEFAULT_DECODE_THREADS",
    "DEFAULT_SAMPLES",
    "LineGeometry",
    "LineSamplingError",
    "LineStrip",
    "line_config_digest",
    "load_strip",
    "read_strip_meta",
    "sample_video_file",
    "save_strip",
    "snap_to_paint",
]
