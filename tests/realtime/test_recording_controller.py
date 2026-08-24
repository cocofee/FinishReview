from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest

from realtime.recording_controller import (
    RecordingSessionController,
    start_recording_pipeline,
)
from realtime.stream_recorder import RecordingError


class _FakeRecorder:
    instances: ClassVar[list["_FakeRecorder"]] = []

    def __init__(
        self,
        source,
        output_dir,
        *,
        camera_index,
        ffmpeg_path,
        review_retention_seconds,
    ):
        self.source = source
        self.output_dir = Path(output_dir)
        self.camera_index = camera_index
        self.ffmpeg_path = ffmpeg_path
        self.review_retention_seconds = review_retention_seconds
        self.playlist_path = (
            self.output_dir
            / "review_buffer"
            / f"camera_{camera_index:02d}"
            / "playlist.m3u8"
        )
        self.is_running = False
        self.stop_calls = 0
        type(self).instances.append(self)

    def start(self):
        self.is_running = True
        return self.playlist_path

    def stop(self):
        self.stop_calls += 1
        self.is_running = False


class _FakeRingBuffer:
    instances: ClassVar[list["_FakeRingBuffer"]] = []

    def __init__(self, playlist_path, *, camera_index, retention_seconds):
        self.playlist_path = Path(playlist_path)
        self.camera_index = camera_index
        self.retention_seconds = retention_seconds
        self.scan_calls = 0
        type(self).instances.append(self)

    def scan(self):
        self.scan_calls += 1


class _FakeCoordinator:
    def __init__(self, ring_buffer):
        self.ring_buffer = ring_buffer


class _FakeTimelinePublisher:
    def __init__(self, ring_buffer, timeline_store, *, timing_error_ms):
        self.ring_buffer = ring_buffer
        self.timeline_store = timeline_store
        self.timing_error_ms = timing_error_ms


class _FakeArchivePublisher:
    def __init__(self, recorder, timeline_store):
        self.recorder = recorder
        self.timeline_store = timeline_store


@pytest.fixture(autouse=True)
def clear_fakes():
    _FakeRecorder.instances.clear()
    _FakeRingBuffer.instances.clear()


def _start(tmp_path, **overrides):
    values = {
        "source": "rtsp://camera/live",
        "output_dir": tmp_path,
        "camera_index": 2,
        "ffmpeg_path": tmp_path / "ffmpeg.exe",
        "review_retention_seconds": 360,
        "timeline_store": object(),
        "timing_error_ms": 125,
        "recorder_factory": _FakeRecorder,
        "ring_buffer_factory": _FakeRingBuffer,
        "coordinator_factory": _FakeCoordinator,
        "timeline_publisher_factory": _FakeTimelinePublisher,
        "archive_publisher_factory": _FakeArchivePublisher,
    }
    values.update(overrides)
    return start_recording_pipeline(**values)


def _controller(**overrides):
    values = {
        "recorder_factory": _FakeRecorder,
        "ring_buffer_factory": _FakeRingBuffer,
        "coordinator_factory": _FakeCoordinator,
        "timeline_publisher_factory": _FakeTimelinePublisher,
        "archive_publisher_factory": _FakeArchivePublisher,
    }
    values.update(overrides)
    return RecordingSessionController(**values)


def _start_session(controller, tmp_path, **overrides):
    values = {
        "sources": ((1, "rtsp://camera-one/live"),),
        "output_dir": tmp_path,
        "ffmpeg_path": tmp_path / "ffmpeg.exe",
        "review_retention_seconds": 360,
        "timeline_store": object(),
        "timing_error_ms": 125,
    }
    values.update(overrides)
    return controller.start(**values)


def test_start_recording_pipeline_builds_connected_components(tmp_path):
    pipeline = _start(tmp_path)

    assert pipeline.camera_index == 2
    assert pipeline.recorder is _FakeRecorder.instances[0]
    assert pipeline.recorder.is_running
    assert pipeline.recorder.source == "rtsp://camera/live"
    assert pipeline.recorder.ffmpeg_path == tmp_path / "ffmpeg.exe"
    assert pipeline.recorder.review_retention_seconds == 360
    assert pipeline.ring_buffer is _FakeRingBuffer.instances[0]
    assert pipeline.ring_buffer.playlist_path == pipeline.recorder.playlist_path
    assert pipeline.ring_buffer.camera_index == 2
    assert pipeline.ring_buffer.retention_seconds == 360
    assert pipeline.ring_buffer.scan_calls == 1
    assert pipeline.coordinator.ring_buffer is pipeline.ring_buffer
    assert pipeline.timeline_publisher.ring_buffer is pipeline.ring_buffer
    assert pipeline.timeline_publisher.timing_error_ms == 125
    assert pipeline.archive_publisher.recorder is pipeline.recorder

    pipeline.recorder.stop()


def test_component_creation_failure_stops_started_recorder(tmp_path):
    class _FailingRingBuffer(_FakeRingBuffer):
        def scan(self):
            raise RuntimeError("playlist unavailable")

    with pytest.raises(RuntimeError, match="playlist unavailable"):
        _start(tmp_path, ring_buffer_factory=_FailingRingBuffer)

    recorder = _FakeRecorder.instances[0]
    assert recorder.stop_calls == 1
    assert not recorder.is_running


def test_recorder_start_failure_attempts_cleanup_without_masking_error(tmp_path):
    class _FailingRecorder(_FakeRecorder):
        def start(self):
            self.is_running = True
            raise RuntimeError("camera unavailable")

    with pytest.raises(RuntimeError, match="camera unavailable"):
        _start(tmp_path, recorder_factory=_FailingRecorder)

    recorder = _FailingRecorder.instances[0]
    assert recorder.stop_calls == 1
    assert not recorder.is_running


def test_session_controller_starts_two_cameras_and_exposes_components(tmp_path):
    controller = _controller()

    pipelines = _start_session(
        controller,
        tmp_path,
        sources=(
            (1, "rtsp://camera-one/live"),
            (2, "rtsp://camera-two/live"),
        ),
    )

    assert [pipeline.camera_index for pipeline in pipelines] == [1, 2]
    assert set(controller.pipelines) == {1, 2}
    assert set(controller.recorders) == {1, 2}
    assert set(controller.ring_buffers) == {1, 2}
    assert set(controller.coordinators) == {1, 2}
    assert set(controller.timeline_publishers) == {1, 2}
    assert len(controller.archive_publishers) == 2
    assert controller.any_active
    assert controller.all_active(((1, "rtsp://camera-one/live"), (2, "rtsp://camera-two/live")))

    assert controller.stop() == ()


def test_session_controller_reuses_an_already_active_matching_session(tmp_path):
    controller = _controller()
    sources = ((1, "rtsp://camera-one/live"),)

    first = _start_session(controller, tmp_path, sources=sources)
    second = _start_session(controller, tmp_path, sources=sources)

    assert second == first
    assert len(_FakeRecorder.instances) == 1

    controller.stop()


def test_session_controller_rejects_a_different_session_while_active(tmp_path):
    controller = _controller()
    _start_session(controller, tmp_path)

    with pytest.raises(RecordingError, match="already active"):
        _start_session(
            controller,
            tmp_path,
            sources=((2, "rtsp://camera-two/live"),),
        )

    assert len(_FakeRecorder.instances) == 1
    controller.stop()


def test_session_controller_rolls_back_prior_camera_when_later_start_fails(tmp_path):
    class _FailSecondRecorder(_FakeRecorder):
        def start(self):
            if self.camera_index == 2:
                raise RuntimeError("camera two unavailable")
            return super().start()

    controller = _controller(recorder_factory=_FailSecondRecorder)

    with pytest.raises(RuntimeError, match="camera two unavailable"):
        _start_session(
            controller,
            tmp_path,
            sources=(
                (1, "rtsp://camera-one/live"),
                (2, "rtsp://camera-two/live"),
            ),
        )

    assert not controller.pipelines
    assert len(_FailSecondRecorder.instances) == 2
    assert all(recorder.stop_calls == 1 for recorder in _FailSecondRecorder.instances)
    assert not any(recorder.is_running for recorder in _FailSecondRecorder.instances)


def test_session_controller_retains_prior_camera_when_startup_rollback_fails(
    tmp_path,
):
    class _FailStartAndRollbackRecorder(_FakeRecorder):
        def start(self):
            if self.camera_index == 2:
                raise RuntimeError("camera two unavailable")
            return super().start()

        def stop(self):
            self.stop_calls += 1
            if self.camera_index == 1 and self.stop_calls == 1:
                raise OSError("shutdown unavailable")
            self.is_running = False

    controller = _controller(recorder_factory=_FailStartAndRollbackRecorder)

    with pytest.raises(RuntimeError, match="camera two unavailable"):
        _start_session(
            controller,
            tmp_path,
            sources=(
                (1, "rtsp://camera-one/live"),
                (2, "rtsp://camera-two/live"),
            ),
        )

    assert set(controller.recorders) == {1}
    assert controller.recorders[1].is_running
    assert controller.stop() == ()
    assert not controller.recorders


def test_session_controller_retains_incomplete_pipeline_when_cleanup_fails(tmp_path):
    class _FailStartAndCleanupRecorder(_FakeRecorder):
        def start(self):
            self.is_running = True
            raise RuntimeError("camera unavailable")

        def stop(self):
            self.stop_calls += 1
            if self.stop_calls == 1:
                raise OSError("shutdown unavailable")
            self.is_running = False

    controller = _controller(recorder_factory=_FailStartAndCleanupRecorder)

    with pytest.raises(RuntimeError, match="camera unavailable"):
        _start_session(controller, tmp_path)

    assert not controller.pipelines
    assert set(controller.recorders) == {1}
    assert controller.recorders[1].is_running
    assert controller.stop() == ()
    assert not controller.recorders


def test_session_controller_stop_is_idempotent(tmp_path):
    controller = _controller()
    _start_session(
        controller,
        tmp_path,
        sources=(
            (1, "rtsp://camera-one/live"),
            (2, "rtsp://camera-two/live"),
        ),
    )

    assert controller.stop() == ()
    assert controller.stop() == ()

    assert not controller.pipelines
    assert [recorder.stop_calls for recorder in _FakeRecorder.instances] == [1, 1]


def test_session_controller_keeps_running_stop_failure_for_retry(tmp_path):
    class _FailOnceStopRecorder(_FakeRecorder):
        def stop(self):
            self.stop_calls += 1
            if self.camera_index == 1 and self.stop_calls == 1:
                raise OSError("shutdown unavailable")
            self.is_running = False

    controller = _controller(recorder_factory=_FailOnceStopRecorder)
    _start_session(
        controller,
        tmp_path,
        sources=(
            (1, "rtsp://camera-one/live"),
            (2, "rtsp://camera-two/live"),
        ),
    )

    failures = controller.stop()

    assert [
        (failure.camera_index, type(failure.error), failure.still_running)
        for failure in failures
    ] == [
        (1, OSError, True)
    ]
    assert set(controller.pipelines) == {1}
    assert controller.recorders[1].is_running
    assert not _FailOnceStopRecorder.instances[1].is_running

    assert controller.stop() == ()
    assert not controller.pipelines
    assert _FailOnceStopRecorder.instances[0].stop_calls == 2


def test_session_controller_distinguishes_stopped_recorder_warning(tmp_path):
    class _StoppedWithWarningRecorder(_FakeRecorder):
        def stop(self):
            self.stop_calls += 1
            self.is_running = False
            raise RecordingError("ffmpeg exited with code 7")

    controller = _controller(recorder_factory=_StoppedWithWarningRecorder)
    _start_session(controller, tmp_path)

    failures = controller.stop()

    assert len(failures) == 1
    assert failures[0].camera_index == 1
    assert not failures[0].still_running
    assert not controller.recorders
