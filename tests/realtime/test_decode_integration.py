"""Shared decoder ownership for single-video analysis and duration probing."""

import threading
import time

import cv2
import pytest

from realtime import decode_resources, video_activity, video_timeline
from realtime.decode_resources import DecodeResources


class Capture:
    def __init__(self):
        self.released = False

    def isOpened(self):
        return True

    def set(self, *_args):
        return True

    def get(self, key):
        return 25 if key == cv2.CAP_PROP_FPS else 200

    def release(self):
        self.released = True


def test_queued_activity_cancels_without_opening(monkeypatch, tmp_path):
    pool = DecodeResources(max_captures=1, max_background=1)
    lease = pool.open_capture("busy", lambda _path: Capture())
    opened = []
    monkeypatch.setattr(video_activity, "DECODE_RESOURCES", pool)
    monkeypatch.setattr(cv2, "VideoCapture", lambda path: opened.append(path))
    worker = video_activity.VideoActivityWorker(tmp_path / "test.mp4", 0, 1000)
    thread = threading.Thread(target=worker.run)
    thread.start()
    try:
        deadline = time.monotonic() + 2
        while not pool.snapshot().waiting and time.monotonic() < deadline:
            time.sleep(0.005)
        assert pool.snapshot().waiting == 1
        worker.stop()
        thread.join(2)
        assert not thread.is_alive()
        assert not opened and pool.snapshot().waiting == 0
    finally:
        worker.stop()
        lease.release()
        thread.join(2)


@pytest.mark.parametrize("failure", ["open", "read"])
def test_activity_decoder_failure_releases_budget(monkeypatch, tmp_path, failure):
    pool = DecodeResources()
    capture = Capture()
    capture.isOpened = lambda: failure != "open"
    capture.read = lambda: (_ for _ in ()).throw(RuntimeError("read failed"))
    monkeypatch.setattr(video_activity, "DECODE_RESOURCES", pool)
    monkeypatch.setattr(cv2, "VideoCapture", lambda _path: capture)
    worker = video_activity.VideoActivityWorker(tmp_path / "test.mp4", 0, 1000)
    failures, completed = [], []
    worker.failed.connect(failures.append)
    worker.completed.connect(lambda: completed.append(True))
    worker.run()
    assert len(failures) == 1 and not completed and capture.released
    assert pool.snapshot().captures == 0


def test_duration_probe_defers_when_saturated_or_cancelled_and_can_retry(monkeypatch, tmp_path):
    pool = DecodeResources(max_captures=1, max_background=1)
    monkeypatch.setattr(decode_resources, "DECODE_RESOURCES", pool)
    path = tmp_path / "test.mp4"
    path.write_bytes(b"media")
    capture = Capture()
    opens = []

    def factory(_path):
        opens.append(True)
        return capture

    monkeypatch.setattr(cv2, "VideoCapture", factory)
    lease = pool.open_capture("busy", lambda _path: Capture())
    try:
        assert video_timeline.probe_video_duration_ms(path) is None
        assert not opens and pool.snapshot().waiting == 0
    finally:
        lease.release()
    assert video_timeline.probe_video_duration_ms(path, cancelled=lambda: True) is None
    assert not opens
    assert video_timeline.probe_video_duration_ms(path) == 8000
    assert capture.released and pool.snapshot().captures == 0
