from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

import pytest

import realtime.stream_recorder as stream_recorder
from realtime.stream_recorder import (
    FfmpegStreamRecorder,
    ManualRecordingManager,
    RecordingError,
    find_ffmpeg_executable,
    sanitize_recording_message,
)
from realtime.video_timeline import VideoTimelineStore


class _InspectableBytesIO(BytesIO):
    def close(self):
        pass


class _FakeProcess:
    def __init__(self):
        self.returncode = None
        self.stdin = _InspectableBytesIO()
        self.stderr = BytesIO()
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = 0
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 1

    def kill(self):
        self.killed = True
        self.returncode = 1


class _ProcessFactory:
    def __init__(self):
        self.calls = []
        self.processes = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        Path(command[-1]).write_bytes(b"recorded")
        process = _FakeProcess()
        self.processes.append(process)
        return process


def test_find_ffmpeg_prefers_bundled_executable(tmp_path):
    executable = tmp_path / "ffmpeg.exe"
    executable.write_bytes(b"binary")

    assert find_ffmpeg_executable(base_dir=tmp_path, environ={}, which=lambda _name: None) == executable.resolve()


def test_find_ffmpeg_uses_pyinstaller_resource_directory(monkeypatch, tmp_path):
    executable = tmp_path / "ffmpeg.exe"
    executable.write_bytes(b"binary")
    monkeypatch.setattr(stream_recorder, "application_dir", lambda: tmp_path / "app")
    monkeypatch.setattr(stream_recorder, "resource_dir", lambda: tmp_path)

    assert find_ffmpeg_executable(environ={}, which=lambda _name: None) == executable.resolve()


def test_recorder_uses_packet_copy_and_stops_cleanly(tmp_path):
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffmpeg.write_bytes(b"binary")
    factory = _ProcessFactory()
    recorder = FfmpegStreamRecorder(
        "rtsp://admin:secret@192.0.2.10/live",
        tmp_path / "videos",
        camera_index=1,
        ffmpeg_path=ffmpeg,
        popen_factory=factory,
        clock=lambda: datetime(2026, 8, 8, 12, 34, 56, tzinfo=timezone.utc),
    )

    output = recorder.start()

    command, kwargs = factory.calls[0]
    assert output.name == "camera_01_20260808_123456.mkv"
    assert command[command.index("-c") + 1] == "copy"
    assert command[command.index("-rtsp_transport") + 1] == "tcp"
    assert "creation_time=2026-08-08T12:34:56Z" in command
    assert kwargs["stdout"] is not None
    assert recorder.is_running is True

    assert recorder.stop() == output
    assert factory.processes[0].stdin.getvalue() == b"q\n"
    assert output.read_bytes() == b"recorded"


def test_recorder_rejects_non_rtsp_sources(tmp_path):
    recorder = FfmpegStreamRecorder(
        "race.mp4",
        tmp_path,
        camera_index=1,
        ffmpeg_path=tmp_path / "ffmpeg.exe",
    )

    with pytest.raises(RecordingError, match="RTSP"):
        recorder.start()


def test_recording_errors_redact_rtsp_passwords():
    message = sanitize_recording_message(
        "failed: rtsp://admin:top-secret@192.0.2.10/live"
    )

    assert "top-secret" not in message
    assert "rtsp://admin:***@192.0.2.10/live" in message


def test_recorder_drains_and_redacts_ffmpeg_errors(tmp_path):
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffmpeg.write_bytes(b"binary")

    def _factory(_command, **_kwargs):
        process = _FakeProcess()
        process.stderr = BytesIO(
            b"failed to open rtsp://admin:top-secret@192.0.2.10/live\n"
        )
        process.returncode = 1
        return process

    recorder = FfmpegStreamRecorder(
        "rtsp://admin:top-secret@192.0.2.10/live",
        tmp_path / "videos",
        camera_index=1,
        ffmpeg_path=ffmpeg,
        popen_factory=_factory,
    )
    recorder.start()
    recorder._stderr_thread.join(timeout=1)

    error = recorder.check_error()

    assert "top-secret" not in error
    assert "rtsp://admin:***@192.0.2.10/live" in error


def test_recorder_restart_keeps_previous_segment(tmp_path):
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffmpeg.write_bytes(b"binary")
    factory = _ProcessFactory()
    recorder = FfmpegStreamRecorder(
        "rtsp://camera/live",
        tmp_path / "videos",
        camera_index=1,
        ffmpeg_path=ffmpeg,
        popen_factory=factory,
        clock=lambda: datetime(2026, 8, 10, 22, 0, 0, tzinfo=timezone.utc),
    )

    first_path = recorder.start()
    factory.processes[0].returncode = 1
    second_path = recorder.restart()

    assert first_path.name == "camera_01_20260810_220000.mkv"
    assert second_path.name == "camera_01_20260810_220000_02.mkv"
    assert recorder.output_paths == (first_path, second_path)
    assert recorder.size_bytes == len(b"recorded") * 2
    assert recorder.stop() == second_path


def test_manager_starts_one_recorder_per_rtsp_source(tmp_path):
    created = []

    class _Recorder:
        def __init__(self, source, output_dir, *, camera_index, ffmpeg_path):
            self.source = source
            self.camera_index = camera_index
            self.output_path = output_dir / f"camera_{camera_index:02d}.mkv"
            self.started_at = datetime.now().astimezone()
            self.is_running = False
            self.size_bytes = 7
            created.append(self)

        def start(self):
            self.output_path.parent.mkdir(parents=True, exist_ok=True)
            self.output_path.write_bytes(b"recorded")
            self.is_running = True
            return self.output_path

        def check_error(self):
            return None

        def stop(self):
            self.is_running = False
            return self.output_path

    manager = ManualRecordingManager(
        ["rtsp://camera-one/live", "race.mp4", "rtsp://camera-three/live"],
        tmp_path / "videos",
        recorder_factory=_Recorder,
    )

    paths = manager.start()

    assert [recorder.camera_index for recorder in created] == [1, 3]
    assert manager.is_recording is True
    assert manager.total_size_bytes == 14
    assert paths == (
        tmp_path / "videos" / "camera_01.mkv",
        tmp_path / "videos" / "camera_03.mkv",
    )
    assert manager.stop() == paths


def test_manager_auto_restarts_failed_recorder_and_reports_new_segment(tmp_path):
    created = []

    class _RecoverableRecorder:
        def __init__(self, source, output_dir, *, camera_index, ffmpeg_path):
            self.source = source
            self.output_dir = output_dir
            self.camera_index = camera_index
            self.output_path = None
            self.output_paths = ()
            self.started_at = datetime.now().astimezone()
            self.is_running = False
            self.size_bytes = 0
            self.failed = False
            created.append(self)

        def start(self):
            self.output_path = self.output_dir / "camera_01_first.mkv"
            self.output_paths = (self.output_path,)
            self.is_running = True
            return self.output_path

        def check_error(self):
            if self.failed:
                self.failed = False
                self.is_running = False
                return "Error number -10054 occurred"
            return None

        def restart(self):
            self.output_path = self.output_dir / "camera_01_second.mkv"
            self.output_paths = (*self.output_paths, self.output_path)
            self.is_running = True
            return self.output_path

        def stop(self):
            self.is_running = False
            return self.output_path

    manager = ManualRecordingManager(
        ["rtsp://camera/live"],
        tmp_path / "videos",
        recorder_factory=_RecoverableRecorder,
    )
    manager.start()
    created[0].failed = True

    assert manager.check_error() is None
    assert manager.is_recording is True
    assert manager.output_paths == (
        tmp_path / "videos" / "camera_01_first.mkv",
        tmp_path / "videos" / "camera_01_second.mkv",
    )
    assert manager.consume_recovery_notice() == (
        "机位 1 RTSP 连接中断，已自动续录到 camera_01_second.mkv"
    )
    assert manager.consume_recovery_notice() is None
    assert manager.stop() == (
        tmp_path / "videos" / "camera_01_first.mkv",
        tmp_path / "videos" / "camera_01_second.mkv",
    )


def test_manager_requires_an_rtsp_source(tmp_path):
    manager = ManualRecordingManager([0, "race.mp4"], tmp_path / "videos")

    with pytest.raises(RecordingError, match="RTSP"):
        manager.start()


def test_manager_persists_restart_segments_in_video_timeline(tmp_path, monkeypatch):
    timeline = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    utc = timezone.utc
    end_times = iter(
        [
            datetime(2026, 8, 20, 12, 0, 5, tzinfo=utc),
            datetime(2026, 8, 20, 12, 0, 10, tzinfo=utc),
        ]
    )
    media_durations = {
        "camera_01_first.mkv": 4_000,
        "camera_01_second.mkv": 4_500,
    }
    monkeypatch.setattr(
        stream_recorder,
        "probe_video_duration_ms",
        lambda path: media_durations[path.name],
    )

    class _Recorder:
        def __init__(self, source, output_dir, *, camera_index, ffmpeg_path):
            self.source = source
            self.output_dir = output_dir
            self.camera_index = camera_index
            self.output_path = None
            self.output_paths = ()
            self.started_at = datetime(2026, 8, 20, 12, 0, 0, tzinfo=utc)
            self.is_running = False
            self.size_bytes = 0
            self.failed = False

        def _write(self, name):
            self.output_path = self.output_dir / name
            self.output_path.parent.mkdir(parents=True, exist_ok=True)
            self.output_path.write_bytes(b"video")
            self.output_paths = (*self.output_paths, self.output_path)
            self.size_bytes = sum(path.stat().st_size for path in self.output_paths)
            self.is_running = True
            return self.output_path

        def start(self):
            return self._write("camera_01_first.mkv")

        def check_error(self):
            if not self.failed:
                return None
            self.failed = False
            self.is_running = False
            return "connection lost"

        def restart(self):
            self.started_at = datetime(2026, 8, 20, 12, 0, 5, tzinfo=utc)
            return self._write("camera_01_second.mkv")

        def stop(self):
            self.is_running = False
            return self.output_path

    manager = ManualRecordingManager(
        ["rtsp://camera/live"],
        tmp_path / "videos",
        recorder_factory=_Recorder,
        timeline_store=timeline,
        timeline_timing_error_ms=1_250,
        clock=lambda: next(end_times),
    )
    manager.start()
    manager._recorders[0].failed = True

    assert manager.check_error() is None
    manager.stop()

    segments = timeline.segments()
    assert [segment.video_path for segment in segments] == [
        "videos/camera_01_first.mkv",
        "videos/camera_01_second.mkv",
    ]
    assert [segment.end_reason for segment in segments] == [
        "unexpected_exit",
        "stopped",
    ]
    assert segments[0].ended_at_ms == 1_787_227_205_000
    assert segments[0].media_duration_ms == 4_000
    assert segments[0].media_started_at_ms == 1_787_227_201_000
    assert segments[1].started_at_ms == 1_787_227_205_000
    assert segments[1].ended_at_ms == 1_787_227_210_000
    assert segments[1].media_duration_ms == 4_500
    assert segments[1].media_started_at_ms == 1_787_227_205_500
    assert all(segment.timing_error_ms == 1_250 for segment in segments)
