"""Read-only discovery and decoding for AYT/Auyat line-scan RGB files."""

from __future__ import annotations

import bisect
import hashlib
import json
import os
import re
import struct
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import numpy as np
from PyQt5.QtCore import QThread, pyqtSignal
from PyQt5.QtGui import QImage

from .video_timeline import PassageVideoLocation, RecordingSegment


AUYAT_CLOCK_SOURCE = "auyat_rgb_beijing"
AUYAT_SOURCE_ID = "auyat_high_speed"
BEIJING_TIMEZONE = timezone(timedelta(hours=8))
HEADER_SIZE = 48
RECORD_SIZE = 3080
RECORD_METADATA_SIZE = 8
PIXEL_BYTES = 3072
DEFAULT_HEIGHT = 1024
TICKS_PER_SECOND = 20_000
TICKS_PER_MILLISECOND = 20
TICKS_PER_DAY = 24 * 60 * 60 * TICKS_PER_SECOND
TICK_COUNTER_SIZE = 1 << 32
CAPTURE_START_FLAG = 0x01000000
CAPTURE_END_FLAG = 0x02000000
SNAPSHOT_SUFFIX = re.compile(r"_\d{4}-\d{2}-\d{2} \d{6}\.RGB$", re.IGNORECASE)
LIVE_HEADER_NAME = "BRSY_Head.RGB"
LIVE_DATA_NAME = "BRSY_Photo.RGB"


class AuyatRgbError(RuntimeError):
    """Raised when an AYT/Auyat RGB source is invalid or unavailable."""


class AuyatRgbBusyError(AuyatRgbError):
    """Raised while the vendor application still owns an RGB file."""


class AuyatScanCancelled(RuntimeError):
    """Raised internally when a directory or RGB scan is stopping."""


@dataclass(frozen=True, slots=True)
class AuyatRgbCapture:
    file_path: Path
    capture_date: date
    start_record: int
    end_record: int
    start_tick: int
    end_tick: int
    height: int = DEFAULT_HEIGHT
    channel_order: str = "rgb"
    data_offset: int = HEADER_SIZE

    def __post_init__(self) -> None:
        if self.start_record < 0 or self.end_record < self.start_record:
            raise AuyatRgbError("invalid RGB capture record range")
        if not 0 <= self.start_tick < TICK_COUNTER_SIZE:
            raise AuyatRgbError("invalid RGB capture tick range")
        if not 0 <= self.end_tick < TICK_COUNTER_SIZE:
            raise AuyatRgbError("invalid RGB capture tick range")
        if self.height <= 0 or self.height * 3 != PIXEL_BYTES:
            raise AuyatRgbError("unsupported RGB line height")
        if self.channel_order not in {"rgb", "bgr"}:
            raise AuyatRgbError("unsupported RGB channel order")
        if self.data_offset < 0:
            raise AuyatRgbError("invalid RGB data offset")

    @property
    def column_count(self) -> int:
        return self.end_record - self.start_record + 1

    @property
    def day_started_at_ms(self) -> int:
        midnight = datetime.combine(
            self.capture_date,
            datetime.min.time(),
            tzinfo=BEIJING_TIMEZONE,
        )
        return int(midnight.timestamp() * 1000.0)

    @property
    def media_started_at_ms(self) -> int:
        return self.day_started_at_ms + _round_tick_ms(self.start_tick % TICKS_PER_DAY)

    @property
    def media_ended_at_ms(self) -> int:
        return self.media_started_at_ms + _round_tick_ms(
            _tick_delta(self.end_tick, self.start_tick)
        )

    @property
    def media_duration_ms(self) -> int:
        return max(1, self.media_ended_at_ms - self.media_started_at_ms)

    @property
    def segment_id(self) -> str:
        identity = "|".join(
            (
                os.path.normcase(str(self.file_path.absolute())),
                self.capture_date.isoformat(),
                str(self.start_record),
                str(self.end_record),
                str(self.start_tick),
                str(self.end_tick),
                str(self.data_offset),
            )
        )
        return "auyat-" + hashlib.sha1(identity.encode("utf-8")).hexdigest()

    @property
    def media_locator(self) -> str:
        return ":".join(
            str(value)
            for value in (
                self.start_record,
                self.end_record,
                self.start_tick,
                self.end_tick,
                self.height,
                self.data_offset,
            )
        ) + f":{self.channel_order}"

    def distance_ms(self, timestamp_ms: int) -> int:
        timestamp_ms = int(timestamp_ms)
        if timestamp_ms < self.media_started_at_ms:
            return self.media_started_at_ms - timestamp_ms
        if timestamp_ms > self.media_ended_at_ms:
            return timestamp_ms - self.media_ended_at_ms
        return 0

    def to_location(
        self,
        timestamp_ms: int,
        *,
        race_id: str,
        clock_offset_ms: int = 0,
        pre_roll_ms: int = 3_000,
        status: str = "located",
    ) -> PassageVideoLocation:
        target_ms = int(timestamp_ms) + int(clock_offset_ms)
        position_ms = max(0, target_ms - self.media_started_at_ms)
        position_ms = min(position_ms, self.media_duration_ms)
        segment = RecordingSegment(
            segment_id=self.segment_id,
            source_id=AUYAT_SOURCE_ID,
            camera_index=2,
            video_path=str(self.file_path.absolute()),
            started_at_ms=self.media_started_at_ms,
            ended_at_ms=self.media_ended_at_ms,
            media_duration_ms=self.media_duration_ms,
            media_started_at_ms=self.media_started_at_ms,
            clock_source=AUYAT_CLOCK_SOURCE,
            timing_error_ms=1,
            end_reason="auyat_rgb_capture",
            race_id=str(race_id),
        )
        return PassageVideoLocation(
            segment=segment,
            video_path=self.file_path.absolute(),
            passage_position_ms=position_ms,
            playback_position_ms=max(0, position_ms - max(0, int(pre_roll_ms))),
            clock_offset_ms=int(clock_offset_ms),
            timing_error_ms=segment.timing_error_ms,
            status=str(status),
            media_locator=self.media_locator,
        )

    @classmethod
    def from_location(cls, location: PassageVideoLocation) -> "AuyatRgbCapture":
        values = str(location.media_locator).split(":")
        if len(values) not in {6, 7}:
            raise AuyatRgbError("invalid RGB media locator")
        try:
            start_record, end_record, start_tick, end_tick, height = (
                int(value) for value in values[:5]
            )
            data_offset = int(values[5]) if len(values) == 7 else HEADER_SIZE
        except ValueError as error:
            raise AuyatRgbError("invalid RGB media locator") from error
        capture_datetime = datetime.fromtimestamp(
            location.segment.media_started_at_ms / 1000.0,
            tz=BEIJING_TIMEZONE,
        )
        return cls(
            file_path=Path(location.video_path).absolute(),
            capture_date=capture_datetime.date(),
            start_record=start_record,
            end_record=end_record,
            start_tick=start_tick,
            end_tick=end_tick,
            height=height,
            channel_order=values[-1],
            data_offset=data_offset,
        )


@dataclass(frozen=True, slots=True)
class AuyatRgbFrame:
    capture: AuyatRgbCapture
    ticks: np.ndarray
    pixels_rgb: np.ndarray

    @property
    def width(self) -> int:
        return int(self.pixels_rgb.shape[1])

    @property
    def height(self) -> int:
        return int(self.pixels_rgb.shape[0])

    def column_for_position_ms(self, position_ms: int) -> int:
        target_tick = int(self.ticks[0]) + max(0, int(position_ms)) * TICKS_PER_MILLISECOND
        column = bisect.bisect_left(self.ticks, target_tick)
        if column <= 0:
            return 0
        if column >= len(self.ticks):
            return len(self.ticks) - 1
        before = int(self.ticks[column - 1])
        after = int(self.ticks[column])
        return column - 1 if target_tick - before <= after - target_tick else column

    def position_ms_for_column(self, column: int) -> int:
        column = max(0, min(int(column), len(self.ticks) - 1))
        return _round_tick_ms(int(self.ticks[column]) - int(self.ticks[0]))

    def column_for_x(self, x_normalized: float) -> int:
        return max(
            0,
            min(
                self.width - 1,
                int(round(max(0.0, min(1.0, float(x_normalized))) * (self.width - 1))),
            ),
        )

    def x_for_position_ms(self, position_ms: int) -> float:
        if self.width <= 1:
            return 0.0
        return self.column_for_position_ms(position_ms) / float(self.width - 1)


@dataclass(frozen=True, slots=True)
class AuyatScanResult:
    status: str
    captures: tuple[AuyatRgbCapture, ...]
    changed: bool
    message: str = ""
    waiting_file_count: int = 0


@dataclass(frozen=True, slots=True)
class _CachedRgbFile:
    signature: tuple[int, int, int, int]
    capture_date: Optional[date]
    captures: Optional[tuple[AuyatRgbCapture, ...]]
    channel_order: str
    error: str = ""
    scanned_records: int = 0
    open_capture: Optional[tuple[int, int]] = None


class AuyatRgbPlaybackWorker(QThread):
    """Expose one line-scan capture through the existing playback worker API."""

    metadata_ready = pyqtSignal(int, float, int, int, int)
    frame_ready = pyqtSignal(QImage, int, int)
    full_resolution_ready = pyqtSignal(QImage, int, int)
    playback_finished = pyqtSignal()
    playback_error = pyqtSignal(str)

    def __init__(self, location: PassageVideoLocation, parent=None):
        super().__init__(parent)
        self.location = location
        self.video_path = Path(location.video_path)
        self.media_locator = str(location.media_locator)
        self._capture = AuyatRgbCapture.from_location(location)
        self._condition = threading.Condition()
        self._stop_requested = False
        self._playing = False
        self._seek_position_ms: Optional[int] = None
        self._step_columns = 0
        self._full_resolution_requested = False
        self._current_position_ms = 0
        self._current_frame_index = -1
        self._frame: Optional[AuyatRgbFrame] = None
        self._image = QImage()

    @property
    def current_position_ms(self) -> int:
        with self._condition:
            return self._current_position_ms

    @property
    def current_frame_index(self) -> int:
        with self._condition:
            return self._current_frame_index

    def play(self) -> None:
        self.set_shuttle_speed(1.0)

    def pause(self) -> None:
        with self._condition:
            self._playing = False
            self._condition.notify_all()

    def set_shuttle_speed(self, speed: float) -> None:
        with self._condition:
            self._playing = float(speed) > 0.01
            self._condition.notify_all()

    def seek_frame(self, frame_index: int) -> None:
        frame = self._frame
        if frame is not None:
            self.seek(frame.position_ms_for_column(frame_index))

    def seek(self, milliseconds: int) -> None:
        with self._condition:
            self._seek_position_ms = max(0, int(milliseconds))
            self._condition.notify_all()

    def seek_and_play(self, milliseconds: int, speed: float = 1.0) -> None:
        with self._condition:
            self._seek_position_ms = max(0, int(milliseconds))
            self._playing = float(speed) > 0.01
            self._condition.notify_all()

    def jump(self, delta_ms: int) -> None:
        self.seek(self.current_position_ms + int(delta_ms))

    def step(self, frame_delta: int) -> None:
        with self._condition:
            self._playing = False
            self._step_columns += int(frame_delta)
            self._condition.notify_all()

    def request_full_resolution(self, _frame_index: Optional[int] = None) -> None:
        with self._condition:
            self._full_resolution_requested = True
            self._condition.notify_all()

    def position_ms_for_x(self, x_normalized: float) -> Optional[int]:
        frame = self._frame
        if frame is None:
            return None
        return frame.position_ms_for_column(frame.column_for_x(x_normalized))

    def x_for_position_ms(self, position_ms: int) -> Optional[float]:
        frame = self._frame
        if frame is None:
            return None
        return frame.x_for_position_ms(position_ms)

    def stop(self) -> None:
        with self._condition:
            self._stop_requested = True
            self._condition.notify_all()

    def run(self) -> None:
        try:
            frame = read_capture(self._capture)
            image = QImage(
                frame.pixels_rgb.data,
                frame.width,
                frame.height,
                frame.width * 3,
                QImage.Format_RGB888,
            ).copy()
            self._frame = frame
            self._image = image
            duration_ms = frame.position_ms_for_column(frame.width - 1)
            fps = (
                (frame.width - 1) * 1000.0 / duration_ms
                if duration_ms > 0 and frame.width > 1
                else 1000.0
            )
            self.metadata_ready.emit(
                max(1, duration_ms),
                max(1.0, fps),
                frame.width,
                frame.height,
                frame.width,
            )
            last_tick = time.monotonic()
            while True:
                with self._condition:
                    if self._stop_requested:
                        return
                    seek_position_ms = self._seek_position_ms
                    self._seek_position_ms = None
                    step_columns = self._step_columns
                    self._step_columns = 0
                    full_resolution = self._full_resolution_requested
                    self._full_resolution_requested = False
                    playing = self._playing

                if seek_position_ms is not None:
                    self._emit_position(frame, image, seek_position_ms)
                    last_tick = time.monotonic()
                    continue
                if step_columns:
                    column = max(
                        0,
                        min(frame.width - 1, self.current_frame_index + step_columns),
                    )
                    self._emit_column(frame, image, column)
                    continue
                if full_resolution and self.current_frame_index >= 0:
                    self.full_resolution_ready.emit(
                        image,
                        self.current_position_ms,
                        self.current_frame_index,
                    )
                    continue
                if playing:
                    now = time.monotonic()
                    elapsed_ms = max(1, int(round((now - last_tick) * 1000.0)))
                    last_tick = now
                    target_ms = self.current_position_ms + elapsed_ms
                    if target_ms >= duration_ms:
                        self._emit_column(frame, image, frame.width - 1)
                        with self._condition:
                            self._playing = False
                        self.playback_finished.emit()
                    else:
                        self._emit_position(frame, image, target_ms)
                    time.sleep(0.015)
                    continue
                with self._condition:
                    self._condition.wait(timeout=0.05)
        except (AuyatRgbError, OSError, ValueError) as error:
            self.playback_error.emit(str(error))
        except Exception as error:  # noqa: BLE001 - thread boundary reports failures.
            self.playback_error.emit(f"高速图像回放失败: {error}")
        finally:
            self._frame = None
            self._image = QImage()

    def _emit_position(
        self,
        frame: AuyatRgbFrame,
        image: QImage,
        position_ms: int,
    ) -> None:
        self._emit_column(frame, image, frame.column_for_position_ms(position_ms))

    def _emit_column(
        self,
        frame: AuyatRgbFrame,
        image: QImage,
        column: int,
    ) -> None:
        column = max(0, min(frame.width - 1, int(column)))
        position_ms = frame.position_ms_for_column(column)
        with self._condition:
            self._current_frame_index = column
            self._current_position_ms = position_ms
        self.frame_ready.emit(image, position_ms, column)


class AuyatRgbScanWorker(QThread):
    """Periodically refresh an RGB catalog without blocking the Qt event loop."""

    scan_finished = pyqtSignal(object)

    def __init__(
        self,
        catalog: "AuyatRgbCatalog",
        parent=None,
        *,
        interval_seconds: float = 2.0,
    ):
        super().__init__(parent)
        self.catalog = catalog
        self.interval_seconds = max(0.5, float(interval_seconds))
        self._condition = threading.Condition()
        self._stop_requested = False
        self._scan_requested = True

    def request_scan(self) -> None:
        with self._condition:
            self._scan_requested = True
            self._condition.notify_all()

    def stop(self) -> None:
        with self._condition:
            self._stop_requested = True
            self._condition.notify_all()

    def _should_stop(self) -> bool:
        with self._condition:
            return self._stop_requested

    def run(self) -> None:
        while True:
            with self._condition:
                if self._stop_requested:
                    return
                requested = self._scan_requested
                self._scan_requested = False
            if requested:
                try:
                    result = self.catalog.scan(cancel_requested=self._should_stop)
                except AuyatScanCancelled:
                    return
                self.scan_finished.emit(result)
            with self._condition:
                if self._stop_requested:
                    return
                if self._scan_requested:
                    continue
                self._condition.wait(timeout=self.interval_seconds)
                self._scan_requested = True


class AuyatRgbCatalog:
    """Thread-safe catalog of completed captures in a vendor Photo directory."""

    def __init__(
        self,
        root: str | Path | None,
        *,
        cache_path: str | Path | None = None,
        target_dates: Iterable[date] = (),
    ):
        self._lock = threading.RLock()
        self._root = _absolute_optional_path(root)
        self._cache_path = _absolute_optional_path(cache_path)
        self._target_dates = frozenset(target_dates)
        self._generation = 0
        self._captures: tuple[AuyatRgbCapture, ...] = ()
        self._file_cache: dict[Path, _CachedRgbFile] = {}
        self._status = "unavailable" if self._root is None else "checking"
        self._message = ""
        self._waiting_file_count = 0
        self._load_cache_locked()

    @property
    def root(self) -> Optional[Path]:
        with self._lock:
            return self._root

    @property
    def status(self) -> str:
        with self._lock:
            return self._status

    @property
    def message(self) -> str:
        with self._lock:
            return self._message

    def captures(self) -> tuple[AuyatRgbCapture, ...]:
        with self._lock:
            return self._captures

    @property
    def target_dates(self) -> frozenset[date]:
        with self._lock:
            return self._target_dates

    def snapshot(self) -> AuyatScanResult:
        with self._lock:
            return AuyatScanResult(
                status=self._status,
                captures=self._captures,
                changed=False,
                message=self._message,
                waiting_file_count=self._waiting_file_count,
            )

    def set_root(self, root: str | Path | None) -> None:
        with self._lock:
            resolved = _absolute_optional_path(root)
            if resolved == self._root:
                return
            self._root = resolved
            self._generation += 1
            self._captures = ()
            self._file_cache.clear()
            self._status = "unavailable" if resolved is None else "checking"
            self._message = ""
            self._waiting_file_count = 0
            self._load_cache_locked()

    def set_cache_path(self, cache_path: str | Path | None) -> None:
        with self._lock:
            resolved = _absolute_optional_path(cache_path)
            if resolved == self._cache_path:
                return
            self._cache_path = resolved
            self._generation += 1
            self._captures = ()
            self._file_cache.clear()
            self._status = "unavailable" if self._root is None else "checking"
            self._message = ""
            self._waiting_file_count = 0
            self._load_cache_locked()

    def set_target_dates(self, target_dates: Iterable[date]) -> bool:
        resolved = frozenset(target_dates)
        with self._lock:
            if resolved == self._target_dates:
                return False
            self._target_dates = resolved
            self._generation += 1
            self._captures = ()
            self._status = "unavailable" if self._root is None else "checking"
            self._message = ""
            self._waiting_file_count = 0
            return True

    def scan(
        self,
        *,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> AuyatScanResult:
        with self._lock:
            root = self._root
            generation = self._generation
            target_dates = self._target_dates
            previous_ids = tuple(capture.segment_id for capture in self._captures)
        _raise_if_cancelled(cancel_requested)
        if root is None:
            return self._publish(
                generation,
                "unavailable",
                (),
                previous_ids,
                "未配置高速数据目录",
            )

        photo_dir = resolve_photo_dir(root)
        _raise_if_cancelled(cancel_requested)
        if not photo_dir.is_dir():
            return self._publish(
                generation,
                "unavailable",
                (),
                previous_ids,
                "高速数据目录不可用",
            )

        try:
            sources = {
                path.absolute(): (path.absolute(), HEADER_SIZE)
                for path in photo_dir.glob("*.RGB")
                if path.name.casefold()
                not in {LIVE_HEADER_NAME.casefold(), LIVE_DATA_NAME.casefold()}
            }
            live_roots = (root, photo_dir.parent)
            for live_root in live_roots:
                live_header = (live_root / LIVE_HEADER_NAME).absolute()
                live_data = (live_root / LIVE_DATA_NAME).absolute()
                if live_header.is_file() and live_data.is_file():
                    sources[live_data] = (live_header, 0)
                    break
            paths = tuple(sorted(sources, key=lambda item: str(item).casefold()))
        except OSError as error:
            return self._publish(
                generation,
                "unavailable",
                (),
                previous_ids,
                str(error),
            )

        channel_order = read_channel_order(root)
        captures: list[AuyatRgbCapture] = []
        errors: list[str] = []
        waiting_file_count = 0
        cache_changed = False
        current_paths = set(paths)
        with self._lock:
            removed_paths = set(self._file_cache) - current_paths
            cache_changed = bool(removed_paths)
            self._file_cache = {
                path: cached
                for path, cached in self._file_cache.items()
                if path in current_paths
            }
        for path in paths:
            _raise_if_cancelled(cancel_requested)
            if not self._is_current_generation(generation):
                return self.snapshot()
            try:
                header_path, data_offset = sources[path]
                stat = path.stat()
                if data_offset:
                    header_signature = (0, 0)
                else:
                    header_stat = header_path.stat()
                    header_signature = (
                        int(header_stat.st_size),
                        int(header_stat.st_mtime_ns),
                    )
                signature = (
                    int(stat.st_size),
                    int(stat.st_mtime_ns),
                    *header_signature,
                )
                with self._lock:
                    cached = self._file_cache.get(path)
                matching_cache = bool(
                    cached is not None
                    and cached.signature == signature
                    and cached.channel_order == channel_order
                )
                if matching_cache and cached is not None and cached.error:
                    errors.append(cached.error)
                    continue

                if matching_cache and cached is not None:
                    capture_date = cached.capture_date
                else:
                    try:
                        capture_date, _height = read_rgb_header(header_path)
                    except AuyatRgbBusyError:
                        waiting_file_count += 1
                        continue
                    except AuyatRgbError as error:
                        cached = _CachedRgbFile(
                            signature=signature,
                            capture_date=None,
                            captures=(),
                            channel_order=channel_order,
                            error=str(error),
                        )
                        with self._lock:
                            if generation != self._generation:
                                return self.snapshot()
                            self._file_cache[path] = cached
                        cache_changed = True
                        errors.append(str(error))
                        continue

                if target_dates and capture_date not in target_dates:
                    if not matching_cache:
                        with self._lock:
                            if generation != self._generation:
                                return self.snapshot()
                            self._file_cache[path] = _CachedRgbFile(
                                signature=signature,
                                capture_date=capture_date,
                                captures=None,
                                channel_order=channel_order,
                            )
                        cache_changed = True
                    continue

                if matching_cache and cached is not None and cached.captures is not None:
                    parsed = cached.captures
                    scanned_records = cached.scanned_records
                    open_capture = cached.open_capture
                else:
                    can_resume = bool(
                        cached is not None
                        and cached.capture_date == capture_date
                        and cached.channel_order == channel_order
                        and cached.captures is not None
                        and cached.scanned_records > 0
                        and signature[0] > cached.signature[0]
                    )
                    parsed, scanned_records, open_capture = _scan_rgb_file(
                        path,
                        header_path=header_path,
                        data_offset=data_offset,
                        channel_order=channel_order,
                        cancel_requested=cancel_requested,
                        start_record=(cached.scanned_records if can_resume else 0),
                        open_capture=(cached.open_capture if can_resume else None),
                        existing_captures=(cached.captures if can_resume else ()),
                    )
                    with self._lock:
                        if generation != self._generation:
                            return self.snapshot()
                        self._file_cache[path] = _CachedRgbFile(
                            signature=signature,
                            capture_date=capture_date,
                            captures=parsed,
                            channel_order=channel_order,
                            scanned_records=scanned_records,
                            open_capture=open_capture,
                        )
                    cache_changed = True
                payload_bytes = max(0, signature[0] - data_offset)
                if open_capture is not None or payload_bytes % RECORD_SIZE:
                    waiting_file_count += 1
                captures.extend(parsed)
            except AuyatRgbBusyError:
                waiting_file_count += 1
            except (AuyatRgbError, OSError) as error:
                errors.append(str(error))

        _raise_if_cancelled(cancel_requested)
        unique_captures: dict[
            tuple[date, int, int, int, int, str],
            AuyatRgbCapture,
        ] = {}
        for capture in captures:
            key = (
                capture.capture_date,
                capture.start_tick,
                capture.end_tick,
                capture.column_count,
                capture.height,
                capture.channel_order,
            )
            existing = unique_captures.get(key)
            if existing is None or (
                SNAPSHOT_SUFFIX.search(capture.file_path.name)
                and not SNAPSHOT_SUFFIX.search(existing.file_path.name)
            ):
                unique_captures[key] = capture
        captures = list(unique_captures.values())
        captures.sort(
            key=lambda item: (
                item.media_started_at_ms,
                item.file_path.name,
                item.start_record,
            )
        )
        status = "ready" if captures else "waiting"
        if waiting_file_count:
            message = (
                "目录可访问，等待高速摄像软件完成判读并释放 "
                f"{waiting_file_count} 个文件"
            )
            if errors:
                message += f"；另跳过 {len(errors)} 个不可读文件"
        elif errors:
            message = f"已跳过 {len(errors)} 个不可读文件；{errors[0]}"
        else:
            message = "" if captures else "等待高速摄像软件完成判读并保存高速图像"
        if cache_changed:
            self._save_cache(generation)
        return self._publish(
            generation,
            status,
            tuple(captures),
            previous_ids,
            message,
            waiting_file_count=waiting_file_count,
        )

    def locate(
        self,
        timestamp_ms: int,
        *,
        race_id: str,
        clock_offset_ms: int = 0,
        pre_roll_ms: int = 3_000,
        tolerance_ms: int = 2_000,
    ) -> Optional[PassageVideoLocation]:
        target_ms = int(timestamp_ms) + int(clock_offset_ms)
        captures = self.captures()
        if not captures:
            return None
        capture = min(captures, key=lambda item: item.distance_ms(target_ms))
        distance_ms = capture.distance_ms(target_ms)
        if distance_ms > max(0, int(tolerance_ms)):
            return None
        return capture.to_location(
            timestamp_ms,
            race_id=race_id,
            clock_offset_ms=clock_offset_ms,
            pre_roll_ms=pre_roll_ms,
            status="located" if distance_ms == 0 else "near_boundary",
        )

    def _publish(
        self,
        generation: int,
        status: str,
        captures: tuple[AuyatRgbCapture, ...],
        previous_ids: tuple[str, ...],
        message: str,
        *,
        waiting_file_count: int = 0,
    ) -> AuyatScanResult:
        current_ids = tuple(capture.segment_id for capture in captures)
        with self._lock:
            if generation != self._generation:
                return AuyatScanResult(
                    status=self._status,
                    captures=self._captures,
                    changed=False,
                    message=self._message,
                    waiting_file_count=self._waiting_file_count,
                )
            self._captures = captures
            self._status = str(status)
            self._message = str(message)
            self._waiting_file_count = max(0, int(waiting_file_count))
        return AuyatScanResult(
            status=str(status),
            captures=captures,
            changed=current_ids != previous_ids,
            message=str(message),
            waiting_file_count=max(0, int(waiting_file_count)),
        )

    def _is_current_generation(self, generation: int) -> bool:
        with self._lock:
            return generation == self._generation

    def _load_cache_locked(self) -> None:
        cache_path = self._cache_path
        root = self._root
        if cache_path is None or root is None:
            return
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError):
            return
        schema_version = int(payload.get("schema_version", 0))
        if schema_version not in {1, 2}:
            return
        if _path_identity(payload.get("root")) != _path_identity(root):
            return
        entries = payload.get("files")
        if not isinstance(entries, list):
            return
        loaded: dict[Path, _CachedRgbFile] = {}
        for item in entries:
            try:
                path = Path(str(item["path"])).absolute()
                signature = (
                    int(item["size"]),
                    int(item["mtime_ns"]),
                    int(item.get("header_size", 0)),
                    int(item.get("header_mtime_ns", 0)),
                )
                date_value = str(item.get("capture_date") or "")
                capture_date = date.fromisoformat(date_value) if date_value else None
                channel_order = str(item.get("channel_order") or "rgb")
                indexed = bool(item.get("indexed", False))
                cached_captures = (
                    tuple(
                        _capture_from_cache(path, capture_date, channel_order, value)
                        for value in item.get("captures", ())
                    )
                    if indexed and capture_date is not None
                    else None
                )
                open_values = item.get("open_capture")
                open_capture = (
                    (int(open_values[0]), int(open_values[1]))
                    if isinstance(open_values, list) and len(open_values) == 2
                    else None
                )
                loaded[path] = _CachedRgbFile(
                    signature=signature,
                    capture_date=capture_date,
                    captures=cached_captures,
                    channel_order=channel_order,
                    error=str(item.get("error") or ""),
                    scanned_records=max(0, int(item.get("scanned_records", 0))),
                    open_capture=open_capture,
                )
            except (KeyError, TypeError, ValueError, AuyatRgbError):
                continue
        self._file_cache = loaded

    def _save_cache(self, generation: int) -> None:
        with self._lock:
            if generation != self._generation:
                return
            cache_path = self._cache_path
            root = self._root
            entries = tuple(self._file_cache.items())
        if cache_path is None or root is None:
            return
        payload = {
            "schema_version": 2,
            "root": str(root),
            "files": [
                {
                    "path": str(path),
                    "size": cached.signature[0],
                    "mtime_ns": cached.signature[1],
                    "header_size": cached.signature[2],
                    "header_mtime_ns": cached.signature[3],
                    "capture_date": (
                        cached.capture_date.isoformat()
                        if cached.capture_date is not None
                        else ""
                    ),
                    "channel_order": cached.channel_order,
                    "indexed": cached.captures is not None,
                    "captures": [
                        _capture_to_cache(capture)
                        for capture in (cached.captures or ())
                    ],
                    "error": cached.error,
                    "scanned_records": cached.scanned_records,
                    "open_capture": (
                        list(cached.open_capture)
                        if cached.open_capture is not None
                        else None
                    ),
                }
                for path, cached in entries
            ],
        }
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path = cache_path.with_suffix(cache_path.suffix + ".tmp")
            temporary_path.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(temporary_path, cache_path)
        except OSError:
            return


def discover_auyat_root(home: str | Path | None = None) -> Optional[Path]:
    home_path = Path(home).expanduser() if home is not None else Path.home()
    desktop = home_path / "Desktop"
    preferred = desktop / "电动计时软件20230309"
    candidates = (preferred,) + tuple(
        path
        for path in desktop.glob("*")
        if path != preferred and path.is_dir()
    )
    for candidate in candidates:
        if (candidate / "Photo").is_dir() and (
            (candidate / "PhotoTime.ini").is_file()
            or (candidate / "BRSY_Head.RGB").is_file()
        ):
            return candidate.resolve()
    return None


def is_network_share(root: str | Path | None) -> bool:
    if root is None:
        return False
    value = os.fspath(root).strip().replace("/", "\\")
    return value.startswith("\\\\")


def resolve_photo_dir(root: str | Path) -> Path:
    path = Path(root).expanduser().absolute()
    photo_dir = path / "Photo"
    return photo_dir if photo_dir.is_dir() else path


def read_channel_order(root: str | Path) -> str:
    path = Path(root).expanduser().absolute()
    for ini_path in (path / "PhotoTime.ini", path.parent / "PhotoTime.ini"):
        try:
            lines = ini_path.read_text(encoding="mbcs", errors="ignore").splitlines()
        except OSError:
            continue
        for raw_line in lines:
            name, separator, value = raw_line.partition("=")
            if separator and name.strip().lower() == "colorbitexchange":
                return "bgr" if value.strip() == "1" else "rgb"
    return "rgb"


def scan_rgb_file(
    path: str | Path,
    *,
    channel_order: str = "rgb",
    cancel_requested: Callable[[], bool] | None = None,
) -> tuple[AuyatRgbCapture, ...]:
    captures, _scanned_records, _open_capture = _scan_rgb_file(
        path,
        channel_order=channel_order,
        cancel_requested=cancel_requested,
    )
    return captures


def _scan_rgb_file(
    path: str | Path,
    *,
    header_path: str | Path | None = None,
    data_offset: int = HEADER_SIZE,
    channel_order: str = "rgb",
    cancel_requested: Callable[[], bool] | None = None,
    start_record: int = 0,
    open_capture: Optional[tuple[int, int]] = None,
    existing_captures: tuple[AuyatRgbCapture, ...] = (),
) -> tuple[
    tuple[AuyatRgbCapture, ...],
    int,
    Optional[tuple[int, int]],
]:
    file_path = Path(path).expanduser().absolute()
    resolved_header_path = (
        Path(header_path).expanduser().absolute()
        if header_path is not None
        else file_path
    )
    data_offset = max(0, int(data_offset))
    try:
        with resolved_header_path.open("rb") as header_source:
            header = header_source.read(HEADER_SIZE)
        if len(header) != HEADER_SIZE:
            raise AuyatRgbError(f"RGB文件头不完整: {resolved_header_path}")
        capture_date, height = _parse_header(header, resolved_header_path)
        with file_path.open("rb") as source:
            complete_records = max(
                0,
                (file_path.stat().st_size - data_offset) // RECORD_SIZE,
            )
            record_index = max(0, int(start_record))
            if record_index > complete_records:
                record_index = 0
                open_capture = None
                existing_captures = ()
            captures = list(existing_captures)
            source.seek(data_offset + record_index * RECORD_SIZE)
            records_per_chunk = 64
            while record_index < complete_records:
                _raise_if_cancelled(cancel_requested)
                count = min(records_per_chunk, complete_records - record_index)
                chunk = source.read(count * RECORD_SIZE)
                complete_chunk_records = len(chunk) // RECORD_SIZE
                if complete_chunk_records <= 0:
                    break
                for local_index in range(complete_chunk_records):
                    offset = local_index * RECORD_SIZE
                    tick, flag = struct.unpack_from("<II", chunk, offset)
                    current_index = record_index + local_index
                    if flag & CAPTURE_START_FLAG:
                        open_capture = (current_index, tick)
                    if flag & CAPTURE_END_FLAG and open_capture is not None:
                        start_record, start_tick = open_capture
                        captures.append(
                            AuyatRgbCapture(
                                file_path=file_path,
                                capture_date=capture_date,
                                start_record=start_record,
                                end_record=current_index,
                                start_tick=start_tick,
                                end_tick=tick,
                                height=height,
                                channel_order=channel_order,
                                data_offset=data_offset,
                            )
                        )
                        open_capture = None
                record_index += complete_chunk_records
                if complete_chunk_records < count:
                    break
    except PermissionError as error:
        raise AuyatRgbBusyError(
            f"高速图像仍被原厂软件占用，请先完成判读: {file_path}"
        ) from error
    except OSError as error:
        raise AuyatRgbError(f"无法读取高速图像: {file_path}") from error
    return tuple(captures), record_index, open_capture


def read_rgb_header(path: str | Path) -> tuple[date, int]:
    file_path = Path(path).expanduser().absolute()
    try:
        with file_path.open("rb") as source:
            header = source.read(HEADER_SIZE)
    except PermissionError as error:
        raise AuyatRgbBusyError(
            f"高速图像仍被原厂软件占用，请先完成判读: {file_path}"
        ) from error
    except OSError as error:
        raise AuyatRgbError(f"无法读取高速图像: {file_path}") from error
    if len(header) != HEADER_SIZE:
        raise AuyatRgbError(f"RGB文件头不完整: {file_path}")
    return _parse_header(header, file_path)


def read_capture(capture: AuyatRgbCapture) -> AuyatRgbFrame:
    count = capture.column_count
    start_offset = capture.data_offset + capture.start_record * RECORD_SIZE
    byte_count = count * RECORD_SIZE
    try:
        with capture.file_path.open("rb") as source:
            source.seek(start_offset)
            raw = source.read(byte_count)
    except PermissionError as error:
        raise AuyatRgbBusyError(
            f"高速图像仍被原厂软件占用，请先完成判读: {capture.file_path}"
        ) from error
    except OSError as error:
        raise AuyatRgbError(f"无法读取高速图像: {capture.file_path}") from error
    if len(raw) != byte_count:
        raise AuyatRgbError(f"高速图像采集段尚未完整写入: {capture.file_path}")

    raw_ticks = np.ndarray(
        shape=(count,),
        dtype="<u4",
        buffer=raw,
        offset=0,
        strides=(RECORD_SIZE,),
    ).copy()
    ticks = _unwrap_ticks(raw_ticks)
    pixels = np.ndarray(
        shape=(count, capture.height, 3),
        dtype=np.uint8,
        buffer=raw,
        offset=RECORD_METADATA_SIZE,
        strides=(RECORD_SIZE, 3, 1),
    ).transpose(1, 0, 2).copy()
    if capture.channel_order == "bgr":
        pixels = pixels[:, :, ::-1].copy()
    return AuyatRgbFrame(capture=capture, ticks=ticks, pixels_rgb=pixels)


def _parse_header(header: bytes, file_path: Path) -> tuple[date, int]:
    year = struct.unpack_from("<H", header, 36)[0]
    month = int(header[38])
    day = int(header[39])
    try:
        capture_date = date(year, month, day)
    except ValueError as error:
        raise AuyatRgbError(f"RGB文件日期无效: {file_path}") from error
    height = struct.unpack_from("<H", header, 24)[0]
    if height <= 0:
        height = DEFAULT_HEIGHT
    if height * 3 != PIXEL_BYTES:
        raise AuyatRgbError(f"不支持的RGB图像高度 {height}: {file_path}")
    return capture_date, height


def _round_tick_ms(ticks: int) -> int:
    ticks = int(ticks)
    return (ticks + TICKS_PER_MILLISECOND // 2) // TICKS_PER_MILLISECOND


def _tick_delta(end_tick: int, start_tick: int) -> int:
    return (int(end_tick) - int(start_tick)) % TICK_COUNTER_SIZE


def _unwrap_ticks(raw_ticks: np.ndarray) -> np.ndarray:
    if not len(raw_ticks):
        return np.empty((0,), dtype=np.uint64)
    values = raw_ticks.astype(np.uint64)
    wraps = np.zeros(values.shape, dtype=np.uint64)
    if len(values) > 1:
        wraps[1:] = np.cumsum(raw_ticks[1:] < raw_ticks[:-1], dtype=np.uint64)
    return values + wraps * np.uint64(TICK_COUNTER_SIZE)


def _absolute_optional_path(value: str | Path | None) -> Optional[Path]:
    if value is None or not str(value).strip():
        return None
    return Path(value).expanduser().absolute()


def _raise_if_cancelled(
    cancel_requested: Callable[[], bool] | None,
) -> None:
    if cancel_requested is not None and cancel_requested():
        raise AuyatScanCancelled()


def _path_identity(value: object) -> str:
    if value is None or not str(value).strip():
        return ""
    return os.path.normcase(str(Path(str(value)).absolute()))


def _capture_to_cache(capture: AuyatRgbCapture) -> dict[str, object]:
    return {
        "start_record": capture.start_record,
        "end_record": capture.end_record,
        "start_tick": capture.start_tick,
        "end_tick": capture.end_tick,
        "height": capture.height,
        "data_offset": capture.data_offset,
    }


def _capture_from_cache(
    path: Path,
    capture_date: date,
    channel_order: str,
    value: object,
) -> AuyatRgbCapture:
    if not isinstance(value, dict):
        raise ValueError("invalid RGB cache entry")
    return AuyatRgbCapture(
        file_path=path,
        capture_date=capture_date,
        start_record=int(value["start_record"]),
        end_record=int(value["end_record"]),
        start_tick=int(value["start_tick"]),
        end_tick=int(value["end_tick"]),
        height=int(value.get("height", DEFAULT_HEIGHT)),
        channel_order=channel_order,
        data_offset=int(value.get("data_offset", HEADER_SIZE)),
    )


__all__ = [
    "AUYAT_CLOCK_SOURCE",
    "AuyatRgbCapture",
    "AuyatRgbCatalog",
    "AuyatRgbError",
    "AuyatRgbFrame",
    "AuyatRgbPlaybackWorker",
    "AuyatScanCancelled",
    "AuyatRgbScanWorker",
    "AuyatScanResult",
    "discover_auyat_root",
    "read_capture",
    "read_channel_order",
    "read_rgb_header",
    "is_network_share",
    "resolve_photo_dir",
    "scan_rgb_file",
]
