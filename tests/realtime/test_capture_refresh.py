import threading

from realtime.capture_refresh import (
    ArchiveRefreshJob,
    CaptureRefreshRequest,
    CaptureRefreshWorker,
)
from realtime.runtime_metrics import RuntimeMetrics


class _RingBuffer:
    def __init__(self, *, block_scan: bool = False, scan_error: str = ""):
        self.block_scan = block_scan
        self.scan_error = scan_error
        self.scan_started = threading.Event()
        self.release_scan = threading.Event()
        self.scan_threads = []
        self.cleanup_times = []

    def scan(self):
        self.scan_threads.append(threading.get_ident())
        self.scan_started.set()
        if self.scan_error:
            raise OSError(self.scan_error)
        if self.block_scan:
            assert self.release_scan.wait(2)
        return (object(),)

    def cleanup(self, *, current_time_ms: int):
        self.cleanup_times.append(int(current_time_ms))
        return ()


class _ArchivePublisher:
    def __init__(self):
        self.calls = []

    def publish_completed(self, *, race_id: str, recording: bool):
        self.calls.append((race_id, recording, threading.get_ident()))
        return (object(),)


def _request(
    generation: int,
    ring_buffer,
    *,
    publisher=None,
    cleanup: bool = False,
    current_time_ms: int = 1_000,
):
    jobs = (
        (ArchiveRefreshJob(publisher, "race-1", True),)
        if publisher is not None
        else ()
    )
    return CaptureRefreshRequest(
        generation=generation,
        ring_buffers=(ring_buffer,),
        archive_jobs=jobs,
        cleanup=cleanup,
        current_time_ms=current_time_ms,
    )


def test_capture_refresh_runs_file_work_off_caller_thread():
    caller_thread = threading.get_ident()
    ring_buffer = _RingBuffer()
    publisher = _ArchivePublisher()
    metrics = RuntimeMetrics()
    completed = threading.Event()
    results = []
    worker = CaptureRefreshWorker(
        lambda result: (results.append(result), completed.set()),
        metrics=metrics,
    )
    worker.start()

    worker.submit(_request(1, ring_buffer, publisher=publisher, cleanup=True))
    assert completed.wait(2)
    assert worker.stop()

    assert results[0].generation == 1
    assert results[0].discovered_segment_count == 1
    assert len(results[0].archive_segments) == 1
    assert ring_buffer.scan_threads == [publisher.calls[0][2]]
    assert ring_buffer.scan_threads[0] != caller_thread
    assert ring_buffer.cleanup_times == [1_000]
    assert metrics.snapshot("capture_refresh_background").count == 1


def test_capture_refresh_coalesces_pending_requests_and_keeps_cleanup():
    ring_buffer = _RingBuffer(block_scan=True)
    results = []
    completed = threading.Event()

    def receive(result):
        results.append(result)
        if result.generation == 3:
            completed.set()

    worker = CaptureRefreshWorker(receive)
    worker.start()
    worker.submit(_request(1, ring_buffer))
    assert ring_buffer.scan_started.wait(2)

    worker.submit(_request(2, ring_buffer, cleanup=True, current_time_ms=2_000))
    worker.submit(_request(3, ring_buffer, cleanup=False, current_time_ms=3_000))
    ring_buffer.release_scan.set()

    assert completed.wait(2)
    assert worker.stop()
    assert [result.generation for result in results] == [1, 3]
    assert ring_buffer.cleanup_times == []
    assert results[-1].cleanup_after_apply


def test_capture_refresh_isolates_failed_camera_and_still_cleans_all_rings():
    failed = _RingBuffer(scan_error="camera offline")
    healthy = _RingBuffer()
    results = []
    completed = threading.Event()
    worker = CaptureRefreshWorker(
        lambda result: (results.append(result), completed.set())
    )
    worker.start()
    worker.submit(
        CaptureRefreshRequest(
            generation=1,
            ring_buffers=(failed, healthy),
            archive_jobs=(),
            cleanup=True,
            current_time_ms=5_000,
        )
    )

    assert completed.wait(2)
    assert worker.stop()
    assert results[0].discovered_segment_count == 1
    assert "camera offline" in results[0].error
    assert failed.cleanup_times == [5_000]
    assert healthy.cleanup_times == [5_000]


def test_capture_refresh_invalidation_stops_remaining_old_context_work():
    first = _RingBuffer(block_scan=True)
    second = _RingBuffer()
    results = []
    worker = CaptureRefreshWorker(results.append)
    worker.start()
    worker.submit(
        CaptureRefreshRequest(
            generation=1,
            ring_buffers=(first, second),
            archive_jobs=(),
            cleanup=True,
            current_time_ms=5_000,
        )
    )
    assert first.scan_started.wait(2)

    worker.invalidate(2)
    first.release_scan.set()
    assert worker.stop(timeout=2)

    assert second.scan_threads == []
    assert first.cleanup_times == []
    assert second.cleanup_times == []
    assert results == []


def test_capture_refresh_invalidate_waits_for_active_request():
    ring_buffer = _RingBuffer(block_scan=True)
    results = []
    worker = CaptureRefreshWorker(results.append)
    worker.start()
    worker.submit(_request(1, ring_buffer))
    assert ring_buffer.scan_started.wait(2)

    drained = []
    drain_thread = threading.Thread(
        target=lambda: drained.append(worker.invalidate_and_wait(2, timeout=2))
    )
    drain_thread.start()
    assert drain_thread.is_alive()
    ring_buffer.release_scan.set()
    drain_thread.join(timeout=2)

    assert drained == [True]
    assert results == []
    assert worker.stop()


def test_capture_refresh_stop_suppresses_late_result():
    ring_buffer = _RingBuffer(block_scan=True)
    results = []
    worker = CaptureRefreshWorker(results.append)
    worker.start()
    worker.submit(_request(1, ring_buffer))
    assert ring_buffer.scan_started.wait(2)

    assert not worker.stop(timeout=0)
    ring_buffer.release_scan.set()
    assert worker.stop(timeout=2)

    assert results == []
