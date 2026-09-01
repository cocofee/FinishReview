"""Incremental chronological thumbnail filmstrip for video review."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import cv2
from PyQt5.QtCore import QThread, QTimer, Qt, QRectF, pyqtSignal
from PyQt5.QtGui import QColor, QImage, QPainter, QPen
from PyQt5.QtWidgets import QComboBox, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget

DEFAULT_FILMSTRIP_INTERVAL_MS = 2_000
FILMSTRIP_TILE_WIDTH = 360
FILMSTRIP_TILE_HEIGHT = 240
FILMSTRIP_TILE_GAP = 8
FILMSTRIP_INITIAL_BATCH = 12


@dataclass(frozen=True, slots=True)
class FilmstripFrame:
    position_ms: int
    frame_index: int
    image: QImage


def filmstrip_positions(start_ms: int, end_ms: int, *, interval_ms: int = DEFAULT_FILMSTRIP_INTERVAL_MS, anchors: Iterable[int] = ()) -> tuple[int, ...]:
    start = max(0, int(start_ms))
    end = max(start, int(end_ms))
    interval = max(1, int(interval_ms))
    anchor_values = [max(start, min(end, int(anchor))) for anchor in anchors]
    values = list(range(start, end + 1, interval))
    if not values or values[-1] != end:
        values.append(end)
    return tuple(sorted(set(values).union(anchor_values)))


class VideoFilmstripWorker(QThread):
    frame_ready = pyqtSignal(QImage, int, int)
    failed = pyqtSignal(str)

    def __init__(self, video_path: Path, positions_ms: Iterable[int], parent=None):
        super().__init__(parent)
        self.video_path = Path(video_path)
        self.positions_ms = tuple(int(value) for value in positions_ms)
        self._stop_requested = False

    def request_stop(self) -> None:
        self._stop_requested = True

    def run(self) -> None:
        capture = cv2.VideoCapture(str(self.video_path))
        if not capture.isOpened():
            self.failed.emit(f"Unable to open video: {self.video_path}")
            return
        try:
            fps = max(0.1, float(capture.get(cv2.CAP_PROP_FPS) or 0.0))
            for position_ms in self.positions_ms:
                if self._stop_requested:
                    return
                frame_index = max(0, int(round(position_ms * fps / 1000.0)))
                capture.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
                ok, frame = capture.read()
                if not ok or frame is None:
                    continue
                height, width = frame.shape[:2]
                if width <= 0 or height <= 0:
                    continue
                scale = min(1.0, FILMSTRIP_TILE_WIDTH / float(width))
                size = (max(1, int(round(width * scale))), max(1, int(round(height * scale))))
                if size != (width, height):
                    frame = cv2.resize(frame, size, interpolation=cv2.INTER_AREA)
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                image = QImage(rgb.data, rgb.shape[1], rgb.shape[0], rgb.strides[0], QImage.Format_RGB888).copy()
                self.frame_ready.emit(image, position_ms, frame_index)
        except Exception as error:  # pragma: no cover
            self.failed.emit(str(error))
        finally:
            capture.release()


class FilmstripCanvas(QWidget):
    """One paint-only surface; avoids hundreds of native child widgets."""

    position_released = pyqtSignal(int)

    def __init__(self, owner, parent=None):
        super().__init__(parent)
        self.owner = owner
        self._dragging = False
        self._last_x = 0
        self.setMouseTracking(True)
        self.setCursor(Qt.OpenHandCursor)
        self.setMinimumHeight(FILMSTRIP_TILE_HEIGHT + 34)

    def _visual_frames(self) -> list[FilmstripFrame]:
        return sorted(self.owner._frames, key=lambda value: value.position_ms, reverse=self.owner._reverse)

    def _frame_at_x(self, x: float) -> FilmstripFrame | None:
        frames = self._visual_frames()
        if not frames:
            return None
        step = FILMSTRIP_TILE_WIDTH + FILMSTRIP_TILE_GAP
        # Convert viewport coordinates back to canvas coordinates after the
        # horizontal hand-scroll; otherwise release selects a stale tile.
        scroll_offset = self.owner.scroll.horizontalScrollBar().value()
        index = max(
            0,
            min(len(frames) - 1, int((x + scroll_offset - 4) // step)),
        )
        return frames[index]

    def refresh_geometry(self) -> None:
        count = len(self.owner._frames)
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
        first = max(0, int((event.rect().left() - 4) // step) - 1)
        last = min(len(frames), int((event.rect().right() - 4) // step) + 2)
        for index, frame in enumerate(frames[first:last], start=first):
            x = 4 + index * step
            target = QRectF(x, 4, FILMSTRIP_TILE_WIDTH, FILMSTRIP_TILE_HEIGHT)
            if frame.image.isNull():
                painter.fillRect(target, QColor("#e2e8f0"))
            else:
                painter.drawImage(target, frame.image)
            selected = frame.position_ms == self.owner._current_position_ms
            painter.setPen(QPen(QColor("#2563eb" if selected else "#cbd5e1"), 3 if selected else 1))
            painter.drawRect(target)
            painter.setPen(QColor("#475569"))
            painter.drawText(QRectF(x, FILMSTRIP_TILE_HEIGHT + 7, FILMSTRIP_TILE_WIDTH, 20), Qt.AlignCenter, f"{frame.position_ms / 1000.0:.3f}s")
        painter.end()

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.LeftButton:
            event.ignore()
            return
        self._dragging = True
        self._last_x = event.pos().x()
        self.setCursor(Qt.ClosedHandCursor)
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        if not self._dragging:
            event.ignore()
            return
        x = event.pos().x()
        delta = x - self._last_x
        if delta:
            scroll = self.owner.scroll.horizontalScrollBar()
            # Match the operator's review direction: dragging right advances
            # toward later arrivals, while dragging left goes back in time.
            scroll.setValue(scroll.value() + delta)
        self._last_x = x
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() != Qt.LeftButton or not self._dragging:
            event.ignore()
            return
        self._dragging = False
        self.setCursor(Qt.OpenHandCursor)
        frame = self._frame_at_x(event.pos().x())
        if frame is not None:
            self.position_released.emit(frame.position_ms)
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:
        """Treat a double-click as an immediate preview-to-video seek."""

        if event.button() != Qt.LeftButton:
            event.ignore()
            return
        self._dragging = False
        self.setCursor(Qt.OpenHandCursor)
        frame = self._frame_at_x(event.pos().x())
        if frame is not None:
            self.position_released.emit(frame.position_ms)
        event.accept()


class VideoFilmstripWidget(QFrame):
    """Scrollable, clickable filmstrip backed by one paint-only canvas."""

    position_selected = pyqtSignal(int)
    direction_changed = pyqtSignal(bool)
    reload_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._worker: VideoFilmstripWorker | None = None
        self._frames: list[FilmstripFrame] = []
        self._frames_by_path: dict[Path, dict[int, FilmstripFrame]] = {}
        self._requested_positions: set[int] = set()
        self._pending_positions: list[int] = []
        self._render_timer_pending = False
        self._video_path: Path | None = None
        self._reverse = False
        self._current_position_ms = -1
        self._first_frame_received = False
        self.setObjectName("videoFilmstrip")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumHeight(220)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(4)
        header = QHBoxLayout()
        title = QLabel("时间胶卷")
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
        header.addWidget(self.direction_combo)
        self.reload_button = QPushButton("刷新胶卷", self)
        self.reload_button.setToolTip("重新读取当前连续判读时间窗的缩略图")
        self.reload_button.clicked.connect(self.reload_requested.emit)
        header.addWidget(self.reload_button)
        layout.addLayout(header)

        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(False)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.content = FilmstripCanvas(self, self)
        self.scroll.setWidget(self.content)
        self.scroll.viewport().setCursor(Qt.OpenHandCursor)
        self.content.position_released.connect(self.position_selected.emit)
        layout.addWidget(self.scroll, 1)

    def _on_direction_changed(self, index: int) -> None:
        self._reverse = bool(self.direction_combo.itemData(index))
        self.direction_changed.emit(self._reverse)
        self.content.refresh_geometry()

    def set_current_position(self, position_ms: int) -> None:
        self._current_position_ms = int(position_ms)
        self.content.update()

    def clear(self, message: str = "选择连续判读时间窗") -> None:
        self.stop()
        self._frames.clear()
        self._frames_by_path.clear()
        self._requested_positions.clear()
        self._pending_positions.clear()
        self._video_path = None
        self._first_frame_received = False
        self.content.refresh_geometry()
        self.status_label.setText(message)

    def stop(self) -> None:
        worker = self._worker
        self._worker = None
        if worker is None:
            return
        worker.request_stop()
        for signal in (worker.frame_ready, worker.failed):
            try:
                signal.disconnect()
            except (TypeError, RuntimeError):
                pass
        if worker.isRunning():
            worker.wait(2_000)
        worker.deleteLater()

    def load(self, video_path: Path, start_ms: int, end_ms: int, *, positions_ms: Iterable[int] = ()) -> None:
        path = Path(video_path)
        self.stop()
        if self._video_path != path:
            # Retain only the active recording file to bound memory across a
            # long race with many recording segments.
            self._frames_by_path.clear()
            self._frames.clear()
            self._video_path = path
        self._frames = sorted(self._frames_by_path.get(path, {}).values(), key=lambda value: value.position_ms)
        self.content.refresh_geometry()
        self._requested_positions = {frame.position_ms for frame in self._frames}
        self._pending_positions.clear()
        self.status_label.setText("正在生成时间胶卷...")
        positions = filmstrip_positions(start_ms, end_ms, anchors=positions_ms)
        missing = tuple(position for position in positions if position not in self._requested_positions)
        self._requested_positions.update(missing)
        if missing:
            # Prioritize the selected athlete's neighborhood so a usable
            # preview appears quickly; continue the remaining strip later.
            priority = tuple(sorted(missing, key=lambda value: (abs(value - self._current_position_ms), value)))
            self._first_frame_received = False
            self._start_worker(priority[:FILMSTRIP_INITIAL_BATCH])
            self._pending_positions.extend(priority[FILMSTRIP_INITIAL_BATCH:])
        else:
            self.status_label.setText(f"{len(self._frames)} 个预览点")

    def append_positions(self, video_path: Path, positions_ms: Iterable[int]) -> None:
        if self._video_path != Path(video_path):
            return
        new_positions = sorted({max(0, int(value)) for value in positions_ms if int(value) not in self._requested_positions and int(value) not in self._pending_positions})
        if not new_positions:
            return
        self._requested_positions.update(new_positions)
        if self._worker is not None and self._worker.isRunning():
            self._pending_positions.extend(new_positions)
            return
        self._start_worker(new_positions)

    def _start_worker(self, positions: Iterable[int]) -> None:
        if self._video_path is None:
            return
        worker = VideoFilmstripWorker(self._video_path, positions, self)
        worker.frame_ready.connect(self._on_frame_ready)
        worker.failed.connect(self._on_worker_failed)
        worker.finished.connect(self._on_worker_finished)
        self._worker = worker
        worker.start()

    def _on_worker_failed(self, message: str) -> None:
        self.status_label.setText(str(message))

    def _on_worker_finished(self) -> None:
        if self.sender() is not self._worker:
            return
        self._worker = None
        if self._pending_positions:
            pending = tuple(self._pending_positions)
            self._pending_positions.clear()
            self._start_worker(pending)
            return
        self.status_label.setText(f"{len(self._frames)} 个预览点")

    def _on_frame_ready(self, image: QImage, position_ms: int, frame_index: int) -> None:
        if self._video_path is None:
            return
        frame = FilmstripFrame(int(position_ms), int(frame_index), image)
        frame_cache = self._frames_by_path.setdefault(self._video_path, {})
        if frame.position_ms in frame_cache:
            return
        frame_cache[frame.position_ms] = frame
        self._frames = sorted(frame_cache.values(), key=lambda value: value.position_ms)
        if not self._first_frame_received:
            self._first_frame_received = True
            self.status_label.setText("预览已就绪，后台生成剩余胶卷...")
        self._schedule_render_pending()

    def _schedule_render_pending(self) -> None:
        if self._render_timer_pending:
            return
        self._render_timer_pending = True
        QTimer.singleShot(50, self._flush_render_pending)

    def _flush_render_pending(self) -> None:
        self._render_timer_pending = False
        self.content.refresh_geometry()

    def closeEvent(self, event) -> None:
        self.stop()
        super().closeEvent(event)


__all__ = ["DEFAULT_FILMSTRIP_INTERVAL_MS", "FILMSTRIP_INITIAL_BATCH", "FilmstripFrame", "FilmstripCanvas", "VideoFilmstripWidget", "filmstrip_positions"]
