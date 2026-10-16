"""Large, frame-by-frame bunch review inside the original problem list."""

from __future__ import annotations

import queue
import time
from dataclasses import dataclass

import cv2
import numpy as np
from PyQt5.QtCore import QPointF, QRectF, Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QImage, QPainter, QPainterPath, QPen, QPixmap
from PyQt5.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QGraphicsScene, QGraphicsView,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QSlider, QSpinBox,
    QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .bunch_review import (BunchContext, BunchReviewStore, align_counts, completion_issue, detected_crossings,
                           is_excluded, load_context, add_viewed, uncovered_intervals, review_candidates,
                           group_member_batch)
from .bunch_timeline import BunchTimeline, group_strip, pixmap_of
from .chip_history import format_chip_time
from .line_sampling import load_strip, line_config_digest


@dataclass
class DecodedFrame:
    jpeg: bytes
    recorder_ms: int
    ref: dict


class BunchDecoder:
    """One decoder, two decode threads, a bounded JPEG cache of real PTS frames."""

    def __init__(self):
        self.capture = None
        self.path = None
        self.cache = []

    def close(self):
        if self.capture is not None:
            self.capture.release()
        self.capture, self.path, self.cache = None, None, []

    def read(self, context: BunchContext, target: int, direction: int = 0, cancelled=lambda: False):
        def pick(frames):
            if direction > 0:
                choices = [f for f in frames if f.recorder_ms > target]
                return min(choices, key=lambda f: f.recorder_ms) if choices else None
            if direction < 0:
                choices = [f for f in frames if f.recorder_ms < target]
                return max(choices, key=lambda f: f.recorder_ms) if choices else None
            return min(frames, key=lambda f: abs(f.recorder_ms - target)) if frames else None

        cached = pick(self.cache)
        if cached is not None and abs(cached.recorder_ms - target) <= (200 if direction else 30):
            return cached
        segments = [s for s in context.segments if s.start_ms <= target <= s.end_ms]
        if direction > 0:
            segments += [s for s in context.segments if target < s.start_ms <= target + 200]
        elif direction < 0:
            segments += [s for s in reversed(context.segments) if target - 200 <= s.end_ms < target]
        for segment in segments:
            path = str(segment.path)
            if path != self.path:
                self.close()
                self.capture = cv2.VideoCapture(path, cv2.CAP_FFMPEG, [cv2.CAP_PROP_N_THREADS, 2])
                self.path = path
            cap = self.capture
            if not cap.isOpened():
                continue
            for lead in (4000, 8000):
                position = max(0, target - segment.start_ms - 250 - lead)
                cap.set(cv2.CAP_PROP_POS_MSEC, position)
                frames, byte_count, previous_pts = [], 0, -1
                while not cancelled():
                    ok, frame = cap.read()
                    if not ok:
                        break
                    pts = float(cap.get(cv2.CAP_PROP_POS_MSEC))
                    if not np.isfinite(pts) or pts <= previous_pts:
                        break
                    previous_pts = pts
                    absolute = round(segment.start_ms + pts)
                    if absolute < target - 250:
                        continue
                    if absolute > target + 600:
                        break
                    ok, jpeg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 93])
                    if not ok:
                        continue
                    ref = {"camera_index": context.camera, "segment_id": segment.segment.segment_id,
                           "video_path": path, "frame_index": max(0, round(cap.get(cv2.CAP_PROP_POS_FRAMES)) - 1),
                           "position_ms": round(pts), "session_id": segment.session_id,
                           "calibration_version": context.model.version}
                    frames.append(DecodedFrame(jpeg.tobytes(), absolute, ref))
                    byte_count += jpeg.nbytes
                    if byte_count >= 32 * 1024 * 1024:
                        break
                self.cache = frames
                candidate = pick(frames)
                if candidate is not None and abs(candidate.recorder_ms - target) <= 200:
                    return candidate
        return None


class DecodeWorker(QThread):
    frame_ready = pyqtSignal(int, object)
    strip_ready = pyqtSignal(str, object, bool)
    failed = pyqtSignal(int, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.requests = queue.Queue()

    def submit(self, number, context, target, direction=0):
        self.requests.put((number, context, target, direction))

    def run(self):
        reader = BunchDecoder()
        signature = None
        try:
            while not self.isInterruptionRequested():
                try:
                    request = self.requests.get(timeout=.1)
                    while not self.requests.empty():
                        request = self.requests.get_nowait()
                except queue.Empty:
                    continue
                number, context, target, direction = request
                try:
                    if signature != context.signature:
                        reader.close()
                        signature = context.signature
                        strip, present = group_strip(context, self.isInterruptionRequested)
                        self.strip_ready.emit(signature, strip, present)
                    frame = reader.read(context, target, direction, self.isInterruptionRequested)
                    if frame is None:
                        self.failed.emit(number, '该时刻没有可用原帧，请检查录像缺口，或选择另一个机位。')
                    else:
                        self.frame_ready.emit(number, frame)
                except (OSError, ValueError, KeyError, cv2.error) as error:
                    self.failed.emit(number, f'原录像读取失败：{error}')
        finally:
            reader.close()


class CountView(QGraphicsView):
    counted = pyqtSignal(float, float, bool, object)

    def __init__(self):
        super().__init__()
        self.setScene(QGraphicsScene(self))
        self.setMinimumSize(400, 260)
        self.setBackgroundBrush(QColor('#18222e'))
        self.setTransformationAnchor(self.AnchorUnderMouse)
        self.setDragMode(self.ScrollHandDrag)
        self.setToolTip('左键点前轮计一人；右键撤销附近计数；滚轮放大看号码，拖动画面；双击还原')
        self.image_size = None
        self.zoomed = False
        self.click_timer = QTimer(self)
        self.click_timer.setSingleShot(True)
        self.click_timer.timeout.connect(lambda: self.counted.emit(*self.pending_click))

    def show_frame(self, frame, line, marks, source_frame=None):
        self.source_frame = source_frame
        old_transform = self.transform()
        self.scene().clear()
        self.scene().addPixmap(pixmap_of(frame))
        h, w = frame.shape[:2]
        self.image_size = (w, h)
        self.scene().setSceneRect(QRectF(0, 0, w, h))
        points = line.get('points') or ([(line['x1'], line['y1']), (line['x2'], line['y2'])] if 'x1' in line else [])
        if points:
            path = QPainterPath(QPointF(points[0][0] * w, points[0][1] * h))
            for x, y in points[1:]:
                path.lineTo(x * w, y * h)
            pen = QPen(QColor('#ffe23b'), 2)
            pen.setCosmetic(True)
            self.scene().addPath(path, pen)
        for number, mark in marks:
            x, y = mark['x'] * w, mark['y'] * h
            excluded = is_excluded(mark)
            color = QColor('#aeb8c4' if excluded else '#20ff87')
            pen = QPen(color, 3)
            pen.setCosmetic(True)
            self.scene().addEllipse(x - 12, y - 12, 24, 24, pen)
            if excluded:
                self.scene().addLine(x - 12, y - 12, x + 12, y + 12, pen)
            text = f"已排除：{mark['exclusion_reason']}" if excluded else mark.get('bib', '')
            label = self.scene().addText(f"{number + 1} {text}")
            label.setDefaultTextColor(color)
            label.setScale(max(w / 1000, 1))
            label.setPos(x + 12, y - 20)
        if self.zoomed:
            self.setTransform(old_transform)
        else:
            self.fitInView(self.sceneRect(), Qt.KeepAspectRatio)

    def wheelEvent(self, event):
        if self.image_size:
            factor = 1.25 if event.angleDelta().y() > 0 else .8
            self.scale(factor, factor)
            self.zoomed = True
        event.accept()

    def mousePressEvent(self, event):
        self._press = event.pos()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if getattr(self, '_suppress_release', False):
            self._suppress_release = False
            super().mouseReleaseEvent(event)
            return
        if self.image_size and (event.pos() - getattr(self, '_press', event.pos())).manhattanLength() < 5:
            point = self.mapToScene(event.pos())
            w, h = self.image_size
            if 0 <= point.x() < w and 0 <= point.y() < h:
                if self.click_timer.isActive():
                    self.flush_click()
                self.pending_click = (point.x() / w, point.y() / h, event.button() == Qt.RightButton, self.source_frame)
                if event.button() == Qt.RightButton:
                    self.click_timer.stop()
                    self.counted.emit(*self.pending_click)
                elif event.button() == Qt.LeftButton:
                    self.click_timer.start(QApplication.doubleClickInterval())
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        self.click_timer.stop()
        self._suppress_release = True
        self.zoomed = False
        self.fitInView(self.sceneRect(), Qt.KeepAspectRatio)
        event.accept()

    def flush_click(self):
        if self.click_timer.isActive():
            self.click_timer.stop()
            self.counted.emit(*self.pending_click)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.image_size and not self.zoomed:
            self.fitInView(self.sceneRect(), Qt.KeepAspectRatio)


class BunchReviewDialog(QDialog):
    def __init__(self, workspace, item, parent=None):
        super().__init__(parent)
        self.setWindowTitle('集团逐帧核对 · 点前轮计数 / 放大认号')
        self.resize(1460, 940)
        self.context = load_context(workspace, item)
        self.store = BunchReviewStore(self.context.workspace / 'finish_problems')
        self.marks, self.frame, self.started, self.elapsed = [], None, None, 0.0
        self.request_number, self.busy, self.dirty, self.closing = 0, False, False, False
        self.saved_completed = False
        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        self.camera_combo = QComboBox()
        self.camera_combo.addItem(f'机位 {self.context.camera}', self.context.camera)
        from .analysis_workspace import AnalysisWorkspace
        for camera in AnalysisWorkspace(workspace).camera_indexes():
            if camera != self.context.camera:
                self.camera_combo.addItem(f'机位 {camera}', camera)
        header.addWidget(self.camera_combo)
        self.summary = QLabel()
        header.addWidget(self.summary, 1)
        self.clock = QLabel('本团操作用时 0 秒')
        header.addWidget(self.clock)
        layout.addLayout(header)
        self.timeline = BunchTimeline()
        layout.addWidget(QLabel("整团过线图 / 芯片刻度：蓝 画面检测到的每一个人（浅蓝 当时画面被遮挡）· 红 芯片预期时刻 · "
                                "绿 选手计数 · 灰 已排除人员（点击跳到原帧；整场浏览仍用主界面的时间胶卷）"))
        layout.addWidget(self.timeline)
        attention = QHBoxLayout()
        self.previous_candidate = QPushButton("上一候选疑点")
        self.next_candidate = QPushButton("下一候选疑点")
        self.previous_candidate.clicked.connect(lambda: self.navigate_candidate(-1))
        self.next_candidate.clicked.connect(lambda: self.navigate_candidate(1))
        attention.addWidget(self.previous_candidate)
        attention.addWidget(self.next_candidate)
        self.candidate_label = QLabel("候选定位不是已找全漏读；本团仍须完整点数。")
        self.candidate_label.setWordWrap(True)
        attention.addWidget(self.candidate_label, 1)
        layout.addLayout(attention)
        self.coverage_label = QLabel()
        self.coverage_label.setWordWrap(True)
        layout.addWidget(self.coverage_label)
        self.near = QLabel()
        self.near.setWordWrap(True)
        layout.addWidget(self.near)
        split = QSplitter(Qt.Horizontal)
        self.view = CountView()
        split.addWidget(self.view)
        sidebar = QWidget()
        side = QVBoxLayout(sidebar)
        instructions = QLabel('① 检查候选疑点，再逐帧完整点数\n② 正常计数不必填号码；只对漏读等疑点认号\n③ 局部查看或总数相等不能直接关闭整团\n滚轮放大，拖动查看；双击还原')
        instructions.setWordWrap(True)
        side.addWidget(instructions)
        self.count_table = QTableWidget(0, 4)
        self.count_table.setHorizontalHeaderLabels(['序号', '录像时间', '认出号码', '核对参考'])
        self.count_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.count_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.count_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.count_table.verticalHeader().hide()
        side.addWidget(self.count_table, 1)
        bib_row = QHBoxLayout()
        self.bib_input = QLineEdit()
        self.bib_input.setPlaceholderText('看清的号码（可留空）')
        bib_row.addWidget(self.bib_input)
        self.apply_bib = QPushButton('记号码')
        self.apply_bib.clicked.connect(self.assign_bib)
        bib_row.addWidget(self.apply_bib)
        side.addLayout(bib_row)
        exclusion_help = QLabel('双击所选记录回看原帧；确认是非选手后再排除。看不清号码的选手请保留待查。')
        exclusion_help.setWordWrap(True)
        side.addWidget(exclusion_help)
        exclusion_row = QHBoxLayout()
        self.exclusion_reason = QComboBox()
        self.exclusion_reason.addItems(['行人', '工作人员', '其他无关人员'])
        exclusion_row.addWidget(self.exclusion_reason)
        self.exclude_button = QPushButton('确认非选手并排除')
        self.exclude_button.clicked.connect(self.exclude_selected)
        exclusion_row.addWidget(self.exclude_button)
        side.addLayout(exclusion_row)
        correction_row = QHBoxLayout()
        self.restore_button = QPushButton('恢复为选手计数')
        self.restore_button.clicked.connect(self.restore_selected)
        correction_row.addWidget(self.restore_button)
        self.remove_button = QPushButton('删除误点计数')
        self.remove_button.clicked.connect(self.remove_selected)
        correction_row.addWidget(self.remove_button)
        side.addLayout(correction_row)
        side.addWidget(QLabel('本团芯片（官方时间）'))
        self.chip_table = QTableWidget(0, 3)
        self.chip_table.setHorizontalHeaderLabels(['号码', '芯片时间', '核对状态'])
        self.chip_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.chip_table.verticalHeader().hide()
        side.addWidget(self.chip_table, 1)
        self.members_button = QPushButton("按成员核对额外芯片 / 暂存")
        self.members_button.clicked.connect(self.review_members)
        side.addWidget(self.members_button)
        bias_row = QHBoxLayout()
        bias_row.addWidget(QLabel('前轮比刻度晚'))
        self.bias = QSpinBox()
        self.bias.setRange(-1000, 1000)
        self.bias.setSuffix(' ms')
        self.bias.setToolTip('优先由至少 3 位独立过线者估计；不足时用此值。后方机位原型参考值 120 ms，其他机位需核实。')
        bias_row.addWidget(self.bias)
        side.addLayout(bias_row)
        split.addWidget(sidebar)
        split.setSizes([1000, 420])
        layout.addWidget(split, 1)
        navigation = QHBoxLayout()
        self.prev_button = QPushButton('← 上一帧')
        self.next_button = QPushButton('下一帧 →')
        self.prev_button.clicked.connect(lambda: self.step(-1))
        self.next_button.clicked.connect(lambda: self.step(1))
        navigation.addWidget(self.prev_button)
        navigation.addWidget(self.next_button)
        start = QPushButton('回到团首')
        start.clicked.connect(lambda: self.seek(self.context.start_ms))
        navigation.addWidget(start)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 10000)
        self.slider.sliderReleased.connect(lambda: self.seek(round(self.context.start_ms + self.slider.value() / 10000
                                                                   * (self.context.end_ms - self.context.start_ms))))
        navigation.addWidget(self.slider, 1)
        self.position = QLabel()
        navigation.addWidget(self.position)
        layout.addLayout(navigation)
        self.notice = QLabel('左键点击压线前轮计一人；密集刻度只作顺序参考，不要求给正常成员逐一认号；遮挡无法点清则保留待核。')
        self.notice.setWordWrap(True)
        layout.addWidget(self.notice)
        bottom = QHBoxLayout()
        self.fully_viewed = QCheckBox('已看完整团，计数没有遗漏或重复')
        bottom.addWidget(self.fully_viewed, 1)
        save = QPushButton('保存进度，稍后继续')
        save.clicked.connect(lambda: self.save(False))
        bottom.addWidget(save)
        self.complete_button = QPushButton('确认本团已核对')
        self.complete_button.clicked.connect(lambda: self.save(True))
        bottom.addWidget(self.complete_button)
        close = QPushButton('返回清单')
        close.clicked.connect(self.close)
        bottom.addWidget(close)
        layout.addLayout(bottom)
        self.view.counted.connect(self.count_at)
        self.timeline.seek.connect(self.seek)
        self.count_table.currentCellChanged.connect(self.select_count)
        self.count_table.cellDoubleClicked.connect(
            lambda row, _column: self.seek(self.marks[row]['recorder_ms']) if 0 <= row < len(self.marks) else None)
        self.bib_input.returnPressed.connect(self.assign_bib)
        self.bias.valueChanged.connect(self.changed)
        self.fully_viewed.toggled.connect(self.changed)
        self.camera_combo.currentIndexChanged.connect(self.switch_camera)
        self.worker = DecodeWorker(self)
        self.worker.frame_ready.connect(self.frame_ready)
        self.worker.strip_ready.connect(self.strip_ready)
        self.worker.failed.connect(self.decode_failed)
        self.worker.finished.connect(self.worker_stopped)
        self.worker.start(QThread.LowPriority)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_clock)
        self.timer.start(500)
        self.restore()
        self.seek(self.context.start_ms, touch=False)

    def touch(self):
        if self.started is None:
            self.started = time.monotonic()

    def seconds(self):
        return self.elapsed + (time.monotonic() - self.started if self.started is not None else 0)

    def update_clock(self):
        self.clock.setText(f'本团操作用时 {self.seconds():.0f} 秒')

    def restore(self):
        record = self.store.get(self.context) or {}
        self.marks = list(record.get('marks', []))
        self.viewed_ranges = list(record.get("viewed_ranges", []))
        self.visited_candidates = set(record.get("visited_candidates", []))
        self.selected_candidate = None
        self.elapsed, self.started = float(record.get('seconds', 0)), None
        self.fully_viewed.blockSignals(True)
        self.fully_viewed.setChecked(record.get('fully_viewed', False))
        self.fully_viewed.blockSignals(False)
        self.bias.blockSignals(True)
        self.bias.setValue(record.get('fallback_bias_ms', 0))
        self.bias.blockSignals(False)
        self.timeline.context, self.timeline.image = self.context, None
        self.timeline.detected = self.context.detected
        self.dirty = False
        self.update_results()

    def switch_camera(self):
        self.view.flush_click()
        camera = self.camera_combo.currentData()
        if camera == self.context.camera:
            return
        try:
            context = load_context(self.context.workspace, self.context.item, camera)
            if self.dirty and not self.save(False):
                raise ValueError('当前机位的进度未保存，未切换。')
        except (ValueError, OSError) as error:
            self.notice.setText(str(error))
            self.camera_combo.blockSignals(True)
            self.camera_combo.setCurrentIndex(self.camera_combo.findData(self.context.camera))
            self.camera_combo.blockSignals(False)
            return
        self.context, self.frame = context, None
        self.view.scene().clear()
        self.restore()
        self.seek(self.context.start_ms, touch=False)
        self.notice.setText('已切换机位。各机位计数分别保存，不能重复计入同一团；按选定机位确认完成。')

    def seek(self, target, direction=0, *, touch=True):
        if self.closing:
            return
        if touch:
            self.touch()
            self.dirty = True
        self.request_number += 1
        self.busy = True
        self.prev_button.setEnabled(False)
        self.next_button.setEnabled(False)
        self.position.setText('正在读取原帧…')
        self.worker.submit(self.request_number, self.context,
                           max(self.context.start_ms, min(self.context.end_ms, target)), direction)

    def step(self, direction):
        if self.frame is not None and not self.busy:
            self.seek(self.frame.recorder_ms, direction)

    def strip_ready(self, signature, image, present):
        if signature != self.context.signature:
            return
        self.timeline.image = pixmap_of(image)
        self.timeline.update()
        if not present:
            self.notice.setText('本团没有已生成的过线图缓存；仍可逐帧点数。灰色区域不代表无人过线。')

    def decode_failed(self, number, message):
        if number != self.request_number:
            return
        self.busy = False
        self.frame = None
        self.view.scene().clear()
        self.view.image_size = None
        self.position.setText('没有可用原帧')
        self.notice.setText(message)
        self.prev_button.setEnabled(False)
        self.next_button.setEnabled(False)

    def frame_ready(self, number, frame):
        if number != self.request_number or self.closing:
            return
        self.frame, self.busy = frame, False
        self.viewed_ranges = add_viewed(self.viewed_ranges, frame.recorder_ms, self.context.start_ms, self.context.end_ms)
        if self.selected_candidate:
            candidate = next((c for c in self.candidates if c["id"] == self.selected_candidate), None)
            if candidate and candidate["start_ms"]-100 <= frame.recorder_ms <= candidate["end_ms"]+100:
                self.visited_candidates.add(candidate["id"])
        self.dirty = True
        self.update_attention()
        self.prev_button.setEnabled(frame.recorder_ms > self.context.start_ms + 20)
        self.next_button.setEnabled(frame.recorder_ms < self.context.end_ms - 20)
        self.position.setText(f"{format_chip_time(frame.recorder_ms)} · 第 {frame.ref['frame_index'] + 1} 帧")
        self.slider.setValue(round((frame.recorder_ms - self.context.start_ms) /
                                   (self.context.end_ms - self.context.start_ms) * 10000))
        self.draw_frame()

    def draw_frame(self):
        if self.frame is None:
            return
        frame = self.frame
        image = cv2.imdecode(np.frombuffer(frame.jpeg, np.uint8), cv2.IMREAD_COLOR)
        marks = [(i, m) for i, m in enumerate(self.marks) if m['frame']['segment_id'] == frame.ref['segment_id']
                 and m['frame']['frame_index'] == frame.ref['frame_index']]
        self.view.show_frame(image, self.context.lines.get(frame.ref['segment_id'], self.context.line), marks, frame)
        result = align_counts(self.context.chips, self.marks, self.bias.value())
        nearby = [f'{chip.bib}（{chip.recorder_ms + result["bias_ms"] - frame.recorder_ms:+d} ms）'
                  for chip in self.context.chips if abs(chip.recorder_ms + result['bias_ms'] - frame.recorder_ms) <= 200]
        self.near.setText('这一帧附近 ±200 ms 的芯片：' + ('、'.join(nearby) if nearby else '无')
                          + '；仅供对照，不按最近时间认人。')
        self.timeline.current = frame.recorder_ms
        self.timeline.update()

    def count_at(self, x, y, remove=False, source_frame=None):
        frame = source_frame or self.frame
        if (frame is None or self.closing or (self.busy and source_frame is None)
                or frame.ref['camera_index'] != self.context.camera):
            return
        self.touch()
        same = [i for i, m in enumerate(self.marks) if m['frame']['segment_id'] == frame.ref['segment_id']
                and m['frame']['frame_index'] == frame.ref['frame_index']]
        if remove:
            if not same:
                return
            index = min(same, key=lambda i: (self.marks[i]['x'] - x) ** 2 + (self.marks[i]['y'] - y) ** 2)
            if is_excluded(self.marks[index]):
                self.notice.setText('已排除人员保留原帧和原因；如需纠正，请选中后恢复为选手计数。')
                return
            self.marks.pop(index)
        else:
            if any((self.marks[i]['x'] - x) ** 2 + (self.marks[i]['y'] - y) ** 2 < .02 ** 2 for i in same):
                self.notice.setText('这个位置已计过数；并排两人请分别点各自前轮。')
                return
            self.marks.append({'recorder_ms': frame.recorder_ms, 'x': x, 'y': y,
                               'bib': '', 'frame': dict(frame.ref)})
        self.changed()
        if not remove:
            self.count_table.setCurrentCell(len(self.marks) - 1, 0)

    def changed(self, *_args):
        self.touch()
        self.dirty = True
        self.saved_completed = False
        self.update_results()
        self.draw_frame()

    def update_results(self):
        result = align_counts(self.context.chips, self.marks, self.bias.value())
        excluded = sum(is_excluded(mark) for mark in self.marks)
        # 画面检测是本团里每个人都在场的凭据：芯片少了它就没有对照。
        seen = ('画面检测 %d 处过线' % len(self.context.detected)
                if detected_crossings(self.context.workspace, self.context.camera) is not None
                else '画面检测结果缺失')
        self.summary.setText(f'芯片 {len(self.context.chips)} 个 · {seen} · 选手计数 {len(self.marks) - excluded} 人 · '
                             f'人工认号 {sum(bool(m.get("bib")) and not is_excluded(m) for m in self.marks)} 人 · '
                             f'已排除 {excluded} 人 · '
                             f'多出 {len(result["extras"])} 人 · 未对应芯片 {len(result["missing"])} 个')
        selected = self.count_table.currentRow()
        self.count_table.blockSignals(True)
        self.count_table.setRowCount(len(self.marks))
        extras = {e['mark']: e for e in result['extras']}
        for i, mark in enumerate(self.marks):
            if is_excluded(mark):
                status = '已排除：' + mark['exclusion_reason']
            elif i in result['pairs']:
                chip = self.context.chips[result['pairs'][i]]
                status = ('同号芯片 ' if mark.get('bib') else '顺序参考 ') + chip.bib
                if i in result['ambiguous']:
                    status += '；密集，仅作参考'
            else:
                extra = extras[i]
                status = ('多出；估算 ' + format_chip_time(extra['chip_time_ms']) if extra['chip_time_ms'] is not None
                          else '多出；缺少前后参考，无法估时')
            for column, value in enumerate((str(i + 1), format_chip_time(mark['recorder_ms']), mark.get('bib', ''), status)):
                cell = QTableWidgetItem(value)
                if is_excluded(mark):
                    cell.setForeground(QColor('#687586'))
                self.count_table.setItem(i, column, cell)
        self.count_table.resizeColumnsToContents()
        self.count_table.blockSignals(False)
        if 0 <= selected < len(self.marks):
            self.count_table.setCurrentCell(selected, 0)
        self.select_count(self.count_table.currentRow())
        self.chip_table.setRowCount(len(self.context.chips))
        for i, chip in enumerate(self.context.chips):
            status = '未对应，需核查' if i in result['missing'] else '已有计数'
            for column, value in enumerate((chip.bib, format_chip_time(chip.chip_ms), status)):
                self.chip_table.setItem(i, column, QTableWidgetItem(value))
        self.chip_table.resizeColumnsToContents()
        self.timeline.marks = self.marks
        self.update_attention()
        self.timeline.update()

    def update_attention(self):
        result = align_counts(self.context.chips, self.marks, self.bias.value())
        self.candidates = review_candidates(self.context, self.marks, result)
        unseen = [c for c in self.candidates if c["id"] not in self.visited_candidates]
        self.previous_candidate.setEnabled(bool(self.candidates))
        self.next_candidate.setEnabled(bool(self.candidates))
        selected = next((c for c in self.candidates if c["id"] == self.selected_candidate), None)
        self.candidate_label.setText(f"候选 {len(self.candidates)} 处 / 未查看 {len(unseen)} 处；"
                                     + (selected["reason"] if selected else "只定位已有证据，仍须完整点数"))
        missing = uncovered_intervals(self.context.start_ms, self.context.end_ms, self.viewed_ranges, tolerance=100)
        ranges = "、".join(f"{format_chip_time(a)}–{format_chip_time(b)}" for a, b in missing[:3])
        self.coverage_label.setText(f"未查看 {sum(b-a for a,b in missing)/1000:.1f} 秒"
                                   + (f"：{ranges}" if ranges else "；本团已逐帧覆盖，请确认计数完整")
                                   + (f"；原录像缺口 {len(self.context.gaps)} 处，保留待核" if self.context.gaps else ""))
        self.fully_viewed.setEnabled(not missing and not self.context.gaps)
        if missing or self.context.gaps:
            self.fully_viewed.blockSignals(True)
            self.fully_viewed.setChecked(False)
            self.fully_viewed.blockSignals(False)
        self.timeline.viewed, self.timeline.candidates = self.viewed_ranges, self.candidates
        self.timeline.update()

    def navigate_candidate(self, direction):
        if not self.candidates:
            return
        indexes = [i for i, c in enumerate(self.candidates) if c["id"] == self.selected_candidate]
        index = ((indexes[0]+direction) % len(self.candidates)) if indexes else (0 if direction > 0 else len(self.candidates)-1)
        candidate = self.candidates[index]
        self.selected_candidate = candidate["id"]
        self.seek(candidate["start_ms"])
        self.update_attention()

    def review_members(self):
        from .batch_dialog import BatchReviewDialog
        self.view.flush_click()
        if self.dirty and not self.save(False):
            return
        try:
            batch = group_member_batch(self.context)
            def current(_identity):
                return group_member_batch(load_context(self.context.workspace, self.context.item, self.context.camera))
            dialog = BatchReviewDialog(self.context.workspace / "finish_problems", batch, current, lambda: (),
                                       lambda frame: self.seek(next((s.start_ms for s in self.context.segments
                                           if s.segment.segment_id == frame.get("segment_id")), self.context.start_ms)
                                           + frame.get("position_ms", 0)), self)
            dialog.exec_()
            self.notice.setText("成员决定已保留；未对应芯片仍须核实，整团继续待核。")
        except (OSError, ValueError, RuntimeError) as error:
            self.notice.setText(f"成员核对不可用：{error}")

    def select_count(self, row, *_args):
        valid = 0 <= row < len(self.marks)
        excluded = valid and is_excluded(self.marks[row])
        self.bib_input.setEnabled(valid and not excluded)
        self.apply_bib.setEnabled(valid and not excluded)
        self.exclude_button.setEnabled(valid and not excluded)
        self.exclusion_reason.setEnabled(valid and not excluded)
        self.restore_button.setEnabled(bool(excluded))
        self.remove_button.setEnabled(valid and not excluded)
        if valid:
            self.bib_input.setText(self.marks[row].get('bib', ''))
        else:
            self.bib_input.clear()

    def assign_bib(self):
        row = self.count_table.currentRow()
        if not 0 <= row < len(self.marks) or is_excluded(self.marks[row]):
            self.notice.setText('请先点压线者计数，或选择一条计数。')
            return
        bib = self.bib_input.text().strip()
        if bib and any(i != row and not is_excluded(m) and m.get('bib') == bib for i, m in enumerate(self.marks)):
            self.notice.setText('这个号码已经填在另一位过线者上，请核对。')
            return
        self.marks[row]['bib'] = bib
        self.changed()

    def exclude_selected(self):
        row = self.count_table.currentRow()
        if 0 <= row < len(self.marks) and not is_excluded(self.marks[row]):
            self.marks[row]['exclusion_reason'] = self.exclusion_reason.currentText()
            self.changed()
            self.notice.setText('已按人工确认排除：不计入选手人数和补录建议，原帧及原因保留；可恢复。')

    def restore_selected(self):
        row = self.count_table.currentRow()
        if 0 <= row < len(self.marks) and is_excluded(self.marks[row]):
            self.marks[row].pop('exclusion_reason')
            self.changed()
            self.notice.setText('已恢复为选手计数，请重新核对号码及芯片对应关系。')

    def remove_selected(self):
        row = self.count_table.currentRow()
        if 0 <= row < len(self.marks) and not is_excluded(self.marks[row]):
            self.marks.pop(row)
            self.changed()

    def save(self, completed):
        self.view.flush_click()
        result = align_counts(self.context.chips, self.marks, self.bias.value())
        if completed:
            if self.context.gaps or uncovered_intervals(self.context.start_ms, self.context.end_ms, self.viewed_ranges, tolerance=100):
                self.notice.setText("仍有未查看范围或原录像缺口；不能只查看候选就关闭整团。")
                return False
            issue = completion_issue(self.context.chips, self.marks, result)
            if issue:
                self.notice.setText(issue)
                return False
            if result['extras'] and QMessageBox.question(
                self, '确认补录建议', '将以下人工认出的号码写入补录建议：\n'
                + '\n'.join(f"{e['bib']}  约 {format_chip_time(e['chip_time_ms'])}" for e in result['extras'])
                + '\n时间由前后过线者插值估算，请到 CycleRace 复核后录入。',
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            ) != QMessageBox.Yes:
                return False
        try:
            current = load_context(self.context.workspace, self.context.item, self.context.camera)
            if current.signature != self.context.signature:
                raise ValueError('芯片、录像索引或校时已变化，请重新打开本团核对；当前结果未覆盖。')
            self.store.save(self.context, self.marks, self.seconds(), completed=completed,
                            fully_viewed=self.fully_viewed.isChecked(), fallback_bias_ms=self.bias.value(),
                            viewed_ranges=self.viewed_ranges, visited_candidates=self.visited_candidates)
        except (ValueError, RuntimeError, OSError) as error:
            self.notice.setText(f'未保存：{error}')
            return False
        self.saved_completed = completed
        self.dirty = False
        self.notice.setText('本团核对结果已保存，补录建议随清单导出。' if completed else '进度已保存，本团继续保留待处理。')
        return True

    def keyPressEvent(self, event):
        if self.bib_input.hasFocus():
            super().keyPressEvent(event)
        elif event.key() in (Qt.Key_Left, Qt.Key_Right):
            self.step(-1 if event.key() == Qt.Key_Left else 1)
            event.accept()
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event):
        self.view.flush_click()
        if self.worker.isRunning():
            if self.dirty and not self.save(False):
                event.ignore()
                return
            self.closing = True
            self.worker.requestInterruption()
            self.setEnabled(False)
            self.notice.setText('正在释放录像，已保存核对进度…')
            event.ignore()
        else:
            event.accept()

    def reject(self):
        self.close()

    def worker_stopped(self):
        if self.closing:
            self.timer.stop()
            super().reject()
