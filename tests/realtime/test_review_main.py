import json
from pathlib import Path

import pytest

from realtime.review_main import (
    build_argument_parser,
    load_review_settings,
    requested_source,
    save_review_settings,
)
from realtime.review_recorder import parse_directshow_source
from realtime.review_window import FinishReviewSettings


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

    save_review_settings(config_path, settings)
    loaded = load_review_settings(
        config_path,
        output_dir=tmp_path / "other-race",
        passage_host="0.0.0.0",
        passage_port=20000,
        camera_index=2,
    )

    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 5
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

    save_review_settings(config_path, settings)
    loaded = load_review_settings(
        config_path,
        output_dir=None,
        passage_host="0.0.0.0",
        passage_port=20000,
        camera_index=2,
    )

    assert loaded.timing_provider == "racetiger"
    assert loaded.racetiger_base_url == settings.racetiger_base_url
    assert loaded.racetiger_pc == settings.racetiger_pc
    assert loaded.racetiger_rid == settings.racetiger_rid
    assert loaded.racetiger_token == settings.racetiger_token
    assert loaded.racetiger_poll_interval_seconds == 3.5


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
