"""Bicycle check for unpaired crossings with a user-supplied YOLOX model.

The model (Apache-2.0 YOLOX ONNX) is not shipped with FinishReview; the user
configures its path. It runs through ``cv2.dnn`` with one OpenCV thread and a
single instance, only on 1-3 crops per candidate crossing. Without a model
the check is skipped and nothing is excluded.

Decision rules (plan 7.1):
- a bicycle on the occluded part of the line keeps the crossing;
- only a confident, consistent pedestrian or vehicle excludes it;
- low confidence, disagreement between frames, a person cut by the image
  edge, or a crowded period marks it "disputed" instead.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

import cv2
import numpy as np


logger = logging.getLogger("FinishReview.BikeCheck")

PERSON, BICYCLE, CAR, MOTORCYCLE, TRUCK = 0, 1, 2, 3, 7
VEHICLES = {CAR: "汽车", MOTORCYCLE: "摩托车", TRUCK: "卡车"}
DETECTION_THRESHOLD = 0.2
EXCLUDE_THRESHOLD = 0.5
NMS_THRESHOLD = 0.45
SEEK_LEAD_MS = 4_000
FORWARD_READ_MS = 6_000

LABEL_BICYCLE = "bicycle"
LABEL_PEDESTRIAN = "pedestrian"
LABEL_VEHICLE = "vehicle"
LABEL_NOTHING = "nothing"
LABEL_UNCHECKED = "unchecked"
LABEL_NO_FRAME = "no_frame"

DECISION_KEEP = "keep"
DECISION_EXCLUDE = "exclude"
DECISION_DISPUTED = "disputed"

LABEL_TEXT = {
    LABEL_BICYCLE: "自行车",
    LABEL_PEDESTRIAN: "行人",
    LABEL_VEHICLE: "车辆",
    LABEL_NOTHING: "未认出",
    LABEL_UNCHECKED: "未认车",
    LABEL_NO_FRAME: "取帧失败",
}


@dataclass(frozen=True)
class Detection:
    cls: int
    score: float
    x: float
    y: float
    w: float
    h: float


@dataclass(frozen=True)
class BikeVerdict:
    label: str
    score: float
    decision: str
    reason: str = ""
    frames: int = 0
    vehicle: str = ""
    details: tuple = field(default_factory=tuple)
    edge_cut: bool = False


@dataclass(frozen=True)
class CropPlan:
    """Which frames and which part of them to examine for one camera."""

    mode: str = "up"
    offsets_ms: tuple[int, ...] = (50,)

    @classmethod
    def for_mode(cls, mode: str) -> "CropPlan":
        mode = (mode or "up").lower()
        if mode.startswith("side"):
            return cls(mode, (-400, -200, 50))
        return cls(mode, (50,))


class YoloxDetector:
    """Raw YOLOX ONNX output decoded with grids and strides."""

    def __init__(self, model_path: str | Path, input_size: int = 640):
        cv2.setNumThreads(1)
        self.input_size = int(input_size)
        self.net = cv2.dnn.readNetFromONNX(str(model_path))
        grids, strides = [], []
        for stride in (8, 16, 32):
            size = self.input_size // stride
            xv, yv = np.meshgrid(np.arange(size), np.arange(size))
            grids.append(np.stack((xv, yv), 2).reshape(-1, 2))
            strides.append(np.full((size * size, 1), stride))
        self._grid = np.concatenate(grids, 0).astype(np.float32)
        self._stride = np.concatenate(strides, 0).astype(np.float32)

    def detect(self, image: np.ndarray, classes: Sequence[int] = (PERSON, BICYCLE, CAR, MOTORCYCLE, TRUCK)) -> list[Detection]:
        height, width = image.shape[:2]
        ratio = min(self.input_size / height, self.input_size / width)
        padded = np.full((self.input_size, self.input_size, 3), 114, np.uint8)
        resized = cv2.resize(image, (int(width * ratio), int(height * ratio)), interpolation=cv2.INTER_LINEAR)
        padded[:resized.shape[0], :resized.shape[1]] = resized
        self.net.setInput(padded.transpose(2, 0, 1)[None].astype(np.float32))
        output = self.net.forward()[0]
        output[:, :2] = (output[:, :2] + self._grid) * self._stride
        output[:, 2:4] = np.exp(output[:, 2:4]) * self._stride
        detections = []
        for cls in classes:
            scores = output[:, 4] * output[:, 5 + cls]
            keep = scores > DETECTION_THRESHOLD
            if not keep.any():
                continue
            boxes = output[keep, :4]
            xywh = np.stack([boxes[:, 0] - boxes[:, 2] / 2, boxes[:, 1] - boxes[:, 3] / 2, boxes[:, 2], boxes[:, 3]], 1) / ratio
            indexes = cv2.dnn.NMSBoxes(xywh.tolist(), scores[keep].tolist(), DETECTION_THRESHOLD, NMS_THRESHOLD)
            for index in np.array(indexes).reshape(-1):
                x, y, w, h = (float(value) for value in xywh[index])
                detections.append(Detection(int(cls), float(scores[keep][index]), x, y, w, h))
        return detections


class FrameReader:
    """Decoded frames by media time: seek early, then read forward.

    Seeking HEVC recordings can land late (about 1.8 s on Helan camera 2) and
    their header frame rate can be wrong, so every request seeks at least
    four seconds early and keeps the decoder's real frame times.
    """

    def __init__(self, path: str | Path, threads: int = 2):
        self.path = Path(path)
        self.threads = int(threads)
        self._capture = None

    def _open(self):
        if self._capture is None:
            self._capture = cv2.VideoCapture(
                str(self.path), cv2.CAP_FFMPEG, [cv2.CAP_PROP_N_THREADS, self.threads],
            )
        return self._capture

    def frames_at(self, targets_ms: Sequence[float], tolerance_ms: float = 20.0) -> list[tuple[np.ndarray, float]]:
        """Return (frame, pts) for each ascending target; missing ones are skipped."""

        targets = sorted(float(value) for value in targets_ms)
        if not targets:
            return []
        last = getattr(self, "_last", None)
        if last is not None and last[1] <= targets[0] - tolerance_ms <= last[1] + FORWARD_READ_MS:
            # Close ahead of the last decoded frame: keep reading, no seek.
            return self._read_forward(self._capture, last[0], last[1], targets, tolerance_ms)
        for lead in (SEEK_LEAD_MS, 2 * SEEK_LEAD_MS):
            capture = self._open()
            if not capture.isOpened():
                return []
            capture.set(cv2.CAP_PROP_POS_MSEC, max(0.0, targets[0] - lead))
            ok, frame = capture.read()
            if not ok:
                return []
            pts = float(capture.get(cv2.CAP_PROP_POS_MSEC))
            if pts > targets[0] + tolerance_ms and targets[0] - lead > 0:
                continue  # overshoot: retry from further back
            return self._read_forward(capture, frame, pts, targets, tolerance_ms)
        return []

    def _read_forward(self, capture, frame, pts, targets, tolerance_ms):
        found = []
        pending = list(targets)
        while pending:
            if pts >= pending[0] - tolerance_ms:
                found.append((frame, pts))
                pending.pop(0)
                continue
            ok, frame = capture.read()
            if not ok:
                self._last = None
                return found
            pts = float(capture.get(cv2.CAP_PROP_POS_MSEC))
        self._last = (frame, pts)
        return found

    def close(self) -> None:
        self._last = None
        if self._capture is not None:
            self._capture.release()
            self._capture = None


def _crop(frame: np.ndarray, x0: int, y0: int, size: int) -> tuple[np.ndarray, int, int]:
    height, width = frame.shape[:2]
    size = min(size, height, width)
    x0 = int(np.clip(x0, 0, width - size))
    y0 = int(np.clip(y0, 0, height - size))
    return frame[y0:y0 + size, x0:x0 + size], x0, y0


def _edge_cut(detection: Detection, x0: int, y0: int, size: int, width: int, height: int) -> bool:
    left, top = detection.x + x0, detection.y + y0
    right, bottom = left + detection.w, top + detection.h
    return left <= 2 or top <= 2 or right >= width - 3 or bottom >= height - 3


def classify_frames(
    detector: YoloxDetector,
    frames: Sequence[np.ndarray],
    segment_points: Sequence[tuple[float, float]],
    plan: CropPlan,
    *,
    crowded: bool = False,
) -> BikeVerdict:
    """Classify one crossing from 1-3 frames around its line contact."""

    if not frames:
        return BikeVerdict(LABEL_NO_FRAME, 0.0, DECISION_KEEP, "没有可用画面", 0)
    points = np.asarray(segment_points, dtype=np.float64)
    cx, cy = float(points[:, 0].mean()), float(points[:, 1].mean())
    ylo, yhi = float(points[:, 1].min()), float(points[:, 1].max())
    per_frame = []
    edge_cut = False
    for frame in frames:
        height, width = frame.shape[:2]
        if plan.mode.startswith("side"):
            size = 1200
            reach_back, reach_forward = (300, 900) if plan.mode != "side-left" else (900, 300)
            crop, x0, y0 = _crop(frame, int(cx - reach_back), int((ylo + yhi) / 2 - size / 2), size)
            size = crop.shape[0]

            def near(d: Detection, x0=x0, y0=y0) -> bool:
                left, top = d.x + x0, d.y + y0
                return (left + d.w >= cx - reach_back and left <= cx + reach_forward
                        and top + d.h >= ylo - 200 and top <= yhi + 200)
        else:
            size = 1000
            lift = 700 if plan.mode == "up" else 500
            crop, x0, y0 = _crop(frame, int(cx - size / 2), int(cy - lift), size)
            size = crop.shape[0]
            local = points - (x0, y0)

            def near(d: Detection, local=local, grow=0.2) -> bool:
                return bool(np.any(
                    (local[:, 0] >= d.x - grow * d.w) & (local[:, 0] <= d.x + d.w * (1 + grow))
                    & (local[:, 1] >= d.y - grow * d.h) & (local[:, 1] <= d.y + d.h * (1 + grow))
                ))
        detections = [d for d in detector.detect(crop) if near(d)]
        bikes = [d.score for d in detections if d.cls == BICYCLE]
        people = [d for d in detections if d.cls == PERSON]
        vehicles = [d for d in detections if d.cls in VEHICLES]
        if any(_edge_cut(d, x0, y0, size, width, height) for d in people):
            edge_cut = True
        per_frame.append((max(bikes) if bikes else 0.0,
                          max((d.score for d in people), default=0.0),
                          max(vehicles, key=lambda d: d.score) if vehicles else None))
        if bikes and plan.mode.startswith("side"):
            break  # the whole bicycle was in view before the line
    bike = max(item[0] for item in per_frame)
    person = max(item[1] for item in per_frame)
    vehicle = max((item[2] for item in per_frame if item[2] is not None), key=lambda d: d.score, default=None)
    details = tuple((round(b, 3), round(p, 3), round(v.score, 3) if v else 0.0) for b, p, v in per_frame)
    if bike > 0 and (vehicle is None or vehicle.score <= bike or vehicle.cls != MOTORCYCLE):
        return BikeVerdict(LABEL_BICYCLE, bike, DECISION_KEEP, "", len(per_frame), details=details)
    if vehicle is not None:
        label, score = LABEL_VEHICLE, vehicle.score
    elif person > 0:
        label, score = LABEL_PEDESTRIAN, person
    else:
        return BikeVerdict(LABEL_NOTHING, 0.0, DECISION_KEEP, "", len(per_frame), details=details)
    return decide_verdict(
        label, score, details, edge_cut=edge_cut, crowded=crowded,
        vehicle=VEHICLES.get(vehicle.cls, "") if vehicle is not None else "",
    )


def decide_verdict(
    label: str,
    score: float,
    details: Sequence[Sequence[float]],
    *,
    edge_cut: bool,
    crowded: bool,
    vehicle: str = "",
) -> BikeVerdict:
    """Exclude or dispute a pedestrian/vehicle verdict from per-frame scores.

    ``details`` holds (bicycle, person, vehicle) scores per frame; a bicycle
    in any frame contradicts the verdict. Vehicles are fast, so one frame
    without them is normal and two clear frames are enough. A walking person
    stays in view in every frame; a person who shows up only in later frames
    moved fast, which looks more like a rider whose bicycle was missed
    (Tongxin 14:57:03), so pedestrians need every frame clear. Cached
    verdicts are re-decided with this, without running the detector again.
    """

    column = 2 if label == LABEL_VEHICLE else 1
    support = sum(1 for item in details if item[column] >= EXCLUDE_THRESHOLD)
    bicycle_seen = any(item[0] > 0 for item in details)
    needed = min(2, len(details)) if label == LABEL_VEHICLE else len(details)
    consistent = not bicycle_seen and support >= needed
    reasons = []
    if score < EXCLUDE_THRESHOLD or not consistent:
        reasons.append("把握不高或几帧不一致")
    if edge_cut and label == LABEL_PEDESTRIAN:
        reasons.append("人被画面边缘切掉")
    if crowded:
        reasons.append("终点线拥挤时段")
    decision = DECISION_DISPUTED if reasons else DECISION_EXCLUDE
    return BikeVerdict(
        label, score, decision, "；".join(reasons), len(details), vehicle,
        tuple(tuple(item) for item in details), edge_cut,
    )


__all__ = [
    "BikeVerdict",
    "CropPlan",
    "DECISION_DISPUTED",
    "DECISION_EXCLUDE",
    "DECISION_KEEP",
    "FrameReader",
    "decide_verdict",
    "LABEL_BICYCLE",
    "LABEL_NOTHING",
    "LABEL_PEDESTRIAN",
    "LABEL_TEXT",
    "LABEL_UNCHECKED",
    "LABEL_VEHICLE",
    "YoloxDetector",
    "classify_frames",
]
