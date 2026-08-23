"""Production finish console without detection or OCR dependencies."""

from __future__ import annotations

import logging
import inspect
import socket
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from PyQt5.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QKeySequence
from PyQt5.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QShortcut,
    QStyle,
    QDoubleSpinBox,
    QVBoxLayout,
)

try:
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
    from .racetiger_source import RaceTigerClient, RaceTigerSource, RaceTigerStatus
    from .race_metadata import RaceMetadata, RaceMetadataStore
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
    from .stream_recorder import (
        RecordingError,
        sanitize_recording_message,
    )
    from .video_timeline import DEFAULT_TIMING_ERROR_MS, VideoTimelineStore
except ImportError:
    from auyat_rgb import (
        AuyatRgbCatalog,
        AuyatRgbScanWorker,
        AuyatScanResult,
        is_network_share,
    )
    from external_clip_import import ExternalClipImportError, race_id_from_passage_store
    from passage_evidence import PassageEvidenceAssociationStore
    from passage_receiver import (
        DEFAULT_HOST,
        DEFAULT_PORT,
        PassageEvent,
        PassageEventReceiver,
        PassageEventStore,
        RaceFocus,
    )
    from passage_review import PassageReviewDialog, source_location
    from racetiger_source import RaceTigerClient, RaceTigerSource, RaceTigerStatus
    from race_metadata import RaceMetadata, RaceMetadataStore
    from review_recorder import (
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
    from stream_recorder import (
        RecordingError,
        sanitize_recording_message,
    )
    from video_timeline import DEFAULT_TIMING_ERROR_MS, VideoTimelineStore


logger = logging.getLogger("FinishReview")
BEIJING_TIMEZONE = timezone(timedelta(hours=8))
HIGH_SPEED_INDEX_FILENAME = ".videopipe_auyat_index.json"
LIVE_EVIDENCE_DATE_TOLERANCE_MS = 5 * 60 * 1000


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


@dataclass(frozen=True, slots=True)
class FinishReviewSettings:
    source: str
    output_dir: Path
    passage_host: str
    passage_port: int
    camera_index: int
    high_speed_dir: Path | None = None
    timing_provider: str = "cyclerace"
    racetiger_base_url: str = ""
    racetiger_pc: str = ""
    racetiger_rid: str = ""
    racetiger_token: str = ""
    racetiger_poll_interval_seconds: float = 2.0


class FinishReviewLaunchDialog(QDialog):
    """Operator-facing device and race-directory settings."""

    def __init__(
        self,
        settings: FinishReviewSettings,
        parent=None,
        *,
        ffmpeg_path: Path | None = None,
        device_provider: Callable[[], tuple[str, ...]] | None = None,
    ):
        super().__init__(parent)
        self._source = str(settings.source).strip()
        self._output_dir = Path(settings.output_dir).expanduser().resolve()
        self._passage_host = settings.passage_host
        self._passage_port = settings.passage_port
        self._camera_index = settings.camera_index
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
            lambda: discover_directshow_video_devices(self._ffmpeg_path)
        )
        self.setWindowTitle("设备与赛事设置")
        self.setMinimumWidth(760)
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

        form = QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(12)
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

        device_row = QHBoxLayout()
        device_row.setSpacing(6)
        self.device_combo = QComboBox(self)
        self.device_combo.setMinimumWidth(360)
        self.device_combo.currentIndexChanged.connect(self._refresh_camera_status)
        device_row.addWidget(self.device_combo, 1)
        self.detect_button = QPushButton("重新检测", self)
        self.detect_button.clicked.connect(self._refresh_devices)
        device_row.addWidget(self.detect_button)
        form.addRow("录像设备", device_row)

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
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel,
            parent=self,
        )
        self.start_button = buttons.button(QDialogButtonBox.Ok)
        self.start_button.setText("保存设置")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._refresh_timing_provider_fields()
        self._refresh_devices()

    def _refresh_timing_provider_fields(self) -> None:
        enabled = self.timing_provider_combo.currentData() == "racetiger"
        for control in (
            self.racetiger_base_url_edit,
            self.racetiger_pc_edit,
            self.racetiger_rid_edit,
            self.racetiger_token_edit,
            self.racetiger_poll_interval_spin,
        ):
            control.setEnabled(enabled)

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
        self.start_button.setEnabled(configured)

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
        selected = str(self.device_combo.currentData() or "").strip()
        if is_supported_review_source(selected):
            source = selected
        else:
            source = make_directshow_source(
                selected,
                video_size=self.video_size_combo.currentData(),
                framerate=self.framerate_combo.currentData(),
            )
        high_speed_value = self.high_speed_edit.text().strip()
        timing_provider = str(self.timing_provider_combo.currentData() or "cyclerace")
        return FinishReviewSettings(
            source=source,
            output_dir=self._output_dir,
            passage_host=self._passage_host,
            passage_port=self._passage_port,
            camera_index=self._camera_index,
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
        )


class _PassageSignalBridge(QObject):
    accepted = pyqtSignal(object)
    metadata_accepted = pyqtSignal(object)
    focus_accepted = pyqtSignal(object)
    timing_status = pyqtSignal(object)


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
        high_speed_dir: str | Path | None = None,
        timing_provider: str = "cyclerace",
        racetiger_base_url: str = "",
        racetiger_pc: str = "",
        racetiger_rid: str = "",
        racetiger_token: str = "",
        racetiger_poll_interval_seconds: float = 2.0,
        ffmpeg_path: Path | None = None,
        review_retention_seconds: int = 90,
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
        self.high_speed_dir = (
            Path(high_speed_dir).expanduser().absolute()
            if high_speed_dir is not None and str(high_speed_dir).strip()
            else None
        )
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
        super().__init__(
            passage_store,
            timeline_store,
            parent,
            metadata_store=metadata_store,
            high_speed_locator=self._locate_high_speed,
        )

        self.setWindowTitle("终点复核系统")
        self.setMinimumSize(1180, 760)
        self._recorder: FfmpegReviewRecorder | None = None
        self._receiver: PassageEventReceiver | None = None
        self._racetiger_source: RaceTigerSource | None = None
        self._ring_buffer: ReviewRingBuffer | None = None
        self._coordinator: PassageReviewCoordinator | None = None
        self._publisher: PassageReviewTimelinePublisher | None = None
        self._archive_publishers = [
            ArchiveTimelinePublisher(session, self.timeline_store)
            for session in load_archive_recording_sessions(self.output_dir)
        ]
        self._capture_windows: dict[str, PassageReviewWindow] = {}
        self._published_keys: set[tuple[str, int]] = set()
        self._unsupported_event_ids: set[str] = set()
        self._runtime_error = ""
        self._capture_error = ""
        self._receiver_error = ""
        self._racetiger_status: RaceTigerStatus | None = None
        self._racetiger_generation = 0
        self._started = False
        self._last_cleanup_at = 0.0
        self._recording_started_at = 0.0
        self._historical_passage_count = len(passage_store)
        self._received_passage_count = 0
        self._last_passage_monotonic = 0.0
        self._pending_focus: RaceFocus | None = None
        self._pending_passages: dict[str, PassageEvent] = {}

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
        recorder = self._recorder
        if recorder is not None and recorder.is_running:
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

    def _configure_devices(self) -> None:
        if self._recorder is not None and self._recorder.is_running:
            answer = QMessageBox.question(
                self,
                "停止录像并修改设置",
                "修改录像设备或赛事目录前需要停止当前录像，是否继续？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
            self.stop_recording()
        dialog = FinishReviewLaunchDialog(
            FinishReviewSettings(
                source=self.source,
                output_dir=self.output_dir,
                passage_host=self.passage_host,
                passage_port=self.passage_port,
                camera_index=self.camera_index,
                high_speed_dir=self.high_speed_dir,
                timing_provider=self.timing_provider,
                racetiger_base_url=self.racetiger_base_url,
                racetiger_pc=self.racetiger_pc,
                racetiger_rid=self.racetiger_rid,
                racetiger_token=self.racetiger_token,
                racetiger_poll_interval_seconds=self.racetiger_poll_interval_seconds,
            ),
            self,
            ffmpeg_path=self.ffmpeg_path,
        )
        if dialog.exec_() != QDialog.Accepted:
            return
        self._apply_settings(dialog.settings)

    def _apply_settings(self, settings: FinishReviewSettings) -> None:
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
        if receiver_restart_needed:
            self.stop_receiver()
            self._passage_batch_timer.stop()
            self._pending_passages.clear()
        self.source = str(settings.source).strip()
        self.passage_host = str(settings.passage_host).strip()
        self.passage_port = int(settings.passage_port)
        self.camera_index = max(1, int(settings.camera_index))
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
            output_dir.mkdir(parents=True, exist_ok=True)
            self.output_dir = output_dir
            self.passage_store = PassageEventStore(
                output_dir
                / (
                    "racetiger_passage_events.jsonl"
                    if self.timing_provider == "racetiger"
                    else "cyclerace_passage_events.jsonl"
                )
            )
            self.metadata_store = (
                None
                if self.timing_provider == "racetiger"
                else RaceMetadataStore(output_dir / "cyclerace_race_metadata.json")
            )
            self.timeline_store = VideoTimelineStore(
                output_dir / "video_timeline.jsonl"
            )
            self.association_store = PassageEvidenceAssociationStore(
                output_dir / "passage_evidence_associations.jsonl"
            )
            self._lookup_cache.clear()
            self._timeline_signature = ()
            self._selected_event_id = ""
            self._capture_windows.clear()
            self._published_keys.clear()
            self._archive_publishers = [
                ArchiveTimelinePublisher(session, self.timeline_store)
                for session in load_archive_recording_sessions(self.output_dir)
            ]
            self._unsupported_event_ids.clear()
            historical_events = self.passage_store.events()
            self._evidence_timestamp_overrides = (
                _historical_evidence_timestamp_overrides(historical_events)
                if self.timing_provider == "cyclerace"
                else {}
            )
            self._historical_passage_count = len(self.passage_store)
            self._received_passage_count = 0
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
        if self._settings_saver is not None:
            try:
                self._settings_saver(settings)
            except Exception as exc:  # noqa: BLE001 - report persistence failures.
                QMessageBox.warning(self, "设置未保存", str(exc))
        self._runtime_error = ""
        self._update_runtime_status()

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
                metadata_store=self.metadata_store,
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
            self.passage_store,
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
        if self._recorder is not None and self._recorder.is_running:
            return
        if not is_supported_review_source(self.source):
            raise RecordingError("请先在设备设置中选择录像摄像头")
        try:
            free_bytes = shutil.disk_usage(self.output_dir).free
        except OSError as exc:
            raise RecordingError(f"无法检查赛事存储空间: {exc}") from exc
        if free_bytes < 1024**3:
            raise RecordingError("赛事存储空间不足 1 GB，无法开始录像")
        recorder = self._recorder_factory(
            self.source,
            self.output_dir,
            camera_index=self.camera_index,
            ffmpeg_path=self.ffmpeg_path,
            review_retention_seconds=self.review_retention_seconds,
        )
        archive_publisher = None
        try:
            playlist_path = recorder.start()
            ring_buffer = ReviewRingBuffer(
                playlist_path,
                camera_index=self.camera_index,
                retention_seconds=self.review_retention_seconds,
            )
            ring_buffer.scan()
            coordinator = PassageReviewCoordinator(ring_buffer)
            publisher = PassageReviewTimelinePublisher(
                ring_buffer,
                self.timeline_store,
                timing_error_ms=self.timing_error_ms,
            )
            archive_publisher = ArchiveTimelinePublisher(
                recorder,
                self.timeline_store,
            )
            self._recorder = recorder
            self._ring_buffer = ring_buffer
            self._coordinator = coordinator
            self._publisher = publisher
            self._archive_publishers.append(archive_publisher)
            for event in self._events_for_current_metadata(
                self.passage_store.events()
            ):
                self._register_passage(event, scan=False)
            self._started = True
            self._recording_started_at = time.monotonic()
            self._runtime_error = ""
            self._capture_error = ""
            self._refresh_timer.start()
            self._refresh_capture_windows()
            self._lookup_cache.clear()
            self.refresh()
        except Exception:
            try:
                recorder.stop()
            except Exception as cleanup_error:  # noqa: BLE001
                logger.warning(
                    "Failed to stop recorder during startup rollback: %s",
                    cleanup_error,
                )
            self._recorder = None
            self._ring_buffer = None
            self._coordinator = None
            self._publisher = None
            if (
                archive_publisher is not None
                and archive_publisher in self._archive_publishers
            ):
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
        coordinator = self._coordinator
        if coordinator is None:
            return
        timestamp_ms = self._evidence_timestamp(event)
        if timestamp_ms is None:
            self._unsupported_event_ids.add(event.event_id)
            self._capture_windows.pop(event.event_id, None)
            return
        self._unsupported_event_ids.discard(event.event_id)
        window = coordinator.register(
            event.event_id,
            passage_timestamp_ms=timestamp_ms,
            scan=scan,
        )
        self._capture_windows[event.event_id] = window
        self._publish_window(window, event)

    def _discard_registered_passage(self, event_id: str) -> None:
        coordinator = self._coordinator
        if coordinator is not None:
            coordinator.discard(event_id)
        self._capture_windows.pop(event_id, None)
        self._unsupported_event_ids.discard(event_id)

    def _publish_window(
        self,
        window: PassageReviewWindow,
        event: PassageEvent,
    ) -> bool:
        publisher = self._publisher
        key = (window.event_id, window.passage_timestamp_ms)
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
            if active_events and self._ring_buffer is not None:
                self._ring_buffer.scan()
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
        for event_id in tuple(self._capture_windows):
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
        return filtered

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
        coordinator = self._coordinator
        ring_buffer = self._ring_buffer
        recorder = self._recorder
        if coordinator is None or ring_buffer is None:
            self._update_runtime_status()
            return
        changed_event_ids: set[str] = set()
        try:
            # Keep indexing completed camera segments even when no passage is
            # waiting, so device health and retention remain current.
            ring_buffer.scan()
            if self._publish_archive_segments():
                changed_event_ids.update(
                    event.event_id
                    for event in self._events_for_current_metadata(
                        self.passage_store.events()
                    )
                )
            for window in coordinator.refresh(scan=False):
                previous = self._capture_windows.get(window.event_id)
                self._capture_windows[window.event_id] = window
                event = self.passage_store.get(window.event_id)
                if event is not None:
                    if self._publish_window(window, event):
                        changed_event_ids.add(window.event_id)
                if previous is not None and previous.state is not window.state:
                    changed_event_ids.add(window.event_id)
            now = time.monotonic()
            if now - self._last_cleanup_at >= 5.0:
                ring_buffer.cleanup(current_time_ms=int(time.time() * 1000.0))
                self._last_cleanup_at = now
            if recorder is not None:
                recorder_error = recorder.check_error()
                if recorder_error:
                    self._runtime_error = recorder_error
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
            recorder = self._recorder
            recording = bool(recorder is not None and recorder.is_running)
        published = []
        for publisher in publishers:
            publisher_recording = bool(
                recording and publisher.recorder is self._recorder
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

        recorder = self._recorder
        recording_active = bool(recorder is not None and recorder.is_running)
        configured = is_supported_review_source(self.source)
        ring_buffer = self._ring_buffer
        segments = ring_buffer.segments() if ring_buffer is not None else ()
        if recording_active and segments:
            newest_age_ms = int(time.time() * 1000.0) - segments[-1].ended_at_ms
            if newest_age_ms <= 8_000:
                camera_text, camera_color = "录像设备: 已连接", "#247a52"
                camera_tooltip = "FFmpeg 正常运行且持续生成可判读画面"
            else:
                camera_text, camera_color = "录像设备: 无新画面", "#b54747"
                camera_tooltip = "录像进程仍在运行，但超过 8 秒没有新的完整画面"
        elif recording_active:
            camera_text, camera_color = "录像设备: 正在检查", "#a56300"
            camera_tooltip = "等待首个 2 秒判读片段完成"
        elif configured:
            camera_text, camera_color = "录像设备: 已配置", "#526170"
            camera_tooltip = "开始录像后验证设备与画面是否真正联通"
        else:
            camera_text, camera_color = "录像设备: 未配置", "#b54747"
            camera_tooltip = "请打开设备设置并选择USB/Type-C摄像头"
        self.camera_status_label.setText(camera_text)
        self.camera_status_label.setToolTip(camera_tooltip)
        self.camera_status_label.setStyleSheet(f"color: {camera_color};")

        if self._runtime_error and not recording_active:
            recording_text, recording_color = "普通录像: 异常", "#b54747"
            recording_tooltip = self._runtime_error
        elif recording_active:
            elapsed = max(0, int(time.monotonic() - self._recording_started_at))
            hours, remainder = divmod(elapsed, 3600)
            minutes, seconds = divmod(remainder, 60)
            recording_text = f"普通录像: {hours:02d}:{minutes:02d}:{seconds:02d}"
            recording_color = "#247a52"
            recording_tooltip = "5 分钟赛事存档 + 2 秒判读时间片"
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
        for window in self._capture_windows.values():
            counts[window.state] += 1
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

    def stop_recording(self) -> None:
        recorder = self._recorder
        if recorder is not None:
            try:
                recorder.stop()
            except RecordingError as exc:
                self._runtime_error = sanitize_recording_message(exc)
            try:
                self._publish_archive_segments(recording=False)
                self._refresh_capture_windows()
            except Exception:
                logger.exception("Failed to publish final review segments")
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
            self.setWindowTitle("终点复核系统 - 正在停止高速目录扫描")
            QTimer.singleShot(100, self.close)
            return
        super().closeEvent(event)


__all__ = [
    "FinishReviewLaunchDialog",
    "FinishReviewSettings",
    "FinishReviewWindow",
]
