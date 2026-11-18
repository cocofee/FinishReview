"""Playback, frame stepping, two-pane sync and maximized panes of the passage review surface."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import time
from typing import Optional

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QKeySequence
from PyQt5.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplitter,
    QShortcut,
    QVBoxLayout,
)

from . import APP_DISPLAY_NAME
from .race_filmstrip import recording_sources
from .passage_evidence import REGULAR_SOURCE
from .passage_review_common import CTRL_FRAME_STEP, _OPENABLE_STATUSES, format_passage_time
from .passage_evidence_pane import PassageEvidencePane
from .ui_parts import UiPart


class PassageReviewPlaybackPart(UiPart):
    """Play and step the panes (across recording files), keep two panes in sync, maximize a pane."""

    def _focused_evidence_pane(self) -> Optional[PassageEvidencePane]:
        widget = self._host.focusWidget()
        while widget is not None:
            for pane in self._host.evidence_panes:
                if widget is pane:
                    return pane
            widget = widget.parentWidget()
        return None

    def _active_playback_pane(self) -> PassageEvidencePane:
        focused = self._host._focused_evidence_pane()
        if focused is not None and focused.available_delta_bounds() is not None:
            return focused
        if (
            self._host._active_pane is not None
            and self._host._active_pane.available_delta_bounds() is not None
        ):
            return self._host._active_pane
        for pane in self._host.evidence_panes:
            if pane.available_delta_bounds() is not None:
                return pane
        return self._host._active_pane or self._host.regular_pane

    def _pause_inactive_panes(self, active: PassageEvidencePane) -> None:
        self._host._set_active_idle_prefetch(active)
        for pane in self._host.evidence_panes:
            if pane is not active:
                pane.set_playing(False)
                pane.park_playback_cache()

    def _set_active_idle_prefetch(
        self,
        active: Optional[PassageEvidencePane],
    ) -> None:
        for pane in self._host.all_evidence_panes:
            pane.set_idle_prefetch_enabled(pane is active)
            pane.set_active_judging(pane is active)

    def _update_shared_from_pane(self, pane: PassageEvidencePane) -> None:
        delta_ms = pane.current_delta_ms()
        if delta_ms is None:
            return
        self._host._shared_delta_ms = delta_ms
        self._host._update_shared_time_label()

    def _activate_pane(
        self,
        pane: PassageEvidencePane,
        *,
        align: bool,
    ) -> bool:
        if pane not in self._host.evidence_panes:
            return False
        if self._host._sync_playing:
            self._host._set_sync_playing(False, seek_final=False)
        previous = self._host._active_pane
        switched = previous is not pane
        if switched and previous is not None:
            self._host._update_shared_from_pane(previous)
            previous.set_playing(False)
        self._host._active_pane = pane
        # In low-power preview mode secondary cameras are prepared but not
        # decoded until the operator explicitly switches to them.
        pane.start_deferred_worker()
        self._host._pause_inactive_panes(pane)
        if switched and align:
            pane.seek_passage_delta(self._host._shared_delta_ms)
        if self._host._maximized_window is not None and pane in self._host._maximized_hosted_panes:
            self._host._maximized_pane = pane
            self._host._maximized_pane_index = self._host._maximized_original_indexes.get(pane, -1)
            self._host._update_maximized_mode_controls(
                side_by_side=len(self._host._maximized_hosted_panes) > 1
            )
        return switched

    def _toggle_active_pane(self) -> None:
        self._host._toggle_pane(self._host._active_playback_pane())

    def _on_pane_detail_requested(self, pane: PassageEvidencePane) -> None:
        self._host._mark_filmstrip_operator_busy()
        self._host._activate_pane(pane, align=False)

    def _toggle_pane(self, pane: PassageEvidencePane) -> None:
        self._host._mark_filmstrip_operator_busy()
        was_sync_playing = self._host._sync_playing
        if was_sync_playing:
            self._host._set_sync_playing(False, seek_final=False)
        switched = self._host._activate_pane(pane, align=False)
        if pane.is_playing and not was_sync_playing:
            pane.set_playing(False)
            self._host._update_shared_from_pane(pane)
            return
        if not switched:
            self._host._update_shared_from_pane(pane)
        pane.seek_passage_delta(self._host._shared_delta_ms, linked_playing=True)

    def _continue_playback(self, pane: PassageEvidencePane) -> None:
        """▶ that ran to the last frame of a file plays on in the file that continues it.

        Rows open 2.5 s review pieces, so ▶ used to stop every 2.5 s (现代五项
        10-29, “2 秒割裂感”).  Only forward play of one pane at its file's last
        frame carries on, through the same neighbour lookup as →; a gap in the
        recording, synchronised play or reverse play just stops as before.
        """

        if self._host._sync_playing or pane not in self._host.evidence_panes:
            return
        if self._host._step_across_recording_boundary(pane, 1):
            pane.set_playing(True)

    def _step_active_pane(self, frame_delta: int) -> None:
        self._host._step_pane(self._host._active_playback_pane(), frame_delta)

    def _step_pane(
        self,
        pane: PassageEvidencePane,
        frame_delta: int,
    ) -> None:
        self._host._mark_filmstrip_operator_busy()
        if abs(int(frame_delta)) == CTRL_FRAME_STEP:
            direction = 1 if int(frame_delta) > 0 else -1
            if self._host._navigate_video_candidate(direction, pane):
                return
        switched = self._host._activate_pane(pane, align=False)
        if not switched:
            if self._host._step_across_recording_boundary(pane, frame_delta):
                return
            current_delta_ms = pane.current_delta_ms()
            pane.step(frame_delta)
            bounds = pane.available_delta_bounds()
            if bounds is not None:
                lower, upper = bounds
                base_delta_ms = (
                    self._host._shared_delta_ms
                    if current_delta_ms is None
                    else current_delta_ms
                )
                self._host._shared_delta_ms = max(
                    lower,
                    min(
                        base_delta_ms
                        + int(frame_delta) * pane.frame_duration_ms(),
                        upper,
                    ),
                )
                self._host._update_shared_time_label()
            return
        bounds = pane.available_delta_bounds()
        if bounds is None:
            return
        lower, upper = bounds
        self._host._shared_delta_ms = max(
            lower,
            min(
                self._host._shared_delta_ms
                + int(frame_delta) * pane.frame_duration_ms(),
                upper,
            ),
        )
        pane.seek_passage_delta(self._host._shared_delta_ms)
        self._host._update_shared_time_label()

    def _step_across_recording_boundary(
        self,
        pane: PassageEvidencePane,
        frame_delta: int,
        at_edge: bool = False,
    ) -> bool:
        """Continue single-frame navigation into an adjacent recording file.

        AT_EDGE: the decoder itself found no further frame in this file.
        """

        location = pane.location
        worker = getattr(pane, "_worker", None)
        if location is None or worker is None or abs(int(frame_delta)) != 1:
            return False
        frame_ms = max(1, pane.frame_duration_ms())
        position_ms = int(getattr(pane, "_current_position_ms", 0))
        duration_ms = int(getattr(pane, "_duration_ms", 0))
        direction = 1 if int(frame_delta) > 0 else -1
        frame_index = int(getattr(pane, "_current_frame_index", -1))
        frame_count = int(getattr(pane, "_frame_count", 0))
        at_first_frame = frame_index == 0 if frame_index >= 0 else position_ms == 0
        at_last_frame = (
            frame_index >= frame_count - 1
            if frame_count > 0 and frame_index >= 0
            else duration_ms > 0 and position_ms >= duration_ms - frame_ms
        )
        if not at_edge and direction < 0 and not at_first_frame:
            return False
        if not at_edge and direction > 0 and not at_last_frame:
            return False

        current = location.segment
        event = self._host.passage_store.get(self._host._selected_event_id)
        if pane is self._host._camera_one_pane() or event is None:
            crossed = self._host._step_into_neighbour_recording(pane, direction, frame_ms, duration_ms)
            if not crossed and at_edge:
                pane.show_navigation_notice("相邻录像暂不可用或存在缺口，已保留当前画面。")
            return crossed
        current_session_key = self._host._recording_session_key(location)
        segments = sorted(
            (
                segment
                for segment in self._host.timeline_store.segments()
                if segment.segment_id != current.segment_id
                and int(segment.camera_index) == int(current.camera_index)
                and segment.source_id == current.source_id
                and self._host._recording_session_key_from_path(
                    segment.source_id,
                    self._host.timeline_store.resolve_video_path(segment),
                )
                == current_session_key
                and (not event.race_id or not segment.race_id or segment.race_id == event.race_id)
                and segment.ended_at_ms is not None
            ),
            key=lambda segment: (segment.started_at_ms, segment.segment_id),
        )
        current_origin_ms = int(
            current.media_started_at_ms
            if current.media_started_at_ms is not None
            else current.started_at_ms
        )
        current_end_ms = current_origin_ms + int(
            current.media_duration_ms
            if current.media_duration_ms is not None
            else max(0, int(current.ended_at_ms or current_origin_ms) - current_origin_ms)
        )
        if direction < 0:
            adjacent = next(
                (
                    segment
                    for segment in reversed(segments)
                    if int(segment.ended_at_ms or 0) <= current_origin_ms + frame_ms
                ),
                None,
            )
        else:
            adjacent = next(
                (
                    segment
                    for segment in segments
                    if int(
                        segment.media_started_at_ms
                        if segment.media_started_at_ms is not None
                        else segment.started_at_ms
                    ) >= current_end_ms - frame_ms
                ),
                None,
            )
        if adjacent is None:
            return False

        adjacent_origin_ms = int(
            adjacent.media_started_at_ms
            if adjacent.media_started_at_ms is not None
            else adjacent.started_at_ms
        )
        adjacent_duration_ms = int(
            adjacent.media_duration_ms
            if adjacent.media_duration_ms is not None
            else max(0, int(adjacent.ended_at_ms or adjacent_origin_ms) - adjacent_origin_ms)
        )
        target_position_ms = (
            max(0, adjacent_duration_ms - frame_ms) if direction < 0 else 0
        )
        target_timestamp_ms = adjacent_origin_ms + target_position_ms
        lookup = self._host.timeline_store.locate_passage(
            target_timestamp_ms,
            race_id=event.race_id,
        )
        target_location = next(
            (
                item
                for item in lookup.locations
                if item.segment.segment_id == adjacent.segment_id
                and item.status in _OPENABLE_STATUSES
            ),
            None,
        )
        if target_location is None:
            return False
        initial_delta_ms = target_position_ms - int(target_location.playback_position_ms)
        pane.set_passage(event, target_location, initial_delta_ms=initial_delta_ms)
        self._host._shared_delta_ms = target_position_ms - int(
            target_location.passage_position_ms
        )
        self._host._update_shared_time_label()
        return True

    @staticmethod
    def _is_archive(location) -> bool:
        return "archive" in Path(location.video_path).stem.lower()

    def _archive_on_piece_clock(self) -> bool:
        """Whether archives are shown on their buffer pieces' clock (the arrival workbench, 10-29)."""

        return False

    def _aligned_archive(self, location, moment_ms, piece=None):
        """An archive's LOCATION with its clock moved onto its buffer pieces' (see archive_alignment).

        PIECE: (location, recorder ms) of a buffer piece showing a picture of
        this archive; else one at MOMENT_MS is looked up.  The shift found is
        kept per archive file.  Anything else, or no picture matched, is
        returned as it is.
        """

        segment = location.segment
        if (not self._host._archive_on_piece_clock() or not self._host._is_archive(location)
                or segment.media_started_at_ms is None):
            return location
        start = int(segment.media_started_at_ms)
        shifts = self._host.__dict__.setdefault("_archive_shifts", {})  # segment id -> (start, shift)
        shift = shifts.get(segment.segment_id, (start, None))[1]
        if shift is None:
            import cv2

            from .archive_alignment import archive_shift_ms

            piece = piece or self._host._piece_at(location, int(moment_ms))
            if piece is None:
                return location
            piece_location, piece_ms = piece
            if piece_location.segment.media_started_at_ms is None:
                return location  # a piece not timed yet has no picture to match
            # A picture tried once is not decoded again: unsure (a still scene) or not found.
            tried = self._host.__dict__.setdefault("_archive_tries", {})
            attempt = (segment.segment_id, str(piece_location.video_path), int(piece_ms))
            if attempt not in tried:
                # The drift grows from file to file: the nearest archive's shift is the first guess.
                guess = min(shifts.values(), key=lambda known: abs(known[0] - start), default=(start, 0))[1]
                try:
                    tried[attempt] = archive_shift_ms(
                        piece_location.video_path, piece_location.segment.media_started_at_ms, piece_ms,
                        location.video_path, start, guess_ms=guess)
                except (cv2.error, OSError, ValueError):
                    tried[attempt] = (None, False)
            shift, sure = tried[attempt]
            if shift is None:
                return location
            if sure:
                shifts[segment.segment_id] = (start, shift)
        return replace(location, segment=replace(segment, media_started_at_ms=start - int(shift)))

    def _piece_at(self, archive, moment_ms):
        """(location, recorder ms) of a buffer piece of ARCHIVE's recording showing MOMENT_MS, or None."""

        camera = int(archive.segment.camera_index)
        session = self._host._recording_session_key(archive)
        sources, _pending = recording_sources(
            self._host.timeline_store, camera, self._host._filmstrip_race_id(),
            path_available=lambda _path, _live: True)  # only the file used is checked
        piece = next((source for source in sources
                      if source.priority == 1 and not self._host._is_archive(source.location)
                      and source.start_ms <= moment_ms < source.end_ms
                      and self._host._recording_session_key(source.location) == session
                      and Path(source.location.video_path).is_file()), None)
        return (piece.location, int(moment_ms)) if piece is not None else None

    def _recording_sources_for_step(self, pane: PassageEvidencePane):
        """This camera's recordings, closed files and the live tail, read afresh."""

        sources, _pending = recording_sources(
            self._host.timeline_store, int(getattr(pane, "camera_index", 1) or 1), self._host._filmstrip_race_id(),
            live_locations=self._host._live_locations_for_filmstrip(pane),
            # Only the file actually opened is checked, not every file of the race.
            path_available=lambda _path, _live: True,
        )
        return sources

    def _seek_pane_delta(
        self,
        pane: PassageEvidencePane,
        delta_ms: int,
        *,
        preview: bool,
    ) -> None:
        self._host._mark_filmstrip_operator_busy()
        self._host._activate_pane(pane, align=False)
        # Preview clips from camera 1/2 are one temporary evidence window. A
        # scrub or seek on either preview pane must move both cameras together;
        # formal clips keep the existing single-camera precision workflow.
        if preview or (
            pane.location is not None and pane.location.status == "preview"
        ):
            self._host._apply_both_delta(delta_ms, preview=preview)
            return
        bounds = pane.available_delta_bounds()
        if bounds is None:
            return
        lower, upper = bounds
        self._host._shared_delta_ms = max(lower, min(int(delta_ms), upper))
        pane.seek_passage_delta(self._host._shared_delta_ms, preview=preview)
        self._host._update_shared_time_label()

    def _on_pane_scrub_started(self, pane: PassageEvidencePane) -> None:
        self._host._mark_filmstrip_operator_busy()
        if self._host._sync_playing:
            self._host._set_sync_playing(False, seek_final=False)
        self._host._active_pane = pane
        self._host._pause_inactive_panes(pane)
        self._host._update_shared_from_pane(pane)

    def _on_pane_position_changed(
        self,
        pane: PassageEvidencePane,
        delta_ms: int,
    ) -> None:
        self._host._mark_filmstrip_operator_busy()
        if self._host._sync_playing or pane is not self._host._active_pane:
            return
        self._host._shared_delta_ms = int(delta_ms)
        self._host._update_shared_time_label()
        self._host._sync_filmstrip_from_pane(pane)

    def _step_both(self, frame_delta: int) -> None:
        self._host._set_sync_playing(False)
        reference = next(
            (
                pane
                for pane in reversed(self._host.evidence_panes)
                if pane.available_delta_bounds() is not None
            ),
            self._host.regular_pane,
        )
        step_ms = reference.frame_duration_ms()
        self._host._seek_both_delta(
            self._host._shared_delta_ms + int(frame_delta) * step_ms
        )

    def _toggle_both(self) -> None:
        self._host._set_sync_playing(not self._host._sync_playing)

    def _sync_delta_bounds(self) -> Optional[tuple[int, int]]:
        bounds = [
            pane_bounds
            for pane in self._host.evidence_panes
            if (pane_bounds := pane.available_delta_bounds()) is not None
        ]
        if not bounds:
            return None
        lower = min(value[0] for value in bounds)
        upper = max(value[1] for value in bounds)
        return lower, upper

    def _seek_both_delta(self, delta_ms: int) -> None:
        self._host._apply_both_delta(delta_ms, preview=False)

    def _seek_to_target_position(self) -> None:
        """Jump every visible camera to its calculated passage timestamp."""

        if self._host.passage_store.get(self._host._selected_event_id) is None:
            return
        self._host._set_sync_playing(False)
        self._host._seek_both_delta(0)

    def _preview_both_delta(self, delta_ms: int) -> None:
        self._host._apply_both_delta(delta_ms, preview=True)

    def _apply_both_delta(self, delta_ms: int, *, preview: bool) -> None:
        bounds = self._host._sync_delta_bounds()
        if bounds is None:
            return
        lower, upper = bounds
        self._host._shared_delta_ms = max(lower, min(int(delta_ms), upper))
        for pane in self._host.evidence_panes:
            pane.seek_passage_delta(
                self._host._shared_delta_ms,
                linked_playing=self._host._sync_playing,
                preview=preview,
            )
        self._host._update_shared_time_label()

    def _set_sync_playing(
        self,
        playing: bool,
        *,
        seek_final: bool = True,
    ) -> None:
        self._host._mark_filmstrip_operator_busy()
        playing = bool(playing) and self._host._sync_delta_bounds() is not None
        if playing == self._host._sync_playing:
            return
        if playing:
            self._host._pause_inactive_panes(self._host.regular_pane)
            self._host._sync_origin_delta_ms = self._host._shared_delta_ms
            self._host._sync_playing = True
            self._host._seek_both_delta(self._host._shared_delta_ms)
            self._host._sync_started_at = time.monotonic()
            self._host._last_sync_correction_at = (
                self._host._sync_started_at - self._host.SYNC_CORRECTION_COOLDOWN_SECONDS
            )
            self._host._sync_timer.start()
        else:
            if self._host._sync_playing:
                elapsed_ms = int((time.monotonic() - self._host._sync_started_at) * 1000.0)
                self._host._shared_delta_ms = self._host._sync_origin_delta_ms + elapsed_ms
            self._host._sync_playing = False
            self._host._sync_timer.stop()
            for pane in self._host.evidence_panes:
                pane.set_linked_playing(False)
        self._host.play_both_btn.setText(
            "联动 Ⅱ" if self._host._sync_playing else "联动 ▶"
        )
        if not self._host._sync_playing and seek_final:
            self._host._seek_both_delta(self._host._shared_delta_ms)

    def _on_sync_tick(self) -> None:
        if not self._host._sync_playing:
            return
        elapsed_ms = int((time.monotonic() - self._host._sync_started_at) * 1000.0)
        target_delta_ms = self._host._sync_origin_delta_ms + elapsed_ms
        bounds = self._host._sync_delta_bounds()
        if bounds is None or target_delta_ms >= bounds[1]:
            if bounds is not None:
                self._host._shared_delta_ms = bounds[1]
            self._host._set_sync_playing(False)
            return
        self._host._shared_delta_ms = target_delta_ms
        self._host._update_shared_time_label()
        self._host._correct_sync_drift(target_delta_ms)

    def _correct_sync_drift(self, target_delta_ms: int) -> None:
        now = time.monotonic()
        elapsed_ms = int((now - self._host._sync_started_at) * 1000.0)
        if (
            elapsed_ms < self._host.SYNC_STARTUP_GRACE_MS
            or (now - self._host._last_sync_correction_at)
            < self._host.SYNC_CORRECTION_COOLDOWN_SECONDS
        ):
            return

        corrected = False
        for pane in self._host.evidence_panes:
            drift_ms = pane.linked_drift_ms(target_delta_ms)
            if drift_ms is None:
                continue
            if abs(drift_ms) <= pane.linked_drift_tolerance_ms():
                continue
            pane.seek_passage_delta(target_delta_ms, linked_playing=True)
            corrected = True
        if corrected:
            self._host._last_sync_correction_at = now

    def _update_shared_time_label(self) -> None:
        event = self._host.passage_store.get(self._host._selected_event_id)
        if event is None:
            return
        self._host.current_time_label.setText(
            format_passage_time(event.timeline_timestamp_ms + self._host._shared_delta_ms)
        )
        self._host.current_time_label.setToolTip(
            f"预计过线 {format_passage_time(event.timeline_timestamp_ms)}；"
            f"距预计过线 {self._host._shared_delta_ms:+d} ms"
        )

    def _fit_visible_evidence_views(self) -> None:
        for pane in self._host.evidence_panes:
            if pane.isVisible() and pane.video_view.has_frame:
                pane.video_view.fit_to_window()

    def _toggle_fullscreen(self) -> None:
        if self._host.isFullScreen():
            self._host.showMaximized()
            self._host.fullscreen_btn.setText("全屏")
        else:
            self._host.showFullScreen()
            self._host.fullscreen_btn.setText("退出全屏")
        QTimer.singleShot(0, self._host._fit_visible_evidence_views)

    def _toggle_maximized_pane(self, pane: PassageEvidencePane) -> None:
        if getattr(self._host, "_problem_workbench_active", False) and not self._host._uses_popup_judging():
            # In the problem workbench the picture grows in place: the queue and the
            # decisions come along with it, so a small screen never gets a second
            # window to manage while looking at one crossing.
            panel = getattr(self._host, "problem_panel", None)
            if panel is not None:
                panel.toggle_big_view()
                return
        if self._host._maximized_pane is pane and len(self._host._maximized_hosted_panes) == 1:
            self._host._restore_maximized_pane()
            return
        if self._host._maximized_pane is not None:
            if (
                pane.source_kind == REGULAR_SOURCE
                and self._host._maximized_pane.source_kind == REGULAR_SOURCE
            ):
                self._host._set_maximized_regular_mode(pane.camera_index)
                return
            self._host._restore_maximized_pane()

        pane_index = self._host.evidence_splitter.indexOf(pane)
        if pane_index < 0:
            return
        if self._host._uses_popup_judging():
            pane.set_playing(False)
            pane.set_full_resolution_enabled(True)
        window = QDialog(
            self._host,
            Qt.Window
            | Qt.WindowTitleHint
            | Qt.WindowSystemMenuHint
            | Qt.WindowMinMaxButtonsHint
            | Qt.WindowCloseButtonHint,
        )
        title = pane.title_label.text().strip() or "机位画面"
        if pane._identity:
            title = f"{title} - {pane._identity}"
        window.setWindowTitle(title)
        window.setModal(False)
        window_layout = QVBoxLayout(window)
        window_layout.setContentsMargins(8, 8, 8, 8)
        window_layout.setSpacing(6)
        mode_bar = QHBoxLayout()
        mode_bar.setContentsMargins(4, 0, 4, 0)
        mode_bar.setSpacing(6)
        mode_label = QLabel(window)
        mode_label.setStyleSheet(
            "color: #0f4f86; font-size: 14pt; font-weight: 700; padding: 4px 2px;"
        )
        mode_bar.addWidget(mode_label)
        mode_bar.addStretch(1)
        mode_buttons: dict[object, QPushButton] = {}
        if pane.source_kind == REGULAR_SOURCE and len(self._host.regular_panes) > 1:
            side_by_side_btn = QPushButton("并排", window)
            side_by_side_btn.setCheckable(True)
            side_by_side_btn.setToolTip("同时显示全部普通机位（B）")
            side_by_side_btn.clicked.connect(
                lambda _checked=False: self._host._set_maximized_regular_mode(None)
            )
            mode_buttons["side_by_side"] = side_by_side_btn
            mode_bar.addWidget(side_by_side_btn)
            for regular_pane in self._host.regular_panes:
                camera_index = regular_pane.camera_index
                camera_btn = QPushButton(f"机位 {camera_index}", window)
                camera_btn.setCheckable(True)
                camera_btn.setToolTip(f"切换到机位 {camera_index}（{camera_index}）")
                camera_btn.clicked.connect(
                    lambda _checked=False, index=camera_index: (
                        self._host._set_maximized_regular_mode(index)
                    )
                )
                mode_buttons[camera_index] = camera_btn
                mode_bar.addWidget(camera_btn)
        window_layout.addLayout(mode_bar)
        content_splitter = QSplitter(Qt.Horizontal, window)
        content_splitter.setChildrenCollapsible(False)
        content_splitter.setHandleWidth(5)
        window_layout.addWidget(content_splitter, 1)

        self._host._maximized_pane = pane
        self._host._maximized_window = window
        self._host._maximized_pane_index = pane_index
        self._host._maximized_content_splitter = content_splitter
        self._host._maximized_original_indexes = {
            candidate: self._host.evidence_splitter.indexOf(candidate)
            for candidate in self._host.evidence_panes
        }
        self._host._maximized_mode_label = mode_label
        self._host._maximized_mode_buttons = mode_buttons
        self._host._maximized_camera_shortcuts = []
        if self._host._uses_popup_judging():
            panel = self._host.video_filmstrip.full_race
            bar = panel.canvas.horizontalScrollBar()
            self._host._popup_return_state = (
                self._host.review_content_splitter.sizes(),
                panel.index.start_ms + bar.value() / panel.tile_pitch * panel.interval_ms,
            )
            roster_splitter = QSplitter(Qt.Horizontal, window)
            roster_splitter.setChildrenCollapsible(False)
            roster_splitter.setHandleWidth(6)
            window_layout.removeWidget(content_splitter)
            roster_splitter.addWidget(self._host.results_panel)
            roster_splitter.addWidget(content_splitter)
            self._host.results_panel.show()
            roster_splitter.setStretchFactor(0, 0)
            roster_splitter.setStretchFactor(1, 1)
            window_layout.addWidget(roster_splitter, 1)
            self._host._maximized_roster_splitter = roster_splitter
            instruction = QLabel("核对名单号码后标线确认 · 单击名单换号码，双击定位计时时刻 · Esc 返回胶卷", window)
            window_layout.addWidget(instruction)
            back_button = QPushButton("返回胶卷（Esc）", window)
            back_button.clicked.connect(self._host._restore_maximized_pane)
            mode_bar.addWidget(back_button)
        escape_shortcut = QShortcut(QKeySequence(Qt.Key_Escape), window)
        escape_shortcut.setContext(Qt.WindowShortcut)
        escape_shortcut.setAutoRepeat(False)
        escape_shortcut.activated.connect(self._host._restore_maximized_pane)
        self._host._maximized_escape_shortcut = escape_shortcut
        if pane.source_kind == REGULAR_SOURCE and len(self._host.regular_panes) > 1:
            side_by_side_shortcut = QShortcut(QKeySequence("B"), window)
            side_by_side_shortcut.setContext(Qt.WindowShortcut)
            side_by_side_shortcut.setAutoRepeat(False)
            side_by_side_shortcut.activated.connect(
                lambda: self._host._set_maximized_regular_mode(None)
            )
            self._host._maximized_camera_shortcuts.append(side_by_side_shortcut)
            cycle_shortcut = QShortcut(QKeySequence(Qt.Key_Tab), window)
            cycle_shortcut.setContext(Qt.WindowShortcut)
            cycle_shortcut.setAutoRepeat(False)
            cycle_shortcut.activated.connect(self._host._cycle_maximized_regular_pane)
            self._host._maximized_camera_shortcuts.append(cycle_shortcut)
            for regular_pane in self._host.regular_panes:
                camera_index = regular_pane.camera_index
                shortcut = QShortcut(QKeySequence(str(camera_index)), window)
                shortcut.setContext(Qt.WindowShortcut)
                shortcut.setAutoRepeat(False)
                shortcut.activated.connect(
                    lambda index=camera_index: self._host._set_maximized_regular_mode(index)
                )
                self._host._maximized_camera_shortcuts.append(shortcut)
        window.finished.connect(
            lambda _result: self._host._restore_maximized_pane(close_window=False)
        )
        self._host._host_maximized_panes((pane,))
        self._host._activate_pane(pane, align=True)
        self._host._update_maximized_mode_controls(side_by_side=False)

        screen = self._host.screen() or QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            window.resize(
                max(1, int(available.width() * 0.9)),
                max(1, int(available.height() * 0.88)),
            )
        window.show()
        if screen is not None:
            frame = window.frameGeometry()
            frame.moveCenter(available.center())
            window.move(frame.topLeft())
        window.raise_()
        window.activateWindow()
        pane.video_view.setFocus(Qt.ShortcutFocusReason)
        if self._host._maximized_roster_splitter is not None:
            self._host._maximized_roster_splitter.setSizes([max(340, window.width() // 3), max(500, window.width() * 2 // 3)])
        QTimer.singleShot(0, pane.video_view.fit_to_window)

    def _restore_hosted_panes_to_splitter(self) -> None:
        if not self._host._maximized_hosted_panes:
            return
        for pane in sorted(
            self._host._maximized_hosted_panes,
            key=lambda candidate: self._host._maximized_original_indexes.get(candidate, 0),
        ):
            pane_index = self._host._maximized_original_indexes.get(pane, 0)
            pane.setParent(self._host.evidence_splitter)
            self._host.evidence_splitter.insertWidget(max(0, pane_index), pane)
        self._host._maximized_hosted_panes = ()

    def _host_maximized_panes(
        self,
        panes: tuple[PassageEvidencePane, ...],
    ) -> None:
        content_splitter = self._host._maximized_content_splitter
        if content_splitter is None:
            return
        self._host._restore_hosted_panes_to_splitter()
        active_panes = self._host.evidence_panes
        for candidate in self._host.all_evidence_panes:
            if candidate not in panes:
                candidate.setVisible(candidate in active_panes)
                candidate.maximize_btn.setText("放大")
                candidate.maximize_btn.setToolTip("放大该机位（双击画面或按 F）")
        for pane in panes:
            content_splitter.addWidget(pane)
            pane.show()
            pane.maximize_btn.setText("缩小")
            pane.maximize_btn.setToolTip("恢复主界面（Esc 或 F）")
            content_splitter.setStretchFactor(content_splitter.indexOf(pane), 1)
        content_splitter.setSizes([1] * len(panes))
        self._host._maximized_hosted_panes = panes

    def _set_maximized_regular_mode(self, camera_index: Optional[int]) -> None:
        if self._host._maximized_window is None or not self._host.regular_panes:
            return
        if camera_index is None:
            panes = self._host.regular_panes
            active = (
                self._host._active_pane
                if self._host._active_pane in panes
                else panes[0]
            )
            side_by_side = True
        else:
            active = self._host._regular_panes_by_camera.get(int(camera_index))
            if active is None or active not in self._host.regular_panes:
                return
            panes = (active,)
            side_by_side = False
        self._host._host_maximized_panes(panes)
        self._host._maximized_pane = active
        self._host._maximized_pane_index = self._host._maximized_original_indexes.get(active, -1)
        self._host._activate_pane(active, align=True)
        active.video_view.setFocus()
        self._host._update_maximized_mode_controls(side_by_side=side_by_side)
        for pane in panes:
            QTimer.singleShot(0, pane.video_view.fit_to_window)

    def _cycle_maximized_regular_pane(self) -> None:
        panes = self._host.regular_panes
        if not panes:
            return
        active = self._host._active_pane if self._host._active_pane in panes else panes[0]
        next_index = (panes.index(active) + 1) % len(panes)
        self._host._set_maximized_regular_mode(panes[next_index].camera_index)

    def _update_maximized_mode_controls(self, *, side_by_side: bool) -> None:
        active = self._host._active_pane or self._host._maximized_pane
        if active is None:
            return
        if active.source_kind == REGULAR_SOURCE:
            current_text = f"当前判读：机位 {active.camera_index}"
        else:
            current_text = "当前判读：高速摄像"
        if self._host._maximized_mode_label is not None:
            self._host._maximized_mode_label.setText(current_text)
        if self._host._maximized_window is not None:
            identity = f" - {active._identity}" if active._identity else ""
            self._host._maximized_window.setWindowTitle(
                f"{APP_DISPLAY_NAME} · {current_text}{identity}"
            )
        for key, button in self._host._maximized_mode_buttons.items():
            checked = (
                side_by_side
                if key == "side_by_side"
                else not side_by_side
                and active.source_kind == REGULAR_SOURCE
                and key == active.camera_index
            )
            button.setChecked(bool(checked))
            button.setStyleSheet(
                "QPushButton { padding: 6px 12px; font-weight: 700; }"
                "QPushButton:checked { background: #1976c9; color: #ffffff; "
                "border: 2px solid #0f5f9f; }"
            )

    def _restore_maximized_pane(self, *, close_window: bool = True) -> None:
        pane = self._host._maximized_pane
        window = self._host._maximized_window
        if pane is None or window is None:
            return

        return_state = self._host._popup_return_state
        if self._host._maximized_roster_splitter is not None:
            self._host.workspace_splitter.insertWidget(0, self._host.results_panel)
            self._host.results_panel.show()
            self._host._maximized_roster_splitter = None
        self._host._popup_return_state = None
        self._host._restore_hosted_panes_to_splitter()
        self._host._maximized_pane = None
        self._host._maximized_window = None
        self._host._maximized_escape_shortcut = None
        self._host._maximized_pane_index = -1
        self._host._maximized_content_splitter = None
        self._host._maximized_original_indexes = {}
        self._host._maximized_mode_label = None
        self._host._maximized_mode_buttons = {}
        self._host._maximized_camera_shortcuts = []

        active_panes = self._host.evidence_panes
        for candidate in self._host.all_evidence_panes:
            candidate.setVisible(candidate in active_panes)
            candidate.maximize_btn.setText("放大")
            candidate.maximize_btn.setToolTip("放大该机位（双击画面或按 F）")
        for index in range(self._host.evidence_splitter.count()):
            self._host.evidence_splitter.setStretchFactor(index, 1)
        self._host._distribute_evidence_panes()
        QTimer.singleShot(0, self._host._distribute_evidence_panes)
        if self._host._uses_popup_judging():
            self._host.evidence_splitter.hide()
            pane.set_full_resolution_enabled(False)
            pane.set_playing(False)
            if return_state is not None:
                self._host.review_content_splitter.setSizes(return_state[0])
                self._host.video_filmstrip.full_race._position_scroll(return_state[1], center=False)
            self._host.video_filmstrip.full_race.canvas.setFocus(Qt.ShortcutFocusReason)
        if close_window:
            window.close()
        window.deleteLater()
        QTimer.singleShot(0, self._host._fit_visible_evidence_views)
