"""The finish-line filmstrip of the passage review surface: windows, markers, preview and camera judgments."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import QMessageBox

from .camera_judgments import CameraJudgment
from .race_filmstrip import RaceFilmstripFrame, recording_sources
from .passage_evidence import REGULAR_SOURCE
from .passage_receiver import PassageEvent
from .person_follow import FOLLOW_AFTER_MS, FOLLOW_BEFORE_MS, FollowTracks, follow_path
from .person_follow import OUTPUT_DIRNAME as FOLLOW_OUTPUT_DIRNAME
from .video_timeline import DEFAULT_CLOCK_SOURCE, PassageVideoLocation
from .passage_review_common import _CONFIRMABLE_STATUSES, _OPENABLE_STATUSES, format_passage_time
from .passage_evidence_pane import PassageEvidencePane


class PassageReviewFilmstripMixin:
    """Filmstrip window and coverage, its markers and preview, and camera judgments."""

    def _toggle_filmstrip_size(self):
        splitter = self.review_content_splitter
        self._review_split_manually_sized = True
        if self._filmstrip_restore_sizes is None:
            self._filmstrip_restore_sizes = splitter.sizes()
            total = sum(splitter.sizes())
            lower = self.workspace_splitter.minimumHeight()
            splitter.setSizes([max(420, total - lower), lower])
            self.video_filmstrip.full_race.enlarge_button.setText("还原布局")
        else:
            splitter.setSizes(self._filmstrip_restore_sizes)
            self._filmstrip_restore_sizes = None
            self.video_filmstrip.full_race.enlarge_button.setText("放大胶卷")

    def _live_location_for_filmstrip(
        self,
        pane: PassageEvidencePane,
        anchor_time_ms: int,
    ) -> Optional[PassageVideoLocation]:
        """Return a live rolling source when a concrete window provides one."""

        return None

    def _live_locations_for_filmstrip(self, pane) -> tuple[PassageVideoLocation, ...]:
        return ()

    def _persist_filmstrip_location(
        self, location: PassageVideoLocation,
    ) -> PassageVideoLocation:
        """Give a window implementation a chance to make live media durable."""

        return location

    def _archive_location_for_filmstrip(
        self,
        pane: PassageEvidencePane,
        anchor_time_ms: int,
    ) -> Optional[PassageVideoLocation]:
        event = self.passage_store.get(self._selected_event_id)
        metadata = self._current_metadata()
        race_id = (
            event.race_id
            if event is not None
            else metadata.race_id if metadata is not None else None
        )
        try:
            lookup = self.timeline_store.locate_passage(
                int(anchor_time_ms),
                race_id=race_id,
                prefer_continuous_media=True,
            )
        except (OSError, RuntimeError, ValueError):
            return None
        return next(
            (
                location
                for location in lookup.locations
                if int(location.segment.camera_index) == int(pane.camera_index)
                and location.segment.clock_source == DEFAULT_CLOCK_SOURCE
                and not str(location.segment.segment_id).startswith("preview-")
                and location.status
                in {"located", "near_boundary", "unverified", "recording"}
                and self.timeline_store.video_path_is_playable(location.video_path)
            ),
            None,
        )

    def _filmstrip_window_coverage(
        self,
        pane: PassageEvidencePane,
        location: PassageVideoLocation,
        absolute_start_ms: int,
        absolute_end_ms: int,
    ) -> tuple[bool, str]:
        """Validate the requested continuous window before decoding it.

        A timeline lookup can locate an athlete at either side of a recorder
        gap, but that must never make the missing middle look like ordinary
        empty video.  Build the camera/source interval union from the durable
        timeline journal and reject an internal gap.  We currently decode one
        media path per active filmstrip; contiguous entries on another path
        therefore remain an explicit incomplete-evidence state instead of
        being silently stitched by the UI.
        """

        start = int(absolute_start_ms)
        end = max(start, int(absolute_end_ms))
        if end <= start:
            return True, ""
        selected = location.segment
        tolerance = max(
            int(self.FILMSTRIP_COVERAGE_TOLERANCE_MS),
            int(getattr(selected, "timing_error_ms", 0) or 0),
        )
        intervals: list[tuple[int, int, Path, object]] = []
        for segment in self.timeline_store.segments():
            if int(segment.camera_index) != int(pane.camera_index):
                continue
            if str(segment.source_id) != str(selected.source_id):
                continue
            if selected.race_id and not (
                segment.race_id == selected.race_id
                or (
                    not segment.race_id
                    and segment.clock_source == DEFAULT_CLOCK_SOURCE
                )
            ):
                continue
            if segment.media_started_at_ms is None or segment.media_duration_ms is None:
                continue
            interval_start = int(segment.media_started_at_ms)
            interval_end = interval_start + int(segment.media_duration_ms)
            if interval_end <= start - tolerance or interval_start >= end + tolerance:
                continue
            try:
                path = self.timeline_store.resolve_video_path(segment)
            except (OSError, RuntimeError, ValueError):
                path = Path(segment.video_path)
            intervals.append((interval_start, interval_end, Path(path), segment))
        # Live rolling HLS locations are synthetic and therefore are not yet
        # present in the durable journal.  Their own interval is still valid
        # for the active window; later archive publication will replace it.
        if (
            selected.media_started_at_ms is not None
            and selected.media_duration_ms is not None
            and not any(item[3] is selected for item in intervals)
        ):
            selected_start = int(selected.media_started_at_ms)
            selected_end = selected_start + int(selected.media_duration_ms)
            intervals.append(
                (selected_start, selected_end, Path(location.video_path), selected)
            )
        if not intervals:
            return False, "当前窗口没有可验证的连续录像时间线"
        intervals.sort(key=lambda item: (item[0], item[1]))
        cursor = start
        selected_path = Path(location.video_path)
        for interval_start, interval_end, path, _segment in intervals:
            # Segments ending exactly at the cursor are already exhausted and
            # must not be compared as a new path; otherwise a clean boundary
            # is falsely reported as a cross-file gap.
            if interval_end <= cursor:
                continue
            if interval_start > cursor + tolerance:
                return (
                    False,
                    f"录像断点：{cursor} ms 至 {interval_start} ms 未覆盖",
                )
            if path != selected_path:
                return (
                    False,
                    "当前两分钟窗口跨越多个录像文件，尚未安全连续拼接",
                )
            cursor = max(cursor, interval_end)
            if cursor >= end - tolerance:
                return True, ""
        if cursor >= end - tolerance:
            return True, ""
        # A final, naturally shorter tail is safe to show as a partial window;
        # an internal gap would have returned above when a following segment
        # existed.  The caller labels this state instead of claiming a full
        # two-minute window.
        latest_end = max(item[1] for item in intervals)
        if latest_end <= cursor + tolerance:
            return True, "录像已到末尾，当前窗口不足两分钟"
        return False, f"录像断点：{cursor} ms 至 {end} ms 未覆盖"

    def _filmstrip_context_for_active_pane(
        self,
    ) -> Optional[tuple[Path, int, int, int, tuple[int, ...], int, int]]:
        self._filmstrip_gap_message = ""
        if not self._batch_mode and not self.FILMSTRIP_ALWAYS_AVAILABLE:
            return None
        pane = self._camera_one_pane()
        location = pane.location
        selected_event = self.passage_store.get(self._selected_event_id)
        offset_ms = self._continuous_offset_for_location(location)
        event_anchor_ms = (
            int(selected_event.timeline_timestamp_ms) + int(offset_ms)
            if selected_event is not None
            else None
        )
        candidate_anchor_ms = getattr(
            self,
            "_video_candidate_anchor_time_ms",
            None,
        )
        anchor_time_ms = (
            int(candidate_anchor_ms)
            if candidate_anchor_ms is not None
            else event_anchor_ms
        )
        is_preview_location = bool(
            location is not None
            and (
                str(getattr(location.segment, "segment_id", "")).startswith(
                    "preview-"
                )
                or location.video_path.suffix.lower() == ".m3u8"
            )
        )
        # The top rail is a continuous camera-1 locator. A persisted short
        # evidence clip (non-preview media_locator) must never become its
        # source merely because it is the current pane binding.
        is_short_evidence_clip = bool(
            location is not None
            and str(getattr(location, "media_locator", "")).strip()
            and not is_preview_location
        )
        if (
            anchor_time_ms is not None
            and (location is None or is_preview_location or is_short_evidence_clip)
            # Directly bound review clips are intentionally kept self-contained
            # outside continuous batch mode; do not trigger a timeline scan in
            # that workflow just to populate an optional top rail.
            and not (
                is_short_evidence_clip
                and not self._batch_mode
                and self.review_binding_store is not None
            )
        ):
            live_location = self._live_location_for_filmstrip(
                pane,
                anchor_time_ms,
            )
            archive_location = (
                None
                if live_location is not None
                else self._archive_location_for_filmstrip(
                    pane,
                    anchor_time_ms,
                )
            )
            if live_location is not None:
                location = live_location
            elif archive_location is not None:
                location = archive_location
        if location is None or not location.video_path.is_file():
            return None
        duration_ms = int(
            location.segment.media_duration_ms
            or getattr(pane, "_duration_ms", 0)
            or 0
        )
        if duration_ms <= 0:
            return None
        media_start_ms = (
            int(location.segment.media_started_at_ms)
            if location.segment.media_started_at_ms is not None
            else int(location.segment.started_at_ms)
        )
        media_end_ms = media_start_ms + duration_ms
        positions: list[int] = []
        for event in self._visible_events:
            event_time_ms = int(event.timeline_timestamp_ms) + int(offset_ms)
            if media_start_ms <= event_time_ms <= media_end_ms:
                positions.append(event_time_ms - media_start_ms)
        for candidate in self._video_navigation_candidates:
            if int(getattr(candidate, "camera_index", 0)) != int(pane.camera_index):
                continue
            candidate_time_ms = int(getattr(candidate, "peak_at_ms", 0))
            if media_start_ms <= candidate_time_ms <= media_end_ms:
                # Candidate files are short HLS pieces, while the judgment pane
                # commonly plays a chip-centered preview playlist. Map by the
                # shared absolute recorder clock instead of segment IDs.
                positions.append(candidate_time_ms - media_start_ms)
                continue
            if str(getattr(candidate, "segment_id", "")) != location.segment.segment_id:
                continue
            positions.append(int(getattr(candidate, "video_position_ms", 0)))
        if not positions:
            positions.append(int(location.passage_position_ms))
        # Before the playback worker emits its first frame, the pane reports
        # position 0 even though the selected athlete's target is later in
        # the recording. Use the target passage frame in that transient state
        # so the filmstrip's rightmost 0-second tile shows the athlete rather
        # than an empty lead-in frame.
        current_frame_index = int(getattr(pane, "_current_frame_index", -1))
        current_position = int(getattr(pane, "_current_position_ms", 0))
        target_position = int(getattr(pane, "_target_position_ms", 0))
        if anchor_time_ms is not None:
            center = max(
                0,
                min(duration_ms - 1, int(anchor_time_ms) - media_start_ms),
            )
        else:
            center = (
                target_position
                if current_frame_index < 0
                else current_position
            )
        filmstrip_window_ms = (
            120_000 if self._low_resource_mode else self.FILMSTRIP_WINDOW_MS
        )
        requested_window_start = self._filmstrip_requested_window_start_ms
        requested_local_start = (
            int(requested_window_start) - media_start_ms
            if requested_window_start is not None
            else None
        )
        # The batch buttons request an exact absolute two-minute successor.
        # Honour that request when this media file contains the whole window;
        # otherwise retain the normal anchor-based window and let coverage
        # validation report a real cross-file/gap condition.
        if (
            requested_local_start is not None
            and duration_ms >= filmstrip_window_ms
            and 0 <= requested_local_start <= duration_ms - filmstrip_window_ms
        ):
            start_ms = requested_local_start
            end_ms = start_ms + filmstrip_window_ms
        else:
            window_start = (
                max(0, center) // filmstrip_window_ms
            ) * filmstrip_window_ms
            # Keep a full two-minute window whenever the media contains one.
            # The old duration clamp could silently shrink a window at a
            # segment boundary and make a recorder gap look like an empty
            # stretch.
            if duration_ms >= filmstrip_window_ms:
                start_ms = min(window_start, duration_ms - filmstrip_window_ms)
                end_ms = start_ms + filmstrip_window_ms
            else:
                start_ms = min(window_start, max(0, duration_ms - 1))
                end_ms = duration_ms
        if end_ms <= start_ms:
            end_ms = min(duration_ms, start_ms + 1_000)
        origin_ms = int(
            location.segment.media_started_at_ms
            if location.segment.media_started_at_ms is not None
            else location.segment.started_at_ms
        )
        absolute_window_start = (
            int(requested_window_start)
            if requested_local_start is not None
            and start_ms == requested_local_start
            else origin_ms + int(start_ms)
        )
        absolute_window_end = origin_ms + int(end_ms)
        covered, coverage_message = self._filmstrip_window_coverage(
            pane,
            location,
            absolute_window_start,
            absolute_window_start + int(filmstrip_window_ms),
        )
        if not covered:
            self._filmstrip_gap_message = coverage_message
            return None
        self._filmstrip_gap_message = coverage_message
        window_positions = tuple(
            position for position in positions if start_ms <= position <= end_ms
        )
        if not window_positions:
            window_positions = (max(start_ms, min(end_ms, center)),)
        return (
            location.video_path,
            start_ms,
            end_ms,
            center,
            window_positions,
            absolute_window_start,
            absolute_window_end,
        )

    def _filmstrip_race_id(self) -> str:
        metadata = self._current_metadata()
        if metadata is not None:
            return metadata.race_id
        race_ids = {item.race_id for item in self.passage_store.events() if item.race_id}
        return next(iter(race_ids)) if len(race_ids) == 1 else ""

    def _update_filmstrip(self) -> None:
        if not hasattr(self, "video_filmstrip"):
            return
        pane = self._camera_one_pane()
        race_id = self._filmstrip_race_id()
        workspace_key = (str(self.timeline_store.journal_path.absolute()), race_id)
        if workspace_key != getattr(self, "_race_filmstrip_workspace_key", None):
            self.video_filmstrip.full_race.clear()
            self._race_filmstrip_workspace_key = workspace_key
        provider = getattr(self, "_recording_sources_for_filmstrip", None)
        if provider is not None:
            sources, pending = provider(pane, race_id)
        else:
            live_locations = self._live_locations_for_filmstrip(pane)
            # There can be a short interval between starting the recorder and the
            # first playable HLS segment. Keep that interval visible as processing
            # instead of presenting it as a permanent recording gap.
            recorder_active_reader = getattr(self, "_recording_any_active", None)
            recording_processing = bool(
                callable(recorder_active_reader)
                and hasattr(self, "_recorders")
                and recorder_active_reader()
            )
            sources, pending = recording_sources(
                self.timeline_store, pane.camera_index, race_id,
                offset_for_location=self._continuous_offset_for_location,
                live_locations=live_locations,
            )
            if recording_processing and not live_locations:
                pending = max(1, pending)
        panel = self.video_filmstrip.full_race
        panel.set_check_context(self.timeline_store.journal_path.parent / "filmstrip_checks.jsonl",
                                race_id, pane.camera_index)
        panel.set_sources(sources, pending)
        if pane.location is not None and pane._current_frame_index >= 0:
            panel.set_current_frame(pane.location, pane._current_position_ms)
        panel.set_judgments(self._camera_judgment_records(pane), self._selected_event_id)

    def _open_race_filmstrip_frame(self, frame: RaceFilmstripFrame) -> bool:
        panel = self.video_filmstrip.full_race
        if frame.source.key not in {source.key for source in panel.index.sources}:
            return False
        if not frame.source.location.video_path.is_file():
            panel.status_label.setText("录像文件不可用，未移动机位 1。")
            return False
        if self._seek_retained_camera_frame(frame.source.location, frame.position_ms, frame.frame_index) is False:
            return False
        panel.set_current_frame(frame.source.location, frame.position_ms)
        self._refresh_camera_judgments()
        return True

    def _maximize_filmstrip_camera(self, frame: RaceFilmstripFrame) -> None:
        """F from the filmstrip keeps its exact frame and enlarges camera 1."""
        panel = self.video_filmstrip.full_race
        if frame.source.key not in {source.key for source in panel.index.sources}:
            return
        if not frame.source.location.video_path.is_file():
            panel.status_label.setText("录像文件不可用，无法打开判读。")
            return
        if not self._open_race_filmstrip_frame(frame):
            return
        pane = self._camera_one_pane()
        if self._maximized_pane is not pane:
            self._toggle_maximized_pane(pane)
        pane.video_view.setFocus(Qt.ShortcutFocusReason)

    def _uses_popup_judging(self) -> bool:
        return self._configured_regular_camera_indexes == (1,) and not self._show_high_speed_pane

    def _on_filmstrip_ready(self, ready: bool, _message: str) -> None:
        self._filmstrip_prefetch_timer.stop()
        if ready:
            if self._filmstrip_gap_message:
                self.video_filmstrip.status_label.setText(
                    f"{self._filmstrip_gap_message}；当前可处理"
                )
            # Let the operator receive the ready state first. The next window
            # is deliberately prepared later and never replaces the active one.
            if not self._filmstrip_operator_idle_timer.isActive():
                self._filmstrip_prefetch_timer.start()

    def _mark_filmstrip_operator_busy(self) -> None:
        if not hasattr(self, "video_filmstrip") or not hasattr(
            self, "_filmstrip_operator_idle_timer"
        ):
            return
        self.video_filmstrip.set_operator_busy(True)
        self._filmstrip_prefetch_timer.stop()
        self._filmstrip_operator_idle_timer.start()

    def _on_filmstrip_operator_idle(self) -> None:
        if not hasattr(self, "video_filmstrip"):
            return
        pane = self._active_playback_pane()
        if pane is not None and (
            bool(getattr(pane, "is_playing", False))
            or bool(getattr(pane, "_video_scrubbing", False))
        ):
            self._filmstrip_operator_idle_timer.start()
            return
        self.video_filmstrip.set_operator_busy(False)
        if self.video_filmstrip.is_ready:
            self._filmstrip_prefetch_timer.start()

    def _prefetch_next_filmstrip_window(self) -> None:
        if not hasattr(self, "video_filmstrip") or not self.video_filmstrip.is_ready:
            return
        context = self._filmstrip_context
        absolute_window = self._filmstrip_absolute_window
        if context is None or absolute_window is None:
            return
        pane = self._camera_one_pane()
        next_start_ms = int(absolute_window[1])
        next_end_ms = next_start_ms + int(self.FILMSTRIP_WINDOW_MS)
        location = self._live_location_for_filmstrip(
            pane,
            next_start_ms + int(self.FILMSTRIP_WINDOW_MS) // 2,
        ) or self._archive_location_for_filmstrip(
            pane,
            next_start_ms + int(self.FILMSTRIP_WINDOW_MS) // 2,
        )
        if location is None:
            return
        origin_ms = int(
            location.segment.media_started_at_ms
            if location.segment.media_started_at_ms is not None
            else location.segment.started_at_ms
        )
        duration_ms = int(location.segment.media_duration_ms or 0)
        if duration_ms <= 0:
            return
        start_position_ms = max(0, next_start_ms - origin_ms)
        end_position_ms = min(duration_ms, next_end_ms - origin_ms)
        if end_position_ms <= start_position_ms:
            return
        self.video_filmstrip.prefetch(
            location.video_path,
            start_position_ms,
            end_position_ms,
        )

    def _filmstrip_judgment_pane(self) -> Optional[PassageEvidencePane]:
        pane = self._camera_one_pane()
        if pane.location is None or pane.location.status not in _OPENABLE_STATUSES:
            return None
        return pane

    def _continuous_marker_positions_for_filmstrip(
        self,
        pane: PassageEvidencePane,
        absolute_start_ms: int,
        absolute_end_ms: int,
    ) -> tuple[tuple[int, str], ...]:
        """Return all confirmed/unknown markers inside the current strip window."""

        location = pane.location
        if location is None or pane.source_kind != REGULAR_SOURCE:
            return ()
        media_origin_ms = int(
            location.segment.media_started_at_ms
            if location.segment.media_started_at_ms is not None
            else location.segment.started_at_ms
        )
        return tuple(
            (record.recorder_time_ms - media_origin_ms, record.label)
            for record in self._camera_judgment_records(pane)
            if record.recorder_time_ms is not None
            and absolute_start_ms <= record.recorder_time_ms <= absolute_end_ms
        )

    def _camera_judgment_records(
        self, pane: PassageEvidencePane,
    ) -> tuple[CameraJudgment, ...]:
        # Use the full event scope, independently of roster search/group/status
        # filters and the current recording file or two-minute window.
        events = {
            event.event_id: event
            for event in self._events_for_current_metadata(self.passage_store.events())
        }
        metadata = self._current_metadata()
        records = []
        saved = [
            (f"event:{item.passage_event_id}", item, False)
            for item in self._current_associations()
            if item.confirmed_source == REGULAR_SOURCE
            and item.passage_event_id in events
        ]
        saved.extend(
            (f"marker:{item.marker_id}", item, True)
            for item in self.continuous_marker_store.markers()
            if item.camera_index == pane.camera_index
        )
        for key, item, unknown in saved:
            segment = self.timeline_store.get_segment(item.segment_id)
            if (
                segment is None
                or segment.camera_index != pane.camera_index
                or segment.clock_source != DEFAULT_CLOCK_SOURCE
                or (metadata is not None and segment.race_id
                    and segment.race_id != metadata.race_id)
            ):
                continue
            if unknown and item.passage_event_id and item.passage_event_id not in events:
                continue
            recorder_time = (
                int(segment.media_started_at_ms) + item.position_ms
                if segment.media_started_at_ms is not None else None
            )
            offset = self._calibrated_clock_offset(
                pane.camera_index, self._recording_session_key_from_path(
                    segment.source_id, self.timeline_store.resolve_video_path(segment),
                ),
            )
            label = item.label if unknown else (
                events[item.passage_event_id].bib.strip() or item.bib.strip() or "未知"
            )
            records.append(CameraJudgment(
                key=key,
                event_id="" if unknown else item.passage_event_id,
                segment_id=item.segment_id,
                position_ms=item.position_ms,
                recorder_time_ms=recorder_time,
                label=label,
                time_label=(format_passage_time(recorder_time - offset)
                            if recorder_time is not None else "时间未校验"),
                clock_offset_ms=offset,
                unknown=unknown,
            ))
        return tuple(sorted(records, key=lambda item: (
            item.recorder_time_ms is None, item.recorder_time_ms or 0,
            # Same-frame judgments have no video evidence of an order. Keep
            # their roster order instead of sorting by opaque passage IDs.
            events[item.event_id].timeline_timestamp_ms
            if item.event_id in events else item.recorder_time_ms or 0,
            item.key,
        )))

    def _seek_retained_camera_frame(
        self, location: PassageVideoLocation, position_ms: int,
        frame_index: Optional[int] = None, camera_index: int = 1,
        *, adjacent_boundary_ms: Optional[int] = None, adjacent_direction: int = 0,
    ) -> bool:
        """Open a pinned image's source independently of the overview window.

        ``frame_index=None`` means the caller only knows the media time (a
        group start has no captured frame number): the pane then seeks by
        milliseconds instead of an invented frame.
        """
        try:
            location = self._persist_filmstrip_location(location)
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            self.video_filmstrip.full_race.status_label.setText(
                f"原帧保留失败，未进入判读：{error}"
            )
            return False
        pane = self._camera_one_pane() if int(camera_index) <= 1 else self._pane_for_camera(camera_index)
        offset = self._continuous_offset_for_location(location)
        origin = location.segment.media_started_at_ms
        event = self.passage_store.get(self._selected_event_id)
        if event is None:
            event = SimpleNamespace(event_id="", bib="待补录", timeline_timestamp_ms=(origin or 0) + position_ms - offset)
        target = int(event.timeline_timestamp_ms) + offset - origin if origin is not None else position_ms
        location = replace(location, passage_position_ms=target, playback_position_ms=position_ms, clock_offset_ms=offset)
        association = self._association_for_event(event.event_id, REGULAR_SOURCE) if event.event_id else None
        self._set_sync_playing(False, seek_final=False)
        self._deferred_start_timer.stop()
        self._deferred_selection_panes.clear()
        self._selection_pending_panes.clear()
        self._filmstrip_preview_timer.stop()
        self._pending_filmstrip_preview_position = None
        self._pending_filmstrip_position = None
        self._activate_pane(pane, align=False)
        self._pause_inactive_panes(pane)
        # The image retains its recording even if the ordinary filmstrip has
        # moved elsewhere. Selection remains the operator's current identity.
        if not pane.rebind_passage(event, location, association):
            pane.set_passage(event, location, association)
        if adjacent_boundary_ms is None:
            pane.seek_media_frame(position_ms, frame_index)
        else:
            pane.seek_adjacent_frame(adjacent_boundary_ms, adjacent_direction)
        self._shared_delta_ms = position_ms - target
        self._update_shared_time_label()
        return True

    def _refresh_camera_judgments(self) -> None:
        pane = self._camera_one_pane()
        records = self._camera_judgment_records(pane)
        self.video_filmstrip.full_race.set_judgments(records, self._selected_event_id)
        self.video_filmstrip.judgment_track.set_records(
            records, self._selected_event_id,
        )
        pane.video_view.set_history_markers(self._continuous_marker_overlays(pane))
        context = self._filmstrip_context
        window = self._filmstrip_absolute_window
        if context is not None and window is not None and pane.location is not None:
            # The filmstrip may already have navigated to the next archive,
            # while the video still displays the previous one.
            origin = window[0] - context[1]
            self.video_filmstrip.set_history_markers(tuple(
                (record.recorder_time_ms - origin, record.label)
                for record in records
                if record.recorder_time_ms is not None
                and window[0] <= record.recorder_time_ms <= window[1]
            ))

    def _open_camera_judgment(self, key: str) -> None:
        pane = self._camera_one_pane()
        record = next((item for item in self._camera_judgment_records(pane)
                       if item.key == key), None)
        if record is None:
            return
        saved = (
            next((item for item in self.continuous_marker_store.markers()
                  if f"marker:{item.marker_id}" == key), None)
            if record.unknown else
            self._association_for_event(record.event_id, REGULAR_SOURCE)
        )
        location = self._saved_regular_location(saved) if saved is not None else None
        if (location is None or location.status not in _OPENABLE_STATUSES
                or location.passage_position_ms != record.position_ms):
            QMessageBox.information(self, "无法回看", "判读记录仍保留，但对应录像暂不可用。")
            return
        offset = record.clock_offset_ms
        origin = location.segment.media_started_at_ms
        event = self.passage_store.get(record.event_id) if record.event_id else None
        # Unassigned records never borrow the selected athlete's identity.
        if event is None:
            event = SimpleNamespace(
                event_id="", bib="待补录",
                timeline_timestamp_ms=(record.recorder_time_ms or 0) - offset,
            )
        target_position = (
            int(event.timeline_timestamp_ms) + offset - int(origin)
            if origin is not None else record.position_ms
        )
        location = replace(
            location, passage_position_ms=target_position,
            playback_position_ms=record.position_ms, clock_offset_ms=offset,
        )
        self._set_sync_playing(False, seek_final=False)
        if record.event_id:
            self._select_event(record.event_id, preserve_current_frame=pane)
        else:
            self._clear_selection_details()
        self._deferred_start_timer.stop()
        self._deferred_selection_panes.clear()
        self._selection_pending_panes.clear()
        self._filmstrip_preview_timer.stop()
        self._pending_filmstrip_preview_position = None
        self._selected_event_id = record.event_id
        self.table.blockSignals(True)
        self.table.clearSelection()
        self.table.setCurrentCell(-1, -1)
        for row, candidate in enumerate(self._visible_events):
            if candidate.event_id == record.event_id:
                self.table.setCurrentCell(row, 1)
                self.table.selectRow(row)
                break
        self.table.blockSignals(False)
        self._activate_pane(pane, align=False)
        self._pause_inactive_panes(pane)
        pane.set_passage(event, location, None if record.unknown else saved)
        self._shared_delta_ms = record.position_ms - target_position
        self.current_passage_label.setText(record.label)
        self.current_context_label.setText("待补录" if record.unknown else "已确认")
        self._video_candidate_anchor_time_ms = record.recorder_time_ms
        self._filmstrip_requested_window_start_ms = None
        self._update_filmstrip()
        self._refresh_camera_judgments()

        # 回看是明确的定位操作：原帧、胶卷和可见判读窗口一起定位。
        pane.seek_media_frame(record.position_ms, saved.frame_index)
        panel = self.video_filmstrip.full_race
        panel.set_current_frame(location, record.position_ms)
        if record.recorder_time_ms is not None:
            panel.browse_to(record.recorder_time_ms)
        if self._uses_popup_judging() and self._maximized_window is None:
            self._toggle_maximized_pane(pane)
        pane.video_view.setFocus(Qt.ShortcutFocusReason)

    def _follow_tracks(self, camera_index: int) -> FollowTracks:
        output_dir = Path(self.passage_store.journal_path).parent / FOLLOW_OUTPUT_DIRNAME
        path = follow_path(output_dir, camera_index)
        tracks = self._follow_cache.get(path)
        if tracks is None:
            tracks = self._follow_cache[path] = FollowTracks(path)
        return tracks.refresh()

    def _continuous_marker_overlays(
        self,
        pane: PassageEvidencePane,
    ) -> tuple[tuple, ...]:
        """Confirmed camera-1 markers nearby, each carried along with its runner.

        A mark is put on a runner at the line; where the analysis saw that
        runner, the number walks with them from a little before the crossing to
        a little after.  Runners the chips paired automatically carry their
        number too, in the grey-blue 自动对上 style: a pairing, not a count.
        Without a seen runner a mark stays where it was put, on its own frame.
        """

        location = pane.location
        if (
            pane.source_kind != REGULAR_SOURCE
            or location is None
            or location.segment.media_started_at_ms is None
            or pane._current_frame_index < 0
        ):
            return ()
        session_key = self._recording_session_key(location)
        offset_ms = self._continuous_clock_offsets.get(
            (int(pane.camera_index), session_key),
            int(location.clock_offset_ms),
        )
        current_recorder_time_ms = (
            int(location.segment.media_started_at_ms)
            + int(pane._current_position_ms)
        )
        tolerance_ms = max(200, int(pane.frame_duration_ms()) * 3)
        follow = self._follow_tracks(int(pane.camera_index))
        nearby = follow.around(current_recorder_time_ms)
        followed: set[int] = set()
        marked_bibs: set[str] = set()
        overlays: list[tuple] = []

        def place(mark_ms: int, x: float, y: float, label: str, *, own: bool = False) -> None:
            # ``own``: the selected runner, whose ring the pane draws on its frame.
            track = follow.owner(nearby, mark_ms, x, y) if nearby else None
            if track is not None:
                followed.add(id(track))
                here = (track.at(current_recorder_time_ms)
                        if track.near(current_recorder_time_ms, mark_ms) else None)
                if here is not None and not (own and pane._association_is_visible(pane.association)):
                    overlays.append((here[0], here[1], label, True, pane.is_auyat_rgb, True, False, False))
                return
            if not own and abs(mark_ms - current_recorder_time_ms) <= tolerance_ms:
                overlays.append((x, y, label, True, pane.is_auyat_rgb))

        for association in self._current_associations():
            if association.confirmed_source != REGULAR_SOURCE:
                continue
            event = self.passage_store.get(association.passage_event_id)
            segment = self.timeline_store.get_segment(association.segment_id)
            if (
                event is None
                or segment is None
                or int(segment.camera_index) != int(pane.camera_index)
            ):
                continue
            if (
                segment.media_started_at_ms is not None
                and association.position_ms is not None
            ):
                # Persisted marks belong to the recorder timeline of the
                # segment on which they were confirmed.  Using the chip event
                # timestamp here reintroduces the very 149/67 collision that
                # the continuous review rail is meant to prevent.
                event_recorder_time_ms = (
                    int(segment.media_started_at_ms)
                    + int(association.position_ms)
                )
            else:
                event_recorder_time_ms = int(event.timeline_timestamp_ms) + int(
                    offset_ms
                )
            if abs(event_recorder_time_ms - current_recorder_time_ms) > FOLLOW_BEFORE_MS + FOLLOW_AFTER_MS:
                continue
            label = event.bib.strip() or association.bib.strip() or "未知"
            marked_bibs.add(label)
            place(event_recorder_time_ms, float(association.marker_x_normalized),
                  float(association.marker_y_normalized), label,
                  own=(association.passage_event_id == self._selected_event_id
                       and pane.association is not None))
        # Unknown/待补录 markers live on their own continuous timeline and
        # must remain visible independently of the selected roster identity.
        for marker in self.continuous_marker_store.markers():
            if int(marker.camera_index) != int(pane.camera_index):
                continue
            segment = self.timeline_store.get_segment(marker.segment_id)
            if segment is None or segment.media_started_at_ms is None:
                continue
            marker_recorder_time_ms = int(segment.media_started_at_ms) + int(marker.position_ms)
            if abs(marker_recorder_time_ms - current_recorder_time_ms) > FOLLOW_BEFORE_MS + FOLLOW_AFTER_MS:
                continue
            place(marker_recorder_time_ms, float(marker.marker_x_normalized),
                  float(marker.marker_y_normalized), marker.label)
        # Runners the chips paired by themselves: their number, not yet counted.
        for track in nearby:
            if id(track) in followed or not track.bib or track.bib in marked_bibs:
                continue
            if not track.near(current_recorder_time_ms, track.crossing_ms):
                continue
            here = track.at(current_recorder_time_ms)
            if here is not None:
                overlays.append((here[0], here[1], track.bib, False, pane.is_auyat_rgb, True, False, True))
        return tuple(overlays)

    def _on_preview_frame_ready(
        self,
        pane: PassageEvidencePane,
        image,
        position_ms: int,
        frame_index: int,
    ) -> None:
        if pane is self._camera_one_pane():
            self.video_filmstrip.full_race.set_current_frame(pane.location, position_ms)
        history_markers = self._continuous_marker_overlays(pane)
        pane.video_view.set_history_markers(history_markers)
        if pane is not self._active_playback_pane():
            return
        self.preview_video_view.set_frame(
            image,
            source_width=pane._source_width,
            source_height=pane._source_height,
        )
        self.preview_video_view.set_frame_indicator(
            pane.frame_indicator_label.text()
        )
        self.preview_video_view.set_history_markers(history_markers)
        self.preview_video_view.set_marker_mode(
            self.preview_mark_btn.isChecked()
        )
        can_mark = (
            pane is self._filmstrip_judgment_pane()
            and pane._current_frame_index >= 0
            and pane.location is not None
            and pane.location.status in _CONFIRMABLE_STATUSES
        )
        self.preview_mark_btn.setEnabled(can_mark)
        if not can_mark:
            self.preview_mark_btn.setChecked(False)
            self.preview_confirm_btn.setEnabled(False)
        pending = pane.pending_confirmation()
        if pending is not None:
            self.preview_video_view.set_marker(
                float(pending["marker_x_normalized"]),
                float(pending["marker_y_normalized"]),
                pane._identity,
                confirmed=False,
                simple=pane.is_auyat_rgb,
            )
        elif pane.association is not None:
            self.preview_video_view.set_marker(
                pane.association.marker_x_normalized,
                pane.association.marker_y_normalized,
                pane._identity,
                confirmed=True,
                simple=pane.is_auyat_rgb,
            )
        else:
            self.preview_video_view.clear_marker()

    def _on_preview_marker_selected(
        self,
        x_normalized: float,
        y_normalized: float,
    ) -> None:
        pane = self._filmstrip_judgment_pane()
        if pane is None:
            return
        self._activate_pane(pane, align=False)
        pane._on_marker_position_selected(
            float(x_normalized),
            float(y_normalized),
        )
        self.preview_video_view.set_marker(
            float(x_normalized),
            float(y_normalized),
            pane._identity,
            confirmed=False,
            simple=pane.is_auyat_rgb,
        )
        self.preview_mark_btn.setChecked(True)
        self.preview_confirm_btn.setEnabled(True)

    def _confirm_preview_marker(self) -> None:
        pane = self._filmstrip_judgment_pane()
        if pane is None or pane.pending_confirmation() is None:
            return
        if not self._confirm_pending_marker(pane):
            return
        association = pane.association
        if association is not None:
            self.preview_video_view.set_marker(
                association.marker_x_normalized,
                association.marker_y_normalized,
                pane._identity,
                confirmed=True,
                simple=pane.is_auyat_rgb,
            )
        else:
            self.preview_video_view.clear_marker()
        self.preview_mark_btn.setChecked(False)
        self.preview_confirm_btn.setEnabled(False)

    def _on_filmstrip_marker_selected(
        self,
        position_ms: int,
        marker_x_normalized: float,
        marker_y_normalized: float,
        frame_index: int,
    ) -> None:
        pane = self._filmstrip_judgment_pane()
        if pane is None:
            return
        self._activate_pane(pane, align=False)
        self.preview_mark_btn.setChecked(True)
        self.preview_mark_btn.setEnabled(True)
        self.preview_confirm_btn.setEnabled(True)
        selected_event = self.passage_store.get(self._selected_event_id)
        nearby_event_id = self._filmstrip_event_id_at_position(int(position_ms))
        unknown_marker = (
            selected_event is None
            or not selected_event.bib.strip()
            or selected_event.bib.strip().casefold() in {"unknown", "未知", "待补录"}
            or not nearby_event_id
        )
        pane.set_external_pending_marker(
            frame_index=int(frame_index),
            position_ms=int(position_ms),
            marker_x_normalized=float(marker_x_normalized),
            marker_y_normalized=float(marker_y_normalized),
            unknown_marker=unknown_marker,
            marker_label="待补录" if unknown_marker else "",
        )
        self.video_filmstrip.set_marker(
            int(position_ms),
            float(marker_x_normalized),
            float(marker_y_normalized),
            int(frame_index),
        )

    def _confirm_filmstrip_marker(self) -> bool:
        pane = self._filmstrip_judgment_pane()
        if pane is None or pane.pending_confirmation() is None:
            return False
        confirmed = self._confirm_pending_marker(pane)
        if confirmed:
            self.video_filmstrip.clear_marker()
            self.preview_mark_btn.setChecked(False)
            self.preview_confirm_btn.setEnabled(False)
        return confirmed

    def _clear_filmstrip_marker(self) -> None:
        pane = self._filmstrip_judgment_pane()
        if pane is not None:
            pane.cancel_marker_edit()
        self.video_filmstrip.clear_marker()
        self.preview_mark_btn.setChecked(False)
        self.preview_confirm_btn.setEnabled(False)

    def _preview_filmstrip_position(self, position_ms: int) -> None:
        """Preview the latest drag position without queuing every mouse move."""

        self._mark_filmstrip_operator_busy()
        self._pending_filmstrip_preview_position = int(position_ms)
        self.video_filmstrip.set_current_position(int(position_ms))
        if hasattr(self, "preview_timeline") and self.preview_timeline.maximum() > self.preview_timeline.minimum():
            self.preview_timeline.setValue(
                max(
                    self.preview_timeline.minimum(),
                    min(int(position_ms), self.preview_timeline.maximum()),
                )
            )
        if not self._filmstrip_preview_timer.isActive():
            self._filmstrip_preview_timer.start()

    def _on_preview_timeline_pressed(self) -> None:
        self._preview_timeline_dragging = True

    def _on_preview_timeline_moved(self, position_ms: int) -> None:
        if not getattr(self, "_preview_timeline_dragging", False):
            return
        self._preview_filmstrip_position(int(position_ms))

    def _on_preview_timeline_released(self) -> None:
        self._preview_timeline_dragging = False
        self._seek_filmstrip_position(int(self.preview_timeline.value()))

    def _flush_filmstrip_preview(self) -> None:
        position_ms = self._pending_filmstrip_preview_position
        self._pending_filmstrip_preview_position = None
        if position_ms is None:
            return
        pane = self._filmstrip_judgment_pane()
        if pane is None or pane.available_delta_bounds() is None:
            return
        target_delta_ms = int(position_ms) - int(pane._target_position_ms)
        if self.SINGLE_CAMERA_PREVIEW:
            self._seek_preview_pane_delta(pane, target_delta_ms)
        else:
            self._preview_both_delta(target_delta_ms)

    def _move_filmstrip_batch(self, direction: int) -> None:
        self.video_filmstrip.full_race.page(1 if direction >= 0 else -1)

    def _seek_filmstrip_position(self, position_ms: int) -> None:
        self._mark_filmstrip_operator_busy()
        self._filmstrip_preview_timer.stop()
        self._pending_filmstrip_preview_position = None
        # The filmstrip is a continuous-camera locating surface, not an
        # identity selector.  Clicking a nearby thumbnail must seek camera 1
        # while leaving the selected roster identity unchanged; otherwise a
        # missing-chip rider could be silently reassigned to its neighbour.
        self._pending_filmstrip_position = int(position_ms)
        self._filmstrip_seek_retry_count = 0
        self.video_filmstrip.set_current_position(int(position_ms))
        if hasattr(self, "preview_timeline") and self.preview_timeline.maximum() > self.preview_timeline.minimum():
            self.preview_timeline.setValue(
                max(
                    self.preview_timeline.minimum(),
                    min(int(position_ms), self.preview_timeline.maximum()),
                )
            )
        if self._filmstrip_seek_pending:
            return
        self._filmstrip_seek_pending = True
        QTimer.singleShot(40, self._flush_filmstrip_position)

    def _open_filmstrip_position_for_judgment(self, position_ms: int) -> None:
        """Seek the linked cameras and enlarge the active pane for marking."""

        self._seek_filmstrip_position(position_ms)
        pane = self._camera_one_pane()
        QTimer.singleShot(60, lambda: self._toggle_maximized_pane(pane))

    def _rebind_pane_to_filmstrip_archive(
        self,
        pane: PassageEvidencePane,
        position_ms: int,
    ) -> bool:
        """Move the judge pane from a chip clip to the two-minute archive batch."""

        context = self._filmstrip_context
        if context is None:
            return False
        event = self.passage_store.get(self._selected_event_id)
        if event is None:
            return False
        filmstrip_path, _start_ms, _end_ms = context
        absolute_window = self._filmstrip_absolute_window
        if absolute_window is None:
            return False
        media_origin_ms = int(absolute_window[0]) - int(_start_ms)
        target_absolute_ms = media_origin_ms + int(position_ms)
        location = self._live_location_for_filmstrip(
            pane,
            target_absolute_ms,
        ) or self._archive_location_for_filmstrip(
            pane,
            target_absolute_ms,
        )
        if location is None or location.video_path.resolve() != filmstrip_path.resolve():
            return False
        current_path = (
            pane.location.video_path.resolve()
            if pane.location is not None
            else None
        )
        current_duration = int(
            getattr(pane.location.segment, "media_duration_ms", 0)
            if pane.location is not None
            else 0
        )
        current_position = int(getattr(pane, "_current_position_ms", 0))
        if (
            current_path == filmstrip_path.resolve()
            and 0 <= current_position <= current_duration
            and current_duration >= int(location.segment.media_duration_ms or 0)
        ):
            return False
        pane.set_passage(
            event,
            location,
            initial_delta_ms=(
                int(position_ms) - int(location.playback_position_ms)
            ),
            lookup_status=location.status,
        )
        offset_ms = self._continuous_offset_for_location(location)
        self._shared_delta_ms = target_absolute_ms - (
            int(event.timeline_timestamp_ms) + int(offset_ms)
        )
        self._update_shared_time_label()
        return True

    def _flush_filmstrip_position(self) -> None:
        self._filmstrip_seek_pending = False
        position_ms = self._pending_filmstrip_position
        self._pending_filmstrip_position = None
        if position_ms is None:
            return
        preferred_pane = self._camera_one_pane()
        seek_pane = next(
            (
                pane
                for pane in (preferred_pane, *self.evidence_panes)
                if pane.location is not None
                and pane.available_delta_bounds() is not None
            ),
            None,
        )
        if seek_pane is None:
            # The filmstrip can be ready before the lower video worker has
            # emitted metadata. Keep the requested position briefly so a
            # double-click still links to both camera panes once duration and
            # frame bounds become available.
            if (
                preferred_pane.location is not None
                and self._filmstrip_seek_retry_count < 25
            ):
                self._filmstrip_seek_retry_count += 1
                self._filmstrip_seek_pending = True
                QTimer.singleShot(120, self._flush_filmstrip_position)
                return
            self._pending_filmstrip_position = None
            return
        self._filmstrip_seek_retry_count = 0
        if self._rebind_pane_to_filmstrip_archive(seek_pane, position_ms):
            self.video_filmstrip.set_current_position(int(position_ms))
            return
        target_delta_ms = int(position_ms) - int(seek_pane._target_position_ms)
        # The filmstrip represents the shared race timeline.  Move every
        # visible camera together so each pane applies its own calibrated
        # passage target instead of leaving the secondary angle behind.
        if self.SINGLE_CAMERA_PREVIEW:
            self._seek_preview_pane_delta(seek_pane, target_delta_ms)
        else:
            self._seek_both_delta(target_delta_ms)

    def _seek_preview_pane_delta(
        self,
        pane: PassageEvidencePane,
        delta_ms: int,
    ) -> None:
        """Seek only the active review camera during low-power preview."""

        self._activate_pane(pane, align=False)
        bounds = pane.available_delta_bounds()
        if bounds is None:
            return
        lower, upper = bounds
        self._shared_delta_ms = max(lower, min(int(delta_ms), upper))
        pane.seek_passage_delta(self._shared_delta_ms, preview=True)
        self._update_shared_time_label()

    def _filmstrip_event_id_at_position(self, position_ms: int) -> str:
        """Resolve a normal filmstrip click to the nearest visible athlete."""

        pane = self._camera_one_pane()
        location = pane.location
        if location is None:
            return ""
        target_path = str(Path(location.video_path).absolute())
        candidates: list[tuple[int, int, str]] = []
        for row, event in enumerate(self._visible_events):
            lookup = self._lookups.get(event.event_id)
            if lookup is None:
                continue
            event_location = self._regular_location_for_camera(
                lookup,
                pane.camera_index,
            )
            if event_location is None:
                continue
            projected = self._location_on_current_media(event, pane)
            if projected is None or projected.segment.segment_id != location.segment.segment_id:
                continue
            if str(Path(event_location.video_path).absolute()) != target_path:
                continue
            distance = abs(int(projected.passage_position_ms) - int(position_ms))
            candidates.append((distance, row, event.event_id))
        if not candidates:
            return ""
        distance, _row, event_id = min(candidates)
        # A sparse strip is sampled every two seconds; do not retarget the
        # athlete list when a click is clearly outside its passage window.
        return event_id if distance <= 1_500 else ""

    def _skip_continuous_gap(
        self,
        event: PassageEvent,
        pane: PassageEvidencePane,
    ) -> bool:
        if (
            not self.CONTINUOUS_AUTO_SKIP
            or not self._batch_mode
            or pane.location is None
        ):
            return False
        target_position_ms = int(pane.location.passage_position_ms)
        current_position_ms = int(pane._current_position_ms)
        gap_ms = target_position_ms - current_position_ms
        if abs(gap_ms) < self.CONTINUOUS_SKIP_GAP_MS:
            return False
        seek_position_ms = max(0, target_position_ms - self.CONTINUOUS_SKIP_LEAD_MS)
        if gap_ms > 0:
            seek_position_ms = max(current_position_ms, seek_position_ms)
        self._shared_delta_ms = seek_position_ms - target_position_ms
        pane.seek_passage_delta(self._shared_delta_ms)
        self._update_shared_time_label()
        return True

    def _sync_filmstrip_from_pane(self, pane: PassageEvidencePane) -> None:
        if not hasattr(self, "video_filmstrip") or pane is not self._camera_one_pane():
            return
        self.video_filmstrip.full_race.set_current_frame(pane.location, pane._current_position_ms)

    def _flush_filmstrip_anchor(self) -> None:
        pending = self._pending_filmstrip_anchor
        self._pending_filmstrip_anchor = None
        if pending is None or not hasattr(self, "video_filmstrip"):
            return
        video_path, position_ms = pending
        if Path(getattr(self.video_filmstrip, "_video_path", "")) != video_path:
            return
        self.video_filmstrip.append_positions(video_path, (position_ms,))
