"""Read the count service report without importing detector or OCR runtimes."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import time

from PyQt5.QtCore import QTimer, QUrl, Qt
from PyQt5.QtGui import QDesktopServices, QPixmap
from PyQt5.QtWidgets import (
    QCheckBox, QDialog, QFileDialog, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout,
)


REASONS = {
    "count_difference": "数量不一致", "eligibility_pending": "有效性待定",
    "sampling_gap": "主计数采样缺口", "observation_insufficient": "跨线观测不足",
    "overlapping_detections": "检测框重叠", "suspected_merged_box": "疑似合框",
    "association_ambiguous": "轨迹关联待确认", "untracked_detection": "检测未建轨",
    "first_observed_after_line": "首次观测已在线后", "broken_track": "跨线附近断轨",
    "stream_reconnected": "相机重连", "clock_uncertain": "时间映射待校准",
    "chip_timestamp_missing": "芯片记录缺少绝对过线时间",
    "prediction_only": "只有预测框", "clock_regressed": "相机时钟回退",
    "raw_audit_unavailable": "原始检测审计不可用", "inference_failed": "检测失败",
    "evidence_unavailable": "原帧证据不足", "rescan_frame_budget": "补扫帧数超预算",
    "remaining_evidence": "补扫后仍有疑点", "group_scope_uncertain": "组别范围待确认",
    "rescan_memory_budget": "补扫内存预算不足", "TimeoutError": "补扫超时",
    "EvidenceUnavailableError": "原始录像证据不完整或超出补扫预算",
}
STATUSES = {"waiting": "等待数据", "rescanning": "自动补扫中", "unresolved": "需人工处理", "matched": "次数一致"}


def read_count_report(path, *, race_id="", stage_id=""):
    report = json.loads(Path(path).read_text(encoding="utf-8"))
    if report.get("schema_version") != 1:
        raise ValueError("不支持的计数报告版本")
    scope = report.get("scope", {})
    if race_id and scope.get("race_id") != race_id or stage_id and scope.get("stage_id") != stage_id:
        raise ValueError("计数报告与当前赛事或赛段不一致")
    if not isinstance(report.get("groups"), list):
        raise ValueError("计数报告缺少集团结果")
    return report


class CountReviewDialog(QDialog):
    def __init__(self, output_dir, parent=None, *, race_id="", stage_id=""):
        super().__init__(parent)
        self.output_dir = Path(output_dir)
        self.race_id, self.stage_id = race_id, stage_id
        self.report = None
        self.rows = []
        self.setWindowTitle("独立计数与芯片对账")
        self.resize(1100, 700)
        layout = QVBoxLayout(self)
        controls = QHBoxLayout()
        self.directory_button = QPushButton("选择计数结果目录", self)
        self.directory_button.clicked.connect(self._choose_directory)
        controls.addWidget(self.directory_button)
        self.remaining_only = QCheckBox("只看补扫后的未决短窗", self)
        self.remaining_only.setChecked(True)
        self.remaining_only.toggled.connect(self.refresh)
        controls.addWidget(self.remaining_only)
        controls.addStretch()
        layout.addLayout(controls)
        self.summary = QLabel(self)
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.table = QTableWidget(0, 8, self)
        self.table.setHorizontalHeaderLabels(["开始", "结束", "视频次数", "芯片次数", "待定", "排除", "状态", "差异原因"])
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self._show_evidence)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table)
        self.evidence_hint = QLabel(self)
        self.evidence_hint.setWordWrap(True)
        layout.addWidget(self.evidence_hint)
        self.preview = QLabel(self)
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMaximumHeight(240)
        layout.addWidget(self.preview)
        actions = QHBoxLayout()
        self.clip_button = QPushButton("打开短片", self)
        self.clip_button.clicked.connect(self._open_clip)
        actions.addWidget(self.clip_button)
        self.frames_button = QPushButton("打开证据目录", self)
        self.frames_button.clicked.connect(self._open_frames)
        actions.addWidget(self.frames_button)
        actions.addStretch()
        layout.addLayout(actions)
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.refresh()

    def _choose_directory(self):
        directory = QFileDialog.getExistingDirectory(self, "选择计数服务输出目录", str(self.output_dir))
        if directory:
            self.output_dir = Path(directory)
            self.refresh()

    def done(self, result):
        self.timer.stop()
        super().done(result)

    def refresh(self):
        path = self.output_dir / "count_reconciliation.json"
        try:
            report = read_count_report(path, race_id=self.race_id, stage_id=self.stage_id)
        except (OSError, ValueError, TypeError) as error:
            self.report = None
            self.rows = []
            self.table.setRowCount(0)
            self.summary.setText(f"等待可用的计数结果：{error}")
            self._show_evidence()
            return
        selected = self._selected()
        selected_uid = selected.get("window_id") if selected else None
        self.report = report
        groups = report["groups"]
        self.rows = [row for row in groups if not self.remaining_only.isChecked() or row["status"] == "unresolved"]
        unresolved = sum(row["status"] == "unresolved" for row in groups)
        rescanning = sum(row["status"] == "rescanning" for row in groups)
        waiting = sum(row["status"] == "waiting" for row in groups)
        stale = int(time.time() * 1000) - report.get("updated_at_ms", 0) > 5000
        scope = report["scope"]
        cutoff = report.get("cutoff_ms")
        cutoff_text = datetime.fromtimestamp(cutoff / 1000).strftime("%H:%M:%S.%f")[:-3] if cutoff is not None else "尚无进度"
        provisional = "暂定；芯片迟到和修订仍会更新结果" if report.get("chip_watermark_ms") is None else "已有芯片收齐进度"
        self.summary.setText(f"计时位置 {scope.get('timing_position', '')} · 机位 {scope.get('source_id', '')} · 截止 {cutoff_text}\n"
                             f"人工未决 {unresolved} 窗 · 自动补扫 {rescanning} 窗 · 等待数据 {waiting} 窗 · {provisional}"
                             + (" · 计数服务更新已停止" if stale else "")
                             + (" · " + "、".join(report["runtime_quality"]) if report.get("runtime_quality") else ""))
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.rows))
        restore_row = -1
        for index, row in enumerate(self.rows):
            def stamp(value):
                return datetime.fromtimestamp(value / 1000).strftime("%H:%M:%S.%f")[:-3]
            reasons = [REASONS.get(reason, reason) for reason in row["reasons"]]
            attempt_reason = row.get("rescan", {}).get("reason")
            if attempt_reason:
                reasons.append(REASONS.get(attempt_reason, attempt_reason))
            values = [stamp(row["start_ms"]), stamp(row["end_ms"]), row["video_count"], row["chip_count"],
                      row["pending_count"], row["excluded_count"], STATUSES.get(row["status"], row["status"]), "；".join(reasons)]
            for column, value in enumerate(values):
                self.table.setItem(index, column, QTableWidgetItem(str(value)))
            if row["window_id"] == selected_uid:
                restore_row = index
        self.table.blockSignals(False)
        if restore_row >= 0:
            self.table.selectRow(restore_row)
        self._show_evidence()

    def _selected(self):
        index = self.table.currentRow()
        return self.rows[index] if 0 <= index < len(self.rows) else None

    def _show_evidence(self):
        row = self._selected()
        evidence = row.get("evidence", {}) if row else {}
        keys = evidence.get("keyframes", [])
        self.preview.clear()
        if keys:
            pixmap = QPixmap(keys[len(keys) // 2])
            self.preview.setPixmap(pixmap.scaled(800, 240, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        self.clip_button.setEnabled(bool(evidence.get("clip")))
        self.frames_button.setEnabled(bool(keys))
        self.evidence_hint.setText("关键帧与原始帧时间保存在证据目录。" if keys else "该短窗的原帧证据不足，请通过赛事录像索引继续核验。" if row else "选择一个未决短窗查看证据。")

    def _open_clip(self):
        row = self._selected()
        if row and row.get("evidence", {}).get("clip"):
            QDesktopServices.openUrl(QUrl.fromLocalFile(row["evidence"]["clip"]))

    def _open_frames(self):
        row = self._selected()
        if row and row.get("evidence", {}).get("keyframes"):
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(row["evidence"]["keyframes"][0]).parent)))
