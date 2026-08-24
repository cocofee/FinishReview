"""Construction helpers for one finish-review recording pipeline."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .review_recorder import (
    ArchiveTimelinePublisher,
    FfmpegReviewRecorder,
    PassageReviewCoordinator,
    PassageReviewTimelinePublisher,
    ReviewRingBuffer,
)
from .video_timeline import VideoTimelineStore


logger = logging.getLogger("FinishReview")


@dataclass(frozen=True, slots=True)
class RecordingPipeline:
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
        try:
            recorder.stop()
        except Exception as cleanup_error:  # noqa: BLE001 - preserve original error.
            logger.warning(
                "Failed to stop camera %s during pipeline startup rollback: %s",
                camera_index,
                cleanup_error,
            )
        raise
    return RecordingPipeline(
        camera_index=int(camera_index),
        recorder=recorder,
        ring_buffer=ring_buffer,
        coordinator=coordinator,
        timeline_publisher=timeline_publisher,
        archive_publisher=archive_publisher,
    )


__all__ = ["RecordingPipeline", "start_recording_pipeline"]
