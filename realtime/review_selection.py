"""Selection planning for the passage review workspace.

The controller deliberately knows about selection data and pane services only.
The Qt surface is kept behind :class:`ReviewSelectionContext` so the planning
rules can be exercised without constructing the main window.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence


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
    locate_target: bool = False


@dataclass(frozen=True)
class ReviewSelectionContext:
    """Small service boundary used by :class:`ReviewSelectionController`.

    Every callable is evaluated when a selection is made.  This matters for
    the Qt surface because panes, the active pane, and the high-speed toggle
    can change while a review window is open. Callbacks may be bound to the
    surface; the controller accesses only these explicit services and reads
    pane state without changing widgets.
    """

    event_for_id: Callable[[str], Any]
    lookup_for_id: Callable[[str], Any]
    regular_panes: Callable[[], Sequence[Any]]
    evidence_panes: Callable[[], Sequence[Any]]
    high_speed_pane: Callable[[], Any]
    active_pane: Callable[[], Any]
    batch_mode: Callable[[], bool]
    selected_event_id: Callable[[], str]
    regular_location_for_camera: Callable[[Any, int], Any]
    regular_summary_location: Callable[[str, Any], Any]
    location_on_current_media: Callable[[Any, Any], Any]
    active_playback_pane: Callable[[], Any]
    show_high_speed_pane: Callable[[], bool]
    apply_selection_plan: Callable[..., None]
    refresh: Callable[[], None]
    continuous_lookup_for_camera: Callable[[Any, int], Any] | None = None
    clear_active_video_identity: Callable[[], None] | None = None

    @classmethod
    def from_review(cls, review: Any) -> "ReviewSelectionContext":
        """Build a compatibility context for the pre-refactor window API."""

        continuous_lookup = getattr(review, "_continuous_lookup_for_camera", None)
        return cls(
            event_for_id=lambda event_id: review.passage_store.get(event_id),
            lookup_for_id=lambda event_id: review._lookups.get(event_id),
            regular_panes=lambda: review.regular_panes,
            evidence_panes=lambda: review.evidence_panes,
            high_speed_pane=lambda: review.high_speed_pane,
            active_pane=lambda: review._active_pane,
            batch_mode=lambda: bool(review._batch_mode),
            selected_event_id=lambda: review._selected_event_id,
            regular_location_for_camera=review._regular_location_for_camera,
            regular_summary_location=review._regular_summary_location,
            location_on_current_media=review._location_on_current_media,
            active_playback_pane=review._active_playback_pane,
            show_high_speed_pane=lambda: bool(review._show_high_speed_pane),
            apply_selection_plan=lambda plan, **kwargs: review._apply_selection_plan(
                plan, **kwargs,
            ),
            refresh=lambda: review.refresh(),
            continuous_lookup_for_camera=continuous_lookup,
            clear_active_video_identity=lambda: _clear_active_video_identity(review),
        )


def _clear_active_video_identity(review: Any) -> None:
    """Clear the transient filmstrip identity cue through the old surface API."""

    if not getattr(review, "_active_video_discovered_entry_id", ""):
        return
    review._active_video_discovered_entry_id = ""
    for pane in review.evidence_panes:
        pane.mark_btn.setEnabled(True)
        pane.video_view.clear_identity_cue()


class ReviewSelectionController:
    """Plan selections from explicit services and delegate applying the result."""

    def __init__(
        self,
        review: Any = None,
        *,
        context: ReviewSelectionContext | None = None,
        high_speed_location: Callable[[Any], Any],
        openable_statuses: frozenset[str],
    ) -> None:
        if context is not None and review is not None:
            raise TypeError("pass either context or review, not both")
        if context is None:
            if review is None:
                raise TypeError("a selection context is required")
            context = (
                review if isinstance(review, ReviewSelectionContext)
                else ReviewSelectionContext.from_review(review)
            )
        self._context = context
        self._high_speed_location = high_speed_location
        self._openable_statuses = openable_statuses

    def select(
        self,
        event_id: str,
        *,
        preserve_current_frame: Any = None,
        locate_target: bool = False,
    ) -> None:
        context = self._context
        if context.clear_active_video_identity is not None:
            context.clear_active_video_identity()
        event = context.event_for_id(event_id)
        lookup = context.lookup_for_id(event_id)
        if event is None or lookup is None:
            context.refresh()
            return
        plan = self.prepare(
            event,
            lookup,
            preserve_current_frame=preserve_current_frame,
            locate_target=locate_target,
        )
        context.apply_selection_plan(
            plan,
            preserve_current_frame=preserve_current_frame,
        )

    def prepare(
        self,
        event: Any,
        lookup: Any,
        *,
        preserve_current_frame: Any = None,
        locate_target: bool = False,
    ) -> ReviewSelectionPlan:
        context = self._context
        regular_panes = tuple(context.regular_panes())
        evidence_panes = tuple(context.evidence_panes())
        regular_locations = {}
        for pane in regular_panes:
            if locate_target and context.continuous_lookup_for_camera is not None:
                # A roster double-click means "locate this passage on the
                # continuous recording".  The normal lookup may still point
                # at a short evidence clip (or an old saved association), so
                # deliberately bypass it for the regular camera panes.
                continuous_lookup = context.continuous_lookup_for_camera(
                    event,
                    pane.camera_index,
                )
                location = context.regular_location_for_camera(
                    continuous_lookup,
                    pane.camera_index,
                )
                if location is not None:
                    regular_locations[pane.camera_index] = location
                    continue
            regular_locations[pane.camera_index] = (
                context.regular_location_for_camera(
                    lookup,
                    pane.camera_index,
                )
            )
        if preserve_current_frame is not None and not locate_target:
            # Changing the roster identity must not move either linked camera
            # to that identity's nominal recording. Keep every loaded regular
            # pane on its current media; the operator advances video explicitly.
            for pane in regular_panes:
                projected = context.location_on_current_media(event, pane)
                if projected is not None:
                    regular_locations[pane.camera_index] = projected

        regular = context.regular_summary_location(event.event_id, lookup)
        high_speed = self._high_speed_location(lookup)
        batch_mode = context.batch_mode()
        selected_event_id = context.selected_event_id()
        reuse_continuous_media = not locate_target and (
            batch_mode or preserve_current_frame is not None
        ) and any(
            pane.location is not None
            and pane._media_context(regular_locations.get(pane.camera_index))
            == pane._media_context(pane.location)
            for pane in regular_panes
        )
        same_batch_media = (
            reuse_continuous_media
            and selected_event_id != event.event_id
        )
        high_speed_pane = context.high_speed_pane()
        primary_pane = regular_panes[0]
        preserve_media = not locate_target and (
            (
                selected_event_id == event.event_id
                # Camera 1 is the authoritative continuous review surface.
                # Missing/late secondary-camera media must not cause a roster
                # click to reload or seek the current judgment frame.
                and primary_pane.matches_passage_context(
                    event,
                    regular_locations.get(primary_pane.camera_index),
                )
            )
            or reuse_continuous_media
        )

        active_pane = context.active_pane()
        if locate_target:
            # The locate command is defined by ordinary camera-1 time.  Make
            # that pane the visible judgment surface even if the operator was
            # previously looking at a high-speed pane.
            continuous_pane = next(
                (
                    pane
                    for pane in regular_panes
                    if (
                        regular_locations.get(pane.camera_index) is not None
                        and regular_locations[pane.camera_index].status
                        in self._openable_statuses
                        and regular_locations[pane.camera_index].video_path.is_file()
                    )
                ),
                None,
            )
            if continuous_pane is not None:
                active_pane = continuous_pane
        active_location = (
            regular_locations.get(active_pane.camera_index)
            if active_pane in regular_panes
            else high_speed
            if active_pane is high_speed_pane
            else None
        )
        active_location_ready = (
            active_location is not None
            and active_location.status in self._openable_statuses
            and active_location.video_path.is_file()
        )
        if active_pane not in evidence_panes or not active_location_ready:
            candidates = [
                (pane, regular_locations.get(pane.camera_index))
                for pane in regular_panes
            ]
            if context.show_high_speed_pane():
                candidates.append((high_speed_pane, high_speed))
            active_pane = next(
                (
                    pane
                    for pane, location in candidates
                    if location is not None
                    and location.status in self._openable_statuses
                    and location.video_path.is_file()
                ),
                context.active_playback_pane(),
            )

        switching_batch_event = (
            batch_mode
            and bool(selected_event_id)
            and selected_event_id != event.event_id
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
            locate_target=bool(locate_target),
        )
