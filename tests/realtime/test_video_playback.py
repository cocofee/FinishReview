import os
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import cv2
import numpy as np
import pytest

import realtime.video_playback as video_playback
from PyQt5.QtCore import QEventLoop, QObject, QPoint, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QImage
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication

from realtime.video_playback import (
    PlaybackVideoLabel,
    SpringShuttleSlider,
    TargetTimelineSlider,
    VideoPlaybackDialog,
    VideoPlaybackWorker,
    _open_video_capture,
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


def test_ffconcat_capture_opens_manifest_without_mutating_ffmpeg_options(tmp_path):
    manifest = tmp_path / "point.ffconcat"
    manifest.write_text("ffconcat version 1.0\n", encoding="utf-8")
    observed = []
    marker = object()

    result = _open_video_capture(
        manifest,
        lambda path: observed.append(path) or marker,
    )

    assert result is marker
    assert observed == [str(manifest)]


def test_speed_selector_shows_only_common_forward_speeds(qapp):
    shuttle = SpringShuttleSlider()
    emitted = []
    shuttle.speed_changed.connect(emitted.append)

    slow_forward_button = shuttle._buttons[1]
    fast_forward_button = shuttle._buttons[4]
    assert set(shuttle._buttons) == {1, 2, 3, 4}

    slow_forward_button.click()
    assert shuttle.value() == 1
    assert emitted == [0.25]
    assert slow_forward_button.isChecked()

    shuttle.set_display_speed(0.0)
    assert slow_forward_button.isChecked()

    fast_forward_button.click()
    assert shuttle.value() == 4
    assert emitted == [0.25, 2.0]
    assert fast_forward_button.isChecked()
    shuttle.close()


def test_video_drag_maps_screen_distance_to_the_recording_span(qapp):
    label = PlaybackVideoLabel()
    label.resize(1_000, 600)
    label.set_jog_frame_span(1_800)
    label._drag_origin_x = 100

    assert label._frame_delta_for_position(600) == 900
    assert label._frame_delta_for_position(-400) == -900
    label.close()


def test_timeline_position_maps_clicks_across_the_full_duration(qapp):
    timeline = TargetTimelineSlider(Qt.Horizontal)
    timeline.resize(1_000, 26)
    timeline.setRange(0, 60_000)

    midpoint = timeline._value_from_position(timeline.width() // 2)

    assert 29_000 <= midpoint <= 31_000
    timeline.close()


class _FakeCapture:
    def __init__(self, frame_count=4):
        self.frames = [
            np.full((24, 32, 3), index % 256, dtype=np.uint8)
            for index in range(frame_count)
        ]
        self.position = 0
        self.released = False
        self.set_positions = []
        self.read_positions = []

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
            self.set_positions.append(self.position)
            return True
        return False

    def read(self):
        if self.position >= len(self.frames):
            return False, None
        self.read_positions.append(self.position)
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


class _BlockingCapture(_FakeCapture):
    def __init__(self, frame_count=30):
        super().__init__(frame_count=frame_count)
        self.read_started = threading.Event()
        self.allow_read = threading.Event()

    def read(self):
        self.read_started.set()
        if not self.allow_read.wait(2.0):
            return False, None
        return super().read()


class _BlockingFrameCapture(_FakeCapture):
    def __init__(self, frame_count=30, *, block_position: int):
        super().__init__(frame_count=frame_count)
        self.block_position = int(block_position)
        self.read_started = threading.Event()
        self.allow_read = threading.Event()

    def read(self):
        if self.position == self.block_position:
            self.read_started.set()
            if not self.allow_read.wait(2.0):
                return False, None
        return super().read()


class _ImpreciseSeekCapture(_FakeCapture):
    def set(self, property_id, value):
        if property_id == cv2.CAP_PROP_POS_FRAMES:
            return super().set(property_id, max(0, int(value) - 1))
        return super().set(property_id, value)


class _MisreportedHlsCapture(_FakeCapture):
    def __init__(self):
        super().__init__(frame_count=200)

    def get(self, property_id):
        if property_id == cv2.CAP_PROP_FPS:
            return 50.0
        if property_id == cv2.CAP_PROP_FRAME_COUNT:
            return 399
        if property_id == cv2.CAP_PROP_POS_MSEC:
            return max(0.0, (self.position - 1) * 40.0)
        return super().get(property_id)


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
        self.idle_prefetch_calls = []

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

    def set_idle_prefetch_enabled(self, enabled):
        self.idle_prefetch_calls.append(bool(enabled))

    def wait(self, _milliseconds):
        return True


class _SlowStoppingDialogWorker(_FakeDialogWorker):
    def __init__(self, video_path, parent=None):
        super().__init__(video_path, parent)
        self.stop_requested = False
        self.wait_calls = 0

    def request_stop(self):
        self.stop_requested = True

    def wait(self, _milliseconds):
        self.wait_calls += 1
        return self.wait_calls > 1


def test_dialog_retires_slow_worker_without_force_termination(
    qapp,
    monkeypatch,
):
    retired = []
    monkeypatch.setattr(video_playback, "retire_qthread", retired.append)
    dialog = VideoPlaybackDialog(
        Path("recording.mkv"),
        worker_factory=_SlowStoppingDialogWorker,
    )

    dialog.close()

    assert dialog.worker.stop_requested
    assert retired == [dialog.worker]


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
    assert dialog.worker.idle_prefetch_calls == [False]
    assert dialog.worker.seek_calls == [4_250]
    assert dialog.timeline.value() == 4_250
    assert dialog.current_time_label.text() == "00:00:04.250"
    assert dialog.video_label._jog_frame_span == 500
    dialog.close()


def test_dialog_timeline_click_jumps_and_previews_immediately(qapp):
    dialog = VideoPlaybackDialog(
        Path("recording.mkv"),
        worker_factory=_FakeDialogWorker,
        autoplay=False,
    )
    dialog.resize(1_000, 700)
    dialog.show()
    qapp.processEvents()
    dialog.worker.seek_calls.clear()

    QTest.mouseClick(
        dialog.timeline,
        Qt.LeftButton,
        pos=QPoint(dialog.timeline.width() * 3 // 4, dialog.timeline.height() // 2),
    )
    qapp.processEvents()

    assert 7_000 <= dialog.timeline.value() <= 8_000
    assert dialog.worker.seek_calls[-1] == dialog.timeline.value()
    dialog.close()


def test_dialog_coalesces_pending_frames_to_latest_for_rendering(qapp):
    dialog = VideoPlaybackDialog(
        Path("recording.mkv"),
        worker_factory=_FakeDialogWorker,
        autoplay=False,
    )
    dialog.show()
    qapp.processEvents()

    for frame_index in (1, 2, 3):
        image = QImage(32, 24, QImage.Format_RGB888)
        image.fill(frame_index)
        dialog.worker.frame_ready.emit(image, frame_index * 40, frame_index)

    assert dialog._current_frame_index == 0
    qapp.processEvents()

    assert dialog._current_frame_index == 3
    assert dialog.timeline.value() == 120
    dialog.close()


def test_dialog_uses_fast_scaling_while_playing(qapp):
    dialog = VideoPlaybackDialog(
        Path("recording.mkv"),
        worker_factory=_FakeDialogWorker,
        autoplay=False,
    )
    assert dialog.video_label._smooth_scaling is True

    dialog._set_playing(True)
    assert dialog.video_label._smooth_scaling is False
    dialog._set_playing(False)
    assert dialog.video_label._smooth_scaling is True
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


def test_dialog_uses_compact_controls_and_returns_to_target(qapp):
    dialog = VideoPlaybackDialog(
        Path("recording.mkv"),
        worker_factory=_FakeDialogWorker,
        initial_position_ms=4_250,
        target_position_ms=7_250,
        autoplay=True,
    )
    qapp.processEvents()

    assert dialog.target_btn.text() == "回到目标"
    assert not hasattr(dialog, "back_two_btn")
    assert not hasattr(dialog, "forward_two_btn")
    assert [button.text() for button in dialog.shuttle_slider._buttons.values()] == [
        "0.25x",
        "0.5x",
        "1x",
        "2x",
    ]

    dialog.target_btn.click()
    assert dialog.worker.seek_calls[-1] == 7_250
    assert dialog.timeline.value() == 7_250
    assert dialog._playing is False

    dialog.worker.seek_calls.clear()
    QTest.keyClick(dialog, Qt.Key_T)
    assert dialog.worker.seek_calls == [7_250]
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


def test_hls_worker_uses_sequential_timestamps_when_metadata_doubles_fps(
    qapp,
    tmp_path,
):
    playlist = tmp_path / "evidence.m3u8"
    playlist.write_text(
        "\n".join(
            (
                "#EXTM3U",
                "#EXTINF:1.996,",
                "segment-1.ts",
                "#EXTINF:1.997,",
                "segment-2.ts",
                "#EXTINF:1.997,",
                "segment-3.ts",
                "#EXTINF:1.996,",
                "segment-4.ts",
                "#EXT-X-ENDLIST",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    captures = []

    def capture_factory(_path):
        capture = _MisreportedHlsCapture()
        captures.append(capture)
        return capture

    worker = VideoPlaybackWorker(playlist, capture_factory=capture_factory)
    metadata = []
    frame_indexes = []
    loop = QEventLoop()

    def finish():
        worker.stop()
        loop.quit()

    def on_metadata(*values):
        metadata.append(values)
        worker.seek(4_798)

    def on_frame(_image, _position_ms, frame_index):
        frame_indexes.append(frame_index)
        if len(frame_indexes) == 1:
            worker.step(5)
        elif len(frame_indexes) == 2:
            worker.step(-5)
        elif len(frame_indexes) == 3:
            worker.step(-5)
        elif len(frame_indexes) == 4:
            finish()

    worker.pause()
    worker.metadata_ready.connect(on_metadata)
    worker.frame_ready.connect(on_frame)
    QTimer.singleShot(2_000, finish)
    worker.start()
    loop.exec_()

    assert worker.wait(2_000)
    assert metadata == [(7_986, 25.0, 32, 24, 200)]
    assert frame_indexes == [119, 124, 119, 114]
    assert captures[0].read_positions == list(range(108, 125))
    assert captures[0].set_positions == []
    assert all(capture.released for capture in captures)


def test_rapid_steps_keep_the_latest_absolute_target_when_decode_is_cancelled(qapp):
    capture = _BlockingFrameCapture(frame_count=40, block_position=15)
    worker = VideoPlaybackWorker(
        Path("recording.mkv"),
        capture_factory=lambda _path: capture,
        reverse_prefetch=False,
    )
    emitted = []
    loop = QEventLoop()

    def finish():
        worker.stop()
        loop.quit()

    def queue_second_step():
        if capture.read_started.wait(1.0):
            worker.step(5)
            capture.allow_read.set()

    def on_metadata(*_values):
        worker.seek_frame(10)

    def on_frame(_image, _position_ms, frame_index):
        emitted.append(frame_index)
        if frame_index == 10:
            worker.step(5)
        elif frame_index == 20:
            finish()

    helper = threading.Thread(target=queue_second_step)
    helper.start()
    worker.pause()
    worker.metadata_ready.connect(on_metadata)
    worker.frame_ready.connect(on_frame)
    QTimer.singleShot(2_000, finish)
    worker.start()
    loop.exec_()
    helper.join(2.0)

    assert worker.wait(2_000)
    assert not helper.is_alive()
    assert emitted == [10, 20]
    assert capture.read_positions == [10, 15, 20]


def test_playing_worker_continues_from_requested_seek_position(qapp):
    capture = _FakeCapture(frame_count=100)
    worker = VideoPlaybackWorker(
        Path("recording.mkv"),
        capture_factory=lambda _path: capture,
    )
    positions = []
    first_seeked_index = []
    loop = QEventLoop()

    def finish():
        worker.stop()
        loop.quit()

    def on_metadata(*_values):
        worker.seek(2_000)

    def on_frame(_image, position_ms, _frame_index):
        positions.append(position_ms)
        if position_ms >= 2_000 and not first_seeked_index:
            first_seeked_index.append(len(positions) - 1)
            QTimer.singleShot(250, finish)

    worker.metadata_ready.connect(on_metadata)
    worker.frame_ready.connect(on_frame)
    QTimer.singleShot(2_000, finish)
    worker.start()
    loop.exec_()

    assert worker.wait(2_000)
    assert first_seeked_index
    assert min(positions[first_seeked_index[0] :]) >= 2_000
    assert capture.released is True


def test_worker_drops_frame_from_obsolete_seek_generation(qapp):
    worker = VideoPlaybackWorker(
        Path("recording.mkv"),
        capture_factory=lambda _path: _FakeCapture(),
    )
    emitted = []
    image = QImage(32, 24, QImage.Format_RGB888)
    worker.frame_ready.connect(
        lambda _image, _position_ms, frame_index: emitted.append(frame_index)
    )

    worker.seek_frame(1)
    obsolete_generation = worker._request_generation
    worker.seek_frame(2)

    assert not worker._emit_image(image, 1, generation=obsolete_generation)
    assert worker.current_frame_index == -1
    assert worker._emit_image(
        image,
        2,
        generation=worker._request_generation,
    )
    qapp.processEvents()

    assert emitted == [2]
    assert worker.current_frame_index == 2


def test_seek_and_play_emits_requested_frame_before_continuing(qapp):
    capture = _FakeCapture(frame_count=100)
    worker = VideoPlaybackWorker(
        Path("recording.mkv"),
        capture_factory=lambda _path: capture,
    )
    worker._fps = 20.0
    worker._frame_count = 100
    emitted = []
    loop = QEventLoop()

    def finish():
        worker.stop()
        loop.quit()

    def on_frame(_image, _position_ms, frame_index):
        emitted.append(frame_index)
        if len(emitted) >= 3:
            finish()

    worker.frame_ready.connect(on_frame)
    worker.seek_and_play(1_000)
    QTimer.singleShot(2_000, finish)
    worker.start()
    loop.exec_()

    assert worker.wait(2_000)
    assert emitted[0] == 20
    assert emitted[1:] == [21, 22]
    assert capture.read_positions[:3] == [20, 21, 22]


def test_reverse_window_uses_one_seek_then_serves_cached_frames(qapp):
    capture = _FakeCapture(frame_count=30)
    worker = VideoPlaybackWorker(
        Path("recording.mkv"),
        capture_factory=lambda _path: capture,
    )
    worker._fps = 20.0
    worker._reverse_window_frames = 6
    emitted = []
    worker.frame_ready.connect(
        lambda _image, _position_ms, frame_index: emitted.append(frame_index)
    )

    ok, capture_next_frame = worker._decode_target(
        capture,
        10,
        20,
        generation=worker._request_generation,
        reverse_window=True,
    )
    assert ok
    assert capture.set_positions == [5]
    assert capture.read_positions == [5, 6, 7, 8, 9, 10]
    assert capture_next_frame == 11
    assert emitted == [10]

    ok, cached_next_frame = worker._decode_target(
        capture,
        9,
        capture_next_frame,
        generation=worker._request_generation,
        reverse_window=True,
    )
    qapp.processEvents()

    assert ok
    assert cached_next_frame == capture_next_frame
    assert capture.set_positions == [5]
    assert capture.read_positions == [5, 6, 7, 8, 9, 10]
    assert emitted == [10, 9]


def test_reverse_window_stops_before_seek_when_request_is_obsolete():
    capture = _FakeCapture(frame_count=30)
    worker = VideoPlaybackWorker(
        Path("recording.mkv"),
        capture_factory=lambda _path: capture,
    )
    worker.seek_frame(10)
    obsolete_generation = worker._request_generation
    worker.seek_frame(20)

    ok, capture_next_frame = worker._decode_target(
        capture,
        10,
        20,
        generation=obsolete_generation,
        reverse_window=True,
    )

    assert ok
    assert capture_next_frame == 20
    assert capture.set_positions == []
    assert capture.read_positions == []


def test_frame_cache_respects_configured_memory_limit():
    worker = VideoPlaybackWorker(Path("recording.mkv"))
    image = QImage(32, 24, QImage.Format_RGB888)
    image.fill(0)
    worker._max_cache_bytes = image.byteCount() * 2

    worker._cache_image(1, image)
    worker._cache_image(2, image)
    worker._cache_image(3, image)

    assert list(worker._frame_cache) == [2, 3]
    assert worker._frame_cache_bytes <= worker._max_cache_bytes


def test_paused_hls_worker_prefetches_the_forward_window():
    capture = _FakeCapture(frame_count=40)
    capture.position = 11
    worker = VideoPlaybackWorker(Path("evidence.m3u8"), reverse_prefetch=False)
    worker._playing = False
    worker._frame_count = 40
    worker._reverse_window_frames = 4

    capture_next_frame = worker._prefetch_sequential_forward(
        capture,
        11,
        10,
        generation=worker._request_generation,
    )

    assert capture_next_frame == 15
    assert capture.read_positions == [11, 12, 13, 14]
    assert list(worker._frame_cache) == [11, 12, 13, 14]


def test_disabled_hls_idle_prefetch_does_not_read_ahead():
    capture = _FakeCapture(frame_count=40)
    capture.position = 11
    worker = VideoPlaybackWorker(
        Path("evidence.m3u8"),
        reverse_prefetch=False,
        idle_prefetch=False,
    )
    worker._playing = False
    worker._frame_count = 40
    worker._reverse_window_frames = 4

    capture_next_frame = worker._prefetch_sequential_forward(
        capture,
        11,
        10,
        generation=worker._request_generation,
    )

    assert capture_next_frame == 11
    assert capture.read_positions == []
    assert list(worker._frame_cache) == []


def test_hls_idle_prefetch_yields_to_a_new_step_request():
    capture = _BlockingFrameCapture(frame_count=40, block_position=11)
    capture.position = 11
    worker = VideoPlaybackWorker(Path("evidence.m3u8"), reverse_prefetch=False)
    worker._playing = False
    worker._frame_count = 40
    worker._reverse_window_frames = 4
    worker._current_frame_index = 10
    worker._navigation_frame_index = 10
    generation = worker._request_generation
    result = []
    prefetch_thread = threading.Thread(
        target=lambda: result.append(
            worker._prefetch_sequential_forward(
                capture,
                11,
                10,
                generation=generation,
            )
        )
    )

    prefetch_thread.start()
    assert capture.read_started.wait(1.0)
    worker.step(1)
    capture.allow_read.set()
    prefetch_thread.join(2.0)

    assert not prefetch_thread.is_alive()
    assert result == [12]
    assert list(worker._frame_cache) == [11]

    requested_frame, request_generation = worker._step_frame
    worker._step_frame = None
    ok, capture_next_frame = worker._decode_target(
        capture,
        requested_frame,
        result[0],
        generation=request_generation,
    )

    assert ok
    assert capture_next_frame == 12
    assert capture.set_positions == []
    assert capture.read_positions == [11]


def test_parked_hls_cache_keeps_the_nearby_navigation_window():
    capture = _FakeCapture(frame_count=40)
    worker = VideoPlaybackWorker(Path("evidence.m3u8"), reverse_prefetch=False)
    worker._playing = False
    worker._frame_count = 40
    worker._reverse_window_frames = 4

    ok, capture_next_frame = worker._decode_target(
        capture,
        10,
        0,
        generation=worker._request_generation,
    )
    assert ok
    capture_next_frame = worker._prefetch_sequential_forward(
        capture,
        capture_next_frame,
        10,
        generation=worker._request_generation,
    )
    worker.park_cache()

    assert list(worker._frame_cache) == list(range(7, 15))
    generation = worker._request_generation
    ok, parked_next_frame = worker._decode_target(
        capture,
        11,
        capture_next_frame,
        generation=generation,
    )

    assert ok
    assert parked_next_frame == capture_next_frame
    assert capture.set_positions == []


def test_release_cache_blocks_an_inflight_forward_prefetch_write():
    capture = _BlockingFrameCapture(frame_count=40, block_position=11)
    capture.position = 11
    worker = VideoPlaybackWorker(Path("evidence.m3u8"), reverse_prefetch=False)
    worker._playing = False
    worker._frame_count = 40
    worker._reverse_window_frames = 4
    generation = worker._request_generation
    result = []
    prefetch_thread = threading.Thread(
        target=lambda: result.append(
            worker._prefetch_sequential_forward(
                capture,
                11,
                10,
                generation=generation,
            )
        )
    )

    prefetch_thread.start()
    assert capture.read_started.wait(1.0)
    worker.release_cache()
    capture.allow_read.set()
    prefetch_thread.join(2.0)

    assert not prefetch_thread.is_alive()
    assert result == [12]
    assert list(worker._frame_cache) == []


def test_reverse_double_buffer_fits_inside_default_cache_budget():
    worker = VideoPlaybackWorker(Path("recording.mkv"))
    worker._fps = 25.0

    worker._configure_reverse_window(1920, 1080)

    preview_frame_bytes = 1280 * 720 * 3
    assert worker._max_cache_bytes == 64 * 1024 * 1024
    assert worker._reverse_window_frames == 12
    assert (
        worker._reverse_window_frames * 2 * preview_frame_bytes
        <= worker._max_cache_bytes
    )


def test_release_cache_cancels_prefetch_and_drops_cached_images():
    worker = VideoPlaybackWorker(Path("recording.mkv"))
    image = QImage(32, 24, QImage.Format_RGB888)
    image.fill(0)
    worker._cache_image(1, image)
    worker._reverse_current_window = (0, 1, 0)
    worker._reverse_prefetch_task = (0, 0, 0, 1)

    worker.release_cache()

    assert list(worker._frame_cache) == []
    assert worker._frame_cache_bytes == 0
    assert worker._reverse_current_window is None
    assert worker._reverse_prefetch_task is None


def test_pause_and_release_cancel_in_progress_reverse_decode():
    capture = _BlockingCapture(frame_count=30)
    worker = VideoPlaybackWorker(
        Path("recording.mkv"),
        capture_factory=lambda _path: capture,
        reverse_prefetch=False,
    )
    worker._fps = 20.0
    worker._frame_count = 30
    worker._reverse_window_frames = 10
    emitted = []
    result = []
    worker.frame_ready.connect(
        lambda _image, _position_ms, frame_index: emitted.append(frame_index)
    )
    generation = worker._request_generation
    decode_thread = threading.Thread(
        target=lambda: result.append(
            worker._decode_reverse_window(
                capture,
                9,
                0,
                generation=generation,
            )
        )
    )
    decode_thread.start()
    assert capture.read_started.wait(1.0)

    worker.pause()
    worker.release_cache()
    capture.allow_read.set()
    decode_thread.join(2.0)

    assert not decode_thread.is_alive()
    assert worker._request_generation > generation
    assert list(worker._frame_cache) == []
    assert worker._frame_cache_bytes == 0
    assert emitted == []
    assert result == [(True, 1)]


def test_reverse_window_rejects_imprecise_seek_result():
    capture = _ImpreciseSeekCapture(frame_count=30)
    worker = VideoPlaybackWorker(
        Path("recording.mkv"),
        capture_factory=lambda _path: capture,
        reverse_prefetch=False,
    )
    worker._fps = 20.0
    worker._frame_count = 30
    worker._reverse_window_frames = 6

    ok, _capture_next_frame = worker._decode_reverse_window(
        capture,
        10,
        20,
        generation=worker._request_generation,
    )

    assert not ok
    assert list(worker._frame_cache) == []


def test_playing_worker_uses_reverse_window_for_continuous_reverse(qapp):
    capture = _FakeCapture(frame_count=100)
    worker = VideoPlaybackWorker(
        Path("recording.mkv"),
        capture_factory=lambda _path: capture,
        reverse_prefetch=False,
    )
    frame_indexes = []
    loop = QEventLoop()

    def finish():
        worker.stop()
        loop.quit()

    def on_frame(_image, _position_ms, frame_index):
        frame_indexes.append(frame_index)
        if len(frame_indexes) >= 8:
            finish()

    worker.frame_ready.connect(on_frame)
    worker.set_shuttle_speed(-1.0)
    worker.seek_frame(75)
    QTimer.singleShot(2_000, finish)
    worker.start()
    loop.exec_()

    assert worker.wait(2_000)
    assert frame_indexes[0] == 75
    assert frame_indexes[1:] == sorted(frame_indexes[1:], reverse=True)
    assert len(set(frame_indexes)) == len(frame_indexes)
    assert len(capture.set_positions) <= 2
    assert capture.released is True


def test_reverse_playback_prefetches_next_window_with_secondary_capture(qapp):
    captures = []

    def capture_factory(_path):
        capture = _FakeCapture(frame_count=100)
        captures.append(capture)
        return capture

    worker = VideoPlaybackWorker(
        Path("recording.mkv"),
        capture_factory=capture_factory,
    )
    frame_indexes = []
    loop = QEventLoop()

    def finish():
        worker.stop()
        loop.quit()

    def on_frame(_image, _position_ms, frame_index):
        frame_indexes.append(frame_index)
        if len(frame_indexes) >= 10:
            QTimer.singleShot(50, finish)

    worker.frame_ready.connect(on_frame)
    worker.set_shuttle_speed(-1.0)
    worker.seek_frame(75)
    QTimer.singleShot(2_000, finish)
    worker.start()
    loop.exec_()

    assert worker.wait(2_000)
    assert len(captures) == 2
    assert captures[1].set_positions == [55]
    assert captures[1].read_positions == list(range(55, 65))
    assert frame_indexes == sorted(frame_indexes, reverse=True)
    assert all(capture.released for capture in captures)
