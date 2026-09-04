"""Judge-oriented playback for recordings stored inside a race directory."""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict, deque
from pathlib import Path
from statistics import median
from typing import Callable, Optional

import cv2
from PyQt5.QtCore import QTimer, Qt, QThread, pyqtSignal
from PyQt5.QtGui import (
    QColor,
    QImage,
    QKeySequence,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPen,
    QPixmap,
    QWheelEvent,
)
from PyQt5.QtWidgets import (
    QButtonGroup,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QShortcut,
    QSizePolicy,
    QSlider,
    QStyle,
    QStyleOptionSlider,
    QVBoxLayout,
    QWidget,
)

from .thread_lifecycle import retire_qthread, track_qthread
from .time_domain import ClockOffsetMs, MediaPositionMs

logger = logging.getLogger("FinishReview.Playback")


SUPPORTED_VIDEO_SUFFIXES = {".mkv", ".mp4", ".avi", ".mov", ".m4v"}


def _open_video_capture(
    video_path: Path,
    capture_factory: Callable[[str], cv2.VideoCapture],
):
    return capture_factory(str(video_path))


def find_recordings(race_dir: Path) -> list[Path]:
    videos_dir = Path(race_dir) / "videos"
    if not videos_dir.is_dir():
        return []

    recordings = []
    for path in videos_dir.iterdir():
        try:
            if (
                path.is_file()
                and path.suffix.lower() in SUPPORTED_VIDEO_SUFFIXES
                and path.stat().st_size > 0
            ):
                recordings.append((path.stat().st_mtime, path))
        except OSError:
            continue
    recordings.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in recordings]


def format_playback_time(milliseconds: int) -> str:
    milliseconds = max(0, int(milliseconds))
    total_seconds, millis = divmod(milliseconds, 1000)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


class PlaybackVideoLabel(QLabel):
    jog_started = pyqtSignal()
    jog_delta_changed = pyqtSignal(int)
    jog_finished = pyqtSignal()
    wheel_jogged = pyqtSignal(int, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._frame: Optional[QPixmap] = None
        self._drag_origin_x: Optional[int] = None
        self._last_drag_frames = 0
        self._jog_frame_span = 1
        self._smooth_scaling = True
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumSize(640, 360)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setStyleSheet("background: #090b0d; border: none;")
        self.setCursor(Qt.OpenHandCursor)
        self.setToolTip("左右拖动画面快速定位；滚轮逐帧前进或后退")

    def set_jog_frame_span(self, frame_count: int) -> None:
        self._jog_frame_span = max(1, int(frame_count))

    def _frame_delta_for_position(self, position_x: int) -> int:
        if self._drag_origin_x is None:
            return 0
        horizontal_delta = int(position_x) - self._drag_origin_x
        return int(
            round(horizontal_delta * self._jog_frame_span / max(1, self.width()))
        )

    def set_frame(self, image: QImage) -> None:
        self._frame = QPixmap.fromImage(image)
        self.setText("")
        self._render_frame()

    def set_smooth_scaling(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self._smooth_scaling:
            return
        self._smooth_scaling = enabled
        self._render_frame()

    def clear_frame(self, message: str = "") -> None:
        self._frame = None
        self.clear()
        self.setText(str(message or ""))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._render_frame()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self._drag_origin_x = event.x()
            self._last_drag_frames = 0
            self.setCursor(Qt.ClosedHandCursor)
            self.jog_started.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_origin_x is not None:
            frame_delta = self._frame_delta_for_position(event.x())
            if frame_delta != self._last_drag_frames:
                self._last_drag_frames = frame_delta
                self.jog_delta_changed.emit(frame_delta)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton and self._drag_origin_x is not None:
            self._drag_origin_x = None
            self.setCursor(Qt.OpenHandCursor)
            self.jog_finished.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event: QWheelEvent) -> None:
        steps = int(event.angleDelta().y() / 120)
        if steps:
            self.wheel_jogged.emit(steps, int(event.modifiers()))
            event.accept()
            return
        super().wheelEvent(event)

    def _render_frame(self) -> None:
        if self._frame is None or self._frame.isNull():
            return
        self.setPixmap(
            self._frame.scaled(
                self.size(),
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation
                if self._smooth_scaling
                else Qt.FastTransformation,
            )
        )


class SpringShuttleSlider(QWidget):
    speed_changed = pyqtSignal(float)
    SPEEDS = {
        -5: -4.0,
        -4: -2.0,
        -3: -1.0,
        -2: -0.5,
        -1: -0.25,
        0: 0.0,
        1: 0.25,
        2: 0.5,
        3: 1.0,
        4: 2.0,
        5: 4.0,
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self._value = 0
        self._display_speed = 0.0
        self._buttons = {}
        self.setMinimumWidth(340)
        self.setMaximumWidth(460)
        self.setFixedHeight(48)
        self.setAccessibleName("回放速度")
        self.setToolTip("选择正向回放速度")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self._button_group = QButtonGroup(self)
        self._button_group.setExclusive(True)

        button_specs = (
            (1, "0.25x", "0.25 倍速回放"),
            (2, "0.5x", "0.5 倍速回放"),
            (3, "1x", "正常速度回放"),
            (4, "2x", "2 倍速回放"),
        )
        for value, text, tooltip in button_specs:
            button = QPushButton(text, self)
            button.setObjectName("shuttleForward")
            button.setToolTip(tooltip)
            button.setAccessibleName(tooltip)
            button.setCheckable(True)
            button.setProperty("shuttleValue", value)
            button.setMinimumWidth(72)
            button.setFixedHeight(42)
            button.clicked.connect(lambda _checked=False, selected=value: self.setValue(selected))
            self._button_group.addButton(button)
            self._buttons[value] = button
            layout.addWidget(button, 1)

        self.setStyleSheet(
            "QPushButton { min-height: 0; padding: 0 7px; border: 1px solid #53606d; "
            "border-radius: 4px; background: #29313a; color: #e9eef2; "
            "font-size: 12px; font-weight: 700; }"
            "QPushButton:hover { background: #35414c; border-color: #8795a3; }"
            "QPushButton#shuttleForward:checked { background: #24754f; "
            "border-color: #65bd8d; color: #ffffff; }"
        )
        self._sync_button_state()

    @classmethod
    def speed_for_value(cls, value: int) -> float:
        return cls.SPEEDS.get(int(value), 0.0)

    @classmethod
    def value_for_speed(cls, speed: float) -> int:
        return min(cls.SPEEDS, key=lambda value: abs(cls.SPEEDS[value] - float(speed)))

    def set_speed(self, speed: float, *, emit: bool = True) -> None:
        self.setValue(self.value_for_speed(speed), emit=emit)

    def value(self) -> int:
        return self._value

    def setValue(self, value: int, *, emit: bool = True) -> None:
        value = max(min(self.SPEEDS), min(max(self.SPEEDS), int(value)))
        if value == self._value:
            self._display_speed = self.speed_for_value(value)
            self._sync_button_state()
            return
        self._value = value
        self._display_speed = self.speed_for_value(value)
        self._sync_button_state()
        if emit:
            self.speed_changed.emit(self._display_speed)

    def set_display_speed(self, speed: float) -> None:
        self._display_speed = float(speed)
        self._sync_button_state()

    def _sync_button_state(self) -> None:
        selected = (
            self.value_for_speed(self._display_speed)
            if abs(self._display_speed) > 0.01
            else self._value
        )
        button = self._buttons.get(selected)
        self._button_group.setExclusive(False)
        for candidate in self._buttons.values():
            candidate.setChecked(candidate is button)
        self._button_group.setExclusive(True)


class TargetTimelineSlider(QSlider):
    """Timeline slider with click-to-seek and a passage target marker."""

    def __init__(self, orientation, parent=None):
        super().__init__(orientation, parent)
        self.target_position_ms: Optional[MediaPositionMs] = None

    def set_target_position(
        self,
        position_ms: Optional[MediaPositionMs | int],
    ) -> None:
        self.target_position_ms = (
            None
            if position_ms is None
            else MediaPositionMs(max(0, int(position_ms)))
        )
        self.update()

    def _value_from_position(self, position_x: int) -> int:
        option = QStyleOptionSlider()
        self.initStyleOption(option)
        groove = self.style().subControlRect(
            QStyle.CC_Slider,
            option,
            QStyle.SC_SliderGroove,
            self,
        )
        span = max(1, groove.width())
        position = max(0, min(int(position_x) - groove.left(), span))
        return QStyle.sliderValueFromPosition(
            self.minimum(),
            self.maximum(),
            position,
            span,
            option.upsideDown,
        )

    def _move_to_position(self, position_x: int) -> None:
        value = self._value_from_position(position_x)
        self.setValue(value)
        self.sliderMoved.emit(value)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self.setFocus()
            self.setSliderDown(True)
            self._move_to_position(event.x())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self.isSliderDown() and event.buttons() & Qt.LeftButton:
            self._move_to_position(event.x())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton and self.isSliderDown():
            self._move_to_position(event.x())
            self.setSliderDown(False)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        target = self.target_position_ms
        if (
            target is None
            or self.maximum() <= self.minimum()
            or target < self.minimum()
            or target > self.maximum()
        ):
            return

        option = QStyleOptionSlider()
        self.initStyleOption(option)
        groove = self.style().subControlRect(
            QStyle.CC_Slider,
            option,
            QStyle.SC_SliderGroove,
            self,
        )
        position = QStyle.sliderPositionFromValue(
            self.minimum(),
            self.maximum(),
            target,
            max(1, groove.width()),
            option.upsideDown,
        )
        x = groove.left() + position
        painter = QPainter(self)
        painter.setPen(QPen(QColor("#ffb020"), 2))
        painter.drawLine(x, groove.top() - 5, x, groove.bottom() + 5)


class VideoPlaybackWorker(QThread):
    metadata_ready = pyqtSignal(int, float, int, int, int)
    frame_ready = pyqtSignal(QImage, int, int)
    full_resolution_ready = pyqtSignal(QImage, int, int)
    playback_finished = pyqtSignal()
    step_boundary_reached = pyqtSignal(int)
    playback_error = pyqtSignal(str)

    REVERSE_WINDOW_SECONDS = 0.5
    MIN_REVERSE_WINDOW_FRAMES = 8
    REVERSE_PREFETCH_TRIGGER_RATIO = 0.5
    FORWARD_PREFETCH_IDLE_SECONDS = 0.12
    DEFAULT_CACHE_BYTES = 64 * 1024 * 1024
    PERFORMANCE_LOG_INTERVAL = 25

    def __init__(
        self,
        video_path: Path,
        parent=None,
        *,
        capture_factory: Callable[[str], cv2.VideoCapture] = cv2.VideoCapture,
        reverse_prefetch: bool = True,
        idle_prefetch: bool = True,
    ):
        super().__init__(parent)
        self.video_path = Path(video_path)
        self._capture_factory = capture_factory
        self._reverse_prefetch_enabled = bool(reverse_prefetch)
        self._idle_prefetch_enabled = bool(idle_prefetch)
        self._condition = threading.Condition()
        self._cache_lock = threading.Lock()
        self._stop_requested = False
        self._playing = True
        self._speed = 1.0
        self._direction = 1
        self._seek_frame: Optional[tuple[int, int]] = None
        self._full_resolution_frame: Optional[tuple[int, int]] = None
        self._step_frame: Optional[tuple[int, int]] = None
        self._navigation_frame_index = -1
        self._request_generation = 0
        self._anchor_reset = True
        self._current_frame_index = -1
        self._current_position_ms = 0
        self._duration_ms = 0
        self._frame_count = 0
        self._fps = 25.0
        self._sequential_capture = self.video_path.suffix.lower() == ".m3u8"
        self._frame_cache: OrderedDict[int, QImage] = OrderedDict()
        self._frame_cache_bytes = 0
        self._cache_generation = 0
        self._max_cache_bytes = self.DEFAULT_CACHE_BYTES
        self._reverse_window_frames = 25
        self._reverse_current_window: Optional[tuple[int, int, int]] = None
        self._reverse_prefetched_window: Optional[tuple[int, int, int]] = None
        self._reverse_prefetch_task: Optional[tuple[int, int, int, int]] = None
        self._reverse_prefetch_active: Optional[tuple[int, int, int, int]] = None
        self._reverse_prefetch_serial = 0
        self._reverse_prefetch_shutdown = False
        self._reverse_prefetch_thread: Optional[threading.Thread] = None
        self._navigation_request: Optional[tuple[int, int, str, float]] = None
        self._navigation_latency_ms: deque[float] = deque(maxlen=100)
        self._navigation_decode_ms: deque[float] = deque(maxlen=100)
        self._navigation_samples = 0
        self._navigation_cache_hits = 0
        self._navigation_boundary_misses = 0

    @property
    def current_position_ms(self) -> MediaPositionMs:
        with self._condition:
            return MediaPositionMs(self._current_position_ms)

    @property
    def current_frame_index(self) -> int:
        with self._condition:
            return self._current_frame_index

    def play(self) -> None:
        self.set_shuttle_speed(1.0)

    def pause(self) -> None:
        with self._condition:
            self._request_generation += 1
            self._playing = False
            self._reset_reverse_prefetch_locked()
            self._step_frame = None
            self._navigation_frame_index = self._current_frame_index
            self._full_resolution_frame = None
            self._condition.notify_all()

    def set_shuttle_speed(self, speed: float) -> None:
        speed = max(-4.0, min(4.0, float(speed)))
        with self._condition:
            self._request_generation += 1
            self._reset_reverse_prefetch_locked()
            self._step_frame = None
            self._navigation_frame_index = self._current_frame_index
            self._full_resolution_frame = None
            if abs(speed) < 0.01:
                self._playing = False
            else:
                self._direction = 1 if speed > 0 else -1
                self._speed = abs(speed)
                self._playing = True
            self._anchor_reset = True
            self._condition.notify_all()

    def seek_frame(self, frame_index: int) -> None:
        with self._condition:
            upper = self._frame_count - 1 if self._frame_count > 0 else int(frame_index)
            self._request_generation += 1
            self._reset_reverse_prefetch_locked()
            self._seek_frame = (
                max(0, min(int(frame_index), upper)),
                self._request_generation,
            )
            self._step_frame = None
            self._navigation_frame_index = self._seek_frame[0]
            self._set_navigation_request_locked(
                "seek",
                self._seek_frame[0],
            )
            self._full_resolution_frame = None
            self._anchor_reset = True
            self._condition.notify_all()

    def seek(self, milliseconds: int) -> None:
        self.seek_frame(int(max(0, milliseconds) * self._fps / 1000.0))

    def seek_and_play(self, milliseconds: int, speed: float = 1.0) -> None:
        frame_index = int(max(0, milliseconds) * self._fps / 1000.0)
        speed = max(-4.0, min(4.0, float(speed)))
        with self._condition:
            upper = self._frame_count - 1 if self._frame_count > 0 else frame_index
            self._request_generation += 1
            self._reset_reverse_prefetch_locked()
            self._seek_frame = (
                max(0, min(frame_index, upper)),
                self._request_generation,
            )
            self._step_frame = None
            self._navigation_frame_index = self._seek_frame[0]
            self._set_navigation_request_locked(
                "seek_play",
                self._seek_frame[0],
            )
            self._full_resolution_frame = None
            self._direction = 1 if speed >= 0 else -1
            self._speed = max(0.01, abs(speed))
            self._playing = True
            self._anchor_reset = True
            self._condition.notify_all()

    def seek_preview(self, milliseconds: int) -> None:
        self.seek(milliseconds)

    def jump(self, delta_ms: ClockOffsetMs | int) -> None:
        delta_frames = int(round(int(delta_ms) * self._fps / 1000.0))
        self.seek_frame(self.current_frame_index + delta_frames)

    def step(self, frame_delta: int) -> None:
        frame_delta = int(frame_delta)
        if not frame_delta:
            return
        with self._condition:
            self._request_generation += 1
            self._reset_reverse_prefetch_locked()
            self._playing = False
            self._seek_frame = None
            self._full_resolution_frame = None
            base_frame = self._navigation_frame_index
            if base_frame < 0:
                base_frame = self._current_frame_index
            target = self._clamp_frame(max(0, base_frame) + frame_delta)
            self._navigation_frame_index = target
            self._step_frame = (target, self._request_generation)
            self._set_navigation_request_locked("step", target)
            self._condition.notify_all()

    def _set_navigation_request_locked(self, kind: str, target: int) -> None:
        self._navigation_request = (
            self._request_generation,
            int(target),
            str(kind),
            time.perf_counter(),
        )

    @staticmethod
    def _percentile(samples: deque[float], percentile: float) -> float:
        if not samples:
            return 0.0
        ordered = sorted(samples)
        index = round((len(ordered) - 1) * float(percentile))
        return ordered[max(0, min(index, len(ordered) - 1))]

    def _record_navigation_result(
        self,
        target: int,
        generation: Optional[int],
        *,
        cache_hit: bool,
        boundary_miss: bool,
        decode_ms: float,
    ) -> None:
        with self._condition:
            request = self._navigation_request
            if (
                request is None
                or generation is None
                or request[0] != generation
                or request[1] != int(target)
            ):
                return
            self._navigation_request = None
            latency_ms = (time.perf_counter() - request[3]) * 1000.0
            self._navigation_latency_ms.append(latency_ms)
            self._navigation_decode_ms.append(max(0.0, float(decode_ms)))
            self._navigation_samples += 1
            self._navigation_cache_hits += int(cache_hit)
            self._navigation_boundary_misses += int(boundary_miss)
            should_log = (
                self._navigation_samples % self.PERFORMANCE_LOG_INTERVAL == 0
            )
            kind = request[2]
        if should_log:
            self._log_navigation_summary(kind=kind, target=target)

    def _log_navigation_summary(self, *, kind: str = "", target: int = -1) -> None:
        with self._condition:
            samples = self._navigation_samples
            if not samples:
                return
            hit_rate = self._navigation_cache_hits * 100.0 / samples
            latency_p50 = self._percentile(self._navigation_latency_ms, 0.50)
            latency_p95 = self._percentile(self._navigation_latency_ms, 0.95)
            decode_p95 = self._percentile(self._navigation_decode_ms, 0.95)
            boundary_misses = self._navigation_boundary_misses
        logger.info(
            "Playback navigation path=%s samples=%d cache_hit=%.1f%% "
            "latency_p50=%.1fms latency_p95=%.1fms decode_p95=%.1fms "
            "boundary_misses=%d last=%s:%d",
            self.video_path.name,
            samples,
            hit_rate,
            latency_p50,
            latency_p95,
            decode_p95,
            boundary_misses,
            kind,
            int(target),
        )

    def set_idle_prefetch_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        with self._condition:
            if enabled == self._idle_prefetch_enabled:
                return
            self._request_generation += 1
            self._idle_prefetch_enabled = enabled
            self._reset_reverse_prefetch_locked()
            self._condition.notify_all()

    def request_full_resolution(self, frame_index: Optional[int] = None) -> None:
        with self._condition:
            target = self._current_frame_index if frame_index is None else int(frame_index)
            if target < 0:
                return
            self._full_resolution_frame = (
                self._clamp_frame(target),
                self._request_generation,
            )
            self._condition.notify_all()

    def request_stop(self) -> None:
        """Request cooperative shutdown and wake every worker wait."""

        self.requestInterruption()
        with self._condition:
            self._stop_requested = True
            self._reset_reverse_prefetch_locked()
            self._condition.notify_all()

    def stop(self) -> None:
        """Backward-compatible alias for the shared cancellation protocol."""

        self.request_stop()

    def release_cache(self) -> None:
        with self._condition:
            self._request_generation += 1
            self._cache_generation += 1
            self._reset_reverse_prefetch_locked()
            self._step_frame = None
            self._navigation_frame_index = self._current_frame_index
            self._full_resolution_frame = None
            self._condition.notify_all()
        self._clear_frame_cache()

    def park_cache(self) -> None:
        with self._condition:
            self._request_generation += 1
            self._reset_reverse_prefetch_locked()
            self._step_frame = None
            self._navigation_frame_index = self._current_frame_index
            self._full_resolution_frame = None
            current_frame_index = self._current_frame_index
            self._condition.notify_all()
        if current_frame_index < 0:
            self._clear_frame_cache()
            return
        self._trim_cache_to_range(
            max(0, current_frame_index - self._reverse_window_frames + 1),
            self._clamp_frame(current_frame_index + self._reverse_window_frames),
        )

    def _reset_reverse_prefetch_locked(self) -> None:
        self._reverse_prefetch_serial += 1
        self._reverse_prefetch_task = None
        self._reverse_current_window = None
        self._reverse_prefetched_window = None

    def _image_from_frame(self, frame, *, full_resolution: bool = False) -> QImage:
        height, width = frame.shape[:2]
        scale = (
            1.0
            if full_resolution
            else min(1.0, 1280.0 / max(1, width), 720.0 / max(1, height))
        )
        if scale < 1.0:
            frame = cv2.resize(
                frame,
                (max(1, int(width * scale)), max(1, int(height * scale))),
                interpolation=cv2.INTER_AREA,
            )
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        height, width, channels = rgb.shape
        return QImage(
            rgb.data,
            width,
            height,
            channels * width,
            QImage.Format_RGB888,
        ).copy()

    def _cache_image(self, frame_index: int, image: QImage) -> None:
        with self._cache_lock:
            previous = self._frame_cache.pop(frame_index, None)
            if previous is not None:
                self._frame_cache_bytes -= previous.byteCount()
            self._frame_cache[frame_index] = image
            self._frame_cache_bytes += image.byteCount()
            while self._frame_cache and self._frame_cache_bytes > self._max_cache_bytes:
                _, evicted = self._frame_cache.popitem(last=False)
                self._frame_cache_bytes -= evicted.byteCount()

    def _cached_image(self, frame_index: int) -> Optional[QImage]:
        with self._cache_lock:
            image = self._frame_cache.get(frame_index)
            if image is not None:
                self._frame_cache.move_to_end(frame_index)
            return image

    def _clear_frame_cache(self) -> None:
        with self._cache_lock:
            self._frame_cache.clear()
            self._frame_cache_bytes = 0

    def _trim_cache_to_range(self, start: int, end: int) -> None:
        with self._cache_lock:
            for frame_index in tuple(self._frame_cache):
                if start <= frame_index <= end:
                    continue
                image = self._frame_cache.pop(frame_index)
                self._frame_cache_bytes -= image.byteCount()

    def _configure_reverse_window(self, width: int, height: int) -> None:
        scale = min(1.0, 1280.0 / max(1, width), 720.0 / max(1, height))
        preview_width = max(1, int(width * scale))
        preview_height = max(1, int(height * scale))
        estimated_frame_bytes = max(1, preview_width * preview_height * 3)
        cache_frame_capacity = max(1, self._max_cache_bytes // estimated_frame_bytes)
        window_capacity = max(1, cache_frame_capacity // 2)
        desired_frames = max(
            self.MIN_REVERSE_WINDOW_FRAMES,
            int(round(self._fps * self.REVERSE_WINDOW_SECONDS)),
        )
        self._reverse_window_frames = max(
            1,
            min(desired_frames, window_capacity),
        )

    @staticmethod
    def _reported_decoded_frame_index(capture, expected: int) -> int:
        try:
            reported_next = float(capture.get(cv2.CAP_PROP_POS_FRAMES) or 0.0)
        except (TypeError, ValueError):
            return int(expected)
        rounded_next = round(reported_next)
        if reported_next <= 0 or abs(reported_next - rounded_next) > 0.01:
            return int(expected)
        return max(0, int(rounded_next) - 1)

    def _decode_request_cancelled(self, generation: Optional[int]) -> bool:
        with self._condition:
            return self._stop_requested or (
                generation is not None
                and generation != self._request_generation
            )

    def _idle_prefetch_cancelled(self, generation: int) -> bool:
        with self._condition:
            return (
                self._stop_requested
                or not self._idle_prefetch_enabled
                or not self._sequential_capture
                or self._playing
                or generation != self._request_generation
                or self._seek_frame is not None
                or self._step_frame is not None
                or self._full_resolution_frame is not None
            )

    def _cache_image_if_generation(
        self,
        frame_index: int,
        image: QImage,
        cache_generation: int,
    ) -> bool:
        with self._condition:
            if (
                self._stop_requested
                or cache_generation != self._cache_generation
            ):
                return False
            self._cache_image(frame_index, image)
            return True

    def _playlist_duration_ms(self) -> Optional[int]:
        if not self._sequential_capture:
            return None
        try:
            lines = self.video_path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            return None

        duration_seconds = 0.0
        found_duration = False
        for line in lines:
            if not line.startswith("#EXTINF:"):
                continue
            try:
                value = float(line.split(":", 1)[1].split(",", 1)[0])
            except (IndexError, ValueError):
                return None
            if value <= 0:
                return None
            duration_seconds += value
            found_duration = True
        if not found_duration:
            return None
        return max(1, int(round(duration_seconds * 1000.0)))

    def _probe_sequential_fps(self, fallback_fps: float) -> float:
        if not self._sequential_capture:
            return fallback_fps
        capture = _open_video_capture(self.video_path, self._capture_factory)
        try:
            if not capture or not capture.isOpened():
                return fallback_fps
            timestamps = []
            for _ in range(8):
                ok, _frame = capture.read()
                if not ok:
                    break
                timestamps.append(
                    float(capture.get(cv2.CAP_PROP_POS_MSEC) or 0.0)
                )
            intervals = [
                current - previous
                for previous, current in zip(timestamps, timestamps[1:])
                if 0.5 <= current - previous <= 1000.0
            ]
            if not intervals:
                return fallback_fps
            probed_fps = 1000.0 / float(median(intervals))
            if not 1.0 <= probed_fps <= 240.0:
                return fallback_fps
            return probed_fps
        except (TypeError, ValueError):
            return fallback_fps
        finally:
            if capture:
                capture.release()

    def _position_capture(
        self,
        capture,
        target: int,
        capture_next_frame: int,
    ) -> tuple[bool, int]:
        if target == capture_next_frame:
            return True, capture_next_frame

        if self._sequential_capture:
            if capture_next_frame < 0 or target < capture_next_frame:
                if not capture.set(cv2.CAP_PROP_POS_FRAMES, 0):
                    return False, capture_next_frame
                capture_next_frame = 0
            forward_gap = target - capture_next_frame
            for _ in range(max(0, forward_gap)):
                if not capture.grab():
                    return False, capture_next_frame
            return True, target

        forward_gap = target - capture_next_frame
        if capture_next_frame >= 0 and 0 < forward_gap <= 12:
            for _ in range(forward_gap):
                if not capture.grab():
                    return False, capture_next_frame
            return True, target
        if not capture.set(cv2.CAP_PROP_POS_FRAMES, target):
            return False, capture_next_frame
        return True, target

    def _start_reverse_prefetcher(self) -> None:
        if not self._reverse_prefetch_enabled or self._sequential_capture:
            return
        self._reverse_prefetch_shutdown = False
        thread = threading.Thread(
            target=self._run_reverse_prefetcher,
            name=f"reverse-prefetch-{self.video_path.name}",
            daemon=True,
        )
        self._reverse_prefetch_thread = thread
        thread.start()

    def _stop_reverse_prefetcher(self) -> None:
        with self._condition:
            self._reverse_prefetch_shutdown = True
            self._reset_reverse_prefetch_locked()
            self._condition.notify_all()
        thread = self._reverse_prefetch_thread
        if thread is not None:
            thread.join(timeout=1.0)
        self._reverse_prefetch_thread = None

    def _run_reverse_prefetcher(self) -> None:
        capture = None
        try:
            while True:
                with self._condition:
                    while (
                        self._reverse_prefetch_task is None
                        and not self._stop_requested
                        and not self._reverse_prefetch_shutdown
                    ):
                        self._condition.wait(timeout=0.1)
                    if self._stop_requested or self._reverse_prefetch_shutdown:
                        return
                    task = self._reverse_prefetch_task
                    self._reverse_prefetch_task = None
                    self._reverse_prefetch_active = task
                if task is None:
                    continue
                window_start, window_end, generation, serial = task
                if capture is None:
                    capture = _open_video_capture(
                        self.video_path,
                        self._capture_factory,
                    )
                    if not capture or not capture.isOpened():
                        with self._condition:
                            if self._reverse_prefetch_active == task:
                                self._reverse_prefetch_active = None
                            self._reverse_prefetch_enabled = False
                            self._condition.notify_all()
                        return
                if not capture.set(cv2.CAP_PROP_POS_FRAMES, window_start):
                    with self._condition:
                        if self._reverse_prefetch_active == task:
                            self._reverse_prefetch_active = None
                        self._condition.notify_all()
                    continue

                decoded: list[tuple[int, QImage]] = []
                for frame_index in range(window_start, window_end + 1):
                    with self._condition:
                        if (
                            self._stop_requested
                            or self._reverse_prefetch_shutdown
                            or serial != self._reverse_prefetch_serial
                            or generation != self._request_generation
                        ):
                            decoded = []
                            break
                    ok, frame = capture.read()
                    if not ok:
                        decoded = []
                        break
                    actual_frame_index = self._reported_decoded_frame_index(
                        capture,
                        frame_index,
                    )
                    if self._decode_request_cancelled(generation):
                        decoded = []
                        break
                    if actual_frame_index != frame_index:
                        decoded = []
                        break
                    decoded.append((frame_index, self._image_from_frame(frame)))

                with self._condition:
                    if (
                        decoded
                        and serial == self._reverse_prefetch_serial
                        and generation == self._request_generation
                        and not self._stop_requested
                        and not self._reverse_prefetch_shutdown
                    ):
                        for frame_index, image in decoded:
                            self._cache_image(frame_index, image)
                        self._reverse_prefetched_window = (
                            window_start,
                            window_end,
                            generation,
                        )
                    if self._reverse_prefetch_active == task:
                        self._reverse_prefetch_active = None
                    self._condition.notify_all()
        except Exception:
            with self._condition:
                self._reverse_prefetch_active = None
                self._reverse_prefetch_task = None
                self._reverse_prefetch_enabled = False
                self._condition.notify_all()
        finally:
            if capture:
                capture.release()

    def _set_reverse_current_window(
        self,
        window_start: int,
        window_end: int,
        generation: Optional[int],
    ) -> None:
        if generation is None:
            return
        with self._condition:
            if generation != self._request_generation:
                return
            self._reverse_current_window = (
                int(window_start),
                int(window_end),
                generation,
            )
        self._trim_cache_to_range(window_start, window_end)

    def _update_reverse_window_for_target(
        self,
        target: int,
        generation: Optional[int],
    ) -> None:
        if generation is None:
            return
        trim_range: Optional[tuple[int, int]] = None
        with self._condition:
            prefetched = self._reverse_prefetched_window
            if (
                prefetched is not None
                and prefetched[2] == generation
                and prefetched[0] <= target <= prefetched[1]
            ):
                self._reverse_current_window = prefetched
                self._reverse_prefetched_window = None
                trim_range = (prefetched[0], prefetched[1])
            else:
                current = self._reverse_current_window
                if (
                    current is None
                    or current[2] != generation
                    or not current[0] <= target <= current[1]
                ):
                    self._reverse_current_window = (target, target, generation)
                    trim_range = (target, target)
        if trim_range is not None:
            self._trim_cache_to_range(*trim_range)

    def _maybe_schedule_reverse_prefetch(
        self,
        target: int,
        generation: Optional[int],
    ) -> None:
        if (
            not self._reverse_prefetch_enabled
            or self._sequential_capture
            or generation is None
        ):
            return
        self._update_reverse_window_for_target(target, generation)
        with self._condition:
            current = self._reverse_current_window
            if current is None or current[2] != generation:
                return
            window_start, window_end, _ = current
            total_frames = max(1, window_end - window_start + 1)
            remaining_frames = max(0, target - window_start + 1)
            trigger_frames = max(
                1,
                int(round(total_frames * self.REVERSE_PREFETCH_TRIGGER_RATIO)),
            )
            if remaining_frames > trigger_frames or window_start <= 0:
                return
            if (
                self._reverse_prefetch_task is not None
                or self._reverse_prefetch_active is not None
                or self._reverse_prefetched_window is not None
            ):
                return
            next_end = window_start - 1
            next_start = max(0, next_end - self._reverse_window_frames + 1)
            self._reverse_prefetch_serial += 1
            self._reverse_prefetch_task = (
                next_start,
                next_end,
                generation,
                self._reverse_prefetch_serial,
            )
            self._condition.notify_all()

    def _emit_image(
        self,
        image: QImage,
        frame_index: int,
        *,
        generation: Optional[int] = None,
    ) -> bool:
        position_ms = int(max(0, frame_index) * 1000.0 / self._fps)
        with self._condition:
            if (
                self._stop_requested
                or (
                    generation is not None
                    and generation != self._request_generation
                )
            ):
                return False
            self._current_frame_index = frame_index
            self._current_position_ms = position_ms
            if self._seek_frame is None and self._step_frame is None:
                self._navigation_frame_index = frame_index
        self.frame_ready.emit(image, position_ms, frame_index)
        return True

    def _decode_target(
        self,
        capture,
        target: int,
        capture_next_frame: int,
        *,
        generation: Optional[int] = None,
        reverse_window: bool = False,
    ) -> tuple[bool, int]:
        decode_started = time.perf_counter()
        if self._decode_request_cancelled(generation):
            return True, capture_next_frame
        cached = self._cached_image(target)
        if cached is not None:
            emitted = self._emit_image(cached, target, generation=generation)
            if emitted:
                self._record_navigation_result(
                    target,
                    generation,
                    cache_hit=True,
                    boundary_miss=False,
                    decode_ms=(time.perf_counter() - decode_started) * 1000.0,
                )
            if emitted and reverse_window:
                self._maybe_schedule_reverse_prefetch(target, generation)
            return True, capture_next_frame

        if self._sequential_capture:
            boundary_miss = target < capture_next_frame
            result = self._decode_sequential_target(
                capture,
                target,
                capture_next_frame,
                generation=generation,
            )
            if result[0] and not self._decode_request_cancelled(generation):
                self._record_navigation_result(
                    target,
                    generation,
                    cache_hit=False,
                    boundary_miss=boundary_miss,
                    decode_ms=(time.perf_counter() - decode_started) * 1000.0,
                )
            return result

        if reverse_window:
            result = self._decode_reverse_window(
                capture,
                target,
                capture_next_frame,
                generation=generation,
            )
            if result[0] and not self._decode_request_cancelled(generation):
                self._record_navigation_result(
                    target,
                    generation,
                    cache_hit=False,
                    boundary_miss=True,
                    decode_ms=(time.perf_counter() - decode_started) * 1000.0,
                )
            return result

        positioned, capture_next_frame = self._position_capture(
            capture,
            target,
            capture_next_frame,
        )
        if not positioned:
            return False, capture_next_frame

        ok, frame = capture.read()
        if not ok:
            return False, capture_next_frame
        if self._sequential_capture:
            actual_frame_index = target
            capture_next_frame = target + 1
        else:
            actual_frame_index = self._reported_decoded_frame_index(capture, target)
            capture_next_frame = int(
                capture.get(cv2.CAP_PROP_POS_FRAMES) or actual_frame_index + 1
            )
        if self._decode_request_cancelled(generation):
            return True, capture_next_frame
        if actual_frame_index != target:
            return False, capture_next_frame
        image = self._image_from_frame(frame)
        self._cache_image(target, image)
        emitted = self._emit_image(image, target, generation=generation)
        if emitted:
            self._record_navigation_result(
                target,
                generation,
                cache_hit=False,
                boundary_miss=False,
                decode_ms=(time.perf_counter() - decode_started) * 1000.0,
            )
        return True, capture_next_frame

    def _decode_sequential_target(
        self,
        capture,
        target: int,
        capture_next_frame: int,
        *,
        generation: Optional[int],
    ) -> tuple[bool, int]:
        if target < capture_next_frame:
            if not capture.set(cv2.CAP_PROP_POS_FRAMES, 0):
                return False, capture_next_frame
            capture_next_frame = 0

        cache_start = max(
            capture_next_frame,
            target - self._reverse_window_frames + 1,
        )
        while capture_next_frame < cache_start:
            if self._decode_request_cancelled(generation):
                return True, capture_next_frame
            if not capture.grab():
                return False, capture_next_frame
            capture_next_frame += 1

        target_image = None
        while capture_next_frame <= target:
            if self._decode_request_cancelled(generation):
                return True, capture_next_frame
            frame_index = capture_next_frame
            ok, frame = capture.read()
            if not ok:
                return False, capture_next_frame
            capture_next_frame = frame_index + 1
            if self._decode_request_cancelled(generation):
                return True, capture_next_frame
            image = self._image_from_frame(frame)
            self._cache_image(frame_index, image)
            if frame_index == target:
                target_image = image

        if target_image is None:
            target_image = self._cached_image(target)
        if target_image is None:
            return False, capture_next_frame
        self._emit_image(target_image, target, generation=generation)
        return True, capture_next_frame

    def _prefetch_sequential_forward(
        self,
        capture,
        capture_next_frame: int,
        current_frame_index: int,
        *,
        generation: int,
    ) -> int:
        if current_frame_index < 0 or self._idle_prefetch_cancelled(generation):
            return capture_next_frame
        with self._condition:
            cache_generation = self._cache_generation

        prefetch_end = self._clamp_frame(
            current_frame_index + self._reverse_window_frames
        )
        while capture_next_frame <= prefetch_end:
            if self._idle_prefetch_cancelled(generation):
                break
            frame_index = capture_next_frame
            ok, frame = capture.read()
            if not ok:
                break
            capture_next_frame = frame_index + 1
            if frame_index > current_frame_index:
                self._cache_image_if_generation(
                    frame_index,
                    self._image_from_frame(frame),
                    cache_generation,
                )
            if self._idle_prefetch_cancelled(generation):
                break
        return capture_next_frame

    def _decode_reverse_window(
        self,
        capture,
        target: int,
        capture_next_frame: int,
        *,
        generation: Optional[int],
    ) -> tuple[bool, int]:
        with self._condition:
            if (
                generation is not None
                and generation != self._request_generation
            ):
                return True, capture_next_frame
            self._reverse_prefetch_serial += 1
            self._reverse_prefetch_task = None
            self._reverse_current_window = None
            self._reverse_prefetched_window = None
        window_start = max(0, target - self._reverse_window_frames + 1)
        positioned, capture_next_frame = self._position_capture(
            capture,
            window_start,
            capture_next_frame,
        )
        if not positioned:
            return False, capture_next_frame

        for frame_index in range(window_start, target + 1):
            with self._condition:
                if (
                    generation is not None
                    and generation != self._request_generation
                ):
                    return True, capture_next_frame
            ok, frame = capture.read()
            if not ok:
                return False, capture_next_frame
            if self._sequential_capture:
                actual_frame_index = frame_index
                capture_next_frame = frame_index + 1
            else:
                actual_frame_index = self._reported_decoded_frame_index(
                    capture,
                    frame_index,
                )
                capture_next_frame = int(
                    capture.get(cv2.CAP_PROP_POS_FRAMES) or actual_frame_index + 1
                )
            if self._decode_request_cancelled(generation):
                return True, capture_next_frame
            if actual_frame_index != frame_index:
                return False, capture_next_frame
            self._cache_image(frame_index, self._image_from_frame(frame))

        image = self._cached_image(target)
        if image is None:
            return False, capture_next_frame
        self._set_reverse_current_window(window_start, target, generation)
        self._emit_image(image, target, generation=generation)
        return True, capture_next_frame

    def _decode_full_resolution(
        self,
        capture,
        target: int,
        capture_next_frame: int,
        *,
        generation: int,
    ) -> tuple[bool, int]:
        with self._condition:
            if generation != self._request_generation:
                return True, capture_next_frame
        positioned, capture_next_frame = self._position_capture(
            capture,
            target,
            capture_next_frame,
        )
        if not positioned:
            return False, capture_next_frame
        ok, frame = capture.read()
        if not ok:
            return False, capture_next_frame
        if self._sequential_capture:
            actual_frame_index = target
            capture_next_frame = target + 1
        else:
            actual_frame_index = self._reported_decoded_frame_index(capture, target)
            capture_next_frame = int(
                capture.get(cv2.CAP_PROP_POS_FRAMES) or actual_frame_index + 1
            )
        if self._decode_request_cancelled(generation):
            return True, capture_next_frame
        if actual_frame_index != target:
            return False, capture_next_frame
        image = self._image_from_frame(frame, full_resolution=True)
        position_ms = int(max(0, target) * 1000.0 / self._fps)
        with self._condition:
            if generation != self._request_generation:
                return True, capture_next_frame
        self.full_resolution_ready.emit(image, position_ms, target)
        return True, capture_next_frame

    def run(self) -> None:
        capture = _open_video_capture(self.video_path, self._capture_factory)
        try:
            if not capture or not capture.isOpened():
                self.playback_error.emit(f"无法打开录像: {self.video_path}")
                return

            reported_fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
            reported_fps = reported_fps if reported_fps > 0.1 else 25.0
            reported_frame_count = max(
                0,
                int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0),
            )
            reported_duration_ms = (
                int(reported_frame_count * 1000.0 / reported_fps)
                if reported_frame_count
                else 0
            )
            self._fps = self._probe_sequential_fps(reported_fps)
            self._duration_ms = self._playlist_duration_ms() or reported_duration_ms
            self._frame_count = (
                max(1, int(round(self._duration_ms * self._fps / 1000.0)))
                if self._sequential_capture and self._duration_ms
                else reported_frame_count
            )
            width = max(0, int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0))
            height = max(0, int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0))
            self._configure_reverse_window(width, height)
            if not self._duration_ms and self._frame_count:
                self._duration_ms = int(self._frame_count * 1000.0 / self._fps)
            self.metadata_ready.emit(
                self._duration_ms,
                self._fps,
                width,
                height,
                self._frame_count,
            )
            self._start_reverse_prefetcher()

            last_frame_index = -1
            capture_next_frame = 0
            anchor_frame = 0
            anchor_clock = time.monotonic()

            while True:
                with self._condition:
                    if self._stop_requested:
                        break
                    seek_frame = self._seek_frame
                    self._seek_frame = None
                    full_resolution_frame = self._full_resolution_frame
                    self._full_resolution_frame = None
                    step_frame = self._step_frame
                    self._step_frame = None
                    playing = self._playing
                    speed = self._speed
                    direction = self._direction
                    anchor_reset = self._anchor_reset
                    self._anchor_reset = False
                    request_generation = self._request_generation

                if seek_frame is not None:
                    requested_frame, generation = seek_frame
                    target = self._clamp_frame(requested_frame)
                    ok, capture_next_frame = self._decode_target(
                        capture,
                        target,
                        capture_next_frame,
                        generation=generation,
                    )
                    if ok and not self._decode_request_cancelled(generation):
                        last_frame_index = target
                        anchor_frame = last_frame_index + direction
                    anchor_clock = time.monotonic()
                    continue

                if step_frame is not None:
                    requested_frame, generation = step_frame
                    target = self._clamp_frame(requested_frame)
                    direction = 1 if requested_frame > last_frame_index else -1
                    if target == last_frame_index and requested_frame != target:
                        if not self._decode_request_cancelled(generation):
                            self.step_boundary_reached.emit(direction)
                        continue
                    ok, capture_next_frame = self._decode_target(
                        capture,
                        target,
                        capture_next_frame,
                        generation=generation,
                        reverse_window=target < last_frame_index,
                    )
                    if ok and not self._decode_request_cancelled(generation):
                        last_frame_index = target
                    elif not self._decode_request_cancelled(generation):
                        self.step_boundary_reached.emit(direction)
                    continue

                if full_resolution_frame is not None:
                    requested_frame, generation = full_resolution_frame
                    _, capture_next_frame = self._decode_full_resolution(
                        capture,
                        self._clamp_frame(requested_frame),
                        capture_next_frame,
                        generation=generation,
                    )
                    continue

                if not playing:
                    with self._condition:
                        self._condition.wait(
                            timeout=self.FORWARD_PREFETCH_IDLE_SECONDS
                        )
                        prefetch_generation = self._request_generation
                    capture_next_frame = self._prefetch_sequential_forward(
                        capture,
                        capture_next_frame,
                        last_frame_index,
                        generation=prefetch_generation,
                    )
                    continue

                if anchor_reset:
                    anchor_frame = last_frame_index + direction
                    anchor_clock = time.monotonic()

                elapsed = max(0.0, time.monotonic() - anchor_clock)
                target = anchor_frame + direction * int(elapsed * self._fps * speed)
                if (
                    (direction > 0 and self._frame_count and target >= self._frame_count)
                    or (direction < 0 and target < 0)
                ):
                    with self._condition:
                        self._playing = False
                    self.playback_finished.emit()
                    continue

                target = self._clamp_frame(target)
                if target == last_frame_index:
                    time.sleep(min(0.008, 1.0 / self._fps))
                    continue

                ok, capture_next_frame = self._decode_target(
                    capture,
                    target,
                    capture_next_frame,
                    generation=request_generation,
                    reverse_window=direction < 0,
                )
                if not ok:
                    with self._condition:
                        self._playing = False
                    self.playback_finished.emit()
                    continue
                if not self._decode_request_cancelled(request_generation):
                    last_frame_index = target
        except Exception as exc:
            self.playback_error.emit(f"录像回放失败: {exc}")
        finally:
            self._log_navigation_summary()
            self._stop_reverse_prefetcher()
            if capture:
                capture.release()
            self._clear_frame_cache()

    def _clamp_frame(self, frame_index: int) -> int:
        if self._frame_count > 0:
            return max(0, min(int(frame_index), self._frame_count - 1))
        return max(0, int(frame_index))


class VideoPlaybackDialog(QDialog):
    def __init__(
        self,
        video_path: Path,
        parent=None,
        *,
        worker_factory: Callable[..., VideoPlaybackWorker] = VideoPlaybackWorker,
        reverse_prefetch: bool = True,
        initial_position_ms: Optional[int] = None,
        target_position_ms: Optional[int] = None,
        context_text: str = "",
        autoplay: bool = True,
        window_title: str = "",
    ):
        super().__init__(parent)
        self.video_path = Path(video_path)
        self._duration_ms = 0
        self._fps = 25.0
        self._frame_count = 0
        self._current_frame_index = 0
        self._playing = bool(autoplay)
        self._last_playback_speed = 1.0
        self._initial_position_ms = (
            None if initial_position_ms is None else max(0, int(initial_position_ms))
        )
        self._target_position_ms = (
            None if target_position_ms is None else max(0, int(target_position_ms))
        )
        self._context_text = str(context_text or "").strip()
        self._window_title = str(window_title or "").strip()
        self._slider_dragging = False
        self._resume_after_seek = False
        self._resume_speed_after_seek = 1.0
        self._jog_origin_frame = 0
        self._pending_frame: Optional[tuple[QImage, int, int]] = None
        self._frame_update_scheduled = False

        self.setWindowTitle(self._window_title or f"裁判回放 - {self.video_path.name}")
        self.resize(1260, 840)
        self.setMinimumSize(820, 580)
        self._init_ui()

        try:
            self.worker = worker_factory(
                self.video_path,
                self,
                reverse_prefetch=bool(reverse_prefetch),
            )
        except TypeError:
            # Preserve compatibility with lightweight worker test doubles.
            self.worker = worker_factory(self.video_path, self)
            if not reverse_prefetch and hasattr(
                self.worker, "_reverse_prefetch_enabled"
            ):
                self.worker._reverse_prefetch_enabled = False
        self.worker.metadata_ready.connect(self._on_metadata_ready)
        self.worker.frame_ready.connect(self._on_frame_ready)
        self.worker.playback_finished.connect(self._on_playback_finished)
        self.worker.playback_error.connect(self._on_playback_error)
        set_idle_prefetch = getattr(self.worker, "set_idle_prefetch_enabled", None)
        if callable(set_idle_prefetch):
            set_idle_prefetch(False)
        if not self._playing:
            self.worker.pause()
        track_qthread(self.worker)
        self.worker.start()
        self._set_playing(self._playing)
        self._set_shuttle_indicator(self._last_playback_speed if self._playing else 0.0)

        self.space_shortcut = QShortcut(QKeySequence(Qt.Key_Space), self)
        self.space_shortcut.setContext(Qt.WindowShortcut)
        self.space_shortcut.activated.connect(self._toggle_playback)

    def _init_ui(self) -> None:
        self.setStyleSheet(
            "QDialog { background: #15191e; color: #f4f6f8; }"
            "QLabel { color: #f4f6f8; }"
            "QWidget#playbackControlDeck { background: #1b2026; border-top: 1px solid #39424c; }"
            "QPushButton { min-height: 44px; border: 1px solid #53606d; "
            "background: #29313a; color: #f4f6f8; padding: 0 14px; border-radius: 4px; "
            "font-size: 14px; font-weight: 700; }"
            "QPushButton:hover { background: #35414c; border-color: #8795a3; }"
            "QPushButton:pressed { background: #20262d; }"
            "QPushButton#playbackPrimary { background: #247fbd; border-color: #3698d6; "
            "font-size: 21px; }"
            "QPushButton#playbackPrimary:hover { background: #2d93d4; }"
            "QPushButton#frameStep { font-size: 17px; padding: 0; }"
            "QSlider#playbackTimeline::groove:horizontal { height: 9px; "
            "background: #414c57; border-radius: 4px; }"
            "QSlider#playbackTimeline::sub-page:horizontal { background: #2788c6; border-radius: 4px; }"
            "QSlider#playbackTimeline::handle:horizontal { width: 18px; margin: -7px 0; "
            "background: #f5f8fa; border: 2px solid #2788c6; border-radius: 9px; }"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.video_label = PlaybackVideoLabel(self)
        self.video_label.jog_started.connect(self._on_jog_started)
        self.video_label.jog_delta_changed.connect(self._on_jog_delta_changed)
        self.video_label.jog_finished.connect(self._on_jog_finished)
        self.video_label.wheel_jogged.connect(self._on_wheel_jogged)
        layout.addWidget(self.video_label, 1)

        control_deck = QWidget(self)
        control_deck.setObjectName("playbackControlDeck")
        deck_layout = QVBoxLayout(control_deck)
        deck_layout.setContentsMargins(18, 10, 18, 12)
        deck_layout.setSpacing(8)

        metadata_layout = QHBoxLayout()
        metadata_layout.setSpacing(12)
        self.current_time_label = QLabel("00:00:00.000")
        self.current_time_label.setMinimumWidth(165)
        self.current_time_label.setStyleSheet(
            "font-family: Consolas, monospace; font-size: 21px; font-weight: 800; color: #ffffff;"
        )
        self.frame_label = QLabel("帧 0 / 0")
        self.frame_label.setStyleSheet("color: #c8d2dc; font-size: 13px; font-weight: 600;")
        self.duration_label = QLabel("总时长 00:00:00.000")
        self.duration_label.setStyleSheet("color: #9eabb7; font-size: 12px;")
        self.duration_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        metadata_layout.addWidget(self.current_time_label)
        metadata_layout.addWidget(self.frame_label)
        metadata_layout.addStretch()
        metadata_layout.addWidget(self.duration_label)
        deck_layout.addLayout(metadata_layout)

        self.target_status_label = QLabel()
        self.target_status_label.setStyleSheet(
            "color: #ffcf70; font-size: 13px; font-weight: 700;"
        )
        self.target_status_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.target_status_label.setVisible(
            self._target_position_ms is not None or bool(self._context_text)
        )
        deck_layout.addWidget(self.target_status_label)

        self.timeline = TargetTimelineSlider(Qt.Horizontal)
        self.timeline.setObjectName("playbackTimeline")
        self.timeline.setRange(0, 0)
        self.timeline.setFixedHeight(26)
        self.timeline.sliderPressed.connect(self._on_slider_pressed)
        self.timeline.sliderReleased.connect(self._on_slider_released)
        self.timeline.sliderMoved.connect(self._on_slider_moved)
        self.timeline.set_target_position(self._target_position_ms)
        deck_layout.addWidget(self.timeline)

        transport = QHBoxLayout()
        transport.setContentsMargins(0, 0, 0, 0)
        transport.setSpacing(10)
        transport.addStretch()
        self.target_btn = self._text_button(
            "回到目标", "回到目标点并暂停（T / Home）", self._seek_target, 112
        )
        self.target_btn.setEnabled(self._target_position_ms is not None)
        self.previous_frame_btn = self._text_button(
            "|◀", "上一帧", lambda: self._step(-1), 54
        )
        self.back_one_btn = self._jump_button("−1s", -1_000)
        self.play_btn = self._text_button(
            "Ⅱ", "暂停", self._toggle_playback, 64, primary=True
        )
        self.forward_one_btn = self._jump_button("+1s", 1_000)
        self.next_frame_btn = self._text_button(
            "▶|", "下一帧", lambda: self._step(1), 54
        )
        for button in (
            self.target_btn,
            self.previous_frame_btn,
            self.back_one_btn,
            self.play_btn,
            self.forward_one_btn,
            self.next_frame_btn,
        ):
            transport.addWidget(button)
        transport.addStretch()
        deck_layout.addLayout(transport)

        shuttle_layout = QHBoxLayout()
        shuttle_layout.setContentsMargins(0, 0, 0, 0)
        self.shuttle_slider = SpringShuttleSlider(self)
        self.shuttle_slider.speed_changed.connect(self._on_shuttle_speed_changed)
        shuttle_layout.addStretch()
        shuttle_layout.addWidget(self.shuttle_slider)
        shuttle_layout.addStretch()
        deck_layout.addLayout(shuttle_layout)
        layout.addWidget(control_deck)

    def _text_button(
        self,
        text: str,
        tooltip: str,
        callback,
        width: int,
        *,
        primary: bool = False,
    ) -> QPushButton:
        button = QPushButton(text)
        button.setToolTip(tooltip)
        button.setFixedSize(width, 48)
        button.setObjectName("playbackPrimary" if primary else "frameStep")
        button.clicked.connect(callback)
        return button

    def _jump_button(self, text: str, delta_ms: int) -> QPushButton:
        button = QPushButton(text)
        button.setFixedSize(76, 48)
        button.clicked.connect(lambda: self._jump(delta_ms))
        return button

    def _set_playing(self, playing: bool) -> None:
        self._playing = playing
        self.video_label.set_smooth_scaling(not playing)
        self.play_btn.setText("Ⅱ" if playing else "▶")
        self.play_btn.setToolTip("暂停（空格）" if playing else "继续播放（空格）")

    def _toggle_playback(self) -> None:
        if self._playing:
            self.worker.pause()
            self._set_playing(False)
            self._set_shuttle_indicator(0.0)
        else:
            speed = self._last_playback_speed
            if speed > 0 and self._duration_ms and self.timeline.value() >= self._duration_ms - 20:
                self.worker.seek_frame(0)
            elif speed < 0 and self.timeline.value() <= 20 and self._frame_count:
                self.worker.seek_frame(self._frame_count - 1)
            self.worker.set_shuttle_speed(speed)
            self._set_playing(True)
            self._set_shuttle_indicator(speed)

    def _jump(self, delta_ms: int) -> None:
        self.worker.jump(delta_ms)

    def _seek_target(self) -> None:
        if self._target_position_ms is None:
            return
        target_ms = self._target_position_ms
        if self._duration_ms > 0:
            target_ms = min(target_ms, self._duration_ms)
        self.worker.pause()
        self.worker.seek(target_ms)
        self.timeline.setValue(target_ms)
        self.current_time_label.setText(format_playback_time(target_ms))
        self._update_target_status(target_ms)
        self._set_playing(False)
        self._set_shuttle_indicator(0.0)

    def _step(self, frame_delta: int) -> None:
        self.worker.step(frame_delta)
        self._set_playing(False)
        self._set_shuttle_indicator(0.0)

    def _on_shuttle_speed_changed(self, speed: float) -> None:
        if abs(speed) > 0.01:
            self._last_playback_speed = speed
        self.worker.set_shuttle_speed(speed)
        self._set_playing(abs(speed) > 0.01)
        self._set_shuttle_indicator(speed, update_slider=False)

    def _set_shuttle_indicator(self, speed: float, *, update_slider: bool = True) -> None:
        if update_slider and abs(speed) > 0.01:
            self.shuttle_slider.set_speed(speed, emit=False)
        self.shuttle_slider.set_display_speed(speed)

    def _on_metadata_ready(
        self,
        duration_ms: int,
        fps: float,
        width: int,
        height: int,
        frame_count: int,
    ) -> None:
        self._duration_ms = max(0, int(duration_ms))
        self._fps = max(0.1, float(fps))
        self._frame_count = max(0, int(frame_count))
        self.video_label.set_jog_frame_span(self._frame_count)
        self.timeline.setRange(0, self._duration_ms)
        self.timeline.set_target_position(self._target_position_ms)
        self.duration_label.setText(f"总时长 {format_playback_time(self._duration_ms)}")
        self.frame_label.setText(f"帧 0 / {self._frame_count}")
        title = self._window_title or f"裁判回放 - {self.video_path.name}"
        self.setWindowTitle(f"{title} | {width}x{height} | {fps:.2f} FPS")
        if self._initial_position_ms is not None:
            target_ms = min(self._initial_position_ms, self._duration_ms)
            self._initial_position_ms = None
            self.timeline.setValue(target_ms)
            self.current_time_label.setText(format_playback_time(target_ms))
            self.worker.seek(target_ms)
        self._update_target_status(self.timeline.value())

    def _on_frame_ready(self, image: QImage, position_ms: int, frame_index: int) -> None:
        self._pending_frame = (image, int(position_ms), int(frame_index))
        if self._frame_update_scheduled:
            return
        self._frame_update_scheduled = True
        QTimer.singleShot(0, self._flush_pending_frame)

    def _flush_pending_frame(self) -> None:
        self._frame_update_scheduled = False
        pending = self._pending_frame
        self._pending_frame = None
        if pending is None:
            return
        self._render_frame_now(*pending)

    def _render_frame_now(self, image: QImage, position_ms: int, frame_index: int) -> None:
        self._current_frame_index = frame_index
        self.video_label.set_frame(image)
        if not self._slider_dragging:
            self.timeline.setValue(min(position_ms, self._duration_ms))
            self.current_time_label.setText(format_playback_time(position_ms))
            self._update_target_status(position_ms)
        self.frame_label.setText(f"帧 {frame_index + 1} / {self._frame_count}")

    def _on_slider_pressed(self) -> None:
        self._slider_dragging = True
        self._resume_after_seek = self._playing
        self._resume_speed_after_seek = self._last_playback_speed
        self.worker.pause()
        self._set_playing(False)
        self._set_shuttle_indicator(0.0)

    def _on_slider_moved(self, value: int) -> None:
        self.current_time_label.setText(format_playback_time(value))
        self._update_target_status(value)
        self.worker.seek(value)

    def _update_target_status(self, current_position_ms: int) -> None:
        parts = []
        if self._context_text:
            parts.append(self._context_text)
        target_ms = self._target_position_ms
        if target_ms is not None:
            target_text = f"Passage 目标 {format_playback_time(target_ms)}"
            if self._duration_ms > 0 and target_ms > self._duration_ms:
                excess_ms = target_ms - self._duration_ms
                target_text += f"（超出录像时长 {excess_ms / 1000.0:.3f} 秒）"
            else:
                delta_ms = int(current_position_ms) - target_ms
                if delta_ms < 0:
                    target_text += f"，目标前 {-delta_ms / 1000.0:.3f} 秒"
                elif delta_ms > 0:
                    target_text += f"，已过目标 {delta_ms / 1000.0:.3f} 秒"
                else:
                    target_text += "，位于目标"
            parts.append(target_text)
        text = " | ".join(parts)
        self.target_status_label.setText(text)
        self.target_status_label.setToolTip(text)

    def _on_slider_released(self) -> None:
        self._slider_dragging = False
        self.worker.seek(self.timeline.value())
        if self._resume_after_seek:
            self.worker.set_shuttle_speed(self._resume_speed_after_seek)
            self._set_playing(True)
            self._set_shuttle_indicator(self._resume_speed_after_seek)
        self._resume_after_seek = False

    def _on_jog_started(self) -> None:
        self._jog_origin_frame = self._current_frame_index
        self.worker.pause()
        self._set_playing(False)
        self._set_shuttle_indicator(0.0)

    def _on_jog_delta_changed(self, frame_delta: int) -> None:
        self.worker.seek_frame(self._jog_origin_frame + frame_delta)

    def _on_jog_finished(self) -> None:
        self.worker.pause()

    def _on_wheel_jogged(self, steps: int, modifiers: int) -> None:
        if modifiers & int(Qt.AltModifier):
            frame_delta = int(round(self._fps)) * steps
        elif modifiers & int(Qt.ShiftModifier):
            frame_delta = 10 * steps
        else:
            frame_delta = steps
        self._step(frame_delta)

    def _on_playback_finished(self) -> None:
        self._set_playing(False)
        self._set_shuttle_indicator(0.0)

    def _on_playback_error(self, message: str) -> None:
        self._set_playing(False)
        self._set_shuttle_indicator(0.0)
        QMessageBox.critical(self, "录像回放失败", message)

    def _cycle_shuttle(self, direction: int) -> None:
        current = SpringShuttleSlider.speed_for_value(self.shuttle_slider.value())
        if direction < 0:
            next_speed = -1.0 if current >= 0 else max(-4.0, current * 2.0)
        else:
            next_speed = 1.0 if current <= 0 else min(4.0, current * 2.0)
        self.shuttle_slider.set_speed(next_speed)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key_J:
            self._cycle_shuttle(-1)
            event.accept()
            return
        if event.key() == Qt.Key_K:
            self.worker.pause()
            self._set_playing(False)
            self._set_shuttle_indicator(0.0)
            event.accept()
            return
        if event.key() == Qt.Key_L:
            self._cycle_shuttle(1)
            event.accept()
            return
        if event.key() in (Qt.Key_Left, Qt.Key_Right):
            direction = -1 if event.key() == Qt.Key_Left else 1
            frame_count = 10 if event.modifiers() & Qt.ShiftModifier else 1
            self._step(direction * frame_count)
            event.accept()
            return
        if event.key() in (Qt.Key_PageUp, Qt.Key_PageDown):
            direction = -1 if event.key() == Qt.Key_PageUp else 1
            self._jump(direction * 1_000)
            event.accept()
            return
        if event.key() in (Qt.Key_Home, Qt.Key_T):
            self._seek_target()
            event.accept()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event) -> None:
        self.worker.request_stop()
        if not self.worker.wait(3_000):
            retire_qthread(self.worker)
        else:
            self.worker.deleteLater()
        event.accept()
