"""Offline problem-list dialog: line setup, analysis, source frames and exports."""

from dataclasses import asdict
import json
from pathlib import Path
import sys
import uuid

import cv2
from PyQt5.QtCore import QPointF, QProcess, QProcessEnvironment, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPushButton, QSpinBox, QSplitter, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from .finish_line import FinishLine
from .finish_problems import (
    ACTIONS, KINDS, apply_decisions, atomic_json, export_suggestions,
    read_decisions, record_decision, report_directory,
)
from .race_filmstrip import format_time


class FinishLineCanvas(QWidget):
    line_changed = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.image = QImage()
        self.points = []
        self.editing = True
        self.setMinimumSize(380, 240)

    def image_rect(self):
        from PyQt5.QtCore import QRectF
        if self.image.isNull():
            return QRectF()
        size = self.image.size().scaled(self.size(), Qt.KeepAspectRatio)
        return QRectF((self.width() - size.width()) / 2, (self.height() - size.height()) / 2,
                      size.width(), size.height())

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#18252e"))
        rect = self.image_rect()
        if not self.image.isNull():
            painter.drawImage(rect, self.image)
        if self.editing and len(self.points) == 2:
            painter.setPen(QPen(QColor("#ff5d5d"), 3))
            painter.drawLine(*[QPointF(rect.x() + x * rect.width(), rect.y() + y * rect.height()) for x, y in self.points])

    def mousePressEvent(self, event):
        rect = self.image_rect()
        if not self.editing or not rect.contains(event.pos()) or rect.isEmpty():
            return
        if len(self.points) == 2:
            self.points = []
        self.points.append(((event.x() - rect.x()) / rect.width(), (event.y() - rect.y()) / rect.height()))
        if len(self.points) == 2:
            try:
                line = FinishLine(1, *self.points[0], *self.points[1])
            except ValueError:
                self.points = []
            else:
                self.line_changed.emit(asdict(line))
        self.update()


class FinishProblemDialog(QDialog):
    locate_requested = pyqtSignal(object)

    def __init__(self, request_factory, signature_reader, parent=None):
        super().__init__(parent)
        self.request_factory = request_factory
        self.signature_reader = signature_reader
        self.report = None
        self.rows = []
        self.request = request_factory(0, None)
        self.directory = report_directory(self.request["output_dir"], self.request["scope"])
        self.line = self.request.get("finish_line")
        self.process = QProcess(self)
        environment = QProcessEnvironment.systemEnvironment()
        environment.insert("PYTHONIOENCODING", "utf-8")
        self.process.setProcessEnvironment(environment)
        self.process.setProcessChannelMode(QProcess.MergedChannels)
        self.process.readyReadStandardOutput.connect(self._read_output)
        self.process.finished.connect(self._finished)
        self.process.errorOccurred.connect(self._process_error)
        self.setWindowTitle("离线问题清单 · 机位 1")
        self.resize(1180, 780)
        layout = QVBoxLayout(self)
        controls = QHBoxLayout()
        self.lap = QSpinBox(self)
        self.lap.setRange(0, 999)
        self.lap.setSpecialValueText("单圈 / 未指定")
        controls.addWidget(QLabel("最后一圈：", self))
        controls.addWidget(self.lap)
        self.line_button = QPushButton("设置终点线", self)
        self.line_button.clicked.connect(self._setup_line)
        controls.addWidget(self.line_button)
        self.start_button = QPushButton("分析归档录像", self)
        self.start_button.clicked.connect(self._start)
        controls.addWidget(self.start_button)
        self.stop_button = QPushButton("停止分析", self)
        self.stop_button.clicked.connect(self._stop)
        self.stop_button.setEnabled(False)
        controls.addWidget(self.stop_button)
        self.export_button = QPushButton("导出三张建议清单", self)
        self.export_button.clicked.connect(self._export)
        controls.addWidget(self.export_button)
        controls.addStretch()
        self.show_closed = QCheckBox("显示已处理 / 已排除", self)
        self.show_closed.toggled.connect(self._render)
        controls.addWidget(self.show_closed)
        layout.addLayout(controls)
        model_controls = QHBoxLayout()
        model_controls.addWidget(QLabel("认车模型（可选）：", self))
        self.model_path = QLineEdit(self)
        self.model_path.setPlaceholderText("未配置时保留全部活动供人工核对")
        model_controls.addWidget(self.model_path, 1)
        model_button = QPushButton("选择 ONNX", self)
        model_button.clicked.connect(self._choose_model)
        model_controls.addWidget(model_button)
        self.model_size = QComboBox(self)
        self.model_size.addItem("YOLOX-S · 640", 640)
        self.model_size.addItem("YOLOX-nano · 416", 416)
        model_controls.addWidget(self.model_size)
        layout.addLayout(model_controls)
        self.summary = QLabel("等待分析", self)
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.clock_label = QLabel(self)
        self.clock_label.setWordWrap(True)
        layout.addWidget(self.clock_label)
        splitter = QSplitter(self)
        self.table = QTableWidget(0, 4, self)
        self.table.setHorizontalHeaderLabels(["时间", "问题", "相关号码", "状态"])
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        for col, width in enumerate((115, 185, 115)):
            self.table.setColumnWidth(col, width)
        self.table.currentCellChanged.connect(self._select)
        splitter.addWidget(self.table)
        right = QWidget(self)
        right_layout = QVBoxLayout(right)
        self.canvas = FinishLineCanvas(self)
        self.canvas.line_changed.connect(self._line_changed)
        right_layout.addWidget(self.canvas, 1)
        self.details = QLabel(self)
        self.details.setWordWrap(True)
        self.details.setTextInteractionFlags(Qt.TextSelectableByMouse)
        right_layout.addWidget(self.details)
        self.locate_button = QPushButton("打开原帧判读", self)
        self.locate_button.clicked.connect(self._locate)
        right_layout.addWidget(self.locate_button)
        splitter.addWidget(right)
        splitter.setSizes([620, 550])
        layout.addWidget(splitter, 1)
        actions = QHBoxLayout()
        self.action = QComboBox(self)
        for key, label in ACTIONS.items():
            self.action.addItem(label, key)
        actions.addWidget(self.action)
        self.chip = QComboBox(self)
        actions.addWidget(self.chip)
        self.bib = QLineEdit(self)
        self.bib.setPlaceholderText("补录 / 改号号码")
        self.bib.setMaximumWidth(145)
        actions.addWidget(self.bib)
        self.reason = QLineEdit(self)
        self.reason.setPlaceholderText("处理原因（必填）")
        actions.addWidget(self.reason, 1)
        self.save_button = QPushButton("保存处理", self)
        self.save_button.clicked.connect(self._save)
        actions.addWidget(self.save_button)
        layout.addLayout(actions)
        self.hint = QLabel("离线分析结果供人工核对；导出的是修改建议，正式成绩仍由 CycleRace 确认。", self)
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._update_enabled)
        self.timer.start(1000)
        current = self.directory / "latest.json"
        if current.is_file():
            try:
                self.report = json.loads(current.read_text(encoding="utf-8"))
                self._render()
            except (OSError, ValueError, KeyError, TypeError) as error:
                self.hint.setText(f"历史问题清单无法读取：{error}")
                self.report = None
        if self.report is None:
            self._setup_line()
        self._update_enabled()

    def _current(self):
        if self.report is None:
            return False
        return self.signature_reader() == (self.request["output_dir"], self.report["scope"],
                                           self.report["chip_signature"], self.report["input_signature"])

    def _busy(self):
        return self.process.state() != QProcess.NotRunning

    def _update_enabled(self):
        current = self._current() and not self._busy()
        row = self._row()
        self.start_button.setEnabled(not self._busy() and bool(self.line))
        self.line_button.setEnabled(not self._busy())
        self.lap.setEnabled(not self._busy())
        self.save_button.setEnabled(current and row is not None)
        self.export_button.setEnabled(current)
        self.locate_button.setEnabled(current and row is not None and bool(row.get("locator")))
        if self.report and not current and not self._busy():
            self.hint.setText("赛事或芯片记录已更新，请重新分析后再处理或导出。")

    def _setup_line(self):
        if not self.request["sources"]:
            self.hint.setText("没有可用的封口归档录像，请先归档录像。")
            return
        capture = cv2.VideoCapture(self.request["sources"][0]["video_path"])
        try:
            ok, frame = capture.read()
            if not ok:
                self.hint.setText("第一段录像无法解码，请检查原文件。")
                return
            h, w = frame.shape[:2]
            scale = min(1, 960 / w)
            rgb = cv2.cvtColor(cv2.resize(frame, (round(w * scale), round(h * scale))), cv2.COLOR_BGR2RGB)
            self.canvas.image = QImage(rgb.data, rgb.shape[1], rgb.shape[0], rgb.strides[0], QImage.Format_RGB888).copy()
        finally:
            capture.release()
        self.canvas.editing = True
        self.canvas.points = [(self.line["x1"], self.line["y1"]), (self.line["x2"], self.line["y2"])] if self.line else []
        self.canvas.update()
        self.hint.setText("在画面上依次点击终点线的两个端点；已有红线可直接使用。")

    def _line_changed(self, line):
        self.line = line
        self.hint.setText("终点线已设置，可开始分析。")
        self._update_enabled()

    def _start(self):
        try:
            request = self.request_factory(self.lap.value(), self.line)
            if not request["sources"]:
                raise ValueError("没有可用的封口归档录像")
            if self.lap.value() == 0 and len({c.get("lap", 0) for c in request["chips"] if c.get("is_active", True)}) > 1:
                raise ValueError("检测到多圈记录，请指定最后一圈；不同组末圈不同的赛事暂需分组核对")
            self.request = request
            request["model_path"] = self.model_path.text().strip()
            request["model_size"] = self.model_size.currentData()
            self.directory = report_directory(request["output_dir"], request["scope"])
            self.run_dir = self.directory / ("run-" + uuid.uuid4().hex[:12])
            atomic_json(self.run_dir / "request.json", request)
            self.report = None
            self.rows = []
            self.table.setRowCount(0)
            self.canvas.editing = False
            self.summary.setText("正在分析归档录像…")
            arguments = (["--offline-finish-worker"] if getattr(sys, "frozen", False) else ["-m", "realtime.offline_finish"])
            self.process.setWorkingDirectory(str(Path(__file__).resolve().parent.parent))
            self.process.start(sys.executable, arguments + [str(self.run_dir / "request.json")])
            self.stop_button.setEnabled(True)
            self._update_enabled()
        except (OSError, ValueError, KeyError) as error:
            self.hint.setText(str(error))

    def _choose_model(self):
        path, _filter = QFileDialog.getOpenFileName(self, "选择 YOLOX ONNX 模型", "", "ONNX 模型 (*.onnx)")
        if path:
            self.model_path.setText(path)

    def _read_output(self):
        output = bytes(self.process.readAllStandardOutput()).decode("utf-8", errors="replace").strip()
        if output:
            self.hint.setText(output.splitlines()[-1])

    def _process_error(self, error):
        self.hint.setText(f"分析进程无法运行：{self.process.errorString()}")
        self.stop_button.setEnabled(False)
        self._update_enabled()

    def _finished(self, exit_code, exit_status):
        self._read_output()
        self.stop_button.setEnabled(False)
        if exit_code == 0 and exit_status == QProcess.NormalExit:
            try:
                self.report = json.loads((self.run_dir / "report.json").read_text(encoding="utf-8"))
                if self.report["scope"] != self.request["scope"]:
                    raise ValueError("分析结果赛事不匹配")
                atomic_json(self.directory / "latest.json", self.report)
                self._render()
            except (OSError, ValueError, KeyError) as error:
                self.report = None
                self.hint.setText(f"分析结果未保存：{error}")
        else:
            self.summary.setText("分析未完成，可调整后重新运行")
        self._update_enabled()

    def _stop(self):
        self.process.kill()

    def _render(self):
        if not self.report:
            return
        previous = self._row()
        all_rows = apply_decisions(self.report, read_decisions(self.directory))
        self.rows = [r for r in all_rows if self.show_closed.isChecked() or r["status"] not in ("resolved", "excluded")]
        pending = sum(r["status"] in ("pending", "pending_sync") for r in all_rows)
        unknown = sum(len(r["chips"]) for r in all_rows if r["kind"] == "no_video")
        self.summary.setText(f"自动配对通过 {len(self.report['automatic_passes'])} 条 · 待处理 {pending} 项 · 录像缺口内 {unknown} 条芯片")
        clock = self.report["clock"]
        self.clock_label.setText((f"自动校时：{clock['offset_ms']:+.0f} ms · 漂移 {clock['drift_ms_per_hour']:+.0f} ms/小时 · "
                                 f"残差中位数 {clock['residual_median_ms']:.0f} ms · {clock['anchors']} 个锚点 · 版本 {clock['version']}"
                                 if clock else "自动校时等待；不会生成自动通过记录") +
                                ("；与人工校时冲突，请核对" if self.report["manual_conflicts"] else ""))
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.rows))
        labels = dict(pending="待核对", pending_sync="待上游修改", resolved="已处理", insufficient="证据不足", excluded="已排除")
        for index, row in enumerate(self.rows):
            values = (format_time(row["start_ms"]) if row["start_ms"] else "—", KINDS[row["kind"]],
                      ", ".join(c.get("bib", "") for c in row["chips"]) or "—", labels[row["status"]])
            for column, value in enumerate(values):
                self.table.setItem(index, column, QTableWidgetItem(value))
        self.table.blockSignals(False)
        selected = next((i for i, r in enumerate(self.rows) if previous and r["problem_id"] == previous["problem_id"]), 0)
        if self.rows:
            self.table.setCurrentCell(selected, 0)
            self.table.selectRow(selected)
        self._select()
        self.hint.setText("；".join(self.report.get("warnings", [])) or "选择问题查看证据；自动配对与人工确认分开保存。")

    def _row(self):
        index = self.table.currentRow() if hasattr(self, "table") else -1
        return self.rows[index] if 0 <= index < len(self.rows) else None

    def _select(self, *_args):
        row = self._row()
        self.chip.clear()
        if row:
            for c in row["chips"]:
                self.chip.addItem(f"{c.get('bib', '')} · {c.get('chip_id', '')}", c["event_id"])
            self.canvas.image = QImage(row.get("card_path", ""))
            self.canvas.editing = False
            self.canvas.update()
            locator = row.get("locator")
            self.details.setText(row["note"] + (f"\n原录像：{Path(locator['video_path']).name}\n"
                                 f"原帧 {locator['frame_index'] + 1} · 片内 {locator['position_ms'] / 1000:.3f} 秒" if locator else "\n当前没有可用原帧证据"))
            decision = row.get("decision", {})
            self.reason.setText(decision.get("reason", ""))
            self.bib.setText(decision.get("bib", ""))
        else:
            self.details.clear()
        self._update_enabled()

    def _locate(self):
        if self._current() and self._row():
            self.locate_requested.emit(self._row())

    def _save(self):
        if not self._current() or not self._row():
            self.hint.setText("当前结果已过期，请重新分析")
            return
        try:
            record_decision(self.directory, self.report, self._row(), action=self.action.currentData(),
                            reason=self.reason.text(), bib=self.bib.text(),
                            lap=self.lap.value() or None, chip_event_id=self.chip.currentData() or "")
            self._render()
        except (OSError, ValueError, RuntimeError) as error:
            self.hint.setText(str(error))

    def _export(self):
        if not self._current():
            return
        path = QFileDialog.getExistingDirectory(self, "导出修改建议", str(self.directory))
        if not path:
            return
        try:
            outputs = export_suggestions(self.directory, self.report, path)
            self.hint.setText("已导出：" + "、".join(p.name for p in outputs))
        except (OSError, ValueError) as error:
            self.hint.setText(f"导出失败：{error}")

    def closeEvent(self, event):
        self.timer.stop()
        if self._busy():
            self.process.kill()
            self.process.waitForFinished(3000)
        super().closeEvent(event)
