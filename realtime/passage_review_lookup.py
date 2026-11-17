"""Locating a passage in the recordings (lookup, cache and saved associations) for the passage review surface."""

from __future__ import annotations

from dataclasses import replace
from typing import Optional

from .auyat_rgb import AUYAT_CLOCK_SOURCE
from .passage_evidence import HIGH_SPEED_SOURCE, REGULAR_SOURCE, PassageEvidenceAssociation
from .passage_receiver import PassageEvent
from .video_timeline import PassageVideoLocation, PassageVideoLookup
from .passage_review_common import (
    _OPENABLE_STATUSES,
    _STATUS_PRIORITY,
    _association_matches_location,
    is_high_speed,
    source_location,
)
from .passage_evidence_pane import PassageEvidencePane


class PassageReviewLookupMixin:
    """Find where a passage is in the recordings, with cache and saved associations."""

    def _association_for_event(
        self, event_id: str, source_kind: str,
    ) -> Optional[PassageEvidenceAssociation]:
        return self.association_store.get_for_event(
            self.passage_store.get(event_id), source_kind,
        )

    def _current_associations(self) -> tuple[PassageEvidenceAssociation, ...]:
        return tuple(
            association for association in self.association_store.associations()
            if association.matches_event(self.passage_store.get(association.passage_event_id))
        )

    def _bound_regular_locations(
        self,
        event: PassageEvent,
    ) -> tuple[PassageVideoLocation, ...]:
        store = self.review_binding_store
        if store is None:
            return ()
        locations = []
        for binding in store.active_bindings(event.event_id, event.revision):
            clip = store.get_clip(binding.clip_id)
            if clip is None:
                continue
            segment = self.timeline_store.get_segment(clip.timeline_segment_id)
            if segment is None:
                continue
            video_path = self.timeline_store.resolve_video_path(segment)
            camera_offset = self._clock_offset_for_camera(
                segment.camera_index
            )
            passage_position_ms = max(
                0,
                binding.passage_offset_ms + camera_offset,
            )
            if segment.media_duration_ms is not None:
                passage_position_ms = min(
                    passage_position_ms,
                    segment.media_duration_ms,
                )
            status = (
                "located"
                if self.timeline_store.video_path_is_playable(video_path)
                else "missing_file"
            )
            locations.append(
                PassageVideoLocation(
                    segment=segment,
                    video_path=video_path,
                    passage_position_ms=passage_position_ms,
                    playback_position_ms=max(
                        0,
                        passage_position_ms - self.pre_roll_ms,
                    ),
                    clock_offset_ms=camera_offset,
                    timing_error_ms=segment.timing_error_ms,
                    status=status,
                    media_locator=clip.clip_id,
                )
            )
        return tuple(sorted(locations, key=lambda item: item.segment.camera_index))

    def _continuous_lookup_for_camera(
        self,
        event: PassageEvent,
        camera_index: int,
    ) -> PassageVideoLookup:
        base_offset = self._clock_offset_for_camera(camera_index)
        lookup = self.timeline_store.locate_passage(
            event.timeline_timestamp_ms,
            clock_offset_ms=base_offset,
            pre_roll_ms=self.CONTINUOUS_SKIP_LEAD_MS,
            race_id=event.race_id,
            prefer_continuous_media=True,
        )
        location = self._regular_location_for_camera(lookup, camera_index)
        if location is None:
            return lookup
        calibrated_offset = self._continuous_offset_for_location(location)
        if calibrated_offset == base_offset:
            return lookup
        return self.timeline_store.locate_passage(
            event.timeline_timestamp_ms,
            clock_offset_ms=calibrated_offset,
            pre_roll_ms=self.CONTINUOUS_SKIP_LEAD_MS,
            race_id=event.race_id,
            prefer_continuous_media=True,
        )

    def _lookup(self, event: PassageEvent) -> PassageVideoLookup:
        direct_locations = (
            () if self._batch_mode else self._bound_regular_locations(event)
        )
        configured = set(self._configured_regular_camera_indexes)
        direct_camera_indexes = {
            location.segment.camera_index
            for location in direct_locations
            if location.status in _OPENABLE_STATUSES
        }
        needs_fallback = not direct_locations or not configured.issubset(
            direct_camera_indexes
        )
        if needs_fallback:
            fallback_locations = []
            fallback_statuses = []
            for camera_index in self._configured_regular_camera_indexes:
                fallback = (
                    self._continuous_lookup_for_camera(event, camera_index)
                    if self._batch_mode
                    else self.timeline_store.locate_passage(
                        event.timeline_timestamp_ms,
                        clock_offset_ms=self._clock_offset_for_camera(camera_index),
                        pre_roll_ms=self.pre_roll_ms,
                        race_id=event.race_id,
                    )
                )
                fallback_statuses.append(fallback.status)
                fallback_locations.extend(
                    location
                    for location in fallback.locations
                    if location.segment.camera_index == camera_index
                )
            # Preserve recorded high-speed/external evidence that is not part
            # of the configured ordinary-camera set.
            external_camera_indexes = sorted(
                {
                    segment.camera_index
                    for segment in self.timeline_store.segments()
                    if segment.camera_index
                    not in self._configured_regular_camera_indexes
                }
            )
            for camera_index in external_camera_indexes:
                fallback = self.timeline_store.locate_passage(
                    event.timeline_timestamp_ms,
                    clock_offset_ms=self._clock_offset_for_camera(camera_index),
                    pre_roll_ms=self.pre_roll_ms,
                    race_id=event.race_id,
                )
                fallback_statuses.append(fallback.status)
                fallback_locations.extend(
                    location
                    for location in fallback.locations
                    if location.segment.camera_index == camera_index
                )
            locations_by_camera = {
                location.segment.camera_index: location
                for location in direct_locations
            }
            for location in fallback_locations:
                current = locations_by_camera.get(location.segment.camera_index)
                if current is None or (
                    current.status == "missing_file"
                    and location.status in _OPENABLE_STATUSES
                ):
                    locations_by_camera[location.segment.camera_index] = location
            locations = list(locations_by_camera.values())
            lookup_status = (
                "located"
                if fallback_locations
                else next(iter(fallback_statuses), "no_segments")
            )
        else:
            locations = list(direct_locations)
            lookup_status = "located"
        lookup = PassageVideoLookup(
            "located"
            if any(location.status == "located" for location in locations)
            else lookup_status,
            event.timeline_timestamp_ms + self.clock_offset_ms,
            tuple(sorted(locations, key=lambda item: item.segment.camera_index)),
        )
        locations = list(lookup.locations)
        if not self._include_recorded_evidence:
            locations = [
                location
                for location in locations
                if (
                    is_high_speed(location)
                    and self._show_high_speed_pane
                )
                or (
                    not is_high_speed(location)
                    and location.segment.camera_index in configured
                )
            ]
        if self._high_speed_locator is None or not self._show_high_speed_pane:
            if tuple(locations) == lookup.locations:
                return lookup
            status = (
                "located"
                if any(location.status == "located" for location in locations)
                else "near_boundary"
                if any(location.status == "near_boundary" for location in locations)
                else lookup.status
            )
            return PassageVideoLookup(
                status,
                event.timeline_timestamp_ms + self.clock_offset_ms,
                tuple(locations),
            )
        locations = [location for location in locations if not is_high_speed(location)]
        high_speed = self._high_speed_locator(
            event,
            self.clock_offset_ms,
            self.pre_roll_ms,
        )
        if high_speed is not None:
            locations.append(high_speed)
        if any(location.status == "located" for location in locations):
            status = "located"
        elif any(location.status == "near_boundary" for location in locations):
            status = "near_boundary"
        else:
            status = lookup.status
        return PassageVideoLookup(
            status,
            event.timeline_timestamp_ms + self.clock_offset_ms,
            tuple(locations),
        )

    def invalidate_external_locations(self) -> None:
        self._external_location_revision += 1
        self._lookup_cache.clear()
        self.refresh()

    def _timeline_cache_signature(self) -> tuple:
        return (self.timeline_store.revision,)

    @staticmethod
    def _cache_survives_timeline_update(lookup: PassageVideoLookup) -> bool:
        return bool(lookup.locations) and all(
            location.status != "preview" for location in lookup.locations
        )

    def _cached_lookup(self, event: PassageEvent) -> PassageVideoLookup:
        key = (
            event.revision,
            event.timeline_timestamp_ms,
            event.race_id,
            self.clock_offset_ms,
            self.pre_roll_ms,
            self._external_location_revision,
            tuple(self._configured_regular_camera_indexes),
            self._show_high_speed_pane,
            self._include_recorded_evidence,
            self._batch_mode,
            self._continuous_calibration_revision,
            self.review_binding_store.active_bindings(event.event_id, event.revision)
            if self.review_binding_store is not None
            else (),
        )
        cached = self._lookup_cache.get(event.event_id)
        if cached is not None and cached[0] == key:
            return cached[1]
        lookup = self._lookup(event)
        self._lookup_cache[event.event_id] = (key, lookup)
        return lookup

    def _source_association(
        self,
        event_id: str,
        source_kind: str,
        location: Optional[PassageVideoLocation],
    ) -> Optional[PassageEvidenceAssociation]:
        association = self._association_for_event(event_id, source_kind)
        if not _association_matches_location(association, location):
            return None
        return association

    def _source_location_with_saved_association(
        self,
        event_id: str,
        lookup: PassageVideoLookup,
        *,
        high_speed: bool,
    ) -> Optional[PassageVideoLocation]:
        source_kind = HIGH_SPEED_SOURCE if high_speed else REGULAR_SOURCE
        association = self._association_for_event(event_id, source_kind)
        if association is not None:
            associated = next(
                (
                    location
                    for location in lookup.locations
                    if is_high_speed(location) is bool(high_speed)
                    and _association_matches_location(association, location)
                ),
                None,
            )
            if associated is not None:
                return associated
            if not high_speed:
                saved_location = self._saved_regular_location(association)
                if saved_location is not None:
                    return saved_location
        return source_location(lookup, high_speed=high_speed)

    def _saved_regular_location(
        self,
        association: PassageEvidenceAssociation,
    ) -> Optional[PassageVideoLocation]:
        """Resolve the exact recording segment stored by a confirmation."""

        segment = self.timeline_store.get_segment(association.segment_id)
        if segment is None or segment.clock_source == AUYAT_CLOCK_SOURCE:
            return None
        video_path = self.timeline_store.resolve_video_path(segment)
        duration_ms = int(
            segment.media_duration_ms
            if segment.media_duration_ms is not None
            else max(
                0,
                int(segment.ended_at_ms or segment.started_at_ms)
                - int(segment.started_at_ms),
            )
        )
        position_ms = max(0, int(association.position_ms))
        if duration_ms > 0:
            position_ms = min(position_ms, duration_ms)
        return PassageVideoLocation(
            segment=segment,
            video_path=video_path,
            passage_position_ms=position_ms,
            playback_position_ms=max(0, position_ms - self.pre_roll_ms),
            clock_offset_ms=self._clock_offset_for_camera(segment.camera_index),
            timing_error_ms=segment.timing_error_ms,
            status=(
                "located"
                if self.timeline_store.video_path_is_playable(video_path)
                else "missing_file"
            ),
        )

    @staticmethod
    def _confirmation_status(
        regular: Optional[PassageEvidenceAssociation],
        high_speed: Optional[PassageEvidenceAssociation],
        fallback: str,
    ) -> str:
        if regular is not None or high_speed is not None:
            return "已确认"
        return fallback

    @staticmethod
    def _display_confirmation_status(status: str) -> str:
        return "已确认" if status == "已确认" else "未确认"

    @staticmethod
    def _saved_delta_ms(
        regular_location: Optional[PassageVideoLocation],
        high_speed_location: Optional[PassageVideoLocation],
        regular: Optional[PassageEvidenceAssociation],
        high_speed: Optional[PassageEvidenceAssociation],
    ) -> int:
        candidates = []
        if regular is not None and regular_location is not None:
            candidates.append(
                (
                    regular.confirmed_at_ms,
                    regular.position_ms - regular_location.passage_position_ms,
                )
            )
        if high_speed is not None and high_speed_location is not None:
            candidates.append(
                (
                    high_speed.confirmed_at_ms,
                    high_speed.position_ms - high_speed_location.passage_position_ms,
                )
            )
        if not candidates:
            return 0
        return int(max(candidates)[1])

    @staticmethod
    def _regular_locations(
        lookup: PassageVideoLookup,
    ) -> tuple[PassageVideoLocation, ...]:
        return tuple(
            sorted(
                (
                    location
                    for location in lookup.locations
                    if not is_high_speed(location)
                ),
                key=lambda location: (
                    _STATUS_PRIORITY.get(location.status, 99),
                    location.segment.camera_index,
                    location.segment.segment_id,
                ),
            )
        )

    def _regular_summary_location(
        self,
        event_id: str,
        lookup: PassageVideoLookup,
    ) -> Optional[PassageVideoLocation]:
        locations = self._regular_locations(lookup)
        association = self._association_for_event(event_id, REGULAR_SOURCE)
        if association is not None:
            associated = next(
                (
                    location
                    for location in locations
                    if _association_matches_location(association, location)
                ),
                None,
            )
            if associated is not None:
                return associated
        configured = set(self._configured_regular_camera_indexes)
        return next(
            (
                location
                for location in locations
                if location.segment.camera_index in configured
            ),
            locations[0] if locations else None,
        )

    def _location_on_current_media(
        self,
        event: PassageEvent,
        pane: PassageEvidencePane,
    ) -> Optional[PassageVideoLocation]:
        location = pane.location
        if location is None or location.segment.media_started_at_ms is None:
            return None
        offset_ms = self._continuous_offset_for_location(location)
        target_time_ms = int(event.timeline_timestamp_ms) + int(offset_ms)
        # 换号码只换身份；预计过线点可以在当前文件之外，不能截断后改变原帧时间。
        position_ms = target_time_ms - int(location.segment.media_started_at_ms)
        playback_ms = max(0, position_ms - self.CONTINUOUS_SKIP_LEAD_MS)
        if location.segment.media_duration_ms is not None:
            playback_ms = min(playback_ms, location.segment.media_duration_ms)
        return replace(
            location,
            passage_position_ms=position_ms,
            playback_position_ms=playback_ms,
            clock_offset_ms=offset_ms,
        )
