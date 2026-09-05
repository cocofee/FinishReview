"""Coalescing background file-system refresh for live review capture."""

from __future__ import annotations

from dataclasses import dataclass, replace
import logging
from pathlib import Path
import threading
import time
from typing import Callable

from .runtime_metrics import RuntimeMetrics


logger = logging.getLogger("FinishReview.CaptureRefresh")


@dataclass(frozen=True, slots=True)
class ArchiveRefreshJob:
    publisher: object
    race_id: str
    recording: bool


@dataclass(frozen=True, slots=True)
class CaptureRefreshRequest:
    generation: int
    ring_buffers: tuple[object, ...]
    archive_jobs: tuple[ArchiveRefreshJob, ...]
    cleanup: bool
    current_time_ms: int
    scan: bool = True
    apply_state: bool = True
    cleanup_after_apply: bool = False


@dataclass(frozen=True, slots=True)
class CaptureRefreshResult:
    generation: int
    archive_segments: tuple[object, ...] = ()
    deleted_paths: tuple[Path, ...] = ()
    discovered_segment_count: int = 0
    error: str = ""
    apply_state: bool = True
    cleanup_after_apply: bool = False


class CaptureRefreshWorker:
    """Run only the latest pending refresh on one daemon thread."""

    def __init__(
        self,
        result_callback: Callable[[CaptureRefreshResult], None],
        *,
        metrics: RuntimeMetrics | None = None,
    ):
        self.result_callback = result_callback
        self.metrics = metrics
        self._condition = threading.Condition()
        self._pending: CaptureRefreshRequest | None = None
        self._thread: threading.Thread | None = None
        self._stop_requested = False
        self._minimum_generation = 0
        self._active_generation: int | None = None

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> None:
        with self._condition:
            if self.is_running:
                return
            self._stop_requested = False
            thread = threading.Thread(
                target=self._run,
                name="capture-refresh",
                daemon=True,
            )
            self._thread = thread
            thread.start()

    def submit(self, request: CaptureRefreshRequest) -> None:
        if not isinstance(request, CaptureRefreshRequest):
            raise TypeError("request must be CaptureRefreshRequest")
        with self._condition:
            if (
                self._stop_requested
                or request.generation < self._minimum_generation
            ):
                return
            pending = self._pending
            if pending is not None:
                cleanup_needed = bool(
                    request.cleanup
                    or request.cleanup_after_apply
                    or pending.cleanup
                    or pending.cleanup_after_apply
                )
                request = replace(
                    request,
                    cleanup=(cleanup_needed and not request.apply_state),
                    cleanup_after_apply=(cleanup_needed and request.apply_state),
                    current_time_ms=max(
                        int(request.current_time_ms),
                        int(pending.current_time_ms),
                    ),
                )
            self._pending = request
            self._condition.notify_all()

    def cancel_pending(self) -> None:
        with self._condition:
            self._pending = None

    def invalidate(self, minimum_generation: int) -> None:
        with self._condition:
            self._minimum_generation = max(
                self._minimum_generation,
                int(minimum_generation),
            )
            if (
                self._pending is not None
                and self._pending.generation < self._minimum_generation
            ):
                self._pending = None
            self._condition.notify_all()

    def invalidate_and_wait(
        self,
        minimum_generation: int,
        timeout: float = 2.0,
    ) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._condition:
            self._minimum_generation = max(
                self._minimum_generation,
                int(minimum_generation),
            )
            self._pending = None
            while self._active_generation is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(timeout=remaining)
            return True

    def _is_cancelled(self, request: CaptureRefreshRequest) -> bool:
        with self._condition:
            return (
                self._stop_requested
                or request.generation < self._minimum_generation
            )

    def stop(self, timeout: float = 2.0) -> bool:
        with self._condition:
            self._stop_requested = True
            self._pending = None
            self._condition.notify_all()
            thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0.0, float(timeout)))
        stopped = thread is None or not thread.is_alive()
        if stopped:
            with self._condition:
                if self._thread is thread:
                    self._thread = None
        return stopped

    def _observe(
        self,
        operation: str,
        started_at: float,
        *,
        item_count: int = 0,
        failed: bool = False,
    ) -> None:
        if self.metrics is None:
            return
        self.metrics.observe(
            operation,
            (time.perf_counter() - started_at) * 1000.0,
            item_count=item_count,
            failed=failed,
        )

    def _run(self) -> None:
        while True:
            with self._condition:
                while self._pending is None and not self._stop_requested:
                    self._condition.wait()
                if self._stop_requested:
                    return
                request = self._pending
                self._pending = None
                self._active_generation = (
                    None if request is None else request.generation
                )
            if request is None:
                with self._condition:
                    self._active_generation = None
                    self._condition.notify_all()
                continue
            try:
                result = self._process(request)
                with self._condition:
                    cancelled = (
                        self._stop_requested
                        or request.generation < self._minimum_generation
                    )
                if not cancelled:
                    try:
                        self.result_callback(result)
                    except Exception:  # noqa: BLE001 - callback failure must not stop refresh.
                        logger.exception("Capture refresh result callback failed")
            finally:
                with self._condition:
                    self._active_generation = None
                    self._condition.notify_all()

    def _process(self, request: CaptureRefreshRequest) -> CaptureRefreshResult:
        refresh_started = time.perf_counter()
        discovered_count = 0
        archive_segments: list[object] = []
        deleted_paths: list[Path] = []
        errors: list[str] = []
        for ring_buffer in request.ring_buffers if request.scan else ():
            if self._is_cancelled(request):
                break
            started = time.perf_counter()
            try:
                discovered = tuple(ring_buffer.scan())
            except Exception as error:  # noqa: BLE001 - isolate each camera.
                errors.append(f"ring scan: {error}")
                self._observe("background_ring_scan", started, failed=True)
                logger.exception("Background ring-buffer scan failed")
                continue
            discovered_count += len(discovered)
            self._observe(
                "background_ring_scan",
                started,
                item_count=len(discovered),
            )
        for job in request.archive_jobs:
            if self._is_cancelled(request):
                break
            started = time.perf_counter()
            try:
                published = tuple(
                    job.publisher.publish_completed(
                        race_id=job.race_id,
                        recording=job.recording,
                    )
                )
            except Exception as error:  # noqa: BLE001 - isolate each archive.
                errors.append(f"archive publish: {error}")
                self._observe("background_archive_publish", started, failed=True)
                logger.exception("Background archive publication failed")
                continue
            archive_segments.extend(published)
            self._observe(
                "background_archive_publish",
                started,
                item_count=len(published),
            )
        if request.cleanup:
            for ring_buffer in request.ring_buffers:
                if self._is_cancelled(request):
                    break
                started = time.perf_counter()
                try:
                    deleted = tuple(
                        ring_buffer.cleanup(
                            current_time_ms=request.current_time_ms,
                        )
                    )
                except Exception as error:  # noqa: BLE001 - isolate cleanup.
                    errors.append(f"ring cleanup: {error}")
                    self._observe("background_ring_cleanup", started, failed=True)
                    logger.exception("Background ring-buffer cleanup failed")
                    continue
                deleted_paths.extend(Path(path) for path in deleted)
                self._observe(
                    "background_ring_cleanup",
                    started,
                    item_count=len(deleted),
                )
        self._observe(
            "capture_refresh_background",
            refresh_started,
            item_count=discovered_count + len(archive_segments),
            failed=bool(errors),
        )
        return CaptureRefreshResult(
            generation=request.generation,
            archive_segments=tuple(archive_segments),
            deleted_paths=tuple(deleted_paths),
            discovered_segment_count=discovered_count,
            error="; ".join(errors),
            apply_state=request.apply_state,
            cleanup_after_apply=request.cleanup_after_apply,
        )


__all__ = [
    "ArchiveRefreshJob",
    "CaptureRefreshRequest",
    "CaptureRefreshResult",
    "CaptureRefreshWorker",
]
