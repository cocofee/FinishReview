"""Video assist for the passage review surface: reconciliation, arrival batches, anomaly list, video-found bibs."""

from __future__ import annotations

import logging
from pathlib import Path
import time
from types import SimpleNamespace
from typing import Iterable, Mapping

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QInputDialog,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from .passage_receiver import PassageEvent
from .passage_batch import PassageReviewBatch
from .video_arrival import VideoArrivalBatch, build_video_arrival_batches
from .video_discovery import VideoDiscoveryError
from .video_timeline import PassageVideoLocation
from .passage_review_common import _OPENABLE_STATUSES, format_passage_time
from .passage_evidence_pane import PassageEvidencePane
from .ui_parts import UiPart

logger = logging.getLogger("FinishReview.Review")


class PassageReviewVideoAssistPart(UiPart):
    """Video reconciliation, arrival batch and anomaly lists, and bibs found on video."""

    def set_video_reconciliation(self, items: Iterable[object]) -> None:
        """Publish advisory video anomalies to the current review workspace.

        The detector is intentionally decoupled from the official passage
        table. Callers should pass reconciliation objects whose ``needs_review``
        property identifies the small set requiring attention.
        """
        if not self._host._video_assist_enabled():
            self._host._video_reconciliation_by_id.clear()
            self._host._video_reconciliation = ()
            self._host._set_video_assist_controls_visible(False)
            return

        def candidate_of(value: object):
            return getattr(value, "candidate", None)

        values = tuple(items)

        def is_visual(value: object) -> bool:
            candidate = candidate_of(value)
            return str(getattr(candidate, "candidate_id", "")).startswith("visual:")

        def overlaps(left: object, right: object) -> bool:
            a = candidate_of(left)
            b = candidate_of(right)
            if a is None or b is None:
                return False
            if int(getattr(a, "camera_index", 0)) != int(getattr(b, "camera_index", 0)):
                return False
            return abs(
                int(getattr(a, "peak_at_ms", 0)) - int(getattr(b, "peak_at_ms", 0))
            ) <= 1_500

        for item in values:
            candidate = getattr(item, "candidate", None)
            candidate_id = str(getattr(candidate, "candidate_id", ""))
            if not candidate_id:
                candidate_id = f"video-anomaly-{id(item)}"
            matching_visual_ids = {
                existing_id
                for existing_id, existing in self._host._video_reconciliation_by_id.items()
                if is_visual(existing) and overlaps(existing, item)
            }
            if not bool(getattr(item, "needs_review", False)):
                for existing_id in matching_visual_ids:
                    self._host._video_reconciliation_by_id.pop(existing_id, None)
                continue
            # Prefer the ordinary-video candidate because it contains the
            # recorded file and playback position; the live candidate is only
            # a timestamp hint.
            if not is_visual(item):
                for existing_id in matching_visual_ids:
                    previous_status = self._host._video_review_statuses.pop(existing_id, "pending")
                    previous_bib = self._host._video_review_bibs.pop(existing_id, "")
                    self._host._video_reconciliation_by_id.pop(existing_id, None)
                    self._host._video_review_statuses.setdefault(candidate_id, previous_status)
                    if previous_bib:
                        self._host._video_review_bibs.setdefault(candidate_id, previous_bib)
            self._host._video_reconciliation_by_id[candidate_id] = item
            self._host._video_review_statuses.setdefault(candidate_id, "pending")
        self._host._video_reconciliation = tuple(
            item
            for candidate_id, item in self._host._video_reconciliation_by_id.items()
            if self._host._video_review_statuses.get(candidate_id, "pending") == "pending"
        )
        if hasattr(self._host, "video_assist_status_label"):
            self._host._update_runtime_status()
        pending_count = len(self._host._video_reconciliation)
        total_count = len(self._host._video_reconciliation_by_id)
        self._host.video_review_button.setText(
            f"视频异常：{pending_count}"
            if pending_count
            else f"视频异常记录：{total_count}"
        )
        self._host.video_review_button.setVisible(False)
        if self._host._video_anomaly_dialog_refresh is not None:
            self._host._video_anomaly_dialog_refresh()
        has_active = bool(self._host._active_video_anomaly_id)
        self._host.video_review_done_button.setVisible(has_active)
        self._host.video_review_ignore_button.setVisible(has_active)
        self._host.video_review_bib_edit.setVisible(has_active)
        self._host.video_review_updated.emit(self._host._video_reconciliation)
        if self._host._video_reconciliation:
            self._host.summary_label.setToolTip(
                f"视频辅助发现 {len(self._host._video_reconciliation)} 个异常批次，需重点复核"
            )
        else:
            self._host.summary_label.setToolTip("视频辅助：当前没有待复核异常批次")

    def video_reconciliation(self) -> tuple[object, ...]:
        """Return advisory anomalies currently supplied by the scanner."""
        return self._host._video_reconciliation

    def _video_assist_enabled(self) -> bool:
        return bool(
            getattr(self._host, "_video_assist_enabled_value", self._host.VIDEO_ASSIST_ENABLED)
        )

    def _set_video_assist_controls_visible(self, visible: bool) -> None:
        visible = bool(visible)
        for name in (
            "video_arrival_button",
            "video_review_button",
            "video_review_done_button",
            "video_review_ignore_button",
            "video_review_bib_edit",
            "video_discovered_btn",
            "video_discovered_table",
        ):
            widget = getattr(self._host, name, None)
            if widget is not None:
                widget.setVisible(visible and widget.isVisible() if visible else False)

    def set_video_navigation_candidates(self, candidates: Iterable[object]) -> None:
        """Store visual passage candidates for modifier-key navigation.

        Candidates are advisory only. They never alter ``PassageEventStore``;
        camera-motion detections are excluded because they are not likely
        athlete passages.
        """

        if not self._host._video_assist_enabled():
            self._host._video_navigation_candidates = ()
            self._host._video_arrival_batches = ()
            self._host._set_video_assist_controls_visible(False)
            return

        def candidate_key(value: object) -> tuple[int, int, str]:
            return (
                int(getattr(value, "camera_index", 0)),
                int(getattr(value, "peak_at_ms", 0)),
                str(getattr(value, "candidate_id", "")),
            )

        ordered: list[object] = []
        seen: set[tuple[int, int]] = set()
        for candidate in sorted(tuple(candidates), key=candidate_key):
            if bool(getattr(candidate, "is_camera_motion", False)):
                continue
            key = (
                int(getattr(candidate, "camera_index", 0)),
                int(getattr(candidate, "peak_at_ms", 0)),
            )
            if key in seen:
                continue
            seen.add(key)
            ordered.append(candidate)
            candidate_id = str(getattr(candidate, "candidate_id", "")).strip()
            if candidate_id:
                self._host._video_review_statuses.setdefault(candidate_id, "pending")
        self._host._video_navigation_candidates = tuple(ordered)
        self._host._video_arrival_batches = build_video_arrival_batches(
            self._host._video_navigation_candidates,
            batch_gap_ms=self._host._video_arrival_batch_gap_ms,
            subwave_gap_ms=self._host._video_arrival_subwave_gap_ms,
        )
        if hasattr(self._host, "video_arrival_button"):
            self._host.video_arrival_button.setText(
                f"到达候选：{len(self._host._video_arrival_batches)}批/"
                f"{len(self._host._video_navigation_candidates)}点"
            )
            self._host.video_arrival_button.setVisible(False)
        if self._host._video_arrival_dialog_refresh is not None:
            self._host._video_arrival_dialog_refresh()

    def video_navigation_candidates(self) -> tuple[object, ...]:
        """Return visual candidates used by Ctrl+Left/Right."""

        return self._host._video_navigation_candidates

    def video_arrival_batches(self) -> tuple[VideoArrivalBatch, ...]:
        """Return video-driven review batches independent from chip events."""

        return self._host._video_arrival_batches

    def video_review_status(self, candidate_id: str) -> str:
        return self._host._video_review_statuses.get(str(candidate_id), "pending")

    def restore_video_review_record(
        self,
        candidate_id: str,
        *,
        status: str = "pending",
        bib: str = "",
    ) -> None:
        candidate_id = str(candidate_id).strip()
        if not candidate_id or status not in {"pending", "verified", "ignored"}:
            return
        self._host._video_review_statuses[candidate_id] = status
        if bib:
            self._host._video_review_bibs[candidate_id] = str(bib).strip()
        if self._host._video_arrival_dialog_refresh is not None:
            self._host._video_arrival_dialog_refresh()

    def _mark_current_video_anomaly(self, status: str) -> None:
        candidate_id = self._host._active_video_anomaly_id
        if not candidate_id or status not in {"verified", "ignored"}:
            return
        self._host._video_review_statuses[candidate_id] = status
        self._host._active_video_anomaly_id = ""
        self._host.video_review_bib_edit.clear()
        self._host.video_review_status_changed.emit(
            candidate_id,
            status,
            self._host._video_review_bibs.get(candidate_id, ""),
        )
        self._host.set_video_reconciliation(())
        if self._host._video_arrival_dialog_refresh is not None:
            self._host._video_arrival_dialog_refresh()

    def _open_next_video_anomaly(self) -> None:
        if not self._host._video_reconciliation:
            return
        current = getattr(self._host, "_video_anomaly_cursor", -1)
        current = (current + 1) % len(self._host._video_reconciliation)
        self._host._video_anomaly_cursor = current
        self._host._activate_video_anomaly(self._host._video_reconciliation[current], current)

    def _activate_video_anomaly(self, item: object, index: int | None = None) -> None:
        candidate = getattr(item, "candidate", None)
        candidate_id = str(getattr(candidate, "candidate_id", ""))
        if not candidate_id:
            return
        if index is not None:
            self._host._video_anomaly_cursor = int(index)
        else:
            try:
                self._host._video_anomaly_cursor = self._host._video_reconciliation.index(item)
            except ValueError:
                self._host._video_anomaly_cursor = -1
        self._host._active_video_anomaly_id = candidate_id
        self._host.video_review_done_button.setVisible(True)
        self._host.video_review_ignore_button.setVisible(True)
        self._host.video_review_bib_edit.setText(
            self._host._video_review_bibs.get(candidate_id, "")
        )
        self._host.video_review_bib_edit.setVisible(True)
        candidate = getattr(item, "candidate", None)
        timestamp = getattr(candidate, "peak_at_ms", None)
        if timestamp is None:
            return
        if not self._host._visible_events:
            self._host.refresh()
        if not self._host._visible_events or int(getattr(item, "chip_count", 0)) == 0:
            self._host.video_candidate_requested.emit(item)
            self._host.summary_label.setToolTip(
                "视频异常已发现，已发出独立视频复核请求"
            )
            return
        camera_index = int(getattr(candidate, "camera_index", 1))
        offset = self._host._clock_offset_for_camera(camera_index)
        nearest = min(
            self._host._visible_events,
            key=lambda event: abs(
                event.timeline_timestamp_ms + offset - int(timestamp)
            ),
        )
        self._host._enter_batch_mode(nearest.event_id)
        self._host._select_event(nearest.event_id)
        position = self._host._video_anomaly_cursor + 1
        total = len(self._host._video_reconciliation)
        self._host.summary_label.setToolTip(
            f"视频异常 {position}/{total}："
            f"已定位到最接近的芯片记录（{getattr(item, 'anomaly', '待复核')}）"
        )
        self._host.video_candidate_requested.emit(item)

    @staticmethod
    def _video_review_status_label(status: str) -> str:
        return {
            "pending": "待核实",
            "verified": "已核实",
            "ignored": "已忽略",
        }.get(str(status), "待核实")

    def _video_arrival_candidates_for_batch(
        self,
        batch: VideoArrivalBatch,
    ) -> tuple[object, ...]:
        by_id = {
            str(getattr(candidate, "candidate_id", "")): candidate
            for candidate in self._host._video_navigation_candidates
        }
        return tuple(
            by_id[candidate_id]
            for candidate_id in batch.candidate_ids
            if candidate_id in by_id
        )

    def _video_arrival_batch_chip_count(self, batch: VideoArrivalBatch) -> int:
        offset = self._host._clock_offset_for_camera(batch.camera_index)
        return sum(
            1
            for event in self._host._events_for_current_metadata(
                self._host.passage_store.events()
            )
            if batch.started_at_ms
            <= int(event.timeline_timestamp_ms) + offset
            <= batch.ended_at_ms
        )

    def _video_arrival_batch_progress(
        self,
        batch: VideoArrivalBatch,
    ) -> tuple[int, int]:
        completed = sum(
            self._host._video_review_statuses.get(candidate_id, "pending")
            in {"verified", "ignored"}
            for candidate_id in batch.candidate_ids
        )
        return completed, batch.size

    def _activate_video_arrival_batch(self, batch: VideoArrivalBatch) -> None:
        candidates = self._host._video_arrival_candidates_for_batch(batch)
        if not candidates:
            return
        candidate = next(
            (
                value
                for value in candidates
                if self._host._video_review_statuses.get(
                    str(getattr(value, "candidate_id", "")),
                    "pending",
                )
                == "pending"
            ),
            candidates[0],
        )
        candidate_id = str(getattr(candidate, "candidate_id", ""))
        self._host._active_video_anomaly_id = candidate_id
        self._host.video_review_done_button.setVisible(True)
        self._host.video_review_ignore_button.setVisible(True)
        self._host.video_review_bib_edit.setText(
            self._host._video_review_bibs.get(candidate_id, "")
        )
        self._host.video_review_bib_edit.setVisible(True)
        chip_count = self._host._video_arrival_batch_chip_count(batch)
        item = SimpleNamespace(
            candidate=candidate,
            chip_count=chip_count,
            anomaly=(
                f"到达批次，{batch.size} 个候选点，"
                f"芯片匹配 {chip_count} 条"
            ),
            arrival_batch=batch,
        )
        if chip_count:
            events = self._host._events_for_current_metadata(self._host.passage_store.events())
            offset = self._host._clock_offset_for_camera(batch.camera_index)
            nearest = min(
                events,
                key=lambda event: abs(
                    int(event.timeline_timestamp_ms)
                    + offset
                    - int(getattr(candidate, "peak_at_ms", 0))
                ),
                default=None,
            )
            if nearest is not None:
                self._host._enter_batch_mode(nearest.event_id)
                self._host._select_event(nearest.event_id)
        self._host.video_candidate_requested.emit(item)

    def _open_video_arrival_list(self) -> None:
        if not self._host._video_arrival_batches:
            return
        dialog = self._host._video_arrival_dialog
        if dialog is not None:
            dialog.raise_()
            dialog.activateWindow()
            return

        dialog = QDialog(self._host)
        dialog.setObjectName("videoArrivalDialog")
        dialog.setWindowTitle("视频到达候选时间轴")
        dialog.resize(1040, 560)
        layout = QVBoxLayout(dialog)

        controls = QHBoxLayout()
        pending_only = QCheckBox("只看未完成", dialog)
        pending_only.setChecked(True)
        controls.addWidget(pending_only)
        controls.addWidget(QLabel("机位", dialog))
        camera_filter = QComboBox(dialog)
        camera_filter.setObjectName("videoArrivalCameraFilter")
        camera_filter.addItem("全部机位", 0)
        camera_indexes = sorted(
            {batch.camera_index for batch in self._host._video_arrival_batches}
        )
        for camera_index in camera_indexes:
            camera_filter.addItem(f"机位 {camera_index}", camera_index)
        fallback_camera = camera_indexes[0] if camera_indexes else 0
        active_camera = int(
            getattr(self._host._active_pane, "camera_index", fallback_camera)
        )
        selected_camera_index = camera_filter.findData(active_camera)
        if selected_camera_index >= 0:
            camera_filter.setCurrentIndex(selected_camera_index)
        controls.addWidget(camera_filter)
        controls.addStretch(1)
        controls.addWidget(QLabel("分批间隔", dialog))
        gap_spin = QSpinBox(dialog)
        gap_spin.setObjectName("videoArrivalBatchGapSpin")
        gap_spin.setRange(2, 30)
        gap_spin.setSuffix(" 秒")
        gap_spin.setValue(max(2, self._host._video_arrival_batch_gap_ms // 1_000))
        gap_spin.setToolTip("相邻候选超过该时间后建立新的到达批次")
        controls.addWidget(gap_spin)
        layout.addLayout(controls)

        summary = QLabel(
            "双击批次直接打开第一个未核候选；候选来自视频，不依赖芯片记录。",
            dialog,
        )
        summary.setStyleSheet("color: #667085;")
        layout.addWidget(summary)

        table = QTableWidget(0, 8, dialog)
        table.setObjectName("videoArrivalTable")
        table.setHorizontalHeaderLabels(
            [
                "序号",
                "时间范围",
                "机位",
                "候选点",
                "芯片匹配",
                "类型",
                "子波",
                "审核进度",
            ]
        )
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.SingleSelection)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        table.setColumnWidth(0, 55)
        table.setColumnWidth(1, 280)
        table.setColumnWidth(2, 60)
        table.setColumnWidth(3, 75)
        table.setColumnWidth(4, 85)
        table.setColumnWidth(5, 110)
        table.setColumnWidth(6, 60)
        records: list[VideoArrivalBatch] = []

        def refresh_table() -> None:
            nonlocal records
            selected_id = ""
            current_row = table.currentRow()
            if 0 <= current_row < len(records):
                selected_id = records[current_row].batch_id
            records = [
                batch
                for batch in self._host._video_arrival_batches
                if (
                    int(camera_filter.currentData() or 0) == 0
                    or batch.camera_index == int(camera_filter.currentData())
                )
                and (
                    not pending_only.isChecked()
                    or self._host._video_arrival_batch_progress(batch)[0] < batch.size
                )
            ]
            table.setRowCount(len(records))
            selected_row = -1
            for row, batch in enumerate(records):
                if batch.batch_id == selected_id:
                    selected_row = row
                completed, total = self._host._video_arrival_batch_progress(batch)
                start_text = format_passage_time(batch.started_at_ms)
                end_text = format_passage_time(batch.ended_at_ms)
                time_text = (
                    start_text
                    if start_text == end_text
                    else f"{start_text} ～ {end_text}"
                )
                batch_type = (
                    "大集团候选"
                    if batch.is_large
                    else "多人候选"
                    if batch.size > 1 or batch.contains_group_detection
                    else "单人候选"
                )
                values = (
                    str(row + 1),
                    time_text,
                    str(batch.camera_index),
                    str(batch.size),
                    str(self._host._video_arrival_batch_chip_count(batch)),
                    batch_type,
                    str(len(batch.subwave_breaks)),
                    f"{completed}/{total}",
                )
                for column, value in enumerate(values):
                    table.setItem(row, column, QTableWidgetItem(value))
                table.item(row, 0).setData(Qt.UserRole, batch.batch_id)
            if selected_row >= 0:
                table.selectRow(selected_row)
            summary.setText(
                f"当前 {len(self._host._video_arrival_batches)} 个批次、"
                f"{len(self._host._video_navigation_candidates)} 个候选点；"
                "双击批次直接打开第一个未核候选。"
            )

        refresh_table()
        self._host._video_arrival_dialog_refresh = refresh_table
        layout.addWidget(table, 1)

        buttons = QHBoxLayout()
        open_button = QPushButton("打开选中批次", dialog)
        open_button.setObjectName("videoArrivalOpenButton")
        close_button = QPushButton("关闭", dialog)
        buttons.addStretch(1)
        buttons.addWidget(open_button)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

        def open_selected() -> None:
            row = table.currentRow()
            if not (0 <= row < len(records)):
                return
            self._host._activate_video_arrival_batch(records[row])
            dialog.close()

        def change_gap(value: int) -> None:
            self._host._video_arrival_batch_gap_ms = max(2_000, int(value) * 1_000)
            self._host.set_video_navigation_candidates(
                self._host._video_navigation_candidates
            )

        pending_only.toggled.connect(lambda _checked: refresh_table())
        camera_filter.currentIndexChanged.connect(lambda _index: refresh_table())
        gap_spin.valueChanged.connect(change_gap)
        open_button.clicked.connect(open_selected)
        table.cellDoubleClicked.connect(lambda _row, _column: open_selected())
        close_button.clicked.connect(dialog.close)

        def clear_dialog_reference(_result: int) -> None:
            self._host._video_arrival_dialog = None
            self._host._video_arrival_dialog_refresh = None

        dialog.finished.connect(clear_dialog_reference)
        self._host._video_arrival_dialog = dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _open_video_anomaly_list(self) -> None:
        if not self._host._video_reconciliation_by_id:
            return
        dialog = self._host._video_anomaly_dialog
        if dialog is not None:
            dialog.raise_()
            dialog.activateWindow()
            return
        dialog = QDialog(self._host)
        dialog.setObjectName("videoAnomalyDialog")
        dialog.setWindowTitle("视频辅助异常列表")
        dialog.resize(980, 480)
        layout = QVBoxLayout(dialog)
        summary = QLabel(
            "双击一行或点击“打开选中”进入复核；已核实和已忽略记录会保留。",
            dialog,
        )
        summary.setStyleSheet("color: #667085;")
        layout.addWidget(summary)
        table = QTableWidget(0, 7, dialog)
        table.setObjectName("videoAnomalyTable")
        table.setHorizontalHeaderLabels(
            ["序号", "时间", "机位", "异常原因", "芯片数", "号码", "状态"]
        )
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.SingleSelection)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        table.setColumnWidth(0, 55)
        table.setColumnWidth(1, 170)
        table.setColumnWidth(2, 60)
        table.setColumnWidth(4, 65)
        table.setColumnWidth(5, 90)
        records: list[tuple[str, object]] = []

        def refresh_table() -> None:
            nonlocal records
            selected_id = ""
            current_row = table.currentRow()
            if 0 <= current_row < len(records):
                selected_item = table.item(current_row, 0)
                if selected_item is not None:
                    selected_id = str(selected_item.data(Qt.UserRole) or "")
            records = sorted(
                self._host._video_reconciliation_by_id.items(),
                key=lambda pair: int(
                    getattr(getattr(pair[1], "candidate", None), "peak_at_ms", 0)
                ),
            )
            table.setRowCount(len(records))
            selected_row = -1
            for row, (candidate_id, item) in enumerate(records):
                if candidate_id == selected_id:
                    selected_row = row
                candidate = getattr(item, "candidate", None)
                values = (
                    str(row + 1),
                    format_passage_time(int(getattr(candidate, "peak_at_ms", 0))),
                    str(getattr(candidate, "camera_index", "-")),
                    str(getattr(item, "anomaly", "待核实")),
                    str(getattr(item, "chip_count", 0)),
                    self._host._video_review_bibs.get(candidate_id, ""),
                    self._host._video_review_status_label(
                        self._host._video_review_statuses.get(candidate_id, "pending")
                    ),
                )
                for column, value in enumerate(values):
                    table.setItem(row, column, QTableWidgetItem(value))
                table.item(row, 0).setData(Qt.UserRole, candidate_id)
            if selected_row >= 0:
                table.selectRow(selected_row)

        refresh_table()
        self._host._video_anomaly_dialog_refresh = refresh_table
        layout.addWidget(table, 1)
        buttons = QHBoxLayout()
        open_button = QPushButton("打开选中", dialog)
        open_button.setObjectName("videoAnomalyOpenButton")
        close_button = QPushButton("关闭", dialog)
        buttons.addStretch(1)
        buttons.addWidget(open_button)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

        def open_selected() -> None:
            row = table.currentRow()
            if not (0 <= row < len(records)):
                return
            self._host._activate_video_anomaly(records[row][1])
            dialog.close()

        open_button.clicked.connect(open_selected)
        table.cellDoubleClicked.connect(lambda _row, _column: open_selected())
        close_button.clicked.connect(dialog.close)
        def clear_dialog_reference(_result: int) -> None:
            self._host._video_anomaly_dialog = None
            self._host._video_anomaly_dialog_refresh = None

        dialog.finished.connect(clear_dialog_reference)
        self._host._video_anomaly_dialog = dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _apply_video_review_bib(self) -> None:
        candidate_id = self._host._active_video_anomaly_id
        bib = self._host.video_review_bib_edit.text().strip()
        if not candidate_id or not bib:
            return
        metadata = self._host._current_metadata()
        if metadata is None:
            self._host.summary_label.setToolTip("当前没有运动员名单，无法按号码定位")
            return
        if not self._host.focus_athlete(metadata.race_id, metadata.stage_id, bib=bib):
            self._host.summary_label.setToolTip(f"名单中没有找到号码 {bib}")
            return
        self._host._video_review_bibs[candidate_id] = bib
        self._host.video_review_status_changed.emit(
            candidate_id,
            self._host._video_review_statuses.get(candidate_id, "pending"),
            bib,
        )
        if self._host._video_arrival_dialog_refresh is not None:
            self._host._video_arrival_dialog_refresh()
        self._host.summary_label.setToolTip(f"已按号码 {bib} 定位名单运动员")

    def _reload_video_discoveries(self) -> None:
        self._host._video_discovered_entries = [
            {**record.to_payload(), "entry_id": record.discovery_id}
            for record in self._host.video_discovery_store.records()
        ]
        self._host._active_video_discovered_entry_id = ""

    def _refresh_video_discovered_table(self) -> None:
        table = getattr(self._host, "video_discovered_table", None)
        if table is None:
            return
        batch = self._host._active_review_batch() if self._host._batch_mode else None
        metadata = self._host._current_metadata()
        race_id = metadata.race_id if metadata is not None else ""
        stage_id = metadata.stage_id if metadata is not None else ""
        entries = [
            entry
            for entry in self._host._video_discovered_entries
            if batch is not None
            and (not race_id or entry.get("race_id") == race_id)
            and (not stage_id or entry.get("stage_id") == stage_id)
            and self._host._video_discovery_matches_batch(entry, batch)
        ]
        for entry in entries:
            if entry.get("batch_id") != batch.batch_id:
                entry["batch_id"] = batch.batch_id
                try:
                    self._host.video_discovery_store.update_batch(
                        str(entry.get("entry_id", "")), batch.batch_id
                    )
                except VideoDiscoveryError:
                    logger.exception("failed to migrate video discovery batch")
        table.blockSignals(True)
        table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            values = (
                str(entry.get("bib", "")),
                f"{batch.size}人" if batch is not None else "",
                f"{max(0, int(entry.get('position_ms', 0))) / 1000.0:.3f} s",
                str(entry.get("frame_index", "-")),
                self._host._video_discovery_status_label(str(entry.get("status", ""))),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column in {0, 2, 3}:
                    item.setTextAlignment(Qt.AlignCenter)
                if column == 4:
                    self._host._apply_status_style(item, "待核对")
                item.setData(Qt.UserRole, str(entry.get("entry_id", "")))
                table.setItem(row, column, item)
        table.blockSignals(False)
        table.setVisible(bool(entries) and batch is not None)
        table.resizeColumnsToContents()
        table.setColumnWidth(4, max(180, table.columnWidth(4)))

    @staticmethod
    def _video_discovery_status_label(status: str) -> str:
        return {
            "pending_manual_entry": "视频发现 · 芯片缺失 · 待补录",
            "resolved": "已补录芯片记录",
            "ignored": "已忽略",
        }.get(status, "视频发现 · 芯片缺失 · 待补录")

    def _video_discovery_matches_batch(
        self,
        entry: Mapping[str, object],
        batch: PassageReviewBatch,
    ) -> bool:
        if str(entry.get("batch_id", "")) == batch.batch_id:
            return True
        started_at_ms = int(entry.get("started_at_ms", 0))
        ended_at_ms = int(entry.get("ended_at_ms", started_at_ms))
        return (
            started_at_ms <= batch.ended_at_ms + self._host.REVIEW_BATCH_GAP_MS
            and ended_at_ms >= batch.started_at_ms - self._host.REVIEW_BATCH_GAP_MS
        )

    def _reconcile_video_discoveries(
        self,
        events: tuple[PassageEvent, ...],
    ) -> None:
        for entry in self._host._video_discovered_entries:
            if entry.get("status") not in {"pending_manual_entry", "resolved"}:
                continue
            entry_race_id = str(entry.get("race_id", ""))
            entry_stage_id = str(entry.get("stage_id", ""))
            entry_bib = str(entry.get("bib", "")).strip().casefold()
            started_at_ms = int(entry.get("started_at_ms", 0))
            ended_at_ms = int(entry.get("ended_at_ms", started_at_ms))
            resolved = any(
                event.is_active
                and event.race_id == entry_race_id
                and event.stage_id == entry_stage_id
                and event.bib.strip().casefold() == entry_bib
                and started_at_ms - self._host.REVIEW_BATCH_GAP_MS
                <= event.timeline_timestamp_ms
                <= ended_at_ms + self._host.REVIEW_BATCH_GAP_MS
                for event in events
            )
            status = "resolved" if resolved else "pending_manual_entry"
            if status == entry.get("status"):
                continue
            try:
                self._host.video_discovery_store.update_status(
                    str(entry.get("entry_id", "")), status
                )
            except VideoDiscoveryError:
                logger.exception("failed to update resolved video discovery")
                continue
            entry["status"] = status

    def _add_video_discovered_bib(self) -> None:
        batch = self._host._active_review_batch()
        if not self._host._batch_mode or batch is None:
            return
        pane = self._host._active_pane if self._host._active_pane in self._host.evidence_panes else self._host.regular_pane
        self._host._set_sync_playing(False, seek_final=False)
        position_ms = int(pane._current_position_ms)
        frame_index = int(pane._current_frame_index)
        camera_index = int(pane.camera_index)
        location = pane.location
        if location is not None:
            try:
                location = self._host._persist_filmstrip_location(location)
            except (OSError, RuntimeError, ValueError) as error:
                self._host.summary_label.setToolTip(f"无法保留视频发现原帧：{error}")
                return
            if not location.video_path.is_file():
                self._host.summary_label.setToolTip("原录像文件不可用，无法保存视频发现原帧。")
                return
        context = self._host._current_metadata()
        selected_event = self._host.passage_store.get(self._host._selected_event_id)
        race_id = context.race_id if context is not None else selected_event.race_id if selected_event else ""
        stage_id = context.stage_id if context is not None else selected_event.stage_id if selected_event else ""
        bib, accepted = QInputDialog.getText(
            self._host,
            "添加视频发现号码",
            "输入视频中看到但芯片没有的号码：",
        )
        bib = str(bib).strip()
        if not accepted or not bib:
            return
        if self._host._active_review_batch() != batch or self._host._current_metadata() != context:
            self._host.summary_label.setToolTip("集团或赛事已更新，请核对后重新记录视频发现号码。")
            return
        if any(
            entry.get("batch_id") == batch.batch_id
            and str(entry.get("bib", "")).casefold() == bib.casefold()
            for entry in self._host._video_discovered_entries
        ):
            self._host.summary_label.setToolTip(f"集团中已经记录视频发现号码 {bib}")
            return
        discovery_id = f"{batch.batch_id}:video:{time.time_ns()}"
        try:
            record = self._host.video_discovery_store.add(
                discovery_id=discovery_id,
                race_id=race_id,
                stage_id=stage_id,
                batch_id=batch.batch_id,
                bib=bib,
                camera_index=camera_index,
                frame_index=frame_index,
                position_ms=position_ms,
                started_at_ms=batch.started_at_ms,
                ended_at_ms=batch.ended_at_ms,
                segment_id=location.segment.segment_id if location is not None else "",
                video_path=str(location.video_path.absolute()) if location is not None else "",
                media_locator=location.media_locator if location is not None else "",
                clock_offset_ms=int(location.clock_offset_ms) if location is not None else 0,
            )
        except (VideoDiscoveryError, OSError, ValueError) as error:
            self._host.summary_label.setToolTip(f"无法保存视频发现号码 {bib}: {error}")
            return
        entry = {**record.to_payload(), "entry_id": record.discovery_id}
        self._host._video_discovered_entries.append(entry)
        self._host._refresh_video_discovered_table()
        self._host._focus_video_discovered_entry(entry)
        self._host.summary_label.setToolTip(
            f"已记录视频发现号码 {bib}，请在 CycleRace 中补录后再确认正式成绩"
        )

    def _focus_video_discovered_entry(self, entry: Mapping[str, object]) -> None:
        bib = str(entry.get("bib", "")).strip()
        if not bib:
            return
        pane = self._host._pane_for_camera(int(entry.get("camera_index", 1)))
        if not self._host._restore_video_discovery_frame(entry, pane):
            return
        self._host._active_video_discovered_entry_id = str(entry.get("entry_id", ""))
        self._host.selected_identity_value.setText(bib)
        self._host.athlete_value.setText("视频发现号码")
        self._host.team_value.setText("--")
        self._host.selected_time_value.setText(
            f"视频位置 {max(0, int(entry.get('position_ms', 0))) / 1000.0:.3f} s"
        )
        status_text = self._host._video_discovery_status_label(str(entry.get("status", "")))
        self._host.source_value.setText(status_text)
        self._host.current_passage_label.setText(f"视频发现 {bib}")
        self._host.current_context_label.setText(status_text)
        pane.mark_btn.setEnabled(False)
        pane.video_view.set_marker_mode(False)
        pane.video_view.set_identity_cue(bib, status_text)

    def _restore_video_discovery_frame(self, entry, pane) -> bool:
        segment_id = str(entry.get("segment_id", ""))
        segment = self._host.timeline_store.get_segment(segment_id) if segment_id else None
        current = pane.location
        if segment is None and current is not None and current.segment.segment_id == segment_id:
            segment = current.segment
        if segment is None:
            self._host.summary_label.setToolTip("该记录缺少可用的原录像索引，请按集团时间人工查找；未跳转当前录像。")
            return False
        path = self._host.timeline_store.resolve_video_path(segment)
        if (not path.is_file() or segment.camera_index != int(entry["camera_index"])
                or segment.race_id and segment.race_id != entry["race_id"]):
            self._host.summary_label.setToolTip("该记录的原录像不可用或赛事不匹配，未跳转当前录像。")
            return False
        position = int(entry["position_ms"])
        offset = int(entry.get("clock_offset_ms", 0))
        origin = segment.media_started_at_ms if segment.media_started_at_ms is not None else segment.started_at_ms
        location = PassageVideoLocation(segment, path, position, position, offset,
                                        segment.timing_error_ms, "located", str(entry.get("media_locator", "")))
        event = SimpleNamespace(event_id="", bib=str(entry["bib"]), timeline_timestamp_ms=origin + position - offset)
        self._host._set_sync_playing(False, seek_final=False)
        self._host._deferred_start_timer.stop()
        self._host._deferred_selection_panes.clear()
        self._host._selection_pending_panes.clear()
        self._host._filmstrip_preview_timer.stop()
        self._host._pending_filmstrip_preview_position = None
        self._host._activate_pane(pane, align=False)
        self._host._pause_inactive_panes(pane)
        if not pane.rebind_passage(event, location, None):
            pane.set_passage(event, location, None)
        frame = int(entry.get("frame_index", -1))
        if frame >= 0:
            pane.seek_media_frame(position, frame)
        elif pane._worker is not None:
            pane._worker.seek(position)
        self._host._shared_delta_ms = 0
        self._host._update_shared_time_label()
        return True

    def _on_video_discovered_selected(self) -> None:
        row = self._host.video_discovered_table.currentRow()
        if row < 0:
            return
        item = self._host.video_discovered_table.item(row, 0)
        if item is None:
            return
        entry_id = str(item.data(Qt.UserRole) or "")
        entry = next(
            (
                candidate
                for candidate in self._host._video_discovered_entries
                if str(candidate.get("entry_id", "")) == entry_id
            ),
            None,
        )
        if entry is not None:
            self._host._focus_video_discovered_entry(entry)

    def _focus_video_candidate_in_review(self, candidate: object) -> bool:
        """Use the nearest chip row as the review anchor for a visual arrival."""

        if not self._host._visible_events:
            self._host.refresh()
        if not self._host._visible_events:
            return False
        camera_index = max(1, int(getattr(candidate, "camera_index", 1)))
        offset_ms = self._host._clock_offset_for_camera(camera_index)
        candidate_time_ms = int(getattr(candidate, "peak_at_ms", 0))
        self._host._video_candidate_anchor_time_ms = candidate_time_ms
        nearest = min(
            self._host._visible_events,
            key=lambda event: abs(
                int(event.timeline_timestamp_ms) + int(offset_ms) - candidate_time_ms
            ),
        )
        if nearest.event_id != self._host._selected_event_id:
            self._host._select_event(nearest.event_id)
        self._host._update_filmstrip()
        return True

    def _navigate_video_candidate(
        self,
        direction: int,
        pane: PassageEvidencePane,
    ) -> bool:
        """Seek to the next visual candidate for the active camera.

        A visual candidate is only a navigation hint. The selected passage
        row and official timing data remain unchanged. Candidates in another
        recording segment rebind the same pane to that segment so navigation
        remains continuous across archived files.
        """

        direction = 1 if int(direction) >= 0 else -1
        if pane not in self._host.evidence_panes:
            return False
        candidates = tuple(
            candidate
            for candidate in self._host._video_navigation_candidates
            if int(getattr(candidate, "camera_index", 0)) == int(pane.camera_index)
        )
        location = pane.location
        if not candidates or location is None or getattr(pane, "_worker", None) is None:
            return False

        current_path = str(Path(location.video_path).absolute())
        current_segment_id = str(location.segment.segment_id)
        origin_ms = int(
            location.segment.media_started_at_ms
            if location.segment.media_started_at_ms is not None
            else location.segment.started_at_ms
        )
        current_absolute_ms = origin_ms + int(getattr(pane, "_current_position_ms", 0))
        ordered = tuple(
            sorted(
                candidates,
                key=lambda candidate: (
                    int(getattr(candidate, "peak_at_ms", 0)),
                    str(getattr(candidate, "candidate_id", "")),
                ),
            )
        )
        frame_guard = max(1, pane.frame_duration_ms())
        if direction > 0:
            target = next(
                (
                    candidate
                    for candidate in ordered
                    if int(getattr(candidate, "peak_at_ms", 0))
                    > current_absolute_ms + frame_guard
                ),
                None,
            )
        else:
            target = next(
                (
                    candidate
                    for candidate in reversed(ordered)
                    if int(getattr(candidate, "peak_at_ms", 0))
                    < current_absolute_ms - frame_guard
                ),
                None,
            )
        if target is None:
            return False

        target_segment_id = str(getattr(target, "segment_id", ""))
        target_path_value = str(getattr(target, "video_path", ""))
        target_path = str(Path(target_path_value).absolute()) if target_path_value else ""
        target_position_ms = int(getattr(target, "video_position_ms", 0))
        if (
            target_segment_id
            and target_segment_id != current_segment_id
        ) or (
            target_path
            and target_path != current_path
        ):
            # Rebind the same pane to the target segment without changing the
            # selected passage row. This keeps the operation inside the main
            # review window and still lets the operator bind a roster number
            # at the displayed frame.
            event = self._host.passage_store.get(self._host._selected_event_id)
            if event is None:
                return False
            lookup = self._host.timeline_store.locate_passage(
                int(getattr(target, "peak_at_ms", 0)),
                race_id=event.race_id,
            )
            target_location = next(
                (
                    location
                    for location in lookup.locations
                    if int(location.segment.camera_index) == int(pane.camera_index)
                    and (
                        not target_segment_id
                        or location.segment.segment_id == target_segment_id
                    )
                    and location.status in _OPENABLE_STATUSES
                ),
                None,
            )
            if target_location is None:
                return False
            target_position_ms = int(
                getattr(target, "video_position_ms", 0)
            )
            initial_delta_ms = target_position_ms - int(
                target_location.passage_position_ms
            )
            self._host._activate_pane(pane, align=False)
            pane.set_passage(
                event,
                target_location,
                initial_delta_ms=initial_delta_ms,
            )
            self._host._shared_delta_ms = int(initial_delta_ms)
            self._host._update_shared_time_label()
            return True

        target_delta_ms = target_position_ms - int(pane._target_position_ms)
        self._host._activate_pane(pane, align=False)
        pane.seek_passage_delta(target_delta_ms)
        self._host._shared_delta_ms = target_delta_ms
        self._host._update_shared_time_label()
        return True
