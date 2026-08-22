"""Manual RTSP recording through an independent FFmpeg process."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional
from urllib.parse import urlsplit

try:
    from .runtime_paths import application_dir, resource_dir
    from .video_timeline import (
        DEFAULT_TIMING_ERROR_MS,
        VideoTimelineStore,
        probe_video_duration_ms,
    )
except ImportError:
    from runtime_paths import application_dir, resource_dir
    from video_timeline import (
        DEFAULT_TIMING_ERROR_MS,
        VideoTimelineStore,
        probe_video_duration_ms,
    )


_RTSP_CREDENTIAL_PATTERN = re.compile(
    r"(?i)(rtsps?://[^:/\s]+:)[^@\s]+@"
)


class RecordingError(RuntimeError):
    """Raised when a recording session cannot start or stop cleanly."""


def is_rtsp_source(source: object) -> bool:
    if not isinstance(source, str):
        return False
    return urlsplit(source.strip()).scheme.lower() in {"rtsp", "rtsps"}


def sanitize_recording_message(value: object) -> str:
    """Remove RTSP passwords before an error reaches logs or the UI."""
    text = str(value or "").strip()
    return _RTSP_CREDENTIAL_PATTERN.sub(r"\1***@", text)


def find_ffmpeg_executable(
    *,
    base_dir: Optional[Path] = None,
    environ: Optional[dict[str, str]] = None,
    which: Callable[[str], Optional[str]] = shutil.which,
) -> Optional[Path]:
    """Find a bundled FFmpeg first, then a system installation."""
    env = os.environ if environ is None else environ
    configured = str(env.get("FINISH_REVIEW_FFMPEG") or "").strip()
    if configured:
        path = Path(configured).expanduser()
        return path.resolve() if path.is_file() else None

    executable_name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    runtime_roots = []
    for root in (
        base_dir,
        application_dir(),
        resource_dir(),
    ):
        if root is None:
            continue
        resolved_root = Path(root).resolve()
        if resolved_root not in runtime_roots:
            runtime_roots.append(resolved_root)
    for runtime_root in runtime_roots:
        bundled = runtime_root / executable_name
        if bundled.is_file():
            return bundled.resolve()

    resolved = which("ffmpeg")
    if not resolved:
        return None
    path = Path(resolved).expanduser()
    return path.resolve() if path.is_file() else None


class FfmpegStreamRecorder:
    """Record one RTSP source without re-encoding the video stream."""

    def __init__(
        self,
        source: str,
        output_dir: Path,
        *,
        camera_index: int,
        ffmpeg_path: Optional[Path] = None,
        popen_factory: Callable[..., subprocess.Popen] = subprocess.Popen,
        clock: Optional[Callable[[], datetime]] = None,
        stop_timeout: float = 5.0,
    ):
        self.source = str(source).strip()
        self.output_dir = Path(output_dir)
        self.camera_index = max(1, int(camera_index))
        self.ffmpeg_path = Path(ffmpeg_path).resolve() if ffmpeg_path else None
        self._popen_factory = popen_factory
        self._clock = clock or (lambda: datetime.now().astimezone())
        self.stop_timeout = max(0.1, float(stop_timeout))

        self._process: Optional[subprocess.Popen] = None
        self._output_path: Optional[Path] = None
        self._output_paths: list[Path] = []
        self._started_at: Optional[datetime] = None
        self._stop_requested = False
        self._last_error: Optional[str] = None
        self._stderr_lines: deque[str] = deque(maxlen=20)
        self._stderr_lock = threading.Lock()
        self._stderr_thread: Optional[threading.Thread] = None

    @property
    def output_path(self) -> Optional[Path]:
        return self._output_path

    @property
    def output_paths(self) -> tuple[Path, ...]:
        return tuple(self._output_paths)

    @property
    def started_at(self) -> Optional[datetime]:
        return self._started_at

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    @property
    def size_bytes(self) -> int:
        total = 0
        for output_path in self._output_paths:
            try:
                total += output_path.stat().st_size
            except OSError:
                continue
        return total

    def _next_output_path(self, started_at: datetime) -> Path:
        stamp = started_at.strftime("%Y%m%d_%H%M%S")
        base_name = f"camera_{self.camera_index:02d}_{stamp}"
        candidate = self.output_dir / f"{base_name}.mkv"
        suffix = 2
        while candidate.exists():
            candidate = self.output_dir / f"{base_name}_{suffix:02d}.mkv"
            suffix += 1
        return candidate

    def _build_command(self, output_path: Path, started_at: datetime) -> list[str]:
        if self.ffmpeg_path is None:
            raise RecordingError("未找到 FFmpeg，无法开始录像")
        creation_time = started_at.astimezone(timezone.utc).isoformat(timespec="seconds")
        creation_time = creation_time.replace("+00:00", "Z")
        return [
            str(self.ffmpeg_path),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostats",
            "-rtsp_transport",
            "tcp",
            "-i",
            self.source,
            "-map",
            "0:v:0",
            "-c",
            "copy",
            "-an",
            "-metadata",
            f"creation_time={creation_time}",
            "-f",
            "matroska",
            str(output_path),
        ]

    def start(self) -> Path:
        if self.is_running:
            raise RecordingError("当前机位已经在录像")
        if not is_rtsp_source(self.source):
            raise RecordingError("当前录像功能仅支持 RTSP 网络摄像头")

        if self.ffmpeg_path is None:
            self.ffmpeg_path = find_ffmpeg_executable()
        if self.ffmpeg_path is None:
            raise RecordingError("未找到 FFmpeg，请重新打包或安装 FFmpeg")

        self.output_dir.mkdir(parents=True, exist_ok=True)
        started_at = self._clock()
        if started_at.tzinfo is None:
            started_at = started_at.astimezone()
        output_path = self._next_output_path(started_at)
        command = self._build_command(output_path, started_at)

        kwargs = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.PIPE,
        }
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if creation_flags:
            kwargs["creationflags"] = creation_flags

        try:
            process = self._popen_factory(command, **kwargs)
        except OSError as exc:
            raise RecordingError(
                f"启动录像进程失败: {sanitize_recording_message(exc)}"
            ) from exc

        self._process = process
        self._output_path = output_path
        self._output_paths.append(output_path)
        self._started_at = started_at
        self._stop_requested = False
        self._last_error = None
        with self._stderr_lock:
            self._stderr_lines.clear()
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr,
            args=(process,),
            name=f"ffmpeg-stderr-camera-{self.camera_index}",
            daemon=True,
        )
        self._stderr_thread.start()
        return output_path

    def _drain_stderr(self, process: subprocess.Popen) -> None:
        stderr = getattr(process, "stderr", None)
        if stderr is None:
            return
        while True:
            try:
                content = stderr.readline()
            except Exception:
                return
            if not content:
                return
            if isinstance(content, bytes):
                content = content.decode("utf-8", errors="replace")
            line = sanitize_recording_message(content)
            if line:
                with self._stderr_lock:
                    self._stderr_lines.append(line)

    def _recent_stderr(self) -> str:
        thread = self._stderr_thread
        if thread is not None and not thread.is_alive():
            thread.join(timeout=0)
        with self._stderr_lock:
            return " | ".join(self._stderr_lines)[-800:]

    def check_error(self) -> Optional[str]:
        if self._process is None:
            return self._last_error
        return_code = self._process.poll()
        if return_code is None or self._stop_requested:
            return None

        detail = self._recent_stderr()
        if return_code == 0:
            message = "录像进程意外提前结束"
        else:
            message = f"录像进程异常退出，代码 {return_code}"
        if detail:
            message = f"{message}: {detail}"
        self._last_error = message
        return message

    def restart(self) -> Path:
        """Start a new MKV segment after an unexpected FFmpeg exit."""
        if self.is_running:
            raise RecordingError("当前录像进程仍在运行，不能自动续录")

        process = self._process
        stderr_thread = self._stderr_thread
        if stderr_thread is not None:
            stderr_thread.join(timeout=1.0)
        if process is not None:
            for stream_name in ("stdin", "stderr"):
                stream = getattr(process, stream_name, None)
                try:
                    if stream is not None:
                        stream.close()
                except Exception:
                    pass

        self._process = None
        self._stderr_thread = None
        return self.start()

    def stop(self) -> Optional[Path]:
        process = self._process
        output_path = self._output_path
        if process is None:
            return output_path

        self._stop_requested = True
        forced_stop = False
        if process.poll() is None:
            try:
                if process.stdin is not None:
                    process.stdin.write(b"q\n")
                    process.stdin.flush()
                process.wait(timeout=self.stop_timeout)
            except (BrokenPipeError, OSError):
                pass
            except subprocess.TimeoutExpired:
                forced_stop = True

            if process.poll() is None:
                forced_stop = True
                process.terminate()
                try:
                    process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2.0)

        return_code = process.poll()
        stderr_thread = self._stderr_thread
        if stderr_thread is not None:
            stderr_thread.join(timeout=1.0)
        detail = self._recent_stderr()
        try:
            if process.stdin is not None:
                process.stdin.close()
        except Exception:
            pass
        self._process = None
        self._stderr_thread = None

        if forced_stop:
            raise RecordingError("录像进程未能正常收尾，当前文件可能不完整")
        if return_code not in (0, None):
            message = f"录像停止异常，代码 {return_code}"
            if detail:
                message = f"{message}: {detail}"
            raise RecordingError(message)
        if output_path is None or not output_path.is_file() or output_path.stat().st_size <= 0:
            raise RecordingError("录像文件未生成或为空")
        return output_path


class ManualRecordingManager:
    """Start and stop one packet-copy recorder per RTSP camera."""

    def __init__(
        self,
        sources: Iterable[object],
        output_dir: Path,
        *,
        ffmpeg_path: Optional[Path] = None,
        recorder_factory: Callable[..., FfmpegStreamRecorder] = FfmpegStreamRecorder,
        max_restart_attempts: int = 3,
        restart_window_seconds: float = 60.0,
        monotonic: Callable[[], float] = time.monotonic,
        clock: Optional[Callable[[], datetime]] = None,
        timeline_store: Optional[VideoTimelineStore] = None,
        timeline_timing_error_ms: int = DEFAULT_TIMING_ERROR_MS,
    ):
        self.sources = list(sources)
        self.output_dir = Path(output_dir)
        self.ffmpeg_path = ffmpeg_path
        self._recorder_factory = recorder_factory
        self.max_restart_attempts = max(1, int(max_restart_attempts))
        self.restart_window_seconds = max(1.0, float(restart_window_seconds))
        self._monotonic = monotonic
        self._clock = clock or (lambda: datetime.now().astimezone())
        self.timeline_store = timeline_store
        self.timeline_timing_error_ms = max(0, int(timeline_timing_error_ms))
        self._recorders: list[FfmpegStreamRecorder] = []
        self._started_at: Optional[datetime] = None
        self._restart_attempts: dict[int, deque[float]] = {}
        self._last_recovery_notice: Optional[str] = None
        self._timeline_segment_ids: dict[int, str] = {}
        self._timeline_warning: Optional[str] = None

    @staticmethod
    def _datetime_ms(value: datetime) -> int:
        if value.tzinfo is None:
            value = value.astimezone()
        return int(round(value.timestamp() * 1000.0))

    def _record_timeline_start(self, recorder: FfmpegStreamRecorder) -> None:
        if self.timeline_store is None or recorder.output_path is None:
            return
        started_at = recorder.started_at or self._clock()
        try:
            segment = self.timeline_store.start_segment(
                source_id=f"camera_{recorder.camera_index:02d}",
                camera_index=recorder.camera_index,
                video_path=recorder.output_path,
                started_at_ms=self._datetime_ms(started_at),
                timing_error_ms=self.timeline_timing_error_ms,
            )
        except Exception as exc:
            self._timeline_warning = (
                f"机位 {recorder.camera_index} 录像已开始，但时间线写入失败: "
                f"{sanitize_recording_message(exc)}"
            )
            return
        self._timeline_segment_ids[recorder.camera_index] = segment.segment_id

    def _record_timeline_end(
        self,
        recorder: FfmpegStreamRecorder,
        end_reason: str,
    ) -> None:
        if self.timeline_store is None:
            return
        camera_index = recorder.camera_index
        segment_id = self._timeline_segment_ids.pop(camera_index, None)
        if segment_id is None:
            return
        media_duration_ms = (
            probe_video_duration_ms(recorder.output_path)
            if recorder.output_path is not None
            else None
        )
        try:
            self.timeline_store.finish_segment(
                segment_id,
                ended_at_ms=self._datetime_ms(self._clock()),
                end_reason=end_reason,
                media_duration_ms=media_duration_ms,
            )
        except Exception as exc:
            self._timeline_warning = (
                f"机位 {camera_index} 录像已保存，但时间线收尾失败: "
                f"{sanitize_recording_message(exc)}"
            )

    @property
    def is_recording(self) -> bool:
        return bool(self._recorders) and all(recorder.is_running for recorder in self._recorders)

    @property
    def output_paths(self) -> tuple[Path, ...]:
        paths: list[Path] = []
        for recorder in self._recorders:
            recorder_paths = tuple(getattr(recorder, "output_paths", ()) or ())
            if recorder_paths:
                paths.extend(recorder_paths)
            elif recorder.output_path is not None:
                paths.append(recorder.output_path)
        return tuple(paths)

    @property
    def total_size_bytes(self) -> int:
        return sum(recorder.size_bytes for recorder in self._recorders)

    @property
    def elapsed_seconds(self) -> float:
        if self._started_at is None:
            return 0.0
        now = datetime.now(self._started_at.tzinfo) if self._started_at.tzinfo else datetime.now()
        return max(0.0, (now - self._started_at).total_seconds())

    def start(self) -> tuple[Path, ...]:
        if self._recorders:
            raise RecordingError("录像已经开始")

        rtsp_sources = [
            (index, str(source).strip())
            for index, source in enumerate(self.sources, start=1)
            if is_rtsp_source(source)
        ]
        if not rtsp_sources:
            raise RecordingError("当前没有可录像的 RTSP 网络摄像头")

        started: list[FfmpegStreamRecorder] = []
        try:
            for camera_index, source in rtsp_sources:
                recorder = self._recorder_factory(
                    source,
                    self.output_dir,
                    camera_index=camera_index,
                    ffmpeg_path=self.ffmpeg_path,
                )
                recorder.start()
                started.append(recorder)
                self._record_timeline_start(recorder)
        except Exception as exc:
            for recorder in started:
                try:
                    recorder.stop()
                except Exception:
                    pass
                self._record_timeline_end(recorder, "start_rollback")
            raise RecordingError(sanitize_recording_message(exc)) from exc

        self._recorders = started
        self._restart_attempts.clear()
        self._last_recovery_notice = None
        self._started_at = min(
            (recorder.started_at for recorder in started if recorder.started_at is not None),
            default=datetime.now().astimezone(),
        )
        return self.output_paths

    def check_error(self) -> Optional[str]:
        for recorder in self._recorders:
            error = recorder.check_error()
            if not error:
                continue

            now = self._monotonic()
            attempts = self._restart_attempts.setdefault(
                recorder.camera_index,
                deque(),
            )
            cutoff = now - self.restart_window_seconds
            while attempts and attempts[0] < cutoff:
                attempts.popleft()
            if len(attempts) >= self.max_restart_attempts:
                return (
                    f"机位 {recorder.camera_index}: {error}; "
                    f"{self.restart_window_seconds:.0f} 秒内自动续录已达 "
                    f"{self.max_restart_attempts} 次"
                )

            try:
                self._record_timeline_end(recorder, "unexpected_exit")
                output_path = recorder.restart()
                self._record_timeline_start(recorder)
            except Exception as exc:
                return (
                    f"机位 {recorder.camera_index}: {error}; "
                    f"自动续录失败: {sanitize_recording_message(exc)}"
                )

            attempts.append(now)
            self._last_recovery_notice = (
                f"机位 {recorder.camera_index} RTSP 连接中断，"
                f"已自动续录到 {output_path.name}"
            )
        return None

    def consume_recovery_notice(self) -> Optional[str]:
        notice = self._last_recovery_notice
        self._last_recovery_notice = None
        return notice

    def consume_timeline_warning(self) -> Optional[str]:
        warning = self._timeline_warning
        self._timeline_warning = None
        return warning

    def stop(self) -> tuple[Path, ...]:
        recorders = list(self._recorders)
        paths = self.output_paths
        self._recorders = []
        self._started_at = None

        errors = []
        for recorder in recorders:
            end_reason = "stopped"
            try:
                recorder.stop()
            except RecordingError as exc:
                end_reason = "stop_error"
                errors.append(f"机位 {recorder.camera_index}: {exc}")
            finally:
                self._record_timeline_end(recorder, end_reason)
        if errors:
            raise RecordingError("; ".join(errors))
        return paths
