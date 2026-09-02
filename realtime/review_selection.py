"""Selection planning for the passage review workspace."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping


@dataclass(frozen=True)
class ReviewSelectionPlan:
    event: Any
    lookup: Any
    regular_locations: Mapping[int, Any]
    regular_summary: Any
    high_speed: Any
    active_pane: Any
    reuse_continuous_media: bool
    same_batch_media: bool
    preserve_media: bool
    switching_batch_event: bool


class ReviewSelectionController:
    """Resolve an event selection without mutating player or widget state."""

    def __init__(
        self,
        review: Any,
        *,
        high_speed_location: Callable[[Any], Any],
        openable_statuses: frozenset[str],
    ) -> None:
        self._review = review
        self._high_speed_location = high_speed_location
        self._openable_statuses = openable_statuses

    def select(self, event_id: str, *, preserve_current_frame: Any = None) -> None:
        review = self._review
        if review._active_video_discovered_entry_id:
            review._active_video_discovered_entry_id = ""
            for pane in review.evidence_panes:
                pane.mark_btn.setEnabled(True)
                pane.video_view.clear_identity_cue()
        event = review.passage_store.get(event_id)
        lookup = review._lookups.get(event_id)
        if event is None or lookup is None:
            review.refresh()
            return
        plan = self.prepare(
            event,
            lookup,
            preserve_current_frame=preserve_current_frame,
        )
        review._apply_selection_plan(
            plan,
            preserve_current_frame=preserve_current_frame,
        )

    def prepare(
        self,
        event: Any,
        lookup: Any,
        *,
        preserve_current_frame: Any = None,
    ) -> ReviewSelectionPlan:
        review = self._review
        regular_locations = {
            pane.camera_index: review._regular_location_for_camera(
                lookup,
                pane.camera_index,
            )
            for pane in review.regular_panes
        }
        if review._batch_mode and preserve_current_frame is not None:
            projected = review._location_on_current_media(
                event,
                preserve_current_frame,
            )
            if projected is not None:
                regular_locations[preserve_current_frame.camera_index] = projected

        regular = review._regular_summary_location(event.event_id, lookup)
        high_speed = self._high_speed_location(lookup)
        reuse_continuous_media = review._batch_mode and any(
            pane.location is not None
            and pane._media_context(regular_locations.get(pane.camera_index))
            == pane._media_context(pane.location)
            for pane in review.regular_panes
        )
        same_batch_media = (
            reuse_continuous_media
            and review._selected_event_id != event.event_id
        )
        preserve_media = (
            review._selected_event_id == event.event_id
            and all(
                pane.matches_passage_context(
                    event,
                    regular_locations.get(pane.camera_index),
                )
                for pane in review.regular_panes
            )
            and (
                not review._show_high_speed_pane
                or review.high_speed_pane.matches_passage_context(event, high_speed)
            )
        ) or reuse_continuous_media

        active_pane = review._active_pane
        active_location = (
            regular_locations.get(active_pane.camera_index)
            if active_pane in review.regular_panes
            else high_speed
            if active_pane is review.high_speed_pane
            else None
        )
        active_location_ready = (
            active_location is not None
            and active_location.status in self._openable_statuses
            and active_location.video_path.is_file()
        )
        if active_pane not in review.evidence_panes or not active_location_ready:
            candidates = [
                (pane, regular_locations.get(pane.camera_index))
                for pane in review.regular_panes
            ]
            if review._show_high_speed_pane:
                candidates.append((review.high_speed_pane, high_speed))
            active_pane = next(
                (
                    pane
                    for pane, location in candidates
                    if location is not None
                    and location.status in self._openable_statuses
                    and location.video_path.is_file()
                ),
                review._active_playback_pane(),
            )

        switching_batch_event = (
            review._batch_mode
            and bool(review._selected_event_id)
            and review._selected_event_id != event.event_id
        )
        return ReviewSelectionPlan(
            event=event,
            lookup=lookup,
            regular_locations=regular_locations,
            regular_summary=regular,
            high_speed=high_speed,
            active_pane=active_pane,
            reuse_continuous_media=reuse_continuous_media,
            same_batch_media=same_batch_media,
            preserve_media=preserve_media,
            switching_batch_event=switching_batch_event,
        )
