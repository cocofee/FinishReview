"""Finish-console archive recording and passage-oriented rolling review buffer."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

try:
    from .stream_recorder import (
        RecordingError,
        find_ffmpeg_executable,
        is_rtsp_source,
        sanitize_recording_message,
    )
    from .video_timeline import (
        DEFAULT_CLOCK_SOURCE,
        DEFAULT_TIMING_ERROR_MS,
        RecordingSegment,
        VideoTimelineStore,
        probe_video_duration_ms,
    )
except ImportError:
    from stream_recorder import (
        RecordingError,
        find_ffmpeg_executable,
        is_rtsp_source,
        sanitize_recording_message,
    )
    from video_timeline import (
        DEFAULT_CLOCK_SOURCE,
        DEFAULT_TIMING_ERROR_MS,
        RecordingSegment,
        VideoTimelineStore,
        probe_video_duration_ms,
    )


DEFAULT_ARCHIVE_SEGMENT_SECONDS = 300
DEFAULT_REVIEW_SEGMENT_SECONDS = 2
DEFAULT_REVIEW_RETENTION_SECONDS = 90
DEFAULT_REVIEW_PRE_ROLL_SECONDS = 3
DEFAULT_REVIEW_POST_ROLL_SECONDS = 3
DEFAULT_ARCHIVE_TIMING_ERROR_MS = 3_000
ARCHIVE_SESSION_SCHEMA_VERSION = 1
BEIJING_TIMEZONE = timezone(timedelta(hours=8))
_DIRECTSHOW_SCHEME = "dshow"
_VIDEO_SIZE_PATTERN = re.compile(r"^[1-9]\d{1,4}x[1-9]\d{1,4}$")
_DIRECTSHOW_VIDEO_DEVICE_PATTERN = re.compile(r'"(?P<name>[^"]+)"\s+\(video\)')


@dataclass(frozen=True, slots=True)
class DirectShowReviewSource:
    """Validated Windows USB/UVC camera input settings."""

    device_name: str
    video_size: str | None = None
    framerate: float | None = None


def make_directshow_source(
    device_name: str,
    *,
    video_size: str | None = None,
    framerate: float | None = None,
) -> str:
    """Build an internal source URI from an installed DirectShow device."""

    name = str(device_name).strip()
    if not name:
        raise ValueError("USB 摄像头名称不能为空")
    query: list[str] = []
    if video_size is not None:
        normalized_size = str(video_size).strip().lower()
        if not _VIDEO_SIZE_PATTERN.fullmatch(normalized_size):
            raise ValueError("USB 摄像头分辨率必须使用 WIDTHxHEIGHT 格式")
        query.append(f"video_size={quote(normalized_size, safe='x')}")
    if framerate is not None:
        normalized_framerate = float(framerate)
        if not 1.0 <= normalized_framerate <= 240.0:
            raise ValueError("USB 摄像头帧率必须在 1 到 240 之间")
        query.append(f"framerate={normalized_framerate:g}")
    suffix = f"?{'&'.join(query)}" if query else ""
    return f"{_DIRECTSHOW_SCHEME}://{quote(name, safe='')}{suffix}"


def parse_directshow_source(source: object) -> DirectShowReviewSource | None:
    """Parse the private URI used to persist one Windows USB/UVC camera."""

    if not isinstance(source, str):
        return None
    try:
        parsed = urlsplit(source.strip())
    except ValueError:
        return None
    if parsed.scheme.lower() != _DIRECTSHOW_SCHEME or not parsed.netloc or parsed.path:
        return None
    try:
        query = parse_qs(parsed.query, strict_parsing=True)
    except ValueError:
        return None
    if set(query) - {"video_size", "framerate"}:
        return None
    if any(len(values) != 1 for values in query.values()):
        return None

    device_name = unquote(parsed.netloc).strip()
    if not device_name:
        return None
    video_size = query.get("video_size", [None])[0]
    if video_size is not None:
        video_size = video_size.strip().lower()
        if not _VIDEO_SIZE_PATTERN.fullmatch(video_size):
            return None
    framerate_text = query.get("framerate", [None])[0]
    framerate = None
    if framerate_text is not None:
        try:
            framerate = float(framerate_text)
        except (TypeError, ValueError):
            return None
        if not 1.0 <= framerate <= 240.0:
            return None
    return DirectShowReviewSource(
        device_name=device_name,
        video_size=video_size,
        framerate=framerate,
    )


def is_supported_review_source(source: object) -> bool:
    """Return whether the finish-console recorder can open an installed source."""

    return is_rtsp_source(source) or parse_directshow_source(source) is not None


def discover_directshow_video_devices(
    ffmpeg_path: Path | None = None,
    *,
    run_factory: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> tuple[str, ...]:
    """List Windows DirectShow video inputs without exposing FFmpeg details."""

    executable = Path(ffmpeg_path).resolve() if ffmpeg_path else find_ffmpeg_executable()
    if executable is None or not executable.is_file():
        return ()
    command = [
        str(executable),
        "-hide_banner",
        "-list_devices",
        "true",
        "-f",
        "dshow",
        "-i",
        "dummy",
    ]
    kwargs = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": 8,
        "check": False,
    }
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if creation_flags:
        kwargs["creationflags"] = creation_flags
    try:
        result = run_factory(command, **kwargs)
    except (OSError, subprocess.SubprocessError):
        return ()
    output = "\n".join(
        str(value or "")
        for value in (getattr(result, "stdout", ""), getattr(result, "stderr", ""))
    )
    devices = []
    for match in _DIRECTSHOW_VIDEO_DEVICE_PATTERN.finditer(output):
        name = match.group("name").strip()
        if name and name not in devices:
            devices.append(name)
    return tuple(devices)


@dataclass(frozen=True, slots=True)
class ReviewSegment:
    """One completed short recording segment indexed by wall-clock time."""

    segment_id: str
    source_id: str
    camera_index: int
    video_path: str
    started_at_ms: int
    duration_ms: int

    @property
    def ended_at_ms(self) -> int:
        return self.started_at_ms + self.duration_ms


class PassageReviewState(str, Enum):
    """Readiness of one passage-centered review window."""

    WAITING = "waiting_for_recording"
    READY = "ready"
    PARTIAL = "partial"


@dataclass(frozen=True, slots=True)
class PassageReviewWindow:
    """Completed recording evidence currently available around one passage."""

    event_id: str
    passage_timestamp_ms: int
    started_at_ms: int
    ended_at_ms: int
    state: PassageReviewState
    segments: tuple[ReviewSegment, ...] = ()


def _archive_paths(pattern: Path) -> tuple[Path, ...]:
    paths = []
    for path in pattern.parent.glob(pattern.name.replace("%04d", "*")):
        try:
            if path.is_file() and path.stat().st_size > 0:
                paths.append(path.resolve())
        except OSError:
            continue
    return tuple(sorted(paths, key=lambda path: path.name))


@dataclass(frozen=True, slots=True)
class ArchiveRecordingSession:
    camera_index: int
    session_id: str
    session_started_at_ms: int
    archive_pattern: Path

    @property
    def source_id(self) -> str:
        return f"camera_{self.camera_index:02d}_review"

    def archive_paths(self) -> tuple[Path, ...]:
        return _archive_paths(self.archive_pattern)


def load_archive_recording_sessions(
    output_dir: str | Path,
) -> tuple[ArchiveRecordingSession, ...]:
    archive_dir = (Path(output_dir).expanduser().resolve() / "videos").resolve()
    sessions = []
    for manifest_path in archive_dir.glob("*_archive_session.json"):
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            if payload.get("schema_version") != ARCHIVE_SESSION_SCHEMA_VERSION:
                continue
            camera_index = int(payload["camera_index"])
            session_id = str(payload["session_id"]).strip()
            session_started_at_ms = int(payload["session_started_at_ms"])
            pattern_name = str(payload["archive_pattern"]).strip()
            if (
                camera_index <= 0
                or not session_id
                or session_started_at_ms < 0
                or Path(pattern_name).name != pattern_name
                or "%04d" not in pattern_name
            ):
                continue
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        sessions.append(
            ArchiveRecordingSession(
                camera_index=camera_index,
                session_id=session_id,
                session_started_at_ms=session_started_at_ms,
                archive_pattern=(archive_dir / pattern_name).resolve(),
            )
        )
    return tuple(
        sorted(
            sessions,
            key=lambda item: (
                item.session_started_at_ms,
                item.camera_index,
                item.session_id,
            ),
        )
    )


class FfmpegReviewRecorder:
    """Record one RTSP or Windows USB/UVC input for the finish console."""

    def __init__(
        self,
        source: str,
        output_dir: Path,
        *,
        camera_index: int,
        ffmpeg_path: Path | None = None,
        archive_segment_seconds: int = DEFAULT_ARCHIVE_SEGMENT_SECONDS,
        review_segment_seconds: int = DEFAULT_REVIEW_SEGMENT_SECONDS,
        review_retention_seconds: int = DEFAULT_REVIEW_RETENTION_SECONDS,
        popen_factory: Callable[..., subprocess.Popen] = subprocess.Popen,
        clock: Callable[[], datetime] | None = None,
        stop_timeout: float = 5.0,
    ):
        self.source = str(source).strip()
        self.output_dir = Path(output_dir)
        self.camera_index = max(1, int(camera_index))
        self.ffmpeg_path = Path(ffmpeg_path).resolve() if ffmpeg_path else None
        self.archive_segment_seconds = max(30, int(archive_segment_seconds))
        self.review_segment_seconds = max(1, int(review_segment_seconds))
        self.review_retention_seconds = max(
            self.review_segment_seconds * 3,
            int(review_retention_seconds),
        )
        self._popen_factory = popen_factory
        self._clock = clock or (lambda: datetime.now().astimezone())
        self.stop_timeout = max(0.1, float(stop_timeout))

        self._process: subprocess.Popen | None = None
        self._session_id = ""
        self._session_started_at_ms: int | None = None
        self._playlist_path: Path | None = None
        self._archive_pattern: Path | None = None
        self._stderr_lines: list[str] = []
        self._stderr_lock = threading.Lock()
        self._stderr_thread: threading.Thread | None = None

    @property
    def archive_dir(self) -> Path:
        return self.output_dir / "videos"

    @property
    def review_dir(self) -> Path:
        return self.output_dir / "review_buffer" / f"camera_{self.camera_index:02d}"

    @property
    def playlist_path(self) -> Path | None:
        return self._playlist_path

    @property
    def archive_pattern(self) -> Path | None:
        return self._archive_pattern

    @property
    def session_started_at_ms(self) -> int | None:
        return self._session_started_at_ms

    def archive_paths(self) -> tuple[Path, ...]:
        session = self.archive_session
        return session.archive_paths() if session is not None else ()

    @property
    def archive_session(self) -> ArchiveRecordingSession | None:
        if self._archive_pattern is None or self._session_started_at_ms is None:
            return None
        return ArchiveRecordingSession(
            camera_index=self.camera_index,
            session_id=self._session_id,
            session_started_at_ms=self._session_started_at_ms,
            archive_pattern=self._archive_pattern.resolve(),
        )

    @property
    def archive_session_path(self) -> Path | None:
        session = self.archive_session
        if session is None:
            return None
        return self.archive_dir / (
            f"camera_{self.camera_index:02d}_{session.session_id}_archive_session.json"
        )

    def _write_archive_session_manifest(self) -> None:
        session = self.archive_session
        manifest_path = self.archive_session_path
        if session is None or manifest_path is None:
            raise RecordingError("archive recording session is not initialized")
        payload = {
            "schema_version": ARCHIVE_SESSION_SCHEMA_VERSION,
            "camera_index": session.camera_index,
            "session_id": session.session_id,
            "session_started_at_ms": session.session_started_at_ms,
            "archive_pattern": session.archive_pattern.name,
        }
        temporary_path = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
        with temporary_path.open("wb") as output:
            output.write(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
            output.write(b"\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, manifest_path)

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def _build_command(self) -> list[str]:
        if self.ffmpeg_path is None:
            raise RecordingError("未找到 FFmpeg，无法开始录像")

        list_size = max(
            3,
            math.ceil(
                self.review_retention_seconds / self.review_segment_seconds
            )
            + 2,
        )
        archive_pattern = (
            self.archive_dir
            / f"camera_{self.camera_index:02d}_{self._session_id}_archive_%04d.mkv"
        )
        review_pattern = (
            self.review_dir
            / f"camera_{self.camera_index:02d}_{self._session_id}_review_%016d.ts"
        )
        playlist_path = (
            self.review_dir
            / f"camera_{self.camera_index:02d}_{self._session_id}.m3u8"
        )
        self._archive_pattern = archive_pattern
        self._playlist_path = playlist_path
        directshow_source = parse_directshow_source(self.source)
        if directshow_source is not None:
            input_args = [
                "-f",
                "dshow",
                "-rtbufsize",
                "512M",
                "-use_wallclock_as_timestamps",
                "1",
            ]
            if directshow_source.video_size is not None:
                input_args.extend(["-video_size", directshow_source.video_size])
            if directshow_source.framerate is not None:
                input_args.extend(
                    ["-framerate", f"{directshow_source.framerate:g}"]
                )
            input_args.extend(["-i", f"video={directshow_source.device_name}"])
            archive_relative = Path("videos") / archive_pattern.name
            review_relative = (
                Path("review_buffer")
                / f"camera_{self.camera_index:02d}"
                / review_pattern.name
            )
            playlist_relative = review_relative.with_name(playlist_path.name)
            tee_output = (
                "[onfail=abort:f=segment:"
                f"segment_time={self.archive_segment_seconds}:"
                "reset_timestamps=1:segment_format=matroska]"
                f"{archive_relative.as_posix()}|"
                "[onfail=abort:f=hls:"
                f"hls_time={self.review_segment_seconds}:"
                f"hls_list_size={list_size}:"
                "hls_flags=temp_file+program_date_time+independent_segments:"
                "hls_segment_type=mpegts:hls_start_number_source=epoch_us:"
                f"hls_segment_filename={review_relative.as_posix()}]"
                f"{playlist_relative.as_posix()}"
            )
            return [
                str(self.ffmpeg_path),
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostats",
                *input_args,
                "-map",
                "0:v:0",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-tune",
                "zerolatency",
                "-pix_fmt",
                "yuv420p",
                "-flags",
                "+global_header",
                "-force_key_frames",
                f"expr:gte(t,n_forced*{self.review_segment_seconds})",
                "-an",
                "-f",
                "tee",
                tee_output,
            ]

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
            "-c:v",
            "copy",
            "-an",
            "-f",
            "segment",
            "-segment_time",
            str(self.archive_segment_seconds),
            "-reset_timestamps",
            "1",
            "-segment_format",
            "matroska",
            str(archive_pattern),
            "-map",
            "0:v:0",
            "-c:v",
            "copy",
            "-an",
            "-f",
            "hls",
            "-hls_time",
            str(self.review_segment_seconds),
            "-hls_list_size",
            str(list_size),
            "-hls_flags",
            "temp_file+program_date_time+independent_segments",
            "-hls_segment_type",
            "mpegts",
            "-hls_start_number_source",
            "epoch_us",
            "-hls_segment_filename",
            str(review_pattern),
            str(playlist_path),
        ]

    def start(self) -> Path:
        if self.is_running:
            raise RecordingError("当前机位已经在录像")
        if not is_supported_review_source(self.source):
            raise RecordingError("录像设备未安装或配置无效")
        if self.ffmpeg_path is None:
            self.ffmpeg_path = find_ffmpeg_executable()
        if self.ffmpeg_path is None:
            raise RecordingError("未找到 FFmpeg，请重新打包或安装 FFmpeg")

        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self.review_dir.mkdir(parents=True, exist_ok=True)
        started_at = self._clock()
        if started_at.tzinfo is None or started_at.utcoffset() is None:
            started_at = started_at.astimezone()
        self._session_id = started_at.strftime("%Y%m%d_%H%M%S_%f")
        self._session_started_at_ms = round(started_at.timestamp() * 1000.0)
        command = self._build_command()
        kwargs = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.PIPE,
        }
        if parse_directshow_source(self.source) is not None:
            kwargs["cwd"] = str(self.output_dir.resolve())
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if creation_flags:
            kwargs["creationflags"] = creation_flags
        try:
            process = self._popen_factory(command, **kwargs)
        except OSError as exc:
            raise RecordingError(
                f"启动终点录像失败: {sanitize_recording_message(exc)}"
            ) from exc

        self._process = process
        with self._stderr_lock:
            self._stderr_lines.clear()
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr,
            args=(process,),
            name=f"ReviewRecorderStderr-{self.camera_index}",
            daemon=True,
        )
        self._stderr_thread.start()
        try:
            self._write_archive_session_manifest()
        except OSError as exc:
            try:
                self.stop()
            except RecordingError:
                pass
            raise RecordingError(f"failed to save archive session metadata: {exc}") from exc
        assert self._playlist_path is not None
        return self._playlist_path

    def _drain_stderr(self, process: subprocess.Popen) -> None:
        stream = process.stderr
        if stream is None:
            return
        while True:
            raw = stream.readline()
            if not raw:
                break
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            line = sanitize_recording_message(raw)
            if not line:
                continue
            with self._stderr_lock:
                self._stderr_lines.append(line)
                del self._stderr_lines[:-20]

    def check_error(self) -> str | None:
        process = self._process
        if process is None or process.poll() is None:
            return None
        with self._stderr_lock:
            detail = " | ".join(self._stderr_lines[-3:])
        message = f"终点录像异常退出，代码 {process.returncode}"
        return f"{message}: {detail}" if detail else message

    def stop(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        if process.poll() is None:
            try:
                if process.stdin is not None:
                    process.stdin.write(b"q\n")
                    process.stdin.flush()
                process.wait(timeout=self.stop_timeout)
            except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
                process.terminate()
                try:
                    process.wait(timeout=self.stop_timeout)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=self.stop_timeout)
        thread = self._stderr_thread
        if thread is not None:
            thread.join(timeout=1.0)
        with self._stderr_lock:
            detail = " | ".join(self._stderr_lines[-3:])
        try:
            if process.stdin is not None:
                process.stdin.close()
        except OSError:
            pass
        self._process = None
        self._stderr_thread = None
        if process.returncode not in (0, None):
            message = f"终点录像停止异常，代码 {process.returncode}"
            if detail:
                message = f"{message}: {detail}"
            raise RecordingError(message)


class ArchiveTimelinePublisher:
    """Index sealed long-form archive files as a late-passage fallback."""

    def __init__(
        self,
        recorder: FfmpegReviewRecorder | ArchiveRecordingSession,
        timeline_store: VideoTimelineStore,
        *,
        timing_error_ms: int = DEFAULT_ARCHIVE_TIMING_ERROR_MS,
        duration_probe: Callable[[Path], int | None] = probe_video_duration_ms,
    ):
        self.recorder = recorder
        self.timeline_store = timeline_store
        self.timing_error_ms = max(0, int(timing_error_ms))
        self.duration_probe = duration_probe
        self.source_id = f"camera_{recorder.camera_index:02d}_review"

    def publish_completed(
        self,
        *,
        race_id: str,
        recording: bool,
    ) -> tuple[RecordingSegment, ...]:
        session_started_at_ms = getattr(
            self.recorder,
            "session_started_at_ms",
            None,
        )
        if session_started_at_ms is None:
            return ()
        archive_paths_reader = getattr(self.recorder, "archive_paths", None)
        if not callable(archive_paths_reader):
            return ()
        archive_paths = tuple(archive_paths_reader())
        if recording and archive_paths:
            archive_paths = archive_paths[:-1]
        if not archive_paths:
            return ()

        existing_by_path = {
            self.timeline_store.resolve_video_path(segment).resolve(): segment
            for segment in self.timeline_store.segments()
        }
        cursor_ms = int(session_started_at_ms)
        published = []
        for archive_path in archive_paths:
            existing = existing_by_path.get(archive_path)
            if existing is not None:
                if (
                    existing.media_started_at_ms is not None
                    and existing.media_duration_ms is not None
                ):
                    cursor_ms = max(
                        cursor_ms,
                        existing.media_started_at_ms + existing.media_duration_ms,
                    )
                continue
            media_duration_ms = self.duration_probe(archive_path)
            if media_duration_ms is None or int(media_duration_ms) <= 0:
                break
            segment = self.timeline_store.add_completed_segment(
                source_id=self.source_id,
                camera_index=self.recorder.camera_index,
                video_path=archive_path,
                media_started_at_ms=cursor_ms,
                media_duration_ms=int(media_duration_ms),
                clock_source=DEFAULT_CLOCK_SOURCE,
                timing_error_ms=self.timing_error_ms,
                end_reason="continuous_archive_fallback",
                race_id=str(race_id),
            )
            published.append(segment)
            existing_by_path[archive_path] = segment
            cursor_ms += int(media_duration_ms)
        return tuple(published)


class ReviewRingBuffer:
    """Index completed HLS segments and retain those referenced by passages."""

    def __init__(
        self,
        playlist_path: Path,
        *,
        camera_index: int,
        retention_seconds: int = DEFAULT_REVIEW_RETENTION_SECONDS,
        pin_journal_path: Path | None = None,
    ):
        self.playlist_path = Path(playlist_path).resolve()
        self.buffer_dir = self.playlist_path.parent.resolve()
        self.camera_index = max(1, int(camera_index))
        self.source_id = f"camera_{self.camera_index:02d}_review"
        self.retention_ms = max(1, int(retention_seconds)) * 1000
        self.pin_journal_path = (
            Path(pin_journal_path)
            if pin_journal_path is not None
            else self.buffer_dir / "review_buffer_pins.jsonl"
        )
        self._segments: dict[str, ReviewSegment] = {}
        self._pins_by_event: dict[str, set[str]] = {}
        self._pins_by_segment: dict[str, set[str]] = {}
        self._load_pin_journal()

    @staticmethod
    def _datetime_ms(value: str) -> int:
        value = value.strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("HLS program date time must include a timezone")
        return round(parsed.timestamp() * 1000.0)

    def _segment_from_payload(self, payload: dict) -> ReviewSegment:
        return ReviewSegment(
            segment_id=str(payload["segment_id"]),
            source_id=str(payload["source_id"]),
            camera_index=int(payload["camera_index"]),
            video_path=str(payload["video_path"]),
            started_at_ms=int(payload["started_at_ms"]),
            duration_ms=int(payload["duration_ms"]),
        )

    def _load_pin_journal(self) -> None:
        path = self.pin_journal_path
        if not path.is_file():
            return
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for raw_line in lines:
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError:
                break
            event_id = str(record.get("event_id") or "")
            if not event_id:
                continue
            if record.get("op") == "release":
                self._release_in_memory(event_id)
                continue
            if record.get("op") != "pin":
                continue
            segment_ids = set()
            for payload in record.get("segments") or ():
                try:
                    segment = self._segment_from_payload(payload)
                except (KeyError, TypeError, ValueError):
                    continue
                self._segments[segment.segment_id] = segment
                segment_ids.add(segment.segment_id)
            self._set_event_pins(event_id, segment_ids)

    def _append_pin_record(self, record: dict) -> None:
        path = self.pin_journal_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab") as output:
            output.write(json.dumps(record, ensure_ascii=False).encode("utf-8"))
            output.write(b"\n")
            output.flush()
            os.fsync(output.fileno())

    def _set_event_pins(self, event_id: str, segment_ids: set[str]) -> None:
        previous = self._pins_by_event.get(event_id, set())
        for segment_id in previous - segment_ids:
            events = self._pins_by_segment.get(segment_id)
            if events is not None:
                events.discard(event_id)
                if not events:
                    self._pins_by_segment.pop(segment_id, None)
        self._pins_by_event[event_id] = set(segment_ids)
        for segment_id in segment_ids:
            self._pins_by_segment.setdefault(segment_id, set()).add(event_id)

    def _release_in_memory(self, event_id: str) -> None:
        previous = self._pins_by_event.pop(event_id, set())
        for segment_id in previous:
            events = self._pins_by_segment.get(segment_id)
            if events is not None:
                events.discard(event_id)
                if not events:
                    self._pins_by_segment.pop(segment_id, None)

    def scan(self) -> tuple[ReviewSegment, ...]:
        """Load completed segments currently published by the HLS playlist."""
        try:
            lines = self.playlist_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return ()
        discovered = []
        current_start_ms: int | None = None
        running_start_ms: int | None = None
        duration_ms: int | None = None
        for raw_line in lines:
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
                value = line.split(":", 1)[1]
                try:
                    current_start_ms = self._datetime_ms(value)
                except ValueError:
                    current_start_ms = None
                continue
            if line.startswith("#EXTINF:"):
                value = line.split(":", 1)[1].split(",", 1)[0]
                try:
                    duration_ms = max(1, round(float(value) * 1000.0))
                except ValueError:
                    duration_ms = None
                continue
            if line.startswith("#") or duration_ms is None:
                continue

            segment_path = (self.buffer_dir / line).resolve()
            try:
                segment_path.relative_to(self.buffer_dir)
            except ValueError:
                duration_ms = None
                current_start_ms = None
                continue
            started_at_ms = (
                current_start_ms
                if current_start_ms is not None
                else running_start_ms
            )
            if (
                started_at_ms is None
                or segment_path.suffix.lower() == ".tmp"
                or not segment_path.is_file()
                or segment_path.stat().st_size <= 0
            ):
                duration_ms = None
                current_start_ms = None
                continue
            relative_path = segment_path.relative_to(self.buffer_dir).as_posix()
            segment = ReviewSegment(
                segment_id=relative_path,
                source_id=self.source_id,
                camera_index=self.camera_index,
                video_path=relative_path,
                started_at_ms=int(started_at_ms),
                duration_ms=int(duration_ms),
            )
            if segment.segment_id not in self._segments:
                discovered.append(segment)
            self._segments[segment.segment_id] = segment
            running_start_ms = segment.ended_at_ms
            duration_ms = None
            current_start_ms = None
        return tuple(discovered)

    def segments(self) -> tuple[ReviewSegment, ...]:
        return tuple(
            sorted(
                self._segments.values(),
                key=lambda item: (item.started_at_ms, item.segment_id),
            )
        )

    def resolve_path(self, segment: ReviewSegment) -> Path:
        return (self.buffer_dir / segment.video_path).resolve()

    def pin_window(
        self,
        event_id: str,
        *,
        started_at_ms: int,
        ended_at_ms: int,
        scan: bool = True,
    ) -> tuple[ReviewSegment, ...]:
        """Protect completed segments overlapping one passage evidence window."""
        event_id = str(event_id).strip()
        if not event_id:
            raise ValueError("event_id is required")
        started_at_ms = int(started_at_ms)
        ended_at_ms = int(ended_at_ms)
        if ended_at_ms < started_at_ms:
            raise ValueError("ended_at_ms cannot precede started_at_ms")
        if scan:
            self.scan()
        matches = {
            segment.segment_id
            for segment in self._segments.values()
            if segment.started_at_ms <= ended_at_ms
            and segment.ended_at_ms >= started_at_ms
        }
        combined = self._pins_by_event.get(event_id, set()) | matches
        if combined != self._pins_by_event.get(event_id, set()):
            self._set_event_pins(event_id, combined)
            pinned_segments = [self._segments[item] for item in sorted(combined)]
            self._append_pin_record(
                {
                    "op": "pin",
                    "event_id": event_id,
                    "segments": [asdict(segment) for segment in pinned_segments],
                }
            )
        return tuple(
            sorted(
                (self._segments[item] for item in combined),
                key=lambda item: (item.started_at_ms, item.segment_id),
            )
        )

    def release(self, event_id: str) -> None:
        event_id = str(event_id).strip()
        if not event_id or event_id not in self._pins_by_event:
            return
        self._release_in_memory(event_id)
        self._append_pin_record({"op": "release", "event_id": event_id})

    def pinned_event_ids(self, segment_id: str) -> frozenset[str]:
        return frozenset(self._pins_by_segment.get(str(segment_id), set()))

    def cleanup(self, *, current_time_ms: int) -> tuple[Path, ...]:
        """Delete expired, completed segments that no passage references."""
        cutoff_ms = int(current_time_ms) - self.retention_ms
        deleted = []
        for segment in list(self._segments.values()):
            if (
                segment.ended_at_ms >= cutoff_ms
                or self._pins_by_segment.get(segment.segment_id)
            ):
                continue
            path = self.resolve_path(segment)
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue
            self._segments.pop(segment.segment_id, None)
            deleted.append(path)
        return tuple(deleted)


class PassageReviewCoordinator:
    """Pin and track passage windows while short recording segments close."""

    def __init__(
        self,
        ring_buffer: ReviewRingBuffer,
        *,
        pre_roll_seconds: int = DEFAULT_REVIEW_PRE_ROLL_SECONDS,
        post_roll_seconds: int = DEFAULT_REVIEW_POST_ROLL_SECONDS,
    ):
        self.ring_buffer = ring_buffer
        self.pre_roll_ms = max(0, int(pre_roll_seconds)) * 1000
        self.post_roll_ms = max(0, int(post_roll_seconds)) * 1000
        self._windows: dict[str, PassageReviewWindow] = {}

    def register(
        self,
        event_id: str,
        *,
        passage_timestamp_ms: int,
        scan: bool = True,
    ) -> PassageReviewWindow:
        event_id = str(event_id).strip()
        if not event_id:
            raise ValueError("event_id is required")
        passage_timestamp_ms = int(passage_timestamp_ms)
        if passage_timestamp_ms < 0:
            raise ValueError("passage_timestamp_ms must be non-negative")
        self._windows[event_id] = PassageReviewWindow(
            event_id=event_id,
            passage_timestamp_ms=passage_timestamp_ms,
            started_at_ms=max(0, passage_timestamp_ms - self.pre_roll_ms),
            ended_at_ms=passage_timestamp_ms + self.post_roll_ms,
            state=PassageReviewState.WAITING,
        )
        return self.refresh_event(event_id, scan=scan)

    @staticmethod
    def _covers_window(
        segments: tuple[ReviewSegment, ...],
        *,
        started_at_ms: int,
        ended_at_ms: int,
    ) -> bool:
        cursor = started_at_ms
        for segment in segments:
            if segment.ended_at_ms < cursor:
                continue
            if segment.started_at_ms > cursor:
                return False
            cursor = max(cursor, segment.ended_at_ms)
            if cursor >= ended_at_ms:
                return True
        return cursor >= ended_at_ms

    def refresh_event(
        self,
        event_id: str,
        *,
        scan: bool = True,
    ) -> PassageReviewWindow:
        event_id = str(event_id).strip()
        window = self._windows.get(event_id)
        if window is None:
            raise KeyError(event_id)
        pinned = self.ring_buffer.pin_window(
            event_id,
            started_at_ms=window.started_at_ms,
            ended_at_ms=window.ended_at_ms,
            scan=scan,
        )
        current_segments = tuple(
            segment
            for segment in pinned
            if segment.started_at_ms <= window.ended_at_ms
            and segment.ended_at_ms >= window.started_at_ms
        )
        timeline_complete = any(
            segment.ended_at_ms >= window.ended_at_ms
            for segment in self.ring_buffer.segments()
        )
        if not timeline_complete:
            state = PassageReviewState.WAITING
        elif self._covers_window(
            current_segments,
            started_at_ms=window.started_at_ms,
            ended_at_ms=window.ended_at_ms,
        ):
            state = PassageReviewState.READY
        else:
            state = PassageReviewState.PARTIAL
        refreshed = PassageReviewWindow(
            event_id=window.event_id,
            passage_timestamp_ms=window.passage_timestamp_ms,
            started_at_ms=window.started_at_ms,
            ended_at_ms=window.ended_at_ms,
            state=state,
            segments=current_segments,
        )
        self._windows[event_id] = refreshed
        return refreshed

    def refresh(self, *, scan: bool = True) -> tuple[PassageReviewWindow, ...]:
        pending_event_ids = [
            event_id
            for event_id, window in self._windows.items()
            if window.state is PassageReviewState.WAITING
        ]
        if pending_event_ids and scan:
            self.ring_buffer.scan()
        for event_id in pending_event_ids:
            self.refresh_event(event_id, scan=False)
        return tuple(self._windows.values())

    def get(self, event_id: str) -> PassageReviewWindow | None:
        return self._windows.get(str(event_id))

    def discard(self, event_id: str) -> None:
        event_id = str(event_id).strip()
        if not event_id:
            return
        self._windows.pop(event_id, None)
        self.ring_buffer.release(event_id)


class PassageReviewTimelinePublisher:
    """Publish one sealed passage window as a short playable HLS timeline."""

    def __init__(
        self,
        ring_buffer: ReviewRingBuffer,
        timeline_store: VideoTimelineStore,
        *,
        timing_error_ms: int = DEFAULT_TIMING_ERROR_MS,
    ):
        self.ring_buffer = ring_buffer
        self.timeline_store = timeline_store
        self.timing_error_ms = max(0, int(timing_error_ms))

    @staticmethod
    def _playlist_name(window: PassageReviewWindow) -> str:
        digest = hashlib.sha256(window.event_id.encode("utf-8")).hexdigest()[:16]
        return f"evidence_{digest}_{window.passage_timestamp_ms}.m3u8"

    @staticmethod
    def _program_date_time(timestamp_ms: int) -> str:
        value = datetime.fromtimestamp(
            int(timestamp_ms) / 1000.0,
            tz=BEIJING_TIMEZONE,
        )
        return value.isoformat(timespec="milliseconds")

    def _write_playlist(
        self,
        path: Path,
        segments: tuple[ReviewSegment, ...],
    ) -> None:
        target_duration = max(
            1,
            math.ceil(max(segment.duration_ms for segment in segments) / 1000.0),
        )
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:6",
            f"#EXT-X-TARGETDURATION:{target_duration}",
            "#EXT-X-MEDIA-SEQUENCE:0",
            "#EXT-X-INDEPENDENT-SEGMENTS",
        ]
        previous_end_ms: int | None = None
        for segment in segments:
            if (
                previous_end_ms is not None
                and segment.started_at_ms > previous_end_ms + 1
            ):
                lines.append("#EXT-X-DISCONTINUITY")
            lines.extend(
                [
                    "#EXT-X-PROGRAM-DATE-TIME:"
                    + self._program_date_time(segment.started_at_ms),
                    f"#EXTINF:{segment.duration_ms / 1000.0:.3f},",
                    segment.video_path,
                ]
            )
            previous_end_ms = segment.ended_at_ms
        lines.append("#EXT-X-ENDLIST")
        payload = ("\n".join(lines) + "\n").encode("utf-8")
        try:
            if path.read_bytes() == payload:
                return
        except FileNotFoundError:
            pass
        temporary_path = path.with_suffix(path.suffix + ".tmp")
        with temporary_path.open("wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, path)

    def publish(
        self,
        window: PassageReviewWindow,
        *,
        race_id: str,
    ) -> RecordingSegment:
        if window.state is not PassageReviewState.READY or not window.segments:
            raise ValueError("only a complete passage review window can be published")
        playlist_path = self.ring_buffer.buffer_dir / self._playlist_name(window)
        self._write_playlist(playlist_path, window.segments)
        resolved_playlist = playlist_path.resolve()
        for segment in self.timeline_store.segments():
            if self.timeline_store.resolve_video_path(segment) == resolved_playlist:
                return segment

        media_started_at_ms = window.segments[0].started_at_ms
        media_duration_ms = (
            window.segments[-1].ended_at_ms - media_started_at_ms
        )
        return self.timeline_store.add_completed_segment(
            source_id=self.ring_buffer.source_id,
            camera_index=self.ring_buffer.camera_index,
            video_path=playlist_path,
            media_started_at_ms=media_started_at_ms,
            media_duration_ms=media_duration_ms,
            clock_source=DEFAULT_CLOCK_SOURCE,
            timing_error_ms=self.timing_error_ms,
            end_reason="passage_review_window",
            race_id=str(race_id),
        )


__all__ = [
    "ArchiveTimelinePublisher",
    "ArchiveRecordingSession",
    "ARCHIVE_SESSION_SCHEMA_VERSION",
    "DEFAULT_ARCHIVE_SEGMENT_SECONDS",
    "DEFAULT_ARCHIVE_TIMING_ERROR_MS",
    "DEFAULT_REVIEW_POST_ROLL_SECONDS",
    "DEFAULT_REVIEW_PRE_ROLL_SECONDS",
    "DEFAULT_REVIEW_RETENTION_SECONDS",
    "DEFAULT_REVIEW_SEGMENT_SECONDS",
    "DirectShowReviewSource",
    "FfmpegReviewRecorder",
    "PassageReviewCoordinator",
    "PassageReviewState",
    "PassageReviewTimelinePublisher",
    "PassageReviewWindow",
    "ReviewRingBuffer",
    "ReviewSegment",
    "discover_directshow_video_devices",
    "is_supported_review_source",
    "load_archive_recording_sessions",
    "make_directshow_source",
    "parse_directshow_source",
]
