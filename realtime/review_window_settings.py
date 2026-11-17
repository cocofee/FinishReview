"""Settings and race workspaces of the finish review window: saved races, live race, clearing and CycleRace races."""

from __future__ import annotations

import logging
import shutil
import time
from datetime import datetime
from pathlib import Path

from PyQt5.QtWidgets import QMessageBox

from .external_clip_import import race_id_from_passage_store
from .finish_line import FinishLineStore
from .event_workspace import (
    EventWorkspaceDescriptor,
    EventWorkspaceError,
    EventWorkspaceSummary,
    discover_event_workspaces,
    summarize_event_workspace,
    validate_event_workspace,
)
from .passage_evidence import PassageEvidenceAssociationStore, VideoClockCalibrationStore
from .passage_receiver import PassageEvent, PassageEventStore
from .preflight import PreflightJournal
from .race_metadata import RaceMetadata, RaceMetadataStore
from .workspace_context import (
    EventWorkspaceMetadataStore,
    is_event_folder_under,
    save_workspace_mode,
)
from .review_export import export_review_summary
from .review_clip import PassageReviewBindingStore
from .recording_lock import LOCK_FILENAME, FolderInUseError
from .settings import FinishReviewSettings
from .video_discovery import VideoDiscoveryStore
from .review_recorder import ArchiveTimelinePublisher, load_archive_recording_sessions
from .stream_recorder import sanitize_recording_message
from .video_timeline import VideoTimelineStore
from .video_arrival import VideoArrivalCandidateStore
from .video_review import VideoReviewJournal
from .review_window_common import (
    BEIJING_TIMEZONE,
    CYCLERACE_INBOX_DIRNAME,
    HIGH_SPEED_INDEX_FILENAME,
    _event_workspace_dir,
    _high_speed_target_dates,
    _historical_evidence_timestamp_overrides,
    _is_test_group_name,
    _saved_race_folders,
    _saved_race_id,
)

logger = logging.getLogger("FinishReview")


class FinishReviewSettingsMixin:
    """Apply settings, open saved or received races, return to the live race, clear race data."""

    def _current_settings(self, *, output_dir: Path | None = None) -> FinishReviewSettings:
        return FinishReviewSettings(
            source=self.source,
            output_dir=output_dir or self.workspace_root,
            passage_host=self.passage_host,
            passage_port=self.passage_port,
            camera_index=self.camera_index,
            secondary_source=self.secondary_source,
            high_speed_dir=self.high_speed_dir,
            finishreview_ip=self.finishreview_ip,
            cyclerace_ip=self.cyclerace_ip,
            high_speed_pc_ip=self.high_speed_pc_ip,
            switch_ip=self.switch_ip,
            timing_provider=self.timing_provider,
            racetiger_base_url=self.racetiger_base_url,
            racetiger_pc=self.racetiger_pc,
            racetiger_rid=self.racetiger_rid,
            racetiger_token=self.racetiger_token,
            racetiger_poll_interval_seconds=self.racetiger_poll_interval_seconds,
            visual_detection_enabled=self._visual_detection_enabled,
            visual_camera_index=self._visual_camera_index,
            visual_finish_line=self._visual_finish_line,
            visual_gate_width=self._visual_gate_width,
            visual_forward_direction=self._visual_forward_direction,
            visual_roi_top=self._visual_roi_top,
            visual_roi_bottom=self._visual_roi_bottom,
        )

    def _saved_event_workspaces(self) -> tuple[EventWorkspaceDescriptor, ...]:
        return discover_event_workspaces(self.workspace_root)

    def _received_event_candidates(self):
        if self.timing_provider != "cyclerace":
            return ()
        store = EventWorkspaceMetadataStore(
            self._receiver_metadata_store.snapshot_path, self._receiver_passage_store,
            VideoTimelineStore(self.workspace_root / ".finishreview" / "inbox_timeline.jsonl"),
        )
        received = self._receiver_passage_store.events(include_inactive=True)
        times = {}
        for event in received:
            scope = (event.race_id, event.stage_id)
            times[scope] = max(times.get(scope, 0), event.received_at_ms)
        return tuple(sorted(store.candidates(), key=lambda item: -times.get((item.race_id, item.stage_id), 0)))

    def _select_received_event(self, race_id, stage_id) -> bool:
        if self._recording_any_active():
            QMessageBox.warning(self, "无法切换赛事", "请先停止普通录像，再接入当前赛事。")
            return False
        metadata = next((item for item in self._received_event_candidates()
                         if (item.race_id, item.stage_id) == (race_id, stage_id)), None)
        if metadata is None:
            return False
        authoritative = self._receiver_metadata_store.current()
        if authoritative is not None and (authoritative.race_id, authoritative.stage_id) == (race_id, stage_id):
            return self._activate_cyclerace_workspace(authoritative, force=True, preserve_cyclerace_receiver=True)
        try:
            target = _event_workspace_dir(self.workspace_root, metadata)
            target.mkdir(parents=True, exist_ok=True)
            passages = PassageEventStore(target / "cyclerace_passage_events.jsonl")
            self._merge_cyclerace_events(passages, race_id)
            local = EventWorkspaceMetadataStore(target / "cyclerace_race_metadata.json", passages,
                                                VideoTimelineStore(target / "video_timeline.jsonl"))
            local.select(race_id, stage_id)
            applied = self._apply_settings(
                self._current_settings(output_dir=target), persist_settings=False,
                update_workspace_root=False, preserve_cyclerace_receiver=True, reload_data_source=True,
            )
            if applied:
                self._workspace_mode = "live"
                save_workspace_mode(self.workspace_root, target, "live_received")
                self._workspace_notice = "已接入收到的赛事；完整名单等待 CycleRace 同步"
                self._capture_error = ""
                self.refresh()
                self._update_runtime_status()
            return applied
        except (OSError, RuntimeError, ValueError) as error:
            QMessageBox.warning(self, "无法接入赛事", str(error))
            return False

    def _select_workspace_event(self, race_id, stage_id) -> bool:
        if self._recording_any_active():
            QMessageBox.warning(self, "无法切换赛事", "请先停止普通录像。")
            return False
        try:
            self.metadata_store.select(race_id, stage_id)
            save_workspace_mode(self.workspace_root, self.output_dir, "archive")
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "无法切换赛事", str(error))
            return False
        self._invalidate_capture_refresh()
        self._workspace_mode = "archive"
        self._selected_event_id = ""
        self._lookup_cache.clear()
        self.video_filmstrip.full_race.clear()
        self.refresh()
        self._update_runtime_status()
        return True

    @staticmethod
    def _saved_event_summary(
        workspace: EventWorkspaceDescriptor,
    ) -> EventWorkspaceSummary:
        return summarize_event_workspace(workspace)

    def _open_saved_event_workspace(self, path: Path) -> bool:
        if self.timing_provider != "cyclerace":
            QMessageBox.warning(self, "无法打开赛事", "打开赛事仅支持 CycleRace。")
            return False
        if self._recording_any_active():
            QMessageBox.warning(self, "无法打开赛事", "请先停止普通录像。")
            return False
        try:
            workspace = validate_event_workspace(path, self.workspace_root)
        except EventWorkspaceError as error:
            QMessageBox.warning(self, "无法打开赛事", str(error))
            return False
        if workspace.path == self.output_dir.resolve() and self._workspace_mode == "live":
            return True

        self._export_review_summary()
        applied = self._apply_settings(
            self._current_settings(output_dir=workspace.path),
            persist_settings=False,
            update_workspace_root=False,
            preserve_cyclerace_receiver=True,
        )
        if not applied:
            return False
        self._workspace_mode = "archive"
        save_workspace_mode(self.workspace_root, self.output_dir, "archive")
        self._archive_background_passage_count = 0
        self._workspace_notice = ""
        self._capture_error = ""
        self.refresh()
        self._update_runtime_status()
        return True

    def _may_enter_received_race(self, metadata: RaceMetadata) -> bool:
        """A race CycleRace sent on an earlier day gets no new folder without the operator's word.

        11-05: 「返回当前赛事」 made a folder for the previous evening's 接力测试赛.
        A race that already has its folder, or that CycleRace sent today, enters as before.
        """

        if any(race_id == metadata.race_id for _folder, race_id, _name in _saved_race_folders(self.workspace_root)):
            return True
        if (self.workspace_root / "cyclerace_race_metadata.json").is_file() and (
                _saved_race_id(self.workspace_root) == metadata.race_id):
            return True
        try:
            sent = datetime.fromtimestamp(self._receiver_metadata_store.snapshot_path.stat().st_mtime,
                                          BEIJING_TIMEZONE)
        except OSError:
            return True
        today = datetime.now(BEIJING_TIMEZONE).date()
        if sent.date() >= today:
            return True
        name = metadata.race_name or metadata.race_id
        stage = f"「{metadata.stage_name}」" if metadata.stage_name else ""
        answer = QMessageBox.question(
            self, "返回当前赛事",
            "\n\n".join((
                f"CycleRace 最后发来的是 {sent:%m-%d %H:%M} 的「{name}」{stage}，不是今天发来的。",
                "进入这场会为它新建本场文件夹。今天的比赛请先在 CycleRace 打开，它会重新发来。",
                "仍要进入这场吗？")),
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
        return answer == QMessageBox.Yes

    def _return_to_live_event(self) -> bool:
        if self._workspace_mode != "archive":
            return True
        metadata = self._receiver_metadata_store.current()
        if metadata is not None and not self._may_enter_received_race(metadata):
            return False
        try:
            if metadata is not None:
                applied = self._activate_cyclerace_workspace(
                    metadata,
                    force=True,
                    preserve_cyclerace_receiver=True,
                )
            else:
                received = self._received_event_candidates()
                if received:
                    applied = self._select_received_event(received[0].race_id, received[0].stage_id)
                elif self._current_metadata() is not None:
                    # 已选的赛事也可以现场录像，不要求计时设备重新发送名单或芯片。
                    save_workspace_mode(self.workspace_root, self.output_dir, "live")
                    applied = True
                else:
                    # Chinese kept out of strftime: Python 3.10 on an English Windows cannot encode it.
                    target = self.workspace_root / ("现场录像_" + datetime.now(BEIJING_TIMEZONE).strftime("%Y%m%d_%H%M%S_%f"))
                    applied = self._apply_settings(
                        self._current_settings(output_dir=target), persist_settings=False,
                        update_workspace_root=False, preserve_cyclerace_receiver=True,
                    )
                    if applied:
                        save_workspace_mode(self.workspace_root, self.output_dir, "live_ready")
        except Exception as error:  # noqa: BLE001 - keep the archive open on failure.
            QMessageBox.warning(
                self,
                "无法返回当前赛事",
                sanitize_recording_message(error),
            )
            return False
        if not applied:
            return False
        self._workspace_mode = "live"
        self._archive_background_passage_count = 0
        self._workspace_notice = "现场待机，可开始录像；无需等待芯片到达"
        self._capture_error = ""
        self._prepare_live_recording_view(int(time.time() * 1000), "现场待机：点击开始录像，等待本次画面。")
        self.refresh()
        self._apply_pending_focus()
        self._update_runtime_status()
        return True

    def _prepare_live_recording_view(self, started_at_ms: int, message: str) -> None:
        """Show this recording and nothing older; the earlier evidence stays saved.

        The filmstrip gets a fresh scope and the picture is cleared, so yesterday's
        replay cannot be mistaken for today's field picture.  The saved records of
        this race are untouched.
        """

        self._clear_selection_details()
        self._opened_normal_crossing = ""
        self.video_filmstrip.full_race.set_recording_start(started_at_ms)
        if self._problem_workbench_active:
            for pane in self.evidence_panes:
                pane.clear_passage(message)
            self._problem_count_mode_changed(True)

    def _clear_event_data(self) -> None:
        """Wipe this race's test data before the real race starts; nothing is kept back.

        赛前试芯片和试画面留下的读数、判读与试录录像都属于本场垃圾数据。终点线、
        认车模型、摄像头设置和保存位置不在此列，CycleRace 的正式成绩也不受任何影响。
        """

        panel = getattr(self, "problem_panel", None)
        # A held folder lock means a recorder of ours failed to stop.
        if self._recording_any_active() or self._recording_lock.held:
            QMessageBox.warning(self, "无法清空数据", "请先停止普通录像，再清空本场测试数据。")
            return
        if self._workspace_mode == "archive":
            QMessageBox.warning(
                self, "无法清空数据",
                "正在查看历史赛事，未做任何改动。请先点「返回现场」，再清空本场测试数据。",
            )
            return
        event_dir = self.output_dir.resolve()
        root = self.workspace_root.resolve()
        if event_dir == root:
            # The root also holds every other race's folder; clearing it would take them too.
            QMessageBox.warning(self, "无法清空数据", "本场数据直接放在保存根目录里，和其他赛事混在一起，未做任何改动。")
            return
        if not is_event_folder_under(event_dir, root):
            QMessageBox.warning(self, "无法清空数据", "本场目录不在保存根目录中，未做任何改动。")
            return
        count = len(self.passage_store.events(include_inactive=True))
        answer = QMessageBox.question(
            self,
            "清空本场数据",
            f"将删除本场的 {count} 条通过记录、问题判读、校时和试录画面，不能恢复。\n"
            "终点线、认车模型、摄像头和保存位置设置保留；CycleRace 正式成绩不受影响。\n\n"
            "是否清空？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        # The question ran an event loop, and a reading may have started recording.
        if self._recording_any_active():
            QMessageBox.warning(self, "无法清空数据", "请先停止普通录像，再清空本场测试数据。")
            return
        # Another window recording into this folder would lose its race; holding
        # the folder also keeps one from starting while the files go.
        try:
            self._recording_lock.acquire(self.output_dir)
        except FolderInUseError as error:
            QMessageBox.warning(
                self, "无法清空数据",
                f"另一个 FinishReview{error.owner}正在向这个赛事文件夹录像，未做任何改动。"
                "请先在那个窗口停止录像，再清空本场测试数据。",
            )
            return
        try:
            self._invalidate_capture_refresh()
            self._clear_selection_details()
            self._filmstrip_prefetch_timer.stop()
            self.playback_coordinator.shutdown()
            for pane in self.all_evidence_panes:
                pane.shutdown(wait=True)
            self.video_filmstrip.clear()
            self.video_filmstrip.full_race.clear()
            if panel is not None:
                panel.reset_data()
            # Retired decoders still own file handles until their reads finish.
            from .thread_lifecycle import wait_for_retired_threads
            wait_for_retired_threads(timeout_ms=5000)
            failures = self._remove_event_data(event_dir, root)
            for store, label in ((self.passage_store, "通过记录"), (self._receiver_passage_store, "收件箱")):
                try:
                    store.clear()
                except Exception as error:  # noqa: BLE001 - report what could not be removed.
                    failures.append(f"{label}：{error}")
        finally:
            # Hand the folder back before anything below may start recording.
            self._recording_lock.release()
        applied = self._apply_settings(
            self._current_settings(output_dir=self.output_dir),
            persist_settings=False,
            update_workspace_root=False,
            preserve_cyclerace_receiver=True,
            reload_data_source=True,
        )
        if not applied:
            return
        keep_marker = any(
            (self.output_dir / name).is_file()
            for name in ("cyclerace_race_metadata.json", "cyclerace_passage_events.jsonl")
        )
        try:
            save_workspace_mode(self.workspace_root, self.output_dir,
                                "live" if keep_marker else "live_ready")
        except (OSError, ValueError) as error:
            failures.append(f"赛事状态：{error}")
        self._workspace_notice = "本场数据已清空；可开始正式录像，测试读数不会保留。"
        self._refresh_problem_list()
        self.refresh()
        self._update_runtime_status()
        if failures:
            QMessageBox.warning(
                self, "部分文件未能删除",
                "以下文件仍留在磁盘上，请关闭占用它们的程序后重试：\n" + "\n".join(failures[:8]),
            )

    def _remove_event_data(self, event_dir: Path, root: Path) -> list[str]:
        """Delete the saved results of this race, keeping what steers recording.

        The sweep is a keep-list, not a name list: every journal written under the
        event directory is test data, while the finish line, the chosen model and
        the race identity are the settings an operator would hate to redraw.
        """

        from .problem_analysis import OUTPUT_DIRNAME

        keep = {
            "motion_finish_lines.json",
            "finish_lines.json",
            "cyclerace_race_metadata.json",
            "review_event_scope.json",
            ".finishreview",
            LOCK_FILENAME,
        }
        keep_in_problems = ("analysis_config",)
        failures: list[str] = []

        def remove(path: Path) -> None:
            try:
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink()
            except FileNotFoundError:
                return
            except OSError as error:
                failures.append(f"{path.name}：{error}")

        for entry in sorted(event_dir.iterdir(), key=lambda item: item.name):
            if entry.name in keep:
                continue
            if entry.name == OUTPUT_DIRNAME and entry.is_dir():
                for item in sorted(entry.iterdir(), key=lambda node: node.name):
                    if item.name.startswith(keep_in_problems):
                        continue
                    remove(item)
                continue
            remove(entry)
        for name in ("inbox_timeline.jsonl",):
            remove(root / CYCLERACE_INBOX_DIRNAME / name)
        return failures

    def _apply_settings(
        self,
        settings: FinishReviewSettings,
        *,
        stop_recording: bool = False,
        persist_settings: bool = True,
        update_workspace_root: bool = True,
        preserve_cyclerace_receiver: bool = False,
        reload_data_source: bool = False,
    ) -> bool:
        self._runtime_error = ""
        self._auto_recording_error = ""
        requested_output_dir = Path(settings.output_dir).expanduser().resolve()
        workspace_root_changed = bool(
            update_workspace_root and requested_output_dir != self.workspace_root
        )
        output_dir = (
            requested_output_dir
            if not update_workspace_root or workspace_root_changed
            else self.output_dir
        )
        output_changed = output_dir != self.output_dir
        next_timing_provider = str(settings.timing_provider or "cyclerace").strip().lower()
        if next_timing_provider not in {"cyclerace", "racetiger"}:
            next_timing_provider = "cyclerace"
        timing_changed = next_timing_provider != self.timing_provider
        next_racetiger_values = (
            str(settings.racetiger_base_url or "").strip(),
            str(settings.racetiger_pc or "").strip(),
            str(settings.racetiger_rid or "").strip(),
            str(settings.racetiger_token or ""),
            max(0.5, float(settings.racetiger_poll_interval_seconds or 2.0)),
        )
        racetiger_changed = next_racetiger_values != (
            self.racetiger_base_url,
            self.racetiger_pc,
            self.racetiger_rid,
            self.racetiger_token,
            self.racetiger_poll_interval_seconds,
        )
        data_source_changed = bool(
            reload_data_source
            or output_changed
            or timing_changed
            or workspace_root_changed
        )
        preserve_running_receiver = bool(
            preserve_cyclerace_receiver
            and self.timing_provider == "cyclerace"
            and next_timing_provider == "cyclerace"
            and not workspace_root_changed
            and self._receiver is not None
            and self._receiver.is_running
        )
        receiver_restart_needed = (
            data_source_changed
            or (self.timing_provider == "racetiger" and racetiger_changed)
        ) and not preserve_running_receiver
        prepared_data_source = None
        if data_source_changed:
            try:
                output_dir.mkdir(parents=True, exist_ok=True)
                passage_store = PassageEventStore(
                    output_dir
                    / (
                        "racetiger_passage_events.jsonl"
                        if next_timing_provider == "racetiger"
                        else "cyclerace_passage_events.jsonl"
                    )
                )
                timeline_store = VideoTimelineStore(
                    output_dir / "video_timeline.jsonl"
                )
                metadata_store = (
                    None
                    if next_timing_provider == "racetiger"
                    else EventWorkspaceMetadataStore(
                        output_dir / "cyclerace_race_metadata.json", passage_store, timeline_store
                    )
                )
                review_binding_store = PassageReviewBindingStore(
                    output_dir / "review_clips.jsonl"
                )
                association_store = PassageEvidenceAssociationStore(
                    output_dir / "passage_evidence_associations.jsonl"
                )
                calibration_store = VideoClockCalibrationStore(
                    output_dir / "video_clock_calibrations.jsonl"
                )
                video_discovery_store = VideoDiscoveryStore(
                    output_dir / "video_discoveries.jsonl"
                )
                preflight_journal = PreflightJournal(
                    output_dir / "preflight_tests.jsonl"
                )
                carried_metadata = None
                if (workspace_root_changed and not timing_changed
                        and next_timing_provider == "cyclerace"
                        and self._workspace_mode == "live"
                        and metadata_store.current() is None):
                    carried_metadata = self.metadata_store.current()
                    if carried_metadata is not None:
                        metadata_store.store(carried_metadata)
                        for event in self.passage_store.events(include_inactive=True):
                            if (event.race_id, event.stage_id) == (
                                carried_metadata.race_id, carried_metadata.stage_id
                            ):
                                passage_store.append(event)
                historical_events = passage_store.events()
                evidence_timestamp_overrides = (
                    _historical_evidence_timestamp_overrides(historical_events)
                    if next_timing_provider == "cyclerace"
                    else {}
                )
                archive_publishers = [
                    ArchiveTimelinePublisher(session, timeline_store)
                    for session in load_archive_recording_sessions(output_dir)
                ]
                receiver_passage_store = self._receiver_passage_store
                receiver_metadata_store = self._receiver_metadata_store
                if workspace_root_changed:
                    inbox_dir = requested_output_dir / CYCLERACE_INBOX_DIRNAME
                    receiver_passage_store = PassageEventStore(
                        inbox_dir / "cyclerace_passage_inbox.jsonl"
                    )
                    receiver_metadata_store = RaceMetadataStore(
                        inbox_dir / "cyclerace_metadata_inbox.json"
                    )
                    # Changing storage roots must not discard already received
                    # races merely because the destination contains an archive.
                    for event in self._receiver_passage_store.events(include_inactive=True):
                        receiver_passage_store.append(event)
                    live_metadata = self._receiver_metadata_store.current()
                    if (live_metadata is not None and self._live_metadata_scope ==
                            (live_metadata.race_id, live_metadata.stage_id)):
                        receiver_metadata_store.store(live_metadata)
                    if carried_metadata is not None:
                        receiver_metadata_store.store(carried_metadata)
                        for event in passage_store.events(include_inactive=True):
                            receiver_passage_store.append(event)
                prepared_data_source = (
                    passage_store,
                    metadata_store,
                    timeline_store,
                    review_binding_store,
                    association_store,
                    calibration_store,
                    video_discovery_store,
                    preflight_journal,
                    historical_events,
                    evidence_timestamp_overrides,
                    archive_publishers,
                    receiver_passage_store,
                    receiver_metadata_store,
                )
            except Exception as exc:  # noqa: BLE001 - validate before runtime mutation.
                self._runtime_error = sanitize_recording_message(exc)
                QMessageBox.warning(self, "设置无法应用", self._runtime_error)
                self._update_runtime_status()
                return False
        previous_runtime_settings = self._current_settings()
        if data_source_changed:
            self._capture_refresh_generation += 1
            if not self._capture_refresh_worker.invalidate_and_wait(
                self._capture_refresh_generation,
                timeout=5.0,
            ):
                QMessageBox.warning(
                    self,
                    "设置无法应用",
                    "后台录像刷新仍在运行，设置未切换。",
                )
                return False
        if persist_settings and self._settings_saver is not None:
            try:
                self._settings_saver(settings)
            except Exception as exc:  # noqa: BLE001 - keep current runtime unchanged.
                QMessageBox.warning(self, "设置未保存", str(exc))
                return False
        if stop_recording:
            failures = self.stop_recording()
            if any(failure.still_running for failure in failures):
                if persist_settings and self._settings_saver is not None:
                    try:
                        self._settings_saver(previous_runtime_settings)
                    except Exception:
                        logger.exception("Failed to roll back settings after stop failure")
                QMessageBox.warning(
                    self,
                    "设置无法应用",
                    "仍有录像进程未停止，设置未切换。",
                )
                return False
        if receiver_restart_needed:
            self.stop_receiver()
        if data_source_changed:
            self._stop_archive_video_scan_workers()
            self._passage_batch_timer.stop()
            self._pending_passages.clear()
        self.source = str(settings.source).strip()
        self.secondary_source = str(settings.secondary_source).strip()
        self.passage_host = str(settings.passage_host).strip()
        self.passage_port = int(settings.passage_port)
        self.camera_index = max(1, int(settings.camera_index))
        self.finishreview_ip = str(settings.finishreview_ip).strip()
        self.cyclerace_ip = str(settings.cyclerace_ip).strip()
        self.high_speed_pc_ip = str(settings.high_speed_pc_ip).strip()
        self.switch_ip = str(settings.switch_ip).strip()
        self.timing_provider = next_timing_provider
        (
            self.racetiger_base_url,
            self.racetiger_pc,
            self.racetiger_rid,
            self.racetiger_token,
            self.racetiger_poll_interval_seconds,
        ) = next_racetiger_values
        next_visual_values = (
            bool(settings.visual_detection_enabled),
            max(1, int(settings.visual_camera_index)),
            float(settings.visual_finish_line),
            float(settings.visual_gate_width),
            str(settings.visual_forward_direction or "left_to_right"),
            float(settings.visual_roi_top),
            float(settings.visual_roi_bottom),
        )
        visual_changed = next_visual_values != (
            self._visual_detection_enabled,
            self._visual_camera_index,
            self._visual_finish_line,
            self._visual_gate_width,
            self._visual_forward_direction,
            self._visual_roi_top,
            self._visual_roi_bottom,
        )
        (
            self._visual_detection_enabled,
            self._visual_camera_index,
            self._visual_finish_line,
            self._visual_gate_width,
            self._visual_forward_direction,
            self._visual_roi_top,
            self._visual_roi_bottom,
        ) = next_visual_values
        next_high_speed_dir = (
            Path(settings.high_speed_dir).expanduser().absolute()
            if settings.high_speed_dir is not None
            and str(settings.high_speed_dir).strip()
            else None
        )
        high_speed_changed = next_high_speed_dir != self.high_speed_dir
        if high_speed_changed:
            self.high_speed_dir = next_high_speed_dir
            self._high_speed_catalog.set_root(next_high_speed_dir)
        configured_camera_indexes = tuple(
            camera_index
            for camera_index, _source in self._configured_recording_sources()
        ) or (self.camera_index,)
        evidence_layout_changed = bool(
            configured_camera_indexes != self._configured_regular_camera_indexes
            or (self.high_speed_dir is not None) != self._show_high_speed_pane
        )
        if evidence_layout_changed:
            self._sync_evidence_pane_layout(include_recorded=False)
        if data_source_changed:
            assert prepared_data_source is not None
            self.video_filmstrip.full_race.set_recording_start(None)
            (
                passage_store,
                metadata_store,
                timeline_store,
                review_binding_store,
                association_store,
                calibration_store,
                video_discovery_store,
                preflight_journal,
                historical_events,
                evidence_timestamp_overrides,
                archive_publishers,
                receiver_passage_store,
                receiver_metadata_store,
            ) = prepared_data_source
            if workspace_root_changed:
                self.workspace_root = requested_output_dir
                self._receiver_passage_store = receiver_passage_store
                self._receiver_metadata_store = receiver_metadata_store
                self._workspace_mode = "live"
                self._archive_background_passage_count = 0
            self.output_dir = output_dir
            self._finish_line_store = FinishLineStore(
                self.output_dir / "motion_finish_lines.json", legacy_path=self.output_dir / "finish_lines.json"
            )
            self._finish_line_rois = self._finish_line_store.rois()
            self._video_review_journal = VideoReviewJournal(
                self.output_dir / "video_review.jsonl"
            )
            self.video_arrival_store = VideoArrivalCandidateStore(
                self.output_dir / "video_arrival_candidates.jsonl",
                metrics=self.runtime_metrics,
            )
            self._video_reconciliation_by_id.clear()
            self._video_candidate_cache = {
                str(candidate.candidate_id): candidate
                for candidate in self.video_arrival_store.candidates()
            }
            self.set_video_navigation_candidates(
                tuple(self._video_candidate_cache.values())
            )
            self._video_review_statuses.clear()
            self._video_review_bibs.clear()
            for record in self._video_review_journal.records():
                self.restore_video_review_record(
                    record.candidate_id,
                    status=record.status,
                    bib=record.bib,
                )
            self._video_reconciliation = ()
            self._active_video_anomaly_id = ""
            self._video_anomaly_cursor = -1
            self.passage_store = passage_store
            self.metadata_store = metadata_store
            self.timeline_store = timeline_store
            self.review_binding_store = review_binding_store
            self.association_store = association_store
            self.calibration_store = calibration_store
            self.video_discovery_store = video_discovery_store
            self._reload_video_discoveries()
            self._lookup_cache.clear()
            self._timeline_signature = ()
            self._selected_event_id = ""
            self._capture_windows_by_camera = {self.camera_index: {}}
            self._capture_windows = self._capture_windows_by_camera[
                self.camera_index
            ]
            self._archive_publishers = archive_publishers
            self._unsupported_event_ids.clear()
            self._preflight_journal = preflight_journal
            self._preflight_event_keys = set(preflight_journal.event_keys())
            self._evidence_timestamp_overrides = evidence_timestamp_overrides
            self._historical_passage_count = len(self._events_for_current_metadata(self.passage_store.events()))
            self._received_passage_count = 0
            self._received_passage_sequence = 0
            self._received_event_order.clear()
            self._capture_error = ""
            self.refresh()
            self._refresh_problem_list()
            if self._video_candidate_cache:
                self._reconcile_video_candidates(
                    tuple(self._video_candidate_cache.values())
                )
            if not preserve_running_receiver:
                try:
                    self.start_receiver()
                except Exception as exc:  # noqa: BLE001 - settings remain applied.
                    self._runtime_error = sanitize_recording_message(exc)
                    QMessageBox.warning(
                        self,
                        (
                            "赛虎读取未启动"
                            if self.timing_provider == "racetiger"
                            else "CycleRace监听未启动"
                        ),
                        self._runtime_error,
                    )
            self._high_speed_catalog.set_cache_path(
                output_dir / HIGH_SPEED_INDEX_FILENAME
            )
            self._high_speed_catalog.set_target_dates(
                _high_speed_target_dates(
                    historical_events,
                    self._evidence_timestamp_overrides,
                )
            )
        if high_speed_changed or data_source_changed:
            self.invalidate_external_locations()
            self._request_high_speed_scan()
        elif evidence_layout_changed:
            self.refresh()
        if receiver_restart_needed and not data_source_changed:
            try:
                self.start_receiver()
            except Exception as exc:  # noqa: BLE001 - settings remain applied.
                self._runtime_error = sanitize_recording_message(exc)
                QMessageBox.warning(
                    self,
                    "Timing source not started",
                    self._runtime_error,
                )
        if visual_changed and self._recording_any_active():
            self._start_visual_workers()
        self._update_runtime_status()
        return True

    def _clean_old_folders(self) -> None:
        """Folders left by earlier tests: the operator ticks which go (see workspace_cleanup)."""
        from .workspace_cleanup import clean_old_folders
        done = clean_old_folders(self, self.workspace_root, self.output_dir)
        if done:
            self.problem_panel.hint.setText(done)


    def _activate_cyclerace_workspace(
        self,
        metadata: RaceMetadata,
        *,
        force: bool = False,
        preserve_cyclerace_receiver: bool = False,
    ) -> bool:
        if self.timing_provider != "cyclerace" or self.metadata_store is None:
            return False
        current_metadata = self.metadata_store.current()
        if (
            not force
            and current_metadata is not None
            and current_metadata.race_id == metadata.race_id
        ):
            self.metadata_store.store(metadata)
            self._merge_cyclerace_events(self.passage_store, metadata.race_id)
            self.refresh()
            return False

        self._export_review_summary()
        target_dir = _event_workspace_dir(self.workspace_root, metadata)
        target_dir.mkdir(parents=True, exist_ok=True)
        target_metadata_store = RaceMetadataStore(
            target_dir / "cyclerace_race_metadata.json"
        )
        target_metadata_store.store(metadata)
        target_passage_store = PassageEventStore(
            target_dir / "cyclerace_passage_events.jsonl"
        )
        self._merge_cyclerace_events(target_passage_store, metadata.race_id)

        recording_was_active = self._recording_any_active()
        applied = self._apply_settings(
            self._current_settings(output_dir=target_dir),
            stop_recording=recording_was_active,
            persist_settings=False,
            update_workspace_root=False,
            preserve_cyclerace_receiver=preserve_cyclerace_receiver,
            reload_data_source=force,
        )
        if applied:
            self._workspace_mode = "live"
            save_workspace_mode(self.workspace_root, self.output_dir, "live")
            self._archive_background_passage_count = 0
            self._workspace_notice = ""
        if applied and recording_was_active:
            self._workspace_notice = "已切换新赛事，录像待重新开始"
        return applied

    def _merge_cyclerace_events(
        self,
        target_store: PassageEventStore,
        race_id: str,
    ) -> int:
        """Merge the newest live/inbox revisions into an event workspace.

        ``PassageEventStore.events()`` hides inactive events by default.  The
        inactive records are revisioned tombstones, however, and must travel
        with the workspace or an older active passage can be resurrected when
        switching back from an archive or restarting the application.
        """
        events_by_id: dict[str, PassageEvent] = {}
        stores = [target_store, self._receiver_passage_store]
        # During ``__init__`` the Qt base class has not been initialised yet,
        # so attribute lookup through QObject can raise RuntimeError.
        existing_store = self.__dict__.get("passage_store")
        if existing_store is not None and existing_store is not target_store:
            stores.append(existing_store)
        for store in stores:
            for event in store.events(include_inactive=True):
                if event.race_id != race_id:
                    continue
                current = events_by_id.get(event.event_id)
                if current is None or event.revision > current.revision:
                    events_by_id[event.event_id] = event
        merged = 0
        for event in events_by_id.values():
            current = target_store.get(event.event_id)
            if current is not None and current.revision >= event.revision:
                continue
            target_store.append(event)
            merged += 1
        return merged

    def _enter_race_opened_in_cyclerace(self) -> None:
        """Starting to record is when the race open in CycleRace gets its folder here.

        11-05: every race opened in CycleRace used to get a folder the moment its
        details arrived, so a morning of tests left dozens behind.
        """
        if self._workspace_mode != "live" or self.timing_provider != "cyclerace":
            return
        opened = self._receiver_metadata_store.current()
        if opened is None or self._live_metadata_scope != (opened.race_id, opened.stage_id):
            return
        current = self.metadata_store.current() if self.metadata_store is not None else None
        if current is not None and current.race_id == opened.race_id:
            return
        if opened.groups and all(_is_test_group_name(group.name) or _is_test_group_name(group.group_id)
                                 for group in opened.groups):
            return
        self._activate_cyclerace_workspace(opened)

    def _current_archive_race_id(self) -> str:
        metadata = self._current_metadata()
        if metadata is not None:
            return metadata.race_id
        return race_id_from_passage_store(self.passage_store)

    def _export_review_summary(self, *, show_warning: bool = False) -> Path | None:
        metadata = self._current_metadata()
        if self.timing_provider == "cyclerace" and metadata is None:
            return None
        if self.timing_provider == "racetiger" and not self.racetiger_rid:
            return None
        try:
            return export_review_summary(
                self.output_dir,
                self._events_for_current_metadata(self.passage_store.events()),
                self.association_store,
                metadata,
            )
        except Exception as error:  # noqa: BLE001 - review operation must not be blocked.
            logger.exception("Failed to export the event review summary")
            if show_warning:
                QMessageBox.warning(
                    self,
                    "复核清单未更新",
                    f"无法更新终点复核清单：{error}",
                )
            return None
