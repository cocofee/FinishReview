"""The passage queue table of the review surface: filters, search, rows, refresh and record deletion."""

from __future__ import annotations

from typing import Iterable, Optional

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QFontMetrics
from PyQt5.QtWidgets import QMessageBox, QInputDialog, QTableWidgetItem

from .passage_evidence import HIGH_SPEED_SOURCE, REGULAR_SOURCE
from .passage_receiver import PassageEvent
from .passage_batch import build_review_batches
from .race_metadata import RaceAthleteMetadata, RaceMetadata
from .video_timeline import PassageVideoLookup
from .passage_review_common import (
    format_passage_time,
    review_status_text,
    source_confirmation_status,
)
from .ui_parts import UiPart


class PassageReviewQueuePart(UiPart):
    """Queue table: group/search/review filters, row rendering, refresh and deleting a record."""

    def _on_group_changed(self) -> None:
        if self._host._batch_mode:
            self._host._exit_batch_mode()
        self._host._refresh_filtered_view()

    def _on_search_changed(self) -> None:
        if self._host._batch_mode:
            self._host._exit_batch_mode()
        self._host._search_refresh_timer.start()

    def _set_review_filter(self, filter_key: str) -> None:
        if filter_key not in self._host.review_filter_buttons:
            return
        if filter_key == self._host._active_review_filter:
            return
        if self._host._batch_mode:
            self._host._exit_batch_mode()
        self._host._active_review_filter = filter_key
        self._host._sync_review_filter_buttons()
        self._host._refresh_filtered_view()

    def _sync_review_filter_buttons(self) -> None:
        for filter_key, button in self._host.review_filter_buttons.items():
            active = filter_key == self._host._active_review_filter
            button.setChecked(active)
            button.setProperty("queueFilterActive", active)
            button.style().unpolish(button)
            button.style().polish(button)

    def _toggle_queue_expanded(self) -> None:
        sizes = self._host.workspace_splitter.sizes()
        total = sum(sizes)
        if total <= 0:
            return
        if self._host._queue_expanded:
            restore = self._host._queue_default_sizes or [310, max(1, total - 310)]
            self._host.workspace_splitter.setSizes(restore)
            self._host._queue_expanded = False
            self._host.queue_expand_btn.setToolTip("展开运动员列表")
            return
        self._host._queue_default_sizes = sizes
        queue_height = max(360, int(total * 0.58))
        self._host.workspace_splitter.setSizes([queue_height, max(1, total - queue_height)])
        self._host._queue_expanded = True
        self._host.queue_expand_btn.setToolTip("恢复运动员列表高度")

    def _matches_search(self, event: PassageEvent, query: str) -> bool:
        if not query:
            return True
        metadata_athlete = self._host._metadata_athlete_for_event(event)
        athlete_name = event.athlete_name.strip() or (
            metadata_athlete.name.strip() if metadata_athlete is not None else ""
        )
        return query in event.bib.strip().casefold() or query in athlete_name.casefold()

    def _review_status_for_event(
        self,
        event_id: str,
        lookup: PassageVideoLookup,
    ) -> str:
        regular = self._host._source_location_with_saved_association(
            event_id,
            lookup,
            high_speed=False,
        )
        high_speed = self._host._source_location_with_saved_association(
            event_id,
            lookup,
            high_speed=True,
        )
        # A manually confirmed rider may be found in the adjacent archive
        # segment rather than the segment selected from the official passage
        # timestamp. The saved association is authoritative for list status;
        # location matching is only needed when rendering a marker in a pane.
        regular_association = self._host._association_for_event(event_id, REGULAR_SOURCE)
        high_speed_association = self._host._association_for_event(event_id, HIGH_SPEED_SOURCE)
        fallback = review_status_text(lookup, regular, high_speed)
        return self._host._confirmation_status(
            regular_association,
            high_speed_association,
            fallback,
        )

    def _matches_review_filter(self, status: str) -> bool:
        if self._host._active_review_filter == "all":
            return True
        if self._host._active_review_filter == "confirmed":
            return status == "已确认"
        if self._host._active_review_filter == "blocked":
            return status == "受阻"
        return status == "待核对"

    def _current_metadata(self) -> Optional[RaceMetadata]:
        if self._host.metadata_store is None:
            return None
        return self._host.metadata_store.current()

    def _events_for_current_metadata(
        self,
        events: tuple[PassageEvent, ...],
    ) -> tuple[PassageEvent, ...]:
        metadata = self._host._current_metadata()
        if metadata is None:
            filtered = events
        else:
            filtered = tuple(
                event
                for event in events
                if event.race_id == metadata.race_id
                and event.stage_id == metadata.stage_id
            )
        return tuple(
            sorted(
                filtered,
                key=lambda event: (
                    event.timeline_timestamp_ms,
                    event.event_id,
                ),
            )
        )

    def _ensure_metadata_athlete_index(
        self,
        metadata: RaceMetadata,
    ) -> None:
        if metadata is self._host._indexed_metadata:
            return
        athletes_by_id: dict[str, RaceAthleteMetadata] = {}
        athletes_by_identity: dict[str, RaceAthleteMetadata] = {}
        for athlete in metadata.athletes:
            athletes_by_id[athlete.athlete_id] = athlete
            for identity in (athlete.bib, *athlete.chip_ids):
                key = identity.strip().casefold()
                if key:
                    athletes_by_identity.setdefault(key, athlete)
        self._host._indexed_metadata = metadata
        self._host._metadata_athletes_by_id = athletes_by_id
        self._host._metadata_athletes_by_identity = athletes_by_identity

    def _metadata_athlete_for_event(
        self,
        event: PassageEvent,
    ) -> Optional[RaceAthleteMetadata]:
        metadata = self._host._current_metadata()
        if metadata is None:
            return None
        self._host._ensure_metadata_athlete_index(metadata)
        if event.athlete_id:
            athlete = self._host._metadata_athletes_by_id.get(event.athlete_id)
            if athlete is not None:
                return athlete
        for identity in (event.bib, event.chip_id):
            athlete = self._host._metadata_athletes_by_identity.get(
                identity.strip().casefold()
            )
            if athlete is not None:
                return athlete
        return None

    def _resize_group_popup(self) -> None:
        metrics = QFontMetrics(self._host.group_combo.font())
        widest_text = max(
            (
                metrics.horizontalAdvance(self._host.group_combo.itemText(index))
                for index in range(self._host.group_combo.count())
            ),
            default=0,
        )
        popup_width = min(360, max(self._host.group_combo.minimumWidth(), widest_text + 44))
        self._host.group_combo.view().setMinimumWidth(popup_width)
        for index in range(self._host.group_combo.count()):
            self._host.group_combo.setItemData(
                index,
                self._host.group_combo.itemText(index),
                Qt.ToolTipRole,
            )

    def _update_group_combo(self, events: tuple[PassageEvent, ...]) -> bool:
        previous_group = str(self._host.group_combo.currentData() or "")
        group_labels = {
            event.group_id: event.group_name.strip() or event.group_id
            for event in events
        }
        metadata = self._host._current_metadata()
        if metadata is not None:
            group_labels.update(
                {group.group_id: group.name for group in metadata.groups}
            )
        expected_items = [
            (group_id, group_label)
            for group_id, group_label in sorted(
                group_labels.items(), key=lambda item: (item[1], item[0])
            )
        ]
        current_items = [
            (
                str(self._host.group_combo.itemData(index) or ""),
                self._host.group_combo.itemText(index),
            )
            for index in range(1, self._host.group_combo.count())
        ]
        if current_items == expected_items:
            self._host._resize_group_popup()
            return False
        self._host.group_combo.blockSignals(True)
        self._host.group_combo.clear()
        self._host.group_combo.addItem("全部组别", "")
        for group_id, group_label in expected_items:
            self._host.group_combo.addItem(group_label, group_id)
        group_index = self._host.group_combo.findData(previous_group)
        self._host.group_combo.setCurrentIndex(max(0, group_index))
        self._host.group_combo.blockSignals(False)
        self._host._resize_group_popup()
        return str(self._host.group_combo.currentData() or "") != previous_group

    def _write_event_row(
        self,
        row: int,
        event: PassageEvent,
        lookup: PassageVideoLookup,
    ) -> None:
        regular = self._host._source_location_with_saved_association(
            event.event_id,
            lookup,
            high_speed=False,
        )
        high_speed = self._host._source_location_with_saved_association(
            event.event_id,
            lookup,
            high_speed=True,
        )
        readiness_status = review_status_text(lookup, regular, high_speed)
        regular_association = self._host._source_association(
            event.event_id, REGULAR_SOURCE, regular
        )
        high_speed_association = self._host._source_association(
            event.event_id, HIGH_SPEED_SOURCE, high_speed
        )
        review_status = self._host._confirmation_status(
            regular_association,
            high_speed_association,
            readiness_status,
        )
        display_review_status = self._host._display_confirmation_status(review_status)
        self._host._event_review_statuses[event.event_id] = review_status
        self._host._record_summary_state(
            event.event_id,
            regular,
            high_speed,
            readiness_status,
            regular_association,
            high_speed_association,
        )
        metadata_athlete = self._host._metadata_athlete_for_event(event)
        identity = event.bib.strip() or "未知"
        athlete_name = event.athlete_name.strip() or (
            metadata_athlete.name.strip() if metadata_athlete is not None else ""
        ) or "--"
        values = (
            str(row + 1),
            identity,
            athlete_name,
            event.group_name.strip() or event.group_id,
            str(event.lap),
            format_passage_time(event.timeline_timestamp_ms),
            source_confirmation_status(regular, regular_association),
            source_confirmation_status(high_speed, high_speed_association),
            display_review_status,
        )
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            if column in {0, 4, 5, 6, 7, 8}:
                item.setTextAlignment(Qt.AlignCenter)
            if column == 0:
                item.setData(Qt.UserRole, event.event_id)
            if column in {6, 7, 8}:
                self._host._apply_status_style(item, value)
            self._host.table.setItem(row, column, item)

    def _renumber_visible_rows(self) -> None:
        for row, event in enumerate(self._host._visible_events):
            item = self._host.table.item(row, 0)
            if item is None:
                item = QTableWidgetItem()
                item.setTextAlignment(Qt.AlignCenter)
                self._host.table.setItem(row, 0, item)
            item.setText(str(row + 1))
            item.setData(Qt.UserRole, event.event_id)

    def _render_filtered_queue(
        self,
        events: tuple[PassageEvent, ...],
        previous_event_id: str,
        changed_event_ids: set[str] | None = None,
    ) -> None:
        previous_ids = [event.event_id for event in self._host._visible_events]
        scroll = self._host.table.verticalScrollBar().value()
        if self._host._batch_mode:
            self._host._visible_events = list(events)
        else:
            selected_group = str(self._host.group_combo.currentData() or "")
            query = self._host.identity_search.text().strip().casefold()
            self._host._visible_events = [
                event
                for event in events
                if (not selected_group or event.group_id == selected_group)
                and self._host._matches_search(event, query)
                and self._host._matches_review_filter(
                    self._host._event_review_statuses.get(event.event_id, "待核对")
                )
            ]

        self._host.table.blockSignals(True)
        self._host.table.setUpdatesEnabled(False)
        desired_ids = [event.event_id for event in self._host._visible_events]
        order_changed = previous_ids != desired_ids
        selected_row = -1
        try:
            if changed_event_ids is None:
                self._host.table.setRowCount(len(self._host._visible_events))
                for row, event in enumerate(self._host._visible_events):
                    self._host._write_event_row(row, event, self._host._lookups[event.event_id])
            else:
                desired = set(desired_ids)
                for row in range(len(previous_ids) - 1, -1, -1):
                    if previous_ids[row] not in desired:
                        self._host.table.removeRow(row)
                        previous_ids.pop(row)
                for row, event in enumerate(self._host._visible_events):
                    inserted = row >= len(previous_ids) or previous_ids[row] != event.event_id
                    if inserted:
                        if event.event_id in previous_ids:
                            old_row = previous_ids.index(event.event_id)
                            self._host.table.removeRow(old_row)
                            previous_ids.pop(old_row)
                        self._host.table.insertRow(row)
                        previous_ids.insert(row, event.event_id)
                    if inserted or event.event_id in changed_event_ids:
                        self._host._write_event_row(row, event, self._host._lookups[event.event_id])
                if order_changed:
                    self._host._renumber_visible_rows()
            if changed_event_ids is None or order_changed:
                self._host._event_row_by_id = {event_id: row for row, event_id in enumerate(desired_ids)}
            selected_row = self._host._event_row_by_id.get(previous_event_id, -1)
        finally:
            self._host.table.setUpdatesEnabled(True)

        if selected_row < 0 and self._host._visible_events:
            selected_row = 0
        if selected_row >= 0 and (changed_event_ids is None or order_changed
                                  or self._host.table.currentRow() != selected_row):
            self._host.table.setCurrentCell(selected_row, 1)
            self._host.table.selectRow(selected_row)
        self._host.table.blockSignals(False)
        self._host.table.schedule_auto_fit()
        self._host.delete_record_button.setEnabled(selected_row >= 0)
        self._host.table.verticalScrollBar().setValue(scroll)

        self._host._render_summary()
        if selected_row >= 0:
            selected_event_id = self._host._visible_events[selected_row].event_id
            active_pane = self._host._camera_one_pane()
            preserve_current_frame = (
                active_pane
                if selected_event_id == self._host._selected_event_id
                and getattr(active_pane, "_current_frame_index", -1) >= 0
                else None
            )
            if (changed_event_ids is None or selected_event_id != self._host._selected_event_id
                    or selected_event_id in changed_event_ids):
                self._host._select_event(
                    selected_event_id, preserve_current_frame=preserve_current_frame,
                )
        else:
            self._host._clear_selection_details()

    def _update_review_more_actions(self) -> None:
        for action, control, title in self._host._review_more_actions:
            action.setText(title or control.text())
            action.setEnabled(control.isEnabled())
            action.setChecked(control.isChecked())
            action.setToolTip(control.toolTip())
            action.setVisible(
                control not in (self._host.preview_mark_btn, self._host.preview_confirm_btn)
                or self._host._top_preview_video_visible
            )

    def _refresh_filtered_view(self) -> None:
        events = self._host._events_for_current_metadata(self._host.passage_store.events())
        metadata = self._host._current_metadata()
        metadata_context_key = (
            (metadata.race_id, metadata.stage_id)
            if metadata is not None
            else ("", "")
        )
        event_ids = {event.event_id for event in events}
        if (
            metadata_context_key != self._host._metadata_context_key
            or self._host._timeline_cache_signature() != self._host._timeline_signature
            or event_ids != set(self._host._lookups)
            or event_ids != set(self._host._event_review_statuses)
        ):
            self._host.refresh()
            return
        self._host._total_event_count = len(events)
        self._host._render_filtered_queue(events, self._host._selected_event_id, set())

    def refresh(self) -> None:
        previous_event_id = self._host._selected_event_id
        metadata = self._host._current_metadata()
        metadata_context_key = (
            (metadata.race_id, metadata.stage_id)
            if metadata is not None
            else ("", "")
        )
        if metadata_context_key != self._host._metadata_context_key:
            self._host._metadata_context_key = metadata_context_key
            self._host.video_filmstrip.full_race.clear()
            previous_event_id = ""
            self._host.identity_search.clear()
        events = self._host._events_for_current_metadata(self._host.passage_store.events())
        self._host._total_event_count = len(events)
        signature = self._host._timeline_cache_signature()
        if signature != self._host._timeline_signature:
            self._host._timeline_signature = signature
            self._host._lookup_cache = {
                event_id: cached
                for event_id, cached in self._host._lookup_cache.items()
                if self._host._cache_survives_timeline_update(cached[1])
            }

        self._host._update_group_combo(events)
        self._host._review_batches = build_review_batches(
            events,
            review_gap_ms=self._host.REVIEW_BATCH_GAP_MS,
            subwave_gap_ms=self._host.REVIEW_SUBWAVE_GAP_MS,
        )
        self._host._review_batch_by_event_id = {
            event_id: batch
            for batch in self._host._review_batches
            for event_id in batch.event_ids
        }
        self._host._reconcile_video_discoveries(self._host.passage_store.events())
        if self._host._active_review_batch_id and not any(
            batch.batch_id == self._host._active_review_batch_id
            for batch in self._host._review_batches
        ):
            self._host._batch_mode = False
            self._host._active_review_batch_id = ""
        self._host._lookups = {
            event.event_id: self._host._cached_lookup(event)
            for event in events
        }
        self._host._located_event_ids.clear()
        self._host._confirmed_event_ids.clear()
        self._host._event_review_statuses.clear()
        for event in events:
            self._host._project_event_status(event)

        self._host._render_filtered_queue(events, previous_event_id)
        self._host._update_filmstrip()
        self._host._refresh_camera_judgments()

    def _project_event_status(self, event: PassageEvent) -> None:
        lookup = self._host._lookups[event.event_id]
        regular = self._host._source_location_with_saved_association(
            event.event_id,
            lookup,
            high_speed=False,
        )
        high_speed = self._host._source_location_with_saved_association(
            event.event_id,
            lookup,
            high_speed=True,
        )
        regular_association = self._host._source_association(
            event.event_id,
            REGULAR_SOURCE,
            regular,
        )
        high_speed_association = self._host._source_association(
            event.event_id,
            HIGH_SPEED_SOURCE,
            high_speed,
        )
        readiness_status = review_status_text(lookup, regular, high_speed)
        self._host._event_review_statuses[event.event_id] = self._host._confirmation_status(
            regular_association,
            high_speed_association,
            readiness_status,
        )
        self._host._record_summary_state(
            event.event_id,
            regular,
            high_speed,
            readiness_status,
            regular_association,
            high_speed_association,
        )

    def refresh_events(self, event_ids: Iterable[str]) -> None:
        changed = {str(event_id) for event_id in event_ids if event_id}
        if not changed:
            return
        metadata = self._host._current_metadata()
        context = (metadata.race_id, metadata.stage_id) if metadata is not None else ("", "")
        if context != self._host._metadata_context_key:
            self._host.refresh()
            return
        events = self._host._events_for_current_metadata(self._host.passage_store.events())
        by_id = {event.event_id: event for event in events}
        self._host._total_event_count = len(events)
        self._host._update_group_combo(events)
        signature = self._host._timeline_cache_signature()
        if signature != self._host._timeline_signature:
            self._host._timeline_signature = signature
            self._host._lookup_cache = {
                event_id: cached for event_id, cached in self._host._lookup_cache.items()
                if self._host._cache_survives_timeline_update(cached[1])
            }
        for event_id in changed:
            self._host._lookup_cache.pop(event_id, None)
            event = by_id.get(event_id)
            if event is None:
                self._host._lookups.pop(event_id, None)
                self._host._event_review_statuses.pop(event_id, None)
                self._host._discard_summary_state(event_id)
            else:
                self._host._lookups[event_id] = self._host._cached_lookup(event)
                self._host._project_event_status(event)
        # Handle a first snapshot that also contains events whose UI notification
        # was coalesced with this batch, without rebuilding existing table items.
        for event in events:
            if event.event_id not in self._host._lookups:
                self._host._lookups[event.event_id] = self._host._cached_lookup(event)
                self._host._project_event_status(event)
                changed.add(event.event_id)
        self._host._review_batches = build_review_batches(
            events, review_gap_ms=self._host.REVIEW_BATCH_GAP_MS,
            subwave_gap_ms=self._host.REVIEW_SUBWAVE_GAP_MS,
        )
        self._host._review_batch_by_event_id = {
            event_id: batch for batch in self._host._review_batches for event_id in batch.event_ids
        }
        self._host._reconcile_video_discoveries(self._host.passage_store.events())
        if self._host._active_review_batch_id and not any(
            batch.batch_id == self._host._active_review_batch_id for batch in self._host._review_batches
        ):
            self._host._batch_mode = False
            self._host._active_review_batch_id = ""
        self._host._render_filtered_queue(events, self._host._selected_event_id, changed)
        self._host._update_navigation_controls()
        self._host._refresh_camera_judgments()

    @staticmethod
    def _status_color(value: str) -> QColor:
        if value == "已确认":
            return QColor("#16845b")
        if value == "未确认":
            return QColor("#c0372b")
        if value in {"异常", "受阻"}:
            return QColor("#c0372b")
        if value == "待核对":
            return QColor("#7d5b0c")
        return QColor("#526170")

    @classmethod
    def _apply_status_style(cls, item: QTableWidgetItem, value: str) -> None:
        item.setForeground(cls._status_color(value))
        font = item.font()
        font.setBold(value in {
            "未确认",
            "异常",
            "受阻",
            "已确认",
        })
        item.setFont(font)

    def _delete_selected_record(self) -> None:
        row = self._host.table.currentRow()
        if not 0 <= row < len(self._host._visible_events):
            return
        self._host._delete_record(self._host._visible_events[row])

    def _delete_record(self, event: PassageEvent) -> None:
        store = self._host.passage_store
        identity = " ".join(value for value in (event.bib, event.athlete_name) if value)
        reason, accepted = QInputDialog.getText(
            self._host, "删除记录",
            f"删除 {identity or event.event_id} 在 "
            f"{format_passage_time(event.timeline_timestamp_ms)} 的通过记录。\n"
            "请填写原因（只在本机隐藏；CycleRace 改了号码、芯片或时间会重新显示）：",
        )
        if not accepted or self._host.passage_store is not store:
            return
        if not str(reason).strip():
            QMessageBox.information(self._host, "删除记录", "请填写删除原因。")
            return
        try:
            deleted = store.delete(event.event_id, reason=str(reason).strip())
        except (OSError, RuntimeError) as error:
            QMessageBox.critical(self._host, "删除失败", f"无法删除记录：{error}")
            return
        self._host.refresh()
        if deleted:
            self._host.record_deleted.emit(event.event_id)
