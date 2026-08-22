import os
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import cv2
import numpy as np
import pytest
from PyQt5.QtCore import QEventLoop, QObject, Qt, QTimer, pyqtSignal
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication

from realtime.video_playback import (
    SpringShuttleSlider,
    VideoPlaybackDialog,
    VideoPlaybackWorker,
    find_recordings,
    format_playback_time,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_find_recordings_returns_supported_nonempty_files_newest_first(tmp_path):
    videos_dir = tmp_path / "videos"
    videos_dir.mkdir()
    older = videos_dir / "camera_01_older.mkv"
    newer = videos_dir / "camera_01_newer.mp4"
    ignored = videos_dir / "notes.txt"
    empty = videos_dir / "empty.avi"
    older.write_bytes(b"old")
    newer.write_bytes(b"new")
    ignored.write_text("ignore", encoding="utf-8")
    empty.touch()
    os.utime(older, (10, 10))
    os.utime(newer, (20, 20))

    assert find_recordings(tmp_path) == [newer, older]
    assert format_playback_time(3_661_999) == "01:01:01.999"
    assert SpringShuttleSlider.speed_for_value(-5) == -4.0
    assert SpringShuttleSlider.speed_for_value(-1) == -0.25
    assert SpringShuttleSlider.speed_for_value(2) == 0.5
    assert SpringShuttleSlider.value_for_speed(4.0) == 5


def test_shuttle_speed_selection_persists_until_stop(qapp):
    shuttle = SpringShuttleSlider()
    emitted = []
    shuttle.speed_changed.connect(emitted.append)

    reverse_button = shuttle._buttons[-4]
    slow_forward_button = shuttle._buttons[1]
    stop_button = shuttle._buttons[0]
    reverse_button.click()
    assert shuttle.value() == -4
    assert emitted == [-2.0]
    assert reverse_button.isChecked()

    slow_forward_button.click()
    assert shuttle.value() == 1
    assert emitted == [-2.0, 0.25]
    assert slow_forward_button.isChecked()

    stop_button.click()
    assert shuttle.value() == 0
    assert emitted == [-2.0, 0.25, 0.0]
    assert stop_button.isChecked()
    assert not reverse_button.isChecked()
    shuttle.close()


class _FakeCapture:
    def __init__(self):
        self.frames = [
            np.full((24, 32, 3), value, dtype=np.uint8)
            for value in (0, 64, 128, 192)
        ]
        self.position = 0
        self.released = False

    def isOpened(self):
        return True

    def get(self, property_id):
        if property_id == cv2.CAP_PROP_FPS:
            return 20.0
        if property_id == cv2.CAP_PROP_FRAME_COUNT:
            return len(self.frames)
        if property_id == cv2.CAP_PROP_FRAME_WIDTH:
            return 32
        if property_id == cv2.CAP_PROP_FRAME_HEIGHT:
            return 24
        if property_id == cv2.CAP_PROP_POS_FRAMES:
            return self.position
        return 0

    def set(self, property_id, value):
        if property_id == cv2.CAP_PROP_POS_FRAMES:
            self.position = max(0, min(int(value), len(self.frames)))
            return True
        return False

    def read(self):
        if self.position >= len(self.frames):
            return False, None
        frame = self.frames[self.position]
        self.position += 1
        return True, frame.copy()

    def grab(self):
        if self.position >= len(self.frames):
            return False
        self.position += 1
        return True

    def release(self):
        self.released = True


class _FakeDialogWorker(QObject):
    metadata_ready = pyqtSignal(int, float, int, int, int)
    frame_ready = pyqtSignal(object, int, int)
    playback_finished = pyqtSignal()
    playback_error = pyqtSignal(str)

    def __init__(self, _video_path, parent=None):
        super().__init__(parent)
        self.speed_calls = []
        self.pause_calls = 0
        self.seek_calls = []

    def start(self):
        self.metadata_ready.emit(10_000, 50.0, 640, 360, 500)

    def set_shuttle_speed(self, speed):
        self.speed_calls.append(speed)

    def pause(self):
        self.pause_calls += 1

    def seek_frame(self, _frame_index):
        pass

    def seek(self, _milliseconds):
        self.seek_calls.append(_milliseconds)

    def jump(self, _delta_ms):
        pass

    def step(self, _frame_delta):
        pass

    def stop(self):
        pass

    def wait(self, _milliseconds):
        return True


def test_space_pauses_and_resumes_selected_slow_speed(qapp):
    dialog = VideoPlaybackDialog(Path("recording.mkv"), worker_factory=_FakeDialogWorker)
    dialog.show()
    qapp.processEvents()

    slow_forward_button = dialog.shuttle_slider._buttons[1]
    slow_forward_button.click()
    assert dialog.worker.speed_calls[-1] == 0.25

    QTest.keyClick(slow_forward_button, Qt.Key_Space)
    qapp.processEvents()
    assert dialog._playing is False
    assert dialog.worker.pause_calls == 1

    QTest.keyClick(slow_forward_button, Qt.Key_Space)
    qapp.processEvents()
    assert dialog._playing is True
    assert dialog.worker.speed_calls[-1] == 0.25
    dialog.close()


def test_dialog_starts_paused_at_requested_passage_position(qapp):
    dialog = VideoPlaybackDialog(
        Path("recording.mkv"),
        worker_factory=_FakeDialogWorker,
        initial_position_ms=4_250,
        autoplay=False,
    )
    qapp.processEvents()

    assert dialog._playing is False
    assert dialog.worker.pause_calls == 1
    assert dialog.worker.seek_calls == [4_250]
    assert dialog.timeline.value() == 4_250
    assert dialog.current_time_label.text() == "00:00:04.250"
    dialog.close()


def test_dialog_marks_passage_target_and_updates_relative_time(qapp):
    dialog = VideoPlaybackDialog(
        Path("recording.mkv"),
        worker_factory=_FakeDialogWorker,
        initial_position_ms=4_250,
        target_position_ms=7_250,
        context_text="号码 23 | 机位 1",
        autoplay=False,
    )
    qapp.processEvents()

    assert dialog.timeline.target_position_ms == 7_250
    assert "Passage 目标 00:00:07.250" in dialog.target_status_label.text()
    assert "目标前 3.000 秒" in dialog.target_status_label.text()
    dialog._on_slider_moved(8_250)
    assert "已过目标 1.000 秒" in dialog.target_status_label.text()
    dialog.close()


def test_dialog_reports_target_beyond_real_media_duration(qapp):
    dialog = VideoPlaybackDialog(
        Path("recording.mkv"),
        worker_factory=_FakeDialogWorker,
        initial_position_ms=9_000,
        target_position_ms=12_000,
        autoplay=False,
    )
    qapp.processEvents()

    assert dialog.timeline.target_position_ms == 12_000
    assert "Passage 目标 00:00:12.000" in dialog.target_status_label.text()
    assert "超出录像时长 2.000 秒" in dialog.target_status_label.text()
    dialog.close()


def test_playback_worker_decodes_frames_and_stops_cleanly(qapp):
    capture = _FakeCapture()
    worker = VideoPlaybackWorker(
        Path("recording.mkv"),
        capture_factory=lambda _path: capture,
    )
    metadata = []
    positions = []
    errors = []
    loop = QEventLoop()

    worker.metadata_ready.connect(lambda *values: metadata.append(values))
    worker.frame_ready.connect(
        lambda _image, position, _frame_index: positions.append(position)
    )
    worker.playback_error.connect(errors.append)

    def finish():
        worker.stop()
        loop.quit()

    worker.playback_finished.connect(finish)
    QTimer.singleShot(2_000, finish)
    worker.start()
    loop.exec_()

    assert worker.wait(2_000)
    assert errors == []
    assert metadata == [(200, 20.0, 32, 24, 4)]
    assert positions
    assert positions[0] == 0
    assert positions[-1] <= 150
    assert capture.released is True
