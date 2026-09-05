"""Entry point for the standalone finish review console."""

from __future__ import annotations

import argparse
import json
import logging
import math
import multiprocessing
import os
import sys
import tempfile
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont, QIcon
from PyQt5.QtWidgets import QApplication, QMessageBox

from realtime.auyat_rgb import discover_auyat_root
from realtime.passage_review import UI_BASE_FONT_POINT_SIZE, UI_FONT_FAMILY
from realtime.passage_receiver import DEFAULT_HOST, DEFAULT_PORT
from realtime.review_recorder import (
    is_supported_review_source,
    make_directshow_source,
)
from realtime.review_window import FinishReviewWindow
from realtime.runtime_paths import (
    application_dir,
    resource_dir,
    resolve_output_dir,
    resolve_runtime_path,
)
from realtime.secure_storage import protect_secret, unprotect_secret
from realtime.settings import FinishReviewSettings
from realtime.thread_lifecycle import install_qthread_shutdown
from realtime.stream_recorder import (
    apply_rtsp_credentials,
    is_rtsp_source,
    sanitize_recording_message,
    split_rtsp_credentials,
)


logger = logging.getLogger("FinishReview.Entry")
_MANAGED_LOG_HANDLER = "_finish_review_managed_handler"
APPLICATION_ICON_RESOURCE = "assets/finishreview.ico"


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="FinishReview · 终点多源复核")
    source_group = parser.add_mutually_exclusive_group()
    source_group.add_argument("--source", help="安装人员使用的网络录像源地址")
    source_group.add_argument("--usb-device", help="安装人员使用的 Windows 摄像头名称")
    parser.add_argument("--usb-video-size", help="可选 USB 摄像头分辨率，如 1920x1080")
    parser.add_argument("--usb-framerate", type=float, help="可选 USB 摄像头帧率")
    parser.add_argument(
        "--install-source",
        action="store_true",
        help="保存指定录像源供日常启动使用，然后退出",
    )
    parser.add_argument("--output", help="赛事数据目录")
    parser.add_argument(
        "--high-speed-dir",
        help="高速摄像电脑共享的原厂数据目录，可使用 UNC 路径",
    )
    parser.add_argument("--passage-host", default=DEFAULT_HOST)
    parser.add_argument("--passage-port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--camera-index", type=int)
    parser.add_argument("--ffmpeg", help="可选 FFmpeg 路径")
    parser.add_argument(
        "--auto-record",
        action="store_true",
        help="启动后自动开始录像，日常操作默认使用界面按钮",
    )
    parser.add_argument("--smoke-test", action="store_true", help=argparse.SUPPRESS)
    return parser


def requested_source(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> str:
    """Resolve installer-only source arguments to the persisted source URI."""

    if args.usb_device:
        try:
            source = make_directshow_source(
                args.usb_device,
                video_size=args.usb_video_size,
                framerate=args.usb_framerate,
            )
        except (TypeError, ValueError) as exc:
            parser.error(str(exc))
    else:
        if args.usb_video_size or args.usb_framerate is not None:
            parser.error("USB 分辨率和帧率只能与 --usb-device 一起使用")
        source = str(args.source or "").strip()
    if source and not is_supported_review_source(source):
        parser.error("指定的录像源不受支持")
    if args.install_source and not source:
        parser.error("--install-source 需要同时指定 --source 或 --usb-device")
    return source


def default_race_dir() -> Path:
    return (Path.home() / "Desktop" / "race").resolve()


def default_config_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    root = (
        Path(local_app_data)
        if local_app_data
        else Path.home() / "AppData" / "Local"
    )
    return root / "FinishReview" / "config.json"


def default_log_path() -> Path:
    return default_config_path().parent / "logs" / "finish_review.log"


def configure_runtime_logging(log_path: Path | None = None) -> Path:
    """Configure a bounded UTF-8 log shared by all FinishReview components."""

    resolved_path = Path(log_path or default_log_path()).expanduser().resolve()
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    application_logger = logging.getLogger("FinishReview")
    for handler in tuple(application_logger.handlers):
        if getattr(handler, _MANAGED_LOG_HANDLER, False):
            application_logger.removeHandler(handler)
            handler.close()
    handler = RotatingFileHandler(
        resolved_path,
        maxBytes=2 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    setattr(handler, _MANAGED_LOG_HANDLER, True)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(threadName)s: %(message)s"
        )
    )
    application_logger.addHandler(handler)
    application_logger.setLevel(logging.INFO)
    application_logger.propagate = False
    return resolved_path


def install_exception_logging() -> None:
    """Record otherwise-unhandled main-thread and worker-thread exceptions."""

    if not getattr(sys.excepthook, _MANAGED_LOG_HANDLER, False):
        previous_sys_hook = sys.excepthook

        def log_unhandled_exception(exc_type, exc_value, traceback) -> None:
            if issubclass(exc_type, KeyboardInterrupt):
                previous_sys_hook(exc_type, exc_value, traceback)
                return
            logger.critical(
                "Unhandled application exception",
                exc_info=(exc_type, exc_value, traceback),
            )

        setattr(log_unhandled_exception, _MANAGED_LOG_HANDLER, True)
        sys.excepthook = log_unhandled_exception

    if not getattr(threading.excepthook, _MANAGED_LOG_HANDLER, False):
        previous_thread_hook = threading.excepthook

        def log_unhandled_thread_exception(args: threading.ExceptHookArgs) -> None:
            if args.exc_type is SystemExit:
                previous_thread_hook(args)
                return
            logger.critical(
                "Unhandled exception in thread %s",
                args.thread.name if args.thread is not None else "unknown",
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
            )

        setattr(log_unhandled_thread_exception, _MANAGED_LOG_HANDLER, True)
        threading.excepthook = log_unhandled_thread_exception


def _load_secret(
    payload: dict,
    protected_key: str,
    legacy_key: str,
    secret_unprotector: Callable[[str], str],
) -> str:
    protected_value = str(payload.get(protected_key) or "")
    if protected_value:
        try:
            return secret_unprotector(protected_value)
        except Exception:  # noqa: BLE001 - an unreadable secret must not hide other settings.
            logger.warning("Could not decrypt saved setting %s", protected_key)
            return ""
    return str(payload.get(legacy_key) or "")


def _existing_config_payload(config_path: Path) -> dict:
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _review_settings_payload(config_path: Path) -> dict:
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as error:
        logger.warning(
            "Could not load saved configuration (%s)",
            type(error).__name__,
        )
        return {}
    if not isinstance(payload, dict):
        logger.warning(
            "Ignoring saved configuration: configuration root must be an object"
        )
        return {}
    return payload


def _warn_invalid_setting(name: str, error: Exception) -> None:
    logger.warning(
        "Ignoring invalid saved setting %s (%s)",
        name,
        type(error).__name__,
    )


def _protected_secret_for_save(
    existing_payload: dict,
    protected_key: str,
    secret: str,
    secret_protector: Callable[[str], str],
    secret_unprotector: Callable[[str], str],
) -> str:
    if secret:
        return secret_protector(secret)
    existing_value = str(existing_payload.get(protected_key) or "")
    if not existing_value:
        return ""
    try:
        secret_unprotector(existing_value)
    except Exception:  # noqa: BLE001 - preserve unreadable ciphertext verbatim.
        logger.warning("Preserving unreadable saved setting %s", protected_key)
        return existing_value
    return ""


def load_review_settings(
    config_path: Path,
    *,
    output_dir: Path | None,
    passage_host: str,
    passage_port: int,
    camera_index: int | None,
    high_speed_dir: Path | None = None,
    secret_unprotector: Callable[[str], str] = unprotect_secret,
) -> FinishReviewSettings:
    source = ""
    secondary_source = ""
    saved_output_dir = None
    saved_camera_index = None
    saved_high_speed_dir = None
    timing_provider = "cyclerace"
    racetiger_base_url = ""
    racetiger_pc = ""
    racetiger_rid = ""
    racetiger_token = ""
    racetiger_poll_interval_seconds = 2.0
    visual_detection_enabled = True
    visual_camera_index = 1
    visual_finish_line = 0.50
    visual_gate_width = 0.08
    visual_forward_direction = "left_to_right"
    visual_roi_top = 0.08
    visual_roi_bottom = 0.95
    finishreview_ip = "192.168.50.10"
    cyclerace_ip = "192.168.50.20"
    high_speed_pc_ip = "192.168.50.30"
    switch_ip = "192.168.50.2"
    payload = _review_settings_payload(config_path)

    try:
        candidate = str(payload.get("source") or "").strip()
        clean_candidate, legacy_rtsp_username, legacy_rtsp_password = (
            split_rtsp_credentials(candidate)
        )
        rtsp_username = str(
            payload.get("rtsp_username") or legacy_rtsp_username
        ).strip()
        rtsp_password = _load_secret(
            payload,
            "rtsp_password_protected",
            "rtsp_password",
            secret_unprotector,
        ) or legacy_rtsp_password
        candidate = apply_rtsp_credentials(
            clean_candidate,
            rtsp_username,
            rtsp_password,
        )
        if is_supported_review_source(candidate):
            source = candidate
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        _warn_invalid_setting("source", error)

    try:
        secondary_candidate = str(payload.get("secondary_source") or "").strip()
        (
            clean_secondary_candidate,
            legacy_secondary_username,
            legacy_secondary_password,
        ) = split_rtsp_credentials(secondary_candidate)
        secondary_username = str(
            payload.get("secondary_rtsp_username") or legacy_secondary_username
        ).strip()
        secondary_password = _load_secret(
            payload,
            "secondary_rtsp_password_protected",
            "secondary_rtsp_password",
            secret_unprotector,
        ) or legacy_secondary_password
        secondary_candidate = apply_rtsp_credentials(
            clean_secondary_candidate,
            secondary_username,
            secondary_password,
        )
        if is_supported_review_source(secondary_candidate):
            secondary_source = secondary_candidate
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        _warn_invalid_setting("secondary_source", error)

    try:
        output_candidate = str(payload.get("output_dir") or "").strip()
        if output_candidate:
            saved_output_dir = Path(output_candidate).expanduser().resolve()
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        _warn_invalid_setting("output_dir", error)

    try:
        camera_candidate = int(payload.get("camera_index", 0))
        if camera_candidate > 0:
            saved_camera_index = camera_candidate
    except (TypeError, ValueError) as error:
        _warn_invalid_setting("camera_index", error)

    try:
        high_speed_candidate = str(payload.get("high_speed_dir") or "").strip()
        if high_speed_candidate:
            saved_high_speed_dir = Path(high_speed_candidate).expanduser().absolute()
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        _warn_invalid_setting("high_speed_dir", error)

    candidate_provider = str(payload.get("timing_provider") or "").strip().lower()
    if candidate_provider in {"cyclerace", "racetiger"}:
        timing_provider = candidate_provider
    racetiger_base_url = str(payload.get("racetiger_base_url") or "").strip()
    racetiger_pc = str(payload.get("racetiger_pc") or "").strip()
    racetiger_rid = str(payload.get("racetiger_rid") or "").strip()
    finishreview_ip = str(payload.get("finishreview_ip") or finishreview_ip).strip()
    cyclerace_ip = str(payload.get("cyclerace_ip") or cyclerace_ip).strip()
    high_speed_pc_ip = str(
        payload.get("high_speed_pc_ip") or high_speed_pc_ip
    ).strip()
    switch_ip = str(payload.get("switch_ip") or switch_ip).strip()

    try:
        racetiger_token = _load_secret(
            payload,
            "racetiger_token_protected",
            "racetiger_token",
            secret_unprotector,
        )
    except (TypeError, ValueError) as error:
        _warn_invalid_setting("racetiger_token", error)

    try:
        poll_interval_candidate = float(
            payload.get("racetiger_poll_interval_seconds", 2.0)
        )
        if not math.isfinite(poll_interval_candidate):
            raise ValueError("poll interval must be finite")
        racetiger_poll_interval_seconds = max(0.5, poll_interval_candidate)
    except (TypeError, ValueError) as error:
        _warn_invalid_setting("racetiger_poll_interval_seconds", error)

    enabled_value = payload.get("visual_detection_enabled", True)
    visual_detection_enabled = (
        enabled_value.strip().casefold() not in {"0", "false", "no", "off"}
        if isinstance(enabled_value, str)
        else bool(enabled_value)
    )
    try:
        visual_camera_index = max(1, int(payload.get("visual_camera_index", 1)))
        visual_finish_line = max(0.10, min(0.90, float(payload.get("visual_finish_line", 0.50))))
        visual_gate_width = max(0.02, min(0.30, float(payload.get("visual_gate_width", 0.08))))
        visual_roi_top = max(0.0, min(0.80, float(payload.get("visual_roi_top", 0.08))))
        visual_roi_bottom = max(0.20, min(1.0, float(payload.get("visual_roi_bottom", 0.95))))
        if not all(
            math.isfinite(value)
            for value in (
                visual_finish_line,
                visual_gate_width,
                visual_roi_top,
                visual_roi_bottom,
            )
        ):
            raise ValueError("visual settings must be finite")
        if visual_roi_bottom <= visual_roi_top:
            raise ValueError("visual ROI bottom must be below top")
    except (TypeError, ValueError) as error:
        _warn_invalid_setting("visual_detection", error)
        visual_camera_index = 1
        visual_finish_line = 0.50
        visual_gate_width = 0.08
        visual_roi_top = 0.08
        visual_roi_bottom = 0.95
    direction_candidate = str(payload.get("visual_forward_direction") or "left_to_right").strip()
    if direction_candidate in {"left_to_right", "right_to_left"}:
        visual_forward_direction = direction_candidate

    return FinishReviewSettings(
        source=source,
        secondary_source=secondary_source,
        output_dir=(output_dir or saved_output_dir or default_race_dir()).resolve(),
        passage_host=passage_host,
        passage_port=passage_port,
        camera_index=camera_index or saved_camera_index or 1,
        high_speed_dir=(
            high_speed_dir
            or saved_high_speed_dir
            or discover_auyat_root()
        ),
        finishreview_ip=finishreview_ip,
        cyclerace_ip=cyclerace_ip,
        high_speed_pc_ip=high_speed_pc_ip,
        switch_ip=switch_ip,
        timing_provider=timing_provider,
        racetiger_base_url=racetiger_base_url,
        racetiger_pc=racetiger_pc,
        racetiger_rid=racetiger_rid,
        racetiger_token=racetiger_token,
        racetiger_poll_interval_seconds=racetiger_poll_interval_seconds,
        visual_detection_enabled=visual_detection_enabled,
        visual_camera_index=visual_camera_index,
        visual_finish_line=visual_finish_line,
        visual_gate_width=visual_gate_width,
        visual_forward_direction=visual_forward_direction,
        visual_roi_top=visual_roi_top,
        visual_roi_bottom=visual_roi_bottom,
    )


def save_review_settings(
    config_path: Path,
    settings: FinishReviewSettings,
    *,
    secret_protector: Callable[[str], str] = protect_secret,
    secret_unprotector: Callable[[str], str] = unprotect_secret,
) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    existing_payload = _existing_config_payload(config_path)
    clean_source, rtsp_username, rtsp_password = split_rtsp_credentials(
        settings.source
    )
    (
        clean_secondary_source,
        secondary_rtsp_username,
        secondary_rtsp_password,
    ) = split_rtsp_credentials(settings.secondary_source)
    payload = dict(existing_payload)
    payload.update({
        "schema_version": 8,
        "source": clean_source,
        "rtsp_username": rtsp_username,
        "rtsp_password_protected": _protected_secret_for_save(
            existing_payload,
            "rtsp_password_protected",
            rtsp_password,
            secret_protector,
            secret_unprotector,
        ),
        "secondary_source": clean_secondary_source,
        "secondary_rtsp_username": secondary_rtsp_username,
        "secondary_rtsp_password_protected": _protected_secret_for_save(
            existing_payload,
            "secondary_rtsp_password_protected",
            secondary_rtsp_password,
            secret_protector,
            secret_unprotector,
        ),
        "output_dir": str(settings.output_dir),
        "camera_index": settings.camera_index,
        "high_speed_dir": (
            str(settings.high_speed_dir) if settings.high_speed_dir is not None else ""
        ),
        "finishreview_ip": settings.finishreview_ip,
        "cyclerace_ip": settings.cyclerace_ip,
        "high_speed_pc_ip": settings.high_speed_pc_ip,
        "switch_ip": settings.switch_ip,
        "timing_provider": settings.timing_provider,
        "racetiger_base_url": settings.racetiger_base_url,
        "racetiger_pc": settings.racetiger_pc,
        "racetiger_rid": settings.racetiger_rid,
        "racetiger_token_protected": _protected_secret_for_save(
            existing_payload,
            "racetiger_token_protected",
            settings.racetiger_token,
            secret_protector,
            secret_unprotector,
        ),
        "racetiger_poll_interval_seconds": settings.racetiger_poll_interval_seconds,
        "visual_detection_enabled": settings.visual_detection_enabled,
        "visual_camera_index": settings.visual_camera_index,
        "visual_finish_line": settings.visual_finish_line,
        "visual_gate_width": settings.visual_gate_width,
        "visual_forward_direction": settings.visual_forward_direction,
        "visual_roi_top": settings.visual_roi_top,
        "visual_roi_bottom": settings.visual_roi_bottom,
    })
    payload.pop("rtsp_password", None)
    payload.pop("secondary_rtsp_password", None)
    payload.pop("racetiger_token", None)
    temporary_path = config_path.with_suffix(config_path.suffix + ".tmp")
    with temporary_path.open("wb") as output:
        output.write(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        )
        output.write(b"\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary_path, config_path)


def configure_application_font(app: QApplication) -> None:
    application_font = QFont(UI_FONT_FAMILY)
    application_font.setPointSize(UI_BASE_FONT_POINT_SIZE)
    app.setFont(application_font)


def configure_application_icon(app: QApplication) -> None:
    icon_path = resource_dir(APPLICATION_ICON_RESOURCE)
    if not icon_path.is_file():
        return
    icon = QIcon(str(icon_path))
    if not icon.isNull():
        app.setWindowIcon(icon)


def run_packaged_smoke_test(app_argv: list[str]) -> int:
    """Create the packaged Qt window without starting external services."""

    app = QApplication.instance() or QApplication(app_argv)
    install_qthread_shutdown(app)
    configure_application_font(app)
    configure_application_icon(app)
    with tempfile.TemporaryDirectory(prefix="FinishReview-smoke-") as temp_dir:
        window = FinishReviewWindow(
            "",
            Path(temp_dir),
            passage_host="127.0.0.1",
            passage_port=0,
            high_speed_dir=None,
        )
        window.setAttribute(Qt.WA_DontShowOnScreen, True)
        window.show()
        app.processEvents()
        if not window.isVisible() or int(window.winId()) == 0:
            raise RuntimeError("packaged FinishReview window did not initialize")
        window.close()
        app.processEvents()
    logger.info("Packaged Qt window smoke test passed")
    return 0


def main(argv: list[str] | None = None) -> int:
    multiprocessing.freeze_support()
    logging_error = ""
    try:
        log_path = configure_runtime_logging()
    except Exception as exc:  # noqa: BLE001 - GUI startup must remain diagnosable.
        logging_error = sanitize_recording_message(exc)
        log_path = None
    install_exception_logging()
    logger.info("FinishReview starting%s", f"; log={log_path}" if log_path else "")
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    app_argv = [sys.argv[0]] if argv is not None else sys.argv
    if getattr(args, "smoke_test", False):
        return run_packaged_smoke_test(app_argv)
    source_override = requested_source(args, parser)
    runtime_root = application_dir(module_file=__file__)
    requested_output_dir = (
        resolve_output_dir(args.output, base_dir=runtime_root)
        if args.output
        else None
    )
    ffmpeg_path = (
        resolve_runtime_path(args.ffmpeg, base_dir=runtime_root)
        if args.ffmpeg
        else None
    )
    config_path = default_config_path()
    saved_settings = load_review_settings(
        config_path,
        output_dir=requested_output_dir,
        passage_host=args.passage_host,
        passage_port=args.passage_port,
        camera_index=args.camera_index,
        high_speed_dir=(
            Path(args.high_speed_dir).expanduser().absolute()
            if args.high_speed_dir
            else None
        ),
    )
    if args.install_source:
        settings = FinishReviewSettings(
            source=source_override,
            secondary_source=saved_settings.secondary_source,
            output_dir=requested_output_dir or saved_settings.output_dir,
            passage_host=args.passage_host,
            passage_port=args.passage_port,
            camera_index=args.camera_index or saved_settings.camera_index,
            high_speed_dir=saved_settings.high_speed_dir,
            finishreview_ip=saved_settings.finishreview_ip,
            cyclerace_ip=saved_settings.cyclerace_ip,
            high_speed_pc_ip=saved_settings.high_speed_pc_ip,
            switch_ip=saved_settings.switch_ip,
            timing_provider=saved_settings.timing_provider,
            racetiger_base_url=saved_settings.racetiger_base_url,
            racetiger_pc=saved_settings.racetiger_pc,
            racetiger_rid=saved_settings.racetiger_rid,
            racetiger_token=saved_settings.racetiger_token,
            racetiger_poll_interval_seconds=saved_settings.racetiger_poll_interval_seconds,
            visual_detection_enabled=saved_settings.visual_detection_enabled,
            visual_camera_index=saved_settings.visual_camera_index,
            visual_finish_line=saved_settings.visual_finish_line,
            visual_gate_width=saved_settings.visual_gate_width,
            visual_forward_direction=saved_settings.visual_forward_direction,
            visual_roi_top=saved_settings.visual_roi_top,
            visual_roi_bottom=saved_settings.visual_roi_bottom,
        )
        try:
            save_review_settings(config_path, settings)
        except Exception as exc:  # noqa: BLE001 - installer must report DPAPI failures.
            parser.error(
                f"无法保存录像设备配置: {sanitize_recording_message(exc)}"
            )
        return 0

    app = QApplication.instance() or QApplication(app_argv)
    install_qthread_shutdown(app)
    configure_application_font(app)
    configure_application_icon(app)
    settings = FinishReviewSettings(
        source=source_override or saved_settings.source,
        secondary_source=saved_settings.secondary_source,
        output_dir=saved_settings.output_dir,
        passage_host=args.passage_host,
        passage_port=args.passage_port,
        camera_index=(
            args.camera_index or saved_settings.camera_index
            if source_override
            else saved_settings.camera_index
        ),
        high_speed_dir=saved_settings.high_speed_dir,
        finishreview_ip=saved_settings.finishreview_ip,
        cyclerace_ip=saved_settings.cyclerace_ip,
        high_speed_pc_ip=saved_settings.high_speed_pc_ip,
        switch_ip=saved_settings.switch_ip,
        timing_provider=saved_settings.timing_provider,
        racetiger_base_url=saved_settings.racetiger_base_url,
        racetiger_pc=saved_settings.racetiger_pc,
        racetiger_rid=saved_settings.racetiger_rid,
        racetiger_token=saved_settings.racetiger_token,
        racetiger_poll_interval_seconds=saved_settings.racetiger_poll_interval_seconds,
        visual_detection_enabled=saved_settings.visual_detection_enabled,
        visual_camera_index=saved_settings.visual_camera_index,
        visual_finish_line=saved_settings.visual_finish_line,
        visual_gate_width=saved_settings.visual_gate_width,
        visual_forward_direction=saved_settings.visual_forward_direction,
        visual_roi_top=saved_settings.visual_roi_top,
        visual_roi_bottom=saved_settings.visual_roi_bottom,
    )

    window = FinishReviewWindow(
        settings.source,
        settings.output_dir,
        passage_host=settings.passage_host,
        passage_port=settings.passage_port,
        camera_index=settings.camera_index,
        secondary_source=settings.secondary_source,
        high_speed_dir=settings.high_speed_dir,
        finishreview_ip=settings.finishreview_ip,
        cyclerace_ip=settings.cyclerace_ip,
        high_speed_pc_ip=settings.high_speed_pc_ip,
        switch_ip=settings.switch_ip,
        timing_provider=settings.timing_provider,
        racetiger_base_url=settings.racetiger_base_url,
        racetiger_pc=settings.racetiger_pc,
        racetiger_rid=settings.racetiger_rid,
        racetiger_token=settings.racetiger_token,
        racetiger_poll_interval_seconds=settings.racetiger_poll_interval_seconds,
        visual_detection_enabled=settings.visual_detection_enabled,
        visual_camera_index=settings.visual_camera_index,
        visual_finish_line=settings.visual_finish_line,
        visual_gate_width=settings.visual_gate_width,
        visual_forward_direction=settings.visual_forward_direction,
        visual_roi_top=settings.visual_roi_top,
        visual_roi_bottom=settings.visual_roi_bottom,
        video_assist_enabled=False,
        ffmpeg_path=Path(ffmpeg_path) if ffmpeg_path else None,
        settings_saver=lambda updated: save_review_settings(config_path, updated),
    )
    window.showMaximized()
    app.processEvents()
    if logging_error:
        QMessageBox.warning(
            window,
            "日志不可用",
            f"无法创建运行日志：{logging_error}",
        )
    try:
        window.start_receiver()
    except Exception as exc:  # noqa: BLE001 - the console remains usable for setup.
        QMessageBox.warning(
            window,
            (
                "赛虎读取未启动"
                if settings.timing_provider == "racetiger"
                else "CycleRace监听未启动"
            ),
            sanitize_recording_message(exc),
        )
    if args.auto_record and is_supported_review_source(settings.source):
        try:
            window.start_recording()
        except Exception as exc:  # noqa: BLE001 - GUI boundary reports device failures.
            QMessageBox.critical(
                window,
                "自动录像启动失败",
                sanitize_recording_message(exc),
            )
    result = app.exec_()
    logger.info("FinishReview stopped with exit code %s", result)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
