"""Runtime status and operator controls of the finish review window."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from types import MappingProxyType

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStyle,
    QVBoxLayout,
)

from . import APP_DISPLAY_NAME
from .runtime_status import (
    RuntimeStatusSnapshot as RuntimeStatusSnapshot,
    RuntimeStatusPresenter,
    STATUS_WIDGET_NAMES,
)
from .decode_resources import DECODE_RESOURCES
from .auyat_rgb import is_network_share
from .workspace_context import EventWorkspaceMetadataStore
from .review_recorder import PassageReviewState
from .stream_recorder import sanitize_recording_message
from .review_window_widgets import _CompactStatusIndicator, _ElidedLabel, _FinishReviewLogo
from .ui_parts import UiPart

logger = logging.getLogger("FinishReview")


class FinishReviewStatusPart(UiPart):
    """Runtime status bar, metrics, header and deployment snapshot, and the operator controls."""

    def _observe_runtime_metric(
        self,
        operation: str,
        started_at: float,
        *,
        item_count: int = 0,
        failed: bool = False,
    ) -> None:
        self._host.runtime_metrics.observe(
            operation,
            (time.perf_counter() - started_at) * 1000.0,
            item_count=item_count,
            failed=failed,
        )

    def _log_runtime_metrics(self) -> None:
        if getattr(self._host, "_runtime_metrics_reported", False):
            return
        self._host._runtime_metrics_reported = True
        logger.info("Review decode resources: %s", DECODE_RESOURCES.snapshot())
        catalog = getattr(self._host, "_recording_catalog", None)
        if catalog is not None:
            logger.info("Recording file checks: %s", catalog.metrics.snapshot("catalog.file_check"))
        for summary in (*self._host.runtime_metrics.snapshots(), *DECODE_RESOURCES.metrics.snapshots()):
            logger.info(
                "Runtime metrics operation=%s count=%d items_total=%d failures=%d "
                "p50=%.1fms p95=%.1fms max=%.1fms",
                summary.operation,
                summary.count,
                summary.item_count,
                summary.failure_count,
                summary.p50_ms,
                summary.p95_ms,
                summary.max_ms,
            )

    def _update_operator_controls(self) -> None:
        if not hasattr(self._host, "operator_identity_label"):
            return
        event = self._host.passage_store.get(self._host._selected_event_id)
        identity = ""
        if event is not None:
            identity = event.bib.strip()
        else:
            identity = self._host.selected_identity_value.text().strip()
            if identity == "--":
                identity = ""
        if identity:
            row = self._host.table.currentRow()
            position_text = (
                f"{row + 1:,} / {len(self._host._visible_events):,}"
                if 0 <= row < len(self._host._visible_events)
                else "未进入终点记录"
            )
            group_label = self._host.group_value.text().strip()
            review_status = (
                self._host._display_confirmation_status(
                    self._host._event_review_statuses.get(self._host._selected_event_id, "")
                )
                if event is not None
                else "尚无通过记录"
            )
            self._host.operator_identity_label.setText(
                " · ".join(
                    value
                    for value in (group_label, position_text, review_status)
                    if value and value != "--"
                )
            )
        else:
            self._host.operator_identity_label.clear()
        active_panes = set(self._host.evidence_panes)
        for pane in self._host.all_evidence_panes:
            pane.mark_btn.setEnabled(
                bool(
                    pane in active_panes
                    and identity
                    and getattr(pane.video_view, "has_frame", False)
                )
            )
        if hasattr(self._host, "mark_regular_button"):
            self._host.mark_regular_button.setEnabled(
                bool(
                    identity
                    and any(
                        getattr(pane.video_view, "has_frame", False)
                        for pane in self._host.regular_panes
                    )
                )
            )
        if hasattr(self._host, "mark_high_speed_button"):
            self._host.mark_high_speed_button.setEnabled(
                bool(
                    self._host.high_speed_dir is not None
                    and identity
                    and getattr(self._host.high_speed_pane.video_view, "has_frame", False)
                )
            )
        has_pending_marker = any(
            pane.has_pending_marker for pane in self._host.evidence_panes
        )
        self._host.confirm_next_button.setEnabled(bool(identity and has_pending_marker))
        if getattr(self._host, "_problem_workbench_active", False):
            self._host.confirm_next_button.setEnabled(False)
        for pane in self._host.all_evidence_panes:
            legacy = not getattr(self._host, "_problem_workbench_active", False)
            if not legacy and not hasattr(pane, "_workbench_video_minimum"):
                pane._workbench_video_minimum = pane.video_view.minimumSize()
                pane.video_view.setMinimumSize(0, 140)
            elif legacy and hasattr(pane, "_workbench_video_minimum"):
                pane.video_view.setMinimumSize(pane._workbench_video_minimum)
                del pane._workbench_video_minimum
            pane.mark_btn.setVisible(legacy)
            pane.confirm_btn.setVisible(legacy)
            pane.status_label.setVisible(legacy)
            pane.active_badge.setVisible(legacy)
            pane.video_view.set_identity_badge_visible(legacy)

    def _pending_marker_pane(self):
        return next(
            (pane for pane in self._host.evidence_panes if pane.has_pending_marker),
            None,
        )

    def _regular_pane_for_operator_marking(self):
        return next(
            (
                pane
                for pane in self._host.regular_panes
                if getattr(pane.video_view, "has_frame", False)
            ),
            self._host.regular_pane,
        )

    def _confirm_current_marker(self) -> None:
        pane = self._host._pending_marker_pane()
        if pane is None:
            return
        self._host._confirm_pending_marker(pane)
        self._host._update_operator_controls()

    def _confirm_and_next(self) -> None:
        pane = self._host._pending_marker_pane()
        if pane is None:
            return
        row = self._host.table.currentRow()
        event_id = self._host._selected_event_id
        confirmed = self._host._confirm_pending_marker(pane)
        if (
            confirmed
            and self._host._selected_event_id == event_id
            and 0 <= row < self._host.table.rowCount() - 1
        ):
            self._host._move_selection(1)

    def _init_runtime_status(self) -> None:
        panel = QFrame(self._host)
        panel.setObjectName("finishConsoleHeader")
        panel.setMinimumHeight(78)
        panel.setStyleSheet(
            "QFrame#finishConsoleHeader { background: #ffffff; "
            "border: 1px solid #cfd7df; border-radius: 4px; }"
            "QPushButton { min-height: 32px; padding: 0 10px; "
            "font-size: 10pt; font-weight: 600; }"
        )
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(10, 5, 10, 5)
        panel_layout.setSpacing(4)

        top_layout = QHBoxLayout()
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(8)

        top_layout.addWidget(_FinishReviewLogo(panel))
        self._host.product_title_label = QLabel(APP_DISPLAY_NAME, panel)
        self._host.product_title_label.setStyleSheet(
            "font-size: 12pt; font-weight: 700; color: #17212b;"
        )
        self._host.product_subtitle_label = QLabel("终点多源复核", panel)
        self._host.product_subtitle_label.hide()
        top_layout.addWidget(self._host.product_title_label)

        brand_separator = QFrame(panel)
        brand_separator.setFrameShape(QFrame.VLine)
        brand_separator.setStyleSheet("color: #dce2e6;")
        top_layout.addWidget(brand_separator)

        event_layout = QHBoxLayout()
        event_layout.setContentsMargins(0, 0, 0, 0)
        event_layout.setSpacing(10)
        self._host.event_name_label = _ElidedLabel("未加载赛事", panel)
        self._host.event_name_label.setStyleSheet(
            "font-size: 11pt; font-weight: 700; color: #17212b;"
        )
        self._host.event_name_label.setMinimumWidth(120)
        self._host.event_name_label.setSizePolicy(
            QSizePolicy.Ignored,
            QSizePolicy.Preferred,
        )
        self._host.event_path_label = _ElidedLabel(
            "等待计时源赛事信息 · 终点",
            panel,
        )
        self._host.event_path_label.setStyleSheet(
            "font-size: 10pt; font-weight: 600; color: #667085;"
        )
        self._host.event_path_label.setMinimumWidth(100)
        self._host.event_path_label.setSizePolicy(
            QSizePolicy.Ignored,
            QSizePolicy.Preferred,
        )
        event_layout.addWidget(self._host.event_name_label, 3)
        event_layout.addWidget(self._host.event_path_label, 2)
        top_layout.addLayout(event_layout, 1)

        self._host.runtime_alert_label = QLabel(panel)
        self._host.runtime_alert_label.setStyleSheet(
            "color: #b54747; font-size: 9pt; font-weight: 700;"
        )
        self._host.runtime_alert_label.setMaximumWidth(180)
        self._host.runtime_alert_label.hide()
        top_layout.addWidget(self._host.runtime_alert_label)

        clock_layout = QHBoxLayout()
        clock_layout.setContentsMargins(0, 0, 0, 0)
        clock_layout.setSpacing(4)
        self._host.beijing_clock_label = QLabel(panel)
        # Keep the live clock readable when the event header is compressed.
        self._host.beijing_clock_label.setMinimumWidth(72)
        self._host.beijing_clock_label.setSizePolicy(
            QSizePolicy.Fixed,
            QSizePolicy.Preferred,
        )
        self._host.beijing_clock_label.setStyleSheet(
            "font-family: Consolas; color: #17212b; font-size: 11pt; font-weight: 700;"
        )
        self._host.beijing_zone_label = QLabel("北京时间", panel)
        self._host.beijing_zone_label.setMinimumWidth(64)
        self._host.beijing_zone_label.setSizePolicy(
            QSizePolicy.Fixed,
            QSizePolicy.Preferred,
        )
        self._host.beijing_zone_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self._host.beijing_zone_label.setStyleSheet(
            "color: #667085; font-size: 9pt; font-weight: 500;"
        )
        clock_layout.addWidget(self._host.beijing_clock_label)
        clock_layout.addWidget(self._host.beijing_zone_label)
        top_layout.addLayout(clock_layout)

        self._host.race_dir_label = QLabel(panel)
        self._host.race_dir_label.hide()
        self._host.recording_status_label = QLabel(panel)
        self._host.recording_status_label.hide()
        self._host.storage_status_label = QLabel(panel)
        self._host.storage_status_label.hide()
        self._host.capture_status_label = QLabel(panel)
        self._host.capture_status_label.hide()

        self._host.recheck_button = QPushButton("刷新", panel)
        self._host.recheck_button.setObjectName("finishRecheckButton")
        self._host.recheck_button.setIcon(self._host.style().standardIcon(QStyle.SP_BrowserReload))
        self._host.recheck_button.setFixedHeight(32)
        self._host.recheck_button.setToolTip("重新检查连接")
        self._host.recheck_button.clicked.connect(self._host._recheck_connections)
        top_layout.addWidget(self._host.recheck_button)
        self._host.settings_button = QPushButton("设置", panel)
        self._host.settings_button.setObjectName("finishSettingsButton")
        self._host.settings_button.setFixedHeight(32)
        self._host.settings_button.setToolTip("设备与赛事设置")
        self._host.settings_button.clicked.connect(self._host._configure_devices)
        top_layout.addWidget(self._host.settings_button)
        self._host.problem_list_button = QPushButton("问题清单", panel)
        self._host.problem_list_button.setObjectName("finishProblemListButton")
        self._host.problem_list_button.setFixedHeight(32)
        self._host.problem_list_button.setCheckable(True)
        self._host.problem_list_button.clicked.connect(self._host._open_problem_list)
        # The workbench is the only view now: its table already lists every chip,
        # with search, groups and delete; the old records page has no entry.
        self._host.problem_list_button.hide()
        top_layout.addWidget(self._host.problem_list_button)
        tool_button_style = (
            "QPushButton { background: #eef3f5; color: #17212b; "
            "border: 1px solid #aab6bf; border-radius: 4px; "
            "font-size: 10pt; font-weight: 600; }"
            "QPushButton:hover { background: #dfe9ed; border-color: #617783; }"
            "QPushButton:pressed { background: #cfdde2; }"
        )
        self._host.recheck_button.setStyleSheet(tool_button_style)
        self._host.settings_button.setStyleSheet(tool_button_style)
        self._host.problem_list_button.setStyleSheet(tool_button_style)
        self._host.recording_reminder = QPushButton("有芯片读数，还没录像 ▶", panel)
        self._host.recording_reminder.setObjectName("finishRecordingReminder")
        self._host.recording_reminder.setToolTip("正式比赛请点右边「开始录像」。只是测芯片就不用管；点这条提示本场不再提醒。")
        self._host.recording_reminder.setStyleSheet(
            "QPushButton { background: #b42318; color: #ffffff; border: none; border-radius: 4px;"
            " padding: 2px 10px; font-size: 10pt; font-weight: 700; }")
        self._host.recording_reminder.clicked.connect(self._host._dismiss_recording_reminder)
        self._host.recording_reminder.hide()
        top_layout.addWidget(self._host.recording_reminder)
        self._host.record_button = QPushButton("开始录像", panel)
        self._host.record_button.setObjectName("finishRecordButton")
        self._host.record_button.setMinimumWidth(88)
        self._host.record_button.clicked.connect(self._host._toggle_recording)
        top_layout.addWidget(self._host.record_button)
        self._host.archive_scan_button = QPushButton("分析历史视频", panel)
        self._host.archive_scan_button.setObjectName("finishArchiveScanButton")
        self._host.archive_scan_button.setMinimumWidth(104)
        self._host.archive_scan_button.setToolTip(
            "仅在历史比赛中手动启动一次性视频候选分析"
        )
        self._host.archive_scan_button.clicked.connect(self._host._toggle_archive_video_scan)
        self._host.archive_scan_button.setVisible(False)
        top_layout.addWidget(self._host.archive_scan_button)
        panel_layout.addLayout(top_layout)

        status_strip = QFrame(panel)
        status_strip.setObjectName("finishStatusStrip")
        status_strip.setStyleSheet(
            "QFrame#finishStatusStrip { background: #f5f7f8; "
            "border: none; border-radius: 3px; }"
        )
        status_layout = QHBoxLayout(status_strip)
        status_layout.setContentsMargins(2, 0, 2, 0)
        status_layout.setSpacing(2)
        self._host.receiver_status_label = self._host._status_chip("计时源", status_strip)
        self._host.camera_status_label = self._host._status_chip("普通摄像", status_strip)
        self._host.high_speed_status_label = self._host._status_chip("高速摄像", status_strip)
        self._host.video_assist_status_label = self._host._status_chip("视频辅助", status_strip)
        status_layout.addWidget(self._host.receiver_status_label)
        status_layout.addWidget(self._host.camera_status_label)
        status_layout.addWidget(self._host.high_speed_status_label)
        status_layout.addWidget(self._host.video_assist_status_label)
        status_layout.addStretch(1)
        panel_layout.addWidget(status_strip)
        self._host.runtime_status_strip = status_strip
        self._host.runtime_header = panel
        root_layout = self._host.layout()
        if root_layout is not None:
            root_layout.insertWidget(0, panel)
        self._host._update_event_header()

    @staticmethod
    def _status_chip(title: str, parent) -> _CompactStatusIndicator:
        return _CompactStatusIndicator(title, parent)

    def _update_event_header(self) -> None:
        metadata = self._host._current_metadata()
        event = self._host.passage_store.get(self._host._selected_event_id)
        race_name = (
            (metadata.race_name.strip() if metadata is not None else "")
            or (event.race_name.strip() if event is not None else "")
            or "未加载赛事"
        )
        race_id = (
            (metadata.race_id if metadata is not None else "")
            or (event.race_id if event is not None else "")
        )
        stage_name = (
            (metadata.stage_name.strip() if metadata is not None else "")
            or (event.stage_name.strip() if event is not None else "")
            or "终点"
        )
        stage_label = f"第 {stage_name} 赛段" if stage_name.isdigit() else stage_name
        group_name = self._host.group_value.text().strip()
        if not group_name or group_name == "--":
            group_name = "全部组别"
        detail_parts = []
        context_values = (
            ("历史复核", stage_label, group_name, "终点")
            if self._host._workspace_mode == "archive"
            else (stage_label, group_name, "终点")
        )
        for value in context_values:
            if value and value not in detail_parts:
                detail_parts.append(value)
        if (self._host._workspace_mode == "live" and isinstance(self._host.metadata_store, EventWorkspaceMetadataStore)
                and self._host.metadata_store.recovered):
            detail_parts.append("完整赛事信息待同步")
        detail_text = " · ".join(detail_parts)
        identity_text = f"赛事ID：{race_id}\n" if race_id else ""
        self._host.event_name_label.setText(race_name)
        self._host.event_path_label.setText(detail_text)
        self._host.event_name_label.setToolTip(
            f"{race_name}\n{detail_text}\n{identity_text}赛事目录：{self._host.output_dir}"
        )
        self._host.event_path_label.setToolTip(
            f"{detail_text}\n{identity_text}赛事目录：{self._host.output_dir}"
        )
        if metadata is None and len({(item.race_id, item.stage_id)
                                     for item in self._host.passage_store.events()}) > 1:
            self._host.event_name_label.setText("未确定赛事")
            self._host.event_path_label.setText("此目录包含多场赛事")
        # The workbench top bar names the race it is reporting on.
        self._host._event_label = f"{race_name} · {stage_label}" if metadata is not None or event is not None else "未加载赛事"

    def _set_compact_header(self, compact: bool) -> None:
        """In the workbench the top bar is one low line, and 刷新 / 设置 are under 更多.

        11-14: the picture was letterboxed by height (150 px of black either
        side on a 16-inch laptop); every line taken off the top and the bottom
        goes to it.
        """
        header = getattr(self._host, "runtime_header", None)
        if header is None:
            return
        header.setMinimumHeight(38 if compact else 78)
        header.layout().setContentsMargins(10, 1 if compact else 5, 10, 1 if compact else 5)
        style = header.styleSheet()
        header.setStyleSheet(style.replace("min-height: 32px", "min-height: 26px") if compact
                             else style.replace("min-height: 26px", "min-height: 32px"))
        for button in (self._host.recheck_button, self._host.settings_button):
            button.setVisible(not compact)
        self._host.product_title_label.setVisible(not compact)
        menu = getattr(getattr(self._host.problem_panel, "more_button", None), "menu", lambda: None)()
        if menu is not None and not getattr(self._host, "_header_actions", None):
            menu.addAction("清理旧赛事文件夹…", self._host._clean_old_folders)
            menu.addSeparator()
            self._host._header_actions = [menu.addAction("重新检查连接（刷新）", self._host.recheck_button.click),
                                    menu.addAction("设备与赛事设置", self._host.settings_button.click)]
        for action in getattr(self._host, "_header_actions", None) or ():
            action.setVisible(compact)

    def _init_operator_controls(self) -> None:
        self._host.operator_identity_label = self._host.current_context_label
        self._host.mark_regular_button = self._host.regular_pane.mark_btn
        self._host.mark_regular_button.clicked.disconnect()
        self._host.mark_regular_button.clicked.connect(
            lambda: self._host._begin_marking(self._host._regular_pane_for_operator_marking())
        )
        self._host.mark_high_speed_button = self._host.high_speed_pane.mark_btn
        self._host.mark_high_speed_button.setVisible(self._host.high_speed_dir is not None)
        self._host.confirm_next_button = QPushButton("确认并下一条", self._host.transport)
        self._host.confirm_next_button.setShortcut("Ctrl+Return")
        self._host.confirm_next_button.setToolTip("确认当前标线并选择下一条")
        self._host.confirm_next_button.clicked.connect(self._host._confirm_and_next)
        self._host.transport_layout.addWidget(self._host.confirm_next_button)
        self._host.evidence_pane_added.connect(self._host._bind_operator_pane)
        for pane in self._host.all_evidence_panes:
            self._host._bind_operator_pane(pane)
        self._host._update_operator_controls()

    def _bind_operator_pane(self, pane) -> None:
        pane.video_view.marker_position_selected.connect(
            lambda _x, _y: QTimer.singleShot(0, self._host._update_operator_controls)
        )
        pane.confirmation_requested.connect(
            lambda _pane: QTimer.singleShot(0, self._host._update_operator_controls)
        )
        pane.cancel_requested.connect(
            lambda _pane: QTimer.singleShot(0, self._host._update_operator_controls)
        )
        pane.delete_requested.connect(
            lambda _pane: QTimer.singleShot(0, self._host._update_operator_controls)
        )
        # Counting a crossing: the picture reports where the click landed, this
        # window knows which frame it landed on, and the panel keeps the count.
        pane.video_view.count_mark_selected.connect(
            lambda x, y, pane=pane: self._host._problem_count_clicked(x, y, pane)
        )
        pane.video_view.count_mark_cancelled.connect(
            lambda x, y, pane=pane: self._host._problem_count_cancelled(x, y, pane)
        )
        pane.video_view.frame_wheel.connect(lambda delta, pane=pane: self._host._problem_wheel_step(delta, pane))
        self._host._problem_count_mode_changed(True)

    def _deployment_runtime_snapshot(self) -> dict[str, str]:
        timing_name = "赛虎" if self._host.timing_provider == "racetiger" else "CycleRace"
        if self._host._receiver_error:
            timing_state = "异常"
            timing_detail = self._host._receiver_error
        elif self._host._last_passage_monotonic:
            age_seconds = max(0.0, time.monotonic() - self._host._last_passage_monotonic)
            timing_state = "通过" if age_seconds <= 30.0 else "待检查"
            timing_detail = (
                f"{age_seconds:.0f}秒前收到{timing_name}数据"
                if age_seconds >= 1.0
                else f"刚刚收到{timing_name}数据"
            )
        elif self._host.timing_provider == "racetiger":
            source = self._host._racetiger_source
            if source is not None and source.is_running:
                timing_state = "待检查"
                timing_detail = "赛虎读取服务运行中，尚未读取到本次终点记录"
            else:
                timing_state = "异常"
                timing_detail = "赛虎读取服务未启动"
        elif self._host._receiver is not None and self._host._receiver.is_running:
            timing_state = "待检查"
            timing_detail = "接收服务正在监听，尚未收到本次数据"
        else:
            timing_state = "异常"
            timing_detail = "CycleRace接收服务未启动"

        configured_sources = self._host._configured_recording_sources()
        active_recorders = {
            camera_index: recorder
            for camera_index, recorder in self._host._recorders.items()
            if recorder.is_running
        }
        if self._host._runtime_error:
            camera_state = "异常"
            camera_detail = self._host._runtime_error
        elif active_recorders:
            missing = [
                camera_index
                for camera_index, _source in configured_sources
                if camera_index not in active_recorders
            ]
            segment_counts = {
                camera_index: len(ring_buffer.status_segments())
                for camera_index, ring_buffer in self._host._ring_buffers.items()
            }
            waiting = [
                camera_index
                for camera_index in active_recorders
                if segment_counts.get(camera_index, 0) == 0
            ]
            if missing:
                camera_state = "异常"
                camera_detail = "普通机位未全部运行：" + "、".join(
                    f"机位{camera_index}" for camera_index in missing
                )
            elif waiting:
                camera_state = "待检查"
                camera_detail = "等待首个缓存片段：" + "、".join(
                    f"机位{camera_index}" for camera_index in waiting
                )
            else:
                camera_state = "通过"
                camera_detail = "普通录像全部运行：" + "、".join(
                    f"机位{camera_index} {segment_counts[camera_index]}段"
                    for camera_index in sorted(active_recorders)
                )
        elif configured_sources:
            camera_state = "待检查"
            camera_detail = f"已配置 {len(configured_sources)} 个普通机位，尚未开始录像"
        else:
            camera_state = "异常"
            camera_detail = "未配置普通录像源"

        high_speed = self._host._high_speed_scan_result
        if self._host.high_speed_dir is None:
            high_speed_state = "异常"
            high_speed_detail = "未配置Auyat共享目录"
        elif high_speed.status == "unavailable":
            high_speed_state = "异常"
            high_speed_detail = high_speed.message or "Auyat目录不可访问"
        elif high_speed.status == "ready":
            high_speed_state = "通过"
            high_speed_detail = f"目录可读，已索引 {len(high_speed.captures)} 段"
        else:
            high_speed_state = "待检查"
            high_speed_detail = high_speed.message or "目录可读，等待高速画面"
        metadata = self._host._current_metadata()
        if self._host.timing_provider == "cyclerace":
            event_name = (
                (metadata.race_name.strip() or metadata.race_id)
                if metadata is not None
                else ""
            )
            event_stage = (
                (metadata.stage_name.strip() or metadata.stage_id)
                if metadata is not None
                else ""
            )
            event_state = (
                "历史赛事已打开"
                if metadata is not None and self._host._workspace_mode == "archive"
                else "赛事已加载"
                if metadata is not None
                else "等待 CycleRace 赛事信息"
            )
            event_dir = str(self._host.output_dir) if metadata is not None else ""
        else:
            event_name = self._host.racetiger_rid
            event_stage = "终点" if event_name else ""
            event_state = "赛事已配置" if event_name else "等待赛虎赛事 RID"
            event_dir = str(self._host.output_dir) if event_name else ""
        return {
            "timing_provider": self._host.timing_provider,
            "timing_state": timing_state,
            "timing_detail": timing_detail,
            "cycle_state": timing_state,
            "cycle_detail": timing_detail,
            "camera_state": camera_state,
            "camera_detail": camera_detail,
            "high_speed_state": high_speed_state,
            "high_speed_detail": high_speed_detail,
            "event_state": event_state,
            "event_name": event_name,
            "event_stage": event_stage,
            "event_dir": event_dir,
            "workspace_root": str(self._host.workspace_root),
            "workspace_mode": self._host._workspace_mode,
            "recording_active": "1" if self._host._recording_any_active() else "0",
        }

    def _recheck_connections(self) -> None:
        if self._host.timing_provider == "racetiger":
            source = self._host._racetiger_source
            if source is None or not source.is_running:
                try:
                    self._host.start_receiver()
                except Exception as exc:  # noqa: BLE001 - retry remains operator-visible.
                    QMessageBox.warning(
                        self._host,
                        "赛虎读取未启动",
                        sanitize_recording_message(exc),
                    )
            self._host._high_speed_scan_worker.request_scan()
            self._host._update_runtime_status()
            return
        receiver = self._host._receiver
        if receiver is None or not receiver.is_running:
            try:
                self._host.start_receiver()
            except Exception as exc:  # noqa: BLE001 - retry remains operator-visible.
                QMessageBox.warning(
                    self._host,
                    "CycleRace监听未启动",
                    sanitize_recording_message(exc),
                )
        self._host._request_high_speed_scan()
        self._host._update_runtime_status()

    def _collect_runtime_status(self) -> RuntimeStatusSnapshot:
        self._host._reap_finished_archive_video_scan_workers()
        configured_sources = tuple(self._host._configured_recording_sources())
        recording_active = self._host._recording_any_active()
        segments_by_camera = {
            camera_index: tuple(ring_buffer.status_segments())
            for camera_index, ring_buffer in self._host._ring_buffers.items()
        }
        metadata = (
            self._host.metadata_store.current()
            if self._host.metadata_store is not None
            else None
        )
        high_speed_result = self._host._high_speed_scan_result
        high_speed_root = self._host._high_speed_catalog.root
        storage_free_gb = None
        storage_error = ""
        storage = getattr(self._host, "_storage_snapshot", None)
        if storage is not None and storage[0] == self._host.output_dir:
            _, storage_free_gb, storage_error = storage

        counts = {state: 0 for state in PassageReviewState}
        event_states: dict[str, list[PassageReviewState]] = {}
        for windows in self._host._capture_windows_by_camera.values():
            for event_id, window in windows.items():
                event_states.setdefault(event_id, []).append(window.state)
        for states in event_states.values():
            if PassageReviewState.WAITING in states:
                state = PassageReviewState.WAITING
            elif PassageReviewState.PARTIAL in states:
                state = PassageReviewState.PARTIAL
            else:
                state = PassageReviewState.READY
            counts[state] += 1

        return RuntimeStatusSnapshot(
            output_dir=self._host.output_dir, video_assist_enabled=self._host._video_assist_enabled(),
            beijing_clock_text=datetime.now(
                timezone(timedelta(hours=8))
            ).strftime("%H:%M:%S"),
            epoch_now_ms=int(time.time() * 1000.0),
            configured_sources=configured_sources,
            recording_active=recording_active,
            recording_all_active=self._host._recording_all_active(),
            segments_by_camera=MappingProxyType(segments_by_camera),
            reconnecting_cameras=tuple(sorted(self._host._camera_reconnect_errors)),
            reconnect_errors=MappingProxyType(dict(self._host._camera_reconnect_errors)),
            running_recorder_cameras=frozenset(
                camera_index
                for camera_index, recorder in self._host._recorders.items()
                if recorder.is_running
            ),
            auto_recording_error=self._host._auto_recording_error,
            archive_scan_active=any(
                bool(getattr(worker, "is_running", False))
                for worker in self._host._archive_video_scan_workers.values()
            ),
            anomaly_count=len(self._host.video_reconciliation()),
            archive_candidate_count=(
                len(self._host.video_navigation_candidates())
                if not recording_active
                and any(
                    bool(getattr(worker, "is_running", False))
                    for worker in self._host._archive_video_scan_workers.values()
                )
                else 0
            ),
            visual_failed=self._host._visual_failed,
            visual_status=self._host._visual_status,
            video_scan_active=bool(self._host._video_scan_workers),
            finish_line_count=len(self._host._finish_line_rois),
            visual_detection_enabled=self._host._visual_detection_enabled,
            workspace_mode=self._host._workspace_mode,
            runtime_error=self._host._runtime_error,
            recording_elapsed_seconds=(
                max(0, int(time.monotonic() - self._host._recording_started_at))
                if recording_active
                else 0
            ),
            receiver_running=bool(
                self._host._receiver is not None and self._host._receiver.is_running
            ),
            receiver_metadata=(None if isinstance(self._host.metadata_store, EventWorkspaceMetadataStore)
                               and self._host.metadata_store.recovered else metadata),
            pending_passage_count=len(getattr(self._host, "_pending_passages", ())),
            archive_background_passage_count=self._host._archive_background_passage_count,
            received_passage_count=self._host._received_passage_count,
            historical_passage_count=self._host._historical_passage_count,
            receiver_error=self._host._receiver_error,
            timing_provider=self._host.timing_provider,
            racetiger_running=bool(
                self._host._racetiger_source is not None
                and self._host._racetiger_source.is_running
            ),
            racetiger_status=self._host._racetiger_status,
            racetiger_configured=all(
                (
                    self._host.racetiger_base_url,
                    self._host.racetiger_pc,
                    self._host.racetiger_rid,
                    self._host.racetiger_token,
                )
            ),
            high_speed_result=high_speed_result,
            high_speed_root=high_speed_root,
            high_speed_remote=is_network_share(high_speed_root),
            storage_free_gb=storage_free_gb,
            storage_error=storage_error,
            capture_counts=MappingProxyType(counts),
            aligned_event_count=len(self._host._evidence_timestamp_overrides),
            capture_error=self._host._capture_error,
            available_evidence_count=self._host._available_evidence_count,
            unsupported_event_count=len(self._host._unsupported_event_ids),
            workspace_notice=self._host._workspace_notice,
        )

    def _update_runtime_status(self) -> None:
        self._host._render_runtime_status(self._host._collect_runtime_status())

    def _render_runtime_status(self, snapshot: RuntimeStatusSnapshot) -> None:
        if not hasattr(self._host, "_status_presenter"):
            self._host._status_presenter = RuntimeStatusPresenter(
                {name: getattr(self._host, name) for name in STATUS_WIDGET_NAMES},
            )
        self._host._status_presenter.render(snapshot)
        # The whole row disappears while every device behaves, so the operator
        # never has to read the same four "待机" chips again.
        self._host.runtime_status_strip.setVisible(
            any(
                not chip.isHidden()
                for chip in (
                    self._host.receiver_status_label,
                    self._host.camera_status_label,
                    self._host.high_speed_status_label,
                    self._host.video_assist_status_label,
                )
            )
        )
        self._host._update_event_header()
        self._host._update_operator_controls()
        self._host._sync_recording_reminder()
