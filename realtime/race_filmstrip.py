"""Virtual, chronological full-race filmstrip; judgments stay in camera 1."""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import math
from pathlib import Path

import cv2
from PyQt5.QtCore import QEasingCurve, QPointF, QPropertyAnimation, QRectF, Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QImage, QPainter, QPen
from PyQt5.QtWidgets import (
    QAbstractScrollArea, QComboBox, QHBoxLayout, QLabel, QMenu, QPushButton,
    QShortcut, QSizePolicy, QVBoxLayout, QWidget,
)
from PyQt5.QtGui import QKeySequence

from .recording_catalog import FilmstripSource, RecordingSpan, RaceRecordingIndex, recording_sources
from .thread_lifecycle import retire_qthread, track_qthread
from .video_timeline import DEFAULT_CLOCK_SOURCE, PassageVideoLocation
from .filmstrip_checks import FilmstripCheckStore, merge_ranges, subtract_ranges
from .decode_resources import DECODE_RESOURCES, FOREGROUND, THUMBNAIL, ImageCache

TILE_GAP = 8
IMAGE_TOP = 4
IMAGE_FOOTER = 48
MAX_CACHE = 160
MAX_CACHE_BYTES = 64 * 1024 * 1024
MAX_BATCH = 16
BEIJING = timezone(timedelta(hours=8))
CHIP_COLOR = "#2563eb"
JUDGMENT_COLOR = "#1bbf83"


@dataclass(frozen=True)
class ChipTimeMarker:
    event_id: str
    label: str
    chip_time_ms: int
    recorder_time_ms: int


def chip_time_markers(events, index, default_offset_ms=0):
    """Project active chips onto each recording session's calibrated clock.

    The canonical non-overlapping spans prevent live/archive duplicates.
    Bisect the passage times rather than scanning the full roster per segment.
    Gaps retain chip references without claiming there is video evidence.
    """
    events = tuple(sorted((event for event in events if event.is_active),
                          key=lambda event: (event.timeline_timestamp_ms, event.event_id)))
    times = tuple(int(event.timeline_timestamp_ms) for event in events)
    markers = []
    for span in index.spans:
        location = span.source.location if span.source is not None else None
        offset = location.clock_offset_ms if location is not None else default_offset_ms
        race_id = location.segment.race_id if location is not None else ""
        start = bisect_left(times, span.start_ms - offset)
        stop = bisect_left(times, span.end_ms - offset)
        for event in events[start:stop]:
            if race_id and race_id != event.race_id:
                continue
            timestamp = int(event.timeline_timestamp_ms)
            markers.append(ChipTimeMarker(event.event_id, event.bib.strip() or "未知",
                                          timestamp, timestamp + offset))
    return tuple(sorted(markers, key=lambda marker: (marker.recorder_time_ms, marker.event_id)))


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
        self.decode_priority = THUMBNAIL

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
            capture = DECODE_RESOURCES.open_capture(
                source.location.video_path, cv2.VideoCapture,
                priority=self.decode_priority, cancelled=lambda: self._stop_requested,
            )
            if capture is None:
                return
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
                        if source.location.video_path.suffix.lower() not in {".m3u8", ".ts"} and (
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
        self._judgment_menu = None
        self._wheel_target = None
        self._wheel_animation = QPropertyAnimation(self.horizontalScrollBar(), b"value", self)
        self._wheel_animation.setDuration(100)
        self._wheel_animation.setEasingCurve(QEasingCurve.OutCubic)
        self._wheel_animation.finished.connect(self._wheel_finished)
        self.horizontalScrollBar().setToolTip("左右拖动浏览整场录像；鼠标在胶卷上向下滚看后面，向上滚看前面。")
        self.horizontalScrollBar().setStyleSheet(
            "QScrollBar:horizontal { height: 10px; background: #edf1f4; margin: 0; }"
            "QScrollBar::handle:horizontal { background: #aab6c3; min-width: 32px; border-radius: 3px; }"
            "QScrollBar::handle:horizontal:hover { background: #718096; }"
            "QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }"
            "QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: transparent; }")
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
        checked_ranges = owner.checked_ranges()
        for tile in owner.visible_tiles():
            x = tile * owner.tile_pitch - offset
            timestamp = owner.time_at(tile)
            source, sample = owner.index.sample_at(timestamp, owner.interval_ms)
            rect = QRectF(x, IMAGE_TOP, owner.tile_width, owner.image_height())
            key = (source.key, sample) if source else None
            frame = owner.cache.get(key)
            painter.fillRect(rect, QColor("#0f172a" if source else "#cbd5e1"))
            painter.setPen(QColor("#334155"))
            if frame is not None:
                size = frame.image.size().scaled(int(rect.width()), int(rect.height()), Qt.KeepAspectRatio)
                target = QRectF(rect.x() + (rect.width() - size.width()) / 2, rect.y() + (rect.height() - size.height()) / 2, size.width(), size.height())
                painter.drawImage(target, frame.image)
                if frame.recorder_time_ms == owner.current_time:
                    # 描边收在实际图片内，不能覆盖时间刻度和下方判读标记。
                    painter.setPen(QPen(QColor("#ef4444"), 2))
                    painter.drawRect(target.adjusted(1, 1, -1, -1))
            else:
                text = "录像缺口" if source is None else "录像文件不可用" if not source.available else owner.errors.get(key, "加载中…")
                painter.setPen(QColor("#64748b" if source is None else "#f1f5f9"))
                painter.drawText(rect, Qt.AlignCenter | Qt.TextWordWrap, text)
            for left, right in checked_ranges:
                start, end = max(timestamp, left), min(timestamp + owner.interval_ms, right)
                if start < end:
                    painter.fillRect(QRectF(x + (start - timestamp) / owner.interval_ms * owner.tile_width,
                                            IMAGE_TOP - 4, (end - start) / owner.interval_ms * owner.tile_width, 3), QColor("#16a34a"))
            boundary = bisect_left(owner.index.starts, timestamp)
            if boundary < len(owner.index.starts) and owner.index.starts[boundary] < timestamp + owner.interval_ms:
                painter.setPen(QPen(QColor("#38bdf8"), 2))
                painter.drawLine(QPointF(x, 0), QPointF(x, height))

        # 胶卷和判读标记共用一套时间坐标，拖动、缩放时保持逐像素对齐。
        ruler_y = height - IMAGE_FOOTER + 5
        painter.setPen(QPen(QColor("#94a3b8"), 1))
        painter.drawLine(0, ruler_y, self.viewport().width(), ruler_y)
        for tile in owner.visible_tiles():
            x = owner.x_for_time(owner.time_at(tile))
            painter.setPen(QColor("#64748b"))
            painter.drawLine(QPointF(x, ruler_y - 3), QPointF(x, ruler_y + 4))
            source, sample = owner.index.sample_at(owner.time_at(tile), owner.interval_ms)
            label = owner.display_time(owner.time_at(tile), source)
            painter.drawText(int(x + 4), ruler_y + 16, label if source else "录像缺口")
        # A saved judgment is a green reference at its exact recording time.
        # This is a time-axis line, not a copied spatial mark on a moving rider.
        left, right = owner.visible_times()
        for record in owner.judgments_between(left, right):
            x = owner.x_for_time(record.recorder_time_ms)
            painter.setPen(QPen(QColor("#b45309" if record.unknown else JUDGMENT_COLOR), 2))
            painter.drawLine(QPointF(x, IMAGE_TOP), QPointF(x, ruler_y - 29))
        for marker in owner.chips_between(left, right):
            x = owner.x_for_time(marker.recorder_time_ms)
            painter.setPen(QPen(QColor(CHIP_COLOR), 2, Qt.DashLine))
            painter.drawLine(QPointF(x, IMAGE_TOP), QPointF(x, ruler_y - 29))
        for rect, references in self.reference_regions():
            kinds = {reference[2] for reference in references}
            kind = next(iter(kinds)) if len(kinds) == 1 else "记录"
            color = CHIP_COLOR if kind == "芯片" else JUDGMENT_COLOR if kind == "已判" else "#b45309" if kind == "待补录" else "#475569"
            label = f"{kind} · {references[0][1]}"
            if len(references) > 1:
                label += f" +{len(references) - 1}"
            painter.fillRect(rect, QColor(color))
            painter.setPen(QColor("#07120e" if kind == "已判" else "white"))
            painter.drawText(rect.adjusted(4, 0, -4, 0), Qt.AlignCenter,
                             self.fontMetrics().elidedText(label, Qt.ElideRight, int(rect.width() - 8)))
        for rect, records in self.judgment_regions():
            selected = any(record.event_id == owner.selected_event_id and not record.unknown for record in records)
            color = QColor("#1976c9" if selected else "#b45309" if all(record.unknown for record in records) else "#15803d")
            painter.setPen(QPen(color, 2))
            for record in records:
                x = owner.x_for_time(record.recorder_time_ms)
                painter.drawLine(QPointF(x, ruler_y - 5), QPointF(x, ruler_y + 3))
            painter.fillRect(rect, color)
            painter.setPen(QColor("white"))
            label = records[0].label if len(records) == 1 else f"{records[0].label} +{len(records) - 1}"
            painter.drawText(rect.adjusted(4, 0, -4, 0), Qt.AlignCenter,
                             self.fontMetrics().elidedText(label, Qt.ElideRight, int(rect.width() - 8)))
        if owner.current_time is not None:
            x = owner.x_for_time(owner.current_time)
            painter.setPen(QPen(QColor("#ef4444"), 2))
            painter.drawLine(QPointF(x, ruler_y - 4), QPointF(x, ruler_y + 6))

    def reference_regions(self):
        """Group nearby time labels without dropping simultaneous finishers."""
        owner = self.owner
        left, right = owner.visible_times()
        references = [(marker.recorder_time_ms, marker.label, "芯片",
                       f"芯片时间：{format_time(marker.chip_time_ms)}")
                      for marker in owner.chips_between(left, right)]
        references.extend((record.recorder_time_ms, record.label,
                           "待补录" if record.unknown else "已判",
                           f"判读时间：{record.time_label}")
                          for record in owner.judgments_between(left, right))
        width = self.viewport().width()
        badge_width = min(width, max(110, self.fontMetrics().horizontalAdvance("芯片 · 0000 +99") + 12))
        clusters = []
        for reference in sorted(references, key=lambda item: item[0]):
            x = owner.x_for_time(reference[0])
            if not 0 <= x < width:
                continue
            x = max(0, min(x, width - badge_width))
            if clusters and x < clusters[-1][0] + badge_width + 4:
                clusters[-1][1].append(reference)
            else:
                clusters.append((x, [reference]))
        ruler_y = self.viewport().height() - IMAGE_FOOTER + 5
        return tuple((QRectF(x, ruler_y - 28, badge_width, 24), tuple(items))
                     for x, items in clusters)

    def judgment_regions(self):
        owner = self.owner
        width = self.viewport().width()
        left, right = owner.visible_times()
        clusters = []
        for record in owner.judgments_between(left, right):
            x = owner.x_for_time(record.recorder_time_ms)
            if not 0 <= x < width:
                continue
            label_x = max(0, min(x, width - 72))
            if clusters and label_x < clusters[-1][0] + 76:
                clusters[-1][1].append(record)
            else:
                clusters.append((label_x, [record]))
        y = self.viewport().height() - IMAGE_FOOTER + 24
        return tuple((QRectF(x, y, 72, 22), tuple(records))
                     for x, records in clusters)

    def _open_judgments(self, records, position):
        if len(records) == 1:
            self.owner.saved_judgment_requested.emit(records[0].key)
            return
        menu = QMenu(self)
        for record in records:
            status = "待补录" if record.unknown else "已确认"
            action = menu.addAction(f"{record.label} · {record.time_label} · {status}")
            action.triggered.connect(lambda checked=False, key=record.key: self.owner.saved_judgment_requested.emit(key))
        if self._judgment_menu is not None:
            self._judgment_menu.close()
            self._judgment_menu.deleteLater()
        self._judgment_menu = menu
        menu.popup(self.viewport().mapToGlobal(position))

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.setFocus(Qt.MouseFocusReason)
            self.owner._begin_browsing()
            self._press = event.pos()
            self._drag_scroll = self.horizontalScrollBar().value()
            self._moved = False

    def mouseMoveEvent(self, event):
        if self._press is None:
            records = next((records for rect, records in self.judgment_regions() if rect.contains(QPointF(event.pos()))), ())
            tooltip = "\n".join(f"{record.label} · {record.time_label}" for record in records)
            references = next((items for rect, items in self.reference_regions() if rect.contains(QPointF(event.pos()))), ())
            if references:
                tooltip = "\n".join(f"{kind} · {label} · {detail} · 录像时间：{format_time(timestamp)}"
                                    for timestamp, label, kind, detail in references)
                tooltip += "\n蓝色虚线为芯片预计过线时间，绿色实线为人工已判位置。"
            if not records and not references and event.y() < self.viewport().height() - IMAGE_FOOTER:
                tile = (self.horizontalScrollBar().value() + event.x()) // self.owner.tile_pitch
                source, sample = self.owner.index.sample_at(self.owner.time_at(tile), self.owner.interval_ms)
                frame = self.owner.cache.get((source.key, sample)) if source else None
                if frame is not None:
                    tooltip = (f"录像时间（北京时间）：{self.owner.display_time(frame.recorder_time_ms, source)} · 帧 {frame.frame_index + 1}"
                               f"\n校时后判读时间：{self.owner.judgment_time(frame.recorder_time_ms, source)}"
                               "\n滚轮向下看后面，向上看前面；点击图片后按 F 判读。")
            self.viewport().setToolTip(tooltip)
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
                records = next((records for rect, records in self.judgment_regions() if rect.contains(QPointF(event.pos()))), ())
                if records:
                    self._open_judgments(records, event.pos())
                elif event.y() >= self.viewport().height() - IMAGE_FOOTER:
                    self.owner.open_time(self.owner.time_for_x(event.x()))
                else:
                    tile = (self.horizontalScrollBar().value() + event.x()) // self.owner.tile_pitch
                    self.owner.open_tile(tile)
            self._press = None

    def wheelEvent(self, event):
        self.owner._begin_browsing(keep_wheel=True)
        bar = self.horizontalScrollBar()
        pixels = event.pixelDelta().y() or event.pixelDelta().x()
        if pixels:
            # Touchpads already provide a smooth pixel stream; do not add lag.
            self.stop_scroll()
            bar.setValue(bar.value() - pixels)
        else:
            delta = event.angleDelta().y() or event.angleDelta().x()
            if delta:
                distance = -delta / 120 * self.owner.tile_pitch
                target = self._wheel_target
                if target is None or (target - bar.value()) * distance < 0:
                    target = bar.value()
                self._wheel_animation.stop()
                self._wheel_target = max(bar.minimum(), min(bar.maximum(), target + distance))
                self._wheel_animation.setStartValue(bar.value())
                self._wheel_animation.setEndValue(round(self._wheel_target))
                self._wheel_animation.start()
        event.accept()

    def stop_scroll(self):
        self._wheel_animation.stop()
        self._wheel_target = None

    def _wheel_finished(self):
        self._wheel_target = None
        if not self.owner._closed:
            QTimer.singleShot(0, self.owner._load_visible)


class _FilmstripNotice(QLabel):
    """单行反馈不挤压胶卷；较长的异常信息可悬停查看。"""

    def setText(self, text):
        super().setText(text)
        self.setToolTip(text)
        self.setVisible(bool(text))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setPen(self.palette().windowText().color())
        painter.drawText(self.contentsRect(), Qt.AlignLeft | Qt.AlignVCenter,
                         self.fontMetrics().elidedText(self.text(), Qt.ElideRight, self.contentsRect().width()))


class RaceFilmstripPanel(QWidget):
    frame_requested = pyqtSignal(object)
    judgment_requested = pyqtSignal(object)
    saved_judgment_requested = pyqtSignal(str)
    refresh_requested = pyqtSignal()
    enlarge_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.index = RaceRecordingIndex()
        self.interval_ms = 500
        self.tile_width = 400
        self.tile_pitch = self.tile_width + TILE_GAP
        self._image_aspect = 4 / 3
        self.current_time = None
        self.markers = ()
        self._judgments = ()
        self._judgment_times = ()
        self.selected_event_id = ""
        self._passages = ()
        self._chip_markers = ()
        self._chip_times = ()
        self._chip_default_offset_ms = 0
        self._judged_event_ids = set()
        self.cache = ImageCache(priority=THUMBNAIL, image_of=lambda frame: frame.image,
                                max_items=MAX_CACHE, max_bytes=MAX_CACHE_BYTES)
        self.errors = OrderedDict()
        self._worker = None
        self._closed = False
        self._operator_busy = False
        self._signature = None
        self._sources_by_key = {}
        self._initial_positioned = False
        self._user_navigated = False
        self._pending = None
        self._check_store = None
        self._check_context = None
        self._check_error = ""
        self._seen_end_ms = 0
        self._pending_sources = 0
        self._recording_start_ms = None
        self._all_sources = ()
        self._all_pending_sources = 0
        self._active_frame = None
        self._pending_judgment = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        summary = QHBoxLayout()
        self.toolbar = summary
        self.range_label = QLabel("时间胶卷 · 机位 1 · 录像时间")
        summary.addWidget(self.range_label)
        self.scope_combo = QComboBox(self)
        self.scope_combo.addItem("本次录像", "current")
        self.scope_combo.addItem("全部录像（含历史）", "all")
        self.scope_combo.setToolTip("本次录像从点击开始录像时起；自动重连仍属于本次，历史录像保留在全部录像中")
        self.scope_combo.hide()
        self.scope_combo.currentIndexChanged.connect(self._scope_changed)
        summary.addWidget(self.scope_combo)
        self.new_recording_label = QLabel(self)
        self.new_recording_label.setStyleSheet("color: #64748b; font-size: 9pt;")
        self.follow_button = QPushButton("跟随最新", self)
        self.follow_button.setCheckable(True)
        self.follow_button.setToolTip("跟随最新可回看的录像；拖动、翻页或点击原帧时退出跟随")
        self.follow_button.toggled.connect(self._follow_changed)
        self.follow_button.hide()
        self.density_combo = QComboBox()
        for milliseconds in (50, 100, 200, 500, 1000, 2000, 5000, 10000):
            spacing = f"{milliseconds} ms" if milliseconds < 1000 else f"{milliseconds // 1000} 秒"
            self.density_combo.addItem(f"每 {spacing} 一张", milliseconds)
        self.density_combo.setCurrentIndex(self.density_combo.findData(self.interval_ms))
        self.density_combo.setToolTip("间隔大便于查找时段；缩小间隔看密集到达和遮挡。缩略图只用于浏览，判定请到机位 1 逐帧查看。")
        self.density_combo.currentIndexChanged.connect(self._density_changed)
        summary.insertWidget(1, self.density_combo)
        current_button = QPushButton("当前帧", self)
        current_button.setToolTip("回到当前判读原帧所在的时间")
        current_button.clicked.connect(self.browse_camera)
        summary.addWidget(current_button)
        self.inspection_label = QLabel(self)
        self.inspection_label.setStyleSheet("color: #b45309; font-size: 9pt;")
        self.inspection_label.hide()
        summary.addWidget(self.inspection_label)
        self.status_label = _FilmstripNotice(self)
        self.status_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.status_label.setMaximumWidth(360)
        self.status_label.setStyleSheet("color: #475569; font-size: 9pt;")
        self.status_label.hide()
        summary.addWidget(self.status_label, 1)
        summary.addStretch(1)
        summary.addWidget(self.new_recording_label)
        summary.addWidget(self.follow_button)
        self.enlarge_button = QPushButton("放大胶卷", self)
        self.enlarge_button.setToolTip("增加胶卷高度；也可拖动胶卷下方分隔线调节")
        self.enlarge_button.clicked.connect(self.enlarge_requested.emit)
        self.enlarge_button.hide()
        self.more_button = QPushButton("更多", self)
        menu = QMenu(self.more_button)
        menu.addAction("前一屏", lambda: self.page(-1))
        menu.addAction("后一屏", lambda: self.page(1))
        menu.addAction("回到起点", lambda: self.browse_to(self.index.start_ms))
        menu.addAction("回到末尾", lambda: self.browse_to(self.index.end_ms))
        follow_action = menu.addAction("跟随最新")
        follow_action.setCheckable(True)
        follow_action.toggled.connect(self.follow_button.setChecked)
        self.follow_button.toggled.connect(follow_action.setChecked)
        menu.addSeparator()
        menu.addAction("刷新录像", self.reload)
        enlarge_action = menu.addAction("放大胶卷", self.enlarge_button.click)
        menu.aboutToShow.connect(lambda: enlarge_action.setText(self.enlarge_button.text()))
        self.more_button.setMenu(menu)
        summary.addWidget(self.more_button)
        layout.addLayout(summary)
        self.check_button = QPushButton("本屏已检查", self)
        self.check_button.clicked.connect(lambda: self.mark_visible_checked(True))
        self.check_button.setToolTip("手动确认本屏完整显示的图片所代表的时间范围已检查；空档、未加载及失败图片不计入")
        self.check_button.hide()
        self.uncheck_button = QPushButton("取消本屏检查", self)
        self.uncheck_button.clicked.connect(lambda: self.mark_visible_checked(False))
        self.uncheck_button.hide()
        menu.addSeparator()
        self.inspection_action = menu.addAction("等待录像")
        self.inspection_action.setEnabled(False)
        check_action = menu.addAction("本屏已检查", self.check_button.click)
        menu.aboutToShow.connect(lambda: check_action.setEnabled(self.check_button.isEnabled()))
        uncheck_action = menu.addAction("取消本屏检查", self.uncheck_button.click)
        menu.aboutToShow.connect(lambda: uncheck_action.setEnabled(self.uncheck_button.isEnabled()))
        self.next_unchecked_button = QPushButton("下一处未检查", self)
        self.next_unchecked_button.clicked.connect(self.browse_unchecked)
        self.next_unchecked_button.hide()
        next_unchecked_action = menu.addAction("下一处未检查", self.next_unchecked_button.click)
        menu.aboutToShow.connect(lambda: next_unchecked_action.setEnabled(self.next_unchecked_button.isEnabled()))
        self.canvas = RaceFilmstripCanvas(self)
        layout.addWidget(self.canvas, 1)
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

    def x_for_time(self, timestamp):
        return ((timestamp - self.index.start_ms) / self.interval_ms * self.tile_pitch
                - self.canvas.horizontalScrollBar().value())

    def time_for_x(self, x):
        return round(self.index.start_ms + (self.canvas.horizontalScrollBar().value() + x)
                     / self.tile_pitch * self.interval_ms)

    def display_time(self, timestamp, source=None):
        # The browsing ruler uses the recorder's Beijing time. Calibrating an
        # athlete's chip/video association must not relabel live recording time.
        return format_time(timestamp)

    def judgment_time(self, timestamp, source=None):
        if source is None:
            span = self.index.span_at(min(timestamp, self.index.end_ms - 1))
            source = span.source if span else None
        if source is None:
            return "时间未校验"
        # 图片缓存保存原始帧；校时变化只影响标签，不重新解码或移动录像位置。
        source = self._sources_by_key.get(source.key, source)
        return format_time(timestamp - source.location.clock_offset_ms)

    def judgments_between(self, left, right):
        return self._judgments[bisect_left(self._judgment_times, left):bisect_left(self._judgment_times, right)]

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
        self.canvas.stop_scroll()
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
        self._all_sources = tuple(sources)
        sources = self._all_sources
        self._all_pending_sources = int(pending)
        if self._recording_start_ms is not None and self.scope_combo.currentData() == "current":
            # Keep each file's original origin: thumbnail frame offsets depend
            # on start_ms, so a display filter must never rewrite it.
            sources = tuple(source for source in sources
                            if source.end_ms > self._recording_start_ms)
        signature = (
            tuple(
                (
                    source.key,
                    source.end_ms,
                    source.available,
                    source.priority,
                    source.location.clock_offset_ms,
                )
                for source in sources
            ),
            pending,
        )
        if signature == self._signature:
            return
        self._signature = signature
        scroll = self.canvas.horizontalScrollBar().value()
        left = self.index.start_ms + scroll / self.tile_pitch * self.interval_ms
        had_spans = bool(self.index.spans)
        self.index = RaceRecordingIndex(sources)
        self._project_chips()
        self._sources_by_key = {source.key: source for source in sources}
        def request_available(key):
            source = self._sources_by_key.get(key[0])
            return (source is not None and source.available
                    and source.start_ms <= key[1] < source.end_ms)

        for key in tuple(self.cache):
            if not request_available(key):
                self.cache.pop(key, None)
        self.errors = OrderedDict((key, value) for key, value in self.errors.items() if request_available(key))
        if self._pending is not None:
            # The same source may have lost its file or part of its coverage.
            if not request_available(self._pending):
                self._pending = None
                self._pending_judgment = None
        # Background appends do not represent operator navigation. Preserve
        # an in-flight click and useful decoding while changing scroll bounds.
        bar = self.canvas.horizontalScrollBar()
        previously_blocked = bar.blockSignals(True)
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
        bar.blockSignals(previously_blocked)
        if self.index.spans:
            self.range_label.setText("时间胶卷 · 机位 1 · 录像时间 · 蓝虚线芯片 · 绿实线已判")
            self.range_label.setToolTip(
                f"录像时间（北京时间）：{format_time(self.index.start_ms, date=True)} — {format_time(self.index.end_ms, date=True)}\n"
                f"校时后判读时间：{self.judgment_time(self.index.start_ms)} — {self.judgment_time(self.index.end_ms)}\n"
                "胶卷刻度显示录像时间，不随号码确认或校时改变。")
            gaps = sum(span.source is None or not span.source.available for span in self.index.spans)
            self.range_label.setToolTip(self.range_label.toolTip() +
                f"\n{len(sources)} 段录像 · {gaps} 处缺口/不可用" +
                (f" · {pending} 段等待归档" if pending else "") +
                "\n点击图片，按 F 打开原帧判读；Esc 返回胶卷。")
            live_sources = tuple(
                source
                for source in sources
                if source.location.segment.end_reason == "live_filmstrip_tail"
            )
            live_count = len(live_sources)
            if live_count:
                self.status_label.setText(
                    "录像处理中 · 当前尾部已可回看；归档完成后自动切换完整录像"
                    if any(source.available for source in live_sources)
                    else "录像处理中 · 实时缓存正在准备，暂不可回看"
                )
            elif pending:
                self.status_label.setText(
                    f"录像处理中 · {pending} 段等待封口；已归档部分仍可判读"
                )
            else:
                self.status_label.setText("")
        else:
            self.range_label.setText("时间胶卷 · 相机 1 · 等待录像")
            self.status_label.setText(
                "本次录像尚无可用画面，请检查录像状态；历史录像可切换至“全部录像”查看。"
                if self._recording_start_ms is not None and self.scope_combo.currentData() == "current"
                else "录像处理中 · 尚未产生可回看片段，请检查录像状态或稍后刷新。"
                if pending
                else "尚无可验证的录像时间范围，请等待录像归档或刷新。"
            )
        self._viewport_changed(preserve_requests=True)

    def set_recording_start(self, timestamp_ms):
        """Start a new browsing scope without modifying the durable journals."""
        self._recording_start_ms = None if timestamp_ms is None else int(timestamp_ms)
        self.scope_combo.blockSignals(True)
        self.scope_combo.setCurrentIndex(0)
        self.scope_combo.setVisible(timestamp_ms is not None)
        self.scope_combo.blockSignals(False)
        if timestamp_ms is not None:
            self.scope_combo.setItemText(0, f"本次录像 · {format_time(int(timestamp_ms), date=True)[:14]}")
        self._scope_changed()

    def _scope_changed(self):
        self._cancel_decode()
        self._pending = None
        self._pending_judgment = None
        self._active_frame = None
        self._initial_positioned = False
        self._user_navigated = False
        self._signature = None
        # Only the displayed range resets. Keep judgments and check journals.
        self.index = RaceRecordingIndex()
        self.set_sources(self._all_sources, self._all_pending_sources)

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
        self.canvas.viewport().update()

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
        percent = inspected * 100 / total if total else 0
        progress = "<0.1" if 0 < percent < 0.1 else f"{percent:.1f}"
        self.inspection_label.setText(f"本屏 {format_duration(right - left)} · 已检查 {progress}%" if total else "等待录像")
        self.inspection_label.setToolTip(self._check_error or f"本屏录像时间（北京时间）：{self.display_time(left)} — {self.display_time(right)}\n手动已检查 {format_duration(inspected)} / 可用录像 {format_duration(total)}；图片上沿绿条为已检查范围，下沿号码为判读记录。")
        self.check_button.setEnabled(self._check_store is not None and bool(self._screen_ranges(loaded_only=True)))
        self.uncheck_button.setEnabled(self._check_store is not None and bool(self.checked_ranges()))
        self.next_unchecked_button.setEnabled(self._check_store is not None and bool(self.unchecked_ranges()))
        if self._check_error:
            self.inspection_label.setText("检查进度读取失败")
        self.inspection_label.setVisible(bool(self._check_error))
        self.inspection_action.setText(self.inspection_label.text())
        self.more_button.setToolTip(self.inspection_label.toolTip())

    def _follow_changed(self, enabled):
        self.follow_button.setText("正在跟随" if enabled else "跟随最新")
        self.follow_button.setVisible(enabled)
        if enabled:
            self.canvas.stop_scroll()
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
        else:
            text = ""
        if self._pending_sources:
            text += (" · " if text else "") + f"{self._pending_sources} 段等待归档"
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

    def set_judgments(self, records, selected_event_id=""):
        self._judgments = tuple(sorted((record for record in records if record.recorder_time_ms is not None),
                                      key=lambda record: record.recorder_time_ms))
        self._judgment_times = tuple(record.recorder_time_ms for record in self._judgments)
        self._judged_event_ids = {record.event_id for record in self._judgments if not record.unknown}
        self.selected_event_id = selected_event_id
        self.markers = tuple(sorted((record.recorder_time_ms, record.label) for record in records if record.recorder_time_ms is not None))
        self.canvas.viewport().update()

    def set_passages(self, events, default_offset_ms=0):
        """Receipts update references only; never seek, select or confirm."""
        events = tuple(events)
        if events == self._passages and default_offset_ms == self._chip_default_offset_ms:
            return
        self._passages = events
        self._chip_default_offset_ms = int(default_offset_ms)
        self._project_chips()

    def _project_chips(self):
        self._chip_markers = chip_time_markers(self._passages, self.index, self._chip_default_offset_ms)
        self._chip_times = tuple(marker.recorder_time_ms for marker in self._chip_markers)
        self.canvas.viewport().update()

    def chips_between(self, left, right):
        return tuple(marker for marker in self._chip_markers[
            bisect_left(self._chip_times, left):bisect_left(self._chip_times, right)]
            if marker.event_id not in self._judged_event_ids)

    def _position_scroll(self, timestamp, *, center=True):
        pixel = round((timestamp - self.index.start_ms) / self.interval_ms * self.tile_pitch)
        if center:
            pixel -= self.canvas.viewport().width() // 2
        self.canvas.horizontalScrollBar().setValue(max(0, min(self.canvas.horizontalScrollBar().maximum(), pixel)))

    def browse_to(self, timestamp):
        self._begin_browsing()
        self._position_scroll(timestamp)

    def _begin_browsing(self, *_, keep_wheel=False):
        if not keep_wheel:
            self.canvas.stop_scroll()
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

    def _viewport_changed(self, *_, preserve_requests=False):
        if not hasattr(self, "_load_timer"):
            return
        if not preserve_requests:
            self._pending = None
            self._pending_judgment = None
        wheeling = self.canvas._wheel_target is not None
        left, right = self.visible_times()
        # Keep useful decoding alive through animation ticks. Once its whole
        # batch leaves the screen, retire it before starting the next batch.
        worker_valid = self._worker is not None and all(
            source.key in self._sources_by_key
            and self._sources_by_key[source.key].available
            and self._sources_by_key[source.key].end_ms == source.end_ms
            for source, _ in self._worker.jobs
        )
        if preserve_requests:
            if not worker_valid:
                self._cancel_decode()
        elif not wheeling or (self._worker is not None and
                            not any(left <= timestamp < right for _, timestamp in self._worker.jobs)):
            self._cancel_decode()
        self.canvas.viewport().update()
        if self.canvas.horizontalScrollBar().value() == self.canvas.horizontalScrollBar().maximum():
            self._seen_end_ms = self.index.end_ms
        self._update_new_recording_label()
        self._update_inspection_controls()
        if not self._closed and (not wheeling or not self._load_timer.isActive()):
            self._load_timer.start()

    def open_tile(self, tile):
        self._active_frame = None
        self._begin_browsing()
        if not 0 <= tile < self.tile_count():
            return
        source, sample = self.index.sample_at(self.time_at(tile), self.interval_ms)
        self._open_sample(source, sample)

    def open_time(self, timestamp):
        self._active_frame = None
        self._begin_browsing()
        span = self.index.span_at(timestamp)
        self._open_sample(span.source if span else None, timestamp)

    def _open_sample(self, source, sample):
        if source is None or not source.available:
            self.status_label.setText("此处为录像缺口或文件不可用，无法跳到原帧。")
            return
        key = (source.key, sample)
        frame = self.cache.get(key)
        if frame is not None:
            self._active_frame = frame
            self.status_label.setText("")
            self.frame_requested.emit(frame)
        elif key in self.errors:
            self.status_label.setText(self.errors[key] + "；可点“刷新录像”重试。")
        else:
            self.status_label.setText("正在读取这张图片；读到准确原帧后跳到机位 1。")
            # Showing the notice can synchronously resize the viewport.
            # Keep the explicit request after that layout pass.
            self._pending = key
            self._cancel_decode()
            QTimer.singleShot(0, self._load_visible)

    def set_operator_busy(self, busy):
        self._operator_busy = bool(busy)
        if busy and self._pending is None:
            self._cancel_decode()
        elif not busy and not self._closed:
            self._load_timer.start()

    def _load_visible(self):
        if self._closed or not self.isVisible() or self._worker is not None:
            return
        if self._operator_busy and self._pending is None:
            return
        jobs = []
        if self._pending is not None:
            source = self._sources_by_key.get(self._pending[0])
            if source is not None and source.available:
                jobs.append((source, self._pending[1]))
        # 点击的原帧优先独立解码，避免排在整批缩略图后面。
        for tile in (() if jobs else self.visible_tiles(margin=2)):
            source, sample = self.index.sample_at(self.time_at(tile), self.interval_ms)
            if source is None or not source.available:
                continue
            key = source.key, sample
            if key not in self.cache and key not in self.errors and key != self._pending:
                jobs.append((source, sample))
            elif key in self.cache:
                self.cache.move_to_end(key)
        if not jobs:
            return
        worker = RaceThumbnailWorker(jobs[:MAX_BATCH], self)
        worker.decode_priority = FOREGROUND if self._pending is not None else THUMBNAIL
        self._worker = worker
        worker.frame_ready.connect(self._frame_ready)
        worker.failed.connect(self._failed)
        worker.finished.connect(self._finished)
        track_qthread(worker)
        worker.start(QThread.NormalPriority if self._pending is not None else QThread.LowPriority)

    def _frame_ready(self, frame):
        if self.sender() is not self._worker or self._closed:
            return
        source = self._sources_by_key.get(frame.source.key)
        if (source is None or not source.available
                or not source.start_ms <= frame.key[1] < source.end_ms):
            return
        self.cache[frame.key] = frame
        self.cache.move_to_end(frame.key)
        if self._pending == frame.key:
            open_judgment = self._pending_judgment == frame.key
            self._pending = None
            self._pending_judgment = None
            self._active_frame = frame
            self.status_label.setText("")
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
        self._all_sources = ()
        self._all_pending_sources = 0
        self.index = RaceRecordingIndex()
        self._sources_by_key.clear()
        self.cache.clear()
        self.errors.clear()
        self.markers = ()
        self._judgments = ()
        self._judgment_times = ()
        self.selected_event_id = ""
        self._active_frame = None
        self._passages = ()
        self._chip_markers = ()
        self._chip_times = ()
        self._judged_event_ids.clear()
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
        self._update_new_recording_label()
        self._update_inspection_controls()

    def showEvent(self, event):
        super().showEvent(event)
        if not self._closed:
            self._refresh_timer.start()
            self._load_timer.start()

    def stop(self):
        self._closed = True
        self.canvas.stop_scroll()
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
