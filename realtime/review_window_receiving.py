"""Chip passage receiving of the finish review window: receiver, RaceTiger source, metadata and focus."""

from __future__ import annotations

import logging
import time
from dataclasses import replace

from .passage_receiver import PassageEvent, RaceFocus
from .racetiger_source import RaceTigerStatus
from .race_metadata import RaceMetadata
from .stream_recorder import sanitize_recording_message
from .review_window_common import (
    LIVE_EVIDENCE_DATE_TOLERANCE_MS,
    _align_live_evidence_timestamp,
    _is_test_group_name,
)
from .ui_parts import UiPart

logger = logging.getLogger("FinishReview")


class FinishReviewReceivingPart(UiPart):
    """Start/stop the passage receiver and RaceTiger source, take in passages, metadata and focus requests."""

    def start_receiver(self) -> None:
        if self._host.timing_provider == "racetiger":
            if self._host._receiver_controller.receiver is not None:
                self._host.stop_receiver()
            if (
                self._host._receiver_controller.racetiger_source is not None
                and self._host._receiver_controller.racetiger_source.is_running
            ):
                return
            try:
                self._host._start_racetiger_source()
            except Exception as exc:
                self._host._receiver_error = sanitize_recording_message(exc)
                self._host._racetiger_status = RaceTigerStatus(
                    "error",
                    f"RaceTiger: {self._host._receiver_error}",
                )
                self._host._update_runtime_status()
                raise
            return
        if self._host._receiver_controller.racetiger_source is not None:
            self._host.stop_receiver()
        receiver = self._host._receiver_controller.receiver
        if receiver is not None and receiver.is_running:
            self._host._receiver = receiver
            return
        try:
            receiver = self._host._receiver_controller.start_cyclerace(
                self._host.passage_host,
                self._host.passage_port,
                self._host._receiver_passage_store,
                on_accepted=self._host._signal_bridge.accepted.emit,
                metadata_store=self._host._receiver_metadata_store,
                on_metadata_accepted=self._host._signal_bridge.metadata_accepted.emit,
                on_focus_accepted=self._host._signal_bridge.focus_accepted.emit,
            )
        except Exception as exc:
            self._host._receiver_error = sanitize_recording_message(exc)
            self._host._update_runtime_status()
            raise
        self._host._receiver = receiver
        self._host._receiver_error = ""
        self._host._refresh_timer.start()
        self._host._update_runtime_status()

    def _start_racetiger_source(self) -> None:
        source = self._host._receiver_controller.start_racetiger(
            self._host.racetiger_base_url,
            self._host.racetiger_token,
            pc=self._host.racetiger_pc,
            rid=self._host.racetiger_rid,
            store=self._host.passage_store,
            poll_interval_seconds=self._host.racetiger_poll_interval_seconds,
            on_event=lambda event, _generation: self._host._signal_bridge.accepted.emit(event),
            on_status=lambda status, _generation: self._host._signal_bridge.timing_status.emit(status),
        )
        self._host._racetiger_source = source
        self._host._receiver_error = ""
        self._host._racetiger_status = RaceTigerStatus(
            "starting",
            "RaceTiger: polling started",
        )
        self._host._refresh_timer.start()
        self._host._update_runtime_status()

    def _passage_timestamp(self, event: PassageEvent) -> int | None:
        timestamp_ms = int(event.timeline_timestamp_ms)
        if event.passage_timestamp_ms is None and timestamp_ms < 86_400_000:
            return None
        return timestamp_ms

    def _evidence_timestamp(self, event: PassageEvent) -> int | None:
        timestamp_ms = self._host._passage_timestamp(event)
        if timestamp_ms is None:
            return None
        override = self._host._evidence_timestamp_overrides.get(event.event_id)
        if override is not None and override[0] == timestamp_ms:
            return override[1]
        return timestamp_ms

    def _is_test_passage(self, event: PassageEvent) -> bool:
        event_key = (event.race_id, event.stage_id, event.event_id)
        if event_key in self._host._preflight_event_keys:
            return True
        group_names = [event.group_id, event.group_name]
        metadata = self._host._current_metadata()
        if metadata is not None and (
            metadata.race_id == event.race_id
            and metadata.stage_id == event.stage_id
        ):
            group_names.append(metadata.group_label(event.group_id))
        return any(_is_test_group_name(name) for name in group_names)

    def _is_live_passage(self, event: PassageEvent) -> bool:
        timestamp_ms = self._host._evidence_timestamp(event)
        if timestamp_ms is None:
            return False
        received_at_ms = (
            event.received_at_ms
            if event.received_at_ms > 0
            else int(time.time() * 1000.0)
        )
        return (
            abs(int(timestamp_ms) - int(received_at_ms))
            <= LIVE_EVIDENCE_DATE_TOLERANCE_MS
        )

    def _on_passage_received(self, event: PassageEvent) -> None:
        if self._host._defer_capture_callback(self._host._on_passage_received, event):
            return
        self._host._last_passage_monotonic = time.monotonic()
        if self._host.timing_provider == "cyclerace":
            if self._host._workspace_mode == "archive":
                self._host._archive_background_passage_count += 1
                self._host._last_passage_monotonic = time.monotonic()
                self._host._update_runtime_status()
                return
            inbox_metadata = self._host._receiver_metadata_store.current()
            active_metadata = (
                self._host.metadata_store.current()
                if self._host.metadata_store is not None
                else None
            )
            if (
                inbox_metadata is not None
                and inbox_metadata.race_id == event.race_id
                and (
                    active_metadata is None
                    or (active_metadata.race_id != event.race_id
                        and self._host._live_metadata_scope == (event.race_id, event.stage_id)
                        and not self._host._other_race_while_recording(event.race_id, event.race_name))
                )
            ):
                self._host._activate_cyclerace_workspace(inbox_metadata)
                active_metadata = (
                    self._host.metadata_store.current()
                    if self._host.metadata_store is not None
                    else None
                )
            if (
                active_metadata is not None
                and (active_metadata.race_id, active_metadata.stage_id) != (event.race_id, event.stage_id)
            ):
                self._host._capture_error = (
                    f"收到其他赛事：{event.race_name or event.race_id}。"
                    "请在“设置”中打开对应赛事，或等待 CycleRace 同步赛事信息。"
                )
                self._host._update_runtime_status()
                return
            if active_metadata is None:
                scopes = {(item.race_id, item.stage_id) for item in self._host.passage_store.events()
                          if (item.race_id, item.stage_id, item.event_id) not in self._host._preflight_event_keys}
                if scopes and scopes != {(event.race_id, event.stage_id)}:
                    self._host._capture_error = "收到其他赛事或赛段的通过记录，等待 CycleRace 赛事信息"
                    self._host._update_runtime_status()
                    return
        if self._host.timing_provider == "cyclerace" and event.received_at_ms <= 0:
            event = replace(event, received_at_ms=int(time.time() * 1000.0))
        try:
            self._host.passage_store.append(event)
        except Exception as exc:
            self._host._capture_error = sanitize_recording_message(exc)
            logger.exception("Failed to store passage in active event workspace")
            self._host._update_runtime_status()
            return
        self._host._received_passage_sequence += 1
        event_key = (event.race_id, event.stage_id, event.event_id)
        self._host._received_event_order[event_key] = self._host._received_passage_sequence
        formal_timestamp_ms = self._host._passage_timestamp(event)
        if not event.is_active:
            self._host._evidence_timestamp_overrides.pop(event.event_id, None)
        elif self._host.timing_provider == "cyclerace" and formal_timestamp_ms is not None:
            alignment_reference_ms = event.received_at_ms
            aligned_timestamp_ms = _align_live_evidence_timestamp(
                formal_timestamp_ms,
                alignment_reference_ms,
            )
            existing_override = self._host._evidence_timestamp_overrides.get(event.event_id)
            if aligned_timestamp_ms != formal_timestamp_ms:
                self._host._evidence_timestamp_overrides[event.event_id] = (
                    formal_timestamp_ms,
                    aligned_timestamp_ms,
                )
            elif (
                existing_override is not None
                and existing_override[0] != formal_timestamp_ms
            ):
                self._host._evidence_timestamp_overrides.pop(event.event_id, None)
        self._host._received_passage_count += 1
        self._host._historical_passage_count = len(self._host._events_for_current_metadata(self._host.passage_store.events()))
        self._host._last_passage_monotonic = time.monotonic()
        self._host._include_high_speed_event_date(event)
        self._host._auto_start_recording_for_passage(event)
        self._host._pending_passages[event.event_id] = event
        if not self._host._passage_batch_timer.isActive():
            self._host._passage_batch_timer.start()
        self._host._update_runtime_status()

    def _flush_passage_batch(self) -> None:
        if self._host._defer_capture_callback(self._host._flush_passage_batch):
            return
        pending_events = tuple(self._host._pending_passages.values())
        self._host._pending_passages.clear()
        if not pending_events:
            return
        started = time.perf_counter()
        # These events are already durable. Show corrections and withdrawals
        # without waiting for disk scans, pin journals or media publication.
        changed = {event.event_id for event in pending_events}
        for event in pending_events:
            if event.is_active and self._host._evidence_timestamp(event) is None:
                self._host._unsupported_event_ids.add(event.event_id)
            else:
                self._host._unsupported_event_ids.discard(event.event_id)
        for event_id in changed:
            for windows in self._host._capture_windows_by_camera.values():
                windows.pop(event_id, None)
        self._host.refresh_events(changed)
        displayed_at_ms = int(time.time() * 1000)
        for event in pending_events:
            if event.received_at_ms > 0:
                self._host.runtime_metrics.observe(
                    "passage_received_to_visible",
                    max(0, displayed_at_ms - event.received_at_ms),
                    item_count=1,
                )
        self._host._apply_pending_focus()
        self._host._request_capture_refresh()
        self._host._observe_runtime_metric("passage_batch_apply", started, item_count=len(changed))
        self._host._update_runtime_status()

    def _on_metadata_received(self, metadata: RaceMetadata) -> None:
        if self._host._defer_capture_callback(self._host._on_metadata_received, metadata):
            return
        if self._host._workspace_mode != "archive" and self._host._other_race_while_recording(metadata.race_id,
                                                                                 metadata.race_name):
            return
        self._host._live_metadata_scope = (metadata.race_id, metadata.stage_id)
        if self._host._workspace_mode == "archive":
            self._host._update_runtime_status()
            return
        current = self._host.metadata_store.current() if self._host.metadata_store is not None else None
        if current is None or current.race_id != metadata.race_id:
            # Opening a race in CycleRace is not that race starting here: no folder
            # and no recording until the operator records or its first chip arrives.
            self._host._workspace_notice = (f"CycleRace 当前赛事：{metadata.race_name or metadata.race_id}。"
                                      "点「开始录像」或收到芯片时才建立本场文件夹。")
            self._host._update_runtime_status()
            return
        try:
            self._host._activate_cyclerace_workspace(metadata)
            self._host._capture_error = ""
        except Exception as exc:
            self._host._capture_error = sanitize_recording_message(exc)
            logger.exception("Failed to activate CycleRace event workspace")
            self._host._update_runtime_status()
            return
        pending_focus = self._host._pending_focus
        if pending_focus is not None and (
            pending_focus.race_id != metadata.race_id
            or pending_focus.stage_id != metadata.stage_id
        ):
            self._host._pending_focus = None
        self._host._lookup_cache.clear()
        self._host._selected_event_id = ""
        registered_event_ids = {
            event_id
            for windows in self._host._capture_windows_by_camera.values()
            for event_id in windows
        }
        for event_id in registered_event_ids:
            event = self._host.passage_store.get(event_id)
            if event is None or not event.is_active or (
                event.race_id != metadata.race_id
                or event.stage_id != metadata.stage_id
            ):
                for windows in self._host._capture_windows_by_camera.values():
                    windows.pop(event_id, None)
                self._host._unsupported_event_ids.discard(event_id)
        self._host.refresh()
        self._host._apply_pending_focus()
        self._host._request_capture_refresh()
        self._host._update_runtime_status()

    def _on_racetiger_status(self, status: RaceTigerStatus) -> None:
        self._host._racetiger_status = status
        if status.state == "error":
            self._host._receiver_error = status.message
        elif status.state == "ok":
            self._host._receiver_error = ""
        self._host._update_runtime_status()

    def _on_focus_received(self, focus: RaceFocus) -> None:
        if self._host._defer_capture_callback(self._host._on_focus_received, focus):
            return
        self._host._pending_focus = focus
        if self._host._workspace_mode == "archive":
            return
        self._host._apply_pending_focus()

    def _apply_pending_focus(self) -> bool:
        focus = self._host._pending_focus
        if focus is None:
            return False
        applied = self._host.focus_athlete(
            focus.race_id,
            focus.stage_id,
            athlete_id=focus.athlete_id,
            bib=focus.bib,
            group_id=focus.group_id,
        )
        if applied:
            self._host._update_operator_controls()
        return applied

    def stop_receiver(self) -> None:
        errors = self._host._receiver_controller.stop()
        self._host._receiver = self._host._receiver_controller.receiver
        self._host._racetiger_source = self._host._receiver_controller.racetiger_source
        if errors:
            self._host._receiver_error = "; ".join(errors)
        self._host._update_runtime_status()
