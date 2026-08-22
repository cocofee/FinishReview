"""Entry point for the standalone finish review console."""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import sys
from pathlib import Path

from PyQt5.QtWidgets import QApplication, QMessageBox

from realtime.auyat_rgb import discover_auyat_root
from realtime.passage_receiver import DEFAULT_HOST, DEFAULT_PORT
from realtime.review_recorder import (
    is_supported_review_source,
    make_directshow_source,
)
from realtime.review_window import (
    FinishReviewSettings,
    FinishReviewWindow,
)
from realtime.runtime_paths import (
    application_dir,
    resolve_output_dir,
    resolve_runtime_path,
)
from realtime.stream_recorder import sanitize_recording_message


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="终点复核系统")
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


def load_review_settings(
    config_path: Path,
    *,
    output_dir: Path | None,
    passage_host: str,
    passage_port: int,
    camera_index: int | None,
    high_speed_dir: Path | None = None,
) -> FinishReviewSettings:
    source = ""
    saved_output_dir = None
    saved_camera_index = None
    saved_high_speed_dir = None
    timing_provider = "cyclerace"
    racetiger_base_url = ""
    racetiger_pc = ""
    racetiger_rid = ""
    racetiger_token = ""
    racetiger_poll_interval_seconds = 2.0
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        candidate = str(payload.get("source") or "").strip()
        if is_supported_review_source(candidate):
            source = candidate
        output_candidate = str(payload.get("output_dir") or "").strip()
        if output_candidate:
            saved_output_dir = Path(output_candidate).expanduser().resolve()
        camera_candidate = int(payload.get("camera_index", 0))
        if camera_candidate > 0:
            saved_camera_index = camera_candidate
        high_speed_candidate = str(payload.get("high_speed_dir") or "").strip()
        if high_speed_candidate:
            saved_high_speed_dir = Path(high_speed_candidate).expanduser().absolute()
        candidate_provider = str(payload.get("timing_provider") or "").strip().lower()
        if candidate_provider in {"cyclerace", "racetiger"}:
            timing_provider = candidate_provider
        racetiger_base_url = str(payload.get("racetiger_base_url") or "").strip()
        racetiger_pc = str(payload.get("racetiger_pc") or "").strip()
        racetiger_rid = str(payload.get("racetiger_rid") or "").strip()
        racetiger_token = str(payload.get("racetiger_token") or "")
        try:
            racetiger_poll_interval_seconds = max(
                0.5,
                float(payload.get("racetiger_poll_interval_seconds", 2.0)),
            )
        except (TypeError, ValueError):
            racetiger_poll_interval_seconds = 2.0
    except (OSError, TypeError, ValueError):
        pass
    return FinishReviewSettings(
        source=source,
        output_dir=(output_dir or saved_output_dir or default_race_dir()).resolve(),
        passage_host=passage_host,
        passage_port=passage_port,
        camera_index=camera_index or saved_camera_index or 1,
        high_speed_dir=(
            high_speed_dir
            or saved_high_speed_dir
            or discover_auyat_root()
        ),
        timing_provider=timing_provider,
        racetiger_base_url=racetiger_base_url,
        racetiger_pc=racetiger_pc,
        racetiger_rid=racetiger_rid,
        racetiger_token=racetiger_token,
        racetiger_poll_interval_seconds=racetiger_poll_interval_seconds,
    )


def save_review_settings(config_path: Path, settings: FinishReviewSettings) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 5,
        "source": settings.source,
        "output_dir": str(settings.output_dir),
        "camera_index": settings.camera_index,
        "high_speed_dir": (
            str(settings.high_speed_dir) if settings.high_speed_dir is not None else ""
        ),
        "timing_provider": settings.timing_provider,
        "racetiger_base_url": settings.racetiger_base_url,
        "racetiger_pc": settings.racetiger_pc,
        "racetiger_rid": settings.racetiger_rid,
        "racetiger_token": settings.racetiger_token,
        "racetiger_poll_interval_seconds": settings.racetiger_poll_interval_seconds,
    }
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


def main(argv: list[str] | None = None) -> int:
    multiprocessing.freeze_support()
    parser = build_argument_parser()
    args = parser.parse_args(argv)
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
            output_dir=requested_output_dir or saved_settings.output_dir,
            passage_host=args.passage_host,
            passage_port=args.passage_port,
            camera_index=args.camera_index or saved_settings.camera_index,
            high_speed_dir=saved_settings.high_speed_dir,
            timing_provider=saved_settings.timing_provider,
            racetiger_base_url=saved_settings.racetiger_base_url,
            racetiger_pc=saved_settings.racetiger_pc,
            racetiger_rid=saved_settings.racetiger_rid,
            racetiger_token=saved_settings.racetiger_token,
            racetiger_poll_interval_seconds=saved_settings.racetiger_poll_interval_seconds,
        )
        try:
            save_review_settings(config_path, settings)
        except OSError as exc:
            parser.error(f"无法保存录像设备配置: {exc}")
        return 0

    app_argv = [sys.argv[0]] if argv is not None else sys.argv
    app = QApplication.instance() or QApplication(app_argv)
    settings = FinishReviewSettings(
        source=source_override or saved_settings.source,
        output_dir=saved_settings.output_dir,
        passage_host=args.passage_host,
        passage_port=args.passage_port,
        camera_index=(
            args.camera_index or saved_settings.camera_index
            if source_override
            else saved_settings.camera_index
        ),
        high_speed_dir=saved_settings.high_speed_dir,
        timing_provider=saved_settings.timing_provider,
        racetiger_base_url=saved_settings.racetiger_base_url,
        racetiger_pc=saved_settings.racetiger_pc,
        racetiger_rid=saved_settings.racetiger_rid,
        racetiger_token=saved_settings.racetiger_token,
        racetiger_poll_interval_seconds=saved_settings.racetiger_poll_interval_seconds,
    )

    window = FinishReviewWindow(
        settings.source,
        settings.output_dir,
        passage_host=settings.passage_host,
        passage_port=settings.passage_port,
        camera_index=settings.camera_index,
        high_speed_dir=settings.high_speed_dir,
        timing_provider=settings.timing_provider,
        racetiger_base_url=settings.racetiger_base_url,
        racetiger_pc=settings.racetiger_pc,
        racetiger_rid=settings.racetiger_rid,
        racetiger_token=settings.racetiger_token,
        racetiger_poll_interval_seconds=settings.racetiger_poll_interval_seconds,
        ffmpeg_path=Path(ffmpeg_path) if ffmpeg_path else None,
        settings_saver=lambda updated: save_review_settings(config_path, updated),
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
    window.showMaximized()
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
