"""Read-only runtime status projection and its widget presenter."""

from __future__ import annotations
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from .auyat_rgb import AuyatScanResult
from .racetiger_source import RaceTigerStatus
from .race_metadata import RaceMetadata
from .review_recorder import PassageReviewState
from .launch_dialog import _is_rtsp_auth_error

@dataclass(frozen=True)
class RuntimeStatusSnapshot:
    beijing_clock_text: str
    epoch_now_ms: int
    configured_sources: tuple[tuple[int, str], ...]
    recording_active: bool
    recording_all_active: bool
    segments_by_camera: Mapping[int, tuple[object, ...]]
    reconnecting_cameras: tuple[int, ...]
    reconnect_errors: Mapping[int, str]
    running_recorder_cameras: frozenset[int]
    auto_recording_error: str
    archive_scan_active: bool
    anomaly_count: int
    archive_candidate_count: int
    visual_failed: bool
    visual_status: str
    video_scan_active: bool
    finish_line_count: int
    visual_detection_enabled: bool
    workspace_mode: str
    runtime_error: str
    recording_elapsed_seconds: int
    receiver_running: bool
    receiver_metadata: RaceMetadata | None
    pending_passage_count: int
    archive_background_passage_count: int
    received_passage_count: int
    historical_passage_count: int
    receiver_error: str
    timing_provider: str
    racetiger_running: bool
    racetiger_status: RaceTigerStatus | None
    racetiger_configured: bool
    high_speed_result: AuyatScanResult
    high_speed_root: Path | None
    high_speed_remote: bool
    storage_free_gb: float | None
    storage_error: str
    capture_counts: Mapping[PassageReviewState, int]
    aligned_event_count: int
    capture_error: str
    available_evidence_count: int
    unsupported_event_count: int
    workspace_notice: str
    output_dir: Path = Path(".")
    video_assist_enabled: bool = False


STATUS_WIDGET_NAMES = (
    'archive_scan_button', 'beijing_clock_label', 'camera_status_label',
    'capture_status_label', 'high_speed_status_label', 'race_dir_label',
    'receiver_status_label', 'record_button', 'recording_status_label',
    'runtime_alert_label', 'storage_status_label', 'video_assist_status_label',
)


class RuntimeStatusPresenter:
    def __init__(self, widgets):
        self.widgets = {name: widgets[name] for name in STATUS_WIDGET_NAMES}

    def render(self, snapshot: RuntimeStatusSnapshot) -> None:
        self.widgets['beijing_clock_label'].setText(snapshot.beijing_clock_text)
        self.widgets['race_dir_label'].setText(f"证据目录：{snapshot.output_dir.name}")
        self.widgets['race_dir_label'].setToolTip(str(snapshot.output_dir))

        configured_sources = snapshot.configured_sources
        recording_active = snapshot.recording_active
        recording_all_active = snapshot.recording_all_active
        segments_by_camera = snapshot.segments_by_camera
        reconnecting = snapshot.reconnecting_cameras
        if reconnecting:
            auth_failed = any(
                _is_rtsp_auth_error(snapshot.reconnect_errors[index])
                for index in reconnecting
            )
            camera_text = "录像设备: 认证失败" if auth_failed else "录像设备: 自动重连中"
            camera_color = "#b54747"
            camera_state = "error"
            camera_tooltip = "\n".join(
                f"机位{camera_index}: {snapshot.reconnect_errors[camera_index]}"
                for camera_index in reconnecting
            )
        elif recording_active:
            missing = [
                camera_index
                for camera_index, _source in configured_sources
                if camera_index not in snapshot.running_recorder_cameras
            ]
            waiting = [
                camera_index
                for camera_index in snapshot.running_recorder_cameras
                if not segments_by_camera.get(camera_index)
            ]
            stale = []
            for camera_index, segments in segments_by_camera.items():
                if (
                    segments
                    and snapshot.epoch_now_ms - segments[-1].ended_at_ms > 8_000
                ):
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
        elif snapshot.auto_recording_error and configured_sources:
            camera_text, camera_color = "录像设备: 自动启动失败", "#b54747"
            camera_state = "error"
            camera_tooltip = snapshot.auto_recording_error
        elif configured_sources:
            camera_text, camera_color = "录像设备: 已配置", "#526170"
            camera_state = "waiting"
            camera_tooltip = f"已配置 {len(configured_sources)} 个普通机位，开始录像后验证画面"
        else:
            camera_text, camera_color = "录像设备: 未配置", "#b54747"
            camera_state = "error"
            camera_tooltip = "请打开设备设置并选择USB/Type-C摄像头"
        self.widgets['camera_status_label'].setStatus(camera_text, camera_state)
        self.widgets['camera_status_label'].setToolTip(camera_tooltip)
        self.widgets['camera_status_label'].setStyleSheet(f"color: {camera_color};")

        anomaly_count = snapshot.anomaly_count
        if not recording_active and snapshot.archive_scan_active:
            candidate_count = snapshot.archive_candidate_count
            video_text = (
                f"视频辅助: 候选 {candidate_count}"
                if candidate_count
                else "视频辅助: 扫描中"
            )
            video_state = "ready" if candidate_count else "busy"
            video_tip = "正在扫描已录制录像；Ctrl+右/左可跳到下一个/上一个有人通过位置"
        elif not recording_active:
            video_text, video_state = "视频辅助: 待机", "waiting"
            video_tip = "开始普通录像后自动扫描疑似过线批次"
        elif snapshot.visual_failed:
            video_text = "视频辅助: 视觉检测异常"
            video_state = "error"
            video_tip = snapshot.visual_status or "实时视觉检测失败"
        elif snapshot.video_scan_active:
            video_text = f"视频辅助: {anomaly_count} 个异常" if anomaly_count else "视频辅助: 扫描中"
            video_state = "error" if anomaly_count else "busy"
            configured_lines = snapshot.finish_line_count
            video_tip = (
                f"已配置 {configured_lines} 个机位终点线；只提示异常批次"
                if configured_lines
                else "当前使用默认终点线区域；可通过设置接口调整"
            )
            if snapshot.visual_detection_enabled and snapshot.visual_status:
                video_tip += f"；实时视觉：{snapshot.visual_status}"
        else:
            video_text, video_state = "视频辅助: 未启动", "error"
            video_tip = "普通录像运行时辅助扫描器未启动"
        self.widgets['video_assist_status_label'].setStatus(video_text, video_state)
        self.widgets['video_assist_status_label'].setToolTip(video_tip)
        self.widgets['video_assist_status_label'].setVisible(snapshot.video_assist_enabled)
        if 'archive_scan_button' in self.widgets:
            archive_mode = snapshot.workspace_mode == "archive" and not recording_active
            archive_scan_available = archive_mode and snapshot.video_assist_enabled
            self.widgets['archive_scan_button'].setVisible(archive_scan_available)
            self.widgets['archive_scan_button'].setEnabled(archive_scan_available)
            self.widgets['archive_scan_button'].setText(
                "停止视频分析"
                if snapshot.archive_scan_active
                else "分析历史视频"
            )

        if snapshot.workspace_mode == "archive" and not recording_active:
            recording_text, recording_color = "普通录像: 历史查看", "#667085"
            recording_tooltip = "返回当前赛事后可开始录像"
        elif snapshot.runtime_error and not recording_all_active:
            recording_text, recording_color = "普通录像: 异常", "#b54747"
            recording_tooltip = snapshot.runtime_error
        elif recording_all_active:
            elapsed = snapshot.recording_elapsed_seconds
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
        self.widgets['recording_status_label'].setText(recording_text)
        self.widgets['recording_status_label'].setToolTip(recording_tooltip)
        self.widgets['recording_status_label'].setStyleSheet(f"color: {recording_color};")
        self.widgets['record_button'].setText(
            "停止录像"
            if recording_active
            else "历史查看中"
            if snapshot.workspace_mode == "archive"
            else "开始录像"
        )
        self.widgets['record_button'].setEnabled(
            recording_active or snapshot.workspace_mode != "archive"
        )
        self.widgets['record_button'].setStyleSheet(
            "background: #a33d4b; color: white; border: 1px solid #a33d4b;"
            if recording_active
            else "background: #eef1f4; color: #667085; border: 1px solid #cfd7df;"
            if snapshot.workspace_mode == "archive"
            else "background: #247a52; color: white; border: 1px solid #247a52;"
        )

        if snapshot.receiver_running:
            metadata = snapshot.receiver_metadata
            pending_count = snapshot.pending_passage_count
            if snapshot.workspace_mode == "archive":
                background_count = snapshot.archive_background_passage_count
                self.widgets['receiver_status_label'].setStatus(
                    "CycleRace: 后台监听，"
                    + (
                        f"已收到 {background_count} 条"
                        if background_count
                        else "当前查看历史赛事"
                    ),
                    "ready" if background_count else "waiting",
                )
                self.widgets['receiver_status_label'].setStyleSheet(
                    "color: #247a52;" if background_count else "color: #a56300;"
                )
                self.widgets['receiver_status_label'].setToolTip(
                    "实时数据继续保存到独立收件箱，不会写入当前历史赛事目录。"
                )
            elif pending_count:
                self.widgets['receiver_status_label'].setStatus(
                    "CycleRace: 监听中，正在处理；"
                    f"本次收到 {snapshot.received_passage_count} 条，待处理 {pending_count}",
                    "busy",
                )
                self.widgets['receiver_status_label'].setStyleSheet("color: #a56300;")
                self.widgets['receiver_status_label'].setToolTip(
                    "通过记录已先写入本地审计日志，正在合并刷新录像定位和判读列表。"
                    "监听状态只表示本机接收服务已启动，不能判断发送端持续在线。"
                )
            elif snapshot.received_passage_count:
                self.widgets['receiver_status_label'].setStatus(
                    f"CycleRace: 监听中，本次收到 {snapshot.received_passage_count} 条",
                    "ready",
                )
                self.widgets['receiver_status_label'].setStyleSheet("color: #247a52;")
                self.widgets['receiver_status_label'].setToolTip(
                    "本次运行已收到CycleRace通过记录。"
                    "当前协议没有持续心跳，不能判断发送端持续在线。"
                )
            elif metadata is not None:
                race_label = metadata.race_name.strip() or metadata.race_id
                stage_label = metadata.stage_name.strip() or metadata.stage_id
                self.widgets['receiver_status_label'].setStatus(
                    f"CycleRace: 监听中，已加载赛事 {race_label} / {stage_label}",
                    "waiting",
                )
                self.widgets['receiver_status_label'].setStyleSheet("color: #a56300;")
                self.widgets['receiver_status_label'].setToolTip(
                    f"已读取 {len(metadata.groups)} 个组别、"
                    f"{len(metadata.athletes)} 名运动员；这些资料可能来自本地缓存。"
                    "监听状态只表示本机接收服务已启动，不能判断发送端持续在线。"
                )
            elif snapshot.historical_passage_count:
                self.widgets['receiver_status_label'].setStatus(
                    "CycleRace: 监听中，"
                    f"已加载历史 {snapshot.historical_passage_count} 条",
                    "waiting",
                )
                self.widgets['receiver_status_label'].setStyleSheet("color: #a56300;")
                self.widgets['receiver_status_label'].setToolTip(
                    "历史记录已加载，但本次运行还没有收到CycleRace新数据。"
                    "监听状态只表示本机接收服务已启动，不能判断发送端持续在线。"
                )
            else:
                self.widgets['receiver_status_label'].setStatus(
                    "CycleRace: 监听中，等待数据", "waiting"
                )
                self.widgets['receiver_status_label'].setStyleSheet("color: #a56300;")
                self.widgets['receiver_status_label'].setToolTip(
                    "本机接收服务已启动，等待CycleRace主动发送数据。"
                    "当前协议没有持续心跳，不能判断发送端是否在线。"
                )
        else:
            self.widgets['receiver_status_label'].setStatus(
                "CycleRace: 异常" if snapshot.receiver_error else "CycleRace: 未监听",
                "error",
            )
            self.widgets['receiver_status_label'].setStyleSheet("color: #b54747;")
            self.widgets['receiver_status_label'].setToolTip(
                snapshot.receiver_error or "CycleRace接收服务未启动"
            )

        if snapshot.timing_provider == "racetiger":
            status = snapshot.racetiger_status
            if snapshot.racetiger_running:
                pending_count = snapshot.pending_passage_count
                if status is not None and status.state == "error":
                    self.widgets['receiver_status_label'].setStatus("赛虎: API 错误", "error")
                    self.widgets['receiver_status_label'].setStyleSheet("color: #b54747;")
                    self.widgets['receiver_status_label'].setToolTip(status.message)
                elif pending_count:
                    self.widgets['receiver_status_label'].setStatus(
                        "赛虎: 正在处理，"
                        f"已读取 {snapshot.received_passage_count}，待处理 {pending_count}",
                        "busy",
                    )
                    self.widgets['receiver_status_label'].setStyleSheet("color: #a56300;")
                    self.widgets['receiver_status_label'].setToolTip(
                        "赛虎终点记录已写入本地只读日志，正在准备视频定位"
                    )
                elif status is not None and status.state == "ok":
                    self.widgets['receiver_status_label'].setStatus(
                        f"赛虎: 已读取 {status.count} 条", "ready"
                    )
                    self.widgets['receiver_status_label'].setStyleSheet("color: #247a52;")
                    self.widgets['receiver_status_label'].setToolTip(status.message)
                else:
                    self.widgets['receiver_status_label'].setStatus("赛虎: 正在读取", "busy")
                    self.widgets['receiver_status_label'].setStyleSheet("color: #a56300;")
                    self.widgets['receiver_status_label'].setToolTip("正在轮询赛虎 FINISH 记录")
            else:
                configured = snapshot.racetiger_configured
                self.widgets['receiver_status_label'].setStatus(
                    "赛虎: 异常"
                    if snapshot.receiver_error
                    else ("赛虎: 未启动" if configured else "赛虎: 未配置"),
                    "error",
                )
                self.widgets['receiver_status_label'].setStyleSheet("color: #b54747;")
                self.widgets['receiver_status_label'].setToolTip(
                    snapshot.receiver_error or "请在设备与赛事设置中填写赛虎接口参数"
                )

        high_speed_result = snapshot.high_speed_result
        high_speed_root = snapshot.high_speed_root
        high_speed_remote = snapshot.high_speed_remote
        if high_speed_root is None:
            self.widgets['high_speed_status_label'].setStatus(
                "高速摄像: 未配置共享目录", "error"
            )
            self.widgets['high_speed_status_label'].setStyleSheet("color: #b54747;")
        elif high_speed_result.status == "checking":
            self.widgets['high_speed_status_label'].setStatus(
                (
                    "高速摄像: 正在连接共享目录"
                    if high_speed_remote
                    else "高速摄像: 正在检查本机测试目录"
                ),
                "busy",
            )
            self.widgets['high_speed_status_label'].setStyleSheet("color: #a56300;")
        elif high_speed_result.status == "unavailable":
            self.widgets['high_speed_status_label'].setStatus(
                (
                    "高速摄像: 共享目录未连接"
                    if high_speed_remote
                    else "高速摄像: 本机测试目录不可用"
                ),
                "error",
            )
            self.widgets['high_speed_status_label'].setStyleSheet("color: #b54747;")
        elif high_speed_result.waiting_file_count:
            self.widgets['high_speed_status_label'].setStatus(
                (
                    "高速摄像: 共享目录可访问，等待原厂软件完成判读"
                    if high_speed_remote
                    else "高速摄像: 本机测试目录可读，等待原厂软件完成判读"
                ),
                "waiting",
            )
            self.widgets['high_speed_status_label'].setStyleSheet("color: #a56300;")
        elif high_speed_result.status == "waiting":
            self.widgets['high_speed_status_label'].setStatus(
                (
                    "高速摄像: 共享目录可访问，等待高速画面"
                    if high_speed_remote
                    else "高速摄像: 本机测试目录可读，等待测试数据"
                ),
                "waiting",
            )
            self.widgets['high_speed_status_label'].setStyleSheet("color: #a56300;")
        else:
            self.widgets['high_speed_status_label'].setStatus(
                f"高速摄像: {'共享目录可访问' if high_speed_remote else '本机测试数据可读'}，"
                f"{len(high_speed_result.captures)} 段",
                "ready",
            )
            self.widgets['high_speed_status_label'].setStyleSheet("color: #247a52;")
        self.widgets['high_speed_status_label'].setToolTip(
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
        free_gb = snapshot.storage_free_gb
        if free_gb is not None:
            storage_color = (
                "#b54747"
                if free_gb < 5
                else "#a56300"
                if free_gb < 20
                else "#247a52"
            )
            self.widgets['storage_status_label'].setText(f"存储: {free_gb:.1f} GB")
            self.widgets['storage_status_label'].setStyleSheet(f"color: {storage_color};")
            self.widgets['storage_status_label'].setToolTip(str(snapshot.output_dir))
            if free_gb < 5:
                storage_alert = "磁盘空间严重不足"
                storage_alert_tooltip = (
                    f"证据目录仅剩 {free_gb:.1f} GB：{snapshot.output_dir}"
                )
            elif free_gb < 20:
                storage_alert = "磁盘空间不足"
                storage_alert_tooltip = (
                    f"证据目录剩余 {free_gb:.1f} GB：{snapshot.output_dir}"
                )
                storage_alert_color = "#a56300"
        elif snapshot.storage_error:
            self.widgets['storage_status_label'].setText("存储: 不可用")
            self.widgets['storage_status_label'].setStyleSheet("color: #b54747;")
            self.widgets['storage_status_label'].setToolTip(snapshot.storage_error)
            storage_alert = "存储不可用"
            storage_alert_tooltip = (
                f"无法读取证据目录磁盘状态：{snapshot.storage_error}"
            )
        else:
            self.widgets['storage_status_label'].setText("存储: 检查中")
            self.widgets['storage_status_label'].setStyleSheet("color: #667085;")
            self.widgets['storage_status_label'].setToolTip(str(snapshot.output_dir))

        counts = snapshot.capture_counts
        aligned_event_count = snapshot.aligned_event_count
        if snapshot.capture_error:
            self.widgets['capture_status_label'].setText(f"证据处理异常：{snapshot.capture_error}")
            self.widgets['capture_status_label'].setToolTip(snapshot.capture_error)
            self.widgets['capture_status_label'].setStyleSheet(
                "color: #b54747; font-weight: 700;"
            )
        else:
            alignment_text = (
                f"  |  证据日期已对齐 {aligned_event_count} 条"
                if aligned_event_count
                else ""
            )
            self.widgets['capture_status_label'].setText(
                f"本次待封口 {counts[PassageReviewState.WAITING]}  |  "
                f"本次可核对 {counts[PassageReviewState.READY]}  |  "
                f"本次缺口 {counts[PassageReviewState.PARTIAL]}  |  "
                f"已有证据 {snapshot.available_evidence_count}  |  "
                f"缺少绝对时间 {snapshot.unsupported_event_count}"
                f"{alignment_text}"
            )
            self.widgets['capture_status_label'].setToolTip(
                "仅将证据检索日期对齐到实时接收日期；CycleRace正式通过时间未改变。"
                if aligned_event_count
                else ""
            )
            self.widgets['capture_status_label'].setStyleSheet(
                "color: #667085; font-weight: 500;"
            )
        alert_entries = []
        if snapshot.capture_error:
            alert_entries.append(
                ("证据处理异常", snapshot.capture_error, "#b54747")
            )
        if storage_alert:
            alert_entries.append(
                (storage_alert, storage_alert_tooltip, storage_alert_color)
            )
        if snapshot.workspace_notice:
            alert_entries.append(
                (
                    snapshot.workspace_notice,
                    f"当前赛事目录：{snapshot.output_dir}",
                    "#a56300",
                )
            )
        if len(alert_entries) > 1:
            self.widgets['runtime_alert_label'].setText("多项运行异常")
            self.widgets['runtime_alert_label'].setToolTip(
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
            self.widgets['runtime_alert_label'].setText(title)
            self.widgets['runtime_alert_label'].setToolTip(detail)
        else:
            self.widgets['runtime_alert_label'].clear()
            self.widgets['runtime_alert_label'].setToolTip("")
            self.widgets['runtime_alert_label'].hide()
            alert_color = ""
        if alert_color:
            self.widgets['runtime_alert_label'].setStyleSheet(
                f"color: {alert_color}; font-size: 9pt; font-weight: 700;"
            )
            self.widgets['runtime_alert_label'].show()
