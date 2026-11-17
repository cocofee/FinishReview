"""FinishLynx-style review workspace for CycleRace passages and video evidence."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import logging
import math
from pathlib import Path
import re
import time
from types import SimpleNamespace
from typing import Callable, Iterable, Mapping, Optional

from PyQt5.QtCore import QEvent, QPoint, QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QImage,
    QKeySequence,
    QPainter,
    QPalette,
    QPen,
    QPixmap,
    QTransform,
)
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsView,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QInputDialog,
    QPushButton,
    QScrollArea,
    QSlider,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStyledItemDelegate,
    QShortcut,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import APP_DISPLAY_NAME, APP_WINDOW_TITLE
from .auyat_rgb import AUYAT_CLOCK_SOURCE, AuyatRgbPlaybackWorker
from .camera_judgments import CameraJudgment
from .clock_sync import CLOCK_MODEL_FILENAME, MANUAL_ANCHOR_TOLERANCE_MS, ClockModelStore
from .review_marker_style import marker_style
from .race_filmstrip import RaceFilmstripFrame, recording_sources
from .external_clip_import import EXTERNAL_CLOCK_SOURCE
from .passage_evidence import (
    ContinuousMarkerStore,
    HIGH_SPEED_SOURCE,
    REGULAR_SOURCE,
    PassageEvidenceAssociation,
    PassageEvidenceAssociationStore,
    VideoClockCalibrationStore,
)
from .passage_receiver import PassageEvent, PassageEventStore
from .person_follow import FOLLOW_AFTER_MS, FOLLOW_BEFORE_MS, FollowTracks, follow_path
from .person_follow import OUTPUT_DIRNAME as FOLLOW_OUTPUT_DIRNAME
from .passage_batch import (
    DEFAULT_REVIEW_GAP_MS,
    DEFAULT_SUBWAVE_GAP_MS,
    PassageReviewBatch,
    build_review_batches,
)
from .video_arrival import (
    DEFAULT_ARRIVAL_BATCH_GAP_MS,
    DEFAULT_ARRIVAL_SUBWAVE_GAP_MS,
    VideoArrivalBatch,
    VideoArrivalCandidateStore,
    build_video_arrival_batches,
)
from .video_discovery import VideoDiscoveryError, VideoDiscoveryStore
from .playback_coordinator import PlaybackCoordinator
from .race_metadata import (
    RaceAthleteMetadata,
    RaceMetadata,
    RaceMetadataStore,
)
from .review_selection import ReviewSelectionController, ReviewSelectionPlan
from .review_clip import PassageReviewBindingStore
from .runtime_metrics import RuntimeMetrics
from .thread_lifecycle import retire_qthread, track_qthread
from .video_playback import TargetTimelineSlider, VideoPlaybackWorker
from .video_filmstrip import (
    CONTINUOUS_FILMSTRIP_INTERVAL_MS,
    VideoFilmstripWidget,
)
from .video_timeline import (
    DEFAULT_CLOCK_SOURCE,
    PassageVideoLocation,
    PassageVideoLookup,
    VideoTimelineStore,
)
from .passage_review_common import (
    BEIJING_TIMEZONE,
    CTRL_FRAME_STEP,
    SHIFT_FRAME_STEP,
    UI_BASE_FONT_POINT_SIZE,
    UI_FONT_FAMILY,
    UI_INFO_PANEL_DEFAULT_WIDTH,
    UI_INFO_PANEL_MAX_WIDTH,
    UI_INFO_PANEL_MIN_WIDTH,
    _ARCHIVE_SEGMENT_SUFFIX_RE,
    _AutoFitTableWidget,
    _CONFIRMABLE_STATUSES,
    _OPENABLE_STATUSES,
    _ReviewFilterBar,
    _STATUS_PRIORITY,
    _STATUS_TEXT,
    _StatusColorDelegate,
    _TABLE_COLUMN_MIN_WIDTHS,
    _association_matches_location,
    _expanded_column_widths,
    _resolve_evidence_layout,
    combined_review_status,
    compact_source_status,
    format_passage_time,
    is_high_speed,
    location_clock_text,
    location_source_text,
    location_status_text,
    lookup_status_text,
    review_status_text,
    source_confirmation_status,
    source_location,
)
from .evidence_image_view import (
    COUNT_MODE_HELP,
    EvidenceImageView,
    HOVER_REACH_PX,
    PERSON_TAG_FONT_PX,
    PERSON_TAG_LIFT_PX,
    SELECTED_RING_PX,
)
from .passage_evidence_pane import PassageEvidencePane
from .passage_review_video_assist import PassageReviewVideoAssistMixin

logger = logging.getLogger("FinishReview.Review")


class PassageReviewSurface(
    PassageReviewVideoAssistMixin,
    QDialog,
):
    """Shared Qt review surface used by the standalone and formal windows.

    This class is an implementation surface rather than an application window;
    concrete windows are siblings so neither owns the other's lifecycle.
    """
    INACTIVE_CAMERA_START_DELAY_MS = 120
    # Keep the locating surface aligned with the two-minute visual scan core.
    FILMSTRIP_WINDOW_MS = 120_000
    FILMSTRIP_COVERAGE_TOLERANCE_MS = 250
    FILMSTRIP_ALWAYS_AVAILABLE = True
    VIDEO_ASSIST_ENABLED = True
    # Automatic gap seeking can hide riders whose chips were not read. Keep it
    # disabled; operators can use the guarded Shift/Ctrl shortcuts while
    # watching the video.
    CONTINUOUS_AUTO_SKIP = False
    CONTINUOUS_SKIP_GAP_MS = 2_000
    CONTINUOUS_SKIP_LEAD_MS = 2_000
    MANUAL_CONTINUATION_WINDOW_MS = 5_000
    CONTINUOUS_ROSTER_SIZE = 24
    SINGLE_CAMERA_PREVIEW = True

    clock_offset_changed = pyqtSignal(int)
    record_deleted = pyqtSignal(str)
    evidence_pane_added = pyqtSignal(object)
    video_review_updated = pyqtSignal(object)
    video_candidates_received = pyqtSignal(object)
    video_candidate_persistence_failed = pyqtSignal(object)
    video_candidate_requested = pyqtSignal(object)
    video_review_apply_requested = pyqtSignal(object)
    video_review_status_changed = pyqtSignal(str, str, str)

    SYNC_STARTUP_GRACE_MS = 250
    SYNC_CORRECTION_COOLDOWN_SECONDS = 0.5
    REVIEW_BATCH_GAP_MS = DEFAULT_REVIEW_GAP_MS
    REVIEW_SUBWAVE_GAP_MS = DEFAULT_SUBWAVE_GAP_MS

    def __init__(
        self,
        passage_store: PassageEventStore,
        timeline_store: VideoTimelineStore,
        parent=None,
        *,
        clock_offset_ms: int = 0,
        clock_offset_by_camera: Optional[Mapping[int, int]] = None,
        pre_roll_ms: int = 3_000,
        association_store: Optional[PassageEvidenceAssociationStore] = None,
        continuous_marker_store: Optional[ContinuousMarkerStore] = None,
        calibration_store: Optional[VideoClockCalibrationStore] = None,
        metadata_store: Optional[RaceMetadataStore] = None,
        open_location: Optional[
            Callable[[PassageEvent, PassageVideoLocation], None]
        ] = None,
        high_speed_locator: Optional[
            Callable[[PassageEvent, int, int], Optional[PassageVideoLocation]]
        ] = None,
        regular_camera_indexes: Optional[Iterable[int]] = None,
        show_high_speed_pane: Optional[bool] = None,
        include_recorded_evidence: bool = True,
        review_binding_store: Optional[PassageReviewBindingStore] = None,
        video_discovery_store: Optional[VideoDiscoveryStore] = None,
        video_arrival_store: Optional[VideoArrivalCandidateStore] = None,
        low_resource_mode: bool = False,
    ):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.Window
            | Qt.WindowTitleHint
            | Qt.WindowSystemMenuHint
            | Qt.WindowMinMaxButtonsHint
            | Qt.WindowCloseButtonHint
        )
        self.passage_store = passage_store
        self.timeline_store = timeline_store
        self.review_binding_store = review_binding_store
        self.runtime_metrics = RuntimeMetrics()
        self.video_discovery_store = video_discovery_store or VideoDiscoveryStore(
            passage_store.journal_path.with_name("video_discoveries.jsonl")
        )
        self.video_arrival_store = video_arrival_store or VideoArrivalCandidateStore(
            passage_store.journal_path.with_name("video_arrival_candidates.jsonl"),
            metrics=self.runtime_metrics,
        )
        self.association_store = association_store or PassageEvidenceAssociationStore(
            passage_store.journal_path.with_name("passage_evidence_associations.jsonl")
        )
        self.continuous_marker_store = continuous_marker_store or ContinuousMarkerStore(
            passage_store.journal_path.with_name("continuous_markers.jsonl")
        )
        # Saved runner paths per camera, for numbers that walk with their runner.
        self._follow_cache: dict[Path, FollowTracks] = {}
        self.calibration_store = calibration_store or VideoClockCalibrationStore(
            passage_store.journal_path.with_name("video_clock_calibrations.jsonl")
        )
        self.metadata_store = metadata_store
        self.clock_offset_ms = int(clock_offset_ms)
        self._clock_offset_by_camera = {
            int(camera): int(offset)
            for camera, offset in (clock_offset_by_camera or {}).items()
        }
        self.pre_roll_ms = max(0, int(pre_roll_ms))
        self._low_resource_mode = bool(low_resource_mode)
        self._workbench_compact_mode = False
        self._workbench_allow_multi_camera_maximize = False
        self._open_location = open_location
        self._high_speed_locator = high_speed_locator
        requested_high_speed = (
            bool(high_speed_locator)
            if show_high_speed_pane is None
            else bool(show_high_speed_pane)
        )
        (
            self._configured_regular_camera_indexes,
            self._show_high_speed_pane,
        ) = _resolve_evidence_layout(
            timeline_store,
            regular_camera_indexes=regular_camera_indexes,
            show_high_speed_pane=requested_high_speed,
            include_recorded=include_recorded_evidence,
        )
        self._include_recorded_evidence = bool(include_recorded_evidence)
        self._regular_panes_by_camera: dict[int, PassageEvidencePane] = {}
        self._external_location_revision = 0
        self._visible_events: list[PassageEvent] = []
        self._lookups: dict[str, PassageVideoLookup] = {}
        self._lookup_cache: dict[str, tuple[tuple, PassageVideoLookup]] = {}
        self._timeline_signature: tuple = ()
        self._selected_event_id = ""
        self._shared_delta_ms = 0
        self._sync_playing = False
        self._sync_origin_delta_ms = 0
        self._sync_started_at = 0.0
        self._last_sync_correction_at = 0.0
        self._sync_timer = QTimer(self)
        self._sync_timer.setInterval(30)
        self._sync_timer.timeout.connect(self._on_sync_tick)
        self._active_pane: Optional[PassageEvidencePane] = None
        self._selection_event_id = ""
        self._selection_started_at = 0.0
        self._selection_first_frame_ms = 0.0
        self._selection_expected_panes = 0
        self._selection_pending_panes: set[PassageEvidencePane] = set()
        self._deferred_selection_panes: set[PassageEvidencePane] = set()
        self._selection_priority_pane: Optional[PassageEvidencePane] = None
        self._deferred_start_timer = QTimer(self)
        self._deferred_start_timer.setSingleShot(True)
        self._deferred_start_timer.setInterval(
            self.INACTIVE_CAMERA_START_DELAY_MS
        )
        self._deferred_start_timer.timeout.connect(
            self._start_deferred_selection_panes
        )
        self._maximized_pane: Optional[PassageEvidencePane] = None
        self._maximized_window: Optional[QDialog] = None
        self._maximized_escape_shortcut: Optional[QShortcut] = None
        self._maximized_pane_index = -1
        self._maximized_content_splitter: Optional[QSplitter] = None
        self._maximized_original_indexes: dict[PassageEvidencePane, int] = {}
        self._maximized_hosted_panes: tuple[PassageEvidencePane, ...] = ()
        self._maximized_mode_label: Optional[QLabel] = None
        self._maximized_mode_buttons: dict[object, QPushButton] = {}
        self._maximized_camera_shortcuts: list[QShortcut] = []
        self._maximized_roster_splitter = None
        self._popup_return_state = None
        self._available_evidence_count = 0
        self._located_event_ids: set[str] = set()
        self._confirmed_event_ids: set[str] = set()
        self._event_review_statuses: dict[str, str] = {}
        self._review_batches: tuple[PassageReviewBatch, ...] = ()
        self._review_batch_by_event_id: dict[str, PassageReviewBatch] = {}
        self._active_review_batch_id = ""
        self._batch_mode = False
        self._auto_advance_before_continuous = False
        self._continuous_clock_offsets: dict[tuple[int, str], int] = {}
        self._continuous_calibration_revision = 0
        # Video candidates are advisory only; they never enter PassageEventStore.
        self._video_reconciliation: tuple[object, ...] = ()
        self._video_reconciliation_by_id: dict[str, object] = {}
        # Keep every visual candidate for fast Ctrl+Left/Right navigation.
        # This is intentionally separate from the anomaly-only review list.
        self._video_navigation_candidates: tuple[object, ...] = tuple(
            self.video_arrival_store.candidates()
        )
        self._video_arrival_batch_gap_ms = DEFAULT_ARRIVAL_BATCH_GAP_MS
        self._video_arrival_subwave_gap_ms = DEFAULT_ARRIVAL_SUBWAVE_GAP_MS
        self._video_arrival_batches: tuple[VideoArrivalBatch, ...] = (
            build_video_arrival_batches(
                self._video_navigation_candidates,
                batch_gap_ms=self._video_arrival_batch_gap_ms,
                subwave_gap_ms=self._video_arrival_subwave_gap_ms,
            )
        )
        self._video_review_statuses: dict[str, str] = {}
        self._video_review_bibs: dict[str, str] = {}
        self._active_video_anomaly_id = ""
        self._video_anomaly_cursor = -1
        self._video_anomaly_dialog: Optional[QDialog] = None
        self._video_anomaly_dialog_refresh: Optional[Callable[[], None]] = None
        self._video_arrival_dialog: Optional[QDialog] = None
        self._video_arrival_dialog_refresh: Optional[Callable[[], None]] = None
        self._reload_video_discoveries()
        self._active_video_discovered_entry_id = ""
        self._active_review_filter = "all"
        self._total_event_count = 0
        self._queue_expanded = False
        self._queue_default_sizes: list[int] = []
        # The filmstrip is the fast locating surface; the lower camera pane is
        # the frame-accurate judgment surface. Do not render the duplicate
        # large preview in continuous review mode.
        self._top_preview_video_visible = False
        self._metadata_context_key: tuple[str, str] = ("", "")
        self._indexed_metadata: Optional[RaceMetadata] = None
        self._metadata_athletes_by_id: dict[str, RaceAthleteMetadata] = {}
        self._metadata_athletes_by_identity: dict[str, RaceAthleteMetadata] = {}
        self._search_refresh_timer = QTimer(self)
        self._search_refresh_timer.setSingleShot(True)
        self._search_refresh_timer.setInterval(120)
        self._search_refresh_timer.timeout.connect(self._refresh_filtered_view)
        self._filmstrip_context: tuple[Path, int, int] | None = None
        self._filmstrip_absolute_window: tuple[int, int] | None = None
        # A batch button press is an explicit operator navigation request. It
        # must be allowed to replace the active strip even when the next
        # archive segment computes a temporarily overlapping local window.
        self._filmstrip_batch_navigation_pending = False
        self._filmstrip_requested_window_start_ms: int | None = None
        self._filmstrip_gap_message = ""
        self._review_split_resize_pending = False
        self._pending_filmstrip_position: int | None = None
        self._filmstrip_seek_pending = False
        self._filmstrip_seek_retry_count = 0
        self._pending_filmstrip_preview_position: int | None = None
        self._filmstrip_preview_timer = QTimer(self)
        self._filmstrip_preview_timer.setSingleShot(True)
        self._filmstrip_preview_timer.setInterval(80)
        self._filmstrip_preview_timer.timeout.connect(
            self._flush_filmstrip_preview
        )
        self._pending_filmstrip_anchor: tuple[Path, int] | None = None
        self._filmstrip_anchor_timer = QTimer(self)
        self._filmstrip_anchor_timer.setSingleShot(True)
        self._filmstrip_anchor_timer.setInterval(100)
        self._filmstrip_anchor_timer.timeout.connect(
            self._flush_filmstrip_anchor
        )
        self._filmstrip_prefetch_timer = QTimer(self)
        self._filmstrip_prefetch_timer.setSingleShot(True)
        self._filmstrip_prefetch_timer.setInterval(800)
        self._filmstrip_prefetch_timer.timeout.connect(
            self._prefetch_next_filmstrip_window
        )
        self._filmstrip_operator_idle_timer = QTimer(self)
        self._filmstrip_operator_idle_timer.setSingleShot(True)
        self._filmstrip_operator_idle_timer.setInterval(1_200)
        self._filmstrip_operator_idle_timer.timeout.connect(
            self._on_filmstrip_operator_idle
        )
        self.playback_coordinator: PlaybackCoordinator | None = None

        self.setWindowTitle(APP_WINDOW_TITLE)
        self.resize(1400, 860)
        self.setMinimumSize(1100, 700)
        self._init_ui()
        self.selection_controller = ReviewSelectionController(
            self,
            high_speed_location=lambda lookup: source_location(
                lookup,
                high_speed=True,
            ),
            openable_statuses=frozenset(_OPENABLE_STATUSES),
        )
        # Keep the standard queue/evidence layout until the operator enters
        # continuous filmstrip review explicitly.
        self._set_continuous_review_layout(False)
        self._set_video_assist_controls_visible(self._video_assist_enabled())
        self.space_shortcut = QShortcut(QKeySequence(Qt.Key_Space), self)
        self.space_shortcut.setContext(Qt.WindowShortcut)
        self.space_shortcut.activated.connect(self._toggle_active_pane)
        self.previous_passage_shortcut = QShortcut(
            QKeySequence(Qt.Key_PageUp), self
        )
        self.previous_passage_shortcut.setContext(Qt.WindowShortcut)
        self.previous_passage_shortcut.activated.connect(
            lambda: self._move_selection(-1)
        )
        self.next_passage_shortcut = QShortcut(
            QKeySequence(Qt.Key_PageDown), self
        )
        self.next_passage_shortcut.setContext(Qt.WindowShortcut)
        self.next_passage_shortcut.activated.connect(
            lambda: self._move_selection(1)
        )
        self.fullscreen_shortcut = QShortcut(QKeySequence(Qt.Key_F11), self)
        self.fullscreen_shortcut.setContext(Qt.WindowShortcut)
        self.fullscreen_shortcut.activated.connect(self._toggle_fullscreen)
        self.refresh()

    def _init_ui(self) -> None:
        self.setStyleSheet(
            f'QDialog {{ background: #edf1f4; color: #17212b; '
            f'font-family: "{UI_FONT_FAMILY}"; '
            f'font-size: {UI_BASE_FONT_POINT_SIZE}pt; }}'
            "QFrame#reviewPanel, QFrame#passageEvidencePane { background: #ffffff; "
            "border: 1px solid #cfd7df; border-radius: 4px; }"
            "QFrame#passageEvidencePane[activeJudging=\"true\"] { "
            "border: 3px solid #1976c9; background: #f5faff; }"
            "QLabel#panelTitle, QLabel#evidencePaneTitle { font-size: 11pt; font-weight: 700; }"
            "QLabel#activeJudgingBadge { background: #1976c9; color: #ffffff; "
            "border-radius: 3px; padding: 3px 8px; font-size: 10pt; font-weight: 700; }"
            "QLabel#evidencePaneStatus { color: #667085; font-size: 9pt; }"
            "QLabel#evidencePaneTime { font-family: Consolas; font-size: 10pt; font-weight: 700; }"
            "QPushButton { min-height: 28px; padding: 0 9px; font-size: 10pt; "
            "border: 1px solid #aeb8c2; "
            "border-radius: 4px; background: #ffffff; }"
            "QPushButton:hover { background: #eef5fa; border-color: #5d91b5; }"
            "QPushButton:disabled { color: #9ba5ae; background: #f4f6f8; }"
            "QPushButton[queueFilter='true'] { min-height: 28px; padding: 0 8px; "
            "border-color: transparent; color: #526170; font-weight: 600; }"
            "QPushButton[queueFilterActive='true'], "
            "QPushButton[queueFilter='true']:checked { background: #185f73; "
            "border-color: #185f73; color: #ffffff; font-weight: 700; }"
            "QPushButton[queueFilterActive='true']:hover, "
            "QPushButton[queueFilter='true']:checked:hover { background: #124d5e; "
            "border-color: #124d5e; color: #ffffff; }"
            "QCheckBox, QComboBox, QLineEdit, QSpinBox { min-height: 28px; font-size: 10pt; }"
            "QComboBox, QLineEdit, QSpinBox { background: #ffffff; "
            "border: 1px solid #b8c5cf; border-radius: 4px; padding: 0 7px; }"
            "QTableWidget { background: #ffffff; gridline-color: #d8dee5; "
            "alternate-background-color: #f8fafb; font-size: 10pt; }"
            "QHeaderView::section { background: #eef2f5; color: #526170; "
            "font-size: 10pt; font-weight: 600; padding: 5px; "
            "border: none; border-right: 1px solid #d5dce3; border-bottom: 1px solid #c8d1da; }"
            "QTableWidget::item:selected { background: #dcecf8; }"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self.info_panel = QFrame(self)
        self.info_panel.setObjectName("reviewPanel")
        self.info_panel.setMinimumWidth(UI_INFO_PANEL_MIN_WIDTH)
        self.info_panel.setMaximumWidth(UI_INFO_PANEL_MAX_WIDTH)
        self.info_panel.hide()
        self.race_value = QLabel("--", self.info_panel)
        self.stage_value = QLabel("--", self.info_panel)
        self.group_value = QLabel("--", self.info_panel)
        self.selected_identity_value = QLabel("--", self.info_panel)
        self.athlete_value = QLabel("--", self.info_panel)
        self.team_value = QLabel("--", self.info_panel)
        self.selected_time_value = QLabel("--", self.info_panel)
        self.selected_time_value.setStyleSheet("font-family: Consolas; font-weight: 700;")
        self.source_value = QLabel("--", self.info_panel)
        self.source_value.setWordWrap(True)

        results_panel = QFrame(self)
        results_panel.setObjectName("reviewPanel")
        self.results_panel = results_panel
        results_layout = QVBoxLayout(results_panel)
        results_layout.setContentsMargins(8, 6, 8, 8)
        results_layout.setSpacing(6)
        self.filter_bar = _ReviewFilterBar(results_panel)
        filters = self.filter_bar.status
        results_title = QLabel("通过记录")
        results_title.setObjectName("panelTitle")
        filters.addWidget(results_title)

        self.review_filter_buttons: dict[str, QPushButton] = {}
        self.review_filter_labels = {
            "pending": "异常复核",
            "blocked": "待人工确认",
            "confirmed": "已确认",
            "all": "全部",
        }
        review_filter_tooltips = {
            "pending": "有可用视频或时间位置，打开证据进行异常复核",
            "blocked": "暂时没有可直接核实的证据，需要人工确认或补充",
            "confirmed": "已经保存复核确认结果的记录",
            "all": "显示全部通过记录",
        }
        for filter_key in ("pending", "blocked", "confirmed", "all"):
            button = QPushButton(self.review_filter_labels[filter_key], self)
            button.setCheckable(True)
            button.setProperty("queueFilter", True)
            button.setToolTip(review_filter_tooltips[filter_key])
            button.clicked.connect(
                lambda _checked=False, key=filter_key: self._set_review_filter(key)
            )
            self.review_filter_buttons[filter_key] = button
            filters.addWidget(button)
        self._sync_review_filter_buttons()

        filters.addStretch(1)
        filters = self.filter_bar.tools
        self.group_combo = QComboBox(self)
        self.group_combo.setMinimumWidth(180)
        self.group_combo.setMaximumWidth(220)
        self.group_combo.currentIndexChanged.connect(self._on_group_changed)
        filters.addWidget(self.group_combo)
        self.identity_search = QLineEdit(self)
        self.identity_search.setPlaceholderText("运动员编号 / 姓名")
        self.identity_search.setClearButtonEnabled(True)
        self.identity_search.setMinimumWidth(180)
        self.identity_search.setMaximumWidth(320)
        self.identity_search.textChanged.connect(self._on_search_changed)
        self.identity_search.returnPressed.connect(self._find_identity)
        filters.addWidget(self.identity_search)
        self.delete_record_button = QPushButton("删除记录", self)
        self.delete_record_button.setToolTip("删除选中的通过记录")
        self.delete_record_button.clicked.connect(self._delete_selected_record)
        filters.addWidget(self.delete_record_button)
        self.filter_bar.width_controls = (
            (self.group_combo, 180, 110), (self.identity_search, 180, 120),
        )

        self.summary_label = QLabel()
        self.summary_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.summary_label.setStyleSheet("color: #667085; font-size: 9pt;")
        self.summary_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        filters.addWidget(self.summary_label)
        self.video_arrival_button = QPushButton(
            f"到达候选：{len(self._video_arrival_batches)}批/"
            f"{len(self._video_navigation_candidates)}点"
        )
        self.video_arrival_button.setToolTip(
            "打开由视频自动扫描生成的到达批次；候选独立于芯片记录"
        )
        self.video_arrival_button.setVisible(False)
        self.video_arrival_button.clicked.connect(self._open_video_arrival_list)
        filters.addWidget(self.video_arrival_button)
        self.video_review_apply_requested.connect(self.set_video_reconciliation)
        self.video_review_button = QPushButton("视频异常：0")
        self.video_review_button.setToolTip("打开视频辅助异常列表")
        self.video_review_button.setVisible(False)
        self.video_review_button.clicked.connect(self._open_video_anomaly_list)
        filters.addWidget(self.video_review_button)
        self.video_review_done_button = QPushButton("已核实")
        self.video_review_done_button.setToolTip("将当前视频异常标记为已核实")
        self.video_review_done_button.setVisible(False)
        self.video_review_done_button.clicked.connect(
            lambda: self._mark_current_video_anomaly("verified")
        )
        filters.addWidget(self.video_review_done_button)
        self.video_review_ignore_button = QPushButton("忽略")
        self.video_review_ignore_button.setToolTip("将当前视频异常标记为背景或无效")
        self.video_review_ignore_button.setVisible(False)
        self.video_review_ignore_button.clicked.connect(
            lambda: self._mark_current_video_anomaly("ignored")
        )
        filters.addWidget(self.video_review_ignore_button)
        self.video_review_bib_edit = QLineEdit(self)
        self.video_review_bib_edit.setPlaceholderText("异常批次号码")
        self.video_review_bib_edit.setClearButtonEnabled(True)
        self.video_review_bib_edit.setMinimumWidth(110)
        self.video_review_bib_edit.setMaximumWidth(150)
        self.video_review_bib_edit.setVisible(False)
        self.video_review_bib_edit.returnPressed.connect(self._apply_video_review_bib)
        filters.addWidget(self.video_review_bib_edit)

        self.video_discovered_btn = QPushButton("添加视频发现号码", self)
        self.video_discovered_btn.setToolTip(
            "在当前集团视频帧记录芯片未读到的号码，仅标记为待补录，不写入正式成绩"
        )
        self.video_discovered_btn.setEnabled(False)
        self.video_discovered_btn.clicked.connect(self._add_video_discovered_bib)
        filters.addWidget(self.video_discovered_btn)

        self.queue_expand_btn = QPushButton("↕", self)
        self.queue_expand_btn.setFixedWidth(34)
        self.queue_expand_btn.setToolTip("展开运动员列表")
        self.queue_expand_btn.clicked.connect(self._toggle_queue_expanded)
        filters.addWidget(self.queue_expand_btn)

        self.offset_spin = QSpinBox(self)
        self.offset_spin.setRange(-600_000, 600_000)
        self.offset_spin.setSingleStep(100)
        self.offset_spin.setPrefix("基础校时 ")
        self.offset_spin.setSuffix(" ms")
        self.offset_spin.setMinimumWidth(125)
        self.offset_spin.setMaximumWidth(145)
        self.offset_spin.setValue(self.clock_offset_ms)
        self.offset_spin.setToolTip("未做首人校时的录像使用此偏移；已校准录像沿用各自会话的生效偏移。")
        self.offset_spin.valueChanged.connect(self._on_offset_changed)
        filters.addWidget(self.offset_spin)
        results_layout.addWidget(self.filter_bar)

        self.table = _AutoFitTableWidget(0, 9, self)
        table_palette = self.table.palette()
        table_palette.setColor(QPalette.HighlightedText, QColor("#17212b"))
        self.table.setPalette(table_palette)
        self.table.setHorizontalHeaderLabels(
            [
                "序号",
                "号码",
                "姓名",
                "组别",
                "",
                "通过时间",
                "普通录像",
                "高速摄像",
                "复核状态",
            ]
        )
        # The operator opens the selected row directly in the single-camera
        # judgment pane, so per-source availability columns only add clutter.
        # Keep their underlying data for navigation/status decisions.
        for column in (3, 4, 6, 7):
            self.table.setColumnHidden(column, True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        table_palette = self.table.palette()
        table_palette.setColor(QPalette.HighlightedText, QColor("#17212b"))
        self.table.setPalette(table_palette)
        self.table.verticalHeader().setDefaultSectionSize(34)
        for column in (6, 7, 8):
            self.table.setItemDelegateForColumn(
                column,
                _StatusColorDelegate(self.table),
            )
        self.table.verticalHeader().setVisible(False)
        # ``itemSelectionChanged`` can fire while Qt still exposes the previous
        # current row. Use the current-cell signal so the highlighted row,
        # selected event and evidence pane always change atomically.
        self.table.currentCellChanged.connect(self._on_table_selection_changed)
        self.table.cellDoubleClicked.connect(self._open_preferred_source)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)
        results_layout.addWidget(self.table, 1)

        self.video_discovered_table = QTableWidget(0, 5, self)
        self.video_discovered_table.setObjectName("videoDiscoveredTable")
        self.video_discovered_table.setHorizontalHeaderLabels(
            ["视频发现", "批次", "视频位置", "帧号", "状态"]
        )
        self.video_discovered_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.video_discovered_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.video_discovered_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.video_discovered_table.verticalHeader().setVisible(False)
        self.video_discovered_table.setMaximumHeight(130)
        self.video_discovered_table.setVisible(False)
        self.video_discovered_table.itemSelectionChanged.connect(
            self._on_video_discovered_selected
        )
        results_layout.addWidget(self.video_discovered_table)

        transport = QFrame(self)
        transport.setObjectName("reviewPanel")
        transport.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        self.transport = transport
        self.transport_layout = QHBoxLayout(transport)
        self.transport_layout.setContentsMargins(10, 4, 10, 4)
        self.transport_layout.setSpacing(7)
        self.current_passage_label = QLabel("未选择通过记录")
        self.current_passage_label.setStyleSheet(
            "font-size: 12pt; font-weight: 700; color: #17212b;"
        )
        self.current_context_label = QLabel()
        self.current_context_label.setStyleSheet(
            "color: #667085; font-size: 9pt; font-weight: 600;"
        )
        self.batch_context_label = QLabel()
        self.batch_context_label.setStyleSheet(
            "color: #185f73; font-size: 9pt; font-weight: 700;"
        )
        self.batch_review_btn = QPushButton("连续判读", self)
        self.batch_review_btn.setToolTip(
            "按芯片时间从前向后连续播放，看到谁就点击号码并确认当前帧"
        )
        self.batch_review_btn.setVisible(True)
        self.batch_review_btn.clicked.connect(self._toggle_batch_mode)
        self.current_time_label = QLabel("--:--:--.---")
        self.current_time_label.setStyleSheet(
            "font-family: Consolas; font-size: 10pt; font-weight: 700;"
        )
        self.target_position_btn = QPushButton("目标", self)
        self.target_position_btn.setToolTip("跳转到当前运动员的预计过线时间")
        self.preview_mark_btn = QPushButton("预览标线", self)
        self.preview_mark_btn.setCheckable(True)
        self.preview_mark_btn.setToolTip("直接在顶部预览画面上点击运动员位置")
        self.preview_confirm_btn = QPushButton("预览确认", self)
        self.preview_confirm_btn.setToolTip("确认顶部预览画面上的当前标线")
        self.preview_confirm_btn.setEnabled(False)
        self.previous_batch_btn = QPushButton("前一屏")
        self.previous_batch_btn.setToolTip("向前浏览时间胶卷，机位 1 保持当前帧")
        self.previous_batch_btn.setFixedWidth(58)
        self.previous_batch_btn.clicked.connect(
            lambda: self._move_filmstrip_batch(-1)
        )
        self.next_batch_btn = QPushButton("后一屏")
        self.next_batch_btn.setToolTip("向后浏览时间胶卷，机位 1 保持当前帧")
        self.next_batch_btn.setFixedWidth(58)
        self.next_batch_btn.clicked.connect(
            lambda: self._move_filmstrip_batch(1)
        )
        self.previous_passage_btn = QPushButton("上一条")
        self.previous_passage_btn.setToolTip("上一条")
        self.previous_frame_btn = QPushButton("|◀")
        self.previous_frame_btn.setToolTip("上一帧")
        self.play_both_btn = QPushButton("联动 ▶")
        self.play_both_btn.setToolTip("同时播放或暂停全部画面")
        self.next_frame_btn = QPushButton("▶|")
        self.next_frame_btn.setToolTip("下一帧")
        self.next_passage_btn = QPushButton("下一条")
        self.next_passage_btn.setToolTip("下一条")
        self.fullscreen_btn = QPushButton("全屏")
        self.fullscreen_btn.setToolTip("全屏显示整个复核窗口（F11）")
        self.fullscreen_btn.setFixedWidth(48)
        for button in (
            self.previous_passage_btn,
            self.previous_frame_btn,
            self.play_both_btn,
            self.next_frame_btn,
            self.next_passage_btn,
        ):
            button.setFixedWidth(36)
        self.play_both_btn.setFixedWidth(68)
        self.previous_passage_btn.setFixedWidth(58)
        self.next_passage_btn.setFixedWidth(58)
        self.auto_advance_checkbox = QCheckBox("确认后下一条")
        self.auto_advance_checkbox.setChecked(False)
        self.auto_advance_checkbox.setToolTip(
            "连续判读时确认后选择下一条；密集通过保持当前帧，长空档才向前跳转"
        )
        self.previous_passage_btn.clicked.connect(lambda: self._move_selection(-1))
        self.previous_frame_btn.setToolTip("当前画面上一帧")
        self.next_frame_btn.setToolTip("当前画面下一帧")
        self.previous_frame_btn.clicked.connect(lambda: self._step_active_pane(-1))
        self.play_both_btn.clicked.connect(self._toggle_both)
        self.next_frame_btn.clicked.connect(lambda: self._step_active_pane(1))
        self.next_passage_btn.clicked.connect(lambda: self._move_selection(1))
        self.target_position_btn.clicked.connect(self._seek_to_target_position)
        self.fullscreen_btn.clicked.connect(self._toggle_fullscreen)
        self.review_more_btn = QPushButton("更多", self)
        self.review_more_menu = QMenu(self.review_more_btn)
        self.review_more_btn.setMenu(self.review_more_menu)
        self._review_more_actions = []
        for control, title in (
            (self.target_position_btn, "定位到预计过线"),
            (self.previous_frame_btn, "上一帧（←）"),
            (self.next_frame_btn, "下一帧（→）"),
            (self.play_both_btn, ""),
            (self.fullscreen_btn, ""),
            (self.auto_advance_checkbox, ""),
            (self.preview_mark_btn, ""),
            (self.preview_confirm_btn, ""),
        ):
            control.setParent(transport)
            control.hide()
            action = self.review_more_menu.addAction(title or control.text())
            action.setCheckable(control.isCheckable())
            action.triggered.connect(
                lambda _checked=False, button=control: button.click()
            )
            self._review_more_actions.append((action, control, title))
        self.review_more_menu.aboutToShow.connect(self._update_review_more_actions)
        self.transport_layout.addWidget(self.current_passage_label)
        self.transport_layout.addWidget(self.current_context_label)
        self.transport_layout.addWidget(self.batch_context_label)
        self.transport_layout.addWidget(self.batch_review_btn)
        self.transport_layout.addStretch(1)
        self.transport_layout.addWidget(self.current_time_label)
        self.previous_batch_btn.hide()
        self.next_batch_btn.hide()
        self.transport_layout.addWidget(self.previous_passage_btn)
        self.transport_layout.addWidget(self.next_passage_btn)
        self.transport_layout.addWidget(self.review_more_btn)

        self.evidence_splitter = QSplitter(Qt.Horizontal)
        self.evidence_splitter.setChildrenCollapsible(False)
        self.evidence_splitter.setHandleWidth(5)
        self.evidence_splitter.setMinimumWidth(0)
        self.evidence_splitter.setSizePolicy(
            QSizePolicy.Ignored,
            QSizePolicy.Expanding,
        )
        for camera_index in self._configured_regular_camera_indexes:
            self._create_regular_pane(camera_index)
        self.regular_pane = self.regular_panes[0]
        self.high_speed_pane = PassageEvidencePane(
            "高速摄像", HIGH_SPEED_SOURCE, self
        )
        self._connect_evidence_pane(self.high_speed_pane)
        self._active_pane = self.regular_pane
        self.evidence_splitter.addWidget(self.high_speed_pane)
        self.configure_evidence_panes(
            self._configured_regular_camera_indexes,
            show_high_speed=self._show_high_speed_pane,
        )
        self._set_active_idle_prefetch(self._active_pane)

        preview_panel = QFrame(self)
        self.preview_panel = preview_panel
        # Reserve enough space for the transport row plus the complete
        # filmstrip. QSizePolicy.Ignored otherwise lets the outer splitter
        # compress this panel even when the inner filmstrip has a minimum.
        preview_panel.setMinimumSize(0, 420)
        preview_panel.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        preview_layout = QVBoxLayout(preview_panel)
        self.preview_layout = preview_layout
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_layout.setSpacing(6)
        preview_layout.addWidget(transport)
        self.video_filmstrip = VideoFilmstripWidget(self)
        self.video_filmstrip.enable_full_race()
        self.video_filmstrip.full_race.frame_requested.connect(self._open_race_filmstrip_frame)
        self.video_filmstrip.full_race.judgment_requested.connect(self._maximize_filmstrip_camera)
        self.video_filmstrip.full_race.saved_judgment_requested.connect(self._open_camera_judgment)
        self.video_filmstrip.full_race.refresh_requested.connect(self._update_filmstrip)
        self.video_filmstrip.full_race.enlarge_requested.connect(self._toggle_filmstrip_size)
        self.video_filmstrip.judgment_track.show()
        self.video_filmstrip.judgment_track.judgment_requested.connect(
            self._open_camera_judgment
        )
        self.video_filmstrip.setVisible(True)
        self.preview_video_view = EvidenceImageView(self)
        self.preview_video_view.setMinimumHeight(260)
        self.preview_video_view.setSizePolicy(
            QSizePolicy.Expanding,
            QSizePolicy.Expanding,
        )
        self.preview_video_view.clear_frame("选择一条通过记录后在这里预览判读")
        self.preview_video_view.marker_position_selected.connect(
            self._on_preview_marker_selected
        )
        self.preview_video_view.marker_confirm_requested.connect(
            self._confirm_preview_marker
        )
        self.preview_video_view.frame_step_requested.connect(
            self._step_active_pane
        )
        self.preview_video_view.passage_step_requested.connect(
            self._move_selection
        )
        self.preview_video_view.activated.connect(
            lambda: self._activate_pane(self._active_playback_pane(), align=False)
        )
        preview_layout.addWidget(self.preview_video_view, 4)
        self.preview_mark_btn.toggled.connect(self.video_filmstrip.set_marker_mode)
        self.preview_mark_btn.toggled.connect(self.preview_video_view.set_marker_mode)
        self.preview_confirm_btn.clicked.connect(self._confirm_filmstrip_marker)
        self.video_filmstrip.position_selected.connect(
            self._seek_filmstrip_position
        )
        self.video_filmstrip.scrub_position_changed.connect(
            self._preview_filmstrip_position
        )
        self.video_filmstrip.position_double_clicked.connect(
            self._open_filmstrip_position_for_judgment
        )
        self.video_filmstrip.marker_position_selected.connect(
            self._on_filmstrip_marker_selected
        )
        self.video_filmstrip.confirm_button.clicked.connect(
            self._confirm_filmstrip_marker
        )
        self.video_filmstrip.cancel_button.clicked.connect(
            self._clear_filmstrip_marker
        )
        self.video_filmstrip.reload_requested.connect(self._update_filmstrip)
        # Let the vertical splitter allocate space to actual filmstrip images.
        self.video_filmstrip.setMinimumHeight(360)
        self.playback_coordinator = PlaybackCoordinator(
            None,
            self,
            filmstrip_update_delay_ms=self.INACTIVE_CAMERA_START_DELAY_MS + 80,
        )
        self.playback_coordinator.filmstrip_update_requested.connect(
            self._update_filmstrip
        )
        self.video_filmstrip.ready_changed.connect(self._on_filmstrip_ready)
        preview_layout.addWidget(self.video_filmstrip, 1)
        self.preview_timeline = TargetTimelineSlider(Qt.Horizontal, self)
        self.preview_timeline.setRange(0, 0)
        self.preview_timeline.setEnabled(False)
        self.preview_timeline.setFixedHeight(22)
        self.preview_timeline.hide()
        self.preview_timeline.setToolTip("拖动时间线快速定位，松开后同步到机位 1")
        self.preview_timeline.sliderPressed.connect(
            self._on_preview_timeline_pressed
        )
        self.preview_timeline.sliderMoved.connect(
            self._on_preview_timeline_moved
        )
        self.preview_timeline.sliderReleased.connect(
            self._on_preview_timeline_released
        )
        # The camera pane already exposes the authoritative frame timeline;
        # keep this preview slider for compatibility but do not render a
        # duplicate control beneath the filmstrip.
        self.preview_video_view.setVisible(self._top_preview_video_visible)
        self._update_review_more_actions()
        if not self._top_preview_video_visible:
            # A hidden expanding graphics view can still leave excess space in
            # nested Qt layouts. Collapse it explicitly so the filmstrip sits
            # directly below the transport bar.
            self.preview_video_view.setMinimumHeight(0)
            self.preview_video_view.setMaximumHeight(0)
            self.preview_video_view.setSizePolicy(
                QSizePolicy.Ignored,
                QSizePolicy.Ignored,
            )
            self.preview_layout.setStretch(1, 1)
        # In filmstrip-first mode the judgment controls belong to the filmstrip
        # header, while the duplicate preview controls stay hidden with it.

        # Bottom row: the athlete order list stays on the left, while all
        # camera panes remain grouped on the right.
        self.workspace_splitter = QSplitter(Qt.Horizontal, self)
        self.workspace_splitter.setChildrenCollapsible(False)
        self.workspace_splitter.setHandleWidth(5)
        # Keep the judgment row usable when the horizontal divider is dragged
        # downward. Without a vertical minimum, the camera image and its
        # frame controls can be compressed into a thin, broken-looking strip.
        self.workspace_splitter.setMinimumSize(0, 180 if self._uses_popup_judging() else 320)
        self.workspace_splitter.addWidget(results_panel)
        self.workspace_splitter.addWidget(self.evidence_splitter)
        results_panel.setMinimumWidth(0)
        results_panel.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        self.workspace_splitter.setStretchFactor(0, 1)
        self.workspace_splitter.setStretchFactor(1, 1)
        self.workspace_splitter.setSizes([30, 70])

        self.review_content_splitter = QSplitter(Qt.Vertical, self)
        self.review_content_splitter.setChildrenCollapsible(False)
        self.review_content_splitter.setHandleWidth(6)
        self.review_content_splitter.setStyleSheet(
            "QSplitter::handle:vertical { background: #edf1f4; border-top: 1px solid #cbd5e1; }"
            "QSplitter::handle:vertical:hover { background: #cbd5e1; }")
        self.review_content_splitter.setMinimumSize(0, 0)
        self.review_content_splitter.addWidget(preview_panel)
        self.review_content_splitter.addWidget(self.workspace_splitter)
        self.review_content_splitter.setStretchFactor(0, 1)
        self.review_content_splitter.setStretchFactor(1, 1)
        # Full-width preview above the bottom athlete/camera row.
        self.review_content_splitter.setSizes([60, 40])
        self._review_split_manually_sized = False
        self._filmstrip_restore_sizes = None
        self.review_content_splitter.splitterMoved.connect(self._review_divider_moved)
        self.review_content_splitter.handle(1).setToolTip("上下拖动：调整时间胶卷与名单的大小")
        layout.addWidget(self.review_content_splitter, 1)
        # Nested splitters are not laid out yet during construction.  Apply
        # the initial proportions after the dialog receives its real size;
        # otherwise Qt may clamp the evidence area to a few pixels because
        # the results table has a wide horizontal size hint.
        # The real dimensions are available after the dialog is shown.

    @property
    def regular_panes(self) -> tuple[PassageEvidencePane, ...]:
        return tuple(
            self._regular_panes_by_camera[camera_index]
            for camera_index in self._configured_regular_camera_indexes
        )

    @property
    def evidence_panes(self) -> tuple[PassageEvidencePane, ...]:
        panes = list(self.regular_panes)
        if self._show_high_speed_pane:
            panes.append(self.high_speed_pane)
        return tuple(panes)

    @property
    def all_evidence_panes(self) -> tuple[PassageEvidencePane, ...]:
        panes = list(self._regular_panes_by_camera.values())
        if hasattr(self, "high_speed_pane"):
            panes.append(self.high_speed_pane)
        return tuple(panes)

    def set_workbench_compact_mode(
        self,
        enabled: bool,
        *,
        allow_multi_camera_maximize: bool = False,
    ) -> None:
        """Let the main window switch the evidence panes into workbench mode.

        The main thread owns *when* the problem workbench is open; this surface
        only applies the compact picture controls.  A multi-camera review may
        keep its explicit enlarge button through the keyword argument.
        """

        self._workbench_compact_mode = bool(enabled)
        self._workbench_allow_multi_camera_maximize = bool(
            allow_multi_camera_maximize
        )
        self._apply_workbench_compact_controls()

    def _apply_workbench_compact_controls(self) -> None:
        enabled = self._workbench_compact_mode
        allow_maximize = (
            enabled
            and self._workbench_allow_multi_camera_maximize
            and len(self.evidence_panes) > 1
        )
        visible = set(self.evidence_panes)
        for pane in self.all_evidence_panes:
            pane.set_workbench_compact(
                enabled,
                allow_maximize=allow_maximize and pane in visible,
            )

    def _connect_evidence_pane(self, pane: PassageEvidencePane) -> None:
        if pane.source_kind == REGULAR_SOURCE:
            pane.open_requested.connect(self._open_location_if_available)
        pane.step_requested.connect(
            lambda delta, current=pane: self._step_pane(current, delta)
        )
        pane.step_boundary_reached.connect(
            lambda direction, current=pane: self._step_across_recording_boundary(
                current, direction, at_edge=True
            )
        )
        pane.playback_reached_end.connect(
            lambda current=pane: self._continue_playback(current)
        )
        pane.play_requested.connect(lambda current=pane: self._toggle_pane(current))
        pane.detail_requested.connect(self._on_pane_detail_requested)
        pane.passage_delta_requested.connect(
            lambda delta, current=pane: self._seek_pane_delta(
                current, delta, preview=False
            )
        )
        pane.scrub_preview_requested.connect(
            lambda delta, current=pane: self._seek_pane_delta(
                current, delta, preview=True
            )
        )
        pane.scrub_started.connect(
            lambda current=pane: self._on_pane_scrub_started(current)
        )
        pane.position_changed.connect(
            lambda delta, current=pane: self._on_pane_position_changed(
                current, delta
            )
        )
        pane.preview_frame_ready.connect(
            lambda image, position_ms, frame_index, current=pane:
            self._on_preview_frame_ready(
                current,
                image,
                position_ms,
                frame_index,
            )
        )
        pane.initial_frame_ready.connect(
            lambda event_id, current=pane: self._on_pane_initial_frame_ready(
                current,
                event_id,
            )
        )
        pane.selection_step_requested.connect(self._move_selection)
        pane.maximize_requested.connect(self._toggle_maximized_pane)
        pane.video_view.activated.connect(
            lambda current=pane: self._activate_pane(current, align=True)
        )
        pane.video_view.batch_event_requested.connect(
            lambda event_id, current=pane: self._select_batch_event_at_current_frame(
                event_id,
                current,
            )
        )
        pane.marking_requested.connect(self._begin_marking)
        pane.confirmation_requested.connect(self._confirm_pending_marker)
        pane.cancel_requested.connect(self._cancel_pending_marker)
        pane.delete_requested.connect(self._delete_marker)

    def _create_regular_pane(self, camera_index: int) -> PassageEvidencePane:
        camera_index = max(1, int(camera_index))
        existing = self._regular_panes_by_camera.get(camera_index)
        if existing is not None:
            return existing
        pane = PassageEvidencePane(
            f"机位 {camera_index}",
            REGULAR_SOURCE,
            self,
            camera_index=camera_index,
            low_resource_mode=self._low_resource_mode,
        )
        pane.camera_combo.hide()
        self._connect_evidence_pane(pane)
        self._regular_panes_by_camera[camera_index] = pane
        if hasattr(self, "high_speed_pane"):
            high_speed_index = self.evidence_splitter.indexOf(self.high_speed_pane)
            self.evidence_splitter.insertWidget(max(0, high_speed_index), pane)
        else:
            self.evidence_splitter.addWidget(pane)
        self.evidence_pane_added.emit(pane)
        if self._workbench_compact_mode:
            self._apply_workbench_compact_controls()
        return pane

    def configure_evidence_panes(
        self,
        regular_camera_indexes: Iterable[int],
        *,
        show_high_speed: bool,
        include_recorded: bool = False,
    ) -> None:
        normalized_indexes, resolved_high_speed = _resolve_evidence_layout(
            self.timeline_store,
            regular_camera_indexes=regular_camera_indexes,
            show_high_speed_pane=show_high_speed,
            include_recorded=include_recorded,
        )
        self._set_sync_playing(False)
        self._restore_maximized_pane()
        for camera_index in normalized_indexes:
            self._create_regular_pane(camera_index)
        self._configured_regular_camera_indexes = normalized_indexes
        self._show_high_speed_pane = resolved_high_speed
        self.regular_pane = self.regular_panes[0]
        if self._active_pane not in self.evidence_panes:
            self._active_pane = self.regular_pane
        self._set_active_idle_prefetch(self._active_pane)

        active_regular = set(normalized_indexes)
        for camera_index, pane in self._regular_panes_by_camera.items():
            active = camera_index in active_regular
            if not active:
                pane.clear_passage()
            pane.setVisible(active)
        if not self._show_high_speed_pane:
            self.high_speed_pane.clear_passage()
        self.high_speed_pane.setVisible(self._show_high_speed_pane)

        compact = len(self.evidence_panes) >= 3
        for pane in self.all_evidence_panes:
            pane.set_compact_controls(compact and pane in self.evidence_panes)
            pane.maximize_btn.setText("放大")
            pane.maximize_btn.setToolTip("放大该机位（双击画面或按 F）")
        if self._workbench_compact_mode:
            self._apply_workbench_compact_controls()
        for index in range(self.evidence_splitter.count()):
            self.evidence_splitter.setStretchFactor(index, 1)
        self._distribute_evidence_panes()
        QTimer.singleShot(0, self._distribute_evidence_panes)
        if hasattr(self, "workspace_splitter"):
            self._set_continuous_review_layout(self._batch_mode)

    def _on_offset_changed(self, value: int) -> None:
        self.clock_offset_ms = int(value)
        if self._clock_offset_by_camera:
            for camera_index in self._configured_regular_camera_indexes:
                self._clock_offset_by_camera[camera_index] = self.clock_offset_ms
        self.clock_offset_changed.emit(self.clock_offset_ms)
        self._lookup_cache.clear()
        self.refresh()

    def set_camera_clock_offset(self, camera_index: int, offset_ms: int) -> None:
        """Set a camera-specific review offset without changing race timing."""

        camera_index = max(1, int(camera_index))
        self._clock_offset_by_camera[camera_index] = int(offset_ms)
        self._lookup_cache.clear()
        self.refresh()

    def _clock_offset_for_camera(self, camera_index: int) -> int:
        return int(
            self._clock_offset_by_camera.get(
                max(1, int(camera_index)),
                self.clock_offset_ms,
            )
        )

    def _camera_one_pane(self) -> PassageEvidencePane:
        """Return the authoritative continuous-review pane (camera 1)."""

        by_camera = getattr(self, "_regular_panes_by_camera", {})
        pane = by_camera.get(1)
        if pane is not None:
            return pane
        # A camera-1 pane is the normal layout. Keep a safe fallback for
        # camera-less or legacy layouts used by integrations and tests.
        regular_panes = getattr(self, "regular_panes", ())
        if regular_panes:
            return regular_panes[0]
        return self.regular_pane

    def _continuous_offset_for_location(
        self,
        location: Optional[PassageVideoLocation],
    ) -> int:
        if location is None:
            return self._clock_offset_for_camera(1)
        camera_index = max(1, int(location.segment.camera_index))
        session_key = self._recording_session_key(location)
        return self._calibrated_clock_offset(camera_index, session_key)

    def _calibrated_clock_offset(self, camera_index: int, session_key: str) -> int:
        calibration = self.calibration_store.get(camera_index, session_key)
        if calibration is None:
            # Older versions keyed HLS calibrations by the generated window name.
            aliases = []
            for saved in self.calibration_store.calibrations():
                if saved.camera_index != camera_index:
                    continue
                parts = saved.session_key.split("|", 2)
                if len(parts) != 3:
                    continue
                path = Path(parts[1]) / (parts[2] + ".m3u8")
                if path.is_file() and self._recording_session_key_from_path(parts[0], path) == session_key:
                    aliases.append(saved)
            if aliases:
                calibration = max(aliases, key=lambda saved: saved.calibrated_at_ms)
        return int(
            self._continuous_clock_offsets.get(
                (camera_index, session_key),
                calibration.offset_ms if calibration is not None else self._clock_offset_for_camera(camera_index),
            )
        )

    def _on_group_changed(self) -> None:
        if self._batch_mode:
            self._exit_batch_mode()
        self._refresh_filtered_view()

    def _on_search_changed(self) -> None:
        if self._batch_mode:
            self._exit_batch_mode()
        self._search_refresh_timer.start()

    def _set_review_filter(self, filter_key: str) -> None:
        if filter_key not in self.review_filter_buttons:
            return
        if filter_key == self._active_review_filter:
            return
        if self._batch_mode:
            self._exit_batch_mode()
        self._active_review_filter = filter_key
        self._sync_review_filter_buttons()
        self._refresh_filtered_view()

    def _sync_review_filter_buttons(self) -> None:
        for filter_key, button in self.review_filter_buttons.items():
            active = filter_key == self._active_review_filter
            button.setChecked(active)
            button.setProperty("queueFilterActive", active)
            button.style().unpolish(button)
            button.style().polish(button)

    def _toggle_queue_expanded(self) -> None:
        sizes = self.workspace_splitter.sizes()
        total = sum(sizes)
        if total <= 0:
            return
        if self._queue_expanded:
            restore = self._queue_default_sizes or [310, max(1, total - 310)]
            self.workspace_splitter.setSizes(restore)
            self._queue_expanded = False
            self.queue_expand_btn.setToolTip("展开运动员列表")
            return
        self._queue_default_sizes = sizes
        queue_height = max(360, int(total * 0.58))
        self.workspace_splitter.setSizes([queue_height, max(1, total - queue_height)])
        self._queue_expanded = True
        self.queue_expand_btn.setToolTip("恢复运动员列表高度")

    def _matches_search(self, event: PassageEvent, query: str) -> bool:
        if not query:
            return True
        metadata_athlete = self._metadata_athlete_for_event(event)
        athlete_name = event.athlete_name.strip() or (
            metadata_athlete.name.strip() if metadata_athlete is not None else ""
        )
        return query in event.bib.strip().casefold() or query in athlete_name.casefold()

    def _association_for_event(
        self, event_id: str, source_kind: str,
    ) -> Optional[PassageEvidenceAssociation]:
        return self.association_store.get_for_event(
            self.passage_store.get(event_id), source_kind,
        )

    def _current_associations(self) -> tuple[PassageEvidenceAssociation, ...]:
        return tuple(
            association for association in self.association_store.associations()
            if association.matches_event(self.passage_store.get(association.passage_event_id))
        )

    def _review_status_for_event(
        self,
        event_id: str,
        lookup: PassageVideoLookup,
    ) -> str:
        regular = self._source_location_with_saved_association(
            event_id,
            lookup,
            high_speed=False,
        )
        high_speed = self._source_location_with_saved_association(
            event_id,
            lookup,
            high_speed=True,
        )
        # A manually confirmed rider may be found in the adjacent archive
        # segment rather than the segment selected from the official passage
        # timestamp. The saved association is authoritative for list status;
        # location matching is only needed when rendering a marker in a pane.
        regular_association = self._association_for_event(event_id, REGULAR_SOURCE)
        high_speed_association = self._association_for_event(event_id, HIGH_SPEED_SOURCE)
        fallback = review_status_text(lookup, regular, high_speed)
        return self._confirmation_status(
            regular_association,
            high_speed_association,
            fallback,
        )

    def _matches_review_filter(self, status: str) -> bool:
        if self._active_review_filter == "all":
            return True
        if self._active_review_filter == "confirmed":
            return status == "已确认"
        if self._active_review_filter == "blocked":
            return status == "受阻"
        return status == "待核对"

    def _bound_regular_locations(
        self,
        event: PassageEvent,
    ) -> tuple[PassageVideoLocation, ...]:
        store = self.review_binding_store
        if store is None:
            return ()
        locations = []
        for binding in store.active_bindings(event.event_id, event.revision):
            clip = store.get_clip(binding.clip_id)
            if clip is None:
                continue
            segment = self.timeline_store.get_segment(clip.timeline_segment_id)
            if segment is None:
                continue
            video_path = self.timeline_store.resolve_video_path(segment)
            camera_offset = self._clock_offset_for_camera(
                segment.camera_index
            )
            passage_position_ms = max(
                0,
                binding.passage_offset_ms + camera_offset,
            )
            if segment.media_duration_ms is not None:
                passage_position_ms = min(
                    passage_position_ms,
                    segment.media_duration_ms,
                )
            status = (
                "located"
                if self.timeline_store.video_path_is_playable(video_path)
                else "missing_file"
            )
            locations.append(
                PassageVideoLocation(
                    segment=segment,
                    video_path=video_path,
                    passage_position_ms=passage_position_ms,
                    playback_position_ms=max(
                        0,
                        passage_position_ms - self.pre_roll_ms,
                    ),
                    clock_offset_ms=camera_offset,
                    timing_error_ms=segment.timing_error_ms,
                    status=status,
                    media_locator=clip.clip_id,
                )
            )
        return tuple(sorted(locations, key=lambda item: item.segment.camera_index))

    @staticmethod
    def _recording_session_key_from_path(source_id: str, video_path: Path) -> str:
        from .runtime_paths import recording_session_path
        path = recording_session_path(Path(video_path).absolute())
        stem = _ARCHIVE_SEGMENT_SUFFIX_RE.sub("", path.stem)
        match = re.fullmatch(
            r"(camera_\d+)_\d{8}_\d{6}_\d{6}(?:_review_\d+)?", stem,
        )
        parent = path.parent
        if match is not None:
            stem = re.sub(r"_review_\d+$", "", stem)
            if parent.parent.name == "review_buffer":
                parent = parent.parent.parent / "videos"
            source_id = match.group(1) + "_review"
        return f"{source_id}|{parent}|{stem}"

    @classmethod
    def _recording_session_key(cls, location: PassageVideoLocation) -> str:
        return cls._recording_session_key_from_path(
            location.segment.source_id,
            location.video_path,
        )

    def _seed_continuous_clock_offsets(self) -> None:
        events_by_id = {
            event.event_id: event
            for event in self._events_for_current_metadata(
                self.passage_store.events()
            )
        }
        changed = False
        for calibration in self.calibration_store.calibrations():
            parts = calibration.session_key.split("|", 2)
            session_key = (
                self._recording_session_key_from_path(parts[0], Path(parts[1]) / parts[2])
                if len(parts) == 3 else calibration.session_key
            )
            key = (calibration.camera_index, session_key)
            if key not in self._continuous_clock_offsets:
                self._continuous_clock_offsets[key] = int(calibration.offset_ms)
                changed = True
        associations = sorted(
            self._current_associations(),
            key=lambda item: (
                events_by_id.get(item.passage_event_id).timeline_timestamp_ms
                if item.passage_event_id in events_by_id
                else 2**63 - 1,
                item.confirmed_at_ms,
            ),
        )
        for association in associations:
            if association.confirmed_source != REGULAR_SOURCE:
                continue
            event = events_by_id.get(association.passage_event_id)
            segment = self.timeline_store.get_segment(association.segment_id)
            if (
                event is None
                or segment is None
                or segment.media_started_at_ms is None
            ):
                continue
            continuous_lookup = self.timeline_store.locate_passage(
                event.timeline_timestamp_ms,
                clock_offset_ms=self._clock_offset_for_camera(
                    segment.camera_index
                ),
                pre_roll_ms=self.CONTINUOUS_SKIP_LEAD_MS,
                race_id=event.race_id,
                prefer_continuous_media=True,
            )
            continuous_location = self._regular_location_for_camera(
                continuous_lookup,
                segment.camera_index,
            )
            session_segment = (
                continuous_location.segment
                if continuous_location is not None
                else segment
            )
            video_path = (
                continuous_location.video_path
                if continuous_location is not None
                else self.timeline_store.resolve_video_path(segment)
            )
            key = (
                session_segment.camera_index,
                self._recording_session_key_from_path(
                    session_segment.source_id,
                    video_path,
                ),
            )
            if key in self._continuous_clock_offsets:
                continue
            self._continuous_clock_offsets[key] = (
                int(segment.media_started_at_ms)
                + int(association.position_ms)
                - int(event.timeline_timestamp_ms)
            )
            changed = True
        if changed:
            self._continuous_calibration_revision += 1

    def _continuous_lookup_for_camera(
        self,
        event: PassageEvent,
        camera_index: int,
    ) -> PassageVideoLookup:
        base_offset = self._clock_offset_for_camera(camera_index)
        lookup = self.timeline_store.locate_passage(
            event.timeline_timestamp_ms,
            clock_offset_ms=base_offset,
            pre_roll_ms=self.CONTINUOUS_SKIP_LEAD_MS,
            race_id=event.race_id,
            prefer_continuous_media=True,
        )
        location = self._regular_location_for_camera(lookup, camera_index)
        if location is None:
            return lookup
        calibrated_offset = self._continuous_offset_for_location(location)
        if calibrated_offset == base_offset:
            return lookup
        return self.timeline_store.locate_passage(
            event.timeline_timestamp_ms,
            clock_offset_ms=calibrated_offset,
            pre_roll_ms=self.CONTINUOUS_SKIP_LEAD_MS,
            race_id=event.race_id,
            prefer_continuous_media=True,
        )

    def _lookup(self, event: PassageEvent) -> PassageVideoLookup:
        direct_locations = (
            () if self._batch_mode else self._bound_regular_locations(event)
        )
        configured = set(self._configured_regular_camera_indexes)
        direct_camera_indexes = {
            location.segment.camera_index
            for location in direct_locations
            if location.status in _OPENABLE_STATUSES
        }
        needs_fallback = not direct_locations or not configured.issubset(
            direct_camera_indexes
        )
        if needs_fallback:
            fallback_locations = []
            fallback_statuses = []
            for camera_index in self._configured_regular_camera_indexes:
                fallback = (
                    self._continuous_lookup_for_camera(event, camera_index)
                    if self._batch_mode
                    else self.timeline_store.locate_passage(
                        event.timeline_timestamp_ms,
                        clock_offset_ms=self._clock_offset_for_camera(camera_index),
                        pre_roll_ms=self.pre_roll_ms,
                        race_id=event.race_id,
                    )
                )
                fallback_statuses.append(fallback.status)
                fallback_locations.extend(
                    location
                    for location in fallback.locations
                    if location.segment.camera_index == camera_index
                )
            # Preserve recorded high-speed/external evidence that is not part
            # of the configured ordinary-camera set.
            external_camera_indexes = sorted(
                {
                    segment.camera_index
                    for segment in self.timeline_store.segments()
                    if segment.camera_index
                    not in self._configured_regular_camera_indexes
                }
            )
            for camera_index in external_camera_indexes:
                fallback = self.timeline_store.locate_passage(
                    event.timeline_timestamp_ms,
                    clock_offset_ms=self._clock_offset_for_camera(camera_index),
                    pre_roll_ms=self.pre_roll_ms,
                    race_id=event.race_id,
                )
                fallback_statuses.append(fallback.status)
                fallback_locations.extend(
                    location
                    for location in fallback.locations
                    if location.segment.camera_index == camera_index
                )
            locations_by_camera = {
                location.segment.camera_index: location
                for location in direct_locations
            }
            for location in fallback_locations:
                current = locations_by_camera.get(location.segment.camera_index)
                if current is None or (
                    current.status == "missing_file"
                    and location.status in _OPENABLE_STATUSES
                ):
                    locations_by_camera[location.segment.camera_index] = location
            locations = list(locations_by_camera.values())
            lookup_status = (
                "located"
                if fallback_locations
                else next(iter(fallback_statuses), "no_segments")
            )
        else:
            locations = list(direct_locations)
            lookup_status = "located"
        lookup = PassageVideoLookup(
            "located"
            if any(location.status == "located" for location in locations)
            else lookup_status,
            event.timeline_timestamp_ms + self.clock_offset_ms,
            tuple(sorted(locations, key=lambda item: item.segment.camera_index)),
        )
        locations = list(lookup.locations)
        if not self._include_recorded_evidence:
            locations = [
                location
                for location in locations
                if (
                    is_high_speed(location)
                    and self._show_high_speed_pane
                )
                or (
                    not is_high_speed(location)
                    and location.segment.camera_index in configured
                )
            ]
        if self._high_speed_locator is None or not self._show_high_speed_pane:
            if tuple(locations) == lookup.locations:
                return lookup
            status = (
                "located"
                if any(location.status == "located" for location in locations)
                else "near_boundary"
                if any(location.status == "near_boundary" for location in locations)
                else lookup.status
            )
            return PassageVideoLookup(
                status,
                event.timeline_timestamp_ms + self.clock_offset_ms,
                tuple(locations),
            )
        locations = [location for location in locations if not is_high_speed(location)]
        high_speed = self._high_speed_locator(
            event,
            self.clock_offset_ms,
            self.pre_roll_ms,
        )
        if high_speed is not None:
            locations.append(high_speed)
        if any(location.status == "located" for location in locations):
            status = "located"
        elif any(location.status == "near_boundary" for location in locations):
            status = "near_boundary"
        else:
            status = lookup.status
        return PassageVideoLookup(
            status,
            event.timeline_timestamp_ms + self.clock_offset_ms,
            tuple(locations),
        )

    def invalidate_external_locations(self) -> None:
        self._external_location_revision += 1
        self._lookup_cache.clear()
        self.refresh()

    def _timeline_cache_signature(self) -> tuple:
        return (self.timeline_store.revision,)

    @staticmethod
    def _cache_survives_timeline_update(lookup: PassageVideoLookup) -> bool:
        return bool(lookup.locations) and all(
            location.status != "preview" for location in lookup.locations
        )

    def _cached_lookup(self, event: PassageEvent) -> PassageVideoLookup:
        key = (
            event.revision,
            event.timeline_timestamp_ms,
            event.race_id,
            self.clock_offset_ms,
            self.pre_roll_ms,
            self._external_location_revision,
            tuple(self._configured_regular_camera_indexes),
            self._show_high_speed_pane,
            self._include_recorded_evidence,
            self._batch_mode,
            self._continuous_calibration_revision,
            self.review_binding_store.active_bindings(event.event_id, event.revision)
            if self.review_binding_store is not None
            else (),
        )
        cached = self._lookup_cache.get(event.event_id)
        if cached is not None and cached[0] == key:
            return cached[1]
        lookup = self._lookup(event)
        self._lookup_cache[event.event_id] = (key, lookup)
        return lookup

    def _source_association(
        self,
        event_id: str,
        source_kind: str,
        location: Optional[PassageVideoLocation],
    ) -> Optional[PassageEvidenceAssociation]:
        association = self._association_for_event(event_id, source_kind)
        if not _association_matches_location(association, location):
            return None
        return association

    def _source_location_with_saved_association(
        self,
        event_id: str,
        lookup: PassageVideoLookup,
        *,
        high_speed: bool,
    ) -> Optional[PassageVideoLocation]:
        source_kind = HIGH_SPEED_SOURCE if high_speed else REGULAR_SOURCE
        association = self._association_for_event(event_id, source_kind)
        if association is not None:
            associated = next(
                (
                    location
                    for location in lookup.locations
                    if is_high_speed(location) is bool(high_speed)
                    and _association_matches_location(association, location)
                ),
                None,
            )
            if associated is not None:
                return associated
            if not high_speed:
                saved_location = self._saved_regular_location(association)
                if saved_location is not None:
                    return saved_location
        return source_location(lookup, high_speed=high_speed)

    def _saved_regular_location(
        self,
        association: PassageEvidenceAssociation,
    ) -> Optional[PassageVideoLocation]:
        """Resolve the exact recording segment stored by a confirmation."""

        segment = self.timeline_store.get_segment(association.segment_id)
        if segment is None or segment.clock_source == AUYAT_CLOCK_SOURCE:
            return None
        video_path = self.timeline_store.resolve_video_path(segment)
        duration_ms = int(
            segment.media_duration_ms
            if segment.media_duration_ms is not None
            else max(
                0,
                int(segment.ended_at_ms or segment.started_at_ms)
                - int(segment.started_at_ms),
            )
        )
        position_ms = max(0, int(association.position_ms))
        if duration_ms > 0:
            position_ms = min(position_ms, duration_ms)
        return PassageVideoLocation(
            segment=segment,
            video_path=video_path,
            passage_position_ms=position_ms,
            playback_position_ms=max(0, position_ms - self.pre_roll_ms),
            clock_offset_ms=self._clock_offset_for_camera(segment.camera_index),
            timing_error_ms=segment.timing_error_ms,
            status=(
                "located"
                if self.timeline_store.video_path_is_playable(video_path)
                else "missing_file"
            ),
        )

    @staticmethod
    def _confirmation_status(
        regular: Optional[PassageEvidenceAssociation],
        high_speed: Optional[PassageEvidenceAssociation],
        fallback: str,
    ) -> str:
        if regular is not None or high_speed is not None:
            return "已确认"
        return fallback

    @staticmethod
    def _display_confirmation_status(status: str) -> str:
        return "已确认" if status == "已确认" else "未确认"

    @staticmethod
    def _saved_delta_ms(
        regular_location: Optional[PassageVideoLocation],
        high_speed_location: Optional[PassageVideoLocation],
        regular: Optional[PassageEvidenceAssociation],
        high_speed: Optional[PassageEvidenceAssociation],
    ) -> int:
        candidates = []
        if regular is not None and regular_location is not None:
            candidates.append(
                (
                    regular.confirmed_at_ms,
                    regular.position_ms - regular_location.passage_position_ms,
                )
            )
        if high_speed is not None and high_speed_location is not None:
            candidates.append(
                (
                    high_speed.confirmed_at_ms,
                    high_speed.position_ms - high_speed_location.passage_position_ms,
                )
            )
        if not candidates:
            return 0
        return int(max(candidates)[1])

    def _current_metadata(self) -> Optional[RaceMetadata]:
        if self.metadata_store is None:
            return None
        return self.metadata_store.current()

    def _events_for_current_metadata(
        self,
        events: tuple[PassageEvent, ...],
    ) -> tuple[PassageEvent, ...]:
        metadata = self._current_metadata()
        if metadata is None:
            filtered = events
        else:
            filtered = tuple(
                event
                for event in events
                if event.race_id == metadata.race_id
                and event.stage_id == metadata.stage_id
            )
        return tuple(
            sorted(
                filtered,
                key=lambda event: (
                    event.timeline_timestamp_ms,
                    event.event_id,
                ),
            )
        )

    def _ensure_metadata_athlete_index(
        self,
        metadata: RaceMetadata,
    ) -> None:
        if metadata is self._indexed_metadata:
            return
        athletes_by_id: dict[str, RaceAthleteMetadata] = {}
        athletes_by_identity: dict[str, RaceAthleteMetadata] = {}
        for athlete in metadata.athletes:
            athletes_by_id[athlete.athlete_id] = athlete
            for identity in (athlete.bib, *athlete.chip_ids):
                key = identity.strip().casefold()
                if key:
                    athletes_by_identity.setdefault(key, athlete)
        self._indexed_metadata = metadata
        self._metadata_athletes_by_id = athletes_by_id
        self._metadata_athletes_by_identity = athletes_by_identity

    def _metadata_athlete_for_event(
        self,
        event: PassageEvent,
    ) -> Optional[RaceAthleteMetadata]:
        metadata = self._current_metadata()
        if metadata is None:
            return None
        self._ensure_metadata_athlete_index(metadata)
        if event.athlete_id:
            athlete = self._metadata_athletes_by_id.get(event.athlete_id)
            if athlete is not None:
                return athlete
        for identity in (event.bib, event.chip_id):
            athlete = self._metadata_athletes_by_identity.get(
                identity.strip().casefold()
            )
            if athlete is not None:
                return athlete
        return None

    def _resize_group_popup(self) -> None:
        metrics = QFontMetrics(self.group_combo.font())
        widest_text = max(
            (
                metrics.horizontalAdvance(self.group_combo.itemText(index))
                for index in range(self.group_combo.count())
            ),
            default=0,
        )
        popup_width = min(360, max(self.group_combo.minimumWidth(), widest_text + 44))
        self.group_combo.view().setMinimumWidth(popup_width)
        for index in range(self.group_combo.count()):
            self.group_combo.setItemData(
                index,
                self.group_combo.itemText(index),
                Qt.ToolTipRole,
            )

    def _update_group_combo(self, events: tuple[PassageEvent, ...]) -> bool:
        previous_group = str(self.group_combo.currentData() or "")
        group_labels = {
            event.group_id: event.group_name.strip() or event.group_id
            for event in events
        }
        metadata = self._current_metadata()
        if metadata is not None:
            group_labels.update(
                {group.group_id: group.name for group in metadata.groups}
            )
        expected_items = [
            (group_id, group_label)
            for group_id, group_label in sorted(
                group_labels.items(), key=lambda item: (item[1], item[0])
            )
        ]
        current_items = [
            (
                str(self.group_combo.itemData(index) or ""),
                self.group_combo.itemText(index),
            )
            for index in range(1, self.group_combo.count())
        ]
        if current_items == expected_items:
            self._resize_group_popup()
            return False
        self.group_combo.blockSignals(True)
        self.group_combo.clear()
        self.group_combo.addItem("全部组别", "")
        for group_id, group_label in expected_items:
            self.group_combo.addItem(group_label, group_id)
        group_index = self.group_combo.findData(previous_group)
        self.group_combo.setCurrentIndex(max(0, group_index))
        self.group_combo.blockSignals(False)
        self._resize_group_popup()
        return str(self.group_combo.currentData() or "") != previous_group

    def _write_event_row(
        self,
        row: int,
        event: PassageEvent,
        lookup: PassageVideoLookup,
    ) -> None:
        regular = self._source_location_with_saved_association(
            event.event_id,
            lookup,
            high_speed=False,
        )
        high_speed = self._source_location_with_saved_association(
            event.event_id,
            lookup,
            high_speed=True,
        )
        readiness_status = review_status_text(lookup, regular, high_speed)
        regular_association = self._source_association(
            event.event_id, REGULAR_SOURCE, regular
        )
        high_speed_association = self._source_association(
            event.event_id, HIGH_SPEED_SOURCE, high_speed
        )
        review_status = self._confirmation_status(
            regular_association,
            high_speed_association,
            readiness_status,
        )
        display_review_status = self._display_confirmation_status(review_status)
        self._event_review_statuses[event.event_id] = review_status
        self._record_summary_state(
            event.event_id,
            regular,
            high_speed,
            readiness_status,
            regular_association,
            high_speed_association,
        )
        metadata_athlete = self._metadata_athlete_for_event(event)
        identity = event.bib.strip() or "未知"
        athlete_name = event.athlete_name.strip() or (
            metadata_athlete.name.strip() if metadata_athlete is not None else ""
        ) or "--"
        values = (
            str(row + 1),
            identity,
            athlete_name,
            event.group_name.strip() or event.group_id,
            str(event.lap),
            format_passage_time(event.timeline_timestamp_ms),
            source_confirmation_status(regular, regular_association),
            source_confirmation_status(high_speed, high_speed_association),
            display_review_status,
        )
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            if column in {0, 4, 5, 6, 7, 8}:
                item.setTextAlignment(Qt.AlignCenter)
            if column == 0:
                item.setData(Qt.UserRole, event.event_id)
            if column in {6, 7, 8}:
                self._apply_status_style(item, value)
            self.table.setItem(row, column, item)

    def _renumber_visible_rows(self) -> None:
        for row, event in enumerate(self._visible_events):
            item = self.table.item(row, 0)
            if item is None:
                item = QTableWidgetItem()
                item.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(row, 0, item)
            item.setText(str(row + 1))
            item.setData(Qt.UserRole, event.event_id)

    def _render_filtered_queue(
        self,
        events: tuple[PassageEvent, ...],
        previous_event_id: str,
        changed_event_ids: set[str] | None = None,
    ) -> None:
        previous_ids = [event.event_id for event in self._visible_events]
        scroll = self.table.verticalScrollBar().value()
        if self._batch_mode:
            self._visible_events = list(events)
        else:
            selected_group = str(self.group_combo.currentData() or "")
            query = self.identity_search.text().strip().casefold()
            self._visible_events = [
                event
                for event in events
                if (not selected_group or event.group_id == selected_group)
                and self._matches_search(event, query)
                and self._matches_review_filter(
                    self._event_review_statuses.get(event.event_id, "待核对")
                )
            ]

        self.table.blockSignals(True)
        self.table.setUpdatesEnabled(False)
        desired_ids = [event.event_id for event in self._visible_events]
        order_changed = previous_ids != desired_ids
        selected_row = -1
        try:
            if changed_event_ids is None:
                self.table.setRowCount(len(self._visible_events))
                for row, event in enumerate(self._visible_events):
                    self._write_event_row(row, event, self._lookups[event.event_id])
            else:
                desired = set(desired_ids)
                for row in range(len(previous_ids) - 1, -1, -1):
                    if previous_ids[row] not in desired:
                        self.table.removeRow(row)
                        previous_ids.pop(row)
                for row, event in enumerate(self._visible_events):
                    inserted = row >= len(previous_ids) or previous_ids[row] != event.event_id
                    if inserted:
                        if event.event_id in previous_ids:
                            old_row = previous_ids.index(event.event_id)
                            self.table.removeRow(old_row)
                            previous_ids.pop(old_row)
                        self.table.insertRow(row)
                        previous_ids.insert(row, event.event_id)
                    if inserted or event.event_id in changed_event_ids:
                        self._write_event_row(row, event, self._lookups[event.event_id])
                if order_changed:
                    self._renumber_visible_rows()
            if changed_event_ids is None or order_changed:
                self._event_row_by_id = {event_id: row for row, event_id in enumerate(desired_ids)}
            selected_row = self._event_row_by_id.get(previous_event_id, -1)
        finally:
            self.table.setUpdatesEnabled(True)

        if selected_row < 0 and self._visible_events:
            selected_row = 0
        if selected_row >= 0 and (changed_event_ids is None or order_changed
                                  or self.table.currentRow() != selected_row):
            self.table.setCurrentCell(selected_row, 1)
            self.table.selectRow(selected_row)
        self.table.blockSignals(False)
        self.table.schedule_auto_fit()
        self.delete_record_button.setEnabled(selected_row >= 0)
        self.table.verticalScrollBar().setValue(scroll)

        self._render_summary()
        if selected_row >= 0:
            selected_event_id = self._visible_events[selected_row].event_id
            active_pane = self._camera_one_pane()
            preserve_current_frame = (
                active_pane
                if selected_event_id == self._selected_event_id
                and getattr(active_pane, "_current_frame_index", -1) >= 0
                else None
            )
            if (changed_event_ids is None or selected_event_id != self._selected_event_id
                    or selected_event_id in changed_event_ids):
                self._select_event(
                    selected_event_id, preserve_current_frame=preserve_current_frame,
                )
        else:
            self._clear_selection_details()

    def _active_review_batch(self) -> Optional[PassageReviewBatch]:
        if not self._active_review_batch_id:
            return None
        return next(
            (
                batch
                for batch in self._review_batches
                if batch.batch_id == self._active_review_batch_id
            ),
            None,
        )

    def _update_review_more_actions(self) -> None:
        for action, control, title in self._review_more_actions:
            action.setText(title or control.text())
            action.setEnabled(control.isEnabled())
            action.setChecked(control.isChecked())
            action.setToolTip(control.toolTip())
            action.setVisible(
                control not in (self.preview_mark_btn, self.preview_confirm_btn)
                or self._top_preview_video_visible
            )

    def _update_batch_controls(self, event_id: str = "") -> None:
        batch = self._review_batch_by_event_id.get(str(event_id))
        if not self._visible_events:
            self.batch_context_label.clear()
            self.batch_context_label.setToolTip("")
            self.batch_review_btn.setVisible(False)
            self.video_discovered_btn.setEnabled(False)
            self.video_discovered_table.setVisible(False)
            for pane in self.evidence_panes:
                pane.video_view.clear_batch_roster()
            return
        self.batch_review_btn.setVisible(True)
        self.batch_context_label.setText(self._continuous_calibration_summary())
        self.batch_review_btn.setText(
            "退出判读" if self._batch_mode else "判读"
        )
        # The split review workflow is always active; no mode toggle is shown.
        self.batch_review_btn.setVisible(False)
        self.video_discovered_btn.setEnabled(self._batch_mode and batch is not None)
        self._update_batch_roster_overlays(batch)
        self._refresh_video_discovered_table()

    def _continuous_calibration_summary(self) -> str:
        pane = self._active_playback_pane()
        location = pane.location
        if pane.source_kind != REGULAR_SOURCE or location is None:
            self.batch_context_label.setToolTip("")
            return ""
        camera_index = int(location.segment.camera_index)
        key = (camera_index, self._recording_session_key(location))
        offset_ms = self._continuous_clock_offsets.get(key)
        calibration = self.calibration_store.get(*key)
        if offset_ms is None and calibration is not None:
            offset_ms = int(calibration.offset_ms)
        if offset_ms is None:
            self.batch_context_label.setToolTip(
                "找到首位运动员的实际过线画面并确认，后续选手沿用该机位时间偏移。"
            )
            return f"机位{camera_index} 待首人校时"
        auto_label, auto_tip = self._auto_clock_cross_check(
            camera_index, offset_ms, calibration,
        )
        self.batch_context_label.setToolTip(
            f"机位 {camera_index} 已按首人校准，时间偏移 {offset_ms:+d} ms；"
            f"后续运动员沿用此偏移定位。\n录像会话：{key[1]}"
            + (f"\n校时锚点：{calibration.anchor_bib}" if calibration is not None else "")
            + auto_tip
        )
        return f"机位{camera_index} 已校准 {offset_ms:+d} ms" + auto_label

    def _auto_clock_model(self, camera_index: int):
        """Latest automatic calibration published by the offline analysis."""

        path = self.passage_store.journal_path.with_name(CLOCK_MODEL_FILENAME)
        try:
            stat = path.stat()
            signature = (str(path), stat.st_mtime_ns, stat.st_size)
        except OSError:
            return None
        cached = getattr(self, "_auto_clock_store_cache", None)
        if cached is None or cached[0] != signature:
            try:
                cached = (signature, ClockModelStore(path))
            except (OSError, RuntimeError, ValueError) as error:
                logger.warning("automatic clock models unavailable: %s", error)
                return None
            self._auto_clock_store_cache = cached
        return cached[1].latest(camera_index)

    def _auto_clock_cross_check(self, camera_index, offset_ms, calibration) -> tuple[str, str]:
        """Compare the first-rider anchor with the automatic model; never override it."""

        model = self._auto_clock_model(camera_index)
        if model is None:
            return "", ""
        anchor = (
            self.passage_store.get(calibration.anchor_event_id)
            if calibration is not None else None
        )
        anchor_time = (
            int(anchor.timeline_timestamp_ms)
            if anchor is not None else (model.valid_from_ms + model.valid_to_ms) // 2
        )
        auto_offset = model.offset_at(anchor_time)
        difference = int(offset_ms) - auto_offset
        tip = (
            f"\n自动校时 v{model.version}：该时刻偏移 {auto_offset:+d} ms，"
            f"漂移 {model.slope_ms_per_hour:+.0f} ms/小时；人工与自动相差 {difference:+d} ms"
        )
        if abs(difference) > MANUAL_ANCHOR_TOLERANCE_MS:
            return (
                f" · 与自动校时差 {difference:+d} ms，请核对",
                tip + f"，超过 {MANUAL_ANCHOR_TOLERANCE_MS} ms，请核对首人校时；未自动覆盖。",
            )
        return "", tip

    def _update_batch_roster_overlays(
        self,
        batch: Optional[PassageReviewBatch],
    ) -> None:
        # The athlete table is the single source of truth for bibs during
        # review. Keep camera panes unobstructed for direct frame inspection.
        for pane in self.evidence_panes:
            pane.video_view.clear_batch_roster()

    def _select_batch_event_at_current_frame(
        self,
        event_id: str,
        pane: PassageEvidencePane,
    ) -> None:
        event_id = str(event_id)
        if (
            not self._batch_mode
            or not any(
                event.event_id == event_id for event in self._visible_events
            )
            or pane not in self.evidence_panes
        ):
            return
        pane.set_playing(False)
        row = next(
            (
                index
                for index, event in enumerate(self._visible_events)
                if event.event_id == event_id
            ),
            -1,
        )
        if row < 0:
            return
        self.table.blockSignals(True)
        self.table.setCurrentCell(row, 1)
        self.table.selectRow(row)
        self.table.blockSignals(False)
        self._select_event(
            event_id,
            preserve_current_frame=pane,
        )
        self._activate_pane(pane, align=False)
        batch = self._active_review_batch()
        event = self.passage_store.get(event_id)
        if (
            batch is not None
            and event is not None
            and batch.event_ids
            and event_id == batch.event_ids[0]
        ):
            self._calibrate_continuous_session_at_position(
                event,
                pane,
                int(pane._current_position_ms),
                int(time.time() * 1000.0),
            )
        self._begin_marking(pane)

    def _pane_for_camera(self, camera_index: int) -> PassageEvidencePane:
        camera_index = max(1, int(camera_index))
        return next(
            (
                pane
                for pane in self.evidence_panes
                if pane.camera_index == camera_index
            ),
            self._active_pane
            if self._active_pane in self.evidence_panes
            else self.regular_pane,
        )

    def _enter_batch_mode(self, event_id: str = "") -> bool:
        explicit_event_id = str(event_id)
        event_id = explicit_event_id or self._continuous_resume_event_id()
        batch = self._review_batch_by_event_id.get(event_id)
        if not event_id or self.passage_store.get(event_id) is None:
            return False
        self._batch_mode = True
        self._set_continuous_review_layout(True)
        self._active_review_batch_id = batch.batch_id if batch is not None else ""
        self._auto_advance_before_continuous = self.auto_advance_checkbox.isChecked()
        self.auto_advance_checkbox.setChecked(True)
        self._deferred_start_timer.stop()
        self._deferred_selection_panes.clear()
        self._selection_pending_panes.clear()
        self.group_combo.blockSignals(True)
        self.group_combo.setCurrentIndex(0)
        self.group_combo.blockSignals(False)
        self.identity_search.blockSignals(True)
        self.identity_search.clear()
        self.identity_search.blockSignals(False)
        self._seed_continuous_clock_offsets()
        self._lookup_cache.clear()
        self._selected_event_id = event_id
        self.refresh()
        self._update_filmstrip()
        return True

    def _continuous_resume_event_id(self) -> str:
        events = self._events_for_current_metadata(self.passage_store.events())
        if not events:
            return ""
        anchor_index = next(
            (
                index
                for index, event in enumerate(events)
                if event.event_id == self._selected_event_id
            ),
            0,
        )
        ordered_events = events[anchor_index:] + events[:anchor_index]
        return next(
            (
                event.event_id
                for event in ordered_events
                if self._association_for_event(event.event_id, REGULAR_SOURCE) is None
            ),
            events[anchor_index].event_id,
        )

    def _exit_batch_mode(self) -> None:
        if not self._batch_mode and not self._active_review_batch_id:
            return
        self._batch_mode = False
        self._set_continuous_review_layout(True)
        self._active_review_batch_id = ""
        self.auto_advance_checkbox.setChecked(
            self._auto_advance_before_continuous
        )
        self._lookup_cache.clear()
        self._update_batch_controls(self._selected_event_id)
        self._filmstrip_preview_timer.stop()
        self._pending_filmstrip_preview_position = None
        # Filmstrip review remains available after leaving continuous mode.
        self._update_filmstrip()

    def _toggle_batch_mode(self) -> None:
        if self._batch_mode:
            self._exit_batch_mode()
            self._refresh_filtered_view()
            return
        self._enter_batch_mode()

    def _set_continuous_review_layout(self, enabled: bool) -> None:
        """Keep the top preview and lower reference panes usable together."""

        if not hasattr(self, "workspace_splitter"):
            return
        if self._uses_popup_judging():
            self._set_continuous_results_table(False)
            pane = self._camera_one_pane()
            pane.set_full_resolution_enabled(pane is self._maximized_pane)
            self.evidence_splitter.hide()
            self.workspace_splitter.setMinimumHeight(180)
            self.workspace_splitter.setHandleWidth(0)
            self.results_panel.setMinimumWidth(0)
            self.results_panel.setMaximumWidth(16777215)
            return
        if enabled:
            self._set_continuous_results_table(True)
            # The top preview is the judgment surface. Keep the lower camera
            # panes visible as a fast before/after reference while the operator
            # scans for the next rider.
            self.evidence_splitter.setVisible(True)
            for pane in getattr(self, "evidence_panes", ()):
                pane.setMinimumSize(0, 0)
                pane.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
                pane.video_view.setMinimumSize(0, 0)
                pane.video_view.setSizePolicy(
                    QSizePolicy.Ignored,
                    QSizePolicy.Expanding,
                )
                # Keep the lower reference panes self-describing even though
                # the top preview owns the actual judgment action.
                pane.video_view.set_identity_badge_visible(True)
                pane.video_view.set_marker_label_visible(True)
            if self.workspace_splitter.orientation() != Qt.Horizontal:
                self.workspace_splitter.setOrientation(Qt.Horizontal)
            self.workspace_splitter.setStretchFactor(0, 1)
            self.workspace_splitter.setStretchFactor(1, 1)
            # Keep the athlete list fixed without disabling the native Qt
            # splitter handle, which can crash Qt5 during video re-layout.
            if self.workspace_splitter.handleWidth() != 0:
                self.workspace_splitter.setHandleWidth(0)
            self._apply_review_split_sizes()
            return
        self.evidence_splitter.setVisible(True)
        if self.workspace_splitter.orientation() != Qt.Horizontal:
            self.workspace_splitter.setOrientation(Qt.Horizontal)
        if self.workspace_splitter.handleWidth() != 5:
            self.workspace_splitter.setHandleWidth(5)
        for pane in getattr(self, "evidence_panes", ()):
            pane.video_view.set_identity_badge_visible(True)
            pane.video_view.set_marker_label_visible(True)
        self._set_continuous_results_table(False)
        if hasattr(self, "results_panel"):
            self.results_panel.setMinimumWidth(0)
            self.results_panel.setMaximumWidth(16777215)
        self.workspace_splitter.setStretchFactor(0, 4)
        self.workspace_splitter.setStretchFactor(1, 6)
        total = max(2, self.workspace_splitter.height())
        queue_height = min(360, max(220, int(total * 0.38)))
        self.workspace_splitter.setSizes([queue_height, total - queue_height])

    def _apply_review_split_sizes(self) -> None:
        if self._uses_popup_judging():
            return
        splitter = getattr(self, "workspace_splitter", None)
        if splitter is None or splitter.orientation() != Qt.Horizontal:
            return
        total = splitter.width()
        if total <= 0:
            return
        total = splitter.width()
        if total <= 0:
            return
        current = splitter.sizes()
        athlete_width = max(240, int(total * 0.30))
        target = [athlete_width, max(1, total - athlete_width)]
        if len(current) == 2 and abs(current[0] - target[0]) <= 2:
            return
        splitter.setSizes(target)

    def _apply_review_content_split_sizes(self) -> None:
        if getattr(self, "_review_split_manually_sized", False):
            return
        splitter = getattr(self, "review_content_splitter", None)
        if splitter is None or splitter.height() <= 0:
            return
        total = splitter.height()
        preview_height = max(420, int(total * (0.64 if self._uses_popup_judging() else 0.65)))
        target = [preview_height, max(1, total - preview_height)]
        current = splitter.sizes()
        if len(current) == 2 and abs(current[0] - target[0]) <= 2:
            return
        splitter.setSizes(target)

    def _review_divider_moved(self, *_):
        self._review_split_manually_sized = True
        self._filmstrip_restore_sizes = None
        self.video_filmstrip.full_race.enlarge_button.setText("放大胶卷")

    def _toggle_filmstrip_size(self):
        splitter = self.review_content_splitter
        self._review_split_manually_sized = True
        if self._filmstrip_restore_sizes is None:
            self._filmstrip_restore_sizes = splitter.sizes()
            total = sum(splitter.sizes())
            lower = self.workspace_splitter.minimumHeight()
            splitter.setSizes([max(420, total - lower), lower])
            self.video_filmstrip.full_race.enlarge_button.setText("还原布局")
        else:
            splitter.setSizes(self._filmstrip_restore_sizes)
            self._filmstrip_restore_sizes = None
            self.video_filmstrip.full_race.enlarge_button.setText("放大胶卷")

    def _finish_review_content_split_resize(self) -> None:
        self._review_split_resize_pending = False
        self._apply_review_content_split_sizes()

    def _set_continuous_results_table(self, enabled: bool) -> None:
        table = getattr(self, "table", None)
        if table is None:
            return
        hidden_columns = (3, 6, 7)
        compact_controls = (
            "group_combo",
            "identity_search",
            "delete_record_button",
            "summary_label",
            "video_arrival_button",
            "video_review_button",
            "video_review_done_button",
            "video_review_ignore_button",
            "video_review_bib_edit",
            "video_discovered_btn",
            "queue_expand_btn",
            "offset_spin",
        )
        if enabled:
            table.setMinimumWidth(0)
            table.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
            if not hasattr(self, "_continuous_table_state"):
                self._continuous_table_state = {
                    column: (table.isColumnHidden(column), table.columnWidth(column))
                    for column in range(table.columnCount())
                }
            if not hasattr(self, "_continuous_control_state"):
                self._continuous_control_state = {
                    name: getattr(self, name).isVisible()
                    for name in compact_controls
                    if hasattr(self, name)
                }
            for column in hidden_columns:
                table.setColumnHidden(column, True)
            for name in compact_controls:
                widget = getattr(self, name, None)
                if widget is not None:
                    widget.setVisible(False)
            table.set_compact_mode(True)
            return
        state = getattr(self, "_continuous_table_state", None)
        if state is None:
            return
        table.set_compact_mode(False)
        table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        for column, (was_hidden, width) in state.items():
            table.setColumnHidden(column, was_hidden)
            table.setColumnWidth(column, width)
        del self._continuous_table_state
        control_state = getattr(self, "_continuous_control_state", {})
        for name, was_visible in control_state.items():
            widget = getattr(self, name, None)
            if widget is not None:
                widget.setVisible(was_visible)
        if hasattr(self, "_continuous_control_state"):
            del self._continuous_control_state

    def _refresh_filtered_view(self) -> None:
        events = self._events_for_current_metadata(self.passage_store.events())
        metadata = self._current_metadata()
        metadata_context_key = (
            (metadata.race_id, metadata.stage_id)
            if metadata is not None
            else ("", "")
        )
        event_ids = {event.event_id for event in events}
        if (
            metadata_context_key != self._metadata_context_key
            or self._timeline_cache_signature() != self._timeline_signature
            or event_ids != set(self._lookups)
            or event_ids != set(self._event_review_statuses)
        ):
            self.refresh()
            return
        self._total_event_count = len(events)
        self._render_filtered_queue(events, self._selected_event_id, set())

    def refresh(self) -> None:
        previous_event_id = self._selected_event_id
        metadata = self._current_metadata()
        metadata_context_key = (
            (metadata.race_id, metadata.stage_id)
            if metadata is not None
            else ("", "")
        )
        if metadata_context_key != self._metadata_context_key:
            self._metadata_context_key = metadata_context_key
            self.video_filmstrip.full_race.clear()
            previous_event_id = ""
            self.identity_search.clear()
        events = self._events_for_current_metadata(self.passage_store.events())
        self._total_event_count = len(events)
        signature = self._timeline_cache_signature()
        if signature != self._timeline_signature:
            self._timeline_signature = signature
            self._lookup_cache = {
                event_id: cached
                for event_id, cached in self._lookup_cache.items()
                if self._cache_survives_timeline_update(cached[1])
            }

        self._update_group_combo(events)
        self._review_batches = build_review_batches(
            events,
            review_gap_ms=self.REVIEW_BATCH_GAP_MS,
            subwave_gap_ms=self.REVIEW_SUBWAVE_GAP_MS,
        )
        self._review_batch_by_event_id = {
            event_id: batch
            for batch in self._review_batches
            for event_id in batch.event_ids
        }
        self._reconcile_video_discoveries(self.passage_store.events())
        if self._active_review_batch_id and not any(
            batch.batch_id == self._active_review_batch_id
            for batch in self._review_batches
        ):
            self._batch_mode = False
            self._active_review_batch_id = ""
        self._lookups = {
            event.event_id: self._cached_lookup(event)
            for event in events
        }
        self._located_event_ids.clear()
        self._confirmed_event_ids.clear()
        self._event_review_statuses.clear()
        for event in events:
            self._project_event_status(event)

        self._render_filtered_queue(events, previous_event_id)
        self._update_filmstrip()
        self._refresh_camera_judgments()

    def _project_event_status(self, event: PassageEvent) -> None:
        lookup = self._lookups[event.event_id]
        regular = self._source_location_with_saved_association(
            event.event_id,
            lookup,
            high_speed=False,
        )
        high_speed = self._source_location_with_saved_association(
            event.event_id,
            lookup,
            high_speed=True,
        )
        regular_association = self._source_association(
            event.event_id,
            REGULAR_SOURCE,
            regular,
        )
        high_speed_association = self._source_association(
            event.event_id,
            HIGH_SPEED_SOURCE,
            high_speed,
        )
        readiness_status = review_status_text(lookup, regular, high_speed)
        self._event_review_statuses[event.event_id] = self._confirmation_status(
            regular_association,
            high_speed_association,
            readiness_status,
        )
        self._record_summary_state(
            event.event_id,
            regular,
            high_speed,
            readiness_status,
            regular_association,
            high_speed_association,
        )

    def refresh_events(self, event_ids: Iterable[str]) -> None:
        changed = {str(event_id) for event_id in event_ids if event_id}
        if not changed:
            return
        metadata = self._current_metadata()
        context = (metadata.race_id, metadata.stage_id) if metadata is not None else ("", "")
        if context != self._metadata_context_key:
            self.refresh()
            return
        events = self._events_for_current_metadata(self.passage_store.events())
        by_id = {event.event_id: event for event in events}
        self._total_event_count = len(events)
        self._update_group_combo(events)
        signature = self._timeline_cache_signature()
        if signature != self._timeline_signature:
            self._timeline_signature = signature
            self._lookup_cache = {
                event_id: cached for event_id, cached in self._lookup_cache.items()
                if self._cache_survives_timeline_update(cached[1])
            }
        for event_id in changed:
            self._lookup_cache.pop(event_id, None)
            event = by_id.get(event_id)
            if event is None:
                self._lookups.pop(event_id, None)
                self._event_review_statuses.pop(event_id, None)
                self._discard_summary_state(event_id)
            else:
                self._lookups[event_id] = self._cached_lookup(event)
                self._project_event_status(event)
        # Handle a first snapshot that also contains events whose UI notification
        # was coalesced with this batch, without rebuilding existing table items.
        for event in events:
            if event.event_id not in self._lookups:
                self._lookups[event.event_id] = self._cached_lookup(event)
                self._project_event_status(event)
                changed.add(event.event_id)
        self._review_batches = build_review_batches(
            events, review_gap_ms=self.REVIEW_BATCH_GAP_MS,
            subwave_gap_ms=self.REVIEW_SUBWAVE_GAP_MS,
        )
        self._review_batch_by_event_id = {
            event_id: batch for batch in self._review_batches for event_id in batch.event_ids
        }
        self._reconcile_video_discoveries(self.passage_store.events())
        if self._active_review_batch_id and not any(
            batch.batch_id == self._active_review_batch_id for batch in self._review_batches
        ):
            self._batch_mode = False
            self._active_review_batch_id = ""
        self._render_filtered_queue(events, self._selected_event_id, changed)
        self._update_navigation_controls()
        self._refresh_camera_judgments()

    @staticmethod
    def _status_color(value: str) -> QColor:
        if value == "已确认":
            return QColor("#16845b")
        if value == "未确认":
            return QColor("#c0372b")
        if value in {"异常", "受阻"}:
            return QColor("#c0372b")
        if value == "待核对":
            return QColor("#7d5b0c")
        return QColor("#526170")

    @classmethod
    def _apply_status_style(cls, item: QTableWidgetItem, value: str) -> None:
        item.setForeground(cls._status_color(value))
        font = item.font()
        font.setBold(value in {
            "未确认",
            "异常",
            "受阻",
            "已确认",
        })
        item.setFont(font)

    def _delete_selected_record(self) -> None:
        row = self.table.currentRow()
        if not 0 <= row < len(self._visible_events):
            return
        self._delete_record(self._visible_events[row])

    def _delete_record(self, event: PassageEvent) -> None:
        store = self.passage_store
        identity = " ".join(value for value in (event.bib, event.athlete_name) if value)
        reason, accepted = QInputDialog.getText(
            self, "删除记录",
            f"删除 {identity or event.event_id} 在 "
            f"{format_passage_time(event.timeline_timestamp_ms)} 的通过记录。\n"
            "请填写原因（只在本机隐藏；CycleRace 改了号码、芯片或时间会重新显示）：",
        )
        if not accepted or self.passage_store is not store:
            return
        if not str(reason).strip():
            QMessageBox.information(self, "删除记录", "请填写删除原因。")
            return
        try:
            deleted = store.delete(event.event_id, reason=str(reason).strip())
        except (OSError, RuntimeError) as error:
            QMessageBox.critical(self, "删除失败", f"无法删除记录：{error}")
            return
        self.refresh()
        if deleted:
            self.record_deleted.emit(event.event_id)

    def _on_table_selection_changed(
        self,
        current_row: int | None = None,
        _current_column: int = -1,
        _previous_row: int = -1,
        _previous_column: int = -1,
    ) -> None:
        row = self.table.currentRow() if current_row is None else int(current_row)
        if 0 <= row < len(self._visible_events):
            event_id = self._visible_events[row].event_id
            active_pane = self._active_playback_pane()
            event = self.passage_store.get(event_id)
            preserve_current_frame = (
                active_pane
                if event is not None
                and getattr(active_pane, "_current_frame_index", -1) >= 0
                else None
            )
            self._select_event(
                event_id,
                preserve_current_frame=preserve_current_frame,
                include_historical=True,
            )

    @staticmethod
    def _regular_locations(
        lookup: PassageVideoLookup,
    ) -> tuple[PassageVideoLocation, ...]:
        return tuple(
            sorted(
                (
                    location
                    for location in lookup.locations
                    if not is_high_speed(location)
                ),
                key=lambda location: (
                    _STATUS_PRIORITY.get(location.status, 99),
                    location.segment.camera_index,
                    location.segment.segment_id,
                ),
            )
        )

    @staticmethod
    def _regular_location_for_camera(
        lookup: PassageVideoLookup,
        camera_index: int,
    ) -> Optional[PassageVideoLocation]:
        return next(
            (
                location
                for location in PassageReviewSurface._regular_locations(lookup)
                if location.segment.camera_index == int(camera_index)
            ),
            None,
        )

    def _regular_summary_location(
        self,
        event_id: str,
        lookup: PassageVideoLookup,
    ) -> Optional[PassageVideoLocation]:
        locations = self._regular_locations(lookup)
        association = self._association_for_event(event_id, REGULAR_SOURCE)
        if association is not None:
            associated = next(
                (
                    location
                    for location in locations
                    if _association_matches_location(association, location)
                ),
                None,
            )
            if associated is not None:
                return associated
        configured = set(self._configured_regular_camera_indexes)
        return next(
            (
                location
                for location in locations
                if location.segment.camera_index in configured
            ),
            locations[0] if locations else None,
        )

    def _begin_selection_timing(
        self,
        event_id: str,
        priority_pane: PassageEvidencePane,
    ) -> None:
        self._deferred_start_timer.stop()
        self._selection_event_id = str(event_id)
        self._selection_started_at = time.perf_counter()
        self._selection_first_frame_ms = 0.0
        self._selection_expected_panes = 0
        self._selection_pending_panes.clear()
        self._deferred_selection_panes.clear()
        self._selection_priority_pane = priority_pane

    def _track_selection_pane(
        self,
        pane: PassageEvidencePane,
        *,
        deferred: bool,
    ) -> None:
        if not pane.has_playback_worker:
            return
        self._selection_expected_panes += 1
        self._selection_pending_panes.add(pane)
        if deferred and pane.worker_start_deferred:
            self._deferred_selection_panes.add(pane)

    def _start_deferred_selection_panes(self) -> None:
        self._deferred_start_timer.stop()
        panes = tuple(self._deferred_selection_panes)
        self._deferred_selection_panes.clear()
        for pane in panes:
            pane.start_deferred_worker()

    def _on_pane_initial_frame_ready(
        self,
        pane: PassageEvidencePane,
        event_id: str,
    ) -> None:
        if (
            str(event_id) != self._selection_event_id
            or pane not in self._selection_pending_panes
        ):
            return
        elapsed_ms = (time.perf_counter() - self._selection_started_at) * 1000.0
        if self._selection_first_frame_ms <= 0.0:
            self._selection_first_frame_ms = elapsed_ms
        self._selection_pending_panes.discard(pane)
        if pane is self._selection_priority_pane:
            self._start_deferred_selection_panes()
        if self._selection_pending_panes:
            return
        logger.info(
            "Athlete selection event=%s panes=%d first_frame=%.1fms all_frames=%.1fms",
            self._selection_event_id,
            self._selection_expected_panes,
            self._selection_first_frame_ms,
            elapsed_ms,
        )
        self._deferred_start_timer.stop()
        self._selection_event_id = ""
        self._selection_priority_pane = None

    def _location_on_current_media(
        self,
        event: PassageEvent,
        pane: PassageEvidencePane,
    ) -> Optional[PassageVideoLocation]:
        location = pane.location
        if location is None or location.segment.media_started_at_ms is None:
            return None
        offset_ms = self._continuous_offset_for_location(location)
        target_time_ms = int(event.timeline_timestamp_ms) + int(offset_ms)
        # 换号码只换身份；预计过线点可以在当前文件之外，不能截断后改变原帧时间。
        position_ms = target_time_ms - int(location.segment.media_started_at_ms)
        playback_ms = max(0, position_ms - self.CONTINUOUS_SKIP_LEAD_MS)
        if location.segment.media_duration_ms is not None:
            playback_ms = min(playback_ms, location.segment.media_duration_ms)
        return replace(
            location,
            passage_position_ms=position_ms,
            playback_position_ms=playback_ms,
            clock_offset_ms=offset_ms,
        )

    def _select_event(
        self,
        event_id: str,
        *,
        preserve_current_frame: Optional[PassageEvidencePane] = None,
        locate_target: bool = False,
        include_historical: bool = False,
    ) -> None:
        self._video_candidate_anchor_time_ms = None
        if preserve_current_frame is None and self._batch_mode and not locate_target:
            active_pane = self._active_playback_pane()
            if getattr(active_pane, "_current_frame_index", -1) >= 0:
                preserve_current_frame = active_pane
        self._include_historical_filmstrip_on_selection = bool(include_historical)
        try:
            self.selection_controller.select(
                event_id,
                preserve_current_frame=preserve_current_frame,
                locate_target=locate_target,
            )
        finally:
            self._include_historical_filmstrip_on_selection = False

    def _apply_selection_plan(
        self,
        plan: ReviewSelectionPlan,
        *,
        preserve_current_frame: Optional[PassageEvidencePane] = None,
    ) -> None:
        event = plan.event
        lookup = plan.lookup
        regular_locations = plan.regular_locations
        if getattr(self, "_include_historical_filmstrip_on_selection", False):
            self.video_filmstrip.full_race.ensure_time_visible(event.timeline_timestamp_ms)
        regular = plan.regular_summary
        high_speed = plan.high_speed
        reuse_continuous_media = plan.reuse_continuous_media
        same_batch_media = plan.same_batch_media
        preserve_media = plan.preserve_media
        active_pane = plan.active_pane
        switching_batch_event = plan.switching_batch_event
        # A double-click locate can explicitly choose camera 1 even when the
        # operator was previously focused on another pane. Apply that choice
        # before scheduling linked playback or filmstrip work.
        if active_pane in self.evidence_panes:
            self._activate_pane(active_pane, align=False)
        if switching_batch_event:
            # Identity changes must leave the displayed frame stable. Do not
            # start deferred workers merely to pause them.
            self._set_sync_playing(False, seek_final=False)
            for pane in self.evidence_panes:
                pane.set_playing(False)
        regular_association = self._source_association(
            event.event_id, REGULAR_SOURCE, regular
        )
        high_speed_association = self._source_association(
            event.event_id, HIGH_SPEED_SOURCE, high_speed
        )
        if not preserve_media:
            self._set_sync_playing(False)
            self._begin_selection_timing(event.event_id, active_pane)
            self._shared_delta_ms = self._saved_delta_ms(
                regular,
                high_speed,
                regular_association,
                high_speed_association,
            )
            if plan.locate_target:
                # A roster double-click is an absolute locate command.  Do
                # not replay an old saved evidence offset on the continuous
                # camera timeline.
                self._shared_delta_ms = 0
        self._selected_event_id = event.event_id
        if self._batch_mode:
            batch = self._review_batch_by_event_id.get(event.event_id)
            self._active_review_batch_id = batch.batch_id if batch is not None else ""
        identity = event.bib.strip() or "未知"
        metadata = self._current_metadata()
        metadata_athlete = self._metadata_athlete_for_event(event)
        athlete_name = event.athlete_name.strip() or (
            metadata_athlete.name.strip() if metadata_athlete is not None else ""
        ) or "--"
        team_name = event.team_name.strip() or (
            metadata_athlete.team_name.strip()
            if metadata_athlete is not None
            else ""
        ) or "--"
        passage_time = format_passage_time(event.timeline_timestamp_ms)
        status = self._confirmation_status(
            regular_association,
            high_speed_association,
            review_status_text(lookup, regular, high_speed),
        )
        display_status = self._display_confirmation_status(status)
        self.race_value.setText(
            (metadata.race_name.strip() if metadata is not None else "")
            or event.race_name.strip()
            or event.race_id
        )
        self.stage_value.setText(
            (metadata.stage_name.strip() if metadata is not None else "")
            or event.stage_name.strip()
            or event.stage_id
        )
        self.group_value.setText(
            (
                metadata.group_label(event.group_id)
                if metadata is not None
                else ""
            )
            or event.group_name.strip()
            or event.group_id
        )
        self.selected_identity_value.setText(identity)
        self.athlete_value.setText(athlete_name)
        self.team_value.setText(team_name)
        self.selected_time_value.setText(passage_time)
        self.source_value.setText(display_status)
        athlete_summary = (
            f"{identity} {athlete_name if athlete_name != '--' else ''}".strip()
        )
        self.current_passage_label.setText(athlete_summary)
        self._update_current_context(display_status)
        if not preserve_media:
            self.current_time_label.setText(
                format_passage_time(event.timeline_timestamp_ms + self._shared_delta_ms)
            )
            for pane in self.regular_panes:
                pane_location = regular_locations.get(pane.camera_index)
                defer_worker_start = pane is not active_pane
                pane.set_passage(
                    event,
                    pane_location,
                    self._source_association(
                        event.event_id,
                        REGULAR_SOURCE,
                        pane_location,
                    ),
                    initial_delta_ms=self._shared_delta_ms,
                    lookup_status=(
                        pane_location.status
                        if pane_location is not None
                        else lookup.status
                    ),
                    defer_worker_start=defer_worker_start,
                )
                if not (
                    self._batch_mode
                    and defer_worker_start
                    and self.SINGLE_CAMERA_PREVIEW
                ):
                    self._track_selection_pane(
                        pane,
                        deferred=defer_worker_start,
                    )
            if self._show_high_speed_pane:
                defer_high_speed = (
                    self._batch_mode and self.high_speed_pane is not active_pane
                )
                self.high_speed_pane.set_passage(
                    event,
                    high_speed,
                    high_speed_association,
                    initial_delta_ms=self._shared_delta_ms,
                    lookup_status=high_speed.status if high_speed is not None else "",
                    defer_worker_start=defer_high_speed,
                )
                if not defer_high_speed or not self.SINGLE_CAMERA_PREVIEW:
                    self._track_selection_pane(
                        self.high_speed_pane,
                        deferred=False,
                    )
            if self._deferred_selection_panes:
                if active_pane not in self._selection_pending_panes:
                    self._start_deferred_selection_panes()
                else:
                    self._deferred_start_timer.start()
            self.play_both_btn.setText("联动 ▶")
        else:
            for pane in self.regular_panes:
                pane_location = regular_locations.get(pane.camera_index)
                if (
                    preserve_current_frame is not None
                    and pane.location is not None
                    and getattr(pane, "_current_frame_index", -1) >= 0
                ):
                    # Manual navigation establishes the active recording for
                    # continuous judging. Identity changes must stay on that
                    # recording even when the next rider's official timestamp
                    # resolves to the neighbouring archive segment.
                    current_media_location = self._location_on_current_media(
                        event, pane
                    )
                    # At a split boundary the new passage may not resolve to
                    # this segment at all. The loaded location still identifies
                    # the active recording and is valid for rebinding the
                    # passage while preserving the decoded frame.
                    if current_media_location is not None:
                        pane_location = current_media_location
                pane_association = self._source_association(
                    event.event_id,
                    REGULAR_SOURCE,
                    pane_location,
                )
                pane_same_media = (
                    pane.location is not None
                    and pane._media_context(pane_location)
                    == pane._media_context(pane.location)
                )
                if (reuse_continuous_media or preserve_current_frame is not None) and (
                    pane_same_media or preserve_current_frame is pane
                ):
                    pane.rebind_passage(
                        event,
                        pane_location,
                        pane_association,
                        lookup_status=(
                            pane_location.status
                            if pane_location is not None
                            else lookup.status
                        ),
                    )
                elif pane_location is not None and not pane_same_media:
                    pane.set_passage(
                        event,
                        pane_location,
                        pane_association,
                        initial_delta_ms=self._shared_delta_ms,
                        lookup_status=pane_location.status,
                        defer_worker_start=pane is not active_pane,
                    )
                elif pane_location is None and pane.location is not None:
                    pane.clear_passage()
                elif pane.association != pane_association:
                    pane.set_association(pane_association)
            if (
                self._show_high_speed_pane
            ):
                high_speed_same_media = (
                    self.high_speed_pane.location is not None
                    and self.high_speed_pane._media_context(high_speed)
                    == self.high_speed_pane._media_context(
                        self.high_speed_pane.location
                    )
                )
                if reuse_continuous_media and high_speed_same_media:
                    self.high_speed_pane.rebind_passage(
                        event,
                        high_speed,
                        high_speed_association,
                        lookup_status=(
                            high_speed.status if high_speed is not None else ""
                        ),
                    )
                elif high_speed is not None and not high_speed_same_media:
                    self.high_speed_pane.set_passage(
                        event,
                        high_speed,
                        high_speed_association,
                        initial_delta_ms=self._shared_delta_ms,
                        lookup_status=high_speed.status,
                        defer_worker_start=(
                            self._batch_mode
                            and self.high_speed_pane is not active_pane
                        ),
                    )
                elif self.high_speed_pane.association != high_speed_association:
                    self.high_speed_pane.set_association(high_speed_association)
        if active_pane in self.evidence_panes:
            self._activate_pane(active_pane, align=False)
        self._update_reference_states(event.event_id)
        if preserve_current_frame is not None:
            # In continuous judging the operator-selected current recording is
            # the authoritative source, even when an older association points
            # at the neighbouring archive segment. Keep it editable so the
            # next rider can be marked and confirmed without pressing Target.
            for pane in self.regular_panes:
                if pane.location is not None and pane.association is None:
                    pane.set_reference_only(False)
        if same_batch_media:
            self._update_shared_from_pane(active_pane)
            if preserve_current_frame is None:
                self._skip_continuous_gap(event, active_pane)
        self._update_batch_controls(event.event_id)
        self._update_navigation_controls()
        self._refresh_camera_judgments()
        if not self._batch_mode and self._filmstrip_context is not None:
            # Avoid blocking the UI while the previous strip worker is being
            # stopped; let the inactive camera start first on low-end PCs.
            if self.playback_coordinator is not None:
                self.playback_coordinator.request_filmstrip_update(deferred=True)
        elif self.playback_coordinator is not None:
            self.playback_coordinator.request_filmstrip_update(deferred=False)
        else:
            self._update_filmstrip()

    def _live_location_for_filmstrip(
        self,
        pane: PassageEvidencePane,
        anchor_time_ms: int,
    ) -> Optional[PassageVideoLocation]:
        """Return a live rolling source when a concrete window provides one."""

        return None

    def _live_locations_for_filmstrip(self, pane) -> tuple[PassageVideoLocation, ...]:
        return ()

    def _persist_filmstrip_location(
        self, location: PassageVideoLocation,
    ) -> PassageVideoLocation:
        """Give a window implementation a chance to make live media durable."""

        return location

    def _archive_location_for_filmstrip(
        self,
        pane: PassageEvidencePane,
        anchor_time_ms: int,
    ) -> Optional[PassageVideoLocation]:
        event = self.passage_store.get(self._selected_event_id)
        metadata = self._current_metadata()
        race_id = (
            event.race_id
            if event is not None
            else metadata.race_id if metadata is not None else None
        )
        try:
            lookup = self.timeline_store.locate_passage(
                int(anchor_time_ms),
                race_id=race_id,
                prefer_continuous_media=True,
            )
        except (OSError, RuntimeError, ValueError):
            return None
        return next(
            (
                location
                for location in lookup.locations
                if int(location.segment.camera_index) == int(pane.camera_index)
                and location.segment.clock_source == DEFAULT_CLOCK_SOURCE
                and not str(location.segment.segment_id).startswith("preview-")
                and location.status
                in {"located", "near_boundary", "unverified", "recording"}
                and self.timeline_store.video_path_is_playable(location.video_path)
            ),
            None,
        )

    def _filmstrip_window_coverage(
        self,
        pane: PassageEvidencePane,
        location: PassageVideoLocation,
        absolute_start_ms: int,
        absolute_end_ms: int,
    ) -> tuple[bool, str]:
        """Validate the requested continuous window before decoding it.

        A timeline lookup can locate an athlete at either side of a recorder
        gap, but that must never make the missing middle look like ordinary
        empty video.  Build the camera/source interval union from the durable
        timeline journal and reject an internal gap.  We currently decode one
        media path per active filmstrip; contiguous entries on another path
        therefore remain an explicit incomplete-evidence state instead of
        being silently stitched by the UI.
        """

        start = int(absolute_start_ms)
        end = max(start, int(absolute_end_ms))
        if end <= start:
            return True, ""
        selected = location.segment
        tolerance = max(
            int(self.FILMSTRIP_COVERAGE_TOLERANCE_MS),
            int(getattr(selected, "timing_error_ms", 0) or 0),
        )
        intervals: list[tuple[int, int, Path, object]] = []
        for segment in self.timeline_store.segments():
            if int(segment.camera_index) != int(pane.camera_index):
                continue
            if str(segment.source_id) != str(selected.source_id):
                continue
            if selected.race_id and not (
                segment.race_id == selected.race_id
                or (
                    not segment.race_id
                    and segment.clock_source == DEFAULT_CLOCK_SOURCE
                )
            ):
                continue
            if segment.media_started_at_ms is None or segment.media_duration_ms is None:
                continue
            interval_start = int(segment.media_started_at_ms)
            interval_end = interval_start + int(segment.media_duration_ms)
            if interval_end <= start - tolerance or interval_start >= end + tolerance:
                continue
            try:
                path = self.timeline_store.resolve_video_path(segment)
            except (OSError, RuntimeError, ValueError):
                path = Path(segment.video_path)
            intervals.append((interval_start, interval_end, Path(path), segment))
        # Live rolling HLS locations are synthetic and therefore are not yet
        # present in the durable journal.  Their own interval is still valid
        # for the active window; later archive publication will replace it.
        if (
            selected.media_started_at_ms is not None
            and selected.media_duration_ms is not None
            and not any(item[3] is selected for item in intervals)
        ):
            selected_start = int(selected.media_started_at_ms)
            selected_end = selected_start + int(selected.media_duration_ms)
            intervals.append(
                (selected_start, selected_end, Path(location.video_path), selected)
            )
        if not intervals:
            return False, "当前窗口没有可验证的连续录像时间线"
        intervals.sort(key=lambda item: (item[0], item[1]))
        cursor = start
        selected_path = Path(location.video_path)
        for interval_start, interval_end, path, _segment in intervals:
            # Segments ending exactly at the cursor are already exhausted and
            # must not be compared as a new path; otherwise a clean boundary
            # is falsely reported as a cross-file gap.
            if interval_end <= cursor:
                continue
            if interval_start > cursor + tolerance:
                return (
                    False,
                    f"录像断点：{cursor} ms 至 {interval_start} ms 未覆盖",
                )
            if path != selected_path:
                return (
                    False,
                    "当前两分钟窗口跨越多个录像文件，尚未安全连续拼接",
                )
            cursor = max(cursor, interval_end)
            if cursor >= end - tolerance:
                return True, ""
        if cursor >= end - tolerance:
            return True, ""
        # A final, naturally shorter tail is safe to show as a partial window;
        # an internal gap would have returned above when a following segment
        # existed.  The caller labels this state instead of claiming a full
        # two-minute window.
        latest_end = max(item[1] for item in intervals)
        if latest_end <= cursor + tolerance:
            return True, "录像已到末尾，当前窗口不足两分钟"
        return False, f"录像断点：{cursor} ms 至 {end} ms 未覆盖"

    def _filmstrip_context_for_active_pane(
        self,
    ) -> Optional[tuple[Path, int, int, int, tuple[int, ...], int, int]]:
        self._filmstrip_gap_message = ""
        if not self._batch_mode and not self.FILMSTRIP_ALWAYS_AVAILABLE:
            return None
        pane = self._camera_one_pane()
        location = pane.location
        selected_event = self.passage_store.get(self._selected_event_id)
        offset_ms = self._continuous_offset_for_location(location)
        event_anchor_ms = (
            int(selected_event.timeline_timestamp_ms) + int(offset_ms)
            if selected_event is not None
            else None
        )
        candidate_anchor_ms = getattr(
            self,
            "_video_candidate_anchor_time_ms",
            None,
        )
        anchor_time_ms = (
            int(candidate_anchor_ms)
            if candidate_anchor_ms is not None
            else event_anchor_ms
        )
        is_preview_location = bool(
            location is not None
            and (
                str(getattr(location.segment, "segment_id", "")).startswith(
                    "preview-"
                )
                or location.video_path.suffix.lower() == ".m3u8"
            )
        )
        # The top rail is a continuous camera-1 locator. A persisted short
        # evidence clip (non-preview media_locator) must never become its
        # source merely because it is the current pane binding.
        is_short_evidence_clip = bool(
            location is not None
            and str(getattr(location, "media_locator", "")).strip()
            and not is_preview_location
        )
        if (
            anchor_time_ms is not None
            and (location is None or is_preview_location or is_short_evidence_clip)
            # Directly bound review clips are intentionally kept self-contained
            # outside continuous batch mode; do not trigger a timeline scan in
            # that workflow just to populate an optional top rail.
            and not (
                is_short_evidence_clip
                and not self._batch_mode
                and self.review_binding_store is not None
            )
        ):
            live_location = self._live_location_for_filmstrip(
                pane,
                anchor_time_ms,
            )
            archive_location = (
                None
                if live_location is not None
                else self._archive_location_for_filmstrip(
                    pane,
                    anchor_time_ms,
                )
            )
            if live_location is not None:
                location = live_location
            elif archive_location is not None:
                location = archive_location
        if location is None or not location.video_path.is_file():
            return None
        duration_ms = int(
            location.segment.media_duration_ms
            or getattr(pane, "_duration_ms", 0)
            or 0
        )
        if duration_ms <= 0:
            return None
        media_start_ms = (
            int(location.segment.media_started_at_ms)
            if location.segment.media_started_at_ms is not None
            else int(location.segment.started_at_ms)
        )
        media_end_ms = media_start_ms + duration_ms
        positions: list[int] = []
        for event in self._visible_events:
            event_time_ms = int(event.timeline_timestamp_ms) + int(offset_ms)
            if media_start_ms <= event_time_ms <= media_end_ms:
                positions.append(event_time_ms - media_start_ms)
        for candidate in self._video_navigation_candidates:
            if int(getattr(candidate, "camera_index", 0)) != int(pane.camera_index):
                continue
            candidate_time_ms = int(getattr(candidate, "peak_at_ms", 0))
            if media_start_ms <= candidate_time_ms <= media_end_ms:
                # Candidate files are short HLS pieces, while the judgment pane
                # commonly plays a chip-centered preview playlist. Map by the
                # shared absolute recorder clock instead of segment IDs.
                positions.append(candidate_time_ms - media_start_ms)
                continue
            if str(getattr(candidate, "segment_id", "")) != location.segment.segment_id:
                continue
            positions.append(int(getattr(candidate, "video_position_ms", 0)))
        if not positions:
            positions.append(int(location.passage_position_ms))
        # Before the playback worker emits its first frame, the pane reports
        # position 0 even though the selected athlete's target is later in
        # the recording. Use the target passage frame in that transient state
        # so the filmstrip's rightmost 0-second tile shows the athlete rather
        # than an empty lead-in frame.
        current_frame_index = int(getattr(pane, "_current_frame_index", -1))
        current_position = int(getattr(pane, "_current_position_ms", 0))
        target_position = int(getattr(pane, "_target_position_ms", 0))
        if anchor_time_ms is not None:
            center = max(
                0,
                min(duration_ms - 1, int(anchor_time_ms) - media_start_ms),
            )
        else:
            center = (
                target_position
                if current_frame_index < 0
                else current_position
            )
        filmstrip_window_ms = (
            120_000 if self._low_resource_mode else self.FILMSTRIP_WINDOW_MS
        )
        requested_window_start = self._filmstrip_requested_window_start_ms
        requested_local_start = (
            int(requested_window_start) - media_start_ms
            if requested_window_start is not None
            else None
        )
        # The batch buttons request an exact absolute two-minute successor.
        # Honour that request when this media file contains the whole window;
        # otherwise retain the normal anchor-based window and let coverage
        # validation report a real cross-file/gap condition.
        if (
            requested_local_start is not None
            and duration_ms >= filmstrip_window_ms
            and 0 <= requested_local_start <= duration_ms - filmstrip_window_ms
        ):
            start_ms = requested_local_start
            end_ms = start_ms + filmstrip_window_ms
        else:
            window_start = (
                max(0, center) // filmstrip_window_ms
            ) * filmstrip_window_ms
            # Keep a full two-minute window whenever the media contains one.
            # The old duration clamp could silently shrink a window at a
            # segment boundary and make a recorder gap look like an empty
            # stretch.
            if duration_ms >= filmstrip_window_ms:
                start_ms = min(window_start, duration_ms - filmstrip_window_ms)
                end_ms = start_ms + filmstrip_window_ms
            else:
                start_ms = min(window_start, max(0, duration_ms - 1))
                end_ms = duration_ms
        if end_ms <= start_ms:
            end_ms = min(duration_ms, start_ms + 1_000)
        origin_ms = int(
            location.segment.media_started_at_ms
            if location.segment.media_started_at_ms is not None
            else location.segment.started_at_ms
        )
        absolute_window_start = (
            int(requested_window_start)
            if requested_local_start is not None
            and start_ms == requested_local_start
            else origin_ms + int(start_ms)
        )
        absolute_window_end = origin_ms + int(end_ms)
        covered, coverage_message = self._filmstrip_window_coverage(
            pane,
            location,
            absolute_window_start,
            absolute_window_start + int(filmstrip_window_ms),
        )
        if not covered:
            self._filmstrip_gap_message = coverage_message
            return None
        self._filmstrip_gap_message = coverage_message
        window_positions = tuple(
            position for position in positions if start_ms <= position <= end_ms
        )
        if not window_positions:
            window_positions = (max(start_ms, min(end_ms, center)),)
        return (
            location.video_path,
            start_ms,
            end_ms,
            center,
            window_positions,
            absolute_window_start,
            absolute_window_end,
        )

    def _filmstrip_race_id(self) -> str:
        metadata = self._current_metadata()
        if metadata is not None:
            return metadata.race_id
        race_ids = {item.race_id for item in self.passage_store.events() if item.race_id}
        return next(iter(race_ids)) if len(race_ids) == 1 else ""

    def _update_filmstrip(self) -> None:
        if not hasattr(self, "video_filmstrip"):
            return
        pane = self._camera_one_pane()
        race_id = self._filmstrip_race_id()
        workspace_key = (str(self.timeline_store.journal_path.absolute()), race_id)
        if workspace_key != getattr(self, "_race_filmstrip_workspace_key", None):
            self.video_filmstrip.full_race.clear()
            self._race_filmstrip_workspace_key = workspace_key
        provider = getattr(self, "_recording_sources_for_filmstrip", None)
        if provider is not None:
            sources, pending = provider(pane, race_id)
        else:
            live_locations = self._live_locations_for_filmstrip(pane)
            # There can be a short interval between starting the recorder and the
            # first playable HLS segment. Keep that interval visible as processing
            # instead of presenting it as a permanent recording gap.
            recorder_active_reader = getattr(self, "_recording_any_active", None)
            recording_processing = bool(
                callable(recorder_active_reader)
                and hasattr(self, "_recorders")
                and recorder_active_reader()
            )
            sources, pending = recording_sources(
                self.timeline_store, pane.camera_index, race_id,
                offset_for_location=self._continuous_offset_for_location,
                live_locations=live_locations,
            )
            if recording_processing and not live_locations:
                pending = max(1, pending)
        panel = self.video_filmstrip.full_race
        panel.set_check_context(self.timeline_store.journal_path.parent / "filmstrip_checks.jsonl",
                                race_id, pane.camera_index)
        panel.set_sources(sources, pending)
        if pane.location is not None and pane._current_frame_index >= 0:
            panel.set_current_frame(pane.location, pane._current_position_ms)
        panel.set_judgments(self._camera_judgment_records(pane), self._selected_event_id)

    def _open_race_filmstrip_frame(self, frame: RaceFilmstripFrame) -> bool:
        panel = self.video_filmstrip.full_race
        if frame.source.key not in {source.key for source in panel.index.sources}:
            return False
        if not frame.source.location.video_path.is_file():
            panel.status_label.setText("录像文件不可用，未移动机位 1。")
            return False
        if self._seek_retained_camera_frame(frame.source.location, frame.position_ms, frame.frame_index) is False:
            return False
        panel.set_current_frame(frame.source.location, frame.position_ms)
        self._refresh_camera_judgments()
        return True

    def _maximize_filmstrip_camera(self, frame: RaceFilmstripFrame) -> None:
        """F from the filmstrip keeps its exact frame and enlarges camera 1."""
        panel = self.video_filmstrip.full_race
        if frame.source.key not in {source.key for source in panel.index.sources}:
            return
        if not frame.source.location.video_path.is_file():
            panel.status_label.setText("录像文件不可用，无法打开判读。")
            return
        if not self._open_race_filmstrip_frame(frame):
            return
        pane = self._camera_one_pane()
        if self._maximized_pane is not pane:
            self._toggle_maximized_pane(pane)
        pane.video_view.setFocus(Qt.ShortcutFocusReason)

    def _uses_popup_judging(self) -> bool:
        return self._configured_regular_camera_indexes == (1,) and not self._show_high_speed_pane

    def _on_filmstrip_ready(self, ready: bool, _message: str) -> None:
        self._filmstrip_prefetch_timer.stop()
        if ready:
            if self._filmstrip_gap_message:
                self.video_filmstrip.status_label.setText(
                    f"{self._filmstrip_gap_message}；当前可处理"
                )
            # Let the operator receive the ready state first. The next window
            # is deliberately prepared later and never replaces the active one.
            if not self._filmstrip_operator_idle_timer.isActive():
                self._filmstrip_prefetch_timer.start()

    def _mark_filmstrip_operator_busy(self) -> None:
        if not hasattr(self, "video_filmstrip") or not hasattr(
            self, "_filmstrip_operator_idle_timer"
        ):
            return
        self.video_filmstrip.set_operator_busy(True)
        self._filmstrip_prefetch_timer.stop()
        self._filmstrip_operator_idle_timer.start()

    def _on_filmstrip_operator_idle(self) -> None:
        if not hasattr(self, "video_filmstrip"):
            return
        pane = self._active_playback_pane()
        if pane is not None and (
            bool(getattr(pane, "is_playing", False))
            or bool(getattr(pane, "_video_scrubbing", False))
        ):
            self._filmstrip_operator_idle_timer.start()
            return
        self.video_filmstrip.set_operator_busy(False)
        if self.video_filmstrip.is_ready:
            self._filmstrip_prefetch_timer.start()

    def _prefetch_next_filmstrip_window(self) -> None:
        if not hasattr(self, "video_filmstrip") or not self.video_filmstrip.is_ready:
            return
        context = self._filmstrip_context
        absolute_window = self._filmstrip_absolute_window
        if context is None or absolute_window is None:
            return
        pane = self._camera_one_pane()
        next_start_ms = int(absolute_window[1])
        next_end_ms = next_start_ms + int(self.FILMSTRIP_WINDOW_MS)
        location = self._live_location_for_filmstrip(
            pane,
            next_start_ms + int(self.FILMSTRIP_WINDOW_MS) // 2,
        ) or self._archive_location_for_filmstrip(
            pane,
            next_start_ms + int(self.FILMSTRIP_WINDOW_MS) // 2,
        )
        if location is None:
            return
        origin_ms = int(
            location.segment.media_started_at_ms
            if location.segment.media_started_at_ms is not None
            else location.segment.started_at_ms
        )
        duration_ms = int(location.segment.media_duration_ms or 0)
        if duration_ms <= 0:
            return
        start_position_ms = max(0, next_start_ms - origin_ms)
        end_position_ms = min(duration_ms, next_end_ms - origin_ms)
        if end_position_ms <= start_position_ms:
            return
        self.video_filmstrip.prefetch(
            location.video_path,
            start_position_ms,
            end_position_ms,
        )

    def _filmstrip_judgment_pane(self) -> Optional[PassageEvidencePane]:
        pane = self._camera_one_pane()
        if pane.location is None or pane.location.status not in _OPENABLE_STATUSES:
            return None
        return pane

    def _continuous_marker_positions_for_filmstrip(
        self,
        pane: PassageEvidencePane,
        absolute_start_ms: int,
        absolute_end_ms: int,
    ) -> tuple[tuple[int, str], ...]:
        """Return all confirmed/unknown markers inside the current strip window."""

        location = pane.location
        if location is None or pane.source_kind != REGULAR_SOURCE:
            return ()
        media_origin_ms = int(
            location.segment.media_started_at_ms
            if location.segment.media_started_at_ms is not None
            else location.segment.started_at_ms
        )
        return tuple(
            (record.recorder_time_ms - media_origin_ms, record.label)
            for record in self._camera_judgment_records(pane)
            if record.recorder_time_ms is not None
            and absolute_start_ms <= record.recorder_time_ms <= absolute_end_ms
        )

    def _camera_judgment_records(
        self, pane: PassageEvidencePane,
    ) -> tuple[CameraJudgment, ...]:
        # Use the full event scope, independently of roster search/group/status
        # filters and the current recording file or two-minute window.
        events = {
            event.event_id: event
            for event in self._events_for_current_metadata(self.passage_store.events())
        }
        metadata = self._current_metadata()
        records = []
        saved = [
            (f"event:{item.passage_event_id}", item, False)
            for item in self._current_associations()
            if item.confirmed_source == REGULAR_SOURCE
            and item.passage_event_id in events
        ]
        saved.extend(
            (f"marker:{item.marker_id}", item, True)
            for item in self.continuous_marker_store.markers()
            if item.camera_index == pane.camera_index
        )
        for key, item, unknown in saved:
            segment = self.timeline_store.get_segment(item.segment_id)
            if (
                segment is None
                or segment.camera_index != pane.camera_index
                or segment.clock_source != DEFAULT_CLOCK_SOURCE
                or (metadata is not None and segment.race_id
                    and segment.race_id != metadata.race_id)
            ):
                continue
            if unknown and item.passage_event_id and item.passage_event_id not in events:
                continue
            recorder_time = (
                int(segment.media_started_at_ms) + item.position_ms
                if segment.media_started_at_ms is not None else None
            )
            offset = self._calibrated_clock_offset(
                pane.camera_index, self._recording_session_key_from_path(
                    segment.source_id, self.timeline_store.resolve_video_path(segment),
                ),
            )
            label = item.label if unknown else (
                events[item.passage_event_id].bib.strip() or item.bib.strip() or "未知"
            )
            records.append(CameraJudgment(
                key=key,
                event_id="" if unknown else item.passage_event_id,
                segment_id=item.segment_id,
                position_ms=item.position_ms,
                recorder_time_ms=recorder_time,
                label=label,
                time_label=(format_passage_time(recorder_time - offset)
                            if recorder_time is not None else "时间未校验"),
                clock_offset_ms=offset,
                unknown=unknown,
            ))
        return tuple(sorted(records, key=lambda item: (
            item.recorder_time_ms is None, item.recorder_time_ms or 0,
            # Same-frame judgments have no video evidence of an order. Keep
            # their roster order instead of sorting by opaque passage IDs.
            events[item.event_id].timeline_timestamp_ms
            if item.event_id in events else item.recorder_time_ms or 0,
            item.key,
        )))

    def _seek_retained_camera_frame(
        self, location: PassageVideoLocation, position_ms: int,
        frame_index: Optional[int] = None, camera_index: int = 1,
        *, adjacent_boundary_ms: Optional[int] = None, adjacent_direction: int = 0,
    ) -> bool:
        """Open a pinned image's source independently of the overview window.

        ``frame_index=None`` means the caller only knows the media time (a
        group start has no captured frame number): the pane then seeks by
        milliseconds instead of an invented frame.
        """
        try:
            location = self._persist_filmstrip_location(location)
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            self.video_filmstrip.full_race.status_label.setText(
                f"原帧保留失败，未进入判读：{error}"
            )
            return False
        pane = self._camera_one_pane() if int(camera_index) <= 1 else self._pane_for_camera(camera_index)
        offset = self._continuous_offset_for_location(location)
        origin = location.segment.media_started_at_ms
        event = self.passage_store.get(self._selected_event_id)
        if event is None:
            event = SimpleNamespace(event_id="", bib="待补录", timeline_timestamp_ms=(origin or 0) + position_ms - offset)
        target = int(event.timeline_timestamp_ms) + offset - origin if origin is not None else position_ms
        location = replace(location, passage_position_ms=target, playback_position_ms=position_ms, clock_offset_ms=offset)
        association = self._association_for_event(event.event_id, REGULAR_SOURCE) if event.event_id else None
        self._set_sync_playing(False, seek_final=False)
        self._deferred_start_timer.stop()
        self._deferred_selection_panes.clear()
        self._selection_pending_panes.clear()
        self._filmstrip_preview_timer.stop()
        self._pending_filmstrip_preview_position = None
        self._pending_filmstrip_position = None
        self._activate_pane(pane, align=False)
        self._pause_inactive_panes(pane)
        # The image retains its recording even if the ordinary filmstrip has
        # moved elsewhere. Selection remains the operator's current identity.
        if not pane.rebind_passage(event, location, association):
            pane.set_passage(event, location, association)
        if adjacent_boundary_ms is None:
            pane.seek_media_frame(position_ms, frame_index)
        else:
            pane.seek_adjacent_frame(adjacent_boundary_ms, adjacent_direction)
        self._shared_delta_ms = position_ms - target
        self._update_shared_time_label()
        return True

    def _refresh_camera_judgments(self) -> None:
        pane = self._camera_one_pane()
        records = self._camera_judgment_records(pane)
        self.video_filmstrip.full_race.set_judgments(records, self._selected_event_id)
        self.video_filmstrip.judgment_track.set_records(
            records, self._selected_event_id,
        )
        pane.video_view.set_history_markers(self._continuous_marker_overlays(pane))
        context = self._filmstrip_context
        window = self._filmstrip_absolute_window
        if context is not None and window is not None and pane.location is not None:
            # The filmstrip may already have navigated to the next archive,
            # while the video still displays the previous one.
            origin = window[0] - context[1]
            self.video_filmstrip.set_history_markers(tuple(
                (record.recorder_time_ms - origin, record.label)
                for record in records
                if record.recorder_time_ms is not None
                and window[0] <= record.recorder_time_ms <= window[1]
            ))

    def _open_camera_judgment(self, key: str) -> None:
        pane = self._camera_one_pane()
        record = next((item for item in self._camera_judgment_records(pane)
                       if item.key == key), None)
        if record is None:
            return
        saved = (
            next((item for item in self.continuous_marker_store.markers()
                  if f"marker:{item.marker_id}" == key), None)
            if record.unknown else
            self._association_for_event(record.event_id, REGULAR_SOURCE)
        )
        location = self._saved_regular_location(saved) if saved is not None else None
        if (location is None or location.status not in _OPENABLE_STATUSES
                or location.passage_position_ms != record.position_ms):
            QMessageBox.information(self, "无法回看", "判读记录仍保留，但对应录像暂不可用。")
            return
        offset = record.clock_offset_ms
        origin = location.segment.media_started_at_ms
        event = self.passage_store.get(record.event_id) if record.event_id else None
        # Unassigned records never borrow the selected athlete's identity.
        if event is None:
            event = SimpleNamespace(
                event_id="", bib="待补录",
                timeline_timestamp_ms=(record.recorder_time_ms or 0) - offset,
            )
        target_position = (
            int(event.timeline_timestamp_ms) + offset - int(origin)
            if origin is not None else record.position_ms
        )
        location = replace(
            location, passage_position_ms=target_position,
            playback_position_ms=record.position_ms, clock_offset_ms=offset,
        )
        self._set_sync_playing(False, seek_final=False)
        if record.event_id:
            self._select_event(record.event_id, preserve_current_frame=pane)
        else:
            self._clear_selection_details()
        self._deferred_start_timer.stop()
        self._deferred_selection_panes.clear()
        self._selection_pending_panes.clear()
        self._filmstrip_preview_timer.stop()
        self._pending_filmstrip_preview_position = None
        self._selected_event_id = record.event_id
        self.table.blockSignals(True)
        self.table.clearSelection()
        self.table.setCurrentCell(-1, -1)
        for row, candidate in enumerate(self._visible_events):
            if candidate.event_id == record.event_id:
                self.table.setCurrentCell(row, 1)
                self.table.selectRow(row)
                break
        self.table.blockSignals(False)
        self._activate_pane(pane, align=False)
        self._pause_inactive_panes(pane)
        pane.set_passage(event, location, None if record.unknown else saved)
        self._shared_delta_ms = record.position_ms - target_position
        self.current_passage_label.setText(record.label)
        self.current_context_label.setText("待补录" if record.unknown else "已确认")
        self._video_candidate_anchor_time_ms = record.recorder_time_ms
        self._filmstrip_requested_window_start_ms = None
        self._update_filmstrip()
        self._refresh_camera_judgments()

        # 回看是明确的定位操作：原帧、胶卷和可见判读窗口一起定位。
        pane.seek_media_frame(record.position_ms, saved.frame_index)
        panel = self.video_filmstrip.full_race
        panel.set_current_frame(location, record.position_ms)
        if record.recorder_time_ms is not None:
            panel.browse_to(record.recorder_time_ms)
        if self._uses_popup_judging() and self._maximized_window is None:
            self._toggle_maximized_pane(pane)
        pane.video_view.setFocus(Qt.ShortcutFocusReason)

    def _follow_tracks(self, camera_index: int) -> FollowTracks:
        output_dir = Path(self.passage_store.journal_path).parent / FOLLOW_OUTPUT_DIRNAME
        path = follow_path(output_dir, camera_index)
        tracks = self._follow_cache.get(path)
        if tracks is None:
            tracks = self._follow_cache[path] = FollowTracks(path)
        return tracks.refresh()

    def _continuous_marker_overlays(
        self,
        pane: PassageEvidencePane,
    ) -> tuple[tuple, ...]:
        """Confirmed camera-1 markers nearby, each carried along with its runner.

        A mark is put on a runner at the line; where the analysis saw that
        runner, the number walks with them from a little before the crossing to
        a little after.  Runners the chips paired automatically carry their
        number too, in the grey-blue 自动对上 style: a pairing, not a count.
        Without a seen runner a mark stays where it was put, on its own frame.
        """

        location = pane.location
        if (
            pane.source_kind != REGULAR_SOURCE
            or location is None
            or location.segment.media_started_at_ms is None
            or pane._current_frame_index < 0
        ):
            return ()
        session_key = self._recording_session_key(location)
        offset_ms = self._continuous_clock_offsets.get(
            (int(pane.camera_index), session_key),
            int(location.clock_offset_ms),
        )
        current_recorder_time_ms = (
            int(location.segment.media_started_at_ms)
            + int(pane._current_position_ms)
        )
        tolerance_ms = max(200, int(pane.frame_duration_ms()) * 3)
        follow = self._follow_tracks(int(pane.camera_index))
        nearby = follow.around(current_recorder_time_ms)
        followed: set[int] = set()
        marked_bibs: set[str] = set()
        overlays: list[tuple] = []

        def place(mark_ms: int, x: float, y: float, label: str, *, own: bool = False) -> None:
            # ``own``: the selected runner, whose ring the pane draws on its frame.
            track = follow.owner(nearby, mark_ms, x, y) if nearby else None
            if track is not None:
                followed.add(id(track))
                here = (track.at(current_recorder_time_ms)
                        if track.near(current_recorder_time_ms, mark_ms) else None)
                if here is not None and not (own and pane._association_is_visible(pane.association)):
                    overlays.append((here[0], here[1], label, True, pane.is_auyat_rgb, True, False, False))
                return
            if not own and abs(mark_ms - current_recorder_time_ms) <= tolerance_ms:
                overlays.append((x, y, label, True, pane.is_auyat_rgb))

        for association in self._current_associations():
            if association.confirmed_source != REGULAR_SOURCE:
                continue
            event = self.passage_store.get(association.passage_event_id)
            segment = self.timeline_store.get_segment(association.segment_id)
            if (
                event is None
                or segment is None
                or int(segment.camera_index) != int(pane.camera_index)
            ):
                continue
            if (
                segment.media_started_at_ms is not None
                and association.position_ms is not None
            ):
                # Persisted marks belong to the recorder timeline of the
                # segment on which they were confirmed.  Using the chip event
                # timestamp here reintroduces the very 149/67 collision that
                # the continuous review rail is meant to prevent.
                event_recorder_time_ms = (
                    int(segment.media_started_at_ms)
                    + int(association.position_ms)
                )
            else:
                event_recorder_time_ms = int(event.timeline_timestamp_ms) + int(
                    offset_ms
                )
            if abs(event_recorder_time_ms - current_recorder_time_ms) > FOLLOW_BEFORE_MS + FOLLOW_AFTER_MS:
                continue
            label = event.bib.strip() or association.bib.strip() or "未知"
            marked_bibs.add(label)
            place(event_recorder_time_ms, float(association.marker_x_normalized),
                  float(association.marker_y_normalized), label,
                  own=(association.passage_event_id == self._selected_event_id
                       and pane.association is not None))
        # Unknown/待补录 markers live on their own continuous timeline and
        # must remain visible independently of the selected roster identity.
        for marker in self.continuous_marker_store.markers():
            if int(marker.camera_index) != int(pane.camera_index):
                continue
            segment = self.timeline_store.get_segment(marker.segment_id)
            if segment is None or segment.media_started_at_ms is None:
                continue
            marker_recorder_time_ms = int(segment.media_started_at_ms) + int(marker.position_ms)
            if abs(marker_recorder_time_ms - current_recorder_time_ms) > FOLLOW_BEFORE_MS + FOLLOW_AFTER_MS:
                continue
            place(marker_recorder_time_ms, float(marker.marker_x_normalized),
                  float(marker.marker_y_normalized), marker.label)
        # Runners the chips paired by themselves: their number, not yet counted.
        for track in nearby:
            if id(track) in followed or not track.bib or track.bib in marked_bibs:
                continue
            if not track.near(current_recorder_time_ms, track.crossing_ms):
                continue
            here = track.at(current_recorder_time_ms)
            if here is not None:
                overlays.append((here[0], here[1], track.bib, False, pane.is_auyat_rgb, True, False, True))
        return tuple(overlays)

    def _on_preview_frame_ready(
        self,
        pane: PassageEvidencePane,
        image,
        position_ms: int,
        frame_index: int,
    ) -> None:
        if pane is self._camera_one_pane():
            self.video_filmstrip.full_race.set_current_frame(pane.location, position_ms)
        history_markers = self._continuous_marker_overlays(pane)
        pane.video_view.set_history_markers(history_markers)
        if pane is not self._active_playback_pane():
            return
        self.preview_video_view.set_frame(
            image,
            source_width=pane._source_width,
            source_height=pane._source_height,
        )
        self.preview_video_view.set_frame_indicator(
            pane.frame_indicator_label.text()
        )
        self.preview_video_view.set_history_markers(history_markers)
        self.preview_video_view.set_marker_mode(
            self.preview_mark_btn.isChecked()
        )
        can_mark = (
            pane is self._filmstrip_judgment_pane()
            and pane._current_frame_index >= 0
            and pane.location is not None
            and pane.location.status in _CONFIRMABLE_STATUSES
        )
        self.preview_mark_btn.setEnabled(can_mark)
        if not can_mark:
            self.preview_mark_btn.setChecked(False)
            self.preview_confirm_btn.setEnabled(False)
        pending = pane.pending_confirmation()
        if pending is not None:
            self.preview_video_view.set_marker(
                float(pending["marker_x_normalized"]),
                float(pending["marker_y_normalized"]),
                pane._identity,
                confirmed=False,
                simple=pane.is_auyat_rgb,
            )
        elif pane.association is not None:
            self.preview_video_view.set_marker(
                pane.association.marker_x_normalized,
                pane.association.marker_y_normalized,
                pane._identity,
                confirmed=True,
                simple=pane.is_auyat_rgb,
            )
        else:
            self.preview_video_view.clear_marker()

    def _on_preview_marker_selected(
        self,
        x_normalized: float,
        y_normalized: float,
    ) -> None:
        pane = self._filmstrip_judgment_pane()
        if pane is None:
            return
        self._activate_pane(pane, align=False)
        pane._on_marker_position_selected(
            float(x_normalized),
            float(y_normalized),
        )
        self.preview_video_view.set_marker(
            float(x_normalized),
            float(y_normalized),
            pane._identity,
            confirmed=False,
            simple=pane.is_auyat_rgb,
        )
        self.preview_mark_btn.setChecked(True)
        self.preview_confirm_btn.setEnabled(True)

    def _confirm_preview_marker(self) -> None:
        pane = self._filmstrip_judgment_pane()
        if pane is None or pane.pending_confirmation() is None:
            return
        if not self._confirm_pending_marker(pane):
            return
        association = pane.association
        if association is not None:
            self.preview_video_view.set_marker(
                association.marker_x_normalized,
                association.marker_y_normalized,
                pane._identity,
                confirmed=True,
                simple=pane.is_auyat_rgb,
            )
        else:
            self.preview_video_view.clear_marker()
        self.preview_mark_btn.setChecked(False)
        self.preview_confirm_btn.setEnabled(False)

    def _on_filmstrip_marker_selected(
        self,
        position_ms: int,
        marker_x_normalized: float,
        marker_y_normalized: float,
        frame_index: int,
    ) -> None:
        pane = self._filmstrip_judgment_pane()
        if pane is None:
            return
        self._activate_pane(pane, align=False)
        self.preview_mark_btn.setChecked(True)
        self.preview_mark_btn.setEnabled(True)
        self.preview_confirm_btn.setEnabled(True)
        selected_event = self.passage_store.get(self._selected_event_id)
        nearby_event_id = self._filmstrip_event_id_at_position(int(position_ms))
        unknown_marker = (
            selected_event is None
            or not selected_event.bib.strip()
            or selected_event.bib.strip().casefold() in {"unknown", "未知", "待补录"}
            or not nearby_event_id
        )
        pane.set_external_pending_marker(
            frame_index=int(frame_index),
            position_ms=int(position_ms),
            marker_x_normalized=float(marker_x_normalized),
            marker_y_normalized=float(marker_y_normalized),
            unknown_marker=unknown_marker,
            marker_label="待补录" if unknown_marker else "",
        )
        self.video_filmstrip.set_marker(
            int(position_ms),
            float(marker_x_normalized),
            float(marker_y_normalized),
            int(frame_index),
        )

    def _confirm_filmstrip_marker(self) -> bool:
        pane = self._filmstrip_judgment_pane()
        if pane is None or pane.pending_confirmation() is None:
            return False
        confirmed = self._confirm_pending_marker(pane)
        if confirmed:
            self.video_filmstrip.clear_marker()
            self.preview_mark_btn.setChecked(False)
            self.preview_confirm_btn.setEnabled(False)
        return confirmed

    def _clear_filmstrip_marker(self) -> None:
        pane = self._filmstrip_judgment_pane()
        if pane is not None:
            pane.cancel_marker_edit()
        self.video_filmstrip.clear_marker()
        self.preview_mark_btn.setChecked(False)
        self.preview_confirm_btn.setEnabled(False)

    def _preview_filmstrip_position(self, position_ms: int) -> None:
        """Preview the latest drag position without queuing every mouse move."""

        self._mark_filmstrip_operator_busy()
        self._pending_filmstrip_preview_position = int(position_ms)
        self.video_filmstrip.set_current_position(int(position_ms))
        if hasattr(self, "preview_timeline") and self.preview_timeline.maximum() > self.preview_timeline.minimum():
            self.preview_timeline.setValue(
                max(
                    self.preview_timeline.minimum(),
                    min(int(position_ms), self.preview_timeline.maximum()),
                )
            )
        if not self._filmstrip_preview_timer.isActive():
            self._filmstrip_preview_timer.start()

    def _on_preview_timeline_pressed(self) -> None:
        self._preview_timeline_dragging = True

    def _on_preview_timeline_moved(self, position_ms: int) -> None:
        if not getattr(self, "_preview_timeline_dragging", False):
            return
        self._preview_filmstrip_position(int(position_ms))

    def _on_preview_timeline_released(self) -> None:
        self._preview_timeline_dragging = False
        self._seek_filmstrip_position(int(self.preview_timeline.value()))

    def _flush_filmstrip_preview(self) -> None:
        position_ms = self._pending_filmstrip_preview_position
        self._pending_filmstrip_preview_position = None
        if position_ms is None:
            return
        pane = self._filmstrip_judgment_pane()
        if pane is None or pane.available_delta_bounds() is None:
            return
        target_delta_ms = int(position_ms) - int(pane._target_position_ms)
        if self.SINGLE_CAMERA_PREVIEW:
            self._seek_preview_pane_delta(pane, target_delta_ms)
        else:
            self._preview_both_delta(target_delta_ms)

    def _move_filmstrip_batch(self, direction: int) -> None:
        self.video_filmstrip.full_race.page(1 if direction >= 0 else -1)

    def _seek_filmstrip_position(self, position_ms: int) -> None:
        self._mark_filmstrip_operator_busy()
        self._filmstrip_preview_timer.stop()
        self._pending_filmstrip_preview_position = None
        pane = self._filmstrip_judgment_pane()
        # The filmstrip is a continuous-camera locating surface, not an
        # identity selector.  Clicking a nearby thumbnail must seek camera 1
        # while leaving the selected roster identity unchanged; otherwise a
        # missing-chip rider could be silently reassigned to its neighbour.
        self._pending_filmstrip_position = int(position_ms)
        self._filmstrip_seek_retry_count = 0
        self.video_filmstrip.set_current_position(int(position_ms))
        if hasattr(self, "preview_timeline") and self.preview_timeline.maximum() > self.preview_timeline.minimum():
            self.preview_timeline.setValue(
                max(
                    self.preview_timeline.minimum(),
                    min(int(position_ms), self.preview_timeline.maximum()),
                )
            )
        if self._filmstrip_seek_pending:
            return
        self._filmstrip_seek_pending = True
        QTimer.singleShot(40, self._flush_filmstrip_position)

    def _open_filmstrip_position_for_judgment(self, position_ms: int) -> None:
        """Seek the linked cameras and enlarge the active pane for marking."""

        self._seek_filmstrip_position(position_ms)
        pane = self._camera_one_pane()
        QTimer.singleShot(60, lambda: self._toggle_maximized_pane(pane))

    def _rebind_pane_to_filmstrip_archive(
        self,
        pane: PassageEvidencePane,
        position_ms: int,
    ) -> bool:
        """Move the judge pane from a chip clip to the two-minute archive batch."""

        context = self._filmstrip_context
        if context is None:
            return False
        event = self.passage_store.get(self._selected_event_id)
        if event is None:
            return False
        filmstrip_path, _start_ms, _end_ms = context
        absolute_window = self._filmstrip_absolute_window
        if absolute_window is None:
            return False
        media_origin_ms = int(absolute_window[0]) - int(_start_ms)
        target_absolute_ms = media_origin_ms + int(position_ms)
        location = self._live_location_for_filmstrip(
            pane,
            target_absolute_ms,
        ) or self._archive_location_for_filmstrip(
            pane,
            target_absolute_ms,
        )
        if location is None or location.video_path.resolve() != filmstrip_path.resolve():
            return False
        current_path = (
            pane.location.video_path.resolve()
            if pane.location is not None
            else None
        )
        current_duration = int(
            getattr(pane.location.segment, "media_duration_ms", 0)
            if pane.location is not None
            else 0
        )
        current_position = int(getattr(pane, "_current_position_ms", 0))
        if (
            current_path == filmstrip_path.resolve()
            and 0 <= current_position <= current_duration
            and current_duration >= int(location.segment.media_duration_ms or 0)
        ):
            return False
        pane.set_passage(
            event,
            location,
            initial_delta_ms=(
                int(position_ms) - int(location.playback_position_ms)
            ),
            lookup_status=location.status,
        )
        offset_ms = self._continuous_offset_for_location(location)
        self._shared_delta_ms = target_absolute_ms - (
            int(event.timeline_timestamp_ms) + int(offset_ms)
        )
        self._update_shared_time_label()
        return True

    def _flush_filmstrip_position(self) -> None:
        self._filmstrip_seek_pending = False
        position_ms = self._pending_filmstrip_position
        self._pending_filmstrip_position = None
        if position_ms is None:
            return
        preferred_pane = self._camera_one_pane()
        seek_pane = next(
            (
                pane
                for pane in (preferred_pane, *self.evidence_panes)
                if pane.location is not None
                and pane.available_delta_bounds() is not None
            ),
            None,
        )
        if seek_pane is None:
            # The filmstrip can be ready before the lower video worker has
            # emitted metadata. Keep the requested position briefly so a
            # double-click still links to both camera panes once duration and
            # frame bounds become available.
            if (
                preferred_pane.location is not None
                and self._filmstrip_seek_retry_count < 25
            ):
                self._filmstrip_seek_retry_count += 1
                self._filmstrip_seek_pending = True
                QTimer.singleShot(120, self._flush_filmstrip_position)
                return
            self._pending_filmstrip_position = None
            return
        self._filmstrip_seek_retry_count = 0
        if self._rebind_pane_to_filmstrip_archive(seek_pane, position_ms):
            self.video_filmstrip.set_current_position(int(position_ms))
            return
        target_delta_ms = int(position_ms) - int(seek_pane._target_position_ms)
        # The filmstrip represents the shared race timeline.  Move every
        # visible camera together so each pane applies its own calibrated
        # passage target instead of leaving the secondary angle behind.
        if self.SINGLE_CAMERA_PREVIEW:
            self._seek_preview_pane_delta(seek_pane, target_delta_ms)
        else:
            self._seek_both_delta(target_delta_ms)

    def _seek_preview_pane_delta(
        self,
        pane: PassageEvidencePane,
        delta_ms: int,
    ) -> None:
        """Seek only the active review camera during low-power preview."""

        self._activate_pane(pane, align=False)
        bounds = pane.available_delta_bounds()
        if bounds is None:
            return
        lower, upper = bounds
        self._shared_delta_ms = max(lower, min(int(delta_ms), upper))
        pane.seek_passage_delta(self._shared_delta_ms, preview=True)
        self._update_shared_time_label()

    def _filmstrip_event_id_at_position(self, position_ms: int) -> str:
        """Resolve a normal filmstrip click to the nearest visible athlete."""

        pane = self._camera_one_pane()
        location = pane.location
        if location is None:
            return ""
        target_path = str(Path(location.video_path).absolute())
        candidates: list[tuple[int, int, str]] = []
        for row, event in enumerate(self._visible_events):
            lookup = self._lookups.get(event.event_id)
            if lookup is None:
                continue
            event_location = self._regular_location_for_camera(
                lookup,
                pane.camera_index,
            )
            if event_location is None:
                continue
            projected = self._location_on_current_media(event, pane)
            if projected is None or projected.segment.segment_id != location.segment.segment_id:
                continue
            if str(Path(event_location.video_path).absolute()) != target_path:
                continue
            distance = abs(int(projected.passage_position_ms) - int(position_ms))
            candidates.append((distance, row, event.event_id))
        if not candidates:
            return ""
        distance, _row, event_id = min(candidates)
        # A sparse strip is sampled every two seconds; do not retarget the
        # athlete list when a click is clearly outside its passage window.
        return event_id if distance <= 1_500 else ""

    def _skip_continuous_gap(
        self,
        event: PassageEvent,
        pane: PassageEvidencePane,
    ) -> bool:
        if (
            not self.CONTINUOUS_AUTO_SKIP
            or not self._batch_mode
            or pane.location is None
        ):
            return False
        target_position_ms = int(pane.location.passage_position_ms)
        current_position_ms = int(pane._current_position_ms)
        gap_ms = target_position_ms - current_position_ms
        if abs(gap_ms) < self.CONTINUOUS_SKIP_GAP_MS:
            return False
        seek_position_ms = max(0, target_position_ms - self.CONTINUOUS_SKIP_LEAD_MS)
        if gap_ms > 0:
            seek_position_ms = max(current_position_ms, seek_position_ms)
        self._shared_delta_ms = seek_position_ms - target_position_ms
        pane.seek_passage_delta(self._shared_delta_ms)
        self._update_shared_time_label()
        return True

    def _update_navigation_controls(self) -> None:
        row = self.table.currentRow()
        self.previous_passage_btn.setEnabled(row > 0)
        self.next_passage_btn.setEnabled(0 <= row < self.table.rowCount() - 1)

    def _clear_selection_details(self) -> None:
        self._set_sync_playing(False, seek_final=False)
        self._deferred_start_timer.stop()
        self._deferred_selection_panes.clear()
        self._selection_pending_panes.clear()
        self._filmstrip_preview_timer.stop()
        self._pending_filmstrip_preview_position = None
        self._pending_filmstrip_position = None
        self._shared_delta_ms = 0
        self._selected_event_id = ""
        metadata = self._current_metadata()
        self.race_value.setText(
            (metadata.race_name.strip() or metadata.race_id)
            if metadata is not None
            else "--"
        )
        self.stage_value.setText(
            (metadata.stage_name.strip() or metadata.stage_id)
            if metadata is not None
            else "--"
        )
        selected_group = str(self.group_combo.currentData() or "")
        self.group_value.setText(
            metadata.group_label(selected_group)
            if metadata is not None and selected_group
            else "--"
        )
        for label in (
            self.selected_identity_value,
            self.athlete_value,
            self.team_value,
            self.selected_time_value,
            self.source_value,
        ):
            label.setText("--")
        self.current_passage_label.setText("未选择通过记录")
        self.current_context_label.clear()
        self.current_time_label.setText("--:--:--.---")
        self.current_time_label.setToolTip("")
        self.batch_context_label.clear()
        self.batch_context_label.setToolTip("")
        self.batch_review_btn.setVisible(False)
        self.video_discovered_btn.setEnabled(False)
        self.video_discovered_table.setVisible(False)
        for pane in self.evidence_panes:
            pane.clear_passage()
        self.preview_video_view.clear_frame("选择一条通过记录后在这里预览判读")
        self.preview_confirm_btn.setEnabled(False)
        self._update_navigation_controls()

    def focus_athlete(
        self,
        race_id: str,
        stage_id: str,
        *,
        athlete_id: str = "",
        bib: str = "",
        group_id: str = "",
    ) -> bool:
        """Select the newest passage or show roster-only details without a modal."""

        race_id = str(race_id).strip()
        stage_id = str(stage_id).strip()
        athlete_id = str(athlete_id).strip()
        bib = str(bib).strip()
        group_id = str(group_id).strip()
        metadata = self._current_metadata()
        if metadata is not None and (
            metadata.race_id != race_id or metadata.stage_id != stage_id
        ):
            return False

        events = tuple(
            event
            for event in self._events_for_current_metadata(self.passage_store.events())
            if event.race_id == race_id and event.stage_id == stage_id
        )
        athlete_matches = (
            [event for event in events if athlete_id and event.athlete_id == athlete_id]
            if athlete_id
            else []
        )
        matches = athlete_matches or [
            event for event in events if bib and event.bib.strip() == bib
        ]
        if matches:
            event = max(
                matches,
                key=lambda item: (
                    item.timeline_timestamp_ms,
                    item.sequence,
                    item.revision,
                ),
            )
            target_group = event.group_id or group_id
            group_index = self.group_combo.findData(target_group)
            if group_index >= 0 and group_index != self.group_combo.currentIndex():
                self.group_combo.setCurrentIndex(group_index)
            row = next(
                (
                    index
                    for index, visible in enumerate(self._visible_events)
                    if visible.event_id == event.event_id
                ),
                -1,
            )
            if row < 0:
                self.refresh()
                row = next(
                    (
                        index
                        for index, visible in enumerate(self._visible_events)
                        if visible.event_id == event.event_id
                    ),
                    -1,
                )
            if row < 0:
                return False
            self.table.blockSignals(True)
            self.table.setCurrentCell(row, 1)
            self.table.selectRow(row)
            self.table.blockSignals(False)
            item = self.table.item(row, 1)
            if item is not None:
                self.table.scrollToItem(item, QAbstractItemView.PositionAtCenter)
            self._select_event(event.event_id)
            return True

        roster_athlete = None
        if metadata is not None:
            for candidate in metadata.athletes:
                if athlete_id and candidate.athlete_id == athlete_id:
                    roster_athlete = candidate
                    break
            if roster_athlete is None and bib:
                roster_athlete = next(
                    (
                        candidate
                        for candidate in metadata.athletes
                        if candidate.bib.strip() == bib
                    ),
                    None,
                )
        target_group = (
            roster_athlete.group_id if roster_athlete is not None else group_id
        )
        group_index = self.group_combo.findData(target_group)
        if group_index >= 0 and group_index != self.group_combo.currentIndex():
            self.group_combo.setCurrentIndex(group_index)
        self.table.blockSignals(True)
        self.table.clearSelection()
        self.table.setCurrentCell(-1, -1)
        self.table.blockSignals(False)
        self._clear_selection_details()

        identity = (
            roster_athlete.bib.strip()
            if roster_athlete is not None
            else bib
        )
        athlete_name = (
            roster_athlete.name.strip() if roster_athlete is not None else ""
        )
        team_name = (
            roster_athlete.team_name.strip() if roster_athlete is not None else ""
        )
        if metadata is not None:
            self.race_value.setText(metadata.race_name.strip() or metadata.race_id)
            self.stage_value.setText(metadata.stage_name.strip() or metadata.stage_id)
            self.group_value.setText(
                metadata.group_label(target_group) if target_group else "--"
            )
        self.selected_identity_value.setText(identity or "--")
        self.athlete_value.setText(athlete_name or "--")
        self.team_value.setText(team_name or "--")
        self.selected_time_value.setText("尚无通过记录")
        self.source_value.setText("等待 CycleRace 通过时间")
        athlete_summary = f"{identity} {athlete_name}".strip()
        self.current_passage_label.setText(
            f"名单运动员 {athlete_summary}（尚无通过记录）"
        )
        return bool(roster_athlete is not None or identity)

    def _begin_marking(self, pane: PassageEvidencePane) -> None:
        self._set_sync_playing(False)
        for candidate in self.evidence_panes:
            if candidate is not pane:
                candidate.cancel_marker_edit()
        pane.begin_marking()

    def _update_reference_states(self, event_id: str) -> None:
        confirmed = any(
            self._association_for_event(event_id, source_kind) is not None
            for source_kind in (REGULAR_SOURCE, HIGH_SPEED_SOURCE)
        )
        for pane in self.evidence_panes:
            pane.set_reference_only(confirmed and pane.association is None)

    def _calibrate_continuous_session(
        self,
        event: PassageEvent,
        pane: PassageEvidencePane,
        association: PassageEvidenceAssociation,
    ) -> bool:
        return self._calibrate_continuous_session_at_position(
            event,
            pane,
            int(association.position_ms),
            int(association.confirmed_at_ms),
        )

    def _calibrate_continuous_session_at_position(
        self,
        event: PassageEvent,
        pane: PassageEvidencePane,
        position_ms: int,
        calibrated_at_ms: int,
    ) -> bool:
        location = pane.location
        if (
            pane.source_kind != REGULAR_SOURCE
            or location is None
            or location.segment.media_started_at_ms is None
        ):
            return False
        key = (
            location.segment.camera_index,
            self._recording_session_key(location),
        )
        if key in self._continuous_clock_offsets or self.calibration_store.get(*key) is not None:
            return False
        offset_ms = (
            int(location.segment.media_started_at_ms)
            + int(position_ms)
            - int(event.timeline_timestamp_ms)
        )
        self._continuous_clock_offsets[key] = offset_ms
        anchor_frame = {
            "segment_id": str(location.segment.segment_id),
            "position_ms": int(position_ms),
        }
        try:
            self.calibration_store.record(
                camera_index=location.segment.camera_index,
                session_key=key[1],
                offset_ms=offset_ms,
                anchor_event_id=event.event_id,
                anchor_bib=event.bib.strip() or "未知",
                calibrated_at_ms=int(calibrated_at_ms),
                anchor_event_revision=int(event.revision),
                anchor_chip_time_ms=int(event.timeline_timestamp_ms),
                anchor_frame=anchor_frame,
                anchor_race_id=event.race_id,
                anchor_stage_id=event.stage_id,
            )
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            logger.warning("failed to persist camera calibration: %s", error)
        self._continuous_calibration_revision += 1
        self._update_batch_controls(event.event_id)
        self._lookup_cache.clear()
        events = self._events_for_current_metadata(self.passage_store.events())
        self._lookups = {
            candidate.event_id: self._cached_lookup(candidate)
            for candidate in events
        }
        self.batch_context_label.setToolTip(
            f"机位 {location.segment.camera_index} 已按首人校准 {offset_ms:+d} ms"
        )
        return True

    SAME_FRAME_MARKER_DISTANCE = 0.03

    def _same_frame_confirmations(self, event, source_kind, location, pending) -> list[str]:
        """Other bibs already confirmed on this frame at nearly the same spot.

        Frames are compared by recorder time (segment start + position) so a
        short clip and the archive of the same moment count as one frame.
        """

        segment = location.segment
        if segment.media_started_at_ms is None:
            return []
        frame_time = int(segment.media_started_at_ms) + int(pending["position_ms"])
        x, y = float(pending["marker_x_normalized"]), float(pending["marker_y_normalized"])
        conflicts = []
        for association in self.association_store.associations():
            if (
                association.passage_event_id == event.event_id
                or association.confirmed_source != source_kind
                or association.confirmation_status != "confirmed"
            ):
                continue
            other = self.timeline_store.get_segment(association.segment_id)
            if other is None or other.media_started_at_ms is None:
                continue
            other_time = int(other.media_started_at_ms) + int(association.position_ms)
            distance = ((association.marker_x_normalized - x) ** 2 + (association.marker_y_normalized - y) ** 2) ** 0.5
            if abs(other_time - frame_time) <= 25 and distance <= self.SAME_FRAME_MARKER_DISTANCE:
                conflicts.append(association.bib)
        return conflicts

    def _confirm_pending_marker(self, pane: PassageEvidencePane) -> bool:
        event = self.passage_store.get(self._selected_event_id)
        pending = pane.pending_confirmation()
        if pending is None:
            return False
        if bool(pending.get("unknown_marker")):
            location = pane.location
            if location is None:
                return False
            try:
                location = self._persist_filmstrip_location(location)
                pane._location = location
                pending["segment_id"] = location.segment.segment_id
                self.continuous_marker_store.create(
                    camera_index=max(1, int(location.segment.camera_index)),
                    segment_id=str(pending["segment_id"]),
                    frame_index=int(pending["frame_index"]),
                    position_ms=int(pending["position_ms"]),
                    marker_x_normalized=float(pending["marker_x_normalized"]),
                    marker_y_normalized=float(pending["marker_y_normalized"]),
                    label=str(pending.get("marker_label") or "待补录"),
                    passage_event_id=(event.event_id if event is not None else ""),
                    bib=(event.bib.strip() if event is not None else ""),
                    confirmed_at_ms=int(time.time() * 1000.0),
                )
            except (OSError, RuntimeError, TypeError, ValueError) as error:
                QMessageBox.critical(self, "保存失败", f"无法保存待补录标记：{error}")
                return False
            pane.cancel_marker_edit()
            self.video_filmstrip.clear_marker()
            self.preview_mark_btn.setChecked(False)
            self.preview_confirm_btn.setEnabled(False)
            self._refresh_camera_judgments()
            return True
        if event is None:
            return False
        pane_event = getattr(pane, "_event", None)
        if (pane_event is None or pane_event.event_id != event.event_id
                or pane_event.revision != event.revision or not event.is_active):
            logger.error(
                "Refused mismatched confirmation selected=%s pane=%s",
                event.event_id,
                getattr(pane_event, "event_id", ""),
            )
            return False
        identity = event.bib.strip() or "未知"
        is_rejudgment = (
            self._association_for_event(event.event_id, pane.source_kind) is not None
        )
        try:
            location = self._persist_filmstrip_location(pane.location)
            if location is None:
                return False
            pane._location = location
            pending["segment_id"] = location.segment.segment_id
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            QMessageBox.critical(self, "保存失败", f"无法保存证据标记：{error}")
            return False
        conflicts = self._same_frame_confirmations(event, pane.source_kind, location, pending)
        if conflicts and QMessageBox.question(
            self, "同一帧已确认其他号码",
            f"该帧同一位置已确认 {'、'.join(conflicts)}。\n"
            f"再确认 {identity} 前请核对号码牌，是否仍要确认？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            return False
        try:
            association = self.association_store.confirm(
                passage_event_id=event.event_id,
                bib=identity,
                confirmed_source=pane.source_kind,
                segment_id=str(pending["segment_id"]),
                frame_index=int(pending["frame_index"]),
                position_ms=int(pending["position_ms"]),
                marker_x_normalized=float(pending["marker_x_normalized"]),
                marker_y_normalized=float(pending["marker_y_normalized"]),
                confirmed_at_ms=int(time.time() * 1000.0),
                passage_revision=event.revision,
            )
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            QMessageBox.critical(self, "保存失败", f"无法保存证据标记：{error}")
            return False
        confirmed_row = self.table.currentRow()
        pane.set_association(association)
        self._calibrate_continuous_session(event, pane, association)
        for candidate in self.evidence_panes:
            if candidate is pane or candidate.source_kind != pane.source_kind:
                continue
            candidate.set_association(None)
        self._update_reference_states(event.event_id)
        should_advance = (self._batch_mode and not is_rejudgment) or (
            self.auto_advance_checkbox.isChecked()
            and self._all_available_sources_confirmed(event.event_id)
        )
        self._update_event_confirmation_status(event.event_id)
        if should_advance and confirmed_row >= 0:
            self._move_selection(
                1,
                skip_confirmed=True,
                preserve_current_frame=True,
            )
        return True

    def _update_current_context(self, display_status: str) -> None:
        row = self.table.currentRow()
        position = row + 1 if 0 <= row < len(self._visible_events) else 0
        self.current_context_label.setText(" · ".join(
            value for value in (self.group_value.text().strip(),
                                f"{position:,} / {len(self._visible_events):,}", display_status)
            if value and value != "--"
        ))

    def _update_event_confirmation_status(self, event_id: str) -> None:
        self._refresh_camera_judgments()
        lookup = self._lookups.get(event_id)
        if lookup is None:
            return
        regular = self._source_location_with_saved_association(
            event_id,
            lookup,
            high_speed=False,
        )
        high_speed = self._source_location_with_saved_association(
            event_id,
            lookup,
            high_speed=True,
        )
        regular_association = self._association_for_event(event_id, REGULAR_SOURCE)
        high_speed_association = self._association_for_event(event_id, HIGH_SPEED_SOURCE)
        readiness_status = review_status_text(lookup, regular, high_speed)
        status = self._confirmation_status(
            regular_association,
            high_speed_association,
            readiness_status,
        )
        self._event_review_statuses[event_id] = status
        self._record_summary_state(
            event_id,
            regular,
            high_speed,
            readiness_status,
            regular_association,
            high_speed_association,
        )
        row_values = (
            (6, source_confirmation_status(regular, regular_association)),
            (7, source_confirmation_status(high_speed, high_speed_association)),
            (8, self._display_confirmation_status(status)),
        )
        for row, event in enumerate(self._visible_events):
            if event.event_id != event_id:
                continue
            for column, value in row_values:
                item = self.table.item(row, column)
                if item is not None:
                    item.setText(value)
                    self._apply_status_style(item, value)
            break
        if event_id == self._selected_event_id:
            self.source_value.setText(self._display_confirmation_status(status))
            self._update_current_context(self._display_confirmation_status(status))
        batch = self._active_review_batch()
        if self._batch_mode and batch is not None:
            self._update_batch_roster_overlays(batch)
        if self._active_review_filter != "all":
            self._refresh_filtered_view()
            return
        self._render_summary()

    def _record_summary_state(
        self,
        event_id: str,
        regular: Optional[PassageVideoLocation],
        high_speed: Optional[PassageVideoLocation],
        _readiness_status: str,
        regular_association: Optional[PassageEvidenceAssociation],
        high_speed_association: Optional[PassageEvidenceAssociation],
    ) -> None:
        self._set_membership(
            self._located_event_ids,
            event_id,
            any(
                location is not None and location.status in _OPENABLE_STATUSES
                for location in (regular, high_speed)
            ),
        )
        self._set_membership(
            self._confirmed_event_ids,
            event_id,
            regular_association is not None or high_speed_association is not None,
        )

    @staticmethod
    def _set_membership(values: set[str], event_id: str, present: bool) -> None:
        if present:
            values.add(event_id)
        else:
            values.discard(event_id)

    def _discard_summary_state(self, event_id: str) -> None:
        self._located_event_ids.discard(event_id)
        self._confirmed_event_ids.discard(event_id)
        self._event_review_statuses.pop(event_id, None)

    def _render_summary(self) -> None:
        counts = {
            "pending": sum(
                status == "待核对" for status in self._event_review_statuses.values()
            ),
            "blocked": sum(
                status == "受阻" for status in self._event_review_statuses.values()
            ),
            "confirmed": sum(
                status == "已确认" for status in self._event_review_statuses.values()
            ),
            "all": self._total_event_count,
        }
        for filter_key, button in self.review_filter_buttons.items():
            prefix = "✓ " if filter_key == self._active_review_filter else ""
            button.setText(
                f"{prefix}{self.review_filter_labels[filter_key]} "
                f"{counts[filter_key]:,}"
            )
        active_label = self.review_filter_labels[self._active_review_filter]
        if self._active_review_filter == "all":
            summary = (
                f"当前显示 {len(self._visible_events):,} / "
                f"{self._total_event_count:,} 条"
            )
        else:
            summary = (
                f"当前筛选：{active_label} · "
                f"{len(self._visible_events):,} / {self._total_event_count:,} 条"
            )
        self.summary_label.setText(summary)
        self._available_evidence_count = len(self._located_event_ids)
        self.summary_label.setText(
            f"{summary} · 可回看 {self._available_evidence_count:,}"
        )
        self.summary_label.setToolTip("可回看表示录像已可用；是否完成判读以已确认为准。")

    def _all_available_sources_confirmed(self, event_id: str) -> bool:
        lookup = self._lookups.get(event_id)
        if lookup is None:
            return False
        for location in lookup.locations:
            if location.status not in _OPENABLE_STATUSES:
                continue
            source_kind = HIGH_SPEED_SOURCE if is_high_speed(location) else REGULAR_SOURCE
            if self._source_association(event_id, source_kind, location) is not None:
                return True
        return False

    def _cancel_pending_marker(self, pane: PassageEvidencePane) -> None:
        pane.cancel_marker_edit()

    def _delete_marker(self, pane: PassageEvidencePane) -> None:
        association = pane.association
        if association is None:
            pane.cancel_marker_edit()
            return
        answer = QMessageBox.question(
            self,
            "删除证据标记",
            f"确定删除 {association.bib} 号在{pane.title_label.text()}中的人工标记吗？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        try:
            self.association_store.clear(
                association.passage_event_id,
                association.confirmed_source,
                confirmed_at_ms=int(time.time() * 1000.0),
            )
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            QMessageBox.critical(self, "删除失败", f"无法删除证据标记：{error}")
            return
        pane.set_association(None)
        self._update_reference_states(association.passage_event_id)
        self._update_event_confirmation_status(association.passage_event_id)

    def _find_identity(self) -> None:
        value = self.identity_search.text().strip().casefold()
        if not value:
            return
        matches = []
        for row, event in enumerate(self._visible_events):
            metadata_athlete = self._metadata_athlete_for_event(event)
            athlete_name = event.athlete_name.strip() or (
                metadata_athlete.name.strip() if metadata_athlete is not None else ""
            )
            if value in {
                event.bib.strip().casefold(),
                athlete_name.casefold(),
            }:
                matches.append((row, event))
        if matches:
            row, _event = max(
                matches,
                key=lambda match: (
                    match[1].timeline_timestamp_ms,
                    match[1].sequence,
                    match[1].revision,
                ),
            )
            self.table.setCurrentCell(row, 1)
            self.table.selectRow(row)
            item = self.table.item(row, 1)
            if item is not None:
                self.table.scrollToItem(item, QAbstractItemView.PositionAtCenter)
            return
        metadata = self._current_metadata()
        if metadata is not None:
            selected_group = str(self.group_combo.currentData() or "")
            for athlete in metadata.athletes:
                if selected_group and athlete.group_id != selected_group:
                    continue
                if not (
                    athlete.matches_identity(value)
                    or athlete.name.strip().casefold() == value
                ):
                    continue
                identity = athlete.bib.strip() or "未知"
                self.race_value.setText(metadata.race_name or metadata.race_id)
                self.stage_value.setText(metadata.stage_name or metadata.stage_id)
                self.group_value.setText(metadata.group_label(athlete.group_id))
                self.selected_identity_value.setText(identity)
                self.athlete_value.setText(athlete.name.strip() or "--")
                self.team_value.setText(athlete.team_name.strip() or "--")
                self.selected_time_value.setText("尚无通过记录")
                self.source_value.setText("等待 CycleRace 通过时间")
                athlete_summary = f"{identity} {athlete.name}".strip()
                self.current_passage_label.setText(
                    f"名单运动员 {athlete_summary}（尚无通过记录）"
                )
                return
        QMessageBox.information(self, "未找到", "当前组别没有该号码或姓名的通过记录。")

    def _move_selection(
        self,
        delta: int,
        *,
        skip_confirmed: bool = False,
        preserve_current_frame: bool = False,
    ) -> None:
        if self.table.rowCount() <= 0:
            return
        current_row = self.table.currentRow()
        direction = 1 if int(delta) >= 0 else -1
        row = current_row + int(delta)
        if skip_confirmed:
            while 0 <= row < self.table.rowCount():
                candidate = self._visible_events[row]
                has_evidence = (
                    self._association_for_event(candidate.event_id, REGULAR_SOURCE)
                    is not None
                    or self._association_for_event(
                        candidate.event_id,
                        HIGH_SPEED_SOURCE,
                    )
                    is not None
                )
                if not has_evidence:
                    break
                row += direction
            if not 0 <= row < self.table.rowCount():
                return
        row = max(0, min(row, self.table.rowCount() - 1))
        if row == self.table.currentRow():
            return
        event_id = self._visible_events[row].event_id
        self.table.blockSignals(True)
        self.table.setCurrentCell(row, 1)
        self.table.selectRow(row)
        self.table.blockSignals(False)
        item = self.table.item(row, 1)
        if item is not None:
            self.table.scrollToItem(item, QAbstractItemView.PositionAtCenter)
        active_pane = self._active_playback_pane()
        moving_back_in_continuous_mode = self._batch_mode and int(delta) < 0
        keep_current_frame = preserve_current_frame or (
            getattr(active_pane, "_current_frame_index", -1) >= 0
            and self._selection_is_near_current_event(event_id)
        )
        self._select_event(
            event_id,
            preserve_current_frame=(active_pane if keep_current_frame else None),
        )
        if not moving_back_in_continuous_mode:
            return
        if self._seek_saved_confirmation(event_id):
            return
        event = self.passage_store.get(event_id)
        if event is not None:
            self._skip_continuous_gap(event, active_pane)

    def _selection_is_near_current_event(self, event_id: str) -> bool:
        current = self.passage_store.get(self._selected_event_id)
        target = self.passage_store.get(event_id)
        if current is None or target is None:
            return False
        return (
            abs(
                int(target.timeline_timestamp_ms)
                - int(current.timeline_timestamp_ms)
            )
            <= self.MANUAL_CONTINUATION_WINDOW_MS
        )

    def _seek_saved_confirmation(self, event_id: str) -> bool:
        association = self._association_for_event(event_id, REGULAR_SOURCE)
        if association is None:
            return False
        segment = self.timeline_store.get_segment(association.segment_id)
        pane = (
            self._pane_for_camera(segment.camera_index)
            if segment is not None
            else self._active_playback_pane()
        )
        if pane.location is None or pane.location.segment.segment_id != association.segment_id:
            event = self.passage_store.get(event_id)
            location = self._saved_regular_location(association)
            if (
                event is None
                or location is None
                or location.status not in _OPENABLE_STATUSES
            ):
                return False
            self._activate_pane(pane, align=False)
            pane.set_passage(event, location, association)
            self._shared_delta_ms = 0
            self._update_reference_states(event_id)
            self._update_shared_time_label()
            if self.playback_coordinator is not None:
                self.playback_coordinator.request_filmstrip_update(deferred=True)
            else:
                self._update_filmstrip()
            return True
        self._activate_pane(pane, align=False)
        self._shared_delta_ms = (
            int(association.position_ms) - int(pane._target_position_ms)
        )
        if pane.available_delta_bounds() is not None:
            pane.seek_passage_delta(self._shared_delta_ms)
        self._update_shared_time_label()
        return True

    def _focused_evidence_pane(self) -> Optional[PassageEvidencePane]:
        widget = self.focusWidget()
        while widget is not None:
            for pane in self.evidence_panes:
                if widget is pane:
                    return pane
            widget = widget.parentWidget()
        return None

    def _active_playback_pane(self) -> PassageEvidencePane:
        focused = self._focused_evidence_pane()
        if focused is not None and focused.available_delta_bounds() is not None:
            return focused
        if (
            self._active_pane is not None
            and self._active_pane.available_delta_bounds() is not None
        ):
            return self._active_pane
        for pane in self.evidence_panes:
            if pane.available_delta_bounds() is not None:
                return pane
        return self._active_pane or self.regular_pane

    def _pause_inactive_panes(self, active: PassageEvidencePane) -> None:
        self._set_active_idle_prefetch(active)
        for pane in self.evidence_panes:
            if pane is not active:
                pane.set_playing(False)
                pane.park_playback_cache()

    def _set_active_idle_prefetch(
        self,
        active: Optional[PassageEvidencePane],
    ) -> None:
        for pane in self.all_evidence_panes:
            pane.set_idle_prefetch_enabled(pane is active)
            pane.set_active_judging(pane is active)

    def _update_shared_from_pane(self, pane: PassageEvidencePane) -> None:
        delta_ms = pane.current_delta_ms()
        if delta_ms is None:
            return
        self._shared_delta_ms = delta_ms
        self._update_shared_time_label()

    def _activate_pane(
        self,
        pane: PassageEvidencePane,
        *,
        align: bool,
    ) -> bool:
        if pane not in self.evidence_panes:
            return False
        if self._sync_playing:
            self._set_sync_playing(False, seek_final=False)
        previous = self._active_pane
        switched = previous is not pane
        if switched and previous is not None:
            self._update_shared_from_pane(previous)
            previous.set_playing(False)
        self._active_pane = pane
        # In low-power preview mode secondary cameras are prepared but not
        # decoded until the operator explicitly switches to them.
        pane.start_deferred_worker()
        self._pause_inactive_panes(pane)
        if switched and align:
            pane.seek_passage_delta(self._shared_delta_ms)
        if self._maximized_window is not None and pane in self._maximized_hosted_panes:
            self._maximized_pane = pane
            self._maximized_pane_index = self._maximized_original_indexes.get(pane, -1)
            self._update_maximized_mode_controls(
                side_by_side=len(self._maximized_hosted_panes) > 1
            )
        return switched

    def _toggle_active_pane(self) -> None:
        self._toggle_pane(self._active_playback_pane())

    def _on_pane_detail_requested(self, pane: PassageEvidencePane) -> None:
        self._mark_filmstrip_operator_busy()
        self._activate_pane(pane, align=False)

    def _toggle_pane(self, pane: PassageEvidencePane) -> None:
        self._mark_filmstrip_operator_busy()
        was_sync_playing = self._sync_playing
        if was_sync_playing:
            self._set_sync_playing(False, seek_final=False)
        switched = self._activate_pane(pane, align=False)
        if pane.is_playing and not was_sync_playing:
            pane.set_playing(False)
            self._update_shared_from_pane(pane)
            return
        if not switched:
            self._update_shared_from_pane(pane)
        pane.seek_passage_delta(self._shared_delta_ms, linked_playing=True)

    def _continue_playback(self, pane: PassageEvidencePane) -> None:
        """▶ that ran to the last frame of a file plays on in the file that continues it.

        Rows open 2.5 s review pieces, so ▶ used to stop every 2.5 s (现代五项
        10-29, “2 秒割裂感”).  Only forward play of one pane at its file's last
        frame carries on, through the same neighbour lookup as →; a gap in the
        recording, synchronised play or reverse play just stops as before.
        """

        if self._sync_playing or pane not in self.evidence_panes:
            return
        if self._step_across_recording_boundary(pane, 1):
            pane.set_playing(True)

    def _step_active_pane(self, frame_delta: int) -> None:
        self._step_pane(self._active_playback_pane(), frame_delta)

    def _step_pane(
        self,
        pane: PassageEvidencePane,
        frame_delta: int,
    ) -> None:
        self._mark_filmstrip_operator_busy()
        if abs(int(frame_delta)) == CTRL_FRAME_STEP:
            direction = 1 if int(frame_delta) > 0 else -1
            if self._navigate_video_candidate(direction, pane):
                return
        switched = self._activate_pane(pane, align=False)
        if not switched:
            if self._step_across_recording_boundary(pane, frame_delta):
                return
            current_delta_ms = pane.current_delta_ms()
            pane.step(frame_delta)
            bounds = pane.available_delta_bounds()
            if bounds is not None:
                lower, upper = bounds
                base_delta_ms = (
                    self._shared_delta_ms
                    if current_delta_ms is None
                    else current_delta_ms
                )
                self._shared_delta_ms = max(
                    lower,
                    min(
                        base_delta_ms
                        + int(frame_delta) * pane.frame_duration_ms(),
                        upper,
                    ),
                )
                self._update_shared_time_label()
            return
        bounds = pane.available_delta_bounds()
        if bounds is None:
            return
        lower, upper = bounds
        self._shared_delta_ms = max(
            lower,
            min(
                self._shared_delta_ms
                + int(frame_delta) * pane.frame_duration_ms(),
                upper,
            ),
        )
        pane.seek_passage_delta(self._shared_delta_ms)
        self._update_shared_time_label()

    def _step_across_recording_boundary(
        self,
        pane: PassageEvidencePane,
        frame_delta: int,
        at_edge: bool = False,
    ) -> bool:
        """Continue single-frame navigation into an adjacent recording file.

        AT_EDGE: the decoder itself found no further frame in this file.
        """

        location = pane.location
        worker = getattr(pane, "_worker", None)
        if location is None or worker is None or abs(int(frame_delta)) != 1:
            return False
        frame_ms = max(1, pane.frame_duration_ms())
        position_ms = int(getattr(pane, "_current_position_ms", 0))
        duration_ms = int(getattr(pane, "_duration_ms", 0))
        direction = 1 if int(frame_delta) > 0 else -1
        frame_index = int(getattr(pane, "_current_frame_index", -1))
        frame_count = int(getattr(pane, "_frame_count", 0))
        at_first_frame = frame_index == 0 if frame_index >= 0 else position_ms == 0
        at_last_frame = (
            frame_index >= frame_count - 1
            if frame_count > 0 and frame_index >= 0
            else duration_ms > 0 and position_ms >= duration_ms - frame_ms
        )
        if not at_edge and direction < 0 and not at_first_frame:
            return False
        if not at_edge and direction > 0 and not at_last_frame:
            return False

        current = location.segment
        event = self.passage_store.get(self._selected_event_id)
        if pane is self._camera_one_pane() or event is None:
            crossed = self._step_into_neighbour_recording(pane, direction, frame_ms, duration_ms)
            if not crossed and at_edge:
                pane.show_navigation_notice("相邻录像暂不可用或存在缺口，已保留当前画面。")
            return crossed
        current_session_key = self._recording_session_key(location)
        segments = sorted(
            (
                segment
                for segment in self.timeline_store.segments()
                if segment.segment_id != current.segment_id
                and int(segment.camera_index) == int(current.camera_index)
                and segment.source_id == current.source_id
                and self._recording_session_key_from_path(
                    segment.source_id,
                    self.timeline_store.resolve_video_path(segment),
                )
                == current_session_key
                and (not event.race_id or not segment.race_id or segment.race_id == event.race_id)
                and segment.ended_at_ms is not None
            ),
            key=lambda segment: (segment.started_at_ms, segment.segment_id),
        )
        current_origin_ms = int(
            current.media_started_at_ms
            if current.media_started_at_ms is not None
            else current.started_at_ms
        )
        current_end_ms = current_origin_ms + int(
            current.media_duration_ms
            if current.media_duration_ms is not None
            else max(0, int(current.ended_at_ms or current_origin_ms) - current_origin_ms)
        )
        if direction < 0:
            adjacent = next(
                (
                    segment
                    for segment in reversed(segments)
                    if int(segment.ended_at_ms or 0) <= current_origin_ms + frame_ms
                ),
                None,
            )
        else:
            adjacent = next(
                (
                    segment
                    for segment in segments
                    if int(
                        segment.media_started_at_ms
                        if segment.media_started_at_ms is not None
                        else segment.started_at_ms
                    ) >= current_end_ms - frame_ms
                ),
                None,
            )
        if adjacent is None:
            return False

        adjacent_origin_ms = int(
            adjacent.media_started_at_ms
            if adjacent.media_started_at_ms is not None
            else adjacent.started_at_ms
        )
        adjacent_duration_ms = int(
            adjacent.media_duration_ms
            if adjacent.media_duration_ms is not None
            else max(0, int(adjacent.ended_at_ms or adjacent_origin_ms) - adjacent_origin_ms)
        )
        target_position_ms = (
            max(0, adjacent_duration_ms - frame_ms) if direction < 0 else 0
        )
        target_timestamp_ms = adjacent_origin_ms + target_position_ms
        lookup = self.timeline_store.locate_passage(
            target_timestamp_ms,
            race_id=event.race_id,
        )
        target_location = next(
            (
                item
                for item in lookup.locations
                if item.segment.segment_id == adjacent.segment_id
                and item.status in _OPENABLE_STATUSES
            ),
            None,
        )
        if target_location is None:
            return False
        initial_delta_ms = target_position_ms - int(target_location.playback_position_ms)
        pane.set_passage(event, target_location, initial_delta_ms=initial_delta_ms)
        self._shared_delta_ms = target_position_ms - int(
            target_location.passage_position_ms
        )
        self._update_shared_time_label()
        return True

    def _step_into_neighbour_recording(
        self, pane: PassageEvidencePane, direction: int, frame_ms: int, duration_ms: int,
    ) -> bool:
        """Open the frame right after (or before) the one shown, in the file that continues it.

        The neighbour is looked up in the recording index itself, not in the
        whole-race strip's copy of it (rebuilt in the background, so it can be
        empty while a recorder reconnects) and not through the selected chip
        record (the arrival workbench has none): stepping must not stop at the
        end of a two-second file (古浪 548 → 261).  The same recording session
        is preferred, then the long archive over its live copies.  A gap in
        the recording, however short, is never bridged.
        """

        location = pane.location
        current = location.segment
        origin = int(current.media_started_at_ms if current.media_started_at_ms is not None
                     else current.started_at_ms)
        edge = origin + int(current.media_duration_ms or duration_ms)
        boundary = edge if direction > 0 else origin - 1
        shown = origin + int(getattr(pane, "_current_position_ms", 0))
        if (direction > 0 and edge - shown - frame_ms
                > max(frame_ms, PassageReviewSurface.FILMSTRIP_COVERAGE_TOLERANCE_MS)):
            pane.show_navigation_notice("当前片段提前读取失败，未跳过剩余录像。")
            return True
        session = self._recording_session_key(location)
        neighbours = sorted(
            (source for source in self._recording_sources_for_step(pane)
             if source.start_ms <= boundary < source.end_ms
             and source.location.segment.segment_id != current.segment_id
             and Path(source.location.video_path) != Path(location.video_path)),
            key=lambda source: (self._recording_session_key(source.location) != session,
                                source.priority, source.start_ms - source.end_ms),
        )
        for source in neighbours:
            if not Path(source.location.video_path).is_file():
                continue
            store = getattr(self, "timeline_store", None)
            if store is not None and not store.video_path_is_playable(
                source.location.video_path, refresh=True,
            ):
                continue
            target = self._aligned_archive(
                source.location, shown, None if self._is_archive(location) else (location, shown))
            start = int(target.segment.media_started_at_ms)
            if direction > 0:  # the first frame after the one shown
                frame = (shown + frame_ms // 2 - start) // frame_ms + 1
            else:  # the last frame before it
                frame = -((start - shown + frame_ms // 2) // frame_ms) - 1
            last = max(0, -(-(source.end_ms - source.start_ms) // frame_ms) - 1)
            frame = max(0, min(int(frame), last))
            return self._seek_retained_camera_frame(
                target, frame * frame_ms, frame, int(getattr(pane, "camera_index", 1) or 1),
                adjacent_boundary_ms=shown - start, adjacent_direction=direction,
            )
        return False

    @staticmethod
    def _is_archive(location) -> bool:
        return "archive" in Path(location.video_path).stem.lower()

    def _archive_on_piece_clock(self) -> bool:
        """Whether archives are shown on their buffer pieces' clock (the arrival workbench, 10-29)."""

        return False

    def _aligned_archive(self, location, moment_ms, piece=None):
        """An archive's LOCATION with its clock moved onto its buffer pieces' (see archive_alignment).

        PIECE: (location, recorder ms) of a buffer piece showing a picture of
        this archive; else one at MOMENT_MS is looked up.  The shift found is
        kept per archive file.  Anything else, or no picture matched, is
        returned as it is.
        """

        segment = location.segment
        if (not self._archive_on_piece_clock() or not self._is_archive(location)
                or segment.media_started_at_ms is None):
            return location
        start = int(segment.media_started_at_ms)
        shifts = self.__dict__.setdefault("_archive_shifts", {})  # segment id -> (start, shift)
        shift = shifts.get(segment.segment_id, (start, None))[1]
        if shift is None:
            import cv2

            from .archive_alignment import archive_shift_ms

            piece = piece or self._piece_at(location, int(moment_ms))
            if piece is None:
                return location
            piece_location, piece_ms = piece
            if piece_location.segment.media_started_at_ms is None:
                return location  # a piece not timed yet has no picture to match
            # A picture tried once is not decoded again: unsure (a still scene) or not found.
            tried = self.__dict__.setdefault("_archive_tries", {})
            attempt = (segment.segment_id, str(piece_location.video_path), int(piece_ms))
            if attempt not in tried:
                # The drift grows from file to file: the nearest archive's shift is the first guess.
                guess = min(shifts.values(), key=lambda known: abs(known[0] - start), default=(start, 0))[1]
                try:
                    tried[attempt] = archive_shift_ms(
                        piece_location.video_path, piece_location.segment.media_started_at_ms, piece_ms,
                        location.video_path, start, guess_ms=guess)
                except (cv2.error, OSError, ValueError):
                    tried[attempt] = (None, False)
            shift, sure = tried[attempt]
            if shift is None:
                return location
            if sure:
                shifts[segment.segment_id] = (start, shift)
        return replace(location, segment=replace(segment, media_started_at_ms=start - int(shift)))

    def _piece_at(self, archive, moment_ms):
        """(location, recorder ms) of a buffer piece of ARCHIVE's recording showing MOMENT_MS, or None."""

        camera = int(archive.segment.camera_index)
        session = self._recording_session_key(archive)
        sources, _pending = recording_sources(
            self.timeline_store, camera, self._filmstrip_race_id(),
            path_available=lambda _path, _live: True)  # only the file used is checked
        piece = next((source for source in sources
                      if source.priority == 1 and not self._is_archive(source.location)
                      and source.start_ms <= moment_ms < source.end_ms
                      and self._recording_session_key(source.location) == session
                      and Path(source.location.video_path).is_file()), None)
        return (piece.location, int(moment_ms)) if piece is not None else None

    def _recording_sources_for_step(self, pane: PassageEvidencePane):
        """This camera's recordings, closed files and the live tail, read afresh."""

        sources, _pending = recording_sources(
            self.timeline_store, int(getattr(pane, "camera_index", 1) or 1), self._filmstrip_race_id(),
            live_locations=self._live_locations_for_filmstrip(pane),
            # Only the file actually opened is checked, not every file of the race.
            path_available=lambda _path, _live: True,
        )
        return sources

    def _seek_pane_delta(
        self,
        pane: PassageEvidencePane,
        delta_ms: int,
        *,
        preview: bool,
    ) -> None:
        self._mark_filmstrip_operator_busy()
        self._activate_pane(pane, align=False)
        # Preview clips from camera 1/2 are one temporary evidence window. A
        # scrub or seek on either preview pane must move both cameras together;
        # formal clips keep the existing single-camera precision workflow.
        if preview or (
            pane.location is not None and pane.location.status == "preview"
        ):
            self._apply_both_delta(delta_ms, preview=preview)
            return
        bounds = pane.available_delta_bounds()
        if bounds is None:
            return
        lower, upper = bounds
        self._shared_delta_ms = max(lower, min(int(delta_ms), upper))
        pane.seek_passage_delta(self._shared_delta_ms, preview=preview)
        self._update_shared_time_label()

    def _on_pane_scrub_started(self, pane: PassageEvidencePane) -> None:
        self._mark_filmstrip_operator_busy()
        if self._sync_playing:
            self._set_sync_playing(False, seek_final=False)
        self._active_pane = pane
        self._pause_inactive_panes(pane)
        self._update_shared_from_pane(pane)

    def _on_pane_position_changed(
        self,
        pane: PassageEvidencePane,
        delta_ms: int,
    ) -> None:
        self._mark_filmstrip_operator_busy()
        if self._sync_playing or pane is not self._active_pane:
            return
        self._shared_delta_ms = int(delta_ms)
        self._update_shared_time_label()
        self._sync_filmstrip_from_pane(pane)

    def _sync_filmstrip_from_pane(self, pane: PassageEvidencePane) -> None:
        if not hasattr(self, "video_filmstrip") or pane is not self._camera_one_pane():
            return
        self.video_filmstrip.full_race.set_current_frame(pane.location, pane._current_position_ms)

    def _flush_filmstrip_anchor(self) -> None:
        pending = self._pending_filmstrip_anchor
        self._pending_filmstrip_anchor = None
        if pending is None or not hasattr(self, "video_filmstrip"):
            return
        video_path, position_ms = pending
        if Path(getattr(self.video_filmstrip, "_video_path", "")) != video_path:
            return
        self.video_filmstrip.append_positions(video_path, (position_ms,))

    def _step_both(self, frame_delta: int) -> None:
        self._set_sync_playing(False)
        reference = next(
            (
                pane
                for pane in reversed(self.evidence_panes)
                if pane.available_delta_bounds() is not None
            ),
            self.regular_pane,
        )
        step_ms = reference.frame_duration_ms()
        self._seek_both_delta(
            self._shared_delta_ms + int(frame_delta) * step_ms
        )

    def _toggle_both(self) -> None:
        self._set_sync_playing(not self._sync_playing)

    def _sync_delta_bounds(self) -> Optional[tuple[int, int]]:
        bounds = [
            pane_bounds
            for pane in self.evidence_panes
            if (pane_bounds := pane.available_delta_bounds()) is not None
        ]
        if not bounds:
            return None
        lower = min(value[0] for value in bounds)
        upper = max(value[1] for value in bounds)
        return lower, upper

    def _seek_both_delta(self, delta_ms: int) -> None:
        self._apply_both_delta(delta_ms, preview=False)

    def _seek_to_target_position(self) -> None:
        """Jump every visible camera to its calculated passage timestamp."""

        if self.passage_store.get(self._selected_event_id) is None:
            return
        self._set_sync_playing(False)
        self._seek_both_delta(0)

    def _preview_both_delta(self, delta_ms: int) -> None:
        self._apply_both_delta(delta_ms, preview=True)

    def _apply_both_delta(self, delta_ms: int, *, preview: bool) -> None:
        bounds = self._sync_delta_bounds()
        if bounds is None:
            return
        lower, upper = bounds
        self._shared_delta_ms = max(lower, min(int(delta_ms), upper))
        for pane in self.evidence_panes:
            pane.seek_passage_delta(
                self._shared_delta_ms,
                linked_playing=self._sync_playing,
                preview=preview,
            )
        self._update_shared_time_label()

    def _set_sync_playing(
        self,
        playing: bool,
        *,
        seek_final: bool = True,
    ) -> None:
        self._mark_filmstrip_operator_busy()
        playing = bool(playing) and self._sync_delta_bounds() is not None
        if playing == self._sync_playing:
            return
        if playing:
            self._pause_inactive_panes(self.regular_pane)
            self._sync_origin_delta_ms = self._shared_delta_ms
            self._sync_playing = True
            self._seek_both_delta(self._shared_delta_ms)
            self._sync_started_at = time.monotonic()
            self._last_sync_correction_at = (
                self._sync_started_at - self.SYNC_CORRECTION_COOLDOWN_SECONDS
            )
            self._sync_timer.start()
        else:
            if self._sync_playing:
                elapsed_ms = int((time.monotonic() - self._sync_started_at) * 1000.0)
                self._shared_delta_ms = self._sync_origin_delta_ms + elapsed_ms
            self._sync_playing = False
            self._sync_timer.stop()
            for pane in self.evidence_panes:
                pane.set_linked_playing(False)
        self.play_both_btn.setText(
            "联动 Ⅱ" if self._sync_playing else "联动 ▶"
        )
        if not self._sync_playing and seek_final:
            self._seek_both_delta(self._shared_delta_ms)

    def _on_sync_tick(self) -> None:
        if not self._sync_playing:
            return
        elapsed_ms = int((time.monotonic() - self._sync_started_at) * 1000.0)
        target_delta_ms = self._sync_origin_delta_ms + elapsed_ms
        bounds = self._sync_delta_bounds()
        if bounds is None or target_delta_ms >= bounds[1]:
            if bounds is not None:
                self._shared_delta_ms = bounds[1]
            self._set_sync_playing(False)
            return
        self._shared_delta_ms = target_delta_ms
        self._update_shared_time_label()
        self._correct_sync_drift(target_delta_ms)

    def _correct_sync_drift(self, target_delta_ms: int) -> None:
        now = time.monotonic()
        elapsed_ms = int((now - self._sync_started_at) * 1000.0)
        if (
            elapsed_ms < self.SYNC_STARTUP_GRACE_MS
            or (now - self._last_sync_correction_at)
            < self.SYNC_CORRECTION_COOLDOWN_SECONDS
        ):
            return

        corrected = False
        for pane in self.evidence_panes:
            drift_ms = pane.linked_drift_ms(target_delta_ms)
            if drift_ms is None:
                continue
            if abs(drift_ms) <= pane.linked_drift_tolerance_ms():
                continue
            pane.seek_passage_delta(target_delta_ms, linked_playing=True)
            corrected = True
        if corrected:
            self._last_sync_correction_at = now

    def _update_shared_time_label(self) -> None:
        event = self.passage_store.get(self._selected_event_id)
        if event is None:
            return
        self.current_time_label.setText(
            format_passage_time(event.timeline_timestamp_ms + self._shared_delta_ms)
        )
        self.current_time_label.setToolTip(
            f"预计过线 {format_passage_time(event.timeline_timestamp_ms)}；"
            f"距预计过线 {self._shared_delta_ms:+d} ms"
        )

    def _fit_visible_evidence_views(self) -> None:
        for pane in self.evidence_panes:
            if pane.isVisible() and pane.video_view.has_frame:
                pane.video_view.fit_to_window()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        QTimer.singleShot(0, self._initialize_review_splitters)

    def _initialize_review_splitters(self) -> None:
        """Give the queue and evidence panes usable initial dimensions."""

        self._apply_review_content_split_sizes()
        if self._uses_popup_judging():
            self.evidence_splitter.hide()
            return
        total_width = self.workspace_splitter.width()
        if total_width <= 0:
            return
        queue_width = max(320, int(total_width * 0.42))
        evidence_width = max(1, total_width - queue_width)
        self.workspace_splitter.setSizes([queue_width, evidence_width])
        self._queue_default_sizes = [queue_width, evidence_width]
        self._distribute_evidence_panes()

    def _distribute_evidence_panes(self) -> None:
        """Distribute the visible camera panes across the evidence splitter."""

        count = self.evidence_splitter.count()
        total_width = self.evidence_splitter.width()
        if count <= 0 or total_width <= 0:
            return
        active_panes = set(self.evidence_panes)
        active_count = sum(
            1
            for index in range(count)
            if self.evidence_splitter.widget(index) in active_panes
        )
        if active_count <= 0:
            return
        share = max(1, total_width // active_count)
        sizes = [
            share if self.evidence_splitter.widget(index) in active_panes else 0
            for index in range(count)
        ]
        self.evidence_splitter.setSizes(sizes)

    def _toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showMaximized()
            self.fullscreen_btn.setText("全屏")
        else:
            self.showFullScreen()
            self.fullscreen_btn.setText("退出全屏")
        QTimer.singleShot(0, self._fit_visible_evidence_views)

    def _toggle_maximized_pane(self, pane: PassageEvidencePane) -> None:
        if getattr(self, "_problem_workbench_active", False) and not self._uses_popup_judging():
            # In the problem workbench the picture grows in place: the queue and the
            # decisions come along with it, so a small screen never gets a second
            # window to manage while looking at one crossing.
            panel = getattr(self, "problem_panel", None)
            if panel is not None:
                panel.toggle_big_view()
                return
        if self._maximized_pane is pane and len(self._maximized_hosted_panes) == 1:
            self._restore_maximized_pane()
            return
        if self._maximized_pane is not None:
            if (
                pane.source_kind == REGULAR_SOURCE
                and self._maximized_pane.source_kind == REGULAR_SOURCE
            ):
                self._set_maximized_regular_mode(pane.camera_index)
                return
            self._restore_maximized_pane()

        pane_index = self.evidence_splitter.indexOf(pane)
        if pane_index < 0:
            return
        if self._uses_popup_judging():
            pane.set_playing(False)
            pane.set_full_resolution_enabled(True)
        window = QDialog(
            self,
            Qt.Window
            | Qt.WindowTitleHint
            | Qt.WindowSystemMenuHint
            | Qt.WindowMinMaxButtonsHint
            | Qt.WindowCloseButtonHint,
        )
        title = pane.title_label.text().strip() or "机位画面"
        if pane._identity:
            title = f"{title} - {pane._identity}"
        window.setWindowTitle(title)
        window.setModal(False)
        window_layout = QVBoxLayout(window)
        window_layout.setContentsMargins(8, 8, 8, 8)
        window_layout.setSpacing(6)
        mode_bar = QHBoxLayout()
        mode_bar.setContentsMargins(4, 0, 4, 0)
        mode_bar.setSpacing(6)
        mode_label = QLabel(window)
        mode_label.setStyleSheet(
            "color: #0f4f86; font-size: 14pt; font-weight: 700; padding: 4px 2px;"
        )
        mode_bar.addWidget(mode_label)
        mode_bar.addStretch(1)
        mode_buttons: dict[object, QPushButton] = {}
        if pane.source_kind == REGULAR_SOURCE and len(self.regular_panes) > 1:
            side_by_side_btn = QPushButton("并排", window)
            side_by_side_btn.setCheckable(True)
            side_by_side_btn.setToolTip("同时显示全部普通机位（B）")
            side_by_side_btn.clicked.connect(
                lambda _checked=False: self._set_maximized_regular_mode(None)
            )
            mode_buttons["side_by_side"] = side_by_side_btn
            mode_bar.addWidget(side_by_side_btn)
            for regular_pane in self.regular_panes:
                camera_index = regular_pane.camera_index
                camera_btn = QPushButton(f"机位 {camera_index}", window)
                camera_btn.setCheckable(True)
                camera_btn.setToolTip(f"切换到机位 {camera_index}（{camera_index}）")
                camera_btn.clicked.connect(
                    lambda _checked=False, index=camera_index: (
                        self._set_maximized_regular_mode(index)
                    )
                )
                mode_buttons[camera_index] = camera_btn
                mode_bar.addWidget(camera_btn)
        window_layout.addLayout(mode_bar)
        content_splitter = QSplitter(Qt.Horizontal, window)
        content_splitter.setChildrenCollapsible(False)
        content_splitter.setHandleWidth(5)
        window_layout.addWidget(content_splitter, 1)

        self._maximized_pane = pane
        self._maximized_window = window
        self._maximized_pane_index = pane_index
        self._maximized_content_splitter = content_splitter
        self._maximized_original_indexes = {
            candidate: self.evidence_splitter.indexOf(candidate)
            for candidate in self.evidence_panes
        }
        self._maximized_mode_label = mode_label
        self._maximized_mode_buttons = mode_buttons
        self._maximized_camera_shortcuts = []
        if self._uses_popup_judging():
            panel = self.video_filmstrip.full_race
            bar = panel.canvas.horizontalScrollBar()
            self._popup_return_state = (
                self.review_content_splitter.sizes(),
                panel.index.start_ms + bar.value() / panel.tile_pitch * panel.interval_ms,
            )
            roster_splitter = QSplitter(Qt.Horizontal, window)
            roster_splitter.setChildrenCollapsible(False)
            roster_splitter.setHandleWidth(6)
            window_layout.removeWidget(content_splitter)
            roster_splitter.addWidget(self.results_panel)
            roster_splitter.addWidget(content_splitter)
            self.results_panel.show()
            roster_splitter.setStretchFactor(0, 0)
            roster_splitter.setStretchFactor(1, 1)
            window_layout.addWidget(roster_splitter, 1)
            self._maximized_roster_splitter = roster_splitter
            instruction = QLabel("核对名单号码后标线确认 · 单击名单换号码，双击定位计时时刻 · Esc 返回胶卷", window)
            window_layout.addWidget(instruction)
            back_button = QPushButton("返回胶卷（Esc）", window)
            back_button.clicked.connect(self._restore_maximized_pane)
            mode_bar.addWidget(back_button)
        escape_shortcut = QShortcut(QKeySequence(Qt.Key_Escape), window)
        escape_shortcut.setContext(Qt.WindowShortcut)
        escape_shortcut.setAutoRepeat(False)
        escape_shortcut.activated.connect(self._restore_maximized_pane)
        self._maximized_escape_shortcut = escape_shortcut
        if pane.source_kind == REGULAR_SOURCE and len(self.regular_panes) > 1:
            side_by_side_shortcut = QShortcut(QKeySequence("B"), window)
            side_by_side_shortcut.setContext(Qt.WindowShortcut)
            side_by_side_shortcut.setAutoRepeat(False)
            side_by_side_shortcut.activated.connect(
                lambda: self._set_maximized_regular_mode(None)
            )
            self._maximized_camera_shortcuts.append(side_by_side_shortcut)
            cycle_shortcut = QShortcut(QKeySequence(Qt.Key_Tab), window)
            cycle_shortcut.setContext(Qt.WindowShortcut)
            cycle_shortcut.setAutoRepeat(False)
            cycle_shortcut.activated.connect(self._cycle_maximized_regular_pane)
            self._maximized_camera_shortcuts.append(cycle_shortcut)
            for regular_pane in self.regular_panes:
                camera_index = regular_pane.camera_index
                shortcut = QShortcut(QKeySequence(str(camera_index)), window)
                shortcut.setContext(Qt.WindowShortcut)
                shortcut.setAutoRepeat(False)
                shortcut.activated.connect(
                    lambda index=camera_index: self._set_maximized_regular_mode(index)
                )
                self._maximized_camera_shortcuts.append(shortcut)
        window.finished.connect(
            lambda _result: self._restore_maximized_pane(close_window=False)
        )
        self._host_maximized_panes((pane,))
        self._activate_pane(pane, align=True)
        self._update_maximized_mode_controls(side_by_side=False)

        screen = self.screen() or QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            window.resize(
                max(1, int(available.width() * 0.9)),
                max(1, int(available.height() * 0.88)),
            )
        window.show()
        if screen is not None:
            frame = window.frameGeometry()
            frame.moveCenter(available.center())
            window.move(frame.topLeft())
        window.raise_()
        window.activateWindow()
        pane.video_view.setFocus(Qt.ShortcutFocusReason)
        if self._maximized_roster_splitter is not None:
            self._maximized_roster_splitter.setSizes([max(340, window.width() // 3), max(500, window.width() * 2 // 3)])
        QTimer.singleShot(0, pane.video_view.fit_to_window)

    def _restore_hosted_panes_to_splitter(self) -> None:
        if not self._maximized_hosted_panes:
            return
        for pane in sorted(
            self._maximized_hosted_panes,
            key=lambda candidate: self._maximized_original_indexes.get(candidate, 0),
        ):
            pane_index = self._maximized_original_indexes.get(pane, 0)
            pane.setParent(self.evidence_splitter)
            self.evidence_splitter.insertWidget(max(0, pane_index), pane)
        self._maximized_hosted_panes = ()

    def _host_maximized_panes(
        self,
        panes: tuple[PassageEvidencePane, ...],
    ) -> None:
        content_splitter = self._maximized_content_splitter
        if content_splitter is None:
            return
        self._restore_hosted_panes_to_splitter()
        active_panes = self.evidence_panes
        for candidate in self.all_evidence_panes:
            if candidate not in panes:
                candidate.setVisible(candidate in active_panes)
                candidate.maximize_btn.setText("放大")
                candidate.maximize_btn.setToolTip("放大该机位（双击画面或按 F）")
        for pane in panes:
            content_splitter.addWidget(pane)
            pane.show()
            pane.maximize_btn.setText("缩小")
            pane.maximize_btn.setToolTip("恢复主界面（Esc 或 F）")
            content_splitter.setStretchFactor(content_splitter.indexOf(pane), 1)
        content_splitter.setSizes([1] * len(panes))
        self._maximized_hosted_panes = panes

    def _set_maximized_regular_mode(self, camera_index: Optional[int]) -> None:
        if self._maximized_window is None or not self.regular_panes:
            return
        if camera_index is None:
            panes = self.regular_panes
            active = (
                self._active_pane
                if self._active_pane in panes
                else panes[0]
            )
            side_by_side = True
        else:
            active = self._regular_panes_by_camera.get(int(camera_index))
            if active is None or active not in self.regular_panes:
                return
            panes = (active,)
            side_by_side = False
        self._host_maximized_panes(panes)
        self._maximized_pane = active
        self._maximized_pane_index = self._maximized_original_indexes.get(active, -1)
        self._activate_pane(active, align=True)
        active.video_view.setFocus()
        self._update_maximized_mode_controls(side_by_side=side_by_side)
        for pane in panes:
            QTimer.singleShot(0, pane.video_view.fit_to_window)

    def _cycle_maximized_regular_pane(self) -> None:
        panes = self.regular_panes
        if not panes:
            return
        active = self._active_pane if self._active_pane in panes else panes[0]
        next_index = (panes.index(active) + 1) % len(panes)
        self._set_maximized_regular_mode(panes[next_index].camera_index)

    def _update_maximized_mode_controls(self, *, side_by_side: bool) -> None:
        active = self._active_pane or self._maximized_pane
        if active is None:
            return
        if active.source_kind == REGULAR_SOURCE:
            current_text = f"当前判读：机位 {active.camera_index}"
        else:
            current_text = "当前判读：高速摄像"
        if self._maximized_mode_label is not None:
            self._maximized_mode_label.setText(current_text)
        if self._maximized_window is not None:
            identity = f" - {active._identity}" if active._identity else ""
            self._maximized_window.setWindowTitle(
                f"{APP_DISPLAY_NAME} · {current_text}{identity}"
            )
        for key, button in self._maximized_mode_buttons.items():
            checked = (
                side_by_side
                if key == "side_by_side"
                else not side_by_side
                and active.source_kind == REGULAR_SOURCE
                and key == active.camera_index
            )
            button.setChecked(bool(checked))
            button.setStyleSheet(
                "QPushButton { padding: 6px 12px; font-weight: 700; }"
                "QPushButton:checked { background: #1976c9; color: #ffffff; "
                "border: 2px solid #0f5f9f; }"
            )

    def _restore_maximized_pane(self, *, close_window: bool = True) -> None:
        pane = self._maximized_pane
        window = self._maximized_window
        if pane is None or window is None:
            return

        return_state = self._popup_return_state
        if self._maximized_roster_splitter is not None:
            self.workspace_splitter.insertWidget(0, self.results_panel)
            self.results_panel.show()
            self._maximized_roster_splitter = None
        self._popup_return_state = None
        self._restore_hosted_panes_to_splitter()
        self._maximized_pane = None
        self._maximized_window = None
        self._maximized_escape_shortcut = None
        self._maximized_pane_index = -1
        self._maximized_content_splitter = None
        self._maximized_original_indexes = {}
        self._maximized_mode_label = None
        self._maximized_mode_buttons = {}
        self._maximized_camera_shortcuts = []

        active_panes = self.evidence_panes
        for candidate in self.all_evidence_panes:
            candidate.setVisible(candidate in active_panes)
            candidate.maximize_btn.setText("放大")
            candidate.maximize_btn.setToolTip("放大该机位（双击画面或按 F）")
        for index in range(self.evidence_splitter.count()):
            self.evidence_splitter.setStretchFactor(index, 1)
        self._distribute_evidence_panes()
        QTimer.singleShot(0, self._distribute_evidence_panes)
        if self._uses_popup_judging():
            self.evidence_splitter.hide()
            pane.set_full_resolution_enabled(False)
            pane.set_playing(False)
            if return_state is not None:
                self.review_content_splitter.setSizes(return_state[0])
                self.video_filmstrip.full_race._position_scroll(return_state[1], center=False)
            self.video_filmstrip.full_race.canvas.setFocus(Qt.ShortcutFocusReason)
        if close_window:
            window.close()
        window.deleteLater()
        QTimer.singleShot(0, self._fit_visible_evidence_views)

    def _open_preferred_source(self, row: int, _column: int) -> None:
        if not (0 <= row < len(self._visible_events)):
            return
        event = self._visible_events[row]
        # A single row selection deliberately keeps the current frame so an
        # operator can scan continuously without skipping an unchipped rider.
        # A double-click is an explicit request to locate this passage on the
        # continuous camera recording.  It must not open the old short
        # evidence clip; that remains an explicit pane-button action.
        self._select_event(event.event_id, locate_target=True)
        # A race configured with no ordinary camera has no continuous target
        # to locate.  Preserve the established high-speed-only affordance by
        # maximising that pane, without invoking the generic short-clip
        # callback used by the ordinary-camera workflow.
        regular_available = any(
            pane.location is not None
            and pane.location.status in _OPENABLE_STATUSES
            for pane in self.regular_panes
        )
        if (
            regular_available
            and self._uses_popup_judging()
            and self._maximized_window is None
        ):
            self._toggle_maximized_pane(self._camera_one_pane())
        if not regular_available:
            lookup = self._lookups.get(event.event_id)
            high_speed = (
                source_location(lookup, high_speed=True)
                if lookup is not None
                else None
            )
            if high_speed is not None and high_speed.status in _OPENABLE_STATUSES:
                self._toggle_maximized_pane(self.high_speed_pane)
            elif self._uses_popup_judging():
                self.video_filmstrip.full_race.status_label.setText(
                    "该通过时刻暂无录像，请先检查摄像头连接；芯片记录已保留。"
                )

    def _open_location_if_available(
        self,
        event: PassageEvent,
        location: PassageVideoLocation,
    ) -> None:
        if is_high_speed(location):
            if self._maximized_pane is not self.high_speed_pane:
                self._toggle_maximized_pane(self.high_speed_pane)
            return
        if self._open_location is not None:
            self._open_location(event, location)

    def _open_row(self, row: int, column: int) -> None:
        self._open_preferred_source(row, column)

    def _open_event(self, event_id: str) -> None:
        self._open_event_location(event_id, "")

    def _open_event_location(self, event_id: str, segment_id: str) -> None:
        event = self.passage_store.get(event_id)
        if event is None:
            self.refresh()
            return
        lookup = self._lookups.get(event_id) or self._cached_lookup(event)
        available = [
            item for item in lookup.locations if item.status in _OPENABLE_STATUSES
        ]
        if not available:
            QMessageBox.information(self, "无法定位", lookup_status_text(lookup))
            return
        location = next(
            (
                item
                for item in available
                if not segment_id or item.segment.segment_id == segment_id
            ),
            None,
        )
        if location is None:
            QMessageBox.information(self, "无法定位", "该证据已变化，请刷新后重试。")
            self.refresh()
            return
        self._open_location_if_available(event, location)

    def reject(self) -> None:
        if self._maximized_window is not None:
            self._restore_maximized_pane()

    def closeEvent(self, event) -> None:
        self._restore_maximized_pane()
        self._sync_playing = False
        self._sync_timer.stop()
        if hasattr(self, "_filmstrip_prefetch_timer"):
            self._filmstrip_prefetch_timer.stop()
        if hasattr(self, "_filmstrip_operator_idle_timer"):
            self._filmstrip_operator_idle_timer.stop()
        if hasattr(self, "video_filmstrip"):
            self.video_filmstrip.full_race.stop()
            self.video_filmstrip.stop()
            self.video_filmstrip.stop_prefetch()
        if self.playback_coordinator is not None:
            self.playback_coordinator.shutdown()
        for pane in self.all_evidence_panes:
            pane.shutdown(wait=True)
        event.accept()


class PassageReviewDialog(PassageReviewSurface):
    """Standalone passage-review window."""


__all__ = [
    "PassageEvidencePane",
    "PassageReviewDialog",
    "PassageReviewSurface",
    "UI_BASE_FONT_POINT_SIZE",
    "UI_FONT_FAMILY",
    "compact_source_status",
    "combined_review_status",
    "format_passage_time",
    "lookup_status_text",
    "review_status_text",
    "source_location",
]
