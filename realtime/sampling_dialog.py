"""Edit actual sampling geometry on a sealed frame; never touch a recorder."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import cv2
from PyQt5.QtCore import QPointF, Qt, QThread, QProcess, pyqtSignal
from PyQt5.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
                            QGraphicsScene, QGraphicsView, QHBoxLayout, QLabel, QLineEdit,
                            QPushButton, QVBoxLayout)

from .analysis_settings import AnalysisSettingsStore, CROP_MODES, normalized_points
from .analysis_workspace import AnalysisWorkspace
from .chip_history import format_chip_time
from .line_sampling import LineGeometry
from .problem_analysis import config_from_workspace, line_for_segment


def default_segment_index(segments, race_end_ms) -> int:
    """The recording that covers the race's own end, never merely the newest.

    A folder can hold a later test recording; the operator would then draw the
    real finish line on a picture of the wrong day. Fall back to the newest
    segment when the race clock is unknown or nothing matches it.
    """

    if race_end_ms is not None:
        for index in range(len(segments) - 1, -1, -1):
            if segments[index].start_ms <= int(race_end_ms) <= segments[index].end_ms:
                return index
        for index in range(len(segments) - 1, -1, -1):
            if segments[index].start_ms <= int(race_end_ms):
                return index
    return len(segments) - 1


class SamplingLineView(QGraphicsView):
    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setRenderHint(QPainter.Antialiasing)
        self.setMinimumSize(320, 220)
        self.setBackgroundBrush(QColor("#18242b"))
        self.points = []
        self.history = []
        self.width_px = self.height_px = 0
        self._drag = None
        self._overlays = []

    def set_frame(self, frame, line=None):
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        height, width = rgb.shape[:2]
        image = QImage(rgb.data, width, height, rgb.strides[0], QImage.Format_RGB888).copy()
        self.scene().clear()
        self._overlays = []
        self.scene().addPixmap(QPixmap.fromImage(image))
        self.scene().setSceneRect(0, 0, width, height)
        self.width_px, self.height_px = width, height
        self.points = normalized_points(line) if line and line.get("points") != [] else []
        self.history = []
        self.fitInView(self.sceneRect(), Qt.KeepAspectRatio)
        self.redraw()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.width_px:
            self.fitInView(self.sceneRect(), Qt.KeepAspectRatio)

    def normalized_at(self, position):
        point = self.mapToScene(position)
        if not self.width_px or not self.sceneRect().contains(point):
            return None
        return [point.x() / self.width_px, point.y() / self.height_px]

    def remember(self):
        self.history.append([p[:] for p in self.points])

    def undo(self):
        if self.history:
            self.points = self.history.pop()
            self.redraw()

    def clear_points(self):
        self.remember()
        self.points = []
        self.redraw()

    def nearest(self, position):
        for i, (x, y) in enumerate(self.points):
            pixel = self.mapFromScene(QPointF(x * self.width_px, y * self.height_px))
            if (pixel - position).manhattanLength() <= 16:
                return i
        return None

    def mousePressEvent(self, event):
        point = self.normalized_at(event.pos())
        if point is None:
            return
        nearest = self.nearest(event.pos())
        if event.button() == Qt.RightButton:
            if nearest is not None:
                self.remember()
                self.points.pop(nearest)
                self.redraw()
            return
        if event.button() != Qt.LeftButton:
            return
        self.remember()
        if nearest is not None:
            self._drag = nearest
        elif event.modifiers() & Qt.ShiftModifier and len(self.points) >= 2:
            px, py = point[0] * self.width_px, point[1] * self.height_px
            def distance(i):
                a, b = self.points[i], self.points[i + 1]
                ax, ay = a[0] * self.width_px, a[1] * self.height_px
                dx, dy = (b[0] - a[0]) * self.width_px, (b[1] - a[1]) * self.height_px
                t = max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / max(1e-9, dx * dx + dy * dy)))
                return (px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2
            index = min(range(len(self.points) - 1), key=distance)
            self.points.insert(index + 1, point)
        else:
            self.points.append(point)
        self.redraw()

    def mouseMoveEvent(self, event):
        point = self.normalized_at(event.pos())
        if self._drag is not None and point is not None:
            self.points[self._drag] = point
            self.redraw()

    def mouseReleaseEvent(self, event):
        point = self.normalized_at(event.pos())
        if self._drag is not None and point is not None:
            self.points[self._drag] = point
            self.redraw()
        self._drag = None

    def redraw(self):
        for item in self._overlays:
            self.scene().removeItem(item)
        self._overlays = []
        pen = QPen(QColor("#00edbb"), 2)
        pen.setCosmetic(True)
        for a, b in zip(self.points, self.points[1:]):
            self._overlays.append(self.scene().addLine(a[0] * self.width_px, a[1] * self.height_px,
                                                      b[0] * self.width_px, b[1] * self.height_px, pen))
        radius = 5 / max(.01, self.transform().m11())
        for x, y in self.points:
            self._overlays.append(self.scene().addEllipse(x * self.width_px - radius, y * self.height_px - radius,
                                                          2 * radius, 2 * radius, pen))
        self.changed.emit()


class SealedFrameWorker(QThread):
    loaded = pyqtSignal(object, float)
    failed = pyqtSignal(str)

    def __init__(self, segment, parent=None):
        super().__init__(parent)
        self.segment = segment

    def run(self):
        capture = None
        try:
            capture = cv2.VideoCapture(str(self.segment.path), cv2.CAP_FFMPEG, [cv2.CAP_PROP_N_THREADS, 1])
            ok, frame = capture.read()
            if not ok or frame is None:
                raise ValueError("无法解码此封存片段")
            if not self.isInterruptionRequested():
                self.loaded.emit(frame, float(capture.get(cv2.CAP_PROP_POS_MSEC)))
        except Exception as error:
            self.failed.emit(str(error))
        finally:
            if capture is not None:
                capture.release()


class SamplingSettingsDialog(QDialog):
    def __init__(self, workspace, parent=None, *, race_id="", stage_id=""):
        super().__init__(parent)
        self.root = Path(workspace)
        self.source = AnalysisWorkspace(
            self.root, race_id=race_id, stage_id=stage_id,
            include_review_segments=True, allow_empty_scope=True,
        )
        self.store = AnalysisSettingsStore(self.root)
        self.config = config_from_workspace(self.root)
        self.segments = sorted(self.source.archive_segments(for_configuration=True), key=lambda s: (s.camera_index, s.start_ms))
        self.worker = None
        self.probe = None
        self._closing = False
        self._drafts = {}
        self._selected = None
        self.position_ms = 0
        self._frame_ready = False
        self.setWindowTitle("实际终点线与认车配置（录制中也可设置）")
        self.resize(1000, 760)
        layout = QVBoxLayout(self)
        self.scope = QLabel(self._scope_text())
        self.scope.setWordWrap(True)
        layout.addWidget(self.scope)
        segment_row = QHBoxLayout()
        self.segment_combo = QComboBox()
        for segment in self.segments:
            self.segment_combo.addItem(f"机位 {segment.camera_index} · {format_chip_time(segment.start_ms, date=True)} · {segment.session_id}")
        segment_row.addWidget(self.segment_combo, 1)
        self.refresh_button = QPushButton("刷新画面")
        self.refresh_button.setToolTip("录像刚开始或还没有短段时点一下；不用停止录像，也不用等芯片过线")
        self.refresh_button.clicked.connect(self._reload_segments)
        segment_row.addWidget(self.refresh_button)
        layout.addLayout(segment_row)
        instructions = QLabel("左键依次加点（两点即直线）；拖动节点调整；Shift+单击在线段间插点；右键删除节点。\n"
                              "坐标按原始解码画面归一化，黑边不可落点；绿线为配置取样位置，自动贴线后的修正另存于分析证据。")
        instructions.setWordWrap(True)
        layout.addWidget(instructions)
        self.view = SamplingLineView()
        layout.addWidget(self.view, 1)
        row = QHBoxLayout()
        for label, callback in (("撤销编辑", self.view.undo), ("清空节点", self.view.clear_points)):
            button = QPushButton(label)
            button.clicked.connect(callback)
            row.addWidget(button)
        self.snap = QCheckBox("自动贴合白线（实际修正位置记录在取样证据中）")
        row.addWidget(self.snap)
        layout.addLayout(row)
        self.geometry_label = QLabel()
        layout.addWidget(self.geometry_label)
        self.view.changed.connect(self._geometry_changed)
        self.apply_scope = QLabel("保存为当前机位的终点线：从所选片段起生效，并成为该机位的默认线。\n"
                                  "之后新开的录像会话（重启、换版本、双开）自动沿用，比赛开始画一次即可。\n"
                                  "只有移动相机或换机位时才需要重画；相机在片段内移动时，请选移动后的首个完整片段。")
        self.apply_scope.setWordWrap(True)
        layout.addWidget(self.apply_scope)
        model_row = QHBoxLayout()
        self.model_path = QLineEdit(str(self.config.model_path or ""))
        self.model_path.setPlaceholderText("可选本机 YOLOX ONNX 模型；留空保留候选但不降噪")
        model_row.addWidget(self.model_path, 1)
        browse = QPushButton("选择模型")
        browse.clicked.connect(self._browse)
        model_row.addWidget(browse)
        self.crop = QComboBox()
        self.crop.addItems(CROP_MODES)
        model_row.addWidget(self.crop)
        self.probe_button = QPushButton("试运行当前画面")
        self.probe_button.clicked.connect(self._probe)
        model_row.addWidget(self.probe_button)
        layout.addLayout(model_row)
        self.model_status = QLabel()
        self.model_status.setWordWrap(True)
        layout.addWidget(self.model_status)
        self.model_path.textChanged.connect(self._model_changed)
        self.crop.currentTextChanged.connect(self._model_changed)
        self.notice = QLabel("模型为赛事分析共用；终点线和取景方式按机位保存并作为默认沿用。不会改变录像或正式成绩。")
        self.notice.setWordWrap(True)
        layout.addWidget(self.notice)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self._save)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.segment_combo.currentIndexChanged.connect(self._select)
        self._model_changed()
        if self.segments:
            self.segment_combo.setCurrentIndex(default_segment_index(self.segments, self.race_end_ms()))
            if self.worker is None:
                self._select(self.segment_combo.currentIndex())
        else:
            self.notice.setText("还没有可用画面：请先开始录像，2 秒后点「刷新画面」；"
                                "不必等芯片过线，也不用停止录像。")
            self.buttons.button(QDialogButtonBox.Save).setEnabled(False)
            self.probe_button.setEnabled(False)

    def _scope_text(self):
        if self.source.race_id:
            return f"赛事：{self.source.race_id} / {self.source.stage_id}"
        return "赛事：尚未收到（不影响画线；开赛后同一录像会话自动沿用）"

    def _reload_segments(self):
        """Pick up short segments that were sealed while this dialog was open."""

        if self.worker is not None:
            self.notice.setText("画面仍在读取，请稍候再刷新。")
            return
        segments = sorted(self.source.archive_segments(for_configuration=True),
                          key=lambda s: (s.camera_index, s.start_ms))
        if not segments:
            self.notice.setText("还没有可用画面：请确认录像已开始，2 秒后再点「刷新画面」。")
            return
        self._selected = None
        self._drafts = {}
        self._frame_ready = False
        self.segments = segments
        self.segment_combo.blockSignals(True)
        self.segment_combo.clear()
        for segment in segments:
            self.segment_combo.addItem(
                f"机位 {segment.camera_index} · {format_chip_time(segment.start_ms, date=True)} · {segment.session_id}")
        index = default_segment_index(segments, self.race_end_ms())
        self.segment_combo.setCurrentIndex(index)
        self.segment_combo.blockSignals(False)
        self._select(index)
        self.scope.setText(self._scope_text())
        self.notice.setText("已刷新画面；左键在画面上画终点线。")

    def race_end_ms(self):
        """The last chip read this race itself produced, if any."""

        for reads in (
            [read for read in self.source.reads if not read.excluded and not read.manual_entry],
            self.source.reads,
        ):
            times = [int(read.chip_ms) for read in reads if int(read.chip_ms) > 0]
            if times:
                return max(times)
        return None

    def _geometry_changed(self):
        self.geometry_label.setText(f"节点 {len(self.view.points)} · 解码画面 {self.view.width_px} × {self.view.height_px}")
        try:
            normalized_points({"points": self.view.points})
            valid = self._frame_ready
        except ValueError:
            valid = False
        if hasattr(self, "buttons"):
            self.buttons.button(QDialogButtonBox.Save).setEnabled(valid)
            self.probe_button.setEnabled(valid and self.probe is None)

    def _select(self, index):
        if self._selected is not None:
            self._drafts[self._selected] = {"points": [p[:] for p in self.view.points], "snap": self.snap.isChecked()}
        self._selected = index
        self._frame_ready = False
        self._stop_probe()
        segment = self.segments[index]
        self.segment_combo.setEnabled(False)
        self.buttons.button(QDialogButtonBox.Save).setEnabled(False)
        self.probe_button.setEnabled(False)
        self.crop.setCurrentText(self.config.crop_modes.get(segment.camera_index, "up"))
        self.worker = SealedFrameWorker(segment, self)
        self.worker.loaded.connect(self._loaded)
        self.worker.failed.connect(lambda text: self.notice.setText(text))
        self.worker.finished.connect(self._preview_done)
        self.worker.start()

    def _loaded(self, frame, pts):
        if self._closing:
            return
        self.position_ms = pts
        self._frame_ready = True
        segment = self.segments[self._selected]
        line = self._drafts.get(self._selected) or line_for_segment(self.config, self.source, segment)
        try:
            self.view.set_frame(frame, line)
        except (ValueError, KeyError, TypeError) as error:
            self.view.set_frame(frame)
            self.notice.setText(f"原取样线无效，请重新画线：{error}")
            return
        self.snap.setChecked(bool((line or {}).get("snap")))
        self._geometry_changed()
        self.notice.setText(f"机位 {segment.camera_index} · 原帧 PTS {pts:.1f} ms · 保存不停止录像；取消不落盘。")

    def _preview_done(self):
        worker, self.worker = self.worker, None
        if worker:
            worker.deleteLater()
        self.segment_combo.setEnabled(True)
        if self._closing:
            super().reject()

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择本机认车模型", self.model_path.text(), "ONNX (*.onnx)")
        if path:
            self.model_path.setText(path)

    def _model_changed(self, *_args):
        self.model_status.setText("模型未配置：降噪未启用，候选会保留，待核可能增多。" if not self.model_path.text().strip()
                                 else "模型已选择，尚未试运行；路径存在不代表已加载。")

    def _probe(self):
        if self.probe is not None:
            return
        try:
            normalized_points({"points": self.view.points})
        except (ValueError, KeyError) as error:
            self.notice.setText(str(error))
            return
        segment = self.segments[self._selected]
        model_text = self.model_path.text().strip()
        model = Path(model_text).expanduser() if model_text else None
        if model is not None and not model.is_absolute():
            model = self.root / model
        request = {"model_path": str(model or ""), "model_text": model_text, "input_size": self.config.model_input,
                   "crop_mode": self.crop.currentText(), "video_path": str(segment.path),
                   "position_ms": self.position_ms, "points": [point[:] for point in self.view.points]}
        process = self.probe = QProcess(self)
        args = [] if getattr(sys, "frozen", False) else ["-m", "realtime.review_main"]
        process.setProgram(sys.executable)
        process.setArguments(args + ["--probe-bike-model"])
        process.started.connect(lambda: (process.write(json.dumps(request).encode()), process.closeWriteChannel()))
        process.finished.connect(lambda code, _status: self._probe_done(process, request, code))
        process.errorOccurred.connect(lambda error: self._probe_done(process, request, -1)
                                     if error == QProcess.FailedToStart else None)
        self.probe_button.setEnabled(False)
        self.model_status.setText("低优先级进程试运行中；录像继续。")
        process.start()

    def _probe_done(self, process, request, code):
        if process is not self.probe:
            return
        self.probe = None
        try:
            if code:
                raise ValueError(bytes(process.readAllStandardError()).decode("utf-8", errors="replace")[-300:] or "试运行进程失败")
            result = json.loads(bytes(process.readAllStandardOutput()).decode("utf-8"))
            if (request["model_text"] == self.model_path.text().strip() and request["crop_mode"] == self.crop.currentText()
                    and request["points"] == self.view.points):
                self.model_status.setText(result["message"])
        except (ValueError, KeyError) as error:
            self.model_status.setText(f"认车试运行失败：{error}；未排除任何候选。")
        finally:
            process.deleteLater()
            self._geometry_changed()

    def _session_start_ms(self, segment):
        """A line drawn from a live 2 s segment must cover the whole recording.

        The archive rotates every five minutes; the session manifest (or the
        earliest segment of that session) gives the moment the recorder started,
        so the very first archive is sampled with this line too.
        """

        manifest = self.root / "videos" / f"{segment.session_id}_archive_session.json"
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            started = int(payload["session_started_at_ms"])
            if started > 0:
                return started
        except (OSError, ValueError, KeyError, TypeError):
            pass
        starts = [item.start_ms for item in self.segments
                  if item.camera_index == segment.camera_index
                  and item.session_id == segment.session_id]
        return min(starts) if starts else segment.start_ms

    def _save(self):
        try:
            line = {"points": self.view.points, "snap": self.snap.isChecked()}
            LineGeometry.from_config(line, self.view.width_px, self.view.height_px)
            segment = self.segments[self._selected]
            self.store.save(race_id=self.source.race_id, stage_id=self.source.stage_id,
                            segment=segment, line=line,
                            effective_from_ms=self._session_start_ms(segment),
                            model_path=self.model_path.text(), crop_mode=self.crop.currentText())
        except (OSError, ValueError, KeyError) as error:
            self.notice.setText(f"配置未保存：{error}")
            return
        self._stop_probe()
        super().accept()

    def _stop_probe(self):
        process, self.probe = self.probe, None
        if process:
            process.blockSignals(True)
            process.kill()
            process.waitForFinished(3000)
            process.deleteLater()

    def reject(self):
        self._stop_probe()
        if self.worker is not None and self.worker.isRunning():
            self._closing = True
            self.worker.requestInterruption()
            self.setEnabled(False)
            return
        super().reject()

    def closeEvent(self, event):
        self.reject()
        if self.worker is not None and self.worker.isRunning():
            event.ignore()
        else:
            event.accept()
