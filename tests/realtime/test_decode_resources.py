import threading
import time
import gc

import pytest
from PyQt5.QtGui import QImage

from realtime.decode_resources import DecodeResources, FOREGROUND, THUMBNAIL, PREFETCH, ANALYSIS, ImageCache


class Capture:
    def __init__(self, _path):
        self.released = False

    def release(self):
        self.released = True


def test_cancel_during_open_releases_capture_without_delivering_it():
    resources = DecodeResources()
    cancelled = threading.Event()
    capture = Capture("x")

    def factory(_path):
        cancelled.set()
        return capture

    assert resources.open_capture("x", factory, cancelled=cancelled.is_set) is None
    assert capture.released
    assert resources.snapshot().captures == 0


def test_analysis_yields_to_waiting_thumbnail_and_timeout_removes_ticket():
    resources = DecodeResources(max_captures=2, max_background=1)
    analysis = resources.open_capture("x", Capture, priority=ANALYSIS)
    acquired = threading.Event()

    def thumbnail():
        capture = resources.open_capture("x", Capture, priority=THUMBNAIL, timeout=1)
        if capture is not None:
            acquired.set()
            capture.release()

    thread = threading.Thread(target=thumbnail)
    thread.start()
    try:
        deadline = time.monotonic() + 1
        while not analysis.should_yield() and time.monotonic() < deadline:
            time.sleep(0.005)
        assert analysis.should_yield()
        assert resources.open_capture("x", Capture, priority=ANALYSIS, timeout=0.01) is None
        assert resources.snapshot().waiting == 1
    finally:
        analysis.release()
        thread.join(2)
    assert acquired.is_set()
    assert resources.snapshot().captures == resources.snapshot().waiting == 0


def test_background_capacity_reserves_foreground_and_releases_on_failure():
    resources = DecodeResources(max_captures=2, max_background=1)
    background = resources.open_capture("x", Capture, priority=PREFETCH)
    assert resources.open_capture("x", Capture, priority=THUMBNAIL, wait=False) is None
    foreground = resources.open_capture("x", Capture)
    assert resources.snapshot().captures == 2
    foreground.release()
    foreground.release()
    background.release()

    def fail(_path):
        raise OSError("cannot open")

    with pytest.raises(OSError):
        resources.open_capture("x", fail)
    assert resources.snapshot().captures == 0
    assert resources.snapshot().peak_captures == 2


def test_waiting_foreground_wins_and_cancelled_task_never_opens():
    resources = DecodeResources(max_captures=1, max_background=1)
    held = resources.open_capture("x", Capture)
    opened = []
    cancel = threading.Event()

    def request(priority):
        capture = resources.open_capture("x", Capture, priority=priority, cancelled=cancel.is_set)
        if capture is not None:
            opened.append(priority)
            if priority == FOREGROUND:
                cancel.set()
            capture.release()

    threads = [threading.Thread(target=request, args=(priority,)) for priority in (PREFETCH, FOREGROUND)]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + 2
    while resources.snapshot().waiting < 2 and time.monotonic() < deadline:
        time.sleep(0.005)
    assert resources.snapshot().waiting == 2
    held.release()
    for thread in threads:
        thread.join(2)
        assert not thread.is_alive()
    assert opened == [FOREGROUND]
    assert resources.snapshot().captures == resources.snapshot().waiting == 0


def test_global_cache_evicts_thumbnails_before_review_frames_and_reclaims_bytes():
    image = QImage(10, 10, QImage.Format_RGB32)
    resources = DecodeResources(max_cache_bytes=image.sizeInBytes() * 3)
    review = ImageCache(resources=resources)
    thumbnails = ImageCache(resources=resources, priority=THUMBNAIL)
    review[1] = image
    review[2] = image
    thumbnails[1] = image
    thumbnails[2] = image
    assert list(review) == [1, 2]
    assert list(thumbnails) == [2]
    assert resources.snapshot().cache_bytes == image.sizeInBytes() * 3
    assert review.get(1) is image
    review[3] = image
    assert not thumbnails
    assert resources.snapshot().peak_cache_bytes <= resources.max_cache_bytes
    review.clear()
    assert resources.snapshot().cache_bytes == 0


def test_repeated_concurrent_work_obeys_budgets_and_releases_all_resources():
    resources = DecodeResources(max_captures=3, max_background=1, max_cache_bytes=4096)
    errors = []

    def exercise(priority):
        try:
            cache = ImageCache(resources=resources, priority=priority)
            for iteration in range(20):
                capture = resources.open_capture("x", Capture, priority=priority)
                try:
                    cache[iteration] = QImage(16, 16, QImage.Format_RGB32)
                    assert resources.snapshot().cache_bytes <= 4096
                finally:
                    capture.release()
        except Exception as error:
            errors.append(error)

    threads = [threading.Thread(target=exercise, args=(index % 3,)) for index in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
        assert not thread.is_alive()
    gc.collect()
    assert not errors
    snapshot = resources.snapshot()
    assert snapshot.peak_captures <= 3
    assert snapshot.peak_cache_bytes <= 4096
    assert snapshot.captures == snapshot.waiting == snapshot.cache_bytes == 0
