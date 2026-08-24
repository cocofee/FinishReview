"""Construction helpers for one finish-review recording pipeline."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from .review_recorder import (
    ArchiveTimelinePublisher,
    FfmpegReviewRecorder,
    PassageReviewCoordinator,
    PassageReviewTimelinePublisher,
    ReviewRingBuffer,
)
from .stream_recorder import RecordingError
from .video_timeline import VideoTimelineStore


logger = logging.getLogger("FinishReview")


@dataclass(frozen=True, slots=True)
class RecordingPipeline:
    source: str
    camera_index: int
    recorder: FfmpegReviewRecorder
    ring_buffer: ReviewRingBuffer
    coordinator: PassageReviewCoordinator
    timeline_publisher: PassageReviewTimelinePublisher
    archive_publisher: ArchiveTimelinePublisher


def start_recording_pipeline(
    *,
    source: str,
    output_dir: Path,
    camera_index: int,
    ffmpeg_path: Path | None,
    review_retention_seconds: int,
    timeline_store: VideoTimelineStore,
    timing_error_ms: int,
    recorder_factory: Callable[..., FfmpegReviewRecorder] = FfmpegReviewRecorder,
    ring_buffer_factory: Callable[..., ReviewRingBuffer] = ReviewRingBuffer,
    coordinator_factory: Callable[..., PassageReviewCoordinator] = (
        PassageReviewCoordinator
    ),
    timeline_publisher_factory: Callable[..., PassageReviewTimelinePublisher] = (
        PassageReviewTimelinePublisher
    ),
    archive_publisher_factory: Callable[..., ArchiveTimelinePublisher] = (
        ArchiveTimelinePublisher
    ),
    cleanup_failure_handler: Callable[
        [int, FfmpegReviewRecorder, Exception], None
    ]
    | None = None,
) -> RecordingPipeline:
    """Start one recorder and assemble its review components atomically."""

    recorder = recorder_factory(
        source,
        output_dir,
        camera_index=camera_index,
        ffmpeg_path=ffmpeg_path,
        review_retention_seconds=review_retention_seconds,
    )
    try:
        playlist_path = recorder.start()
        ring_buffer = ring_buffer_factory(
            playlist_path,
            camera_index=camera_index,
            retention_seconds=review_retention_seconds,
        )
        ring_buffer.scan()
        coordinator = coordinator_factory(ring_buffer)
        timeline_publisher = timeline_publisher_factory(
            ring_buffer,
            timeline_store,
            timing_error_ms=timing_error_ms,
        )
        archive_publisher = archive_publisher_factory(recorder, timeline_store)
    except Exception:
        cleanup_error: Exception | None = None
        try:
            recorder.stop()
        except Exception as exc:  # noqa: BLE001 - preserve original error.
            cleanup_error = exc
            logger.warning(
                "Failed to stop camera %s during pipeline startup rollback: %s",
                camera_index,
                exc,
            )
        try:
            still_running = bool(recorder.is_running)
        except Exception as exc:  # noqa: BLE001 - uncertain state must be retained.
            still_running = True
            if cleanup_error is None:
                cleanup_error = exc
        if still_running:
            if cleanup_error is None:
                cleanup_error = RecordingError("Recorder did not stop")
                logger.warning(
                    "Camera %s recorder remained active after startup rollback",
                    camera_index,
                )
            if cleanup_failure_handler is not None:
                cleanup_failure_handler(camera_index, recorder, cleanup_error)
        raise
    return RecordingPipeline(
        source=str(source),
        camera_index=int(camera_index),
        recorder=recorder,
        ring_buffer=ring_buffer,
        coordinator=coordinator,
        timeline_publisher=timeline_publisher,
        archive_publisher=archive_publisher,
    )


@dataclass(frozen=True, slots=True)
class RecordingStopFailure:
    camera_index: int
    error: Exception
    still_running: bool


@dataclass(frozen=True, slots=True)
class _PendingRecorder:
    source: str
    camera_index: int
    recorder: FfmpegReviewRecorder


class RecordingSessionController:
    """Manage the non-UI lifecycle of a multi-camera recording session."""

    def __init__(
        self,
        *,
        recorder_factory: Callable[..., FfmpegReviewRecorder] = FfmpegReviewRecorder,
        ring_buffer_factory: Callable[..., ReviewRingBuffer] = ReviewRingBuffer,
        coordinator_factory: Callable[..., PassageReviewCoordinator] = (
            PassageReviewCoordinator
        ),
        timeline_publisher_factory: Callable[..., PassageReviewTimelinePublisher] = (
            PassageReviewTimelinePublisher
        ),
        archive_publisher_factory: Callable[..., ArchiveTimelinePublisher] = (
            ArchiveTimelinePublisher
        ),
    ):
        self._recorder_factory = recorder_factory
        self._ring_buffer_factory = ring_buffer_factory
        self._coordinator_factory = coordinator_factory
        self._timeline_publisher_factory = timeline_publisher_factory
        self._archive_publisher_factory = archive_publisher_factory
        self._pipelines: dict[int, RecordingPipeline] = {}
        self._pending_recorders: dict[int, _PendingRecorder] = {}
        self._sources: tuple[tuple[int, str], ...] = ()

    @property
    def pipelines(self) -> dict[int, RecordingPipeline]:
        return dict(self._pipelines)

    @property
    def recorders(self) -> dict[int, FfmpegReviewRecorder]:
        recorders = {
            camera_index: pending.recorder
            for camera_index, pending in self._pending_recorders.items()
        }
        recorders.update(
            {
                camera_index: pipeline.recorder
                for camera_index, pipeline in self._pipelines.items()
            }
        )
        return recorders

    @property
    def ring_buffers(self) -> dict[int, ReviewRingBuffer]:
        return {
            camera_index: pipeline.ring_buffer
            for camera_index, pipeline in self._pipelines.items()
        }

    @property
    def coordinators(self) -> dict[int, PassageReviewCoordinator]:
        return {
            camera_index: pipeline.coordinator
            for camera_index, pipeline in self._pipelines.items()
        }

    @property
    def timeline_publishers(self) -> dict[int, PassageReviewTimelinePublisher]:
        return {
            camera_index: pipeline.timeline_publisher
            for camera_index, pipeline in self._pipelines.items()
        }

    @property
    def archive_publishers(self) -> tuple[ArchiveTimelinePublisher, ...]:
        return tuple(
            pipeline.archive_publisher for pipeline in self._pipelines.values()
        )

    @property
    def any_active(self) -> bool:
        return any(
            self._recorder_is_running(recorder, uncertain=True)
            for recorder in self.recorders.values()
        )

    def all_active(self, sources: Iterable[tuple[int, str]]) -> bool:
        configured_sources = self._normalize_sources(sources)
        return (
            bool(configured_sources)
            and not self._pending_recorders
            and configured_sources == self._sources
            and len(self._pipelines) == len(configured_sources)
            and all(
                self._recorder_is_running(
                    self._pipelines[camera_index].recorder,
                    uncertain=False,
                )
                for camera_index, _source in configured_sources
            )
        )

    def start(
        self,
        *,
        sources: Iterable[tuple[int, str]],
        output_dir: Path,
        ffmpeg_path: Path | None,
        review_retention_seconds: int,
        timeline_store: VideoTimelineStore,
        timing_error_ms: int,
    ) -> tuple[RecordingPipeline, ...]:
        configured_sources = self._normalize_sources(sources)
        if not configured_sources:
            raise RecordingError("No recording sources are configured")
        if self._pipelines or self._pending_recorders:
            if self.all_active(configured_sources):
                return tuple(self._pipelines.values())
            raise RecordingError("A different recording session is already active")

        pipelines: dict[int, RecordingPipeline] = {}
        source_by_camera = dict(configured_sources)

        def retain_incomplete_recorder(
            camera_index: int,
            recorder: FfmpegReviewRecorder,
            _error: Exception,
        ) -> None:
            self._pending_recorders[camera_index] = _PendingRecorder(
                source=source_by_camera[camera_index],
                camera_index=camera_index,
                recorder=recorder,
            )

        try:
            for camera_index, source in configured_sources:
                pipelines[camera_index] = start_recording_pipeline(
                    source=source,
                    output_dir=output_dir,
                    camera_index=camera_index,
                    ffmpeg_path=ffmpeg_path,
                    review_retention_seconds=review_retention_seconds,
                    timeline_store=timeline_store,
                    timing_error_ms=timing_error_ms,
                    recorder_factory=self._recorder_factory,
                    ring_buffer_factory=self._ring_buffer_factory,
                    coordinator_factory=self._coordinator_factory,
                    timeline_publisher_factory=self._timeline_publisher_factory,
                    archive_publisher_factory=self._archive_publisher_factory,
                    cleanup_failure_handler=retain_incomplete_recorder,
                )
        except Exception:
            retained_pipelines: dict[int, RecordingPipeline] = {}
            for pipeline in reversed(tuple(pipelines.values())):
                failure = self._stop_recorder(
                    pipeline.camera_index,
                    pipeline.recorder,
                    context="session startup rollback",
                )
                if failure is not None and failure.still_running:
                    retained_pipelines[pipeline.camera_index] = pipeline
            self._pipelines = retained_pipelines
            self._reset_sources_from_state()
            raise

        self._pipelines = pipelines
        self._pending_recorders.clear()
        self._sources = configured_sources
        logger.info(
            "Recording session started for cameras: %s",
            ", ".join(str(camera_index) for camera_index in pipelines),
        )
        return tuple(pipelines.values())

    def stop(self) -> tuple[RecordingStopFailure, ...]:
        failures: list[RecordingStopFailure] = []
        for camera_index, pipeline in tuple(self._pipelines.items()):
            failure = self._stop_recorder(camera_index, pipeline.recorder)
            if failure is None or not failure.still_running:
                self._pipelines.pop(camera_index, None)
            if failure is not None:
                failures.append(failure)

        for camera_index, pending in tuple(self._pending_recorders.items()):
            failure = self._stop_recorder(camera_index, pending.recorder)
            if failure is None or not failure.still_running:
                self._pending_recorders.pop(camera_index, None)
            if failure is not None:
                failures.append(failure)

        self._reset_sources_from_state()
        if not self._pipelines and not self._pending_recorders:
            if not failures:
                logger.info("Recording session stopped")
        return tuple(failures)

    @staticmethod
    def _stop_recorder(
        camera_index: int,
        recorder: FfmpegReviewRecorder,
        *,
        context: str = "shutdown",
    ) -> RecordingStopFailure | None:
        error: Exception | None = None
        try:
            recorder.stop()
        except Exception as exc:  # noqa: BLE001 - recorder factories may vary.
            error = exc
            logger.warning(
                "Failed to stop camera %s recorder during %s: %s",
                camera_index,
                context,
                exc,
            )
        try:
            still_running = bool(recorder.is_running)
        except Exception as exc:  # noqa: BLE001 - uncertain state must be retained.
            still_running = True
            if error is None:
                error = exc
        if still_running and error is None:
            error = RecordingError("Recorder did not stop")
            logger.warning("Camera %s recorder did not stop", camera_index)
        if error is None:
            return None
        return RecordingStopFailure(camera_index, error, still_running)

    @staticmethod
    def _recorder_is_running(
        recorder: FfmpegReviewRecorder,
        *,
        uncertain: bool,
    ) -> bool:
        try:
            return bool(recorder.is_running)
        except Exception:  # noqa: BLE001 - callers choose the conservative default.
            return bool(uncertain)

    def _reset_sources_from_state(self) -> None:
        sources = {
            camera_index: pipeline.source
            for camera_index, pipeline in self._pipelines.items()
        }
        sources.update(
            {
                camera_index: pending.source
                for camera_index, pending in self._pending_recorders.items()
            }
        )
        self._sources = tuple(sorted(sources.items()))

    @staticmethod
    def _normalize_sources(
        sources: Iterable[tuple[int, str]],
    ) -> tuple[tuple[int, str], ...]:
        configured_sources = tuple(
            (int(camera_index), str(source)) for camera_index, source in sources
        )
        camera_indexes = [camera_index for camera_index, _source in configured_sources]
        if len(set(camera_indexes)) != len(camera_indexes):
            raise ValueError("Recording camera indexes must be unique")
        return configured_sources


__all__ = [
    "RecordingPipeline",
    "RecordingSessionController",
    "RecordingStopFailure",
    "start_recording_pipeline",
]
