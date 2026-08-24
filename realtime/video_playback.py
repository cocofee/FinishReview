"""Judge-oriented playback for recordings stored inside a race directory."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Callable, Optional

import cv2
from PyQt5.QtCore import Qt, QThread, pyqtSignal
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
                Qt.SmoothTransformation,
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
        self.target_position_ms: Optional[int] = None

    def set_target_position(self, position_ms: Optional[int]) -> None:
        self.target_position_ms = (
            None if position_ms is None else max(0, int(position_ms))
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
    playback_error = pyqtSignal(str)

    def __init__(
        self,
        video_path: Path,
        parent=None,
        *,
        capture_factory: Callable[[str], cv2.VideoCapture] = cv2.VideoCapture,
    ):
        super().__init__(parent)
        self.video_path = Path(video_path)
        self._capture_factory = capture_factory
        self._condition = threading.Condition()
        self._stop_requested = False
        self._playing = True
        self._speed = 1.0
        self._direction = 1
        self._seek_frame: Optional[int] = None
        self._full_resolution_frame: Optional[int] = None
        self._step_frames = 0
        self._anchor_reset = True
        self._current_frame_index = -1
        self._current_position_ms = 0
        self._duration_ms = 0
        self._frame_count = 0
        self._fps = 25.0
        self._frame_cache: OrderedDict[int, QImage] = OrderedDict()
        self._frame_cache_bytes = 0
        self._max_cache_bytes = 160 * 1024 * 1024

    @property
    def current_position_ms(self) -> int:
        with self._condition:
            return self._current_position_ms

    @property
    def current_frame_index(self) -> int:
        with self._condition:
            return self._current_frame_index

    def play(self) -> None:
        self.set_shuttle_speed(1.0)

    def pause(self) -> None:
        with self._condition:
            self._playing = False
            self._condition.notify_all()

    def set_shuttle_speed(self, speed: float) -> None:
        speed = max(-4.0, min(4.0, float(speed)))
        with self._condition:
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
            self._seek_frame = max(0, min(int(frame_index), upper))
            self._anchor_reset = True
            self._condition.notify_all()

    def seek(self, milliseconds: int) -> None:
        self.seek_frame(int(max(0, milliseconds) * self._fps / 1000.0))

    def jump(self, delta_ms: int) -> None:
        delta_frames = int(round(int(delta_ms) * self._fps / 1000.0))
        self.seek_frame(self.current_frame_index + delta_frames)

    def step(self, frame_delta: int) -> None:
        with self._condition:
            self._playing = False
            self._step_frames += int(frame_delta)
            self._condition.notify_all()

    def request_full_resolution(self, frame_index: Optional[int] = None) -> None:
        with self._condition:
            target = self._current_frame_index if frame_index is None else int(frame_index)
            if target < 0:
                return
            self._full_resolution_frame = self._clamp_frame(target)
            self._condition.notify_all()

    def stop(self) -> None:
        with self._condition:
            self._stop_requested = True
            self._condition.notify_all()

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
        previous = self._frame_cache.pop(frame_index, None)
        if previous is not None:
            self._frame_cache_bytes -= previous.byteCount()
        self._frame_cache[frame_index] = image
        self._frame_cache_bytes += image.byteCount()
        while self._frame_cache and self._frame_cache_bytes > self._max_cache_bytes:
            _, evicted = self._frame_cache.popitem(last=False)
            self._frame_cache_bytes -= evicted.byteCount()

    def _emit_image(self, image: QImage, frame_index: int) -> None:
        position_ms = int(max(0, frame_index) * 1000.0 / self._fps)
        with self._condition:
            self._current_frame_index = frame_index
            self._current_position_ms = position_ms
        self.frame_ready.emit(image, position_ms, frame_index)

    def _decode_target(self, capture, target: int, capture_next_frame: int) -> tuple[bool, int]:
        cached = self._frame_cache.get(target)
        if cached is not None:
            self._frame_cache.move_to_end(target)
            self._emit_image(cached, target)
            return True, capture_next_frame

        if target != capture_next_frame:
            forward_gap = target - capture_next_frame
            if capture_next_frame >= 0 and 0 < forward_gap <= 12:
                for _ in range(forward_gap):
                    if not capture.grab():
                        return False, capture_next_frame
                capture_next_frame = target
            else:
                capture.set(cv2.CAP_PROP_POS_FRAMES, target)
                capture_next_frame = target

        ok, frame = capture.read()
        if not ok:
            return False, capture_next_frame
        capture_next_frame = int(capture.get(cv2.CAP_PROP_POS_FRAMES) or target + 1)
        image = self._image_from_frame(frame)
        self._cache_image(target, image)
        self._emit_image(image, target)
        return True, capture_next_frame

    def _decode_full_resolution(
        self,
        capture,
        target: int,
        capture_next_frame: int,
    ) -> tuple[bool, int]:
        if target != capture_next_frame:
            capture.set(cv2.CAP_PROP_POS_FRAMES, target)
            capture_next_frame = target
        ok, frame = capture.read()
        if not ok:
            return False, capture_next_frame
        capture_next_frame = int(capture.get(cv2.CAP_PROP_POS_FRAMES) or target + 1)
        image = self._image_from_frame(frame, full_resolution=True)
        position_ms = int(max(0, target) * 1000.0 / self._fps)
        self.full_resolution_ready.emit(image, position_ms, target)
        return True, capture_next_frame

    def run(self) -> None:
        capture = _open_video_capture(self.video_path, self._capture_factory)
        try:
            if not capture or not capture.isOpened():
                self.playback_error.emit(f"无法打开录像: {self.video_path}")
                return

            fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
            self._fps = fps if fps > 0.1 else 25.0
            self._frame_count = max(0, int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0))
            width = max(0, int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0))
            height = max(0, int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0))
            self._duration_ms = (
                int(self._frame_count * 1000.0 / self._fps)
                if self._frame_count
                else 0
            )
            self.metadata_ready.emit(
                self._duration_ms,
                self._fps,
                width,
                height,
                self._frame_count,
            )

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
                    step_frames = self._step_frames
                    self._step_frames = 0
                    playing = self._playing
                    speed = self._speed
                    direction = self._direction
                    anchor_reset = self._anchor_reset
                    self._anchor_reset = False

                if seek_frame is not None:
                    target = self._clamp_frame(seek_frame)
                    ok, capture_next_frame = self._decode_target(
                        capture,
                        target,
                        capture_next_frame,
                    )
                    if ok:
                        last_frame_index = target
                        anchor_frame = last_frame_index + direction
                    anchor_clock = time.monotonic()
                    continue

                if full_resolution_frame is not None:
                    _, capture_next_frame = self._decode_full_resolution(
                        capture,
                        self._clamp_frame(full_resolution_frame),
                        capture_next_frame,
                    )
                    continue

                if step_frames:
                    target = self._clamp_frame(last_frame_index + step_frames)
                    ok, capture_next_frame = self._decode_target(
                        capture,
                        target,
                        capture_next_frame,
                    )
                    if ok:
                        last_frame_index = target
                    continue

                if not playing:
                    with self._condition:
                        self._condition.wait(timeout=0.05)
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
                )
                if not ok:
                    with self._condition:
                        self._playing = False
                    self.playback_finished.emit()
                    continue
                last_frame_index = target
        except Exception as exc:
            self.playback_error.emit(f"录像回放失败: {exc}")
        finally:
            if capture:
                capture.release()
            self._frame_cache.clear()
            self._frame_cache_bytes = 0

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

        self.setWindowTitle(self._window_title or f"裁判回放 - {self.video_path.name}")
        self.resize(1260, 840)
        self.setMinimumSize(820, 580)
        self._init_ui()

        self.worker = worker_factory(self.video_path, self)
        self.worker.metadata_ready.connect(self._on_metadata_ready)
        self.worker.frame_ready.connect(self._on_frame_ready)
        self.worker.playback_finished.connect(self._on_playback_finished)
        self.worker.playback_error.connect(self._on_playback_error)
        if not self._playing:
            self.worker.pause()
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
        self.worker.stop()
        if not self.worker.wait(3_000):
            self.worker.terminate()
            self.worker.wait(1_000)
        event.accept()
