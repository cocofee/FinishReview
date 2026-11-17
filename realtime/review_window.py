"""Production finish console without detection or OCR dependencies."""

from __future__ import annotations

import logging
import json
import importlib
import os
import re
import shutil
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType

from PyQt5.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QPainter, QPen
from PyQt5.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QInputDialog,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from . import APP_DISPLAY_NAME, APP_WINDOW_TITLE
from .ui_metrics import UiLatencyProbe
from .runtime_status import (
    RuntimeStatusSnapshot as RuntimeStatusSnapshot, RuntimeStatusPresenter, STATUS_WIDGET_NAMES,
)
from .decode_resources import DECODE_RESOURCES
from .background_wait import wait_for_background
from .recording_catalog import RecordingCatalog, RecordingCatalogJob
from .evidence_pipeline import EvidencePassage, EvidencePipeline, EvidenceRefreshJob
from .evidence_clock import LIVE_DATE_TOLERANCE_MS, align_evidence_timestamp
from .capture_refresh import (
    ArchiveRefreshJob,
    CaptureRefreshRequest,
    CaptureRefreshResult,
    CaptureRefreshWorker,
)
from .auyat_rgb import (
        AuyatRgbCatalog,
        AuyatRgbScanWorker,
        AuyatScanResult,
        is_network_share,
    )
from .external_clip_import import ExternalClipImportError, race_id_from_passage_store
from .finish_line import FinishLineStore
from .event_workspace import (
        EventWorkspaceDescriptor,
        EventWorkspaceError,
        EventWorkspaceSummary,
        discover_event_workspaces,
        summarize_event_workspace,
        validate_event_workspace,
    )
from .passage_evidence import (
    PassageEvidenceAssociationStore,
    VideoClockCalibrationStore,
)
from .passage_receiver import (
        DEFAULT_HOST,
        DEFAULT_PORT,
        PassageEvent,
        PassageEventReceiver,
        PassageEventStore,
        RaceFocus,
    )
from .passage_review import (
    PassageEvidencePane,
    PassageReviewSurface,
    source_location,
)
from .point_playback import PointPlaybackUnavailable, prepare_point_playback
from .preflight import (
        PreflightJournal,
        PreflightRun,
    )
from .racetiger_source import RaceTigerClient, RaceTigerSource, RaceTigerStatus
from .race_metadata import RaceMetadata, RaceMetadataStore
from .workspace_context import (
    EventWorkspaceMetadataStore, is_event_folder_under, recovered_archive_path, saved_archive_path, saved_live_path,
    saved_received_path, save_workspace_mode,
)
from .review_export import export_review_summary
from .runtime_paths import relocated_video_path
from .review_clip import PassageReviewBindingStore
from .receiver_controller import ReceiverController
from .recording_lock import LOCK_FILENAME, FolderInUseError, RecordingFolderLock
from .recording_controller import (
    PendingRecordingPassage,
    RecordingSessionController,
    RecordingRecoveryState,
    RecordingStopFailure,
)
from .settings import FinishReviewSettings
from .video_discovery import VideoDiscoveryStore
from .review_recorder import (
    ArchiveTimelinePublisher,
    FfmpegReviewRecorder,
    PassageReviewCoordinator,
    PassageReviewState,
    PassageReviewTimelinePublisher,
    PassageReviewWindow,
    ReviewRingBuffer,
    is_supported_review_source,
    load_archive_recording_sessions,
    )
from .stream_recorder import (
        is_rtsp_source,
        RecordingError,
        sanitize_recording_message,
    )
from .video_timeline import (
        DEFAULT_CLOCK_SOURCE,
        DEFAULT_TIMING_ERROR_MS,
        PassageVideoLocation,
        PassageVideoLookup,
        RecordingSegment,
        VideoTimelineStore,
    )
from .thread_lifecycle import retire_qthread, track_qthread
from .video_playback import VideoPlaybackDialog
from .video_arrival import VideoArrivalCandidateStore
from .video_review import VideoReviewJournal
from .visual_crossing import (
    CrossingConfig,
    VisualCrossingEvent,
    VisualCrossingWorker,
)


from .launch_dialog import (
    _open_event_directory as _open_event_directory,
    _is_rtsp_auth_error as _is_rtsp_auth_error,
    EventWorkspacePickerDialog as EventWorkspacePickerDialog,
    FinishReviewLaunchDialog as FinishReviewLaunchDialog,
    _format_rtsp_probe_error as _format_rtsp_probe_error,
)
from .review_window_widgets import (
    _CompactStatusIndicator,
    _ElidedLabel,
    _FinishReviewLogo,
    _PassageSignalBridge,
)
from .review_window_common import (
    BEIJING_TIMEZONE,
    CYCLERACE_INBOX_DIRNAME,
    HIGH_SPEED_INDEX_FILENAME,
    IS_WINDOWS,
    LIVE_EVIDENCE_DATE_TOLERANCE_MS,
    PROBLEM_FRAME_TOLERANCE_MS,
    _INVALID_EVENT_FOLDER_CHARS,
    _TEST_GROUP_MARKERS,
    _TEST_GROUP_NAMES,
    _VideoCandidateDelivery,
    _VideoCandidatePersistenceFailure,
    _WINDOWS_RESERVED_NAMES,
    _align_live_evidence_timestamp,
    _event_workspace_dir,
    _format_point_playback_time,
    _high_speed_target_dates,
    _historical_evidence_timestamp_overrides,
    _is_test_group_name,
    _safe_event_folder_name,
    _saved_race_folders,
    _saved_race_id,
)
from .review_window_settings import FinishReviewSettingsMixin
from .review_window_status import FinishReviewStatusMixin
from .review_window_problems import FinishReviewProblemsMixin
from .review_window_recording import FinishReviewRecordingMixin
from .review_window_receiving import FinishReviewReceivingMixin
from .review_window_capture import FinishReviewCaptureMixin


logger = logging.getLogger("FinishReview")


class FinishReviewWindow(
    FinishReviewSettingsMixin,
    FinishReviewStatusMixin,
    FinishReviewProblemsMixin,
    FinishReviewRecordingMixin,
    FinishReviewReceivingMixin,
    FinishReviewCaptureMixin,
    PassageReviewSurface,
):
    """Production console for recording, CycleRace intake, and evidence review."""

    FILMSTRIP_ALWAYS_AVAILABLE = True

    def __init__(
        self,
        source: str,
        output_dir: str | Path,
        parent=None,
        *,
        passage_host: str = DEFAULT_HOST,
        passage_port: int = DEFAULT_PORT,
        camera_index: int = 1,
        secondary_source: str = "",
        high_speed_dir: str | Path | None = None,
        finishreview_ip: str = "192.168.50.10",
        cyclerace_ip: str = "192.168.50.20",
        high_speed_pc_ip: str = "192.168.50.30",
        switch_ip: str = "192.168.50.2",
        timing_provider: str = "cyclerace",
        racetiger_base_url: str = "",
        racetiger_pc: str = "",
        racetiger_rid: str = "",
        racetiger_token: str = "",
        racetiger_poll_interval_seconds: float = 2.0,
        visual_detection_enabled: bool = False,
        visual_camera_index: int = 1,
        visual_finish_line: float = 0.50,
        visual_gate_width: float = 0.08,
        visual_forward_direction: str = "left_to_right",
        visual_roi_top: float = 0.08,
        visual_roi_bottom: float = 0.95,
        ffmpeg_path: Path | None = None,
        review_retention_seconds: int = 360,
        timing_error_ms: int = DEFAULT_TIMING_ERROR_MS,
        refresh_interval_ms: int = 500,
        passage_batch_interval_ms: int = 150,
        video_assist_enabled: bool = True,
        recorder_factory: Callable[..., FfmpegReviewRecorder] = FfmpegReviewRecorder,
        receiver_factory: Callable[..., PassageEventReceiver] = PassageEventReceiver,
        settings_saver: Callable[[FinishReviewSettings], None] | None = None,
    ):
        self._video_assist_enabled_value = bool(video_assist_enabled)
        self.source = str(source).strip()
        self.workspace_root = Path(output_dir).expanduser().resolve()
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.output_dir = self.workspace_root
        self.passage_host = str(passage_host).strip()
        self.passage_port = int(passage_port)
        self.camera_index = max(1, int(camera_index))
        self.secondary_source = str(secondary_source).strip()
        self.high_speed_dir = (
            Path(high_speed_dir).expanduser().absolute()
            if high_speed_dir is not None and str(high_speed_dir).strip()
            else None
        )
        self.finishreview_ip = str(finishreview_ip).strip()
        self.cyclerace_ip = str(cyclerace_ip).strip()
        self.high_speed_pc_ip = str(high_speed_pc_ip).strip()
        self.switch_ip = str(switch_ip).strip()
        self.timing_provider = (
            str(timing_provider or "cyclerace").strip().lower()
            if str(timing_provider or "cyclerace").strip().lower()
            in {"cyclerace", "racetiger"}
            else "cyclerace"
        )
        self.racetiger_base_url = str(racetiger_base_url or "").strip()
        self.racetiger_pc = str(racetiger_pc or "").strip()
        self.racetiger_rid = str(racetiger_rid or "").strip()
        self.racetiger_token = str(racetiger_token or "").strip()
        self.racetiger_poll_interval_seconds = max(
            0.5,
            float(racetiger_poll_interval_seconds or 2.0),
        )
        self._visual_detection_enabled = bool(visual_detection_enabled)
        self._visual_camera_index = max(1, int(visual_camera_index))
        self._visual_finish_line = float(visual_finish_line)
        self._visual_gate_width = float(visual_gate_width)
        self._visual_forward_direction = str(visual_forward_direction or "left_to_right")
        self._visual_roi_top = float(visual_roi_top)
        self._visual_roi_bottom = float(visual_roi_bottom)
        self.ffmpeg_path = Path(ffmpeg_path).resolve() if ffmpeg_path else None
        self.review_retention_seconds = max(6, int(review_retention_seconds))
        self.timing_error_ms = max(0, int(timing_error_ms))
        self._recorder_factory = recorder_factory
        self._receiver_factory = receiver_factory
        self._receiver_controller = ReceiverController(
            receiver_factory=receiver_factory,
            racetiger_client_factory=RaceTigerClient,
            racetiger_source_factory=RaceTigerSource,
        )
        self._recording_controller = RecordingSessionController(
            recorder_factory=recorder_factory,
        )
        self._recording_lock = RecordingFolderLock()
        self._settings_saver = settings_saver
        self._workspace_mode = "live"
        self._archive_background_passage_count = 0
        self._live_metadata_scope = None
        self._held_race_ids: set[str] = set()
        # Races whose recording the first chip already started, or the operator stopped.
        self._auto_recorded_race_ids: set[str] = set()
        self._recording_reminder_race = ""
        self._last_chip_race_id = ""
        # The base review surface starts timers while the production window is
        # still being assembled. Initialise health-check state before that
        # happens so an early clock tick cannot observe half-built state.
        self._started = False
        self._stop_requested = False

        inbox_dir = self.workspace_root / CYCLERACE_INBOX_DIRNAME
        self._receiver_passage_store = PassageEventStore(
            inbox_dir / "cyclerace_passage_inbox.jsonl"
        )
        self._receiver_metadata_store = RaceMetadataStore(
            inbox_dir / "cyclerace_metadata_inbox.json"
        )
        restored_received = (saved_received_path(self.workspace_root)
                             if self.timing_provider == "cyclerace" else None)
        restored_live = (saved_live_path(self.workspace_root)
                         if self.timing_provider == "cyclerace" else None)
        restored_selection = restored_received or restored_live
        restored_archive = ((saved_archive_path(self.workspace_root) or
                             (None if restored_selection else recovered_archive_path(self.workspace_root)))
                            if self.timing_provider == "cyclerace" else None)
        if restored_selection is not None:
            self.output_dir = restored_selection
        if restored_archive is not None:
            self.output_dir = restored_archive
            self._workspace_mode = "archive"
        if self.timing_provider == "cyclerace":
            inbox_metadata = self._receiver_metadata_store.current()
            if inbox_metadata is not None and restored_archive is None and restored_selection is None:
                restored_output_dir = _event_workspace_dir(
                    self.workspace_root,
                    inbox_metadata,
                )
                if restored_output_dir.exists():
                    self.output_dir = restored_output_dir.resolve()

        passage_store = PassageEventStore(
            self.output_dir
            / (
                "racetiger_passage_events.jsonl"
                if self.timing_provider == "racetiger"
                else "cyclerace_passage_events.jsonl"
            )
        )
        # CycleRace delivers events to the workspace-root inbox while this
        # window may be opened on another event workspace.  Reconcile the
        # latest revisions, including inactive tombstones, before building the
        # review surface so an idle/withdrawn passage cannot reappear after a
        # restart.
        if (self.timing_provider == "cyclerace" and inbox_metadata is not None
                and self._workspace_mode == "live" and restored_received is None):
            selected_metadata = (RaceMetadataStore(self.output_dir / "cyclerace_race_metadata.json").current()
                                 if restored_live is not None else None)
            if restored_live is None or (selected_metadata is not None and
                    (selected_metadata.race_id, selected_metadata.stage_id) ==
                    (inbox_metadata.race_id, inbox_metadata.stage_id)):
                self._merge_cyclerace_events(passage_store, inbox_metadata.race_id)
        timeline_store = VideoTimelineStore(self.output_dir / "video_timeline.jsonl")
        metadata_store = (
            None
            if self.timing_provider == "racetiger"
            else EventWorkspaceMetadataStore(self.output_dir / "cyclerace_race_metadata.json",
                                             passage_store, timeline_store)
        )
        if (metadata_store is not None and metadata_store.recovered and timeline_store.segments()
                and restored_selection is None):
            self._workspace_mode = "archive"
            metadata = metadata_store.current()
            metadata_store.select(metadata.race_id, metadata.stage_id)
            save_workspace_mode(self.workspace_root, self.output_dir, "archive")
        elif metadata_store is not None and metadata_store.current() is None and len(metadata_store.candidates()) > 1:
            self._workspace_mode = "archive"
        review_binding_store = PassageReviewBindingStore(
            self.output_dir / "review_clips.jsonl"
        )
        calibration_store = VideoClockCalibrationStore(
            self.output_dir / "video_clock_calibrations.jsonl"
        )
        historical_events = passage_store.events()
        self._evidence_timestamp_overrides = (
            _historical_evidence_timestamp_overrides(historical_events)
            if self.timing_provider == "cyclerace"
            else {}
        )
        self._high_speed_catalog = AuyatRgbCatalog(
            self.high_speed_dir,
            cache_path=self.output_dir / HIGH_SPEED_INDEX_FILENAME,
            target_dates=_high_speed_target_dates(
                historical_events,
                self._evidence_timestamp_overrides,
            ),
        )
        self._high_speed_scan_result = self._high_speed_catalog.snapshot()
        self._preflight_journal = PreflightJournal(
            self.output_dir / "preflight_tests.jsonl"
        )
        self._preflight_event_keys = set(self._preflight_journal.event_keys())
        regular_camera_indexes = tuple(
            camera_index
            for camera_index, source_value in (
                (self.camera_index, self.source),
                (self.camera_index + 1, self.secondary_source),
            )
            if is_supported_review_source(source_value)
        ) or (self.camera_index,)
        super().__init__(
            passage_store,
            timeline_store,
            parent,
            metadata_store=metadata_store,
            high_speed_locator=self._locate_high_speed,
            open_location=self._open_point_playback,
            regular_camera_indexes=regular_camera_indexes,
            show_high_speed_pane=self.high_speed_dir is not None,
            include_recorded_evidence=False,
            review_binding_store=review_binding_store,
            calibration_store=calibration_store,
            low_resource_mode=len(regular_camera_indexes) == 1,
        )
        self._runtime_metrics_reported = False

        self.setWindowTitle(APP_WINDOW_TITLE)
        self.setMinimumSize(1180, 760)
        self._recorder: FfmpegReviewRecorder | None = None
        self._recorders: dict[int, FfmpegReviewRecorder] = {}
        self._receiver: PassageEventReceiver | None = None
        self._racetiger_source: RaceTigerSource | None = None
        self._ring_buffer: ReviewRingBuffer | None = None
        self._ring_buffers: dict[int, ReviewRingBuffer] = {}
        self._coordinator: PassageReviewCoordinator | None = None
        self._coordinators: dict[int, PassageReviewCoordinator] = {}
        self._publisher: PassageReviewTimelinePublisher | None = None
        self._video_scan_workers: dict[int, object] = {}
        self._archive_video_scan_workers: dict[int, object] = {}
        self._visual_workers: dict[int, VisualCrossingWorker] = {}
        self._video_candidate_cache: dict[str, object] = {
            str(candidate.candidate_id): candidate
            for candidate in self.video_arrival_store.candidates()
        }
        self._visual_status = ""
        self._visual_failed = False
        self._video_candidate_dialogs: set[VideoPlaybackDialog] = set()
        self._video_scan_pause_tokens: set[object] = set()
        self._video_scan_generation = 0
        self._video_scan_tokens: dict[int, object] = {}
        self._video_scan_state_lock = threading.RLock()
        self._finish_line_store = FinishLineStore(
            self.output_dir / "motion_finish_lines.json", legacy_path=self.output_dir / "finish_lines.json"
        )
        self._finish_line_rois = self._finish_line_store.rois()
        self._video_review_journal = VideoReviewJournal(
            self.output_dir / "video_review.jsonl"
        )
        for record in self._video_review_journal.records():
            self.restore_video_review_record(
                record.candidate_id,
                status=record.status,
                bib=record.bib,
            )
        self._publishers: dict[int, PassageReviewTimelinePublisher] = {}
        self._archive_publishers = [
            ArchiveTimelinePublisher(session, self.timeline_store)
            for session in load_archive_recording_sessions(self.output_dir)
        ]
        self._capture_windows_by_camera: dict[
            int, dict[str, PassageReviewWindow]
        ] = {self.camera_index: {}}
        self._capture_windows = self._capture_windows_by_camera[self.camera_index]
        self._unsupported_event_ids: set[str] = set()
        self._runtime_error = ""
        self._auto_recording_error = ""
        self._capture_error = ""
        self._workspace_notice = ""
        self._receiver_error = ""
        self._racetiger_status: RaceTigerStatus | None = None
        self._racetiger_generation = 0
        self._last_cleanup_at = 0.0
        self._recording_started_at = 0.0
        self._recording_recovery = RecordingRecoveryState()
        self._camera_reconnect_attempts = self._recording_recovery.attempts
        self._camera_reconnect_not_before = self._recording_recovery.not_before
        self._camera_reconnect_errors = self._recording_recovery.errors
        self._camera_auth_failed_sources = self._recording_recovery.auth_failed_sources
        self._camera_segment_progress = self._recording_recovery.segment_progress
        self._historical_passage_count = len(self._events_for_current_metadata(passage_store.events()))
        self._received_passage_count = 0
        self._last_passage_monotonic = 0.0
        self._received_passage_sequence = 0
        self._received_event_order: dict[tuple[str, str, str], int] = {}
        self._pending_focus: RaceFocus | None = None
        self._pending_passages: dict[str, PassageEvent] = {}

        self._signal_bridge = _PassageSignalBridge(self)
        self._signal_bridge.accepted.connect(self._on_passage_received)
        self._signal_bridge.metadata_accepted.connect(self._on_metadata_received)
        self._signal_bridge.focus_accepted.connect(self._on_focus_received)
        self._signal_bridge.timing_status.connect(self._on_racetiger_status)
        self._signal_bridge.capture_refresh_finished.connect(
            self._on_capture_refresh_finished
        )
        self._capture_refresh_generation = 0
        self._capture_refresh_worker = CaptureRefreshWorker(
            self._signal_bridge.capture_refresh_finished.emit,
            metrics=self.runtime_metrics,
        )
        self._capture_refresh_worker.start()
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(max(100, int(refresh_interval_ms)))
        self._refresh_timer.timeout.connect(self._request_capture_refresh)
        self._passage_batch_timer = QTimer(self)
        self._passage_batch_timer.setSingleShot(True)
        self._passage_batch_timer.setInterval(
            max(0, int(passage_batch_interval_ms))
        )
        self._passage_batch_timer.timeout.connect(self._flush_passage_batch)
        self._clock_timer = QTimer(self)
        self._clock_timer.setInterval(1_000)
        self._clock_timer.timeout.connect(self._on_clock_tick)
        self._init_runtime_status()
        self._init_problem_list()
        self._init_operator_controls()
        self.problem_list_button.setChecked(True)
        self._open_problem_list()
        self._high_speed_scan_worker = AuyatRgbScanWorker(
            self._high_speed_catalog,
            self,
        )
        self._high_speed_scan_worker.scan_finished.connect(
            self._on_high_speed_scan_finished
        )
        self.video_candidate_requested.connect(self._open_video_candidate)
        self.video_candidates_received.connect(self._reconcile_video_candidates)
        self.video_candidate_persistence_failed.connect(
            self._on_video_candidate_persistence_failed
        )
        self.video_review_status_changed.connect(self._persist_video_review)
        self.record_deleted.connect(
            lambda _event_id: self._export_review_summary(show_warning=True)
        )
        if self._video_candidate_cache:
            self._reconcile_video_candidates(
                tuple(self._video_candidate_cache.values())
            )
        self.auto_advance_checkbox.setChecked(False)
        self.auto_advance_checkbox.hide()
        self._request_capture_refresh()
        self._clock_timer.start()
        self._ui_latency_probe = UiLatencyProbe(self.runtime_metrics, self)
        if self.high_speed_dir is not None:
            track_qthread(self._high_speed_scan_worker)
            self._high_speed_scan_worker.start()
        self._update_runtime_status()

    @property
    def recorder(self) -> FfmpegReviewRecorder | None:
        return self._recorder

    def _sync_evidence_pane_layout(self, *, include_recorded: bool = False) -> None:
        self.configure_evidence_panes(
            (camera_index for camera_index, _source in self._configured_recording_sources()),
            show_high_speed=self.high_speed_dir is not None,
            include_recorded=include_recorded,
        )
        if hasattr(self, "mark_regular_button"):
            try:
                self.mark_regular_button.clicked.disconnect()
            except TypeError:
                pass
            self.mark_regular_button = self.regular_pane.mark_btn
            self.mark_regular_button.clicked.connect(
                lambda: self._begin_marking(self._regular_pane_for_operator_marking())
            )
        if hasattr(self, "mark_high_speed_button"):
            self.mark_high_speed_button.setVisible(self.high_speed_dir is not None)
        self._update_operator_controls()

    @property
    def receiver(self) -> PassageEventReceiver | None:
        return self._receiver

    def _prepare_point_playback(self, *args, **kwargs):
        def prepare():
            self._publish_archive_segments()
            return prepare_point_playback(*args, **kwargs)
        return self._run_capture_task(prepare)

    def _open_point_playback(self, event: PassageEvent, location) -> None:
        evidence_timestamp_ms = self._evidence_timestamp(event)
        if evidence_timestamp_ms is None:
            QMessageBox.information(
                self,
                "无法定点回放",
                "当前通过记录缺少可用于录像定位的绝对时间。",
            )
            return
        anchor_time_ms = int(evidence_timestamp_ms) + int(self._shared_delta_ms)
        try:
            session = self._prepare_point_playback(
                self.timeline_store,
                location,
                anchor_time_ms=anchor_time_ms,
                race_id=event.race_id,
                output_dir=self.output_dir,
                ring_buffer=self._ring_buffers.get(
                    location.segment.camera_index,
                    self._ring_buffer,
                ),
            )
        except (OSError, PointPlaybackUnavailable, RuntimeError, ValueError) as error:
            QMessageBox.information(self, "无法定点回放", str(error))
            return

        identity = event.bib.strip() or "未知"
        available_before_ms = max(
            0,
            anchor_time_ms - session.available_started_at_ms,
        )
        available_after_ms = max(
            0,
            session.available_ended_at_ms - anchor_time_ms,
        )
        context_text = (
            f"{identity}号 | 目标 {_format_point_playback_time(anchor_time_ms)} | "
            f"实际可用：前 {available_before_ms / 1000.0:.1f} 秒，"
            f"后 {available_after_ms / 1000.0:.1f} 秒"
        )
        self._set_sync_playing(False)
        self.regular_pane.set_playing(False)
        playback = VideoPlaybackDialog(
            session.manifest_path,
            self,
            initial_position_ms=max(0, session.target_position_ms - 10_000),
            target_position_ms=session.target_position_ms,
            context_text=context_text,
            autoplay=True,
            reverse_prefetch=True,
            window_title=f"定点回放 - {identity}号",
        )
        pause_token = self._pause_video_scan_workers()
        try:
            playback.exec_()
        finally:
            self._run_capture_task(session.cleanup)
            self._resume_video_scan_workers(pause_token)

    def _select_event(
        self,
        event_id: str,
        *,
        preserve_current_frame: PassageEvidencePane | None = None,
        locate_target: bool = False,
    ) -> None:
        super()._select_event(
            event_id,
            preserve_current_frame=preserve_current_frame,
            locate_target=locate_target,
        )

    def _clear_selection_details(self) -> None:
        super()._clear_selection_details()
        self._update_operator_controls()
        if hasattr(self, "high_speed_status_label"):
            self._update_runtime_status()

    def _configure_devices(self) -> None:
        dialog = FinishReviewLaunchDialog(
            self._current_settings(),
            self,
            ffmpeg_path=self.ffmpeg_path,
            passage_provider=lambda: self.passage_store.events(),
            evidence_provider=self._preflight_evidence_status,
            runtime_snapshot_provider=self._deployment_runtime_snapshot,
            event_export_callback=lambda: self._export_review_summary(
                show_warning=True
            ),
            event_workspace_provider=self._saved_event_workspaces,
            event_workspace_summary_provider=self._saved_event_summary,
            event_open_callback=self._open_saved_event_workspace,
            return_live_event_callback=self._return_to_live_event,
            recheck_callback=self._recheck_connections,
            recording_start_callback=self._start_preflight_recording,
            preflight_event_callback=self._record_preflight_event,
            preflight_restore_callback=self._restore_latest_preflight_event,
            passage_reception_order_provider=lambda: dict(
                self._received_event_order
            ),
        )
        if dialog.exec_() != QDialog.Accepted:
            return
        settings = dialog.settings
        disruptive_change = bool(
            self._recording_any_active()
            and (
                str(settings.source).strip() != self.source
                or str(settings.secondary_source).strip() != self.secondary_source
                or Path(settings.output_dir).expanduser().resolve()
                != self.workspace_root
            )
        )
        if disruptive_change:
            answer = QMessageBox.question(
                self,
                "停止录像并应用设置",
                "录像设备或证据目录已经变化，需要停止当前录像才能应用。是否继续？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
        self._apply_settings(settings, stop_recording=disruptive_change)

    def _uses_popup_judging(self):
        return not getattr(self, "_problem_workbench_active", False) and super()._uses_popup_judging()

    def _toggle_maximized_pane(self, pane):
        # Reopening a paused frame may not decode again. Refresh saved geometry
        # on every F entry as well as on frame changes, including popup judging.
        # Force the read: two rapid saves can share Windows' coarse mtime.
        self._review_line_signature = None
        self._refresh_problem_finish_lines()
        super()._toggle_maximized_pane(pane)

    def _begin_marking(self, pane):
        if not getattr(self, "_problem_workbench_active", False):
            return super()._begin_marking(pane)

    def _confirm_pending_marker(self, pane):
        if getattr(self, "_problem_workbench_active", False):
            return False
        return super()._confirm_pending_marker(pane)

    def _select_event(self, *args, **kwargs):
        # Incoming chip messages and CycleRace focus never take over the
        # evidence frame while an operator works through exceptions.
        if getattr(self, "_problem_workbench_active", False):
            return
        return super()._select_event(*args, **kwargs)

    def _initialize_review_splitters(self):
        if getattr(self, "_problem_workbench_active", False):
            self.evidence_splitter.show()
            self._distribute_evidence_panes()
            return
        super()._initialize_review_splitters()

    def _toggle_filmstrip_size(self):
        if not getattr(self, "_problem_workbench_active", False):
            return super()._toggle_filmstrip_size()
        sizes = self.review_content_splitter.sizes()
        self.review_content_splitter.setSizes([sum(sizes) // 2, sum(sizes) // 2])

    def keyPressEvent(self, event) -> None:
        """The judging keys work from whatever corner of the workbench holds the keys."""

        if (getattr(self, "_problem_workbench_active", False)
                and self.problem_panel._queue_key(event)):
            event.accept()
            return
        super().keyPressEvent(event)

    def _continuous_marker_overlays(self, pane):
        """The counted people ride along with the crossing markers of the same frame."""

        if (getattr(self, "_problem_workbench_active", False)
                and getattr(self.problem_panel, "quick_review", False)):
            return self._problem_count_overlays(pane)
        overlays = list(super()._continuous_marker_overlays(pane))
        overlays.extend(self._problem_count_overlays(pane))
        return tuple(overlays)

    def _on_preview_frame_ready(self, pane, image, position_ms, frame_index):
        super()._on_preview_frame_ready(pane, image, position_ms, frame_index)
        self._refresh_problem_finish_lines()
        if not getattr(self, "_problem_workbench_active", False):
            return
        pane.video_view.set_identity_badge_visible(False)
        pane.active_badge.hide()
        pane.status_label.hide()
        if getattr(self.problem_panel, "quick_review", False):
            # The red "待补录" identity guide is not the camera's finish line.
            pane.video_view.clear_marker()
            pane.video_view.set_history_markers(self._problem_count_overlays(pane))
            self._show_problem_count_badge(pane)
            self.problem_panel._sync_arrival_confirmation()
        if pane is not self._active_playback_pane():
            return
        item = self.problem_panel.current_item()
        frame = item.evidence.get("frame", {}) if item else {}
        actual = self._frame_for_problem_count(pane)
        if actual:
            stamp = self.problem_panel.record_viewed_frame(actual, actual["recorder_ms"])
            if getattr(self.problem_panel, "quick_review", False):
                pane.set_judged_time(actual["position_ms"], stamp)  # the table's clock, not the recorder's
        normal = self._normal_crossing_cards.get(self._opened_normal_crossing)
        if normal is not None:
            self.problem_panel.set_browsing(True)
            self.problem_panel.hint.setText(f"{normal.bibs[0] or '该选手'} 已自动对应；这是复查，不会新建待处理。")
            normal_frame = normal.evidence["frame"]
            if (pane.camera_index == int(normal_frame.get("camera_index", 1))
                    and frame_index == normal_frame.get("frame_index")
                    and pane.location.segment.segment_id == normal_frame.get("segment_id")):
                self.crossing_cards.set_preview(normal.item_id, pane.video_view._pixmap_item.pixmap())
            self._problem_count_mode_changed(True)
            return
        if frame:
            context = self.problem_panel._inline_context()
            within_group = (actual is not None and context is not None and pane.camera_index == context.camera
                            and context.start_ms <= actual["recorder_ms"] <= context.end_ms)
            self.problem_panel.set_browsing(not (within_group or self._shows_the_problem_frame(pane, position_ms, frame)))
        self._problem_count_mode_changed(True)

    def _open_race_filmstrip_frame(self, frame):
        if getattr(self, "_problem_workbench_active", False):
            self.problem_panel.set_browsing(True)
        return super()._open_race_filmstrip_frame(frame)

    def start(self) -> None:
        """Compatibility helper used by automation that starts the full session."""

        self.start_receiver()
        self.start_recording()

    def _events_for_current_metadata(
        self,
        events: tuple[PassageEvent, ...],
    ) -> tuple[PassageEvent, ...]:
        filtered = super()._events_for_current_metadata(events)
        if self.timing_provider == "racetiger" and self.racetiger_rid:
            filtered = tuple(
                event for event in filtered if event.race_id == self.racetiger_rid
            )
        filtered = tuple(
            event
            for event in filtered
            if (event.race_id, event.stage_id, event.event_id)
            not in self._preflight_event_keys
        )
        if (self.timing_provider == "cyclerace" and self._current_metadata() is None
                and len({(event.race_id, event.stage_id) for event in filtered}) > 1):
            return ()
        return filtered

    def _lookup(self, event: PassageEvent):
        evidence_timestamp_ms = self._evidence_timestamp(event)
        lookup_event = event
        if (
            evidence_timestamp_ms is None
            or evidence_timestamp_ms == event.timeline_timestamp_ms
        ):
            lookup = super()._lookup(event)
        else:
            lookup_event = replace(
                event,
                passage_timestamp_ms=evidence_timestamp_ms,
            )
            lookup = super()._lookup(lookup_event)

        if not hasattr(self, "_capture_windows_by_camera"):
            return lookup
        locations = list(lookup.locations)
        finalized_cameras = {
            location.segment.camera_index
            for location in locations
            if location.segment.clock_source == DEFAULT_CLOCK_SOURCE
            and location.status in {"located", "near_boundary", "unverified"}
        }
        for camera_index, windows in self._capture_windows_by_camera.items():
            if camera_index in finalized_cameras:
                continue
            window = windows.get(event.event_id)
            publisher = self._publishers.get(camera_index)
            if (
                window is None
                or publisher is None
                or window.state is PassageReviewState.READY
            ):
                continue
            preview = getattr(self, "_capture_previews", {}).get((camera_index, event.event_id))
            if (
                preview is None
                or preview.media_started_at_ms is None
                or preview.media_duration_ms is None
            ):
                continue
            media_end_at_ms = (
                preview.media_started_at_ms + preview.media_duration_ms
            )
            if not (
                preview.media_started_at_ms
                <= lookup.target_time_ms
                <= media_end_at_ms
            ):
                continue
            passage_position_ms = (
                lookup.target_time_ms - preview.media_started_at_ms
            )
            locations.append(
                PassageVideoLocation(
                    segment=preview,
                    video_path=Path(preview.video_path),
                    passage_position_ms=passage_position_ms,
                    playback_position_ms=max(
                        0,
                        passage_position_ms - self.pre_roll_ms,
                    ),
                    clock_offset_ms=self.clock_offset_ms,
                    timing_error_ms=preview.timing_error_ms,
                    status="preview",
                    media_locator=preview.segment_id,
                )
            )
        if not any(location.status == "preview" for location in locations):
            return lookup
        if any(location.status == "located" for location in locations):
            status = "located"
        elif any(location.status == "preview" for location in locations):
            status = "preview"
        elif any(location.status == "near_boundary" for location in locations):
            status = "near_boundary"
        elif any(location.status == "unverified" for location in locations):
            status = "unverified"
        else:
            status = lookup.status
        return PassageVideoLookup(
            status,
            lookup.target_time_ms,
            tuple(locations),
        )

    def _open_video_candidate(self, item) -> None:
        candidate = getattr(item, "candidate", None)
        if candidate is not None and str(getattr(candidate, "segment_id", "")).startswith(
            "visual:"
        ):
            self._open_visual_candidate(item)
            return
        video_path = Path(str(getattr(candidate, "video_path", ""))).expanduser()
        if not video_path.is_file() and candidate is not None:
            segment_id = str(getattr(candidate, "segment_id", "")).strip()
            segment = self.timeline_store.get_segment(segment_id) if segment_id else None
            if segment is not None:
                video_path = self.timeline_store.resolve_video_path(segment)
        if candidate is None or not video_path.is_file():
            self._capture_error = "视频异常没有可打开的录像文件"
            self._update_runtime_status()
            return
        dialog = VideoPlaybackDialog(
            video_path,
            self,
            initial_position_ms=max(
                0,
                int(getattr(candidate, "video_position_ms", 0)) - 3_000,
            ),
            target_position_ms=int(getattr(candidate, "video_position_ms", 0)),
            context_text=(
                f"视频异常：{getattr(item, 'anomaly', '待核实')}；"
                f"机位 {getattr(candidate, 'camera_index', '?')}"
            ),
            autoplay=False,
            reverse_prefetch=True,
            window_title="视频异常复核",
        )
        pause_token = self._pause_video_scan_workers()
        self._video_candidate_dialogs.add(dialog)
        dialog.finished.connect(
            lambda _result=0, current=dialog: self._video_candidate_dialogs.discard(
                current
            )
        )
        dialog.finished.connect(
            lambda _result=0, token=pause_token: self._resume_video_scan_workers(token)
        )
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _open_visual_candidate(self, item) -> None:
        candidate = getattr(item, "candidate", None)
        if candidate is None:
            return
        metadata = self._current_metadata()
        race_id = metadata.race_id if metadata is not None else None
        clock_offset_ms = self._clock_offset_for_camera(int(getattr(candidate, "camera_index", 1)))
        def prepare():
            self._publish_archive_segments()
            lookup = self.timeline_store.locate_passage(
                int(getattr(candidate, "peak_at_ms", 0)),
                clock_offset_ms=clock_offset_ms,
                pre_roll_ms=self.pre_roll_ms,
                race_id=race_id,
            )
            location = next(
                (
                    value
                    for value in lookup.locations
                    if self.timeline_store.video_path_is_playable(value.video_path)
                ),
                None,
            )
            if location is None:
                raise PointPlaybackUnavailable("视觉异常暂时没有可播放的录像片段")
            return prepare_point_playback(
                self.timeline_store,
                location,
                anchor_time_ms=lookup.target_time_ms,
                race_id=race_id,
                output_dir=self.output_dir,
                ring_buffer=self._ring_buffers.get(
                    location.segment.camera_index,
                    self._ring_buffer,
                ),
            )
        try:
            session = self._run_capture_task(prepare)
        except (OSError, PointPlaybackUnavailable, RuntimeError, ValueError) as error:
            self._capture_error = sanitize_recording_message(error)
            self._update_runtime_status()
            return
        playback = VideoPlaybackDialog(
            session.manifest_path,
            self,
            initial_position_ms=max(0, session.target_position_ms - 1_000),
            target_position_ms=session.target_position_ms,
            context_text=f"视觉异常：{getattr(item, 'anomaly', '待复核')}",
            autoplay=False,
            reverse_prefetch=True,
            window_title="视觉异常复核",
        )
        pause_token = self._pause_video_scan_workers()
        try:
            playback.exec_()
        finally:
            self._run_capture_task(session.cleanup)
            self._resume_video_scan_workers(pause_token)

    def stop(self) -> bool:
        self._shutdown_requested = True
        self._refresh_timer.stop()
        if not self._capture_refresh_worker.stop(timeout=0.1):
            return False
        self._passage_batch_timer.stop()
        self._pending_passages.clear()
        worker = getattr(self, "_high_speed_scan_worker", None)
        if worker is not None and worker.isRunning():
            worker.stop()
            if not worker.wait(1_000):
                return False
        recording_failures = self.stop_recording()
        if not self._capture_refresh_worker.stop(timeout=0.1):
            return False
        if any(failure.still_running for failure in recording_failures):
            return False
        self._stop_archive_video_scan_workers()
        self.stop_receiver()
        for dialog in tuple(self._video_candidate_dialogs):
            dialog.close()
        self._video_candidate_dialogs.clear()
        self._update_runtime_status()
        self._log_runtime_metrics()
        return True

    def closeEvent(self, event) -> None:
        if getattr(self, "_capture_task_waiting", False):
            self._defer_capture_callback(self.close)
            event.ignore()
            return
        self._clock_timer.stop()
        self.problem_panel.stop_analysis()
        self.problem_panel.stop_chip_checks()
        self.problem_panel.save_drafts()
        if hasattr(self, "_ui_latency_probe"):
            self._ui_latency_probe.stop()
        if not self.stop():
            event.ignore()
            self.setEnabled(False)
            self.setWindowTitle(f"{APP_WINDOW_TITLE} - 正在停止后台扫描")
            QTimer.singleShot(100, self.close)
            return
        self._export_review_summary()
        super().closeEvent(event)


__all__ = [
    "EventWorkspacePickerDialog",
    "FinishReviewLaunchDialog",
    "FinishReviewSettings",
    "FinishReviewWindow",
]
