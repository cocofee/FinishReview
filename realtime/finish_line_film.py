"""Frame-by-frame finish-line sampling; no athlete recognition or timing edits."""

from __future__ import annotations

from dataclasses import dataclass
from bisect import bisect_right
from datetime import datetime, timezone, timedelta
import math
from pathlib import Path

import cv2
import numpy as np
from PyQt5.QtCore import QThread, pyqtSignal, QRectF, QPointF, Qt
from PyQt5.QtGui import QImage, QPainter, QPen, QColor
from PyQt5.QtWidgets import (QWidget, QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                             QPushButton, QDialogButtonBox)

from .decode_resources import DECODE_RESOURCES, THUMBNAIL
from .finish_line import FinishLine, FinishLineStore
from .thread_lifecycle import track_qthread, retire_qthread


STRIP_HEIGHT = 480
PAGE_MS = 8000
MAX_FRAMES = 2400


def sample_finish_line(frame, line: FinishLine, height=STRIP_HEIGHT):
    """Sample only the chosen spatial line, preserving endpoint order."""
    rows, columns = frame.shape[:2]
    xs = np.linspace(line.x1 * (columns - 1), line.x2 * (columns - 1), height,
                     dtype=np.float32).reshape(-1, 1)
    ys = np.linspace(line.y1 * (rows - 1), line.y2 * (rows - 1), height,
                     dtype=np.float32).reshape(-1, 1)
    return cv2.remap(frame, xs, ys, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)


@dataclass(frozen=True)
class FinishStrip:
    source: object
    times: tuple[float, ...]
    frame_indices: tuple[int, ...]
    end_ms: float
    image: QImage


def decode_finish_strip(source, start_ms, end_ms, line, *, cancelled=lambda: False,
                        capture_factory=cv2.VideoCapture):
    """Decode every source frame in a bounded interval using a shared lease.

    Each image column retains its original frame number and recorder time.
    Never interpolate video across missing files or fabricate samples on error.
    """
    if end_ms <= start_ms or end_ms - start_ms > PAGE_MS:
        raise ValueError("终点线胶卷每次最多读取 8 秒")
    capture = DECODE_RESOURCES.open_capture(source.location.video_path, capture_factory,
                                          priority=THUMBNAIL, cancelled=cancelled)
    if capture is None:
        return None
    try:
        if not capture.isOpened():
            raise ValueError("录像无法打开")
        fps = float(capture.get(cv2.CAP_PROP_FPS))
        if not math.isfinite(fps) or fps <= 0:
            raise ValueError("录像帧率无法验证")
        first = max(0, math.ceil((start_ms - source.start_ms) * fps / 1000 - 1e-7))
        stop = math.ceil((min(end_ms, source.end_ms) - source.start_ms) * fps / 1000 - 1e-7)
        if stop - first > MAX_FRAMES:
            raise ValueError("帧数超过单屏限制，请使用普通帧率录像")
        if first >= stop:
            return None
        next_frame = 0
        if source.location.video_path.suffix.lower() not in {".ts", ".m3u8"}:
            if capture.set(cv2.CAP_PROP_POS_FRAMES, first):
                reported = float(capture.get(cv2.CAP_PROP_POS_FRAMES))
                if not math.isfinite(reported) or abs(reported - first) > .01:
                    raise ValueError("原帧定位未验证")
                next_frame = first
        columns = []
        indices = []
        while next_frame < stop:
            if cancelled():
                return None
            if next_frame < first:
                if not capture.grab():
                    raise ValueError("录像解码中断")
            else:
                ok, frame = capture.read()
                if not ok or frame is None:
                    raise ValueError("录像解码中断，此段未完成")
                columns.append(sample_finish_line(frame, line))
                indices.append(next_frame)
            reported = float(capture.get(cv2.CAP_PROP_POS_FRAMES))
            if not math.isfinite(reported) or abs(reported - (next_frame + 1)) > .01:
                raise ValueError("原帧序号不连续")
            next_frame += 1
        if cancelled():
            return None
        rgb = cv2.cvtColor(np.concatenate(columns, axis=1), cv2.COLOR_BGR2RGB)
        image = QImage(rgb.data, rgb.shape[1], rgb.shape[0], rgb.strides[0], QImage.Format_RGB888).copy()
        return FinishStrip(source, tuple(source.start_ms + i * 1000 / fps for i in indices),
                           tuple(indices), min(end_ms, source.start_ms + stop * 1000 / fps), image)
    finally:
        capture.release()


class FinishStripWorker(QThread):
    ready = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, spans, start_ms, end_ms, line, parent=None):
        super().__init__(parent)
        self.spans, self.start_ms, self.end_ms, self.line = spans, start_ms, end_ms, line
        self.cancelled = False

    def request_stop(self):
        self.cancelled = True

    def run(self):
        strips = []
        errors = []
        for span in self.spans:
            left, right = max(span.start_ms, self.start_ms), min(span.end_ms, self.end_ms)
            if self.cancelled:
                return
            if right <= left or span.source is None or not span.source.available:
                continue
            try:
                strip = decode_finish_strip(span.source, left, right, self.line,
                                            cancelled=lambda: self.cancelled)
                if strip is not None:
                    strips.append(strip)
            except (OSError, ValueError, cv2.error) as error:
                errors.append(str(error))
        if not self.cancelled:
            self.ready.emit(tuple(strips))
            if errors:
                self.failed.emit("；".join(dict.fromkeys(errors)))


class FinishLineCanvas(QWidget):
    """Paint the sampled finish line as a chronological strip."""

    def __init__(self, owner, parent=None):
        super().__init__(parent or owner)
        self.owner = owner
        self._strips = ()
        self._start_ms = self._end_ms = 0
        self._markers = ()
        self.message = "先画终点线，再查看连续过线画面"
        self.setMinimumHeight(220)
        self.setMouseTracking(True)

    def x_for_time(self, timestamp):
        return (timestamp - self._start_ms) / max(1, self._end_ms - self._start_ms) * self.width()

    def time_for_x(self, x):
        return self._start_ms + x / max(1, self.width()) * (self._end_ms - self._start_ms)

    def set_data(self, strips, start_ms, end_ms, markers=()):
        self._strips = tuple(strips)
        self._start_ms, self._end_ms = int(start_ms), int(end_ms)
        self._markers = tuple(markers)
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#111827"))
        if not self._strips:
            painter.setPen(QColor("#cbd5e1"))
            painter.drawText(self.rect(), Qt.AlignCenter, self.message)
            return
        ruler = self.height() - 80
        painter.setPen(QColor("#94a3b8"))
        painter.drawText(self.rect(), Qt.AlignCenter, "未加载 / 录像缺口")
        for strip in self._strips:
            if strip.image.isNull() or not strip.times:
                continue
            # Uniform source frame times give one column per real frame.
            x = self.x_for_time(strip.times[0])
            w = self.x_for_time(strip.end_ms) - x
            painter.drawImage(QRectF(x, 0, w, ruler), strip.image)
        painter.setPen(QPen(QColor("#94a3b8"), 1))
        painter.drawLine(0, ruler, self.width(), ruler)
        for tick in range(math.ceil(self._start_ms / 1000) * 1000, self._end_ms, 1000):
            x = self.x_for_time(tick)
            text = datetime.fromtimestamp(tick / 1000, timezone(timedelta(hours=8))).strftime("%H:%M:%S")
            painter.drawText(QRectF(x, ruler + 55, 80, 22), Qt.AlignLeft, text)
        lane_ends = [-100] * 3
        for timestamp, label, kind in sorted(self._markers):
            x = self.x_for_time(timestamp)
            if 0 <= x < self.width():
                color = QColor("#22c55e" if kind == "已判" else "#f59e0b" if kind == "待补录" else "#60a5fa")
                painter.setPen(QPen(color, 2, Qt.SolidLine if kind == "已判" else Qt.DashLine))
                painter.drawLine(QPointF(x, 0), QPointF(x, ruler))
                lane = next((i for i, end in enumerate(lane_ends) if end < x), 2)
                label_x = max(0, min(x, self.width() - 100))
                lane_ends[lane] = label_x + 100
                painter.drawText(QRectF(label_x, ruler + lane * 18, 100, 18), Qt.AlignLeft, f"{kind} {label}")

    def sample_at_x(self, x):
        timestamp = self.time_for_x(x)
        for strip in self._strips:
            if strip.times[0] <= timestamp < strip.end_ms:
                column = bisect_right(strip.times, timestamp) - 1
                return strip, column
        return None

    def mouseMoveEvent(self, event):
        timestamp = self.time_for_x(event.x())
        labels = [f"{kind} · {label}" for t, label, kind in self._markers if abs(t - timestamp) <= 200]
        self.setToolTip("\n".join(labels) or "点击打开这一列对应的原帧核对；Esc 返回胶卷")

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.LeftButton or self._end_ms <= self._start_ms:
            return
        sample = self.sample_at_x(event.x())
        if sample is not None:
            self.owner.open_sample(*sample, enlarge=True)

    def mouseDoubleClickEvent(self, event):
        # A normal release already requested the popup. Do not cancel its
        # pending decoder by issuing a duplicate request on double-click.
        event.accept()


class LineEditor(QWidget):
    """Two endpoints on a still frame, including slanted physical lines."""

    def __init__(self, image, line=None, parent=None):
        super().__init__(parent)
        self.image = image
        self.points = [] if line is None else [(line.x1, line.y1), (line.x2, line.y2)]
        self.setMinimumSize(640, 420)
        self.setCursor(Qt.CrossCursor)

    def image_rect(self):
        size = self.image.size().scaled(self.size(), Qt.KeepAspectRatio)
        return QRectF((self.width() - size.width()) / 2, (self.height() - size.height()) / 2,
                      size.width(), size.height())

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#111827"))
        rect = self.image_rect()
        painter.drawImage(rect, self.image)
        painter.setPen(QPen(QColor("#f97316"), 3))
        points = [QPointF(rect.x() + x * rect.width(), rect.y() + y * rect.height())
                  for x, y in self.points]
        for point in points:
            painter.drawEllipse(point, 5, 5)
        if len(points) == 2:
            painter.drawLine(*points)

    def mousePressEvent(self, event):
        rect = self.image_rect()
        if event.button() == Qt.LeftButton and rect.contains(QPointF(event.pos())):
            if len(self.points) == 2:
                self.points.clear()
            self.points.append(((event.x() - rect.x()) / rect.width(),
                                (event.y() - rect.y()) / rect.height()))
            self.update()


class FinishLineEditor(QDialog):
    def __init__(self, image, camera_index, line=None, parent=None):
        super().__init__(parent)
        self.camera_index = camera_index
        self.line = line
        self.setWindowTitle("画终点线 · 在白色终点线两端各点一下")
        self.resize(1000, 720)
        layout = QVBoxLayout(self)
        label = QLabel("沿实际白色终点线点两个端点，可画斜线。胶卷只取这条线上的画面；重新点击可重画。")
        label.setWordWrap(True)
        layout.addWidget(label)
        self.editor = LineEditor(image, line, self)
        layout.addWidget(self.editor, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def accept(self):
        if len(self.editor.points) != 2:
            return
        (x1, y1), (x2, y2) = self.editor.points
        if math.hypot(x2 - x1, y2 - y1) < .05:
            return
        self.line = FinishLine(self.camera_index, x1, y1, x2, y2)
        super().accept()


class FinishLinePanel(QWidget):
    anchor_requested = pyqtSignal()
    line_changed = pyqtSignal(object)

    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.context = None
        self.line = None
        self.store = None
        self.start_ms = self.end_ms = 0
        self._worker = None
        self._requested = None
        self._pending = None
        self._closed = False
        self._strips = ()
        self._generation = 0
        self._compact = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        toolbar = QHBoxLayout()
        self.draw_button = QPushButton("画终点线")
        self.draw_button.clicked.connect(self.edit_line)
        toolbar.addWidget(self.draw_button)
        self.anchor_button = QPushButton("用当前原帧对齐芯片")
        self.anchor_button.setToolTip("先选首人号码并定位到实际过线原帧，再点此处；只校准时间，不自动确认其他人")
        self.anchor_button.clicked.connect(self.anchor_requested.emit)
        toolbar.addWidget(self.anchor_button)
        self._range_buttons = []
        for label, direction in (("前 8 秒", -1), ("后 8 秒", 1)):
            button = QPushButton(label)
            button.clicked.connect(lambda checked=False, step=direction: self.show_range(self.start_ms + step * PAGE_MS))
            toolbar.addWidget(button)
            self._range_buttons.append(button)
        current = QPushButton("当前原帧")
        current.clicked.connect(lambda: self.show_range((self.owner.current_time or self.start_ms) - PAGE_MS // 2))
        toolbar.addWidget(current)
        self._current_button = current
        close_chips = QPushButton("下一处密集芯片")
        close_chips.setToolTip("跳到间隔不超过 300 ms 的芯片记录；可能是并排到达，需看画面判断")
        close_chips.clicked.connect(self.next_close_chips)
        toolbar.addWidget(close_chips)
        self._close_chips_button = close_chips
        self.checked_button = QPushButton("本屏看过，下一屏")
        self.checked_button.setToolTip("保存本屏已加载画面的检查进度，不逐人确认；缺口和未加载画面不计入")
        self.checked_button.clicked.connect(self.check_and_next)
        toolbar.addWidget(self.checked_button)
        toolbar.addStretch(1)
        layout.addLayout(toolbar)
        self.hint = QLabel("蓝线：芯片已收到；绿线：人工已判。正常过线无需逐人确认；重点看有人无线、一个人对应多条线。")
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)
        self.canvas = FinishLineCanvas(self)
        layout.addWidget(self.canvas, 1)
        self.status = QLabel("先在全景胶卷点一张清晰图片，再画终点线")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

    def set_compact(self, compact: bool) -> None:
        """Show only line setup controls above the normal full-frame filmstrip.

        The old panel rendered a spatial slit of the finish line.  That view is
        useful for experiments but is not readable from an oblique camera: it
        turns riders into horizontal bands.  In finish-focus mode the owner
        keeps rendering its ordinary 500 ms full frames, so this widget only
        provides the line and calibration actions.
        """
        self._compact = bool(compact)
        for button in (*self._range_buttons, self._current_button,
                       self._close_chips_button, self.checked_button):
            button.setVisible(not self._compact)
        self.canvas.setVisible(not self._compact)
        self.status.setVisible(not self._compact)
        self.hint.setText(
            "终点线模式：橙线是画在完整画面上的实际白色终点线；蓝虚线是芯片时间，"
            "绿实线是人工判读。正常有芯片的人不用逐个确认，只查画面有人却无线和疑似一人多芯片。"
            if self._compact else
            "蓝线：芯片已收到；绿线：人工已判。正常过线无需逐人确认；重点看有人无线、一个人对应多条线。"
        )

    def set_context(self, path, race_id, camera_index):
        context = (str(Path(path).absolute()), race_id, camera_index)
        if context == self.context:
            return
        self.clear()
        self.context = context
        # Separate reference geometry from the motion detector's ROI.
        # Import an existing line initially without changing that detector.
        self.store = FinishLineStore(Path(path).with_name("finish_film_lines.json"))
        self.line = self.store.get(camera_index)
        if self.line is None:
            self.line = FinishLineStore(Path(path).with_name("finish_lines.json")).get(camera_index)
        self.owner.set_finish_line(self.line)

    def edit_line(self):
        frame = self.owner._active_frame
        if frame is None:
            visible = self.owner.visible_times()
            frame = next((frame for frame in self.owner.cache.values()
                          if visible[0] <= frame.recorder_time_ms < visible[1]), None)
        if frame is None or self.store is None:
            self.status.setText("先在全景胶卷点击一张清晰图片，加载完成后再画终点线。")
            return
        dialog = FinishLineEditor(frame.image, self.context[2], self.line, self)
        if dialog.exec_() != QDialog.Accepted:
            return
        try:
            self.store.set(dialog.line)
        except OSError as error:
            self.status.setText(f"终点线保存失败：{error}")
            return
        self.line = dialog.line
        self.owner.set_finish_line(self.line)
        self.line_changed.emit(self.line)
        self._requested = None
        if self._compact:
            # Finish focus uses the ordinary readable full-frame filmstrip;
            # never start the legacy spatial-slit worker behind that view.
            self.status.setText("终点线已保存；完整画面上的橙色终点线已刷新。")
            return
        self.show_range(frame.recorder_time_ms - PAGE_MS // 2)

    def show_range(self, start_ms):
        index = self.owner.index
        if self.line is None:
            self.canvas.message = "先点“画终点线”，沿白色终点线画两个端点"
            self.canvas.update()
            return
        start = max(index.start_ms, min(int(start_ms), max(index.start_ms, index.end_ms - PAGE_MS)))
        end = min(index.end_ms, start + PAGE_MS)
        spans = tuple(span for span in index.spans if span.end_ms > start and span.start_ms < end)
        signature = (self._generation, self.line, start, end,
                     tuple((max(start, span.start_ms), min(end, span.end_ms), span.source.key if span.source else None,
                            span.source.available if span.source else False) for span in spans))
        if signature == self._requested:
            self.update_markers()
            return
        self.start_ms, self.end_ms = start, end
        self._requested = signature
        self._strips = ()
        self.canvas.message = "正在逐帧读取终点线画面…"
        self.canvas.set_data((), start, end)
        self.status.setText("正在读取；黑色区域为未加载或录像缺口，不代表无人通过。")
        self._pending = (signature, spans, start, end, self.line)
        if self._worker is not None:
            self._worker.request_stop()
        else:
            self._start_pending()

    def _start_pending(self):
        if self._closed or self._pending is None or not self.isVisible():
            return
        signature, spans, start, end, line = self._pending
        self._pending = None
        worker = FinishStripWorker(spans, start, end, line, self)
        worker.signature = signature
        self._worker = worker
        worker.ready.connect(self._ready)
        worker.failed.connect(self._failed)
        worker.finished.connect(self._finished)
        track_qthread(worker)
        worker.start(QThread.LowPriority)

    def _ready(self, strips):
        worker = self.sender()
        if worker is not self._worker or worker.signature != self._requested or self._closed:
            return
        self._strips = strips
        self.update_markers()
        self.status.setText("连续源帧已加载；点击打开原帧核对，Esc 返回。斜拍主要显示车轮；黑色空档为未加载或录像缺口。")
        if not strips:
            self.canvas.message = "此处无可读取的终点线画面，不能判断是否有人通过"
            self.canvas.update()

    def _failed(self, message):
        if self.sender() is self._worker and self._worker.signature == self._requested:
            self.status.setText(f"部分画面无法读取：{message}；空档不能判断是否有人通过。")

    def _finished(self):
        if self.sender() is self._worker:
            worker, self._worker = self._worker, None
            worker.deleteLater()
            self._start_pending()

    def update_markers(self):
        markers = [(m.recorder_time_ms, m.label, "芯片")
                   for m in self.owner.chips_between(self.start_ms, self.end_ms)]
        markers += [(r.recorder_time_ms, r.label, "待补录" if r.unknown else "已判")
                    for r in self.owner.judgments_between(self.start_ms, self.end_ms)]
        self.canvas.set_data(self._strips, self.start_ms, self.end_ms, markers)

    def open_sample(self, strip, column, enlarge=False):
        # Ceil the sub-millisecond timestamp to recover the same source frame
        # through the existing floor-based thumbnail decoder.
        timestamp = math.ceil(strip.times[column])
        self.owner.open_time(timestamp)
        if enlarge:
            self.owner._maximize_active_frame()

    def next_close_chips(self):
        chips = self.owner._chip_markers
        pairs = [(a, b) for a, b in zip(chips, chips[1:])
                 if a.event_id != b.event_id and a.label != b.label
                 and 0 <= b.recorder_time_ms - a.recorder_time_ms <= 300]
        if not pairs:
            self.status.setText("当前没有间隔 300 ms 内的不同号码芯片；仍需查看有人无线的过线画面。")
            return
        pair = next((pair for pair in pairs if pair[0].recorder_time_ms > self.end_ms - PAGE_MS // 2), pairs[0])
        self.show_range(pair[0].recorder_time_ms - PAGE_MS // 2)
        self.hint.setText(f"密集芯片 {pair[0].label} / {pair[1].label}：可能并排到达，也可能一人多芯片；看过线画面后判断。")

    def check_and_next(self):
        store = self.owner._check_store
        ranges = tuple((math.ceil(strip.times[0]), math.floor(strip.end_ms))
                       for strip in self._strips if strip.times and strip.end_ms > strip.times[0])
        if store is None or not ranges or self._worker is not None:
            self.status.setText("本屏尚未完成读取，不能保存检查进度。")
            return
        try:
            store.set_checked(ranges, True)
        except (OSError, RuntimeError, ValueError) as error:
            self.status.setText(f"检查进度未保存：{error}")
            return
        self.owner._update_inspection_controls()
        if self.end_ms < self.owner.index.end_ms:
            self.show_range(self.end_ms)
        else:
            self.status.setText("本屏已检查，已到录像末尾。保存的是浏览进度，没有自动确认运动员。")

    def clear(self):
        self._generation += 1
        self._pending = self._requested = None
        self._strips = ()
        self.line = self.store = self.context = None
        self.start_ms = self.end_ms = 0
        if self._worker is not None:
            self._worker.request_stop()
        self.canvas.set_data((), 0, 0)

    def stop(self):
        self._closed = True
        self._pending = None
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.request_stop()
            retire_qthread(worker)

    def hideEvent(self, event):
        self._pending = self._requested = None
        if self._worker is not None:
            self._worker.request_stop()
        super().hideEvent(event)
