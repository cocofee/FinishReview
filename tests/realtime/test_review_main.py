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
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import QApplication

from realtime import review_main
from realtime.review_main import (
    build_argument_parser,
    load_review_settings,
    requested_source,
    save_review_settings,
)
from realtime.review_recorder import make_directshow_source, parse_directshow_source
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
    monkeypatch.setattr(review_main, "install_qthread_shutdown", lambda _app: None)
    monkeypatch.setattr(
        review_main,
        "configure_application_icon",
        lambda _app: events.append("icon"),
    )
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
        "icon",
        "show",
        "process-events",
        "start-receiver",
        "warning",
        "event-loop",
    ]


def test_application_icon_uses_finishreview_asset():
    app = QApplication.instance() or QApplication([])
    app.setWindowIcon(QIcon())

    review_main.configure_application_icon(app)

    assert not app.windowIcon().isNull()


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
    assert payload["schema_version"] == 8
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


def test_load_review_settings_ignores_non_object_json_root(tmp_path, caplog):
    config_path = tmp_path / "finish_review_config.json"
    config_path.write_text("[]", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="FinishReview.Entry"):
        loaded = load_review_settings(
            config_path,
            output_dir=tmp_path / "race",
            passage_host="127.0.0.1",
            passage_port=18765,
            camera_index=2,
            high_speed_dir=tmp_path / "high-speed",
        )

    assert loaded.output_dir == (tmp_path / "race").resolve()
    assert loaded.camera_index == 2
    assert loaded.timing_provider == "cyclerace"
    assert "configuration root must be an object" in caplog.text


def test_load_review_settings_isolates_invalid_field_from_later_values(
    tmp_path,
    caplog,
):
    config_path = tmp_path / "finish_review_config.json"
    config_path.write_text(
        json.dumps(
            {
                "camera_index": "not-an-integer",
                "high_speed_dir": str(tmp_path / "saved-high-speed"),
                "timing_provider": "racetiger",
                "racetiger_base_url": "https://rqs.racetigertiming.com",
                "racetiger_pc": "finish-pc",
                "racetiger_rid": "RID-2026",
                "racetiger_poll_interval_seconds": 3.5,
            }
        ),
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING, logger="FinishReview.Entry"):
        loaded = load_review_settings(
            config_path,
            output_dir=tmp_path / "race",
            passage_host="127.0.0.1",
            passage_port=18765,
            camera_index=None,
        )

    assert loaded.camera_index == 1
    assert loaded.high_speed_dir == (tmp_path / "saved-high-speed").absolute()
    assert loaded.timing_provider == "racetiger"
    assert loaded.racetiger_base_url == "https://rqs.racetigertiming.com"
    assert loaded.racetiger_pc == "finish-pc"
    assert loaded.racetiger_rid == "RID-2026"
    assert loaded.racetiger_poll_interval_seconds == 3.5
    assert "camera_index" in caplog.text


def test_load_review_settings_rejects_nonfinite_poll_interval(tmp_path, caplog):
    config_path = tmp_path / "finish_review_config.json"
    config_path.write_text(
        '{"timing_provider":"racetiger",'
        '"racetiger_poll_interval_seconds":1e309}',
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING, logger="FinishReview.Entry"):
        loaded = load_review_settings(
            config_path,
            output_dir=tmp_path / "race",
            passage_host="127.0.0.1",
            passage_port=18765,
            camera_index=1,
            high_speed_dir=tmp_path / "high-speed",
        )

    assert loaded.timing_provider == "racetiger"
    assert loaded.racetiger_poll_interval_seconds == 2.0
    assert "racetiger_poll_interval_seconds" in caplog.text


def test_review_settings_round_trip_two_encrypted_rtsp_sources(tmp_path):
    config_path = tmp_path / "finish_review_config.json"
    settings = FinishReviewSettings(
        source="rtsp://review-one:secret-one@192.168.50.101/live",
        secondary_source="rtsp://review-two:secret-two@192.168.50.102/live",
        output_dir=tmp_path / "race",
        passage_host="127.0.0.1",
        passage_port=18765,
        camera_index=1,
    )

    save_review_settings(
        config_path,
        settings,
        secret_protector=_protect_for_test,
    )
    loaded = load_review_settings(
        config_path,
        output_dir=None,
        passage_host="127.0.0.1",
        passage_port=18765,
        camera_index=1,
        secret_unprotector=_unprotect_for_test,
    )

    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert loaded.source == settings.source
    assert loaded.secondary_source == settings.secondary_source
    assert payload["source"] == "rtsp://192.168.50.101/live"
    assert payload["secondary_source"] == "rtsp://192.168.50.102/live"
    assert payload["secondary_rtsp_username"] == "review-two"
    assert payload["secondary_rtsp_password_protected"] == _protect_for_test(
        "secret-two"
    )
    assert "secret-one" not in config_path.read_text(encoding="utf-8")
    assert "secret-two" not in config_path.read_text(encoding="utf-8")


def test_review_settings_round_trip_secondary_usb_camera(tmp_path):
    config_path = tmp_path / "finish_review_config.json"
    settings = FinishReviewSettings(
        source=make_directshow_source("@device_pnp_dji_one"),
        secondary_source=make_directshow_source("@device_pnp_dji_two"),
        output_dir=tmp_path / "race",
        passage_host="127.0.0.1",
        passage_port=18765,
        camera_index=1,
    )

    save_review_settings(config_path, settings)
    loaded = load_review_settings(
        config_path,
        output_dir=None,
        passage_host="127.0.0.1",
        passage_port=18765,
        camera_index=1,
    )

    assert loaded.source == settings.source
    assert loaded.secondary_source == settings.secondary_source


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
    assert payload["schema_version"] == 8
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


def test_legacy_plaintext_rtsp_password_is_migrated_on_next_save(tmp_path):
    config_path = tmp_path / "finish_review_config.json"
    config_path.write_text(
        json.dumps(
            {
                "schema_version": 6,
                "source": "rtsp://192.0.2.10:8554/live",
                "rtsp_username": "review user",
                "rtsp_password": "legacy-password",
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
    assert loaded.source == (
        "rtsp://review%20user:legacy-password@192.0.2.10:8554/live"
    )

    save_review_settings(
        config_path,
        loaded,
        secret_protector=_protect_for_test,
    )
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 8
    assert payload["source"] == "rtsp://192.0.2.10:8554/live"
    assert payload["rtsp_username"] == "review user"
    assert "rtsp_password" not in payload
    assert payload["rtsp_password_protected"] == _protect_for_test(
        "legacy-password"
    )
    assert "legacy-password" not in config_path.read_text(encoding="utf-8")


def test_saving_settings_preserves_unknown_configuration_fields(tmp_path):
    config_path = tmp_path / "finish_review_config.json"
    config_path.write_text(
        json.dumps(
            {
                "schema_version": 99,
                "future_device": {"enabled": True, "profile": "main"},
            }
        ),
        encoding="utf-8",
    )
    settings = FinishReviewSettings(
        source="rtsp://camera/live",
        output_dir=tmp_path / "race",
        passage_host="127.0.0.1",
        passage_port=18765,
        camera_index=1,
    )

    save_review_settings(
        config_path,
        settings,
        secret_protector=_protect_for_test,
    )

    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 8
    assert payload["future_device"] == {"enabled": True, "profile": "main"}


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
