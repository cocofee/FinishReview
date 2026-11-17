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


logger = logging.getLogger("FinishReview")


class FinishReviewWindow(
    FinishReviewSettingsMixin,
    FinishReviewStatusMixin,
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

    def _configured_recording_sources(self) -> tuple[tuple[int, str], ...]:
        sources = []
        if is_supported_review_source(self.source):
            sources.append((self.camera_index, self.source))
        if is_supported_review_source(self.secondary_source):
            sources.append((self.camera_index + 1, self.secondary_source))
        return tuple(sources)

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

    def _recording_any_active(self) -> bool:
        return any(recorder.is_running for recorder in self._recorders.values())

    def _recording_all_active(self) -> bool:
        configured = self._configured_recording_sources()
        return bool(configured) and len(self._recorders) == len(configured) and all(
            self._recorders.get(camera_index) is not None
            and self._recorders[camera_index].is_running
            for camera_index, _source in configured
        )

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

    def _toggle_recording(self) -> None:
        if self._recording_any_active():
            # Stopped on purpose: no reminder for this race again.  The race the
            # chips name counts too; without race details here the folder's own
            # id is empty and the stop used to be forgotten.
            for race_id in (self._race_being_recorded(), self._last_chip_race_id):
                if race_id:
                    self._auto_recorded_race_ids.add(race_id)
            self.stop_recording()
            return
        if self._workspace_mode == "archive":
            self._return_to_live_event()
            return
        if not is_supported_review_source(self.source):
            self._configure_devices()
            if not is_supported_review_source(self.source):
                return
        try:
            self._enter_race_opened_in_cyclerace()
            self.start_recording()
        except Exception as exc:  # noqa: BLE001 - GUI boundary reports device failures.
            self._runtime_error = sanitize_recording_message(exc)
            QMessageBox.critical(self, "无法开始录像", self._runtime_error)
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

    def _set_problem_list_layout(self, expanded: bool) -> None:
        self._problem_workbench_active = expanded
        self.set_workbench_compact_mode(expanded, allow_multi_camera_maximize=True)
        # Workbench: F belongs to the problem's own picture.  The time rail would
        # otherwise swallow the press for its own judging window (operator: "F does
        # not work"), and the press has to land wherever the hands are.
        self.video_filmstrip.full_race.set_enlarge_key_enabled(not expanded)
        # In the workbench the picture already fills the room; F enlarges nothing.
        for pane in self.evidence_panes:
            pane._maximize_shortcut.setEnabled(not expanded)
            if not expanded and hasattr(pane, "set_passed_count"):
                pane.set_passed_count("")
                if getattr(pane, "_header_strip", None) is not None:
                    strip = pane._header_strip
                    pane.set_header_strip(None)
                    strip.setParent(self.problem_panel)
                    strip.hide()
                    pane.video_view.set_target_badge("")
                    self._slot_pane = None
        self.setMinimumHeight(700 if expanded else 760)
        self._set_compact_header(expanded)
        self.problem_panel.card.setVisible(not expanded)
        self.problem_panel.filmstrip.setVisible(not expanded)
        self.filter_bar.setVisible(not expanded)
        self.table.setVisible(not expanded)
        self.video_discovered_table.setVisible(False)
        self.transport.setVisible(not expanded)
        self.preview_panel.setMinimumHeight(190 if expanded else 420)
        self.video_filmstrip.setMinimumHeight(180 if expanded else 360)
        self.workspace_splitter.setMinimumHeight(300 if expanded else 180)
        self._review_split_manually_sized = expanded
        if expanded:
            self._selected_event_id = ""
            self.problem_panel.splitter.insertWidget(1, self.evidence_splitter)
            self.problem_panel.splitter.setSizes([360, 1000, 0])
            self.review_content_splitter.insertWidget(0, self.workspace_splitter)
            self.review_content_splitter.setSizes([520, 210])
            self.evidence_splitter.show()
            self.auto_advance_checkbox.setChecked(False)
        else:
            self.workspace_splitter.addWidget(self.evidence_splitter)
            self.review_content_splitter.insertWidget(0, self.preview_panel)
            self._set_continuous_review_layout(False)
        self.return_problem_button.setVisible(expanded)
        self.manual_problem_button.setVisible(expanded)
        self.pipeline_label.setVisible(expanded)
        self.crossing_cards.setVisible(False)
        self.recording_rail_button.setVisible(expanded)
        if not getattr(self, "_picture_hint_bound", False):
            self._picture_hint_bound = True
            self.problem_panel.hint.changed.connect(self.picture_hint.set_hint)
        self.picture_hint.set_hint(self.problem_panel.hint.text())
        self.picture_hint.setVisible(expanded)
        self.problem_panel.hint.setVisible(not expanded)
        self._set_recording_rail_visible(not expanded or self.recording_rail_button.isChecked())
        self._problem_count_mode_changed(expanded)
        self._update_operator_controls()

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

    def _init_problem_list(self) -> None:
        from .quick_review_panel import QuickReviewPanel

        self.problem_panel = QuickReviewPanel(self.results_panel)
        self.problem_panel.set_frame_provider(
            lambda: self._frame_for_problem_count(self._active_playback_pane())
        )
        self.problem_panel.confirmation_preparing.connect(self._pause_arrival_confirmation)
        self.problem_panel.candidate_preparing.connect(self._prepare_arrival_candidate)
        self.problem_panel.sampling_preparing.connect(self._prepare_sampling_frame)
        self.problem_panel.sampling_configured.connect(self._refresh_problem_finish_lines)
        self.results_panel.layout().insertWidget(1, self.problem_panel)
        self.problem_panel.hide()
        self.problem_panel.set_events_provider(
            lambda: self.passage_store.events(include_inactive=True)
        )
        self.problem_panel.set_metadata_provider(
            lambda: self.metadata_store.current() if getattr(self, "metadata_store", None) is not None else None
        )
        self.problem_panel.jump_requested.connect(self._open_problem_frame)
        self.problem_panel.current_changed.connect(self._problem_selection_changed)
        self.problem_panel.big_view_changed.connect(self._problem_big_view_changed)
        self.problem_panel.marks_changed.connect(self._problem_marks_changed)
        self.problem_panel.count_mode_changed.connect(self._problem_count_mode_changed)
        self.problem_panel.frame_step_requested.connect(self._step_active_pane)
        self.problem_panel.run_requested.connect(self._run_problem_playback)
        self.problem_panel.run_speed.connect(self._run_problem_speed)
        self.problem_panel.clear_data_requested.connect(self._clear_event_data)
        self.problem_panel.delete_requested.connect(self._delete_problem_record)
        self.problem_panel.calibration_requested.connect(self._calibrate_crossing_frame)
        self.problem_panel.summary_changed.connect(lambda _text: self._refresh_crossing_cards())
        self._normal_crossing_cards = {}
        self._opened_normal_crossing = ""
        # The raw recording is offered once, before any card exists; whether the
        # operator keeps it on screen afterwards is their own choice.
        self._rail_suggested = False
        from .crossing_widgets import CrossingCardRail
        self.crossing_cards = CrossingCardRail(self)
        self.crossing_cards.item_selected.connect(self._select_crossing_card)
        self.preview_panel.layout().addWidget(self.crossing_cards)
        # 过线记录已经是左边那张表，横排卡片是同一批数据的第二面：收起来，
        # 只留下查找原录像；校时在判读工具条最前面。
        self.crossing_cards.list.hide()
        self.crossing_cards.title.hide()
        self.recording_rail_button = QPushButton("查找原录像", self.crossing_cards)
        self.recording_rail_button.setCheckable(True)
        self.recording_rail_button.toggled.connect(self._set_recording_rail_visible)
        # 11-14: it sat alone on a band under the picture; it shares the picture's button line now.
        self.evidence_panes[0].controls_layout.addWidget(self.recording_rail_button)
        self.recording_rail_button.hide()
        # 11-14: the advice sits beside the picture's buttons, not at the foot of the window.
        from .problem_panel import PictureHint
        self.picture_hint = PictureHint(self)
        controls = self.evidence_panes[0].controls_layout
        controls.insertWidget(controls.indexOf(self.evidence_panes[0].time_label) + 1, self.picture_hint, 3)
        self.picture_hint.hide()
        self._creating_manual_crossing = False
        # 校时 lives on the judging toolbar (QuickReviewPanel.calibrate_button), not down here.
        self.group_start_button = QPushButton("回到团首", self)
        self.group_start_button.clicked.connect(self._open_group_start)
        self.evidence_panes[0].controls_layout.addWidget(self.group_start_button)
        self.group_start_button.hide()
        self.return_problem_button = QPushButton("回到当前问题", self)
        self.return_problem_button.clicked.connect(self._return_to_problem)
        self.manual_problem_button = QPushButton("＋ 补记过线", self)
        self.manual_problem_button.clicked.connect(self._add_manual_problem)
        rail = self.video_filmstrip.full_race
        rail.toolbar.insertWidget(3, self.return_problem_button)
        rail.toolbar.insertWidget(4, self.manual_problem_button)
        rail.follow_button.setProperty("alwaysVisible", True)
        rail.follow_button.show()
        rail.navigation_started.connect(self._problem_browse_started)
        rail.follow_button.toggled.connect(lambda enabled: self._problem_browse_started() if enabled else None)
        self.pipeline_label = QLabel("", self.problem_panel.status_widget)
        self.pipeline_label.setStyleSheet("color:#17675b;")
        self.pipeline_label.setWordWrap(True)
        self.pipeline_label.setMinimumWidth(0)
        self.pipeline_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.problem_panel.status_bar.insertWidget(0, self.pipeline_label, 1)
        self.problem_panel.set_workspace(self.output_dir)

    def _problem_selection_changed(self, item):
        self._opened_normal_crossing = ""
        if hasattr(self, "group_start_button"):
            group = item is not None and item.kind in ("bunch_check", "obstruction")
            selected = getattr(self.problem_panel, "_selected_arrival", lambda: None)()
            wave = getattr(self.problem_panel, "_row_wave", lambda _row: None)(selected)
            self.group_start_button.setVisible(group or len(wave or ()) > 1)
        if getattr(self, "_problem_workbench_active", False):
            if getattr(self.problem_panel, "holds_picture", False):
                # The panel places (or keeps) the picture for this row itself.
                self._opened_normal_crossing = ""
                self.video_filmstrip.full_race.follow_button.setChecked(False)
            elif not getattr(self, "_creating_manual_crossing", False):
                self._return_to_problem()
            self._refresh_crossing_cards()
            self._problem_count_mode_changed(True)

        # 表里点到的是一条自动对应的记录：那是在复查这条过线，点画面不算新建待处理。
        # 放在 _return_to_problem 之后，因为回看问题本身会清掉这个标记。
        # 到达记录表不同：点到人就算数，自动对应的单人也一样。
        from .problem_items import KIND_AUTO_PASSED
        if (item is not None and item.kind == KIND_AUTO_PASSED
                and not getattr(self.problem_panel, "quick_review", False)):
            self._opened_normal_crossing = item.item_id

    def _pause_arrival_confirmation(self):
        # Freeze the displayed frame without seeking to a playback clock estimate.
        self._set_sync_playing(False, seek_final=False)
        self._active_playback_pane().set_playing(False)

    def _prepare_arrival_candidate(self):
        """Raw filmstrip review does not need to wait for automatic video analysis."""

        panel = self.problem_panel
        pane = self._active_playback_pane()
        frame = self._frame_for_problem_count(pane)
        if frame is None:
            return
        item = panel.current_item()
        if item is not None:
            if item.evidence.get("configuration_stale") or item.evidence.get("superseded"):
                return
            original = item.evidence.get("frame")
            from .bunch_review import mark_on_frame
            if any(mark_on_frame(mark, frame, frame["recorder_ms"]) for mark in panel.current_marks()):
                return
            if original and self._shows_the_problem_frame(pane, frame["position_ms"], original):
                return
        selected_key = panel._selected_key
        self._pause_arrival_confirmation()
        try:
            self._creating_manual_crossing = True
            location = self._persist_filmstrip_location(pane.location)
            pane._location = location
            frame.update(segment_id=location.segment.segment_id, video_path=str(location.video_path))
            item = panel.add_manual_problem(frame, reason="人工查看录像候选", recorder_ms=frame["recorder_ms"],
                                            thumbnail=pane.video_view._pixmap_item.pixmap())
            if item is not None:
                panel.select_item(item.item_id)
                index = next((i for i, row in enumerate(panel._arrival_rows) if row.key == selected_key), None)
                if index is not None:
                    # Restore only the identity: clicking would reopen its chip
                    # reference and discard the raw frame being classified.
                    panel._selected_key = selected_key
                    panel._active_item = item
                    panel.table.setCurrentCell(index, 0)
                    panel._sync_arrival_confirmation()
        except (OSError, ValueError, RuntimeError) as error:
            panel.hint.setText(f"当前原帧未保存：{error}")
        finally:
            self._creating_manual_crossing = False

    def _refresh_problem_finish_lines(self):
        """Resolve the real line for the displayed camera, session and recording time."""

        if not hasattr(self, "problem_panel"):
            return
        from .analysis_settings import CONFIG_PATH, inherited_profile, normalized_points, profile_scope_matches
        from .analysis_workspace import session_id_for_path

        paths = [self.output_dir / name for name in ("long_image_lines.json", "finish_lines.json", CONFIG_PATH)]
        try:
            signature = tuple((str(path), path.stat().st_mtime_ns, path.stat().st_size)
                              if path.exists() else (str(path),) for path in paths)
            if signature != getattr(self, "_review_line_signature", None):
                lines, settings = {}, {}
                for path in paths:
                    payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
                    if path == paths[-1]:
                        if not isinstance(payload, dict):
                            raise ValueError("分析配置必须是JSON对象")
                        settings = payload
                    records = payload.get("finish_lines", []) if isinstance(payload, dict) else payload
                    for line in records:
                        lines[int(line["camera_index"])] = line
                self._review_line_config = (lines, settings.get("sampling_profiles", []))
                self._review_line_signature = signature
            lines, profiles = self._review_line_config
            scope = self.problem_panel._arrival_scope
            for pane in self.evidence_panes:
                frame = self._frame_for_problem_count(pane)
                line = None
                if frame and pane.source_kind == "regular":
                    session = re.sub(r"_review_\d+$", "", session_id_for_path(pane.location.video_path))
                    scope_known = bool(scope[0] or scope[1])
                    matches = [p for p in profiles if int(p["camera_index"]) == pane.camera_index
                               and p["session_id"] == session
                               and (not scope_known or profile_scope_matches(p, *scope))
                               and int(p["effective_from_ms"]) <= frame["recorder_ms"]]
                    # Like the analysis: a session without its own line shows the one it inherits.
                    line = (max(matches, key=lambda p: (int(p["effective_from_ms"]), int(p["revision"])))
                            if matches else inherited_profile(
                                profiles, pane.camera_index, frame["recorder_ms"], scope[0])
                            or lines.get(pane.camera_index))
                pane.video_view.set_finish_line(normalized_points(line) if line else ())
            active = self._active_playback_pane()
            self.problem_panel.set_finish_line_status(bool(active.video_view._finish_line_points))
        except (OSError, ValueError, KeyError, TypeError):
            for pane in self.evidence_panes:
                pane.video_view.set_finish_line()
            self.problem_panel.set_finish_line_status(False)

    def _problem_big_view_changed(self, big: bool) -> None:
        """Give the enlarged picture the room of the rail, and fit the frame to it.

        A 4:3 crossing only grows with the height it is given, so while one
        problem is judged the time rail steps aside; the frame is re-fitted to the
        new area both ways, the same as the old judging window did when it opened.
        """

        if getattr(self, "_problem_workbench_active", False):
            self.preview_panel.setVisible(not big)
        QTimer.singleShot(0, self._fit_visible_evidence_views)
        self._problem_count_mode_changed(True)
        self._refresh_problem_finish_lines()

    def keyPressEvent(self, event) -> None:
        """The judging keys work from whatever corner of the workbench holds the keys."""

        if (getattr(self, "_problem_workbench_active", False)
                and self.problem_panel._queue_key(event)):
            event.accept()
            return
        super().keyPressEvent(event)

    def _problem_pane(self):
        """The pane the operator judges on: the problem's own camera, else the regular one."""

        item = self.problem_panel.current_item()
        if item is None:
            return self.regular_pane
        camera = int(item.evidence.get("frame", {}).get("camera_index")
                     or item.evidence.get("camera_index") or 1)
        return self._camera_one_pane() if camera <= 1 else self._pane_for_camera(camera)

    def _problem_count_mode_changed(self, _counting: bool) -> None:
        """Every decoded regular-camera picture can record a crossing, large or small."""

        active = getattr(self, "_problem_workbench_active", False)
        quick_review = bool(getattr(self.problem_panel, "quick_review", False))
        for pane in self.evidence_panes:
            pane.set_identity_marker_enabled(not (active and quick_review))
            pane.video_view.set_count_mode(active
                                           and (not quick_review or pane is self._active_playback_pane())
                                           and pane.source_kind == "regular"
                                           and pane.location is not None and pane._current_frame_index >= 0)

    def _frame_for_problem_count(self, pane):
        if (pane is None or pane.location is None or pane._current_frame_index < 0
                or not pane.video_view.has_frame):
            return None
        origin = pane.location.segment.media_started_at_ms
        if origin is None:
            return None
        return {"camera_index": pane.camera_index, "segment_id": pane.location.segment.segment_id,
                "video_path": str(pane.location.video_path), "frame_index": pane._current_frame_index,
                "position_ms": pane._current_position_ms,
                "recorder_ms": int(origin) + int(pane._current_position_ms)}

    def _prepare_sampling_frame(self):
        for pane in self.evidence_panes:
            try:
                locations = self._live_locations_for_filmstrip(pane)
                location = locations[-1] if locations else pane.location
                if location is not None:
                    saved = self._persist_filmstrip_location(location)
                    if location is pane.location:
                        pane._location = saved
            except (OSError, ValueError, RuntimeError) as error:
                self.problem_panel.hint.setText(f"实时画面暂存失败，终点线仍可手工设置：{error}")

    def _problem_count_cancelled(self, x, y, pane):
        if (getattr(self, "_problem_workbench_active", False)
                and getattr(self.problem_panel, "quick_review", False)
                and pane is self._active_playback_pane()):
            self.problem_panel.cancel_picture_mark(x, y)

    def _problem_wheel_step(self, delta: int, pane) -> None:
        """The wheel over the counting picture: hold whatever was playing, one frame on or back."""

        if not getattr(self, "_problem_workbench_active", False):
            return
        stop = getattr(self.problem_panel, "stop_run", None)
        if stop is not None:
            stop()
        self._step_pane(pane, int(delta))

    def _problem_count_clicked(self, x_normalized: float, y_normalized: float, pane=None) -> None:
        """A click records the displayed frame; an unreported crossing starts its own item."""

        if not getattr(self, "_problem_workbench_active", False):
            return
        pane = pane or self._problem_pane()
        frame = self._frame_for_problem_count(pane)
        if frame is None:
            return
        if getattr(self.problem_panel, "quick_review", False):
            self._quick_review_mark_arrival(frame, x_normalized, y_normalized, pane)
            return
        item = self.problem_panel.current_item()
        context = self.problem_panel._inline_context()
        normal = self._normal_crossing_cards.get(self._opened_normal_crossing)
        if normal is not None and self._shows_the_problem_frame(pane, frame["position_ms"], normal.evidence["frame"]):
            self.problem_panel.hint.setText("此单人过线已自动对应；发现其他人时，可点「＋ 补记过线」另记原帧。")
            return
        item_frame = (item.evidence.get("frame") or {}) if item is not None else {}
        if context is not None and pane.camera_index != context.camera:
            self.problem_panel.hint.setText(f"本团按机位 {context.camera} 点数；其他机位可回看，避免同一人重复计数。")
            return
        own = bool(item and item_frame and (
            (int(item_frame.get("camera_index", 1)) == pane.camera_index
             and self._shows_the_problem_frame(pane, frame["position_ms"], item_frame))
            or (context is not None and pane.camera_index == context.camera
                and context.start_ms <= frame["recorder_ms"] <= context.end_ms)))
        settled = item is not None and self.problem_panel.state(item).startswith(("已处理", "待 CycleRace"))
        if settled and own:
            self.problem_panel.hint.setText("此过线已处理；如需修改，请先重新处理。")
            return
        if not own or item.status in ("waiting", "excluded", "info", "closed_upstream"):
            try:
                self._creating_manual_crossing = True
                location = self._persist_filmstrip_location(pane.location)
                pane.location = location
                frame["segment_id"], frame["video_path"] = location.segment.segment_id, str(location.video_path)
                item = self.problem_panel.add_manual_problem(
                    frame, recorder_ms=frame["recorder_ms"], thumbnail=pane.video_view._pixmap_item.pixmap())
            except (OSError, ValueError, RuntimeError) as error:
                self.problem_panel.hint.setText(f"过线未保存：{error}")
                return
            finally:
                self._creating_manual_crossing = False
        if item is None:
            return
        self.problem_panel.set_browsing(False)
        self.problem_panel.add_mark(x_normalized, y_normalized, frame["recorder_ms"], frame)
        self._problem_count_mode_changed(True)
        self._refresh_crossing_cards()
        self.problem_panel.setFocus()

    def _run_problem_playback(self, playing) -> None:
        """The arrival list plays the recording on to the next person, or holds it: what Space does."""

        pane = self._active_playback_pane()
        if pane is not None and bool(playing) != bool(pane.is_playing):
            self._toggle_pane(pane)

    def _run_problem_speed(self, speed) -> None:
        """A run through a stretch the strip shows empty plays fast; a pause ends that."""

        pane = self._active_playback_pane()
        if pane is not None:
            pane.set_play_speed(speed)

    def _toggle_active_pane(self) -> None:
        """Space.  On the arrival list's wave it plays on to the next person and stops (11-12).

        古浪 big bunch: Space played the recording straight on and everyone went
        past before one could be clicked.  A run playing is held; plain playing
        is paused; with no wave on screen Space plays as it always did.
        """

        panel = getattr(self, "problem_panel", None)
        if getattr(self, "_problem_workbench_active", False) and getattr(panel, "quick_review", False):
            pane = self._active_playback_pane()
            if pane is not None and pane.is_playing:
                if panel.stop_run():
                    return
            elif pane is not None and panel.play_on():
                return
        PassageReviewSurface._toggle_active_pane(self)

    def _quick_review_mark_arrival(self, frame, x_normalized, y_normalized, pane) -> None:
        """Use the operator's picture click as the bicycle cue, not a final ruling."""

        panel = self.problem_panel
        row = panel._selected_arrival()
        item, refusal = panel.picture_candidate(frame)
        if refusal:
            panel.hint.setText(refusal)
            return
        frame = dict(frame, marker_x_normalized=float(x_normalized),
                     marker_y_normalized=float(y_normalized))
        selected_key = row.key if row is not None and row.event is not None else None
        reuse = item is not None
        self._pause_arrival_confirmation()
        try:
            location = self._persist_filmstrip_location(pane.location)
            pane._location = location
            frame.update(segment_id=location.segment.segment_id, video_path=str(location.video_path))
            self._creating_manual_crossing = True
            if not reuse:
                item = panel.add_manual_problem(
                    frame, reason="人工点选自行车与终点线位置，待确认芯片",
                    recorder_ms=frame["recorder_ms"], thumbnail=pane.video_view._pixmap_item.pixmap(),
                )
                panel.adopt_picture_candidate(item, selected_key)
        except (OSError, ValueError, RuntimeError) as error:
            panel.hint.setText(f"标记未保存：{error}")
            return
        finally:
            self._creating_manual_crossing = False
        if item is None:
            return
        self._creating_manual_crossing = True
        try:
            if item.item_id in panel._candidate_wave:
                panel.open_picture(item)  # the click's own wave; the frame stays where it is
            else:
                panel.select_item(item.item_id)
        finally:
            self._creating_manual_crossing = False
        from .bunch_review import mark_on_frame
        existing = next((i for i, mark in enumerate(panel.current_marks())
                         if mark_on_frame(mark, frame, frame["recorder_ms"])
                         and (mark["x"] - x_normalized) ** 2 + (mark["y"] - y_normalized) ** 2 < .02 ** 2), None)
        builds = panel._table_builds
        if existing is None:
            panel.add_mark(x_normalized, y_normalized, frame["recorder_ms"], frame)
        else:
            panel._editing_mark = existing
        index = next((i for i, entry in enumerate(panel._arrival_rows) if entry.key == selected_key), None)
        wave = panel._wave_plan() is not None
        if wave:
            # On a wave's picture the row of whoever was clicked is selected.
            panel.show_counted(panel._editing_mark)
        elif index is not None:
            panel._selected_key = selected_key
            panel._active_item = item
            panel.table.setCurrentCell(index, 0)
            panel._sync_arrival_confirmation()
        if not (wave and panel._table_builds != builds):
            # Saving the wave's count filled the table already, and the row of
            # whoever was clicked is selected: a second fill only cost the
            # operator a fifth of a second a click (古浪, 11-14).
            panel._quick_render_signature = None
            panel._render()
        self._problem_count_mode_changed(True)
        self._problem_marks_changed(())
        panel.hint.setText(
            panel.picture_mark_hint()
            or (f"已标记 {row.bib} 的压线位置。"
                if selected_key else "已圈出压线位置；请在左边选对应的芯片记录。")
        )

    def _problem_marks_changed(self, _marks) -> None:
        """Redraw the counted people on the frame that is being judged."""

        pane = self._problem_pane()
        if pane is not None:
            pane.video_view.set_history_markers(self._continuous_marker_overlays(pane))
            self._show_problem_count_badge(pane)

    def _show_problem_count_badge(self, pane) -> None:
        """已过 N 人 · X 号 above the picture, read against the timing software."""
        text = ""
        if (getattr(self, "_problem_workbench_active", False) and pane is self._problem_pane()
                and getattr(self.problem_panel, "quick_review", False)
                and hasattr(self.problem_panel, "passed_count_text")):
            text = self.problem_panel.passed_count_text(self._frame_for_problem_count(pane))
            if hasattr(self.problem_panel, "refresh_slots") and hasattr(pane, "set_header_strip"):
                # The cells of who is counted take the place of 已过 N 人 · X 号 (11-14).
                holder = getattr(self, "_slot_pane", None)
                if holder is not None and holder is not pane:
                    holder.set_header_strip(None)  # another camera's picture had them: its own header is back
                self._slot_pane = pane
                pane.set_header_strip(self.problem_panel.wave_slots)
                self.problem_panel.refresh_slots(text.split(" · ")[0])
                if hasattr(pane.video_view, "set_target_badge"):
                    _key, words, kind = self.problem_panel.current_target()
                    pane.video_view.set_target_badge(words, kind)
        if hasattr(pane, "set_passed_count"):
            pane.set_passed_count(text)

    def _continuous_marker_overlays(self, pane):
        """The counted people ride along with the crossing markers of the same frame."""

        if (getattr(self, "_problem_workbench_active", False)
                and getattr(self.problem_panel, "quick_review", False)):
            return self._problem_count_overlays(pane)
        overlays = list(super()._continuous_marker_overlays(pane))
        overlays.extend(self._problem_count_overlays(pane))
        return tuple(overlays)

    def _problem_count_overlays(self, pane):
        """Numbered marks for the people counted on the problem being judged."""

        if not getattr(self, "_problem_workbench_active", False) or pane is not self._problem_pane():
            return ()
        frame = self._frame_for_problem_count(pane)
        if frame is None:
            return ()
        if hasattr(self.problem_panel, "picture_overlays"):
            return tuple(self.problem_panel.picture_overlays(frame))
        from .bunch_review import is_excluded, mark_on_frame
        from .crossing_order import mark_order_labels, ordered_mark_indices
        marks = self.problem_panel.current_marks()
        labels = mark_order_labels(marks)
        dot = bool(getattr(self.problem_panel, "quick_review", False))
        # A person beyond the wave's chips, or without a chip, is drawn in red.
        warning = getattr(self.problem_panel, "picture_mark_warning", lambda _mark: False)
        overlays = []
        for index in ordered_mark_indices(marks):
            mark = marks[index]
            if not mark_on_frame(mark, frame, frame["recorder_ms"]):
                continue
            if dot:
                label = self.problem_panel.picture_mark_label(mark)
                reason = mark.get("exclusion_reason", "").strip() if is_excluded(mark) else ""
                label += f" 已排除：{reason}" if reason and reason != label else ""
            else:
                label = (labels[index] + (f" {mark['bib']}" if mark.get("bib") else "")
                         + (f" 已排除：{mark['exclusion_reason']}" if is_excluded(mark) else ""))
            alert = bool(warning(mark))
            overlays.append((float(mark["x"]), float(mark["y"]), label, not alert, alert, dot))
        return tuple(overlays)

    def _problem_browse_started(self):
        if getattr(self, "_problem_workbench_active", False):
            self.problem_panel.set_browsing(True)

    def _return_to_problem(self):
        self._opened_normal_crossing = ""
        item = self.problem_panel.current_item()
        self.video_filmstrip.full_race.follow_button.setChecked(False)
        if item is not None and item.evidence.get("frame"):
            self.problem_panel.set_browsing(True)
            self._open_problem_frame(dict(item.evidence["frame"]))
        elif item is not None:
            self._set_sync_playing(False, seek_final=False)
            if self._open_chip_reference(item):
                return
            for pane in self.evidence_panes:
                pane.clear_passage("该芯片时刻暂无可回看录像；请检查录像覆盖或从下方胶卷查找")
            self.problem_panel.set_browsing(False)
        else:
            self.problem_panel.set_browsing(False)
            if not any(pane.video_view.has_frame for pane in self.evidence_panes):
                recent = [entry for entry in (self.problem_panel.payload or {}).get("auto_passed", []) if entry.get("frame")]
                if recent:
                    latest = max(recent, key=lambda entry: entry.get("recorder_ms", entry.get("chip_time_ms", 0)))
                    self._open_problem_frame(dict(latest["frame"]))
                else:
                    for pane in self.evidence_panes:
                        pane.clear_passage(self.problem_panel.queue_text() + "；可从原录像打开画面校时或补记")

    def _open_chip_reference(self, item) -> bool:
        """Raw playback must not depend on detection, line setup or calibration."""
        events = [self.passage_store.get(event_id) for event_id in item.event_ids]
        event = next((event for event in events if event is not None and event.is_active), None)
        if event is None:
            reference = item.evidence.get("reference_frame")
            if reference:
                return self._open_problem_frame(dict(reference, reference_only=True))
            return False
        timestamp = self._evidence_timestamp(event)
        if timestamp is None:
            return False
        timestamp = align_evidence_timestamp(timestamp, event.received_at_ms)
        camera = int(item.evidence.get("camera_index") or self._camera_one_pane().camera_index)
        # A chip reference uses the same session calibration as normal playback.
        # Rebuild it on every open: a cached reference predates later calibration.
        offsets = {self._clock_offset_for_camera(camera)}
        for segment in self.timeline_store.segments():
            if segment.camera_index == camera and segment.clock_source == DEFAULT_CLOCK_SOURCE:
                key = self._recording_session_key_from_path(
                    segment.source_id, self.timeline_store.resolve_video_path(segment))
                offsets.add(self._calibrated_clock_offset(camera, key))
        locations = []
        for offset in offsets:
            lookup = self.timeline_store.locate_passage(
                timestamp, clock_offset_ms=offset, race_id=event.race_id,
                pre_roll_ms=0, prefer_continuous_media=True,
            )
            locations.extend(location for location in lookup.locations
                             if location.status == "located" and location.segment.camera_index == camera
                             and location.segment.clock_source == DEFAULT_CLOCK_SOURCE
                             and self._continuous_offset_for_location(location) == offset)
        if not locations:
            return False
        # Immutable short clips are preferred, but a sealed archive can cover
        # a missing clip without waiting for any analysis cache to catch up.
        location = min(locations, key=lambda value: (
            value.video_path.suffix.lower() == ".m3u8",
            value.segment.media_duration_ms or 0,
        ))
        return self._open_problem_frame({
            "camera_index": camera, "segment_id": location.segment.segment_id,
            "video_path": str(location.video_path), "position_ms": location.passage_position_ms,
            "reference_only": True,
            "date_aligned": timestamp != event.timeline_timestamp_ms,
        })

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

    def _shows_the_problem_frame(self, pane, position_ms: int, frame: dict) -> bool:
        """Whether what is on the screen is this problem's own frame.

        A two-minute archive counts as the same moment as the clip it covers, so the
        comparison follows the recording clock whenever both sides know it, and falls
        back to the file only when they do not: judging a rebound archive is still
        judging this crossing, and the panel must not call that "browsing".
        """

        location = getattr(pane, "location", None)
        if location is None:
            return False
        source = self.timeline_store.get_segment(str(frame.get("segment_id", "")))
        on_screen = location.segment.media_started_at_ms
        wanted = getattr(source, "media_started_at_ms", None)
        if on_screen is None or wanted is None:
            same = location.segment.segment_id == str(frame.get("segment_id", ""))
            return same and abs(int(position_ms) - int(frame.get("position_ms", 0))) <= PROBLEM_FRAME_TOLERANCE_MS
        return abs((on_screen + int(position_ms))
                   - (wanted + int(frame.get("position_ms", 0)))) <= PROBLEM_FRAME_TOLERANCE_MS

    def _open_race_filmstrip_frame(self, frame):
        if getattr(self, "_problem_workbench_active", False):
            self.problem_panel.set_browsing(True)
        return super()._open_race_filmstrip_frame(frame)

    def _add_manual_problem(self):
        self._opened_normal_crossing = ""
        pane = self._active_playback_pane()
        frame = self._frame_for_problem_count(pane)
        if frame is None:
            self.problem_panel.hint.setText("请先从原录像打开一张可用画面，再点选手补记过线。")
            return
        try:
            self._creating_manual_crossing = True
            location = self._persist_filmstrip_location(pane.location)
            pane.location = location
            frame["segment_id"], frame["video_path"] = location.segment.segment_id, str(location.video_path)
            self.problem_panel.add_manual_problem(
                frame, recorder_ms=frame["recorder_ms"], thumbnail=pane.video_view._pixmap_item.pixmap())
            self.problem_panel.hint.setText("过线原帧已记住；点选手计数，号码可以稍后再填。")
        except (OSError, ValueError, RuntimeError) as error:
            self.problem_panel.hint.setText(f"过线未保存：{error}")
        finally:
            self._creating_manual_crossing = False
        self._problem_count_mode_changed(True)

    def _open_group_start(self):
        if getattr(self.problem_panel, "open_wave_start", lambda: False)():
            return
        context = self.problem_panel._inline_context()
        if context is None:
            return
        segment = next((source for source in context.segments
                        if source.start_ms <= context.start_ms <= source.end_ms), None)
        if segment is None:
            self.problem_panel.hint.setText("团首录像存在缺口，请检查证据。")
            return
        position = context.start_ms - segment.start_ms
        self._open_problem_frame({"camera_index": context.camera, "segment_id": segment.segment.segment_id,
                                  "video_path": str(segment.path), "position_ms": position})

    def _calibrate_crossing_frame(self):
        pane = self._active_playback_pane()
        frame = self._frame_for_problem_count(pane)
        if frame is None:
            self.problem_panel.hint.setText("先在左表选这位选手的芯片记录，把画面停在他压线的那一帧，再点「先校时」。")
            self.recording_rail_button.setChecked(True)
            return
        from .analysis_workspace import AnalysisWorkspace
        metadata = self._current_metadata()
        try:
            workspace = AnalysisWorkspace(self.output_dir,
                                          race_id=metadata.race_id if metadata else "",
                                          stage_id=metadata.stage_id if metadata else "")
        except (OSError, ValueError) as error:
            self.problem_panel.hint.setText(f"等待本场有效芯片记录：{error}")
            return
        eligible = {read.event_id for read in workspace.reads
                    if read.active_now and not read.excluded and not read.manual_entry
                    and read.bib and read.chip_ms == read.latest_chip_ms}
        events = [event for event in self.passage_store.events() if event.event_id in eligible
                  and event.is_active and event.bib and event.timeline_timestamp_ms > 0
                  and (event.race_id, event.stage_id) == (workspace.race_id, workspace.stage_id)]
        events.sort(key=lambda event: event.timeline_timestamp_ms)
        if not events:
            self.problem_panel.hint.setText("等待本场有效芯片记录；画面可以先补记过线。")
            return
        labels = [f"{event.bib} · {datetime.fromtimestamp(event.timeline_timestamp_ms / 1000).strftime('%H:%M:%S.%f')[:-3]} · {event.event_id}"
                  for event in events]
        selected = self.problem_panel._selected_arrival()
        default = next((i for i, candidate in enumerate(events)
                        if selected and selected.event and candidate.event_id == selected.event.event_id), 0)
        label, ok = QInputDialog.getItem(self, "以当前帧校时", "当前画面压线的是哪位选手？", labels, default, False)
        if not ok or label not in labels:
            return
        event = events[labels.index(label)]
        current = self.passage_store.get(event.event_id)
        if current != event or not current.is_active:
            self.problem_panel.hint.setText("所选芯片记录已变化，请重新选择压线选手校时。")
            return
        try:
            location = self._persist_filmstrip_location(pane.location)
            frame["segment_id"], frame["video_path"] = location.segment.segment_id, str(location.video_path)
            session_key = self._recording_session_key(location)
            offset = frame["recorder_ms"] - int(event.timeline_timestamp_ms)
            self.calibration_store.record(camera_index=pane.camera_index, session_key=session_key,
                                          offset_ms=offset, anchor_event_id=event.event_id, anchor_bib=event.bib,
                                          calibrated_at_ms=int(time.time() * 1000),
                                          anchor_event_revision=event.revision,
                                          anchor_chip_time_ms=event.timeline_timestamp_ms,
                                          anchor_race_id=event.race_id, anchor_stage_id=event.stage_id,
                                          anchor_frame=frame)
            self._continuous_clock_offsets[(pane.camera_index, session_key)] = offset
            self._continuous_calibration_revision += 1
            self._lookup_cache.clear()
        except (OSError, ValueError, RuntimeError) as error:
            self.problem_panel.hint.setText(f"校时未保存：{error}")
            return
        self.problem_panel._last_live_signature = None
        self.problem_panel._next_analysis = 0
        if not self.problem_panel._live_paused and self.problem_panel._process is None:
            self.problem_panel._start_analysis()
        self.problem_panel.hint.setText(f"已按 {event.bib} 建立初始校时 {offset:+d} ms；后续独立过线继续检查漂移。")

    def _set_recording_rail_visible(self, visible):
        workbench = getattr(self, "_problem_workbench_active", False)
        self.video_filmstrip.setVisible(bool(visible) or not workbench)
        self.crossing_cards.setVisible(False)
        # Without the recording rail the bottom holds two buttons only: give the
        # picture the height instead of leaving an empty band.
        compact = workbench and not visible
        self.preview_panel.setMinimumHeight(0 if compact else 190 if workbench else 420)
        self.preview_panel.setMaximumHeight(0 if compact else 16777215)
        self.preview_panel.setVisible(not compact)
    def _refresh_crossing_cards(self):
        if not hasattr(self, "crossing_cards"):
            return
        panel = self.problem_panel
        from .problem_items import KIND_AUTO_PASSED
        # 自动对应的正常单人已经是左边表里的一行（「全部」按时间排），不再另造一份卡片。
        normal = {item.item_id: item for item in panel.items if item.kind == KIND_AUTO_PASSED}
        self._normal_crossing_cards = normal
        if self._opened_normal_crossing and self._opened_normal_crossing not in normal:
            self._opened_normal_crossing = ""
        if not self._rail_suggested:
            # Before any candidate is available the raw recording stays reachable:
            # the first calibration and a crossing the analysis never reported.
            self._rail_suggested = True
            if not panel.rows and not normal:
                self.recording_rail_button.setChecked(True)
        self._set_recording_rail_visible(self.recording_rail_button.isChecked())

    def _select_crossing_card(self, item_id):
        if getattr(self.problem_panel, "quick_review", False):
            self.problem_panel.select_item(item_id)
            return
        normal = self._normal_crossing_cards.get(item_id)
        if normal is not None:
            self._opened_normal_crossing = item_id
            self._open_problem_frame(dict(normal.evidence["frame"]))
            self.problem_panel.set_browsing(True)
            self.problem_panel.hint.setText(f"{normal.bibs[0]} 已自动对应，正在复查原帧。")
            self.crossing_cards.set_selected(item_id)
            return
        self._opened_normal_crossing = ""
        for row, item in enumerate(self.problem_panel.rows):
            if item.item_id == item_id:
                self.problem_panel.table.setCurrentCell(row, 0)
                self._return_to_problem()
                break

    def _open_problem_list(self) -> None:
        expanded = self.problem_list_button.isChecked()
        self.problem_panel.setVisible(expanded)
        self._set_problem_list_layout(expanded)
        self._refresh_problem_list()
        if expanded:
            self._return_to_problem()

    def _delete_problem_record(self, event_id: str) -> None:
        event = self.passage_store.get(event_id)
        if event is not None:
            self._delete_record(event)
            self.problem_panel.refresh()

    def _open_passages(self):
        self.problem_list_button.setChecked(False)
        self._open_problem_list()
        self._refresh_filtered_view()

    def _refresh_problem_list(self) -> None:
        panel = getattr(self, "problem_panel", None)
        if panel is None:
            return
        panel.set_workspace(self.output_dir)
        metadata = self._current_metadata()
        panel.set_scope(metadata.race_id if metadata else "", metadata.stage_id if metadata else "")
        if panel.isVisible():
            panel.refresh()
        result = (panel.payload or {}).get("cameras", {}).get(str(self._camera_one_pane().camera_index), {})
        ranges = [(start, min(end, result.get("analyzed_through_ms", end)))
                  for start, end, _session in result.get("coverage", []) if not result.get("error")]
        current = panel.current_item()
        frame = current.evidence.get("frame", {}) if current else {}
        segment = self.timeline_store.get_segment(frame.get("segment_id", ""))
        timestamp = (segment.media_started_at_ms + int(frame.get("position_ms", 0))) if (
            segment and segment.media_started_at_ms is not None and segment.camera_index == self._camera_one_pane().camera_index
        ) else None
        self.video_filmstrip.full_race.set_analysis_progress(ranges, timestamp, enabled=self._problem_workbench_active)

    def _open_problem_frame(self, frame) -> bool:
        """Open the item's original frame in its camera's pane, without changing identity."""

        from .video_timeline import PassageVideoLocation

        segment = self.timeline_store.get_segment(str(frame.get("segment_id", "")))
        # Follows the same file into this race folder if the folder was moved (11-05).
        path = relocated_video_path(str(frame.get("video_path", "")), self.output_dir)
        if segment is None or not path.is_file():
            self.problem_panel.hint.setText("原录像缺失或不在本赛事的录像索引中，未跳转其他录像。")
            return False
        position = int(frame.get("position_ms", 0))
        index = None if frame.get("frame_index") is None else int(frame["frame_index"])
        camera = int(frame.get("camera_index", 1) or 1)
        location = PassageVideoLocation(segment, path, position, position, 0,
                                        segment.timing_error_ms, "located")
        location, position, index = self._problem_frame_source(location, position, index)
        self._set_sync_playing(False, seek_final=False)
        if location.segment.media_started_at_ms is not None:
            self.video_filmstrip.full_race.browse_to(location.segment.media_started_at_ms + position)
        opened = self._seek_retained_camera_frame(location, position, index, camera)
        if opened:
            # The pane is on the problem's own frame now, so the panel is not
            # browsing elsewhere and may be decided on: F would otherwise leave the
            # operator looking at the right picture while every button stays greyed.
            reference = bool(frame.get("reference_only"))
            self.problem_panel.set_browsing(reference)
            target = f"第 {index + 1} 帧" if index is not None else f"片内 {position / 1000:.2f} 秒"
            self._active_playback_pane().set_reference_only(reference)
            if frame.get("auto_pair"):
                self.problem_panel.hint.setText("已定位自动对应的过线原帧；灰蓝虚线圈为系统对应（鼠标移到圈上看号码），不算人工点数或裁判确认。")
            elif reference:
                date_note = "；芯片日期与接收日期不同，按接收日期查找，正式时间未改" if frame.get("date_aligned") else ""
                self.problem_panel.hint.setText(
                    f"参考录像 · {target}：尚未校时/匹配，不代表已确认过线{date_note}。请逐帧找到压线帧，左键点人。"
                )
            else:
                self.problem_panel.hint.setText(f"已定位原录像{target}，请逐帧核对后处理。")
        # The problem's pane is the one that takes the counting clicks from here on.
        self._problem_count_mode_changed(True)
        self._problem_marks_changed(None)
        return opened

    def _archive_on_piece_clock(self) -> bool:
        return bool(getattr(self, "_problem_workbench_active", False))

    def _problem_frame_source(self, location, position, index):
        """Where a row's frame is shown: (location, position, frame index).

        A two-second buffer piece plays two seconds and stops, and a lead-in
        before it (2 s for a crossing without a chip) falls in the piece
        before (10-29: "2 秒割裂").  Once the five-minute archive of that moment
        is sealed the frame opens there, on the very picture and on the pieces'
        clock (see archive_alignment), so the rings saved on pieces stay on
        their people; an archive whose picture is not found is not used.
        Before that, a moment ahead of the piece opens in the piece holding it.
        """

        from .race_filmstrip import recording_sources

        segment = location.segment
        if segment.media_started_at_ms is None:
            return location, max(0, position), index
        moment = int(segment.media_started_at_ms) + int(position)
        if self._is_archive(location):
            return self._aligned_archive(location, moment), position, index
        session = self._recording_session_key(location)
        sources, _pending = recording_sources(
            self.timeline_store, int(segment.camera_index), self._filmstrip_race_id(),
            path_available=lambda _path, _live: True)  # only the files used are checked
        holding = [source for source in sources
                   if source.start_ms <= moment < source.end_ms
                   and self._recording_session_key(source.location) == session
                   and Path(source.location.video_path).is_file()]
        archive = next((source.location for source in holding if self._is_archive(source.location)), None)
        if archive is not None:
            duration = max(1, int(segment.media_duration_ms or 1))
            shown = int(segment.media_started_at_ms) + min(max(0, int(position)), duration - 1)
            aligned = self._aligned_archive(archive, moment, (location, shown))
            if aligned is not archive:
                return aligned, max(0, moment - int(aligned.segment.media_started_at_ms)), None
        if position >= 0:
            return location, position, index
        piece = next((source for source in holding if source.priority == 1), None)
        if piece is not None:
            return piece.location, moment - piece.start_ms, None
        return location, 0, index

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

    def _preflight_evidence_status(
        self,
        event: PassageEvent,
    ) -> tuple[bool, bool, str, str]:
        try:
            lookup = self._cached_lookup(event)
            high_speed = source_location(lookup, high_speed=True)
        except Exception as error:  # noqa: BLE001 - keep the test status visible.
            detail = sanitize_recording_message(error)
            return False, False, detail, detail
        required_camera_indexes = {
            camera_index
            for camera_index, _source in self._configured_recording_sources()
        }
        ready_camera_indexes = {
            location.segment.camera_index
            for location in lookup.locations
            if location.segment.clock_source == DEFAULT_CLOCK_SOURCE
            and location.status in {"located", "near_boundary", "unverified"}
        }
        regular_ready = bool(required_camera_indexes) and required_camera_indexes.issubset(
            ready_camera_indexes
        )
        high_speed_ready = high_speed is not None
        regular_detail = (
            "普通录像全部机位已覆盖测试时间点"
            if regular_ready
            else "等待普通录像覆盖："
            + "、".join(
                f"机位{camera_index}"
                for camera_index in sorted(
                    required_camera_indexes - ready_camera_indexes
                )
            )
        )
        high_speed_detail = (
            "高速画面已找到并可打开"
            if high_speed_ready
            else (
                self._high_speed_scan_result.message
                or "等待Auyat完成判读和保存"
            )
        )
        return regular_ready, high_speed_ready, regular_detail, high_speed_detail

    def _start_preflight_recording(self) -> bool:
        if self._recording_all_active():
            return True
        if not is_supported_review_source(self.source):
            return False
        try:
            self.start_recording()
        except Exception as error:  # noqa: BLE001 - dialog shows the runtime error.
            self._runtime_error = sanitize_recording_message(error)
            self._update_runtime_status()
            return False
        return self._recording_all_active()

    def _record_preflight_event(self, run: PreflightRun) -> None:
        if not run.passed:
            return
        try:
            self._preflight_journal.append(
                run,
                recorded_at_ms=int(time.time() * 1000.0),
            )
        except OSError as error:
            self._capture_error = sanitize_recording_message(error)
            self._update_runtime_status()
            return
        event_key = (run.race_id, run.stage_id, run.event_id)
        is_new = event_key not in self._preflight_event_keys
        self._preflight_event_keys.add(event_key)
        if is_new:
            self.refresh()

    def _restore_latest_preflight_event(self) -> tuple[bool, str]:
        entry = self._preflight_journal.latest_entry()
        if entry is None:
            return False, "当前没有被隔离的联调记录。"
        event_key = (
            str(entry.get("race_id") or "").strip(),
            str(entry.get("stage_id") or "").strip(),
            str(entry.get("event_id") or "").strip(),
        )
        self._preflight_journal.restore(
            event_key,
            recorded_at_ms=int(time.time() * 1000.0),
        )
        self._preflight_event_keys.discard(event_key)
        self.refresh()
        identity = str(entry.get("bib") or event_key[2]).strip()
        return True, f"{identity} 已恢复到正式复核列表；原始计时记录未被修改。"

    def start_recording(self) -> None:
        if self._workspace_mode == "archive":
            raise RecordingError("历史赛事模式不能开始录像，请先返回当前赛事")
        if self._recording_all_active():
            return
        self._invalidate_capture_refresh()
        self._stop_archive_video_scan_workers()
        if self._recorders:
            self.stop_recording()
        configured_sources = self._configured_recording_sources()
        if not configured_sources:
            raise RecordingError("请先在设备设置中选择录像摄像头")
        self._camera_reconnect_attempts.clear()
        self._camera_reconnect_not_before.clear()
        self._camera_reconnect_errors.clear()
        self._camera_auth_failed_sources.clear()
        self._stop_requested = False
        try:
            free_bytes = self._run_capture_task(lambda: shutil.disk_usage(self.output_dir).free)
        except OSError as exc:
            raise RecordingError(f"无法检查赛事存储空间: {exc}") from exc
        self._storage_snapshot = (self.output_dir, free_bytes / (1024**3), "")
        if free_bytes < 1024**3:
            raise RecordingError("赛事存储空间不足 1 GB，无法开始录像")
        self._recording_lock.acquire(self.output_dir)
        archive_publishers = []
        recording_started_ms = int(time.time() * 1000)
        try:
            pipelines = self._run_capture_task(lambda: self._recording_controller.start(
                sources=configured_sources,
                output_dir=self.output_dir,
                ffmpeg_path=self.ffmpeg_path,
                review_retention_seconds=self.review_retention_seconds,
                timeline_store=self.timeline_store,
                timing_error_ms=self.timing_error_ms,
                binding_store=self.review_binding_store,
            ))
            self._recorders = self._recording_controller.recorders
            self._ring_buffers = self._recording_controller.ring_buffers
            self._coordinators = self._recording_controller.coordinators
            self._publishers = self._recording_controller.timeline_publishers
            archive_publishers = [pipeline.archive_publisher for pipeline in pipelines]
            recorders = self._recorders
            ring_buffers = self._ring_buffers
            coordinators = self._coordinators
            publishers = self._publishers
            self._recorder = recorders.get(self.camera_index)
            self._ring_buffer = ring_buffers.get(self.camera_index)
            self._coordinator = coordinators.get(self.camera_index)
            self._publisher = publishers.get(self.camera_index)
            self._video_scan_workers = {}
            if self._video_assist_enabled():
                for camera_index, ring_buffer in ring_buffers.items():
                    generation = self._video_scan_generation
                    token = object()
                    self._video_scan_tokens[camera_index] = token
                    callback = (
                        lambda candidates, generation=generation,
                        camera_index=camera_index, token=token: (
                            self._on_video_candidates(
                                candidates,
                                generation,
                                worker_token=(camera_index, token),
                            )
                        )
                    )
                    if len(configured_sources) == 1:
                        worker = ring_buffer.create_live_review_batch_scan_worker(
                            callback,
                            width=480,
                            height=270,
                            ffmpeg_path=self.ffmpeg_path or "ffmpeg",
                            sample_fps=3.0,
                            roi=self._finish_line_rois.get(
                                camera_index, (0.35, 0.15, 0.65, 0.95)
                            ),
                            interval_seconds=2.0,
                            batch_ms=120_000,
                            overlap_ms=2_000,
                            finish_line=self._finish_line_store.get(camera_index),
                        )
                    else:
                        worker = ring_buffer.create_video_passage_scan_worker(
                            callback,
                            width=640,
                            height=360,
                            ffmpeg_path=self.ffmpeg_path or "ffmpeg",
                            sample_fps=8.0,
                            roi=self._finish_line_rois.get(
                                camera_index, (0.35, 0.15, 0.65, 0.95)
                            ),
                            interval_seconds=2.0,
                            finish_line=self._finish_line_store.get(camera_index),
                        )
                    self._video_scan_workers[camera_index] = worker
                    worker.start()
            self._start_visual_workers()
            self._capture_windows_by_camera = {
                camera_index: {} for camera_index in coordinators
            }
            self._capture_windows = self._capture_windows_by_camera.setdefault(
                self.camera_index,
                {},
            )
            self._archive_publishers.extend(archive_publishers)
            self._started = True
            self._stop_requested = False
            self._recording_started_at = time.monotonic()
            self._camera_segment_progress.clear()
            self._camera_segment_progress.update({
                camera_index: (None, self._recording_started_at)
                for camera_index in recorders
            })
            self._runtime_error = ""
            self._auto_recording_error = ""
            self._capture_error = ""
            self._workspace_notice = ""
            self._prepare_live_recording_view(recording_started_ms, "本次录像开始；有新过线时在这里出现。")
            self._refresh_timer.start()
            self._request_capture_refresh()
            self._lookup_cache.clear()
            self.refresh()
        except Exception:
            rollback_failures = self._run_capture_task(self._recording_controller.stop)
            workers = tuple(self._video_scan_workers.values())
            self._run_capture_task(lambda: [worker.stop() for worker in workers])
            self._video_scan_workers = {}
            self._stop_visual_workers()
            self._recorders = self._recording_controller.recorders
            self._ring_buffers = self._recording_controller.ring_buffers
            self._coordinators = self._recording_controller.coordinators
            self._publishers = self._recording_controller.timeline_publishers
            self._recorder = self._recorders.get(self.camera_index)
            self._ring_buffer = self._ring_buffers.get(self.camera_index)
            self._coordinator = self._coordinators.get(self.camera_index)
            self._publisher = self._publishers.get(self.camera_index)
            retained_publishers = self._recording_controller.archive_publishers
            for archive_publisher in retained_publishers:
                if archive_publisher not in self._archive_publishers:
                    self._archive_publishers.append(archive_publisher)
            if any(failure.still_running for failure in rollback_failures):
                self._runtime_error = "录像启动失败，且部分录像进程仍在运行"
            else:
                self._recording_lock.release()
            for archive_publisher in archive_publishers:
                if archive_publisher in self._archive_publishers:
                    self._archive_publishers.remove(archive_publisher)
            raise
        finally:
            self._update_runtime_status()

    def _restart_recording_camera(self, camera_index: int, source: str) -> None:
        self._invalidate_capture_refresh()
        camera_index = max(1, int(camera_index))
        token = object()
        with self._video_scan_state_lock:
            self._video_scan_tokens[camera_index] = token
        previous_worker = self._video_scan_workers.pop(camera_index, None)
        previous_windows = dict(self._capture_windows_by_camera.get(camera_index, {}))
        replacement_windows = {
            event_id: window for event_id, window in previous_windows.items()
            if window.state is not PassageReviewState.WAITING
        }
        pending_passages = []
        for event_id, window in previous_windows.items():
            if window.state is not PassageReviewState.WAITING:
                continue
            event = self.passage_store.get(event_id)
            timestamp_ms = self._evidence_timestamp(event) if event is not None else None
            if event is not None and event.is_active and timestamp_ms is not None:
                pending_passages.append(PendingRecordingPassage(
                    event_id, timestamp_ms, event.revision, event.race_id,
                ))
        restart_kwargs = dict(
            camera_index=camera_index, source=str(source), output_dir=self.output_dir,
            ffmpeg_path=self.ffmpeg_path, review_retention_seconds=self.review_retention_seconds,
            timeline_store=self.timeline_store, timing_error_ms=self.timing_error_ms,
            binding_store=self.review_binding_store, pending_passages=tuple(pending_passages),
        )

        def restart():
            if previous_worker is not None:
                previous_worker.stop()
            return self._recording_controller.restart_camera(**restart_kwargs)

        try:
            pipeline, waiting_windows = self._run_capture_task(restart)
        finally:
            self._recorders = self._recording_controller.recorders
            self._ring_buffers = self._recording_controller.ring_buffers
            self._coordinators = self._recording_controller.coordinators
            self._publishers = self._recording_controller.timeline_publishers
            self._recorder = self._recorders.get(self.camera_index)
            self._ring_buffer = self._ring_buffers.get(self.camera_index)
            self._coordinator = self._coordinators.get(self.camera_index)
            self._publisher = self._publishers.get(self.camera_index)
        replacement_windows.update(waiting_windows)
        self._capture_windows_by_camera[camera_index] = replacement_windows
        if camera_index == self.camera_index:
            self._capture_windows = replacement_windows
        self._camera_segment_progress[camera_index] = (None, time.monotonic())
        self._archive_publishers.append(pipeline.archive_publisher)
        self._lookup_cache.clear()
        ring_buffer = pipeline.ring_buffer
        scan_worker = None
        try:
            if self._video_assist_enabled():
                callback = (
                    lambda candidates, generation=self._video_scan_generation,
                    camera_index=camera_index, token=token: self._on_video_candidates(
                        candidates,
                        generation,
                        worker_token=(camera_index, token),
                    )
                )
                if len(self._configured_recording_sources()) == 1:
                    scan_worker = ring_buffer.create_live_review_batch_scan_worker(
                        callback,
                        width=480,
                        height=270,
                        ffmpeg_path=self.ffmpeg_path or "ffmpeg",
                        sample_fps=3.0,
                        roi=self._finish_line_rois.get(
                            camera_index, (0.35, 0.15, 0.65, 0.95)
                        ),
                        interval_seconds=2.0,
                        batch_ms=120_000,
                        overlap_ms=2_000,
                        finish_line=self._finish_line_store.get(camera_index),
                    )
                else:
                    scan_worker = ring_buffer.create_video_passage_scan_worker(
                        callback,
                        width=640,
                        height=360,
                        ffmpeg_path=self.ffmpeg_path or "ffmpeg",
                        sample_fps=8.0,
                        roi=self._finish_line_rois.get(
                            camera_index, (0.35, 0.15, 0.65, 0.95)
                        ),
                        interval_seconds=2.0,
                        finish_line=self._finish_line_store.get(camera_index),
                    )
                scan_worker.start()
                if self._video_scan_pause_tokens:
                    pause = getattr(scan_worker, "pause", None)
                    if callable(pause):
                        pause()
        except Exception:
            if scan_worker is not None:
                self._run_capture_task(scan_worker.stop)
            raise
        if scan_worker is not None:
            self._video_scan_workers[camera_index] = scan_worker

    def _live_location_for_filmstrip(
        self,
        pane,
        anchor_time_ms: int,
    ) -> PassageVideoLocation | None:
        """Return the immutable live TS containing ``anchor_time_ms``."""

        ring_buffers = getattr(self, "_ring_buffers", {})
        ring_buffer = ring_buffers.get(int(getattr(pane, "camera_index", 0)))
        if ring_buffer is None:
            return None
        locations = self._live_locations_for_filmstrip(pane)
        if not locations:
            return None
        anchor_time_ms = int(anchor_time_ms)
        selected = next((location for location in locations
                         if location.segment.started_at_ms <= anchor_time_ms
                         < location.segment.ended_at_ms), None)
        # Never bridge a real gap or substitute the newest frame for an
        # unavailable requested time. The full-race rail indexes every TS.
        if selected is None:
            return None
        origin = int(selected.segment.media_started_at_ms or selected.segment.started_at_ms)
        position = max(0, min(
            int(selected.segment.media_duration_ms or 1) - 1,
            anchor_time_ms - origin,
        ))
        return replace(
            selected,
            passage_position_ms=position,
            playback_position_ms=max(0, position - self.pre_roll_ms),
        )

    def _live_locations_for_filmstrip(self, pane) -> tuple[PassageVideoLocation, ...]:
        """Build stable sources from the closed TS files in the rolling buffer."""

        ring_buffer = getattr(self, "_ring_buffers", {}).get(
            int(getattr(pane, "camera_index", 0))
        )
        if ring_buffer is None:
            return ()
        event = self.passage_store.get(self._selected_event_id)
        metadata = self._current_metadata()
        race_id = metadata.race_id if metadata is not None else event.race_id if event is not None else ""
        locations = []
        for item in ring_buffer.filmstrip_segments():
            path = ring_buffer.resolve_path(item)
            try:
                available = path.is_file() and path.stat().st_size > 0
            except OSError:
                available = False
            if not available:
                continue
            duration_ms = int(item.duration_ms)
            segment = RecordingSegment(
                segment_id=f"live-filmstrip-{ring_buffer.camera_index}-{item.segment_id}",
                source_id=ring_buffer.source_id,
                camera_index=ring_buffer.camera_index,
                video_path=str(path),
                started_at_ms=int(item.started_at_ms),
                ended_at_ms=int(item.ended_at_ms),
                media_duration_ms=duration_ms,
                media_started_at_ms=int(item.started_at_ms),
                clock_source=DEFAULT_CLOCK_SOURCE,
                timing_error_ms=DEFAULT_TIMING_ERROR_MS,
                end_reason="live_filmstrip_tail",
                race_id=race_id,
            )
            locations.append(PassageVideoLocation(
                segment=segment,
                video_path=path,
                passage_position_ms=0,
                playback_position_ms=0,
                clock_offset_ms=0,
                timing_error_ms=DEFAULT_TIMING_ERROR_MS,
                status="unverified",
            ))
        return tuple(locations)

    def _persist_filmstrip_location(
        self, location: PassageVideoLocation,
    ) -> PassageVideoLocation:
        if location is None or location.segment.end_reason != "live_filmstrip_tail":
            return location
        ring_buffer = getattr(self, "_ring_buffers", {}).get(
            int(location.segment.camera_index)
        )
        if ring_buffer is None:
            raise ValueError("实时缓存已结束，请刷新胶卷后打开归档录像")
        segment = ring_buffer.retain_filmstrip_segment(
            location.segment, self.timeline_store
        )
        return replace(
            location,
            segment=segment,
            video_path=self.timeline_store.resolve_video_path(segment),
            status="located",
        )

    def _recording_stall_error(
        self,
        camera_index: int,
        *,
        now: float,
    ) -> str:
        ring_buffer = self._ring_buffers.get(int(camera_index))
        if ring_buffer is None:
            return ""
        return self._recording_recovery.stall_error(
            int(camera_index), ring_buffer.segment_revision, now=now,
        )

    def _poll_recording_health(self, *, now: float | None = None) -> None:
        if getattr(self, "_capture_task_waiting", False):
            return
        if self._stop_requested or not self._started:
            return
        current_time = time.monotonic() if now is None else float(now)
        for camera_index, recorder in tuple(self._recorders.items()):
            recorder_error = recorder.check_error()
            if not recorder_error and camera_index in self._camera_reconnect_errors:
                recorder_error = self._camera_reconnect_errors[camera_index]
            if not recorder_error and recorder.is_running:
                recorder_error = self._recording_stall_error(
                    camera_index,
                    now=current_time,
                )
            if recorder_error:
                self._recover_failed_camera(
                    camera_index,
                    recorder_error,
                    now=current_time,
                )

    def _on_clock_tick(self) -> None:
        self._refresh_problem_list()
        panel = self.problem_panel
        recording = self._recording_any_active()
        panel.live_tick(recording)
        live_cameras = (panel.payload or {}).get("cameras", {})
        through = [value.get("analyzed_through_ms") for value in live_cameras.values() if value.get("analyzed_through_ms")]
        analyzed = datetime.fromtimestamp(min(through) / 1000).strftime("%H:%M:%S") if through else "等待录像 / 校时"
        analysis_state = panel.analysis_status.replace("\n", " ")
        # Only routine states may disappear. Failure/paused/configuration states
        # take priority even if their longer explanation also contains “等待”.
        actionable = any(word in analysis_state for word in ("失败", "错误", "暂停", "无法", "未完成", "跳过", "终点线"))
        routine = any(word in analysis_state for word in ("等待", "空闲", "后台分析中", "后台分析：", "本轮分析完成"))
        analysis_note = (analysis_state.split("；")[0].split("，")[0].split("：")[0]
                         if actionable or not routine else "")
        short_ends = [segment.media_started_at_ms + segment.media_duration_ms for segment in self.timeline_store.segments()
                      if segment.end_reason == "retained_problem_segment" and segment.media_started_at_ms is not None
                      and segment.media_duration_ms is not None and (not panel._scope[0] or segment.race_id == panel._scope[0])]
        sealed = datetime.fromtimestamp(max(short_ends) / 1000).strftime("%H:%M:%S") if short_ends else "等待首段"
        seconds = max(0, int(time.monotonic() - self._recording_started_at)) if recording else 0
        duration = f"{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}"
        backlog_seconds = max(0, (max(short_ends) - min(through)) // 1000) if short_ends and through else 0
        chip_note = (panel.chip_status.split("（")[0].split("：")[0]
                     if any(word in panel.chip_status for word in ("发现", "失败", "错误")) else "")
        # 11-14: "证据缺口 602 条待证据" hung there all race and told the operator
        # nothing to do.  People nobody can check on any recording are still said.
        gap = panel.evidence_gap_text()
        notes = [f"● 录像 {duration}" if recording else "", chip_note, analysis_note,
                 f"分析落后 {backlog_seconds} 秒" if backlog_seconds else "",
                 gap.split(" · ", 1)[1] if " · " in gap else ""]
        self.pipeline_label.setText(" ｜ ".join(note for note in notes if note))
        self.pipeline_label.setToolTip(
            f"封存到 {sealed} · 分析到 {analyzed} · 积压 {backlog_seconds} 秒\n"
            + panel.chip_status + "\n" + panel.analysis_status + "\n" + panel.health_text()
            + "\n" + panel.summary.toolTip()
        )
        panel.quick_toolbar.setToolTip(self.pipeline_label.toolTip())
        self.problem_panel.refresh_status_visibility()
        self._poll_recording_health()
        self._update_runtime_status()

    def _recover_failed_camera(
        self,
        camera_index: int,
        recorder_error: str,
        *,
        now: float,
    ) -> bool:
        camera_index = max(1, int(camera_index))
        if not self._recording_recovery.can_retry(camera_index, now=now):
            return False
        source = dict(self._configured_recording_sources()).get(camera_index)
        if not source:
            return False
        if _is_rtsp_auth_error(recorder_error):
            detail = (
                _format_rtsp_probe_error(recorder_error)
                + " 自动重试已暂停；修改凭据后，点击开始录像重试。"
            )
            self._recording_recovery.pause_auth(camera_index, source, detail)
            self._runtime_error = f"机位{camera_index}：{detail}"
            self._auto_recording_error = self._runtime_error
            logger.warning("Camera %s authentication failed; automatic retries paused", camera_index)
            return False
        reconnect_started = time.perf_counter()
        try:
            self._restart_recording_camera(camera_index, source)
        except Exception as exc:  # noqa: BLE001 - reconnect remains operator-visible.
            self._observe_runtime_metric(
                "camera_reconnect",
                reconnect_started,
                item_count=1,
                failed=True,
            )
            attempt, delay_seconds = self._recording_recovery.failed(camera_index, now=now)
            detail = sanitize_recording_message(exc)
            self._camera_reconnect_errors[camera_index] = (
                f"{recorder_error}；重连失败：{detail}；"
                f"{delay_seconds:g}秒后重试"
            )
            self._runtime_error = (
                f"机位{camera_index}断开，自动重连中："
                f"{self._camera_reconnect_errors[camera_index]}"
            )
            logger.warning(
                "Camera %s reconnect attempt %s failed; retrying in %.1fs: %s",
                camera_index,
                attempt,
                delay_seconds,
                detail,
            )
            return False

        self._recording_recovery.succeeded(camera_index)
        self._observe_runtime_metric(
            "camera_reconnect",
            reconnect_started,
            item_count=1,
        )
        if self._runtime_error.startswith(f"机位{camera_index}"):
            self._runtime_error = ""
        logger.warning("Camera %s recording automatically reconnected", camera_index)
        return True

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

    def _camera_auth_retry_blocked(self) -> bool:
        self._poll_recording_health()
        return any(
            self._camera_auth_failed_sources.get(camera_index) == source
            for camera_index, source in self._configured_recording_sources()
        )

    def _auto_start_recording_for_passage(self, event: PassageEvent) -> bool:
        """A chip read while nothing records: say so in red, start nothing.

        11-14: the first chip of a race used to start the recording.  Before a
        race the chips are tested again and again, and every test left a
        recording and a folder behind ("它就会自动连接录像…非常麻烦").  The
        reminder beside 开始录像 stays for a race that really was forgotten; a
        click on it puts it away for this race, as stopping a recording does.
        """
        if event.is_active and not self._is_test_passage(event):
            self._last_chip_race_id = event.race_id
        if (
            self._workspace_mode != "live"
            or not event.is_active
            or event.race_id in self._auto_recorded_race_ids
            or self._camera_auth_retry_blocked()
            or self._recording_any_active()
            or self._is_test_passage(event)
            or not self._is_live_passage(event)
        ):
            return False
        if self._recording_reminder_race != event.race_id:
            self._recording_reminder_race = event.race_id
            logger.info("Chip read %s while not recording: reminder shown", event.event_id)
        self._sync_recording_reminder()
        return False

    def _sync_recording_reminder(self) -> None:
        label = getattr(self, "recording_reminder", None)
        if label is None:
            return
        race = self._recording_reminder_race
        if race and (self._recording_any_active() or race in self._auto_recorded_race_ids
                     or self._workspace_mode != "live"):
            race = self._recording_reminder_race = ""
        label.setVisible(bool(race))

    def _dismiss_recording_reminder(self) -> None:
        if self._recording_reminder_race:
            self._auto_recorded_race_ids.add(self._recording_reminder_race)
        self._sync_recording_reminder()

    def _race_being_recorded(self) -> str:
        current = self._current_metadata()
        if current is not None:
            return current.race_id
        return self._live_metadata_scope[0] if self._live_metadata_scope else ""

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

    def _other_race_while_recording(self, race_id: str, race_name: str = "") -> bool:
        """A recording is never cut over to another race by itself; the operator decides.

        10-28: CycleRace's test races (接力测试赛, 78 of them) reached the live
        console and switched it every 3-4 s for 35 minutes, restarting the
        finish recording each time and leaving 120 folders behind.
        """
        current = self.metadata_store.current() if self.metadata_store is not None else None
        if current is None or current.race_id == race_id or not self._recording_any_active():
            return False
        self._workspace_notice = (f"收到其他赛事：{race_name or race_id}。正在录像，未切换；"
                                  "要切换请先停止录像，再在“设置”中打开该赛事。")
        if race_id not in self._held_race_ids:
            self._held_race_ids.add(race_id)
            logger.info("Live recording kept on %s; other race %s not switched to", current.race_id, race_id)
        self._update_runtime_status()
        return True

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

    def stop_recording(self) -> tuple[RecordingStopFailure, ...]:
        self._stop_requested = True
        self._started = False
        self._invalidate_capture_refresh()
        with self._video_scan_state_lock:
            self._video_scan_generation += 1
            self._video_scan_tokens.clear()
        workers = tuple(self._video_scan_workers.values())
        self._video_scan_workers = {}
        self._stop_visual_workers()
        had_recorders = bool(self._recorders)

        def stop_pipeline():
            for worker in workers:
                worker.stop()
            return self._recording_controller.stop()

        failures = self._run_capture_task(stop_pipeline)
        if had_recorders:
            try:
                self._refresh_capture_windows()
            except Exception:
                logger.exception("Failed to publish final review segments")
            if failures:
                self._runtime_error = "; ".join(
                    f"机位{failure.camera_index}: "
                    f"{sanitize_recording_message(failure.error)}"
                    for failure in failures
                )
        self._recorders = self._recording_controller.recorders
        self._ring_buffers = self._recording_controller.ring_buffers
        self._coordinators = self._recording_controller.coordinators
        self._publishers = self._recording_controller.timeline_publishers
        still_running = any(
            failure.still_running for failure in failures
        )
        if still_running:
            # Keep every authoritative reference while a retryable recorder is
            # alive; never hand its archive publisher to a sealed-file scanner.
            self._recorder = self._recorders.get(self.camera_index)
            self._ring_buffer = self._ring_buffers.get(self.camera_index)
            self._coordinator = self._coordinators.get(self.camera_index)
            self._publisher = self._publishers.get(self.camera_index)
        else:
            # Another window may record into this folder once nothing here does.
            self._recording_lock.release()
            self._recorder = None
            self._ring_buffer = None
            self._coordinator = None
            self._publisher = None
            self._recording_started_at = 0.0
            self._camera_reconnect_attempts.clear()
            self._camera_reconnect_not_before.clear()
            self._camera_reconnect_errors.clear()
            self._camera_segment_progress.clear()
        self._update_runtime_status()
        return failures

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
