"""One camera's evidence pane: picture, playback worker, timeline and per-pane controls."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Optional

from PyQt5.QtCore import QEvent, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QKeySequence
from PyQt5.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QShortcut,
    QVBoxLayout,
)

from .auyat_rgb import AUYAT_CLOCK_SOURCE, AuyatRgbPlaybackWorker
from .passage_evidence import HIGH_SPEED_SOURCE, REGULAR_SOURCE, PassageEvidenceAssociation
from .passage_receiver import PassageEvent
from .thread_lifecycle import retire_qthread, track_qthread
from .video_playback import TargetTimelineSlider, VideoPlaybackWorker
from .video_timeline import PassageVideoLocation
from .passage_review_common import (
    CTRL_FRAME_STEP,
    SHIFT_FRAME_STEP,
    _CONFIRMABLE_STATUSES,
    _OPENABLE_STATUSES,
    _STATUS_TEXT,
    _association_matches_location,
    format_passage_time,
    location_status_text,
)
from .evidence_image_view import EvidenceImageView


class PassageEvidencePane(QFrame):
    open_requested = pyqtSignal(object, object)
    step_requested = pyqtSignal(int)
    step_boundary_reached = pyqtSignal(int)
    playback_reached_end = pyqtSignal()
    play_requested = pyqtSignal()
    passage_delta_requested = pyqtSignal(int)
    selection_step_requested = pyqtSignal(int)
    position_changed = pyqtSignal(int)
    maximize_requested = pyqtSignal(object)
    marking_requested = pyqtSignal(object)
    confirmation_requested = pyqtSignal(object)
    cancel_requested = pyqtSignal(object)
    delete_requested = pyqtSignal(object)
    scrub_started = pyqtSignal()
    scrub_preview_requested = pyqtSignal(int)
    initial_frame_ready = pyqtSignal(str)
    preview_frame_ready = pyqtSignal(object, int, int)
    detail_requested = pyqtSignal(object)

    MAX_SCRUB_SPAN_MS = 6_000
    SCRUB_PREVIEW_INTERVAL_MS = 80
    FULL_RESOLUTION_IDLE_MS = 200
    MIN_LINKED_DRIFT_TOLERANCE_MS = 40
    LINKED_DRIFT_FRAME_MULTIPLIER = 1
    STATUS_COLORS = {
        "已确认": "#16845b",
        "未确认": "#c0372b",
        "参考": "#667085",
        "录像处理中": "#a56300",
        "未定位": "#a56300",
        "无录像": "#c0372b",
        "文件缺失": "#c0372b",
        "未提供": "#667085",
        "未选择": "#667085",
    }

    def __init__(
        self,
        title: str,
        source_kind: str,
        parent=None,
        *,
        camera_index: int = 0,
        low_resource_mode: bool = False,
    ):
        super().__init__(parent)
        if source_kind not in {REGULAR_SOURCE, HIGH_SPEED_SOURCE}:
            raise ValueError("source_kind must be regular or high_speed")
        self.source_kind = source_kind
        self.camera_index = max(0, int(camera_index))
        self._low_resource_mode = bool(low_resource_mode)
        self._event: Optional[PassageEvent] = None
        self._location: Optional[PassageVideoLocation] = None
        self._lookup_status = ""
        self._association: Optional[PassageEvidenceAssociation] = None
        self._reference_only = False
        self._pending_marker: Optional[tuple[float, float, int, int]] = None
        self._pending_marker_unknown = False
        self._pending_marker_label = ""
        self._marking_enabled = False
        self._identity_marker_enabled = True
        self._identity = ""
        self._worker: Optional[object] = None
        self._idle_prefetch_enabled = False
        self._target_position_ms = 0
        self._playing = False
        self._play_speed = 1.0
        self._duration_ms = 0
        self._fps = 0.0
        self._frame_count = 0
        self._source_width = 0
        self._source_height = 0
        self._current_frame_index = -1
        self._current_position_ms = 0
        self._timeline_dragging = False
        self._last_full_resolution_request = -1
        self._full_resolution_enabled = True
        self._full_resolution_error_frame = -1
        self._workbench_compact = False
        self._workbench_allow_maximize = False
        self._scrub_origin_delta_ms = 0
        self._video_scrubbing = False
        self._pending_scrub_delta_ms: Optional[int] = None
        self._scrub_preview_timer = QTimer(self)
        self._scrub_preview_timer.setSingleShot(True)
        self._scrub_preview_timer.setInterval(self.SCRUB_PREVIEW_INTERVAL_MS)
        self._scrub_preview_timer.timeout.connect(self._flush_scrub_preview)
        self._full_resolution_timer = QTimer(self)
        self._full_resolution_timer.setSingleShot(True)
        self._full_resolution_timer.setInterval(self.FULL_RESOLUTION_IDLE_MS)
        self._full_resolution_timer.timeout.connect(
            self._flush_full_resolution_request
        )

        self.setObjectName("passageEvidencePane")
        self.setFrameShape(QFrame.StyledPanel)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        header = QHBoxLayout()
        self.title_label = QLabel(title)
        self.title_label.setObjectName("evidencePaneTitle")
        self.active_badge = QLabel("当前判读")
        self.active_badge.setObjectName("activeJudgingBadge")
        self.active_badge.hide()
        self.frame_indicator_label = QLabel("当前帧 -- | --:--:--.---")
        self.frame_indicator_label.setObjectName("evidenceFrameIndicator")
        self.frame_indicator_label.setStyleSheet(
            "QLabel#evidenceFrameIndicator {"
            " background: #092333; color: #8feaff;"
            " border: 1px solid #00a9d6; border-radius: 3px;"
            " padding: 2px 7px; font-size: 10pt; font-weight: 700;"
            "}"
        )
        self.frame_indicator_label.setMinimumHeight(30)
        self.frame_indicator_label.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        # 11-12: 已过 N 人 · X 号 in the middle above the picture, read against the
        # timing software's own running count (the operator's words).
        self.passed_label = QLabel("")
        self.passed_label.setObjectName("evidencePassedCount")
        self.passed_label.setAlignment(Qt.AlignCenter)
        self.passed_label.setStyleSheet(
            "QLabel#evidencePassedCount {"
            " background: #0b2b1f; color: #ffffff;"
            " border: 2px solid #1bbf83; border-radius: 3px;"
            " padding: 2px 18px; font-size: 18pt; font-weight: 800;"
            "}"
        )
        self.passed_label.setMinimumHeight(40)
        self.passed_label.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        self.passed_label.hide()
        self.camera_combo = QComboBox(self)
        self.camera_combo.setMinimumWidth(88)
        self.camera_combo.setToolTip("切换普通录像机位")
        self.camera_combo.hide()
        self.status_label = QLabel("未选择")
        self.status_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.status_label.setObjectName("evidencePaneStatus")
        self._set_status_label("未选择")
        self.quality_label = QLabel("")
        self.quality_label.setVisible(self.source_kind == REGULAR_SOURCE and self.camera_index == 1)
        self.quality_label.installEventFilter(self)
        header.addWidget(self.title_label)
        header.addWidget(self.active_badge)
        header.addWidget(self.frame_indicator_label)
        header.addWidget(self.camera_combo)
        header.addStretch()
        header.addWidget(self.passed_label)
        self._header, self._header_strip = header, None
        header.addStretch()
        header.addWidget(self.quality_label)
        header.addWidget(self.status_label)
        layout.addLayout(header)

        self.video_view = EvidenceImageView(self)
        self.video_label = self.video_view
        self.video_view.clear_frame("选择一条通过记录后自动定位")
        self.video_view.zoom_changed.connect(self._on_zoom_changed)
        self.video_view.full_resolution_requested.connect(
            self._on_detail_requested
        )
        self.video_view.maximize_requested.connect(
            self._on_maximize_requested
        )
        self.video_view.marker_position_selected.connect(
            self._on_marker_position_selected
        )
        self.video_view.marker_confirm_requested.connect(
            lambda: self.confirmation_requested.emit(self)
        )
        self.video_view.marker_cancel_requested.connect(
            lambda: self.cancel_requested.emit(self)
        )
        self.video_view.marker_delete_requested.connect(
            lambda: self.delete_requested.emit(self)
        )
        self.video_view.frame_step_requested.connect(self.step_requested.emit)
        self.video_view.passage_step_requested.connect(
            self.selection_step_requested.emit
        )
        self.video_view.scrub_started.connect(self._on_video_scrub_started)
        self.video_view.scrub_delta_requested.connect(
            self._on_video_scrub_delta
        )
        self.video_view.scrub_finished.connect(self._on_video_scrub_finished)
        layout.addWidget(self.video_view, 1)

        self.timeline = TargetTimelineSlider(Qt.Horizontal, self)
        self.timeline.setRange(0, 0)
        self.timeline.setEnabled(False)
        self.timeline.setFocusPolicy(Qt.NoFocus)
        self.timeline.setToolTip("左早右晚；拖动定位，左右方向键逐帧查看")
        self.timeline.sliderPressed.connect(self._on_timeline_pressed)
        self.timeline.sliderMoved.connect(self._on_timeline_moved)
        self.timeline.sliderReleased.connect(self._on_timeline_released)
        layout.addWidget(self.timeline)

        controls = QHBoxLayout()
        controls.setSpacing(6)
        self.controls_layout = controls
        self.previous_frame_btn = QPushButton("|◀")
        self.previous_frame_btn.setToolTip("上一帧")
        self.play_btn = QPushButton("▶")
        self.play_btn.setToolTip("播放或暂停")
        self.next_frame_btn = QPushButton("▶|")
        self.next_frame_btn.setToolTip("下一帧")
        self.time_label = QLabel("--:--:--.---")
        self.time_label.setObjectName("evidencePaneTime")
        self.zoom_out_btn = QPushButton("−")
        self.zoom_out_btn.setToolTip("缩小")
        self.actual_size_btn = QPushButton("100%")
        self.actual_size_btn.setToolTip("按原始像素显示")
        self.fit_btn = QPushButton("适应")
        self.fit_btn.setToolTip("适应当前窗格")
        self.zoom_in_btn = QPushButton("+")
        self.zoom_in_btn.setToolTip("放大")
        self.hd_btn = QPushButton("高清")
        self.hd_btn.setToolTip("暂停并读取当前帧原图；滚轮放大，中键拖动查看号码")
        self.hd_btn.setVisible(self.source_kind == REGULAR_SOURCE and self.camera_index == 1)
        self.maximize_btn = QPushButton("放大")
        self.maximize_btn.setToolTip("放大该机位（双击画面或按 F）")
        self.mark_btn = QPushButton("标线")
        self.mark_btn.setToolTip("在画面中按住左键移动身份判读线")
        self.confirm_btn = QPushButton("确认")
        self.confirm_btn.setToolTip("保存当前帧和标线位置为人工判罚结果")
        self.confirm_btn.setEnabled(False)
        self.open_btn = QPushButton("定点回放")
        self.open_btn.setToolTip("回看当前目标点前 45 秒、后 15 秒")
        self.open_btn.setVisible(self.source_kind == REGULAR_SOURCE)
        self.open_btn.setEnabled(False)
        controls.addWidget(self.previous_frame_btn)
        controls.addWidget(self.play_btn)
        controls.addWidget(self.next_frame_btn)
        controls.addWidget(self.time_label)
        controls.addStretch()
        controls.addWidget(self.zoom_out_btn)
        controls.addWidget(self.actual_size_btn)
        controls.addWidget(self.fit_btn)
        controls.addWidget(self.zoom_in_btn)
        controls.addWidget(self.hd_btn)
        controls.addWidget(self.maximize_btn)
        controls.addWidget(self.mark_btn)
        controls.addWidget(self.confirm_btn)
        controls.addWidget(self.open_btn)
        layout.addLayout(controls)

        self.previous_frame_btn.clicked.connect(lambda: self.step_requested.emit(-1))
        self.play_btn.clicked.connect(self.play_requested.emit)
        self.next_frame_btn.clicked.connect(lambda: self.step_requested.emit(1))
        self.zoom_out_btn.clicked.connect(lambda: self.video_view.zoom_by(1.0 / 1.2))
        self.actual_size_btn.clicked.connect(self.video_view.set_actual_size)
        self.fit_btn.clicked.connect(self._on_fit_requested)
        self.zoom_in_btn.clicked.connect(lambda: self.video_view.zoom_by(1.2))
        self.hd_btn.clicked.connect(self._on_detail_requested)
        self.maximize_btn.clicked.connect(self._on_maximize_button)
        self._maximize_shortcut = QShortcut(QKeySequence(Qt.Key_F), self)
        self._maximize_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self._maximize_shortcut.setAutoRepeat(False)
        self._maximize_shortcut.activated.connect(self._on_maximize_requested)
        self.mark_btn.clicked.connect(lambda: self.marking_requested.emit(self))
        self.confirm_btn.clicked.connect(
            lambda: self.confirmation_requested.emit(self)
        )
        self.open_btn.clicked.connect(self._request_open)
        self._frame_step_shortcuts = []
        for sequence, frame_delta in (
            ("Left", -1),
            ("Right", 1),
            ("Shift+Left", -SHIFT_FRAME_STEP),
            ("Shift+Right", SHIFT_FRAME_STEP),
            ("Ctrl+Left", -CTRL_FRAME_STEP),
            ("Ctrl+Right", CTRL_FRAME_STEP),
        ):
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.setContext(Qt.WidgetWithChildrenShortcut)
            shortcut.setAutoRepeat(False)
            shortcut.activated.connect(
                lambda delta=frame_delta: self.step_requested.emit(delta)
            )
            self._frame_step_shortcuts.append(shortcut)
        self._set_transport_enabled(False)


    def set_passed_count(self, text: str) -> None:
        """已过 N 人 · X 号 in the middle of the header; empty hides it."""
        value = str(text or "").strip()
        if value != self.passed_label.text():
            self.passed_label.setText(value)
        self.passed_label.setVisible(bool(value) and self._header_strip is None)

    def set_header_strip(self, widget) -> None:
        """WIDGET takes the middle of the header, in the place of 已过 N 人 (11-14: the cells of
        who is counted say the same and more, right above the picture); None gives it back."""
        held = self._header_strip is not None and self._header.indexOf(self._header_strip) >= 0
        if widget is self._header_strip and (widget is None or held):
            return
        if held:
            self._header.removeWidget(self._header_strip)
        self._header_strip = widget
        if widget is not None:
            self._header.insertWidget(self._header.indexOf(self.passed_label), widget, 1)
            self.passed_label.hide()
        # The cells need the whole line (11-14: the camera name and the frame block took a
        # third of it); the time shown stays under the picture, beside the frame buttons.
        self.title_label.setVisible(widget is None)
        self.frame_indicator_label.setVisible(widget is None)
        self.quality_label.setVisible(widget is None and self.source_kind == REGULAR_SOURCE
                                      and self.camera_index == 1)

    def set_active_judging(self, active: bool) -> None:
        active = bool(active)
        if self.property("activeJudging") == active:
            return
        self.setProperty("activeJudging", active)
        self.active_badge.setVisible(active)
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def set_compact_controls(self, compact: bool) -> None:
        compact = bool(compact)
        for button in (
            self.previous_frame_btn,
            self.play_btn,
            self.next_frame_btn,
            self.zoom_out_btn,
            self.actual_size_btn,
            self.zoom_in_btn,
        ):
            button.setVisible(not compact)
        self.open_btn.setText("回放" if compact else "定点回放")
        self.controls_layout.setSpacing(4 if compact else 6)

    def set_workbench_compact(
        self, enabled: bool, *, allow_maximize: bool = False
    ) -> None:
        """Strip the workbench picture down to one click and one reset button.

        Rare controls stay in ordinary review: manual zoom, 100%, HD, the
        single-camera enlarge button and short-clip playback.  The fit button
        remains as "reset picture"; the underlying maximize behaviour and
        automatic paused-frame original reads are preserved.
        """

        self._workbench_compact = bool(enabled)
        self._workbench_allow_maximize = bool(allow_maximize)
        compact = self._workbench_compact
        # 11-14: the picture was letterboxed by height.  The position slider had a
        # line of its own under it; in the workbench it shares the buttons' line.
        if compact != (self.controls_layout.indexOf(self.timeline) >= 0):
            if compact:
                self.layout().removeWidget(self.timeline)
                self.controls_layout.insertWidget(self.controls_layout.indexOf(self.time_label) + 1,
                                                  self.timeline, 1)
            else:
                self.controls_layout.removeWidget(self.timeline)
                self.layout().insertWidget(self.layout().indexOf(self.video_view) + 1, self.timeline)
        self.zoom_out_btn.setVisible(not compact)
        self.actual_size_btn.setVisible(not compact)
        self.zoom_in_btn.setVisible(not compact)
        self.hd_btn.setVisible(
            not compact
            and self.source_kind == REGULAR_SOURCE
            and self.camera_index == 1
        )
        self.open_btn.setVisible(not compact and self.source_kind == REGULAR_SOURCE)
        self.maximize_btn.setVisible(not compact or self._workbench_allow_maximize)
        self.fit_btn.setText("复位画面" if compact else "适应")
        self.fit_btn.setToolTip(
            "复位画面；原图读取失败时重试当前帧"
            if compact
            else "适应当前窗格"
        )
        # F and double-click are duplicate ways into a second enlarged copy.
        # The workbench already owns the screen, so both are closed there.
        self._maximize_shortcut.setEnabled(not compact)
        self.video_view.set_workbench_compact(compact)
        if compact:
            # Workbench pauses are for reading the number: fetch the original
            # automatically instead of adding a permanent HD button.
            self.set_full_resolution_enabled(True)

    def _on_fit_requested(self) -> None:
        self.video_view.fit_to_window()
        self._retry_paused_frame_detail()

    def _on_maximize_requested(self) -> None:
        if self._workbench_compact:
            return
        self.maximize_requested.emit(self)

    def _on_maximize_button(self) -> None:
        if self._workbench_compact and not self._workbench_allow_maximize:
            return
        self.maximize_requested.emit(self)

    def _retry_paused_frame_detail(self) -> bool:
        """Re-read the original of the failed frame, never a newer one."""

        frame_index = self._current_frame_index
        if (
            not self._workbench_compact
            or not self._full_resolution_enabled
            or frame_index < 0
            or self._full_resolution_error_frame != frame_index
        ):
            return False
        self._full_resolution_error_frame = -1
        self._last_full_resolution_request = -1
        self._request_full_resolution()
        return True

    def eventFilter(self, watched, event) -> bool:
        if (
            watched is self.quality_label
            and event.type() == QEvent.MouseButtonRelease
            and event.button() == Qt.LeftButton
            and self._retry_paused_frame_detail()
        ):
            return True
        return super().eventFilter(watched, event)

    @property
    def location(self) -> Optional[PassageVideoLocation]:
        return self._location

    @property
    def association(self) -> Optional[PassageEvidenceAssociation]:
        return self._association

    @property
    def has_pending_marker(self) -> bool:
        return self._pending_marker is not None

    @property
    def is_auyat_rgb(self) -> bool:
        return bool(
            self._location is not None
            and self._location.segment.clock_source == AUYAT_CLOCK_SOURCE
        )

    def matches_passage_context(
        self,
        event: PassageEvent,
        location: Optional[PassageVideoLocation],
    ) -> bool:
        return (
            self._event is not None
            and self._event.event_id == event.event_id
            and self._location == location
        )

    def begin_marking(self) -> None:
        if (
            self._worker is None
            or not self.video_view.has_frame
            or self._location is None
            or self._location.status not in _CONFIRMABLE_STATUSES
        ):
            return
        self._marking_enabled = True
        self._playing = False
        self.play_btn.setText("▶")
        self._worker.pause()
        self.mark_btn.setText("拖动标线")
        self.confirm_btn.setEnabled(False)
        self.video_view.set_marker_mode(True)
        self.video_view.setFocus(Qt.ShortcutFocusReason)

    def cancel_marker_edit(self) -> None:
        self._pending_marker = None
        self._pending_marker_unknown = False
        self._pending_marker_label = ""
        self._marking_enabled = (
            self._association is None
            and self._location is not None
            and self._location.status in _CONFIRMABLE_STATUSES
        )
        self.mark_btn.setText("重标" if self._association is not None else "标线")
        self.confirm_btn.setEnabled(False)
        self.video_view.set_marker_mode(
            self.video_view.has_frame and self._marking_enabled
        )
        self._render_marker()

    def set_association(
        self,
        association: Optional[PassageEvidenceAssociation],
    ) -> None:
        self._association = association
        if association is not None:
            self._reference_only = False
        self._pending_marker = None
        self._pending_marker_unknown = False
        self._pending_marker_label = ""
        self._marking_enabled = (
            association is None
            and self._location is not None
            and self._location.status in _CONFIRMABLE_STATUSES
        )
        self.video_view.set_marker_mode(
            self.video_view.has_frame and self._marking_enabled
        )
        self.mark_btn.setText("重标" if association is not None else "标线")
        self.confirm_btn.setEnabled(False)
        self._update_status_label()
        self._render_marker()

    def set_reference_only(self, reference_only: bool) -> None:
        next_value = bool(reference_only) and self._association is None
        if next_value == self._reference_only:
            return
        self._reference_only = next_value
        self._update_status_label()
        self._render_marker()

    def pending_confirmation(self) -> Optional[dict[str, object]]:
        location = self._location
        marker = self._pending_marker
        if (
            location is None
            or location.status not in _CONFIRMABLE_STATUSES
            or marker is None
        ):
            return None
        x_normalized, y_normalized, frame_index, position_ms = marker
        return {
            "segment_id": location.segment.segment_id,
            "frame_index": frame_index,
            "position_ms": position_ms,
            "marker_x_normalized": x_normalized,
            "marker_y_normalized": y_normalized,
            "unknown_marker": self._pending_marker_unknown,
            "marker_label": self._pending_marker_label or self._identity,
        }

    def set_external_pending_marker(
        self,
        *,
        frame_index: int,
        position_ms: int,
        marker_x_normalized: float,
        marker_y_normalized: float,
        unknown_marker: bool = False,
        marker_label: str = "",
    ) -> None:
        """Accept a marker selected on the primary time-film interface."""

        self._pending_marker = (
            max(0.0, min(1.0, float(marker_x_normalized))),
            max(0.0, min(1.0, float(marker_y_normalized))),
            int(frame_index),
            int(position_ms),
        )
        self._pending_marker_unknown = bool(unknown_marker)
        self._pending_marker_label = str(marker_label).strip()
        self._marking_enabled = True
        self.confirm_btn.setEnabled(True)
        self._render_marker()

    def _on_marker_position_selected(
        self,
        x_normalized: float,
        y_normalized: float,
    ) -> None:
        if self._current_frame_index < 0:
            return
        self.marking_requested.emit(self)
        frame_index = self._current_frame_index
        position_ms = self._current_position_ms
        worker = self._worker
        if self.is_auyat_rgb and worker is not None:
            mapped_position = worker.position_ms_for_x(x_normalized)
            if mapped_position is not None:
                position_ms = int(mapped_position)
                frame_index = max(
                    0,
                    min(
                        self._source_width - 1,
                        int(round(float(x_normalized) * (self._source_width - 1))),
                    ),
                )
                self.passage_delta_requested.emit(
                    position_ms - self._target_position_ms
                )
        self._pending_marker = (
            float(x_normalized),
            float(y_normalized),
            frame_index,
            position_ms,
        )
        normalized_identity = self._identity.strip().casefold()
        self._pending_marker_unknown = normalized_identity in {"", "unknown", "未知", "待补录"}
        self._pending_marker_label = "待补录" if self._pending_marker_unknown else ""
        self.confirm_btn.setEnabled(True)
        self._render_marker()

    def _update_status_label(self) -> None:
        status = self._availability_status()
        detail = ""
        if self._location is not None:
            detail = location_status_text(self._location)
        if not detail and self._lookup_status:
            detail = _STATUS_TEXT.get(self._lookup_status, self._lookup_status)
        if self._association is not None:
            detail = f"{detail} · 已标记 {self._identity}" if detail else "已标记"
        self._set_status_label(status, detail)

    def _availability_status(self) -> str:
        if self._association is not None:
            return "已确认"
        location = self._location
        if (
            self._reference_only
            and location is not None
            and location.status in _OPENABLE_STATUSES
        ):
            return "参考"
        if location is not None and location.status == "preview":
            return "录像处理中"
        if location is not None and location.status in _OPENABLE_STATUSES:
            return "未确认"
        if location is not None and location.status == "recording":
            return "录像处理中"
        if location is not None and location.status == "missing_file":
            return "文件缺失"
        if self.source_kind == HIGH_SPEED_SOURCE and location is None:
            return "未提供"
        if self._lookup_status == "no_segments":
            return "无录像"
        return "未定位"

    def _set_status_label(self, status: str, tooltip: str = "") -> None:
        color = self.STATUS_COLORS.get(status, "#667085")
        self.status_label.setText(status)
        self.status_label.setToolTip(tooltip)
        self.status_label.setStyleSheet(
            f"color: {color}; font-size: 9pt; font-weight: 700;"
        )

    def _empty_message(
        self,
        location: Optional[PassageVideoLocation],
    ) -> str:
        source = "普通录像" if self.source_kind == REGULAR_SOURCE else "高速画面"
        if location is not None and location.status == "recording":
            return f"{source}正在录制，等待片段封口"
        if location is not None and location.status == "missing_file":
            return f"{source}文件缺失"
        if self.source_kind == HIGH_SPEED_SOURCE and location is None:
            return "暂无高速画面"
        if self._lookup_status == "no_segments":
            return "当前赛事没有普通录像"
        if self._lookup_status in {
            "before_recording",
            "after_recording",
            "recording_gap",
            "race_mismatch",
        } or (location is not None and location.status == "outside_media"):
            return "未定位到对应普通录像"
        return f"暂无{source}"

    def _locating_message(self) -> str:
        source = "普通录像" if self.source_kind == REGULAR_SOURCE else "高速画面"
        return f"正在定位{source}"

    def _render_marker(self) -> None:
        if not self._identity_marker_enabled:
            self.video_view.clear_marker()
            return
        marker = self._pending_marker
        if self.is_auyat_rgb:
            self.video_view.clear_identity_cue()
        else:
            if self._association is not None:
                cue_status = "已确认"
            elif self._reference_only:
                cue_status = "参考"
            else:
                cue_status = "未确认"
            self.video_view.set_identity_cue(self._identity, cue_status)
        if marker is not None:
            self.video_view.set_marker(
                marker[0],
                marker[1],
                self._identity,
                confirmed=False,
                simple=self.is_auyat_rgb,
            )
            return
        association = self._association
        if (
            association is not None
            and self._association_is_visible(association)
        ):
            self.video_view.set_marker(
                association.marker_x_normalized,
                association.marker_y_normalized,
                self._identity,
                confirmed=True,
                simple=self.is_auyat_rgb,
            )
            return
        if self.is_auyat_rgb and self._current_frame_index >= 0:
            worker = self._worker
            marker_x = (
                worker.x_for_position_ms(self._current_position_ms)
                if worker is not None
                else None
            )
            if marker_x is not None:
                self.video_view.set_marker(
                    marker_x,
                    0.5,
                    self._identity,
                    confirmed=False,
                    simple=True,
                )
                return
        self.video_view.clear_marker()

    def set_identity_marker_enabled(self, enabled: bool) -> None:
        self._identity_marker_enabled = bool(enabled)
        if not self._identity_marker_enabled:
            self.video_view.clear_marker()

    def _association_is_visible(
        self,
        association: PassageEvidenceAssociation,
    ) -> bool:
        if self._current_frame_index < 0:
            return False
        if self._current_frame_index == association.frame_index:
            return True
        return (
            abs(self._current_position_ms - association.position_ms)
            <= self.frame_duration_ms()
        )

    def set_passage(
        self,
        event: PassageEvent,
        location: Optional[PassageVideoLocation],
        association: Optional[PassageEvidenceAssociation] = None,
        *,
        initial_delta_ms: int = 0,
        lookup_status: str = "",
        defer_worker_start: bool = False,
    ) -> None:
        previous_context = self._media_context(self._location)
        previous_event_id = self._event.event_id if self._event is not None else ""
        next_context = self._media_context(location)
        same_context = bool(previous_context) and previous_context == next_context
        self._event = event
        self._location = location
        self._lookup_status = str(
            lookup_status or (location.status if location is not None else "")
        )
        self._identity = event.bib.strip() or "未知"
        if association is not None and not _association_matches_location(
            association, location
        ):
            association = None
        self._association = association
        self._pending_marker = None
        self._pending_marker_unknown = False
        self._pending_marker_label = ""
        self._marking_enabled = (
            association is None
            and location is not None
            and location.status in _CONFIRMABLE_STATUSES
        )
        self._playing = False
        self._current_frame_index = -1
        self._current_position_ms = 0
        self._last_full_resolution_request = -1
        self.quality_label.clear()
        self.timeline.setProperty("initial_frame_index", None)
        self.timeline.setProperty("initial_adjacent_frame", None)
        self._full_resolution_timer.stop()
        self._reset_video_scrub()
        self.play_btn.setText("▶")
        self.mark_btn.setText("重标" if association is not None else "标线")
        self.video_view.set_marker_mode(False)
        self.video_view.clear_marker()
        if not same_context or previous_event_id != event.event_id:
            self.video_view.reset_view()
        if (
            location is None
            or location.status not in _OPENABLE_STATUSES
            or not location.video_path.is_file()
        ):
            self._stop_worker()
            self._duration_ms = 0
            self._fps = 0.0
            self._source_width = 0
            self._source_height = 0
            self.timeline.setRange(0, 0)
            self.timeline.setEnabled(False)
            self._target_position_ms = 0
            detail = location_status_text(location) if location is not None else ""
            if not detail and self._lookup_status:
                detail = _STATUS_TEXT.get(self._lookup_status, self._lookup_status)
            self._set_status_label(self._availability_status(), detail)
            self.video_label.clear_frame(self._empty_message(location))
            self.time_label.setText("--:--:--.---")
            self.frame_indicator_label.setText("当前帧 -- | --:--:--.---")
            self.video_view.clear_frame_indicator()
            self._set_transport_enabled(False)
            return

        self._target_position_ms = int(location.passage_position_ms)
        self._update_status_label()
        if not same_context:
            self.video_label.clear_frame(self._locating_message())
        self.time_label.setText(f"目标 {self._target_position_ms / 1000.0:.3f} s")
        if association is not None:
            # Preserve a previously confirmed frame instead of moving it back
            # to the generic preview lead-in.
            initial_position_ms = max(0, int(association.position_ms))
        elif str(getattr(location, "media_locator", "")):
            # A persisted clip binding already has an operator-selected origin.
            initial_position_ms = max(
                0,
                self._target_position_ms + int(initial_delta_ms),
            )
        elif self.source_kind == REGULAR_SOURCE:
            # Start before the passage so a rider who has already crossed the
            # line at the target frame remains visible for manual review.
            initial_position_ms = max(
                0,
                int(location.playback_position_ms) + int(initial_delta_ms),
            )
        else:
            initial_position_ms = max(
                0,
                self._target_position_ms + int(initial_delta_ms),
            )

        worker = self._worker
        if (
            worker is not None
            and worker.isRunning()
            and worker.video_path == Path(location.video_path)
            and str(getattr(worker, "media_locator", ""))
            == str(location.media_locator)
        ):
            worker.pause()
            self.timeline.set_target_position(self._target_position_ms)
            self.timeline.setValue(min(initial_position_ms, self._duration_ms))
            self.timeline.setEnabled(self._duration_ms > 0)
            worker.seek(min(initial_position_ms, self._duration_ms))
            self._set_transport_enabled(True)
            return

        self._stop_worker()
        self._duration_ms = 0
        self._fps = 0.0
        self._source_width = 0
        self._source_height = 0
        self.timeline.setRange(0, 0)
        self.timeline.setEnabled(False)
        self.timeline.setProperty("initial_position_ms", int(initial_position_ms))
        if location.segment.clock_source == AUYAT_CLOCK_SOURCE:
            worker = AuyatRgbPlaybackWorker(location, self)
        else:
            try:
                worker = VideoPlaybackWorker(
                    location.video_path,
                    self,
                    # Continuous reverse playback uses one bounded lookahead;
                    # paused single-frame review still avoids idle decoding.
                    reverse_prefetch=True,
                    idle_prefetch=not self._low_resource_mode,
                )
            except TypeError:
                # Keep lightweight test doubles and downstream integrations
                # compatible with the historical two-argument constructor.
                worker = VideoPlaybackWorker(location.video_path, self)
                if self._low_resource_mode:
                    if hasattr(worker, "_idle_prefetch_enabled"):
                        worker._idle_prefetch_enabled = False
        worker.media_locator = str(location.media_locator)
        set_presentation_ack = getattr(worker, "set_presentation_ack_enabled", None)
        if callable(set_presentation_ack):
            set_presentation_ack(True)
        set_idle_prefetch = getattr(worker, "set_idle_prefetch_enabled", None)
        if callable(set_idle_prefetch):
            set_idle_prefetch(self._idle_prefetch_enabled)
        worker.pause()
        worker.metadata_ready.connect(self._on_metadata_ready)
        worker.frame_ready.connect(self._on_frame_ready)
        worker.full_resolution_ready.connect(self._on_full_resolution_ready)
        if hasattr(worker, "full_resolution_error"):
            worker.full_resolution_error.connect(self._on_full_resolution_error)
        worker.playback_finished.connect(self._on_playback_finished)
        if hasattr(worker, "step_boundary_reached"):
            worker.step_boundary_reached.connect(self._on_step_boundary_reached)
        if hasattr(worker, "step_failed"):
            worker.step_failed.connect(self._on_step_failed)
        worker.playback_error.connect(self._on_playback_error)
        self._worker = worker
        if not defer_worker_start:
            track_qthread(worker)
            worker.start()

    def rebind_passage(
        self,
        event: PassageEvent,
        location: Optional[PassageVideoLocation],
        association: Optional[PassageEvidenceAssociation] = None,
        *,
        lookup_status: str = "",
    ) -> bool:
        """Change the athlete identity while keeping the current video frame."""

        if self._media_context(location) != self._media_context(self._location):
            return False
        if location is None or location.status not in _OPENABLE_STATUSES:
            return False
        if (
            association is not None
            and association.segment_id != location.segment.segment_id
        ):
            association = None
        self._event = event
        self._location = location
        self._target_position_ms = int(location.passage_position_ms)
        self.timeline.set_target_position(self._target_position_ms)
        if self._duration_ms > 0:
            self.timeline.setValue(
                max(0, min(self._current_position_ms, self._duration_ms))
            )
        self._lookup_status = str(lookup_status or location.status)
        self._identity = event.bib.strip() or "未知"
        # The playback frame belongs to the recording, while markers belong to
        # one passage. Rebinding an athlete must therefore discard the prior
        # athlete's marker/confirmation state even though the frame is kept.
        self._association = association
        self._pending_marker = None
        self._pending_marker_unknown = False
        self._pending_marker_label = ""
        self._marking_enabled = association is None
        self._reference_only = False
        self.mark_btn.setText("重标" if association is not None else "标线")
        self.video_view.set_marker_mode(
            self.video_view.has_frame and self._marking_enabled
        )
        self._update_status_label()
        self._render_marker()
        self._update_time_label(self._current_position_ms)
        self._update_frame_indicator(self._current_position_ms)
        return True

    @property
    def has_playback_worker(self) -> bool:
        return self._worker is not None

    @property
    def worker_start_deferred(self) -> bool:
        worker = self._worker
        return worker is not None and not worker.isRunning()

    def start_deferred_worker(self) -> bool:
        worker = self._worker
        if worker is None or worker.isRunning():
            return False
        track_qthread(worker)
        worker.start()
        return True

    @staticmethod
    def _media_context(
        location: Optional[PassageVideoLocation],
    ) -> tuple[str, str]:
        if location is None:
            return ("", "")
        return (
            str(Path(location.video_path).absolute()),
            str(location.media_locator or location.segment.segment_id),
        )

    def clear_passage(self, message: str = "没有通过记录") -> None:
        self._stop_worker()
        self.quality_label.clear()
        self._event = None
        self._location = None
        self._lookup_status = ""
        self._association = None
        self._reference_only = False
        self._pending_marker = None
        self._pending_marker_unknown = False
        self._pending_marker_label = ""
        self._marking_enabled = False
        self._identity = ""
        self._target_position_ms = 0
        self._playing = False
        self._duration_ms = 0
        self._fps = 0.0
        self._source_width = 0
        self._source_height = 0
        self._current_frame_index = -1
        self._current_position_ms = 0
        self.timeline.setRange(0, 0)
        self.timeline.setEnabled(False)
        self.play_btn.setText("▶")
        self.mark_btn.setText("标线")
        self._set_status_label("未选择")
        self.time_label.setText("--:--:--.---")
        self.video_view.set_marker_mode(False)
        self.video_view.clear_marker()
        self.video_label.clear_frame(message)
        self.frame_indicator_label.setText("当前帧 -- | --:--:--.---")
        self.video_view.clear_frame_indicator()
        self._set_transport_enabled(False)

    def _on_metadata_ready(
        self,
        duration_ms: int,
        fps: float,
        width: int,
        height: int,
        _frame_count: int,
    ) -> None:
        if self.sender() is not self._worker:
            return
        self._duration_ms = max(0, int(duration_ms))
        self._fps = max(0.0, float(fps))
        self._frame_count = max(0, int(_frame_count))
        self._source_width = max(0, int(width))
        self._source_height = max(0, int(height))
        initial_position_ms = int(self.timeline.property("initial_position_ms") or 0)
        target_ms = min(max(0, initial_position_ms), max(0, duration_ms))
        self.timeline.setRange(0, self._duration_ms)
        self.timeline.set_target_position(self._target_position_ms)
        self.timeline.setValue(target_ms)
        self.timeline.setEnabled(self._duration_ms > 0)
        self._set_transport_enabled(True)
        initial_frame = self.timeline.property("initial_frame_index")
        self.timeline.setProperty("initial_frame_index", None)
        adjacent_frame = self.timeline.property("initial_adjacent_frame")
        self.timeline.setProperty("initial_adjacent_frame", None)
        if adjacent_frame is not None:
            # EOF handoff may already have requested forward play. The deferred
            # FPS-aware seek pauses for positioning, not to cancel that request.
            resume_playing = self._playing
            self.seek_adjacent_frame(*adjacent_frame)
            if resume_playing:
                self.set_playing(True)
        elif initial_frame is not None and hasattr(self._worker, "seek_frame"):
            self._worker.seek_frame(int(initial_frame))
        else:
            self._worker.seek(target_ms)

    def seek_media_frame(self, position_ms: int, frame_index: Optional[int]) -> None:
        """Seek a retained original frame without millisecond rounding loss.

        ``frame_index=None`` asks for a time-only seek: the pane goes to
        ``position_ms`` and never invents a frame number, whether the metadata
        has already arrived or is still on its way.
        """
        worker = self._worker
        if worker is None:
            return
        self.set_playing(False)
        self._full_resolution_timer.stop()
        self._last_full_resolution_request = -1
        self._reset_video_scrub()
        if self._fps <= 0:
            self.timeline.setProperty("initial_position_ms", int(position_ms))
            self.timeline.setProperty(
                "initial_frame_index", None if frame_index is None else int(frame_index)
            )
        elif frame_index is not None and hasattr(worker, "seek_frame"):
            worker.seek_frame(int(frame_index))
        else:
            worker.seek(int(position_ms))

    def seek_adjacent_frame(self, boundary_position_ms: int, direction: int) -> None:
        """Select the first frame strictly beyond a seam in the new file's FPS."""
        worker = self._worker
        if worker is None:
            return
        self.set_playing(False)
        if self._fps <= 0:
            self.timeline.setProperty("initial_adjacent_frame", (boundary_position_ms, direction))
            return
        boundary_frame = boundary_position_ms * self._fps / 1000.0
        frame = (
            math.ceil((boundary_position_ms + 1) * self._fps / 1000.0)
            if direction > 0 else math.ceil(boundary_frame) - 1
        )
        frame = max(0, min(frame, self._frame_count - 1)) if self._frame_count else max(0, frame)
        if hasattr(worker, "seek_frame"):
            worker.seek_frame(frame)
        else:
            worker.seek(int(math.ceil(frame * 1000.0 / self._fps)))

    def _on_frame_ready(self, image, position_ms: int, frame_index: int) -> None:
        if self.sender() is not self._worker:
            return
        worker = self._worker
        previous_frame_index = self._current_frame_index
        self._current_frame_index = int(frame_index)
        self._current_position_ms = int(position_ms)
        # A cached preview can replace a previously refined copy of this frame.
        # Let the next idle request restore the original pixels in that case too.
        self._last_full_resolution_request = -1
        if previous_frame_index != self._current_frame_index:
            self._full_resolution_error_frame = -1
        if (
            self._pending_marker is not None
            and previous_frame_index >= 0
            and self._current_frame_index != self._pending_marker[2]
        ):
            self.cancel_marker_edit()
        self.video_view.set_frame(
            image,
            source_width=self._source_width,
            source_height=self._source_height,
        )
        self._update_frame_quality(image)
        self._update_status_label()
        self._set_transport_enabled(True)
        self.video_view.set_marker_mode(self._marking_enabled)
        self._render_marker()
        if not self._timeline_dragging:
            self.timeline.setValue(max(0, min(int(position_ms), self._duration_ms)))
        self._update_time_label(position_ms)
        self._update_frame_indicator(position_ms)
        self.position_changed.emit(
            self._current_position_ms - self._target_position_ms
        )
        self.preview_frame_ready.emit(image, self._current_position_ms, self._current_frame_index)
        self._schedule_paused_frame_detail()
        if previous_frame_index < 0 and self._event is not None:
            self.initial_frame_ready.emit(self._event.event_id)
        needs_presentation = getattr(worker, "frame_needs_presentation", None)
        if callable(needs_presentation) and needs_presentation(frame_index):
            self.video_view.viewport().repaint()
            worker.acknowledge_presented_frame(frame_index)

    def _on_full_resolution_ready(
        self,
        image,
        position_ms: int,
        frame_index: int,
    ) -> None:
        if self.sender() is not self._worker:
            return
        if (not self._full_resolution_enabled
                or int(frame_index) != self._current_frame_index or self._playing
                or self._video_scrubbing or self._timeline_dragging):
            return
        self.video_view.set_frame(
            image,
            source_width=self._source_width,
            source_height=self._source_height,
        )
        self._current_position_ms = int(position_ms)
        self._full_resolution_error_frame = -1
        self._update_frame_quality(image)
        self._render_marker()
        self._update_time_label(position_ms)
        self._update_frame_indicator(position_ms)
        # Camera 1 originals belong to the judging view. The overview and
        # timeline keep their lightweight previews for spotting passages.
        if self.source_kind != REGULAR_SOURCE or self.camera_index != 1:
            self.preview_frame_ready.emit(image, int(position_ms), int(frame_index))

    def _on_full_resolution_error(self, message: str, frame_index=None) -> None:
        if self.sender() is not self._worker:
            return
        if (not self._full_resolution_enabled or self._playing
                or self._video_scrubbing or self._timeline_dragging
                or (frame_index is not None and int(frame_index) != self._current_frame_index)):
            return
        self._last_full_resolution_request = -1
        self._full_resolution_error_frame = self._current_frame_index
        if self._workbench_compact:
            # The workbench has no HD button: make the short label itself the
            # retry target for this exact frame.
            self.quality_label.setText("原图失败，点此重试")
            self.quality_label.setToolTip(
                "点击此处或“复位画面”重读当前帧原图"
            )
            self.quality_label.setCursor(Qt.PointingHandCursor)
        else:
            self.quality_label.setText("原图读取失败，可点高清重试")
        self._set_status_label(self._availability_status(), str(message))

    def _update_frame_quality(self, image) -> None:
        original = (self._source_width > 0 and self._source_height > 0
                    and image.width() >= self._source_width
                    and image.height() >= self._source_height)
        kind = "原图" if original else "预览"
        self.quality_label.setText(f"{kind} {image.width()}×{image.height()}")
        self.quality_label.setCursor(Qt.ArrowCursor)

    def set_full_resolution_enabled(self, enabled: bool) -> None:
        """Only the judging popup needs original pixels in single-camera mode."""
        if self._full_resolution_enabled == bool(enabled):
            return
        self._full_resolution_enabled = bool(enabled)
        self._full_resolution_timer.stop()
        self._last_full_resolution_request = -1
        if enabled:
            self._schedule_paused_frame_detail()
        elif self.video_view.has_frame:
            image = self.video_view._pixmap_item.pixmap().toImage()
            if image.width() > 1280 or image.height() > 720:
                image = image.scaled(1280, 720, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.video_view.set_frame(image, source_width=self._source_width,
                                     source_height=self._source_height)
            self._update_frame_quality(image)

    def _on_detail_requested(self) -> None:
        if not self._full_resolution_enabled or self._current_frame_index < 0:
            return
        if self.source_kind == REGULAR_SOURCE and self.camera_index == 1:
            was_playing = self._playing
            position_ms, frame_index = self._current_position_ms, self._current_frame_index
            self.detail_requested.emit(self)
            if was_playing:
                self.seek_media_frame(position_ms, frame_index)
        self._schedule_full_resolution_request()

    def _update_time_label(self, position_ms: int) -> None:
        event = self._event
        if event is None:
            return
        delta_ms = int(position_ms) - self._target_position_ms
        current_timestamp_ms = event.timeline_timestamp_ms + delta_ms
        self.time_label.setText(
            format_passage_time(current_timestamp_ms)
        )
        self.time_label.setToolTip(
            f"文件位置 {position_ms / 1000.0:.3f} s；"
            f"预计过线 {format_passage_time(event.timeline_timestamp_ms)}；"
            f"距预计过线 {delta_ms:+d} ms"
        )
        judged = getattr(self, "_judged_time", None)
        if judged is not None and judged[0] == int(position_ms):
            # One clock on the screen (11-14): the table and the strip are in chip
            # time, this label gave the recorder's, 0.6 s later on 古浪.
            self.time_label.setToolTip(
                f"校时后的时间，与左表和过线图一致；录像机时钟 {self.time_label.text()}。" + self.time_label.toolTip())
            self.time_label.setText(format_passage_time(judged[1]))

    def set_judged_time(self, position_ms, timestamp_ms) -> None:
        """Show the calibrated (chip) time for the frame at POSITION_MS; None goes back to the recorder's."""
        self._judged_time = None if timestamp_ms is None else (int(position_ms), int(timestamp_ms))
        self._update_time_label(self._current_position_ms)

    def _update_frame_indicator(self, position_ms: int) -> None:
        event = self._event
        if event is None or self._current_frame_index < 0:
            self.frame_indicator_label.setText("当前帧 -- | --:--:--.---")
            self.video_view.clear_frame_indicator()
            return
        delta_ms = int(position_ms) - int(self._target_position_ms)
        timestamp_ms = int(event.timeline_timestamp_ms) + delta_ms
        text = (
            f"帧 {self._current_frame_index + 1}"
            f" | {format_passage_time(timestamp_ms)}"
            f" | Δ{delta_ms:+d}ms"
        )
        self.frame_indicator_label.setText(text)
        # The pane header is the single authoritative frame/time display.
        # Avoid repeating the same text over the video image.
        self.video_view.clear_frame_indicator()

    def _request_full_resolution(self) -> None:
        worker = self._worker
        frame_index = self._current_frame_index
        if (not self._full_resolution_enabled or worker is None or frame_index < 0 or self._playing
                or self._video_scrubbing or self._timeline_dragging):
            return
        if frame_index == self._last_full_resolution_request:
            return
        self._last_full_resolution_request = frame_index
        self.quality_label.setText("原图读取中…")
        worker.request_full_resolution(frame_index)

    def _schedule_full_resolution_request(self) -> None:
        if (
            not self._full_resolution_enabled
            or self._worker is None
            or self._current_frame_index < 0
            or self._playing
            or self._video_scrubbing
            or self._timeline_dragging
        ):
            return
        self._full_resolution_timer.start()

    def _schedule_paused_frame_detail(self) -> None:
        # Ordinary video pauses should use the original even when fitted below
        # 100%. Very wide line-scan images retain their on-demand detail tiles.
        if self.source_kind == REGULAR_SOURCE or self.video_view.zoom_percent >= 100:
            self._schedule_full_resolution_request()

    def _flush_full_resolution_request(self) -> None:
        self._request_full_resolution()

    def _on_zoom_changed(self, percent: int) -> None:
        self.actual_size_btn.setText(f"{int(percent)}%")

    def _on_timeline_pressed(self) -> None:
        self._reset_video_scrub()
        self._timeline_dragging = True
        self.set_playing(False)
        self.scrub_started.emit()

    def _on_timeline_moved(self, position_ms: int) -> None:
        if not self._timeline_dragging:
            return
        position_ms = int(position_ms)
        self._pending_scrub_delta_ms = position_ms - self._target_position_ms
        if not self._scrub_preview_timer.isActive():
            self._scrub_preview_timer.start()

    def _on_timeline_released(self) -> None:
        self._timeline_dragging = False
        self._scrub_preview_timer.stop()
        self._pending_scrub_delta_ms = None
        self.passage_delta_requested.emit(
            int(self.timeline.value()) - self._target_position_ms
        )

    def _on_video_scrub_started(self) -> None:
        self._reset_video_scrub()
        self._video_scrubbing = True
        self._last_full_resolution_request = -1
        self._scrub_origin_delta_ms = (
            self._current_position_ms - self._target_position_ms
        )
        self.scrub_started.emit()

    def _on_video_scrub_delta(self, horizontal_pixels: int) -> None:
        delta_ms = self._scrub_delta_ms(horizontal_pixels)
        self._pending_scrub_delta_ms = delta_ms
        if not self._scrub_preview_timer.isActive():
            self._scrub_preview_timer.start()

    def _scrub_delta_ms(self, horizontal_pixels: int) -> int:
        viewport_width = max(1, self.video_view.viewport().width())
        frame_ms = self.frame_duration_ms()
        scrub_span_ms = max(
            frame_ms,
            min(self._duration_ms, self.MAX_SCRUB_SPAN_MS),
        )
        raw_delta_ms = (
            int(horizontal_pixels) * scrub_span_ms / viewport_width
        )
        frame_delta = int(round(raw_delta_ms / frame_ms))
        return self._scrub_origin_delta_ms + frame_delta * frame_ms

    def _flush_scrub_preview(self) -> None:
        delta_ms = self._pending_scrub_delta_ms
        self._pending_scrub_delta_ms = None
        if delta_ms is not None and (self._video_scrubbing or self._timeline_dragging):
            self.scrub_preview_requested.emit(delta_ms)

    def _on_video_scrub_finished(self, horizontal_pixels: int) -> None:
        final_delta_ms = self._scrub_delta_ms(horizontal_pixels)
        self._scrub_preview_timer.stop()
        self._pending_scrub_delta_ms = None
        self._video_scrubbing = False
        self._timeline_dragging = False
        self.passage_delta_requested.emit(final_delta_ms)

    def _reset_video_scrub(self) -> None:
        self._scrub_preview_timer.stop()
        self._full_resolution_timer.stop()
        self._pending_scrub_delta_ms = None
        self._video_scrubbing = False
        self._timeline_dragging = False

    def _on_playback_finished(self) -> None:
        if self.sender() is not self._worker:
            return
        was_playing = self._playing
        self._playing = False
        self.play_btn.setText("▶")
        self._schedule_paused_frame_detail()
        if was_playing:
            # The surface decides whether the next file carries on (2.5 s pieces, 10-29).
            self.playback_reached_end.emit()

    def _on_step_boundary_reached(self, direction: int) -> None:
        if self.sender() is not self._worker:
            return
        self.step_boundary_reached.emit(1 if int(direction) > 0 else -1)

    def show_navigation_notice(self, message: str) -> None:
        self.quality_label.setText(message)
        self.quality_label.setToolTip(message)

    def _on_step_failed(self, message: str) -> None:
        if self.sender() is self._worker:
            self.show_navigation_notice(message)

    def _on_playback_error(self, message: str) -> None:
        if self.sender() is not self._worker:
            return
        self._playing = False
        self.play_btn.setText("▶")
        self._set_status_label(self._availability_status(), message)
        self.video_label.clear_frame("画面读取失败")
        self._set_transport_enabled(False)
        self.open_btn.setEnabled(
            self.source_kind == REGULAR_SOURCE
            and self._location is not None
            and self._location.status in _CONFIRMABLE_STATUSES
            and self._location.video_path.is_file()
        )

    def set_play_speed(self, speed: float) -> None:
        """How fast ▶ plays from now on; a pause puts it back to normal speed."""

        self._play_speed = max(1.0, min(4.0, float(speed)))
        even = getattr(self._worker, "set_even_playback", None)
        if callable(even):
            even(True)  # a fast run steps evenly; at normal speed it changes nothing
        if self._playing and self._worker is not None:
            self._worker.set_shuttle_speed(self._play_speed)

    def set_playing(self, playing: bool) -> None:
        worker = self._worker
        if worker is None:
            return
        if playing:
            self.start_deferred_worker()
        self._full_resolution_timer.stop()
        self._playing = bool(playing)
        if self._playing:
            worker.set_shuttle_speed(self._play_speed)
            self.play_btn.setText("Ⅱ")
        else:
            self._play_speed = 1.0
            worker.pause()
            self.play_btn.setText("▶")
            self._last_full_resolution_request = -1
            self._schedule_paused_frame_detail()

    def toggle_playing(self) -> None:
        self.set_playing(not self._playing)

    @property
    def is_playing(self) -> bool:
        return self._playing

    def step(self, frame_delta: int) -> None:
        worker = self._worker
        if worker is None:
            return
        self.start_deferred_worker()
        self._full_resolution_timer.stop()
        self._last_full_resolution_request = -1
        worker.step(frame_delta)
        self._playing = False
        self.play_btn.setText("▶")

    def frame_duration_ms(self) -> int:
        if self._fps <= 0.1:
            return 40
        return max(1, int(round(1000.0 / self._fps)))

    def available_delta_bounds(self) -> Optional[tuple[int, int]]:
        if self._worker is None or self._duration_ms <= 0:
            return None
        return (
            -self._target_position_ms,
            self._duration_ms - self._target_position_ms,
        )

    def seek_passage_delta(
        self,
        delta_ms: int,
        *,
        linked_playing: bool = False,
        preview: bool = False,
    ) -> None:
        worker = self._worker
        if worker is None:
            return
        self.start_deferred_worker()
        self._full_resolution_timer.stop()
        self._last_full_resolution_request = -1
        bounds = self.available_delta_bounds()
        if bounds is None:
            return
        lower, upper = bounds
        clamped_delta_ms = max(lower, min(int(delta_ms), upper))
        if preview:
            self._video_scrubbing = True
            self._last_full_resolution_request = -1
        position_ms = self._target_position_ms + clamped_delta_ms
        self._playing = bool(linked_playing)
        seek_and_play = getattr(worker, "seek_and_play", None)
        if not self._playing:
            self._play_speed = 1.0
        if self._playing and callable(seek_and_play):
            seek_and_play(position_ms, self._play_speed)
        else:
            worker.pause()
            preview_seek = getattr(worker, "seek_preview", None)
            if preview and callable(preview_seek):
                preview_seek(position_ms)
            else:
                worker.seek(position_ms)
            if self._playing:
                worker.set_shuttle_speed(self._play_speed)
        self.play_btn.setText("Ⅱ" if self._playing else "▶")

    def set_linked_playing(self, playing: bool) -> None:
        self._full_resolution_timer.stop()
        self._playing = bool(playing)
        worker = self._worker
        if not self._playing:
            self._play_speed = 1.0
        if worker is not None:
            if self._playing:
                worker.set_shuttle_speed(self._play_speed)
            else:
                worker.pause()
                self._last_full_resolution_request = -1
                self._schedule_paused_frame_detail()
        self.play_btn.setText("Ⅱ" if self._playing else "▶")

    def linked_drift_ms(self, delta_ms: int) -> Optional[int]:
        bounds = self.available_delta_bounds()
        if bounds is None:
            return None
        lower, upper = bounds
        clamped_delta_ms = max(lower, min(int(delta_ms), upper))
        expected_position_ms = self._target_position_ms + clamped_delta_ms
        current_position_ms = self._current_position_ms
        worker = self._worker
        if worker is not None:
            worker_position_ms = getattr(worker, "current_position_ms", None)
            if isinstance(worker_position_ms, (int, float)):
                current_position_ms = int(worker_position_ms)
        return current_position_ms - expected_position_ms

    def current_delta_ms(self) -> Optional[int]:
        bounds = self.available_delta_bounds()
        if bounds is None:
            return None
        current_frame_index = self._current_frame_index
        current_position_ms = self._current_position_ms
        worker = self._worker
        if worker is not None:
            worker_frame_index = getattr(worker, "current_frame_index", None)
            if isinstance(worker_frame_index, int):
                current_frame_index = worker_frame_index
            worker_position_ms = getattr(worker, "current_position_ms", None)
            if isinstance(worker_position_ms, (int, float)):
                current_position_ms = int(worker_position_ms)
        if current_frame_index < 0:
            return None
        lower, upper = bounds
        return max(
            lower,
            min(current_position_ms - self._target_position_ms, upper),
        )

    def release_playback_cache(self) -> None:
        worker = self._worker
        if worker is None:
            return
        release_cache = getattr(worker, "release_cache", None)
        if callable(release_cache):
            release_cache()

    def park_playback_cache(self) -> None:
        worker = self._worker
        if worker is None:
            return
        park_cache = getattr(worker, "park_cache", None)
        if callable(park_cache):
            park_cache()
            return
        release_cache = getattr(worker, "release_cache", None)
        if callable(release_cache):
            release_cache()

    def set_idle_prefetch_enabled(self, enabled: bool) -> None:
        self._idle_prefetch_enabled = bool(enabled) and not self._low_resource_mode
        worker = self._worker
        if worker is None:
            return
        set_enabled = getattr(worker, "set_idle_prefetch_enabled", None)
        if callable(set_enabled):
            set_enabled(self._idle_prefetch_enabled)

    def linked_drift_tolerance_ms(self) -> int:
        return max(
            self.MIN_LINKED_DRIFT_TOLERANCE_MS,
            self.frame_duration_ms() * self.LINKED_DRIFT_FRAME_MULTIPLIER,
        )

    def _set_transport_enabled(self, enabled: bool) -> None:
        self.hd_btn.setEnabled(enabled and self.video_view.has_frame)
        self.previous_frame_btn.setEnabled(enabled)
        self.play_btn.setEnabled(enabled)
        self.next_frame_btn.setEnabled(enabled)
        self.mark_btn.setEnabled(
            enabled
            and self.video_view.has_frame
            and self._location is not None
            and self._location.status in _CONFIRMABLE_STATUSES
        )
        self.confirm_btn.setEnabled(enabled and self.pending_confirmation() is not None)
        self.open_btn.setEnabled(
            enabled
            and self.source_kind == REGULAR_SOURCE
            and self._location is not None
            and self._location.status in _CONFIRMABLE_STATUSES
            and self._location.segment.clock_source != AUYAT_CLOCK_SOURCE
        )

    def _request_open(self) -> None:
        if self._event is not None and self._location is not None:
            self.open_requested.emit(self._event, self._location)

    def _retire_worker(
        self,
        worker: VideoPlaybackWorker,
        *,
        wait: bool,
    ) -> None:
        worker.request_stop()
        if wait:
            worker.wait(2_000)
        if worker.isRunning():
            retire_qthread(worker)
        else:
            worker.deleteLater()

    def _stop_worker(self, *, wait: bool = False) -> None:
        self._reset_video_scrub()
        worker = self._worker
        self._worker = None
        if worker is None:
            return
        self._retire_worker(worker, wait=wait)

    def shutdown(self, *, wait: bool = True) -> None:
        self._stop_worker(wait=wait)
