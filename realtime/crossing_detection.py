"""Find line crossings in a finish-line strip (rows = points, columns = frames).

Background is a rolling per-row median over about +/-3 s, minus the frame's
global brightness change. Connected regions that differ from it are merged
into one event per rider ("same start" or "still there"). A rider-like event
covers at least ~4 % of the line for at most ~1 s. Longer coverage is an
obstructed spell (people standing on the line); inside it, short vertical
bursts of frame-to-frame change are kept as crossings for chip pairing only.

Parameters are in milliseconds and converted with the strip's real frame
interval, so 20 and 25 fps recordings behave alike.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import cv2
import numpy as np


@dataclass(frozen=True)
class DetectionParams:
    background_window_ms: int = 3_000
    background_step_ms: int = 1_000
    background_stride: int = 2
    threshold: float = 40.0
    motion_threshold: float = 45.0
    min_area: int = 10
    min_rows: int = 6
    same_start_ms: int = 150
    same_start_gap_rows: int = 40
    still_there_gap_rows: int = 10
    rider_min_rows: int = 20
    rider_max_ms: int = 1_000
    rider_min_area: int = 40
    burst_min_rows: int = 20
    burst_max_ms: int = 500
    burst_min_area: int = 40


@dataclass(frozen=True)
class LineCrossing:
    """One detected crossing; columns index the strip's decoded frames."""

    c0: int
    c1: int
    r0: int
    r1: int
    row: int
    area: int
    fragments: int
    rider_like: bool
    in_obstruction: bool = False

    @property
    def rows(self) -> int:
        return self.r1 - self.r0 + 1

    @property
    def columns(self) -> int:
        return self.c1 - self.c0 + 1


@dataclass(frozen=True)
class DetectionResult:
    crossings: tuple[LineCrossing, ...]
    obstructions: tuple[tuple[int, int, int, int], ...]
    frame_ms: float


def frame_interval_ms(pts_ms: np.ndarray, fallback_fps: float = 20.0) -> float:
    pts = np.asarray(pts_ms, dtype=np.float64)
    if pts.size >= 3:
        steps = np.diff(pts)
        steps = steps[steps > 5]
        if steps.size:
            return float(np.median(steps))
    return 1000.0 / max(1.0, fallback_fps)


def _frames(milliseconds: float, frame_ms: float) -> int:
    return max(1, int(round(milliseconds / frame_ms)))


def rolling_background(strip: np.ndarray, window: int, step: int, stride: int) -> np.ndarray:
    """Per-row median over +/-window frames, sampled every ``step`` frames."""

    rows, total, _channels = strip.shape
    centres = np.arange(0, total, step)
    medians = np.stack(
        [
            np.median(strip[:, max(0, c - window):min(total, c + window):stride], axis=1)
            for c in centres
        ],
        axis=1,
    ).astype(np.float32)
    if centres.size == 1:
        return np.repeat(medians, total, axis=1)
    index = np.clip(np.searchsorted(centres, np.arange(total)), 1, centres.size - 1)
    weight = (
        (np.arange(total) - centres[index - 1])
        / np.maximum(1, centres[index] - centres[index - 1])
    ).astype(np.float32)[None, :, None]
    return medians[:, index - 1] * (1.0 - weight) + medians[:, index] * weight


def _components(mask: np.ndarray, min_area: int, min_rows: int) -> list[dict]:
    count, labels, stats, _centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
    components = []
    for label in range(1, count):
        x, y, width, height, area = (int(value) for value in stats[label])
        if area >= min_area and height >= min_rows:
            rows = np.nonzero(labels[:, x] == label)[0]
            components.append(dict(
                c0=x, c1=x + width - 1, r0=y, r1=y + height - 1,
                row=int(np.median(rows)), area=area,
            ))
    components.sort(key=lambda item: item["c0"])
    return components


def _merge_fragments(components: list[dict], params: DetectionParams, same_start: int) -> list[dict]:
    events: list[dict] = []
    for component in components:
        for event in events:
            same = (
                abs(component["c0"] - event["c0"]) <= same_start
                and component["r0"] - event["r1"] <= params.same_start_gap_rows
                and event["r0"] - component["r1"] <= params.same_start_gap_rows
            )
            still = (
                component["c0"] <= event["c1"] + 1
                and component["r0"] - event["r1"] <= params.still_there_gap_rows
                and event["r0"] - component["r1"] <= params.still_there_gap_rows
            )
            if same or still:
                event["r0"] = min(event["r0"], component["r0"])
                event["r1"] = max(event["r1"], component["r1"])
                event["area"] += component["area"]
                event["c1"] = max(event["c1"], component["c1"])
                event["n"] += 1
                if component["c0"] < event["c0"]:
                    event["c0"], event["row"] = component["c0"], component["row"]
                break
        else:
            events.append(dict(component, n=1))
    return events


def detect_crossings(
    strip: np.ndarray,
    pts_ms: np.ndarray,
    params: Optional[DetectionParams] = None,
) -> DetectionResult:
    """Detect crossings and obstructed spells in one strip."""

    params = params or DetectionParams()
    if strip.ndim != 3 or strip.shape[1] < 2:
        return DetectionResult((), (), frame_interval_ms(pts_ms))
    frame_ms = frame_interval_ms(pts_ms)
    window = _frames(params.background_window_ms, frame_ms)
    step = _frames(params.background_step_ms, frame_ms)
    same_start = _frames(params.same_start_ms, frame_ms)
    rider_max = _frames(params.rider_max_ms, frame_ms)
    burst_max = _frames(params.burst_max_ms, frame_ms)

    values = strip.astype(np.float32)
    background = rolling_background(values, window, step, max(1, params.background_stride))
    difference = np.linalg.norm(values - background, axis=2)
    del background
    difference -= np.median(difference, axis=0, keepdims=True)
    kernel = np.ones((3, 1), np.uint8)
    mask = cv2.morphologyEx((difference > params.threshold).astype(np.uint8), cv2.MORPH_OPEN, kernel)
    del difference
    events = _merge_fragments(_components(mask, params.min_area, params.min_rows), params, same_start)
    for event in events:
        event["rider_like"] = (
            event["r1"] - event["r0"] + 1 >= params.rider_min_rows
            and event["c1"] - event["c0"] + 1 <= rider_max
            and event["area"] >= params.rider_min_area
        )

    motion = np.zeros(values.shape[:2], np.float32)
    motion[:, 1:] = np.linalg.norm(values[:, 1:] - values[:, :-1], axis=2)
    motion_mask = cv2.morphologyEx((motion > params.motion_threshold).astype(np.uint8), cv2.MORPH_OPEN, kernel)
    del motion, values
    extras = []
    for event in events:
        if event["rider_like"] or event["c1"] - event["c0"] + 1 <= rider_max:
            continue
        sub = np.ascontiguousarray(motion_mask[event["r0"]:event["r1"] + 1, event["c0"]:event["c1"] + 1])
        count, labels, stats, _centroids = cv2.connectedComponentsWithStats(sub, connectivity=8)
        for label in range(1, count):
            x, y, width, height, area = (int(value) for value in stats[label])
            if height >= params.burst_min_rows and width <= burst_max and area >= params.burst_min_area:
                rows = np.nonzero(labels[:, x] == label)[0] + event["r0"]
                extras.append(dict(
                    c0=event["c0"] + x, c1=event["c0"] + x + width - 1,
                    r0=event["r0"] + y, r1=event["r0"] + y + height - 1,
                    row=int(np.median(rows)), area=area, n=1,
                    rider_like=True, in_obstruction=True,
                ))
    kept: list[dict] = []
    for extra in sorted(extras, key=lambda item: item["c0"]):
        if any(
            abs(extra["c0"] - other["c0"]) <= same_start
            and extra["r0"] <= other["r1"] + params.same_start_gap_rows
            and other["r0"] <= extra["r1"] + params.same_start_gap_rows
            for other in kept
        ):
            continue
        kept.append(extra)
    obstructions = tuple(
        (event["c0"], event["c1"], event["r0"], event["r1"])
        for event in events
        if not event["rider_like"] and event["c1"] - event["c0"] + 1 > rider_max
    )
    crossings = [
        LineCrossing(
            c0=int(item["c0"]), c1=int(item["c1"]), r0=int(item["r0"]), r1=int(item["r1"]),
            row=int(item["row"]), area=int(item["area"]), fragments=int(item["n"]),
            rider_like=bool(item["rider_like"]), in_obstruction=bool(item.get("in_obstruction", False)),
        )
        for item in events + kept
    ]
    crossings.sort(key=lambda item: (item.c0, item.row))
    return DetectionResult(tuple(crossings), obstructions, frame_ms)


__all__ = [
    "DetectionParams",
    "DetectionResult",
    "LineCrossing",
    "detect_crossings",
    "frame_interval_ms",
    "rolling_background",
]
