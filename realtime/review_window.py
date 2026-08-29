"""Production finish console without detection or OCR dependencies."""

from __future__ import annotations

import logging
import inspect
import os
import re
import socket
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from PyQt5.QtCore import QObject, Qt, QThread, QTimer, QUrl, pyqtSignal
from PyQt5.QtGui import QColor, QDesktopServices, QPainter, QPen
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStyle,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QDoubleSpinBox,
    QVBoxLayout,
    QWidget,
)

from .auyat_rgb import (
        AuyatRgbCatalog,
        AuyatRgbScanWorker,
        AuyatScanResult,
        is_network_share,
    )
from .external_clip_import import ExternalClipImportError, race_id_from_passage_store
from .event_workspace import (
        EventWorkspaceDescriptor,
        EventWorkspaceError,
        EventWorkspaceSummary,
        discover_event_workspaces,
        summarize_event_workspace,
        validate_event_workspace,
    )
from .passage_evidence import PassageEvidenceAssociationStore
from .passage_receiver import (
        DEFAULT_HOST,
        DEFAULT_PORT,
        PassageEvent,
        PassageEventReceiver,
        PassageEventStore,
        RaceFocus,
    )
from .passage_review import PassageReviewDialog, source_location
from .point_playback import PointPlaybackUnavailable, prepare_point_playback
from .preflight import (
        PreflightJournal,
        PreflightRun,
        local_ipv4_addresses,
        validate_event_network,
    )
from .racetiger_source import RaceTigerClient, RaceTigerSource, RaceTigerStatus
from .race_metadata import RaceMetadata, RaceMetadataStore
from .review_export import export_review_summary
from .review_recorder import (
        ArchiveTimelinePublisher,
        DirectShowVideoDevice,
        FfmpegReviewRecorder,
        PassageReviewCoordinator,
        PassageReviewState,
        PassageReviewTimelinePublisher,
        PassageReviewWindow,
        ReviewRingBuffer,
        discover_directshow_video_device_choices,
        is_supported_review_source,
        load_archive_recording_sessions,
        make_directshow_source,
        parse_directshow_source,
    )
from .stream_recorder import (
        apply_rtsp_credentials,
        find_ffmpeg_executable,
        is_rtsp_source,
        RecordingError,
        sanitize_recording_message,
        split_rtsp_credentials,
    )
from .video_timeline import (
        DEFAULT_CLOCK_SOURCE,
        DEFAULT_TIMING_ERROR_MS,
        PassageVideoLocation,
        PassageVideoLookup,
        VideoTimelineStore,
    )
from .video_playback import VideoPlaybackDialog
from .visual_crossing import (
    CrossingConfig,
    VisualCrossingEvent,
    VisualCrossingEventStore,
    VisualCrossingWorker,
    VisualLineCalibrationDialog,
)


logger = logging.getLogger("FinishReview")
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


def _open_event_directory(event_dir: Path) -> bool:
    if IS_WINDOWS:
        try:
            subprocess.Popen(["explorer.exe", "/n,", str(event_dir)])
        except OSError:
            logger.exception("Failed to launch Windows Explorer")
        else:
            return True
    return QDesktopServices.openUrl(QUrl.fromLocalFile(str(event_dir)))


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


@dataclass(frozen=True, slots=True)
class FinishReviewSettings:
    source: str
    output_dir: Path
    passage_host: str
    passage_port: int
    camera_index: int
    secondary_source: str = ""
    high_speed_dir: Path | None = None
    finishreview_ip: str = "192.168.50.10"
    cyclerace_ip: str = "192.168.50.20"
    high_speed_pc_ip: str = "192.168.50.30"
    switch_ip: str = "192.168.50.2"
    timing_provider: str = "cyclerace"
    racetiger_base_url: str = ""
    racetiger_pc: str = ""
    racetiger_rid: str = ""
    racetiger_token: str = ""
    racetiger_poll_interval_seconds: float = 2.0
    visual_detection_enabled: bool = True
    visual_camera_index: int = 1
    visual_finish_line: float = 0.50
    visual_gate_width: float = 0.08
    visual_forward_direction: str = "left_to_right"
    visual_roi_top: float = 0.08
    visual_roi_bottom: float = 0.95


class _RtspProbeWorker(QThread):
    probe_finished = pyqtSignal(bool, str)

    def __init__(self, source: str, ffmpeg_path: Path | None, parent=None):
        super().__init__(parent)
        self.source = str(source).strip()
        self.ffmpeg_path = Path(ffmpeg_path).resolve() if ffmpeg_path else None
        self._process: subprocess.Popen | None = None

    def cancel(self) -> None:
        self.requestInterruption()
        process = self._process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass

    def run(self) -> None:
        ffmpeg_path = self.ffmpeg_path or find_ffmpeg_executable()
        if ffmpeg_path is None:
            self.probe_finished.emit(False, "未找到FFmpeg")
            return
        command = [
            str(ffmpeg_path),
            "-hide_banner",
            "-loglevel",
            "error",
            "-rtsp_transport",
            "tcp",
            "-i",
            self.source,
            "-map",
            "0:v:0",
            "-frames:v",
            "1",
            "-an",
            "-f",
            "null",
            "-",
        ]
        kwargs = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.PIPE,
        }
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if creation_flags:
            kwargs["creationflags"] = creation_flags
        try:
            process = subprocess.Popen(command, **kwargs)
            self._process = process
            deadline = time.monotonic() + 8.0
            while True:
                if self.isInterruptionRequested():
                    self._terminate_process(process)
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._terminate_process(process)
                    self.probe_finished.emit(False, "8秒内没有读取到画面")
                    return
                try:
                    _stdout, stderr = process.communicate(timeout=min(0.1, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue
        except OSError as error:
            self.probe_finished.emit(False, sanitize_recording_message(error))
            return
        finally:
            self._process = None
        if process.returncode == 0:
            self.probe_finished.emit(True, "已读取到RTSP画面")
            return
        detail = (stderr or b"").decode("utf-8", errors="replace").strip()
        self.probe_finished.emit(
            False,
            sanitize_recording_message(detail or f"FFmpeg退出代码 {process.returncode}"),
        )

    @staticmethod
    def _terminate_process(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        try:
            process.terminate()
            process.communicate(timeout=1.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
        except OSError:
            pass


class EventWorkspacePickerDialog(QDialog):
    """Compact picker for saved CycleRace event workspaces."""

    def __init__(
        self,
        workspaces: tuple[EventWorkspaceDescriptor, ...],
        parent=None,
        *,
        current_dir: Path | None = None,
        summary_provider: Callable[
            [EventWorkspaceDescriptor], EventWorkspaceSummary
        ] = summarize_event_workspace,
    ):
        super().__init__(parent)
        self.setWindowTitle("打开赛事")
        self.setMinimumSize(720, 430)
        self.resize(820, 500)
        self._workspaces = tuple(workspaces)
        self._summary_provider = summary_provider
        self._selected_path: Path | None = None
        self._summary_cache: dict[Path, EventWorkspaceSummary | str] = {}
        current_path = current_dir.resolve() if current_dir is not None else None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        title = QLabel("已保存赛事", self)
        title.setStyleSheet("font-size: 14pt; font-weight: 700; color: #17212b;")
        layout.addWidget(title)

        self.search_edit = QLineEdit(self)
        self.search_edit.setPlaceholderText("搜索赛事或赛段")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self._filter_rows)
        layout.addWidget(self.search_edit)

        self.table = QTableWidget(len(self._workspaces), 4, self)
        self.table.setHorizontalHeaderLabels(("赛事名称", "赛段", "最后更新", "状态"))
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.itemSelectionChanged.connect(self._update_selection)
        self.table.cellDoubleClicked.connect(self._open_selected_row)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        selected_row = -1
        for row, workspace in enumerate(self._workspaces):
            modified = datetime.fromtimestamp(
                workspace.modified_at_ms / 1000.0
            ).strftime("%Y-%m-%d %H:%M") if workspace.modified_at_ms else "--"
            is_current = current_path is not None and workspace.path == current_path
            values = (
                workspace.race_name,
                workspace.stage_name,
                modified,
                "当前打开" if is_current else "已保存",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.UserRole, str(workspace.path))
                    item.setToolTip(str(workspace.path))
                if column in {2, 3}:
                    item.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(row, column, item)
            if is_current:
                selected_row = row
        layout.addWidget(self.table, 1)

        self.summary_label = QLabel("选择一个赛事", self)
        self.summary_label.setStyleSheet("color: #526170; font-weight: 600;")
        layout.addWidget(self.summary_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Open | QDialogButtonBox.Cancel,
            parent=self,
        )
        self.open_button = buttons.button(QDialogButtonBox.Open)
        self.open_button.setText("打开赛事")
        self.open_button.setEnabled(False)
        self.cancel_button = buttons.button(QDialogButtonBox.Cancel)
        self.cancel_button.setText("取消")
        buttons.accepted.connect(self._accept_selected)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        if selected_row < 0 and self._workspaces:
            selected_row = 0
        if selected_row >= 0:
            self.table.selectRow(selected_row)
        elif not self._workspaces:
            self.summary_label.setText("当前保存根目录中没有可打开的赛事")

    @property
    def selected_path(self) -> Path | None:
        return self._selected_path

    def _workspace_for_row(self, row: int) -> EventWorkspaceDescriptor | None:
        if row < 0 or row >= len(self._workspaces):
            return None
        return self._workspaces[row]

    def _filter_rows(self, text: str) -> None:
        query = str(text).strip().casefold()
        first_visible = -1
        for row, workspace in enumerate(self._workspaces):
            searchable = " ".join(
                (workspace.race_name, workspace.stage_name, workspace.path.name)
            ).casefold()
            hidden = bool(query and query not in searchable)
            self.table.setRowHidden(row, hidden)
            if not hidden and first_visible < 0:
                first_visible = row
        if first_visible >= 0:
            self.table.selectRow(first_visible)
        else:
            self.table.clearSelection()
            self.summary_label.setText("没有匹配的赛事")

    def _update_selection(self) -> None:
        rows = self.table.selectionModel().selectedRows()
        workspace = self._workspace_for_row(rows[0].row()) if rows else None
        self._selected_path = workspace.path if workspace is not None else None
        self.open_button.setEnabled(workspace is not None)
        if workspace is None:
            return
        summary = self._summary_cache.get(workspace.path)
        if summary is None:
            try:
                summary = self._summary_provider(workspace)
            except EventWorkspaceError as error:
                summary = str(error)
            self._summary_cache[workspace.path] = summary
        if isinstance(summary, str):
            self.summary_label.setText(summary)
            self.summary_label.setStyleSheet("color: #b54747; font-weight: 600;")
            self.open_button.setEnabled(False)
            return
        self.summary_label.setStyleSheet("color: #526170; font-weight: 600;")
        self.summary_label.setText(
            f"通过记录 {summary.passage_count:,} 条 · "
            f"已确认 {summary.confirmed_count:,} 条"
        )

    def _open_selected_row(self, row: int, _column: int) -> None:
        if self._workspace_for_row(row) is None:
            return
        self.table.selectRow(row)
        self._accept_selected()

    def _accept_selected(self) -> None:
        if self._selected_path is not None and self.open_button.isEnabled():
            self.accept()


class FinishReviewLaunchDialog(QDialog):
    """Operator-facing device and race-directory settings."""

    def __init__(
        self,
        settings: FinishReviewSettings,
        parent=None,
        *,
        ffmpeg_path: Path | None = None,
        device_provider: Callable[
            [], tuple[str | DirectShowVideoDevice, ...]
        ]
        | None = None,
        passage_provider: Callable[[], tuple[PassageEvent, ...]] | None = None,
        evidence_provider: Callable[[PassageEvent], tuple[bool, bool, str, str]]
        | None = None,
        runtime_snapshot_provider: Callable[[], dict[str, str]] | None = None,
        event_export_callback: Callable[[], object] | None = None,
        event_workspace_provider: Callable[
            [], tuple[EventWorkspaceDescriptor, ...]
        ]
        | None = None,
        event_workspace_summary_provider: Callable[
            [EventWorkspaceDescriptor], EventWorkspaceSummary
        ]
        | None = None,
        event_open_callback: Callable[[Path], bool] | None = None,
        return_live_event_callback: Callable[[], bool] | None = None,
        recheck_callback: Callable[[], None] | None = None,
        recording_start_callback: Callable[[], bool] | None = None,
        preflight_event_callback: Callable[[PreflightRun], None] | None = None,
        preflight_restore_callback: Callable[[], tuple[bool, str]] | None = None,
        passage_reception_order_provider: Callable[
            [], dict[tuple[str, str, str], int]
        ]
        | None = None,
        local_address_provider: Callable[[], tuple[str, ...]] = local_ipv4_addresses,
        clock_ms: Callable[[], int] | None = None,
    ):
        super().__init__(parent)
        self._source = str(settings.source).strip()
        self._secondary_source = str(settings.secondary_source).strip()
        self._output_dir = Path(settings.output_dir).expanduser().resolve()
        self._passage_host = settings.passage_host
        self._passage_port = settings.passage_port
        self._camera_index = settings.camera_index
        self._visual_detection_enabled = bool(settings.visual_detection_enabled)
        self._visual_camera_index = max(1, int(settings.visual_camera_index))
        self._visual_finish_line = float(settings.visual_finish_line)
        self._visual_gate_width = float(settings.visual_gate_width)
        self._visual_forward_direction = str(settings.visual_forward_direction)
        self._visual_roi_top = float(settings.visual_roi_top)
        self._visual_roi_bottom = float(settings.visual_roi_bottom)
        self._finishreview_ip = str(settings.finishreview_ip).strip()
        self._cyclerace_ip = str(settings.cyclerace_ip).strip()
        self._high_speed_pc_ip = str(settings.high_speed_pc_ip).strip()
        self._switch_ip = str(settings.switch_ip).strip()
        self._high_speed_dir = (
            Path(settings.high_speed_dir).expanduser().absolute()
            if settings.high_speed_dir is not None
            else None
        )
        self._timing_provider = (
            str(settings.timing_provider or "cyclerace").strip().lower()
            if str(settings.timing_provider or "cyclerace").strip().lower()
            in {"cyclerace", "racetiger"}
            else "cyclerace"
        )
        self._racetiger_base_url = str(settings.racetiger_base_url or "").strip()
        self._racetiger_pc = str(settings.racetiger_pc or "").strip()
        self._racetiger_rid = str(settings.racetiger_rid or "").strip()
        self._racetiger_token = str(settings.racetiger_token or "").strip()
        self._racetiger_poll_interval = max(
            0.5,
            float(settings.racetiger_poll_interval_seconds or 2.0),
        )
        self._ffmpeg_path = Path(ffmpeg_path).resolve() if ffmpeg_path else None
        self._detected_device_names: set[str] = set()
        self._device_provider = device_provider or (
            lambda: discover_directshow_video_device_choices(self._ffmpeg_path)
        )
        self._passage_provider = passage_provider or (lambda: ())
        self._evidence_provider = evidence_provider or (
            lambda _event: (False, False, "等待普通录像", "等待高速画面")
        )
        self._runtime_snapshot_provider = runtime_snapshot_provider or (lambda: {})
        self._event_export_callback = event_export_callback
        self._event_workspace_provider = event_workspace_provider
        self._event_workspace_summary_provider = (
            event_workspace_summary_provider or summarize_event_workspace
        )
        self._event_open_callback = event_open_callback
        self._return_live_event_callback = return_live_event_callback
        self._recheck_callback = recheck_callback
        self._recording_start_callback = recording_start_callback or (lambda: True)
        self._preflight_event_callback = preflight_event_callback
        self._preflight_restore_callback = preflight_restore_callback
        self._passage_reception_order_provider = passage_reception_order_provider
        self._local_address_provider = local_address_provider
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000.0))
        self._preflight_run: PreflightRun | None = None
        self._reported_preflight_state: tuple[str, str] = ("", "")
        self._rtsp_probe_worker: _RtspProbeWorker | None = None
        self._secondary_rtsp_probe_worker: _RtspProbeWorker | None = None
        self._pending_dialog_result: int | None = None
        self._rtsp_probe_source = ""
        self._rtsp_probe_ok = False
        self._rtsp_probe_message = ""
        self._secondary_rtsp_probe_source = ""
        self._secondary_rtsp_probe_ok = False
        self._secondary_rtsp_probe_message = ""
        clean_rtsp_source, rtsp_username, rtsp_password = split_rtsp_credentials(
            self._source
        )
        self._clean_rtsp_source = clean_rtsp_source
        self._rtsp_username = rtsp_username
        self._rtsp_password = rtsp_password
        (
            self._clean_secondary_rtsp_source,
            self._secondary_rtsp_username,
            self._secondary_rtsp_password,
        ) = split_rtsp_credentials(self._secondary_source)
        self.setWindowTitle("设备与赛事设置")
        self.setMinimumSize(960, 680)
        self.setModal(True)
        self.setStyleSheet(
            'QDialog { background: #eef2f5; color: #17212b; '
            'font-family: "Microsoft YaHei UI"; font-size: 10pt; }'
            "QLineEdit, QComboBox, QDoubleSpinBox { min-height: 32px; "
            "font-size: 10pt; padding: 0 8px; background: #ffffff; "
            "border: 1px solid #aeb8c2; border-radius: 4px; }"
            "QPushButton { min-height: 32px; padding: 0 12px; font-size: 10pt; "
            "background: #ffffff; "
            "border: 1px solid #aeb8c2; border-radius: 4px; }"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(14)
        title = QLabel("终点设备与赛事设置", self)
        title.setStyleSheet("font-size: 14pt; font-weight: 700;")
        layout.addWidget(title)

        self.tabs = QTabWidget(self)
        self.event_page = QWidget(self.tabs)
        self.deployment_page = QWidget(self.tabs)
        self.devices_page = QWidget(self.tabs)
        self.preflight_page = QWidget(self.tabs)
        self.tabs.addTab(self.event_page, "赛事与保存")
        self.tabs.addTab(self.deployment_page, "部署总览")
        self.tabs.addTab(self.devices_page, "设备设置")
        self.tabs.addTab(self.preflight_page, "赛前联调")
        layout.addWidget(self.tabs, 1)

        self._init_event_page()
        device_layout = QVBoxLayout(self.devices_page)
        device_layout.setContentsMargins(12, 12, 12, 12)
        device_layout.setSpacing(10)

        form = QFormLayout()
        self._device_form = form
        form.setHorizontalSpacing(14)
        # Hidden QFormLayout rows retain spacing on the shipped Qt version.
        form.setVerticalSpacing(4)
        self.timing_provider_combo = QComboBox(self)
        self.timing_provider_combo.addItem("CycleRace", "cyclerace")
        self.timing_provider_combo.addItem("赛虎计时", "racetiger")
        self.timing_provider_combo.setCurrentIndex(
            max(0, self.timing_provider_combo.findData(self._timing_provider))
        )
        self.timing_provider_combo.currentIndexChanged.connect(
            self._refresh_timing_provider_fields
        )
        form.addRow("计时源", self.timing_provider_combo)

        self.racetiger_base_url_edit = QLineEdit(self._racetiger_base_url, self)
        self.racetiger_base_url_edit.setPlaceholderText(
            "https://rqs.racetigertiming.com"
        )
        form.addRow("赛虎接口地址", self.racetiger_base_url_edit)

        self.racetiger_pc_edit = QLineEdit(self._racetiger_pc, self)
        self.racetiger_pc_edit.setPlaceholderText("赛事电脑标识 pc")
        form.addRow("赛虎 PC", self.racetiger_pc_edit)

        self.racetiger_rid_edit = QLineEdit(self._racetiger_rid, self)
        self.racetiger_rid_edit.setPlaceholderText("赛事 RID")
        form.addRow("赛虎赛事 RID", self.racetiger_rid_edit)

        self.racetiger_token_edit = QLineEdit(self._racetiger_token, self)
        self.racetiger_token_edit.setEchoMode(QLineEdit.Password)
        self.racetiger_token_edit.setPlaceholderText("本机保存，不显示明文")
        form.addRow("赛虎令牌", self.racetiger_token_edit)

        self.racetiger_poll_interval_spin = QDoubleSpinBox(self)
        self.racetiger_poll_interval_spin.setRange(0.5, 60.0)
        self.racetiger_poll_interval_spin.setSingleStep(0.5)
        self.racetiger_poll_interval_spin.setDecimals(1)
        self.racetiger_poll_interval_spin.setSuffix(" 秒")
        self.racetiger_poll_interval_spin.setValue(self._racetiger_poll_interval)
        form.addRow("赛虎读取间隔", self.racetiger_poll_interval_spin)

        racetiger_hint = QLabel(
            "选择赛虎后，终点列表只读取赛虎 FINISH 记录；视频仍只用于人工复核，"
            "不会回写赛虎或 CycleRace 正式成绩。",
            self,
        )
        racetiger_hint.setWordWrap(True)
        racetiger_hint.setStyleSheet("color: #667085;")
        form.addRow("", racetiger_hint)
        self.racetiger_controls = (
            self.racetiger_base_url_edit,
            self.racetiger_pc_edit,
            self.racetiger_rid_edit,
            self.racetiger_token_edit,
            self.racetiger_poll_interval_spin,
            racetiger_hint,
        )

        self.source_type_combo = QComboBox(self)
        self.source_type_combo.addItem("本机USB/Type-C摄像头", "usb")
        self.source_type_combo.addItem("RTSP网络摄像头", "rtsp")
        self.source_type_combo.setCurrentIndex(
            1 if is_rtsp_source(self._source) else 0
        )
        self.source_type_combo.currentIndexChanged.connect(
            self._refresh_source_fields
        )
        form.addRow("机位1连接", self.source_type_combo)

        self.rtsp_address_edit = QLineEdit(
            self._clean_rtsp_source if is_rtsp_source(self._source) else "",
            self,
        )
        self.rtsp_address_edit.setPlaceholderText("rtsp://192.168.50.101/stream")
        self.rtsp_address_edit.textChanged.connect(self._invalidate_rtsp_probe)
        form.addRow("RTSP地址", self.rtsp_address_edit)

        self.rtsp_username_edit = QLineEdit(self._rtsp_username, self)
        self.rtsp_username_edit.setPlaceholderText("只读录像账号")
        self.rtsp_username_edit.textChanged.connect(self._invalidate_rtsp_probe)
        rtsp_credentials_row = QHBoxLayout()
        self.rtsp_password_row = rtsp_credentials_row
        rtsp_credentials_row.setSpacing(6)
        rtsp_credentials_row.addWidget(QLabel("用户", self))
        rtsp_credentials_row.addWidget(self.rtsp_username_edit, 1)
        self.rtsp_password_edit = QLineEdit(self._rtsp_password, self)
        self.rtsp_password_edit.setEchoMode(QLineEdit.Password)
        self.rtsp_password_edit.setPlaceholderText("使用Windows用户加密保存")
        self.rtsp_password_edit.textChanged.connect(self._invalidate_rtsp_probe)
        rtsp_credentials_row.addWidget(QLabel("密码", self))
        rtsp_credentials_row.addWidget(self.rtsp_password_edit, 2)
        self.rtsp_test_button = QPushButton("测试画面", self)
        self.rtsp_test_button.clicked.connect(self._test_rtsp_source)
        rtsp_credentials_row.addWidget(self.rtsp_test_button)
        form.addRow("RTSP凭据", rtsp_credentials_row)

        secondary_device_row = QHBoxLayout()
        self.secondary_device_row = secondary_device_row
        secondary_device_row.setSpacing(6)
        self.secondary_enabled_checkbox = QCheckBox("启用", self)
        self.secondary_enabled_checkbox.setChecked(
            is_supported_review_source(self._secondary_source)
        )
        self.secondary_enabled_checkbox.toggled.connect(self._refresh_source_fields)
        self.secondary_rtsp_enabled_checkbox = self.secondary_enabled_checkbox
        secondary_device_row.addWidget(self.secondary_enabled_checkbox)
        self.secondary_source_type_combo = QComboBox(self)
        self.secondary_source_type_combo.addItem("USB/Type-C", "usb")
        self.secondary_source_type_combo.addItem("RTSP", "rtsp")
        self.secondary_source_type_combo.setCurrentIndex(
            1 if is_rtsp_source(self._secondary_source) else 0
        )
        self.secondary_source_type_combo.currentIndexChanged.connect(
            self._refresh_source_fields
        )
        secondary_device_row.addWidget(self.secondary_source_type_combo)
        self.secondary_device_combo = QComboBox(self)
        self.secondary_device_combo.setMinimumWidth(280)
        self.secondary_device_combo.currentIndexChanged.connect(
            self._refresh_camera_status
        )
        secondary_device_row.addWidget(self.secondary_device_combo, 1)
        self.secondary_detect_button = QPushButton("重新检测", self)
        self.secondary_detect_button.clicked.connect(self._refresh_devices)
        secondary_device_row.addWidget(self.secondary_detect_button)
        form.addRow("普通机位2", secondary_device_row)

        self.secondary_rtsp_address_edit = QLineEdit(
            self._clean_secondary_rtsp_source,
            self,
        )
        self.secondary_rtsp_address_edit.setPlaceholderText(
            "rtsp://192.168.50.102/stream"
        )
        self.secondary_rtsp_address_edit.textChanged.connect(
            self._invalidate_secondary_rtsp_probe
        )
        form.addRow("机位2 RTSP地址", self.secondary_rtsp_address_edit)

        self.secondary_rtsp_username_edit = QLineEdit(
            self._secondary_rtsp_username,
            self,
        )
        self.secondary_rtsp_username_edit.setPlaceholderText("只读录像账号")
        self.secondary_rtsp_username_edit.textChanged.connect(
            self._invalidate_secondary_rtsp_probe
        )
        secondary_rtsp_credentials_row = QHBoxLayout()
        self.secondary_rtsp_password_row = secondary_rtsp_credentials_row
        secondary_rtsp_credentials_row.setSpacing(6)
        secondary_rtsp_credentials_row.addWidget(QLabel("用户", self))
        secondary_rtsp_credentials_row.addWidget(
            self.secondary_rtsp_username_edit,
            1,
        )
        self.secondary_rtsp_password_edit = QLineEdit(
            self._secondary_rtsp_password,
            self,
        )
        self.secondary_rtsp_password_edit.setEchoMode(QLineEdit.Password)
        self.secondary_rtsp_password_edit.setPlaceholderText(
            "使用Windows用户加密保存"
        )
        self.secondary_rtsp_password_edit.textChanged.connect(
            self._invalidate_secondary_rtsp_probe
        )
        secondary_rtsp_credentials_row.addWidget(QLabel("密码", self))
        secondary_rtsp_credentials_row.addWidget(
            self.secondary_rtsp_password_edit,
            2,
        )
        self.secondary_rtsp_test_button = QPushButton("测试画面", self)
        self.secondary_rtsp_test_button.clicked.connect(
            self._test_secondary_rtsp_source
        )
        secondary_rtsp_credentials_row.addWidget(self.secondary_rtsp_test_button)
        form.addRow("机位2 RTSP凭据", secondary_rtsp_credentials_row)

        device_row = QHBoxLayout()
        self.device_row = device_row
        device_row.setSpacing(6)
        self.device_combo = QComboBox(self)
        self.device_combo.setMinimumWidth(360)
        self.device_combo.currentIndexChanged.connect(self._refresh_camera_status)
        device_row.addWidget(self.device_combo, 1)
        self.detect_button = QPushButton("重新检测", self)
        self.detect_button.clicked.connect(self._refresh_devices)
        device_row.addWidget(self.detect_button)
        form.addRow("机位1 USB设备", device_row)

        self.video_size_combo = QComboBox(self)
        self.video_size_combo.addItem("自动", None)
        for value in ("1920x1080", "2560x1440", "3840x2160"):
            self.video_size_combo.addItem(value, value)
        form.addRow("USB录像分辨率", self.video_size_combo)

        self.framerate_combo = QComboBox(self)
        self.framerate_combo.addItem("自动", None)
        for value in (25.0, 30.0, 50.0, 60.0):
            self.framerate_combo.addItem(f"{value:g} FPS", value)
        form.addRow("USB录像帧率", self.framerate_combo)

        visual_settings_row = QHBoxLayout()
        visual_settings_row.setSpacing(6)
        self.visual_enabled_checkbox = QCheckBox("启用", self)
        self.visual_enabled_checkbox.setChecked(self._visual_detection_enabled)
        visual_settings_row.addWidget(self.visual_enabled_checkbox)
        self.visual_camera_combo = QComboBox(self)
        self.visual_camera_combo.addItem("机位1", self._camera_index)
        self.visual_camera_combo.addItem("机位2", self._camera_index + 1)
        self.visual_camera_combo.setCurrentIndex(
            max(0, self.visual_camera_combo.findData(self._visual_camera_index))
        )
        visual_settings_row.addWidget(self.visual_camera_combo)
        self.visual_line_label = QLabel(
            f"终点线 {self._visual_finish_line * 100:.1f}%",
            self,
        )
        visual_settings_row.addWidget(self.visual_line_label, 1)
        self.visual_calibrate_button = QPushButton("在画面上设置", self)
        self.visual_calibrate_button.clicked.connect(self._calibrate_visual_line)
        visual_settings_row.addWidget(self.visual_calibrate_button)
        self.visual_direction_combo = QComboBox(self)
        self.visual_direction_combo.addItem("正向：左 → 右", "left_to_right")
        self.visual_direction_combo.addItem("正向：右 → 左", "right_to_left")
        self.visual_direction_combo.setCurrentIndex(
            max(0, self.visual_direction_combo.findData(self._visual_forward_direction))
        )
        visual_settings_row.addWidget(self.visual_direction_combo)
        self.visual_candidates_settings_button = QPushButton("候选事件：0 条", self)
        self.visual_candidates_settings_button.setToolTip(
            "打开视觉过线候选，逐条回放确认是否为真实运动员或芯片漏读"
        )
        review_window = self.parent()
        open_candidates = getattr(review_window, "_open_visual_candidates", None)
        if callable(open_candidates):
            self.visual_candidates_settings_button.setText(
                f"候选事件：{int(getattr(review_window, '_visual_event_count', 0))} 条"
            )
            self.visual_candidates_settings_button.clicked.connect(open_candidates)
        else:
            self.visual_candidates_settings_button.setEnabled(False)
        self.visual_candidates_settings_button.setMinimumWidth(132)
        visual_settings_row.addWidget(self.visual_candidates_settings_button)
        form.addRow("过线辅助", visual_settings_row)
        high_speed_row = QHBoxLayout()
        high_speed_row.setSpacing(6)
        self.high_speed_edit = QLineEdit(
            str(self._high_speed_dir) if self._high_speed_dir is not None else "",
            self,
        )
        self.high_speed_enabled_checkbox = QCheckBox("启用高速摄像", self)
        self.high_speed_enabled_checkbox.setChecked(self._high_speed_dir is not None)
        self.high_speed_enabled_checkbox.toggled.connect(self._refresh_source_fields)
        form.addRow("高速摄像", self.high_speed_enabled_checkbox)
        self.high_speed_edit.setPlaceholderText(r"\\高速摄像电脑\AuyatData")
        self.high_speed_edit.setToolTip(
            "正式比赛请填写另一台高速摄像电脑的只读共享目录，"
            "例如 \\\\FINISH-RGB\\AuyatData"
        )
        high_speed_row.addWidget(self.high_speed_edit, 1)
        high_speed_browse_button = QPushButton(self)
        self.high_speed_browse_button = high_speed_browse_button
        high_speed_browse_button.setIcon(
            self.style().standardIcon(QStyle.SP_DirOpenIcon)
        )
        high_speed_browse_button.setToolTip("选择高速摄像电脑的局域网共享目录")
        high_speed_browse_button.setFixedWidth(42)
        high_speed_browse_button.clicked.connect(self._browse_high_speed_dir)
        high_speed_row.addWidget(high_speed_browse_button)
        form.addRow("高速电脑共享目录", high_speed_row)
        high_speed_hint = QLabel(
            "正式比赛从另一台高速摄像电脑读取；本机目录仅用于单机测试。",
            self,
        )
        high_speed_hint.setStyleSheet("color: #667085;")
        high_speed_hint.setWordWrap(True)
        form.addRow("", high_speed_hint)

        cycle_status = QLabel(
            f"自动发现本机“{socket.gethostname()}”，"
            "同机或局域网电脑都无需共享目录、无需填写IP。"
            "当前兼容模式未启用认证，仅限受信任赛事局域网。",
            self,
        )
        cycle_status.setStyleSheet("color: #a56300; font-weight: 600;")
        cycle_status.setWordWrap(True)
        form.addRow("CycleRace", cycle_status)
        self.camera_status_label = QLabel(self)
        self.camera_status_label.setObjectName("recordingDeviceStatus")
        form.addRow("设备检查", self.camera_status_label)
        device_layout.addLayout(form)
        device_layout.addStretch(1)

        self._init_deployment_page()
        self._init_preflight_page()

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel,
            parent=self,
        )
        self.start_button = buttons.button(QDialogButtonBox.Ok)
        self.start_button.setText("保存设置")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self._accept_settings)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._refresh_timing_provider_fields()
        self._refresh_devices()
        self._refresh_source_fields()
        self._dialog_timer = QTimer(self)
        self._dialog_timer.setInterval(500)
        self._dialog_timer.timeout.connect(self._refresh_live_pages)
        self._dialog_timer.start()
        self._refresh_live_pages()

    def _init_event_page(self) -> None:
        page_layout = QVBoxLayout(self.event_page)
        page_layout.setContentsMargins(12, 12, 12, 12)
        page_layout.setSpacing(12)
        self._event_dir: Path | None = None

        status_row = QHBoxLayout()
        status_row.setSpacing(8)
        self.event_status_label = QLabel("等待 CycleRace 赛事信息", self)
        self.event_status_label.setStyleSheet(
            "color: #a56300; font-size: 11pt; font-weight: 700;"
        )
        status_row.addWidget(self.event_status_label)
        status_row.addStretch(1)
        self.return_live_event_button = QPushButton("返回当前赛事", self)
        self.return_live_event_button.clicked.connect(self._return_to_live_event)
        self.return_live_event_button.setVisible(False)
        status_row.addWidget(self.return_live_event_button)
        page_layout.addLayout(status_row)

        form = QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)

        self.event_name_edit = QLineEdit(self)
        self.event_name_edit.setReadOnly(True)
        event_name_row = QHBoxLayout()
        event_name_row.setSpacing(6)
        event_name_row.addWidget(self.event_name_edit, 1)
        self.open_saved_event_button = QPushButton("打开赛事", self)
        self.open_saved_event_button.setIcon(
            self.style().standardIcon(QStyle.SP_DialogOpenButton)
        )
        self.open_saved_event_button.clicked.connect(self._open_saved_event)
        event_name_row.addWidget(self.open_saved_event_button)
        form.addRow("当前赛事", event_name_row)

        self.event_stage_edit = QLineEdit(self)
        self.event_stage_edit.setReadOnly(True)
        form.addRow("当前赛段", self.event_stage_edit)

        event_dir_row = QHBoxLayout()
        event_dir_row.setSpacing(6)
        self.event_dir_edit = QLineEdit(self)
        self.event_dir_edit.setReadOnly(True)
        self.event_dir_edit.setCursorPosition(0)
        event_dir_row.addWidget(self.event_dir_edit, 1)
        self.open_event_dir_button = QPushButton(self)
        self.open_event_dir_button.setIcon(
            self.style().standardIcon(QStyle.SP_DirOpenIcon)
        )
        self.open_event_dir_button.setToolTip("打开当前赛事目录")
        self.open_event_dir_button.setFixedWidth(42)
        self.open_event_dir_button.clicked.connect(self._open_event_dir)
        event_dir_row.addWidget(self.open_event_dir_button)
        form.addRow("当前赛事目录", event_dir_row)

        output_row = QHBoxLayout()
        output_row.setSpacing(6)
        self.output_edit = QLineEdit(str(self._output_dir), self)
        self.output_edit.setReadOnly(True)
        self.output_edit.setCursorPosition(0)
        self.output_edit.setToolTip(
            "CycleRace发送赛事信息后，将在此目录下自动创建赛事名称文件夹"
        )
        output_row.addWidget(self.output_edit, 1)
        browse_button = QPushButton(self)
        browse_button.setIcon(self.style().standardIcon(QStyle.SP_DirOpenIcon))
        browse_button.setToolTip("选择赛事保存根目录")
        browse_button.setFixedWidth(42)
        browse_button.clicked.connect(self._browse_output_dir)
        output_row.addWidget(browse_button)
        form.addRow("赛事保存根目录", output_row)

        page_layout.addLayout(form)
        page_layout.addStretch(1)

    def _init_deployment_page(self) -> None:
        page_layout = QVBoxLayout(self.deployment_page)
        page_layout.setContentsMargins(12, 12, 12, 12)
        page_layout.setSpacing(10)

        network_grid = QGridLayout()
        network_grid.setHorizontalSpacing(10)
        network_grid.setVerticalSpacing(8)
        self.finishreview_ip_edit = QLineEdit(self._finishreview_ip, self)
        self.cyclerace_ip_edit = QLineEdit(self._cyclerace_ip, self)
        self.high_speed_pc_ip_edit = QLineEdit(self._high_speed_pc_ip, self)
        self.switch_ip_edit = QLineEdit(self._switch_ip, self)
        fields = (
            ("本机FinishReview", self.finishreview_ip_edit),
            ("CycleRace电脑", self.cyclerace_ip_edit),
            ("Auyat高速电脑", self.high_speed_pc_ip_edit),
            ("PoE交换机", self.switch_ip_edit),
        )
        for index, (label, field) in enumerate(fields):
            row = index // 2
            column = (index % 2) * 2
            network_grid.addWidget(QLabel(label, self), row, column)
            network_grid.addWidget(field, row, column + 1)
        page_layout.addLayout(network_grid)

        device_note = QLabel(
            "赛事网卡统一使用掩码 255.255.255.0，网关和DNS留空。"
            "CycleRace直连网卡可设 192.168.1.10（芯片 192.168.1.254）；"
            "Auyat直连网卡可设 192.168.0.10（高速设备 192.168.0.254），"
            "两张直连网卡都不设网关。",
            self,
        )
        device_note.setStyleSheet("color: #667085;")
        device_note.setWordWrap(True)
        page_layout.addWidget(device_note)

        self.deployment_table = QTableWidget(0, 5, self)
        self.deployment_table.setHorizontalHeaderLabels(
            ("来源", "所在位置", "连接地址", "状态", "说明")
        )
        self.deployment_table.verticalHeader().setVisible(False)
        self.deployment_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.deployment_table.setSelectionMode(QTableWidget.NoSelection)
        header = self.deployment_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.Stretch)
        page_layout.addWidget(self.deployment_table, 1)

        actions = QHBoxLayout()
        actions.addStretch(1)
        self.deployment_recheck_button = QPushButton("重新检查", self)
        self.deployment_recheck_button.clicked.connect(self._request_recheck)
        actions.addWidget(self.deployment_recheck_button)
        page_layout.addLayout(actions)

    def _init_preflight_page(self) -> None:
        page_layout = QVBoxLayout(self.preflight_page)
        page_layout.setContentsMargins(12, 12, 12, 12)
        page_layout.setSpacing(10)

        self.preflight_hint = QLabel(
            "启动普通录像后，刷任意测试芯片，并用Auyat拍摄、判读保存；"
            "只接受本次开始后的新记录。",
            self,
        )
        self.preflight_hint.setStyleSheet("color: #667085;")
        self.preflight_hint.setWordWrap(True)
        page_layout.addWidget(self.preflight_hint)

        controls = QHBoxLayout()
        self.preflight_restore_button = QPushButton("恢复最近联调记录", self)
        self.preflight_restore_button.setToolTip(
            "撤销最近一次联调记录的隐藏状态；原始计时记录始终保留"
        )
        self.preflight_restore_button.clicked.connect(
            self._restore_latest_preflight_event
        )
        controls.addWidget(self.preflight_restore_button)
        controls.addStretch(1)
        self.preflight_start_button = QPushButton("启动普通录像并联调", self)
        self.preflight_start_button.clicked.connect(self._start_preflight)
        controls.addWidget(self.preflight_start_button)
        page_layout.addLayout(controls)

        self.preflight_table = QTableWidget(4, 3, self)
        self.preflight_table.setHorizontalHeaderLabels(("检查项", "状态", "详情"))
        self.preflight_table.verticalHeader().setVisible(False)
        self.preflight_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.preflight_table.setSelectionMode(QTableWidget.NoSelection)
        for row, label in enumerate(
            ("芯片计时新过线", "普通录像", "高速摄像", "联调结果")
        ):
            self.preflight_table.setItem(row, 0, QTableWidgetItem(label))
        header = self.preflight_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        page_layout.addWidget(self.preflight_table, 1)

        self.preflight_status_label = QLabel(
            "请先启动普通录像联调，再刷任意测试芯片",
            self,
        )
        self.preflight_status_label.setStyleSheet("color: #667085; font-weight: 600;")
        page_layout.addWidget(self.preflight_status_label)
        self._update_preflight_table()

    def _refresh_live_pages(self) -> None:
        snapshot = dict(self._runtime_snapshot_provider() or {})
        self._refresh_event_page(snapshot)
        self._refresh_deployment_table(snapshot)
        self._poll_preflight()

    def _request_recheck(self) -> None:
        if self._recheck_callback is not None:
            self._recheck_callback()
        self._refresh_live_pages()

    def _refresh_event_page(self, snapshot: dict[str, str] | None = None) -> None:
        snapshot = dict(snapshot or self._runtime_snapshot_provider() or {})
        timing_provider = str(
            self.timing_provider_combo.currentData() or "cyclerace"
        )
        runtime_matches_selection = (
            not snapshot.get("timing_provider")
            or snapshot.get("timing_provider") == timing_provider
        )
        if timing_provider == "racetiger":
            event_name = (
                snapshot.get("event_name", "")
                if runtime_matches_selection
                else ""
            ) or self.racetiger_rid_edit.text().strip()
            event_stage = (
                snapshot.get("event_stage", "")
                if runtime_matches_selection
                else ""
            ) or "终点"
            event_dir = (
                snapshot.get("event_dir", "")
                if runtime_matches_selection
                else ""
            ) or str(self._output_dir)
            event_state = (
                snapshot.get("event_state", "")
                if runtime_matches_selection
                else ""
            ) or (
                "已配置" if event_name else "等待赛虎赛事 RID"
            )
        else:
            event_name = (
                snapshot.get("event_name", "") if runtime_matches_selection else ""
            )
            event_stage = (
                snapshot.get("event_stage", "") if runtime_matches_selection else ""
            )
            event_dir = (
                snapshot.get("event_dir", "") if runtime_matches_selection else ""
            )
            event_state = (
                snapshot.get("event_state", "") if runtime_matches_selection else ""
            ) or "等待 CycleRace 赛事信息"

        has_event = bool(event_name and event_dir)
        workspace_mode = snapshot.get("workspace_mode", "live")
        is_archive = workspace_mode == "archive"
        recording_active = snapshot.get("recording_active", "") == "1"
        self.event_status_label.setText(event_state)
        self.event_status_label.setStyleSheet(
            "color: #a56300; font-size: 11pt; font-weight: 700;"
            if is_archive
            else "color: #247a52; font-size: 11pt; font-weight: 700;"
            if has_event
            else "color: #a56300; font-size: 11pt; font-weight: 700;"
        )
        self.event_name_edit.setText(event_name or "--")
        self.event_stage_edit.setText(event_stage or "--")
        self.event_dir_edit.setText(event_dir or "等待赛事信息")
        self.event_dir_edit.setToolTip(event_dir)
        self.event_dir_edit.setCursorPosition(0)
        self._event_dir = Path(event_dir) if event_dir else None
        self.open_event_dir_button.setEnabled(self._event_dir is not None)
        can_open_saved = bool(
            timing_provider == "cyclerace"
            and self._event_workspace_provider is not None
            and self._event_open_callback is not None
            and not recording_active
        )
        self.open_saved_event_button.setEnabled(can_open_saved)
        self.open_saved_event_button.setToolTip(
            "停止录像后可打开历史赛事"
            if recording_active
            else "打开已保存的 CycleRace 赛事"
        )
        self.return_live_event_button.setVisible(
            is_archive and timing_provider == "cyclerace"
        )

    def _refresh_deployment_table(
        self,
        snapshot: dict[str, str] | None = None,
    ) -> None:
        snapshot = dict(snapshot or self._runtime_snapshot_provider() or {})
        expected_ip = self.finishreview_ip_edit.text().strip()
        local_addresses = tuple(self._local_address_provider())
        if expected_ip and expected_ip in local_addresses:
            local_state = "通过"
            local_detail = "本机赛事网卡地址正确"
        elif local_addresses:
            local_state = "待处理"
            local_detail = "本机地址：" + "、".join(local_addresses)
        else:
            local_state = "异常"
            local_detail = "未检测到本机IPv4地址"
        camera_addresses = [
            self.rtsp_address_edit.text().strip()
            if self.source_type_combo.currentData() == "rtsp"
            else self.device_combo.currentText()
        ]
        if self.secondary_enabled_checkbox.isChecked():
            camera_addresses.append(
                self.secondary_rtsp_address_edit.text().strip()
                if self.secondary_source_type_combo.currentData() == "rtsp"
                else self.secondary_device_combo.currentText()
            )
        camera_address = " / ".join(value for value in camera_addresses if value)
        timing_provider = str(
            self.timing_provider_combo.currentData() or "cyclerace"
        )
        timing_label = "赛虎计时" if timing_provider == "racetiger" else "CycleRace"
        timing_location = "云端接口" if timing_provider == "racetiger" else "计时电脑"
        timing_address = (
            self.racetiger_base_url_edit.text().strip()
            if timing_provider == "racetiger"
            else self.cyclerace_ip_edit.text().strip()
        )
        rows = (
            (
                "FinishReview",
                "本机",
                expected_ip or "未填写",
                local_state,
                local_detail,
            ),
            (
                timing_label,
                timing_location,
                timing_address or "未填写",
                snapshot.get(
                    "timing_state",
                    snapshot.get("cycle_state", "待检查"),
                ),
                snapshot.get(
                    "timing_detail",
                    snapshot.get("cycle_detail", "等待任意测试芯片新过线"),
                ),
            ),
            (
                "普通录像",
                "本机/PoE交换机",
                camera_address or "未配置",
                snapshot.get("camera_state", "待检查"),
                snapshot.get("camera_detail", "保存并启动录像后验证"),
            ),
            (
                "Auyat高速",
                "高速电脑",
                self.high_speed_edit.text().strip() or "已关闭"
                if not self.high_speed_enabled_checkbox.isChecked()
                else self.high_speed_edit.text().strip() or "未配置",
                "已关闭"
                if not self.high_speed_enabled_checkbox.isChecked()
                else snapshot.get("high_speed_state", "待检查"),
                "高速摄像未启用"
                if not self.high_speed_enabled_checkbox.isChecked()
                else snapshot.get("high_speed_detail", "等待共享目录检查"),
            ),
            (
                "PoE交换机",
                "赛事网络",
                self.switch_ip_edit.text().strip() or "未填写",
                "人工确认",
                "同一VLAN，关闭端口隔离",
            ),
        )
        self.deployment_table.setRowCount(len(rows))
        for row_index, row_values in enumerate(rows):
            for column, value in enumerate(row_values):
                item = QTableWidgetItem(str(value))
                item.setToolTip(str(value))
                if column == 3:
                    color = {
                        "通过": "#247a52",
                        "异常": "#b54747",
                        "待处理": "#a56300",
                        "待检查": "#a56300",
                        "人工确认": "#667085",
                    }.get(str(value), "#667085")
                    item.setForeground(QColor(color))
                self.deployment_table.setItem(row_index, column, item)

    def _start_preflight(self) -> None:
        selected_source = self._selected_recording_source()
        selected_secondary_source = self._current_secondary_rtsp_source()
        high_speed_enabled = self.high_speed_enabled_checkbox.isChecked()
        selected_high_speed = (
            self.high_speed_edit.text().strip() if high_speed_enabled else ""
        )
        if not selected_source:
            QMessageBox.warning(self, "无法开始联调", "请先配置普通录像源")
            return
        if high_speed_enabled and not selected_high_speed:
            QMessageBox.warning(self, "无法开始联调", "请先配置Auyat高速共享目录")
            return
        current_high_speed = str(self._high_speed_dir or "")
        current_high_speed = current_high_speed if high_speed_enabled else ""
        if (
            selected_source != self._source
            or selected_secondary_source != self._secondary_source
            or selected_high_speed != current_high_speed
        ):
            QMessageBox.warning(
                self,
                "请先保存设置",
                "录像源或高速共享目录已经修改，请先保存后重新打开赛事联调。",
            )
            return
        try:
            recording_started = bool(self._recording_start_callback())
        except Exception as error:  # noqa: BLE001 - keep startup failure visible.
            recording_started = False
            recording_error = sanitize_recording_message(error)
        else:
            recording_error = ""
        if not recording_started:
            snapshot = dict(self._runtime_snapshot_provider() or {})
            QMessageBox.warning(
                self,
                "普通录像未启动",
                recording_error
                or snapshot.get("camera_detail", "请检查录像设备后重试"),
            )
            return
        events = tuple(self._passage_provider())
        reception_order = (
            self._passage_reception_order_provider()
            if self._passage_reception_order_provider is not None
            else {}
        )
        self._preflight_run = PreflightRun.start(
            events,
            started_at_ms=self._clock_ms(),
            require_regular=True,
            require_high_speed=high_speed_enabled,
            started_receive_sequence=max(
                reception_order.values(),
                default=0,
            ),
        )
        self._reported_preflight_state = ("", "")
        self.preflight_start_button.setText("重新开始联调")
        self.preflight_status_label.setText(
            "普通录像已启动，等待任意测试芯片新过线"
        )
        self._request_recheck()
        self._update_preflight_table()

    def _poll_preflight(self) -> None:
        run = self._preflight_run
        if run is None:
            return
        events = tuple(self._passage_provider())
        received_order = (
            self._passage_reception_order_provider()
            if self._passage_reception_order_provider is not None
            else None
        )
        updated = run.observe(events, received_order=received_order)
        event = next(
            (item for item in events if item.event_id == updated.event_id),
            None,
        )
        regular_detail = "等待普通录像覆盖测试时间点"
        high_speed_detail = "等待Auyat完成判读和保存"
        if event is not None:
            (
                regular_ready,
                high_speed_ready,
                regular_detail,
                high_speed_detail,
            ) = self._evidence_provider(event)
            updated = updated.with_evidence(
                regular_ready=regular_ready,
                high_speed_ready=high_speed_ready,
            )
        self._preflight_run = updated
        self._preflight_regular_detail = regular_detail
        self._preflight_high_speed_detail = high_speed_detail
        report_key = (updated.event_id, updated.status)
        if (
            updated.passed
            and report_key != self._reported_preflight_state
            and self._preflight_event_callback is not None
        ):
            self._preflight_event_callback(updated)
            self._reported_preflight_state = report_key
        self._update_preflight_table()

    def _restore_latest_preflight_event(self) -> None:
        callback = self._preflight_restore_callback
        if callback is None:
            QMessageBox.information(
                self,
                "没有可恢复记录",
                "当前没有可恢复的联调记录。",
            )
            return
        try:
            restored, detail = callback()
        except Exception as error:  # noqa: BLE001 - keep recovery failure visible.
            QMessageBox.warning(
                self,
                "恢复失败",
                sanitize_recording_message(error),
            )
            return
        QMessageBox.information(
            self,
            "已恢复" if restored else "没有可恢复记录",
            detail,
        )

    def _update_preflight_table(self) -> None:
        run = self._preflight_run
        if run is None:
            values = (
                ("等待", "尚未开始"),
                ("等待", "尚未开始"),
                ("等待", "尚未开始"),
                ("未开始", "请启动普通录像联调"),
            )
        else:
            passage_detail = (
                f"{run.bib or run.event_id} 已收到"
                if run.passage_received
                else "只接受开始联调后的新记录"
            )
            regular_required = run.require_regular
            high_speed_required = run.require_high_speed
            values = (
                ("通过" if run.passage_received else "等待", passage_detail),
                (
                    "通过" if run.regular_ready else ("跳过" if not regular_required else "等待"),
                    getattr(self, "_preflight_regular_detail", "等待普通录像"),
                ),
                (
                    "通过" if run.high_speed_ready else ("跳过" if not high_speed_required else "等待"),
                    getattr(self, "_preflight_high_speed_detail", "等待高速画面"),
                ),
                (
                    "通过" if run.passed else "进行中",
                    "赛前联调通过" if run.passed else "等待所有必需来源完成",
                ),
            )
            self.preflight_status_label.setText(
                "赛前联调通过"
                if run.passed
                else (
                    "等待普通录像或高速画面"
                    if run.passage_received
                    else "普通录像已启动，等待任意测试芯片新过线"
                )
            )
            self.preflight_status_label.setStyleSheet(
                "color: #247a52; font-weight: 700;"
                if run.passed
                else "color: #a56300; font-weight: 600;"
            )
        for row, (state, detail) in enumerate(values):
            state_item = QTableWidgetItem(state)
            state_item.setForeground(
                {
                    "通过": Qt.darkGreen,
                    "进行中": Qt.darkYellow,
                    "等待": Qt.darkYellow,
                    "跳过": Qt.gray,
                    "未开始": Qt.gray,
                }.get(state, Qt.black)
            )
            self.preflight_table.setItem(row, 1, state_item)
            self.preflight_table.setItem(row, 2, QTableWidgetItem(detail))

    def _refresh_timing_provider_fields(self) -> None:
        enabled = self.timing_provider_combo.currentData() == "racetiger"
        for control in self.racetiger_controls:
            self._set_form_row_visible(control, enabled)

    def _set_form_row_visible(self, field, visible: bool) -> None:
        label = self._device_form.labelForField(field)
        if label is not None:
            label.setVisible(visible)
        if isinstance(field, QWidget):
            field.setVisible(visible)
            return
        for index in range(field.count()):
            widget = field.itemAt(index).widget()
            if widget is not None:
                widget.setVisible(visible)

    def _refresh_source_fields(self) -> None:
        is_rtsp = self.source_type_combo.currentData() == "rtsp"
        for field in (
            self.rtsp_address_edit,
            self.rtsp_username_edit,
            self.rtsp_password_row,
        ):
            self._set_form_row_visible(field, is_rtsp)
        secondary_enabled = self.secondary_enabled_checkbox.isChecked()
        secondary_is_rtsp = (
            secondary_enabled
            and self.secondary_source_type_combo.currentData() == "rtsp"
        )
        secondary_is_usb = secondary_enabled and not secondary_is_rtsp
        self.secondary_source_type_combo.setVisible(secondary_enabled)
        self.secondary_device_combo.setVisible(secondary_is_usb)
        self.secondary_detect_button.setVisible(secondary_is_usb)
        for field in (
            self.secondary_rtsp_address_edit,
            self.secondary_rtsp_username_edit,
            self.secondary_rtsp_password_row,
        ):
            self._set_form_row_visible(field, secondary_is_rtsp)
        self._set_form_row_visible(self.device_row, not is_rtsp)
        usb_settings_visible = not is_rtsp or secondary_is_usb
        self._set_form_row_visible(self.video_size_combo, usb_settings_visible)
        self._set_form_row_visible(self.framerate_combo, usb_settings_visible)
        high_speed_enabled = self.high_speed_enabled_checkbox.isChecked()
        self.high_speed_edit.setEnabled(high_speed_enabled)
        self.high_speed_browse_button.setEnabled(high_speed_enabled)
        self._refresh_camera_status()

    def _invalidate_rtsp_probe(self) -> None:
        self._rtsp_probe_ok = False
        self._rtsp_probe_source = ""
        self._rtsp_probe_message = ""
        self._refresh_camera_status()

    def _invalidate_secondary_rtsp_probe(self) -> None:
        self._secondary_rtsp_probe_ok = False
        self._secondary_rtsp_probe_source = ""
        self._secondary_rtsp_probe_message = ""
        self._refresh_camera_status()

    def _current_rtsp_source(self) -> str:
        return apply_rtsp_credentials(
            self.rtsp_address_edit.text().strip(),
            self.rtsp_username_edit.text(),
            self.rtsp_password_edit.text(),
        )

    def _current_secondary_rtsp_source(self) -> str:
        if (
            not self.secondary_enabled_checkbox.isChecked()
            or self.secondary_source_type_combo.currentData() != "rtsp"
        ):
            return ""
        return apply_rtsp_credentials(
            self.secondary_rtsp_address_edit.text().strip(),
            self.secondary_rtsp_username_edit.text(),
            self.secondary_rtsp_password_edit.text(),
        )

    def _selected_usb_source(self, combo: QComboBox) -> str:
        selected = str(combo.currentData() or "").strip()
        if is_supported_review_source(selected):
            return selected
        if not selected:
            return ""
        return make_directshow_source(
            selected,
            video_size=self.video_size_combo.currentData(),
            framerate=self.framerate_combo.currentData(),
        )

    def _selected_secondary_recording_source(self) -> str:
        if not self.secondary_enabled_checkbox.isChecked():
            return ""
        if self.secondary_source_type_combo.currentData() == "rtsp":
            return self._current_secondary_rtsp_source()
        return self._selected_usb_source(self.secondary_device_combo)

    def _test_rtsp_source(self) -> None:
        source = self._current_rtsp_source()
        if not is_rtsp_source(source):
            QMessageBox.warning(self, "无法测试画面", "请填写有效的RTSP地址")
            return
        worker = self._rtsp_probe_worker
        if worker is not None and worker.isRunning():
            return
        self._rtsp_probe_ok = False
        self._rtsp_probe_source = source
        self.rtsp_test_button.setEnabled(False)
        self.camera_status_label.setText("正在读取RTSP画面")
        self.camera_status_label.setStyleSheet("color: #a56300; font-weight: 600;")
        worker = _RtspProbeWorker(source, self._ffmpeg_path, self)
        worker.probe_finished.connect(self._on_rtsp_probe_finished)
        worker.finished.connect(self._on_rtsp_probe_worker_finished)
        self._rtsp_probe_worker = worker
        worker.start()

    def _test_secondary_rtsp_source(self) -> None:
        source = self._current_secondary_rtsp_source()
        if not is_rtsp_source(source):
            QMessageBox.warning(self, "无法测试画面", "请填写有效的机位2 RTSP地址")
            return
        worker = self._secondary_rtsp_probe_worker
        if worker is not None and worker.isRunning():
            return
        self._secondary_rtsp_probe_ok = False
        self._secondary_rtsp_probe_source = source
        self.secondary_rtsp_test_button.setEnabled(False)
        self.camera_status_label.setText("正在读取机位2 RTSP画面")
        self.camera_status_label.setStyleSheet(
            "color: #a56300; font-weight: 600;"
        )
        worker = _RtspProbeWorker(source, self._ffmpeg_path, self)
        worker.probe_finished.connect(self._on_secondary_rtsp_probe_finished)
        worker.finished.connect(self._on_secondary_rtsp_probe_worker_finished)
        self._secondary_rtsp_probe_worker = worker
        worker.start()

    def _on_rtsp_probe_finished(self, ok: bool, message: str) -> None:
        current_source = self._current_rtsp_source()
        self._rtsp_probe_ok = bool(ok and current_source == self._rtsp_probe_source)
        self._rtsp_probe_message = message
        self._refresh_camera_status()

    def _on_secondary_rtsp_probe_finished(self, ok: bool, message: str) -> None:
        current_source = self._current_secondary_rtsp_source()
        self._secondary_rtsp_probe_ok = bool(
            ok and current_source == self._secondary_rtsp_probe_source
        )
        self._secondary_rtsp_probe_message = message
        self._refresh_camera_status()

    def _on_rtsp_probe_worker_finished(self) -> None:
        self._rtsp_probe_worker = None
        self.rtsp_test_button.setEnabled(True)
        self._finish_pending_dialog_if_ready()

    def _on_secondary_rtsp_probe_worker_finished(self) -> None:
        self._secondary_rtsp_probe_worker = None
        self.secondary_rtsp_test_button.setEnabled(True)
        self._finish_pending_dialog_if_ready()

    def _finish_pending_dialog_if_ready(self) -> None:
        workers = (self._rtsp_probe_worker, self._secondary_rtsp_probe_worker)
        if any(worker is not None and worker.isRunning() for worker in workers):
            return
        pending_result = self._pending_dialog_result
        if pending_result is None:
            return
        self._pending_dialog_result = None
        QDialog.done(self, pending_result)

    def _refresh_devices(self) -> None:
        parsed_source = parse_directshow_source(self._source)
        parsed_secondary_source = parse_directshow_source(self._secondary_source)
        try:
            discovered = tuple(self._device_provider())
        except Exception:  # noqa: BLE001 - device discovery is best effort.
            discovered = ()
        devices: list[DirectShowVideoDevice] = []
        seen_inputs: set[str] = set()
        for item in discovered:
            if isinstance(item, DirectShowVideoDevice):
                choice = item
            else:
                name = str(item).strip()
                if not name:
                    continue
                choice = DirectShowVideoDevice(name, name, name)
            if not choice.input_name or choice.input_name in seen_inputs:
                continue
            seen_inputs.add(choice.input_name)
            devices.append(choice)
        self._detected_device_names = {device.input_name for device in devices}

        def resolved_device_name(selected_name: str) -> str:
            if not selected_name or selected_name in self._detected_device_names:
                return selected_name
            normalized = selected_name.strip().casefold()
            friendly_matches = [
                device.input_name
                for device in devices
                if (device.friendly_name or device.display_name).strip().casefold()
                == normalized
            ]
            return friendly_matches[0] if len(friendly_matches) == 1 else selected_name

        def populate(combo: QComboBox, selected_name: str) -> None:
            selected_name = resolved_device_name(selected_name)
            combo.blockSignals(True)
            combo.clear()
            for device in devices:
                combo.addItem(device.display_name, device.input_name)
            if selected_name and combo.findData(selected_name) < 0:
                combo.addItem(f"{selected_name}（当前未检测到）", selected_name)
            selected_index = combo.findData(selected_name)
            combo.setCurrentIndex(selected_index if selected_index >= 0 else 0)
            combo.blockSignals(False)

        populate(
            self.device_combo,
            parsed_source.device_name if parsed_source is not None else "",
        )
        populate(
            self.secondary_device_combo,
            (
                parsed_secondary_source.device_name
                if parsed_secondary_source is not None
                else ""
            ),
        )
        usb_source = parsed_source or parsed_secondary_source
        if usb_source is not None:
            size_index = self.video_size_combo.findData(usb_source.video_size)
            fps_index = self.framerate_combo.findData(usb_source.framerate)
            self.video_size_combo.setCurrentIndex(max(0, size_index))
            self.framerate_combo.setCurrentIndex(max(0, fps_index))
        self._refresh_camera_status()

    def _refresh_camera_status(self) -> None:
        if self.source_type_combo.currentData() == "rtsp":
            source = self._current_rtsp_source()
            if not is_rtsp_source(source):
                text = "机位1未填写有效的RTSP地址"
                color = "#b54747"
            elif self._rtsp_probe_ok and source == self._rtsp_probe_source:
                text = "机位1已读取到画面"
                color = "#247a52"
            else:
                text = self._rtsp_probe_message or "机位1已配置，尚未测试实际画面"
                color = "#a56300"
        else:
            selected = str(self.device_combo.currentData() or "")
            if not selected:
                text = "未检测到USB/Type-C摄像头"
                color = "#b54747"
            elif selected not in self._detected_device_names:
                text = "录像设备已配置，但当前未检测到"
                color = "#b54747"
            else:
                text = "已检测到摄像头，开始录像后验证画面"
                color = "#247a52"

        if self.secondary_enabled_checkbox.isChecked():
            if self.source_type_combo.currentData() == "usb":
                text = {
                    "未检测到USB/Type-C摄像头": "机位1未检测到USB/Type-C摄像头",
                    "录像设备已配置，但当前未检测到": "机位1已配置，但当前未检测到",
                    "已检测到摄像头，开始录像后验证画面": (
                        "机位1已检测到，开始录像后验证画面"
                    ),
                }.get(text, text)
            if self.secondary_source_type_combo.currentData() == "rtsp":
                secondary_source = self._current_secondary_rtsp_source()
                if not is_rtsp_source(secondary_source):
                    secondary_text = "机位2地址无效"
                    secondary_color = "#b54747"
                elif (
                    self._secondary_rtsp_probe_ok
                    and secondary_source == self._secondary_rtsp_probe_source
                ):
                    secondary_text = "机位2已读取到画面"
                    secondary_color = "#247a52"
                else:
                    secondary_text = (
                        self._secondary_rtsp_probe_message
                        or "机位2尚未测试实际画面"
                    )
                    secondary_color = "#a56300"
            else:
                secondary_selected = str(
                    self.secondary_device_combo.currentData() or ""
                )
                primary_selected = str(self.device_combo.currentData() or "")
                if not secondary_selected:
                    secondary_text = "机位2未检测到USB/Type-C摄像头"
                    secondary_color = "#b54747"
                elif (
                    self.source_type_combo.currentData() == "usb"
                    and secondary_selected == primary_selected
                ):
                    secondary_text = "机位1和机位2选择了同一设备"
                    secondary_color = "#b54747"
                elif secondary_selected not in self._detected_device_names:
                    secondary_text = "机位2已配置，但当前未检测到"
                    secondary_color = "#b54747"
                else:
                    secondary_text = "机位2已检测到，开始录像后验证画面"
                    secondary_color = "#247a52"
            text = f"{text}；{secondary_text}"
            if secondary_color == "#b54747" or color == "#b54747":
                color = "#b54747"
            elif secondary_color == "#a56300" or color == "#a56300":
                color = "#a56300"
            else:
                color = "#247a52"
        self.camera_status_label.setText(text)
        self.camera_status_label.setStyleSheet(f"color: {color}; font-weight: 600;")

    def _accept_settings(self) -> None:
        try:
            settings = self.settings
            if settings.secondary_source and not is_supported_review_source(
                settings.secondary_source
            ):
                raise ValueError("机位2必须选择有效的录像设备")
            primary_usb = parse_directshow_source(settings.source)
            secondary_usb = parse_directshow_source(settings.secondary_source)
            if (
                primary_usb is not None
                and secondary_usb is not None
                and primary_usb.device_name == secondary_usb.device_name
            ):
                raise ValueError("机位1和机位2不能选择同一台USB/Type-C摄像头")
            validate_event_network(
                (
                    settings.finishreview_ip,
                    settings.cyclerace_ip,
                    settings.high_speed_pc_ip,
                    settings.switch_ip,
                )
            )
        except (TypeError, ValueError) as error:
            QMessageBox.warning(self, "设置不完整", str(error))
            return
        self.accept()

    def _browse_output_dir(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self,
            "选择本机录像与证据保存目录",
            str(self._output_dir),
        )
        if selected:
            self._output_dir = Path(selected).resolve()
            self.output_edit.setText(str(self._output_dir))

    def _open_event_dir(self) -> None:
        event_dir = self._event_dir
        if event_dir is None or not event_dir.is_dir():
            QMessageBox.warning(self, "赛事目录不可用", "当前赛事目录尚未创建")
            return
        if self._event_export_callback is not None:
            try:
                self._event_export_callback()
            except Exception as error:  # noqa: BLE001 - opening the directory must continue.
                logger.exception("Failed to update the event review summary")
                QMessageBox.warning(
                    self,
                    "复核清单未更新",
                    f"无法更新终点复核清单：{error}\n仍将打开赛事目录。",
                )
        if not _open_event_directory(event_dir):
            QMessageBox.warning(self, "无法打开赛事目录", str(event_dir))

    def _open_saved_event(self) -> None:
        if self._event_workspace_provider is None or self._event_open_callback is None:
            return
        try:
            workspaces = self._event_workspace_provider()
        except EventWorkspaceError as error:
            QMessageBox.warning(self, "无法读取赛事列表", str(error))
            return
        picker = EventWorkspacePickerDialog(
            workspaces,
            self,
            current_dir=self._event_dir,
            summary_provider=self._event_workspace_summary_provider,
        )
        if picker.exec_() != QDialog.Accepted or picker.selected_path is None:
            return
        if self._event_open_callback(picker.selected_path):
            self.reject()

    def _return_to_live_event(self) -> None:
        if self._return_live_event_callback is None:
            return
        if self._return_live_event_callback():
            self.reject()

    def _browse_high_speed_dir(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self,
            "选择原厂高速摄像数据目录",
            str(self._high_speed_dir or self._output_dir),
        )
        if selected:
            self._high_speed_dir = Path(selected).absolute()
            self.high_speed_edit.setText(str(self._high_speed_dir))

    @property
    def settings(self) -> FinishReviewSettings:
        source = self._selected_recording_source()
        high_speed_value = (
            self.high_speed_edit.text().strip()
            if self.high_speed_enabled_checkbox.isChecked()
            else ""
        )
        timing_provider = str(self.timing_provider_combo.currentData() or "cyclerace")
        return FinishReviewSettings(
            source=source,
            secondary_source=self._selected_secondary_recording_source(),
            output_dir=self._output_dir,
            passage_host=self._passage_host,
            passage_port=self._passage_port,
            camera_index=self._camera_index,
            finishreview_ip=self.finishreview_ip_edit.text().strip(),
            cyclerace_ip=self.cyclerace_ip_edit.text().strip(),
            high_speed_pc_ip=self.high_speed_pc_ip_edit.text().strip(),
            switch_ip=self.switch_ip_edit.text().strip(),
            high_speed_dir=(
                Path(high_speed_value).expanduser().absolute()
                if high_speed_value
                else None
            ),
            timing_provider=timing_provider,
            racetiger_base_url=self.racetiger_base_url_edit.text().strip(),
            racetiger_pc=self.racetiger_pc_edit.text().strip(),
            racetiger_rid=self.racetiger_rid_edit.text().strip(),
            racetiger_token=self.racetiger_token_edit.text(),
            racetiger_poll_interval_seconds=(
                self.racetiger_poll_interval_spin.value()
            ),
            visual_detection_enabled=self.visual_enabled_checkbox.isChecked(),
            visual_camera_index=int(self.visual_camera_combo.currentData() or self._camera_index),
            visual_finish_line=self._visual_finish_line,
            visual_gate_width=self._visual_gate_width,
            visual_forward_direction=str(
                self.visual_direction_combo.currentData() or "left_to_right"
            ),
            visual_roi_top=self._visual_roi_top,
            visual_roi_bottom=self._visual_roi_bottom,
        )

    def _calibrate_visual_line(self) -> None:
        source = (
            self._current_rtsp_source()
            if self.visual_camera_combo.currentData() == self._camera_index
            else self._current_secondary_rtsp_source()
        )
        if not is_rtsp_source(source):
            QMessageBox.information(self, "无法设置终点线", "请选择有效的 RTSP 机位后再设置。")
            return
        dialog = VisualLineCalibrationDialog(
            source,
            line_x=self._visual_finish_line,
            gate_width=self._visual_gate_width,
            roi_top=self._visual_roi_top,
            roi_bottom=self._visual_roi_bottom,
            direction=str(self.visual_direction_combo.currentData() or "left_to_right"),
            parent=self,
        )
        if dialog.exec_() == QDialog.Accepted:
            self._visual_finish_line = dialog.line_x
            self._visual_roi_top = dialog.roi_top
            self._visual_roi_bottom = dialog.roi_bottom
            self.visual_direction_combo.setCurrentIndex(
                max(0, self.visual_direction_combo.findData(dialog.direction))
            )
            self.visual_line_label.setText(
                f"终点线 {self._visual_finish_line * 100:.1f}%"
            )

    def _selected_recording_source(self) -> str:
        if self.source_type_combo.currentData() == "rtsp":
            return self._current_rtsp_source()
        return self._selected_usb_source(self.device_combo)

    def done(self, result: int) -> None:
        self._dialog_timer.stop()
        workers = (self._rtsp_probe_worker, self._secondary_rtsp_probe_worker)
        running_workers = tuple(
            worker
            for worker in workers
            if worker is not None and worker.isRunning()
        )
        if running_workers:
            self._pending_dialog_result = int(result)
            self.setEnabled(False)
            for worker in running_workers:
                worker.cancel()
            return
        super().done(result)


class _PassageSignalBridge(QObject):
    accepted = pyqtSignal(object)
    metadata_accepted = pyqtSignal(object)
    focus_accepted = pyqtSignal(object)
    timing_status = pyqtSignal(object)


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


class FinishReviewWindow(PassageReviewDialog):
    """Production console for recording, CycleRace intake, and evidence review."""

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
        visual_detection_enabled: bool = True,
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
        recorder_factory: Callable[..., FfmpegReviewRecorder] = FfmpegReviewRecorder,
        receiver_factory: Callable[..., PassageEventReceiver] = PassageEventReceiver,
        settings_saver: Callable[[FinishReviewSettings], None] | None = None,
    ):
        self.source = str(source).strip()
        self.workspace_root = Path(output_dir).expanduser().resolve()
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.output_dir = self.workspace_root
        self.passage_host = str(passage_host).strip()
        self.passage_port = int(passage_port)
        self.camera_index = max(1, int(camera_index))
        self.visual_detection_enabled = bool(visual_detection_enabled)
        self.visual_camera_index = max(1, int(visual_camera_index))
        self.visual_finish_line = float(visual_finish_line)
        self.visual_gate_width = float(visual_gate_width)
        self.visual_forward_direction = str(visual_forward_direction)
        self.visual_roi_top = max(0.0, min(0.80, float(visual_roi_top)))
        self.visual_roi_bottom = max(0.20, min(1.0, float(visual_roi_bottom)))
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
        self.ffmpeg_path = Path(ffmpeg_path).resolve() if ffmpeg_path else None
        self.review_retention_seconds = max(6, int(review_retention_seconds))
        self.timing_error_ms = max(0, int(timing_error_ms))
        self._recorder_factory = recorder_factory
        self._receiver_factory = receiver_factory
        self._settings_saver = settings_saver
        self._workspace_mode = "live"
        self._archive_background_passage_count = 0

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
        metadata_store = (
            None
            if self.timing_provider == "racetiger"
            else RaceMetadataStore(self.output_dir / "cyclerace_race_metadata.json")
        )
        timeline_store = VideoTimelineStore(self.output_dir / "video_timeline.jsonl")
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
        )

        self.setWindowTitle("FinishReview · 终点多源复核")
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
        self._publishers: dict[int, PassageReviewTimelinePublisher] = {}
        self._archive_publishers = [
            ArchiveTimelinePublisher(session, self.timeline_store)
            for session in load_archive_recording_sessions(self.output_dir)
        ]
        self._capture_windows_by_camera: dict[
            int, dict[str, PassageReviewWindow]
        ] = {self.camera_index: {}}
        self._capture_windows = self._capture_windows_by_camera[self.camera_index]
        self._published_keys: set[tuple[int, str, int]] = set()
        self._unsupported_event_ids: set[str] = set()
        self._runtime_error = ""
        self._auto_recording_error = ""
        self._capture_error = ""
        self._workspace_notice = ""
        self._receiver_error = ""
        self._racetiger_status: RaceTigerStatus | None = None
        self._racetiger_generation = 0
        self._started = False
        self._last_cleanup_at = 0.0
        self._recording_started_at = 0.0
        self._historical_passage_count = len(passage_store)
        self._received_passage_count = 0
        self._last_passage_monotonic = 0.0
        self._received_passage_sequence = 0
        self._received_event_order: dict[tuple[str, str, str], int] = {}
        self._pending_focus: RaceFocus | None = None
        self._pending_passages: dict[str, PassageEvent] = {}
        self._visual_workers: dict[int, VisualCrossingWorker] = {}
        try:
            self._visual_event_count = len(
                VisualCrossingEventStore(
                    self.output_dir / "visual_crossing_events.jsonl"
                ).events()
            )
        except (OSError, ValueError, TypeError):
            self._visual_event_count = 0
        self._visual_error = ""

        self._signal_bridge = _PassageSignalBridge(self)
        self._signal_bridge.accepted.connect(self._on_passage_received)
        self._signal_bridge.metadata_accepted.connect(self._on_metadata_received)
        self._signal_bridge.focus_accepted.connect(self._on_focus_received)
        self._signal_bridge.timing_status.connect(self._on_racetiger_status)
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(max(100, int(refresh_interval_ms)))
        self._refresh_timer.timeout.connect(self._refresh_capture_windows)
        self._passage_batch_timer = QTimer(self)
        self._passage_batch_timer.setSingleShot(True)
        self._passage_batch_timer.setInterval(
            max(0, int(passage_batch_interval_ms))
        )
        self._passage_batch_timer.timeout.connect(self._flush_passage_batch)
        self._clock_timer = QTimer(self)
        self._clock_timer.setInterval(1_000)
        self._clock_timer.timeout.connect(self._update_runtime_status)
        self._init_runtime_status()
        self._init_operator_controls()
        self._high_speed_scan_worker = AuyatRgbScanWorker(
            self._high_speed_catalog,
            self,
        )
        self._high_speed_scan_worker.scan_finished.connect(
            self._on_high_speed_scan_finished
        )
        self.auto_advance_checkbox.setChecked(False)
        self.auto_advance_checkbox.hide()
        try:
            if self._publish_archive_segments():
                self._lookup_cache.clear()
                self.refresh()
        except Exception as exc:  # noqa: BLE001 - recovery remains operator-visible.
            self._capture_error = sanitize_recording_message(exc)
            logger.exception("Failed to recover archived recording sessions")
        self._clock_timer.start()
        if self.high_speed_dir is not None:
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
            self._publish_archive_segments()
            session = prepare_point_playback(
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
            window_title=f"定点回放 - {identity}号",
        )
        try:
            playback.exec_()
        finally:
            session.cleanup()

    def _start_visual_crossing_workers(self) -> None:
        """Start low-rate visual candidates alongside active RTSP recorders."""
        self._stop_visual_crossing_workers()
        # Headless Qt tests and packaged smoke tests must not open real camera
        # handles; production Windows sessions still run this feature normally.
        if (
            os.environ.get("QT_QPA_PLATFORM", "").casefold() == "offscreen"
            or os.environ.get("FINISH_REVIEW_DISABLE_VISUAL", "").strip()
            in {"1", "true", "yes"}
        ):
            return
        self._visual_error = ""
        event_path = self.output_dir / "visual_crossing_events.jsonl"
        try:
            self._visual_event_count = len(VisualCrossingEventStore(event_path).events())
        except (OSError, ValueError, TypeError):
            self._visual_event_count = 0
        self._update_visual_candidate_buttons()
        for camera_index, source in self._configured_recording_sources():
            if not self.visual_detection_enabled or camera_index != self.visual_camera_index:
                continue
            if not is_rtsp_source(source):
                continue
            worker = VisualCrossingWorker(
                source,
                camera_index,
                event_path,
                self,
                config=CrossingConfig(
                    finish_line=self.visual_finish_line,
                    gate_width=self.visual_gate_width,
                    forward_direction=self.visual_forward_direction,
                    roi_top=self.visual_roi_top,
                    roi_bottom=self.visual_roi_bottom,
                ),
            )
            worker.crossing_detected.connect(self._on_visual_crossing)
            worker.failed.connect(self._on_visual_detection_failed)
            worker.start()
            self._visual_workers[camera_index] = worker

    def _stop_visual_crossing_workers(self) -> None:
        workers = tuple(self._visual_workers.values())
        self._visual_workers.clear()
        for worker in workers:
            worker.stop()
        for worker in workers:
            if worker.isRunning() and not worker.wait(1500):
                logger.warning("Visual crossing worker did not stop promptly")

    def _on_visual_crossing(self, event: VisualCrossingEvent) -> None:
        del event
        self._visual_event_count += 1
        self._update_visual_candidate_buttons()
        self._update_runtime_status()

    def _on_visual_detection_failed(self, message: str) -> None:
        self._visual_error = str(message)
        logger.warning("%s", self._visual_error)
        self._update_runtime_status()

    def _open_visual_candidates(self) -> None:
        events = VisualCrossingEventStore(
            self.output_dir / "visual_crossing_events.jsonl"
        ).events()
        self._visual_event_count = len(events)
        self._update_visual_candidate_buttons()
        if not events:
            QMessageBox.information(
                self,
                "视觉候选",
                "当前赛事还没有视觉过线候选。请先开始录像并让人员通过终点线。",
            )
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("视觉过线候选（双击回放确认）")
        dialog.resize(760, 420)
        layout = QVBoxLayout(dialog)
        hint = QLabel(
            "候选只代表摄像头检测到运动，双击一行打开对应机位录像；确认后由工作人员决定是否漏读芯片。",
            dialog,
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        table = QTableWidget(len(events), 6, dialog)
        table.setHorizontalHeaderLabels(
            ("时间", "机位", "方向", "置信度", "画面位置", "状态")
        )
        table.setSelectionBehavior(QTableWidget.SelectRows)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        for row, event in enumerate(events):
            values = (
                datetime.fromtimestamp(event.timestamp_ms / 1000.0).strftime(
                    "%H:%M:%S.%f"
                )[:-3],
                f"机位{event.camera_index}",
                "正向" if event.direction == "forward" else "反向",
                f"{event.confidence:.2f}",
                f"({event.centroid_x:.0f}, {event.centroid_y:.0f})",
                "待人工确认",
            )
            for column, value in enumerate(values):
                table.setItem(row, column, QTableWidgetItem(value))
        table.resizeColumnsToContents()
        table.cellDoubleClicked.connect(
            lambda row, _column: self._replay_visual_candidate(events[row], dialog)
        )
        layout.addWidget(table, 1)
        close_button = QDialogButtonBox(QDialogButtonBox.Close, parent=dialog)
        close_button.rejected.connect(dialog.reject)
        layout.addWidget(close_button)
        dialog.exec_()

    def _update_visual_candidate_buttons(self) -> None:
        """Keep the review entry points aligned with the current candidate count."""
        count = max(0, int(self._visual_event_count))
        if hasattr(self, "visual_candidates_settings_button"):
            self.visual_candidates_settings_button.setText(f"候选事件：{count} 条")
        if hasattr(self, "visual_candidates_button"):
            self.visual_candidates_button.setText(f"视觉候选 ({count})")

    def _replay_visual_candidate(
        self,
        candidate: VisualCrossingEvent,
        parent_dialog: QDialog,
    ) -> None:
        try:
            self._publish_archive_segments()
            self._refresh_capture_windows()
            lookup = self.timeline_store.locate_passage(
                candidate.timestamp_ms,
                pre_roll_ms=3_000,
            )
            location = next(
                (
                    item
                    for item in lookup.locations
                    if item.segment.camera_index == candidate.camera_index
                ),
                None,
            )
        except Exception as error:  # noqa: BLE001 - keep candidate list usable.
            QMessageBox.warning(parent_dialog, "无法打开回放", str(error))
            return
        if location is None:
            QMessageBox.information(
                parent_dialog,
                "暂无录像定位",
                "该视觉候选暂时没有对应的完整录像分段，请稍后重试。",
            )
            return
        event = PassageEvent(
            event_id=f"visual-{candidate.event_id}",
            race_id=self._current_archive_race_id() or "visual-review",
            stage_id="finish",
            group_id="visual",
            sequence=max(1, self._visual_event_count),
            chip_id="visual-candidate",
            bib="",
            passage_time_ms=candidate.timestamp_ms,
            passage_timestamp_ms=candidate.timestamp_ms,
            source="visual_candidate",
            emitted_at_ms=candidate.timestamp_ms,
        )
        self._open_point_playback(event, location)

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

    def _select_event(self, event_id: str) -> None:
        super()._select_event(event_id)
        self._update_operator_controls()
        if hasattr(self, "high_speed_status_label"):
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
            visual_detection_enabled=self.visual_detection_enabled,
            visual_camera_index=self.visual_camera_index,
            visual_finish_line=self.visual_finish_line,
            visual_gate_width=self.visual_gate_width,
            visual_forward_direction=self.visual_forward_direction,
            visual_roi_top=self.visual_roi_top,
            visual_roi_bottom=self.visual_roi_bottom,
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
                association_store = PassageEvidenceAssociationStore(
                    output_dir / "passage_evidence_associations.jsonl"
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
                    association_store,
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
        if persist_settings and self._settings_saver is not None:
            try:
                self._settings_saver(settings)
            except Exception as exc:  # noqa: BLE001 - keep current runtime unchanged.
                QMessageBox.warning(self, "设置未保存", str(exc))
                return False
        if stop_recording:
            self.stop_recording()
        if receiver_restart_needed:
            self.stop_receiver()
        if data_source_changed:
            self._passage_batch_timer.stop()
            self._pending_passages.clear()
        self.source = str(settings.source).strip()
        self.secondary_source = str(settings.secondary_source).strip()
        self.passage_host = str(settings.passage_host).strip()
        self.passage_port = int(settings.passage_port)
        self.camera_index = max(1, int(settings.camera_index))
        self.visual_detection_enabled = bool(settings.visual_detection_enabled)
        self.visual_camera_index = max(1, int(settings.visual_camera_index))
        self.visual_finish_line = max(0.10, min(0.90, float(settings.visual_finish_line)))
        self.visual_gate_width = max(0.02, min(0.30, float(settings.visual_gate_width)))
        self.visual_forward_direction = (
            settings.visual_forward_direction
            if settings.visual_forward_direction in {"left_to_right", "right_to_left"}
            else "left_to_right"
        )
        self.visual_roi_top = max(0.0, min(0.80, float(settings.visual_roi_top)))
        self.visual_roi_bottom = max(0.20, min(1.0, float(settings.visual_roi_bottom)))
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
            (
                passage_store,
                metadata_store,
                timeline_store,
                association_store,
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
            self.passage_store = passage_store
            self.metadata_store = metadata_store
            self.timeline_store = timeline_store
            self.association_store = association_store
            self._lookup_cache.clear()
            self._timeline_signature = ()
            self._selected_event_id = ""
            self._capture_windows_by_camera = {self.camera_index: {}}
            self._capture_windows = self._capture_windows_by_camera[
                self.camera_index
            ]
            self._published_keys.clear()
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
        self.product_title_label = QLabel("FinishReview", panel)
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
        self.beijing_clock_label.setStyleSheet(
            "font-family: Consolas; color: #17212b; font-size: 11pt; font-weight: 700;"
        )
        self.beijing_zone_label = QLabel("北京时间", panel)
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
        self.visual_candidates_button = QPushButton("视觉候选", panel)
        self.visual_candidates_button.setToolTip("打开视觉过线候选，双击候选回放录像确认")
        self.visual_candidates_button.clicked.connect(self._open_visual_candidates)
        top_layout.addWidget(self.visual_candidates_button)
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
        self.visual_status_label = self._status_chip("过线辅助", status_strip)
        status_layout.addWidget(self.receiver_status_label)
        status_layout.addWidget(self.camera_status_label)
        status_layout.addWidget(self.high_speed_status_label)
        status_layout.addWidget(self.visual_status_label)
        status_layout.addStretch(1)
        panel_layout.addWidget(status_strip)
        self.runtime_status_strip = status_strip
        self.runtime_header = panel
        self._update_visual_candidate_buttons()
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
            if self._receiver is not None:
                self.stop_receiver()
            if self._racetiger_source is not None and self._racetiger_source.is_running:
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
        if self._racetiger_source is not None:
            self.stop_receiver()
        if self._receiver is not None and self._receiver.is_running:
            return
        receiver_kwargs = {
            "on_accepted": self._signal_bridge.accepted.emit,
        }
        parameters = inspect.signature(self._receiver_factory).parameters.values()
        supports_metadata = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        ) or "metadata_store" in {
            parameter.name for parameter in parameters
        }
        if supports_metadata:
            receiver_kwargs.update(
                metadata_store=self._receiver_metadata_store,
                on_metadata_accepted=self._signal_bridge.metadata_accepted.emit,
            )
        parameters = inspect.signature(self._receiver_factory).parameters.values()
        supports_focus = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        ) or "on_focus_accepted" in {
            parameter.name for parameter in parameters
        }
        if supports_focus:
            receiver_kwargs["on_focus_accepted"] = (
                self._signal_bridge.focus_accepted.emit
            )
        receiver = self._receiver_factory(
            self.passage_host,
            self.passage_port,
            self._receiver_passage_store,
            **receiver_kwargs,
        )
        try:
            receiver.start()
        except Exception as exc:
            self._receiver_error = sanitize_recording_message(exc)
            try:
                receiver.stop()
            except Exception as exc:  # noqa: BLE001 - rollback is best effort.
                logger.warning("Failed to stop CycleRace receiver: %s", exc)
            self._update_runtime_status()
            raise
        self._receiver = receiver
        self._receiver_error = ""
        self._refresh_timer.start()
        self._update_runtime_status()

    def _start_racetiger_source(self) -> None:
        missing = [
            label
            for label, value in (
                ("接口地址", self.racetiger_base_url),
                ("PC", self.racetiger_pc),
                ("RID", self.racetiger_rid),
                ("令牌", self.racetiger_token),
            )
            if not value
        ]
        if missing:
            raise ValueError("赛虎配置不完整，请填写：" + "、".join(missing))
        client = RaceTigerClient(
            self.racetiger_base_url,
            self.racetiger_token,
            pc=self.racetiger_pc,
            rid=self.racetiger_rid,
        )
        generation = self._racetiger_generation

        def emit_event(event: PassageEvent) -> None:
            if generation == self._racetiger_generation:
                self._signal_bridge.accepted.emit(event)

        def emit_status(status: RaceTigerStatus) -> None:
            if generation == self._racetiger_generation:
                self._signal_bridge.timing_status.emit(status)

        source = RaceTigerSource(
            client,
            self.passage_store,
            race_id=self.racetiger_rid,
            stage_id="finish",
            poll_interval_seconds=self.racetiger_poll_interval_seconds,
            on_event=emit_event,
            on_status=emit_status,
        )
        try:
            source.start()
        except Exception:
            try:
                source.stop()
            except Exception:  # noqa: BLE001 - startup rollback is best effort.
                logger.exception("Failed to stop RaceTiger source after startup error")
            raise
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
                camera_index: len(ring_buffer.segments())
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
        if self._recorders:
            self.stop_recording()
        configured_sources = self._configured_recording_sources()
        if not configured_sources:
            raise RecordingError("请先在设备设置中选择录像摄像头")
        try:
            free_bytes = shutil.disk_usage(self.output_dir).free
        except OSError as exc:
            raise RecordingError(f"无法检查赛事存储空间: {exc}") from exc
        if free_bytes < 1024**3:
            raise RecordingError("赛事存储空间不足 1 GB，无法开始录像")
        recorders: dict[int, FfmpegReviewRecorder] = {}
        ring_buffers: dict[int, ReviewRingBuffer] = {}
        coordinators: dict[int, PassageReviewCoordinator] = {}
        publishers: dict[int, PassageReviewTimelinePublisher] = {}
        archive_publishers = []
        try:
            for camera_index, source in configured_sources:
                recorder = self._recorder_factory(
                    source,
                    self.output_dir,
                    camera_index=camera_index,
                    ffmpeg_path=self.ffmpeg_path,
                    review_retention_seconds=self.review_retention_seconds,
                )
                playlist_path = recorder.start()
                recorders[camera_index] = recorder
                ring_buffer = ReviewRingBuffer(
                    playlist_path,
                    camera_index=camera_index,
                    retention_seconds=self.review_retention_seconds,
                )
                ring_buffer.scan()
                ring_buffers[camera_index] = ring_buffer
                coordinators[camera_index] = PassageReviewCoordinator(ring_buffer)
                publishers[camera_index] = PassageReviewTimelinePublisher(
                    ring_buffer,
                    self.timeline_store,
                    timing_error_ms=self.timing_error_ms,
                )
                archive_publishers.append(
                    ArchiveTimelinePublisher(recorder, self.timeline_store)
                )
            self._recorders = recorders
            self._ring_buffers = ring_buffers
            self._coordinators = coordinators
            self._publishers = publishers
            self._recorder = recorders.get(self.camera_index)
            self._ring_buffer = ring_buffers.get(self.camera_index)
            self._coordinator = coordinators.get(self.camera_index)
            self._publisher = publishers.get(self.camera_index)
            self._capture_windows_by_camera = {
                camera_index: {} for camera_index in coordinators
            }
            self._capture_windows = self._capture_windows_by_camera.setdefault(
                self.camera_index,
                {},
            )
            self._archive_publishers.extend(archive_publishers)
            for event in self._events_for_current_metadata(
                self.passage_store.events()
            ):
                self._register_passage(event, scan=False)
            self._started = True
            self._recording_started_at = time.monotonic()
            self._start_visual_crossing_workers()
            self._runtime_error = ""
            self._auto_recording_error = ""
            self._capture_error = ""
            self._workspace_notice = ""
            self._refresh_timer.start()
            self._refresh_capture_windows()
            self._lookup_cache.clear()
            self.refresh()
        except Exception:
            self._stop_visual_crossing_workers()
            for recorder in recorders.values():
                try:
                    recorder.stop()
                except Exception as cleanup_error:  # noqa: BLE001
                    logger.warning(
                        "Failed to stop recorder during startup rollback: %s",
                        cleanup_error,
                    )
            self._recorders = {}
            self._ring_buffers = {}
            self._coordinators = {}
            self._publishers = {}
            self._recorder = None
            self._ring_buffer = None
            self._coordinator = None
            self._publisher = None
            for archive_publisher in archive_publishers:
                if archive_publisher in self._archive_publishers:
                    self._archive_publishers.remove(archive_publisher)
            raise
        finally:
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

    def _register_passage(self, event: PassageEvent, *, scan: bool = True) -> None:
        if not event.is_active:
            self._discard_registered_passage(event.event_id)
            return
        if not self._coordinators:
            return
        timestamp_ms = self._evidence_timestamp(event)
        if timestamp_ms is None:
            self._unsupported_event_ids.add(event.event_id)
            for windows in self._capture_windows_by_camera.values():
                windows.pop(event.event_id, None)
            return
        self._unsupported_event_ids.discard(event.event_id)
        for camera_index, coordinator in self._coordinators.items():
            window = coordinator.register(
                event.event_id,
                passage_timestamp_ms=timestamp_ms,
                scan=scan,
            )
            self._capture_windows_by_camera.setdefault(camera_index, {})[
                event.event_id
            ] = window
            self._publish_window(camera_index, window, event)

    def _discard_registered_passage(self, event_id: str) -> None:
        for coordinator in self._coordinators.values():
            coordinator.discard(event_id)
        for windows in self._capture_windows_by_camera.values():
            windows.pop(event_id, None)
        self._unsupported_event_ids.discard(event_id)

    def _publish_window(
        self,
        camera_index: int,
        window: PassageReviewWindow,
        event: PassageEvent,
    ) -> bool:
        publisher = self._publishers.get(camera_index)
        key = (camera_index, window.event_id, window.passage_timestamp_ms)
        if (
            publisher is None
            or window.state is not PassageReviewState.READY
            or key in self._published_keys
        ):
            return False
        publisher.publish(window, race_id=event.race_id)
        self._published_keys.add(key)
        return True

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
        events_by_id: dict[str, PassageEvent] = {}
        for store in (self.passage_store, self._receiver_passage_store):
            for event in store.events():
                if event.race_id != metadata.race_id:
                    continue
                current = events_by_id.get(event.event_id)
                if current is None or event.revision > current.revision:
                    events_by_id[event.event_id] = event
        for event in events_by_id.values():
            target_passage_store.append(event)

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

    def _auto_start_recording_for_passage(self, event: PassageEvent) -> bool:
        if (
            self._workspace_mode != "live"
            or not event.is_active
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
        pending_events = tuple(self._pending_passages.values())
        self._pending_passages.clear()
        if not pending_events:
            self._update_runtime_status()
            return
        metadata = (
            self.metadata_store.current() if self.metadata_store is not None else None
        )

        def belongs_to_current_context(event: PassageEvent) -> bool:
            if metadata is None and self.timing_provider == "racetiger":
                return not self.racetiger_rid or event.race_id == self.racetiger_rid
            return metadata is None or (
                event.race_id == metadata.race_id
                and event.stage_id == metadata.stage_id
            )

        try:
            active_events = tuple(
                event
                for event in pending_events
                if event.is_active and belongs_to_current_context(event)
            )
            if active_events:
                for ring_buffer in self._ring_buffers.values():
                    ring_buffer.scan()
            archive_segments = (
                self._publish_archive_segments() if active_events else ()
            )
            changed_event_ids = {event.event_id for event in pending_events}
            for event in pending_events:
                if event.is_active and belongs_to_current_context(event):
                    self._register_passage(event, scan=False)
                else:
                    self._discard_registered_passage(event.event_id)
            if archive_segments:
                changed_event_ids.update(
                    item.event_id
                    for item in self._events_for_current_metadata(
                        self.passage_store.events()
                    )
                )
            self.refresh_events(changed_event_ids)
            self._apply_pending_focus()
            self._capture_error = ""
        except Exception as exc:
            self._capture_error = sanitize_recording_message(exc)
            logger.exception("Failed to prepare passage review evidence")
        self._update_runtime_status()

    def _on_metadata_received(self, metadata: RaceMetadata) -> None:
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
                self._discard_registered_passage(event_id)
        self.refresh()
        self._apply_pending_focus()
        self._update_runtime_status()

    def _on_racetiger_status(self, status: RaceTigerStatus) -> None:
        self._racetiger_status = status
        if status.state == "error":
            self._receiver_error = status.message
        elif status.state == "ok":
            self._receiver_error = ""
        self._update_runtime_status()

    def _on_focus_received(self, focus: RaceFocus) -> None:
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
            preview = publisher.preview(window, race_id=event.race_id)
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

    def _refresh_capture_windows(self) -> None:
        if not self._coordinators or not self._ring_buffers:
            self._update_runtime_status()
            return
        changed_event_ids: set[str] = set()
        try:
            # Keep indexing completed camera segments even when no passage is
            # waiting, so device health and retention remain current.
            for ring_buffer in self._ring_buffers.values():
                ring_buffer.scan()
            if self._publish_archive_segments():
                changed_event_ids.update(
                    event.event_id
                    for event in self._events_for_current_metadata(
                        self.passage_store.events()
                    )
                )
            for camera_index, coordinator in self._coordinators.items():
                windows = self._capture_windows_by_camera.setdefault(
                    camera_index,
                    {},
                )
                for window in coordinator.refresh(scan=False):
                    previous = windows.get(window.event_id)
                    windows[window.event_id] = window
                    event = self.passage_store.get(window.event_id)
                    if event is not None and self._publish_window(
                        camera_index,
                        window,
                        event,
                    ):
                        changed_event_ids.add(window.event_id)
                    if previous is not None and previous.state is not window.state:
                        changed_event_ids.add(window.event_id)
                    elif previous is not None and previous.segments != window.segments:
                        changed_event_ids.add(window.event_id)
            now = time.monotonic()
            if now - self._last_cleanup_at >= 5.0:
                current_time_ms = int(time.time() * 1000.0)
                for ring_buffer in self._ring_buffers.values():
                    ring_buffer.cleanup(current_time_ms=current_time_ms)
                self._last_cleanup_at = now
            for camera_index, recorder in self._recorders.items():
                recorder_error = recorder.check_error()
                if recorder_error:
                    self._runtime_error = (
                        f"机位{camera_index}: {recorder_error}"
                    )
            if changed_event_ids:
                self.refresh_events(changed_event_ids)
        except Exception as exc:
            self._capture_error = sanitize_recording_message(exc)
            logger.exception("Failed to refresh passage review capture")
        self._update_operator_controls()
        self._update_runtime_status()

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

    def _update_runtime_status(self) -> None:
        beijing_now = datetime.now(timezone(timedelta(hours=8)))
        self.beijing_clock_label.setText(beijing_now.strftime("%H:%M:%S"))
        self.race_dir_label.setText(f"证据目录：{self.output_dir.name}")
        self.race_dir_label.setToolTip(str(self.output_dir))
        self._update_event_header()

        configured_sources = self._configured_recording_sources()
        recording_active = self._recording_any_active()
        recording_all_active = self._recording_all_active()
        segments_by_camera = {
            camera_index: ring_buffer.segments()
            for camera_index, ring_buffer in self._ring_buffers.items()
        }
        if recording_active:
            missing = [
                camera_index
                for camera_index, _source in configured_sources
                if camera_index not in self._recorders
                or not self._recorders[camera_index].is_running
            ]
            waiting = [
                camera_index
                for camera_index, recorder in self._recorders.items()
                if recorder.is_running and not segments_by_camera.get(camera_index)
            ]
            stale = []
            now_ms = int(time.time() * 1000.0)
            for camera_index, segments in segments_by_camera.items():
                if segments and now_ms - segments[-1].ended_at_ms > 8_000:
                    stale.append(camera_index)
            if missing:
                camera_text, camera_color = "录像设备: 机位异常", "#b54747"
                camera_state = "error"
                camera_tooltip = "未运行：" + "、".join(
                    f"机位{camera_index}" for camera_index in missing
                )
            elif stale:
                camera_text, camera_color = "录像设备: 无新画面", "#b54747"
                camera_state = "error"
                camera_tooltip = "超过8秒无新画面：" + "、".join(
                    f"机位{camera_index}" for camera_index in stale
                )
            elif waiting:
                camera_text, camera_color = "录像设备: 正在检查", "#a56300"
                camera_state = "busy"
                camera_tooltip = "等待首个2秒片段：" + "、".join(
                    f"机位{camera_index}" for camera_index in waiting
                )
            else:
                camera_text, camera_color = "录像设备: 全部已连接", "#247a52"
                camera_state = "ready"
                camera_tooltip = f"{len(configured_sources)} 个普通机位持续生成可判读画面"
        elif self._auto_recording_error and configured_sources:
            camera_text, camera_color = "录像设备: 自动启动失败", "#b54747"
            camera_state = "error"
            camera_tooltip = self._auto_recording_error
        elif configured_sources:
            camera_text, camera_color = "录像设备: 已配置", "#526170"
            camera_state = "waiting"
            camera_tooltip = f"已配置 {len(configured_sources)} 个普通机位，开始录像后验证画面"
        else:
            camera_text, camera_color = "录像设备: 未配置", "#b54747"
            camera_state = "error"
            camera_tooltip = "请打开设备设置并选择USB/Type-C摄像头"
        self.camera_status_label.setStatus(camera_text, camera_state)
        self.camera_status_label.setToolTip(camera_tooltip)
        self.camera_status_label.setStyleSheet(f"color: {camera_color};")

        if self._visual_error:
            visual_text, visual_state = "过线辅助: 降级", "error"
            visual_tooltip = self._visual_error
        elif self._visual_workers:
            visual_text = f"过线辅助: {self._visual_event_count} 个候选"
            visual_state = "ready"
            visual_tooltip = (
                "低分辨率双窄门检测，仅生成辅助候选，不修改正式成绩；"
                f"当前运行 {len(self._visual_workers)} 个机位"
            )
        elif recording_active:
            visual_text, visual_state = "过线辅助: 启动中", "busy"
            visual_tooltip = "等待视觉检测线程连接视频源"
        else:
            visual_text, visual_state = "过线辅助: 待机", "waiting"
            visual_tooltip = "开始普通录像后自动影子运行"
        self.visual_status_label.setStatus(visual_text, visual_state)
        self.visual_status_label.setToolTip(visual_tooltip)
        self.visual_status_label.setStyleSheet(
            "color: #b54747;" if visual_state == "error" else
            "color: #247a52;" if visual_state == "ready" else
            "color: #a56300;" if visual_state == "busy" else
            "color: #667085;"
        )

        if self._workspace_mode == "archive" and not recording_active:
            recording_text, recording_color = "普通录像: 历史查看", "#667085"
            recording_tooltip = "返回当前赛事后可开始录像"
        elif self._runtime_error and not recording_all_active:
            recording_text, recording_color = "普通录像: 异常", "#b54747"
            recording_tooltip = self._runtime_error
        elif recording_all_active:
            elapsed = max(0, int(time.monotonic() - self._recording_started_at))
            hours, remainder = divmod(elapsed, 3600)
            minutes, seconds = divmod(remainder, 60)
            recording_text = f"普通录像: {hours:02d}:{minutes:02d}:{seconds:02d}"
            recording_color = "#247a52"
            recording_tooltip = f"{len(configured_sources)} 个机位：5分钟赛事存档 + 2秒判读时间片"
        elif recording_active:
            recording_text, recording_color = "普通录像: 部分机位异常", "#b54747"
            recording_tooltip = "请停止录像并检查异常机位后重新开始"
        else:
            recording_text, recording_color = "普通录像: 待机", "#667085"
            recording_tooltip = "点击开始录像后持续保存整场赛事"
        self.recording_status_label.setText(recording_text)
        self.recording_status_label.setToolTip(recording_tooltip)
        self.recording_status_label.setStyleSheet(f"color: {recording_color};")
        self.record_button.setText(
            "停止录像"
            if recording_active
            else "历史查看中"
            if self._workspace_mode == "archive"
            else "开始录像"
        )
        self.record_button.setEnabled(
            recording_active or self._workspace_mode != "archive"
        )
        self.record_button.setStyleSheet(
            "background: #a33d4b; color: white; border: 1px solid #a33d4b;"
            if recording_active
            else "background: #eef1f4; color: #667085; border: 1px solid #cfd7df;"
            if self._workspace_mode == "archive"
            else "background: #247a52; color: white; border: 1px solid #247a52;"
        )

        receiver = self._receiver
        if receiver is not None and receiver.is_running:
            metadata = (
                self.metadata_store.current()
                if self.metadata_store is not None
                else None
            )
            pending_count = len(self._pending_passages)
            if self._workspace_mode == "archive":
                background_count = self._archive_background_passage_count
                self.receiver_status_label.setStatus(
                    "CycleRace: 后台监听，"
                    + (
                        f"已收到 {background_count} 条"
                        if background_count
                        else "当前查看历史赛事"
                    ),
                    "ready" if background_count else "waiting",
                )
                self.receiver_status_label.setStyleSheet(
                    "color: #247a52;" if background_count else "color: #a56300;"
                )
                self.receiver_status_label.setToolTip(
                    "实时数据继续保存到独立收件箱，不会写入当前历史赛事目录。"
                )
            elif pending_count:
                self.receiver_status_label.setStatus(
                    "CycleRace: 监听中，正在处理；"
                    f"本次收到 {self._received_passage_count} 条，待处理 {pending_count}",
                    "busy",
                )
                self.receiver_status_label.setStyleSheet("color: #a56300;")
                self.receiver_status_label.setToolTip(
                    "通过记录已先写入本地审计日志，正在合并刷新录像定位和判读列表。"
                    "监听状态只表示本机接收服务已启动，不能判断发送端持续在线。"
                )
            elif self._received_passage_count:
                self.receiver_status_label.setStatus(
                    f"CycleRace: 监听中，本次收到 {self._received_passage_count} 条",
                    "ready",
                )
                self.receiver_status_label.setStyleSheet("color: #247a52;")
                self.receiver_status_label.setToolTip(
                    "本次运行已收到CycleRace通过记录。"
                    "当前协议没有持续心跳，不能判断发送端持续在线。"
                )
            elif metadata is not None:
                race_label = metadata.race_name.strip() or metadata.race_id
                stage_label = metadata.stage_name.strip() or metadata.stage_id
                self.receiver_status_label.setStatus(
                    f"CycleRace: 监听中，已加载赛事 {race_label} / {stage_label}",
                    "waiting",
                )
                self.receiver_status_label.setStyleSheet("color: #a56300;")
                self.receiver_status_label.setToolTip(
                    f"已读取 {len(metadata.groups)} 个组别、"
                    f"{len(metadata.athletes)} 名运动员；这些资料可能来自本地缓存。"
                    "监听状态只表示本机接收服务已启动，不能判断发送端持续在线。"
                )
            elif self._historical_passage_count:
                self.receiver_status_label.setStatus(
                    "CycleRace: 监听中，"
                    f"已加载历史 {self._historical_passage_count} 条",
                    "waiting",
                )
                self.receiver_status_label.setStyleSheet("color: #a56300;")
                self.receiver_status_label.setToolTip(
                    "历史记录已加载，但本次运行还没有收到CycleRace新数据。"
                    "监听状态只表示本机接收服务已启动，不能判断发送端持续在线。"
                )
            else:
                self.receiver_status_label.setStatus(
                    "CycleRace: 监听中，等待数据", "waiting"
                )
                self.receiver_status_label.setStyleSheet("color: #a56300;")
                self.receiver_status_label.setToolTip(
                    "本机接收服务已启动，等待CycleRace主动发送数据。"
                    "当前协议没有持续心跳，不能判断发送端是否在线。"
                )
        else:
            self.receiver_status_label.setStatus(
                "CycleRace: 异常" if self._receiver_error else "CycleRace: 未监听",
                "error",
            )
            self.receiver_status_label.setStyleSheet("color: #b54747;")
            self.receiver_status_label.setToolTip(
                self._receiver_error or "CycleRace接收服务未启动"
            )

        if self.timing_provider == "racetiger":
            source = self._racetiger_source
            status = self._racetiger_status
            if source is not None and source.is_running:
                pending_count = len(self._pending_passages)
                if status is not None and status.state == "error":
                    self.receiver_status_label.setStatus("赛虎: API 错误", "error")
                    self.receiver_status_label.setStyleSheet("color: #b54747;")
                    self.receiver_status_label.setToolTip(status.message)
                elif pending_count:
                    self.receiver_status_label.setStatus(
                        "赛虎: 正在处理，"
                        f"已读取 {self._received_passage_count}，待处理 {pending_count}",
                        "busy",
                    )
                    self.receiver_status_label.setStyleSheet("color: #a56300;")
                    self.receiver_status_label.setToolTip(
                        "赛虎终点记录已写入本地只读日志，正在准备视频定位"
                    )
                elif status is not None and status.state == "ok":
                    self.receiver_status_label.setStatus(
                        f"赛虎: 已读取 {status.count} 条", "ready"
                    )
                    self.receiver_status_label.setStyleSheet("color: #247a52;")
                    self.receiver_status_label.setToolTip(status.message)
                else:
                    self.receiver_status_label.setStatus("赛虎: 正在读取", "busy")
                    self.receiver_status_label.setStyleSheet("color: #a56300;")
                    self.receiver_status_label.setToolTip("正在轮询赛虎 FINISH 记录")
            else:
                configured = all(
                    (
                        self.racetiger_base_url,
                        self.racetiger_pc,
                        self.racetiger_rid,
                        self.racetiger_token,
                    )
                )
                self.receiver_status_label.setStatus(
                    "赛虎: 异常"
                    if self._receiver_error
                    else ("赛虎: 未启动" if configured else "赛虎: 未配置"),
                    "error",
                )
                self.receiver_status_label.setStyleSheet("color: #b54747;")
                self.receiver_status_label.setToolTip(
                    self._receiver_error or "请在设备与赛事设置中填写赛虎接口参数"
                )

        high_speed_result = self._high_speed_scan_result
        high_speed_root = self._high_speed_catalog.root
        high_speed_remote = is_network_share(high_speed_root)
        if high_speed_root is None:
            self.high_speed_status_label.setStatus(
                "高速摄像: 未配置共享目录", "error"
            )
            self.high_speed_status_label.setStyleSheet("color: #b54747;")
        elif high_speed_result.status == "checking":
            self.high_speed_status_label.setStatus(
                (
                    "高速摄像: 正在连接共享目录"
                    if high_speed_remote
                    else "高速摄像: 正在检查本机测试目录"
                ),
                "busy",
            )
            self.high_speed_status_label.setStyleSheet("color: #a56300;")
        elif high_speed_result.status == "unavailable":
            self.high_speed_status_label.setStatus(
                (
                    "高速摄像: 共享目录未连接"
                    if high_speed_remote
                    else "高速摄像: 本机测试目录不可用"
                ),
                "error",
            )
            self.high_speed_status_label.setStyleSheet("color: #b54747;")
        elif high_speed_result.waiting_file_count:
            self.high_speed_status_label.setStatus(
                (
                    "高速摄像: 共享目录可访问，等待原厂软件完成判读"
                    if high_speed_remote
                    else "高速摄像: 本机测试目录可读，等待原厂软件完成判读"
                ),
                "waiting",
            )
            self.high_speed_status_label.setStyleSheet("color: #a56300;")
        elif high_speed_result.status == "waiting":
            self.high_speed_status_label.setStatus(
                (
                    "高速摄像: 共享目录可访问，等待高速画面"
                    if high_speed_remote
                    else "高速摄像: 本机测试目录可读，等待测试数据"
                ),
                "waiting",
            )
            self.high_speed_status_label.setStyleSheet("color: #a56300;")
        else:
            self.high_speed_status_label.setStatus(
                f"高速摄像: {'共享目录可访问' if high_speed_remote else '本机测试数据可读'}，"
                f"{len(high_speed_result.captures)} 段",
                "ready",
            )
            self.high_speed_status_label.setStyleSheet("color: #247a52;")
        self.high_speed_status_label.setToolTip(
            "\n".join(
                value
                for value in (
                    str(high_speed_root or "未配置高速摄像共享目录"),
                    high_speed_result.message,
                )
                if value
            )
        )

        storage_alert = ""
        storage_alert_tooltip = ""
        storage_alert_color = "#b54747"
        try:
            free_gb = shutil.disk_usage(self.output_dir).free / (1024**3)
            storage_color = "#b54747" if free_gb < 5 else "#a56300" if free_gb < 20 else "#247a52"
            self.storage_status_label.setText(f"存储: {free_gb:.1f} GB")
            self.storage_status_label.setStyleSheet(f"color: {storage_color};")
            self.storage_status_label.setToolTip(str(self.output_dir))
            if free_gb < 5:
                storage_alert = "磁盘空间严重不足"
                storage_alert_tooltip = (
                    f"证据目录仅剩 {free_gb:.1f} GB：{self.output_dir}"
                )
            elif free_gb < 20:
                storage_alert = "磁盘空间不足"
                storage_alert_tooltip = (
                    f"证据目录剩余 {free_gb:.1f} GB：{self.output_dir}"
                )
                storage_alert_color = "#a56300"
        except OSError as exc:
            self.storage_status_label.setText("存储: 不可用")
            self.storage_status_label.setStyleSheet("color: #b54747;")
            self.storage_status_label.setToolTip(str(exc))
            storage_alert = "存储不可用"
            storage_alert_tooltip = f"无法读取证据目录磁盘状态：{exc}"

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
        aligned_event_count = len(self._evidence_timestamp_overrides)
        if self._capture_error:
            self.capture_status_label.setText(f"证据处理异常：{self._capture_error}")
            self.capture_status_label.setToolTip(self._capture_error)
            self.capture_status_label.setStyleSheet(
                "color: #b54747; font-weight: 700;"
            )
        else:
            alignment_text = (
                f"  |  证据日期已对齐 {aligned_event_count} 条"
                if aligned_event_count
                else ""
            )
            self.capture_status_label.setText(
                f"本次待封口 {counts[PassageReviewState.WAITING]}  |  "
                f"本次可核对 {counts[PassageReviewState.READY]}  |  "
                f"本次缺口 {counts[PassageReviewState.PARTIAL]}  |  "
                f"已有证据 {self._available_evidence_count}  |  "
                f"缺少绝对时间 {len(self._unsupported_event_ids)}"
                f"{alignment_text}"
            )
            self.capture_status_label.setToolTip(
                "仅将证据检索日期对齐到实时接收日期；CycleRace正式通过时间未改变。"
                if aligned_event_count
                else ""
            )
            self.capture_status_label.setStyleSheet(
                "color: #667085; font-weight: 500;"
            )
        alert_entries = []
        if self._capture_error:
            alert_entries.append(
                ("证据处理异常", self._capture_error, "#b54747")
            )
        if storage_alert:
            alert_entries.append(
                (storage_alert, storage_alert_tooltip, storage_alert_color)
            )
        if self._workspace_notice:
            alert_entries.append(
                (
                    self._workspace_notice,
                    f"当前赛事目录：{self.output_dir}",
                    "#a56300",
                )
            )
        if len(alert_entries) > 1:
            self.runtime_alert_label.setText("多项运行异常")
            self.runtime_alert_label.setToolTip(
                "\n".join(
                    f"{title}：{detail}" if detail else title
                    for title, detail, _color in alert_entries
                )
            )
            alert_color = (
                "#b54747"
                if any(color == "#b54747" for _title, _detail, color in alert_entries)
                else "#a56300"
            )
        elif alert_entries:
            title, detail, alert_color = alert_entries[0]
            self.runtime_alert_label.setText(title)
            self.runtime_alert_label.setToolTip(detail)
        else:
            self.runtime_alert_label.clear()
            self.runtime_alert_label.setToolTip("")
            self.runtime_alert_label.hide()
            alert_color = ""
        if alert_color:
            self.runtime_alert_label.setStyleSheet(
                f"color: {alert_color}; font-size: 9pt; font-weight: 700;"
            )
            self.runtime_alert_label.show()
        self._update_operator_controls()

    def stop_recording(self) -> None:
        self._stop_visual_crossing_workers()
        recorders = tuple(self._recorders.items())
        if recorders:
            errors = []
            for camera_index, recorder in recorders:
                try:
                    recorder.stop()
                except RecordingError as exc:
                    errors.append(
                        f"机位{camera_index}: {sanitize_recording_message(exc)}"
                    )
            try:
                self._publish_archive_segments(recording=False)
                self._refresh_capture_windows()
            except Exception:
                logger.exception("Failed to publish final review segments")
            if errors:
                self._runtime_error = "; ".join(errors)
        self._recorders = {}
        self._ring_buffers = {}
        self._coordinators = {}
        self._publishers = {}
        self._recorder = None
        self._ring_buffer = None
        self._coordinator = None
        self._publisher = None
        self._started = False
        self._recording_started_at = 0.0
        self._update_runtime_status()

    def stop_receiver(self) -> None:
        self._racetiger_generation += 1
        receiver = self._receiver
        self._receiver = None
        if receiver is not None:
            try:
                receiver.stop()
            except Exception as exc:  # noqa: BLE001 - receiver factories may vary.
                logger.warning("Failed to stop CycleRace receiver: %s", exc)
        racetiger_source = self._racetiger_source
        self._racetiger_source = None
        if racetiger_source is not None:
            try:
                racetiger_source.stop()
            except Exception as exc:  # noqa: BLE001 - shutdown is best effort.
                logger.warning("Failed to stop RaceTiger source: %s", exc)
        self._update_runtime_status()

    def stop(self) -> bool:
        self._refresh_timer.stop()
        self._passage_batch_timer.stop()
        self._pending_passages.clear()
        worker = getattr(self, "_high_speed_scan_worker", None)
        if worker is not None and worker.isRunning():
            worker.stop()
            if not worker.wait(1_000):
                return False
        self.stop_recording()
        self.stop_receiver()
        self._update_runtime_status()
        return True

    def closeEvent(self, event) -> None:
        self._clock_timer.stop()
        if not self.stop():
            event.ignore()
            self.setEnabled(False)
            self.setWindowTitle("FinishReview · 终点多源复核 - 正在停止高速目录扫描")
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
