import json
import logging
import os
import socket
import sys
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QApplication

from realtime import review_main
from realtime.review_main import (
    build_argument_parser,
    load_review_settings,
    requested_source,
    save_review_settings,
)
from realtime.review_recorder import parse_directshow_source
from realtime.review_window import FinishReviewSettings
from realtime.secure_storage import (
    SecretProtectionError,
    protect_secret,
    unprotect_secret,
)


def _protect_for_test(value: str) -> str:
    return f"test-protected:{value.encode('utf-8').hex()}" if value else ""


def _unprotect_for_test(value: str) -> str:
    prefix = "test-protected:"
    if not value.startswith(prefix):
        raise ValueError("invalid test secret")
    return bytes.fromhex(value.removeprefix(prefix)).decode("utf-8")


def test_main_shows_real_window_before_entering_event_loop(monkeypatch, tmp_path):
    app = QApplication.instance() or QApplication([])
    captured_windows = []

    class _CapturingFinishReviewWindow(review_main.FinishReviewWindow):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            captured_windows.append(self)

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        passage_port = probe.getsockname()[1]

    monkeypatch.setattr(
        review_main,
        "FinishReviewWindow",
        _CapturingFinishReviewWindow,
    )
    monkeypatch.setattr(
        review_main,
        "default_config_path",
        lambda: tmp_path / "config.json",
    )
    monkeypatch.setattr(review_main, "discover_auyat_root", lambda: None)
    monkeypatch.setattr(review_main, "install_exception_logging", lambda: None)
    monkeypatch.setattr(
        review_main,
        "configure_runtime_logging",
        lambda: tmp_path / "finish_review.log",
    )
    QTimer.singleShot(50, app.quit)

    result = review_main.main(
        [
            "--output",
            str(tmp_path / "race"),
            "--passage-host",
            "127.0.0.1",
            "--passage-port",
            str(passage_port),
        ]
    )

    assert result == 0
    assert len(captured_windows) == 1
    window = captured_windows[0]
    assert window.isVisible()
    assert int(window.winId()) != 0
    assert window.receiver is not None
    assert window.receiver.is_running
    window.close()
    app.processEvents()


def test_main_shows_window_before_receiver_failure_message(monkeypatch, tmp_path):
    events = []

    class _FakeApplication:
        @classmethod
        def instance(cls):
            return None

        def __init__(self, _argv):
            pass

        def processEvents(self):
            events.append("process-events")

        def setFont(self, _font):
            events.append("font")

        def exec_(self):
            events.append("event-loop")
            return 0

    class _FailingWindow:
        def __init__(self, *_args, **_kwargs):
            self.visible = False

        def showMaximized(self):
            self.visible = True
            events.append("show")

        def start_receiver(self):
            events.append("start-receiver")
            assert self.visible is True
            raise RuntimeError("receiver unavailable")

    def warning(parent, *_args):
        assert parent.visible is True
        events.append("warning")

    monkeypatch.setattr(review_main, "QApplication", _FakeApplication)
    monkeypatch.setattr(review_main, "FinishReviewWindow", _FailingWindow)
    monkeypatch.setattr(review_main.QMessageBox, "warning", warning)
    monkeypatch.setattr(review_main, "discover_auyat_root", lambda: None)
    monkeypatch.setattr(review_main, "install_exception_logging", lambda: None)
    monkeypatch.setattr(
        review_main,
        "default_config_path",
        lambda: tmp_path / "config.json",
    )
    monkeypatch.setattr(
        review_main,
        "configure_runtime_logging",
        lambda: tmp_path / "finish_review.log",
    )

    assert review_main.main(["--output", str(tmp_path / "race")]) == 0
    assert events == [
        "font",
        "show",
        "process-events",
        "start-receiver",
        "warning",
        "event-loop",
    ]


def test_packaged_smoke_test_creates_real_qt_window(monkeypatch):
    app = QApplication.instance() or QApplication([])
    captured_windows = []

    class _CapturingFinishReviewWindow(review_main.FinishReviewWindow):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            captured_windows.append(self)

    monkeypatch.setattr(
        review_main,
        "FinishReviewWindow",
        _CapturingFinishReviewWindow,
    )

    assert review_main.run_packaged_smoke_test(["finish-review-smoke"]) == 0
    assert len(captured_windows) == 1
    assert not captured_windows[0].isVisible()
    app.processEvents()


def test_installer_arguments_build_usb_camera_source():
    parser = build_argument_parser()
    args = parser.parse_args(
        [
            "--usb-device",
            "DJI Osmo Action 5 Pro",
            "--usb-video-size",
            "1920x1080",
            "--usb-framerate",
            "30",
            "--install-source",
        ]
    )

    source = requested_source(args, parser)
    parsed = parse_directshow_source(source)

    assert parsed is not None
    assert parsed.device_name == "DJI Osmo Action 5 Pro"
    assert parsed.video_size == "1920x1080"
    assert parsed.framerate == 30.0


def test_installer_rejects_usb_options_without_usb_device():
    parser = build_argument_parser()
    args = parser.parse_args(["--usb-framerate", "30"])

    with pytest.raises(SystemExit):
        requested_source(args, parser)


def test_review_settings_round_trip_installed_usb_source(tmp_path):
    parser = build_argument_parser()
    args = parser.parse_args(
        ["--usb-device", "DJI Osmo Action 5 Pro", "--install-source"]
    )
    source = requested_source(args, parser)
    config_path = tmp_path / "finish_review_config.json"
    settings = FinishReviewSettings(
        source=source,
        output_dir=tmp_path / "race",
        passage_host="127.0.0.1",
        passage_port=18765,
        camera_index=1,
        high_speed_dir=Path(r"\\FINISH-RGB\AuyatData"),
    )

    save_review_settings(
        config_path,
        settings,
        secret_protector=_protect_for_test,
    )
    loaded = load_review_settings(
        config_path,
        output_dir=tmp_path / "other-race",
        passage_host="0.0.0.0",
        passage_port=20000,
        camera_index=2,
        secret_unprotector=_unprotect_for_test,
    )

    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 6
    assert payload["high_speed_dir"] == str(settings.high_speed_dir)
    assert payload["output_dir"] == str(settings.output_dir)
    assert payload["camera_index"] == 1
    assert loaded.source == source
    assert loaded.output_dir == tmp_path / "other-race"
    assert loaded.passage_host == "0.0.0.0"
    assert loaded.passage_port == 20000
    assert loaded.camera_index == 2
    assert loaded.high_speed_dir == settings.high_speed_dir


def test_review_settings_round_trip_racetiger_configuration(tmp_path):
    config_path = tmp_path / "finish_review_config.json"
    settings = FinishReviewSettings(
        source="rtsp://camera/live",
        output_dir=tmp_path / "race",
        passage_host="127.0.0.1",
        passage_port=18765,
        camera_index=1,
        timing_provider="racetiger",
        racetiger_base_url="https://rqs.racetigertiming.com",
        racetiger_pc="finish-pc",
        racetiger_rid="RID-2026",
        racetiger_token="local-test-token",
        racetiger_poll_interval_seconds=3.5,
    )

    save_review_settings(
        config_path,
        settings,
        secret_protector=_protect_for_test,
    )
    loaded = load_review_settings(
        config_path,
        output_dir=None,
        passage_host="0.0.0.0",
        passage_port=20000,
        camera_index=2,
        secret_unprotector=_unprotect_for_test,
    )

    payload = json.loads(config_path.read_text(encoding="utf-8"))

    assert loaded.timing_provider == "racetiger"
    assert loaded.racetiger_base_url == settings.racetiger_base_url
    assert loaded.racetiger_pc == settings.racetiger_pc
    assert loaded.racetiger_rid == settings.racetiger_rid
    assert loaded.racetiger_token == settings.racetiger_token
    assert loaded.racetiger_poll_interval_seconds == 3.5
    assert "racetiger_token" not in payload
    assert payload["racetiger_token_protected"].startswith("test-protected:")
    assert "local-test-token" not in config_path.read_text(encoding="utf-8")


def test_legacy_plaintext_token_is_migrated_on_next_save(tmp_path):
    config_path = tmp_path / "finish_review_config.json"
    config_path.write_text(
        json.dumps(
            {
                "schema_version": 5,
                "racetiger_token": "legacy-token",
                "timing_provider": "racetiger",
            }
        ),
        encoding="utf-8",
    )

    loaded = load_review_settings(
        config_path,
        output_dir=tmp_path / "race",
        passage_host="127.0.0.1",
        passage_port=18765,
        camera_index=1,
        secret_unprotector=_unprotect_for_test,
    )
    assert loaded.racetiger_token == "legacy-token"

    save_review_settings(
        config_path,
        loaded,
        secret_protector=_protect_for_test,
    )
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 6
    assert "racetiger_token" not in payload
    assert payload["racetiger_token_protected"] == _protect_for_test(
        "legacy-token"
    )


def test_unreadable_protected_token_survives_unrelated_settings_save(tmp_path):
    config_path = tmp_path / "finish_review_config.json"
    config_path.write_text(
        json.dumps(
            {
                "schema_version": 6,
                "source": "rtsp://camera/live",
                "racetiger_token_protected": "unreadable-ciphertext",
            }
        ),
        encoding="utf-8",
    )
    loaded = load_review_settings(
        config_path,
        output_dir=tmp_path / "race",
        passage_host="127.0.0.1",
        passage_port=18765,
        camera_index=1,
        secret_unprotector=_unprotect_for_test,
    )
    assert loaded.racetiger_token == ""

    save_review_settings(
        config_path,
        replace(loaded, camera_index=2),
        secret_protector=_protect_for_test,
        secret_unprotector=_unprotect_for_test,
    )

    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload["camera_index"] == 2
    assert payload["racetiger_token_protected"] == "unreadable-ciphertext"


def test_readable_protected_token_can_be_explicitly_cleared(tmp_path):
    config_path = tmp_path / "finish_review_config.json"
    config_path.write_text(
        json.dumps(
            {
                "schema_version": 6,
                "racetiger_token_protected": _protect_for_test("old-token"),
            }
        ),
        encoding="utf-8",
    )
    loaded = load_review_settings(
        config_path,
        output_dir=tmp_path / "race",
        passage_host="127.0.0.1",
        passage_port=18765,
        camera_index=1,
        secret_unprotector=_unprotect_for_test,
    )

    save_review_settings(
        config_path,
        replace(loaded, racetiger_token=""),
        secret_protector=_protect_for_test,
        secret_unprotector=_unprotect_for_test,
    )

    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload["racetiger_token_protected"] == ""


def test_installer_reports_secret_protection_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(
        review_main,
        "default_config_path",
        lambda: tmp_path / "config.json",
    )
    monkeypatch.setattr(review_main, "discover_auyat_root", lambda: None)
    monkeypatch.setattr(review_main, "install_exception_logging", lambda: None)
    monkeypatch.setattr(
        review_main,
        "configure_runtime_logging",
        lambda: tmp_path / "finish_review.log",
    )

    def fail_save(*_args, **_kwargs):
        raise SecretProtectionError("DPAPI unavailable")

    monkeypatch.setattr(review_main, "save_review_settings", fail_save)

    with pytest.raises(SystemExit) as error:
        review_main.main(
            [
                "--source",
                "rtsp://camera/live",
                "--install-source",
            ]
        )

    assert error.value.code == 2


@pytest.mark.skipif(os.name != "nt", reason="DPAPI is available on Windows only")
def test_windows_dpapi_secret_round_trip():
    secret = "FinishReview local secret"

    protected = protect_secret(secret)

    assert protected.startswith("dpapi:v1:")
    assert secret not in protected
    assert unprotect_secret(protected) == secret


def test_runtime_logging_writes_finish_review_log(tmp_path):
    application_logger = logging.getLogger("FinishReview")
    previous_level = application_logger.level
    previous_propagate = application_logger.propagate
    log_path = tmp_path / "logs" / "finish_review.log"
    try:
        assert review_main.configure_runtime_logging(log_path) == log_path.resolve()
        logging.getLogger("FinishReview.Test").error("persistent log test")
        for handler in application_logger.handlers:
            handler.flush()
        assert "persistent log test" in log_path.read_text(encoding="utf-8")
    finally:
        for handler in tuple(application_logger.handlers):
            if getattr(handler, review_main._MANAGED_LOG_HANDLER, False):
                application_logger.removeHandler(handler)
                handler.close()
        application_logger.setLevel(previous_level)
        application_logger.propagate = previous_propagate


def test_exception_hooks_record_main_and_worker_failures(monkeypatch):
    recorded = []

    class _CapturingLogger:
        def critical(self, message, *args, **kwargs):
            recorded.append((message, args, kwargs.get("exc_info")))

    monkeypatch.setattr(review_main, "logger", _CapturingLogger())
    monkeypatch.setattr(sys, "excepthook", lambda *_args: None)
    monkeypatch.setattr(threading, "excepthook", lambda _args: None)
    review_main.install_exception_logging()

    main_error = RuntimeError("main failure")
    sys.excepthook(RuntimeError, main_error, main_error.__traceback__)
    worker_error = ValueError("worker failure")
    threading.excepthook(
        SimpleNamespace(
            exc_type=ValueError,
            exc_value=worker_error,
            exc_traceback=worker_error.__traceback__,
            thread=SimpleNamespace(name="test-worker"),
        )
    )

    assert [entry[0] for entry in recorded] == [
        "Unhandled application exception",
        "Unhandled exception in thread %s",
    ]
    assert recorded[1][1] == ("test-worker",)


def test_review_settings_keep_legacy_rtsp_configuration(tmp_path):
    config_path = tmp_path / "finish_review_config.json"
    config_path.write_text(
        json.dumps({"schema_version": 1, "source": "rtsp://camera/live"}),
        encoding="utf-8",
    )

    loaded = load_review_settings(
        config_path,
        output_dir=tmp_path / "race",
        passage_host="127.0.0.1",
        passage_port=18765,
        camera_index=1,
    )

    assert loaded.source == "rtsp://camera/live"
