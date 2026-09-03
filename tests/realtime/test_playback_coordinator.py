import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtCore import QObject, pyqtSignal
from PyQt5.QtWidgets import QApplication

from realtime.playback_coordinator import PlaybackCoordinator
from realtime.video_activity import ActivityTimelineWidget


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class _FakeActivityWorker(QObject):
    points_ready = pyqtSignal(object)
    progress_ready = pyqtSignal(int)
    completed = pyqtSignal()
    finished = pyqtSignal()

    def __init__(self, video_path, start_ms, end_ms, parent=None):
        super().__init__(parent)
        self.video_path = Path(video_path)
        self.start_ms = int(start_ms)
        self.end_ms = int(end_ms)
        self.started = False
        self.paused = []
        self.stop_requested = False

    def start(self, _priority):
        self.started = True

    def set_paused(self, paused):
        self.paused.append(bool(paused))

    def request_stop(self):
        self.stop_requested = True

    def isRunning(self):
        return False


def test_filmstrip_update_can_be_immediate_or_deferred(qapp):
    timeline = ActivityTimelineWidget()
    coordinator = PlaybackCoordinator(
        timeline,
        filmstrip_update_delay_ms=60_000,
    )
    requests = []
    coordinator.filmstrip_update_requested.connect(lambda: requests.append(True))

    coordinator.request_filmstrip_update(deferred=True)
    assert requests == []

    coordinator.request_filmstrip_update(deferred=False)
    assert requests == [True]


def test_activity_analysis_is_delayed_paused_and_cached(qapp, tmp_path):
    timeline = ActivityTimelineWidget()
    workers = []

    def worker_factory(*args, **kwargs):
        worker = _FakeActivityWorker(*args, **kwargs)
        workers.append(worker)
        return worker

    coordinator = PlaybackCoordinator(
        timeline,
        activity_worker_factory=worker_factory,
        activity_start_delay_ms=60_000,
    )
    video_path = tmp_path / "camera.mkv"

    coordinator.schedule_activity(video_path, 1_000, 9_000)

    assert workers == []
    assert timeline._analysis_state == "等待分析"
    coordinator._start_activity_analysis()
    worker = workers[0]
    assert worker.started

    coordinator.set_operator_busy(True)
    assert worker.paused == [True]
    assert timeline._analysis_state == "暂停分析（正在操作视频）"

    worker.progress_ready.emit(40)
    worker.points_ready.emit(((2_000, 0.25), (3_000, 0.5)))
    worker.completed.emit()

    assert coordinator.activity_progress == 100
    assert timeline._analysis_state == "分析完成"
    assert timeline._points == [(2_000, 0.25), (3_000, 0.5)]

    coordinator.clear_activity()
    coordinator.schedule_activity(video_path, 1_000, 9_000)

    assert len(workers) == 1
    assert timeline._analysis_progress == 100
    assert timeline._points == [(2_000, 0.25), (3_000, 0.5)]
