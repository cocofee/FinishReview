"""Conservative target-trajectory checks; pixel changes alone are not crossings.

The anchor is the bottom centre of a target box, not a measured front wheel.
This gate validates a candidate only; it never supplies an official finish time.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import hypot
from typing import Sequence

import numpy as np

from .bike_check import BICYCLE, PERSON, VEHICLES, Detection


VERIFICATION_OFFSETS_MS = (-600, 0, 600)


@dataclass(frozen=True)
class TargetCrossingCheck:
    state: str
    reason: str
    label: str = ""
    score: float = 0.0
    vehicle: str = ""
    direction: int = 0
    observed_ms: tuple[float, ...] = ()


def _iou(a: Detection, b: Detection) -> float:
    width = max(0.0, min(a.x + a.w, b.x + b.w) - max(a.x, b.x))
    height = max(0.0, min(a.y + a.h, b.y + b.h) - max(a.y, b.y))
    area = width * height
    return area / max(1.0, a.w * a.h + b.w * b.h - area)


def _anchor(box: Detection) -> tuple[float, float]:
    return box.x + box.w / 2, box.y + box.h


def _side(point, a, b) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    return (dx * (point[1] - a[1]) - dy * (point[0] - a[0])) / max(1.0, hypot(dx, dy))


def _crosses(p, q, a, b, margin: float) -> int:
    first, last = _side(p, a, b), _side(q, a, b)
    if not ((first > margin and last < -margin) or (first < -margin and last > margin)):
        return 0
    weight = first / (first - last)
    x, y = p[0] + weight * (q[0] - p[0]), p[1] + weight * (q[1] - p[1])
    dx, dy = b[0] - a[0], b[1] - a[1]
    length2 = dx * dx + dy * dy
    if length2 <= 1:
        return 0
    projection = ((x - a[0]) * dx + (y - a[1]) * dy) / length2
    return (1 if last > first else -1) if 0 <= projection <= 1 else 0


def check_target_tracks(
    observations: Sequence[tuple[float, Sequence[Detection]]],
    line_points: Sequence[tuple[float, float]],
    *,
    frame_size: tuple[int, int],
    margin_px: float = 4.0,
) -> TargetCrossingCheck:
    """Associate same-class boxes and require the same anchor on both sides.

    Ambiguous associations, clipped boxes and missing temporal context fail
    closed. A track touching the line without a side change is not confirmed.
    """
    points = np.asarray(line_points, dtype=float)
    observed = tuple(float(stamp) for stamp, _ in observations)
    if len(observations) < 3 or len(points) < 2:
        return TargetCrossingCheck("unverified", "missing_context", observed_ms=observed)
    if any(right <= left or right - left > 850 for left, right in zip(observed, observed[1:])):
        return TargetCrossingCheck("unverified", "discontinuous_context", observed_ms=observed)
    width, height = frame_size
    tracks: list[list[tuple[float, Detection]]] = []
    ambiguous = False
    for stamp, boxes in observations:
        boxes = [box for box in boxes if box.score >= 0.35 and box.w > 0 and box.h > 0]
        pairs = []
        for ti, track in enumerate(tracks):
            before_ms, before = track[-1]
            if stamp - before_ms > 850:
                continue
            matches = []
            for bi, box in enumerate(boxes):
                overlap = _iou(before, box) if before.cls == box.cls else 0.0
                if overlap >= 0.15:
                    matches.append(overlap)
                    pairs.append((overlap, ti, bi))
            if len(matches) > 1 and sorted(matches)[-1] - sorted(matches)[-2] < 0.08:
                ambiguous = True
        used_tracks, used_boxes = set(), set()
        for _overlap, ti, bi in sorted(pairs, reverse=True):
            if ti not in used_tracks and bi not in used_boxes:
                tracks[ti].append((stamp, boxes[bi]))
                used_tracks.add(ti)
                used_boxes.add(bi)
        for bi, box in enumerate(boxes):
            if bi not in used_boxes:
                tracks.append([(stamp, box)])
    if ambiguous:
        return TargetCrossingCheck("unverified", "ambiguous_tracks", observed_ms=observed)

    confirmed = []
    clipped = False
    for track in tracks:
        if len(track) < 2:
            continue
        if any(box.x <= 2 or box.y <= 2 or box.x + box.w >= width - 3
               or box.y + box.h >= height - 3 for _, box in track):
            clipped = True
            continue
        # Compare all observed endpoints so touching the hysteresis band in an
        # intermediate frame does not erase a genuine side change.
        for i, (_, before) in enumerate(track):
            for _, after in track[i + 1:]:
                direction = next((value for a, b in zip(points, points[1:])
                                  if (value := _crosses(_anchor(before), _anchor(after), a, b, margin_px))), 0)
                if direction:
                    cls = before.cls
                    label = "bicycle" if cls == BICYCLE else "pedestrian" if cls == PERSON else "vehicle"
                    confirmed.append((label, min(before.score, after.score), VEHICLES.get(cls, ""), direction))
                    break
            else:
                continue
            break
    if confirmed:
        # A bicycle and its rider may both be tracked; they are not two events.
        label, score, vehicle, direction = max(confirmed, key=lambda item: (item[0] != "pedestrian", item[1]))
        return TargetCrossingCheck("confirmed", "anchor_crossed_line", label, score, vehicle, direction, observed)
    if clipped:
        reason = "clipped_target"
    elif any(len(track) == len(observations) for track in tracks):
        return TargetCrossingCheck("not_crossed", "no_side_change", observed_ms=observed)
    else:
        reason = "insufficient_track"
    return TargetCrossingCheck("unverified", reason, observed_ms=observed)
