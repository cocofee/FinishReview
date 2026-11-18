import logging

import pytest

from realtime import decode_resources
from realtime.failure_log import reset_handled_failure_log, warn_handled_failure
from realtime.recording_controller import RecordingSessionController
from realtime.stream_recorder import ManualRecordingManager, RecordingError
from realtime.video_arrival import VideoArrivalCandidateStore
from realtime.video_timeline import probe_video_duration_ms


@pytest.fixture(autouse=True)
def fresh_failure_log(caplog):
    reset_handled_failure_log()
    caplog.set_level(logging.WARNING, logger="FinishReview")
    yield
    reset_handled_failure_log()


def _warnings(caplog):
    return [record for record in caplog.records if record.levelno == logging.WARNING]


def test_same_failure_is_logged_once_per_interval(caplog):
    logger = logging.getLogger("FinishReview.Test")
    now = [100.0]

    def clock():
        return now[0]

    assert warn_handled_failure(logger, "a", "first %s", 1, clock=clock)
    assert not warn_handled_failure(logger, "a", "again %s", 2, clock=clock)
    assert warn_handled_failure(logger, "b", "other key", clock=clock)
    now[0] += 60.0
    assert warn_handled_failure(logger, "a", "after interval", clock=clock)

    assert [record.getMessage() for record in _warnings(caplog)] == [
        "first 1",
        "other key",
        "after interval",
    ]


def test_unknown_recorder_state_keeps_default_and_logs_without_password(caplog):
    class _Recorder:
        camera_index = 2

        @property
        def is_running(self):
            raise RuntimeError("rtsp://admin:not-a-real-pw@10.0.0.2/live dropped")

    assert RecordingSessionController._recorder_is_running(_Recorder(), uncertain=True) is True
    assert RecordingSessionController._recorder_is_running(_Recorder(), uncertain=False) is False

    messages = [record.getMessage() for record in _warnings(caplog)]
    assert len(messages) == 1
    assert "Camera 2" in messages[0]
    assert "not-a-real-pw" not in messages[0]
    assert "admin:***@" in messages[0]


def test_unreadable_video_duration_returns_none_and_logs(tmp_path, monkeypatch, caplog):
    video = tmp_path / "camera_01.mkv"
    video.write_bytes(b"not really a video")

    def broken_open(*_args, **_kwargs):
        raise RuntimeError("decoder crashed")

    monkeypatch.setattr(decode_resources.DECODE_RESOURCES, "open_capture", broken_open)

    assert probe_video_duration_ms(video) is None
    assert probe_video_duration_ms(video) is None

    messages = [record.getMessage() for record in _warnings(caplog)]
    assert len(messages) == 1
    assert "camera_01.mkv" in messages[0]
    assert "decoder crashed" in messages[0]


def test_failed_journal_tail_repair_is_logged(tmp_path, caplog):
    store = VideoArrivalCandidateStore(tmp_path / "arrivals.jsonl")
    store.path = tmp_path / "missing" / "arrivals.jsonl"

    store._truncate(3)

    messages = [record.getMessage() for record in _warnings(caplog)]
    assert len(messages) == 1
    assert "arrivals.jsonl" in messages[0]


def test_rollback_stop_failure_is_logged_and_start_error_still_raised(tmp_path, caplog):
    class _Recorder:
        def __init__(self, source, output_dir, *, camera_index, ffmpeg_path):
            self.camera_index = camera_index
            self.output_path = output_dir / f"camera_{camera_index:02d}.mkv"
            self.is_running = False

        def start(self):
            if self.camera_index == 2:
                raise OSError("rtsp://admin:not-a-real-pw@camera-two/live refused")
            self.is_running = True
            return self.output_path

        def stop(self):
            raise OSError("rtsp://admin:not-a-real-pw@camera-one/live still busy")

    manager = ManualRecordingManager(
        ["rtsp://camera-one/live", "rtsp://camera-two/live"],
        tmp_path / "videos",
        recorder_factory=_Recorder,
    )

    with pytest.raises(RecordingError):
        manager.start()

    messages = [record.getMessage() for record in _warnings(caplog)]
    assert len(messages) == 1
    assert "Camera 1" in messages[0]
    assert "not-a-real-pw" not in messages[0]
    assert "admin:***@" in messages[0]


def test_device_discovery_failure_shows_no_devices_and_logs(tmp_path, caplog):
    from PyQt5.QtWidgets import QApplication

    from realtime.launch_dialog import FinishReviewLaunchDialog
    from realtime.settings import FinishReviewSettings

    _app = QApplication.instance() or QApplication([])

    def broken_provider():
        raise RuntimeError("DirectShow enumeration failed")

    dialog = FinishReviewLaunchDialog(
        FinishReviewSettings(
            source="",
            output_dir=tmp_path,
            passage_host="0.0.0.0",
            passage_port=18765,
            camera_index=1,
        ),
        device_provider=broken_provider,
    )

    assert dialog.camera_status_label.text() == "未检测到USB/Type-C摄像头"
    messages = [record.getMessage() for record in _warnings(caplog)]
    assert any("DirectShow enumeration failed" in message for message in messages)
    dialog.close()
