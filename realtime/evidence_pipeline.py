"""Serial evidence preparation, independent of Qt widgets and selection state."""

from dataclasses import dataclass
from typing import Callable

from .review_recorder import DEFAULT_MAX_SHARED_REVIEW_CLIP_MS, PassageReviewState


@dataclass(frozen=True, slots=True)
class EvidencePassage:
    event_id: str
    revision: int
    race_id: str
    timestamp_ms: int | None
    active: bool = True
    eligible: bool = True


@dataclass(frozen=True, slots=True)
class EvidenceSnapshot:
    passages: tuple[EvidencePassage, ...]
    windows: tuple[tuple[int, tuple], ...]
    previews: tuple[tuple[int, str, object], ...]


def group_ready_windows(items):
    """Keep the existing overlap and 20-second media-boundary semantics."""
    groups = []
    for window, passage in sorted(items, key=lambda item: (
        item[1].race_id, item[0].started_at_ms,
        item[0].passage_timestamp_ms, item[0].event_id,
    )):
        start = window.segments[0].started_at_ms
        end = window.segments[-1].ended_at_ms
        if groups:
            group = groups[-1]
            if (passage.race_id == group[0][1].race_id
                    and window.started_at_ms <= max(w.ended_at_ms for w, _ in group)
                    and max(end, max(w.segments[-1].ended_at_ms for w, _ in group))
                    - min(start, min(w.segments[0].started_at_ms for w, _ in group))
                    <= DEFAULT_MAX_SHARED_REVIEW_CLIP_MS):
                group.append((window, passage))
                continue
        groups.append([(window, passage)])
    return groups


class EvidencePipeline:
    """One worker owns registration, publication and preview-file writes.

    Requests contain the full latest durable event projection. Coalescing a
    request therefore cannot lose a withdrawal or an event that needs retry.
    No GUI object or mutable UI dictionary is accessed by this class.
    """

    def __init__(self, coordinators, publishers, binding_store):
        self.coordinators = dict(coordinators)
        self.publishers = dict(publishers)
        self.binding_store = binding_store
        self._registered = {}

    def refresh(self, passages: tuple[EvidencePassage, ...],
                cancelled: Callable[[], bool]) -> EvidenceSnapshot:
        by_id = {passage.event_id: passage for passage in passages}
        previews = []
        windows_by_camera = []
        for passage in passages:
            if cancelled():
                return EvidenceSnapshot((), (), ())
            if not passage.active or not passage.eligible or passage.timestamp_ms is None:
                if self._registered.get(passage.event_id) != passage:
                    for coordinator in self.coordinators.values():
                        coordinator.discard(
                            passage.event_id,
                            revision=passage.revision if not passage.active else None,
                        )
                    if not passage.active:
                        self.binding_store.deactivate(passage.event_id, passage.revision)
                    self._registered[passage.event_id] = passage
                continue
            if self._registered.get(passage.event_id) == passage:
                continue
            bindings = self.binding_store.active_bindings(passage.event_id, passage.revision)
            for camera, coordinator in self.coordinators.items():
                if any(binding.camera_index == camera
                       and binding.passage_timestamp_ms == passage.timestamp_ms
                       for binding in bindings):
                    continue
                coordinator.register(
                    passage.event_id, passage_timestamp_ms=passage.timestamp_ms,
                    revision=passage.revision, race_id=passage.race_id, scan=False,
                )
            # Only acknowledge the registration when every camera succeeded.
            self._registered[passage.event_id] = passage

        for camera, coordinator in self.coordinators.items():
            if cancelled():
                return EvidenceSnapshot((), (), ())
            windows = tuple(window for window in coordinator.refresh(scan=False)
                            if (passage := by_id.get(window.event_id)) is not None
                            and passage.active and passage.eligible
                            and passage.revision == window.revision
                            and passage.timestamp_ms == window.passage_timestamp_ms)
            publisher = self.publishers[camera]
            ready = []
            for window in windows:
                if cancelled():
                    return EvidenceSnapshot((), (), ())
                passage = by_id[window.event_id]
                if window.state is PassageReviewState.READY and window.segments:
                    bindings = self.binding_store.active_bindings(passage.event_id, passage.revision)
                    if not any(binding.camera_index == camera for binding in bindings):
                        ready.append((window, passage))
                else:
                    preview = publisher.preview(window, race_id=passage.race_id)
                    if preview is not None:
                        previews.append((camera, window.event_id, preview))
            for group in group_ready_windows(ready):
                if cancelled():
                    return EvidenceSnapshot((), (), ())
                publisher.publish_many(
                    tuple((window, passage.revision) for window, passage in group),
                    race_id=group[0][1].race_id,
                )
            windows_by_camera.append((camera, windows))
        return EvidenceSnapshot(passages, tuple(windows_by_camera), tuple(previews))


@dataclass(frozen=True, slots=True)
class EvidenceRefreshJob:
    pipeline: EvidencePipeline
    passages: tuple[EvidencePassage, ...]
