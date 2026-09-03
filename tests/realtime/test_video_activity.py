from pathlib import Path

import numpy as np
from PyQt5.QtCore import QPoint, Qt
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication

import realtime.video_activity as activity_module
from realtime.video_activity import ActivityTimelineWidget, VideoActivityWorker


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
