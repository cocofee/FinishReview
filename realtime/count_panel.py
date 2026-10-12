"""Inline count reconciliation controls for the existing finish console."""

from pathlib import Path
import time

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QColor, QPalette
from PyQt5.QtWidgets import (
    QCheckBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QLayout, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from .count_review import REASONS, STATUSES, read_count_report
from .race_filmstrip import format_time


class CountReconciliationPanel(QFrame):
    group_selected = pyqtSignal(object)
    scope_cleared = pyqtSignal()
    locate_requested = pyqtSignal(object)
    discovery_requested = pyqtSignal(object)
    summary_changed = pyqtSignal(str)

    def __init__(self, output_dir, parent=None):
        super().__init__(parent)
        self.setObjectName("reviewPanel")
        self.output_dir = Path(output_dir)
        self.context = None
        self.report = None
        self.rows = []
        self._selected_group = None
        self._signature = None
        layout = QVBoxLayout(self)
        layout.setSizeConstraint(QLayout.SetMinimumSize)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(4)
        controls = QHBoxLayout()
        self.summary = QLabel("等待计数结果", self)
        self.summary.setWordWrap(True)
        controls.addWidget(self.summary, 1)
        self.only_anomalies = QCheckBox("只看异常 / 等待", self)
        self.only_anomalies.setChecked(True)
        self.only_anomalies.toggled.connect(self._render)
        controls.addWidget(self.only_anomalies)
        self.directory_button = QPushButton("计数结果目录", self)
        self.directory_button.clicked.connect(self._choose_directory)
        controls.addWidget(self.directory_button)
        self.clear_button = QPushButton("全部通过", self)
        self.clear_button.clicked.connect(self.clear_scope)
        controls.addWidget(self.clear_button)
        self.locate_button = QPushButton("定位录像", self)
        self.locate_button.setEnabled(False)
        self.locate_button.clicked.connect(self._locate)
        controls.addWidget(self.locate_button)
        self.discovery_button = QPushButton("记录漏读号码", self)
        self.discovery_button.setEnabled(False)
        self.discovery_button.clicked.connect(self._discover)
        controls.addWidget(self.discovery_button)
        layout.addLayout(controls)
        self.table = QTableWidget(0, 8, self)
        self.table.setObjectName("finishCountGroupTable")
        self.table.setHorizontalHeaderLabels(["开始", "结束", "视频", "芯片", "差额", "待定", "状态", "原因"])
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        palette = self.table.palette()
        palette.setColor(QPalette.HighlightedText, QColor("#17212b"))
        self.table.setPalette(palette)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(28)
        self.table.setMaximumHeight(128)
        self.table.horizontalHeader().setStretchLastSection(True)
        for column, width in enumerate((112, 112, 54, 54, 54, 54, 106)):
            self.table.setColumnWidth(column, width)
        self.table.currentCellChanged.connect(self._select)
        self.table.cellDoubleClicked.connect(lambda *_args: self._locate())
        layout.addWidget(self.table)
        self.hint = QLabel("选中集团后，原有通过记录显示该集团已扫人员。", self)
        self.hint.setWordWrap(True)
        self.hint.setMinimumHeight(22)
        self.hint.setStyleSheet("color: #667085; font-size: 9pt;")
        layout.addWidget(self.hint)

    def set_context(self, output_dir, race_id, stage_id):
        context = (str(Path(output_dir).absolute()), race_id, stage_id)
        if context == self.context:
            return
        if self.context is None or context[0] != self.context[0]:
            self.output_dir = Path(output_dir)
        self.context = context
        self._signature = None
        self.report = None
        self.rows = []
        self.clear_scope()
        self.table.setRowCount(0)

    def _choose_directory(self):
        path = QFileDialog.getExistingDirectory(self, "选择单路计数结果目录", str(self.output_dir))
        if path:
            self.output_dir = Path(path)
            self._signature = None
            self.clear_scope()
            self.refresh()

    def refresh(self):
        path = self.output_dir / "count_reconciliation.json"
        try:
            stat = path.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
            if signature != self._signature:
                race_id, stage_id = self.context[1:] if self.context else ("", "")
                report = read_count_report(path, race_id=race_id, stage_id=stage_id)
                if self.report is not None and report["scope"] != self.report["scope"]:
                    self.clear_scope()
                self.report = report
                self._signature = signature
                self._render()
            stale = int(time.time() * 1000) - self.report.get("updated_at_ms", 0) > 5000
            video = sum(row["video_count"] for row in self.report["groups"])
            chip = sum(row["chip_count"] for row in self.report["groups"])
            unresolved = sum(row["status"] == "unresolved" for row in self.report["groups"])
            text = f"视频 {video} · 芯片 {chip} · 差额 {video-chip:+d} · 待核 {unresolved}"
            text += " · 更新已停止" if stale else " · 滚动暂定" if self.report.get("chip_watermark_ms") is None else ""
            if not self.report.get("clock", {}).get("calibrated", False):
                text += " · 校时待确认"
            if self.report.get("runtime_quality"):
                text += " · 服务异常"
            self.summary.setText(text)
            self.summary.setToolTip(f"机位 {self.report['scope'].get('source_id', '')}\n{path}")
            self.summary_changed.emit(text)
        except (OSError, ValueError, TypeError, KeyError, OverflowError) as error:
            self.report = None
            self._signature = None
            self.rows = []
            self.table.setRowCount(0)
            self.clear_scope()
            self.summary.setText("数量对账：等待可用结果")
            self.summary.setToolTip(str(error))
            self.summary_changed.emit(self.summary.text())

    def _render(self):
        if self.report is None:
            return
        previous = self._selected_group
        self.rows = [row for row in self.report["groups"]
                     if not self.only_anomalies.isChecked() or row["status"] != "matched"]
        selected = next((index for index, row in enumerate(self.rows)
                         if previous and row["window_id"] == previous["window_id"]), -1)
        if previous and selected < 0:
            old_members = {(name, uid) for name in ("video_event_uids", "chip_event_ids")
                           for uid in previous.get(name, [])}
            candidates = [index for index, row in enumerate(self.rows)
                          if old_members & {(name, uid) for name in ("video_event_uids", "chip_event_ids")
                                            for uid in row.get(name, [])}]
            if len(candidates) == 1:
                selected = candidates[0]
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.rows))
        self.table.setFixedHeight(self.table.horizontalHeader().height() + min(3, max(1, len(self.rows))) * 28 + 4)
        for index, row in enumerate(self.rows):
            reasons = "；".join(REASONS.get(value, value) for value in row.get("reasons", []))
            difference = row["video_count"] - row["chip_count"]
            values = [format_time(row["start_ms"]), format_time(row["end_ms"]), row["video_count"], row["chip_count"],
                      f"{difference:+d}", row["pending_count"], STATUSES.get(row["status"], row["status"]), reasons]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column in (2, 3, 4, 5):
                    item.setTextAlignment(Qt.AlignCenter)
                if column == 4 and difference:
                    item.setForeground(QColor("#b54747"))
                self.table.setItem(index, column, item)
        self.table.clearSelection()
        self.table.setCurrentCell(-1, -1)
        if selected >= 0:
            self.table.setCurrentCell(selected, 0)
            self.table.selectRow(selected)
        self.table.blockSignals(False)
        if selected >= 0:
            self._select(selected)
        elif previous:
            self.clear_scope()

    def _select(self, index, *_args):
        if not 0 <= index < len(self.rows):
            return
        self._selected_group = self.rows[index]
        row = self._selected_group
        self.locate_button.setEnabled(bool(self.report.get("clock", {}).get("calibrated", False)))
        self.discovery_button.setEnabled(self.locate_button.isEnabled())
        self.hint.setText(f"当前集团：视频 {row['video_count']} / 芯片 {row['chip_count']}。定位录像后，沿用逐帧判读与待补录操作。"
                          + (" 校时待确认，暂不按时间跳转。" if not self.locate_button.isEnabled() else ""))
        self.group_selected.emit(row)

    def clear_scope(self):
        had_selection = self._selected_group is not None
        self._selected_group = None
        self.table.blockSignals(True)
        self.table.clearSelection()
        self.table.setCurrentCell(-1, -1)
        self.table.blockSignals(False)
        self.locate_button.setEnabled(False)
        self.discovery_button.setEnabled(False)
        self.hint.setText("选中集团后，原有通过记录显示该集团已扫人员。")
        if had_selection:
            self.scope_cleared.emit()

    def _locate(self):
        if self._selected_group and self.locate_button.isEnabled():
            self.locate_requested.emit(self._selected_group)

    def _discover(self):
        if self._selected_group and self.discovery_button.isEnabled():
            self.discovery_requested.emit(self._selected_group)
