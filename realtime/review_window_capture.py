"""Frame capture and video scanning of the finish review window."""

from __future__ import annotations

import logging
import importlib
import time
from dataclasses import replace
from datetime import datetime

from PyQt5.QtCore import QTimer

from .background_wait import wait_for_background
from .recording_catalog import RecordingCatalog, RecordingCatalogJob
from .evidence_pipeline import EvidencePassage, EvidencePipeline, EvidenceRefreshJob
from .capture_refresh import ArchiveRefreshJob, CaptureRefreshRequest, CaptureRefreshResult
from .auyat_rgb import AuyatScanResult
from .external_clip_import import ExternalClipImportError
from .passage_receiver import PassageEvent
from .workspace_context import EventWorkspaceMetadataStore
from .stream_recorder import is_rtsp_source, sanitize_recording_message
from .video_timeline import DEFAULT_CLOCK_SOURCE
from .thread_lifecycle import retire_qthread, track_qthread
from .visual_crossing import CrossingConfig, VisualCrossingEvent, VisualCrossingWorker
from .review_window_common import (
    BEIJING_TIMEZONE,
    _VideoCandidateDelivery,
    _VideoCandidatePersistenceFailure,
)

logger = logging.getLogger("FinishReview")


class FinishReviewCaptureMixin:
    """Visual crossing workers, high-speed lookups, capture refresh, archive and video scans, video candidates."""

    def _visual_crossing_config(self) -> CrossingConfig:
        return CrossingConfig(
            finish_line=self._visual_finish_line,
            gate_width=self._visual_gate_width,
            forward_direction=self._visual_forward_direction,
            roi_top=self._visual_roi_top,
            roi_bottom=self._visual_roi_bottom,
        ).normalized()

    def _start_visual_workers(self) -> None:
        """Start advisory crossing detection only for the selected RTSP camera."""
        self._stop_visual_workers()
        self._visual_failed = False
        # In the single-camera field profile the batch scanner consumes the
        # completed low-rate HLS preview. Starting a second RTSP decoder would
        # compete with the recorder and the foreground judge on old CPUs.
        if len(self._configured_recording_sources()) <= 1:
            self._visual_status = "disabled_single_camera_batch_scan"
            return
        if not self._video_assist_enabled():
            self._visual_status = "disabled"
            return
        if not self._visual_detection_enabled:
            self._visual_status = "disabled"
            return
        source = next(
            (
                source_value
                for camera_index, source_value in self._configured_recording_sources()
                if camera_index == self._visual_camera_index
                and is_rtsp_source(source_value)
            ),
            "",
        )
        if not source:
            self._visual_status = "no_rtsp_source"
            return
        worker = VisualCrossingWorker(
            source,
            self._visual_camera_index,
            self.output_dir / "visual_crossing_events.jsonl",
            self,
            config=self._visual_crossing_config(),
        )
        worker.crossing_detected.connect(self._on_visual_crossing)
        worker.status_changed.connect(self._on_visual_status)
        worker.failed.connect(self._on_visual_failed)
        self._visual_workers[self._visual_camera_index] = worker
        worker.start()
        self._visual_status = "starting"

    def _stop_visual_workers(self) -> None:
        workers = tuple(self._visual_workers.values())
        self._visual_workers = {}
        for worker in workers:
            worker.request_stop()
        for worker in workers:
            if worker.isRunning() and not worker.wait(2_000):
                logger.warning("Visual crossing worker did not stop promptly")
                retire_qthread(worker)

    def _on_visual_status(self, message: str) -> None:
        self._visual_status = str(message or "")
        if "已启动" in self._visual_status:
            self._visual_failed = False
        self._update_runtime_status()

    def _on_visual_failed(self, message: str) -> None:
        self._visual_status = str(message or "")
        self._visual_failed = True
        self._update_runtime_status()

    def _on_visual_crossing(self, event: VisualCrossingEvent) -> None:
        # Reverse crossings are evidence of camera movement or a wrong direction,
        # not finish-line candidates for the operator queue.
        if event.direction != "forward":
            return
        tools = importlib.import_module(
            ".video_passage_det" + "ector", __package__
        )
        candidate = tools.VideoPassageCandidate(
            candidate_id=f"visual:{event.event_id}",
            camera_index=event.camera_index,
            started_at_ms=event.timestamp_ms,
            ended_at_ms=event.timestamp_ms,
            peak_at_ms=event.timestamp_ms,
            peak_score=event.confidence,
            changed_area=0.0,
            segment_id=f"visual:{event.event_id}",
        )
        self._on_video_candidates(
            (candidate,),
            self._video_scan_generation,
        )

    def _locate_high_speed(
        self,
        event: PassageEvent,
        clock_offset_ms: int,
        pre_roll_ms: int,
    ):
        return self._high_speed_catalog.locate(
            event.timeline_timestamp_ms,
            race_id=event.race_id,
            clock_offset_ms=clock_offset_ms,
            pre_roll_ms=pre_roll_ms,
        )

    def _run_capture_task(self, operation):
        self._capture_refresh_worker.start()
        self._capture_task_waiting = True
        try:
            return wait_for_background(self._capture_refresh_worker.submit_task(operation), self)
        finally:
            self._capture_task_waiting = False
            callbacks = getattr(self, "_capture_deferred_callbacks", [])
            self._capture_deferred_callbacks = []
            for callback in callbacks:
                QTimer.singleShot(0, callback)

    def _defer_capture_callback(self, callback, *args):
        if getattr(self, "_shutdown_requested", False):
            return True
        if not getattr(self, "_capture_task_waiting", False):
            return False
        if not hasattr(self, "_capture_deferred_callbacks"):
            self._capture_deferred_callbacks = []
        self._capture_deferred_callbacks.append(lambda: callback(*args))
        return True

    def _on_high_speed_scan_finished(self, result: AuyatScanResult) -> None:
        self._high_speed_scan_result = result
        if result.changed:
            self.invalidate_external_locations()
        self._update_runtime_status()

    def _request_high_speed_scan(self) -> None:
        self._high_speed_scan_result = self._high_speed_catalog.snapshot()
        if self.high_speed_dir is None:
            worker = self._high_speed_scan_worker
            if worker.isRunning():
                worker.stop()
                if not worker.wait(1_000):
                    logger.warning("High-speed scan worker did not stop promptly")
            self._update_runtime_status()
            return
        if not self._high_speed_scan_worker.isRunning():
            track_qthread(self._high_speed_scan_worker)
            self._high_speed_scan_worker.start()
        else:
            self._high_speed_scan_worker.request_scan()

    def _include_high_speed_event_date(self, event: PassageEvent) -> None:
        timestamp_ms = self._evidence_timestamp(event)
        if timestamp_ms is None:
            return
        event_date = datetime.fromtimestamp(
            timestamp_ms / 1000.0,
            tz=BEIJING_TIMEZONE,
        ).date()
        dates = self._high_speed_catalog.target_dates
        if event_date in dates:
            return
        if self._high_speed_catalog.set_target_dates((*dates, event_date)):
            self.invalidate_external_locations()
            self._request_high_speed_scan()

    def _start_archive_video_scan_workers(self) -> None:
        """Scan existing ordinary recordings when opening an archive workspace."""

        if not self._video_assist_enabled() or self._recording_any_active():
            return
        self._stop_archive_video_scan_workers()
        segments_by_camera: dict[int, tuple[object, ...]] = {}
        for segment in self.timeline_store.segments():
            if segment.clock_source != DEFAULT_CLOCK_SOURCE:
                continue
            camera_index = max(1, int(segment.camera_index))
            segments_by_camera.setdefault(camera_index, ())
            segments_by_camera[camera_index] = (
                *segments_by_camera[camera_index],
                segment,
            )
        if not segments_by_camera:
            return
        try:
            scan_module = importlib.import_module(
                ".video_passage_det" + "ector", __package__
            )
            worker_type = scan_module.VideoPassageScanWorker
        except (ImportError, AttributeError) as error:
            logger.warning("Archived video candidate scanner unavailable: %s", error)
            return

        for camera_index, camera_segments in segments_by_camera.items():
            def provider(values=camera_segments):
                return values

            generation = self._video_scan_generation
            token = object()
            self._video_scan_tokens[camera_index] = token

            worker = worker_type(
                provider,
                lambda candidates, generation=generation,
                camera_index=camera_index, token=token: self._on_video_candidates(
                    candidates,
                    generation,
                    worker_token=(camera_index, token),
                ),
                camera_index=camera_index,
                width=640,
                height=360,
                ffmpeg_path=self.ffmpeg_path or "ffmpeg",
                sample_fps=4.0,
                continuous=False,
                roi=self._finish_line_rois.get(
                    camera_index, (0.35, 0.15, 0.65, 0.95)
                ),
                interval_seconds=1.0,
                path_resolver=self.timeline_store.resolve_video_path,
                finish_line=self._finish_line_store.get(camera_index),
            )
            self._archive_video_scan_workers[camera_index] = worker
            worker.start()

    def _stop_archive_video_scan_workers(self) -> None:
        with self._video_scan_state_lock:
            self._video_scan_generation += 1
            worker_items = tuple(self._archive_video_scan_workers.items())
            workers = tuple(worker for _camera_index, worker in worker_items)
            self._archive_video_scan_workers = {}
            for camera_index, _worker in worker_items:
                self._video_scan_tokens.pop(camera_index, None)
        for worker in workers:
            try:
                worker.stop()
            except Exception:  # noqa: BLE001 - shutdown remains best effort.
                logger.exception("Failed to stop archived video scanner")
        self._update_operator_controls()
        if hasattr(self, "high_speed_status_label"):
            self._update_runtime_status()

    def _reap_finished_archive_video_scan_workers(self) -> None:
        """Drop completed one-shot archive scanners from runtime state."""

        finished: list[tuple[int, object]] = []
        for camera_index, worker in tuple(self._archive_video_scan_workers.items()):
            if not bool(getattr(worker, "is_running", False)):
                finished.append((camera_index, worker))
        if not finished:
            return
        for camera_index, worker in finished:
            self._archive_video_scan_workers.pop(camera_index, None)
            self._video_scan_tokens.pop(camera_index, None)
            stop = getattr(worker, "stop", None)
            if callable(stop):
                stop(timeout=0.1)

    def _toggle_archive_video_scan(self) -> None:
        """Start or stop the optional, one-shot historical video scan."""

        if (
            self._workspace_mode != "archive"
            or self._recording_any_active()
            or not self._video_assist_enabled()
        ):
            return
        self._reap_finished_archive_video_scan_workers()
        if self._archive_video_scan_workers:
            self._stop_archive_video_scan_workers()
        else:
            self._start_archive_video_scan_workers()
        self._update_runtime_status()

    def _filmstrip_race_id(self) -> str:
        try:
            return self._current_archive_race_id()
        except ExternalClipImportError:
            return ""

    def _invalidate_capture_refresh(self) -> None:
        self._capture_refresh_generation += 1
        self._capture_refresh_worker.invalidate(self._capture_refresh_generation)
        # The serial queue provides a barrier after the cancelled active pass.
        self._run_capture_task(lambda: None)

    def _capture_refresh_request(self, *, cleanup=False) -> CaptureRefreshRequest:
        try:
            race_id = self._current_archive_race_id()
        except ExternalClipImportError:
            race_id = ""
        active_recorders = {recorder for recorder in self._recorders.values() if recorder.is_running}
        archive_jobs = tuple(
            ArchiveRefreshJob(publisher, str(race_id), publisher.recorder in active_recorders)
            for publisher in self._archive_publishers if race_id
        )
        key = (self.review_binding_store, tuple(self._coordinators.items()),
               tuple(self._publishers.items()))
        if key != getattr(self, "_evidence_pipeline_key", None):
            self._evidence_pipeline_key = key
            self._evidence_pipeline = EvidencePipeline(
                self._coordinators, self._publishers, self.review_binding_store,
            )
            self._capture_previews = {}
        if not hasattr(self, "_recording_catalog"):
            self._recording_catalog = RecordingCatalog()
        all_events = self.passage_store.events(include_inactive=True)
        eligible = {event.event_id for event in self._events_for_current_metadata(all_events)}
        passages = tuple(EvidencePassage(
            event.event_id, event.revision, event.race_id, self._evidence_timestamp(event),
            event.is_active, event.event_id in eligible,
        ) for event in all_events)
        return CaptureRefreshRequest(
            generation=self._capture_refresh_generation,
            ring_buffers=tuple(self._ring_buffers.values()), archive_jobs=archive_jobs,
            cleanup=cleanup, current_time_ms=int(time.time() * 1000.0),
            evidence_job=EvidenceRefreshJob(self._evidence_pipeline, passages),
            catalog_job=RecordingCatalogJob(
                self._recording_catalog, self.timeline_store,
                self._camera_one_pane().camera_index, str(race_id), tuple(self._ring_buffers.values()),
            ),
            storage_path=self.output_dir,
            problem_timeline=self.timeline_store,
            problem_race_id=str(race_id),
        )

    def _recording_sources_for_filmstrip(self, pane, race_id):
        if not hasattr(self, "_capture_refresh_worker"):
            return (), 0
        if (isinstance(self.metadata_store, EventWorkspaceMetadataStore)
                and self.metadata_store.current() is None and len(self.metadata_store.candidates()) > 1):
            return (), 0
        context = (str(self.timeline_store.journal_path.absolute()), race_id, pane.camera_index)
        snapshot = getattr(self, "_recording_catalog_snapshot", None)
        key = (context, self.timeline_store.revision)
        if ((snapshot is None or snapshot.context != context or snapshot.revision != key[1]
                or time.monotonic() - getattr(self, "_catalog_refreshed_at", 0.0) >= 2.0)
                and key != getattr(self, "_catalog_requested_key", None)):
            self._catalog_requested_key = key
            QTimer.singleShot(0, self._request_capture_refresh)
        if snapshot is None or snapshot.context != context:
            return (), 1 if self._recording_any_active() else 0
        sources = tuple(replace(source, location=replace(
            source.location, clock_offset_ms=self._continuous_offset_for_location(source.location),
        )) for source in snapshot.sources)
        pending = snapshot.pending
        if self._recording_any_active() and not any(
            source.location.segment.end_reason == "live_filmstrip_tail" for source in sources
        ):
            pending = max(1, pending)
        return sources, pending

    def _request_capture_refresh(self) -> None:
        if getattr(self, "_capture_task_waiting", False):
            return
        now = time.monotonic()
        cleanup = now - self._last_cleanup_at >= 5.0
        if cleanup:
            self._last_cleanup_at = now
        self._capture_refresh_worker.submit(self._capture_refresh_request(cleanup=cleanup))

    def _apply_evidence_snapshot(self, snapshot) -> set[str]:
        changed = set()
        valid = {}
        eligible = {event.event_id for event in self._events_for_current_metadata(
            self.passage_store.events(include_inactive=True)
        )}
        for passage in snapshot.passages:
            current = self.passage_store.get(passage.event_id)
            if (current is not None and current.revision == passage.revision
                    and current.is_active == passage.active
                    and passage.eligible == (passage.event_id in eligible)
                    and self._evidence_timestamp(current) == passage.timestamp_ms):
                valid[passage.event_id] = passage
        previews = dict(getattr(self, "_capture_previews", {}))
        for camera, windows in snapshot.windows:
            previous = self._capture_windows_by_camera.setdefault(camera, {})
            incoming = {window.event_id: window for window in windows if window.event_id in valid}
            for event_id in valid:
                window = incoming.get(event_id)
                if previous.get(event_id) != window:
                    changed.add(event_id)
                if window is None:
                    previous.pop(event_id, None)
                else:
                    previous[event_id] = window
                previews.pop((camera, event_id), None)
        for camera, event_id, preview in snapshot.previews:
            if event_id in valid:
                if self._capture_previews.get((camera, event_id)) != preview:
                    changed.add(event_id)
                previews[camera, event_id] = preview
        self._capture_previews = previews
        self._unsupported_event_ids = {passage.event_id for passage in valid.values()
                                       if passage.active and passage.eligible and passage.timestamp_ms is None}
        return changed

    def _on_capture_refresh_finished(self, result: CaptureRefreshResult) -> None:
        if self._defer_capture_callback(self._on_capture_refresh_finished, result):
            return
        if (
            not isinstance(result, CaptureRefreshResult)
            or result.generation != self._capture_refresh_generation
        ):
            return
        apply_started = time.perf_counter()
        self._catalog_requested_key = None
        if result.storage is not None:
            self._storage_snapshot = result.storage
        if result.catalog is not None:
            self._recording_catalog_snapshot = result.catalog
            self._catalog_refreshed_at = time.monotonic()
        failed = bool(result.error)
        apply_failed = False
        changed_event_ids: set[str] = set()
        if result.apply_state:
            try:
                if result.evidence is not None:
                    changed_event_ids = self._apply_evidence_snapshot(result.evidence)
                if result.archive_segments:
                    changed_event_ids.update(result.archive_affected_events)
                if changed_event_ids:
                    self.refresh_events(changed_event_ids)
                self._poll_recording_health(now=time.monotonic())
            except Exception as exc:
                failed = True
                apply_failed = True
                self._capture_error = sanitize_recording_message(exc)
                logger.exception("Failed to apply background capture refresh")
        if result.error:
            self._capture_error = sanitize_recording_message(result.error)
        elif result.evidence is not None and not apply_failed:
            self._capture_error = ""
        # A newly published HLS segment extends the time-film tail before the
        # five-minute archive is sealed. Refresh only the lightweight source
        # index here; visible thumbnails are still decoded lazily by the panel.
        if result.catalog is not None or (result.apply_state and (result.discovered_segment_count or result.archive_segments)) or result.deleted_paths:
            try:
                self._update_filmstrip()
            except Exception:  # noqa: BLE001 - a preview refresh must not stop capture.
                logger.exception("Failed to refresh live filmstrip tail")
        if result.cleanup_after_apply and not failed:
            self._capture_refresh_worker.submit(
                CaptureRefreshRequest(
                    generation=self._capture_refresh_generation,
                    ring_buffers=tuple(self._ring_buffers.values()),
                    archive_jobs=(),
                    cleanup=True,
                    current_time_ms=int(time.time() * 1000.0),
                    scan=False,
                    apply_state=False,
                )
            )
        self._observe_runtime_metric(
            "capture_refresh_apply",
            apply_started,
            item_count=len(changed_event_ids),
            failed=failed,
        )
        self._update_operator_controls()
        self._update_runtime_status()

    def _refresh_capture_windows(self) -> None:
        """Drain and finish evidence work for explicit lifecycle/test callers."""
        self._invalidate_capture_refresh()
        request = self._capture_refresh_request()
        result = self._run_capture_task(lambda: self._capture_refresh_worker._process(request))
        self._on_capture_refresh_finished(result)

    def _publish_archive_segments(
        self,
        *,
        race_id: str | None = None,
        recording: bool | None = None,
    ):
        publishers = tuple(self._archive_publishers)
        if not publishers:
            return ()
        if race_id is None:
            try:
                race_id = self._current_archive_race_id()
            except ExternalClipImportError:
                return ()
        if recording is None:
            recording = self._recording_any_active()
        active_recorders = {
            recorder
            for recorder in self._recorders.values()
            if recorder.is_running
        }
        published = []
        for publisher in publishers:
            publisher_recording = bool(
                recording and publisher.recorder in active_recorders
            )
            published.extend(
                publisher.publish_completed(
                    race_id=str(race_id),
                    recording=publisher_recording,
                )
            )
        return tuple(published)

    def set_finish_line_roi(
        self,
        camera_index: int,
        roi: tuple[float, float, float, float],
    ) -> None:
        """Set the normalized finish-line band used by video assistance."""
        left, top, right, bottom = (float(value) for value in roi)
        if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
            raise ValueError("终点线区域必须是0到1之间的有效矩形")
        self._finish_line_rois[max(1, int(camera_index))] = (
            left, top, right, bottom
        )
        camera_index = max(1, int(camera_index))
        self._finish_line_store.set_roi(camera_index, (left, top, right, bottom))
        for worker in self._video_scan_workers.values():
            if int(getattr(worker, "camera_index", 0)) == camera_index:
                worker.roi = (left, top, right, bottom)
                worker.request_scan()
        self._update_runtime_status()

    def _on_video_candidates(
        self,
        candidates,
        generation: int | None = None,
        *,
        worker_token: tuple[int, object] | None = None,
    ) -> None:
        """Persist worker results before marshalling them to the Qt main thread."""
        if not self._video_assist_enabled():
            return
        with self._video_scan_state_lock:
            if worker_token is not None:
                camera_index, token = worker_token
                if self._video_scan_tokens.get(int(camera_index)) is not token:
                    return
            delivery_generation = (
                self._video_scan_generation if generation is None else int(generation)
            )
            if delivery_generation != self._video_scan_generation:
                return
            arrival_store = self.video_arrival_store
        values = tuple(candidates)
        persistent = tuple(
            candidate
            for candidate in values
            if not str(getattr(candidate, "candidate_id", "")).startswith("visual:")
            and str(getattr(candidate, "segment_id", "")).strip()
            and str(getattr(candidate, "video_path", "")).strip()
        )
        if persistent:
            try:
                arrival_store.add_many(persistent)
            except Exception as error:  # noqa: BLE001 - report persistence failures to Qt.
                self.video_candidate_persistence_failed.emit(
                    _VideoCandidatePersistenceFailure(
                        delivery_generation,
                        str(error),
                    )
                )
                raise RuntimeError("video candidate persistence failed") from error
        self.video_candidates_received.emit(
            _VideoCandidateDelivery(delivery_generation, values, persisted=True)
        )

    def _on_video_candidate_persistence_failed(self, failure) -> None:
        if isinstance(failure, _VideoCandidatePersistenceFailure):
            with self._video_scan_state_lock:
                if failure.generation != self._video_scan_generation:
                    return
                message = failure.message
        else:
            message = str(failure)
        self._capture_error = str(message)
        self._update_runtime_status()

    def _pause_video_scan_workers(self) -> object:
        """Pause background video scans while a playback window is open."""

        token = object()
        self._video_scan_pause_tokens.add(token)
        if len(self._video_scan_pause_tokens) != 1:
            return token
        workers = (
            *self._video_scan_workers.values(),
            *self._archive_video_scan_workers.values(),
        )
        for worker in workers:
            pause = getattr(worker, "pause", None)
            if callable(pause):
                pause()
        return token

    def _resume_video_scan_workers(self, token: object) -> None:
        if token not in self._video_scan_pause_tokens:
            return
        self._video_scan_pause_tokens.remove(token)
        if self._video_scan_pause_tokens:
            return
        workers = (
            *self._video_scan_workers.values(),
            *self._archive_video_scan_workers.values(),
        )
        for worker in workers:
            resume = getattr(worker, "resume", None)
            if callable(resume):
                resume()

    def _reconcile_video_candidates(self, candidates) -> None:
        if not self._video_assist_enabled():
            return
        already_persisted = False
        if isinstance(candidates, _VideoCandidateDelivery):
            if candidates.generation != self._video_scan_generation:
                return
            already_persisted = candidates.persisted
            candidates = candidates.candidates

        def is_visual(value: object) -> bool:
            return str(getattr(value, "candidate_id", "")).startswith("visual:")

        def overlaps(left: object, right: object) -> bool:
            return (
                int(getattr(left, "camera_index", 0))
                == int(getattr(right, "camera_index", 0))
                and abs(
                    int(getattr(left, "peak_at_ms", 0))
                    - int(getattr(right, "peak_at_ms", 0))
                )
                <= 1_500
            )

        values = tuple(candidates)
        reconcile_started = time.perf_counter()
        self.runtime_metrics.observe(
            "video_candidate_received",
            0.0,
            item_count=len(values),
        )
        persistent = tuple(
            candidate
            for candidate in values
            if not is_visual(candidate)
            and str(getattr(candidate, "segment_id", "")).strip()
            and str(getattr(candidate, "video_path", "")).strip()
        )
        if persistent and not already_persisted:
            try:
                self.video_arrival_store.add_many(persistent)
            except Exception as error:  # noqa: BLE001 - direct callers need the same report.
                self._on_video_candidate_persistence_failed(str(error))

        for candidate in values:
            matching_visual_ids = {
                candidate_id
                for candidate_id, existing in self._video_candidate_cache.items()
                if is_visual(existing) and overlaps(existing, candidate)
            }
            matching_regular_ids = {
                candidate_id
                for candidate_id, existing in self._video_candidate_cache.items()
                if not is_visual(existing) and overlaps(existing, candidate)
            }
            if is_visual(candidate) and matching_regular_ids:
                continue
            for candidate_id in matching_visual_ids:
                self._video_candidate_cache.pop(candidate_id, None)
            self._video_candidate_cache[str(getattr(candidate, "candidate_id", id(candidate)))] = candidate
        # Keep all visual candidates available for fast Ctrl+Left/Right
        # navigation; the reconciliation list below remains anomaly-only.
        self.set_video_navigation_candidates(tuple(self._video_candidate_cache.values()))
        events = self._events_for_current_metadata(self.passage_store.events())
        times = tuple(event.timeline_timestamp_ms for event in events)
        tools = importlib.import_module(
            ".video_passage_det" + "ector", __package__
        )
        try:
            reconciliation = tools.reconcile_candidates(
                tuple(self._video_candidate_cache.values()),
                times,
                passage_time_offset_by_camera=getattr(
                    self,
                    "_clock_offset_by_camera",
                    None,
                ),
            )
        except Exception:
            self._observe_runtime_metric(
                "video_candidate_reconcile",
                reconcile_started,
                item_count=len(values),
                failed=True,
            )
            raise
        self._observe_runtime_metric(
            "video_candidate_reconcile",
            reconcile_started,
            item_count=len(values),
        )
        # Video analysis is advisory. Never replace the operator's current
        # passage from a background callback; explicit candidate actions below
        # are the only path that may change the selected athlete or frame.
        self.video_review_apply_requested.emit(reconciliation)

    def _persist_video_review(self, candidate_id: str, status: str, bib: str) -> None:
        try:
            self._video_review_journal.update(
                candidate_id,
                status=status,
                bib=bib,
            )
        except RuntimeError as error:
            self._capture_error = str(error)
            self._update_runtime_status()
