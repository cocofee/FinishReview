"""Device settings, event selection and preflight UI, independent of the review window."""

from __future__ import annotations
import logging
import os
import re
import socket
import subprocess
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from PyQt5.QtCore import Qt, QThread, QTimer, QUrl, pyqtSignal
from PyQt5.QtGui import QColor, QDesktopServices
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
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
from .finish_line import FinishLine
from .event_workspace import (
        EventWorkspaceDescriptor,
        EventWorkspaceError,
        EventWorkspaceSummary,
        summarize_event_workspace,
    )
from .passage_receiver import (
        PassageEvent,
    )
from .preflight import (
        PreflightRun,
        local_ipv4_addresses,
        validate_event_network,
    )
from .settings import FinishReviewSettings
from .racetiger_source import split_racetiger_endpoint
from .review_recorder import (
    DirectShowVideoDevice,
    discover_directshow_video_device_choices,
    is_supported_review_source,
    make_directshow_source,
    parse_directshow_source,
    )
from .stream_recorder import (
        apply_rtsp_credentials,
        find_ffmpeg_executable,
        is_rtsp_source,
        sanitize_recording_message,
        split_rtsp_credentials,
    )
from .thread_lifecycle import track_qthread
from .visual_crossing import (
    VisualLineCalibrationDialog,
)

logger = logging.getLogger("FinishReview")
IS_WINDOWS = os.name == "nt"
BEIJING_TIMEZONE = timezone(timedelta(hours=8))

def _open_event_directory(event_dir: Path) -> bool:
    if IS_WINDOWS:
        try:
            subprocess.Popen(["explorer.exe", "/n,", str(event_dir)])
        except OSError:
            logger.exception("Failed to launch Windows Explorer")
        else:
            return True
    return QDesktopServices.openUrl(QUrl.fromLocalFile(str(event_dir)))


def _is_rtsp_auth_error(detail: object) -> bool:
    text = str(detail or "").casefold()
    return bool(re.search(r"\b401\b", text)) and any(
        marker in text for marker in ("unauthorized", "authorization failed", "认证失败")
    )


def _format_rtsp_probe_error(detail: object) -> str:
    """Turn noisy FFmpeg probe output into an operator-facing diagnosis."""

    text = sanitize_recording_message(detail)
    normalized = text.casefold()
    if _is_rtsp_auth_error(text):
        return (
            "摄像头认证失败（401）：请核对用户名、密码和预览权限；"
            "如账号已锁定，请等待解锁后再试。"
        )
    if "connection refused" in normalized:
        return "RTSP端口拒绝连接：请确认摄像头已启用RTSP服务，且地址中的端口正确。"
    if "timed out" in normalized or "timeout" in normalized:
        return "RTSP连接超时：请检查摄像头IP、网络连通性和防火墙。"
    if not text:
        return "RTSP测试失败，请检查摄像头地址和网络。"
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), text)
    return first_line[:240]


class _RtspProbeWorker(QThread):
    probe_finished = pyqtSignal(bool, str)

    def __init__(self, source: str, ffmpeg_path: Path | None, parent=None):
        super().__init__(parent)
        self.source = str(source).strip()
        self.ffmpeg_path = Path(ffmpeg_path).resolve() if ffmpeg_path else None
        self._process: subprocess.Popen | None = None

    def request_stop(self) -> None:
        self.requestInterruption()
        process = self._process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass

    def cancel(self) -> None:
        self.request_stop()

    def stop(self) -> None:
        self.request_stop()

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
            _format_rtsp_probe_error(
                detail or f"FFmpeg退出代码 {process.returncode}"
            ),
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
        self._visual_detection_enabled = bool(settings.visual_detection_enabled)
        self._visual_camera_index = max(1, int(settings.visual_camera_index))
        self._visual_finish_line = float(settings.visual_finish_line)
        self._visual_gate_width = float(settings.visual_gate_width)
        self._visual_forward_direction = str(settings.visual_forward_direction)
        self._visual_roi_top = float(settings.visual_roi_top)
        self._visual_roi_bottom = float(settings.visual_roi_bottom)
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
        self.racetiger_base_url_edit.editingFinished.connect(
            self._normalize_racetiger_endpoint_fields
        )

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
        self.visual_calibrate_button = QPushButton("调整红黄蓝标线", self)
        self.visual_calibrate_button.clicked.connect(self._calibrate_visual_line)
        visual_settings_row.addWidget(self.visual_calibrate_button)
        self.visual_direction_combo = QComboBox(self)
        self.visual_direction_combo.addItem("正向：左 → 右", "left_to_right")
        self.visual_direction_combo.addItem("正向：右 → 左", "right_to_left")
        self.visual_direction_combo.setCurrentIndex(
            max(
                0,
                self.visual_direction_combo.findData(self._visual_forward_direction),
            )
        )
        visual_settings_row.addWidget(self.visual_direction_combo)
        form.addRow("视频过线辅助", visual_settings_row)
        visual_hint = QLabel(
            "红线是正式终点线，黄线是两侧辅助检测线，蓝线是上下有效范围；"
            "只生成复核候选，不会写入正式成绩。",
            self,
        )
        visual_hint.setWordWrap(True)
        visual_hint.setStyleSheet("color: #667085;")
        form.addRow("", visual_hint)

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
        self.camera_status_label.setWordWrap(True)
        self.camera_status_label.setTextFormat(Qt.PlainText)
        self.camera_status_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
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
        track_qthread(worker)
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
        track_qthread(worker)
        worker.start()

    def _on_rtsp_probe_finished(self, ok: bool, message: str) -> None:
        current_source = self._current_rtsp_source()
        if current_source != self._rtsp_probe_source:
            return
        self._rtsp_probe_ok = bool(ok)
        self._rtsp_probe_message = message
        self._refresh_camera_status()

    def _on_secondary_rtsp_probe_finished(self, ok: bool, message: str) -> None:
        current_source = self._current_secondary_rtsp_source()
        if current_source != self._secondary_rtsp_probe_source:
            return
        self._secondary_rtsp_probe_ok = bool(ok)
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
                color = "#b54747" if self._rtsp_probe_message else "#a56300"
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
                    secondary_color = (
                        "#b54747" if self._secondary_rtsp_probe_message else "#a56300"
                    )
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
        self._normalize_racetiger_endpoint_fields()
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

    def _normalize_racetiger_endpoint_fields(self) -> None:
        """Accept legacy pasted RaceTiger links and split their parameters."""

        raw = self.racetiger_base_url_edit.text().strip()
        base, embedded_pc, embedded_rid, embedded_token = split_racetiger_endpoint(raw)
        if base == raw and not any((embedded_pc, embedded_rid, embedded_token)):
            return
        self.racetiger_base_url_edit.setText(base)
        if embedded_pc and not self.racetiger_pc_edit.text().strip():
            self.racetiger_pc_edit.setText(embedded_pc)
        if embedded_rid and not self.racetiger_rid_edit.text().strip():
            self.racetiger_rid_edit.setText(embedded_rid)
        if embedded_token and not self.racetiger_token_edit.text():
            self.racetiger_token_edit.setText(embedded_token)

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
            visual_camera_index=int(
                self.visual_camera_combo.currentData() or self._camera_index
            ),
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
            QMessageBox.information(
                self,
                "无法设置终点线",
                "请选择有效的 RTSP 机位后再设置。",
            )
            return
        dialog = VisualLineCalibrationDialog(
            source,
            line_x=self._visual_finish_line,
            gate_width=self._visual_gate_width,
            roi_top=self._visual_roi_top,
            roi_bottom=self._visual_roi_bottom,
            direction=self._visual_forward_direction,
            parent=self,
        )
        if dialog.exec_() == QDialog.Accepted:
            camera_index = int(
                self.visual_camera_combo.currentData() or self._camera_index
            )
            self._visual_finish_line = dialog.line_x
            self._visual_gate_width = dialog.gate_width
            self._visual_roi_top = dialog.roi_top
            self._visual_roi_bottom = dialog.roi_bottom
            self._visual_forward_direction = dialog.direction
            self.visual_direction_combo.setCurrentIndex(
                max(0, self.visual_direction_combo.findData(dialog.direction))
            )
            self.visual_line_label.setText(
                f"终点线 {self._visual_finish_line * 100:.1f}%"
            )
            # Keep ordinary-video fallback aligned with the same red/yellow/blue
            # calibration instead of maintaining a second coordinate system.
            line = FinishLine(
                camera_index,
                dialog.line_x,
                dialog.roi_top,
                dialog.line_x,
                dialog.roi_bottom,
                band_width=dialog.gate_width,
            )
            self._finish_line_store.set(line)
            self._finish_line_rois[camera_index] = line.roi

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
