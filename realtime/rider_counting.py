"""Riders counted before the finish line, where they are still apart (cycling only).

On a low camera beside the finish, riders at the line are cut by the picture's
edge and hide one another; a little before the line they are still apart.  The
operator draws a count line there.  A rider counts once, when their feet cross
it towards the finish line.  The moment handed on is when they cross the finish
line itself: seen on their own track, or estimated from the riders of this race
seen crossing both lines.

The detector sees people, never who they are; a count is neither an identity
nor a confirmation.  Running keeps its own path in ``person_crossing``.

In an experiment on the Tongxin copy (2026-09-05, 161 chips in 88 waves, the
experiment's own scripts, not this module end to end), waves whose rider count
equals their chip count went from 57 when counted at the finish line to 78
when counted before it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from bisect import bisect_left
from dataclasses import dataclass, field
from math import hypot
from pathlib import Path
from typing import Callable, Optional, Sequence

import cv2
import numpy as np

from .bike_check import PERSON, YoloxDetector
from .person_crossing import (
    MOTION_HOLD_MS, MOTION_LEVEL, MOTION_SHARE, PATH_REACH_MS, PeopleAtLine, PersonCrossing, _free_record, _iou,
    _line_record, _nearest_frame, _projection, _roi_frames, _side, _strip_line,
)


logger = logging.getLogger("FinishReview.RiderCounting")

RIDER_VERSION = 3
RIDER_INPUT = 416
# The whole picture is decoded this large (half of a 2560 px camera) and looked
# at whole; the far part of the road at the count line gets a second, closer
# look from it.
DECODE_SIDE = 1280
# That closer look, in count-line lengths: from just above the line's far
# (upper) end to three quarters down it, and from a little short of the finish
# to well up the road.  It is the crop of the Tongxin experiment; the earlier
# crop, around the whole line and taller than wide, found 40 of 59 riders in the
# 16 waves it under-counted, this one 52 (the two looks together; either alone
# found no more than the earlier crop).
ROAD_FROM, ROAD_TO, ROAD_UP, ROAD_DOWN = -0.07, 0.73, 0.7, 0.56
LOOK_STEP_MS = 80           # every second frame at 25 fps while a rider comes up to the count line
# Spectators beside the road move all the time, so movement alone cannot say a
# rider is coming: while anything moves the road is glanced at this often, and
# looked at every LOOK_STEP_MS once someone on it moves towards the finish.
WATCH_STEP_MS = 400
QUIET_STEP_MS = 200         # how often a still picture is noted as seen empty
RIDER_HOLD_MS = 1000
# "Moves towards the finish": feet this many body heights nearer the finish
# line than a look 250-700 ms before, by a box of about the same size.
WAKE_GAIN = 0.3
WAKE_BACK_MS = (250, 700)
WAKE_SIZE = (0.6, 1.67)
WAKE_REACH = 3.0
# A box this sure starts a track; a weaker one only continues a track that
# lost its rider for a moment (two-stage association, as in ByteTrack).
HIGH_SCORE = 0.35
SAME_BOX_IOU = 0.5          # the two looks boxing one rider
# The road around the count line, in count-line lengths: up the road, down it
# towards the finish, above the line (bodies) and below it.
UP_ROAD, DOWN_ROAD, ABOVE_COUNT, BELOW_COUNT = 0.6, 0.25, 0.35, 0.1
TRACK_GAP_MS = 400
MIN_POINTS = 3
LINK_DISTANCE = 0.35        # feet this many body heights from where velocity puts them
SIZE_RATIO = 0.5
COUNT_PROJ_LOW, COUNT_PROJ_HIGH = -0.3, 1.3
EDGE_SHARE = 0.03
EDGE_PROJ_LOW, EDGE_PROJ_HIGH = -0.8, 1.8
# A rider lost before the finish is timed with the count-to-finish time of the
# last riders seen crossing both lines before them.  Only earlier riders: a
# rider's time, and so the review item made from it, never changes when later
# riders arrive.  (Tongxin: median error 146 ms, the same as a whole-race
# median; the rider's own speed carried to the line was 336 ms.)
DELAY_WINDOW = 30
# With fewer earlier riders one odd passer-by decides the time: at Tongxin a
# person walking slowly past both lines before the race took 4.5 s, and the
# first bunch was timed 2.5-4.5 s late.  Until this many riders have been seen,
# a rider's own speed carries them to the finish line.
DELAY_MIN = 3
# More riders crossing the count line away from the finish than towards it:
# the line was drawn after the finish line, and nobody would be counted.
WRONG_WAY_MIN = 5
ON_LINE_PX = 0.5


def count_line_pixels(points, width: int, height: int) -> tuple[tuple[float, float], tuple[float, float]]:
    """The saved count line ([[x, y], [x, y]], 0..1 of the picture) in pixels."""

    (ax, ay), (bx, by) = points[0], points[-1]
    return (float(ax) * width, float(ay) * height), (float(bx) * width, float(by) * height)


def _apart_from(count, finish) -> bool:
    """Both ends of the count line strictly on one side of the finish line."""

    sides = [_side(point, finish[0], finish[-1]) for point in count]
    return sides[0] * sides[1] > 0


def valid_count_line(points, finish_points) -> str:
    """Why a drawn count line cannot be used; empty when it can.

    Both lines are in 0..1 picture coordinates.  The count line must lie wholly
    on one side of the finish line, so "towards the finish" is unambiguous.
    """

    if not points or len(points) != 2:
        return "计数线需要两个点"
    if any(not 0 <= float(v) <= 1 for p in points for v in p) or list(points[0]) == list(points[1]):
        return "计数线的两个点要在画面内且不能重合"
    if not finish_points or len(finish_points) < 2:
        return "请先画终点线"
    if not _apart_from(points, finish_points):
        return "计数线要整条画在终点线前面，不能和终点线相交"
    return ""


def count_line_problem(files: Sequence, count_points) -> str:
    """Why the saved count line does not fit a recording's finish line; empty when it fits them all.

    The count line is saved per camera, the finish line per stretch of
    recording; a finish line redrawn later (camera moved) can leave the count
    line across it.
    """

    for number, item in enumerate(files, start=1):
        geometry = item.geometry
        count = count_line_pixels(count_points, int(geometry.width), int(geometry.height))
        if hypot(count[1][0] - count[0][0], count[1][1] - count[0][1]) < 1.0:
            return "计数线的两个点重合"
        if not _apart_from(count, _strip_line(geometry)):
            return f"计数线和第 {number} 段录像（{Path(item.path).stem}）的终点线相交，请在「终点线 / 模型」里重画计数线"
    return ""


def _even_box(x0, y0, x1, y1, width, height) -> tuple[int, int, int, int]:
    x0 = max(0, int(x0)) // 2 * 2
    y0 = max(0, int(y0)) // 2 * 2
    x1 = min(width, (int(x1) + 1) // 2 * 2)
    y1 = min(height, (int(y1) + 1) // 2 * 2)
    return x0, y0, x1, y1


def _up_road(finish, count) -> tuple[float, float, float]:
    """Unit normal of the count line pointing up the road, away from the finish, and the line's length."""

    length = hypot(count[1][0] - count[0][0], count[1][1] - count[0][1])
    nx, ny = -(count[1][1] - count[0][1]) / max(length, 1.0), (count[1][0] - count[0][0]) / max(length, 1.0)
    finish_mid = ((finish[0][0] + finish[1][0]) / 2, (finish[0][1] + finish[1][1]) / 2)
    if (finish_mid[0] - count[0][0]) * nx + (finish_mid[1] - count[0][1]) * ny > 0:
        nx, ny = -nx, -ny
    return nx, ny, length


def _road_crop(finish, count, width: int, height: int) -> tuple[int, int, int, int]:
    """The far part of the road at the count line, for the closer look."""

    nx, ny, length = _up_road(finish, count)
    far, near = (count[0], count[1]) if count[0][1] <= count[1][1] else (count[1], count[0])
    ux, uy = (near[0] - far[0]) / max(length, 1.0), (near[1] - far[1]) / max(length, 1.0)
    corners = []
    for along in (ROAD_FROM, ROAD_TO):
        x, y = far[0] + ux * along * length, far[1] + uy * along * length
        for across in (ROAD_UP, -ROAD_DOWN):
            corners.append((x + nx * across * length, y + ny * across * length))
    return _even_box(min(p[0] for p in corners), min(p[1] for p in corners),
                     max(p[0] for p in corners), max(p[1] for p in corners), width, height)


def rider_areas(finish, count, width: int, height: int):
    """The picture looked at, the road watched for movement, and the road looked at closer.

    The whole picture is looked at, so riders are seen at both lines.  The road
    around the count line, watched for movement, reaches well up the road, a
    little past the line, and a body above it.  The closer look is the far part
    of that road, where riders are small.
    """

    nx, ny, length = _up_road(finish, count)
    corners = []
    for x, y in count:
        corners += [(x + nx * UP_ROAD * length, y + ny * UP_ROAD * length),
                    (x - nx * DOWN_ROAD * length, y - ny * DOWN_ROAD * length),
                    (x, y - ABOVE_COUNT * length), (x, y + BELOW_COUNT * length)]
    band = _even_box(min(p[0] for p in corners), min(p[1] for p in corners),
                     max(p[0] for p in corners), max(p[1] for p in corners), width, height)
    return _even_box(0, 0, width, height, width, height), band, _road_crop(finish, count, width, height)


def _decode_size(roi) -> tuple[int, int]:
    width, height = roi[2] - roi[0], roi[3] - roi[1]
    scale = min(1.0, DECODE_SIDE / max(width, height, 1))
    return max(2, int(width * scale) // 2 * 2), max(2, int(height * scale) // 2 * 2)


def rider_model_key(path: Path) -> str:
    stat = Path(path).stat()
    return hashlib.sha1(f"{Path(path).name}|{stat.st_size}|{RIDER_INPUT}|rider".encode()).hexdigest()[:12]


def rider_path(output_dir: Path, strip_path: Path) -> Path:
    return Path(output_dir) / "riders" / Path(strip_path).parent.name / (Path(strip_path).stem + ".json")


def _merge_looks(boxes):
    """One box per rider from the two looks: the surer of two that overlap."""

    kept = []
    for box in sorted(boxes, key=lambda b: -b[0]):
        if all(_iou(box[1:], other[1:]) < SAME_BOX_IOU for other in kept):
            kept.append(box)
    return kept


@dataclass
class _RiderGate:
    """Motion, nearness and the last glances, carried from one recording file to the next."""

    background: Optional[np.ndarray] = None
    last_motion_ms: float = -1e18
    near_until_ms: float = -1e18
    last_look_ms: float = -1e18
    # (recorder ms, [[x, y, w, h], ...]) of the riders on the road at recent looks
    recent: list = field(default_factory=list)


# The live console starts a new analysis process every few seconds.  A gate
# left behind by the last one is taken up again when the next file follows
# within this long; without it each process needed 250-700 ms of looks before
# it could tell a rider was coming, and riders at the count line just then
# were not counted (Tongxin, busiest five minutes as 2.5 s pieces: 34 riders
# counted with a process per round, 36 in one process).
GATE_CARRY_MS = 1000


def _gate_path(output_dir: Path, strip_path: Path, session: str) -> Path:
    return Path(output_dir) / "riders" / Path(strip_path).parent.name / f"{session or 'session'}.gate.npz"


def _save_gate(path: Path, gate: _RiderGate, until_ms: float, meta: dict) -> None:
    """Leave the gate for the next process; a failure only costs that process its warm start."""

    if gate.background is None:
        return
    state = {"version": RIDER_VERSION, "model": meta["model"], "roi": meta["roi"], "band": meta["band"],
             "count_line": meta["count_line"], "until_ms": float(until_ms),
             "last_motion_ms": gate.last_motion_ms, "near_until_ms": gate.near_until_ms,
             "last_look_ms": gate.last_look_ms,
             "recent": [[stamp, [list(map(float, box)) for box in riders]] for stamp, riders in gate.recent]}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        with open(temporary, "wb") as handle:
            np.savez(handle, background=gate.background, state=np.array(json.dumps(state)))
        temporary.replace(path)
    except OSError as error:
        logger.warning("rider gate not saved: %s", error)


def _load_gate(path: Path, start_ms: float, meta: dict) -> Optional[_RiderGate]:
    """The gate another process left, if it ended where this file begins and looked at the same road."""

    try:
        with np.load(path, allow_pickle=False) as saved:
            background = np.asarray(saved["background"], np.float32)
            state = json.loads(str(saved["state"]))
        same = all(state[key] == meta[key] for key in ("model", "roi", "band", "count_line"))
        if state["version"] != RIDER_VERSION or not same or not 0 <= start_ms - state["until_ms"] <= GATE_CARRY_MS:
            return None
        return _RiderGate(background=background, last_motion_ms=float(state["last_motion_ms"]),
                          near_until_ms=float(state["near_until_ms"]), last_look_ms=float(state["last_look_ms"]),
                          recent=[(float(stamp), riders) for stamp, riders in state["recent"]])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _wake_area(finish, count, width: int, height: int):
    """The road a rider wakes the detector on: along the count line as far as it counts, up and down the road."""

    nx, ny, length = _up_road(finish, count)
    low, high = _count_range(count, width, height)
    return count[0], count[1], (nx, ny), length, low, high


def _on_road(foot, wake) -> bool:
    a, b, (nx, ny), length, low, high = wake
    along = (foot[0] - a[0]) * nx + (foot[1] - a[1]) * ny
    return low <= _projection(foot, a, b) <= high and -DOWN_ROAD * length <= along <= UP_ROAD * length


def _coming(riders, earlier, up) -> bool:
    """Someone on the road nearer the finish than a box of about their size at an earlier look."""

    for box in riders:
        (fx, fy), height = _foot(box), box[3]
        for other in earlier:
            if not WAKE_SIZE[0] < other[3] / max(height, 1.0) < WAKE_SIZE[1]:
                continue
            ox, oy = _foot(other)
            dx, dy = fx - ox, fy - oy
            if hypot(dx, dy) < WAKE_REACH * height and -(dx * up[0] + dy * up[1]) >= WAKE_GAIN * height:
                return True
    return False


def detect_file_riders(video_path: Path, start_ms: int, pts_ms: np.ndarray, roi, band, road, wake,
                       detector: YoloxDetector, *,
                       executable=None, gate: Optional[_RiderGate] = None,
                       checkpoint: Optional[Callable[[], None]] = None,
                       cancelled: Callable[[], bool] = lambda: False) -> dict:
    """Rider boxes ``[score, x, y, w, h]`` (full-frame pixels) per looked-at frame of one file.

    Each look sees the whole picture (``roi``) and, closer, the far part of the
    road (``road``).  Every frame is decoded for the motion check on the road
    at the count line (``band``).
    While something moves there the detector glances every WATCH_STEP_MS; once
    a person on the road moves towards the finish it looks every LOOK_STEP_MS
    until nobody has done so for RIDER_HOLD_MS.  ``None`` marks a frame seen
    still, not unseen.
    """

    gate = gate or _RiderGate()
    x0, y0, x1, y1 = roi
    size = _decode_size(roi)
    sx, sy = (x1 - x0) / size[0], (y1 - y0) / size[1]
    bx0, by0 = int((band[0] - x0) / sx), int((band[1] - y0) / sy)
    bx1, by1 = max(bx0 + 8, int((band[2] - x0) / sx)), max(by0 + 8, int((band[3] - y0) / sy))
    rx0, ry0 = int((road[0] - x0) / sx), int((road[1] - y0) / sy)
    rx1, ry1 = max(rx0 + 8, int((road[2] - x0) / sx)), max(ry0 + 8, int((road[3] - y0) / sy))
    up = wake[2]
    frames, looked, watched, quiet, decoded = [], 0, 0, 0, 0
    started = time.perf_counter()
    detect_seconds = 0.0
    for index, image in enumerate(_roi_frames(video_path, roi, size, executable, cancelled=cancelled)):
        decoded += 1
        if index >= len(pts_ms):
            raise ValueError("more decoded frames than the strip has")
        now = start_ms + float(pts_ms[index])
        moving_area = image[by0:by1, bx0:bx1]
        shrunk = (max(8, moving_area.shape[1] // 4), max(8, moving_area.shape[0] // 4))
        small = cv2.cvtColor(cv2.resize(moving_area, shrunk, interpolation=cv2.INTER_AREA),
                             cv2.COLOR_BGR2GRAY).astype(np.float32)
        if gate.background is None or gate.background.shape != small.shape:
            gate.background = small.copy()
        if float((np.abs(small - gate.background) > MOTION_LEVEL).mean()) > MOTION_SHARE:
            gate.last_motion_ms = now
        cv2.accumulateWeighted(small, gate.background, 0.05)
        near = now <= gate.near_until_ms
        moving = now - gate.last_motion_ms <= MOTION_HOLD_MS
        step = LOOK_STEP_MS if near else WATCH_STEP_MS if moving else QUIET_STEP_MS
        if now - gate.last_look_ms < step - 25:
            continue
        gate.last_look_ms = now
        if not near and not moving:
            frames.append([index, None])
            quiet += 1
            continue
        looked += near
        watched += not near
        before = time.perf_counter()
        boxes = [[box.score, x0 + box.x * sx, y0 + box.y * sy, box.w * sx, box.h * sy]
                 for box in detector.detect(image, classes=(PERSON,))]
        crop = np.ascontiguousarray(image[ry0:ry1, rx0:rx1])
        boxes += [[box.score, x0 + (rx0 + box.x) * sx, y0 + (ry0 + box.y) * sy, box.w * sx, box.h * sy]
                  for box in detector.detect(crop, classes=(PERSON,))]
        boxes = [[round(float(b[0]), 3), *(round(float(v), 1) for v in b[1:])] for b in _merge_looks(boxes)]
        detect_seconds += time.perf_counter() - before
        riders = [b[1:] for b in boxes if b[0] >= HIGH_SCORE and _on_road(_foot(b[1:]), wake)]
        earlier = [entry for entry in gate.recent if WAKE_BACK_MS[0] <= now - entry[0] <= WAKE_BACK_MS[1]]
        if earlier and _coming(riders, earlier[-1][1], up):
            gate.near_until_ms = now + RIDER_HOLD_MS
        gate.recent = [entry for entry in gate.recent if now - entry[0] <= WAKE_BACK_MS[1]] + [(now, riders)]
        frames.append([index, boxes])
        if checkpoint is not None:
            checkpoint()
    if not cancelled() and decoded != len(pts_ms):
        raise ValueError(f"decoded {decoded} frames, the strip has {len(pts_ms)}")
    return {"frames": frames, "stats": {"decoded": decoded, "looked": looked, "watched": watched, "quiet": quiet,
                                        "seconds": round(time.perf_counter() - started, 3),
                                        "detect_seconds": round(detect_seconds, 3)}}


def _file_setup(item, count_points):
    geometry = item.geometry
    width, height = int(geometry.width), int(geometry.height)
    finish = _strip_line(geometry)
    count = count_line_pixels(count_points, width, height)
    roi, band, road = rider_areas(finish, count, width, height)
    return finish, count, roi, band, road


def _cache_meta(strip_meta: dict, key: str, roi, band, road, count) -> dict:
    return {"version": RIDER_VERSION, "model": key, "roi": list(roi), "band": list(band), "road": list(road),
            "count_line": [[round(v, 1) for v in point] for point in count],
            "segment_id": strip_meta.get("segment_id", ""), "video_size": strip_meta.get("video_size"),
            "line_digest": strip_meta.get("line_digest", ""), "geometry_digest": strip_meta.get("geometry_digest", "")}


def extract_riders(output_dir: Path, files: Sequence, model: Path, count_points, *, executable=None,
                   gates: Optional[dict] = None, progress: Callable[[str, int, int], None] = lambda *_: None,
                   checkpoint: Optional[Callable[[], None]] = None,
                   cancelled: Callable[[], bool] = lambda: False) -> int:
    """Look for riders in every strip file whose rider record is missing or stale."""

    key = rider_model_key(model)
    detector = None
    gates = {} if gates is None else gates
    done = 0
    for index, item in enumerate(files):
        if cancelled():
            break
        target = rider_path(output_dir, item.path)
        finish, count, roi, band, road = _file_setup(item, count_points)
        meta = _cache_meta(item.meta, key, roi, band, road, count)
        if target.is_file():
            try:
                if json.loads(target.read_text(encoding="utf-8")).get("meta") == meta:
                    continue
            except (OSError, ValueError):
                pass
        if detector is None:
            detector = YoloxDetector(model, RIDER_INPUT)
        progress("数车手", index, len(files))
        session = str(item.meta.get("session_id", ""))
        gate = gates.get(("rider", session))
        saved_gate = _gate_path(output_dir, item.path, session)
        if not isinstance(gate, _RiderGate):
            gate = gates[("rider", session)] = _load_gate(saved_gate, float(item.start_ms), meta) or _RiderGate()
        wake = _wake_area(finish, count, int(item.geometry.width), int(item.geometry.height))
        try:
            payload = detect_file_riders(
                Path(item.meta.get("video_path", "")), int(item.start_ms), np.asarray(item.pts, float), roi, band,
                road, wake, detector, executable=executable, gate=gate, checkpoint=checkpoint, cancelled=cancelled,
            )
        except (OSError, ValueError) as error:
            logger.warning("rider detection skipped for %s: %s", item.path.name, error)
            continue
        if cancelled():
            break
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(dict(payload, meta=meta), separators=(",", ":")), encoding="utf-8")
        temporary.replace(target)
        _save_gate(saved_gate, gate, float(item.end_ms), meta)
        logger.info("riders %s: %s", item.path.name, payload["stats"])
        done += 1
    progress("数车手", len(files), len(files))
    return done


def _foot(box) -> tuple[float, float]:
    return box[0] + box[2] / 2, box[1] + box[3]


def _predict(track, stamp):
    last_ms, last = track[-1][0], track[-1][3]
    if len(track) < 2:
        return last
    prior_ms, prior = track[-2][0], track[-2][3]
    factor = (stamp - last_ms) / max(1.0, last_ms - prior_ms)
    return [last[0] + (last[0] - prior[0]) * factor, last[1] + (last[1] - prior[1]) * factor, last[2], last[3]]


def _associate(live, boxes, candidates, stamp, min_iou, used_tracks, used_boxes, entry):
    pairs = []
    for ti, track in enumerate(live):
        if ti in used_tracks:
            continue
        guess = _predict(track, stamp)
        gx, gy = _foot(guess)
        for bi in candidates:
            b = boxes[bi][1:]
            overlap = _iou(guess, b)
            fx, fy = _foot(b)
            distance = hypot(gx - fx, gy - fy) / max(guess[3], b[3], 1.0)
            if (overlap >= min_iou or distance < LINK_DISTANCE) and min(guess[3], b[3]) / max(guess[3], b[3], 1.0) > SIZE_RATIO:
                pairs.append((distance - overlap, ti, bi))
    for _cost, ti, bi in sorted(pairs):
        if ti in used_tracks or bi in used_boxes:
            continue
        used_tracks.add(ti)
        used_boxes.add(bi)
        live[ti].append(entry(bi))


def rider_tracks(observations):
    """Tracks of feet; a weak box only continues a track, never starts one.

    Entries are ``(recorder_ms, session, (file, frame), [x, y, w, h], False, score)``
    like the running tracks, so the picture can carry numbers along either.
    """

    live, done = [], []
    for stamp, session, where, boxes in observations:
        boxes = boxes or []
        still = []
        for track in live:
            (still if stamp - track[-1][0] <= TRACK_GAP_MS and track[-1][1] == session else done).append(track)
        live = still

        def entry(bi):
            return (stamp, session, where, boxes[bi][1:], False, boxes[bi][0])

        strong = [i for i, box in enumerate(boxes) if box[0] >= HIGH_SCORE]
        weak = [i for i, box in enumerate(boxes) if box[0] < HIGH_SCORE]
        used_tracks, used_boxes = set(), set()
        _associate(live, boxes, strong, stamp, 0.2, used_tracks, used_boxes, entry)
        _associate(live, boxes, weak, stamp, 0.3, used_tracks, used_boxes, entry)
        live += [[entry(i)] for i in strong if i not in used_boxes]
    return [track for track in done + live if len(track) >= MIN_POINTS]


def _crossing_of(track, a, b, forward, start=0):
    """First crossing of the feet over a-b towards ``forward``, from ``start`` on.

    Returns (index after, index before, weight between them, crossing point).
    """

    anchors = [_foot(entry[3]) for entry in track]
    sides = [_side(point, a, b) * forward for point in anchors]
    previous = None
    for index in range(max(0, start - 1), len(track)):
        if abs(sides[index]) < ON_LINE_PX:
            continue  # feet on the line: wait for the side they leave it to
        if previous is not None and sides[previous] < 0 < sides[index]:
            weight = sides[previous] / (sides[previous] - sides[index])
            pa, pb = anchors[previous], anchors[index]
            point = (pa[0] + weight * (pb[0] - pa[0]), pa[1] + weight * (pb[1] - pa[1]))
            return index, previous, weight, point
        previous = index
    return None


def _count_range(count, width, height):
    margin = EDGE_SHARE * min(width, height)

    def at_edge(point):
        return point[0] <= margin or point[1] <= margin or point[0] >= width - margin or point[1] >= height - margin

    return (EDGE_PROJ_LOW if at_edge(count[0]) else COUNT_PROJ_LOW), (EDGE_PROJ_HIGH if at_edge(count[1]) else COUNT_PROJ_HIGH)


def _moment(track, found) -> float:
    index, previous, weight, _point = found
    return track[previous][0] + weight * (track[index][0] - track[previous][0])


def _ray_to_line(point, velocity, a, b):
    """Where the feet moving at ``velocity`` (px/ms) from ``point`` meet the line a-b, and after how long."""

    dx, dy = b[0] - a[0], b[1] - a[1]
    denominator = velocity[0] * dy - velocity[1] * dx
    if abs(denominator) < 1e-9:
        return None
    t = ((a[0] - point[0]) * dy - (a[1] - point[1]) * dx) / denominator
    if t <= 0:
        return None
    return (point[0] + velocity[0] * t, point[1] + velocity[1] * t), t


def riders_at_line(output_dir: Path, files: Sequence, camera_index: int, model: Optional[Path], count_points, *,
                   cache: Optional[dict] = None) -> Optional[PeopleAtLine]:
    """Riders counted at the count line, timed at the finish line; ``None`` without any record."""

    if model is None or not Path(model).is_file() or not count_points:
        return None
    key = rider_model_key(model)
    covered, observations = [], []
    stats = {"files": len(files), "files_with_people": 0, "looked": 0, "watched": 0, "quiet": 0, "tracks": 0,
             "counting": "count_line"}
    for item in files:
        target = rider_path(output_dir, item.path)
        try:
            stat = target.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
            saved = cache.get(target) if cache is not None else None
            if saved is not None and saved[0] == stamp:
                payload = saved[1]
            else:
                payload = json.loads(target.read_text(encoding="utf-8"))
                if cache is not None:
                    cache[target] = (stamp, payload)
        except (OSError, ValueError):
            continue
        _finish, count, roi, band, road = _file_setup(item, count_points)
        if payload.get("meta") != _cache_meta(item.meta, key, roi, band, road, count):
            continue
        file_index = len(covered)
        covered.append(item)
        session = str(item.meta.get("session_id", ""))
        for frame_index, boxes in payload.get("frames", []):
            if 0 <= frame_index < len(item.pts):
                observations.append((item.start_ms + float(item.pts[frame_index]), session,
                                     (file_index, int(frame_index)), boxes))
        stats["looked"] += int(payload.get("stats", {}).get("looked", 0))
        stats["watched"] += int(payload.get("stats", {}).get("watched", 0))
        stats["quiet"] += int(payload.get("stats", {}).get("quiet", 0))
    if not covered:
        return None
    stats["files_with_people"] = len(covered)
    observations.sort(key=lambda value: value[0])
    tracks = rider_tracks(observations)
    stats["tracks"] = len(tracks)

    counted, backward = [], 0
    for track in tracks:
        item = covered[track[0][2][0]]
        finish, count, _roi, _band, _road = _file_setup(item, count_points)
        width, height = int(item.geometry.width), int(item.geometry.height)
        low, high = _count_range(count, width, height)
        # Towards the finish: the side of the count line the finish line is on.
        finish_mid = ((finish[0][0] + finish[1][0]) / 2, (finish[0][1] + finish[1][1]) / 2)
        forward = 1 if _side(finish_mid, *count) > 0 else -1
        found = _crossing_of(track, *count, forward)
        if found is None or not low <= _projection(found[3], *count) <= high:
            away = _crossing_of(track, *count, -forward)
            backward += away is not None and low <= _projection(away[3], *count) <= high
            continue
        index, _previous, _weight, point = found
        count_ms = _moment(track, found)
        count_mid = ((count[0][0] + count[1][0]) / 2, (count[0][1] + count[1][1]) / 2)
        finish_forward = -1 if _side(count_mid, *finish) > 0 else 1
        at_finish = _crossing_of(track, *finish, finish_forward, index)
        counted.append((track, index, count_ms, point, finish, finish_forward, at_finish))
    stats["counted"], stats["counted_backward"] = len(counted), backward
    if backward >= WRONG_WAY_MIN and backward > 2 * len(counted):
        stats["count_line_problem"] = (f"计数线可能画在了终点线后面：{backward} 人反方向越过计数线，"
                                       f"只有 {len(counted)} 人朝终点越过")
        return PeopleAtLine(files=covered, stats=stats)
    counted.sort(key=lambda value: value[2])
    seen = [(count_ms, _moment(track, f) - count_ms)
            for track, _index, count_ms, _point, _finish, _way, f in counted if f is not None]
    seen_at = [value[0] for value in seen]
    stats["finish_delay_ms"] = round(float(np.median([value[1] for value in seen]))) if seen else None
    timing = {"seen_at_finish": 0, "race_delay": 0, "velocity": 0, "count_line": 0}

    result = PeopleAtLine(files=covered, stats=stats)
    used_keys: set[str] = set()
    for track, index, count_ms, point, finish, finish_forward, at_finish in counted:
        lo, hi = max(0, index - 3), min(len(track) - 1, index + 2)
        span = max(1.0, track[hi][0] - track[lo][0])
        (ax, ay), (bx, by) = _foot(track[lo][3]), _foot(track[hi][3])
        velocity = ((bx - ax) / span, (by - ay) / span)
        earlier = [delay for _at, delay in seen[max(0, bisect_left(seen_at, count_ms) - DELAY_WINDOW):
                                                 bisect_left(seen_at, count_ms)]]
        if at_finish is not None:
            when, where = _moment(track, at_finish), at_finish[3]
            timing["seen_at_finish"] += 1
        else:
            reached = _ray_to_line(point, velocity, *finish)
            where = reached[0] if reached is not None else point
            if len(earlier) >= DELAY_MIN:
                when = count_ms + float(np.median(earlier))
                timing["race_delay"] += 1
            elif reached is not None:
                when = count_ms + reached[1]
                timing["velocity"] += 1
            else:
                when = count_ms
                timing["count_line"] += 1
        nearest = min(range(len(track)), key=lambda i: abs(track[i][0] - when))
        file_index, frame_index = _nearest_frame(covered, track[nearest][2][0], when)
        item = covered[file_index]
        geometry = item.geometry
        a, b = _strip_line(geometry)
        samples = len(geometry.xs)
        proj = float(np.clip(_projection(where, a, b), 0.0, 1.0))
        row = int(np.clip(round(proj * (samples - 1)), 0, samples - 1))
        box = track[nearest][3]
        rows = sorted(int(np.clip(round(_projection(corner, a, b) * (samples - 1)), 0, samples - 1))
                      for corner in ((box[0], box[1] + box[3]), (box[0] + box[2], box[1] + box[3])))
        record = _free_record(
            lambda at: _line_record(camera_index, item, when, frame_index, at, rows), row, samples, used_keys)
        if record is None:
            logger.warning("no free row left for a rider in frame %s of %s", frame_index, item.path.name)
            continue
        used_keys.add(record.key)
        heights = [entry[3][3] for entry in track]
        height = float(np.median(heights))
        duration = max(0.05, (track[-1][0] - track[0][0]) / 1000)
        first, last = _foot(track[0][3]), _foot(track[-1][3])
        speed = hypot(last[0] - first[0], last[1] - first[1]) / duration / max(1.0, height)
        result.crossings.append(PersonCrossing(
            record=record, direction=finish_forward, proj=round(proj, 3), height=round(height, 1),
            speed=round(float(speed), 3), score=round(float(np.median([entry[5] for entry in track])), 3),
            points=len(track), vehicle="bicycle",
        ))
        result.paths[record.key] = [[round(entry[0]), *(round(float(v), 1) for v in entry[3])]
                                    for entry in track if abs(entry[0] - when) <= PATH_REACH_MS]
    result.crossings.sort(key=lambda value: value.record.recorder_ms)
    stats["crossings"] = len(result.crossings)
    stats["finish_timing"] = timing
    return result


__all__ = ["count_line_pixels", "count_line_problem", "detect_file_riders", "extract_riders", "rider_areas",
           "rider_path", "rider_tracks", "riders_at_line", "valid_count_line"]
