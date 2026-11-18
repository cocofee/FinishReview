"""Recording of the finish review window: start/stop, preflight, health and camera recovery, reminders."""

from __future__ import annotations

import logging
import shutil
import time
from dataclasses import replace
from datetime import datetime

from PyQt5.QtWidgets import QMessageBox

from .passage_receiver import PassageEvent
from .passage_review_common import source_location
from .preflight import PreflightRun
from .recording_controller import PendingRecordingPassage, RecordingStopFailure
from .review_recorder import PassageReviewState, is_supported_review_source
from .stream_recorder import RecordingError, sanitize_recording_message
from .video_timeline import (
    DEFAULT_CLOCK_SOURCE,
    DEFAULT_TIMING_ERROR_MS,
    PassageVideoLocation,
    RecordingSegment,
)
from .launch_dialog import (
    _is_rtsp_auth_error as _is_rtsp_auth_error,
    _format_rtsp_probe_error as _format_rtsp_probe_error,
)
from .ui_parts import UiPart

logger = logging.getLogger("FinishReview")


class FinishReviewRecordingPart(UiPart):
    """Start and stop recording, preflight checks, recording health and camera recovery, live filmstrip locations."""

    def _configured_recording_sources(self) -> tuple[tuple[int, str], ...]:
        sources = []
        if is_supported_review_source(self._host.source):
            sources.append((self._host.camera_index, self._host.source))
        if is_supported_review_source(self._host.secondary_source):
            sources.append((self._host.camera_index + 1, self._host.secondary_source))
        return tuple(sources)

    def _recording_any_active(self) -> bool:
        return any(recorder.is_running for recorder in self._host._recorders.values())

    def _recording_all_active(self) -> bool:
        configured = self._host._configured_recording_sources()
        return bool(configured) and len(self._host._recorders) == len(configured) and all(
            self._host._recorders.get(camera_index) is not None
            and self._host._recorders[camera_index].is_running
            for camera_index, _source in configured
        )

    def _toggle_recording(self) -> None:
        if self._host._recording_any_active():
            # Stopped on purpose: no reminder for this race again.  The race the
            # chips name counts too; without race details here the folder's own
            # id is empty and the stop used to be forgotten.
            for race_id in (self._host._race_being_recorded(), self._host._last_chip_race_id):
                if race_id:
                    self._host._auto_recorded_race_ids.add(race_id)
            self._host.stop_recording()
            return
        if self._host._workspace_mode == "archive":
            self._host._return_to_live_event()
            return
        if not is_supported_review_source(self._host.source):
            self._host._configure_devices()
            if not is_supported_review_source(self._host.source):
                return
        try:
            self._host._enter_race_opened_in_cyclerace()
            self._host.start_recording()
        except Exception as exc:  # noqa: BLE001 - GUI boundary reports device failures.
            self._host._runtime_error = sanitize_recording_message(exc)
            QMessageBox.critical(self._host, "无法开始录像", self._host._runtime_error)
            self._host._update_runtime_status()

    def _preflight_evidence_status(
        self,
        event: PassageEvent,
    ) -> tuple[bool, bool, str, str]:
        try:
            lookup = self._host._cached_lookup(event)
            high_speed = source_location(lookup, high_speed=True)
        except Exception as error:  # noqa: BLE001 - keep the test status visible.
            detail = sanitize_recording_message(error)
            return False, False, detail, detail
        required_camera_indexes = {
            camera_index
            for camera_index, _source in self._host._configured_recording_sources()
        }
        ready_camera_indexes = {
            location.segment.camera_index
            for location in lookup.locations
            if location.segment.clock_source == DEFAULT_CLOCK_SOURCE
            and location.status in {"located", "near_boundary", "unverified"}
        }
        regular_ready = bool(required_camera_indexes) and required_camera_indexes.issubset(
            ready_camera_indexes
        )
        high_speed_ready = high_speed is not None
        regular_detail = (
            "普通录像全部机位已覆盖测试时间点"
            if regular_ready
            else "等待普通录像覆盖："
            + "、".join(
                f"机位{camera_index}"
                for camera_index in sorted(
                    required_camera_indexes - ready_camera_indexes
                )
            )
        )
        high_speed_detail = (
            "高速画面已找到并可打开"
            if high_speed_ready
            else (
                self._host._high_speed_scan_result.message
                or "等待Auyat完成判读和保存"
            )
        )
        return regular_ready, high_speed_ready, regular_detail, high_speed_detail

    def _start_preflight_recording(self) -> bool:
        if self._host._recording_all_active():
            return True
        if not is_supported_review_source(self._host.source):
            return False
        try:
            self._host.start_recording()
        except Exception as error:  # noqa: BLE001 - dialog shows the runtime error.
            self._host._runtime_error = sanitize_recording_message(error)
            self._host._update_runtime_status()
            return False
        return self._host._recording_all_active()

    def _record_preflight_event(self, run: PreflightRun) -> None:
        if not run.passed:
            return
        try:
            self._host._preflight_journal.append(
                run,
                recorded_at_ms=int(time.time() * 1000.0),
            )
        except OSError as error:
            self._host._capture_error = sanitize_recording_message(error)
            self._host._update_runtime_status()
            return
        event_key = (run.race_id, run.stage_id, run.event_id)
        is_new = event_key not in self._host._preflight_event_keys
        self._host._preflight_event_keys.add(event_key)
        if is_new:
            self._host.refresh()

    def _restore_latest_preflight_event(self) -> tuple[bool, str]:
        entry = self._host._preflight_journal.latest_entry()
        if entry is None:
            return False, "当前没有被隔离的联调记录。"
        event_key = (
            str(entry.get("race_id") or "").strip(),
            str(entry.get("stage_id") or "").strip(),
            str(entry.get("event_id") or "").strip(),
        )
        self._host._preflight_journal.restore(
            event_key,
            recorded_at_ms=int(time.time() * 1000.0),
        )
        self._host._preflight_event_keys.discard(event_key)
        self._host.refresh()
        identity = str(entry.get("bib") or event_key[2]).strip()
        return True, f"{identity} 已恢复到正式复核列表；原始计时记录未被修改。"

    def start_recording(self) -> None:
        if self._host._workspace_mode == "archive":
            raise RecordingError("历史赛事模式不能开始录像，请先返回当前赛事")
        if self._host._recording_all_active():
            return
        self._host._invalidate_capture_refresh()
        self._host._stop_archive_video_scan_workers()
        if self._host._recorders:
            self._host.stop_recording()
        configured_sources = self._host._configured_recording_sources()
        if not configured_sources:
            raise RecordingError("请先在设备设置中选择录像摄像头")
        self._host._camera_reconnect_attempts.clear()
        self._host._camera_reconnect_not_before.clear()
        self._host._camera_reconnect_errors.clear()
        self._host._camera_auth_failed_sources.clear()
        self._host._stop_requested = False
        try:
            free_bytes = self._host._run_capture_task(lambda: shutil.disk_usage(self._host.output_dir).free)
        except OSError as exc:
            raise RecordingError(f"无法检查赛事存储空间: {exc}") from exc
        self._host._storage_snapshot = (self._host.output_dir, free_bytes / (1024**3), "")
        if free_bytes < 1024**3:
            raise RecordingError("赛事存储空间不足 1 GB，无法开始录像")
        self._host._recording_lock.acquire(self._host.output_dir)
        archive_publishers = []
        recording_started_ms = int(time.time() * 1000)
        try:
            pipelines = self._host._run_capture_task(lambda: self._host._recording_controller.start(
                sources=configured_sources,
                output_dir=self._host.output_dir,
                ffmpeg_path=self._host.ffmpeg_path,
                review_retention_seconds=self._host.review_retention_seconds,
                timeline_store=self._host.timeline_store,
                timing_error_ms=self._host.timing_error_ms,
                binding_store=self._host.review_binding_store,
            ))
            self._host._recorders = self._host._recording_controller.recorders
            self._host._ring_buffers = self._host._recording_controller.ring_buffers
            self._host._coordinators = self._host._recording_controller.coordinators
            self._host._publishers = self._host._recording_controller.timeline_publishers
            archive_publishers = [pipeline.archive_publisher for pipeline in pipelines]
            recorders = self._host._recorders
            ring_buffers = self._host._ring_buffers
            coordinators = self._host._coordinators
            publishers = self._host._publishers
            self._host._recorder = recorders.get(self._host.camera_index)
            self._host._ring_buffer = ring_buffers.get(self._host.camera_index)
            self._host._coordinator = coordinators.get(self._host.camera_index)
            self._host._publisher = publishers.get(self._host.camera_index)
            self._host._video_scan_workers = {}
            if self._host._video_assist_enabled():
                for camera_index, ring_buffer in ring_buffers.items():
                    generation = self._host._video_scan_generation
                    token = object()
                    self._host._video_scan_tokens[camera_index] = token
                    callback = (
                        lambda candidates, generation=generation,
                        camera_index=camera_index, token=token: (
                            self._host._on_video_candidates(
                                candidates,
                                generation,
                                worker_token=(camera_index, token),
                            )
                        )
                    )
                    if len(configured_sources) == 1:
                        worker = ring_buffer.create_live_review_batch_scan_worker(
                            callback,
                            width=480,
                            height=270,
                            ffmpeg_path=self._host.ffmpeg_path or "ffmpeg",
                            sample_fps=3.0,
                            roi=self._host._finish_line_rois.get(
                                camera_index, (0.35, 0.15, 0.65, 0.95)
                            ),
                            interval_seconds=2.0,
                            batch_ms=120_000,
                            overlap_ms=2_000,
                            finish_line=self._host._finish_line_store.get(camera_index),
                        )
                    else:
                        worker = ring_buffer.create_video_passage_scan_worker(
                            callback,
                            width=640,
                            height=360,
                            ffmpeg_path=self._host.ffmpeg_path or "ffmpeg",
                            sample_fps=8.0,
                            roi=self._host._finish_line_rois.get(
                                camera_index, (0.35, 0.15, 0.65, 0.95)
                            ),
                            interval_seconds=2.0,
                            finish_line=self._host._finish_line_store.get(camera_index),
                        )
                    self._host._video_scan_workers[camera_index] = worker
                    worker.start()
            self._host._start_visual_workers()
            self._host._capture_windows_by_camera = {
                camera_index: {} for camera_index in coordinators
            }
            self._host._capture_windows = self._host._capture_windows_by_camera.setdefault(
                self._host.camera_index,
                {},
            )
            self._host._archive_publishers.extend(archive_publishers)
            self._host._started = True
            self._host._stop_requested = False
            self._host._recording_started_at = time.monotonic()
            self._host._camera_segment_progress.clear()
            self._host._camera_segment_progress.update({
                camera_index: (None, self._host._recording_started_at)
                for camera_index in recorders
            })
            self._host._runtime_error = ""
            self._host._auto_recording_error = ""
            self._host._capture_error = ""
            self._host._workspace_notice = ""
            self._host._prepare_live_recording_view(recording_started_ms, "本次录像开始；有新过线时在这里出现。")
            self._host._refresh_timer.start()
            self._host._request_capture_refresh()
            self._host._lookup_cache.clear()
            self._host.refresh()
        except Exception:
            rollback_failures = self._host._run_capture_task(self._host._recording_controller.stop)
            workers = tuple(self._host._video_scan_workers.values())
            self._host._run_capture_task(lambda: [worker.stop() for worker in workers])
            self._host._video_scan_workers = {}
            self._host._stop_visual_workers()
            self._host._recorders = self._host._recording_controller.recorders
            self._host._ring_buffers = self._host._recording_controller.ring_buffers
            self._host._coordinators = self._host._recording_controller.coordinators
            self._host._publishers = self._host._recording_controller.timeline_publishers
            self._host._recorder = self._host._recorders.get(self._host.camera_index)
            self._host._ring_buffer = self._host._ring_buffers.get(self._host.camera_index)
            self._host._coordinator = self._host._coordinators.get(self._host.camera_index)
            self._host._publisher = self._host._publishers.get(self._host.camera_index)
            retained_publishers = self._host._recording_controller.archive_publishers
            for archive_publisher in retained_publishers:
                if archive_publisher not in self._host._archive_publishers:
                    self._host._archive_publishers.append(archive_publisher)
            if any(failure.still_running for failure in rollback_failures):
                self._host._runtime_error = "录像启动失败，且部分录像进程仍在运行"
            else:
                self._host._recording_lock.release()
            for archive_publisher in archive_publishers:
                if archive_publisher in self._host._archive_publishers:
                    self._host._archive_publishers.remove(archive_publisher)
            raise
        finally:
            self._host._update_runtime_status()

    def _restart_recording_camera(self, camera_index: int, source: str) -> None:
        self._host._invalidate_capture_refresh()
        camera_index = max(1, int(camera_index))
        token = object()
        with self._host._video_scan_state_lock:
            self._host._video_scan_tokens[camera_index] = token
        previous_worker = self._host._video_scan_workers.pop(camera_index, None)
        previous_windows = dict(self._host._capture_windows_by_camera.get(camera_index, {}))
        replacement_windows = {
            event_id: window for event_id, window in previous_windows.items()
            if window.state is not PassageReviewState.WAITING
        }
        pending_passages = []
        for event_id, window in previous_windows.items():
            if window.state is not PassageReviewState.WAITING:
                continue
            event = self._host.passage_store.get(event_id)
            timestamp_ms = self._host._evidence_timestamp(event) if event is not None else None
            if event is not None and event.is_active and timestamp_ms is not None:
                pending_passages.append(PendingRecordingPassage(
                    event_id, timestamp_ms, event.revision, event.race_id,
                ))
        restart_kwargs = dict(
            camera_index=camera_index, source=str(source), output_dir=self._host.output_dir,
            ffmpeg_path=self._host.ffmpeg_path, review_retention_seconds=self._host.review_retention_seconds,
            timeline_store=self._host.timeline_store, timing_error_ms=self._host.timing_error_ms,
            binding_store=self._host.review_binding_store, pending_passages=tuple(pending_passages),
        )

        def restart():
            if previous_worker is not None:
                previous_worker.stop()
            return self._host._recording_controller.restart_camera(**restart_kwargs)

        try:
            pipeline, waiting_windows = self._host._run_capture_task(restart)
        finally:
            self._host._recorders = self._host._recording_controller.recorders
            self._host._ring_buffers = self._host._recording_controller.ring_buffers
            self._host._coordinators = self._host._recording_controller.coordinators
            self._host._publishers = self._host._recording_controller.timeline_publishers
            self._host._recorder = self._host._recorders.get(self._host.camera_index)
            self._host._ring_buffer = self._host._ring_buffers.get(self._host.camera_index)
            self._host._coordinator = self._host._coordinators.get(self._host.camera_index)
            self._host._publisher = self._host._publishers.get(self._host.camera_index)
        replacement_windows.update(waiting_windows)
        self._host._capture_windows_by_camera[camera_index] = replacement_windows
        if camera_index == self._host.camera_index:
            self._host._capture_windows = replacement_windows
        self._host._camera_segment_progress[camera_index] = (None, time.monotonic())
        self._host._archive_publishers.append(pipeline.archive_publisher)
        self._host._lookup_cache.clear()
        ring_buffer = pipeline.ring_buffer
        scan_worker = None
        try:
            if self._host._video_assist_enabled():
                callback = (
                    lambda candidates, generation=self._host._video_scan_generation,
                    camera_index=camera_index, token=token: self._host._on_video_candidates(
                        candidates,
                        generation,
                        worker_token=(camera_index, token),
                    )
                )
                if len(self._host._configured_recording_sources()) == 1:
                    scan_worker = ring_buffer.create_live_review_batch_scan_worker(
                        callback,
                        width=480,
                        height=270,
                        ffmpeg_path=self._host.ffmpeg_path or "ffmpeg",
                        sample_fps=3.0,
                        roi=self._host._finish_line_rois.get(
                            camera_index, (0.35, 0.15, 0.65, 0.95)
                        ),
                        interval_seconds=2.0,
                        batch_ms=120_000,
                        overlap_ms=2_000,
                        finish_line=self._host._finish_line_store.get(camera_index),
                    )
                else:
                    scan_worker = ring_buffer.create_video_passage_scan_worker(
                        callback,
                        width=640,
                        height=360,
                        ffmpeg_path=self._host.ffmpeg_path or "ffmpeg",
                        sample_fps=8.0,
                        roi=self._host._finish_line_rois.get(
                            camera_index, (0.35, 0.15, 0.65, 0.95)
                        ),
                        interval_seconds=2.0,
                        finish_line=self._host._finish_line_store.get(camera_index),
                    )
                scan_worker.start()
                if self._host._video_scan_pause_tokens:
                    pause = getattr(scan_worker, "pause", None)
                    if callable(pause):
                        pause()
        except Exception:
            if scan_worker is not None:
                self._host._run_capture_task(scan_worker.stop)
            raise
        if scan_worker is not None:
            self._host._video_scan_workers[camera_index] = scan_worker

    def _live_location_for_filmstrip(
        self,
        pane,
        anchor_time_ms: int,
    ) -> PassageVideoLocation | None:
        """Return the immutable live TS containing ``anchor_time_ms``."""

        ring_buffers = getattr(self._host, "_ring_buffers", {})
        ring_buffer = ring_buffers.get(int(getattr(pane, "camera_index", 0)))
        if ring_buffer is None:
            return None
        locations = self._host._live_locations_for_filmstrip(pane)
        if not locations:
            return None
        anchor_time_ms = int(anchor_time_ms)
        selected = next((location for location in locations
                         if location.segment.started_at_ms <= anchor_time_ms
                         < location.segment.ended_at_ms), None)
        # Never bridge a real gap or substitute the newest frame for an
        # unavailable requested time. The full-race rail indexes every TS.
        if selected is None:
            return None
        origin = int(selected.segment.media_started_at_ms or selected.segment.started_at_ms)
        position = max(0, min(
            int(selected.segment.media_duration_ms or 1) - 1,
            anchor_time_ms - origin,
        ))
        return replace(
            selected,
            passage_position_ms=position,
            playback_position_ms=max(0, position - self._host.pre_roll_ms),
        )

    def _live_locations_for_filmstrip(self, pane) -> tuple[PassageVideoLocation, ...]:
        """Build stable sources from the closed TS files in the rolling buffer."""

        ring_buffer = getattr(self._host, "_ring_buffers", {}).get(
            int(getattr(pane, "camera_index", 0))
        )
        if ring_buffer is None:
            return ()
        event = self._host.passage_store.get(self._host._selected_event_id)
        metadata = self._host._current_metadata()
        race_id = metadata.race_id if metadata is not None else event.race_id if event is not None else ""
        locations = []
        for item in ring_buffer.filmstrip_segments():
            path = ring_buffer.resolve_path(item)
            try:
                available = path.is_file() and path.stat().st_size > 0
            except OSError:
                available = False
            if not available:
                continue
            duration_ms = int(item.duration_ms)
            segment = RecordingSegment(
                segment_id=f"live-filmstrip-{ring_buffer.camera_index}-{item.segment_id}",
                source_id=ring_buffer.source_id,
                camera_index=ring_buffer.camera_index,
                video_path=str(path),
                started_at_ms=int(item.started_at_ms),
                ended_at_ms=int(item.ended_at_ms),
                media_duration_ms=duration_ms,
                media_started_at_ms=int(item.started_at_ms),
                clock_source=DEFAULT_CLOCK_SOURCE,
                timing_error_ms=DEFAULT_TIMING_ERROR_MS,
                end_reason="live_filmstrip_tail",
                race_id=race_id,
            )
            locations.append(PassageVideoLocation(
                segment=segment,
                video_path=path,
                passage_position_ms=0,
                playback_position_ms=0,
                clock_offset_ms=0,
                timing_error_ms=DEFAULT_TIMING_ERROR_MS,
                status="unverified",
            ))
        return tuple(locations)

    def _persist_filmstrip_location(
        self, location: PassageVideoLocation,
    ) -> PassageVideoLocation:
        if location is None or location.segment.end_reason != "live_filmstrip_tail":
            return location
        ring_buffer = getattr(self._host, "_ring_buffers", {}).get(
            int(location.segment.camera_index)
        )
        if ring_buffer is None:
            raise ValueError("实时缓存已结束，请刷新胶卷后打开归档录像")
        segment = ring_buffer.retain_filmstrip_segment(
            location.segment, self._host.timeline_store
        )
        return replace(
            location,
            segment=segment,
            video_path=self._host.timeline_store.resolve_video_path(segment),
            status="located",
        )

    def _recording_stall_error(
        self,
        camera_index: int,
        *,
        now: float,
    ) -> str:
        ring_buffer = self._host._ring_buffers.get(int(camera_index))
        if ring_buffer is None:
            return ""
        return self._host._recording_recovery.stall_error(
            int(camera_index), ring_buffer.segment_revision, now=now,
        )

    def _poll_recording_health(self, *, now: float | None = None) -> None:
        if getattr(self._host, "_capture_task_waiting", False):
            return
        if self._host._stop_requested or not self._host._started:
            return
        current_time = time.monotonic() if now is None else float(now)
        for camera_index, recorder in tuple(self._host._recorders.items()):
            recorder_error = recorder.check_error()
            if not recorder_error and camera_index in self._host._camera_reconnect_errors:
                recorder_error = self._host._camera_reconnect_errors[camera_index]
            if not recorder_error and recorder.is_running:
                recorder_error = self._host._recording_stall_error(
                    camera_index,
                    now=current_time,
                )
            if recorder_error:
                self._host._recover_failed_camera(
                    camera_index,
                    recorder_error,
                    now=current_time,
                )

    def _on_clock_tick(self) -> None:
        self._host._refresh_problem_list()
        panel = self._host.problem_panel
        recording = self._host._recording_any_active()
        panel.live_tick(recording)
        live_cameras = (panel.payload or {}).get("cameras", {})
        through = [value.get("analyzed_through_ms") for value in live_cameras.values() if value.get("analyzed_through_ms")]
        analyzed = datetime.fromtimestamp(min(through) / 1000).strftime("%H:%M:%S") if through else "等待录像 / 校时"
        analysis_state = panel.analysis_status.replace("\n", " ")
        # Only routine states may disappear. Failure/paused/configuration states
        # take priority even if their longer explanation also contains “等待”.
        actionable = any(word in analysis_state for word in ("失败", "错误", "暂停", "无法", "未完成", "跳过", "终点线"))
        routine = any(word in analysis_state for word in ("等待", "空闲", "后台分析中", "后台分析：", "本轮分析完成"))
        analysis_note = (analysis_state.split("；")[0].split("，")[0].split("：")[0]
                         if actionable or not routine else "")
        short_ends = [segment.media_started_at_ms + segment.media_duration_ms for segment in self._host.timeline_store.segments()
                      if segment.end_reason == "retained_problem_segment" and segment.media_started_at_ms is not None
                      and segment.media_duration_ms is not None and (not panel._scope[0] or segment.race_id == panel._scope[0])]
        sealed = datetime.fromtimestamp(max(short_ends) / 1000).strftime("%H:%M:%S") if short_ends else "等待首段"
        seconds = max(0, int(time.monotonic() - self._host._recording_started_at)) if recording else 0
        duration = f"{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}"
        backlog_seconds = max(0, (max(short_ends) - min(through)) // 1000) if short_ends and through else 0
        chip_note = (panel.chip_status.split("（")[0].split("：")[0]
                     if any(word in panel.chip_status for word in ("发现", "失败", "错误")) else "")
        # 11-14: "证据缺口 602 条待证据" hung there all race and told the operator
        # nothing to do.  People nobody can check on any recording are still said.
        gap = panel.evidence_gap_text()
        notes = [f"● 录像 {duration}" if recording else "", chip_note, analysis_note,
                 f"分析落后 {backlog_seconds} 秒" if backlog_seconds else "",
                 gap.split(" · ", 1)[1] if " · " in gap else ""]
        self._host.pipeline_label.setText(" ｜ ".join(note for note in notes if note))
        self._host.pipeline_label.setToolTip(
            f"封存到 {sealed} · 分析到 {analyzed} · 积压 {backlog_seconds} 秒\n"
            + panel.chip_status + "\n" + panel.analysis_status + "\n" + panel.health_text()
            + "\n" + panel.summary.toolTip()
        )
        panel.quick_toolbar.setToolTip(self._host.pipeline_label.toolTip())
        self._host.problem_panel.refresh_status_visibility()
        self._host._poll_recording_health()
        self._host._update_runtime_status()

    def _recover_failed_camera(
        self,
        camera_index: int,
        recorder_error: str,
        *,
        now: float,
    ) -> bool:
        camera_index = max(1, int(camera_index))
        if not self._host._recording_recovery.can_retry(camera_index, now=now):
            return False
        source = dict(self._host._configured_recording_sources()).get(camera_index)
        if not source:
            return False
        if _is_rtsp_auth_error(recorder_error):
            detail = (
                _format_rtsp_probe_error(recorder_error)
                + " 自动重试已暂停；修改凭据后，点击开始录像重试。"
            )
            self._host._recording_recovery.pause_auth(camera_index, source, detail)
            self._host._runtime_error = f"机位{camera_index}：{detail}"
            self._host._auto_recording_error = self._host._runtime_error
            logger.warning("Camera %s authentication failed; automatic retries paused", camera_index)
            return False
        reconnect_started = time.perf_counter()
        try:
            self._host._restart_recording_camera(camera_index, source)
        except Exception as exc:  # noqa: BLE001 - reconnect remains operator-visible.
            self._host._observe_runtime_metric(
                "camera_reconnect",
                reconnect_started,
                item_count=1,
                failed=True,
            )
            attempt, delay_seconds = self._host._recording_recovery.failed(camera_index, now=now)
            detail = sanitize_recording_message(exc)
            self._host._camera_reconnect_errors[camera_index] = (
                f"{recorder_error}；重连失败：{detail}；"
                f"{delay_seconds:g}秒后重试"
            )
            self._host._runtime_error = (
                f"机位{camera_index}断开，自动重连中："
                f"{self._host._camera_reconnect_errors[camera_index]}"
            )
            logger.warning(
                "Camera %s reconnect attempt %s failed; retrying in %.1fs: %s",
                camera_index,
                attempt,
                delay_seconds,
                detail,
            )
            return False

        self._host._recording_recovery.succeeded(camera_index)
        self._host._observe_runtime_metric(
            "camera_reconnect",
            reconnect_started,
            item_count=1,
        )
        if self._host._runtime_error.startswith(f"机位{camera_index}"):
            self._host._runtime_error = ""
        logger.warning("Camera %s recording automatically reconnected", camera_index)
        return True

    def _camera_auth_retry_blocked(self) -> bool:
        self._host._poll_recording_health()
        return any(
            self._host._camera_auth_failed_sources.get(camera_index) == source
            for camera_index, source in self._host._configured_recording_sources()
        )

    def _auto_start_recording_for_passage(self, event: PassageEvent) -> bool:
        """A chip read while nothing records: say so in red, start nothing.

        11-14: the first chip of a race used to start the recording.  Before a
        race the chips are tested again and again, and every test left a
        recording and a folder behind ("它就会自动连接录像…非常麻烦").  The
        reminder beside 开始录像 stays for a race that really was forgotten; a
        click on it puts it away for this race, as stopping a recording does.
        """
        if event.is_active and not self._host._is_test_passage(event):
            self._host._last_chip_race_id = event.race_id
        if (
            self._host._workspace_mode != "live"
            or not event.is_active
            or event.race_id in self._host._auto_recorded_race_ids
            or self._host._camera_auth_retry_blocked()
            or self._host._recording_any_active()
            or self._host._is_test_passage(event)
            or not self._host._is_live_passage(event)
        ):
            return False
        if self._host._recording_reminder_race != event.race_id:
            self._host._recording_reminder_race = event.race_id
            logger.info("Chip read %s while not recording: reminder shown", event.event_id)
        self._host._sync_recording_reminder()
        return False

    def _sync_recording_reminder(self) -> None:
        label = getattr(self._host, "recording_reminder", None)
        if label is None:
            return
        race = self._host._recording_reminder_race
        if race and (self._host._recording_any_active() or race in self._host._auto_recorded_race_ids
                     or self._host._workspace_mode != "live"):
            race = self._host._recording_reminder_race = ""
        label.setVisible(bool(race))

    def _dismiss_recording_reminder(self) -> None:
        if self._host._recording_reminder_race:
            self._host._auto_recorded_race_ids.add(self._host._recording_reminder_race)
        self._host._sync_recording_reminder()

    def _race_being_recorded(self) -> str:
        current = self._host._current_metadata()
        if current is not None:
            return current.race_id
        return self._host._live_metadata_scope[0] if self._host._live_metadata_scope else ""

    def _other_race_while_recording(self, race_id: str, race_name: str = "") -> bool:
        """A recording is never cut over to another race by itself; the operator decides.

        10-28: CycleRace's test races (接力测试赛, 78 of them) reached the live
        console and switched it every 3-4 s for 35 minutes, restarting the
        finish recording each time and leaving 120 folders behind.
        """
        current = self._host.metadata_store.current() if self._host.metadata_store is not None else None
        if current is None or current.race_id == race_id or not self._host._recording_any_active():
            return False
        self._host._workspace_notice = (f"收到其他赛事：{race_name or race_id}。正在录像，未切换；"
                                  "要切换请先停止录像，再在“设置”中打开该赛事。")
        if race_id not in self._host._held_race_ids:
            self._host._held_race_ids.add(race_id)
            logger.info("Live recording kept on %s; other race %s not switched to", current.race_id, race_id)
        self._host._update_runtime_status()
        return True

    def stop_recording(self) -> tuple[RecordingStopFailure, ...]:
        self._host._stop_requested = True
        self._host._started = False
        self._host._invalidate_capture_refresh()
        with self._host._video_scan_state_lock:
            self._host._video_scan_generation += 1
            self._host._video_scan_tokens.clear()
        workers = tuple(self._host._video_scan_workers.values())
        self._host._video_scan_workers = {}
        self._host._stop_visual_workers()
        had_recorders = bool(self._host._recorders)

        def stop_pipeline():
            for worker in workers:
                worker.stop()
            return self._host._recording_controller.stop()

        failures = self._host._run_capture_task(stop_pipeline)
        if had_recorders:
            try:
                self._host._refresh_capture_windows()
            except Exception:
                logger.exception("Failed to publish final review segments")
            if failures:
                self._host._runtime_error = "; ".join(
                    f"机位{failure.camera_index}: "
                    f"{sanitize_recording_message(failure.error)}"
                    for failure in failures
                )
        self._host._recorders = self._host._recording_controller.recorders
        self._host._ring_buffers = self._host._recording_controller.ring_buffers
        self._host._coordinators = self._host._recording_controller.coordinators
        self._host._publishers = self._host._recording_controller.timeline_publishers
        still_running = any(
            failure.still_running for failure in failures
        )
        if still_running:
            # Keep every authoritative reference while a retryable recorder is
            # alive; never hand its archive publisher to a sealed-file scanner.
            self._host._recorder = self._host._recorders.get(self._host.camera_index)
            self._host._ring_buffer = self._host._ring_buffers.get(self._host.camera_index)
            self._host._coordinator = self._host._coordinators.get(self._host.camera_index)
            self._host._publisher = self._host._publishers.get(self._host.camera_index)
        else:
            # Another window may record into this folder once nothing here does.
            self._host._recording_lock.release()
            self._host._recorder = None
            self._host._ring_buffer = None
            self._host._coordinator = None
            self._host._publisher = None
            self._host._recording_started_at = 0.0
            self._host._camera_reconnect_attempts.clear()
            self._host._camera_reconnect_not_before.clear()
            self._host._camera_reconnect_errors.clear()
            self._host._camera_segment_progress.clear()
        self._host._update_runtime_status()
        return failures
