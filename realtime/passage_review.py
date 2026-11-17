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
from .passage_review_clock import PassageReviewClockMixin
from .passage_review_lookup import PassageReviewLookupMixin
from .passage_review_queue import PassageReviewQueueMixin
from .passage_review_filmstrip import PassageReviewFilmstripMixin

logger = logging.getLogger("FinishReview.Review")


class PassageReviewSurface(
    PassageReviewVideoAssistMixin,
    PassageReviewClockMixin,
    PassageReviewLookupMixin,
    PassageReviewQueueMixin,
    PassageReviewFilmstripMixin,
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
