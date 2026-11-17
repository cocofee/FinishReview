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


logger = logging.getLogger("FinishReview")


class FinishReviewWindow(
    FinishReviewSettingsMixin,
    FinishReviewStatusMixin,
    FinishReviewProblemsMixin,
    FinishReviewRecordingMixin,
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

    def _visual_crossing_config(self) -> CrossingConfig:
        return CrossingConfig(
            finish_line=self._visual_finish_line,
            gate_width=self._visual_gate_width,
            forward_direction=self._visual_forward_direction,
            roi_top=self._visual_roi_top,
            roi_bottom=self._visual_roi_bottom,
        ).normalized()

    def _start_visual_workers(self) -> None:
        """Start advisory crossing detection only for the selected RTSP camera."""
        self._stop_visual_workers()
        self._visual_failed = False
        # In the single-camera field profile the batch scanner consumes the
        # completed low-rate HLS preview. Starting a second RTSP decoder would
        # compete with the recorder and the foreground judge on old CPUs.
        if len(self._configured_recording_sources()) <= 1:
            self._visual_status = "disabled_single_camera_batch_scan"
            return
        if not self._video_assist_enabled():
            self._visual_status = "disabled"
            return
        if not self._visual_detection_enabled:
            self._visual_status = "disabled"
            return
        source = next(
            (
                source_value
                for camera_index, source_value in self._configured_recording_sources()
                if camera_index == self._visual_camera_index
                and is_rtsp_source(source_value)
            ),
            "",
        )
        if not source:
            self._visual_status = "no_rtsp_source"
            return
        worker = VisualCrossingWorker(
            source,
            self._visual_camera_index,
            self.output_dir / "visual_crossing_events.jsonl",
            self,
            config=self._visual_crossing_config(),
        )
        worker.crossing_detected.connect(self._on_visual_crossing)
        worker.status_changed.connect(self._on_visual_status)
        worker.failed.connect(self._on_visual_failed)
        self._visual_workers[self._visual_camera_index] = worker
        worker.start()
        self._visual_status = "starting"

    def _stop_visual_workers(self) -> None:
        workers = tuple(self._visual_workers.values())
        self._visual_workers = {}
        for worker in workers:
            worker.request_stop()
        for worker in workers:
            if worker.isRunning() and not worker.wait(2_000):
                logger.warning("Visual crossing worker did not stop promptly")
                retire_qthread(worker)

    def _on_visual_status(self, message: str) -> None:
        self._visual_status = str(message or "")
        if "已启动" in self._visual_status:
            self._visual_failed = False
        self._update_runtime_status()

    def _on_visual_failed(self, message: str) -> None:
        self._visual_status = str(message or "")
        self._visual_failed = True
        self._update_runtime_status()

    def _on_visual_crossing(self, event: VisualCrossingEvent) -> None:
        # Reverse crossings are evidence of camera movement or a wrong direction,
        # not finish-line candidates for the operator queue.
        if event.direction != "forward":
            return
        tools = importlib.import_module(
            ".video_passage_det" + "ector", __package__
        )
        candidate = tools.VideoPassageCandidate(
            candidate_id=f"visual:{event.event_id}",
            camera_index=event.camera_index,
            started_at_ms=event.timestamp_ms,
            ended_at_ms=event.timestamp_ms,
            peak_at_ms=event.timestamp_ms,
            peak_score=event.confidence,
            changed_area=0.0,
            segment_id=f"visual:{event.event_id}",
        )
        self._on_video_candidates(
            (candidate,),
            self._video_scan_generation,
        )

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

    def _locate_high_speed(
        self,
        event: PassageEvent,
        clock_offset_ms: int,
        pre_roll_ms: int,
    ):
        return self._high_speed_catalog.locate(
            event.timeline_timestamp_ms,
            race_id=event.race_id,
            clock_offset_ms=clock_offset_ms,
            pre_roll_ms=pre_roll_ms,
        )

    def _run_capture_task(self, operation):
        self._capture_refresh_worker.start()
        self._capture_task_waiting = True
        try:
            return wait_for_background(self._capture_refresh_worker.submit_task(operation), self)
        finally:
            self._capture_task_waiting = False
            callbacks = getattr(self, "_capture_deferred_callbacks", [])
            self._capture_deferred_callbacks = []
            for callback in callbacks:
                QTimer.singleShot(0, callback)

    def _defer_capture_callback(self, callback, *args):
        if getattr(self, "_shutdown_requested", False):
            return True
        if not getattr(self, "_capture_task_waiting", False):
            return False
        if not hasattr(self, "_capture_deferred_callbacks"):
            self._capture_deferred_callbacks = []
        self._capture_deferred_callbacks.append(lambda: callback(*args))
        return True

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

    def _on_high_speed_scan_finished(self, result: AuyatScanResult) -> None:
        self._high_speed_scan_result = result
        if result.changed:
            self.invalidate_external_locations()
        self._update_runtime_status()

    def _request_high_speed_scan(self) -> None:
        self._high_speed_scan_result = self._high_speed_catalog.snapshot()
        if self.high_speed_dir is None:
            worker = self._high_speed_scan_worker
            if worker.isRunning():
                worker.stop()
                if not worker.wait(1_000):
                    logger.warning("High-speed scan worker did not stop promptly")
            self._update_runtime_status()
            return
        if not self._high_speed_scan_worker.isRunning():
            track_qthread(self._high_speed_scan_worker)
            self._high_speed_scan_worker.start()
        else:
            self._high_speed_scan_worker.request_scan()

    def _include_high_speed_event_date(self, event: PassageEvent) -> None:
        timestamp_ms = self._evidence_timestamp(event)
        if timestamp_ms is None:
            return
        event_date = datetime.fromtimestamp(
            timestamp_ms / 1000.0,
            tz=BEIJING_TIMEZONE,
        ).date()
        dates = self._high_speed_catalog.target_dates
        if event_date in dates:
            return
        if self._high_speed_catalog.set_target_dates((*dates, event_date)):
            self.invalidate_external_locations()
            self._request_high_speed_scan()

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

    def _start_archive_video_scan_workers(self) -> None:
        """Scan existing ordinary recordings when opening an archive workspace."""

        if not self._video_assist_enabled() or self._recording_any_active():
            return
        self._stop_archive_video_scan_workers()
        segments_by_camera: dict[int, tuple[object, ...]] = {}
        for segment in self.timeline_store.segments():
            if segment.clock_source != DEFAULT_CLOCK_SOURCE:
                continue
            camera_index = max(1, int(segment.camera_index))
            segments_by_camera.setdefault(camera_index, ())
            segments_by_camera[camera_index] = (
                *segments_by_camera[camera_index],
                segment,
            )
        if not segments_by_camera:
            return
        try:
            scan_module = importlib.import_module(
                ".video_passage_det" + "ector", __package__
            )
            worker_type = scan_module.VideoPassageScanWorker
        except (ImportError, AttributeError) as error:
            logger.warning("Archived video candidate scanner unavailable: %s", error)
            return

        for camera_index, camera_segments in segments_by_camera.items():
            def provider(values=camera_segments):
                return values

            generation = self._video_scan_generation
            token = object()
            self._video_scan_tokens[camera_index] = token

            worker = worker_type(
                provider,
                lambda candidates, generation=generation,
                camera_index=camera_index, token=token: self._on_video_candidates(
                    candidates,
                    generation,
                    worker_token=(camera_index, token),
                ),
                camera_index=camera_index,
                width=640,
                height=360,
                ffmpeg_path=self.ffmpeg_path or "ffmpeg",
                sample_fps=4.0,
                continuous=False,
                roi=self._finish_line_rois.get(
                    camera_index, (0.35, 0.15, 0.65, 0.95)
                ),
                interval_seconds=1.0,
                path_resolver=self.timeline_store.resolve_video_path,
                finish_line=self._finish_line_store.get(camera_index),
            )
            self._archive_video_scan_workers[camera_index] = worker
            worker.start()

    def _stop_archive_video_scan_workers(self) -> None:
        with self._video_scan_state_lock:
            self._video_scan_generation += 1
            worker_items = tuple(self._archive_video_scan_workers.items())
            workers = tuple(worker for _camera_index, worker in worker_items)
            self._archive_video_scan_workers = {}
            for camera_index, _worker in worker_items:
                self._video_scan_tokens.pop(camera_index, None)
        for worker in workers:
            try:
                worker.stop()
            except Exception:  # noqa: BLE001 - shutdown remains best effort.
                logger.exception("Failed to stop archived video scanner")
        self._update_operator_controls()
        if hasattr(self, "high_speed_status_label"):
            self._update_runtime_status()

    def _reap_finished_archive_video_scan_workers(self) -> None:
        """Drop completed one-shot archive scanners from runtime state."""

        finished: list[tuple[int, object]] = []
        for camera_index, worker in tuple(self._archive_video_scan_workers.items()):
            if not bool(getattr(worker, "is_running", False)):
                finished.append((camera_index, worker))
        if not finished:
            return
        for camera_index, worker in finished:
            self._archive_video_scan_workers.pop(camera_index, None)
            self._video_scan_tokens.pop(camera_index, None)
            stop = getattr(worker, "stop", None)
            if callable(stop):
                stop(timeout=0.1)

    def _toggle_archive_video_scan(self) -> None:
        """Start or stop the optional, one-shot historical video scan."""

        if (
            self._workspace_mode != "archive"
            or self._recording_any_active()
            or not self._video_assist_enabled()
        ):
            return
        self._reap_finished_archive_video_scan_workers()
        if self._archive_video_scan_workers:
            self._stop_archive_video_scan_workers()
        else:
            self._start_archive_video_scan_workers()
        self._update_runtime_status()

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

    def start_receiver(self) -> None:
        if self.timing_provider == "racetiger":
            if self._receiver_controller.receiver is not None:
                self.stop_receiver()
            if (
                self._receiver_controller.racetiger_source is not None
                and self._receiver_controller.racetiger_source.is_running
            ):
                return
            try:
                self._start_racetiger_source()
            except Exception as exc:
                self._receiver_error = sanitize_recording_message(exc)
                self._racetiger_status = RaceTigerStatus(
                    "error",
                    f"RaceTiger: {self._receiver_error}",
                )
                self._update_runtime_status()
                raise
            return
        if self._receiver_controller.racetiger_source is not None:
            self.stop_receiver()
        receiver = self._receiver_controller.receiver
        if receiver is not None and receiver.is_running:
            self._receiver = receiver
            return
        try:
            receiver = self._receiver_controller.start_cyclerace(
                self.passage_host,
                self.passage_port,
                self._receiver_passage_store,
                on_accepted=self._signal_bridge.accepted.emit,
                metadata_store=self._receiver_metadata_store,
                on_metadata_accepted=self._signal_bridge.metadata_accepted.emit,
                on_focus_accepted=self._signal_bridge.focus_accepted.emit,
            )
        except Exception as exc:
            self._receiver_error = sanitize_recording_message(exc)
            self._update_runtime_status()
            raise
        self._receiver = receiver
        self._receiver_error = ""
        self._refresh_timer.start()
        self._update_runtime_status()

    def _start_racetiger_source(self) -> None:
        source = self._receiver_controller.start_racetiger(
            self.racetiger_base_url,
            self.racetiger_token,
            pc=self.racetiger_pc,
            rid=self.racetiger_rid,
            store=self.passage_store,
            poll_interval_seconds=self.racetiger_poll_interval_seconds,
            on_event=lambda event, _generation: self._signal_bridge.accepted.emit(event),
            on_status=lambda status, _generation: self._signal_bridge.timing_status.emit(status),
        )
        self._racetiger_source = source
        self._receiver_error = ""
        self._racetiger_status = RaceTigerStatus(
            "starting",
            "RaceTiger: polling started",
        )
        self._refresh_timer.start()
        self._update_runtime_status()

    def start(self) -> None:
        """Compatibility helper used by automation that starts the full session."""

        self.start_receiver()
        self.start_recording()

    def _passage_timestamp(self, event: PassageEvent) -> int | None:
        timestamp_ms = int(event.timeline_timestamp_ms)
        if event.passage_timestamp_ms is None and timestamp_ms < 86_400_000:
            return None
        return timestamp_ms

    def _evidence_timestamp(self, event: PassageEvent) -> int | None:
        timestamp_ms = self._passage_timestamp(event)
        if timestamp_ms is None:
            return None
        override = self._evidence_timestamp_overrides.get(event.event_id)
        if override is not None and override[0] == timestamp_ms:
            return override[1]
        return timestamp_ms

    def _is_test_passage(self, event: PassageEvent) -> bool:
        event_key = (event.race_id, event.stage_id, event.event_id)
        if event_key in self._preflight_event_keys:
            return True
        group_names = [event.group_id, event.group_name]
        metadata = self._current_metadata()
        if metadata is not None and (
            metadata.race_id == event.race_id
            and metadata.stage_id == event.stage_id
        ):
            group_names.append(metadata.group_label(event.group_id))
        return any(_is_test_group_name(name) for name in group_names)

    def _is_live_passage(self, event: PassageEvent) -> bool:
        timestamp_ms = self._evidence_timestamp(event)
        if timestamp_ms is None:
            return False
        received_at_ms = (
            event.received_at_ms
            if event.received_at_ms > 0
            else int(time.time() * 1000.0)
        )
        return (
            abs(int(timestamp_ms) - int(received_at_ms))
            <= LIVE_EVIDENCE_DATE_TOLERANCE_MS
        )

    def _on_passage_received(self, event: PassageEvent) -> None:
        if self._defer_capture_callback(self._on_passage_received, event):
            return
        self._last_passage_monotonic = time.monotonic()
        if self.timing_provider == "cyclerace":
            if self._workspace_mode == "archive":
                self._archive_background_passage_count += 1
                self._last_passage_monotonic = time.monotonic()
                self._update_runtime_status()
                return
            inbox_metadata = self._receiver_metadata_store.current()
            active_metadata = (
                self.metadata_store.current()
                if self.metadata_store is not None
                else None
            )
            if (
                inbox_metadata is not None
                and inbox_metadata.race_id == event.race_id
                and (
                    active_metadata is None
                    or (active_metadata.race_id != event.race_id
                        and self._live_metadata_scope == (event.race_id, event.stage_id)
                        and not self._other_race_while_recording(event.race_id, event.race_name))
                )
            ):
                self._activate_cyclerace_workspace(inbox_metadata)
                active_metadata = (
                    self.metadata_store.current()
                    if self.metadata_store is not None
                    else None
                )
            if (
                active_metadata is not None
                and (active_metadata.race_id, active_metadata.stage_id) != (event.race_id, event.stage_id)
            ):
                self._capture_error = (
                    f"收到其他赛事：{event.race_name or event.race_id}。"
                    "请在“设置”中打开对应赛事，或等待 CycleRace 同步赛事信息。"
                )
                self._update_runtime_status()
                return
            if active_metadata is None:
                scopes = {(item.race_id, item.stage_id) for item in self.passage_store.events()
                          if (item.race_id, item.stage_id, item.event_id) not in self._preflight_event_keys}
                if scopes and scopes != {(event.race_id, event.stage_id)}:
                    self._capture_error = "收到其他赛事或赛段的通过记录，等待 CycleRace 赛事信息"
                    self._update_runtime_status()
                    return
        if self.timing_provider == "cyclerace" and event.received_at_ms <= 0:
            event = replace(event, received_at_ms=int(time.time() * 1000.0))
        try:
            self.passage_store.append(event)
        except Exception as exc:
            self._capture_error = sanitize_recording_message(exc)
            logger.exception("Failed to store passage in active event workspace")
            self._update_runtime_status()
            return
        self._received_passage_sequence += 1
        event_key = (event.race_id, event.stage_id, event.event_id)
        self._received_event_order[event_key] = self._received_passage_sequence
        formal_timestamp_ms = self._passage_timestamp(event)
        if not event.is_active:
            self._evidence_timestamp_overrides.pop(event.event_id, None)
        elif self.timing_provider == "cyclerace" and formal_timestamp_ms is not None:
            alignment_reference_ms = event.received_at_ms
            aligned_timestamp_ms = _align_live_evidence_timestamp(
                formal_timestamp_ms,
                alignment_reference_ms,
            )
            existing_override = self._evidence_timestamp_overrides.get(event.event_id)
            if aligned_timestamp_ms != formal_timestamp_ms:
                self._evidence_timestamp_overrides[event.event_id] = (
                    formal_timestamp_ms,
                    aligned_timestamp_ms,
                )
            elif (
                existing_override is not None
                and existing_override[0] != formal_timestamp_ms
            ):
                self._evidence_timestamp_overrides.pop(event.event_id, None)
        self._received_passage_count += 1
        self._historical_passage_count = len(self._events_for_current_metadata(self.passage_store.events()))
        self._last_passage_monotonic = time.monotonic()
        self._include_high_speed_event_date(event)
        self._auto_start_recording_for_passage(event)
        self._pending_passages[event.event_id] = event
        if not self._passage_batch_timer.isActive():
            self._passage_batch_timer.start()
        self._update_runtime_status()

    def _flush_passage_batch(self) -> None:
        if self._defer_capture_callback(self._flush_passage_batch):
            return
        pending_events = tuple(self._pending_passages.values())
        self._pending_passages.clear()
        if not pending_events:
            return
        started = time.perf_counter()
        # These events are already durable. Show corrections and withdrawals
        # without waiting for disk scans, pin journals or media publication.
        changed = {event.event_id for event in pending_events}
        for event in pending_events:
            if event.is_active and self._evidence_timestamp(event) is None:
                self._unsupported_event_ids.add(event.event_id)
            else:
                self._unsupported_event_ids.discard(event.event_id)
        for event_id in changed:
            for windows in self._capture_windows_by_camera.values():
                windows.pop(event_id, None)
        self.refresh_events(changed)
        displayed_at_ms = int(time.time() * 1000)
        for event in pending_events:
            if event.received_at_ms > 0:
                self.runtime_metrics.observe(
                    "passage_received_to_visible",
                    max(0, displayed_at_ms - event.received_at_ms),
                    item_count=1,
                )
        self._apply_pending_focus()
        self._request_capture_refresh()
        self._observe_runtime_metric("passage_batch_apply", started, item_count=len(changed))
        self._update_runtime_status()

    def _on_metadata_received(self, metadata: RaceMetadata) -> None:
        if self._defer_capture_callback(self._on_metadata_received, metadata):
            return
        if self._workspace_mode != "archive" and self._other_race_while_recording(metadata.race_id,
                                                                                 metadata.race_name):
            return
        self._live_metadata_scope = (metadata.race_id, metadata.stage_id)
        if self._workspace_mode == "archive":
            self._update_runtime_status()
            return
        current = self.metadata_store.current() if self.metadata_store is not None else None
        if current is None or current.race_id != metadata.race_id:
            # Opening a race in CycleRace is not that race starting here: no folder
            # and no recording until the operator records or its first chip arrives.
            self._workspace_notice = (f"CycleRace 当前赛事：{metadata.race_name or metadata.race_id}。"
                                      "点「开始录像」或收到芯片时才建立本场文件夹。")
            self._update_runtime_status()
            return
        try:
            self._activate_cyclerace_workspace(metadata)
            self._capture_error = ""
        except Exception as exc:
            self._capture_error = sanitize_recording_message(exc)
            logger.exception("Failed to activate CycleRace event workspace")
            self._update_runtime_status()
            return
        pending_focus = self._pending_focus
        if pending_focus is not None and (
            pending_focus.race_id != metadata.race_id
            or pending_focus.stage_id != metadata.stage_id
        ):
            self._pending_focus = None
        self._lookup_cache.clear()
        self._selected_event_id = ""
        registered_event_ids = {
            event_id
            for windows in self._capture_windows_by_camera.values()
            for event_id in windows
        }
        for event_id in registered_event_ids:
            event = self.passage_store.get(event_id)
            if event is None or not event.is_active or (
                event.race_id != metadata.race_id
                or event.stage_id != metadata.stage_id
            ):
                for windows in self._capture_windows_by_camera.values():
                    windows.pop(event_id, None)
                self._unsupported_event_ids.discard(event_id)
        self.refresh()
        self._apply_pending_focus()
        self._request_capture_refresh()
        self._update_runtime_status()

    def _on_racetiger_status(self, status: RaceTigerStatus) -> None:
        self._racetiger_status = status
        if status.state == "error":
            self._receiver_error = status.message
        elif status.state == "ok":
            self._receiver_error = ""
        self._update_runtime_status()

    def _on_focus_received(self, focus: RaceFocus) -> None:
        if self._defer_capture_callback(self._on_focus_received, focus):
            return
        self._pending_focus = focus
        if self._workspace_mode == "archive":
            return
        self._apply_pending_focus()

    def _apply_pending_focus(self) -> bool:
        focus = self._pending_focus
        if focus is None:
            return False
        applied = self.focus_athlete(
            focus.race_id,
            focus.stage_id,
            athlete_id=focus.athlete_id,
            bib=focus.bib,
            group_id=focus.group_id,
        )
        if applied:
            self._update_operator_controls()
        return applied

    def _filmstrip_race_id(self) -> str:
        try:
            return self._current_archive_race_id()
        except ExternalClipImportError:
            return ""

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

    def _invalidate_capture_refresh(self) -> None:
        self._capture_refresh_generation += 1
        self._capture_refresh_worker.invalidate(self._capture_refresh_generation)
        # The serial queue provides a barrier after the cancelled active pass.
        self._run_capture_task(lambda: None)

    def _capture_refresh_request(self, *, cleanup=False) -> CaptureRefreshRequest:
        try:
            race_id = self._current_archive_race_id()
        except ExternalClipImportError:
            race_id = ""
        active_recorders = {recorder for recorder in self._recorders.values() if recorder.is_running}
        archive_jobs = tuple(
            ArchiveRefreshJob(publisher, str(race_id), publisher.recorder in active_recorders)
            for publisher in self._archive_publishers if race_id
        )
        key = (self.review_binding_store, tuple(self._coordinators.items()),
               tuple(self._publishers.items()))
        if key != getattr(self, "_evidence_pipeline_key", None):
            self._evidence_pipeline_key = key
            self._evidence_pipeline = EvidencePipeline(
                self._coordinators, self._publishers, self.review_binding_store,
            )
            self._capture_previews = {}
        if not hasattr(self, "_recording_catalog"):
            self._recording_catalog = RecordingCatalog()
        all_events = self.passage_store.events(include_inactive=True)
        eligible = {event.event_id for event in self._events_for_current_metadata(all_events)}
        passages = tuple(EvidencePassage(
            event.event_id, event.revision, event.race_id, self._evidence_timestamp(event),
            event.is_active, event.event_id in eligible,
        ) for event in all_events)
        return CaptureRefreshRequest(
            generation=self._capture_refresh_generation,
            ring_buffers=tuple(self._ring_buffers.values()), archive_jobs=archive_jobs,
            cleanup=cleanup, current_time_ms=int(time.time() * 1000.0),
            evidence_job=EvidenceRefreshJob(self._evidence_pipeline, passages),
            catalog_job=RecordingCatalogJob(
                self._recording_catalog, self.timeline_store,
                self._camera_one_pane().camera_index, str(race_id), tuple(self._ring_buffers.values()),
            ),
            storage_path=self.output_dir,
            problem_timeline=self.timeline_store,
            problem_race_id=str(race_id),
        )

    def _recording_sources_for_filmstrip(self, pane, race_id):
        if not hasattr(self, "_capture_refresh_worker"):
            return (), 0
        if (isinstance(self.metadata_store, EventWorkspaceMetadataStore)
                and self.metadata_store.current() is None and len(self.metadata_store.candidates()) > 1):
            return (), 0
        context = (str(self.timeline_store.journal_path.absolute()), race_id, pane.camera_index)
        snapshot = getattr(self, "_recording_catalog_snapshot", None)
        key = (context, self.timeline_store.revision)
        if ((snapshot is None or snapshot.context != context or snapshot.revision != key[1]
                or time.monotonic() - getattr(self, "_catalog_refreshed_at", 0.0) >= 2.0)
                and key != getattr(self, "_catalog_requested_key", None)):
            self._catalog_requested_key = key
            QTimer.singleShot(0, self._request_capture_refresh)
        if snapshot is None or snapshot.context != context:
            return (), 1 if self._recording_any_active() else 0
        sources = tuple(replace(source, location=replace(
            source.location, clock_offset_ms=self._continuous_offset_for_location(source.location),
        )) for source in snapshot.sources)
        pending = snapshot.pending
        if self._recording_any_active() and not any(
            source.location.segment.end_reason == "live_filmstrip_tail" for source in sources
        ):
            pending = max(1, pending)
        return sources, pending

    def _request_capture_refresh(self) -> None:
        if getattr(self, "_capture_task_waiting", False):
            return
        now = time.monotonic()
        cleanup = now - self._last_cleanup_at >= 5.0
        if cleanup:
            self._last_cleanup_at = now
        self._capture_refresh_worker.submit(self._capture_refresh_request(cleanup=cleanup))

    def _apply_evidence_snapshot(self, snapshot) -> set[str]:
        changed = set()
        valid = {}
        eligible = {event.event_id for event in self._events_for_current_metadata(
            self.passage_store.events(include_inactive=True)
        )}
        for passage in snapshot.passages:
            current = self.passage_store.get(passage.event_id)
            if (current is not None and current.revision == passage.revision
                    and current.is_active == passage.active
                    and passage.eligible == (passage.event_id in eligible)
                    and self._evidence_timestamp(current) == passage.timestamp_ms):
                valid[passage.event_id] = passage
        previews = dict(getattr(self, "_capture_previews", {}))
        for camera, windows in snapshot.windows:
            previous = self._capture_windows_by_camera.setdefault(camera, {})
            incoming = {window.event_id: window for window in windows if window.event_id in valid}
            for event_id in valid:
                window = incoming.get(event_id)
                if previous.get(event_id) != window:
                    changed.add(event_id)
                if window is None:
                    previous.pop(event_id, None)
                else:
                    previous[event_id] = window
                previews.pop((camera, event_id), None)
        for camera, event_id, preview in snapshot.previews:
            if event_id in valid:
                if self._capture_previews.get((camera, event_id)) != preview:
                    changed.add(event_id)
                previews[camera, event_id] = preview
        self._capture_previews = previews
        self._unsupported_event_ids = {passage.event_id for passage in valid.values()
                                       if passage.active and passage.eligible and passage.timestamp_ms is None}
        return changed

    def _on_capture_refresh_finished(self, result: CaptureRefreshResult) -> None:
        if self._defer_capture_callback(self._on_capture_refresh_finished, result):
            return
        if (
            not isinstance(result, CaptureRefreshResult)
            or result.generation != self._capture_refresh_generation
        ):
            return
        apply_started = time.perf_counter()
        self._catalog_requested_key = None
        if result.storage is not None:
            self._storage_snapshot = result.storage
        if result.catalog is not None:
            self._recording_catalog_snapshot = result.catalog
            self._catalog_refreshed_at = time.monotonic()
        failed = bool(result.error)
        apply_failed = False
        changed_event_ids: set[str] = set()
        if result.apply_state:
            try:
                if result.evidence is not None:
                    changed_event_ids = self._apply_evidence_snapshot(result.evidence)
                if result.archive_segments:
                    changed_event_ids.update(result.archive_affected_events)
                if changed_event_ids:
                    self.refresh_events(changed_event_ids)
                self._poll_recording_health(now=time.monotonic())
            except Exception as exc:
                failed = True
                apply_failed = True
                self._capture_error = sanitize_recording_message(exc)
                logger.exception("Failed to apply background capture refresh")
        if result.error:
            self._capture_error = sanitize_recording_message(result.error)
        elif result.evidence is not None and not apply_failed:
            self._capture_error = ""
        # A newly published HLS segment extends the time-film tail before the
        # five-minute archive is sealed. Refresh only the lightweight source
        # index here; visible thumbnails are still decoded lazily by the panel.
        if result.catalog is not None or (result.apply_state and (result.discovered_segment_count or result.archive_segments)) or result.deleted_paths:
            try:
                self._update_filmstrip()
            except Exception:  # noqa: BLE001 - a preview refresh must not stop capture.
                logger.exception("Failed to refresh live filmstrip tail")
        if result.cleanup_after_apply and not failed:
            self._capture_refresh_worker.submit(
                CaptureRefreshRequest(
                    generation=self._capture_refresh_generation,
                    ring_buffers=tuple(self._ring_buffers.values()),
                    archive_jobs=(),
                    cleanup=True,
                    current_time_ms=int(time.time() * 1000.0),
                    scan=False,
                    apply_state=False,
                )
            )
        self._observe_runtime_metric(
            "capture_refresh_apply",
            apply_started,
            item_count=len(changed_event_ids),
            failed=failed,
        )
        self._update_operator_controls()
        self._update_runtime_status()

    def _refresh_capture_windows(self) -> None:
        """Drain and finish evidence work for explicit lifecycle/test callers."""
        self._invalidate_capture_refresh()
        request = self._capture_refresh_request()
        result = self._run_capture_task(lambda: self._capture_refresh_worker._process(request))
        self._on_capture_refresh_finished(result)

    def _publish_archive_segments(
        self,
        *,
        race_id: str | None = None,
        recording: bool | None = None,
    ):
        publishers = tuple(self._archive_publishers)
        if not publishers:
            return ()
        if race_id is None:
            try:
                race_id = self._current_archive_race_id()
            except ExternalClipImportError:
                return ()
        if recording is None:
            recording = self._recording_any_active()
        active_recorders = {
            recorder
            for recorder in self._recorders.values()
            if recorder.is_running
        }
        published = []
        for publisher in publishers:
            publisher_recording = bool(
                recording and publisher.recorder in active_recorders
            )
            published.extend(
                publisher.publish_completed(
                    race_id=str(race_id),
                    recording=publisher_recording,
                )
            )
        return tuple(published)

    def set_finish_line_roi(
        self,
        camera_index: int,
        roi: tuple[float, float, float, float],
    ) -> None:
        """Set the normalized finish-line band used by video assistance."""
        left, top, right, bottom = (float(value) for value in roi)
        if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
            raise ValueError("终点线区域必须是0到1之间的有效矩形")
        self._finish_line_rois[max(1, int(camera_index))] = (
            left, top, right, bottom
        )
        camera_index = max(1, int(camera_index))
        self._finish_line_store.set_roi(camera_index, (left, top, right, bottom))
        for worker in self._video_scan_workers.values():
            if int(getattr(worker, "camera_index", 0)) == camera_index:
                worker.roi = (left, top, right, bottom)
                worker.request_scan()
        self._update_runtime_status()

    def _on_video_candidates(
        self,
        candidates,
        generation: int | None = None,
        *,
        worker_token: tuple[int, object] | None = None,
    ) -> None:
        """Persist worker results before marshalling them to the Qt main thread."""
        if not self._video_assist_enabled():
            return
        with self._video_scan_state_lock:
            if worker_token is not None:
                camera_index, token = worker_token
                if self._video_scan_tokens.get(int(camera_index)) is not token:
                    return
            delivery_generation = (
                self._video_scan_generation if generation is None else int(generation)
            )
            if delivery_generation != self._video_scan_generation:
                return
            arrival_store = self.video_arrival_store
        values = tuple(candidates)
        persistent = tuple(
            candidate
            for candidate in values
            if not str(getattr(candidate, "candidate_id", "")).startswith("visual:")
            and str(getattr(candidate, "segment_id", "")).strip()
            and str(getattr(candidate, "video_path", "")).strip()
        )
        if persistent:
            try:
                arrival_store.add_many(persistent)
            except Exception as error:  # noqa: BLE001 - report persistence failures to Qt.
                self.video_candidate_persistence_failed.emit(
                    _VideoCandidatePersistenceFailure(
                        delivery_generation,
                        str(error),
                    )
                )
                raise RuntimeError("video candidate persistence failed") from error
        self.video_candidates_received.emit(
            _VideoCandidateDelivery(delivery_generation, values, persisted=True)
        )

    def _on_video_candidate_persistence_failed(self, failure) -> None:
        if isinstance(failure, _VideoCandidatePersistenceFailure):
            with self._video_scan_state_lock:
                if failure.generation != self._video_scan_generation:
                    return
                message = failure.message
        else:
            message = str(failure)
        self._capture_error = str(message)
        self._update_runtime_status()

    def _pause_video_scan_workers(self) -> object:
        """Pause background video scans while a playback window is open."""

        token = object()
        self._video_scan_pause_tokens.add(token)
        if len(self._video_scan_pause_tokens) != 1:
            return token
        workers = (
            *self._video_scan_workers.values(),
            *self._archive_video_scan_workers.values(),
        )
        for worker in workers:
            pause = getattr(worker, "pause", None)
            if callable(pause):
                pause()
        return token

    def _resume_video_scan_workers(self, token: object) -> None:
        if token not in self._video_scan_pause_tokens:
            return
        self._video_scan_pause_tokens.remove(token)
        if self._video_scan_pause_tokens:
            return
        workers = (
            *self._video_scan_workers.values(),
            *self._archive_video_scan_workers.values(),
        )
        for worker in workers:
            resume = getattr(worker, "resume", None)
            if callable(resume):
                resume()

    def _reconcile_video_candidates(self, candidates) -> None:
        if not self._video_assist_enabled():
            return
        already_persisted = False
        if isinstance(candidates, _VideoCandidateDelivery):
            if candidates.generation != self._video_scan_generation:
                return
            already_persisted = candidates.persisted
            candidates = candidates.candidates

        def is_visual(value: object) -> bool:
            return str(getattr(value, "candidate_id", "")).startswith("visual:")

        def overlaps(left: object, right: object) -> bool:
            return (
                int(getattr(left, "camera_index", 0))
                == int(getattr(right, "camera_index", 0))
                and abs(
                    int(getattr(left, "peak_at_ms", 0))
                    - int(getattr(right, "peak_at_ms", 0))
                )
                <= 1_500
            )

        values = tuple(candidates)
        reconcile_started = time.perf_counter()
        self.runtime_metrics.observe(
            "video_candidate_received",
            0.0,
            item_count=len(values),
        )
        persistent = tuple(
            candidate
            for candidate in values
            if not is_visual(candidate)
            and str(getattr(candidate, "segment_id", "")).strip()
            and str(getattr(candidate, "video_path", "")).strip()
        )
        if persistent and not already_persisted:
            try:
                self.video_arrival_store.add_many(persistent)
            except Exception as error:  # noqa: BLE001 - direct callers need the same report.
                self._on_video_candidate_persistence_failed(str(error))

        for candidate in values:
            matching_visual_ids = {
                candidate_id
                for candidate_id, existing in self._video_candidate_cache.items()
                if is_visual(existing) and overlaps(existing, candidate)
            }
            matching_regular_ids = {
                candidate_id
                for candidate_id, existing in self._video_candidate_cache.items()
                if not is_visual(existing) and overlaps(existing, candidate)
            }
            if is_visual(candidate) and matching_regular_ids:
                continue
            for candidate_id in matching_visual_ids:
                self._video_candidate_cache.pop(candidate_id, None)
            self._video_candidate_cache[str(getattr(candidate, "candidate_id", id(candidate)))] = candidate
        # Keep all visual candidates available for fast Ctrl+Left/Right
        # navigation; the reconciliation list below remains anomaly-only.
        self.set_video_navigation_candidates(tuple(self._video_candidate_cache.values()))
        events = self._events_for_current_metadata(self.passage_store.events())
        times = tuple(event.timeline_timestamp_ms for event in events)
        tools = importlib.import_module(
            ".video_passage_det" + "ector", __package__
        )
        try:
            reconciliation = tools.reconcile_candidates(
                tuple(self._video_candidate_cache.values()),
                times,
                passage_time_offset_by_camera=getattr(
                    self,
                    "_clock_offset_by_camera",
                    None,
                ),
            )
        except Exception:
            self._observe_runtime_metric(
                "video_candidate_reconcile",
                reconcile_started,
                item_count=len(values),
                failed=True,
            )
            raise
        self._observe_runtime_metric(
            "video_candidate_reconcile",
            reconcile_started,
            item_count=len(values),
        )
        # Video analysis is advisory. Never replace the operator's current
        # passage from a background callback; explicit candidate actions below
        # are the only path that may change the selected athlete or frame.
        self.video_review_apply_requested.emit(reconciliation)

    def _persist_video_review(self, candidate_id: str, status: str, bib: str) -> None:
        try:
            self._video_review_journal.update(
                candidate_id,
                status=status,
                bib=bib,
            )
        except RuntimeError as error:
            self._capture_error = str(error)
            self._update_runtime_status()

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

    def stop_receiver(self) -> None:
        errors = self._receiver_controller.stop()
        self._receiver = self._receiver_controller.receiver
        self._racetiger_source = self._receiver_controller.racetiger_source
        if errors:
            self._receiver_error = "; ".join(errors)
        self._update_runtime_status()

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
