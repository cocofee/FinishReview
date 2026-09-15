"""Virtual, chronological full-race filmstrip; judgments stay in camera 1."""

from __future__ import annotations

from bisect import bisect_right
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import heapq
import math
from pathlib import Path

import cv2
from PyQt5.QtCore import QPointF, QRectF, Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QImage, QPainter, QPen
from PyQt5.QtWidgets import (
    QAbstractScrollArea, QComboBox, QHBoxLayout, QLabel, QPushButton,
    QShortcut, QVBoxLayout, QWidget,
)
from PyQt5.QtGui import QKeySequence

from .thread_lifecycle import retire_qthread, track_qthread
from .video_timeline import DEFAULT_CLOCK_SOURCE, PassageVideoLocation
from .filmstrip_checks import FilmstripCheckStore, merge_ranges, subtract_ranges

TILE_GAP = 8
IMAGE_TOP = 24
IMAGE_FOOTER = 24
MAX_CACHE = 160
MAX_CACHE_BYTES = 64 * 1024 * 1024
MAX_BATCH = 16
BEIJING = timezone(timedelta(hours=8))


def format_time(timestamp: int, *, date: bool = False) -> str:
    try:
        return datetime.fromtimestamp(timestamp / 1000, BEIJING).strftime(
            "%m-%d %H:%M:%S.%f" if date else "%H:%M:%S.%f",
        )[:-3]
    except (OSError, ValueError, OverflowError):
        return f"{timestamp} ms"


def format_duration(milliseconds):
    seconds = max(0, milliseconds) / 1000
    if seconds < 60:
        return f"{seconds:g} 秒"
    minutes, seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return (f"{hours} 小时 " if hours else "") + f"{minutes} 分 {seconds} 秒"


@dataclass(frozen=True)
class FilmstripSource:
    location: PassageVideoLocation
    start_ms: int
    end_ms: int
    available: bool
    priority: int = 0

    @property
    def key(self):
        return (str(self.location.video_path.absolute()), self.start_ms,
                self.location.segment.segment_id, self.end_ms)


@dataclass(frozen=True)
class RecordingSpan:
    start_ms: int
    end_ms: int
    source: FilmstripSource | None


class RaceRecordingIndex:
    """Non-overlapping recording spans, including uncompressed real gaps."""

    def __init__(self, sources=()):
        self.sources = tuple(sorted(sources, key=lambda source: (source.start_ms, source.key)))
        events = {}
        for index, source in enumerate(self.sources):
            if source.end_ms <= source.start_ms:
                continue
            events.setdefault(source.start_ms, []).append((True, index))
            events.setdefault(source.end_ms, []).append((False, index))
        points = sorted(events)
        active = set()
        heap = []
        spans = []
        for point_index, point in enumerate(points[:-1]):
            for begins, index in events[point]:
                if begins:
                    active.add(index)
                    source = self.sources[index]
                    heapq.heappush(heap, (not source.available, source.priority,
                                         -(source.end_ms - source.start_ms), index))
                else:
                    active.discard(index)
            while heap and heap[0][-1] not in active:
                heapq.heappop(heap)
            source = self.sources[heap[0][-1]] if heap else None
            end = points[point_index + 1]
            if spans and spans[-1].source == source:
                spans[-1] = RecordingSpan(spans[-1].start_ms, end, source)
            else:
                spans.append(RecordingSpan(point, end, source))
        self.spans = tuple(spans)
        self.starts = tuple(span.start_ms for span in spans)
        self.start_ms = points[0] if points else 0
        self.end_ms = points[-1] if points else 0

    def span_at(self, timestamp):
        index = bisect_right(self.starts, timestamp) - 1
        if index >= 0 and timestamp < self.spans[index].end_ms:
            return self.spans[index]
        return None

    def sample_at(self, timestamp, interval_ms):
        span = self.span_at(timestamp)
        if span is not None and span.source is not None:
            return span.source, timestamp
        # Even a recording shorter than the selected spacing gets a tile.
        index = bisect_right(self.starts, timestamp)
        if index < len(self.spans):
            next_span = self.spans[index]
            if next_span.start_ms < timestamp + interval_ms and next_span.source is not None:
                return next_span.source, next_span.start_ms
        return None, timestamp


def recording_sources(store, camera_index: int, race_id: str = ""):
    """Use recording coverage, never the roster or its filters, for the rail."""
    sources = []
    pending = 0
    for segment in store.segments():
        if segment.camera_index != camera_index or segment.clock_source != DEFAULT_CLOCK_SOURCE:
            continue
        if race_id and segment.race_id and segment.race_id != race_id:
            continue
        if segment.media_started_at_ms is None or segment.media_duration_ms is None:
            pending += 1
            continue
        start = int(segment.media_started_at_ms)
        end = start + int(segment.media_duration_ms)
        path = store.resolve_video_path(segment)
        available = path.is_file()
        continuous = "archive" in path.stem or "archive" in segment.end_reason
        priority = 0 if continuous else 2 if path.suffix.lower() == ".m3u8" else 1
        location = PassageVideoLocation(segment, path, 0, 0, 0,
                                        segment.timing_error_ms, "located" if available else "missing_file")
        sources.append(FilmstripSource(location, start, end, available, priority))
    return tuple(sources), pending


@dataclass(frozen=True)
class RaceFilmstripFrame:
    source: FilmstripSource
    requested_ms: int
    position_ms: int
    frame_index: int
    image: QImage

    @property
    def recorder_time_ms(self):
        return self.source.start_ms + self.position_ms

    @property
    def key(self):
        return self.source.key, self.requested_ms


class RaceThumbnailWorker(QThread):
    frame_ready = pyqtSignal(object)
    failed = pyqtSignal(object, str)

    def __init__(self, jobs, parent=None):
        super().__init__(parent)
        self.jobs = tuple(jobs)
        self._stop_requested = False

    def request_stop(self):
        self._stop_requested = True
        self.requestInterruption()

    def run(self):
        groups = {}
        for source, timestamp in self.jobs:
            groups.setdefault(source.key, []).append((source, timestamp))
        for jobs in groups.values():
            if self._stop_requested:
                return
            source = jobs[0][0]
            capture = cv2.VideoCapture(str(source.location.video_path))
            completed = set()
            try:
                if not capture.isOpened():
                    raise ValueError("录像无法打开")
                fps = float(capture.get(cv2.CAP_PROP_FPS))
                if not math.isfinite(fps) or fps <= 0:
                    raise ValueError("录像帧率无法验证")
                count = capture.get(cv2.CAP_PROP_FRAME_COUNT)
                next_frame = 0
                last_image = None
                last_index = -1
                for _, timestamp in sorted(jobs, key=lambda item: item[1]):
                    if self._stop_requested:
                        return
                    target = max(0, int((timestamp - source.start_ms) * fps / 1000))
                    if count > 0 and target >= int(count):
                        raise ValueError("超出实际录像范围")
                    if last_index != target:
                        if source.location.video_path.suffix.lower() != ".m3u8" and (
                            last_index < 0 or target - next_frame > 100 or target < next_frame
                        ):
                            if capture.set(cv2.CAP_PROP_POS_FRAMES, target):
                                next_frame = target
                        while next_frame < target:
                            if self._stop_requested:
                                return
                            if not capture.grab():
                                raise ValueError("录像解码中断")
                            reported = float(capture.get(cv2.CAP_PROP_POS_FRAMES))
                            if not math.isfinite(reported) or abs(reported - round(reported)) > .01 or int(round(reported)) != next_frame + 1:
                                raise ValueError("录像帧号不连续")
                            next_frame += 1
                        ok, frame = capture.read()
                        if not ok or frame is None:
                            raise ValueError("这一帧无法解码")
                        reported = float(capture.get(cv2.CAP_PROP_POS_FRAMES))
                        if not math.isfinite(reported) or abs(reported - round(reported)) > .01 or int(round(reported)) - 1 != target:
                            raise ValueError("原始帧定位未验证")
                        next_frame = target + 1
                        height, width = frame.shape[:2]
                        ratio = min(1, 960 / width, 720 / height)
                        rgb = cv2.cvtColor(cv2.resize(frame, (max(1, round(width * ratio)), max(1, round(height * ratio))), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2RGB)
                        last_image = QImage(rgb.data, rgb.shape[1], rgb.shape[0], rgb.strides[0], QImage.Format_RGB888).copy()
                        last_index = target
                    if self._stop_requested:
                        return
                    self.frame_ready.emit(RaceFilmstripFrame(source, timestamp, int(target * 1000 / fps), target, last_image))
                    completed.add(timestamp)
            except (ValueError, OSError, cv2.error) as error:
                if not self._stop_requested:
                    for _, timestamp in jobs:
                        if timestamp not in completed:
                            self.failed.emit((source.key, timestamp), str(error))
            finally:
                capture.release()


class RaceOverview(QWidget):
    position_requested = pyqtSignal(object)

    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.setFixedHeight(36)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("点击或拖动定位整场时间；蓝灰为录像，浅灰为缺口，橙色为不可用；下方绿色为手动检查范围，上方绿点为已判记录，红线为机位 1")

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#e2e8f0"))
        index = self.owner.index
        duration = index.end_ms - index.start_ms
        if duration <= 0:
            return
        def x(timestamp):
            return (timestamp - index.start_ms) * self.width() / duration
        for span in index.spans:
            if span.source is not None:
                painter.fillRect(QRectF(x(span.start_ms), 7, max(1, x(span.end_ms) - x(span.start_ms)), 18),
                                 QColor("#94a3b8" if span.source.available else "#f59e0b"))
        for timestamp, _ in self.owner.markers:
            painter.fillRect(QRectF(x(timestamp), 1, 2, 5), QColor("#16a34a"))
        for start, end in self.owner.checked_ranges():
            painter.fillRect(QRectF(x(start), 28, max(1, x(end) - x(start)), 6), QColor("#16a34a"))
        left, right = self.owner.visible_times()
        painter.setPen(QPen(QColor("#0284c7"), 2))
        painter.drawRect(QRectF(x(left), 5, max(3, x(right) - x(left)), 22))
        current = self.owner.current_time
        if current is not None and index.start_ms <= current <= index.end_ms:
            painter.setPen(QPen(QColor("#ef4444"), 2))
            painter.drawLine(QPointF(x(current), 0), QPointF(x(current), 32))

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._request_position(event.x())

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.LeftButton:
            self._request_position(event.x())

    def _request_position(self, pixel):
        index = self.owner.index
        self.position_requested.emit(index.start_ms + int(max(0, min(self.width(), pixel)) / max(1, self.width()) * (index.end_ms - index.start_ms)))


class RaceFilmstripCanvas(QAbstractScrollArea):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.viewport().setCursor(Qt.OpenHandCursor)
        self.viewport().setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self._press = None
        self._moved = False
        self.horizontalScrollBar().valueChanged.connect(owner._viewport_changed)
        self.horizontalScrollBar().sliderPressed.connect(owner._begin_browsing)
        self.horizontalScrollBar().actionTriggered.connect(owner._begin_browsing)

    def scrollContentsBy(self, dx, dy):
        self.viewport().update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.owner._resize_tiles()
        self.owner._viewport_changed()

    def paintEvent(self, event):
        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        painter.fillRect(self.viewport().rect(), QColor("#e2e8f0"))
        owner = self.owner
        if not owner.index.spans:
            painter.setPen(QColor("#475569"))
            painter.drawText(self.viewport().rect(), Qt.AlignCenter, "等待相机 1 录像时间线；无需先选择运动员")
            return
        height = self.viewport().height()
        offset = self.horizontalScrollBar().value()
        for tile in owner.visible_tiles():
            x = tile * owner.tile_pitch - offset
            timestamp = owner.time_at(tile)
            source, sample = owner.index.sample_at(timestamp, owner.interval_ms)
            rect = QRectF(x, IMAGE_TOP, owner.tile_width, owner.image_height())
            key = (source.key, sample) if source else None
            frame = owner.cache.get(key)
            painter.fillRect(rect, QColor("#0f172a" if source else "#cbd5e1"))
            painter.setPen(QColor("#334155"))
            painter.drawText(int(x + 5), 16, format_time(frame.recorder_time_ms if frame else sample))
            if frame is not None:
                size = frame.image.size().scaled(int(rect.width()), int(rect.height()), Qt.KeepAspectRatio)
                target = QRectF(rect.x() + (rect.width() - size.width()) / 2, rect.y() + (rect.height() - size.height()) / 2, size.width(), size.height())
                painter.drawImage(target, frame.image)
            else:
                text = "录像缺口" if source is None else "录像文件不可用" if not source.available else owner.errors.get(key, "加载中…")
                painter.setPen(QColor("#64748b" if source is None else "#f1f5f9"))
                painter.drawText(rect, Qt.AlignCenter | Qt.TextWordWrap, text)
            labels = [label for moment, label in owner.markers if timestamp <= moment < timestamp + owner.interval_ms]
            painter.setPen(QColor("#15803d"))
            painter.drawText(int(x + 5), height - 8, "已判 " + " / ".join(labels) if labels else "")
            for left, right in owner.checked_ranges():
                start, end = max(timestamp, left), min(timestamp + owner.interval_ms, right)
                if start < end:
                    painter.fillRect(QRectF(x + (start - timestamp) / owner.interval_ms * owner.tile_width,
                                            IMAGE_TOP - 4, (end - start) / owner.interval_ms * owner.tile_width, 3), QColor("#16a34a"))
            if owner.current_time is not None and timestamp <= owner.current_time < timestamp + owner.interval_ms:
                painter.setPen(QPen(QColor("#ef4444"), 2))
                painter.drawRect(QRectF(x + 1, 1, owner.tile_width - 2, height - 2))
            for span in owner.index.spans:
                if timestamp <= span.start_ms < timestamp + owner.interval_ms:
                    painter.setPen(QPen(QColor("#38bdf8"), 2))
                    painter.drawLine(QPointF(x, 0), QPointF(x, height))
                    break

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.setFocus(Qt.MouseFocusReason)
            self.owner._begin_browsing()
            self._press = event.pos()
            self._drag_scroll = self.horizontalScrollBar().value()
            self._moved = False

    def mouseMoveEvent(self, event):
        if self._press is not None:
            delta = event.x() - self._press.x()
            if abs(delta) > 5:
                self._moved = True
            if self._moved:
                self.horizontalScrollBar().setValue(self._drag_scroll - delta)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._press is not None:
            delta = event.x() - self._press.x()
            if abs(delta) > 5:
                self._moved = True
                self.horizontalScrollBar().setValue(self._drag_scroll - delta)
            if not self._moved:
                tile = (self.horizontalScrollBar().value() + event.x()) // self.owner.tile_pitch
                self.owner.open_tile(tile)
            self._press = None

    def wheelEvent(self, event):
        self.owner._begin_browsing()
        delta = event.angleDelta().y() or event.angleDelta().x()
        self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - int(delta / 120 * self.owner.tile_pitch))
        event.accept()


class RaceFilmstripPanel(QWidget):
    frame_requested = pyqtSignal(object)
    judgment_requested = pyqtSignal(object)
    refresh_requested = pyqtSignal()
    enlarge_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.index = RaceRecordingIndex()
        self.interval_ms = 100
        self.tile_width = 400
        self.tile_pitch = self.tile_width + TILE_GAP
        self._image_aspect = 4 / 3
        self.current_time = None
        self.markers = ()
        self.cache = OrderedDict()
        self.errors = OrderedDict()
        self._worker = None
        self._closed = False
        self._signature = None
        self._initial_positioned = False
        self._user_navigated = False
        self._pending = None
        self._check_store = None
        self._check_context = None
        self._check_error = ""
        self._seen_end_ms = 0
        self._pending_sources = 0
        self._active_frame = None
        self._pending_judgment = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        summary = QHBoxLayout()
        self.range_label = QLabel("时间胶卷 · 整场相机 1")
        summary.addWidget(self.range_label)
        summary.addStretch()
        self.new_recording_label = QLabel()
        summary.addWidget(self.new_recording_label)
        self.follow_button = QPushButton("跟随最新")
        self.follow_button.setCheckable(True)
        self.follow_button.setToolTip("跟随最新可回看的录像；拖动、翻页或点击原帧时退出跟随")
        self.follow_button.toggled.connect(self._follow_changed)
        summary.addWidget(self.follow_button)
        controls = QHBoxLayout()
        self.density_combo = QComboBox()
        for milliseconds in (50, 100, 200, 500, 1000, 2000, 5000, 10000):
            spacing = f"{milliseconds} ms" if milliseconds < 1000 else f"{milliseconds // 1000} 秒"
            self.density_combo.addItem(f"每 {spacing} 一张", milliseconds)
        self.density_combo.setCurrentIndex(1)
        self.density_combo.setToolTip("间隔大便于查找时段；缩小间隔看密集到达和遮挡。缩略图只用于浏览，判定请到机位 1 逐帧查看。")
        self.density_combo.currentIndexChanged.connect(self._density_changed)
        summary.insertWidget(1, self.density_combo)
        layout.addLayout(summary)
        # A compact navigation row leaves the available height for images.
        for label, action in (("起点", lambda: self.browse_to(self.index.start_ms)),
                              ("前一屏", lambda: self.page(-1)), ("后一屏", lambda: self.page(1)),
                              ("末尾", lambda: self.browse_to(self.index.end_ms)),
                              ("回到机位 1", self.browse_camera), ("刷新录像", self.reload)):
            button = QPushButton(label)
            button.clicked.connect(action)
            controls.addWidget(button)
        self.enlarge_button = QPushButton("放大胶卷")
        self.enlarge_button.setToolTip("增加胶卷高度；也可拖动胶卷下方分隔线调节")
        self.enlarge_button.clicked.connect(self.enlarge_requested.emit)
        controls.addWidget(self.enlarge_button)
        layout.addLayout(controls)
        self.overview = RaceOverview(self)
        self.overview.position_requested.connect(self.browse_to)
        layout.addWidget(self.overview)
        inspection = QHBoxLayout()
        self.inspection_label = QLabel()
        inspection.addWidget(self.inspection_label, 1)
        self.check_button = QPushButton("本屏已检查")
        self.check_button.clicked.connect(lambda: self.mark_visible_checked(True))
        self.check_button.setToolTip("手动确认本屏完整显示的图片所代表的时间范围已检查；空档、未加载及失败图片不计入")
        inspection.addWidget(self.check_button)
        self.uncheck_button = QPushButton("取消本屏检查")
        self.uncheck_button.clicked.connect(lambda: self.mark_visible_checked(False))
        inspection.addWidget(self.uncheck_button)
        self.next_unchecked_button = QPushButton("下一处未检查")
        self.next_unchecked_button.clicked.connect(self.browse_unchecked)
        inspection.addWidget(self.next_unchecked_button)
        layout.addLayout(inspection)
        self.canvas = RaceFilmstripCanvas(self)
        layout.addWidget(self.canvas, 1)
        self.status_label = QLabel("点击运动员所在图片，按 F 打开原帧判读；Esc 返回胶卷。")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("color: #475569; font-size: 9pt;")
        layout.addWidget(self.status_label)
        self._load_timer = QTimer(self)
        self._load_timer.setSingleShot(True)
        self._load_timer.setInterval(90)
        self._load_timer.timeout.connect(self._load_visible)
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(5000)
        self._refresh_timer.timeout.connect(self.refresh_requested.emit)
        self._maximize_shortcut = QShortcut(QKeySequence(Qt.Key_F), self)
        self._maximize_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self._maximize_shortcut.setAutoRepeat(False)
        self._maximize_shortcut.activated.connect(self._maximize_active_frame)
        self._update_inspection_controls()

    def _maximize_active_frame(self):
        if self._pending is not None:
            self._pending_judgment = self._pending
            self.status_label.setText("正在读取所选原帧，完成后打开判读窗口。")
            return
        frame = self._active_frame
        if frame is None or frame.source.key not in {source.key for source in self.index.sources}:
            self.status_label.setText("先点击一张运动员所在的胶卷图片，再按 F 放大机位 1。")
            return
        self.judgment_requested.emit(frame)

    def time_at(self, tile):
        return self.index.start_ms + tile * self.interval_ms

    def tile_count(self):
        return max(0, math.ceil((self.index.end_ms - self.index.start_ms) / self.interval_ms))

    def visible_tiles(self, margin=0):
        offset = self.canvas.horizontalScrollBar().value()
        first = max(0, offset // self.tile_pitch - margin)
        last = min(self.tile_count(), (offset + self.canvas.viewport().width()) // self.tile_pitch + 1 + margin)
        return range(first, last)

    def visible_times(self):
        tiles = self.visible_tiles()
        return self.time_at(tiles.start), min(self.index.end_ms, self.time_at(tiles.stop))

    def _set_scroll_range(self):
        if not hasattr(self, "canvas"):
            return
        bar = self.canvas.horizontalScrollBar()
        bar.setPageStep(self.canvas.viewport().width())
        bar.setSingleStep(self.tile_pitch)
        bar.setRange(0, min(2_147_483_647, max(0, self.tile_count() * self.tile_pitch - self.canvas.viewport().width())))

    def image_height(self):
        return max(1, self.canvas.viewport().height() - IMAGE_TOP - IMAGE_FOOTER)

    def _resize_tiles(self):
        if not hasattr(self, "canvas"):
            return
        bar = self.canvas.horizontalScrollBar()
        # Preserve the left-hand time, including partially visible frames.
        left = self.index.start_ms + bar.value() / self.tile_pitch * self.interval_ms
        self.tile_width = max(160, round(self.image_height() * self._image_aspect))
        self.tile_pitch = self.tile_width + TILE_GAP
        self._set_scroll_range()
        if self.follow_button.isChecked():
            self._scroll_to_latest()
        else:
            self._position_scroll(left, center=False)

    def set_sources(self, sources, pending=0):
        signature = (tuple((source.key, source.available, source.priority) for source in sources), pending)
        if signature == self._signature:
            return
        self._signature = signature
        scroll = self.canvas.horizontalScrollBar().value()
        left = self.index.start_ms + scroll / self.tile_pitch * self.interval_ms
        had_spans = bool(self.index.spans)
        self.index = RaceRecordingIndex(sources)
        keys = {source.key for source in sources}
        self.cache = OrderedDict((key, value) for key, value in self.cache.items() if key[0] in keys)
        self.errors = OrderedDict((key, value) for key, value in self.errors.items() if key[0] in keys)
        self._pending = None
        self._cancel_decode()
        self._set_scroll_range()
        self._pending_sources = pending
        if not had_spans:
            self._seen_end_ms = self.index.end_ms
        if self.follow_button.isChecked():
            self._scroll_to_latest()
        elif had_spans:
            self._position_scroll(left, center=False)
        elif self.current_time is not None and not self._user_navigated:
            self._position_scroll(self.current_time)
            self._initial_positioned = True
        if self.index.spans:
            self.range_label.setText(f"时间胶卷 · {format_time(self.index.start_ms)} — {format_time(self.index.end_ms)}")
            self.range_label.setToolTip(f"录像时钟：{format_time(self.index.start_ms, date=True)} — {format_time(self.index.end_ms, date=True)}")
            gaps = sum(span.source is None or not span.source.available for span in self.index.spans)
            self.status_label.setText(f"{len(sources)} 段录像 · {gaps} 处缺口/不可用 · 点击图片，按 F 判读；Esc 返回。" + (f"另有 {pending} 段等待归档。" if pending else ""))
        else:
            self.range_label.setText("时间胶卷 · 相机 1 · 等待录像")
            self.status_label.setText("尚无可验证的录像时间范围，请等待录像归档或刷新。")
        self._viewport_changed()

    def set_check_context(self, path, race_id, camera_index=1):
        context = (str(Path(path).absolute()), race_id, camera_index)
        if context == self._check_context:
            return
        self._check_context = context
        self._check_store = None
        self._check_error = ""
        try:
            self._check_store = FilmstripCheckStore(Path(path), race_id, camera_index)
        except (OSError, ValueError, RuntimeError) as error:
            self._check_error = f"检查进度读取失败：{error}"
        self._update_inspection_controls()
        self.overview.update()

    def available_ranges(self):
        return merge_ranges((span.start_ms, span.end_ms) for span in self.index.spans
                            if span.source is not None and span.source.available)

    def checked_ranges(self):
        if self._check_store is None:
            return ()
        return merge_ranges((max(start, left), min(end, right))
                            for start, end in self._check_store.ranges
                            for left, right in self.available_ranges())

    def unchecked_ranges(self):
        return subtract_ranges(self.available_ranges(), self.checked_ranges())

    def _screen_ranges(self, *, loaded_only):
        # Cropped edge thumbnails do not count as inspected.
        offset = self.canvas.horizontalScrollBar().value()
        first = max(0, math.ceil(offset / self.tile_pitch))
        stop = min(self.tile_count(), (offset + self.canvas.viewport().width() - self.tile_width) // self.tile_pitch + 1)
        ranges = []
        for tile in range(first, stop):
            start = self.time_at(tile)
            source, sample = self.index.sample_at(start, self.interval_ms)
            if source is None or not source.available:
                continue
            if loaded_only and (source.key, sample) not in self.cache:
                continue
            # Inspection follows time, including automatic file boundaries;
            # missing/unavailable portions remain excluded.
            for left, right in self.available_ranges():
                ranges.append((max(start, left), min(start + self.interval_ms, right)))
        return merge_ranges(ranges)

    def mark_visible_checked(self, checked=True):
        self._begin_browsing()
        if self._check_store is None:
            return
        ranges = self._screen_ranges(loaded_only=checked)
        if not ranges:
            return
        try:
            self._check_store.set_checked(ranges, checked)
        except (OSError, ValueError, RuntimeError) as error:
            self.status_label.setText(f"检查进度保存失败，未更改标记：{error}")
            return
        self._update_inspection_controls()
        self.overview.update()
        self.canvas.viewport().update()
        self.status_label.setText("已保存本屏检查范围；判定仍在机位 1 确认。" if checked else "已取消本屏检查标记。")

    def browse_unchecked(self):
        remaining = self.unchecked_ranges()
        if not remaining:
            return
        left, _ = self.visible_times()
        # Keep an unchecked interval's beginning on screen, including any
        # uninspected tail before the next fully visible thumbnail.
        target = next((max(left, start) for start, end in remaining if end > left), remaining[0][0])
        self._begin_browsing()
        tile = max(0, (target - self.index.start_ms) // self.interval_ms)
        self._position_scroll(self.time_at(tile), center=False)

    def _update_inspection_controls(self):
        if not hasattr(self, "canvas"):
            return
        left, right = self.visible_times()
        inspected = sum(end - start for start, end in self.checked_ranges())
        total = sum(end - start for start, end in self.available_ranges())
        self.inspection_label.setText(f"本屏 {format_duration(right - left)} · 已检查 {format_duration(inspected)} / {format_duration(total)}" if total else "等待录像")
        self.inspection_label.setToolTip(self._check_error or f"本屏：{format_time(left)} — {format_time(right)}\n手动已检查 {format_duration(inspected)} / 可用录像 {format_duration(total)}；下方绿条为检查进度，上方绿点为判读记录。")
        self.check_button.setEnabled(self._check_store is not None and bool(self._screen_ranges(loaded_only=True)))
        self.uncheck_button.setEnabled(self._check_store is not None and bool(self.checked_ranges()))
        self.next_unchecked_button.setEnabled(self._check_store is not None and bool(self.unchecked_ranges()))
        if self._check_error:
            self.inspection_label.setText("检查进度读取失败")

    def _follow_changed(self, enabled):
        self.follow_button.setText("正在跟随" if enabled else "跟随最新")
        if enabled:
            self._user_navigated = True
            self._pending = None
            self._scroll_to_latest()
        self._update_new_recording_label()

    def _scroll_to_latest(self):
        self._seen_end_ms = self.index.end_ms
        self.canvas.horizontalScrollBar().setValue(self.canvas.horizontalScrollBar().maximum())

    def _update_new_recording_label(self):
        new_duration = sum(max(0, end - max(start, self._seen_end_ms))
                           for start, end in self.available_ranges())
        if new_duration > 0:
            text = f"新增可回看 {format_duration(new_duration)}"
        elif self.follow_button.isChecked():
            text = "跟随最新可回看录像"
        else:
            text = "浏览位置已保持"
        if self._pending_sources:
            text += f" · {self._pending_sources} 段等待归档"
        self.new_recording_label.setText(text)

    def set_current_frame(self, location, position_ms):
        if location is None or location.segment.media_started_at_ms is None:
            self.current_time = None
        else:
            self.current_time = int(location.segment.media_started_at_ms) + int(position_ms)
        if self.index.spans and self.current_time is not None and not self._initial_positioned and not self._user_navigated:
            self._position_scroll(self.current_time)
            self._initial_positioned = True
        self.canvas.viewport().update()
        self.overview.update()

    def set_judgments(self, records):
        self.markers = tuple(sorted((record.recorder_time_ms, record.label) for record in records if record.recorder_time_ms is not None))
        self.canvas.viewport().update()
        self.overview.update()

    def _position_scroll(self, timestamp, *, center=True):
        pixel = round((timestamp - self.index.start_ms) / self.interval_ms * self.tile_pitch)
        if center:
            pixel -= self.canvas.viewport().width() // 2
        self.canvas.horizontalScrollBar().setValue(max(0, min(self.canvas.horizontalScrollBar().maximum(), pixel)))

    def browse_to(self, timestamp):
        self._begin_browsing()
        self._position_scroll(timestamp)

    def _begin_browsing(self, *_):
        self._user_navigated = True
        self._pending = None
        self._pending_judgment = None
        self.follow_button.setChecked(False)

    def browse_camera(self):
        if self.current_time is not None:
            self.browse_to(self.current_time)

    def page(self, direction):
        self._begin_browsing()
        bar = self.canvas.horizontalScrollBar()
        bar.setValue(bar.value() + direction * max(self.tile_pitch, bar.pageStep() - self.tile_pitch))

    def _density_changed(self):
        left, right = self.visible_times()
        self.interval_ms = self.density_combo.currentData()
        self._pending = None
        self._cancel_decode()
        self._set_scroll_range()
        if self.follow_button.isChecked():
            self._scroll_to_latest()
        else:
            self._position_scroll((left + right) // 2)
        self._viewport_changed()

    def _viewport_changed(self, *_):
        if not hasattr(self, "_load_timer"):
            return
        self._pending = None
        self._pending_judgment = None
        self._cancel_decode()
        self.canvas.viewport().update()
        self.overview.update()
        if self.canvas.horizontalScrollBar().value() == self.canvas.horizontalScrollBar().maximum():
            self._seen_end_ms = self.index.end_ms
        self._update_new_recording_label()
        self._update_inspection_controls()
        if not self._closed:
            self._load_timer.start()

    def open_tile(self, tile):
        self._active_frame = None
        self._begin_browsing()
        if not 0 <= tile < self.tile_count():
            return
        source, sample = self.index.sample_at(self.time_at(tile), self.interval_ms)
        if source is None or not source.available:
            self.status_label.setText("此处为录像缺口或文件不可用，无法跳到原帧。")
            return
        key = (source.key, sample)
        frame = self.cache.get(key)
        if frame is not None:
            self._active_frame = frame
            self.frame_requested.emit(frame)
        elif key in self.errors:
            self.status_label.setText(self.errors[key] + "；可点“刷新录像”重试。")
        else:
            self._pending = key
            self.status_label.setText("正在读取这张图片；读到准确原帧后跳到机位 1。")
            self._load_timer.start()

    def _load_visible(self):
        if self._closed or not self.isVisible() or self._worker is not None:
            return
        jobs = []
        for tile in self.visible_tiles(margin=2):
            source, sample = self.index.sample_at(self.time_at(tile), self.interval_ms)
            if source is None or not source.available:
                continue
            key = source.key, sample
            if key not in self.cache and key not in self.errors:
                jobs.append((source, sample))
            elif key in self.cache:
                self.cache.move_to_end(key)
        if not jobs:
            return
        worker = RaceThumbnailWorker(jobs[:MAX_BATCH], self)
        self._worker = worker
        worker.frame_ready.connect(self._frame_ready)
        worker.failed.connect(self._failed)
        worker.finished.connect(self._finished)
        track_qthread(worker)
        worker.start()

    def _frame_ready(self, frame):
        if self.sender() is not self._worker or self._closed:
            return
        if frame.source.key not in {source.key for source in self.index.sources}:
            return
        self.cache[frame.key] = frame
        self.cache.move_to_end(frame.key)
        cache_bytes = sum(item.image.sizeInBytes() for item in self.cache.values())
        while len(self.cache) > MAX_CACHE or (cache_bytes > MAX_CACHE_BYTES and len(self.cache) > 1):
            _, removed = self.cache.popitem(last=False)
            cache_bytes -= removed.image.sizeInBytes()
        if self._pending == frame.key:
            open_judgment = self._pending_judgment == frame.key
            self._pending = None
            self._pending_judgment = None
            self._active_frame = frame
            self.frame_requested.emit(frame)
            if open_judgment:
                self.judgment_requested.emit(frame)
        aspect = frame.image.width() / max(1, frame.image.height())
        if abs(aspect - self._image_aspect) > .01:
            self._image_aspect = aspect
            self._resize_tiles()
        self.canvas.viewport().update()
        self._update_inspection_controls()

    def _failed(self, key, message):
        if self.sender() is self._worker and not self._closed:
            self.errors[key] = message
            while len(self.errors) > MAX_CACHE:
                self.errors.popitem(last=False)
            if self._pending == key:
                self._pending = None
                self._pending_judgment = None
                self.status_label.setText(message + "，未移动机位 1。")
            self.canvas.viewport().update()

    def _finished(self):
        if self.sender() is self._worker:
            worker, self._worker = self._worker, None
            worker.deleteLater()
            if not self._closed:
                self._load_timer.start()

    def _cancel_decode(self):
        if self._worker is not None:
            # Do not create more decoders while a native read is unwinding.
            self._worker.request_stop()

    def reload(self):
        self.errors.clear()
        self._signature = None
        if self._check_context:
            context, self._check_context = self._check_context, None
            self.set_check_context(*context)
        self.refresh_requested.emit()
        self._viewport_changed()

    def clear(self):
        self._cancel_decode()
        self._signature = None
        self.index = RaceRecordingIndex()
        self.cache.clear()
        self.errors.clear()
        self.markers = ()
        self._active_frame = None
        self._pending_judgment = None
        self.current_time = None
        self._initial_positioned = False
        self._user_navigated = False
        self._pending = None
        self.follow_button.setChecked(False)
        self._check_store = None
        self._check_context = None
        self._check_error = ""
        self._seen_end_ms = 0
        self._pending_sources = 0
        self._set_scroll_range()
        self.canvas.viewport().update()
        self.overview.update()
        self._update_new_recording_label()
        self._update_inspection_controls()

    def showEvent(self, event):
        super().showEvent(event)
        if not self._closed:
            self._refresh_timer.start()
            self._load_timer.start()

    def stop(self):
        self._closed = True
        self._load_timer.stop()
        self._refresh_timer.stop()
        self._pending = None
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.request_stop()
            retire_qthread(worker)

    def closeEvent(self, event):
        self.stop()
        super().closeEvent(event)
