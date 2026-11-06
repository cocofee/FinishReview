"""People at the finish line: who crossed, when, and which way (running only).

The detector sees people, never who they are.  Each crossing is pinned to an
original strip frame and handed to the existing pairing and rules, which pair
chips one to one, count groups and raise the questions.  A crossing is neither
an identity nor a confirmation.

Cost drives most choices here.  FFmpeg decodes and shrinks only the area
around the drawn line.  The detector runs only while something moves there,
at 5 frames per second, and at 10 while a person is near the line, so two
runners a few tenths of a second apart remain two crossings.
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import time
from dataclasses import dataclass, field
from math import hypot
from pathlib import Path
from typing import Callable, Optional, Sequence

import cv2
import numpy as np

from .bike_check import BICYCLE, CAR, MOTORCYCLE, PERSON, TRUCK, YoloxDetector
from .problem_rules import CrossingRecord


logger = logging.getLogger("FinishReview.PersonCrossing")

PERSON_VERSION = 1
PERSON_INPUT = 320
PERSON_MODEL_NAME = "yolox_nano.onnx"
BASE_STEP_MS = 200          # 5 frames per second while nobody is near the line
NEAR_STEP_MS = 100          # 10 frames per second while someone is
NEAR_LINE_HEIGHTS = 1.2     # "near": the feet within this many body heights of the line
NEAR_HOLD_MS = 600
MOTION_LEVEL = 18.0
MOTION_SHARE = 0.004
MOTION_HOLD_MS = 800
MIN_SCORE = 0.3
TRACK_GAP_MS = 450
SIDE_MARGIN_PX = 6.0
PROJ_LOW, PROJ_HIGH = -0.15, 1.05
# A line end within this share of the picture's edge runs on out of the frame.
EDGE_SHARE = 0.03
EDGE_PROJ_LOW, EDGE_PROJ_HIGH = -0.6, 1.6
# After a crossing, a track counts again only once its feet are this many body
# heights clear of the line.
REARM_HEIGHTS = 0.5
# How much of a crossing person's track is kept for the picture, either side.
PATH_REACH_MS = 2500
PROFILE_MIN = 5
# Two crossings this close, never seen apart, in one place: one person twice.
DUPLICATE_MS = 300
SAME_BOX_IOU = 0.3
SAME_PLACE_HEIGHTS = 0.6
# Tracking: a box mostly inside a stronger one is the same person; feet may move
# this many body heights per 100 ms step from where their velocity puts them.
SAME_PERSON_OVERLAP = 0.6
LINK_HEIGHTS = 0.3
VELOCITY_POINTS = 3
DIRECTION_SHARE = 0.8
DETECT_CLASSES = (PERSON, BICYCLE, CAR, MOTORCYCLE, TRUCK)
VEHICLE_CLASSES = (BICYCLE, CAR, MOTORCYCLE, TRUCK)


def person_model_path(model_path: Optional[Path], override: Optional[Path] = None) -> Optional[Path]:
    """The small model beside the configured one; the configured one otherwise.

    The operator supplies the models.  A larger model is far too slow to look at
    5–10 frames per second on one thread, so a ``yolox_nano.onnx`` placed in the
    same folder is preferred without changing the operator's setting.
    """

    if override:
        return Path(override)
    if not model_path:
        return None
    sibling = Path(model_path).with_name(PERSON_MODEL_NAME)
    return sibling if sibling.is_file() else Path(model_path)


def model_key(path: Path) -> str:
    stat = Path(path).stat()
    return hashlib.sha1(f"{Path(path).name}|{stat.st_size}|{PERSON_INPUT}".encode()).hexdigest()[:12]


def person_roi(xs: Sequence[float], ys: Sequence[float], width: int, height: int) -> tuple[int, int, int, int]:
    """Area around the line where an athlete's whole body can be.

    Athletes stand about a quarter of the frame high at the line, so the area
    reaches that far above it and a body width to either side.
    """

    margin_x, above, below = 0.18 * width, 0.30 * height, 0.06 * height
    x0 = max(0, int(min(xs) - margin_x)) // 2 * 2
    y0 = max(0, int(min(ys) - above)) // 2 * 2
    x1 = min(width, (int(max(xs) + margin_x) + 1) // 2 * 2)
    y1 = min(height, (int(max(ys) + below) + 1) // 2 * 2)
    return x0, y0, x1, y1


def _scaled_size(width: int, height: int) -> tuple[int, int]:
    scale = PERSON_INPUT / max(width, height)
    return max(2, int(width * scale) // 2 * 2), max(2, int(height * scale) // 2 * 2)


def person_path(output_dir: Path, strip_path: Path) -> Path:
    return Path(output_dir) / "persons" / Path(strip_path).parent.name / (Path(strip_path).stem + ".json")


def _roi_frames(path: Path, roi, size, executable, *, cancelled):
    """Yield each decoded frame of the line area, shrunk to ``size``."""

    x0, y0, x1, y1 = roi
    width, height = size
    if executable is not None:
        command = [str(executable), "-hide_banner", "-nostdin", "-loglevel", "error", "-threads", "1",
                   "-i", str(path), "-map", "0:v:0", "-an", "-filter_threads", "1",
                   "-vf", f"crop={x1 - x0}:{y1 - y0}:{x0}:{y0},scale={width}:{height}:flags=area",
                   "-fps_mode", "passthrough", "-pix_fmt", "bgr24", "-f", "rawvideo", "pipe:1"]
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                                  | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)))
        frame_bytes = width * height * 3
        try:
            while not cancelled():
                data = process.stdout.read(frame_bytes)
                if not data:
                    break
                if len(data) != frame_bytes:
                    raise ValueError("incomplete decoded frame")
                yield np.frombuffer(data, np.uint8).reshape(height, width, 3)
            if process.wait(timeout=10) and not cancelled():
                raise ValueError("FFmpeg decode failed")
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
            process.stdout.close()
        return
    capture = cv2.VideoCapture(str(path), cv2.CAP_FFMPEG, [cv2.CAP_PROP_N_THREADS, 1])
    try:
        while not cancelled():
            ok, frame = capture.read()
            if not ok or frame is None:
                break
            yield cv2.resize(frame[y0:y1, x0:x1], (width, height), interpolation=cv2.INTER_AREA)
    finally:
        capture.release()


@dataclass
class _Gate:
    """Motion and nearness state carried from one recording file to the next."""

    background: Optional[np.ndarray] = None
    last_motion_ms: float = -1e18
    near_until_ms: float = -1e18
    last_look_ms: float = -1e18


def _side(point, a, b) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    return (dx * (point[1] - a[1]) - dy * (point[0] - a[0])) / max(1.0, hypot(dx, dy))


def _projection(point, a, b) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    return ((point[0] - a[0]) * dx + (point[1] - a[1]) * dy) / max(1.0, dx * dx + dy * dy)


def detect_file_people(
    video_path: Path,
    start_ms: int,
    pts_ms: np.ndarray,
    line: tuple[tuple[float, float], tuple[float, float]],
    roi: tuple[int, int, int, int],
    detector: YoloxDetector,
    *,
    executable=None,
    gate: Optional[_Gate] = None,
    checkpoint: Optional[Callable[[], None]] = None,
    cancelled: Callable[[], bool] = lambda: False,
) -> dict:
    """Boxes per looked-at frame of one recording file, in full-frame pixels.

    ``frames`` holds ``[frame_index, boxes]``; ``boxes`` is ``None`` for a
    frame looked at while nothing moved (seen empty, not unseen).
    """

    gate = gate or _Gate()
    x0, y0, x1, y1 = roi
    size = _scaled_size(x1 - x0, y1 - y0)
    scale_x, scale_y = (x1 - x0) / size[0], (y1 - y0) / size[1]
    frames, looked, quiet, decoded = [], 0, 0, 0
    started = time.perf_counter()
    detect_seconds = 0.0
    for index, image in enumerate(_roi_frames(video_path, roi, size, executable, cancelled=cancelled)):
        decoded += 1
        if index >= len(pts_ms):
            raise ValueError("more decoded frames than the strip has")
        now = start_ms + float(pts_ms[index])
        small = cv2.cvtColor(cv2.resize(image, (max(8, size[0] // 4), max(8, size[1] // 4)),
                                        interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY).astype(np.float32)
        if gate.background is None or gate.background.shape != small.shape:
            gate.background = small.copy()
        if float((np.abs(small - gate.background) > MOTION_LEVEL).mean()) > MOTION_SHARE:
            gate.last_motion_ms = now
        cv2.accumulateWeighted(small, gate.background, 0.05)
        step = NEAR_STEP_MS if now <= gate.near_until_ms else BASE_STEP_MS
        if now - gate.last_look_ms < step - 25:
            continue
        gate.last_look_ms = now
        if now - gate.last_motion_ms > MOTION_HOLD_MS:
            frames.append([index, None])
            quiet += 1
            continue
        looked += 1
        before = time.perf_counter()
        boxes = []
        for box in detector.detect(image, classes=DETECT_CLASSES):
            if box.score < MIN_SCORE:
                continue
            bx, by, bw, bh = x0 + box.x * scale_x, y0 + box.y * scale_y, box.w * scale_x, box.h * scale_y
            boxes.append([int(box.cls), round(float(box.score), 3), round(bx, 1), round(by, 1), round(bw, 1), round(bh, 1)])
            if box.cls == PERSON and abs(_side((bx + bw / 2, by + bh), *line)) <= NEAR_LINE_HEIGHTS * bh:
                gate.near_until_ms = now + NEAR_HOLD_MS
        detect_seconds += time.perf_counter() - before
        frames.append([index, boxes])
        if checkpoint is not None:
            checkpoint()
    if not cancelled() and decoded != len(pts_ms):
        raise ValueError(f"decoded {decoded} frames, the strip has {len(pts_ms)}")
    return {"frames": frames, "stats": {"decoded": decoded, "looked": looked, "quiet": quiet,
                                        "seconds": round(time.perf_counter() - started, 3),
                                        "detect_seconds": round(detect_seconds, 3)}}


def _strip_line(geometry) -> tuple[tuple[float, float], tuple[float, float]]:
    return (float(geometry.xs[0]), float(geometry.ys[0])), (float(geometry.xs[-1]), float(geometry.ys[-1]))


def _cache_meta(strip_meta: dict, key: str, roi) -> dict:
    return {"version": PERSON_VERSION, "model": key, "roi": list(roi),
            "segment_id": strip_meta.get("segment_id", ""), "video_size": strip_meta.get("video_size"),
            "line_digest": strip_meta.get("line_digest", ""), "geometry_digest": strip_meta.get("geometry_digest", "")}


def extract_people(
    output_dir: Path,
    files: Sequence,
    model: Path,
    *,
    executable=None,
    gates: Optional[dict] = None,
    progress: Callable[[str, int, int], None] = lambda *_: None,
    checkpoint: Optional[Callable[[], None]] = None,
    cancelled: Callable[[], bool] = lambda: False,
) -> int:
    """Look for people in every strip file whose person record is missing or stale.

    ``files`` are loaded strips (``path``, ``meta``, ``start_ms``, ``pts``,
    ``geometry``) in time order.  Returns how many files were processed.
    """

    key = model_key(model)
    detector = None
    gates = {} if gates is None else gates
    done = 0
    for index, item in enumerate(files):
        if cancelled():
            break
        target = person_path(output_dir, item.path)
        line = _strip_line(item.geometry)
        roi = person_roi(item.geometry.xs, item.geometry.ys, int(item.geometry.width), int(item.geometry.height))
        meta = _cache_meta(item.meta, key, roi)
        if target.is_file():
            try:
                if json.loads(target.read_text(encoding="utf-8")).get("meta") == meta:
                    continue
            except (OSError, ValueError):
                pass
        if detector is None:
            detector = YoloxDetector(model, PERSON_INPUT)
        progress("找人", index, len(files))
        session = str(item.meta.get("session_id", ""))
        try:
            payload = detect_file_people(
                Path(item.meta.get("video_path", "")), int(item.start_ms), np.asarray(item.pts, float), line, roi,
                detector, executable=executable, gate=gates.setdefault(session, _Gate()),
                checkpoint=checkpoint, cancelled=cancelled,
            )
        except (OSError, ValueError) as error:
            logger.warning("person detection skipped for %s: %s", item.path.name, error)
            continue
        if cancelled():
            break
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(dict(payload, meta=meta), separators=(",", ":")), encoding="utf-8")
        temporary.replace(target)
        logger.info("people %s: %s", item.path.name, payload["stats"])
        done += 1
    progress("找人", len(files), len(files))
    return done


@dataclass(frozen=True)
class PersonCrossing:
    """One tracked person whose feet changed side of the line."""

    record: CrossingRecord
    direction: int
    proj: float
    height: float
    speed: float
    score: float
    points: int
    vehicle: str = ""


@dataclass
class PeopleAtLine:
    files: list
    crossings: list[PersonCrossing] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    # Where each crossing's person was around the crossing: crossing key ->
    # [[recorder_ms, x, y, w, h], ...] in full-frame pixels, so a number can
    # follow the runner on the picture instead of staying on the line.
    paths: dict = field(default_factory=dict)

    def by_key(self) -> dict[str, PersonCrossing]:
        return {crossing.record.key: crossing for crossing in self.crossings}


def _iou(a, b) -> float:
    w = max(0.0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
    h = max(0.0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    inter = w * h
    return inter / max(1.0, a[2] * a[3] + b[2] * b[3] - inter)


def _overlap_of_smaller(a, b) -> float:
    w = max(0.0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
    h = max(0.0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    return w * h / max(1.0, min(a[2] * a[3], b[2] * b[3]))


def _one_box_each(boxes):
    """Person boxes of one frame, a box mostly inside a stronger one dropped.

    The detector often boxes a runner twice — the body, and the body with its
    shadow or swinging arm — which would split one person into two tracks.
    """

    kept = []
    for box in sorted((b for b in boxes if b[0] == PERSON), key=lambda b: -b[1]):
        if all(_overlap_of_smaller(box[2:], other[2:]) < SAME_PERSON_OVERLAP for other in kept):
            kept.append(box)
    return kept


def _foot(box) -> tuple[float, float]:
    return box[0] + box[2] / 2, box[1] + box[3]


def _tracks(observations):
    """Greedy linking of feet with a constant-velocity guess; one person, one track.

    Feet, not box centres: the crossing is judged at the feet, and a box that
    grows by an arm or a shadow moves its centre far more than its feet.
    """

    live, done = [], []
    for stamp, session, where, boxes in observations:
        people = _one_box_each(boxes or [])
        vehicles = [b[2:] for b in (boxes or []) if b[0] in VEHICLE_CLASSES]
        still = []
        for track in live:
            (still if stamp - track[-1][0] <= TRACK_GAP_MS and track[-1][1] == session else done).append(track)
        live = still
        pairs = []
        for ti, track in enumerate(live):
            last_ms, last = track[-1][0], track[-1][3]
            fx, fy = _foot(last)
            steps = max(1.0, (stamp - last_ms) / 100.0)
            if len(track) >= 2:
                back = min(len(track) - 1, VELOCITY_POINTS)
                prior_ms, prior = track[-1 - back][0], track[-1 - back][3]
                px, py = _foot(prior)
                factor = (stamp - last_ms) / max(1.0, last_ms - prior_ms)
                fx += (fx - px) * factor
                fy += (fy - py) * factor
                limit = LINK_HEIGHTS * steps ** 0.5
            else:
                limit = LINK_HEIGHTS * 1.5 * steps
            for bi, box in enumerate(people):
                b = box[2:]
                bx, by = _foot(b)
                distance = hypot(fx - bx, fy - by) / max(last[3], b[3])
                if distance < limit and min(last[3], b[3]) / max(last[3], b[3]) > 0.5:
                    pairs.append((distance, ti, bi))
        used_tracks, used_boxes = set(), set()
        for _distance, ti, bi in sorted(pairs):
            if ti in used_tracks or bi in used_boxes:
                continue
            used_tracks.add(ti)
            used_boxes.add(bi)
            b = people[bi][2:]
            live[ti].append((stamp, session, where, b, any(_iou(b, v) >= 0.1 for v in vehicles), people[bi][1]))
        for bi, box in enumerate(people):
            if bi not in used_boxes:
                b = box[2:]
                live.append([(stamp, session, where, b, any(_iou(b, v) >= 0.1 for v in vehicles), box[1])])
    return done + live


def people_at_line(output_dir: Path, files: Sequence, camera_index: int, model: Optional[Path], *,
                   cache: Optional[dict] = None) -> Optional[PeopleAtLine]:
    """Crossings of every file that has a current person record; ``None`` without any.

    ``cache`` keeps parsed records between live rounds, keyed by file stamp.
    """

    if model is None or not Path(model).is_file():
        return None
    key = model_key(model)
    covered, observations = [], []
    stats = {"files": len(files), "files_with_people": 0, "looked": 0, "quiet": 0, "tracks": 0}
    for item in files:
        target = person_path(output_dir, item.path)
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
        roi = person_roi(item.geometry.xs, item.geometry.ys, int(item.geometry.width), int(item.geometry.height))
        if payload.get("meta") != _cache_meta(item.meta, key, roi):
            continue
        file_index = len(covered)
        covered.append(item)
        session = str(item.meta.get("session_id", ""))
        for frame_index, boxes in payload.get("frames", []):
            if 0 <= frame_index < len(item.pts):
                observations.append((item.start_ms + float(item.pts[frame_index]), session,
                                     (file_index, int(frame_index)), boxes))
        stats["looked"] += int(payload.get("stats", {}).get("looked", 0))
        stats["quiet"] += int(payload.get("stats", {}).get("quiet", 0))
    if not covered:
        return None
    stats["files_with_people"] = len(covered)
    observations.sort(key=lambda value: value[0])
    tracks = _tracks(observations)
    stats["tracks"] = len(tracks)
    result = PeopleAtLine(files=covered, stats=stats)
    used_keys: set[str] = set()
    found = []
    for track in tracks:
        found += [(crossing, track) for crossing in _track_crossings(track, covered, camera_index, used_keys)]
    kept, merged = _one_person_once(found)
    result.crossings = sorted(kept, key=lambda value: value.record.recorder_ms)
    for crossing, track in found:
        at = crossing.record.recorder_ms
        result.paths[crossing.record.key] = [
            [round(entry[0]), *(round(float(v), 1) for v in entry[3])]
            for entry in track if abs(entry[0] - at) <= PATH_REACH_MS]
    stats["crossings"] = len(result.crossings)
    stats["merged"] = merged
    return result


def _seen_apart(first, second) -> bool:
    """Two tracks seen as two separate people at the same moment."""

    boxes = {entry[0]: entry[3] for entry in first}
    return any(entry[0] in boxes and _iou(boxes[entry[0]], entry[3]) < SAME_BOX_IOU for entry in second)


def _same_place(first, second) -> bool:
    """The two tracks' boxes nearest in time sit where one person would be."""

    gap, a, b = min(((abs(x[0] - y[0]), x[3], y[3]) for x in first for y in second), key=lambda value: value[0])
    if gap > DUPLICATE_MS:
        return False
    height = max(a[3], b[3], 1.0)
    distance = hypot(a[0] + a[2] / 2 - b[0] - b[2] / 2, a[1] + a[3] - b[1] - b[3])
    return _iou(a, b) >= SAME_BOX_IOU or distance < SAME_PLACE_HEIGHTS * height


def _one_person_once(found):
    """Drop the second crossing of one person whose track broke into pieces.

    Runners side by side are seen as two boxes in the same frames, so they stay
    two crossings however close in time; pieces of one track never are.
    """

    found = sorted(found, key=lambda value: value[0].record.recorder_ms)
    dropped = set()
    for i, (crossing, track) in enumerate(found):
        if i in dropped:
            continue
        for j in range(i + 1, len(found)):
            other, other_track = found[j]
            if other.record.recorder_ms - crossing.record.recorder_ms > DUPLICATE_MS:
                break
            if j in dropped or other.direction != crossing.direction:
                continue
            if _seen_apart(track, other_track) or not _same_place(track, other_track):
                continue
            loser = j if len(other_track) <= len(track) else i
            dropped.add(loser)
            if loser == i:
                break
    return [crossing for index, (crossing, _track) in enumerate(found) if index not in dropped], len(dropped)


def _proj_range(geometry) -> tuple[float, float]:
    """How far past each end of the line a crossing still counts.

    A line drawn to the edge of the picture goes on beyond it: someone crossing
    there is half out of the frame, feet cut off, and still crossed the line.
    """

    a, b = _strip_line(geometry)
    width, height = float(geometry.width), float(geometry.height)
    margin = EDGE_SHARE * min(width, height)

    def at_edge(point):
        return (point[0] <= margin or point[1] <= margin
                or point[0] >= width - margin or point[1] >= height - margin)

    return (EDGE_PROJ_LOW if at_edge(a) else PROJ_LOW), (EDGE_PROJ_HIGH if at_edge(b) else PROJ_HIGH)


def _line_record(camera_index: int, item, when: float, frame_index: int, row: int, rows) -> CrossingRecord:
    """A person's crossing as a line record: the original frame, and where on the line."""

    geometry = item.geometry
    return CrossingRecord(
        camera_index=camera_index, session_id=str(item.meta.get("session_id", "")),
        segment_id=str(item.meta.get("segment_id", "")), video_path=str(item.meta.get("video_path", "")),
        recorder_ms=int(round(when)), frame_index=int(frame_index),
        position_ms=int(round(float(item.pts[frame_index]))), row=row, r0=rows[0], r1=rows[1],
        contact=geometry.point(row),
        segment_points=tuple(geometry.point(int(r)) for r in np.linspace(rows[0], rows[1], 9).round()),
    )


def _free_record(make: Callable[[int], CrossingRecord], row: int, samples: int,
                 used_keys: set[str]) -> Optional[CrossingRecord]:
    """The record at ``row``, else the nearest free row up the line, then down it.

    People crossing in one frame can land on one row.  Walking only up and
    then bouncing between the last two rows never ended once both were taken
    (three people in one frame at the end of the line); this walks up first,
    as before, then down.  ``None`` only when every row of that frame is taken.
    """

    for at in (*range(row, samples), *range(row - 1, -1, -1)):
        record = make(at)
        if record.key not in used_keys:
            return record
    return None


def _track_crossings(track, files, camera_index: int, used_keys: set[str]) -> list[PersonCrossing]:
    """Every crossing of one track.

    Usually one.  Two people can still be joined into one track — one walks out
    of the picture where the next walks in — and the second must not vanish
    behind the first, so a track counts again once it has clearly left the line
    on the other side.  Feet wavering on the line count once.
    """

    if len(track) < 2:
        return []
    first_file = files[track[0][2][0]]
    a, b = _strip_line(first_file.geometry)
    low, high = _proj_range(first_file.geometry)
    anchors = [(entry[3][0] + entry[3][2] / 2, entry[3][1] + entry[3][3]) for entry in track]
    sides = [_side(point, a, b) for point in anchors]
    found = []
    previous = None
    armed = True
    for index, value in enumerate(sides):
        if abs(value) < SIDE_MARGIN_PX:
            continue
        if not armed and abs(value) >= REARM_HEIGHTS * track[index][3][3]:
            armed = True
        if previous is not None and (value > 0) != (sides[previous] > 0) and armed:
            weight = sides[previous] / (sides[previous] - value)
            pa, pb = anchors[previous], anchors[index]
            point = (pa[0] + weight * (pb[0] - pa[0]), pa[1] + weight * (pb[1] - pa[1]))
            proj = _projection(point, a, b)
            start, previous = previous, index
            if not low <= proj <= high:
                continue
            armed = False
            when = track[start][0] + weight * (track[index][0] - track[start][0])
            heights = [entry[3][3] for entry in track]
            height = float(np.median(heights))
            duration = max(0.05, (track[-1][0] - track[0][0]) / 1000)
            speed = hypot(anchors[-1][0] - anchors[0][0], anchors[-1][1] - anchors[0][1]) / duration / max(1.0, height)
            on_vehicle = sum(entry[4] for entry in track) >= max(1, len(track) / 2)
            file_index, frame_index = _nearest_frame(files, track[index][2][0], when)
            item = files[file_index]
            geometry = item.geometry
            samples = len(geometry.xs)
            row = int(np.clip(round(proj * (samples - 1)), 0, samples - 1))
            box = track[index][3]
            rows = sorted(int(np.clip(round(_projection(corner, a, b) * (samples - 1)), 0, samples - 1))
                          for corner in ((box[0], box[1] + box[3]), (box[0] + box[2], box[1] + box[3])))
            record = _free_record(
                lambda at: _line_record(camera_index, item, when, frame_index, at, rows), row, samples, used_keys)
            if record is None:
                logger.warning("no free row left for a crossing in frame %s of %s", frame_index, item.path.name)
                continue
            used_keys.add(record.key)
            found.append(PersonCrossing(
                record=record, direction=1 if value > 0 else -1, proj=round(float(proj), 3),
                height=round(height, 1), speed=round(float(speed), 3),
                score=round(float(np.median([entry[5] for entry in track])), 3), points=len(track),
                vehicle="bicycle" if on_vehicle else "",
            ))
            continue
        previous = index
    return found


def _nearest_frame(files, hint: int, when: float) -> tuple[int, int]:
    """The original frame nearest the crossing moment, in the file that holds it."""

    for file_index in (hint - 1, hint, hint + 1):
        if 0 <= file_index < len(files):
            item = files[file_index]
            times = item.start_ms + np.asarray(item.pts, float)
            if times.size and times[0] - 30 <= when <= times[-1] + 30:
                return file_index, int(np.argmin(np.abs(times - when)))
    item = files[hint]
    return hint, int(np.argmin(np.abs(item.start_ms + np.asarray(item.pts, float) - when)))


@dataclass(frozen=True)
class AthleteProfile:
    """What the paired athletes looked like at this line, learned per race."""

    direction: int
    height: tuple[float, float]
    min_speed: float
    proj: tuple[float, float]
    samples: int

    def payload(self) -> dict:
        return {"direction": self.direction, "height": [round(v, 1) for v in self.height],
                "min_speed": round(self.min_speed, 3), "proj": [round(v, 3) for v in self.proj],
                "samples": self.samples}


def configured_direction(arrow, geometry=None) -> Optional[int]:
    """The sprint arrow saved for a camera, as the side runners move towards.

    ``arrow`` is ``None`` (learn it from the paired athletes), ``0`` (不限方向),
    or a unit ``[dx, dy]`` in picture pixels pointing the way the runners cross.
    Returns +1 / -1 (see ``_side``) or ``None``.  The arrow is stored on the
    picture, so the line can be redrawn without turning the runners around.
    """

    if arrow is None:
        return None
    if not isinstance(arrow, (list, tuple)):
        return 0 if not arrow else None
    if geometry is None or len(arrow) != 2:
        return None
    a, b = _strip_line(geometry)
    # _side is positive towards (-dy, dx); a crossing with value > 0 is direction +1.
    along = -(b[1] - a[1]) * float(arrow[0]) + (b[0] - a[0]) * float(arrow[1])
    if abs(along) < 1e-9:
        return None
    return 1 if along > 0 else -1


def learn_profile(matched: Sequence[PersonCrossing], direction: Optional[int]) -> Optional[AthleteProfile]:
    if len(matched) < PROFILE_MIN:
        return None
    heights = np.array([c.height for c in matched], float)
    speeds = np.array([c.speed for c in matched], float)
    projs = np.array([c.proj for c in matched], float)
    if direction is None:
        forward = sum(c.direction > 0 for c in matched)
        share = max(forward, len(matched) - forward) / len(matched)
        direction = (1 if forward * 2 >= len(matched) else -1) if share >= DIRECTION_SHARE else 0
    return AthleteProfile(
        direction=direction,
        height=(0.8 * float(np.percentile(heights, 5)), 1.25 * float(np.percentile(heights, 95))),
        min_speed=0.6 * float(np.percentile(speeds, 5)),
        proj=(float(np.percentile(projs, 2)) - 0.05, float(np.percentile(projs, 98)) + 0.05),
        samples=len(matched),
    )


def not_athlete_reason(crossing: PersonCrossing, profile: Optional[AthleteProfile],
                       direction: Optional[int] = None, *, riders: bool = False) -> str:
    """Why a crossing nobody's chip claimed is not an athlete; empty when it may be one.

    ``riders``: the crossings are cycling riders (rider_counting), so riding is
    what an athlete does, not a reason to leave them out.
    """

    if crossing.vehicle and not riders:
        return "骑车或车辆"
    wanted = profile.direction if profile is not None else (direction or 0)
    if wanted and crossing.direction != wanted:
        return "反方向"
    if profile is None:
        return ""
    if not profile.height[0] <= crossing.height <= profile.height[1]:
        return "大小不像选手"
    if crossing.speed < profile.min_speed:
        return "站着或慢走"
    if not profile.proj[0] <= crossing.proj <= profile.proj[1]:
        return "不在选手通道"
    return ""


__all__ = [
    "AthleteProfile", "PeopleAtLine", "PersonCrossing", "configured_direction", "extract_people",
    "learn_profile", "not_athlete_reason", "people_at_line", "person_model_path", "person_path", "person_roi",
]
