"""FinishLynx-style review workspace for CycleRace passages and video evidence."""

from __future__ import annotations

import logging
from pathlib import Path
import time
from typing import Callable, Iterable, Mapping, Optional

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import (
    QColor,
    QKeySequence,
    QPalette,
)
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QShortcut,
    QTableWidget,
    QVBoxLayout,
)

from . import APP_WINDOW_TITLE
from .passage_evidence import (
    ContinuousMarkerStore,
    HIGH_SPEED_SOURCE,
    REGULAR_SOURCE,
    PassageEvidenceAssociationStore,
    VideoClockCalibrationStore,
)
from .passage_receiver import PassageEvent, PassageEventStore
from .person_follow import FollowTracks
from .passage_batch import (
    DEFAULT_REVIEW_GAP_MS,
    DEFAULT_SUBWAVE_GAP_MS,
    PassageReviewBatch,
)
from .video_arrival import (
    DEFAULT_ARRIVAL_BATCH_GAP_MS,
    DEFAULT_ARRIVAL_SUBWAVE_GAP_MS,
    VideoArrivalBatch,
    VideoArrivalCandidateStore,
    build_video_arrival_batches,
)
from .video_discovery import VideoDiscoveryStore
from .playback_coordinator import PlaybackCoordinator
from .race_metadata import (
    RaceAthleteMetadata,
    RaceMetadata,
    RaceMetadataStore,
)
from .review_selection import ReviewSelectionController
from .review_clip import PassageReviewBindingStore
from .runtime_metrics import RuntimeMetrics
from .video_playback import TargetTimelineSlider
from .video_filmstrip import (
    VideoFilmstripWidget,
)
from .video_timeline import (
    PassageVideoLocation,
    PassageVideoLookup,
    VideoTimelineStore,
)
from .passage_review_common import (
    UI_BASE_FONT_POINT_SIZE,
    UI_FONT_FAMILY,
    UI_INFO_PANEL_MAX_WIDTH,
    UI_INFO_PANEL_MIN_WIDTH,
    _AutoFitTableWidget,
    _OPENABLE_STATUSES,
    _ReviewFilterBar,
    _StatusColorDelegate,
    _resolve_evidence_layout,
    combined_review_status,
    compact_source_status,
    format_passage_time,
    lookup_status_text,
    review_status_text,
    source_location,
)
from .evidence_image_view import (
    EvidenceImageView,
)
from .passage_evidence_pane import PassageEvidencePane
# Not used here any more; re-exported because tests still reach them through this module.
from PyQt5.QtGui import QImage  # noqa: F401
from PyQt5.QtWidgets import QInputDialog, QMessageBox  # noqa: F401
from .evidence_image_view import PERSON_TAG_FONT_PX  # noqa: F401
from .passage_review_common import _TABLE_COLUMN_MIN_WIDTHS, _expanded_column_widths  # noqa: F401
from .passage_review_video_assist import PassageReviewVideoAssistMixin
from .passage_review_clock import PassageReviewClockMixin
from .passage_review_lookup import PassageReviewLookupMixin
from .passage_review_queue import PassageReviewQueueMixin
from .passage_review_filmstrip import PassageReviewFilmstripMixin
from .passage_review_selection import PassageReviewSelectionMixin
from .passage_review_playback import PassageReviewPlaybackMixin

logger = logging.getLogger("FinishReview.Review")


class PassageReviewSurface(
    PassageReviewVideoAssistMixin,
    PassageReviewClockMixin,
    PassageReviewLookupMixin,
    PassageReviewQueueMixin,
    PassageReviewFilmstripMixin,
    PassageReviewSelectionMixin,
    PassageReviewPlaybackMixin,
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

    SAME_FRAME_MARKER_DISTANCE = 0.03

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
