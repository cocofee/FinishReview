"""Selecting, opening and confirming a passage on the review surface."""

from __future__ import annotations

import logging
import time
from typing import Optional

from PyQt5.QtWidgets import QAbstractItemView, QMessageBox

from .passage_evidence import HIGH_SPEED_SOURCE, REGULAR_SOURCE, PassageEvidenceAssociation
from .passage_receiver import PassageEvent
from .review_selection import ReviewSelectionPlan
from .video_timeline import PassageVideoLocation
from .passage_review_common import (
    _OPENABLE_STATUSES,
    format_passage_time,
    is_high_speed,
    lookup_status_text,
    review_status_text,
    source_confirmation_status,
    source_location,
)
from .passage_evidence_pane import PassageEvidencePane

logger = logging.getLogger("FinishReview.Review")


class PassageReviewSelectionMixin:
    """Select and open a passage, mark and confirm it, and keep the summary in step."""

    def _on_table_selection_changed(
        self,
        current_row: int | None = None,
        _current_column: int = -1,
        _previous_row: int = -1,
        _previous_column: int = -1,
    ) -> None:
        row = self.table.currentRow() if current_row is None else int(current_row)
        if 0 <= row < len(self._visible_events):
            event_id = self._visible_events[row].event_id
            active_pane = self._active_playback_pane()
            event = self.passage_store.get(event_id)
            preserve_current_frame = (
                active_pane
                if event is not None
                and getattr(active_pane, "_current_frame_index", -1) >= 0
                else None
            )
            self._select_event(
                event_id,
                preserve_current_frame=preserve_current_frame,
                include_historical=True,
            )

    def _begin_selection_timing(
        self,
        event_id: str,
        priority_pane: PassageEvidencePane,
    ) -> None:
        self._deferred_start_timer.stop()
        self._selection_event_id = str(event_id)
        self._selection_started_at = time.perf_counter()
        self._selection_first_frame_ms = 0.0
        self._selection_expected_panes = 0
        self._selection_pending_panes.clear()
        self._deferred_selection_panes.clear()
        self._selection_priority_pane = priority_pane

    def _track_selection_pane(
        self,
        pane: PassageEvidencePane,
        *,
        deferred: bool,
    ) -> None:
        if not pane.has_playback_worker:
            return
        self._selection_expected_panes += 1
        self._selection_pending_panes.add(pane)
        if deferred and pane.worker_start_deferred:
            self._deferred_selection_panes.add(pane)

    def _start_deferred_selection_panes(self) -> None:
        self._deferred_start_timer.stop()
        panes = tuple(self._deferred_selection_panes)
        self._deferred_selection_panes.clear()
        for pane in panes:
            pane.start_deferred_worker()

    def _on_pane_initial_frame_ready(
        self,
        pane: PassageEvidencePane,
        event_id: str,
    ) -> None:
        if (
            str(event_id) != self._selection_event_id
            or pane not in self._selection_pending_panes
        ):
            return
        elapsed_ms = (time.perf_counter() - self._selection_started_at) * 1000.0
        if self._selection_first_frame_ms <= 0.0:
            self._selection_first_frame_ms = elapsed_ms
        self._selection_pending_panes.discard(pane)
        if pane is self._selection_priority_pane:
            self._start_deferred_selection_panes()
        if self._selection_pending_panes:
            return
        logger.info(
            "Athlete selection event=%s panes=%d first_frame=%.1fms all_frames=%.1fms",
            self._selection_event_id,
            self._selection_expected_panes,
            self._selection_first_frame_ms,
            elapsed_ms,
        )
        self._deferred_start_timer.stop()
        self._selection_event_id = ""
        self._selection_priority_pane = None

    def _select_event(
        self,
        event_id: str,
        *,
        preserve_current_frame: Optional[PassageEvidencePane] = None,
        locate_target: bool = False,
        include_historical: bool = False,
    ) -> None:
        self._video_candidate_anchor_time_ms = None
        if preserve_current_frame is None and self._batch_mode and not locate_target:
            active_pane = self._active_playback_pane()
            if getattr(active_pane, "_current_frame_index", -1) >= 0:
                preserve_current_frame = active_pane
        self._include_historical_filmstrip_on_selection = bool(include_historical)
        try:
            self.selection_controller.select(
                event_id,
                preserve_current_frame=preserve_current_frame,
                locate_target=locate_target,
            )
        finally:
            self._include_historical_filmstrip_on_selection = False

    def _apply_selection_plan(
        self,
        plan: ReviewSelectionPlan,
        *,
        preserve_current_frame: Optional[PassageEvidencePane] = None,
    ) -> None:
        event = plan.event
        lookup = plan.lookup
        regular_locations = plan.regular_locations
        if getattr(self, "_include_historical_filmstrip_on_selection", False):
            self.video_filmstrip.full_race.ensure_time_visible(event.timeline_timestamp_ms)
        regular = plan.regular_summary
        high_speed = plan.high_speed
        reuse_continuous_media = plan.reuse_continuous_media
        same_batch_media = plan.same_batch_media
        preserve_media = plan.preserve_media
        active_pane = plan.active_pane
        switching_batch_event = plan.switching_batch_event
        # A double-click locate can explicitly choose camera 1 even when the
        # operator was previously focused on another pane. Apply that choice
        # before scheduling linked playback or filmstrip work.
        if active_pane in self.evidence_panes:
            self._activate_pane(active_pane, align=False)
        if switching_batch_event:
            # Identity changes must leave the displayed frame stable. Do not
            # start deferred workers merely to pause them.
            self._set_sync_playing(False, seek_final=False)
            for pane in self.evidence_panes:
                pane.set_playing(False)
        regular_association = self._source_association(
            event.event_id, REGULAR_SOURCE, regular
        )
        high_speed_association = self._source_association(
            event.event_id, HIGH_SPEED_SOURCE, high_speed
        )
        if not preserve_media:
            self._set_sync_playing(False)
            self._begin_selection_timing(event.event_id, active_pane)
            self._shared_delta_ms = self._saved_delta_ms(
                regular,
                high_speed,
                regular_association,
                high_speed_association,
            )
            if plan.locate_target:
                # A roster double-click is an absolute locate command.  Do
                # not replay an old saved evidence offset on the continuous
                # camera timeline.
                self._shared_delta_ms = 0
        self._selected_event_id = event.event_id
        if self._batch_mode:
            batch = self._review_batch_by_event_id.get(event.event_id)
            self._active_review_batch_id = batch.batch_id if batch is not None else ""
        identity = event.bib.strip() or "未知"
        metadata = self._current_metadata()
        metadata_athlete = self._metadata_athlete_for_event(event)
        athlete_name = event.athlete_name.strip() or (
            metadata_athlete.name.strip() if metadata_athlete is not None else ""
        ) or "--"
        team_name = event.team_name.strip() or (
            metadata_athlete.team_name.strip()
            if metadata_athlete is not None
            else ""
        ) or "--"
        passage_time = format_passage_time(event.timeline_timestamp_ms)
        status = self._confirmation_status(
            regular_association,
            high_speed_association,
            review_status_text(lookup, regular, high_speed),
        )
        display_status = self._display_confirmation_status(status)
        self.race_value.setText(
            (metadata.race_name.strip() if metadata is not None else "")
            or event.race_name.strip()
            or event.race_id
        )
        self.stage_value.setText(
            (metadata.stage_name.strip() if metadata is not None else "")
            or event.stage_name.strip()
            or event.stage_id
        )
        self.group_value.setText(
            (
                metadata.group_label(event.group_id)
                if metadata is not None
                else ""
            )
            or event.group_name.strip()
            or event.group_id
        )
        self.selected_identity_value.setText(identity)
        self.athlete_value.setText(athlete_name)
        self.team_value.setText(team_name)
        self.selected_time_value.setText(passage_time)
        self.source_value.setText(display_status)
        athlete_summary = (
            f"{identity} {athlete_name if athlete_name != '--' else ''}".strip()
        )
        self.current_passage_label.setText(athlete_summary)
        self._update_current_context(display_status)
        if not preserve_media:
            self.current_time_label.setText(
                format_passage_time(event.timeline_timestamp_ms + self._shared_delta_ms)
            )
            for pane in self.regular_panes:
                pane_location = regular_locations.get(pane.camera_index)
                defer_worker_start = pane is not active_pane
                pane.set_passage(
                    event,
                    pane_location,
                    self._source_association(
                        event.event_id,
                        REGULAR_SOURCE,
                        pane_location,
                    ),
                    initial_delta_ms=self._shared_delta_ms,
                    lookup_status=(
                        pane_location.status
                        if pane_location is not None
                        else lookup.status
                    ),
                    defer_worker_start=defer_worker_start,
                )
                if not (
                    self._batch_mode
                    and defer_worker_start
                    and self.SINGLE_CAMERA_PREVIEW
                ):
                    self._track_selection_pane(
                        pane,
                        deferred=defer_worker_start,
                    )
            if self._show_high_speed_pane:
                defer_high_speed = (
                    self._batch_mode and self.high_speed_pane is not active_pane
                )
                self.high_speed_pane.set_passage(
                    event,
                    high_speed,
                    high_speed_association,
                    initial_delta_ms=self._shared_delta_ms,
                    lookup_status=high_speed.status if high_speed is not None else "",
                    defer_worker_start=defer_high_speed,
                )
                if not defer_high_speed or not self.SINGLE_CAMERA_PREVIEW:
                    self._track_selection_pane(
                        self.high_speed_pane,
                        deferred=False,
                    )
            if self._deferred_selection_panes:
                if active_pane not in self._selection_pending_panes:
                    self._start_deferred_selection_panes()
                else:
                    self._deferred_start_timer.start()
            self.play_both_btn.setText("联动 ▶")
        else:
            for pane in self.regular_panes:
                pane_location = regular_locations.get(pane.camera_index)
                if (
                    preserve_current_frame is not None
                    and pane.location is not None
                    and getattr(pane, "_current_frame_index", -1) >= 0
                ):
                    # Manual navigation establishes the active recording for
                    # continuous judging. Identity changes must stay on that
                    # recording even when the next rider's official timestamp
                    # resolves to the neighbouring archive segment.
                    current_media_location = self._location_on_current_media(
                        event, pane
                    )
                    # At a split boundary the new passage may not resolve to
                    # this segment at all. The loaded location still identifies
                    # the active recording and is valid for rebinding the
                    # passage while preserving the decoded frame.
                    if current_media_location is not None:
                        pane_location = current_media_location
                pane_association = self._source_association(
                    event.event_id,
                    REGULAR_SOURCE,
                    pane_location,
                )
                pane_same_media = (
                    pane.location is not None
                    and pane._media_context(pane_location)
                    == pane._media_context(pane.location)
                )
                if (reuse_continuous_media or preserve_current_frame is not None) and (
                    pane_same_media or preserve_current_frame is pane
                ):
                    pane.rebind_passage(
                        event,
                        pane_location,
                        pane_association,
                        lookup_status=(
                            pane_location.status
                            if pane_location is not None
                            else lookup.status
                        ),
                    )
                elif pane_location is not None and not pane_same_media:
                    pane.set_passage(
                        event,
                        pane_location,
                        pane_association,
                        initial_delta_ms=self._shared_delta_ms,
                        lookup_status=pane_location.status,
                        defer_worker_start=pane is not active_pane,
                    )
                elif pane_location is None and pane.location is not None:
                    pane.clear_passage()
                elif pane.association != pane_association:
                    pane.set_association(pane_association)
            if (
                self._show_high_speed_pane
            ):
                high_speed_same_media = (
                    self.high_speed_pane.location is not None
                    and self.high_speed_pane._media_context(high_speed)
                    == self.high_speed_pane._media_context(
                        self.high_speed_pane.location
                    )
                )
                if reuse_continuous_media and high_speed_same_media:
                    self.high_speed_pane.rebind_passage(
                        event,
                        high_speed,
                        high_speed_association,
                        lookup_status=(
                            high_speed.status if high_speed is not None else ""
                        ),
                    )
                elif high_speed is not None and not high_speed_same_media:
                    self.high_speed_pane.set_passage(
                        event,
                        high_speed,
                        high_speed_association,
                        initial_delta_ms=self._shared_delta_ms,
                        lookup_status=high_speed.status,
                        defer_worker_start=(
                            self._batch_mode
                            and self.high_speed_pane is not active_pane
                        ),
                    )
                elif self.high_speed_pane.association != high_speed_association:
                    self.high_speed_pane.set_association(high_speed_association)
        if active_pane in self.evidence_panes:
            self._activate_pane(active_pane, align=False)
        self._update_reference_states(event.event_id)
        if preserve_current_frame is not None:
            # In continuous judging the operator-selected current recording is
            # the authoritative source, even when an older association points
            # at the neighbouring archive segment. Keep it editable so the
            # next rider can be marked and confirmed without pressing Target.
            for pane in self.regular_panes:
                if pane.location is not None and pane.association is None:
                    pane.set_reference_only(False)
        if same_batch_media:
            self._update_shared_from_pane(active_pane)
            if preserve_current_frame is None:
                self._skip_continuous_gap(event, active_pane)
        self._update_batch_controls(event.event_id)
        self._update_navigation_controls()
        self._refresh_camera_judgments()
        if not self._batch_mode and self._filmstrip_context is not None:
            # Avoid blocking the UI while the previous strip worker is being
            # stopped; let the inactive camera start first on low-end PCs.
            if self.playback_coordinator is not None:
                self.playback_coordinator.request_filmstrip_update(deferred=True)
        elif self.playback_coordinator is not None:
            self.playback_coordinator.request_filmstrip_update(deferred=False)
        else:
            self._update_filmstrip()

    def _update_navigation_controls(self) -> None:
        row = self.table.currentRow()
        self.previous_passage_btn.setEnabled(row > 0)
        self.next_passage_btn.setEnabled(0 <= row < self.table.rowCount() - 1)

    def _clear_selection_details(self) -> None:
        self._set_sync_playing(False, seek_final=False)
        self._deferred_start_timer.stop()
        self._deferred_selection_panes.clear()
        self._selection_pending_panes.clear()
        self._filmstrip_preview_timer.stop()
        self._pending_filmstrip_preview_position = None
        self._pending_filmstrip_position = None
        self._shared_delta_ms = 0
        self._selected_event_id = ""
        metadata = self._current_metadata()
        self.race_value.setText(
            (metadata.race_name.strip() or metadata.race_id)
            if metadata is not None
            else "--"
        )
        self.stage_value.setText(
            (metadata.stage_name.strip() or metadata.stage_id)
            if metadata is not None
            else "--"
        )
        selected_group = str(self.group_combo.currentData() or "")
        self.group_value.setText(
            metadata.group_label(selected_group)
            if metadata is not None and selected_group
            else "--"
        )
        for label in (
            self.selected_identity_value,
            self.athlete_value,
            self.team_value,
            self.selected_time_value,
            self.source_value,
        ):
            label.setText("--")
        self.current_passage_label.setText("未选择通过记录")
        self.current_context_label.clear()
        self.current_time_label.setText("--:--:--.---")
        self.current_time_label.setToolTip("")
        self.batch_context_label.clear()
        self.batch_context_label.setToolTip("")
        self.batch_review_btn.setVisible(False)
        self.video_discovered_btn.setEnabled(False)
        self.video_discovered_table.setVisible(False)
        for pane in self.evidence_panes:
            pane.clear_passage()
        self.preview_video_view.clear_frame("选择一条通过记录后在这里预览判读")
        self.preview_confirm_btn.setEnabled(False)
        self._update_navigation_controls()

    def focus_athlete(
        self,
        race_id: str,
        stage_id: str,
        *,
        athlete_id: str = "",
        bib: str = "",
        group_id: str = "",
    ) -> bool:
        """Select the newest passage or show roster-only details without a modal."""

        race_id = str(race_id).strip()
        stage_id = str(stage_id).strip()
        athlete_id = str(athlete_id).strip()
        bib = str(bib).strip()
        group_id = str(group_id).strip()
        metadata = self._current_metadata()
        if metadata is not None and (
            metadata.race_id != race_id or metadata.stage_id != stage_id
        ):
            return False

        events = tuple(
            event
            for event in self._events_for_current_metadata(self.passage_store.events())
            if event.race_id == race_id and event.stage_id == stage_id
        )
        athlete_matches = (
            [event for event in events if athlete_id and event.athlete_id == athlete_id]
            if athlete_id
            else []
        )
        matches = athlete_matches or [
            event for event in events if bib and event.bib.strip() == bib
        ]
        if matches:
            event = max(
                matches,
                key=lambda item: (
                    item.timeline_timestamp_ms,
                    item.sequence,
                    item.revision,
                ),
            )
            target_group = event.group_id or group_id
            group_index = self.group_combo.findData(target_group)
            if group_index >= 0 and group_index != self.group_combo.currentIndex():
                self.group_combo.setCurrentIndex(group_index)
            row = next(
                (
                    index
                    for index, visible in enumerate(self._visible_events)
                    if visible.event_id == event.event_id
                ),
                -1,
            )
            if row < 0:
                self.refresh()
                row = next(
                    (
                        index
                        for index, visible in enumerate(self._visible_events)
                        if visible.event_id == event.event_id
                    ),
                    -1,
                )
            if row < 0:
                return False
            self.table.blockSignals(True)
            self.table.setCurrentCell(row, 1)
            self.table.selectRow(row)
            self.table.blockSignals(False)
            item = self.table.item(row, 1)
            if item is not None:
                self.table.scrollToItem(item, QAbstractItemView.PositionAtCenter)
            self._select_event(event.event_id)
            return True

        roster_athlete = None
        if metadata is not None:
            for candidate in metadata.athletes:
                if athlete_id and candidate.athlete_id == athlete_id:
                    roster_athlete = candidate
                    break
            if roster_athlete is None and bib:
                roster_athlete = next(
                    (
                        candidate
                        for candidate in metadata.athletes
                        if candidate.bib.strip() == bib
                    ),
                    None,
                )
        target_group = (
            roster_athlete.group_id if roster_athlete is not None else group_id
        )
        group_index = self.group_combo.findData(target_group)
        if group_index >= 0 and group_index != self.group_combo.currentIndex():
            self.group_combo.setCurrentIndex(group_index)
        self.table.blockSignals(True)
        self.table.clearSelection()
        self.table.setCurrentCell(-1, -1)
        self.table.blockSignals(False)
        self._clear_selection_details()

        identity = (
            roster_athlete.bib.strip()
            if roster_athlete is not None
            else bib
        )
        athlete_name = (
            roster_athlete.name.strip() if roster_athlete is not None else ""
        )
        team_name = (
            roster_athlete.team_name.strip() if roster_athlete is not None else ""
        )
        if metadata is not None:
            self.race_value.setText(metadata.race_name.strip() or metadata.race_id)
            self.stage_value.setText(metadata.stage_name.strip() or metadata.stage_id)
            self.group_value.setText(
                metadata.group_label(target_group) if target_group else "--"
            )
        self.selected_identity_value.setText(identity or "--")
        self.athlete_value.setText(athlete_name or "--")
        self.team_value.setText(team_name or "--")
        self.selected_time_value.setText("尚无通过记录")
        self.source_value.setText("等待 CycleRace 通过时间")
        athlete_summary = f"{identity} {athlete_name}".strip()
        self.current_passage_label.setText(
            f"名单运动员 {athlete_summary}（尚无通过记录）"
        )
        return bool(roster_athlete is not None or identity)

    def _begin_marking(self, pane: PassageEvidencePane) -> None:
        self._set_sync_playing(False)
        for candidate in self.evidence_panes:
            if candidate is not pane:
                candidate.cancel_marker_edit()
        pane.begin_marking()

    def _update_reference_states(self, event_id: str) -> None:
        confirmed = any(
            self._association_for_event(event_id, source_kind) is not None
            for source_kind in (REGULAR_SOURCE, HIGH_SPEED_SOURCE)
        )
        for pane in self.evidence_panes:
            pane.set_reference_only(confirmed and pane.association is None)

    def _same_frame_confirmations(self, event, source_kind, location, pending) -> list[str]:
        """Other bibs already confirmed on this frame at nearly the same spot.

        Frames are compared by recorder time (segment start + position) so a
        short clip and the archive of the same moment count as one frame.
        """

        segment = location.segment
        if segment.media_started_at_ms is None:
            return []
        frame_time = int(segment.media_started_at_ms) + int(pending["position_ms"])
        x, y = float(pending["marker_x_normalized"]), float(pending["marker_y_normalized"])
        conflicts = []
        for association in self.association_store.associations():
            if (
                association.passage_event_id == event.event_id
                or association.confirmed_source != source_kind
                or association.confirmation_status != "confirmed"
            ):
                continue
            other = self.timeline_store.get_segment(association.segment_id)
            if other is None or other.media_started_at_ms is None:
                continue
            other_time = int(other.media_started_at_ms) + int(association.position_ms)
            distance = ((association.marker_x_normalized - x) ** 2 + (association.marker_y_normalized - y) ** 2) ** 0.5
            if abs(other_time - frame_time) <= 25 and distance <= self.SAME_FRAME_MARKER_DISTANCE:
                conflicts.append(association.bib)
        return conflicts

    def _confirm_pending_marker(self, pane: PassageEvidencePane) -> bool:
        event = self.passage_store.get(self._selected_event_id)
        pending = pane.pending_confirmation()
        if pending is None:
            return False
        if bool(pending.get("unknown_marker")):
            location = pane.location
            if location is None:
                return False
            try:
                location = self._persist_filmstrip_location(location)
                pane._location = location
                pending["segment_id"] = location.segment.segment_id
                self.continuous_marker_store.create(
                    camera_index=max(1, int(location.segment.camera_index)),
                    segment_id=str(pending["segment_id"]),
                    frame_index=int(pending["frame_index"]),
                    position_ms=int(pending["position_ms"]),
                    marker_x_normalized=float(pending["marker_x_normalized"]),
                    marker_y_normalized=float(pending["marker_y_normalized"]),
                    label=str(pending.get("marker_label") or "待补录"),
                    passage_event_id=(event.event_id if event is not None else ""),
                    bib=(event.bib.strip() if event is not None else ""),
                    confirmed_at_ms=int(time.time() * 1000.0),
                )
            except (OSError, RuntimeError, TypeError, ValueError) as error:
                QMessageBox.critical(self, "保存失败", f"无法保存待补录标记：{error}")
                return False
            pane.cancel_marker_edit()
            self.video_filmstrip.clear_marker()
            self.preview_mark_btn.setChecked(False)
            self.preview_confirm_btn.setEnabled(False)
            self._refresh_camera_judgments()
            return True
        if event is None:
            return False
        pane_event = getattr(pane, "_event", None)
        if (pane_event is None or pane_event.event_id != event.event_id
                or pane_event.revision != event.revision or not event.is_active):
            logger.error(
                "Refused mismatched confirmation selected=%s pane=%s",
                event.event_id,
                getattr(pane_event, "event_id", ""),
            )
            return False
        identity = event.bib.strip() or "未知"
        is_rejudgment = (
            self._association_for_event(event.event_id, pane.source_kind) is not None
        )
        try:
            location = self._persist_filmstrip_location(pane.location)
            if location is None:
                return False
            pane._location = location
            pending["segment_id"] = location.segment.segment_id
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            QMessageBox.critical(self, "保存失败", f"无法保存证据标记：{error}")
            return False
        conflicts = self._same_frame_confirmations(event, pane.source_kind, location, pending)
        if conflicts and QMessageBox.question(
            self, "同一帧已确认其他号码",
            f"该帧同一位置已确认 {'、'.join(conflicts)}。\n"
            f"再确认 {identity} 前请核对号码牌，是否仍要确认？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            return False
        try:
            association = self.association_store.confirm(
                passage_event_id=event.event_id,
                bib=identity,
                confirmed_source=pane.source_kind,
                segment_id=str(pending["segment_id"]),
                frame_index=int(pending["frame_index"]),
                position_ms=int(pending["position_ms"]),
                marker_x_normalized=float(pending["marker_x_normalized"]),
                marker_y_normalized=float(pending["marker_y_normalized"]),
                confirmed_at_ms=int(time.time() * 1000.0),
                passage_revision=event.revision,
            )
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            QMessageBox.critical(self, "保存失败", f"无法保存证据标记：{error}")
            return False
        confirmed_row = self.table.currentRow()
        pane.set_association(association)
        self._calibrate_continuous_session(event, pane, association)
        for candidate in self.evidence_panes:
            if candidate is pane or candidate.source_kind != pane.source_kind:
                continue
            candidate.set_association(None)
        self._update_reference_states(event.event_id)
        should_advance = (self._batch_mode and not is_rejudgment) or (
            self.auto_advance_checkbox.isChecked()
            and self._all_available_sources_confirmed(event.event_id)
        )
        self._update_event_confirmation_status(event.event_id)
        if should_advance and confirmed_row >= 0:
            self._move_selection(
                1,
                skip_confirmed=True,
                preserve_current_frame=True,
            )
        return True

    def _update_current_context(self, display_status: str) -> None:
        row = self.table.currentRow()
        position = row + 1 if 0 <= row < len(self._visible_events) else 0
        self.current_context_label.setText(" · ".join(
            value for value in (self.group_value.text().strip(),
                                f"{position:,} / {len(self._visible_events):,}", display_status)
            if value and value != "--"
        ))

    def _update_event_confirmation_status(self, event_id: str) -> None:
        self._refresh_camera_judgments()
        lookup = self._lookups.get(event_id)
        if lookup is None:
            return
        regular = self._source_location_with_saved_association(
            event_id,
            lookup,
            high_speed=False,
        )
        high_speed = self._source_location_with_saved_association(
            event_id,
            lookup,
            high_speed=True,
        )
        regular_association = self._association_for_event(event_id, REGULAR_SOURCE)
        high_speed_association = self._association_for_event(event_id, HIGH_SPEED_SOURCE)
        readiness_status = review_status_text(lookup, regular, high_speed)
        status = self._confirmation_status(
            regular_association,
            high_speed_association,
            readiness_status,
        )
        self._event_review_statuses[event_id] = status
        self._record_summary_state(
            event_id,
            regular,
            high_speed,
            readiness_status,
            regular_association,
            high_speed_association,
        )
        row_values = (
            (6, source_confirmation_status(regular, regular_association)),
            (7, source_confirmation_status(high_speed, high_speed_association)),
            (8, self._display_confirmation_status(status)),
        )
        for row, event in enumerate(self._visible_events):
            if event.event_id != event_id:
                continue
            for column, value in row_values:
                item = self.table.item(row, column)
                if item is not None:
                    item.setText(value)
                    self._apply_status_style(item, value)
            break
        if event_id == self._selected_event_id:
            self.source_value.setText(self._display_confirmation_status(status))
            self._update_current_context(self._display_confirmation_status(status))
        batch = self._active_review_batch()
        if self._batch_mode and batch is not None:
            self._update_batch_roster_overlays(batch)
        if self._active_review_filter != "all":
            self._refresh_filtered_view()
            return
        self._render_summary()

    def _record_summary_state(
        self,
        event_id: str,
        regular: Optional[PassageVideoLocation],
        high_speed: Optional[PassageVideoLocation],
        _readiness_status: str,
        regular_association: Optional[PassageEvidenceAssociation],
        high_speed_association: Optional[PassageEvidenceAssociation],
    ) -> None:
        self._set_membership(
            self._located_event_ids,
            event_id,
            any(
                location is not None and location.status in _OPENABLE_STATUSES
                for location in (regular, high_speed)
            ),
        )
        self._set_membership(
            self._confirmed_event_ids,
            event_id,
            regular_association is not None or high_speed_association is not None,
        )

    @staticmethod
    def _set_membership(values: set[str], event_id: str, present: bool) -> None:
        if present:
            values.add(event_id)
        else:
            values.discard(event_id)

    def _discard_summary_state(self, event_id: str) -> None:
        self._located_event_ids.discard(event_id)
        self._confirmed_event_ids.discard(event_id)
        self._event_review_statuses.pop(event_id, None)

    def _render_summary(self) -> None:
        counts = {
            "pending": sum(
                status == "待核对" for status in self._event_review_statuses.values()
            ),
            "blocked": sum(
                status == "受阻" for status in self._event_review_statuses.values()
            ),
            "confirmed": sum(
                status == "已确认" for status in self._event_review_statuses.values()
            ),
            "all": self._total_event_count,
        }
        for filter_key, button in self.review_filter_buttons.items():
            prefix = "✓ " if filter_key == self._active_review_filter else ""
            button.setText(
                f"{prefix}{self.review_filter_labels[filter_key]} "
                f"{counts[filter_key]:,}"
            )
        active_label = self.review_filter_labels[self._active_review_filter]
        if self._active_review_filter == "all":
            summary = (
                f"当前显示 {len(self._visible_events):,} / "
                f"{self._total_event_count:,} 条"
            )
        else:
            summary = (
                f"当前筛选：{active_label} · "
                f"{len(self._visible_events):,} / {self._total_event_count:,} 条"
            )
        self.summary_label.setText(summary)
        self._available_evidence_count = len(self._located_event_ids)
        self.summary_label.setText(
            f"{summary} · 可回看 {self._available_evidence_count:,}"
        )
        self.summary_label.setToolTip("可回看表示录像已可用；是否完成判读以已确认为准。")

    def _all_available_sources_confirmed(self, event_id: str) -> bool:
        lookup = self._lookups.get(event_id)
        if lookup is None:
            return False
        for location in lookup.locations:
            if location.status not in _OPENABLE_STATUSES:
                continue
            source_kind = HIGH_SPEED_SOURCE if is_high_speed(location) else REGULAR_SOURCE
            if self._source_association(event_id, source_kind, location) is not None:
                return True
        return False

    def _cancel_pending_marker(self, pane: PassageEvidencePane) -> None:
        pane.cancel_marker_edit()

    def _delete_marker(self, pane: PassageEvidencePane) -> None:
        association = pane.association
        if association is None:
            pane.cancel_marker_edit()
            return
        answer = QMessageBox.question(
            self,
            "删除证据标记",
            f"确定删除 {association.bib} 号在{pane.title_label.text()}中的人工标记吗？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        try:
            self.association_store.clear(
                association.passage_event_id,
                association.confirmed_source,
                confirmed_at_ms=int(time.time() * 1000.0),
            )
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            QMessageBox.critical(self, "删除失败", f"无法删除证据标记：{error}")
            return
        pane.set_association(None)
        self._update_reference_states(association.passage_event_id)
        self._update_event_confirmation_status(association.passage_event_id)

    def _find_identity(self) -> None:
        value = self.identity_search.text().strip().casefold()
        if not value:
            return
        matches = []
        for row, event in enumerate(self._visible_events):
            metadata_athlete = self._metadata_athlete_for_event(event)
            athlete_name = event.athlete_name.strip() or (
                metadata_athlete.name.strip() if metadata_athlete is not None else ""
            )
            if value in {
                event.bib.strip().casefold(),
                athlete_name.casefold(),
            }:
                matches.append((row, event))
        if matches:
            row, _event = max(
                matches,
                key=lambda match: (
                    match[1].timeline_timestamp_ms,
                    match[1].sequence,
                    match[1].revision,
                ),
            )
            self.table.setCurrentCell(row, 1)
            self.table.selectRow(row)
            item = self.table.item(row, 1)
            if item is not None:
                self.table.scrollToItem(item, QAbstractItemView.PositionAtCenter)
            return
        metadata = self._current_metadata()
        if metadata is not None:
            selected_group = str(self.group_combo.currentData() or "")
            for athlete in metadata.athletes:
                if selected_group and athlete.group_id != selected_group:
                    continue
                if not (
                    athlete.matches_identity(value)
                    or athlete.name.strip().casefold() == value
                ):
                    continue
                identity = athlete.bib.strip() or "未知"
                self.race_value.setText(metadata.race_name or metadata.race_id)
                self.stage_value.setText(metadata.stage_name or metadata.stage_id)
                self.group_value.setText(metadata.group_label(athlete.group_id))
                self.selected_identity_value.setText(identity)
                self.athlete_value.setText(athlete.name.strip() or "--")
                self.team_value.setText(athlete.team_name.strip() or "--")
                self.selected_time_value.setText("尚无通过记录")
                self.source_value.setText("等待 CycleRace 通过时间")
                athlete_summary = f"{identity} {athlete.name}".strip()
                self.current_passage_label.setText(
                    f"名单运动员 {athlete_summary}（尚无通过记录）"
                )
                return
        QMessageBox.information(self, "未找到", "当前组别没有该号码或姓名的通过记录。")

    def _move_selection(
        self,
        delta: int,
        *,
        skip_confirmed: bool = False,
        preserve_current_frame: bool = False,
    ) -> None:
        if self.table.rowCount() <= 0:
            return
        current_row = self.table.currentRow()
        direction = 1 if int(delta) >= 0 else -1
        row = current_row + int(delta)
        if skip_confirmed:
            while 0 <= row < self.table.rowCount():
                candidate = self._visible_events[row]
                has_evidence = (
                    self._association_for_event(candidate.event_id, REGULAR_SOURCE)
                    is not None
                    or self._association_for_event(
                        candidate.event_id,
                        HIGH_SPEED_SOURCE,
                    )
                    is not None
                )
                if not has_evidence:
                    break
                row += direction
            if not 0 <= row < self.table.rowCount():
                return
        row = max(0, min(row, self.table.rowCount() - 1))
        if row == self.table.currentRow():
            return
        event_id = self._visible_events[row].event_id
        self.table.blockSignals(True)
        self.table.setCurrentCell(row, 1)
        self.table.selectRow(row)
        self.table.blockSignals(False)
        item = self.table.item(row, 1)
        if item is not None:
            self.table.scrollToItem(item, QAbstractItemView.PositionAtCenter)
        active_pane = self._active_playback_pane()
        moving_back_in_continuous_mode = self._batch_mode and int(delta) < 0
        keep_current_frame = preserve_current_frame or (
            getattr(active_pane, "_current_frame_index", -1) >= 0
            and self._selection_is_near_current_event(event_id)
        )
        self._select_event(
            event_id,
            preserve_current_frame=(active_pane if keep_current_frame else None),
        )
        if not moving_back_in_continuous_mode:
            return
        if self._seek_saved_confirmation(event_id):
            return
        event = self.passage_store.get(event_id)
        if event is not None:
            self._skip_continuous_gap(event, active_pane)

    def _selection_is_near_current_event(self, event_id: str) -> bool:
        current = self.passage_store.get(self._selected_event_id)
        target = self.passage_store.get(event_id)
        if current is None or target is None:
            return False
        return (
            abs(
                int(target.timeline_timestamp_ms)
                - int(current.timeline_timestamp_ms)
            )
            <= self.MANUAL_CONTINUATION_WINDOW_MS
        )

    def _seek_saved_confirmation(self, event_id: str) -> bool:
        association = self._association_for_event(event_id, REGULAR_SOURCE)
        if association is None:
            return False
        segment = self.timeline_store.get_segment(association.segment_id)
        pane = (
            self._pane_for_camera(segment.camera_index)
            if segment is not None
            else self._active_playback_pane()
        )
        if pane.location is None or pane.location.segment.segment_id != association.segment_id:
            event = self.passage_store.get(event_id)
            location = self._saved_regular_location(association)
            if (
                event is None
                or location is None
                or location.status not in _OPENABLE_STATUSES
            ):
                return False
            self._activate_pane(pane, align=False)
            pane.set_passage(event, location, association)
            self._shared_delta_ms = 0
            self._update_reference_states(event_id)
            self._update_shared_time_label()
            if self.playback_coordinator is not None:
                self.playback_coordinator.request_filmstrip_update(deferred=True)
            else:
                self._update_filmstrip()
            return True
        self._activate_pane(pane, align=False)
        self._shared_delta_ms = (
            int(association.position_ms) - int(pane._target_position_ms)
        )
        if pane.available_delta_bounds() is not None:
            pane.seek_passage_delta(self._shared_delta_ms)
        self._update_shared_time_label()
        return True

    def _open_preferred_source(self, row: int, _column: int) -> None:
        if not (0 <= row < len(self._visible_events)):
            return
        event = self._visible_events[row]
        # A single row selection deliberately keeps the current frame so an
        # operator can scan continuously without skipping an unchipped rider.
        # A double-click is an explicit request to locate this passage on the
        # continuous camera recording.  It must not open the old short
        # evidence clip; that remains an explicit pane-button action.
        self._select_event(event.event_id, locate_target=True)
        # A race configured with no ordinary camera has no continuous target
        # to locate.  Preserve the established high-speed-only affordance by
        # maximising that pane, without invoking the generic short-clip
        # callback used by the ordinary-camera workflow.
        regular_available = any(
            pane.location is not None
            and pane.location.status in _OPENABLE_STATUSES
            for pane in self.regular_panes
        )
        if (
            regular_available
            and self._uses_popup_judging()
            and self._maximized_window is None
        ):
            self._toggle_maximized_pane(self._camera_one_pane())
        if not regular_available:
            lookup = self._lookups.get(event.event_id)
            high_speed = (
                source_location(lookup, high_speed=True)
                if lookup is not None
                else None
            )
            if high_speed is not None and high_speed.status in _OPENABLE_STATUSES:
                self._toggle_maximized_pane(self.high_speed_pane)
            elif self._uses_popup_judging():
                self.video_filmstrip.full_race.status_label.setText(
                    "该通过时刻暂无录像，请先检查摄像头连接；芯片记录已保留。"
                )

    def _open_location_if_available(
        self,
        event: PassageEvent,
        location: PassageVideoLocation,
    ) -> None:
        if is_high_speed(location):
            if self._maximized_pane is not self.high_speed_pane:
                self._toggle_maximized_pane(self.high_speed_pane)
            return
        if self._open_location is not None:
            self._open_location(event, location)

    def _open_row(self, row: int, column: int) -> None:
        self._open_preferred_source(row, column)

    def _open_event(self, event_id: str) -> None:
        self._open_event_location(event_id, "")

    def _open_event_location(self, event_id: str, segment_id: str) -> None:
        event = self.passage_store.get(event_id)
        if event is None:
            self.refresh()
            return
        lookup = self._lookups.get(event_id) or self._cached_lookup(event)
        available = [
            item for item in lookup.locations if item.status in _OPENABLE_STATUSES
        ]
        if not available:
            QMessageBox.information(self, "无法定位", lookup_status_text(lookup))
            return
        location = next(
            (
                item
                for item in available
                if not segment_id or item.segment.segment_id == segment_id
            ),
            None,
        )
        if location is None:
            QMessageBox.information(self, "无法定位", "该证据已变化，请刷新后重试。")
            self.refresh()
            return
        self._open_location_if_available(event, location)
