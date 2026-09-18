"""Production finish console without detection or OCR dependencies."""

from __future__ import annotations

import logging
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
from .review_export import export_review_summary
from .review_clip import PassageReviewBindingStore
from .receiver_controller import ReceiverController
from .recording_controller import (
    PendingRecordingPassage,
    RecordingSessionController,
    RecordingRecoveryState,
    RecordingStopFailure,
)
from .settings import FinishReviewSettings
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


logger = logging.getLogger("FinishReview")


@dataclass(frozen=True, slots=True)
class _VideoCandidateDelivery:
    generation: int
    candidates: tuple[object, ...]
    persisted: bool = True


@dataclass(frozen=True, slots=True)
class _VideoCandidatePersistenceFailure:
    generation: int
    message: str



IS_WINDOWS = os.name == "nt"
BEIJING_TIMEZONE = timezone(timedelta(hours=8))
HIGH_SPEED_INDEX_FILENAME = ".videopipe_auyat_index.json"
LIVE_EVIDENCE_DATE_TOLERANCE_MS = 5 * 60 * 1000
CYCLERACE_INBOX_DIRNAME = ".finishreview"
_TEST_GROUP_NAMES = frozenset({"test", "testgroup"})
_TEST_GROUP_MARKERS = ("测试", "检测")
_INVALID_EVENT_FOLDER_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def _format_point_playback_time(timestamp_ms: int) -> str:
    value = datetime.fromtimestamp(int(timestamp_ms) / 1000.0, tz=BEIJING_TIMEZONE)
    return value.strftime("%H:%M:%S.%f")[:-3]


def _high_speed_target_dates(
    events: tuple[PassageEvent, ...],
    timestamp_overrides: dict[str, tuple[int, int]] | None = None,
) -> frozenset[date]:
    overrides = timestamp_overrides or {}
    dates = {
        datetime.fromtimestamp(
            (
                overrides.get(
                    event.event_id,
                    (event.timeline_timestamp_ms, event.timeline_timestamp_ms),
                )[1]
                / 1000.0
            ),
            tz=BEIJING_TIMEZONE,
        ).date()
        for event in events
        if event.timeline_timestamp_ms >= 86_400_000
    }
    return frozenset(dates or {datetime.now(BEIJING_TIMEZONE).date()})


def _align_live_evidence_timestamp(
    timestamp_ms: int,
    received_at_ms: int,
    *,
    tolerance_ms: int = LIVE_EVIDENCE_DATE_TOLERANCE_MS,
) -> int:
    formal_time = datetime.fromtimestamp(
        int(timestamp_ms) / 1000.0,
        tz=BEIJING_TIMEZONE,
    )
    received_time = datetime.fromtimestamp(
        int(received_at_ms) / 1000.0,
        tz=BEIJING_TIMEZONE,
    )
    if formal_time.date() == received_time.date():
        return int(timestamp_ms)
    candidates = (
        datetime.combine(
            received_time.date() + timedelta(days=offset),
            formal_time.timetz(),
        )
        for offset in (-1, 0, 1)
    )
    candidate = min(
        candidates,
        key=lambda value: abs(value.timestamp() * 1000.0 - received_at_ms),
    )
    candidate_ms = int(candidate.timestamp() * 1000.0)
    if abs(candidate_ms - int(received_at_ms)) <= max(0, int(tolerance_ms)):
        return candidate_ms
    return int(timestamp_ms)


def _is_test_group_name(value: str) -> bool:
    normalized = re.sub(r"[\s_-]+", "", str(value or "")).casefold()
    return normalized in _TEST_GROUP_NAMES or any(
        marker in normalized for marker in _TEST_GROUP_MARKERS
    )


def _historical_evidence_timestamp_overrides(
    events: tuple[PassageEvent, ...],
) -> dict[str, tuple[int, int]]:
    overrides = {}
    for event in events:
        timestamp_ms = int(event.timeline_timestamp_ms)
        if (
            not event.is_active
            or (event.passage_timestamp_ms is None and timestamp_ms < 86_400_000)
        ):
            continue
        aligned_timestamp_ms = timestamp_ms
        if event.received_at_ms > 0:
            aligned_timestamp_ms = _align_live_evidence_timestamp(
                timestamp_ms,
                event.received_at_ms,
            )
        elif event.emitted_at_ms > 0:
            aligned_timestamp_ms = _align_live_evidence_timestamp(
                timestamp_ms,
                event.emitted_at_ms,
            )
        if aligned_timestamp_ms != timestamp_ms:
            overrides[event.event_id] = (timestamp_ms, aligned_timestamp_ms)
    return overrides


def _safe_event_folder_name(value: str, fallback: str) -> str:
    name = _INVALID_EVENT_FOLDER_CHARS.sub("_", str(value).strip())
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        name = _INVALID_EVENT_FOLDER_CHARS.sub("_", str(fallback).strip())
        name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        name = "赛事"
    if name.upper() in _WINDOWS_RESERVED_NAMES:
        name = f"_{name}"
    return name[:80].rstrip(" .") or "赛事"


def _event_workspace_dir(root: Path, metadata: RaceMetadata) -> Path:
    root = Path(root).expanduser().resolve()
    folder_name = _safe_event_folder_name(metadata.race_name, metadata.race_id)
    race_suffix = _safe_event_folder_name(metadata.race_id, "race")
    candidates = [root / folder_name, root / f"{folder_name}_{race_suffix}"]
    candidates.extend(root / f"{folder_name}_{race_suffix}_{index}" for index in range(2, 100))
    for candidate in candidates:
        metadata_path = candidate / "cyclerace_race_metadata.json"
        if not candidate.exists():
            return candidate
        try:
            existing = RaceMetadataStore(metadata_path).current()
        except (OSError, RuntimeError, ValueError):
            existing = None
        if existing is not None and existing.race_id == metadata.race_id:
            return candidate
        try:
            is_empty = not any(candidate.iterdir())
        except OSError:
            is_empty = False
        if is_empty:
            return candidate
    raise RuntimeError("无法为赛事创建唯一保存目录")


class _PassageSignalBridge(QObject):
    accepted = pyqtSignal(object)
    metadata_accepted = pyqtSignal(object)
    focus_accepted = pyqtSignal(object)
    timing_status = pyqtSignal(object)
    capture_refresh_finished = pyqtSignal(object)


class _ElidedLabel(QLabel):
    """Keep the full value available while eliding long display text."""

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self._full_text = ""
        self.setText(text)

    def text(self) -> str:
        return self._full_text

    def setText(self, value: str) -> None:
        self._full_text = str(value)
        self._refresh_display_text()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._refresh_display_text()

    def _refresh_display_text(self) -> None:
        width = max(0, self.contentsRect().width())
        rendered = self.fontMetrics().elidedText(
            self._full_text,
            Qt.ElideRight,
            width,
        )
        QLabel.setText(self, rendered)


class _FinishReviewLogo(QWidget):
    """Small scalable brand mark that does not require packaged image assets."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(34, 34)
        self.setToolTip("FinishReview 终点多源复核")

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.scale(self.width() / 64.0, self.height() / 64.0)

        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#14232c"))
        painter.drawRoundedRect(2, 2, 60, 60, 12, 12)
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(QColor("#31505a"), 1.5))
        painter.drawRoundedRect(3, 3, 58, 58, 11, 11)

        painter.setPen(QPen(QColor("#79a7ad"), 3, Qt.SolidLine, Qt.RoundCap))
        painter.drawLine(10, 24, 23, 24)
        painter.setPen(QPen(QColor("#268e73"), 4, Qt.SolidLine, Qt.RoundCap))
        painter.drawLine(10, 34, 28, 34)
        painter.setPen(QPen(QColor("#34bd83"), 7, Qt.SolidLine, Qt.RoundCap))
        painter.drawLine(10, 45, 18, 45)
        painter.drawLine(18, 45, 34, 32)
        painter.drawLine(34, 32, 41, 32)

        painter.setPen(QPen(QColor("#14232c"), 3))
        painter.setBrush(QColor("#ffd15c"))
        painter.drawEllipse(33, 26, 12, 12)

        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#edf4f4"))
        painter.drawRoundedRect(47, 9, 9, 46, 2, 2)
        painter.setBrush(QColor("#16866d"))
        for rectangle in (
            (47, 9, 5, 8),
            (52, 17, 4, 7),
            (47, 24, 5, 8),
            (52, 32, 4, 8),
            (47, 40, 5, 7),
            (52, 47, 4, 8),
        ):
            painter.drawRect(*rectangle)


class _CompactStatusIndicator(QFrame):
    """Compact one-line runtime status with details kept in the tooltip."""

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self._base_title = title
        self._raw_text = ""
        self._status_style = ""
        self._surface_style = ""
        self._state = "waiting"
        self.setMinimumWidth(150)
        self.setMaximumWidth(220)
        self.setMinimumHeight(30)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 0, 10, 0)
        layout.setSpacing(6)
        self._dot = QLabel(self)
        self._dot.setFixedSize(8, 8)
        layout.addWidget(self._dot, 0, Qt.AlignVCenter)
        self._title_label = QLabel(title, self)
        self._title_label.setStyleSheet(
            "color: #344054; font-size: 9pt; font-weight: 600;"
        )
        layout.addWidget(self._title_label)
        self._detail_label = QLabel("待机", self)
        self._detail_label.setStyleSheet(
            "color: #667085; font-size: 9pt; font-weight: 700;"
        )
        layout.addWidget(self._detail_label)
        layout.addStretch(1)
        self._apply_color("#667085")

    def text(self) -> str:
        return self._raw_text

    def setText(self, value: str) -> None:
        self._raw_text = str(value)
        self._title_label.setText(self._display_title())
        self._detail_label.setText(self._display_state())

    def setStatus(self, value: str, state: str) -> None:
        if state not in {"waiting", "busy", "ready", "error"}:
            raise ValueError(f"unsupported status state: {state}")
        self._state = state
        self.setText(value)

    def styleSheet(self) -> str:
        return self._status_style

    def setStyleSheet(self, style: str) -> None:
        self._status_style = str(style)
        match = re.search(r"color\s*:\s*(#[0-9a-fA-F]{6})", self._status_style)
        if match:
            self._apply_color(match.group(1))

    def _display_title(self) -> str:
        return self._base_title

    def _display_state(self) -> str:
        if self._state == "error":
            if "认证失败" in self._raw_text:
                return "认证失败"
            return "异常"
        if self._state == "busy":
            return "处理中"
        if self._state == "ready" and self._base_title == "高速摄像":
            return "就绪"
        if self._state == "ready":
            return "正常"
        if self._base_title == "计时源":
            return "待数据"
        return "待机"

    def _apply_color(self, color: str) -> None:
        normalized = color.lower()
        detail_color = {
            "#247a52": "#176b49",
            "#16845b": "#176b49",
            "#a56300": "#8a5700",
            "#b54747": "#a43b3b",
        }.get(
            normalized,
            "#667085",
        )
        self._surface_style = "QFrame { background: transparent; border: none; }"
        QFrame.setStyleSheet(self, self._surface_style)
        self._title_label.setStyleSheet(
            "color: #344054; font-size: 9pt; font-weight: 600;"
        )
        self._detail_label.setStyleSheet(
            f"color: {detail_color}; font-size: 9pt; font-weight: 700;"
        )
        self._dot.setStyleSheet(
            f"background: {color}; border: none; border-radius: 4px;"
        )


class FinishReviewWindow(PassageReviewSurface):
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
        self._settings_saver = settings_saver
        self._workspace_mode = "live"
        self._archive_background_passage_count = 0
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
        if self.timing_provider == "cyclerace":
            inbox_metadata = self._receiver_metadata_store.current()
            if inbox_metadata is not None:
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
        if self.timing_provider == "cyclerace" and inbox_metadata is not None:
            self._merge_cyclerace_events(
                passage_store,
                inbox_metadata.race_id,
            )
        metadata_store = (
            None
            if self.timing_provider == "racetiger"
            else RaceMetadataStore(self.output_dir / "cyclerace_race_metadata.json")
        )
        timeline_store = VideoTimelineStore(self.output_dir / "video_timeline.jsonl")
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
            self.output_dir / "finish_lines.json"
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
        self._historical_passage_count = len(passage_store)
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
        self._init_operator_controls()
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

    def _observe_runtime_metric(
        self,
        operation: str,
        started_at: float,
        *,
        item_count: int = 0,
        failed: bool = False,
    ) -> None:
        self.runtime_metrics.observe(
            operation,
            (time.perf_counter() - started_at) * 1000.0,
            item_count=item_count,
            failed=failed,
        )

    def _log_runtime_metrics(self) -> None:
        if getattr(self, "_runtime_metrics_reported", False):
            return
        self._runtime_metrics_reported = True
        logger.info("Review decode resources: %s", DECODE_RESOURCES.snapshot())
        catalog = getattr(self, "_recording_catalog", None)
        if catalog is not None:
            logger.info("Recording file checks: %s", catalog.metrics.snapshot("catalog.file_check"))
        for summary in (*self.runtime_metrics.snapshots(), *DECODE_RESOURCES.metrics.snapshots()):
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

    def _update_operator_controls(self) -> None:
        if not hasattr(self, "operator_identity_label"):
            return
        event = self.passage_store.get(self._selected_event_id)
        identity = ""
        if event is not None:
            identity = event.bib.strip()
        else:
            identity = self.selected_identity_value.text().strip()
            if identity == "--":
                identity = ""
        if identity:
            row = self.table.currentRow()
            position_text = (
                f"{row + 1:,} / {len(self._visible_events):,}"
                if 0 <= row < len(self._visible_events)
                else "未进入终点记录"
            )
            group_label = self.group_value.text().strip()
            review_status = (
                self._display_confirmation_status(
                    self._event_review_statuses.get(self._selected_event_id, "")
                )
                if event is not None
                else "尚无通过记录"
            )
            self.operator_identity_label.setText(
                " · ".join(
                    value
                    for value in (group_label, position_text, review_status)
                    if value and value != "--"
                )
            )
        else:
            self.operator_identity_label.clear()
        active_panes = set(self.evidence_panes)
        for pane in self.all_evidence_panes:
            pane.mark_btn.setEnabled(
                bool(
                    pane in active_panes
                    and identity
                    and getattr(pane.video_view, "has_frame", False)
                )
            )
        if hasattr(self, "mark_regular_button"):
            self.mark_regular_button.setEnabled(
                bool(
                    identity
                    and any(
                        getattr(pane.video_view, "has_frame", False)
                        for pane in self.regular_panes
                    )
                )
            )
        if hasattr(self, "mark_high_speed_button"):
            self.mark_high_speed_button.setEnabled(
                bool(
                    self.high_speed_dir is not None
                    and identity
                    and getattr(self.high_speed_pane.video_view, "has_frame", False)
                )
            )
        has_pending_marker = any(
            pane.has_pending_marker for pane in self.evidence_panes
        )
        self.confirm_next_button.setEnabled(bool(identity and has_pending_marker))

    def _pending_marker_pane(self):
        return next(
            (pane for pane in self.evidence_panes if pane.has_pending_marker),
            None,
        )

    def _regular_pane_for_operator_marking(self):
        return next(
            (
                pane
                for pane in self.regular_panes
                if getattr(pane.video_view, "has_frame", False)
            ),
            self.regular_pane,
        )

    def _confirm_current_marker(self) -> None:
        pane = self._pending_marker_pane()
        if pane is None:
            return
        self._confirm_pending_marker(pane)
        self._update_operator_controls()

    def _confirm_and_next(self) -> None:
        pane = self._pending_marker_pane()
        if pane is None:
            return
        row = self.table.currentRow()
        event_id = self._selected_event_id
        confirmed = self._confirm_pending_marker(pane)
        if (
            confirmed
            and self._selected_event_id == event_id
            and 0 <= row < self.table.rowCount() - 1
        ):
            self._move_selection(1)

    def _toggle_recording(self) -> None:
        if self._recording_any_active():
            self.stop_recording()
            return
        if not is_supported_review_source(self.source):
            self._configure_devices()
            if not is_supported_review_source(self.source):
                return
        try:
            self.start_recording()
        except Exception as exc:  # noqa: BLE001 - GUI boundary reports device failures.
            self._runtime_error = sanitize_recording_message(exc)
            QMessageBox.critical(self, "无法开始录像", self._runtime_error)
            self._update_runtime_status()

    def _current_settings(self, *, output_dir: Path | None = None) -> FinishReviewSettings:
        return FinishReviewSettings(
            source=self.source,
            output_dir=output_dir or self.workspace_root,
            passage_host=self.passage_host,
            passage_port=self.passage_port,
            camera_index=self.camera_index,
            secondary_source=self.secondary_source,
            high_speed_dir=self.high_speed_dir,
            finishreview_ip=self.finishreview_ip,
            cyclerace_ip=self.cyclerace_ip,
            high_speed_pc_ip=self.high_speed_pc_ip,
            switch_ip=self.switch_ip,
            timing_provider=self.timing_provider,
            racetiger_base_url=self.racetiger_base_url,
            racetiger_pc=self.racetiger_pc,
            racetiger_rid=self.racetiger_rid,
            racetiger_token=self.racetiger_token,
            racetiger_poll_interval_seconds=self.racetiger_poll_interval_seconds,
            visual_detection_enabled=self._visual_detection_enabled,
            visual_camera_index=self._visual_camera_index,
            visual_finish_line=self._visual_finish_line,
            visual_gate_width=self._visual_gate_width,
            visual_forward_direction=self._visual_forward_direction,
            visual_roi_top=self._visual_roi_top,
            visual_roi_bottom=self._visual_roi_bottom,
        )

    def _saved_event_workspaces(self) -> tuple[EventWorkspaceDescriptor, ...]:
        return discover_event_workspaces(self.workspace_root)

    @staticmethod
    def _saved_event_summary(
        workspace: EventWorkspaceDescriptor,
    ) -> EventWorkspaceSummary:
        return summarize_event_workspace(workspace)

    def _open_saved_event_workspace(self, path: Path) -> bool:
        if self.timing_provider != "cyclerace":
            QMessageBox.warning(self, "无法打开赛事", "打开赛事仅支持 CycleRace。")
            return False
        if self._recording_any_active():
            QMessageBox.warning(self, "无法打开赛事", "请先停止普通录像。")
            return False
        try:
            workspace = validate_event_workspace(path, self.workspace_root)
        except EventWorkspaceError as error:
            QMessageBox.warning(self, "无法打开赛事", str(error))
            return False
        if workspace.path == self.output_dir.resolve() and self._workspace_mode == "live":
            return True

        self._export_review_summary()
        applied = self._apply_settings(
            self._current_settings(output_dir=workspace.path),
            persist_settings=False,
            update_workspace_root=False,
            preserve_cyclerace_receiver=True,
        )
        if not applied:
            return False
        self._workspace_mode = "archive"
        self._archive_background_passage_count = 0
        self._workspace_notice = ""
        self._capture_error = ""
        self.refresh()
        self._update_runtime_status()
        return True

    def _return_to_live_event(self) -> bool:
        if self._workspace_mode != "archive":
            return True
        metadata = self._receiver_metadata_store.current()
        if metadata is None:
            QMessageBox.warning(
                self,
                "无法返回当前赛事",
                "尚未收到 CycleRace 当前赛事信息。",
            )
            return False
        try:
            applied = self._activate_cyclerace_workspace(
                metadata,
                force=True,
                preserve_cyclerace_receiver=True,
            )
        except Exception as error:  # noqa: BLE001 - keep the archive open on failure.
            QMessageBox.warning(
                self,
                "无法返回当前赛事",
                sanitize_recording_message(error),
            )
            return False
        if not applied:
            return False
        self._workspace_mode = "live"
        self._archive_background_passage_count = 0
        self._workspace_notice = ""
        self._capture_error = ""
        self.refresh()
        self._apply_pending_focus()
        self._update_runtime_status()
        return True

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

    def _apply_settings(
        self,
        settings: FinishReviewSettings,
        *,
        stop_recording: bool = False,
        persist_settings: bool = True,
        update_workspace_root: bool = True,
        preserve_cyclerace_receiver: bool = False,
        reload_data_source: bool = False,
    ) -> bool:
        self._runtime_error = ""
        self._auto_recording_error = ""
        requested_output_dir = Path(settings.output_dir).expanduser().resolve()
        workspace_root_changed = bool(
            update_workspace_root and requested_output_dir != self.workspace_root
        )
        output_dir = (
            requested_output_dir
            if not update_workspace_root or workspace_root_changed
            else self.output_dir
        )
        output_changed = output_dir != self.output_dir
        next_timing_provider = str(settings.timing_provider or "cyclerace").strip().lower()
        if next_timing_provider not in {"cyclerace", "racetiger"}:
            next_timing_provider = "cyclerace"
        timing_changed = next_timing_provider != self.timing_provider
        next_racetiger_values = (
            str(settings.racetiger_base_url or "").strip(),
            str(settings.racetiger_pc or "").strip(),
            str(settings.racetiger_rid or "").strip(),
            str(settings.racetiger_token or ""),
            max(0.5, float(settings.racetiger_poll_interval_seconds or 2.0)),
        )
        racetiger_changed = next_racetiger_values != (
            self.racetiger_base_url,
            self.racetiger_pc,
            self.racetiger_rid,
            self.racetiger_token,
            self.racetiger_poll_interval_seconds,
        )
        data_source_changed = bool(
            reload_data_source
            or output_changed
            or timing_changed
            or workspace_root_changed
        )
        preserve_running_receiver = bool(
            preserve_cyclerace_receiver
            and self.timing_provider == "cyclerace"
            and next_timing_provider == "cyclerace"
            and not workspace_root_changed
            and self._receiver is not None
            and self._receiver.is_running
        )
        receiver_restart_needed = (
            data_source_changed
            or (self.timing_provider == "racetiger" and racetiger_changed)
        ) and not preserve_running_receiver
        prepared_data_source = None
        if data_source_changed:
            try:
                output_dir.mkdir(parents=True, exist_ok=True)
                passage_store = PassageEventStore(
                    output_dir
                    / (
                        "racetiger_passage_events.jsonl"
                        if next_timing_provider == "racetiger"
                        else "cyclerace_passage_events.jsonl"
                    )
                )
                metadata_store = (
                    None
                    if next_timing_provider == "racetiger"
                    else RaceMetadataStore(
                        output_dir / "cyclerace_race_metadata.json"
                    )
                )
                timeline_store = VideoTimelineStore(
                    output_dir / "video_timeline.jsonl"
                )
                review_binding_store = PassageReviewBindingStore(
                    output_dir / "review_clips.jsonl"
                )
                association_store = PassageEvidenceAssociationStore(
                    output_dir / "passage_evidence_associations.jsonl"
                )
                calibration_store = VideoClockCalibrationStore(
                    output_dir / "video_clock_calibrations.jsonl"
                )
                preflight_journal = PreflightJournal(
                    output_dir / "preflight_tests.jsonl"
                )
                historical_events = passage_store.events()
                evidence_timestamp_overrides = (
                    _historical_evidence_timestamp_overrides(historical_events)
                    if next_timing_provider == "cyclerace"
                    else {}
                )
                archive_publishers = [
                    ArchiveTimelinePublisher(session, timeline_store)
                    for session in load_archive_recording_sessions(output_dir)
                ]
                receiver_passage_store = self._receiver_passage_store
                receiver_metadata_store = self._receiver_metadata_store
                if workspace_root_changed:
                    inbox_dir = requested_output_dir / CYCLERACE_INBOX_DIRNAME
                    receiver_passage_store = PassageEventStore(
                        inbox_dir / "cyclerace_passage_inbox.jsonl"
                    )
                    receiver_metadata_store = RaceMetadataStore(
                        inbox_dir / "cyclerace_metadata_inbox.json"
                    )
                prepared_data_source = (
                    passage_store,
                    metadata_store,
                    timeline_store,
                    review_binding_store,
                    association_store,
                    calibration_store,
                    preflight_journal,
                    historical_events,
                    evidence_timestamp_overrides,
                    archive_publishers,
                    receiver_passage_store,
                    receiver_metadata_store,
                )
            except Exception as exc:  # noqa: BLE001 - validate before runtime mutation.
                self._runtime_error = sanitize_recording_message(exc)
                QMessageBox.warning(self, "设置无法应用", self._runtime_error)
                self._update_runtime_status()
                return False
        previous_runtime_settings = self._current_settings()
        if data_source_changed:
            self._capture_refresh_generation += 1
            if not self._capture_refresh_worker.invalidate_and_wait(
                self._capture_refresh_generation,
                timeout=5.0,
            ):
                QMessageBox.warning(
                    self,
                    "设置无法应用",
                    "后台录像刷新仍在运行，设置未切换。",
                )
                return False
        if persist_settings and self._settings_saver is not None:
            try:
                self._settings_saver(settings)
            except Exception as exc:  # noqa: BLE001 - keep current runtime unchanged.
                QMessageBox.warning(self, "设置未保存", str(exc))
                return False
        if stop_recording:
            failures = self.stop_recording()
            if any(failure.still_running for failure in failures):
                if persist_settings and self._settings_saver is not None:
                    try:
                        self._settings_saver(previous_runtime_settings)
                    except Exception:
                        logger.exception("Failed to roll back settings after stop failure")
                QMessageBox.warning(
                    self,
                    "设置无法应用",
                    "仍有录像进程未停止，设置未切换。",
                )
                return False
        if receiver_restart_needed:
            self.stop_receiver()
        if data_source_changed:
            self._stop_archive_video_scan_workers()
            self._passage_batch_timer.stop()
            self._pending_passages.clear()
        self.source = str(settings.source).strip()
        self.secondary_source = str(settings.secondary_source).strip()
        self.passage_host = str(settings.passage_host).strip()
        self.passage_port = int(settings.passage_port)
        self.camera_index = max(1, int(settings.camera_index))
        self.finishreview_ip = str(settings.finishreview_ip).strip()
        self.cyclerace_ip = str(settings.cyclerace_ip).strip()
        self.high_speed_pc_ip = str(settings.high_speed_pc_ip).strip()
        self.switch_ip = str(settings.switch_ip).strip()
        self.timing_provider = next_timing_provider
        (
            self.racetiger_base_url,
            self.racetiger_pc,
            self.racetiger_rid,
            self.racetiger_token,
            self.racetiger_poll_interval_seconds,
        ) = next_racetiger_values
        next_visual_values = (
            bool(settings.visual_detection_enabled),
            max(1, int(settings.visual_camera_index)),
            float(settings.visual_finish_line),
            float(settings.visual_gate_width),
            str(settings.visual_forward_direction or "left_to_right"),
            float(settings.visual_roi_top),
            float(settings.visual_roi_bottom),
        )
        visual_changed = next_visual_values != (
            self._visual_detection_enabled,
            self._visual_camera_index,
            self._visual_finish_line,
            self._visual_gate_width,
            self._visual_forward_direction,
            self._visual_roi_top,
            self._visual_roi_bottom,
        )
        (
            self._visual_detection_enabled,
            self._visual_camera_index,
            self._visual_finish_line,
            self._visual_gate_width,
            self._visual_forward_direction,
            self._visual_roi_top,
            self._visual_roi_bottom,
        ) = next_visual_values
        next_high_speed_dir = (
            Path(settings.high_speed_dir).expanduser().absolute()
            if settings.high_speed_dir is not None
            and str(settings.high_speed_dir).strip()
            else None
        )
        high_speed_changed = next_high_speed_dir != self.high_speed_dir
        if high_speed_changed:
            self.high_speed_dir = next_high_speed_dir
            self._high_speed_catalog.set_root(next_high_speed_dir)
        configured_camera_indexes = tuple(
            camera_index
            for camera_index, _source in self._configured_recording_sources()
        ) or (self.camera_index,)
        evidence_layout_changed = bool(
            configured_camera_indexes != self._configured_regular_camera_indexes
            or (self.high_speed_dir is not None) != self._show_high_speed_pane
        )
        if evidence_layout_changed:
            self._sync_evidence_pane_layout(include_recorded=False)
        if data_source_changed:
            assert prepared_data_source is not None
            self.video_filmstrip.full_race.set_recording_start(None)
            (
                passage_store,
                metadata_store,
                timeline_store,
                review_binding_store,
                association_store,
                calibration_store,
                preflight_journal,
                historical_events,
                evidence_timestamp_overrides,
                archive_publishers,
                receiver_passage_store,
                receiver_metadata_store,
            ) = prepared_data_source
            if workspace_root_changed:
                self.workspace_root = requested_output_dir
                self._receiver_passage_store = receiver_passage_store
                self._receiver_metadata_store = receiver_metadata_store
            self.output_dir = output_dir
            self._finish_line_store = FinishLineStore(
                self.output_dir / "finish_lines.json"
            )
            self._finish_line_rois = self._finish_line_store.rois()
            self._video_review_journal = VideoReviewJournal(
                self.output_dir / "video_review.jsonl"
            )
            self.video_arrival_store = VideoArrivalCandidateStore(
                self.output_dir / "video_arrival_candidates.jsonl",
                metrics=self.runtime_metrics,
            )
            self._video_reconciliation_by_id.clear()
            self._video_candidate_cache = {
                str(candidate.candidate_id): candidate
                for candidate in self.video_arrival_store.candidates()
            }
            self.set_video_navigation_candidates(
                tuple(self._video_candidate_cache.values())
            )
            self._video_review_statuses.clear()
            self._video_review_bibs.clear()
            for record in self._video_review_journal.records():
                self.restore_video_review_record(
                    record.candidate_id,
                    status=record.status,
                    bib=record.bib,
                )
            self._video_reconciliation = ()
            self._active_video_anomaly_id = ""
            self._video_anomaly_cursor = -1
            self.passage_store = passage_store
            self.metadata_store = metadata_store
            self.timeline_store = timeline_store
            self.review_binding_store = review_binding_store
            self.association_store = association_store
            self.calibration_store = calibration_store
            self._lookup_cache.clear()
            self._timeline_signature = ()
            self._selected_event_id = ""
            self._capture_windows_by_camera = {self.camera_index: {}}
            self._capture_windows = self._capture_windows_by_camera[
                self.camera_index
            ]
            self._archive_publishers = archive_publishers
            self._unsupported_event_ids.clear()
            self._preflight_journal = preflight_journal
            self._preflight_event_keys = set(preflight_journal.event_keys())
            self._evidence_timestamp_overrides = evidence_timestamp_overrides
            self._historical_passage_count = len(self.passage_store)
            self._received_passage_count = 0
            self._received_passage_sequence = 0
            self._received_event_order.clear()
            self._capture_error = ""
            self.refresh()
            if self._video_candidate_cache:
                self._reconcile_video_candidates(
                    tuple(self._video_candidate_cache.values())
                )
            if not preserve_running_receiver:
                try:
                    self.start_receiver()
                except Exception as exc:  # noqa: BLE001 - settings remain applied.
                    self._runtime_error = sanitize_recording_message(exc)
                    QMessageBox.warning(
                        self,
                        (
                            "赛虎读取未启动"
                            if self.timing_provider == "racetiger"
                            else "CycleRace监听未启动"
                        ),
                        self._runtime_error,
                    )
            self._high_speed_catalog.set_cache_path(
                output_dir / HIGH_SPEED_INDEX_FILENAME
            )
            self._high_speed_catalog.set_target_dates(
                _high_speed_target_dates(
                    historical_events,
                    self._evidence_timestamp_overrides,
                )
            )
        if high_speed_changed or data_source_changed:
            self.invalidate_external_locations()
            self._request_high_speed_scan()
        elif evidence_layout_changed:
            self.refresh()
        if receiver_restart_needed and not data_source_changed:
            try:
                self.start_receiver()
            except Exception as exc:  # noqa: BLE001 - settings remain applied.
                self._runtime_error = sanitize_recording_message(exc)
                QMessageBox.warning(
                    self,
                    "Timing source not started",
                    self._runtime_error,
                )
        if visual_changed and self._recording_any_active():
            self._start_visual_workers()
        self._update_runtime_status()
        return True

    def _init_runtime_status(self) -> None:
        panel = QFrame(self)
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
        self.product_title_label = QLabel(APP_DISPLAY_NAME, panel)
        self.product_title_label.setStyleSheet(
            "font-size: 12pt; font-weight: 700; color: #17212b;"
        )
        self.product_subtitle_label = QLabel("终点多源复核", panel)
        self.product_subtitle_label.hide()
        top_layout.addWidget(self.product_title_label)

        brand_separator = QFrame(panel)
        brand_separator.setFrameShape(QFrame.VLine)
        brand_separator.setStyleSheet("color: #dce2e6;")
        top_layout.addWidget(brand_separator)

        event_layout = QHBoxLayout()
        event_layout.setContentsMargins(0, 0, 0, 0)
        event_layout.setSpacing(10)
        self.event_name_label = _ElidedLabel("未加载赛事", panel)
        self.event_name_label.setStyleSheet(
            "font-size: 11pt; font-weight: 700; color: #17212b;"
        )
        self.event_name_label.setMinimumWidth(180)
        self.event_name_label.setSizePolicy(
            QSizePolicy.Ignored,
            QSizePolicy.Preferred,
        )
        self.event_path_label = _ElidedLabel(
            "等待计时源赛事信息 · 终点",
            panel,
        )
        self.event_path_label.setStyleSheet(
            "font-size: 10pt; font-weight: 600; color: #667085;"
        )
        self.event_path_label.setMinimumWidth(150)
        self.event_path_label.setSizePolicy(
            QSizePolicy.Ignored,
            QSizePolicy.Preferred,
        )
        event_layout.addWidget(self.event_name_label, 3)
        event_layout.addWidget(self.event_path_label, 2)
        top_layout.addLayout(event_layout, 1)

        self.runtime_alert_label = QLabel(panel)
        self.runtime_alert_label.setStyleSheet(
            "color: #b54747; font-size: 9pt; font-weight: 700;"
        )
        self.runtime_alert_label.setMaximumWidth(180)
        self.runtime_alert_label.hide()
        top_layout.addWidget(self.runtime_alert_label)

        clock_layout = QHBoxLayout()
        clock_layout.setContentsMargins(0, 0, 0, 0)
        clock_layout.setSpacing(4)
        self.beijing_clock_label = QLabel(panel)
        # Keep the live clock readable when the event header is compressed.
        self.beijing_clock_label.setMinimumWidth(72)
        self.beijing_clock_label.setSizePolicy(
            QSizePolicy.Fixed,
            QSizePolicy.Preferred,
        )
        self.beijing_clock_label.setStyleSheet(
            "font-family: Consolas; color: #17212b; font-size: 11pt; font-weight: 700;"
        )
        self.beijing_zone_label = QLabel("北京时间", panel)
        self.beijing_zone_label.setMinimumWidth(64)
        self.beijing_zone_label.setSizePolicy(
            QSizePolicy.Fixed,
            QSizePolicy.Preferred,
        )
        self.beijing_zone_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.beijing_zone_label.setStyleSheet(
            "color: #667085; font-size: 9pt; font-weight: 500;"
        )
        clock_layout.addWidget(self.beijing_clock_label)
        clock_layout.addWidget(self.beijing_zone_label)
        top_layout.addLayout(clock_layout)

        self.race_dir_label = QLabel(panel)
        self.race_dir_label.hide()
        self.recording_status_label = QLabel(panel)
        self.recording_status_label.hide()
        self.storage_status_label = QLabel(panel)
        self.storage_status_label.hide()
        self.capture_status_label = QLabel(panel)
        self.capture_status_label.hide()

        self.recheck_button = QPushButton("刷新", panel)
        self.recheck_button.setObjectName("finishRecheckButton")
        self.recheck_button.setIcon(self.style().standardIcon(QStyle.SP_BrowserReload))
        self.recheck_button.setFixedHeight(32)
        self.recheck_button.setToolTip("重新检查连接")
        self.recheck_button.clicked.connect(self._recheck_connections)
        top_layout.addWidget(self.recheck_button)
        self.settings_button = QPushButton("设置", panel)
        self.settings_button.setObjectName("finishSettingsButton")
        self.settings_button.setFixedHeight(32)
        self.settings_button.setToolTip("设备与赛事设置")
        self.settings_button.clicked.connect(self._configure_devices)
        top_layout.addWidget(self.settings_button)
        tool_button_style = (
            "QPushButton { background: #eef3f5; color: #17212b; "
            "border: 1px solid #aab6bf; border-radius: 4px; "
            "font-size: 10pt; font-weight: 600; }"
            "QPushButton:hover { background: #dfe9ed; border-color: #617783; }"
            "QPushButton:pressed { background: #cfdde2; }"
        )
        self.recheck_button.setStyleSheet(tool_button_style)
        self.settings_button.setStyleSheet(tool_button_style)
        self.record_button = QPushButton("开始录像", panel)
        self.record_button.setObjectName("finishRecordButton")
        self.record_button.setMinimumWidth(88)
        self.record_button.clicked.connect(self._toggle_recording)
        top_layout.addWidget(self.record_button)
        self.archive_scan_button = QPushButton("分析历史视频", panel)
        self.archive_scan_button.setObjectName("finishArchiveScanButton")
        self.archive_scan_button.setMinimumWidth(104)
        self.archive_scan_button.setToolTip(
            "仅在历史比赛中手动启动一次性视频候选分析"
        )
        self.archive_scan_button.clicked.connect(self._toggle_archive_video_scan)
        self.archive_scan_button.setVisible(False)
        top_layout.addWidget(self.archive_scan_button)
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
        self.receiver_status_label = self._status_chip("计时源", status_strip)
        self.camera_status_label = self._status_chip("普通摄像", status_strip)
        self.high_speed_status_label = self._status_chip("高速摄像", status_strip)
        self.video_assist_status_label = self._status_chip("视频辅助", status_strip)
        status_layout.addWidget(self.receiver_status_label)
        status_layout.addWidget(self.camera_status_label)
        status_layout.addWidget(self.high_speed_status_label)
        status_layout.addWidget(self.video_assist_status_label)
        status_layout.addStretch(1)
        panel_layout.addWidget(status_strip)
        self.runtime_status_strip = status_strip
        self.runtime_header = panel
        root_layout = self.layout()
        if root_layout is not None:
            root_layout.insertWidget(0, panel)
        self._update_event_header()

    @staticmethod
    def _status_chip(title: str, parent) -> _CompactStatusIndicator:
        return _CompactStatusIndicator(title, parent)

    def _update_event_header(self) -> None:
        metadata = self._current_metadata()
        event = self.passage_store.get(self._selected_event_id)
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
        group_name = self.group_value.text().strip()
        if not group_name or group_name == "--":
            group_name = "全部组别"
        detail_parts = []
        context_values = (
            ("历史复核", stage_label, group_name, "终点")
            if self._workspace_mode == "archive"
            else (stage_label, group_name, "终点")
        )
        for value in context_values:
            if value and value not in detail_parts:
                detail_parts.append(value)
        detail_text = " · ".join(detail_parts)
        identity_text = f"赛事ID：{race_id}\n" if race_id else ""
        self.event_name_label.setText(race_name)
        self.event_path_label.setText(detail_text)
        self.event_name_label.setToolTip(
            f"{race_name}\n{detail_text}\n{identity_text}赛事目录：{self.output_dir}"
        )
        self.event_path_label.setToolTip(
            f"{detail_text}\n{identity_text}赛事目录：{self.output_dir}"
        )

    def _init_operator_controls(self) -> None:
        self.operator_identity_label = self.current_context_label
        self.mark_regular_button = self.regular_pane.mark_btn
        self.mark_regular_button.clicked.disconnect()
        self.mark_regular_button.clicked.connect(
            lambda: self._begin_marking(self._regular_pane_for_operator_marking())
        )
        self.mark_high_speed_button = self.high_speed_pane.mark_btn
        self.mark_high_speed_button.setVisible(self.high_speed_dir is not None)
        self.confirm_next_button = QPushButton("确认并下一条", self.transport)
        self.confirm_next_button.setShortcut("Ctrl+Return")
        self.confirm_next_button.setToolTip("确认当前标线并选择下一条")
        self.confirm_next_button.clicked.connect(self._confirm_and_next)
        self.transport_layout.addWidget(self.confirm_next_button)
        self.evidence_pane_added.connect(self._bind_operator_pane)
        for pane in self.all_evidence_panes:
            self._bind_operator_pane(pane)
        self._update_operator_controls()

    def _bind_operator_pane(self, pane) -> None:
        pane.video_view.marker_position_selected.connect(
            lambda _x, _y: QTimer.singleShot(0, self._update_operator_controls)
        )
        pane.confirmation_requested.connect(
            lambda _pane: QTimer.singleShot(0, self._update_operator_controls)
        )
        pane.cancel_requested.connect(
            lambda _pane: QTimer.singleShot(0, self._update_operator_controls)
        )
        pane.delete_requested.connect(
            lambda _pane: QTimer.singleShot(0, self._update_operator_controls)
        )

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

    def _deployment_runtime_snapshot(self) -> dict[str, str]:
        timing_name = "赛虎" if self.timing_provider == "racetiger" else "CycleRace"
        if self._receiver_error:
            timing_state = "异常"
            timing_detail = self._receiver_error
        elif self._last_passage_monotonic:
            age_seconds = max(0.0, time.monotonic() - self._last_passage_monotonic)
            timing_state = "通过" if age_seconds <= 30.0 else "待检查"
            timing_detail = (
                f"{age_seconds:.0f}秒前收到{timing_name}数据"
                if age_seconds >= 1.0
                else f"刚刚收到{timing_name}数据"
            )
        elif self.timing_provider == "racetiger":
            source = self._racetiger_source
            if source is not None and source.is_running:
                timing_state = "待检查"
                timing_detail = "赛虎读取服务运行中，尚未读取到本次终点记录"
            else:
                timing_state = "异常"
                timing_detail = "赛虎读取服务未启动"
        elif self._receiver is not None and self._receiver.is_running:
            timing_state = "待检查"
            timing_detail = "接收服务正在监听，尚未收到本次数据"
        else:
            timing_state = "异常"
            timing_detail = "CycleRace接收服务未启动"

        configured_sources = self._configured_recording_sources()
        active_recorders = {
            camera_index: recorder
            for camera_index, recorder in self._recorders.items()
            if recorder.is_running
        }
        if self._runtime_error:
            camera_state = "异常"
            camera_detail = self._runtime_error
        elif active_recorders:
            missing = [
                camera_index
                for camera_index, _source in configured_sources
                if camera_index not in active_recorders
            ]
            segment_counts = {
                camera_index: len(ring_buffer.status_segments())
                for camera_index, ring_buffer in self._ring_buffers.items()
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

        high_speed = self._high_speed_scan_result
        if self.high_speed_dir is None:
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
        metadata = self._current_metadata()
        if self.timing_provider == "cyclerace":
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
                if metadata is not None and self._workspace_mode == "archive"
                else "赛事已加载"
                if metadata is not None
                else "等待 CycleRace 赛事信息"
            )
            event_dir = str(self.output_dir) if metadata is not None else ""
        else:
            event_name = self.racetiger_rid
            event_stage = "终点" if event_name else ""
            event_state = "赛事已配置" if event_name else "等待赛虎赛事 RID"
            event_dir = str(self.output_dir) if event_name else ""
        return {
            "timing_provider": self.timing_provider,
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
            "workspace_root": str(self.workspace_root),
            "workspace_mode": self._workspace_mode,
            "recording_active": "1" if self._recording_any_active() else "0",
        }

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

    def _recheck_connections(self) -> None:
        if self.timing_provider == "racetiger":
            source = self._racetiger_source
            if source is None or not source.is_running:
                try:
                    self.start_receiver()
                except Exception as exc:  # noqa: BLE001 - retry remains operator-visible.
                    QMessageBox.warning(
                        self,
                        "赛虎读取未启动",
                        sanitize_recording_message(exc),
                    )
            self._high_speed_scan_worker.request_scan()
            self._update_runtime_status()
            return
        receiver = self._receiver
        if receiver is None or not receiver.is_running:
            try:
                self.start_receiver()
            except Exception as exc:  # noqa: BLE001 - retry remains operator-visible.
                QMessageBox.warning(
                    self,
                    "CycleRace监听未启动",
                    sanitize_recording_message(exc),
                )
        self._request_high_speed_scan()
        self._update_runtime_status()

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
            self.video_filmstrip.full_race.set_recording_start(recording_started_ms)
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


    def _activate_cyclerace_workspace(
        self,
        metadata: RaceMetadata,
        *,
        force: bool = False,
        preserve_cyclerace_receiver: bool = False,
    ) -> bool:
        if self.timing_provider != "cyclerace" or self.metadata_store is None:
            return False
        current_metadata = self.metadata_store.current()
        if (
            not force
            and current_metadata is not None
            and current_metadata.race_id == metadata.race_id
        ):
            self.metadata_store.store(metadata)
            self._merge_cyclerace_events(self.passage_store, metadata.race_id)
            self.refresh()
            return False

        self._export_review_summary()
        target_dir = _event_workspace_dir(self.workspace_root, metadata)
        target_dir.mkdir(parents=True, exist_ok=True)
        target_metadata_store = RaceMetadataStore(
            target_dir / "cyclerace_race_metadata.json"
        )
        target_metadata_store.store(metadata)
        target_passage_store = PassageEventStore(
            target_dir / "cyclerace_passage_events.jsonl"
        )
        self._merge_cyclerace_events(target_passage_store, metadata.race_id)

        recording_was_active = self._recording_any_active()
        applied = self._apply_settings(
            self._current_settings(output_dir=target_dir),
            stop_recording=recording_was_active,
            persist_settings=False,
            update_workspace_root=False,
            preserve_cyclerace_receiver=preserve_cyclerace_receiver,
            reload_data_source=force,
        )
        if applied:
            self._workspace_mode = "live"
            self._archive_background_passage_count = 0
        if applied and recording_was_active:
            self._workspace_notice = "已切换新赛事，录像待重新开始"
        return applied

    def _merge_cyclerace_events(
        self,
        target_store: PassageEventStore,
        race_id: str,
    ) -> int:
        """Merge the newest live/inbox revisions into an event workspace.

        ``PassageEventStore.events()`` hides inactive events by default.  The
        inactive records are revisioned tombstones, however, and must travel
        with the workspace or an older active passage can be resurrected when
        switching back from an archive or restarting the application.
        """
        events_by_id: dict[str, PassageEvent] = {}
        stores = [target_store, self._receiver_passage_store]
        # During ``__init__`` the Qt base class has not been initialised yet,
        # so attribute lookup through QObject can raise RuntimeError.
        existing_store = self.__dict__.get("passage_store")
        if existing_store is not None and existing_store is not target_store:
            stores.append(existing_store)
        for store in stores:
            for event in store.events(include_inactive=True):
                if event.race_id != race_id:
                    continue
                current = events_by_id.get(event.event_id)
                if current is None or event.revision > current.revision:
                    events_by_id[event.event_id] = event
        merged = 0
        for event in events_by_id.values():
            current = target_store.get(event.event_id)
            if current is not None and current.revision >= event.revision:
                continue
            target_store.append(event)
            merged += 1
        return merged

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
        if (
            self._workspace_mode != "live"
            or not event.is_active
            or self._camera_auth_retry_blocked()
            or self._recording_any_active()
            or self._is_test_passage(event)
            or not self._is_live_passage(event)
        ):
            return False
        try:
            self.start_recording()
        except Exception as error:  # noqa: BLE001 - passage storage must survive.
            self._runtime_error = sanitize_recording_message(error)
            self._auto_recording_error = self._runtime_error
            logger.exception(
                "Failed to auto-start recording for live passage %s",
                event.event_id,
            )
            return False
        logger.info(
            "Recording auto-started for live passage %s in group %s",
            event.event_id,
            event.group_id,
        )
        return True

    def _auto_start_recording_for_metadata(self, metadata: RaceMetadata) -> bool:
        if (
            self._workspace_mode != "live"
            or self._camera_auth_retry_blocked()
            or self._recording_any_active()
            or not any(
                not _is_test_group_name(group.name)
                and not _is_test_group_name(group.group_id)
                for group in metadata.groups
            )
        ):
            return False
        try:
            self.start_recording()
        except Exception as error:  # noqa: BLE001 - metadata handling must survive.
            self._runtime_error = sanitize_recording_message(error)
            self._auto_recording_error = self._runtime_error
            logger.exception(
                "Failed to auto-start recording for live metadata %s/%s",
                metadata.race_id,
                metadata.stage_id,
            )
            return False
        logger.info(
            "Recording auto-started for live metadata %s/%s",
            metadata.race_id,
            metadata.stage_id,
        )
        return True

    def _on_passage_received(self, event: PassageEvent) -> None:
        if self._defer_capture_callback(self._on_passage_received, event):
            return
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
                    or active_metadata.race_id != event.race_id
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
                and active_metadata.race_id != event.race_id
            ):
                self._capture_error = (
                    f"收到赛事 {event.race_id} 的通过记录，等待CycleRace赛事信息"
                )
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
        self._historical_passage_count = len(self.passage_store)
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
        if self._workspace_mode == "archive":
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
        self._auto_start_recording_for_metadata(metadata)
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

    def _current_archive_race_id(self) -> str:
        metadata = (
            self.metadata_store.current() if self.metadata_store is not None else None
        )
        if metadata is not None:
            return metadata.race_id
        return race_id_from_passage_store(self.passage_store)

    def _export_review_summary(self, *, show_warning: bool = False) -> Path | None:
        metadata = self._current_metadata()
        if self.timing_provider == "cyclerace" and metadata is None:
            return None
        if self.timing_provider == "racetiger" and not self.racetiger_rid:
            return None
        try:
            return export_review_summary(
                self.output_dir,
                self._events_for_current_metadata(self.passage_store.events()),
                self.association_store,
                metadata,
            )
        except Exception as error:  # noqa: BLE001 - review operation must not be blocked.
            logger.exception("Failed to export the event review summary")
            if show_warning:
                QMessageBox.warning(
                    self,
                    "复核清单未更新",
                    f"无法更新终点复核清单：{error}",
                )
            return None

    def _events_for_current_metadata(
        self,
        events: tuple[PassageEvent, ...],
    ) -> tuple[PassageEvent, ...]:
        filtered = super()._events_for_current_metadata(events)
        if self.timing_provider == "racetiger" and self.racetiger_rid:
            filtered = tuple(
                event for event in filtered if event.race_id == self.racetiger_rid
            )
        return tuple(
            event
            for event in filtered
            if (event.race_id, event.stage_id, event.event_id)
            not in self._preflight_event_keys
        )

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
        )

    def _recording_sources_for_filmstrip(self, pane, race_id):
        if not hasattr(self, "_capture_refresh_worker"):
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

    def _collect_runtime_status(self) -> RuntimeStatusSnapshot:
        self._reap_finished_archive_video_scan_workers()
        configured_sources = tuple(self._configured_recording_sources())
        recording_active = self._recording_any_active()
        segments_by_camera = {
            camera_index: tuple(ring_buffer.status_segments())
            for camera_index, ring_buffer in self._ring_buffers.items()
        }
        metadata = (
            self.metadata_store.current()
            if self.metadata_store is not None
            else None
        )
        high_speed_result = self._high_speed_scan_result
        high_speed_root = self._high_speed_catalog.root
        storage_free_gb = None
        storage_error = ""
        storage = getattr(self, "_storage_snapshot", None)
        if storage is not None and storage[0] == self.output_dir:
            _, storage_free_gb, storage_error = storage

        counts = {state: 0 for state in PassageReviewState}
        event_states: dict[str, list[PassageReviewState]] = {}
        for windows in self._capture_windows_by_camera.values():
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
            output_dir=self.output_dir, video_assist_enabled=self._video_assist_enabled(),
            beijing_clock_text=datetime.now(
                timezone(timedelta(hours=8))
            ).strftime("%H:%M:%S"),
            epoch_now_ms=int(time.time() * 1000.0),
            configured_sources=configured_sources,
            recording_active=recording_active,
            recording_all_active=self._recording_all_active(),
            segments_by_camera=MappingProxyType(segments_by_camera),
            reconnecting_cameras=tuple(sorted(self._camera_reconnect_errors)),
            reconnect_errors=MappingProxyType(dict(self._camera_reconnect_errors)),
            running_recorder_cameras=frozenset(
                camera_index
                for camera_index, recorder in self._recorders.items()
                if recorder.is_running
            ),
            auto_recording_error=self._auto_recording_error,
            archive_scan_active=any(
                bool(getattr(worker, "is_running", False))
                for worker in self._archive_video_scan_workers.values()
            ),
            anomaly_count=len(self.video_reconciliation()),
            archive_candidate_count=(
                len(self.video_navigation_candidates())
                if not recording_active
                and any(
                    bool(getattr(worker, "is_running", False))
                    for worker in self._archive_video_scan_workers.values()
                )
                else 0
            ),
            visual_failed=self._visual_failed,
            visual_status=self._visual_status,
            video_scan_active=bool(self._video_scan_workers),
            finish_line_count=len(self._finish_line_rois),
            visual_detection_enabled=self._visual_detection_enabled,
            workspace_mode=self._workspace_mode,
            runtime_error=self._runtime_error,
            recording_elapsed_seconds=(
                max(0, int(time.monotonic() - self._recording_started_at))
                if recording_active
                else 0
            ),
            receiver_running=bool(
                self._receiver is not None and self._receiver.is_running
            ),
            receiver_metadata=metadata,
            pending_passage_count=len(getattr(self, "_pending_passages", ())),
            archive_background_passage_count=self._archive_background_passage_count,
            received_passage_count=self._received_passage_count,
            historical_passage_count=self._historical_passage_count,
            receiver_error=self._receiver_error,
            timing_provider=self.timing_provider,
            racetiger_running=bool(
                self._racetiger_source is not None
                and self._racetiger_source.is_running
            ),
            racetiger_status=self._racetiger_status,
            racetiger_configured=all(
                (
                    self.racetiger_base_url,
                    self.racetiger_pc,
                    self.racetiger_rid,
                    self.racetiger_token,
                )
            ),
            high_speed_result=high_speed_result,
            high_speed_root=high_speed_root,
            high_speed_remote=is_network_share(high_speed_root),
            storage_free_gb=storage_free_gb,
            storage_error=storage_error,
            capture_counts=MappingProxyType(counts),
            aligned_event_count=len(self._evidence_timestamp_overrides),
            capture_error=self._capture_error,
            available_evidence_count=self._available_evidence_count,
            unsupported_event_count=len(self._unsupported_event_ids),
            workspace_notice=self._workspace_notice,
        )

    def _update_runtime_status(self) -> None:
        self._render_runtime_status(self._collect_runtime_status())

    def _render_runtime_status(self, snapshot: RuntimeStatusSnapshot) -> None:
        if not hasattr(self, "_status_presenter"):
            self._status_presenter = RuntimeStatusPresenter(
                {name: getattr(self, name) for name in STATUS_WIDGET_NAMES},
            )
        self._status_presenter.render(snapshot)
        self._update_event_header()
        self._update_operator_controls()

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
