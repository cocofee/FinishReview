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
    "gate_count_disagreement": "各道线计数不一致", "no_video_observation": "未取得骑手观测",
    "gate_route_unconfirmed": "上下游计数未共同确认",
    "chip_receipt_missing": "芯片接收时间缺失，无法还原原始状态",
    "chip_history_incomplete": "原始芯片历史缺失", "manual_review_stale": "录像观测已改变，需重新核片",
    "manual_count_verified": "人数已核片确认",
}
STATUSES = {"waiting": "等待数据", "rescanning": "自动补扫中", "unresolved": "需人工处理", "matched": "次数一致",
            "resolved": "已处理"}


def _report_integer(value, field, *, timestamp=False):
    if type(value) is not int or value < 0:
        raise ValueError(f"计数报告 {field} 必须是非负整数")
    if timestamp:
        try:
            datetime.fromtimestamp(value / 1000)
        except (OSError, OverflowError, ValueError) as error:
            raise ValueError(f"计数报告 {field} 时间超出可显示范围") from error


def _report_strings(value, field):
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"计数报告 {field} 必须是字符串列表")


def read_count_report(path, *, race_id="", stage_id=""):
    report = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(report, dict):
        raise ValueError("计数报告必须是对象")
    if type(report.get("schema_version")) is not int or report["schema_version"] != 1:
        raise ValueError("不支持的计数报告版本")
    scope = report.get("scope")
    if not isinstance(scope, dict):
        raise ValueError("计数报告赛事范围必须是对象")
    for name in ("race_id", "stage_id", "source_id", "session_id", "timing_position"):
        if not isinstance(scope.get(name, ""), str):
            raise ValueError(f"计数报告 scope.{name} 必须是字符串")
    if race_id and scope.get("race_id") != race_id or stage_id and scope.get("stage_id") != stage_id:
        raise ValueError("计数报告与当前赛事或赛段不一致")
    if not isinstance(report.get("groups"), list):
        raise ValueError("计数报告缺少集团结果")
    clock = report.get("clock", {})
    if not isinstance(clock, dict) or type(clock.get("calibrated", False)) is not bool:
        raise ValueError("计数报告校时状态无效")
    _report_integer(report.get("updated_at_ms", 0), "updated_at_ms", timestamp=True)
    for name in ("cutoff_ms", "chip_watermark_ms"):
        if report.get(name) is not None:
            _report_integer(report[name], name, timestamp=True)
    _report_strings(report.get("runtime_quality", []), "runtime_quality")
    seen = set()
    for row in report["groups"]:
        if not isinstance(row, dict):
            raise ValueError("计数报告集团必须是对象")
        uid = row.get("window_id")
        if not isinstance(uid, str) or not uid or uid in seen:
            raise ValueError("计数报告集团标识缺失或重复")
        seen.add(uid)
        for name in ("start_ms", "end_ms", "video_count", "chip_count", "pending_count", "excluded_count"):
            _report_integer(row.get(name), name, timestamp=name.endswith("_ms"))
        if row["end_ms"] < row["start_ms"]:
            raise ValueError("计数报告集团结束时间早于开始时间")
        for name in ("evidence_start_ms", "evidence_end_ms"):
            if name in row:
                _report_integer(row[name], name, timestamp=True)
        if not isinstance(row.get("status"), str):
            raise ValueError("计数报告集团状态无效")
        for name in ("reasons", "video_event_uids", "chip_event_ids"):
            _report_strings(row.get(name, None if name == "reasons" else []), name)
        for name in ("evidence", "rescan"):
            if not isinstance(row.get(name, {}), dict):
                raise ValueError(f"计数报告 {name} 必须是对象")
        evidence = row.get("evidence", {})
        _report_strings(evidence.get("keyframes", []), "keyframes")
        if not isinstance(evidence.get("clip", ""), str) or not isinstance(row.get("rescan", {}).get("reason", ""), str):
            raise ValueError("计数报告证据路径或补扫原因无效")
        passages = row.get("chip_passages", [])
        if not isinstance(passages, list) or any(not isinstance(item, dict) for item in passages):
            raise ValueError("计数报告芯片人员明细无效")
        for passage in passages:
            if passage.get("passage_timestamp_ms") is not None:
                _report_integer(passage["passage_timestamp_ms"], "passage_timestamp_ms", timestamp=True)
        if "initial_chip_count" in row:
            _report_integer(row["initial_chip_count"], "initial_chip_count")
            _report_integer(row.get("initial_video_count"), "initial_video_count")
        for field in ("history_only", "manual_count_verified"):
            if field in row and type(row[field]) is not bool:
                raise ValueError(f"计数报告 {field} 必须是布尔值")
        if "chip_changes" in row:
            changes = row["chip_changes"]
            if not isinstance(changes, list) or any(not isinstance(c, dict) for c in changes):
                raise ValueError("计数报告芯片处理历史无效")
            for change in changes:
                if change.get("kind") not in ("added", "withdrawn", "moved"):
                    raise ValueError("计数报告芯片处理类型无效")
                if any(not isinstance(change.get(k, ""), str) for k in ("event_id", "bib")):
                    raise ValueError("计数报告芯片处理身份无效")
        if "frame_locator" in row:
            locator = row["frame_locator"]
            if not isinstance(locator, dict):
                raise ValueError("计数报告原帧定位无效")
            for field in ("segment_id", "video_path"):
                if not isinstance(locator.get(field), str) or not locator[field]:
                    raise ValueError("计数报告原帧录像标识缺失")
            for field in ("camera_index", "frame_index", "position_ms"):
                _report_integer(locator.get(field), field)
            if not locator["camera_index"]:
                raise ValueError("计数报告原帧机位无效")
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
        self.remaining_only = QCheckBox("只看异常与处理历史", self)
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
        self.passage_hint = QLabel(self)
        self.passage_hint.setWordWrap(True)
        layout.addWidget(self.passage_hint)
        self.chip_table = QTableWidget(0, 6, self)
        self.chip_table.setHorizontalHeaderLabels(["芯片过线", "号码", "运动员", "组别", "圈次", "芯片号"])
        self.chip_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.chip_table.setMaximumHeight(160)
        self.chip_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.chip_table)
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
        self.rows = [row for row in groups if not self.remaining_only.isChecked() or row["status"] in ("unresolved", "resolved")]
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
                             + (" · 离线回放" if report.get("source_mode") == "offline_replay" else " · 计数服务更新已停止" if stale else "")
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
        self._show_chip_passages(row)
        evidence = row.get("evidence", {}) if row else {}
        keys = evidence.get("keyframes", [])
        self.preview.clear()
        if keys:
            pixmap = QPixmap(keys[len(keys) // 2])
            self.preview.setPixmap(pixmap.scaled(800, 240, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        self.clip_button.setEnabled(bool(evidence.get("clip")))
        self.frames_button.setEnabled(bool(keys))
        self.evidence_hint.setText("关键帧与原始帧时间保存在证据目录。" if keys else "该短窗的原帧证据不足，请通过赛事录像索引继续核验。" if row else "选择一个未决短窗查看证据。")

    def _show_chip_passages(self, row):
        passages = row.get("chip_passages", []) if row else []
        self.chip_table.setRowCount(len(passages))
        for index, passage in enumerate(passages):
            timestamp = passage.get("passage_timestamp_ms")
            stamp = datetime.fromtimestamp(timestamp / 1000).strftime("%H:%M:%S.%f")[:-3] if timestamp is not None else "--"
            values = [stamp, passage.get("bib") or "--", passage.get("athlete_name") or passage.get("athlete_id") or "--",
                      passage.get("group_name") or passage.get("group_id") or "--", passage.get("lap", "--"),
                      passage.get("chip_id") or "--"]
            for column, value in enumerate(values):
                self.chip_table.setItem(index, column, QTableWidgetItem(str(value)))
        if row is None:
            hint = "选择短窗查看已收到的芯片人员信息。"
        elif "chip_passages" not in row:
            hint = "当前报告没有芯片人员明细，请更新计数服务后查看。"
        else:
            hint = f"本窗已收到 {row['chip_count']} 次芯片通过。列表顺序按芯片时间，未与匿名视频逐人绑定。"
            if row['video_count'] > row['chip_count']:
                hint += f" 视频多 {row['video_count'] - row['chip_count']} 次，请结合短片核实疑似漏芯片或无效通过。"
            if row['status'] in ('waiting', 'rescanning'):
                hint += " 当前仍在等待或补扫，迟到芯片会更新结果。"
            from .count_history import history_hint
            history = history_hint(row)
            if history:
                hint += " " + history + "。"
        self.passage_hint.setText(hint)

    def _open_clip(self):
        row = self._selected()
        if row and row.get("evidence", {}).get("clip"):
            QDesktopServices.openUrl(QUrl.fromLocalFile(row["evidence"]["clip"]))

    def _open_frames(self):
        row = self._selected()
        if row and row.get("evidence", {}).get("keyframes"):
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(row["evidence"]["keyframes"][0]).parent)))
