"""Low-priority continuous video activity overview for manual review."""

from __future__ import annotations

from pathlib import Path
import threading

import cv2
import numpy as np
from PyQt5.QtCore import QThread, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QPainter, QPen
from PyQt5.QtWidgets import QWidget


class VideoActivityWorker(QThread):
    """Measure low-resolution frame change without touching the GUI thread."""

    points_ready = pyqtSignal(object)
    completed = pyqtSignal()

    def __init__(self, video_path: Path, start_ms: int, end_ms: int, parent=None):
        super().__init__(parent)
        self.video_path = Path(video_path)
        self.start_ms = max(0, int(start_ms))
        self.end_ms = max(self.start_ms, int(end_ms))
        self._stop = threading.Event()
        self._paused = threading.Event()

    def request_stop(self) -> None:
        self._stop.set()
        self._paused.clear()

    def set_paused(self, paused: bool) -> None:
        if paused:
            self._paused.set()
        else:
            self._paused.clear()

    def run(self) -> None:
        capture = cv2.VideoCapture(str(self.video_path))
        if not capture.isOpened():
            return
        try:
            fps = max(1.0, float(capture.get(cv2.CAP_PROP_FPS) or 25.0))
            sample_step = max(1, int(round(fps / 4.0)))
            start_frame = max(0, int(round(self.start_ms * fps / 1000.0)))
            end_frame = max(start_frame, int(round(self.end_ms * fps / 1000.0)))
            capture.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
            previous = None
            frame_index = start_frame
            batch: list[tuple[int, float]] = []
            while frame_index <= end_frame and not self._stop.is_set():
                while self._paused.is_set() and not self._stop.is_set():
                    self.msleep(30)
                if self._stop.is_set():
                    break
                ok, frame = capture.read()
                if not ok or frame is None:
                    break
                if (frame_index - start_frame) % sample_step == 0:
                    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                    gray = cv2.resize(gray, (160, 90), interpolation=cv2.INTER_AREA)
                    if previous is not None:
                        difference = cv2.absdiff(gray, previous)
                        score = float(np.mean(difference)) / 255.0
                        position_ms = int(round(frame_index * 1000.0 / fps))
                        batch.append((position_ms, score))
                    previous = gray
                    if len(batch) >= 16:
                        self.points_ready.emit(tuple(batch))
                        batch.clear()
                # Deliberately throttle every decoded frame. Thread priority
                # alone does not limit decoder I/O on all Windows systems.
                self.msleep(2)
                frame_index += 1
            if batch and not self._stop.is_set():
                self.points_ready.emit(tuple(batch))
            if not self._stop.is_set():
                self.completed.emit()
        finally:
            capture.release()


class ActivityTimelineWidget(QWidget):
    """Continuous, non-filtering activity graph; clicking seeks by time."""

    position_selected = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._start_ms = 0
        self._end_ms = 0
        self._points: list[tuple[int, float]] = []
        self._current_ms = -1
        self.setFixedHeight(44)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("画面活动强度（仅用于快速定位，不会隐藏任何时间）")

    def set_range(self, start_ms: int, end_ms: int) -> None:
        self._start_ms = int(start_ms)
        self._end_ms = max(self._start_ms, int(end_ms))
        self._points.clear()
        self.update()

    def append_points(self, points) -> None:
        self._points.extend((int(position), max(0.0, float(score))) for position, score in points)
        self.update()

    def set_current_position(self, position_ms: int) -> None:
        self._current_ms = int(position_ms)
        self.update()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self._end_ms > self._start_ms:
            ratio = max(0.0, min(1.0, event.x() / max(1, self.width() - 1)))
            self.position_selected.emit(int(round(self._start_ms + ratio * (self._end_ms - self._start_ms))))
            event.accept()
            return
        super().mousePressEvent(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#101b25"))
        painter.setPen(QPen(QColor("#334155"), 1))
        painter.drawRect(self.rect().adjusted(0, 0, -1, -1))
        if self._points and self._end_ms > self._start_ms:
            scores = np.asarray([score for _position, score in self._points], dtype=float)
            ceiling = max(0.015, float(np.percentile(scores, 95)))
            painter.setPen(QPen(QColor("#35d0ba"), 1))
            usable_height = max(1, self.height() - 8)
            span = self._end_ms - self._start_ms
            for position, score in self._points:
                x = int((position - self._start_ms) * max(1, self.width() - 1) / span)
                height = int(min(1.0, score / ceiling) * usable_height)
                painter.drawLine(x, self.height() - 4, x, self.height() - 4 - height)
        if self._start_ms <= self._current_ms <= self._end_ms and self._end_ms > self._start_ms:
            x = int((self._current_ms - self._start_ms) * max(1, self.width() - 1) / (self._end_ms - self._start_ms))
            painter.setPen(QPen(QColor("#ffb020"), 2))
            painter.drawLine(x, 1, x, self.height() - 2)
        painter.setPen(QColor("#d5e3ec"))
        painter.drawText(6, 14, "画面活动（点击定位）")
