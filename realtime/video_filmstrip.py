"""Incremental chronological thumbnail filmstrip for video review."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import cv2
from PyQt5.QtCore import QPoint, QThread, QTimer, Qt, QRectF, pyqtSignal
from PyQt5.QtGui import QColor, QImage, QPainter, QPen, QPolygon
from PyQt5.QtWidgets import QApplication, QComboBox, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget

from .thread_lifecycle import retire_qthread, track_qthread
from .camera_judgments import CameraJudgmentTrack
from .race_filmstrip import RaceFilmstripPanel
from .time_domain import DurationMs, MediaPositionMs, WallClockMs

DEFAULT_FILMSTRIP_INTERVAL_MS = 2_000
# Continuous camera review uses a denser rail because several riders can cross
# between adjacent two-second overview thumbnails. The generic widget keeps
# its historical default; the review surface opts into this interval explicitly.
CONTINUOUS_FILMSTRIP_INTERVAL_MS = 500
FILMSTRIP_TILE_WIDTH = 360
# Leave room within the 320px filmstrip panel for the timestamp caption and
# horizontal scrollbar. At 240px the caption was clipped on scaled displays.
FILMSTRIP_TILE_HEIGHT = 204
FILMSTRIP_TILE_GAP = 8
# Decode only the selected athlete's immediate neighborhood first. This keeps
# random thumbnail seeks from competing with the primary judgment frame.
FILMSTRIP_INITIAL_BATCH = 5
FILMSTRIP_SEQUENTIAL_GAP_FRAMES = 125
BEIJING_TIMEZONE = timezone(timedelta(hours=8))


def format_filmstrip_time(timestamp_ms: WallClockMs | int) -> str:
    """Format a media timestamp using the race clock shown elsewhere in review."""

    try:
        value = datetime.fromtimestamp(
            int(timestamp_ms) / 1000.0,
            tz=BEIJING_TIMEZONE,
        )
    except (OSError, OverflowError, ValueError):
        return f"{int(timestamp_ms)} ms"
    return value.strftime("%H:%M:%S.%f")[:-3]


@dataclass(frozen=True, slots=True)
class FilmstripFrame:
    position_ms: MediaPositionMs
    frame_index: int
    image: QImage


def filmstrip_positions(
    start_ms: MediaPositionMs | int,
    end_ms: MediaPositionMs | int,
    *,
    interval_ms: DurationMs | int = DEFAULT_FILMSTRIP_INTERVAL_MS,
    anchors: Iterable[MediaPositionMs | int] = (),
) -> tuple[MediaPositionMs, ...]:
    start = max(0, int(start_ms))
    end = max(start, int(end_ms))
    interval = max(1, int(interval_ms))
    anchor_values = [max(start, min(end, int(anchor))) for anchor in anchors]
    values = list(range(start, end + 1, interval))
    if not values or values[-1] != end:
        values.append(end)
    return tuple(
        MediaPositionMs(value)
        for value in sorted(set(values).union(anchor_values))
    )


class VideoFilmstripWorker(QThread):
    frame_ready = pyqtSignal(QImage, int, int)
    failed = pyqtSignal(str)

    def __init__(
        self,
        video_path: Path,
        positions_ms: Iterable[int],
        parent=None,
        *,
        sequential_window: bool = False,
    ):
        super().__init__(parent)
        self.video_path = Path(video_path)
        self.positions_ms = tuple(int(value) for value in positions_ms)
        self.sequential_window = bool(sequential_window)
        self._stop_requested = False

    def request_stop(self) -> None:
        self.requestInterruption()
        self._stop_requested = True

    def stop(self) -> None:
        self.request_stop()

    @staticmethod
    def _reported_frame_index(capture, expected: int) -> int:
        try:
            reported_next = float(
                capture.get(cv2.CAP_PROP_POS_FRAMES) or 0.0
            )
        except (TypeError, ValueError):
            return int(expected)
        rounded = round(reported_next)
        if reported_next <= 0 or abs(reported_next - rounded) > 0.01:
            return int(expected)
        return max(0, int(rounded) - 1)

    def _emit_thumbnail(self, frame, position_ms: int, frame_index: int) -> None:
        height, width = frame.shape[:2]
        if width <= 0 or height <= 0:
            return
        scale = min(1.0, FILMSTRIP_TILE_WIDTH / float(width))
        size = (
            max(1, int(round(width * scale))),
            max(1, int(round(height * scale))),
        )
        if size != (width, height):
            frame = cv2.resize(frame, size, interpolation=cv2.INTER_AREA)
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        image = QImage(
            rgb.data,
            rgb.shape[1],
            rgb.shape[0],
            rgb.strides[0],
            QImage.Format_RGB888,
        ).copy()
        self.frame_ready.emit(image, int(position_ms), int(frame_index))

    def _run_sequential_window(self, capture, fps: float) -> None:
        positions = tuple(sorted(set(self.positions_ms)))
        if not positions:
            return
        first_frame = max(0, int(round(positions[0] * fps / 1000.0)))
        last_frame = max(first_frame, int(round(positions[-1] * fps / 1000.0)))
        sequential_source = self.video_path.suffix.lower() == ".m3u8"
        next_frame = 0 if sequential_source else first_frame
        if not sequential_source and not capture.set(
            cv2.CAP_PROP_POS_FRAMES,
            first_frame,
        ):
            # Some backends reject frame seeks. Fall back to decoding from the
            # beginning while retaining the same completeness check.
            next_frame = 0
        target_index = 0
        while target_index < len(positions):
            if self._stop_requested:
                return
            ok, frame = capture.read()
            if not ok or frame is None:
                self.failed.emit(
                    "当前窗口在视频位置 "
                    f"{next_frame * 1000.0 / max(0.1, fps):.0f} ms 解码中断"
                )
                return
            actual_frame = self._reported_frame_index(capture, next_frame)
            if actual_frame < next_frame:
                actual_frame = next_frame
            while (
                target_index < len(positions)
                and int(round(positions[target_index] * fps / 1000.0)) <= actual_frame
            ):
                self._emit_thumbnail(
                    frame,
                    positions[target_index],
                    actual_frame,
                )
                target_index += 1
            next_frame = max(next_frame + 1, actual_frame + 1)
            if actual_frame >= last_frame and target_index >= len(positions):
                break
        if target_index < len(positions):
            self.failed.emit("当前窗口存在未解码的录像位置")

    def run(self) -> None:
        capture = cv2.VideoCapture(str(self.video_path))
        if not capture.isOpened():
            self.failed.emit(f"Unable to open video: {self.video_path}")
            return
        try:
            fps = max(0.1, float(capture.get(cv2.CAP_PROP_FPS) or 0.0))
            if self.sequential_window:
                self._run_sequential_window(capture, fps)
                return
            positions = self.positions_ms
            sequential_source = self.video_path.suffix.lower() == ".m3u8"
            ordered_positions = (
                tuple(sorted(set(positions)))
                if sequential_source
                else (
                    (positions[0], *sorted(set(positions[1:]) - {positions[0]}))
                    if positions
                    else ()
                )
            )
            capture_next_frame = 0 if sequential_source else -1
            for position_ms in ordered_positions:
                if self._stop_requested:
                    return
                frame_index = max(0, int(round(position_ms * fps / 1000.0)))
                forward_gap = frame_index - capture_next_frame
                needs_reposition = bool(
                    capture_next_frame < 0
                    or frame_index < capture_next_frame
                    or forward_gap > FILMSTRIP_SEQUENTIAL_GAP_FRAMES
                )
                if sequential_source and frame_index < capture_next_frame:
                    continue
                if needs_reposition and not sequential_source:
                    if capture.set(cv2.CAP_PROP_POS_FRAMES, frame_index):
                        capture_next_frame = frame_index
                    elif capture_next_frame < 0:
                        # Some HLS/OpenCV backends reject POS_FRAMES but still
                        # open at frame zero. Fall back to forward decoding.
                        capture_next_frame = 0
                    else:
                        continue
                sequential_failed = False
                while capture_next_frame < frame_index:
                    if self._stop_requested:
                        return
                    if not capture.grab():
                        sequential_failed = True
                        break
                    capture_next_frame += 1
                if sequential_failed:
                    if not capture.set(cv2.CAP_PROP_POS_FRAMES, frame_index):
                        capture_next_frame = -1
                        continue
                    capture_next_frame = frame_index
                ok, frame = capture.read()
                if not ok or frame is None:
                    capture_next_frame = -1
                    continue
                actual_frame_index = self._reported_frame_index(
                    capture,
                    frame_index,
                )
                while actual_frame_index < frame_index:
                    if self._stop_requested:
                        return
                    ok, frame = capture.read()
                    if not ok or frame is None:
                        break
                    actual_frame_index = self._reported_frame_index(
                        capture,
                        actual_frame_index + 1,
                    )
                if not ok or frame is None or actual_frame_index != frame_index:
                    capture_next_frame = -1
                    continue
                capture_next_frame = actual_frame_index + 1
                self._emit_thumbnail(frame, position_ms, frame_index)
        except Exception as error:  # pragma: no cover
            self.failed.emit(str(error))
        finally:
            capture.release()


class FilmstripCanvas(QWidget):
    """One paint-only surface; avoids hundreds of native child widgets."""

    position_released = pyqtSignal(int)
    position_double_clicked = pyqtSignal(int)
    scrub_position_changed = pyqtSignal(int)

    def __init__(self, owner, parent=None):
        super().__init__(parent)
        self.owner = owner
        self._dragging = False
        self._last_x = 0
        self._press_x = 0
        self._moved = False
        self._pending_click_position: int | None = None
        self._click_timer = QTimer(self)
        self._click_timer.setSingleShot(True)
        self._click_timer.setInterval(max(300, QApplication.doubleClickInterval() + 80))
        self._click_timer.timeout.connect(self._emit_pending_click)
        self.setMouseTracking(True)
        self.setCursor(Qt.OpenHandCursor)
        self.setMinimumHeight(FILMSTRIP_TILE_HEIGHT + 34)

    def _visual_frames(self) -> list[FilmstripFrame]:
        cached = {
            int(frame.position_ms): frame for frame in self.owner._frames
        }
        if self.owner._target_positions:
            positions = (
                tuple(reversed(self.owner._target_positions))
                if self.owner._reverse
                else self.owner._target_positions
            )
            return [
                cached.get(
                    position,
                    FilmstripFrame(position, -1, QImage()),
                )
                for position in positions
            ]
        return sorted(
            self.owner._frames,
            key=lambda value: value.position_ms,
            reverse=self.owner._reverse,
        )

    def _frame_at_x(self, x: float) -> FilmstripFrame | None:
        frames = self._visual_frames()
        if not frames:
            return None
        step = FILMSTRIP_TILE_WIDTH + FILMSTRIP_TILE_GAP
        # Mouse events are delivered in the canvas' content coordinates, even
        # when the canvas is viewed through a scrolled viewport.
        index = max(
            0,
            min(len(frames) - 1, int((x - 4) // step)),
        )
        return frames[index]

    def _position_at_x(self, x: float) -> int | None:
        """Interpolate a continuous media position under the pointer."""

        frames = self._visual_frames()
        if not frames:
            return None
        step = FILMSTRIP_TILE_WIDTH + FILMSTRIP_TILE_GAP
        relative = max(0.0, (float(x) - 4.0) / float(step))
        left_index = min(len(frames) - 1, int(relative))
        right_index = min(len(frames) - 1, left_index + 1)
        if right_index == left_index:
            return int(frames[left_index].position_ms)
        fraction = max(0.0, min(1.0, relative - left_index))
        left = int(frames[left_index].position_ms)
        right = int(frames[right_index].position_ms)
        return int(round(left + (right - left) * fraction))

    def _display_time_text(self, frame: FilmstripFrame) -> str:
        origin_ms = self.owner._display_origin_ms
        if origin_ms is not None:
            return format_filmstrip_time(int(origin_ms) + int(frame.position_ms))
        reference_ms = self.owner._display_reference_ms
        if reference_ms is None:
            end_ms = self.owner._display_end_ms
            if end_ms is None:
                return f"{frame.position_ms / 1000.0:.3f}s"
            remaining_ms = max(0, int(end_ms) - int(frame.position_ms))
            return f"{remaining_ms / 1000.0:.3f}s"
        delta_ms = int(reference_ms) - int(frame.position_ms)
        if delta_ms >= 0:
            return f"{delta_ms / 1000.0:.3f}s"
        return f"+{abs(delta_ms) / 1000.0:.3f}s"

    def _update_status_for_frame(self, frame: FilmstripFrame) -> None:
        display_text = self._display_time_text(frame)
        if self.owner._display_origin_ms is not None:
            self.owner.status_label.setText(display_text)
            return
        if display_text.startswith("+"):
            self.owner.status_label.setText(f"T{display_text}")
        else:
            self.owner.status_label.setText(f"T-{display_text}")

    def _emit_pending_click(self) -> None:
        position_ms = self._pending_click_position
        self._pending_click_position = None
        if position_ms is None:
            return
        self.position_released.emit(position_ms)

    def refresh_geometry(self) -> None:
        count = max(len(self.owner._frames), len(self.owner._target_positions))
        viewport_width = self.owner.scroll.viewport().width()
        width = max(viewport_width, 8 + count * (FILMSTRIP_TILE_WIDTH + FILMSTRIP_TILE_GAP))
        height = max(self.owner.scroll.viewport().height(), FILMSTRIP_TILE_HEIGHT + 34)
        self.setMinimumSize(width, height)
        self.resize(width, height)
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(event.rect(), QColor("#f8fafc"))
        step = FILMSTRIP_TILE_WIDTH + FILMSTRIP_TILE_GAP
        frames = self._visual_frames()
        active_index = None
        current_position = int(self.owner._current_position_ms)
        if frames and current_position >= 0:
            active_index = min(
                range(len(frames)),
                key=lambda index: abs(
                    int(frames[index].position_ms) - current_position
                ),
            )
        first = max(0, int((event.rect().left() - 4) // step) - 1)
        last = min(len(frames), int((event.rect().right() - 4) // step) + 2)
        for index, frame in enumerate(frames[first:last], start=first):
            x = 4 + index * step
            target = QRectF(x, 4, FILMSTRIP_TILE_WIDTH, FILMSTRIP_TILE_HEIGHT)
            painter.setBrush(Qt.NoBrush)
            if frame.image.isNull():
                painter.fillRect(target, QColor("#e2e8f0"))
            else:
                painter.drawImage(target, frame.image)
            selected = index == active_index
            painter.setPen(
                QPen(QColor("#f97316" if selected else "#cbd5e1"), 6 if selected else 1)
            )
            painter.drawRect(target)
            if selected:
                # A second inset line keeps the active tile visible against
                # both bright pavement and dark shadows in the thumbnails.
                painter.setPen(QPen(QColor("#fde047"), 2))
                painter.drawRect(target.adjusted(7, 7, -7, -7))
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor("#f97316"))
                painter.drawRoundedRect(QRectF(x + 10, 10, 64, 26), 4, 4)
                painter.setPen(QColor("#ffffff"))
                painter.drawText(
                    QRectF(x + 10, 10, 64, 26),
                    Qt.AlignCenter,
                    "当前",
                )
                painter.setBrush(Qt.NoBrush)
            # Show only the time below each preview frame. The shorter image
            # height reserves this caption row above the horizontal scrollbar.
            painter.setPen(QColor("#334155"))
            painter.drawText(
                QRectF(x, FILMSTRIP_TILE_HEIGHT + 7, FILMSTRIP_TILE_WIDTH, 20),
                Qt.AlignCenter,
                self._display_time_text(frame),
            )
            marker = self.owner._marker
            if marker is not None and marker[0] == frame.position_ms:
                marker_x = x + marker[1] * FILMSTRIP_TILE_WIDTH
                marker_y = 4 + marker[2] * FILMSTRIP_TILE_HEIGHT
                painter.setPen(QPen(QColor("#dc2626"), 3))
                painter.drawLine(
                    int(marker_x),
                    int(4),
                    int(marker_x),
                    int(4 + FILMSTRIP_TILE_HEIGHT),
                )
                painter.drawEllipse(int(marker_x - 5), int(marker_y - 5), 10, 10)
        # Historical markers are part of the continuous timeline, not the
        # currently selected athlete. Draw them across the visible strip so
        # nearby riders remain distinguishable while the operator scans.
        if frames and self.owner._history_markers:
            for marker_position, marker_label in self.owner._history_markers:
                if marker_position < int(frames[0].position_ms) or marker_position > int(frames[-1].position_ms):
                    continue
                nearest = min(
                    range(len(frames)),
                    key=lambda index: abs(int(frames[index].position_ms) - marker_position),
                )
                marker_x = 4.0 + nearest * step + FILMSTRIP_TILE_WIDTH / 2.0
                painter.setPen(QPen(QColor("#2563eb"), 2))
                painter.drawLine(
                    int(marker_x), 4,
                    int(marker_x), 4 + FILMSTRIP_TILE_HEIGHT,
                )
                painter.setPen(QColor("#1d4ed8"))
                painter.drawText(
                    QRectF(marker_x - 34, 4, 68, 18),
                    Qt.AlignCenter,
                    marker_label,
                )
        # The active frame is usually between two filmstrip thumbnails. Draw a
        # continuous playhead so the operator can see the exact frame while
        # the lower camera pane is being scrubbed for the next rider.
        playhead_x = None
        if frames and current_position >= 0:
            if len(frames) == 1:
                playhead_x = 4.0 + FILMSTRIP_TILE_WIDTH / 2.0
            else:
                for left_index in range(len(frames) - 1):
                    left = frames[left_index]
                    right = frames[left_index + 1]
                    low = min(left.position_ms, right.position_ms)
                    high = max(left.position_ms, right.position_ms)
                    if low <= current_position <= high:
                        span = max(1, right.position_ms - left.position_ms)
                        fraction = (current_position - left.position_ms) / span
                        left_x = 4.0 + left_index * step
                        right_x = 4.0 + (left_index + 1) * step
                        playhead_x = left_x + fraction * (right_x - left_x)
                        break
                if playhead_x is None:
                    nearest = min(
                        range(len(frames)),
                        key=lambda index: abs(
                            int(frames[index].position_ms) - current_position
                        ),
                    )
                    playhead_x = 4.0 + nearest * step + FILMSTRIP_TILE_WIDTH / 2.0
        if playhead_x is not None:
            painter.setPen(QPen(QColor("#00a9d6"), 4))
            painter.drawLine(
                int(playhead_x),
                1,
                int(playhead_x),
                FILMSTRIP_TILE_HEIGHT + 27,
            )
            painter.setBrush(QColor("#00a9d6"))
            painter.drawPolygon(
                QPolygon(
                    [
                        QPoint(int(playhead_x - 8), 1),
                        QPoint(int(playhead_x + 8), 1),
                        QPoint(int(playhead_x), 12),
                    ]
                )
            )
        painter.end()

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.LeftButton:
            event.ignore()
            return
        # A second press belongs to a double-click when it arrives before the
        # pending single-click timer fires. Cancel the first click now so the
        # double-click handler can perform exactly one seek.
        if self._click_timer.isActive():
            self._click_timer.stop()
            self._pending_click_position = None
        self._dragging = True
        self._press_x = event.pos().x()
        self._last_x = self._press_x
        self._moved = False
        self.setCursor(Qt.ClosedHandCursor)
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        if not self._dragging:
            event.ignore()
            return
        x = event.pos().x()
        delta = x - self._last_x
        if delta:
            self._moved = True
            scroll = self.owner.scroll.horizontalScrollBar()
            # Match the operator's review direction: dragging right advances
            # toward later arrivals, while dragging left goes back in time.
            scroll.setValue(scroll.value() + delta)
        self._last_x = x
        frame = self._frame_at_x(x)
        if frame is not None:
            self._update_status_for_frame(frame)
        if self._moved:
            position = self._position_at_x(x)
            if position is not None:
                self.scrub_position_changed.emit(position)
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() != Qt.LeftButton or not self._dragging:
            event.ignore()
            return
        self._dragging = False
        self.setCursor(Qt.OpenHandCursor)
        frame = self._frame_at_x(event.pos().x())
        if frame is not None:
            self._update_status_for_frame(frame)
            if self._moved or abs(event.pos().x() - self._press_x) >= 4:
                self.position_released.emit(frame.position_ms)
            elif self.owner._marker_mode:
                self.position_released.emit(frame.position_ms)
                if frame.frame_index >= 0 and not frame.image.isNull():
                    frames = self._visual_frames()
                    step = FILMSTRIP_TILE_WIDTH + FILMSTRIP_TILE_GAP
                    index = frames.index(frame)
                    tile_left = 4 + index * step
                    x_normalized = max(
                        0.0,
                        min(
                            1.0,
                            (event.pos().x() - tile_left) / FILMSTRIP_TILE_WIDTH,
                        ),
                    )
                    y_normalized = max(
                        0.0,
                        min(1.0, (event.pos().y() - 4) / FILMSTRIP_TILE_HEIGHT),
                    )
                    self.owner.marker_position_selected.emit(
                        frame.position_ms,
                        x_normalized,
                        y_normalized,
                        frame.frame_index,
                    )
            else:
                self._pending_click_position = frame.position_ms
                self._click_timer.start()
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:
        """Treat a double-click as an immediate preview-to-video seek."""

        if event.button() != Qt.LeftButton:
            event.ignore()
            return
        self._click_timer.stop()
        self._pending_click_position = None
        self._dragging = False
        self.setCursor(Qt.OpenHandCursor)
        frame = self._frame_at_x(event.pos().x())
        if frame is not None:
            self.position_released.emit(frame.position_ms)
            self.position_double_clicked.emit(frame.position_ms)
            self._update_status_for_frame(frame)
        event.accept()


class VideoFilmstripWidget(QFrame):
    """Scrollable, clickable filmstrip backed by one paint-only canvas."""

    position_selected = pyqtSignal(int)
    position_double_clicked = pyqtSignal(int)
    scrub_position_changed = pyqtSignal(int)
    marker_position_selected = pyqtSignal(int, float, float, int)
    direction_changed = pyqtSignal(bool)
    visible_range_changed = pyqtSignal(int, int)
    reload_requested = pyqtSignal()
    ready_changed = pyqtSignal(bool, str)
    prefetch_finished = pyqtSignal(bool, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._worker: VideoFilmstripWorker | None = None
        self._prefetch_worker: VideoFilmstripWorker | None = None
        self._prefetch_path: Path | None = None
        self._prefetch_error = ""
        self._operator_busy = False
        self._frames: list[FilmstripFrame] = []
        self._frames_by_path: dict[Path, dict[int, FilmstripFrame]] = {}
        self._prefetch_positions_by_path: dict[Path, set[int]] = {}
        # Bounded ring for previous/current/next two-minute windows.
        self._retained_windows: list[tuple[Path, int, int]] = []
        self._requested_positions: set[int] = set()
        self._target_positions: tuple[int, ...] = ()
        self._deferred_positions: set[int] = set()
        self._pending_positions: list[int] = []
        self._render_timer_pending = False
        self._video_path: Path | None = None
        self._reverse = False
        self._display_start_ms: int | None = None
        self._display_end_ms: int | None = None
        self._display_reference_ms: int | None = None
        self._display_origin_ms: int | None = None
        self._current_position_ms = -1
        self._first_frame_received = False
        self._expected_positions: set[int] = set()
        self._decode_error = ""
        self._ready = False
        self._align_pending = False
        self._marker_mode = False
        self._marker: tuple[int, float, float, int] | None = None
        self._history_markers: tuple[tuple[int, str], ...] = ()
        self.setObjectName("videoFilmstrip")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumHeight(220)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(4)
        header = QHBoxLayout()
        title = QLabel("时间胶卷")
        self.title_label = title
        title.setStyleSheet("font-weight: 700; color: #334155;")
        header.addWidget(title)
        self.status_label = QLabel("选择连续判读时间窗")
        self.status_label.setStyleSheet("color: #667085; font-size: 9pt;")
        header.addWidget(self.status_label)
        header.addStretch(1)
        self.direction_combo = QComboBox(self)
        self.direction_combo.addItem("第一名 → 最后一名", False)
        self.direction_combo.addItem("最后一名 → 第一名", True)
        self.direction_combo.setToolTip("只改变胶卷显示方向，不改变真实时间和成绩顺序")
        self.direction_combo.currentIndexChanged.connect(self._on_direction_changed)
        self.direction_combo.hide()
        self.reload_button = QPushButton("刷新胶卷", self)
        self.reload_button.setToolTip("重新读取当前连续判读时间窗的缩略图")
        self.reload_button.clicked.connect(self.reload_requested.emit)
        self.reload_button.hide()
        self.mark_button = QPushButton("标线", self)
        self.mark_button.setToolTip("在时间胶卷上点击运动员位置")
        self.mark_button.setCheckable(True)
        self.mark_button.setEnabled(False)
        self.mark_button.clicked.connect(self._toggle_marker_mode)
        self.mark_button.hide()
        self.confirm_button = QPushButton("确认", self)
        self.confirm_button.setToolTip("确认时间胶卷上的当前标线")
        self.confirm_button.setEnabled(False)
        self.confirm_button.hide()
        self.cancel_button = QPushButton("取消", self)
        self.cancel_button.setToolTip("取消当前时间胶卷标线")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.clear_marker)
        self.cancel_button.hide()
        layout.addLayout(header)

        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(False)
        # Keep one visible horizontal bar for fast preview navigation. The
        # camera pane owns the separate frame-accurate judgment timeline.
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.content = FilmstripCanvas(self, self)
        self.scroll.setWidget(self.content)
        self.scroll.viewport().setCursor(Qt.OpenHandCursor)
        self.scroll.horizontalScrollBar().valueChanged.connect(
            self._emit_visible_range
        )
        self.content.position_released.connect(self._on_content_position_selected)
        self.content.position_double_clicked.connect(
            self._on_content_position_double_clicked
        )
        self.content.scrub_position_changed.connect(self.scrub_position_changed.emit)
        self.marker_position_selected.connect(self._on_marker_position_selected)
        layout.addWidget(self.scroll, 1)
        self.full_race = RaceFilmstripPanel(self)
        self.full_race.hide()
        layout.addWidget(self.full_race, 1)

        # Saved judgments belong to the whole camera timeline, so keep this
        # track independent of thumbnail loading, clearing and window changes.
        self.judgment_track = CameraJudgmentTrack(self)
        self.judgment_track.hide()
        layout.addWidget(self.judgment_track)

    def enable_full_race(self) -> None:
        self.scroll.hide()
        self.status_label.hide()
        self.title_label.hide()
        self.full_race.show()
        self.judgment_track.embed_header(self.full_race.toolbar)
        self.judgment_track.set_expanded(False)

    def closeEvent(self, event) -> None:
        self.full_race.stop()
        self.stop()
        self.stop_prefetch()
        super().closeEvent(event)

    def _on_content_position_selected(self, position_ms: int) -> None:
        if self._expected_positions and not self._ready:
            return
        position_ms = int(position_ms)
        self._load_deferred_visible_range(
            position_ms - DEFAULT_FILMSTRIP_INTERVAL_MS,
            position_ms + DEFAULT_FILMSTRIP_INTERVAL_MS,
        )
        self.position_selected.emit(position_ms)

    def _on_content_position_double_clicked(self, position_ms: int) -> None:
        if self._expected_positions and not self._ready:
            return
        self.position_double_clicked.emit(int(position_ms))

    @property
    def is_ready(self) -> bool:
        return bool(self._ready)

    def _set_ready(self, ready: bool, message: str) -> None:
        next_ready = bool(ready)
        changed = next_ready != self._ready
        self._ready = next_ready
        if message:
            self.status_label.setText(str(message))
        self.mark_button.setEnabled(next_ready and bool(self._frames))
        if changed or message:
            self.ready_changed.emit(next_ready, str(message))

    def _on_marker_position_selected(
        self,
        position_ms: int,
        x_normalized: float,
        y_normalized: float,
        frame_index: int,
    ) -> None:
        self.set_marker(position_ms, x_normalized, y_normalized, frame_index)

    def _toggle_marker_mode(self, checked: bool) -> None:
        self._marker_mode = bool(checked)
        if not self._marker_mode:
            self.clear_marker()
        self.content.setCursor(
            Qt.CrossCursor if self._marker_mode else Qt.OpenHandCursor
        )

    def set_marker_mode(self, enabled: bool) -> None:
        self.mark_button.setChecked(bool(enabled))

    def set_marker(
        self,
        position_ms: int,
        x_normalized: float,
        y_normalized: float,
        frame_index: int,
    ) -> None:
        self._marker = (
            int(position_ms),
            max(0.0, min(1.0, float(x_normalized))),
            max(0.0, min(1.0, float(y_normalized))),
            int(frame_index),
        )
        self.mark_button.setChecked(True)
        self._marker_mode = True
        self.mark_button.setEnabled(True)
        self.confirm_button.setEnabled(True)
        self.cancel_button.setEnabled(True)
        self.content.update()

    def clear_marker(self) -> None:
        self._marker = None
        self._marker_mode = False
        self.mark_button.setChecked(False)
        self.confirm_button.setEnabled(False)
        self.cancel_button.setEnabled(False)
        self.mark_button.setEnabled(self._ready and bool(self._frames))
        self.content.setCursor(Qt.OpenHandCursor)
        self.content.update()

    def set_history_markers(self, markers: Iterable[tuple[int, str]]) -> None:
        """Render confirmed timeline markers without replacing the edit marker."""

        self._history_markers = tuple(
            (max(0, int(position_ms)), str(label or "待补录"))
            for position_ms, label in markers
        )
        self.content.update()

    def _on_direction_changed(self, index: int) -> None:
        self._reverse = bool(self.direction_combo.itemData(index))
        self.direction_changed.emit(self._reverse)
        self._align_pending = True
        self.content.refresh_geometry()
        self._schedule_render_pending()

    def set_current_position(self, position_ms: int) -> None:
        self._current_position_ms = int(position_ms)
        self._display_reference_ms = int(position_ms)
        self._align_pending = True
        self.content.update()
        self._align_current_position_to_center()
        self._schedule_render_pending()

    def set_display_origin(self, origin_ms: int | None) -> None:
        self._display_origin_ms = (
            None if origin_ms is None else int(origin_ms)
        )
        self.content.update()

    def _emit_visible_range(self) -> None:
        positions = self._target_positions or tuple(
            int(frame.position_ms) for frame in self.content._visual_frames()
        )
        if self._reverse and self._target_positions:
            positions = tuple(reversed(positions))
        if not positions:
            return
        step = FILMSTRIP_TILE_WIDTH + FILMSTRIP_TILE_GAP
        scroll_left = self.scroll.horizontalScrollBar().value()
        viewport_width = max(1, self.scroll.viewport().width())
        first_index = max(0, min(len(positions) - 1, int(scroll_left // step)))
        last_index = max(
            first_index,
            min(len(positions) - 1, int((scroll_left + viewport_width) // step)),
        )
        visible_positions = (
            int(positions[first_index]),
            int(positions[last_index]),
        )
        self._load_deferred_visible_range(
            min(visible_positions), max(visible_positions)
        )
        self.visible_range_changed.emit(
            min(visible_positions), max(visible_positions)
        )

    def _load_deferred_visible_range(self, start_ms: int, end_ms: int) -> None:
        if not self._deferred_positions:
            return
        positions = tuple(
            position
            for position in self._target_positions
            if start_ms <= position <= end_ms
            and position in self._deferred_positions
        )
        if not positions:
            return
        self._deferred_positions.difference_update(positions)
        self.append_positions(self._video_path, positions)

    def _remember_window(self, video_path: Path, start_ms: int, end_ms: int) -> None:
        """Retain at most three neighbouring windows and evict older frames."""
        window = (Path(video_path), max(0, int(start_ms)), max(0, int(end_ms)))
        self._retained_windows = [item for item in self._retained_windows if item != window]
        self._retained_windows.append(window)
        active_window = None
        if self._video_path is not None and self._display_end_ms is not None:
            active_window = (
                Path(self._video_path),
                max(0, int(self._display_start_ms or 0)),
                max(0, int(self._display_end_ms)),
            )
        if active_window is not None and active_window in self._retained_windows:
            # Prefetch may add a fourth window, but it must never evict the
            # currently displayed judgment window.
            others = [item for item in self._retained_windows if item != active_window]
            self._retained_windows = others[-2:] + [active_window]
        else:
            self._retained_windows = self._retained_windows[-3:]
        ranges_by_path: dict[Path, list[tuple[int, int]]] = {}
        for path, start, end in self._retained_windows:
            ranges_by_path.setdefault(path, []).append((start, end))
        for path, frames in tuple(self._frames_by_path.items()):
            ranges = ranges_by_path.get(path)
            if not ranges:
                self._frames_by_path.pop(path, None)
                continue
            self._frames_by_path[path] = {
                position: frame for position, frame in frames.items()
                if any(start <= int(position) <= end for start, end in ranges)
            }
        for path, positions in tuple(self._prefetch_positions_by_path.items()):
            ranges = ranges_by_path.get(path)
            if not ranges:
                self._prefetch_positions_by_path.pop(path, None)
                continue
            self._prefetch_positions_by_path[path] = {
                position for position in positions
                if any(start <= int(position) <= end for start, end in ranges)
            }

    def _align_current_position_to_center(self) -> None:
        if not self._frames or self._current_position_ms < 0:
            return
        frames = self.content._visual_frames()
        if not frames:
            return
        target_index, _target = min(
            enumerate(frames),
            key=lambda item: abs(int(item[1].position_ms) - self._current_position_ms),
        )
        step = FILMSTRIP_TILE_WIDTH + FILMSTRIP_TILE_GAP
        target_center = 4 + target_index * step + FILMSTRIP_TILE_WIDTH / 2.0
        viewport_width = self.scroll.viewport().width()
        scroll_bar = self.scroll.horizontalScrollBar()
        scroll_value = max(0, int(round(target_center - max(1, viewport_width) / 2.0)))
        scroll_bar.setValue(min(scroll_bar.maximum(), scroll_value))
        self._emit_visible_range()

    def clear(self, message: str = "选择连续判读时间窗") -> None:
        self.stop()
        self.stop_prefetch()
        self._prefetch_error = ""
        self._frames.clear()
        self._frames_by_path.clear()
        self._prefetch_positions_by_path.clear()
        self._retained_windows.clear()
        self._requested_positions.clear()
        self._target_positions = ()
        self._deferred_positions.clear()
        self._pending_positions.clear()
        self._video_path = None
        self._display_start_ms = None
        self._display_end_ms = None
        self._display_reference_ms = None
        self._display_origin_ms = None
        self._first_frame_received = False
        self._expected_positions.clear()
        self._decode_error = ""
        self._history_markers = ()
        self._set_ready(False, message)
        self._align_pending = False
        self.clear_marker()
        self.content.refresh_geometry()
        self.status_label.setText(message)

    def stop(self) -> None:
        worker = self._worker
        self._worker = None
        if worker is None:
            return
        worker.request_stop()
        for signal in (worker.frame_ready, worker.failed, worker.finished):
            try:
                signal.disconnect()
            except (TypeError, RuntimeError):
                pass
        if not worker.isRunning():
            worker.deleteLater()
            return
        # VideoCapture may still be inside a slow seek/read. Never wait for it
        # on the GUI thread: athlete selection and the primary judgment frame
        # must remain responsive. Retired workers no longer have result
        # signals connected, so late thumbnails cannot update the new strip.
        # Detach it from the widget as well, so closing the dialog cannot
        # destroy a QThread that is still unwinding its decoder call.
        retire_qthread(worker)

    def stop_prefetch(self) -> None:
        worker = self._prefetch_worker
        self._prefetch_worker = None
        self._prefetch_path = None
        if worker is None:
            return
        worker.request_stop()
        for signal in (worker.frame_ready, worker.failed, worker.finished):
            try:
                signal.disconnect()
            except (TypeError, RuntimeError):
                pass
        if worker.isRunning():
            retire_qthread(worker)
        else:
            worker.deleteLater()

    def set_operator_busy(self, busy: bool) -> None:
        """Pause low-priority future-window decoding during active review."""

        self._operator_busy = bool(busy)
        if self._operator_busy:
            self.stop_prefetch()

    def prefetch(
        self,
        video_path: Path,
        start_ms: int,
        end_ms: int,
    ) -> None:
        """Decode a future window into cache without touching the active strip."""

        if self._operator_busy:
            return
        path = Path(video_path)
        positions = tuple(
            int(value)
            for value in filmstrip_positions(start_ms, end_ms)
        )
        self._remember_window(path, int(start_ms), int(end_ms))
        self._prefetch_positions_by_path[path] = set(positions)
        cache = self._frames_by_path.setdefault(path, {})
        missing = tuple(sorted(position for position in positions if position not in cache))
        self.stop_prefetch()
        self._prefetch_error = ""
        if not missing:
            self.prefetch_finished.emit(True, "后续窗口已预取")
            return
        self._prefetch_path = path
        worker = VideoFilmstripWorker(
            path,
            missing,
            self,
            sequential_window=True,
        )
        worker.frame_ready.connect(
            lambda image, position_ms, frame_index, target=path:
            self._on_prefetch_frame_ready(
                target,
                image,
                position_ms,
                frame_index,
            )
        )
        worker.failed.connect(
            lambda message, target=worker: self._on_prefetch_failed(target, message)
        )
        worker.finished.connect(
            lambda target=worker: self._on_prefetch_worker_finished(target)
        )
        self._prefetch_worker = worker
        track_qthread(worker)
        worker.start(QThread.LowPriority)

    def _on_prefetch_frame_ready(
        self,
        path: Path,
        image: QImage,
        position_ms: int,
        frame_index: int,
    ) -> None:
        cache = self._frames_by_path.setdefault(Path(path), {})
        cache.setdefault(
            int(position_ms),
            FilmstripFrame(MediaPositionMs(int(position_ms)), int(frame_index), image),
        )

    def _on_prefetch_failed(self, worker, message: str) -> None:
        if worker is not self._prefetch_worker:
            return
        self._prefetch_error = str(message)
        self.prefetch_finished.emit(False, str(message))

    def _on_prefetch_worker_finished(self, worker) -> None:
        if worker is not self._prefetch_worker:
            return
        path = self._prefetch_path
        self._prefetch_worker = None
        self._prefetch_path = None
        if path is None:
            return
        worker.deleteLater()
        if self._prefetch_error:
            return
        self.prefetch_finished.emit(True, "后续窗口已预取")

    def load(
        self,
        video_path: Path,
        start_ms: int,
        end_ms: int,
        *,
        positions_ms: Iterable[int] = (),
        origin_ms: int | None = None,
        defer_remaining: bool = False,
        require_complete: bool = False,
        interval_ms: int = DEFAULT_FILMSTRIP_INTERVAL_MS,
    ) -> None:
        path = Path(video_path)
        if self._video_path is not None and self._display_end_ms is not None:
            self._remember_window(
                self._video_path,
                int(self._display_start_ms or 0),
                int(self._display_end_ms),
            )
        self.stop()
        self.stop_prefetch()
        self._set_ready(False, "正在准备当前两分钟胶卷...")
        self._decode_error = ""
        if self._video_path != path:
            self._frames.clear()
            self._video_path = path
            self._display_origin_ms = (
                None if origin_ms is None else int(origin_ms)
            )
        elif origin_ms is not None:
            self._display_origin_ms = int(origin_ms)
        self._display_start_ms = max(0, int(start_ms))
        self._display_end_ms = max(self._display_start_ms, int(end_ms))
        self._remember_window(path, self._display_start_ms, self._display_end_ms)
        cached = self._frames_by_path.get(path, {})
        self._frames = sorted(cached.values(), key=lambda value: value.position_ms)
        if self._current_position_ms < 0:
            self._display_reference_ms = self._display_start_ms
        self.content.refresh_geometry()
        self._align_pending = True
        self._schedule_render_pending()
        self._requested_positions = {frame.position_ms for frame in self._frames}
        self.mark_button.setEnabled(self._ready and bool(self._frames))
        self._pending_positions.clear()
        self._deferred_positions.clear()
        positions = tuple(
            int(value)
            for value in filmstrip_positions(
                start_ms,
                end_ms,
                interval_ms=interval_ms,
                anchors=positions_ms,
            )
        )
        self._target_positions = positions
        self._expected_positions = set(positions) if require_complete else set()
        # The target list defines the full scrollable timeline, including
        # deferred thumbnails. Refresh after assigning it so the scrollbar
        # exposes the complete range before the first deferred-load pass.
        self.content.refresh_geometry()
        if require_complete:
            missing = tuple(
                sorted(
                    position
                    for position in positions
                    if position not in self._requested_positions
                )
            )
        else:
            missing = tuple(
                position
                for position in positions
                if position not in self._requested_positions
            )
        if missing:
            if require_complete:
                self._first_frame_received = False
                # Prepare the active window as one complete batch. A first
                # handful of thumbnails is not enough to claim readiness.
                self._requested_positions.update(missing)
                self._start_worker(missing, sequential_window=True)
            else:
                priority = tuple(
                    sorted(
                        missing,
                        key=lambda value: (
                            abs(value - self._current_position_ms),
                            value,
                        ),
                    )
                )
                self._first_frame_received = False
                initial_positions = priority[:FILMSTRIP_INITIAL_BATCH]
                self._requested_positions.update(initial_positions)
                self._start_worker(initial_positions)
                if not defer_remaining:
                    self._requested_positions.update(
                        priority[FILMSTRIP_INITIAL_BATCH:]
                    )
                    self._pending_positions.extend(
                        priority[FILMSTRIP_INITIAL_BATCH:]
                    )
                else:
                    self._deferred_positions.update(
                        priority[FILMSTRIP_INITIAL_BATCH:]
                    )
        else:
            if require_complete:
                self._set_ready(True, "当前两分钟胶卷已就绪，可以处理")
            else:
                self._set_ready(True, f"{len(self._frames)} 个预览点")

    def append_positions(self, video_path: Path, positions_ms: Iterable[int]) -> None:
        if self._video_path != Path(video_path):
            return
        new_positions = sorted({max(0, int(value)) for value in positions_ms if int(value) not in self._requested_positions and int(value) not in self._pending_positions})
        if not new_positions:
            return
        self._requested_positions.update(new_positions)
        self._deferred_positions.difference_update(new_positions)
        if self._worker is not None and self._worker.isRunning():
            self._pending_positions.extend(new_positions)
            return
        self._start_worker(new_positions)

    def _start_worker(
        self,
        positions: Iterable[int],
        *,
        sequential_window: bool = False,
    ) -> None:
        if self._video_path is None:
            return
        worker = VideoFilmstripWorker(
            self._video_path,
            positions,
            self,
            sequential_window=sequential_window,
        )
        worker.frame_ready.connect(self._on_frame_ready)
        worker.failed.connect(self._on_worker_failed)
        worker.finished.connect(self._on_worker_finished)
        self._worker = worker
        track_qthread(worker)
        worker.start()

    def _on_worker_failed(self, message: str) -> None:
        self._decode_error = str(message)
        self._set_ready(False, f"胶卷解码失败：{self._decode_error}")

    def _on_worker_finished(self) -> None:
        if self.sender() is not self._worker:
            return
        self._worker = None
        if self._pending_positions:
            pending = tuple(self._pending_positions)
            self._pending_positions.clear()
            self._start_worker(pending)
            return
        decoded_positions = set(
            self._frames_by_path.get(self._video_path, {})
        )
        if not self._expected_positions:
            self._set_ready(True, f"{len(self._frames)} 个预览点")
            return
        missing = self._expected_positions.difference(decoded_positions)
        if self._decode_error or missing:
            detail = self._decode_error or f"仍有 {len(missing)} 个预览点未解码"
            self._set_ready(False, f"当前窗口不完整：{detail}")
            return
        self._set_ready(True, "当前两分钟胶卷已就绪，可以处理")

    def _on_frame_ready(self, image: QImage, position_ms: int, frame_index: int) -> None:
        if self._video_path is None:
            return
        frame = FilmstripFrame(
            MediaPositionMs(int(position_ms)),
            int(frame_index),
            image,
        )
        frame_cache = self._frames_by_path.setdefault(self._video_path, {})
        if frame.position_ms in frame_cache:
            return
        frame_cache[frame.position_ms] = frame
        self._frames = sorted(frame_cache.values(), key=lambda value: value.position_ms)
        if not self._expected_positions:
            self._ready = True
        else:
            decoded_count = len(self._expected_positions.intersection(frame_cache))
            self.status_label.setText(
                f"正在准备当前两分钟胶卷：{decoded_count} / "
                f"{len(self._expected_positions)}"
            )
        self.mark_button.setEnabled(self._ready)
        if not self._first_frame_received:
            self._first_frame_received = True
        self._schedule_render_pending()

    def _schedule_render_pending(self) -> None:
        if self._render_timer_pending:
            return
        self._render_timer_pending = True
        QTimer.singleShot(50, self._flush_render_pending)

    def _flush_render_pending(self) -> None:
        self._render_timer_pending = False
        self.content.refresh_geometry()
        if self._align_pending and self._frames:
            self._align_pending = False
            self._align_current_position_to_center()
        else:
            self._emit_visible_range()

    def closeEvent(self, event) -> None:
        self.stop()
        self.stop_prefetch()
        super().closeEvent(event)


__all__ = [
    "DEFAULT_FILMSTRIP_INTERVAL_MS",
    "CONTINUOUS_FILMSTRIP_INTERVAL_MS",
    "FILMSTRIP_INITIAL_BATCH",
    "FilmstripFrame",
    "FilmstripCanvas",
    "VideoFilmstripWidget",
    "filmstrip_positions",
]
