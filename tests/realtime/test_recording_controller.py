from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest

from realtime.recording_controller import start_recording_pipeline


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
