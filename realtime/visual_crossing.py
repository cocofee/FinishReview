"""Low-resource visual crossing candidates for finish-line review.

The detector is deliberately conservative: it emits evidence candidates only.
It never creates or changes an official timing result.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import cv2
import numpy as np
from PyQt5.QtCore import QThread, Qt, pyqtSignal
from PyQt5.QtGui import QImage, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import QComboBox, QDialog, QDialogButtonBox, QLabel, QVBoxLayout, QWidget

from .thread_lifecycle import retire_qthread, track_qthread

logger = logging.getLogger("FinishReview.VisualCrossing")


@dataclass(frozen=True, slots=True)
class CrossingConfig:
    """Conservative detector settings suitable for an office laptop."""

    process_width: int = 640
    process_fps: float = 8.0
    roi_left: float = 0.05
    roi_top: float = 0.08
    roi_right: float = 0.95
    roi_bottom: float = 0.95
    gate_a: float = 0.46
    gate_b: float = 0.54
    finish_line: float = 0.50
    gate_width: float = 0.08
    forward_direction: str = "left_to_right"
    min_area_ratio: float = 0.0012
    max_area_ratio: float = 0.30
    min_gate_gap_px: int = 8
    max_track_age_ms: int = 1400
    cooldown_ms: int = 350
    min_motion_px: float = 3.0
    max_track_distance_px: float = 180.0
    history: int = 180
    var_threshold: float = 32.0

    def normalized(self) -> "CrossingConfig":
        left, right = sorted((float(self.roi_left), float(self.roi_right)))
        line = max(0.10, min(0.90, float(self.finish_line)))
        width = max(0.02, min(0.30, float(self.gate_width)))
        a, b = sorted((line - width / 2.0, line + width / 2.0))
        if self.gate_a != 0.46 or self.gate_b != 0.54:
            a, b = sorted((float(self.gate_a), float(self.gate_b)))
        return CrossingConfig(
            process_width=max(240, int(self.process_width)),
            process_fps=max(2.0, min(15.0, float(self.process_fps))),
            roi_left=max(0.0, min(1.0, left)),
            roi_top=max(0.0, min(1.0, float(self.roi_top))),
            roi_right=max(0.0, min(1.0, right)),
            roi_bottom=max(0.0, min(1.0, float(self.roi_bottom))),
            gate_a=max(0.05, min(0.95, a)),
            gate_b=max(0.05, min(0.95, b)),
            finish_line=line,
            gate_width=max(0.02, min(0.30, b - a)),
            forward_direction=(
                self.forward_direction
                if self.forward_direction in {"left_to_right", "right_to_left"}
                else "left_to_right"
            ),
            min_area_ratio=max(0.0001, min(0.5, float(self.min_area_ratio))),
            max_area_ratio=max(0.01, min(0.9, float(self.max_area_ratio))),
            min_gate_gap_px=max(2, int(self.min_gate_gap_px)),
            max_track_age_ms=max(250, int(self.max_track_age_ms)),
            cooldown_ms=max(0, int(self.cooldown_ms)),
            min_motion_px=max(0.5, float(self.min_motion_px)),
            max_track_distance_px=max(40.0, float(self.max_track_distance_px)),
            history=max(30, int(self.history)),
            var_threshold=max(4.0, float(self.var_threshold)),
        )


@dataclass(frozen=True, slots=True)
class VisualCrossingEvent:
    event_id: str
    camera_index: int
    timestamp_ms: int
    direction: str
    confidence: float
    centroid_x: float
    centroid_y: float
    bbox: tuple[int, int, int, int]
    evidence_hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["bbox"] = list(self.bbox)
        return payload


class VisualCrossingEventStore:
    """Append-only JSONL store; writes are short and serialized."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def append(self, event: VisualCrossingEvent) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event.to_dict(), ensure_ascii=False, separators=(",", ":"))
        with self._lock, self.path.open("a", encoding="utf-8") as output:
            output.write(line + "\n")
            output.flush()

    def events(self) -> tuple[VisualCrossingEvent, ...]:
        if not self.path.is_file():
            return ()
        result: list[VisualCrossingEvent] = []
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return ()
        for line in lines:
            try:
                payload = json.loads(line)
                result.append(
                    VisualCrossingEvent(
                        event_id=str(payload["event_id"]),
                        camera_index=int(payload["camera_index"]),
                        timestamp_ms=int(payload["timestamp_ms"]),
                        direction=str(payload["direction"]),
                        confidence=float(payload["confidence"]),
                        centroid_x=float(payload["centroid_x"]),
                        centroid_y=float(payload["centroid_y"]),
                        bbox=tuple(int(value) for value in payload["bbox"]),
                        evidence_hint=str(payload.get("evidence_hint") or ""),
                    )
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
        return tuple(result)


class DualGateCrossingDetector:
    """MOG2 + two-gate tracker for a side-facing finish camera."""

    def __init__(
        self,
        camera_index: int,
        *,
        config: CrossingConfig | None = None,
        event_sink: Callable[[VisualCrossingEvent], None] | None = None,
    ):
        self.camera_index = max(1, int(camera_index))
        self.config = (config or CrossingConfig()).normalized()
        self.event_sink = event_sink
        self._subtractor = cv2.createBackgroundSubtractorMOG2(
            history=self.config.history,
            varThreshold=self.config.var_threshold,
            detectShadows=False,
        )
        self._tracks: dict[int, dict[str, Any]] = {}
        self._next_track_id = 1
        self._last_centroid_x: float | None = None

    def process(self, frame: np.ndarray, timestamp_ms: int | None = None) -> tuple[VisualCrossingEvent, ...]:
        if frame is None or frame.size == 0:
            return ()
        stamp = int(timestamp_ms if timestamp_ms is not None else time.time() * 1000)
        height, width = frame.shape[:2]
        if width > self.config.process_width:
            scale = self.config.process_width / float(width)
            frame = cv2.resize(frame, (self.config.process_width, max(1, int(height * scale))), interpolation=cv2.INTER_AREA)
            height, width = frame.shape[:2]
        x1, y1 = int(width * self.config.roi_left), int(height * self.config.roi_top)
        x2, y2 = int(width * self.config.roi_right), int(height * self.config.roi_bottom)
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(width, max(x1 + 1, x2)), min(height, max(y1 + 1, y2))
        roi = frame[y1:y2, x1:x2]
        mask = self._subtractor.apply(roi, learningRate=0.02)
        kernel = np.ones((3, 3), dtype=np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=1)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
        min_area = max(10.0, roi.shape[0] * roi.shape[1] * self.config.min_area_ratio)
        max_area = roi.shape[0] * roi.shape[1] * self.config.max_area_ratio
        detections: list[tuple[float, float, tuple[int, int, int, int]]] = []
        for contour in cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[-2]:
            area = float(cv2.contourArea(contour))
            if area < min_area or area > max_area:
                continue
            bx, by, bw, bh = cv2.boundingRect(contour)
            if bw < 3 or bh < 5:
                continue
            cx = x1 + bx + bw / 2.0
            cy = y1 + by + bh / 2.0
            detections.append((cx, cy, (x1 + bx, y1 + by, bw, bh)))

        gate_a = width * self.config.gate_a
        gate_b = width * self.config.gate_b
        if gate_b - gate_a < self.config.min_gate_gap_px:
            return ()
        used: set[int] = set()
        events: list[VisualCrossingEvent] = []
        for cx, cy, bbox in detections:
            best_id = None
            best_distance = self.config.max_track_distance_px
            for track_id, track in self._tracks.items():
                if track_id in used or stamp - int(track["last_ms"]) > self.config.max_track_age_ms:
                    continue
                distance = float(np.hypot(cx - float(track["x"]), cy - float(track["y"])))
                if distance < best_distance:
                    best_id, best_distance = track_id, distance
            if best_id is None:
                best_id = self._next_track_id
                self._next_track_id += 1
                self._tracks[best_id] = {
                    "x": cx,
                    "y": cy,
                    "last_ms": stamp,
                    "bbox": bbox,
                    "forward_gate_a_ms": None,
                    "reverse_gate_b_ms": None,
                    "last_event_ms": -10**12,
                }
            track = self._tracks[best_id]
            previous_x = float(track["x"])
            previous_y = float(track["y"])
            track.update(x=cx, y=cy, last_ms=stamp, bbox=bbox)
            used.add(best_id)
            motion_distance = float(np.hypot(cx - previous_x, cy - previous_y))
            if motion_distance < self.config.min_motion_px:
                continue
            if previous_x < gate_a <= cx:
                track["forward_gate_a_ms"] = stamp
            if previous_x > gate_b >= cx:
                track["reverse_gate_b_ms"] = stamp
            forward_confirmed = (
                track.get("forward_gate_a_ms") is not None
                and previous_x < gate_b <= cx
                and stamp - int(track["forward_gate_a_ms"]) <= self.config.max_track_age_ms
            )
            reverse_confirmed = (
                track.get("reverse_gate_b_ms") is not None
                and previous_x > gate_a >= cx
                and stamp - int(track["reverse_gate_b_ms"]) <= self.config.max_track_age_ms
            )
            if (
                not (forward_confirmed or reverse_confirmed)
                or stamp - int(track.get("last_event_ms", -10**12))
                < self.config.cooldown_ms
            ):
                continue
            direction = "forward" if forward_confirmed else "reverse"
            if self.config.forward_direction == "right_to_left":
                direction = "forward" if direction == "reverse" else "reverse"
            confidence = min(0.99, max(0.25, abs(cx - previous_x) / max(1.0, gate_b - gate_a)))
            event = VisualCrossingEvent(
                event_id=uuid.uuid4().hex,
                camera_index=self.camera_index,
                timestamp_ms=stamp,
                direction=direction,
                confidence=round(confidence, 3),
                centroid_x=round(cx, 1),
                centroid_y=round(cy, 1),
                bbox=bbox,
                evidence_hint="candidate_only",
            )
            track["last_event_ms"] = stamp
            track["forward_gate_a_ms"] = None
            track["reverse_gate_b_ms"] = None
            events.append(event)
            if self.event_sink is not None:
                self.event_sink(event)
        cutoff = stamp - self.config.max_track_age_ms
        self._tracks = {key: value for key, value in self._tracks.items() if int(value["last_ms"]) >= cutoff}
        self._last_centroid_x = detections[-1][0] if detections else self._last_centroid_x
        return tuple(events)


class VisualCrossingWorker(QThread):
    """Read one RTSP source at low rate without blocking the Qt GUI."""

    crossing_detected = pyqtSignal(object)
    status_changed = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, source: str, camera_index: int, output_path: Path, parent=None, *, config: CrossingConfig | None = None):
        super().__init__(parent)
        self.source = str(source).strip()
        self.camera_index = max(1, int(camera_index))
        self.store = VisualCrossingEventStore(output_path)
        self.config = (config or CrossingConfig()).normalized()
        self._stop_event = threading.Event()
        self._capture: cv2.VideoCapture | None = None
        self.frames_processed = 0
        self.events_detected = 0

    def request_stop(self) -> None:
        self.requestInterruption()
        self._stop_event.set()
        capture = self._capture
        if capture is not None:
            try:
                capture.release()
            except Exception:
                logger.debug("Could not release visual capture promptly", exc_info=True)

    def stop(self) -> None:
        self.request_stop()

    def run(self) -> None:
        detector = DualGateCrossingDetector(
            self.camera_index,
            config=self.config,
            event_sink=self._on_event,
        )
        capture = cv2.VideoCapture()
        for property_name, value in (
            ("CAP_PROP_OPEN_TIMEOUT_MSEC", 3_000),
            ("CAP_PROP_READ_TIMEOUT_MSEC", 1_000),
        ):
            property_id = getattr(cv2, property_name, None)
            if property_id is not None:
                capture.set(property_id, value)
        capture.open(self.source, cv2.CAP_FFMPEG)
        self._capture = capture
        if not capture.isOpened():
            capture.release()
            self.failed.emit(f"机位{self.camera_index}视觉检测无法打开视频源")
            return
        buffer_property = getattr(cv2, "CAP_PROP_BUFFERSIZE", None)
        if buffer_property is not None:
            capture.set(buffer_property, 1)
        interval = 1.0 / self.config.process_fps
        next_process_at = time.monotonic()
        failed = False
        self.status_changed.emit(f"机位{self.camera_index}视觉检测已启动")
        try:
            while not self._stop_event.is_set():
                ok, frame = capture.read()
                if not ok:
                    failed = True
                    self.failed.emit(f"机位{self.camera_index}视觉检测读取中断")
                    break
                frame_timestamp_ms = int(time.time() * 1000)
                self.frames_processed += 1
                now = time.monotonic()
                if now < next_process_at:
                    continue
                detector.process(frame, frame_timestamp_ms)
                next_process_at = now + interval
        except Exception as exc:  # noqa: BLE001 - detector must never stop recording.
            logger.exception("Visual crossing worker failed")
            failed = True
            self.failed.emit(f"机位{self.camera_index}视觉检测异常: {type(exc).__name__}")
        finally:
            capture.release()
            self._capture = None
            if not failed:
                self.status_changed.emit(f"机位{self.camera_index}视觉检测已停止")

    def _on_event(self, event: VisualCrossingEvent) -> None:
        try:
            self.store.append(event)
        except OSError:
            logger.exception("Could not persist visual crossing event")
        self.events_detected += 1
        self.crossing_detected.emit(event)


class _PreviewFrameWorker(QThread):
    frame_ready = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, source: str, parent=None):
        super().__init__(parent)
        self.source = str(source)

    def request_stop(self) -> None:
        self.requestInterruption()

    def stop(self) -> None:
        self.request_stop()

    def run(self) -> None:
        capture = cv2.VideoCapture(self.source, cv2.CAP_FFMPEG)
        try:
            if not capture.isOpened():
                self.failed.emit("无法打开摄像头画面")
                return
            ok, frame = capture.read()
            if not ok:
                self.failed.emit("摄像头没有返回画面")
                return
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            height, width = rgb.shape[:2]
            image = QImage(rgb.data, width, height, int(rgb.strides[0]), QImage.Format_RGB888).copy()
            self.frame_ready.emit(image)
        finally:
            capture.release()


class _LineCanvas(QWidget):
    def __init__(self, parent=None, *, gate_width: float = 0.08, roi_top: float = 0.08, roi_bottom: float = 0.95):
        super().__init__(parent)
        self._pixmap = QPixmap()
        self._line_x = 0.50
        self._gate_width = max(0.02, min(0.30, float(gate_width)))
        self._roi_top = max(0.0, min(0.80, float(roi_top)))
        self._roi_bottom = max(self._roi_top + 0.05, min(1.0, float(roi_bottom)))
        self._drag_target = "line"
        self.setMinimumSize(640, 360)
        self.setMouseTracking(True)

    @property
    def line_x(self) -> float:
        return self._line_x

    def set_image(self, image: QImage) -> None:
        self._pixmap = QPixmap.fromImage(image)
        self.update()

    def set_line_x(self, value: float) -> None:
        self._line_x = max(0.02, min(0.98, float(value)))
        self.update()

    def set_gate_width(self, value: float) -> None:
        self._gate_width = max(0.02, min(0.30, float(value)))
        self.update()

    @property
    def roi_top(self) -> float:
        return self._roi_top

    @property
    def roi_bottom(self) -> float:
        return self._roi_bottom

    def _display_geometry(self) -> tuple[int, int, int, int]:
        if self._pixmap.isNull():
            return 0, 0, self.width(), self.height()
        scaled = self._pixmap.scaled(self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        return (self.width() - scaled.width()) // 2, (self.height() - scaled.height()) // 2, scaled.width(), scaled.height()

    def _normalized_position(self, event) -> tuple[float, float]:
        left, top, width, height = self._display_geometry()
        return (
            max(0.0, min(1.0, (event.x() - left) / max(1, width))),
            max(0.0, min(1.0, (event.y() - top) / max(1, height))),
        )

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            x, y = self._normalized_position(event)
            distances = {
                "line": abs(x - self._line_x),
                "top": abs(y - self._roi_top),
                "bottom": abs(y - self._roi_bottom),
            }
            # Require a nearby guide so clicks inside the ROI do not move a line.
            target, distance = min(distances.items(), key=lambda item: item[1])
            if distance > 0.04:
                self._drag_target = ""
                return
            self._drag_target = target
            self._update_drag_position(x, y)

    def mouseMoveEvent(self, event) -> None:
        if event.buttons() & Qt.LeftButton:
            x, y = self._normalized_position(event)
            self._update_drag_position(x, y)

    def _update_drag_position(self, x: float, y: float) -> None:
        if self._drag_target == "top":
            self._roi_top = min(max(0.0, y), self._roi_bottom - 0.05)
        elif self._drag_target == "bottom":
            self._roi_bottom = max(min(1.0, y), self._roi_top + 0.05)
        elif self._drag_target == "line":
            self.set_line_x(x)
            return
        else:
            return
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), Qt.black)
        if not self._pixmap.isNull():
            scaled = self._pixmap.scaled(self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            left = (self.width() - scaled.width()) // 2
            top = (self.height() - scaled.height()) // 2
            painter.drawPixmap(left, top, scaled)
            canvas_width = scaled.width()
            origin_x = left
        else:
            canvas_width = self.width()
            origin_x = 0
        x = origin_x + int(self._line_x * canvas_width)
        half_gate = max(1, int(self._gate_width * canvas_width / 2.0))
        line_top = top if not self._pixmap.isNull() else 0
        line_bottom = (
            top + scaled.height() if not self._pixmap.isNull() else self.height()
        )
        painter.setPen(QPen(Qt.yellow, 2, Qt.DashLine))
        painter.drawLine(x - half_gate, line_top, x - half_gate, line_bottom)
        painter.drawLine(x + half_gate, line_top, x + half_gate, line_bottom)
        painter.setPen(QPen(Qt.red, 3))
        painter.drawLine(x, line_top, x, line_bottom)
        top_y = line_top + int(self._roi_top * (line_bottom - line_top))
        bottom_y = line_top + int(self._roi_bottom * (line_bottom - line_top))
        painter.setPen(QPen(Qt.blue, 2, Qt.DashLine))
        painter.drawLine(origin_x, top_y, origin_x + canvas_width, top_y)
        painter.drawLine(origin_x, bottom_y, origin_x + canvas_width, bottom_y)


class VisualLineCalibrationDialog(QDialog):
    """Draw the finish line on one camera frame and choose forward direction."""

    def __init__(self, source: str, *, line_x: float = 0.50, gate_width: float = 0.08, roi_top: float = 0.08, roi_bottom: float = 0.95, direction: str = "left_to_right", parent=None):
        super().__init__(parent)
        self.setWindowTitle("设置过线辅助终点线")
        self.resize(900, 620)
        self._line_x = max(0.02, min(0.98, float(line_x)))
        self._gate_width = max(0.02, min(0.30, float(gate_width)))
        self._roi_top = max(0.0, min(0.80, float(roi_top)))
        self._roi_bottom = max(self._roi_top + 0.05, min(1.0, float(roi_bottom)))
        self._direction = direction if direction in {"left_to_right", "right_to_left"} else "left_to_right"
        layout = QVBoxLayout(self)
        self._calibration_hint = (
            "红线=正式终点线；黄线=两侧辅助检测线；蓝线=上、下有效检测边界。"
            "拖动红线和蓝线到实际位置，再选择正向。蓝线外不参与检测。"
        )
        self.help_label = QLabel(
            "在画面上拖动红线和蓝线到实际位置，再选择正向。",
            self,
        )
        self.help_label.setText(self._calibration_hint)
        layout.addWidget(self.help_label)
        self.canvas = _LineCanvas(
            self,
            gate_width=self._gate_width,
            roi_top=self._roi_top,
            roi_bottom=self._roi_bottom,
        )
        self.canvas.set_line_x(self._line_x)
        layout.addWidget(self.canvas, 1)
        self.direction_combo = QComboBox(self)
        self.direction_combo.addItem("正向：左 → 右", "left_to_right")
        self.direction_combo.addItem("正向：右 → 左", "right_to_left")
        self.direction_combo.setCurrentIndex(max(0, self.direction_combo.findData(self._direction)))
        layout.addWidget(self.direction_combo)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, parent=self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._worker = _PreviewFrameWorker(source, self)
        self._worker.frame_ready.connect(self.canvas.set_image)
        self._worker.failed.connect(lambda message: self.help_label.setText(str(message)))
        import os
        if os.environ.get("QT_QPA_PLATFORM", "").casefold() != "offscreen":
            track_qthread(self._worker)
            self._worker.start()

    @property
    def line_x(self) -> float:
        return self.canvas.line_x

    @property
    def gate_width(self) -> float:
        return self.canvas._gate_width

    @property
    def direction(self) -> str:
        return str(self.direction_combo.currentData() or "left_to_right")

    @property
    def roi_top(self) -> float:
        return self.canvas.roi_top

    @property
    def roi_bottom(self) -> float:
        return self.canvas.roi_bottom

    def closeEvent(self, event) -> None:
        self._stop_preview_worker()
        super().closeEvent(event)

    def done(self, result: int) -> None:
        self._stop_preview_worker()
        super().done(result)

    def _stop_preview_worker(self) -> None:
        if not self._worker.isRunning():
            return
        self._worker.request_stop()
        if not self._worker.wait(1_000):
            retire_qthread(self._worker)


__all__ = [
    "CrossingConfig",
    "DualGateCrossingDetector",
    "VisualCrossingEvent",
    "VisualCrossingEventStore",
    "VisualCrossingWorker",
    "VisualLineCalibrationDialog",
]
