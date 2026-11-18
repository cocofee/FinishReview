"""Problem list linking of the finish review window: counting, problem frames and crossing cards."""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path

from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QLabel, QInputDialog, QPushButton, QSizePolicy

from .evidence_clock import align_evidence_timestamp
from .passage_review import PassageReviewSurface
from .runtime_paths import relocated_video_path
from .video_timeline import DEFAULT_CLOCK_SOURCE
from .review_window_common import PROBLEM_FRAME_TOLERANCE_MS
from .ui_parts import UiPart


class FinishReviewProblemsPart(UiPart):
    """Problem list layout and selection, counting on the picture, problem frames and crossing cards."""

    def _set_problem_list_layout(self, expanded: bool) -> None:
        self._host._problem_workbench_active = expanded
        self._host.set_workbench_compact_mode(expanded, allow_multi_camera_maximize=True)
        # Workbench: F belongs to the problem's own picture.  The time rail would
        # otherwise swallow the press for its own judging window (operator: "F does
        # not work"), and the press has to land wherever the hands are.
        self._host.video_filmstrip.full_race.set_enlarge_key_enabled(not expanded)
        # In the workbench the picture already fills the room; F enlarges nothing.
        for pane in self._host.evidence_panes:
            pane._maximize_shortcut.setEnabled(not expanded)
            if not expanded and hasattr(pane, "set_passed_count"):
                pane.set_passed_count("")
                if getattr(pane, "_header_strip", None) is not None:
                    strip = pane._header_strip
                    pane.set_header_strip(None)
                    strip.setParent(self._host.problem_panel)
                    strip.hide()
                    pane.video_view.set_target_badge("")
                    self._host._slot_pane = None
        self._host.setMinimumHeight(700 if expanded else 760)
        self._host._set_compact_header(expanded)
        self._host.problem_panel.card.setVisible(not expanded)
        self._host.problem_panel.filmstrip.setVisible(not expanded)
        self._host.filter_bar.setVisible(not expanded)
        self._host.table.setVisible(not expanded)
        self._host.video_discovered_table.setVisible(False)
        self._host.transport.setVisible(not expanded)
        self._host.preview_panel.setMinimumHeight(190 if expanded else 420)
        self._host.video_filmstrip.setMinimumHeight(180 if expanded else 360)
        self._host.workspace_splitter.setMinimumHeight(300 if expanded else 180)
        self._host._review_split_manually_sized = expanded
        if expanded:
            self._host._selected_event_id = ""
            self._host.problem_panel.splitter.insertWidget(1, self._host.evidence_splitter)
            self._host.problem_panel.splitter.setSizes([360, 1000, 0])
            self._host.review_content_splitter.insertWidget(0, self._host.workspace_splitter)
            self._host.review_content_splitter.setSizes([520, 210])
            self._host.evidence_splitter.show()
            self._host.auto_advance_checkbox.setChecked(False)
        else:
            self._host.workspace_splitter.addWidget(self._host.evidence_splitter)
            self._host.review_content_splitter.insertWidget(0, self._host.preview_panel)
            self._host._set_continuous_review_layout(False)
        self._host.return_problem_button.setVisible(expanded)
        self._host.manual_problem_button.setVisible(expanded)
        self._host.pipeline_label.setVisible(expanded)
        self._host.crossing_cards.setVisible(False)
        self._host.recording_rail_button.setVisible(expanded)
        if not getattr(self._host, "_picture_hint_bound", False):
            self._host._picture_hint_bound = True
            self._host.problem_panel.hint.changed.connect(self._host.picture_hint.set_hint)
        self._host.picture_hint.set_hint(self._host.problem_panel.hint.text())
        self._host.picture_hint.setVisible(expanded)
        self._host.problem_panel.hint.setVisible(not expanded)
        self._host._set_recording_rail_visible(not expanded or self._host.recording_rail_button.isChecked())
        self._host._problem_count_mode_changed(expanded)
        self._host._update_operator_controls()

    def _init_problem_list(self) -> None:
        from .quick_review_panel import QuickReviewPanel

        self._host.problem_panel = QuickReviewPanel(self._host.results_panel)
        self._host.problem_panel.set_frame_provider(
            lambda: self._host._frame_for_problem_count(self._host._active_playback_pane())
        )
        self._host.problem_panel.confirmation_preparing.connect(self._host._pause_arrival_confirmation)
        self._host.problem_panel.candidate_preparing.connect(self._host._prepare_arrival_candidate)
        self._host.problem_panel.sampling_preparing.connect(self._host._prepare_sampling_frame)
        self._host.problem_panel.sampling_configured.connect(self._host._refresh_problem_finish_lines)
        self._host.results_panel.layout().insertWidget(1, self._host.problem_panel)
        self._host.problem_panel.hide()
        self._host.problem_panel.set_events_provider(
            lambda: self._host.passage_store.events(include_inactive=True)
        )
        self._host.problem_panel.set_metadata_provider(
            lambda: self._host.metadata_store.current() if getattr(self._host, "metadata_store", None) is not None else None
        )
        self._host.problem_panel.jump_requested.connect(self._host._open_problem_frame)
        self._host.problem_panel.current_changed.connect(self._host._problem_selection_changed)
        self._host.problem_panel.big_view_changed.connect(self._host._problem_big_view_changed)
        self._host.problem_panel.marks_changed.connect(self._host._problem_marks_changed)
        self._host.problem_panel.count_mode_changed.connect(self._host._problem_count_mode_changed)
        self._host.problem_panel.frame_step_requested.connect(self._host._step_active_pane)
        self._host.problem_panel.run_requested.connect(self._host._run_problem_playback)
        self._host.problem_panel.run_speed.connect(self._host._run_problem_speed)
        self._host.problem_panel.clear_data_requested.connect(self._host._clear_event_data)
        self._host.problem_panel.delete_requested.connect(self._host._delete_problem_record)
        self._host.problem_panel.calibration_requested.connect(self._host._calibrate_crossing_frame)
        self._host.problem_panel.summary_changed.connect(lambda _text: self._host._refresh_crossing_cards())
        self._host._normal_crossing_cards = {}
        self._host._opened_normal_crossing = ""
        # The raw recording is offered once, before any card exists; whether the
        # operator keeps it on screen afterwards is their own choice.
        self._host._rail_suggested = False
        from .crossing_widgets import CrossingCardRail
        self._host.crossing_cards = CrossingCardRail(self._host)
        self._host.crossing_cards.item_selected.connect(self._host._select_crossing_card)
        self._host.preview_panel.layout().addWidget(self._host.crossing_cards)
        # 过线记录已经是左边那张表，横排卡片是同一批数据的第二面：收起来，
        # 只留下查找原录像；校时在判读工具条最前面。
        self._host.crossing_cards.list.hide()
        self._host.crossing_cards.title.hide()
        self._host.recording_rail_button = QPushButton("查找原录像", self._host.crossing_cards)
        self._host.recording_rail_button.setCheckable(True)
        self._host.recording_rail_button.toggled.connect(self._host._set_recording_rail_visible)
        # 11-14: it sat alone on a band under the picture; it shares the picture's button line now.
        self._host.evidence_panes[0].controls_layout.addWidget(self._host.recording_rail_button)
        self._host.recording_rail_button.hide()
        # 11-14: the advice sits beside the picture's buttons, not at the foot of the window.
        from .problem_panel import PictureHint
        self._host.picture_hint = PictureHint(self._host)
        controls = self._host.evidence_panes[0].controls_layout
        controls.insertWidget(controls.indexOf(self._host.evidence_panes[0].time_label) + 1, self._host.picture_hint, 3)
        self._host.picture_hint.hide()
        self._host._creating_manual_crossing = False
        # 校时 lives on the judging toolbar (QuickReviewPanel.calibrate_button), not down here.
        self._host.group_start_button = QPushButton("回到团首", self._host)
        self._host.group_start_button.clicked.connect(self._host._open_group_start)
        self._host.evidence_panes[0].controls_layout.addWidget(self._host.group_start_button)
        self._host.group_start_button.hide()
        self._host.return_problem_button = QPushButton("回到当前问题", self._host)
        self._host.return_problem_button.clicked.connect(self._host._return_to_problem)
        self._host.manual_problem_button = QPushButton("＋ 补记过线", self._host)
        self._host.manual_problem_button.clicked.connect(self._host._add_manual_problem)
        rail = self._host.video_filmstrip.full_race
        rail.toolbar.insertWidget(3, self._host.return_problem_button)
        rail.toolbar.insertWidget(4, self._host.manual_problem_button)
        rail.follow_button.setProperty("alwaysVisible", True)
        rail.follow_button.show()
        rail.navigation_started.connect(self._host._problem_browse_started)
        rail.follow_button.toggled.connect(lambda enabled: self._host._problem_browse_started() if enabled else None)
        self._host.pipeline_label = QLabel("", self._host.problem_panel.status_widget)
        self._host.pipeline_label.setStyleSheet("color:#17675b;")
        self._host.pipeline_label.setWordWrap(True)
        self._host.pipeline_label.setMinimumWidth(0)
        self._host.pipeline_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self._host.problem_panel.status_bar.insertWidget(0, self._host.pipeline_label, 1)
        self._host.problem_panel.set_workspace(self._host.output_dir)

    def _problem_selection_changed(self, item):
        self._host._opened_normal_crossing = ""
        if hasattr(self._host, "group_start_button"):
            group = item is not None and item.kind in ("bunch_check", "obstruction")
            selected = getattr(self._host.problem_panel, "_selected_arrival", lambda: None)()
            wave = getattr(self._host.problem_panel, "_row_wave", lambda _row: None)(selected)
            self._host.group_start_button.setVisible(group or len(wave or ()) > 1)
        if getattr(self._host, "_problem_workbench_active", False):
            if getattr(self._host.problem_panel, "holds_picture", False):
                # The panel places (or keeps) the picture for this row itself.
                self._host._opened_normal_crossing = ""
                self._host.video_filmstrip.full_race.follow_button.setChecked(False)
            elif not getattr(self._host, "_creating_manual_crossing", False):
                self._host._return_to_problem()
            self._host._refresh_crossing_cards()
            self._host._problem_count_mode_changed(True)

        # 表里点到的是一条自动对应的记录：那是在复查这条过线，点画面不算新建待处理。
        # 放在 _return_to_problem 之后，因为回看问题本身会清掉这个标记。
        # 到达记录表不同：点到人就算数，自动对应的单人也一样。
        from .problem_items import KIND_AUTO_PASSED
        if (item is not None and item.kind == KIND_AUTO_PASSED
                and not getattr(self._host.problem_panel, "quick_review", False)):
            self._host._opened_normal_crossing = item.item_id

    def _pause_arrival_confirmation(self):
        # Freeze the displayed frame without seeking to a playback clock estimate.
        self._host._set_sync_playing(False, seek_final=False)
        self._host._active_playback_pane().set_playing(False)

    def _prepare_arrival_candidate(self):
        """Raw filmstrip review does not need to wait for automatic video analysis."""

        panel = self._host.problem_panel
        pane = self._host._active_playback_pane()
        frame = self._host._frame_for_problem_count(pane)
        if frame is None:
            return
        item = panel.current_item()
        if item is not None:
            if item.evidence.get("configuration_stale") or item.evidence.get("superseded"):
                return
            original = item.evidence.get("frame")
            from .bunch_review import mark_on_frame
            if any(mark_on_frame(mark, frame, frame["recorder_ms"]) for mark in panel.current_marks()):
                return
            if original and self._host._shows_the_problem_frame(pane, frame["position_ms"], original):
                return
        selected_key = panel._selected_key
        self._host._pause_arrival_confirmation()
        try:
            self._host._creating_manual_crossing = True
            location = self._host._persist_filmstrip_location(pane.location)
            pane._location = location
            frame.update(segment_id=location.segment.segment_id, video_path=str(location.video_path))
            item = panel.add_manual_problem(frame, reason="人工查看录像候选", recorder_ms=frame["recorder_ms"],
                                            thumbnail=pane.video_view._pixmap_item.pixmap())
            if item is not None:
                panel.select_item(item.item_id)
                index = next((i for i, row in enumerate(panel._arrival_rows) if row.key == selected_key), None)
                if index is not None:
                    # Restore only the identity: clicking would reopen its chip
                    # reference and discard the raw frame being classified.
                    panel._selected_key = selected_key
                    panel._active_item = item
                    panel.table.setCurrentCell(index, 0)
                    panel._sync_arrival_confirmation()
        except (OSError, ValueError, RuntimeError) as error:
            panel.hint.setText(f"当前原帧未保存：{error}")
        finally:
            self._host._creating_manual_crossing = False

    def _refresh_problem_finish_lines(self):
        """Resolve the real line for the displayed camera, session and recording time."""

        if not hasattr(self._host, "problem_panel"):
            return
        from .analysis_settings import CONFIG_PATH, inherited_profile, normalized_points, profile_scope_matches
        from .analysis_workspace import session_id_for_path

        paths = [self._host.output_dir / name for name in ("long_image_lines.json", "finish_lines.json", CONFIG_PATH)]
        try:
            signature = tuple((str(path), path.stat().st_mtime_ns, path.stat().st_size)
                              if path.exists() else (str(path),) for path in paths)
            if signature != getattr(self._host, "_review_line_signature", None):
                lines, settings = {}, {}
                for path in paths:
                    payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
                    if path == paths[-1]:
                        if not isinstance(payload, dict):
                            raise ValueError("分析配置必须是JSON对象")
                        settings = payload
                    records = payload.get("finish_lines", []) if isinstance(payload, dict) else payload
                    for line in records:
                        lines[int(line["camera_index"])] = line
                self._host._review_line_config = (lines, settings.get("sampling_profiles", []))
                self._host._review_line_signature = signature
            lines, profiles = self._host._review_line_config
            scope = self._host.problem_panel._arrival_scope
            for pane in self._host.evidence_panes:
                frame = self._host._frame_for_problem_count(pane)
                line = None
                if frame and pane.source_kind == "regular":
                    session = re.sub(r"_review_\d+$", "", session_id_for_path(pane.location.video_path))
                    scope_known = bool(scope[0] or scope[1])
                    matches = [p for p in profiles if int(p["camera_index"]) == pane.camera_index
                               and p["session_id"] == session
                               and (not scope_known or profile_scope_matches(p, *scope))
                               and int(p["effective_from_ms"]) <= frame["recorder_ms"]]
                    # Like the analysis: a session without its own line shows the one it inherits.
                    line = (max(matches, key=lambda p: (int(p["effective_from_ms"]), int(p["revision"])))
                            if matches else inherited_profile(
                                profiles, pane.camera_index, frame["recorder_ms"], scope[0])
                            or lines.get(pane.camera_index))
                pane.video_view.set_finish_line(normalized_points(line) if line else ())
            active = self._host._active_playback_pane()
            self._host.problem_panel.set_finish_line_status(bool(active.video_view._finish_line_points))
        except (OSError, ValueError, KeyError, TypeError):
            for pane in self._host.evidence_panes:
                pane.video_view.set_finish_line()
            self._host.problem_panel.set_finish_line_status(False)

    def _problem_big_view_changed(self, big: bool) -> None:
        """Give the enlarged picture the room of the rail, and fit the frame to it.

        A 4:3 crossing only grows with the height it is given, so while one
        problem is judged the time rail steps aside; the frame is re-fitted to the
        new area both ways, the same as the old judging window did when it opened.
        """

        if getattr(self._host, "_problem_workbench_active", False):
            self._host.preview_panel.setVisible(not big)
        QTimer.singleShot(0, self._host._fit_visible_evidence_views)
        self._host._problem_count_mode_changed(True)
        self._host._refresh_problem_finish_lines()

    def _problem_pane(self):
        """The pane the operator judges on: the problem's own camera, else the regular one."""

        item = self._host.problem_panel.current_item()
        if item is None:
            return self._host.regular_pane
        camera = int(item.evidence.get("frame", {}).get("camera_index")
                     or item.evidence.get("camera_index") or 1)
        return self._host._camera_one_pane() if camera <= 1 else self._host._pane_for_camera(camera)

    def _problem_count_mode_changed(self, _counting: bool) -> None:
        """Every decoded regular-camera picture can record a crossing, large or small."""

        active = getattr(self._host, "_problem_workbench_active", False)
        quick_review = bool(getattr(self._host.problem_panel, "quick_review", False))
        for pane in self._host.evidence_panes:
            pane.set_identity_marker_enabled(not (active and quick_review))
            pane.video_view.set_count_mode(active
                                           and (not quick_review or pane is self._host._active_playback_pane())
                                           and pane.source_kind == "regular"
                                           and pane.location is not None and pane._current_frame_index >= 0)

    def _frame_for_problem_count(self, pane):
        if (pane is None or pane.location is None or pane._current_frame_index < 0
                or not pane.video_view.has_frame):
            return None
        origin = pane.location.segment.media_started_at_ms
        if origin is None:
            return None
        return {"camera_index": pane.camera_index, "segment_id": pane.location.segment.segment_id,
                "video_path": str(pane.location.video_path), "frame_index": pane._current_frame_index,
                "position_ms": pane._current_position_ms,
                "recorder_ms": int(origin) + int(pane._current_position_ms)}

    def _prepare_sampling_frame(self):
        for pane in self._host.evidence_panes:
            try:
                locations = self._host._live_locations_for_filmstrip(pane)
                location = locations[-1] if locations else pane.location
                if location is not None:
                    saved = self._host._persist_filmstrip_location(location)
                    if location is pane.location:
                        pane._location = saved
            except (OSError, ValueError, RuntimeError) as error:
                self._host.problem_panel.hint.setText(f"实时画面暂存失败，终点线仍可手工设置：{error}")

    def _problem_count_cancelled(self, x, y, pane):
        if (getattr(self._host, "_problem_workbench_active", False)
                and getattr(self._host.problem_panel, "quick_review", False)
                and pane is self._host._active_playback_pane()):
            self._host.problem_panel.cancel_picture_mark(x, y)

    def _problem_wheel_step(self, delta: int, pane) -> None:
        """The wheel over the counting picture: hold whatever was playing, one frame on or back."""

        if not getattr(self._host, "_problem_workbench_active", False):
            return
        stop = getattr(self._host.problem_panel, "stop_run", None)
        if stop is not None:
            stop()
        self._host._step_pane(pane, int(delta))

    def _problem_count_clicked(self, x_normalized: float, y_normalized: float, pane=None) -> None:
        """A click records the displayed frame; an unreported crossing starts its own item."""

        if not getattr(self._host, "_problem_workbench_active", False):
            return
        pane = pane or self._host._problem_pane()
        frame = self._host._frame_for_problem_count(pane)
        if frame is None:
            return
        if getattr(self._host.problem_panel, "quick_review", False):
            self._host._quick_review_mark_arrival(frame, x_normalized, y_normalized, pane)
            return
        item = self._host.problem_panel.current_item()
        context = self._host.problem_panel._inline_context()
        normal = self._host._normal_crossing_cards.get(self._host._opened_normal_crossing)
        if normal is not None and self._host._shows_the_problem_frame(pane, frame["position_ms"], normal.evidence["frame"]):
            self._host.problem_panel.hint.setText("此单人过线已自动对应；发现其他人时，可点「＋ 补记过线」另记原帧。")
            return
        item_frame = (item.evidence.get("frame") or {}) if item is not None else {}
        if context is not None and pane.camera_index != context.camera:
            self._host.problem_panel.hint.setText(f"本团按机位 {context.camera} 点数；其他机位可回看，避免同一人重复计数。")
            return
        own = bool(item and item_frame and (
            (int(item_frame.get("camera_index", 1)) == pane.camera_index
             and self._host._shows_the_problem_frame(pane, frame["position_ms"], item_frame))
            or (context is not None and pane.camera_index == context.camera
                and context.start_ms <= frame["recorder_ms"] <= context.end_ms)))
        settled = item is not None and self._host.problem_panel.state(item).startswith(("已处理", "待 CycleRace"))
        if settled and own:
            self._host.problem_panel.hint.setText("此过线已处理；如需修改，请先重新处理。")
            return
        if not own or item.status in ("waiting", "excluded", "info", "closed_upstream"):
            try:
                self._host._creating_manual_crossing = True
                location = self._host._persist_filmstrip_location(pane.location)
                pane.location = location
                frame["segment_id"], frame["video_path"] = location.segment.segment_id, str(location.video_path)
                item = self._host.problem_panel.add_manual_problem(
                    frame, recorder_ms=frame["recorder_ms"], thumbnail=pane.video_view._pixmap_item.pixmap())
            except (OSError, ValueError, RuntimeError) as error:
                self._host.problem_panel.hint.setText(f"过线未保存：{error}")
                return
            finally:
                self._host._creating_manual_crossing = False
        if item is None:
            return
        self._host.problem_panel.set_browsing(False)
        self._host.problem_panel.add_mark(x_normalized, y_normalized, frame["recorder_ms"], frame)
        self._host._problem_count_mode_changed(True)
        self._host._refresh_crossing_cards()
        self._host.problem_panel.setFocus()

    def _run_problem_playback(self, playing) -> None:
        """The arrival list plays the recording on to the next person, or holds it: what Space does."""

        pane = self._host._active_playback_pane()
        if pane is not None and bool(playing) != bool(pane.is_playing):
            self._host._toggle_pane(pane)

    def _run_problem_speed(self, speed) -> None:
        """A run through a stretch the strip shows empty plays fast; a pause ends that."""

        pane = self._host._active_playback_pane()
        if pane is not None:
            pane.set_play_speed(speed)

    def _toggle_active_pane(self) -> None:
        """Space.  On the arrival list's wave it plays on to the next person and stops (11-12).

        古浪 big bunch: Space played the recording straight on and everyone went
        past before one could be clicked.  A run playing is held; plain playing
        is paused; with no wave on screen Space plays as it always did.
        """

        panel = getattr(self._host, "problem_panel", None)
        if getattr(self._host, "_problem_workbench_active", False) and getattr(panel, "quick_review", False):
            pane = self._host._active_playback_pane()
            if pane is not None and pane.is_playing:
                if panel.stop_run():
                    return
            elif pane is not None and panel.play_on():
                return
        PassageReviewSurface._toggle_active_pane(self._host)

    def _quick_review_mark_arrival(self, frame, x_normalized, y_normalized, pane) -> None:
        """Use the operator's picture click as the bicycle cue, not a final ruling."""

        panel = self._host.problem_panel
        calibrated, go_on = self._click_calibration(pane, frame)
        if not go_on:
            return
        row = panel._selected_arrival()
        item, refusal = panel.picture_candidate(frame)
        if refusal:
            panel.hint.setText("；".join(part for part in (calibrated, refusal) if part))
            return
        frame = dict(frame, marker_x_normalized=float(x_normalized),
                     marker_y_normalized=float(y_normalized))
        selected_key = row.key if row is not None and row.event is not None else None
        reuse = item is not None
        self._host._pause_arrival_confirmation()
        try:
            location = self._host._persist_filmstrip_location(pane.location)
            pane._location = location
            frame.update(segment_id=location.segment.segment_id, video_path=str(location.video_path))
            self._host._creating_manual_crossing = True
            if not reuse:
                item = panel.add_manual_problem(
                    frame, reason="人工点选自行车与终点线位置，待确认芯片",
                    recorder_ms=frame["recorder_ms"], thumbnail=pane.video_view._pixmap_item.pixmap(),
                )
                panel.adopt_picture_candidate(item, selected_key)
        except (OSError, ValueError, RuntimeError) as error:
            panel.hint.setText(f"标记未保存：{error}")
            return
        finally:
            self._host._creating_manual_crossing = False
        if item is None:
            return
        self._host._creating_manual_crossing = True
        try:
            if item.item_id in panel._candidate_wave:
                panel.open_picture(item)  # the click's own wave; the frame stays where it is
            else:
                panel.select_item(item.item_id)
        finally:
            self._host._creating_manual_crossing = False
        from .bunch_review import mark_on_frame
        existing = next((i for i, mark in enumerate(panel.current_marks())
                         if mark_on_frame(mark, frame, frame["recorder_ms"])
                         and (mark["x"] - x_normalized) ** 2 + (mark["y"] - y_normalized) ** 2 < .02 ** 2), None)
        builds = panel._table_builds
        if existing is None:
            panel.add_mark(x_normalized, y_normalized, frame["recorder_ms"], frame)
        else:
            panel._editing_mark = existing
        index = next((i for i, entry in enumerate(panel._arrival_rows) if entry.key == selected_key), None)
        wave = panel._wave_plan() is not None
        if wave:
            # On a wave's picture the row of whoever was clicked is selected.
            panel.show_counted(panel._editing_mark)
        elif index is not None:
            panel._selected_key = selected_key
            panel._active_item = item
            panel.table.setCurrentCell(index, 0)
            panel._sync_arrival_confirmation()
        if not (wave and panel._table_builds != builds):
            # Saving the wave's count filled the table already, and the row of
            # whoever was clicked is selected: a second fill only cost the
            # operator a fifth of a second a click (古浪, 11-14).
            panel._quick_render_signature = None
            panel._render()
        self._host._problem_count_mode_changed(True)
        self._host._problem_marks_changed(())
        counted = (panel.picture_mark_hint()
                   or (f"已标记 {row.bib} 的压线位置。"
                       if selected_key else "已圈出压线位置；请在左边选对应的芯片记录。"))
        panel.hint.setText(f"{calibrated}。{counted}" if calibrated else counted)

    def _problem_marks_changed(self, _marks) -> None:
        """Redraw the counted people on the frame that is being judged."""

        pane = self._host._problem_pane()
        if pane is not None:
            pane.video_view.set_history_markers(self._host._continuous_marker_overlays(pane))
            self._host._show_problem_count_badge(pane)

    def _show_problem_count_badge(self, pane) -> None:
        """已过 N 人 · X 号 above the picture, read against the timing software."""
        text = ""
        if (getattr(self._host, "_problem_workbench_active", False) and pane is self._host._problem_pane()
                and getattr(self._host.problem_panel, "quick_review", False)
                and hasattr(self._host.problem_panel, "passed_count_text")):
            text = self._host.problem_panel.passed_count_text(self._host._frame_for_problem_count(pane))
            if hasattr(self._host.problem_panel, "refresh_slots") and hasattr(pane, "set_header_strip"):
                # The cells of who is counted take the place of 已过 N 人 · X 号 (11-14).
                holder = getattr(self._host, "_slot_pane", None)
                if holder is not None and holder is not pane:
                    holder.set_header_strip(None)  # another camera's picture had them: its own header is back
                self._host._slot_pane = pane
                pane.set_header_strip(self._host.problem_panel.wave_slots)
                self._host.problem_panel.refresh_slots(text.split(" · ")[0])
                if hasattr(pane.video_view, "set_target_badge"):
                    _key, words, kind = self._host.problem_panel.current_target()
                    pane.video_view.set_target_badge(words, kind)
        if hasattr(pane, "set_passed_count"):
            pane.set_passed_count(text)

    def _problem_count_overlays(self, pane):
        """Numbered marks for the people counted on the problem being judged."""

        if not getattr(self._host, "_problem_workbench_active", False) or pane is not self._host._problem_pane():
            return ()
        frame = self._host._frame_for_problem_count(pane)
        if frame is None:
            return ()
        if hasattr(self._host.problem_panel, "picture_overlays"):
            return tuple(self._host.problem_panel.picture_overlays(frame))
        from .bunch_review import is_excluded, mark_on_frame
        from .crossing_order import mark_order_labels, ordered_mark_indices
        marks = self._host.problem_panel.current_marks()
        labels = mark_order_labels(marks)
        dot = bool(getattr(self._host.problem_panel, "quick_review", False))
        # A person beyond the wave's chips, or without a chip, is drawn in red.
        warning = getattr(self._host.problem_panel, "picture_mark_warning", lambda _mark: False)
        overlays = []
        for index in ordered_mark_indices(marks):
            mark = marks[index]
            if not mark_on_frame(mark, frame, frame["recorder_ms"]):
                continue
            if dot:
                label = self._host.problem_panel.picture_mark_label(mark)
                reason = mark.get("exclusion_reason", "").strip() if is_excluded(mark) else ""
                label += f" 已排除：{reason}" if reason and reason != label else ""
            else:
                label = (labels[index] + (f" {mark['bib']}" if mark.get("bib") else "")
                         + (f" 已排除：{mark['exclusion_reason']}" if is_excluded(mark) else ""))
            alert = bool(warning(mark))
            overlays.append((float(mark["x"]), float(mark["y"]), label, not alert, alert, dot))
        return tuple(overlays)

    def _problem_browse_started(self):
        if getattr(self._host, "_problem_workbench_active", False):
            self._host.problem_panel.set_browsing(True)

    def _return_to_problem(self):
        self._host._opened_normal_crossing = ""
        item = self._host.problem_panel.current_item()
        self._host.video_filmstrip.full_race.follow_button.setChecked(False)
        if item is not None and item.evidence.get("frame"):
            self._host.problem_panel.set_browsing(True)
            self._host._open_problem_frame(dict(item.evidence["frame"]))
        elif item is not None:
            self._host._set_sync_playing(False, seek_final=False)
            if self._host._open_chip_reference(item):
                return
            for pane in self._host.evidence_panes:
                pane.clear_passage("该芯片时刻暂无可回看录像；请检查录像覆盖或从下方胶卷查找")
            self._host.problem_panel.set_browsing(False)
        else:
            self._host.problem_panel.set_browsing(False)
            if not any(pane.video_view.has_frame for pane in self._host.evidence_panes):
                recent = [entry for entry in (self._host.problem_panel.payload or {}).get("auto_passed", []) if entry.get("frame")]
                if recent:
                    latest = max(recent, key=lambda entry: entry.get("recorder_ms", entry.get("chip_time_ms", 0)))
                    self._host._open_problem_frame(dict(latest["frame"]))
                else:
                    for pane in self._host.evidence_panes:
                        pane.clear_passage(self._host.problem_panel.queue_text() + "；可从原录像打开画面校时或补记")

    def _open_chip_reference(self, item) -> bool:
        """Raw playback must not depend on detection, line setup or calibration."""
        events = [self._host.passage_store.get(event_id) for event_id in item.event_ids]
        event = next((event for event in events if event is not None and event.is_active), None)
        if event is None:
            reference = item.evidence.get("reference_frame")
            if reference:
                return self._host._open_problem_frame(dict(reference, reference_only=True))
            return False
        timestamp = self._host._evidence_timestamp(event)
        if timestamp is None:
            return False
        timestamp = align_evidence_timestamp(timestamp, event.received_at_ms)
        camera = int(item.evidence.get("camera_index") or self._host._camera_one_pane().camera_index)
        # A chip reference uses the same session calibration as normal playback.
        # Rebuild it on every open: a cached reference predates later calibration.
        offsets = {self._host._clock_offset_for_camera(camera)}
        for segment in self._host.timeline_store.segments():
            if segment.camera_index == camera and segment.clock_source == DEFAULT_CLOCK_SOURCE:
                key = self._host._recording_session_key_from_path(
                    segment.source_id, self._host.timeline_store.resolve_video_path(segment))
                offsets.add(self._host._calibrated_clock_offset(camera, key))
        locations = []
        for offset in offsets:
            lookup = self._host.timeline_store.locate_passage(
                timestamp, clock_offset_ms=offset, race_id=event.race_id,
                pre_roll_ms=0, prefer_continuous_media=True,
            )
            locations.extend(location for location in lookup.locations
                             if location.status == "located" and location.segment.camera_index == camera
                             and location.segment.clock_source == DEFAULT_CLOCK_SOURCE
                             and self._host._continuous_offset_for_location(location) == offset)
        if not locations:
            return False
        # Immutable short clips are preferred, but a sealed archive can cover
        # a missing clip without waiting for any analysis cache to catch up.
        location = min(locations, key=lambda value: (
            value.video_path.suffix.lower() == ".m3u8",
            value.segment.media_duration_ms or 0,
        ))
        return self._host._open_problem_frame({
            "camera_index": camera, "segment_id": location.segment.segment_id,
            "video_path": str(location.video_path), "position_ms": location.passage_position_ms,
            "reference_only": True,
            "date_aligned": timestamp != event.timeline_timestamp_ms,
        })

    def _shows_the_problem_frame(self, pane, position_ms: int, frame: dict) -> bool:
        """Whether what is on the screen is this problem's own frame.

        A two-minute archive counts as the same moment as the clip it covers, so the
        comparison follows the recording clock whenever both sides know it, and falls
        back to the file only when they do not: judging a rebound archive is still
        judging this crossing, and the panel must not call that "browsing".
        """

        location = getattr(pane, "location", None)
        if location is None:
            return False
        source = self._host.timeline_store.get_segment(str(frame.get("segment_id", "")))
        on_screen = location.segment.media_started_at_ms
        wanted = getattr(source, "media_started_at_ms", None)
        if on_screen is None or wanted is None:
            same = location.segment.segment_id == str(frame.get("segment_id", ""))
            return same and abs(int(position_ms) - int(frame.get("position_ms", 0))) <= PROBLEM_FRAME_TOLERANCE_MS
        return abs((on_screen + int(position_ms))
                   - (wanted + int(frame.get("position_ms", 0)))) <= PROBLEM_FRAME_TOLERANCE_MS

    def _add_manual_problem(self):
        self._host._opened_normal_crossing = ""
        pane = self._host._active_playback_pane()
        frame = self._host._frame_for_problem_count(pane)
        if frame is None:
            self._host.problem_panel.hint.setText("请先从原录像打开一张可用画面，再点选手补记过线。")
            return
        try:
            self._host._creating_manual_crossing = True
            location = self._host._persist_filmstrip_location(pane.location)
            pane.location = location
            frame["segment_id"], frame["video_path"] = location.segment.segment_id, str(location.video_path)
            self._host.problem_panel.add_manual_problem(
                frame, recorder_ms=frame["recorder_ms"], thumbnail=pane.video_view._pixmap_item.pixmap())
            self._host.problem_panel.hint.setText("过线原帧已记住；点选手计数，号码可以稍后再填。")
        except (OSError, ValueError, RuntimeError) as error:
            self._host.problem_panel.hint.setText(f"过线未保存：{error}")
        finally:
            self._host._creating_manual_crossing = False
        self._host._problem_count_mode_changed(True)

    def _open_group_start(self):
        if getattr(self._host.problem_panel, "open_wave_start", lambda: False)():
            return
        context = self._host.problem_panel._inline_context()
        if context is None:
            return
        segment = next((source for source in context.segments
                        if source.start_ms <= context.start_ms <= source.end_ms), None)
        if segment is None:
            self._host.problem_panel.hint.setText("团首录像存在缺口，请检查证据。")
            return
        position = context.start_ms - segment.start_ms
        self._host._open_problem_frame({"camera_index": context.camera, "segment_id": segment.segment.segment_id,
                                  "video_path": str(segment.path), "position_ms": position})

    def _calibration_events(self):
        """(events that can anchor a calibration, by chip time; or a reason there are none)."""

        from .analysis_workspace import AnalysisWorkspace
        metadata = self._host._current_metadata()
        try:
            workspace = AnalysisWorkspace(self._host.output_dir,
                                          race_id=metadata.race_id if metadata else "",
                                          stage_id=metadata.stage_id if metadata else "")
        except (OSError, ValueError) as error:
            return [], f"等待本场有效芯片记录：{error}"
        eligible = {read.event_id for read in workspace.reads
                    if read.active_now and not read.excluded and not read.manual_entry
                    and read.bib and read.chip_ms == read.latest_chip_ms}
        events = [event for event in self._host.passage_store.events() if event.event_id in eligible
                  and event.is_active and event.bib and event.timeline_timestamp_ms > 0
                  and (event.race_id, event.stage_id) == (workspace.race_id, workspace.stage_id)]
        events.sort(key=lambda event: event.timeline_timestamp_ms)
        return events, "" if events else "等待本场有效芯片记录；画面可以先补记过线。"

    def _save_calibration(self, pane, frame, event):
        """Calibrate the shown recording from ``event``'s chip; the offset, or None (hint says why)."""

        current = self._host.passage_store.get(event.event_id)
        if current != event or not current.is_active:
            self._host.problem_panel.hint.setText("这条芯片记录已变化，请重新选择压线选手校时。")
            return None
        try:
            location = self._host._persist_filmstrip_location(pane.location)
            pane._location = location
            frame = dict(frame, segment_id=location.segment.segment_id, video_path=str(location.video_path))
            session_key = self._host._recording_session_key(location)
            offset = frame["recorder_ms"] - int(event.timeline_timestamp_ms)
            self._host.calibration_store.record(camera_index=pane.camera_index, session_key=session_key,
                                          offset_ms=offset, anchor_event_id=event.event_id, anchor_bib=event.bib,
                                          calibrated_at_ms=int(time.time() * 1000),
                                          anchor_event_revision=event.revision,
                                          anchor_chip_time_ms=event.timeline_timestamp_ms,
                                          anchor_race_id=event.race_id, anchor_stage_id=event.stage_id,
                                          anchor_frame=frame)
            self._host._continuous_clock_offsets[(pane.camera_index, session_key)] = offset
            self._host._continuous_calibration_revision += 1
            self._host._lookup_cache.clear()
        except (OSError, ValueError, RuntimeError) as error:
            self._host.problem_panel.hint.setText(f"校时未保存：{error}")
            return None
        panel = self._host.problem_panel
        panel._viewed_clock = (int(pane.camera_index), True, int(offset))  # green at once, on this recording
        panel._last_live_signature = None
        panel._next_analysis = 0
        if not panel._live_paused and panel._process is None:
            panel._start_analysis()
        panel.calibration_done()
        return offset

    @staticmethod
    def _calibration_words(event, offset):
        late = "晚" if offset >= 0 else "早"
        return (f"已按 {event.bib} 号校时：录像比芯片{late} {abs(offset) / 1000:.1f} 秒，"
                "表格和空格停点已按新校时；不对就点「已校时」按钮再点一次")

    def _click_calibration(self, pane, frame):
        """Calibrate on this click when the recording has no clock yet or 重新校时 is armed.

        11-18 "我只需要在运动员过线那里点一下，就可以校时": the person clicked
        is the one due next (下一位 X 号 on a wave, else the selected row).
        Returns (calibration words or "", go on counting).
        """
        panel = self._host.problem_panel
        armed = bool(getattr(panel, "_recalibrate_armed", False))
        if not armed:
            from .crossing_time import recorder_crossing_time
            shown = recorder_crossing_time(frame["recorder_ms"], frame, self._host.output_dir)
            if shown is None or shown.source != "video":
                return "", True
        plan = panel._run_plan()
        row = plan.unmatched[0] if plan is not None and plan.unmatched else panel._selected_arrival()
        target = getattr(row, "event", None)
        events, reason = self._calibration_events()
        event = next((event for event in events if target is not None and event.event_id == target.event_id), None)
        if event is None:
            if armed:
                panel.hint.setText(reason or "重新校时：先在左表选这位压线选手的芯片记录，再左键点他。")
                return "", False
            return "", True   # nothing to calibrate from: count as before, the button stays red
        offset = self._save_calibration(pane, frame, event)
        if offset is None:
            return "", not armed
        return self._calibration_words(event, offset), True

    def _calibrate_crossing_frame(self):
        """更多 → 按列表选人校时: pick the person on the shown frame from a short list."""

        pane = self._host._active_playback_pane()
        frame = self._host._frame_for_problem_count(pane)
        if frame is None:
            self._host.problem_panel.hint.setText("先把画面停在这位选手压线的那一帧，再按列表选人校时。")
            self._host.recording_rail_button.setChecked(True)
            return
        events, reason = self._calibration_events()
        if not events:
            self._host.problem_panel.hint.setText(reason)
            return
        # 11-18 "这一堆，根本看不懂": the bib and the chip time are all one needs.
        labels = [f"{event.bib} 号 · "
                  f"{datetime.fromtimestamp(event.timeline_timestamp_ms / 1000).strftime('%H:%M:%S.%f')[:-3]}"
                  for event in events]
        selected = self._host.problem_panel._selected_arrival()
        default = next((i for i, candidate in enumerate(events)
                        if selected and selected.event and candidate.event_id == selected.event.event_id), 0)
        label, ok = QInputDialog.getItem(self._host, "按列表选人校时", "当前画面压线的是哪位选手？",
                                         labels, default, False)
        if not ok or label not in labels:
            return
        event = events[labels.index(label)]
        offset = self._save_calibration(pane, frame, event)
        if offset is not None:
            self._host.problem_panel.hint.setText(self._calibration_words(event, offset) + "。")

    def _set_recording_rail_visible(self, visible):
        workbench = getattr(self._host, "_problem_workbench_active", False)
        self._host.video_filmstrip.setVisible(bool(visible) or not workbench)
        self._host.crossing_cards.setVisible(False)
        # Without the recording rail the bottom holds two buttons only: give the
        # picture the height instead of leaving an empty band.
        compact = workbench and not visible
        self._host.preview_panel.setMinimumHeight(0 if compact else 190 if workbench else 420)
        self._host.preview_panel.setMaximumHeight(0 if compact else 16777215)
        self._host.preview_panel.setVisible(not compact)

    def _refresh_crossing_cards(self):
        if not hasattr(self._host, "crossing_cards"):
            return
        panel = self._host.problem_panel
        from .problem_items import KIND_AUTO_PASSED
        # 自动对应的正常单人已经是左边表里的一行（「全部」按时间排），不再另造一份卡片。
        normal = {item.item_id: item for item in panel.items if item.kind == KIND_AUTO_PASSED}
        self._host._normal_crossing_cards = normal
        if self._host._opened_normal_crossing and self._host._opened_normal_crossing not in normal:
            self._host._opened_normal_crossing = ""
        if not self._host._rail_suggested:
            # Before any candidate is available the raw recording stays reachable:
            # the first calibration and a crossing the analysis never reported.
            self._host._rail_suggested = True
            if not panel.rows and not normal:
                self._host.recording_rail_button.setChecked(True)
        self._host._set_recording_rail_visible(self._host.recording_rail_button.isChecked())

    def _select_crossing_card(self, item_id):
        if getattr(self._host.problem_panel, "quick_review", False):
            self._host.problem_panel.select_item(item_id)
            return
        normal = self._host._normal_crossing_cards.get(item_id)
        if normal is not None:
            self._host._opened_normal_crossing = item_id
            self._host._open_problem_frame(dict(normal.evidence["frame"]))
            self._host.problem_panel.set_browsing(True)
            self._host.problem_panel.hint.setText(f"{normal.bibs[0]} 已自动对应，正在复查原帧。")
            self._host.crossing_cards.set_selected(item_id)
            return
        self._host._opened_normal_crossing = ""
        for row, item in enumerate(self._host.problem_panel.rows):
            if item.item_id == item_id:
                self._host.problem_panel.table.setCurrentCell(row, 0)
                self._host._return_to_problem()
                break

    def _open_problem_list(self) -> None:
        expanded = self._host.problem_list_button.isChecked()
        self._host.problem_panel.setVisible(expanded)
        self._host._set_problem_list_layout(expanded)
        self._host._refresh_problem_list()
        if expanded:
            self._host._return_to_problem()

    def _delete_problem_record(self, event_id: str) -> None:
        event = self._host.passage_store.get(event_id)
        if event is not None:
            self._host._delete_record(event)
            self._host.problem_panel.refresh()

    def _open_passages(self):
        self._host.problem_list_button.setChecked(False)
        self._host._open_problem_list()
        self._host._refresh_filtered_view()

    def _refresh_problem_list(self) -> None:
        panel = getattr(self._host, "problem_panel", None)
        if panel is None:
            return
        panel.set_workspace(self._host.output_dir)
        metadata = self._host._current_metadata()
        panel.set_scope(metadata.race_id if metadata else "", metadata.stage_id if metadata else "")
        if panel.isVisible():
            panel.refresh()
        result = (panel.payload or {}).get("cameras", {}).get(str(self._host._camera_one_pane().camera_index), {})
        ranges = [(start, min(end, result.get("analyzed_through_ms", end)))
                  for start, end, _session in result.get("coverage", []) if not result.get("error")]
        current = panel.current_item()
        frame = current.evidence.get("frame", {}) if current else {}
        segment = self._host.timeline_store.get_segment(frame.get("segment_id", ""))
        timestamp = (segment.media_started_at_ms + int(frame.get("position_ms", 0))) if (
            segment and segment.media_started_at_ms is not None and segment.camera_index == self._host._camera_one_pane().camera_index
        ) else None
        self._host.video_filmstrip.full_race.set_analysis_progress(ranges, timestamp, enabled=self._host._problem_workbench_active)

    def _open_problem_frame(self, frame) -> bool:
        """Open the item's original frame in its camera's pane, without changing identity."""

        from .video_timeline import PassageVideoLocation

        segment = self._host.timeline_store.get_segment(str(frame.get("segment_id", "")))
        # Follows the same file into this race folder if the folder was moved (11-05).
        path = relocated_video_path(str(frame.get("video_path", "")), self._host.output_dir)
        if segment is None or not path.is_file():
            self._host.problem_panel.hint.setText("原录像缺失或不在本赛事的录像索引中，未跳转其他录像。")
            return False
        position = int(frame.get("position_ms", 0))
        index = None if frame.get("frame_index") is None else int(frame["frame_index"])
        camera = int(frame.get("camera_index", 1) or 1)
        location = PassageVideoLocation(segment, path, position, position, 0,
                                        segment.timing_error_ms, "located")
        location, position, index = self._host._problem_frame_source(location, position, index)
        self._host._set_sync_playing(False, seek_final=False)
        if location.segment.media_started_at_ms is not None:
            self._host.video_filmstrip.full_race.browse_to(location.segment.media_started_at_ms + position)
        opened = self._host._seek_retained_camera_frame(location, position, index, camera)
        if opened:
            # The pane is on the problem's own frame now, so the panel is not
            # browsing elsewhere and may be decided on: F would otherwise leave the
            # operator looking at the right picture while every button stays greyed.
            reference = bool(frame.get("reference_only"))
            self._host.problem_panel.set_browsing(reference)
            target = f"第 {index + 1} 帧" if index is not None else f"片内 {position / 1000:.2f} 秒"
            self._host._active_playback_pane().set_reference_only(reference)
            if frame.get("auto_pair"):
                self._host.problem_panel.hint.setText("已定位自动对应的过线原帧；灰蓝虚线圈为系统对应（鼠标移到圈上看号码），不算人工点数或裁判确认。")
            elif reference:
                date_note = "；芯片日期与接收日期不同，按接收日期查找，正式时间未改" if frame.get("date_aligned") else ""
                self._host.problem_panel.hint.setText(
                    f"参考录像 · {target}：尚未校时/匹配，不代表已确认过线{date_note}。请逐帧找到压线帧，左键点人。"
                )
            else:
                self._host.problem_panel.hint.setText(f"已定位原录像{target}，请逐帧核对后处理。")
        # The problem's pane is the one that takes the counting clicks from here on.
        self._host._problem_count_mode_changed(True)
        self._host._problem_marks_changed(None)
        return opened

    def _archive_on_piece_clock(self) -> bool:
        return bool(getattr(self._host, "_problem_workbench_active", False))

    def _problem_frame_source(self, location, position, index):
        """Where a row's frame is shown: (location, position, frame index).

        A two-second buffer piece plays two seconds and stops, and a lead-in
        before it (2 s for a crossing without a chip) falls in the piece
        before (10-29: "2 秒割裂").  Once the five-minute archive of that moment
        is sealed the frame opens there, on the very picture and on the pieces'
        clock (see archive_alignment), so the rings saved on pieces stay on
        their people; an archive whose picture is not found is not used.
        Before that, a moment ahead of the piece opens in the piece holding it.
        """

        from .race_filmstrip import recording_sources

        segment = location.segment
        if segment.media_started_at_ms is None:
            return location, max(0, position), index
        moment = int(segment.media_started_at_ms) + int(position)
        if self._host._is_archive(location):
            return self._host._aligned_archive(location, moment), position, index
        session = self._host._recording_session_key(location)
        sources, _pending = recording_sources(
            self._host.timeline_store, int(segment.camera_index), self._host._filmstrip_race_id(),
            path_available=lambda _path, _live: True)  # only the files used are checked
        holding = [source for source in sources
                   if source.start_ms <= moment < source.end_ms
                   and self._host._recording_session_key(source.location) == session
                   and Path(source.location.video_path).is_file()]
        archive = next((source.location for source in holding if self._host._is_archive(source.location)), None)
        if archive is not None:
            duration = max(1, int(segment.media_duration_ms or 1))
            shown = int(segment.media_started_at_ms) + min(max(0, int(position)), duration - 1)
            aligned = self._host._aligned_archive(archive, moment, (location, shown))
            if aligned is not archive:
                return aligned, max(0, moment - int(aligned.segment.media_started_at_ms)), None
        if position >= 0:
            return location, position, index
        piece = next((source for source in holding if source.priority == 1), None)
        if piece is not None:
            return piece.location, moment - piece.start_ms, None
        return location, 0, index
