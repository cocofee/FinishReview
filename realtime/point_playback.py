"""Point-centered judge playback over existing recording segments."""

from __future__ import annotations

import logging
import os
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

try:
    from .video_timeline import (
        PassageVideoLocation,
        RecordingSegment,
        VideoTimelineStore,
    )
except ImportError:
    from video_timeline import (
        PassageVideoLocation,
        RecordingSegment,
        VideoTimelineStore,
    )


POINT_PLAYBACK_BEFORE_MS = 45_000
POINT_PLAYBACK_AFTER_MS = 15_000
POINT_PLAYBACK_GAP_TOLERANCE_MS = 250
_SAFE_FFCONCAT_COMPONENT = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]*")
logger = logging.getLogger("FinishReview.PointPlayback")


class PointPlaybackUnavailable(RuntimeError):
    """Raised when recorded media cannot cover the selected target point."""


@dataclass(frozen=True, slots=True)
class _MediaCandidate:
    path: Path
    timeline_started_at_ms: int
    timeline_ended_at_ms: int
    media_started_at_ms: int
    priority: int
    trim_supported: bool


@dataclass(frozen=True, slots=True)
class _SelectedSpan:
    candidate: _MediaCandidate
    timeline_started_at_ms: int
    timeline_ended_at_ms: int

    @property
    def duration_ms(self) -> int:
        return self.timeline_ended_at_ms - self.timeline_started_at_ms

    @property
    def media_inpoint_ms(self) -> int:
        return (
            self.candidate.media_started_at_ms
            + self.timeline_started_at_ms
            - self.candidate.timeline_started_at_ms
        )

    @property
    def media_outpoint_ms(self) -> int:
        return self.media_inpoint_ms + self.duration_ms


@dataclass(slots=True)
class PointPlaybackSession:
    """One temporary FFconcat index for a point-centered playback range."""

    manifest_path: Path
    anchor_time_ms: int
    requested_started_at_ms: int
    requested_ended_at_ms: int
    available_started_at_ms: int
    available_ended_at_ms: int
    target_position_ms: int
    duration_ms: int
    _staging_dir: Optional[Path] = None
    _staged_paths: tuple[Path, ...] = ()
    _cleanup_callback: Optional[Callable[[], None]] = None

    def cleanup(self) -> None:
        callback = self._cleanup_callback
        self._cleanup_callback = None
        try:
            self.manifest_path.unlink(missing_ok=True)
        except OSError:
            pass
        for path in self._staged_paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        self._staged_paths = ()
        staging_dir = self._staging_dir
        self._staging_dir = None
        if staging_dir is not None:
            try:
                staging_dir.rmdir()
            except OSError:
                pass
        if callback is not None:
            try:
                callback()
            except Exception:  # noqa: BLE001 - temporary playback cleanup is best effort.
                logger.warning("Failed to release point playback media pins", exc_info=True)


def _segment_candidate(
    store: VideoTimelineStore,
    segment: RecordingSegment,
    *,
    priority: int,
) -> Optional[_MediaCandidate]:
    media_started_at_ms = segment.media_started_at_ms
    media_duration_ms = segment.media_duration_ms
    if media_started_at_ms is None or media_duration_ms is None:
        return None
    path = store.resolve_video_path(segment)
    try:
        if not path.is_file() or path.stat().st_size <= 0:
            return None
    except OSError:
        return None
    return _MediaCandidate(
        path=path,
        timeline_started_at_ms=int(media_started_at_ms),
        timeline_ended_at_ms=int(media_started_at_ms + media_duration_ms),
        media_started_at_ms=0,
        priority=int(priority),
        trim_supported=path.suffix.lower() not in {".m3u8", ".ts"},
    )


def _candidate_priority(segment: RecordingSegment) -> int:
    if segment.end_reason == "continuous_archive_fallback":
        return 0
    if Path(segment.video_path).suffix.lower() != ".m3u8":
        return 1
    if segment.end_reason == "passage_review_window":
        return 3
    return 2


def _collect_candidates(
    store: VideoTimelineStore,
    location: PassageVideoLocation,
    *,
    race_id: str,
    requested_started_at_ms: int,
    requested_ended_at_ms: int,
) -> tuple[_MediaCandidate, ...]:
    expected_race_id = str(race_id).strip()
    source_id = location.segment.source_id
    candidates: dict[Path, _MediaCandidate] = {}
    for segment in store.segments():
        if segment.source_id != source_id:
            continue
        if expected_race_id and segment.race_id not in {"", expected_race_id}:
            continue
        candidate = _segment_candidate(
            store,
            segment,
            priority=_candidate_priority(segment),
        )
        if candidate is None:
            continue
        if (
            candidate.timeline_ended_at_ms <= requested_started_at_ms
            or candidate.timeline_started_at_ms >= requested_ended_at_ms
        ):
            continue
        current = candidates.get(candidate.path)
        if current is None or candidate.priority < current.priority:
            candidates[candidate.path] = candidate

    fallback = _segment_candidate(store, location.segment, priority=4)
    if fallback is not None and not (
        fallback.timeline_ended_at_ms <= requested_started_at_ms
        or fallback.timeline_started_at_ms >= requested_ended_at_ms
    ):
        candidates.setdefault(fallback.path, fallback)
    return tuple(candidates.values())


def _ring_buffer_candidates(
    ring_buffer: Any,
    *,
    pin_id: str,
    requested_started_at_ms: int,
    requested_ended_at_ms: int,
) -> tuple[_MediaCandidate, ...]:
    pinned = ring_buffer.pin_window(
        pin_id,
        started_at_ms=requested_started_at_ms,
        ended_at_ms=requested_ended_at_ms,
        scan=True,
    )
    candidates = []
    for segment in pinned:
        path = ring_buffer.resolve_path(segment)
        try:
            if not path.is_file() or path.stat().st_size <= 0:
                continue
        except OSError:
            continue
        candidates.append(
            _MediaCandidate(
                path=path,
                timeline_started_at_ms=int(segment.started_at_ms),
                timeline_ended_at_ms=int(segment.ended_at_ms),
                media_started_at_ms=0,
                priority=1,
                trim_supported=False,
            )
        )
    return tuple(candidates)


def _select_intervals(
    candidates: tuple[_MediaCandidate, ...],
    *,
    requested_started_at_ms: int,
    requested_ended_at_ms: int,
) -> tuple[_SelectedSpan, ...]:
    boundaries = {
        int(requested_started_at_ms),
        int(requested_ended_at_ms),
    }
    for candidate in candidates:
        boundaries.add(
            max(requested_started_at_ms, candidate.timeline_started_at_ms)
        )
        boundaries.add(
            min(requested_ended_at_ms, candidate.timeline_ended_at_ms)
        )
    ordered = sorted(boundaries)
    selected: list[_SelectedSpan] = []
    for started_at_ms, ended_at_ms in zip(ordered, ordered[1:]):
        if ended_at_ms <= started_at_ms:
            continue
        covering = [
            candidate
            for candidate in candidates
            if candidate.timeline_started_at_ms <= started_at_ms
            and candidate.timeline_ended_at_ms >= ended_at_ms
        ]
        if not covering:
            continue
        candidate = min(
            covering,
            key=lambda item: (
                item.priority,
                item.timeline_started_at_ms,
                str(item.path),
            ),
        )
        if (
            selected
            and selected[-1].candidate == candidate
            and selected[-1].timeline_ended_at_ms == started_at_ms
        ):
            selected[-1] = _SelectedSpan(
                candidate=candidate,
                timeline_started_at_ms=selected[-1].timeline_started_at_ms,
                timeline_ended_at_ms=ended_at_ms,
            )
        else:
            selected.append(
                _SelectedSpan(
                    candidate=candidate,
                    timeline_started_at_ms=started_at_ms,
                    timeline_ended_at_ms=ended_at_ms,
                )
            )
    return tuple(selected)


def _component_around_anchor(
    spans: tuple[_SelectedSpan, ...],
    *,
    anchor_time_ms: int,
) -> tuple[_SelectedSpan, ...]:
    components: list[list[_SelectedSpan]] = []
    for span in spans:
        if not components:
            components.append([span])
            continue
        gap_ms = span.timeline_started_at_ms - components[-1][-1].timeline_ended_at_ms
        if gap_ms <= POINT_PLAYBACK_GAP_TOLERANCE_MS:
            components[-1].append(span)
        else:
            components.append([span])
    for component in components:
        if (
            component[0].timeline_started_at_ms
            <= anchor_time_ms
            <= component[-1].timeline_ended_at_ms
        ):
            return tuple(component)
    raise PointPlaybackUnavailable("recorded media does not cover the target point")


def _expand_untrimmed_spans(
    spans: tuple[_SelectedSpan, ...],
) -> tuple[_SelectedSpan, ...]:
    expanded: list[_SelectedSpan] = []
    for span in spans:
        if span.candidate.trim_supported:
            normalized = span
        else:
            normalized = _SelectedSpan(
                candidate=span.candidate,
                timeline_started_at_ms=span.candidate.timeline_started_at_ms,
                timeline_ended_at_ms=span.candidate.timeline_ended_at_ms,
            )
        if expanded and normalized.timeline_started_at_ms < expanded[-1].timeline_ended_at_ms:
            previous = expanded[-1]
            if not normalized.candidate.trim_supported and previous.candidate.trim_supported:
                if previous.timeline_started_at_ms < normalized.timeline_started_at_ms:
                    expanded[-1] = _SelectedSpan(
                        candidate=previous.candidate,
                        timeline_started_at_ms=previous.timeline_started_at_ms,
                        timeline_ended_at_ms=normalized.timeline_started_at_ms,
                    )
                else:
                    expanded.pop()
            elif normalized.candidate.trim_supported:
                normalized = _SelectedSpan(
                    candidate=normalized.candidate,
                    timeline_started_at_ms=expanded[-1].timeline_ended_at_ms,
                    timeline_ended_at_ms=normalized.timeline_ended_at_ms,
                )
        if normalized.timeline_ended_at_ms <= normalized.timeline_started_at_ms:
            continue
        if (
            expanded
            and expanded[-1].candidate == normalized.candidate
            and expanded[-1].timeline_ended_at_ms == normalized.timeline_started_at_ms
        ):
            expanded[-1] = _SelectedSpan(
                candidate=normalized.candidate,
                timeline_started_at_ms=expanded[-1].timeline_started_at_ms,
                timeline_ended_at_ms=normalized.timeline_ended_at_ms,
            )
        elif not expanded or expanded[-1] != normalized:
            expanded.append(normalized)
    return tuple(expanded)


def _safe_media_reference(source_path: Path, manifest_dir: Path) -> Optional[str]:
    try:
        relative_path = source_path.resolve().relative_to(manifest_dir.resolve())
    except ValueError:
        return None
    if not relative_path.parts or any(
        _SAFE_FFCONCAT_COMPONENT.fullmatch(part) is None
        for part in relative_path.parts
    ):
        return None
    return relative_path.as_posix()


def _prepare_media_references(
    manifest_dir: Path,
    staging_dir: Path,
    spans: tuple[_SelectedSpan, ...],
) -> tuple[dict[Path, str], tuple[Path, ...]]:
    references: dict[Path, str] = {}
    staged_paths: dict[Path, Path] = {}
    source_path = staging_dir
    try:
        for span in spans:
            source_path = span.candidate.path.resolve()
            if source_path in references:
                continue
            safe_reference = _safe_media_reference(source_path, manifest_dir)
            if safe_reference is not None:
                references[source_path] = safe_reference
                continue
            staging_dir.mkdir(exist_ok=True)
            suffix = source_path.suffix.lower() or ".media"
            staged_path = staging_dir / f"clip_{len(staged_paths):04d}{suffix}"
            os.link(source_path, staged_path)
            staged_paths[source_path] = staged_path
            references[source_path] = f"{staging_dir.name}/{staged_path.name}"
    except OSError as exc:
        for staged_path in staged_paths.values():
            try:
                staged_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise PointPlaybackUnavailable(
            f"无法准备定点回放分段：{source_path.name}"
        ) from exc
    return references, tuple(staged_paths.values())


def _write_manifest(
    path: Path,
    spans: tuple[_SelectedSpan, ...],
    media_references: dict[Path, str],
) -> None:
    lines = ["ffconcat version 1.0"]
    for span in spans:
        reference = media_references[span.candidate.path.resolve()]
        lines.append(f"file '{reference}'")
        if span.candidate.trim_supported:
            lines.extend(
                (
                    f"inpoint {span.media_inpoint_ms / 1000.0:.6f}",
                    f"outpoint {span.media_outpoint_ms / 1000.0:.6f}",
                )
            )
        lines.append(f"duration {span.duration_ms / 1000.0:.6f}")
    payload = ("\n".join(lines) + "\n").encode("utf-8")
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    with temporary_path.open("wb") as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary_path, path)


def prepare_point_playback(
    timeline_store: VideoTimelineStore,
    location: PassageVideoLocation,
    *,
    anchor_time_ms: int,
    race_id: str,
    output_dir: Path,
    ring_buffer: Any = None,
    before_ms: int = POINT_PLAYBACK_BEFORE_MS,
    after_ms: int = POINT_PLAYBACK_AFTER_MS,
) -> PointPlaybackSession:
    """Build a temporary, non-transcoding playback index around one point."""

    anchor_time_ms = int(anchor_time_ms)
    requested_started_at_ms = max(0, anchor_time_ms - max(0, int(before_ms)))
    requested_ended_at_ms = anchor_time_ms + max(0, int(after_ms))
    pin_id = f"point-playback:{uuid.uuid4().hex}" if ring_buffer is not None else ""
    try:
        candidates = list(
            _collect_candidates(
                timeline_store,
                location,
                race_id=race_id,
                requested_started_at_ms=requested_started_at_ms,
                requested_ended_at_ms=requested_ended_at_ms,
            )
        )
        if ring_buffer is not None:
            candidates.extend(
                _ring_buffer_candidates(
                    ring_buffer,
                    pin_id=pin_id,
                    requested_started_at_ms=requested_started_at_ms,
                    requested_ended_at_ms=requested_ended_at_ms,
                )
            )
        spans = _expand_untrimmed_spans(
            _component_around_anchor(
                _select_intervals(
                    tuple(candidates),
                    requested_started_at_ms=requested_started_at_ms,
                    requested_ended_at_ms=requested_ended_at_ms,
                ),
                anchor_time_ms=anchor_time_ms,
            )
        )
    except Exception:
        if ring_buffer is not None and pin_id:
            ring_buffer.release(pin_id)
        raise
    available_started_at_ms = spans[0].timeline_started_at_ms
    available_ended_at_ms = spans[-1].timeline_ended_at_ms
    duration_ms = sum(span.duration_ms for span in spans)
    target_position_ms = 0
    cursor_ms = 0
    for span in spans:
        if span.timeline_started_at_ms <= anchor_time_ms <= span.timeline_ended_at_ms:
            target_position_ms = cursor_ms + max(
                0,
                min(
                    anchor_time_ms - span.timeline_started_at_ms,
                    span.duration_ms,
                ),
            )
            break
        cursor_ms += span.duration_ms

    output_dir = Path(output_dir).resolve()
    session_id = uuid.uuid4().hex
    staging_dir = output_dir / f"_point_playback_{session_id}"
    manifest_path = output_dir / f".point_playback_{session_id}.ffconcat"
    staged_paths: tuple[Path, ...] = ()
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        media_references, staged_paths = _prepare_media_references(
            output_dir,
            staging_dir,
            spans,
        )
        _write_manifest(manifest_path, spans, media_references)
    except Exception:
        for staged_path in staged_paths:
            try:
                staged_path.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            manifest_path.with_suffix(manifest_path.suffix + ".tmp").unlink(
                missing_ok=True
            )
            manifest_path.unlink(missing_ok=True)
            staging_dir.rmdir()
        except OSError:
            pass
        if ring_buffer is not None and pin_id:
            ring_buffer.release(pin_id)
        raise
    cleanup_callback = None
    if ring_buffer is not None and pin_id:
        cleanup_callback = lambda: ring_buffer.release(pin_id)
    return PointPlaybackSession(
        manifest_path=manifest_path,
        anchor_time_ms=anchor_time_ms,
        requested_started_at_ms=requested_started_at_ms,
        requested_ended_at_ms=requested_ended_at_ms,
        available_started_at_ms=available_started_at_ms,
        available_ended_at_ms=available_ended_at_ms,
        target_position_ms=target_position_ms,
        duration_ms=duration_ms,
        _staging_dir=staging_dir if staged_paths else None,
        _staged_paths=staged_paths,
        _cleanup_callback=cleanup_callback,
    )


__all__ = [
    "POINT_PLAYBACK_AFTER_MS",
    "POINT_PLAYBACK_BEFORE_MS",
    "PointPlaybackSession",
    "PointPlaybackUnavailable",
    "prepare_point_playback",
]
