"""Production finish console without detection or OCR dependencies."""

from __future__ import annotations

import logging
import socket
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from PyQt5.QtCore import QObject, Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QKeySequence
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
    QShortcut,
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
from .receiver_controller import ReceiverController
from .recording_controller import RecordingSessionController
from .review_recorder import (
    ArchiveTimelinePublisher,
    FfmpegReviewRecorder,
    PassageReviewCoordinator,
    PassageReviewState,
    PassageReviewTimelinePublisher,
    PassageReviewWindow,
    ReviewRingBuffer,
    discover_directshow_video_devices,
    is_supported_review_source,
    load_archive_recording_sessions,
    make_directshow_source,
    parse_directshow_source,
)
from .settings import FinishReviewSettings
from .stream_recorder import (
    apply_rtsp_credentials,
    find_ffmpeg_executable,
    is_rtsp_source,
    RecordingError,
    sanitize_recording_message,
    split_rtsp_credentials,
)
from .video_timeline import DEFAULT_TIMING_ERROR_MS, VideoTimelineStore
from .video_playback import VideoPlaybackDialog


logger = logging.getLogger("FinishReview")
BEIJING_TIMEZONE = timezone(timedelta(hours=8))
HIGH_SPEED_INDEX_FILENAME = ".videopipe_auyat_index.json"
LIVE_EVIDENCE_DATE_TOLERANCE_MS = 5 * 60 * 1000
RACETIGER_BASE_URL = "https://rqs.racetigertiming.com"
RACETIGER_INFO_PATH = "/Dif/info"


def parse_racetiger_info_url(value: str) -> tuple[str, str, str, str]:
    """Extract RaceTiger connection values from its copied info URL."""

    parsed = urlsplit(str(value or "").strip())
    if (
        parsed.scheme.lower() != "https"
        or (parsed.hostname or "").lower() != "rqs.racetigertiming.com"
        or parsed.port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path.rstrip("/").lower() != RACETIGER_INFO_PATH.lower()
        or parsed.fragment
    ):
        raise ValueError("请粘贴赛虎后台完整的‘赛事信息接口’链接")

    query = parse_qs(parsed.query, keep_blank_values=True)
    values: dict[str, str] = {}
    for name, label in (("pc", "PC"), ("rid", "RID"), ("token", "令牌")):
        candidates = [item.strip() for item in query.get(name, ()) if item.strip()]
        if len(candidates) != 1:
            raise ValueError(f"赛事信息接口缺少有效的{label}")
        values[name] = candidates[0]
    return RACETIGER_BASE_URL, values["pc"], values["rid"], values["token"]


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


def _historical_evidence_timestamp_overrides(
    events: tuple[PassageEvent, ...],
) -> dict[str, tuple[int, int]]:
    overrides = {}
    for event in events:
        timestamp_ms = int(event.timeline_timestamp_ms)
        if (
            not event.is_active
            or event.emitted_at_ms <= 0
            or (event.passage_timestamp_ms is None and timestamp_ms < 86_400_000)
        ):
            continue
        aligned_timestamp_ms = _align_live_evidence_timestamp(
            timestamp_ms,
            event.emitted_at_ms,
        )
        if aligned_timestamp_ms != timestamp_ms:
            overrides[event.event_id] = (timestamp_ms, aligned_timestamp_ms)
    return overrides


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


class FinishReviewLaunchDialog(QDialog):
    """Operator-facing device and race-directory settings."""

    def __init__(
        self,
        settings: FinishReviewSettings,
        parent=None,
        *,
        ffmpeg_path: Path | None = None,
        device_provider: Callable[[], tuple[str, ...]] | None = None,
        passage_provider: Callable[[], tuple[PassageEvent, ...]] | None = None,
        evidence_provider: Callable[[PassageEvent], tuple[bool, bool, str, str]]
        | None = None,
        runtime_snapshot_provider: Callable[[], dict[str, str]] | None = None,
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
        self._racetiger_config_pending_save = False
        self._racetiger_poll_interval = max(
            0.5,
            float(settings.racetiger_poll_interval_seconds or 2.0),
        )
        self._ffmpeg_path = Path(ffmpeg_path).resolve() if ffmpeg_path else None
        self._detected_device_names: set[str] = set()
        self._device_provider = device_provider or (
            lambda: discover_directshow_video_devices(self._ffmpeg_path)
        )
        self._passage_provider = passage_provider or (lambda: ())
        self._evidence_provider = evidence_provider or (
            lambda _event: (False, False, "等待普通录像", "等待高速画面")
        )
        self._runtime_snapshot_provider = runtime_snapshot_provider or (lambda: {})
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
        self.deployment_page = QWidget(self.tabs)
        self.devices_page = QWidget(self.tabs)
        self.preflight_page = QWidget(self.tabs)
        self.tabs.addTab(self.deployment_page, "部署总览")
        self.tabs.addTab(self.devices_page, "设备设置")
        self.tabs.addTab(self.preflight_page, "赛前联调")
        layout.addWidget(self.tabs, 1)

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

        self.racetiger_info_url_edit = QLineEdit(self)
        self.racetiger_info_url_edit.setEchoMode(QLineEdit.Password)
        self.racetiger_info_url_edit.setPlaceholderText(
            "粘贴赛虎后台的‘赛事信息接口’完整链接"
        )
        self.racetiger_info_url_edit.editingFinished.connect(
            self._apply_racetiger_info_url
        )
        form.addRow("赛事信息接口", self.racetiger_info_url_edit)

        self.racetiger_config_status = QLabel(self)
        self.racetiger_config_status.setWordWrap(True)
        form.addRow("", self.racetiger_config_status)
        self._refresh_racetiger_config_status()

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
            self.racetiger_info_url_edit,
            self.racetiger_config_status,
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
        form.addRow("录像连接", self.source_type_combo)

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

        self.secondary_rtsp_enabled_checkbox = QCheckBox(
            "启用第二台RTSP普通摄像机",
            self,
        )
        self.secondary_rtsp_enabled_checkbox.setChecked(
            is_rtsp_source(self._secondary_source)
        )
        self.secondary_rtsp_enabled_checkbox.toggled.connect(
            self._refresh_source_fields
        )
        form.addRow("普通机位2", self.secondary_rtsp_enabled_checkbox)

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
        form.addRow("本机录像设备", device_row)

        self.video_size_combo = QComboBox(self)
        self.video_size_combo.addItem("自动", None)
        for value in ("1920x1080", "2560x1440", "3840x2160"):
            self.video_size_combo.addItem(value, value)
        form.addRow("录像分辨率", self.video_size_combo)

        self.framerate_combo = QComboBox(self)
        self.framerate_combo.addItem("自动", None)
        for value in (25.0, 30.0, 50.0, 60.0):
            self.framerate_combo.addItem(f"{value:g} FPS", value)
        form.addRow("录像帧率", self.framerate_combo)

        output_row = QHBoxLayout()
        output_row.setSpacing(6)
        self.output_edit = QLineEdit(str(self._output_dir), self)
        self.output_edit.setReadOnly(True)
        self.output_edit.setCursorPosition(0)
        output_row.addWidget(self.output_edit, 1)
        browse_button = QPushButton(self)
        browse_button.setIcon(self.style().standardIcon(QStyle.SP_DirOpenIcon))
        browse_button.setToolTip("选择本机录像与证据保存目录")
        browse_button.setFixedWidth(42)
        browse_button.clicked.connect(self._browse_output_dir)
        output_row.addWidget(browse_button)
        form.addRow("录像证据保存", output_row)

        high_speed_row = QHBoxLayout()
        high_speed_row.setSpacing(6)
        self.high_speed_edit = QLineEdit(
            str(self._high_speed_dir) if self._high_speed_dir is not None else "",
            self,
        )
        self.high_speed_edit.setPlaceholderText(r"\\高速摄像电脑\AuyatData")
        self.high_speed_edit.setToolTip(
            "正式比赛请填写另一台高速摄像电脑的只读共享目录，"
            "例如 \\\\FINISH-RGB\\AuyatData"
        )
        high_speed_row.addWidget(self.high_speed_edit, 1)
        high_speed_browse_button = QPushButton(self)
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
        self._refresh_deployment_table()
        self._poll_preflight()

    def _request_recheck(self) -> None:
        if self._recheck_callback is not None:
            self._recheck_callback()
        self._refresh_live_pages()

    def _refresh_deployment_table(self) -> None:
        snapshot = dict(self._runtime_snapshot_provider() or {})
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
        camera_address = (
            self.rtsp_address_edit.text().strip()
            if self.source_type_combo.currentData() == "rtsp"
            else self.device_combo.currentText()
        )
        if (
            self.source_type_combo.currentData() == "rtsp"
            and self.secondary_rtsp_enabled_checkbox.isChecked()
        ):
            camera_address = " / ".join(
                value
                for value in (
                    camera_address,
                    self.secondary_rtsp_address_edit.text().strip(),
                )
                if value
            )
        timing_provider = str(
            self.timing_provider_combo.currentData() or "cyclerace"
        )
        timing_label = "赛虎计时" if timing_provider == "racetiger" else "CycleRace"
        timing_location = "云端接口" if timing_provider == "racetiger" else "计时电脑"
        timing_address = (
            self._racetiger_base_url
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
                self.high_speed_edit.text().strip() or "未配置",
                snapshot.get("high_speed_state", "待检查"),
                snapshot.get("high_speed_detail", "等待共享目录检查"),
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
        selected_high_speed = self.high_speed_edit.text().strip()
        if not selected_source:
            QMessageBox.warning(self, "无法开始联调", "请先配置普通录像源")
            return
        if not selected_high_speed:
            QMessageBox.warning(self, "无法开始联调", "请先配置Auyat高速共享目录")
            return
        current_high_speed = str(self._high_speed_dir or "")
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
            require_high_speed=True,
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

    def _refresh_racetiger_config_status(self, error: str = "") -> None:
        if error:
            self.racetiger_config_status.setText(error)
            self.racetiger_config_status.setStyleSheet(
                "color: #b54747; font-weight: 600;"
            )
            return
        if all((self._racetiger_pc, self._racetiger_rid, self._racetiger_token)):
            suffix = (
                "点击保存设置后生效"
                if self._racetiger_config_pending_save
                else "令牌已保存"
            )
            self.racetiger_config_status.setText(
                f"已配置：PC {self._racetiger_pc} / RID {self._racetiger_rid}；{suffix}"
            )
            self.racetiger_config_status.setStyleSheet(
                "color: #247a52; font-weight: 600;"
            )
            return
        self.racetiger_config_status.setText(
            "复制赛虎后台的‘赛事信息接口’整行并粘贴，其他参数自动读取"
        )
        self.racetiger_config_status.setStyleSheet("color: #667085;")

    def _apply_racetiger_info_url(self) -> bool:
        value = self.racetiger_info_url_edit.text().strip()
        if not value:
            self._refresh_racetiger_config_status()
            return True
        try:
            (
                self._racetiger_base_url,
                self._racetiger_pc,
                self._racetiger_rid,
                self._racetiger_token,
            ) = parse_racetiger_info_url(value)
        except ValueError as error:
            self._refresh_racetiger_config_status(str(error))
            return False
        self._racetiger_config_pending_save = True
        self.racetiger_info_url_edit.clear()
        self._refresh_racetiger_config_status()
        return True

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
        self._set_form_row_visible(self.secondary_rtsp_enabled_checkbox, is_rtsp)
        secondary_visible = (
            is_rtsp and self.secondary_rtsp_enabled_checkbox.isChecked()
        )
        for field in (
            self.secondary_rtsp_address_edit,
            self.secondary_rtsp_username_edit,
            self.secondary_rtsp_password_row,
        ):
            self._set_form_row_visible(field, secondary_visible)
        self._set_form_row_visible(self.device_row, not is_rtsp)
        self._set_form_row_visible(self.video_size_combo, not is_rtsp)
        self._set_form_row_visible(self.framerate_combo, not is_rtsp)
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
        if not self.secondary_rtsp_enabled_checkbox.isChecked():
            return ""
        return apply_rtsp_credentials(
            self.secondary_rtsp_address_edit.text().strip(),
            self.secondary_rtsp_username_edit.text(),
            self.secondary_rtsp_password_edit.text(),
        )

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
        current_source = self._source
        parsed_source = parse_directshow_source(current_source)
        selected_name = parsed_source.device_name if parsed_source is not None else ""
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        if is_supported_review_source(current_source) and parsed_source is None:
            self.device_combo.addItem("网络摄像机（已配置）", current_source)
        try:
            devices = tuple(self._device_provider())
        except Exception:  # noqa: BLE001 - device discovery is best effort.
            devices = ()
        self._detected_device_names = set(devices)
        for device_name in devices:
            self.device_combo.addItem(device_name, device_name)
        if selected_name and self.device_combo.findData(selected_name) < 0:
            self.device_combo.addItem(f"{selected_name}（当前未检测到）", selected_name)
        selected_index = self.device_combo.findData(
            current_source if parsed_source is None else selected_name
        )
        self.device_combo.setCurrentIndex(max(0, selected_index))
        self.device_combo.blockSignals(False)
        if parsed_source is not None:
            size_index = self.video_size_combo.findData(parsed_source.video_size)
            fps_index = self.framerate_combo.findData(parsed_source.framerate)
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
            secondary_source = self._current_secondary_rtsp_source()
            if self.secondary_rtsp_enabled_checkbox.isChecked():
                if not is_rtsp_source(secondary_source):
                    secondary_text = "机位2地址无效"
                    color = "#b54747"
                elif (
                    self._secondary_rtsp_probe_ok
                    and secondary_source == self._secondary_rtsp_probe_source
                ):
                    secondary_text = "机位2已读取到画面"
                else:
                    secondary_text = (
                        self._secondary_rtsp_probe_message
                        or "机位2尚未测试实际画面"
                    )
                    if color != "#b54747":
                        color = "#a56300"
                text = f"{text}；{secondary_text}"
            self.camera_status_label.setText(text)
            self.camera_status_label.setStyleSheet(
                f"color: {color}; font-weight: 600;"
            )
            return
        configured = self.device_combo.currentIndex() >= 0
        selected = str(self.device_combo.currentData() or "")
        if not configured:
            text = "未检测到USB/Type-C摄像头"
        elif is_supported_review_source(selected):
            text = "网络摄像机配置已保留，开始录像后验证画面"
        elif selected not in self._detected_device_names:
            text = "录像设备已配置，但当前未检测到"
        else:
            text = "已检测到摄像头，开始录像后验证画面"
        ready = configured and (
            is_supported_review_source(selected)
            or selected in self._detected_device_names
        )
        self.camera_status_label.setText(text)
        self.camera_status_label.setStyleSheet(
            "color: #247a52; font-weight: 600;"
            if ready
            else "color: #b54747; font-weight: 600;"
        )

    def _accept_settings(self) -> None:
        try:
            if (
                self.timing_provider_combo.currentData() == "racetiger"
                and not self._apply_racetiger_info_url()
            ):
                raise ValueError("请粘贴赛虎后台完整的‘赛事信息接口’链接")
            settings = self.settings
            if settings.timing_provider == "racetiger" and not all(
                (
                    settings.racetiger_base_url,
                    settings.racetiger_pc,
                    settings.racetiger_rid,
                    settings.racetiger_token,
                )
            ):
                raise ValueError(
                    "赛虎配置不完整，请粘贴完整的‘赛事信息接口’链接"
                )
            if settings.secondary_source and not is_rtsp_source(
                settings.secondary_source
            ):
                raise ValueError("机位2必须填写有效的RTSP地址")
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
        high_speed_value = self.high_speed_edit.text().strip()
        timing_provider = str(self.timing_provider_combo.currentData() or "cyclerace")
        return FinishReviewSettings(
            source=source,
            secondary_source=(
                self._current_secondary_rtsp_source()
                if self.source_type_combo.currentData() == "rtsp"
                else ""
            ),
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
            racetiger_base_url=self._racetiger_base_url,
            racetiger_pc=self._racetiger_pc,
            racetiger_rid=self._racetiger_rid,
            racetiger_token=self._racetiger_token,
            racetiger_poll_interval_seconds=(
                self.racetiger_poll_interval_spin.value()
            ),
        )

    def _selected_recording_source(self) -> str:
        if self.source_type_combo.currentData() == "rtsp":
            return self._current_rtsp_source()
        selected = str(self.device_combo.currentData() or "").strip()
        if is_supported_review_source(selected):
            return selected
        if not selected:
            return ""
        return make_directshow_source(
            selected,
            video_size=self.video_size_combo.currentData(),
            framerate=self.framerate_combo.currentData(),
        )

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
    racetiger_event = pyqtSignal(object, int)
    timing_status = pyqtSignal(object, int)


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
        self.output_dir = Path(output_dir).expanduser().resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
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
        self.ffmpeg_path = Path(ffmpeg_path).resolve() if ffmpeg_path else None
        self.review_retention_seconds = max(6, int(review_retention_seconds))
        self.timing_error_ms = max(0, int(timing_error_ms))
        self._settings_saver = settings_saver

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
        super().__init__(
            passage_store,
            timeline_store,
            parent,
            metadata_store=metadata_store,
            high_speed_locator=self._locate_high_speed,
            open_location=self._open_point_playback,
        )

        self.setWindowTitle("终点复核系统")
        self.setMinimumSize(1180, 760)
        self._recording_controller = RecordingSessionController(
            recorder_factory=recorder_factory,
            ring_buffer_factory=ReviewRingBuffer,
            coordinator_factory=PassageReviewCoordinator,
            timeline_publisher_factory=PassageReviewTimelinePublisher,
            archive_publisher_factory=ArchiveTimelinePublisher,
        )
        self._recorder: FfmpegReviewRecorder | None = None
        self._recorders: dict[int, FfmpegReviewRecorder] = {}
        self._receiver_controller = ReceiverController(
            receiver_factory=receiver_factory,
            racetiger_client_factory=RaceTigerClient,
            racetiger_source_factory=RaceTigerSource,
        )
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
        self._recording_stop_error = ""
        self._capture_error = ""
        self._receiver_error = ""
        self._racetiger_status: RaceTigerStatus | None = None
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

        self._signal_bridge = _PassageSignalBridge(self)
        self._signal_bridge.accepted.connect(self._on_passage_received)
        self._signal_bridge.metadata_accepted.connect(self._on_metadata_received)
        self._signal_bridge.focus_accepted.connect(self._on_focus_received)
        self._signal_bridge.racetiger_event.connect(self._on_racetiger_event)
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
        if is_rtsp_source(self.secondary_source):
            sources.append((self.camera_index + 1, self.secondary_source))
        return tuple(sources)

    def _sync_recording_components(self) -> None:
        self._recorders = self._recording_controller.recorders
        self._ring_buffers = self._recording_controller.ring_buffers
        self._coordinators = self._recording_controller.coordinators
        self._publishers = self._recording_controller.timeline_publishers
        self._recorder = self._recorders.get(self.camera_index)
        self._ring_buffer = self._ring_buffers.get(self.camera_index)
        self._coordinator = self._coordinators.get(self.camera_index)
        self._publisher = self._publishers.get(self.camera_index)

    def _recording_any_active(self) -> bool:
        return self._recording_controller.any_active

    def _recording_all_active(self) -> bool:
        return self._recording_controller.all_active(
            self._configured_recording_sources()
        )

    @property
    def receiver(self) -> PassageEventReceiver | None:
        return self._receiver

    @property
    def _receiver(self) -> PassageEventReceiver | None:
        return self._receiver_controller.receiver

    @property
    def _racetiger_source(self) -> RaceTigerSource | None:
        return self._receiver_controller.racetiger_source

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

        identity = event.bib.strip() or event.chip_id.strip() or "未知"
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

    def _on_high_speed_scan_finished(self, result: AuyatScanResult) -> None:
        self._high_speed_scan_result = result
        if result.changed:
            self.invalidate_external_locations()
        self._update_runtime_status()

    def _request_high_speed_scan(self) -> None:
        self._high_speed_scan_result = self._high_speed_catalog.snapshot()
        if self.high_speed_dir is None:
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
        label = getattr(self, "operator_identity_label", None)
        if label is None:
            return
        event = self.passage_store.get(self._selected_event_id)
        identity = ""
        athlete_name = ""
        if event is not None:
            identity = event.bib.strip()
            athlete_name = event.athlete_name.strip()
        else:
            identity = self.selected_identity_value.text().strip()
            athlete_name = self.athlete_value.text().strip()
            if identity == "--":
                identity = ""
            if athlete_name == "--":
                athlete_name = ""
        athlete_summary = f"{identity} {athlete_name}".strip()
        label.setText(
            f"当前运动员：{athlete_summary}"
            if athlete_summary
            else "当前运动员：未选择"
        )
        regular_ready = bool(
            identity
            and getattr(self.regular_pane.video_view, "has_frame", False)
        )
        high_speed_ready = bool(
            identity
            and getattr(self.high_speed_pane.video_view, "has_frame", False)
        )
        self.mark_regular_button.setText(
            f"标线普通录像 {identity}" if identity else "标线普通录像"
        )
        self.mark_high_speed_button.setText(
            f"标线高速摄像 {identity}" if identity else "标线高速摄像"
        )
        self.mark_regular_button.setEnabled(regular_ready)
        self.mark_high_speed_button.setEnabled(high_speed_ready)
        has_pending_marker = bool(
            self.regular_pane.has_pending_marker
            or self.high_speed_pane.has_pending_marker
        )
        self.confirm_next_button.setEnabled(bool(identity and has_pending_marker))
        for shortcut in getattr(self, "confirm_marker_shortcuts", ()):
            shortcut.setEnabled(bool(identity and has_pending_marker))

    def _pending_marker_pane(self):
        if self.regular_pane.has_pending_marker:
            return self.regular_pane
        if self.high_speed_pane.has_pending_marker:
            return self.high_speed_pane
        return None

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

    def _current_settings(self) -> FinishReviewSettings:
        return FinishReviewSettings(
            source=self.source,
            output_dir=self.output_dir,
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
        )

    def _configure_devices(self) -> None:
        dialog = FinishReviewLaunchDialog(
            self._current_settings(),
            self,
            ffmpeg_path=self.ffmpeg_path,
            passage_provider=lambda: self.passage_store.events(),
            evidence_provider=self._preflight_evidence_status,
            runtime_snapshot_provider=self._deployment_runtime_snapshot,
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
                or Path(settings.output_dir).expanduser().resolve() != self.output_dir
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
    ) -> bool:
        self._runtime_error = ""
        self._recording_stop_error = ""
        previous_settings = self._current_settings()
        output_dir = Path(settings.output_dir).expanduser().resolve()
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
        data_source_changed = output_changed or timing_changed
        receiver_restart_needed = data_source_changed or (
            self.timing_provider == "racetiger" and racetiger_changed
        )
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
                prepared_data_source = (
                    passage_store,
                    metadata_store,
                    timeline_store,
                    association_store,
                    preflight_journal,
                    historical_events,
                    evidence_timestamp_overrides,
                    archive_publishers,
                )
            except Exception as exc:  # noqa: BLE001 - validate before runtime mutation.
                self._runtime_error = sanitize_recording_message(exc)
                QMessageBox.warning(self, "设置无法应用", self._runtime_error)
                self._update_runtime_status()
                return False
        if self._settings_saver is not None:
            try:
                self._settings_saver(settings)
            except Exception as exc:  # noqa: BLE001 - keep current runtime unchanged.
                QMessageBox.warning(self, "设置未保存", str(exc))
                return False
        if receiver_restart_needed:
            if not self.stop_receiver():
                stop_error = self._receiver_error or "Timing source did not stop"
                recovery_errors = []
                if self._settings_saver is not None:
                    try:
                        self._settings_saver(previous_settings)
                    except Exception as exc:  # noqa: BLE001 - report rollback failure.
                        recovery_errors.append(f"settings rollback: {exc}")
                try:
                    self.start_receiver()
                except Exception as exc:  # noqa: BLE001 - restore current runtime.
                    recovery_errors.append(
                        "receiver restart: " + sanitize_recording_message(exc)
                    )
                detail = "; ".join((stop_error, *recovery_errors))
                QMessageBox.warning(self, "计时源未停止", detail)
                self._update_runtime_status()
                return False
            self._passage_batch_timer.stop()
            self._pending_passages.clear()
        if stop_recording and not self.stop_recording():
            stop_error = self._runtime_error or "Recording session did not stop"
            recovery_errors = []
            if self._settings_saver is not None:
                try:
                    self._settings_saver(previous_settings)
                except Exception as exc:  # noqa: BLE001 - report rollback failure.
                    recovery_errors.append(f"settings rollback: {exc}")
            if receiver_restart_needed:
                try:
                    self.start_receiver()
                except Exception as exc:  # noqa: BLE001 - restore current runtime.
                    recovery_errors.append(
                        "receiver restart: " + sanitize_recording_message(exc)
                    )
            detail = "; ".join((stop_error, *recovery_errors))
            QMessageBox.warning(self, "录像未停止", detail)
            self._update_runtime_status()
            return False
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
            ) = prepared_data_source
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
        panel.setStyleSheet(
            "QFrame#finishConsoleHeader { background: #f7f9fb; "
            "border: 1px solid #cfd7df; border-radius: 4px; }"
            "QLabel { color: #44515d; font-size: 9pt; font-weight: 600; }"
            "QLabel[statusChip='true'] { background: #ffffff; border: 1px solid #c7d0d9; "
            "border-radius: 4px; padding: 6px 9px; }"
            "QPushButton { min-height: 32px; padding: 0 12px; "
            "font-size: 10pt; font-weight: 600; }"
        )
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(10, 8, 10, 8)
        panel_layout.setSpacing(7)

        title_row = QHBoxLayout()
        title_row.setSpacing(10)
        title = QLabel("终点多源核对", panel)
        title.setStyleSheet("font-size: 13pt; font-weight: 700; color: #17212b;")
        self.race_dir_label = QLabel(panel)
        self.race_dir_label.setStyleSheet("color: #667085; font-weight: 500;")
        self.beijing_clock_label = QLabel(panel)
        self.beijing_clock_label.setStyleSheet(
            "font-family: Consolas; color: #17212b; font-size: 10pt;"
        )
        title_row.addWidget(title)
        title_row.addWidget(self.race_dir_label)
        title_row.addStretch()
        title_row.addWidget(self.beijing_clock_label)
        panel_layout.addLayout(title_row)

        layout = QHBoxLayout()
        layout.setSpacing(8)
        self.camera_status_label = self._status_chip(panel)
        self.recording_status_label = self._status_chip(panel)
        self.receiver_status_label = self._status_chip(panel)
        self.high_speed_status_label = self._status_chip(panel)
        self.storage_status_label = self._status_chip(panel)
        self.capture_status_label = QLabel(panel)
        self.capture_status_label.setStyleSheet("color: #667085; font-weight: 500;")
        layout.addWidget(self.camera_status_label)
        layout.addWidget(self.recording_status_label)
        layout.addWidget(self.receiver_status_label)
        layout.addWidget(self.high_speed_status_label)
        layout.addWidget(self.storage_status_label)
        layout.addStretch(1)

        self.recheck_button = QPushButton("重新检查", panel)
        self.recheck_button.clicked.connect(self._recheck_connections)
        layout.addWidget(self.recheck_button)
        self.settings_button = QPushButton("设备设置", panel)
        self.settings_button.clicked.connect(self._configure_devices)
        layout.addWidget(self.settings_button)
        self.record_button = QPushButton("开始录像", panel)
        self.record_button.setObjectName("finishRecordButton")
        self.record_button.clicked.connect(self._toggle_recording)
        layout.addWidget(self.record_button)
        panel_layout.addLayout(layout)
        panel_layout.addWidget(self.capture_status_label)
        root_layout = self.layout()
        if root_layout is not None:
            root_layout.insertWidget(0, panel)

    @staticmethod
    def _status_chip(parent) -> QLabel:
        label = QLabel(parent)
        label.setProperty("statusChip", True)
        return label

    def _init_operator_controls(self) -> None:
        panel = QFrame(self)
        panel.setObjectName("finishOperatorBar")
        panel.setStyleSheet(
            "QFrame#finishOperatorBar { background: #ffffff; border: 1px solid #cfd7df; "
            "border-radius: 4px; }"
            "QPushButton { min-height: 32px; padding: 0 12px; "
            "font-size: 10pt; font-weight: 600; }"
        )
        layout = QHBoxLayout(panel)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(8)
        self.operator_identity_label = QLabel("当前运动员：未选择", panel)
        self.operator_identity_label.setStyleSheet(
            "font-size: 11pt; font-weight: 700; color: #17212b;"
        )
        layout.addWidget(self.operator_identity_label)
        layout.addStretch()
        self.mark_regular_button = QPushButton("标线普通录像", panel)
        self.mark_regular_button.clicked.connect(
            lambda: self._begin_marking(self.regular_pane)
        )
        layout.addWidget(self.mark_regular_button)
        self.mark_high_speed_button = QPushButton("标线高速摄像", panel)
        self.mark_high_speed_button.clicked.connect(
            lambda: self._begin_marking(self.high_speed_pane)
        )
        layout.addWidget(self.mark_high_speed_button)
        self.confirm_next_button = QPushButton("确认并下一条", panel)
        self.confirm_next_button.setShortcut("Ctrl+Return")
        self.confirm_next_button.clicked.connect(self._confirm_and_next)
        layout.addWidget(self.confirm_next_button)
        self.confirm_marker_shortcuts = []
        for key in (Qt.Key_Return, Qt.Key_Enter):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.setContext(Qt.WindowShortcut)
            shortcut.activated.connect(self._confirm_current_marker)
            shortcut.setEnabled(False)
            self.confirm_marker_shortcuts.append(shortcut)
        for pane in (self.regular_pane, self.high_speed_pane):
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
        root_layout = self.layout()
        if root_layout is not None:
            root_layout.insertWidget(max(0, root_layout.count() - 1), panel)
        self._update_operator_controls()

    def start_receiver(self) -> None:
        if self.timing_provider == "racetiger":
            if self._receiver is not None:
                if not self.stop_receiver():
                    raise RuntimeError(
                        self._receiver_error or "CycleRace receiver did not stop"
                    )
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
            if not self.stop_receiver():
                raise RuntimeError(
                    self._receiver_error or "RaceTiger source did not stop"
                )
        if self._receiver is not None and self._receiver.is_running:
            return
        try:
            self._receiver_controller.start_cyclerace(
                self.passage_host,
                self.passage_port,
                self.passage_store,
                on_accepted=self._signal_bridge.accepted.emit,
                metadata_store=self.metadata_store,
                on_metadata_accepted=self._signal_bridge.metadata_accepted.emit,
                on_focus_accepted=self._signal_bridge.focus_accepted.emit,
            )
        except Exception as exc:
            self._receiver_error = sanitize_recording_message(exc)
            self._update_runtime_status()
            raise
        self._receiver_error = ""
        self._refresh_timer.start()
        self._update_runtime_status()

    def _start_racetiger_source(self) -> None:
        self._receiver_controller.start_racetiger(
            self.racetiger_base_url,
            self.racetiger_token,
            pc=self.racetiger_pc,
            rid=self.racetiger_rid,
            store=self.passage_store,
            poll_interval_seconds=self.racetiger_poll_interval_seconds,
            on_event=self._signal_bridge.racetiger_event.emit,
            on_status=self._signal_bridge.timing_status.emit,
        )
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
        return {
            "timing_state": timing_state,
            "timing_detail": timing_detail,
            "cycle_state": timing_state,
            "cycle_detail": timing_detail,
            "camera_state": camera_state,
            "camera_detail": camera_detail,
            "high_speed_state": high_speed_state,
            "high_speed_detail": high_speed_detail,
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
        self._high_speed_scan_worker.request_scan()
        self._update_runtime_status()

    def start_recording(self) -> None:
        if self._recording_all_active():
            return
        if self._recording_controller.recorders and not self.stop_recording():
            raise RecordingError("现有录像会话未能完全停止，请重试")
        configured_sources = self._configured_recording_sources()
        if not configured_sources:
            raise RecordingError("请先在设备设置中选择录像摄像头")
        try:
            free_bytes = shutil.disk_usage(self.output_dir).free
        except OSError as exc:
            raise RecordingError(f"无法检查赛事存储空间: {exc}") from exc
        if free_bytes < 1024**3:
            raise RecordingError("赛事存储空间不足 1 GB，无法开始录像")
        archive_publishers = []
        try:
            pipelines = self._recording_controller.start(
                sources=configured_sources,
                output_dir=self.output_dir,
                ffmpeg_path=self.ffmpeg_path,
                review_retention_seconds=self.review_retention_seconds,
                timeline_store=self.timeline_store,
                timing_error_ms=self.timing_error_ms,
            )
            self._sync_recording_components()
            archive_publishers = [
                pipeline.archive_publisher for pipeline in pipelines
            ]
            self._capture_windows_by_camera = {
                camera_index: {} for camera_index in self._coordinators
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
            self._runtime_error = ""
            self._recording_stop_error = ""
            self._capture_error = ""
            self._refresh_timer.start()
            self._refresh_capture_windows()
            self._lookup_cache.clear()
            self.refresh()
        except Exception:
            rollback_failures = self._recording_controller.stop()
            for failure in rollback_failures:
                logger.warning(
                    "Failed to stop camera %s during window startup rollback: %s",
                    failure.camera_index,
                    failure.error,
                )
            self._sync_recording_components()
            for archive_publisher in archive_publishers:
                if archive_publisher in self._archive_publishers:
                    self._archive_publishers.remove(archive_publisher)
            self._started = self._recording_controller.any_active
            if not self._started:
                self._recording_started_at = 0.0
                self._refresh_timer.stop()
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

    def _on_passage_received(self, event: PassageEvent) -> None:
        self._received_passage_sequence += 1
        event_key = (event.race_id, event.stage_id, event.event_id)
        self._received_event_order[event_key] = self._received_passage_sequence
        formal_timestamp_ms = self._passage_timestamp(event)
        if not event.is_active:
            self._evidence_timestamp_overrides.pop(event.event_id, None)
        elif self.timing_provider == "cyclerace" and formal_timestamp_ms is not None:
            alignment_reference_ms = (
                event.emitted_at_ms
                if event.emitted_at_ms > 0
                else int(time.time() * 1000.0)
            )
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

    def _on_racetiger_event(self, event: PassageEvent, generation: int) -> None:
        if not self._receiver_controller.is_racetiger_generation_current(generation):
            return
        self._on_passage_received(event)

    def _on_racetiger_status(
        self,
        status: RaceTigerStatus,
        generation: int,
    ) -> None:
        if not self._receiver_controller.is_racetiger_generation_current(generation):
            return
        self._racetiger_status = status
        if status.state == "error":
            self._receiver_error = status.message
        elif status.state == "ok":
            self._receiver_error = ""
        self._update_runtime_status()

    def _on_focus_received(self, focus: RaceFocus) -> None:
        self._pending_focus = focus
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
        if (
            evidence_timestamp_ms is None
            or evidence_timestamp_ms == event.timeline_timestamp_ms
        ):
            return super()._lookup(event)
        return super()._lookup(
            replace(event, passage_timestamp_ms=evidence_timestamp_ms)
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
        self.beijing_clock_label.setText(
            beijing_now.strftime("北京时间 %Y-%m-%d %H:%M:%S")
        )
        self.race_dir_label.setText(f"证据目录：{self.output_dir.name}")
        self.race_dir_label.setToolTip(str(self.output_dir))

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
                camera_tooltip = "未运行：" + "、".join(
                    f"机位{camera_index}" for camera_index in missing
                )
            elif stale:
                camera_text, camera_color = "录像设备: 无新画面", "#b54747"
                camera_tooltip = "超过8秒无新画面：" + "、".join(
                    f"机位{camera_index}" for camera_index in stale
                )
            elif waiting:
                camera_text, camera_color = "录像设备: 正在检查", "#a56300"
                camera_tooltip = "等待首个2秒片段：" + "、".join(
                    f"机位{camera_index}" for camera_index in waiting
                )
            else:
                camera_text, camera_color = "录像设备: 全部已连接", "#247a52"
                camera_tooltip = f"{len(configured_sources)} 个普通机位持续生成可判读画面"
        elif configured_sources:
            camera_text, camera_color = "录像设备: 已配置", "#526170"
            camera_tooltip = f"已配置 {len(configured_sources)} 个普通机位，开始录像后验证画面"
        else:
            camera_text, camera_color = "录像设备: 未配置", "#b54747"
            camera_tooltip = "请打开设备设置并选择USB/Type-C摄像头"
        self.camera_status_label.setText(camera_text)
        self.camera_status_label.setToolTip(camera_tooltip)
        self.camera_status_label.setStyleSheet(f"color: {camera_color};")

        if self._runtime_error and not recording_all_active:
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
        self.record_button.setText("停止录像" if recording_active else "开始录像")
        self.record_button.setStyleSheet(
            "background: #a33d4b; color: white; border: 1px solid #a33d4b;"
            if recording_active
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
            if pending_count:
                self.receiver_status_label.setText(
                    "CycleRace: 监听中，正在处理；"
                    f"本次收到 {self._received_passage_count} 条，待处理 {pending_count}"
                )
                self.receiver_status_label.setStyleSheet("color: #a56300;")
                self.receiver_status_label.setToolTip(
                    "通过记录已先写入本地审计日志，正在合并刷新录像定位和判读列表。"
                    "监听状态只表示本机接收服务已启动，不能判断发送端持续在线。"
                )
            elif self._received_passage_count:
                self.receiver_status_label.setText(
                    f"CycleRace: 监听中，本次收到 {self._received_passage_count} 条"
                )
                self.receiver_status_label.setStyleSheet("color: #247a52;")
                self.receiver_status_label.setToolTip(
                    "本次运行已收到CycleRace通过记录。"
                    "当前协议没有持续心跳，不能判断发送端持续在线。"
                )
            elif metadata is not None:
                race_label = metadata.race_name.strip() or metadata.race_id
                stage_label = metadata.stage_name.strip() or metadata.stage_id
                self.receiver_status_label.setText(
                    f"CycleRace: 监听中，已加载赛事 {race_label} / {stage_label}"
                )
                self.receiver_status_label.setStyleSheet("color: #a56300;")
                self.receiver_status_label.setToolTip(
                    f"已读取 {len(metadata.groups)} 个组别、"
                    f"{len(metadata.athletes)} 名运动员；这些资料可能来自本地缓存。"
                    "监听状态只表示本机接收服务已启动，不能判断发送端持续在线。"
                )
            elif self._historical_passage_count:
                self.receiver_status_label.setText(
                    "CycleRace: 监听中，"
                    f"已加载历史 {self._historical_passage_count} 条"
                )
                self.receiver_status_label.setStyleSheet("color: #a56300;")
                self.receiver_status_label.setToolTip(
                    "历史记录已加载，但本次运行还没有收到CycleRace新数据。"
                    "监听状态只表示本机接收服务已启动，不能判断发送端持续在线。"
                )
            else:
                self.receiver_status_label.setText("CycleRace: 监听中，等待数据")
                self.receiver_status_label.setStyleSheet("color: #a56300;")
                self.receiver_status_label.setToolTip(
                    "本机接收服务已启动，等待CycleRace主动发送数据。"
                    "当前协议没有持续心跳，不能判断发送端是否在线。"
                )
        else:
            self.receiver_status_label.setText(
                "CycleRace: 异常" if self._receiver_error else "CycleRace: 未监听"
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
                    self.receiver_status_label.setText("赛虎: API 错误")
                    self.receiver_status_label.setStyleSheet("color: #b54747;")
                    self.receiver_status_label.setToolTip(status.message)
                elif pending_count:
                    self.receiver_status_label.setText(
                        "赛虎: 正在处理，"
                        f"已读取 {self._received_passage_count}，待处理 {pending_count}"
                    )
                    self.receiver_status_label.setStyleSheet("color: #a56300;")
                    self.receiver_status_label.setToolTip(
                        "赛虎终点记录已写入本地只读日志，正在准备视频定位"
                    )
                elif status is not None and status.state == "ok":
                    self.receiver_status_label.setText(f"赛虎: 已读取 {status.count} 条")
                    self.receiver_status_label.setStyleSheet("color: #247a52;")
                    self.receiver_status_label.setToolTip(status.message)
                else:
                    self.receiver_status_label.setText("赛虎: 正在读取")
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
                self.receiver_status_label.setText(
                    "赛虎: 异常"
                    if self._receiver_error
                    else ("赛虎: 未启动" if configured else "赛虎: 未配置")
                )
                self.receiver_status_label.setStyleSheet("color: #b54747;")
                self.receiver_status_label.setToolTip(
                    self._receiver_error or "请在设备与赛事设置中填写赛虎接口参数"
                )

        high_speed_result = self._high_speed_scan_result
        high_speed_root = self._high_speed_catalog.root
        high_speed_remote = is_network_share(high_speed_root)
        if high_speed_root is None:
            self.high_speed_status_label.setText("高速摄像: 未配置共享目录")
            self.high_speed_status_label.setStyleSheet("color: #b54747;")
        elif high_speed_result.status == "checking":
            self.high_speed_status_label.setText(
                "高速摄像: 正在连接共享目录"
                if high_speed_remote
                else "高速摄像: 正在检查本机测试目录"
            )
            self.high_speed_status_label.setStyleSheet("color: #a56300;")
        elif high_speed_result.status == "unavailable":
            self.high_speed_status_label.setText(
                "高速摄像: 共享目录未连接"
                if high_speed_remote
                else "高速摄像: 本机测试目录不可用"
            )
            self.high_speed_status_label.setStyleSheet("color: #b54747;")
        elif high_speed_result.waiting_file_count:
            self.high_speed_status_label.setText(
                "高速摄像: 共享目录可访问，等待原厂软件完成判读"
                if high_speed_remote
                else "高速摄像: 本机测试目录可读，等待原厂软件完成判读"
            )
            self.high_speed_status_label.setStyleSheet("color: #a56300;")
        elif high_speed_result.status == "waiting":
            self.high_speed_status_label.setText(
                "高速摄像: 共享目录可访问，等待高速画面"
                if high_speed_remote
                else "高速摄像: 本机测试目录可读，等待测试数据"
            )
            self.high_speed_status_label.setStyleSheet("color: #a56300;")
        else:
            self.high_speed_status_label.setText(
                f"高速摄像: {'共享目录可访问' if high_speed_remote else '本机测试数据可读'}，"
                f"{len(high_speed_result.captures)} 段"
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

        try:
            free_gb = shutil.disk_usage(self.output_dir).free / (1024**3)
            storage_color = "#b54747" if free_gb < 5 else "#a56300" if free_gb < 20 else "#247a52"
            self.storage_status_label.setText(f"存储: {free_gb:.1f} GB")
            self.storage_status_label.setStyleSheet(f"color: {storage_color};")
            self.storage_status_label.setToolTip(str(self.output_dir))
        except OSError as exc:
            self.storage_status_label.setText("存储: 不可用")
            self.storage_status_label.setStyleSheet("color: #b54747;")
            self.storage_status_label.setToolTip(str(exc))

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
        self._update_operator_controls()

    def stop_recording(self) -> bool:
        recorders = tuple(self._recorders.items())
        if recorders:
            failures = self._recording_controller.stop()
            try:
                self._publish_archive_segments()
                self._refresh_capture_windows()
            except Exception:
                logger.exception("Failed to publish final review segments")
            if failures:
                self._recording_stop_error = "; ".join(
                    f"机位{failure.camera_index}: "
                    f"{sanitize_recording_message(failure.error)}"
                    for failure in failures
                )
                self._runtime_error = self._recording_stop_error
        else:
            failures = self._recording_controller.stop()
        if not failures:
            if self._runtime_error == self._recording_stop_error:
                self._runtime_error = ""
            self._recording_stop_error = ""
        self._sync_recording_components()
        self._started = self._recording_controller.any_active
        if not self._started:
            self._recording_started_at = 0.0
        self._update_runtime_status()
        return (
            not any(failure.still_running for failure in failures)
            and not self._recording_controller.any_active
        )

    def stop_receiver(self) -> bool:
        errors = self._receiver_controller.stop()
        if errors:
            self._receiver_error = "; ".join(errors)
        else:
            self._receiver_error = ""
        self._update_runtime_status()
        return not errors

    def stop(self) -> bool:
        self._refresh_timer.stop()
        self._passage_batch_timer.stop()
        self._pending_passages.clear()
        worker = getattr(self, "_high_speed_scan_worker", None)
        if worker is not None and worker.isRunning():
            worker.stop()
            if not worker.wait(1_000):
                return False
        recording_stopped = self.stop_recording()
        receiver_stopped = self.stop_receiver()
        self._update_runtime_status()
        return recording_stopped and receiver_stopped

    def closeEvent(self, event) -> None:
        self._clock_timer.stop()
        if not self.stop():
            event.ignore()
            self.setEnabled(False)
            self.setWindowTitle("终点复核系统 - 正在停止高速目录扫描")
            QTimer.singleShot(100, self.close)
            return
        super().closeEvent(event)


__all__ = [
    "FinishReviewLaunchDialog",
    "FinishReviewSettings",
    "FinishReviewWindow",
]
