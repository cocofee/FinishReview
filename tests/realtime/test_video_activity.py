from pathlib import Path
import threading
import time

import numpy as np
import pytest
from PyQt5.QtCore import QPoint, Qt
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication

import realtime.video_activity as activity_module
from realtime.video_activity import ActivityTimelineWidget, VideoActivityWorker
from realtime.decode_resources import DecodeResources


class _FakeCapture:
    def __init__(self, _path):
        self.index = 0
        self.frames = [
            np.full((18, 32, 3), value, dtype=np.uint8)
            for value in (0, 0, 80, 80, 160, 160, 0, 0, 120, 120, 0, 0)
        ]

    def isOpened(self):
        return True

    def get(self, key):
        if key == activity_module.cv2.CAP_PROP_FPS:
            return 8.0
        if key == activity_module.cv2.CAP_PROP_POS_FRAMES:
            return self.index
        return 0.0

    def set(self, _key, value):
        self.index = int(value)
        return True

    def read(self):
        if self.index >= len(self.frames):
            return False, None
        frame = self.frames[self.index]
        self.index += 1
        return True, frame

    def release(self):
        pass


def test_activity_worker_emits_progressive_change_scores(monkeypatch, tmp_path):
    monkeypatch.setattr(activity_module.cv2, "VideoCapture", _FakeCapture)
    worker = VideoActivityWorker(Path(tmp_path / "video.mp4"), 0, 1_000)
    received = []
    progress = []
    worker.points_ready.connect(lambda points: received.extend(points))
    worker.progress_ready.connect(progress.append)

    worker.run()

    assert received
    assert progress[0] == 0
    assert progress[-1] == 100
    assert all(0 <= position <= 1_000 for position, _score in received)
    assert max(score for _position, score in received) > 0


def test_activity_pause_releases_capture_and_resumes_without_skipping(monkeypatch, tmp_path):
    resources = DecodeResources(max_captures=1, max_background=1)
    monkeypatch.setattr(activity_module, "DECODE_RESOURCES", resources)
    worker = VideoActivityWorker(tmp_path / "video.mp4", 0, 1000)
    paused = threading.Event()
    reads, captures, completed = [], [], []

    class Capture(_FakeCapture):
        def read(self):
            reads.append(self.index)
            result = super().read()
            if len(reads) == 3:
                worker.set_paused(True)
                paused.set()
            return result

        def release(self):
            self.released = True

    def factory(path):
        capture = Capture(path)
        captures.append(capture)
        return capture

    monkeypatch.setattr(activity_module.cv2, "VideoCapture", factory)
    worker.completed.connect(lambda: completed.append(True), Qt.DirectConnection)
    thread = threading.Thread(target=worker.run)
    thread.start()
    try:
        assert paused.wait(2)
        deadline = time.monotonic() + 1
        while resources.snapshot().captures and time.monotonic() < deadline:
            time.sleep(0.005)
        assert resources.snapshot().captures == 0
        worker.set_paused(False)
        thread.join(2)
        assert not thread.is_alive()
        assert reads == list(range(9))
        assert len(captures) == 2 and all(capture.released for capture in captures)
        assert completed == [True]
    finally:
        worker.stop()
        thread.join(2)


def test_activity_timeline_click_maps_to_full_time_range():
    app = QApplication.instance() or QApplication([])
    widget = ActivityTimelineWidget()
    widget.resize(1_001, 44)
    widget.set_range(10_000, 20_000)
    selected = []
    widget.position_selected.connect(selected.append)
    widget.show()

    QTest.mouseClick(widget, Qt.LeftButton, pos=widget.rect().center())
    assert 14_990 <= selected[-1] <= 15_010

    QTest.mouseClick(widget, Qt.LeftButton, pos=QPoint(1_000, 22))
    assert selected[-1] == 10_000
    QTest.mouseClick(widget, Qt.LeftButton, pos=QPoint(1, 22))
    assert 19_980 <= selected[-1] <= 20_000
    widget.close()
    app.processEvents()


@pytest.mark.parametrize("offset", [2, float("nan")])
def test_activity_inaccurate_resume_reports_failure_without_completion(monkeypatch, tmp_path, offset):
    resources = DecodeResources(max_captures=1, max_background=1)
    monkeypatch.setattr(activity_module, "DECODE_RESOURCES", resources)
    monkeypatch.setattr(resources, "_should_yield", lambda *_args: len(captures) == 1)
    captures, failures, completed = [], [], []

    class Capture(_FakeCapture):
        def get(self, key):
            value = super().get(key)
            return value + offset if key == activity_module.cv2.CAP_PROP_POS_FRAMES else value

        def release(self):
            self.released = True

    def factory(path):
        capture = Capture(path)
        captures.append(capture)
        return capture

    monkeypatch.setattr(activity_module.cv2, "VideoCapture", factory)
    worker = VideoActivityWorker(tmp_path / "video.mp4", 0, 1000)
    worker.failed.connect(failures.append)
    worker.completed.connect(lambda: completed.append(True))
    worker.run()
    assert failures and not completed
    assert all(capture.released for capture in captures)
    assert resources.snapshot().captures == 0
