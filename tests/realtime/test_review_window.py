import csv
import os
import time
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import ClassVar

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtCore import QObject, Qt, pyqtSignal
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
)

from realtime import passage_review
from realtime import review_window as review_window_module
from realtime.auyat_rgb import AuyatScanResult
from realtime.passage_evidence import (
    REGULAR_SOURCE,
    PassageEvidenceAssociationStore,
)
from realtime.passage_receiver import PassageEvent, PassageEventStore, RaceFocus
from realtime.preflight import PreflightJournal, PreflightRun
from realtime.race_metadata import (
    RaceAthleteMetadata,
    RaceGroupMetadata,
    RaceMetadata,
    RaceMetadataStore,
)
from realtime.review_export import REVIEW_SUMMARY_FILENAME
from realtime.review_recorder import DirectShowVideoDevice, make_directshow_source
from realtime.review_window import (
    FinishReviewLaunchDialog,
    FinishReviewSettings,
    FinishReviewWindow,
)
from realtime.video_timeline import DEFAULT_CLOCK_SOURCE, VideoTimelineStore


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


class _FakePlaybackWorker(QObject):
    metadata_ready = pyqtSignal(int, float, int, int, int)
    frame_ready = pyqtSignal(object, int, int)
    full_resolution_ready = pyqtSignal(object, int, int)
    playback_finished = pyqtSignal()
    playback_error = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(self, video_path, parent=None):
        super().__init__(parent)
        self.video_path = Path(video_path)
        self.running = False

    def start(self):
        self.running = True
        self.metadata_ready.emit(6_000, 25.0, 640, 360, 150)

    def isRunning(self):
        return self.running

    def pause(self):
        pass

    def seek(self, _position_ms):
        pass

    def set_shuttle_speed(self, _speed):
        pass

    def step(self, _frame_delta):
        pass

    def request_full_resolution(self, _frame_index=None):
        pass

    def stop(self):
        if self.running:
            self.running = False
            self.finished.emit()

    def wait(self, _timeout_ms):
        return True


@pytest.fixture(autouse=True)
def fake_playback(monkeypatch):
    monkeypatch.setattr(passage_review, "VideoPlaybackWorker", _FakePlaybackWorker)


class _FakeRecorder:
    instances: ClassVar[list["_FakeRecorder"]] = []

    def __init__(self, source, output_dir, *, camera_index, **_kwargs):
        self.source = source
        self.output_dir = Path(output_dir)
        self.camera_index = camera_index
        self.is_running = False
        self.stopped = False
        type(self).instances.append(self)

    def start(self):
        buffer_dir = (
            self.output_dir
            / "review_buffer"
            / f"camera_{self.camera_index:02d}"
        )
        buffer_dir.mkdir(parents=True, exist_ok=True)
        self.passage_timestamp_ms = int(time.time() * 1000.0) + 1_000
        entries = tuple(
            (
                filename,
                datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc)
                .isoformat(timespec="milliseconds"),
            )
            for filename, timestamp_ms in (
                ("before.ts", self.passage_timestamp_ms - 3_000),
                ("finish.ts", self.passage_timestamp_ms - 1_000),
                ("after.ts", self.passage_timestamp_ms + 1_000),
            )
        )
        lines = ["#EXTM3U", "#EXT-X-VERSION:6"]
        for filename, started_at in entries:
            (buffer_dir / filename).write_bytes(b"video")
            lines.extend(
                [
                    f"#EXT-X-PROGRAM-DATE-TIME:{started_at}",
                    "#EXTINF:2.000,",
                    filename,
                ]
            )
        playlist = buffer_dir / "camera_01.m3u8"
        playlist.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.is_running = True
        return playlist

    def check_error(self):
        return None

    def stop(self):
        self.is_running = False
        self.stopped = True

class _FakeReceiver:
    instances: ClassVar[list["_FakeReceiver"]] = []

    def __init__(
        self,
        host,
        port,
        store,
        *,
        on_accepted,
        metadata_store=None,
        on_metadata_accepted=None,
        on_focus_accepted=None,
    ):
        self.host = host
        self.port = port
        self.listen_port = port or 18765
        self.store = store
        self.on_accepted = on_accepted
        self.metadata_store = metadata_store
        self.on_metadata_accepted = on_metadata_accepted
        self.on_focus_accepted = on_focus_accepted
        self.is_running = False
        self.stopped = False
        type(self).instances.append(self)

    def start(self):
        self.is_running = True

    def stop(self):
        self.is_running = False
        self.stopped = True

    def deliver(self, event):
        self.store.append(event)
        self.on_accepted(event)

    def deliver_metadata(self, metadata):
        self.metadata_store.store(metadata)
        self.on_metadata_accepted(metadata)

    def deliver_focus(self, focus):
        self.on_focus_accepted(focus)


class _FakeRaceTigerSource:
    instances: ClassVar[list["_FakeRaceTigerSource"]] = []

    def __init__(self, client, store, *, race_id, stage_id, poll_interval_seconds,
                 on_event, on_status):
        self.client = client
        self.store = store
        self.race_id = race_id
        self.stage_id = stage_id
        self.poll_interval_seconds = poll_interval_seconds
        self.on_event = on_event
        self.on_status = on_status
        self.is_running = False
        self.stopped = False
        type(self).instances.append(self)

    def start(self):
        self.is_running = True

    def stop(self):
        self.is_running = False
        self.stopped = True

    def deliver(self, event):
        self.on_event(event)


def _event(*, absolute=True, **overrides):
    recorder_timestamp_ms = (
        getattr(_FakeRecorder.instances[-1], "passage_timestamp_ms", None)
        if _FakeRecorder.instances
        else None
    )
    passage_timestamp_ms = (
        recorder_timestamp_ms or int(time.time() * 1000.0)
        if absolute
        else None
    )
    values = dict(
        event_id="race-1-stage-1-passage-15",
        race_id="race-1",
        stage_id="stage-1",
        group_id="men-open",
        sequence=15,
        chip_id="chip-15",
        bib="15",
        passage_time_ms=43_201_000,
        passage_timestamp_ms=passage_timestamp_ms,
        lap=1,
        emitted_at_ms=(passage_timestamp_ms or 0) + 100,
        race_name="2026 城市自行车赛",
        stage_name="第一赛段",
        group_name="男子公开组",
        athlete_name="张三",
        team_name="示例车队",
    )
    values.update(overrides)
    return PassageEvent(**values)


def _window(tmp_path, *, passage_batch_interval_ms=0):
    _FakeRecorder.instances.clear()
    _FakeReceiver.instances.clear()
    return FinishReviewWindow(
        "rtsp://camera/live",
        tmp_path,
        passage_host="127.0.0.1",
        passage_port=18765,
        passage_batch_interval_ms=passage_batch_interval_ms,
        recorder_factory=_FakeRecorder,
        receiver_factory=_FakeReceiver,
    )


def test_runtime_status_reports_loaded_race_metadata_without_claiming_sync(
    qapp,
    tmp_path,
):
    window = _window(tmp_path)
    window.start_receiver()
    window.metadata_store.store(
        RaceMetadata(
            race_id="race-11",
            stage_id="stage-1",
            revision=1,
            emitted_at_ms=1,
            race_name="11",
            stage_name="1",
            groups=(RaceGroupMetadata("elite-men", "男子精英组"),),
        )
    )

    window._update_runtime_status()

    assert window.receiver_status_label.text() == (
        "CycleRace: 监听中，已加载赛事 11 / 1"
    )
    window.group_value.setText("男子精英组")
    window._update_event_header()
    assert window.event_path_label.text() == "第 1 赛段 · 男子精英组 · 终点"
    assert "race-11" not in window.event_path_label.text()
    assert "赛事ID：race-11" in window.event_path_label.toolTip()
    assert "已读取 1 个组别" in window.receiver_status_label.toolTip()
    assert "不能判断发送端持续在线" in window.receiver_status_label.toolTip()
    assert window.receiver_status_label._detail_label.text() == "待数据"
    assert "background: #a56300" in window.receiver_status_label._dot.styleSheet()
    window.close()


def test_racetiger_mode_uses_racetiger_source_and_journal(qapp, tmp_path, monkeypatch):
    _FakeReceiver.instances.clear()
    _FakeRaceTigerSource.instances.clear()
    monkeypatch.setattr(review_window_module, "RaceTigerSource", _FakeRaceTigerSource)

    window = FinishReviewWindow(
        "rtsp://camera/live",
        tmp_path,
        timing_provider="racetiger",
        racetiger_base_url="https://rqs.racetigertiming.com",
        racetiger_pc="finish-pc",
        racetiger_rid="RID-2026",
        racetiger_token="local-test-token",
        receiver_factory=_FakeReceiver,
    )

    assert window.passage_store.journal_path.name == "racetiger_passage_events.jsonl"
    assert window.metadata_store is None
    window.start_receiver()

    assert not _FakeReceiver.instances
    assert len(_FakeRaceTigerSource.instances) == 1
    assert _FakeRaceTigerSource.instances[0].race_id == "RID-2026"
    assert window.receiver_status_label.text() == "赛虎: 正在读取"

    window.stop()
    window.close()


def test_deployment_snapshot_reports_running_racetiger_without_cycle_error(
    qapp,
    tmp_path,
    monkeypatch,
):
    _FakeRaceTigerSource.instances.clear()
    monkeypatch.setattr(review_window_module, "RaceTigerSource", _FakeRaceTigerSource)
    window = FinishReviewWindow(
        "rtsp://camera/live",
        tmp_path,
        timing_provider="racetiger",
        racetiger_base_url="https://rqs.racetigertiming.com",
        racetiger_pc="finish-pc",
        racetiger_rid="RID-2026",
        racetiger_token="local-test-token",
    )
    window.start_receiver()

    snapshot = window._deployment_runtime_snapshot()

    assert snapshot["timing_state"] == "待检查"
    assert snapshot["timing_detail"] == "赛虎读取服务运行中，尚未读取到本次终点记录"
    assert "CycleRace" not in snapshot["timing_detail"]
    window.close()


def test_stopped_racetiger_source_cannot_deliver_late_events(
    qapp,
    tmp_path,
    monkeypatch,
):
    _FakeRaceTigerSource.instances.clear()
    monkeypatch.setattr(review_window_module, "RaceTigerSource", _FakeRaceTigerSource)
    window = FinishReviewWindow(
        "rtsp://camera/live",
        tmp_path,
        timing_provider="racetiger",
        racetiger_base_url="https://rqs.racetigertiming.com",
        racetiger_pc="finish-pc",
        racetiger_rid="RID-2026",
        racetiger_token="local-test-token",
    )
    window.start_receiver()
    source = _FakeRaceTigerSource.instances[0]
    window.stop_receiver()

    source.deliver(_event(race_id="RID-2026", event_id="late-event"))
    qapp.processEvents()

    assert not window._pending_passages
    window.close()


def test_racetiger_view_filters_events_from_another_rid(qapp, tmp_path):
    window = FinishReviewWindow(
        "rtsp://camera/live",
        tmp_path,
        timing_provider="racetiger",
        racetiger_base_url="https://rqs.racetigertiming.com",
        racetiger_pc="finish-pc",
        racetiger_rid="RID-2026",
        racetiger_token="local-test-token",
    )

    events = (
        _event(event_id="old-rid", race_id="RID-OLD"),
        _event(event_id="current-rid", race_id="RID-2026"),
    )
    assert [event.event_id for event in window._events_for_current_metadata(events)] == [
        "current-rid"
    ]
    window.close()


def test_runtime_status_keeps_waiting_for_seal_visible_with_older_captures(
    qapp,
    tmp_path,
):
    window = _window(tmp_path)
    high_speed_root = tmp_path / "vendor"
    window.high_speed_dir = high_speed_root
    window._high_speed_catalog.set_root(high_speed_root)
    window._high_speed_scan_result = AuyatScanResult(
        status="ready",
        captures=(),
        changed=False,
        message="目录可访问，等待高速摄像软件完成判读并释放 1 个文件",
        waiting_file_count=1,
    )

    window._update_runtime_status()

    assert window.high_speed_status_label.text() == (
        "高速摄像: 本机测试目录可读，等待原厂软件完成判读"
    )
    assert window.high_speed_status_label._title_label.text() == "高速摄像"
    assert window.high_speed_status_label._detail_label.text() == "待机"
    window.close()


def test_cyclerace_metadata_creates_named_event_workspace(qapp, tmp_path):
    root = tmp_path / "events"
    window = _window(root)
    window.start_receiver()
    receiver = _FakeReceiver.instances[-1]
    metadata = RaceMetadata(
        race_id="race-2026",
        stage_id="stage-1",
        revision=1,
        emitted_at_ms=1,
        race_name="2026 城市公路自行车赛",
        stage_name="终点",
    )

    receiver.deliver_metadata(metadata)
    qapp.processEvents()

    event_dir = root / "2026 城市公路自行车赛"
    assert window.workspace_root == root.resolve()
    assert window.output_dir == event_dir.resolve()
    assert window.metadata_store.current() == metadata
    assert window.event_name_label.text() == metadata.race_name
    assert window._receiver_passage_store.journal_path.parent == (
        root / review_window_module.CYCLERACE_INBOX_DIRNAME
    ).resolve()
    assert receiver.stopped

    active_receiver = _FakeReceiver.instances[-1]
    event = _event(race_id=metadata.race_id, event_id="race-2026-passage-15")
    active_receiver.deliver(event)
    qapp.processEvents()

    stored_event = window.passage_store.get(event.event_id)
    assert stored_event is not None
    assert stored_event.received_at_ms > 0
    assert replace(stored_event, received_at_ms=0) == event
    assert window.passage_store.journal_path.parent == event_dir.resolve()
    snapshot = window._deployment_runtime_snapshot()
    assert snapshot["event_state"] == "赛事已加载"
    assert snapshot["event_name"] == metadata.race_name
    assert snapshot["event_stage"] == metadata.stage_name
    assert snapshot["event_dir"] == str(event_dir.resolve())
    assert snapshot["workspace_root"] == str(root.resolve())
    assert window._current_settings().output_dir == root.resolve()
    assert window._apply_settings(window._current_settings())
    assert window.output_dir == event_dir.resolve()
    window.close()

    restored = _window(root)
    assert restored.output_dir == event_dir.resolve()
    assert restored.metadata_store.current() == metadata
    assert restored.passage_store.get(event.event_id) == stored_event
    restored.close()


def test_same_named_cyclerace_events_do_not_share_workspace(qapp, tmp_path):
    root = tmp_path / "events"
    occupied = root / "城市赛"
    RaceMetadataStore(occupied / "cyclerace_race_metadata.json").store(
        RaceMetadata(
            race_id="race-old",
            stage_id="stage-1",
            revision=1,
            emitted_at_ms=1,
            race_name="城市赛",
            stage_name="终点",
        )
    )
    metadata = RaceMetadata(
        race_id="race-new",
        stage_id="stage-1",
        revision=1,
        emitted_at_ms=2,
        race_name="城市赛",
        stage_name="终点",
    )

    event_dir = review_window_module._event_workspace_dir(root, metadata)

    assert event_dir == (root / "城市赛_race-new").resolve()


def test_switching_cyclerace_event_exports_previous_review_summary(qapp, tmp_path):
    root = tmp_path / "events"
    window = _window(root)
    first_metadata = RaceMetadata(
        race_id="race-first",
        stage_id="stage-1",
        revision=1,
        emitted_at_ms=1,
        race_name="第一场赛事",
        stage_name="终点",
    )
    second_metadata = RaceMetadata(
        race_id="race-second",
        stage_id="stage-1",
        revision=1,
        emitted_at_ms=2,
        race_name="第二场赛事",
        stage_name="终点",
    )
    assert window._activate_cyclerace_workspace(first_metadata)
    window.passage_store.append(
        _event(race_id="race-first", event_id="first-passage")
    )
    first_event_dir = window.output_dir

    assert window._activate_cyclerace_workspace(second_metadata)

    summary_path = first_event_dir / REVIEW_SUMMARY_FILENAME
    assert summary_path.is_file()
    with summary_path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    assert rows[1][1:4] == ["15", "张三", "男子公开组"]
    window.close()


def test_saved_event_picker_filters_and_fits_laptop_screen(qapp, tmp_path):
    current_dir = tmp_path / "当前赛事"
    archive_dir = tmp_path / "历史赛事"
    for path, race_id, race_name in (
        (current_dir, "race-live", "当前城市赛"),
        (archive_dir, "race-archive", "历史公路赛"),
    ):
        RaceMetadataStore(path / "cyclerace_race_metadata.json").store(
            RaceMetadata(
                race_id=race_id,
                stage_id="stage-1",
                revision=1,
                emitted_at_ms=1,
                race_name=race_name,
                stage_name="终点",
            )
        )

    dialog = review_window_module.EventWorkspacePickerDialog(
        review_window_module.discover_event_workspaces(tmp_path),
        current_dir=current_dir,
    )

    assert dialog.minimumSize().height() <= 500
    assert dialog.table.rowCount() == 2
    assert dialog.cancel_button.text() == "取消"
    assert any(
        dialog.table.item(row, 3).text() == "当前打开"
        for row in range(dialog.table.rowCount())
    )

    dialog.search_edit.setText("历史")
    qapp.processEvents()

    visible_rows = [
        row
        for row in range(dialog.table.rowCount())
        if not dialog.table.isRowHidden(row)
    ]
    assert len(visible_rows) == 1
    assert dialog.table.item(visible_rows[0], 0).text() == "历史公路赛"
    assert dialog.selected_path == archive_dir.resolve()
    dialog.close()


def test_opening_saved_event_isolates_live_data_and_returns_to_current_event(
    qapp,
    tmp_path,
):
    root = tmp_path / "events"
    window = _window(root)
    window.start_receiver()
    initial_receiver = _FakeReceiver.instances[-1]
    live_metadata = RaceMetadata(
        race_id="race-live",
        stage_id="stage-1",
        revision=1,
        emitted_at_ms=1,
        race_name="当前赛事",
        stage_name="终点",
    )
    initial_receiver.deliver_metadata(live_metadata)
    qapp.processEvents()
    live_dir = window.output_dir
    receiver = window._receiver
    assert receiver is not None and receiver.is_running

    archive_dir = root / "历史赛事"
    archive_metadata = RaceMetadata(
        race_id="race-archive",
        stage_id="stage-1",
        revision=1,
        emitted_at_ms=1,
        race_name="历史赛事",
        stage_name="终点",
    )
    RaceMetadataStore(archive_dir / "cyclerace_race_metadata.json").store(
        archive_metadata
    )
    archive_event = _event(
        event_id="archive-passage",
        race_id=archive_metadata.race_id,
    )
    PassageEventStore(archive_dir / "cyclerace_passage_events.jsonl").append(
        archive_event
    )
    PassageEvidenceAssociationStore(
        archive_dir / "passage_evidence_associations.jsonl"
    ).confirm(
        passage_event_id=archive_event.event_id,
        bib=archive_event.bib,
        confirmed_source=REGULAR_SOURCE,
        segment_id="archive-segment",
        frame_index=12,
        position_ms=480,
        marker_x_normalized=0.5,
        marker_y_normalized=0.5,
        confirmed_at_ms=2_000,
    )
    video_path = archive_dir / "race_video" / "camera_01.mkv"
    video_path.parent.mkdir(parents=True)
    video_path.write_bytes(b"video")
    VideoTimelineStore(archive_dir / "video_timeline.jsonl").add_completed_segment(
        source_id="camera_01",
        camera_index=1,
        video_path=video_path,
        media_started_at_ms=archive_event.timeline_timestamp_ms - 1_000,
        media_duration_ms=4_000,
        clock_source=DEFAULT_CLOCK_SOURCE,
        timing_error_ms=0,
        end_reason="test",
        race_id=archive_metadata.race_id,
    )
    saved_settings = []
    window._settings_saver = saved_settings.append

    assert window._open_saved_event_workspace(archive_dir)

    assert window._workspace_mode == "archive"
    assert window.output_dir == archive_dir.resolve()
    assert window.passage_store.get(archive_event.event_id) == archive_event
    assert window.association_store.get(
        archive_event.event_id,
        REGULAR_SOURCE,
    ) is not None
    assert len(window.timeline_store.segments()) == 1
    assert window._receiver is receiver
    assert receiver.is_running and not receiver.stopped
    assert not window.record_button.isEnabled()
    assert saved_settings == []

    live_event = _event(event_id="live-passage", race_id=live_metadata.race_id)
    receiver.deliver(live_event)
    qapp.processEvents()

    assert window._receiver_passage_store.get(live_event.event_id) == live_event
    assert window.passage_store.get(live_event.event_id) is None
    assert PassageEventStore(
        archive_dir / "cyclerace_passage_events.jsonl"
    ).get(live_event.event_id) is None
    assert window.receiver_status_label.text() == (
        "CycleRace: 后台监听，已收到 1 条"
    )

    updated_live_metadata = RaceMetadata(
        race_id=live_metadata.race_id,
        stage_id=live_metadata.stage_id,
        revision=2,
        emitted_at_ms=2,
        race_name=live_metadata.race_name,
        stage_name=live_metadata.stage_name,
    )
    receiver.deliver_metadata(updated_live_metadata)
    qapp.processEvents()

    assert window.output_dir == archive_dir.resolve()
    assert window.metadata_store.current() == archive_metadata
    assert window._receiver_metadata_store.current() == updated_live_metadata

    assert window._return_to_live_event()

    assert window._workspace_mode == "live"
    assert window.output_dir == live_dir
    assert window.metadata_store.current() == updated_live_metadata
    assert window.passage_store.get(live_event.event_id) == live_event
    assert window._receiver is receiver
    assert receiver.is_running and not receiver.stopped
    assert window.record_button.isEnabled()
    assert saved_settings == []
    window.close()


def test_opening_saved_event_is_blocked_while_recording(
    qapp,
    tmp_path,
    monkeypatch,
):
    archive_dir = tmp_path / "历史赛事"
    RaceMetadataStore(archive_dir / "cyclerace_race_metadata.json").store(
        RaceMetadata(
            race_id="race-archive",
            stage_id="stage-1",
            revision=1,
            emitted_at_ms=1,
            race_name="历史赛事",
            stage_name="终点",
        )
    )
    warnings = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, title, message: warnings.append((title, message)),
    )
    window = _window(tmp_path)
    window.start_recording()
    original_dir = window.output_dir

    assert not window._open_saved_event_workspace(archive_dir)

    assert window.output_dir == original_dir
    assert window._workspace_mode == "live"
    assert warnings == [("无法打开赛事", "请先停止普通录像。")]
    window.close()


def test_closing_active_event_exports_review_summary(qapp, tmp_path):
    root = tmp_path / "events"
    window = _window(root)
    metadata = RaceMetadata(
        race_id="race-close",
        stage_id="stage-1",
        revision=1,
        emitted_at_ms=1,
        race_name="退出导出赛事",
        stage_name="终点",
    )
    assert window._activate_cyclerace_workspace(metadata)
    window.passage_store.append(
        _event(race_id="race-close", event_id="close-passage")
    )
    event_dir = window.output_dir

    window.close()

    assert (event_dir / REVIEW_SUMMARY_FILENAME).is_file()


def test_runtime_status_keeps_high_speed_directory_health_when_event_selected(
    qapp,
    tmp_path,
):
    window = _window(tmp_path)
    high_speed_root = tmp_path / "vendor"
    window.high_speed_dir = high_speed_root
    window._high_speed_catalog.set_root(high_speed_root)
    window._high_speed_scan_result = AuyatScanResult(
        status="ready",
        captures=(object(),),
        changed=False,
        message="",
    )
    window._selected_event_id = "event-without-high-speed-match"

    window._update_runtime_status()

    assert window.high_speed_status_label.text() == (
        "高速摄像: 本机测试数据可读，1 段"
    )
    assert window.high_speed_status_label._title_label.text() == "高速摄像"
    assert window.high_speed_status_label._detail_label.text() == "就绪"
    window.close()


def test_formal_console_starts_receives_and_publishes_review(qapp, tmp_path):
    window = _window(tmp_path)

    window.start()
    _FakeReceiver.instances[0].deliver(_event())
    qapp.processEvents()

    assert window.recorder.is_running
    assert window.receiver.is_running
    assert window.table.rowCount() == 1
    assert window.table.item(0, 1).text() == "15"
    assert window.table.item(0, 2).text() == "张三"
    assert window.race_value.text() == "2026 城市自行车赛"
    assert window.stage_value.text() == "第一赛段"
    assert window.group_value.text() == "男子公开组"
    assert window.operator_identity_label.text() == "男子公开组 · 1 / 1 · 未确认"
    assert window.table.item(0, 6).text() == "未确认"
    assert len(window.timeline_store.segments()) == 1
    assert "可核对 1" in window.capture_status_label.text()

    recorder = window.recorder
    receiver = window.receiver
    window.close()
    qapp.processEvents()
    assert recorder.stopped
    assert receiver.stopped


def test_first_live_formal_passage_auto_starts_recording(qapp, tmp_path):
    window = _window(tmp_path)
    window.start_receiver()

    _FakeReceiver.instances[-1].deliver(_event())
    qapp.processEvents()

    assert window.recorder is not None and window.recorder.is_running
    assert len(_FakeRecorder.instances) == 1
    assert window.passage_store.get("race-1-stage-1-passage-15") is not None
    window.close()


def test_live_formal_metadata_starts_recording_before_first_passage(
    qapp,
    tmp_path,
):
    window = _window(tmp_path)
    window.start_receiver()

    _FakeReceiver.instances[-1].deliver_metadata(
        RaceMetadata(
            race_id="race-formal",
            stage_id="stage-1",
            revision=1,
            emitted_at_ms=1,
            race_name="正式赛事",
            stage_name="终点",
            groups=(RaceGroupMetadata("men-open", "男子公开组"),),
        )
    )
    qapp.processEvents()

    assert window.recorder is not None and window.recorder.is_running
    assert len(_FakeRecorder.instances) == 1
    assert window.passage_store.events() == ()
    window.close()


def test_live_test_only_metadata_does_not_start_recording(qapp, tmp_path):
    window = _window(tmp_path)
    window.start_receiver()

    _FakeReceiver.instances[-1].deliver_metadata(
        RaceMetadata(
            race_id="race-test",
            stage_id="stage-1",
            revision=1,
            emitted_at_ms=1,
            groups=(RaceGroupMetadata("test-group", "测试组"),),
        )
    )
    qapp.processEvents()

    assert window.recorder is None
    window.close()


def test_archive_workspace_ignores_live_metadata_recording_trigger(qapp, tmp_path):
    window = _window(tmp_path)
    window._workspace_mode = "archive"

    window._on_metadata_received(
        RaceMetadata(
            race_id="race-formal",
            stage_id="stage-1",
            revision=1,
            emitted_at_ms=1,
            groups=(RaceGroupMetadata("men-open", "男子公开组"),),
        )
    )
    qapp.processEvents()

    assert window.recorder is None
    window.close()


@pytest.mark.parametrize("test_group_name", ["测试组", "检测组"])
def test_live_test_group_passage_does_not_auto_start_recording(
    qapp,
    tmp_path,
    test_group_name,
):
    window = _window(tmp_path)
    window.metadata_store.store(
        RaceMetadata(
            race_id="race-1",
            stage_id="stage-1",
            revision=1,
            emitted_at_ms=1,
            groups=(RaceGroupMetadata("test-group", test_group_name),),
        )
    )
    window.start_receiver()

    _FakeReceiver.instances[-1].deliver(
        _event(group_id="test-group", group_name="")
    )
    qapp.processEvents()

    assert window.recorder is None
    assert window.table.rowCount() == 1
    window.close()


def test_live_test_group_id_without_metadata_does_not_auto_start_recording(
    qapp,
    tmp_path,
):
    window = _window(tmp_path)
    window.start_receiver()

    _FakeReceiver.instances[-1].deliver(
        _event(group_id="test-group", group_name="")
    )
    qapp.processEvents()

    assert window.recorder is None
    assert window.table.rowCount() == 1
    window.close()


def test_first_formal_passage_after_test_group_starts_only_one_recorder(
    qapp,
    tmp_path,
):
    window = _window(tmp_path)
    window.metadata_store.store(
        RaceMetadata(
            race_id="race-1",
            stage_id="stage-1",
            revision=1,
            emitted_at_ms=1,
            groups=(
                RaceGroupMetadata("test-group", "检测组"),
                RaceGroupMetadata("men-open", "男子公开组"),
            ),
        )
    )
    window.start_receiver()
    receiver = _FakeReceiver.instances[-1]

    receiver.deliver(_event(group_id="test-group", group_name="检测组"))
    qapp.processEvents()
    assert window.recorder is None

    receiver.deliver(_event(event_id="formal-first", sequence=16))
    qapp.processEvents()
    assert window.recorder is not None and window.recorder.is_running

    receiver.deliver(_event(event_id="formal-second", sequence=17))
    qapp.processEvents()
    assert len(_FakeRecorder.instances) == 1
    window.close()


def test_historical_passage_does_not_auto_start_recording(qapp, tmp_path):
    window = _window(tmp_path)
    window.start_receiver()
    historical_timestamp_ms = int(time.time() * 1000.0) - 10 * 60 * 1000

    _FakeReceiver.instances[-1].deliver(
        _event(passage_timestamp_ms=historical_timestamp_ms)
    )
    qapp.processEvents()

    assert window.recorder is None
    assert window.table.rowCount() == 1
    window.close()


def test_auto_recording_failure_does_not_drop_live_passage(qapp, tmp_path):
    class _FailRecorder(_FakeRecorder):
        def start(self):
            raise RuntimeError("camera unavailable")

    _FailRecorder.instances.clear()
    _FakeReceiver.instances.clear()
    window = FinishReviewWindow(
        "rtsp://camera/live",
        tmp_path,
        passage_host="127.0.0.1",
        passage_port=18765,
        passage_batch_interval_ms=0,
        recorder_factory=_FailRecorder,
        receiver_factory=_FakeReceiver,
    )
    window.start_receiver()

    _FakeReceiver.instances[-1].deliver(_event())
    qapp.processEvents()

    assert window.recorder is None
    assert window.passage_store.get("race-1-stage-1-passage-15") is not None
    assert window.table.rowCount() == 1
    assert window._runtime_error == "camera unavailable"
    assert window.camera_status_label.text() == "录像设备: 自动启动失败"
    assert window.camera_status_label.toolTip() == "camera unavailable"
    window.close()


def test_live_passage_shows_preview_before_full_post_roll_is_ready(
    qapp,
    tmp_path,
):
    class _PreviewRecorder(_FakeRecorder):
        def start(self):
            playlist = super().start()
            self._complete_playlist = playlist.read_text(encoding="utf-8")
            lines = self._complete_playlist.splitlines()
            playlist.write_text(
                "\n".join(lines[:-3]) + "\n",
                encoding="utf-8",
            )
            self.playlist = playlist
            return playlist

        def seal_post_roll(self):
            self.playlist.write_text(
                self._complete_playlist,
                encoding="utf-8",
            )

    _FakeRecorder.instances.clear()
    _FakeReceiver.instances.clear()
    window = FinishReviewWindow(
        "rtsp://camera/live",
        tmp_path,
        passage_host="127.0.0.1",
        passage_port=18765,
        passage_batch_interval_ms=0,
        recorder_factory=_PreviewRecorder,
        receiver_factory=_FakeReceiver,
    )
    window.start()
    _FakeReceiver.instances[-1].deliver(_event())
    qapp.processEvents()

    preview_location = window.regular_pane.location
    assert preview_location is not None
    assert preview_location.status == "preview"
    assert preview_location.segment.end_reason == "passage_review_preview"
    assert preview_location.video_path.name.startswith("preview_")
    assert window.regular_pane.status_label.text() == "录像处理中"
    assert window.timeline_store.segments() == ()
    group_index = window.group_combo.findData("men-open")
    assert group_index >= 0
    window.group_combo.setCurrentIndex(group_index)
    qapp.processEvents()
    assert window.regular_pane.location.status == "preview"

    recorder = _FakeRecorder.instances[-1]
    recorder.seal_post_roll()
    window._refresh_capture_windows()
    qapp.processEvents()

    final_location = window.regular_pane.location
    assert final_location is not None
    assert final_location.status == "located"
    assert final_location.segment.end_reason == "passage_review_window"
    assert final_location.video_path.name.startswith("evidence_")
    assert final_location.video_path != preview_location.video_path
    assert len(window.timeline_store.segments()) == 1
    window.close()


def test_received_passage_uses_incremental_table_refresh(
    qapp,
    tmp_path,
    monkeypatch,
):
    window = _window(tmp_path)
    window.start()
    full_refresh_calls = 0

    def counted_refresh():
        nonlocal full_refresh_calls
        full_refresh_calls += 1

    monkeypatch.setattr(window, "refresh", counted_refresh)
    _FakeReceiver.instances[0].deliver(_event())
    qapp.processEvents()

    assert full_refresh_calls == 0
    assert window.table.rowCount() == 1
    assert window.table.item(0, 1).text() == "15"
    window.close()


def test_received_passages_are_coalesced_into_one_ui_and_archive_batch(
    qapp,
    tmp_path,
    monkeypatch,
):
    window = _window(tmp_path, passage_batch_interval_ms=1_000)
    window.start_receiver()
    window.start_recording()
    receiver = _FakeReceiver.instances[0]
    refreshed_batches = []
    archive_calls = 0

    def record_refresh(event_ids):
        refreshed_batches.append(set(event_ids))

    def record_archive(**_kwargs):
        nonlocal archive_calls
        archive_calls += 1
        return ()

    monkeypatch.setattr(window, "refresh_events", record_refresh)
    monkeypatch.setattr(window, "_publish_archive_segments", record_archive)
    for sequence in range(1, 4):
        receiver.deliver(
            _event(
                event_id=f"race-1-stage-1-passage-{sequence}",
                sequence=sequence,
                bib=str(sequence),
            )
        )
    qapp.processEvents()

    assert refreshed_batches == []
    assert "待处理 3" in window.receiver_status_label.text()

    window._passage_batch_timer.stop()
    window._flush_passage_batch()

    assert refreshed_batches == [
        {
            "race-1-stage-1-passage-1",
            "race-1-stage-1-passage-2",
            "race-1-stage-1-passage-3",
        }
    ]
    assert archive_calls == 1
    assert "本次收到 3 条" in window.receiver_status_label.text()
    window.close()


def test_focus_before_passage_shows_roster_then_auto_selects_passage(
    qapp,
    tmp_path,
):
    window = _window(tmp_path)
    window.start_receiver()
    receiver = _FakeReceiver.instances[0]
    receiver.deliver_metadata(
        RaceMetadata(
            race_id="race-1",
            stage_id="stage-1",
            revision=1,
            emitted_at_ms=1,
            race_name="11",
            stage_name="1",
            groups=(RaceGroupMetadata("men-open", "男子公开组"),),
            athletes=(
                RaceAthleteMetadata(
                    athlete_id="15",
                    bib="15",
                    name="十五号运动员",
                    team_name="示例队",
                    group_id="men-open",
                ),
            ),
        )
    )
    receiver.deliver_focus(
        RaceFocus(
            race_id="race-1",
            stage_id="stage-1",
            athlete_id="15",
            bib="15",
            group_id="men-open",
            emitted_at_ms=2,
        )
    )
    qapp.processEvents()

    assert window._selected_event_id == ""
    assert window.selected_identity_value.text() == "15"
    assert window.selected_time_value.text() == "尚无通过记录"
    assert window.operator_identity_label.text() == (
        "男子公开组 · 未进入终点记录 · 尚无通过记录"
    )

    receiver.deliver(_event())
    qapp.processEvents()

    assert window._selected_event_id == "race-1-stage-1-passage-15"
    assert window.selected_identity_value.text() == "15"
    window.close()


def test_confirm_and_next_does_not_advance_when_confirmation_fails(
    qapp,
    tmp_path,
    monkeypatch,
):
    window = _window(tmp_path)
    window.table.setRowCount(2)
    window.table.setCurrentCell(0, 0)
    window.regular_pane._pending_marker = (0.5, 0.5, 1, 100)
    move_calls = []
    monkeypatch.setattr(window, "_confirm_pending_marker", lambda _pane: False)
    monkeypatch.setattr(window, "_move_selection", move_calls.append)

    window._confirm_and_next()

    assert move_calls == []
    window.close()


def test_search_enter_does_not_confirm_pending_marker(
    qapp,
    tmp_path,
    monkeypatch,
):
    window = _window(tmp_path)
    window.show()
    window.start()
    _FakeReceiver.instances[0].deliver(_event())
    qapp.processEvents()
    window.regular_pane._pending_marker = (0.5, 0.5, 1, 100)
    confirmed_panes = []
    move_calls = []
    window.regular_pane.confirmation_requested.connect(confirmed_panes.append)
    monkeypatch.setattr(window, "_move_selection", move_calls.append)
    window._update_operator_controls()
    window.identity_search.setFocus()

    QTest.keyClick(window.identity_search, Qt.Key_Return)
    qapp.processEvents()

    assert confirmed_panes == []
    assert move_calls == []

    QTest.keyClick(window.regular_pane.video_view, Qt.Key_Return)
    qapp.processEvents()

    assert confirmed_panes == [window.regular_pane]
    window.close()


def test_legacy_time_of_day_is_not_matched_as_an_epoch(qapp, tmp_path):
    window = _window(tmp_path)
    window.start()

    _FakeReceiver.instances[0].deliver(_event(absolute=False))
    qapp.processEvents()

    assert window.table.rowCount() == 1
    assert len(window.timeline_store.segments()) == 0
    assert "缺少绝对时间 1" in window.capture_status_label.text()

    window.close()


def test_finish_console_entry_does_not_import_detection_main_window():
    root = Path(__file__).resolve().parents[2]
    entry = (root / "realtime" / "review_main.py").read_text(encoding="utf-8")
    window = (root / "realtime" / "review_window.py").read_text(encoding="utf-8")

    for forbidden in ("main_window", "detector", "ocr_manager", "ultralytics"):
        assert forbidden not in entry
        assert forbidden not in window


def test_device_settings_show_required_controls_without_receiver_values(qapp, tmp_path):
    dialog = FinishReviewLaunchDialog(
        FinishReviewSettings(
            source="",
            output_dir=tmp_path,
            passage_host="0.0.0.0",
            passage_port=18765,
            camera_index=1,
        ),
        device_provider=lambda: (),
    )

    assert dialog.camera_status_label.text() == "未检测到USB/Type-C摄像头"
    assert dialog.start_button.isEnabled()
    assert dialog.tabs.count() == 4
    assert dialog.tabs.tabText(dialog.tabs.indexOf(dialog.event_page)) == "赛事与保存"
    assert dialog.event_name_edit.isReadOnly()
    assert dialog.event_stage_edit.isReadOnly()
    assert dialog.event_dir_edit.isReadOnly()
    assert dialog.event_status_label.text() == "等待 CycleRace 赛事信息"
    assert not dialog.open_event_dir_button.isEnabled()
    assert dialog.output_edit.isReadOnly()
    visible_text = " ".join(
        widget.text()
        for widget_type in (QLabel, QLineEdit, QPushButton)
        for widget in dialog.findChildren(widget_type)
    )
    assert "赛事保存根目录" in visible_text
    assert "高速电脑共享目录" in visible_text
    assert "另一台高速摄像电脑" in visible_text
    assert "本机目录仅用于单机测试" in visible_text
    assert "比赛数据" not in visible_text
    assert "无需共享目录、无需填写IP" in visible_text
    assert "未启用认证，仅限受信任赛事局域网" in visible_text
    assert "255.255.255.0" in visible_text
    assert "网关和DNS留空" in visible_text
    assert "192.168.1.10" in visible_text
    assert "192.168.0.10" in visible_text
    assert dialog.rtsp_address_edit.isHidden()
    assert dialog.rtsp_username_edit.isHidden()
    assert dialog.rtsp_password_edit.isHidden()
    assert dialog.minimumSizeHint().height() <= 680
    for forbidden in ("0.0.0.0", "18765", "端口"):
        assert forbidden not in visible_text
    dialog.close()


def test_event_settings_show_and_open_active_cyclerace_workspace(
    qapp,
    tmp_path,
    monkeypatch,
):
    event_dir = tmp_path / "2026 城市公路自行车赛"
    event_dir.mkdir()
    operations = []
    monkeypatch.setattr(
        review_window_module,
        "_open_event_directory",
        lambda path: operations.append(("open", path)) or True,
    )
    dialog = FinishReviewLaunchDialog(
        FinishReviewSettings(
            source="",
            output_dir=tmp_path,
            passage_host="127.0.0.1",
            passage_port=18765,
            camera_index=1,
        ),
        device_provider=lambda: (),
        runtime_snapshot_provider=lambda: {
            "timing_provider": "cyclerace",
            "event_state": "赛事已加载",
            "event_name": "2026 城市公路自行车赛",
            "event_stage": "第 1 赛段",
            "event_dir": str(event_dir),
        },
        event_export_callback=lambda: operations.append(("export", event_dir)),
    )

    assert dialog.event_status_label.text() == "赛事已加载"
    assert dialog.event_name_edit.text() == "2026 城市公路自行车赛"
    assert dialog.event_stage_edit.text() == "第 1 赛段"
    assert dialog.event_dir_edit.text() == str(event_dir)
    assert dialog.open_event_dir_button.isEnabled()

    dialog._open_event_dir()

    assert operations == [
        ("export", event_dir),
        ("open", event_dir),
    ]
    dialog.close()


def test_windows_event_directory_opens_in_a_new_explorer_window(
    tmp_path,
    monkeypatch,
):
    launched = []
    monkeypatch.setattr(review_window_module, "IS_WINDOWS", True)
    monkeypatch.setattr(
        review_window_module.subprocess,
        "Popen",
        lambda command: launched.append(command),
    )

    assert review_window_module._open_event_directory(tmp_path)

    assert launched == [["explorer.exe", "/n,", str(tmp_path)]]


def test_device_settings_select_preinstalled_usb_camera_without_manual_ip(
    qapp,
    tmp_path,
):
    source = make_directshow_source("DJI Osmo Action 5 Pro")
    dialog = FinishReviewLaunchDialog(
        FinishReviewSettings(
            source=source,
            output_dir=tmp_path,
            passage_host="127.0.0.1",
            passage_port=18765,
            camera_index=1,
        ),
        device_provider=lambda: ("DJI Osmo Action 5 Pro",),
    )

    assert dialog.camera_status_label.text() == "已检测到摄像头，开始录像后验证画面"
    assert dialog.start_button.isEnabled()
    visible_text = " ".join(
        widget.text()
        for widget_type in (QLabel, QLineEdit, QPushButton)
        for widget in dialog.findChildren(widget_type)
    )
    combo_text = " ".join(
        widget.currentText() for widget in dialog.findChildren(QComboBox)
    )
    assert "DJI Osmo Action 5 Pro" in combo_text
    assert "dshow" not in visible_text.lower()
    assert dialog.settings.source == source
    dialog.close()


def test_device_settings_expose_racetiger_configuration(qapp, tmp_path):
    dialog = FinishReviewLaunchDialog(
        FinishReviewSettings(
            source="rtsp://camera/live",
            output_dir=tmp_path,
            passage_host="127.0.0.1",
            passage_port=18765,
            camera_index=1,
            timing_provider="racetiger",
            racetiger_base_url="https://rqs.racetigertiming.com",
            racetiger_pc="finish-pc",
            racetiger_rid="RID-2026",
            racetiger_token="local-test-token",
        ),
        device_provider=lambda: (),
    )

    assert dialog.timing_provider_combo.currentData() == "racetiger"
    assert dialog.racetiger_base_url_edit.isEnabled()
    assert dialog.racetiger_rid_edit.text() == "RID-2026"
    assert dialog.settings.timing_provider == "racetiger"
    assert dialog.settings.racetiger_token == "local-test-token"
    dialog.close()


def test_device_settings_expose_two_independent_rtsp_cameras(qapp, tmp_path):
    dialog = FinishReviewLaunchDialog(
        FinishReviewSettings(
            source="rtsp://one-user:one-pass@192.168.50.101/live",
            secondary_source="rtsp://two-user:two-pass@192.168.50.102/live",
            output_dir=tmp_path,
            passage_host="127.0.0.1",
            passage_port=18765,
            camera_index=1,
        ),
        device_provider=lambda: (),
    )

    assert dialog.secondary_rtsp_enabled_checkbox.isChecked()
    assert dialog.secondary_rtsp_address_edit.text() == (
        "rtsp://192.168.50.102/live"
    )
    assert dialog.secondary_rtsp_username_edit.text() == "two-user"
    assert dialog.secondary_rtsp_password_edit.text() == "two-pass"
    assert dialog.settings.secondary_source == (
        "rtsp://two-user:two-pass@192.168.50.102/live"
    )
    assert dialog.minimumSizeHint().height() <= 680
    dialog.close()


def test_device_settings_allow_two_independent_usb_cameras(qapp, tmp_path):
    first_input = "@device_pnp_dji_one"
    second_input = "@device_pnp_dji_two"
    dialog = FinishReviewLaunchDialog(
        FinishReviewSettings(
            source=make_directshow_source(first_input),
            secondary_source=make_directshow_source(second_input),
            output_dir=tmp_path,
            passage_host="127.0.0.1",
            passage_port=18765,
            camera_index=1,
        ),
        device_provider=lambda: (
            DirectShowVideoDevice("DJI Osmo Action 5 Pro（1）", first_input),
            DirectShowVideoDevice("DJI Osmo Action 5 Pro（2）", second_input),
        ),
    )

    assert dialog.secondary_enabled_checkbox.isChecked()
    assert dialog.secondary_source_type_combo.currentData() == "usb"
    assert dialog.device_combo.currentData() == first_input
    assert dialog.secondary_device_combo.currentData() == second_input
    assert dialog.device_combo.currentText().endswith("（1）")
    assert dialog.secondary_device_combo.currentText().endswith("（2）")
    assert dialog.settings.source == make_directshow_source(first_input)
    assert dialog.settings.secondary_source == make_directshow_source(second_input)
    assert "机位1已检测到" in dialog.camera_status_label.text()
    assert "机位2已检测到" in dialog.camera_status_label.text()
    dialog.close()


def test_device_settings_migrate_unique_legacy_usb_name(qapp, tmp_path):
    unique_input = "@device_pnp_dji_one"
    legacy_source = make_directshow_source(
        "DJI Osmo Action 5 Pro",
        video_size="1920x1080",
        framerate=50,
    )
    dialog = FinishReviewLaunchDialog(
        FinishReviewSettings(
            source=legacy_source,
            output_dir=tmp_path,
            passage_host="127.0.0.1",
            passage_port=18765,
            camera_index=1,
        ),
        device_provider=lambda: (
            DirectShowVideoDevice(
                "DJI Osmo Action 5 Pro",
                unique_input,
                "DJI Osmo Action 5 Pro",
            ),
        ),
    )

    assert dialog.device_combo.currentData() == unique_input
    assert dialog.camera_status_label.text() == (
        "已检测到摄像头，开始录像后验证画面"
    )
    assert dialog.settings.source == make_directshow_source(
        unique_input,
        video_size="1920x1080",
        framerate=50,
    )
    dialog.close()


def test_device_settings_do_not_guess_ambiguous_legacy_usb_name(qapp, tmp_path):
    legacy_name = "DJI Osmo Action 5 Pro"
    dialog = FinishReviewLaunchDialog(
        FinishReviewSettings(
            source=make_directshow_source(legacy_name),
            output_dir=tmp_path,
            passage_host="127.0.0.1",
            passage_port=18765,
            camera_index=1,
        ),
        device_provider=lambda: (
            DirectShowVideoDevice(
                "DJI Osmo Action 5 Pro（1）",
                "@device_pnp_dji_one",
                legacy_name,
            ),
            DirectShowVideoDevice(
                "DJI Osmo Action 5 Pro（2）",
                "@device_pnp_dji_two",
                legacy_name,
            ),
        ),
    )

    assert dialog.device_combo.currentData() == legacy_name
    assert dialog.device_combo.currentText().endswith("（当前未检测到）")
    assert dialog.camera_status_label.text() == "录像设备已配置，但当前未检测到"
    assert dialog.settings.source == make_directshow_source(legacy_name)
    dialog.close()


def test_device_settings_allow_dji_and_other_usb_camera(qapp, tmp_path):
    dialog = FinishReviewLaunchDialog(
        FinishReviewSettings(
            source=make_directshow_source("DJI Osmo Action 5 Pro"),
            secondary_source=make_directshow_source("USB Camera"),
            output_dir=tmp_path,
            passage_host="127.0.0.1",
            passage_port=18765,
            camera_index=1,
        ),
        device_provider=lambda: ("DJI Osmo Action 5 Pro", "USB Camera"),
    )

    assert dialog.device_combo.currentText() == "DJI Osmo Action 5 Pro"
    assert dialog.secondary_device_combo.currentText() == "USB Camera"
    assert dialog.settings.secondary_source == make_directshow_source("USB Camera")
    dialog.close()


def test_device_settings_allow_rtsp_and_secondary_usb_camera(qapp, tmp_path):
    secondary_input = "@device_pnp_dji_two"
    dialog = FinishReviewLaunchDialog(
        FinishReviewSettings(
            source="rtsp://camera-one/live",
            secondary_source=make_directshow_source(secondary_input),
            output_dir=tmp_path,
            passage_host="127.0.0.1",
            passage_port=18765,
            camera_index=1,
        ),
        device_provider=lambda: (
            DirectShowVideoDevice("DJI Osmo Action 5 Pro", secondary_input),
        ),
    )

    assert dialog.source_type_combo.currentData() == "rtsp"
    assert dialog.secondary_source_type_combo.currentData() == "usb"
    assert dialog.secondary_device_combo.currentData() == secondary_input
    assert not dialog.video_size_combo.isHidden()
    assert dialog.settings.source == "rtsp://camera-one/live"
    assert dialog.settings.secondary_source == make_directshow_source(
        secondary_input
    )
    dialog.close()


def test_device_settings_do_not_report_unplugged_saved_camera_as_detected(
    qapp,
    tmp_path,
):
    source = make_directshow_source("DJI Osmo Action 5 Pro")
    dialog = FinishReviewLaunchDialog(
        FinishReviewSettings(
            source=source,
            output_dir=tmp_path,
            passage_host="127.0.0.1",
            passage_port=18765,
            camera_index=1,
        ),
        device_provider=lambda: (),
    )

    assert dialog.camera_status_label.text() == "录像设备已配置，但当前未检测到"
    assert dialog.start_button.isEnabled()
    assert "当前未检测到" in dialog.device_combo.currentText()
    assert dialog.settings.source == source
    dialog.close()


def test_preflight_does_not_require_group_metadata_and_starts_recording_first(
    qapp,
    tmp_path,
):
    calls = []
    dialog = FinishReviewLaunchDialog(
        FinishReviewSettings(
            source="rtsp://camera/live",
            output_dir=tmp_path,
            passage_host="127.0.0.1",
            passage_port=18765,
            camera_index=1,
            high_speed_dir=tmp_path / "auyat",
        ),
        device_provider=lambda: (),
        passage_provider=lambda: calls.append("passages") or (),
        recording_start_callback=lambda: calls.append("recording") or True,
    )

    assert dialog.preflight_start_button.isEnabled()
    assert not hasattr(dialog, "preflight_group_combo")
    assert dialog.preflight_hint.wordWrap()

    dialog._start_preflight()

    assert calls[:2] == ["recording", "passages"]
    assert dialog._preflight_run is not None
    assert dialog._preflight_run.group_id == ""
    assert dialog._preflight_run.require_regular
    assert dialog._preflight_run.require_high_speed
    assert dialog.preflight_status_label.text() == (
        "普通录像已启动，等待任意测试芯片新过线"
    )
    dialog.close()


def test_preflight_requires_changed_video_settings_to_be_saved(
    qapp,
    tmp_path,
    monkeypatch,
):
    recording_calls = []
    dialog = FinishReviewLaunchDialog(
        FinishReviewSettings(
            source="rtsp://camera/live",
            output_dir=tmp_path,
            passage_host="127.0.0.1",
            passage_port=18765,
            camera_index=1,
            high_speed_dir=tmp_path / "auyat",
        ),
        device_provider=lambda: (),
        recording_start_callback=lambda: recording_calls.append(True) or True,
    )
    warnings = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *args: warnings.append(args[1:]),
    )
    dialog.rtsp_address_edit.setText("rtsp://other-camera/live")

    dialog._start_preflight()

    assert dialog._preflight_run is None
    assert not recording_calls
    assert warnings and warnings[0][0] == "请先保存设置"
    dialog.close()


def test_preflight_recording_callback_starts_normal_recording(qapp, tmp_path):
    window = _window(tmp_path)

    assert window._recorder is None
    assert window._start_preflight_recording()
    assert window._recorder is not None and window._recorder.is_running
    window.close()


def test_two_rtsp_sources_start_independent_review_pipelines(qapp, tmp_path):
    _FakeRecorder.instances.clear()
    window = FinishReviewWindow(
        "rtsp://camera-one/live",
        tmp_path,
        secondary_source="rtsp://camera-two/live",
        passage_host="127.0.0.1",
        passage_port=18765,
        recorder_factory=_FakeRecorder,
        receiver_factory=_FakeReceiver,
    )

    window.start_recording()

    assert [recorder.camera_index for recorder in _FakeRecorder.instances] == [1, 2]
    assert set(window._recorders) == {1, 2}
    assert set(window._ring_buffers) == {1, 2}
    assert window._recording_all_active()
    assert window.camera_status_label.text() == "录像设备: 全部已连接"
    window.close()


def test_two_regular_sources_show_two_panes_without_high_speed(qapp, tmp_path):
    window = FinishReviewWindow(
        "rtsp://camera-one/live",
        tmp_path,
        secondary_source="rtsp://camera-two/live",
        passage_host="127.0.0.1",
        passage_port=18765,
        recorder_factory=_FakeRecorder,
        receiver_factory=_FakeReceiver,
    )
    window.resize(window.minimumSize())
    window.show()
    qapp.processEvents()

    assert [pane.title_label.text() for pane in window.regular_panes] == [
        "机位 1",
        "机位 2",
    ]
    assert len(window.evidence_panes) == 2
    assert all(pane.isVisible() for pane in window.regular_panes)
    assert window.high_speed_pane.isHidden()
    assert all(
        pane.previous_frame_btn.isVisible() for pane in window.regular_panes
    )
    window.close()


def test_two_regular_sources_and_high_speed_show_three_compact_panes(qapp, tmp_path):
    window = FinishReviewWindow(
        "rtsp://camera-one/live",
        tmp_path,
        secondary_source="rtsp://camera-two/live",
        high_speed_dir=tmp_path / "auyat",
        passage_host="127.0.0.1",
        passage_port=18765,
        recorder_factory=_FakeRecorder,
        receiver_factory=_FakeReceiver,
    )
    window.resize(window.minimumSize())
    window.show()
    qapp.processEvents()

    assert len(window.evidence_panes) == 3
    assert all(pane.isVisible() for pane in window.evidence_panes)
    assert all(
        pane.previous_frame_btn.isHidden() for pane in window.evidence_panes
    )
    assert [pane.open_btn.text() for pane in window.regular_panes] == [
        "回放",
        "回放",
    ]
    assert window.high_speed_pane.open_btn.isHidden()
    for pane in window.evidence_panes:
        visible_controls = [
            widget
            for widget in (
                pane.time_label,
                pane.fit_btn,
                pane.maximize_btn,
                pane.mark_btn,
                pane.open_btn,
            )
            if not widget.isHidden()
        ]
        assert visible_controls[-1].geometry().right() <= pane.contentsRect().right()
        assert all(
            left.geometry().right() < right.geometry().left()
            for left, right in zip(visible_controls, visible_controls[1:])
        )
    window.close()


def test_applying_video_settings_updates_evidence_pane_layout(qapp, tmp_path):
    window = _window(tmp_path)
    window.show()
    qapp.processEvents()

    assert len(window.evidence_panes) == 1
    settings = replace(
        window._current_settings(),
        secondary_source="rtsp://camera-two/live",
        high_speed_dir=tmp_path / "auyat",
    )
    assert window._apply_settings(settings)
    qapp.processEvents()

    assert len(window.evidence_panes) == 3
    assert all(pane.isVisible() for pane in window.evidence_panes)

    settings = replace(
        window._current_settings(),
        secondary_source="",
        high_speed_dir=None,
    )
    assert window._apply_settings(settings)
    qapp.processEvents()

    assert len(window.evidence_panes) == 1
    assert len(window.regular_panes) == 1
    assert window.high_speed_pane.isHidden()
    window.close()


def test_two_usb_sources_start_independent_review_pipelines(qapp, tmp_path):
    _FakeRecorder.instances.clear()
    window = FinishReviewWindow(
        make_directshow_source("@device_pnp_dji_one"),
        tmp_path,
        secondary_source=make_directshow_source("@device_pnp_dji_two"),
        passage_host="127.0.0.1",
        passage_port=18765,
        recorder_factory=_FakeRecorder,
        receiver_factory=_FakeReceiver,
    )

    window.start_recording()

    assert [recorder.camera_index for recorder in _FakeRecorder.instances] == [1, 2]
    assert set(window._recorders) == {1, 2}
    assert window._recording_all_active()
    window.close()


def test_second_rtsp_start_failure_rolls_back_first_camera(qapp, tmp_path):
    class _FailSecondRecorder(_FakeRecorder):
        def start(self):
            if self.camera_index == 2:
                raise RuntimeError("camera two unavailable")
            return super().start()

    _FailSecondRecorder.instances.clear()
    window = FinishReviewWindow(
        "rtsp://camera-one/live",
        tmp_path,
        secondary_source="rtsp://camera-two/live",
        passage_host="127.0.0.1",
        passage_port=18765,
        recorder_factory=_FailSecondRecorder,
        receiver_factory=_FakeReceiver,
    )

    with pytest.raises(RuntimeError, match="camera two unavailable"):
        window.start_recording()

    assert not window._recorders
    assert _FailSecondRecorder.instances[0].stopped
    assert not _FailSecondRecorder.instances[0].is_running
    window.close()


def test_closing_settings_cancels_running_rtsp_probe(qapp, tmp_path):
    dialog = FinishReviewLaunchDialog(
        FinishReviewSettings(
            source="rtsp://camera/live",
            output_dir=tmp_path,
            passage_host="127.0.0.1",
            passage_port=18765,
            camera_index=1,
        ),
        device_provider=lambda: (),
    )

    class _RunningProbe:
        cancelled = False

        def isRunning(self):
            return True

        def cancel(self):
            self.cancelled = True

    worker = _RunningProbe()
    dialog._rtsp_probe_worker = worker

    dialog.done(QDialog.Rejected)

    assert worker.cancelled
    assert dialog._pending_dialog_result == QDialog.Rejected
    assert not dialog.isEnabled()
    dialog._on_rtsp_probe_worker_finished()
    assert dialog.result() == QDialog.Rejected


def test_cancelled_settings_dialog_does_not_stop_recording(
    qapp,
    tmp_path,
    monkeypatch,
):
    window = _window(tmp_path)
    window.start_recording()
    recorder = window._recorder

    class _CancelledDialog:
        def __init__(self, *_args, **_kwargs):
            pass

        def exec_(self):
            return QDialog.Rejected

    monkeypatch.setattr(
        review_window_module,
        "FinishReviewLaunchDialog",
        _CancelledDialog,
    )

    window._configure_devices()

    assert recorder is not None and recorder.is_running
    window.close()


def test_settings_save_failure_keeps_runtime_and_recording_unchanged(
    qapp,
    tmp_path,
    monkeypatch,
):
    def fail_save(_settings):
        raise OSError("config is read-only")

    window = FinishReviewWindow(
        "rtsp://camera/live",
        tmp_path,
        passage_host="127.0.0.1",
        passage_port=18765,
        recorder_factory=_FakeRecorder,
        receiver_factory=_FakeReceiver,
        settings_saver=fail_save,
    )
    window.start_recording()
    recorder = window._recorder
    warnings = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *args: warnings.append(args[1:]),
    )
    settings = FinishReviewSettings(
        source="rtsp://other-camera/live",
        output_dir=tmp_path,
        passage_host="127.0.0.1",
        passage_port=18765,
        camera_index=1,
    )

    applied = window._apply_settings(settings, stop_recording=True)

    assert not applied
    assert window.source == "rtsp://camera/live"
    assert recorder is not None and recorder.is_running
    assert warnings and warnings[0][0] == "设置未保存"
    window.close()


def test_invalid_output_directory_is_rejected_before_save_or_stop(
    qapp,
    tmp_path,
    monkeypatch,
):
    saved = []
    window = FinishReviewWindow(
        "rtsp://camera/live",
        tmp_path / "current",
        passage_host="127.0.0.1",
        passage_port=18765,
        recorder_factory=_FakeRecorder,
        receiver_factory=_FakeReceiver,
        settings_saver=saved.append,
    )
    window.start_recording()
    window.start_receiver()
    recorder = window._recorder
    receiver = window._receiver
    invalid_output = tmp_path / "not-a-directory"
    invalid_output.write_text("file", encoding="utf-8")
    warnings = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *args: warnings.append(args[1:]),
    )
    settings = FinishReviewSettings(
        source="rtsp://other-camera/live",
        output_dir=invalid_output,
        passage_host="127.0.0.1",
        passage_port=18765,
        camera_index=1,
    )

    applied = window._apply_settings(settings, stop_recording=True)

    assert not applied
    assert not saved
    assert window.output_dir == (tmp_path / "current").resolve()
    assert window.source == "rtsp://camera/live"
    assert recorder is not None and recorder.is_running
    assert receiver is not None and receiver.is_running
    assert warnings and warnings[0][0] == "设置无法应用"
    window.close()


def test_preflight_filter_uses_race_stage_and_event_identity(qapp, tmp_path):
    test_event = _event(event_id="shared-event")
    run = PreflightRun.start(
        (),
        started_at_ms=0,
        require_regular=False,
        require_high_speed=False,
    ).observe((test_event,))
    PreflightJournal(tmp_path / "preflight_tests.jsonl").append(
        run,
        recorded_at_ms=test_event.emitted_at_ms,
    )
    window = _window(tmp_path)
    later_race_event = _event(
        event_id="shared-event",
        race_id="race-2",
        stage_id="stage-2",
    )

    visible = window._events_for_current_metadata((test_event, later_race_event))

    assert visible == (later_race_event,)
    window.close()


def test_preflight_event_is_hidden_only_after_pass_and_can_be_restored(
    qapp,
    tmp_path,
):
    window = _window(tmp_path)
    event = _event(event_id="preflight-event")
    window.passage_store.append(event)
    run = PreflightRun.start(
        (),
        started_at_ms=0,
        require_regular=True,
        require_high_speed=True,
    ).observe((event,))

    window._record_preflight_event(
        run.with_evidence(regular_ready=False, high_speed_ready=False)
    )
    assert window._events_for_current_metadata((event,)) == (event,)

    window._record_preflight_event(
        run.with_evidence(regular_ready=True, high_speed_ready=True)
    )
    assert window._events_for_current_metadata((event,)) == ()

    restored, detail = window._restore_latest_preflight_event()

    assert restored
    assert "原始计时记录未被修改" in detail
    assert window._events_for_current_metadata((event,)) == (event,)
    window.close()


def test_runtime_status_hides_receiver_port(qapp, tmp_path):
    window = _window(tmp_path)

    window.start()

    assert window.receiver_status_label.text() == "CycleRace: 监听中，等待数据"
    assert "18765" not in window.receiver_status_label.text()
    window.close()


def test_historical_passages_do_not_report_current_session_reception(qapp, tmp_path):
    store = PassageEventStore(tmp_path / "cyclerace_passage_events.jsonl")
    store.append(_event())
    window = _window(tmp_path)

    window.start_receiver()
    qapp.processEvents()

    assert window.receiver_status_label.text() == (
        "CycleRace: 监听中，已加载历史 1 条"
    )
    assert "#a56300" in window.receiver_status_label.styleSheet()
    window.close()


def test_capture_error_remains_visible_while_recording(qapp, tmp_path):
    window = _window(tmp_path)
    window.start()

    window._capture_error = "timeline publish failed"
    window._update_runtime_status()

    assert window.recorder.is_running
    assert window.runtime_alert_label.text() == "证据处理异常"
    assert window.runtime_alert_label.toolTip() == "timeline publish failed"
    assert not window.runtime_alert_label.isHidden()
    assert window.capture_status_label.text() == (
        "证据处理异常：timeline publish failed"
    )
    assert window.capture_status_label.toolTip() == "timeline publish failed"
    assert "#b54747" in window.capture_status_label.styleSheet()
    window.close()


def test_low_storage_is_visible_in_runtime_alert(qapp, tmp_path, monkeypatch):
    window = _window(tmp_path)
    disk_usage = type("DiskUsage", (), {"free": 10 * 1024**3})()
    monkeypatch.setattr(review_window_module.shutil, "disk_usage", lambda _path: disk_usage)

    window._update_runtime_status()

    assert window.runtime_alert_label.text() == "磁盘空间不足"
    assert "10.0 GB" in window.runtime_alert_label.toolTip()
    assert "#a56300" in window.runtime_alert_label.styleSheet()
    assert not window.runtime_alert_label.isHidden()
    window.close()


def test_storage_failure_and_capture_error_share_runtime_alert(
    qapp,
    tmp_path,
    monkeypatch,
):
    window = _window(tmp_path)

    def fail_disk_usage(_path):
        raise OSError("disk unavailable")

    monkeypatch.setattr(review_window_module.shutil, "disk_usage", fail_disk_usage)
    window._capture_error = "timeline publish failed"
    window._update_runtime_status()

    assert window.runtime_alert_label.text() == "多项运行异常"
    assert "timeline publish failed" in window.runtime_alert_label.toolTip()
    assert "disk unavailable" in window.runtime_alert_label.toolTip()
    assert not window.runtime_alert_label.isHidden()
    window.close()


def test_runtime_status_does_not_block_on_stage_and_recording_date_mismatch(
    qapp,
    tmp_path,
):
    window = _window(tmp_path)
    current_date = datetime.now(review_window_module.BEIJING_TIMEZONE).date()
    stage_date = current_date - timedelta(days=1)
    window.metadata_store.store(
        RaceMetadata(
            race_id="race-11",
            stage_id="stage-1",
            revision=1,
            emitted_at_ms=1,
            stage_date=stage_date.isoformat(),
        )
    )
    window._received_passage_count = 1

    window._update_runtime_status()

    assert window.capture_status_label.text().startswith("本次待封口 0")
    assert "赛事日期" not in window.capture_status_label.text()
    assert "#667085" in window.capture_status_label.styleSheet()
    window.close()


def test_live_evidence_date_aligns_without_changing_formal_passage_time(
    qapp,
    tmp_path,
):
    received_time = datetime(2026, 8, 23, 8, 41, 22, tzinfo=timezone(timedelta(hours=8)))
    formal_time = datetime(2026, 8, 14, 8, 41, 20, tzinfo=timezone(timedelta(hours=8)))
    received_at_ms = int(received_time.timestamp() * 1000)
    formal_timestamp_ms = int(formal_time.timestamp() * 1000)
    event = _event(
        event_id="date-alignment-event",
        passage_timestamp_ms=formal_timestamp_ms,
        emitted_at_ms=int(
            datetime(
                2026,
                8,
                22,
                18,
                15,
                24,
                tzinfo=timezone(timedelta(hours=8)),
            ).timestamp()
            * 1000
        ),
        received_at_ms=received_at_ms,
    )
    window = _window(tmp_path)

    window._on_passage_received(event)
    window._passage_batch_timer.stop()

    assert event.timeline_timestamp_ms == formal_timestamp_ms
    assert window.passage_store.get(event.event_id).received_at_ms == received_at_ms
    assert window._evidence_timestamp(event) == int(
        datetime(2026, 8, 23, 8, 41, 20, tzinfo=timezone(timedelta(hours=8))).timestamp()
        * 1000
    )
    assert date(2026, 8, 23) in window._high_speed_catalog.target_dates
    assert "证据日期已对齐 1 条" in window.capture_status_label.text()
    assert "正式通过时间未改变" in window.capture_status_label.toolTip()
    window.close()


def test_evidence_date_alignment_survives_reopening(qapp, tmp_path):
    formal_time = datetime(
        2026,
        8,
        14,
        9,
        55,
        22,
        606_000,
        tzinfo=timezone(timedelta(hours=8)),
    )
    evidence_time = datetime(
        2026,
        8,
        23,
        9,
        55,
        22,
        606_000,
        tzinfo=timezone(timedelta(hours=8)),
    )
    event = _event(
        event_id="restart-date-alignment-event",
        passage_timestamp_ms=int(formal_time.timestamp() * 1000),
        emitted_at_ms=int(
            datetime(
                2026,
                8,
                22,
                18,
                15,
                24,
                tzinfo=timezone(timedelta(hours=8)),
            ).timestamp()
            * 1000
        ),
        received_at_ms=int(evidence_time.timestamp() * 1000) + 1_000,
    )
    PassageEventStore(tmp_path / "cyclerace_passage_events.jsonl").append(event)
    video_path = tmp_path / "videos" / "camera_01.mkv"
    video_path.parent.mkdir(parents=True)
    video_path.write_bytes(b"video")
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    timeline_store.add_completed_segment(
        source_id="camera_01_review",
        camera_index=1,
        video_path=video_path,
        media_started_at_ms=int(evidence_time.timestamp() * 1000) - 1_000,
        media_duration_ms=4_000,
        clock_source=DEFAULT_CLOCK_SOURCE,
        timing_error_ms=0,
        end_reason="archive_segment",
        race_id=event.race_id,
    )

    window = _window(tmp_path)
    qapp.processEvents()

    assert window._evidence_timestamp(event) == int(evidence_time.timestamp() * 1000)
    assert date(2026, 8, 23) in window._high_speed_catalog.target_dates
    assert window.table.item(0, 6).text() == "未确认"
    assert "证据日期已对齐 1 条" in window.capture_status_label.text()
    window.close()


def test_live_evidence_date_does_not_align_stale_bulk_data():
    received_time = datetime(2026, 8, 23, 9, 30, tzinfo=timezone(timedelta(hours=8)))
    formal_time = datetime(2026, 8, 14, 8, 41, 20, tzinfo=timezone(timedelta(hours=8)))

    assert review_window_module._align_live_evidence_timestamp(
        int(formal_time.timestamp() * 1000),
        int(received_time.timestamp() * 1000),
    ) == int(formal_time.timestamp() * 1000)


def test_existing_timeline_evidence_is_counted_after_reopening(qapp, tmp_path):
    passage_store = PassageEventStore(tmp_path / "cyclerace_passage_events.jsonl")
    event = _event()
    passage_store.append(event)
    video_path = tmp_path / "race_video" / "camera_01.mkv"
    video_path.parent.mkdir(parents=True)
    video_path.write_bytes(b"video")
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    segment = timeline_store.start_segment(
        source_id="camera_01",
        camera_index=1,
        video_path=video_path,
        started_at_ms=event.timeline_timestamp_ms - 1_000,
        race_id="race-1",
    )
    timeline_store.finish_segment(
        segment.segment_id,
        ended_at_ms=event.timeline_timestamp_ms + 3_000,
        media_started_at_ms=event.timeline_timestamp_ms - 1_000,
        media_duration_ms=4_000,
    )

    window = _window(tmp_path)
    qapp.processEvents()

    assert "本次可核对 0" in window.capture_status_label.text()
    assert "已有证据 1" in window.capture_status_label.text()
    window.close()


def test_formal_window_opens_point_playback_around_current_selected_time(
    qapp,
    tmp_path,
    monkeypatch,
):
    window = _window(tmp_path)
    event = _event()
    video_path = tmp_path / "videos" / "archive.mkv"
    video_path.parent.mkdir(parents=True)
    video_path.write_bytes(b"video")
    segment = window.timeline_store.add_completed_segment(
        source_id="camera_01_review",
        camera_index=1,
        video_path=video_path,
        media_started_at_ms=event.timeline_timestamp_ms - 60_000,
        media_duration_ms=120_000,
        clock_source=DEFAULT_CLOCK_SOURCE,
        timing_error_ms=2_000,
        end_reason="continuous_archive_fallback",
        race_id=event.race_id,
    )
    location = window.timeline_store.locate_passage(
        event.timeline_timestamp_ms,
        race_id=event.race_id,
    ).locations[0]
    window._shared_delta_ms = 250
    calls = {}

    class _Session:
        manifest_path = tmp_path / ".point.ffconcat"
        available_started_at_ms = event.timeline_timestamp_ms - 44_750
        available_ended_at_ms = event.timeline_timestamp_ms + 15_250
        target_position_ms = 45_000

        def cleanup(self):
            calls["cleaned"] = True

    def prepare(timeline_store, selected_location, **kwargs):
        calls["timeline_store"] = timeline_store
        calls["location"] = selected_location
        calls["prepare_kwargs"] = kwargs
        return _Session()

    class _PlaybackDialog:
        def __init__(self, video_path, parent, **kwargs):
            calls["dialog_path"] = video_path
            calls["dialog_parent"] = parent
            calls["dialog_kwargs"] = kwargs

        def exec_(self):
            calls["executed"] = True

    monkeypatch.setattr(review_window_module, "prepare_point_playback", prepare)
    monkeypatch.setattr(review_window_module, "VideoPlaybackDialog", _PlaybackDialog)

    assert window.regular_pane.open_btn.text() == "定点回放"
    window._open_location_if_available(event, location)

    assert calls["timeline_store"] is window.timeline_store
    assert calls["location"].segment.segment_id == segment.segment_id
    assert calls["prepare_kwargs"]["anchor_time_ms"] == (
        event.timeline_timestamp_ms + 250
    )
    assert calls["prepare_kwargs"]["race_id"] == event.race_id
    assert calls["dialog_kwargs"]["initial_position_ms"] == 35_000
    assert calls["dialog_kwargs"]["target_position_ms"] == 45_000
    assert calls["dialog_kwargs"]["autoplay"] is True
    assert calls["executed"] is True
    assert calls["cleaned"] is True
    window.close()


def test_recheck_retries_a_failed_cyclerace_receiver(qapp, tmp_path):
    class _FailOnceReceiver(_FakeReceiver):
        attempts = 0

        def start(self):
            type(self).attempts += 1
            if type(self).attempts == 1:
                raise OSError("address unavailable")
            super().start()

    _FailOnceReceiver.instances.clear()
    _FailOnceReceiver.attempts = 0
    window = FinishReviewWindow(
        "rtsp://camera/live",
        tmp_path,
        passage_host="127.0.0.1",
        passage_port=18765,
        recorder_factory=_FakeRecorder,
        receiver_factory=_FailOnceReceiver,
    )

    with pytest.raises(OSError, match="address unavailable"):
        window.start_receiver()
    assert window.receiver_status_label.text() == "CycleRace: 异常"

    window.recheck_button.click()
    qapp.processEvents()

    assert window.receiver is not None
    assert window.receiver.is_running
    assert window.receiver_status_label.text() == "CycleRace: 监听中，等待数据"
    window.close()


def test_formal_console_opens_before_recording_and_exposes_operator_controls(
    qapp,
    tmp_path,
):
    window = _window(tmp_path)

    window.start_receiver()
    window.resize(1366, 768)
    window.show()
    qapp.processEvents()

    assert window.windowTitle() == "FinishReview · 终点多源复核"
    assert window.recorder is None
    assert window.receiver.is_running
    assert window.record_button.text() == "开始录像"
    assert window.recheck_button.text() == "刷新"
    assert window.settings_button.text() == "设置"
    assert window.settings_button.toolTip() == "设备与赛事设置"
    assert not hasattr(window, "import_high_speed_button")
    assert window.receiver_status_label.text() == "CycleRace: 监听中，等待数据"
    assert window.operator_identity_label is window.current_context_label
    assert window.mark_regular_button is window.regular_pane.mark_btn
    assert window.mark_high_speed_button is window.high_speed_pane.mark_btn
    assert window.transport_layout.indexOf(window.confirm_next_button) >= 0
    assert window.capture_status_label.isHidden()
    assert window.product_title_label.text() == "FinishReview"
    assert window.product_subtitle_label.text() == "终点多源复核"
    assert window.product_subtitle_label.isHidden()
    assert window.event_path_label.isVisible()
    assert window.event_name_label.font().pointSize() >= 11
    assert window.event_path_label.font().pointSize() >= 10
    assert window.receiver_status_label._title_label.font().pointSize() >= 9
    assert window.receiver_status_label._detail_label.font().pointSize() >= 9
    assert len(window.beijing_clock_label.text().split(":")) == 3
    assert window.beijing_clock_label.font().pointSize() >= 11
    assert window.beijing_zone_label.text() == "北京时间"
    assert window.beijing_zone_label.font().pointSize() >= 9
    header = window.findChild(
        review_window_module.QFrame,
        "finishConsoleHeader",
    )
    assert header.minimumHeight() >= 78
    assert (
        window.event_name_label.sizePolicy().horizontalPolicy()
        == QSizePolicy.Ignored
    )
    assert window.recheck_button.isVisible()
    assert window.settings_button.isVisible()
    assert "color: #17212b" in window.recheck_button.styleSheet()
    assert "color: #17212b" in window.settings_button.styleSheet()
    assert window.recheck_button.geometry().right() < window.settings_button.geometry().left()
    assert window.settings_button.geometry().right() < window.record_button.geometry().left()
    assert (
        window.event_name_label.geometry().right()
        < window.event_path_label.geometry().left()
    )
    assert (
        window.event_path_label.geometry().right()
        < window.beijing_clock_label.geometry().left()
    )
    assert window.runtime_status_strip.geometry().top() > window.event_name_label.geometry().top()

    window.start_recording()
    assert window.recorder.is_running
    assert window.record_button.text() == "停止录像"
    assert window.camera_status_label.text() in {
        "录像设备: 正在检查",
        "录像设备: 已连接",
        "录像设备: 全部已连接",
    }
    window.close()


def test_compact_ready_status_uses_a_visible_green_indicator(qapp):
    indicator = review_window_module._CompactStatusIndicator("普通摄像")

    indicator.setStatus("录像设备: 全部已连接", "ready")
    indicator.setStyleSheet("color: #247a52;")

    assert "background: transparent" in indicator._surface_style
    assert indicator._title_label.text() == "普通摄像"
    assert indicator._detail_label.text() == "正常"
    assert "color: #176b49" in indicator._detail_label.styleSheet()
    assert "background: #247a52" in indicator._dot.styleSheet()
    indicator.close()


def test_compact_timing_status_distinguishes_waiting_from_ready(qapp):
    indicator = review_window_module._CompactStatusIndicator("计时源")

    indicator.setStatus("CycleRace: 监听中，等待数据", "waiting")
    indicator.setStyleSheet("color: #a56300;")

    assert indicator._detail_label.text() == "待数据"
    assert "background: #a56300" in indicator._dot.styleSheet()
    assert "正常" not in indicator._detail_label.text()
    indicator.close()


def test_recording_health_scans_buffer_without_pending_passages(
    qapp,
    tmp_path,
    monkeypatch,
):
    window = _window(tmp_path)
    window.start_recording()
    scan_calls = 0
    original_scan = window._ring_buffer.scan

    def counted_scan():
        nonlocal scan_calls
        scan_calls += 1
        return original_scan()

    monkeypatch.setattr(window._ring_buffer, "scan", counted_scan)

    window._refresh_capture_windows()

    assert scan_calls == 1
    window.close()
