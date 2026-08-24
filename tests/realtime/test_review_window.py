import os
import threading
import time
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
)

from realtime import passage_review
from realtime import review_window as review_window_module
from realtime.auyat_rgb import AuyatScanResult
from realtime.passage_receiver import PassageEvent, PassageEventStore, RaceFocus
from realtime.preflight import PreflightJournal, PreflightRun
from realtime.race_metadata import (
    RaceAthleteMetadata,
    RaceGroupMetadata,
    RaceMetadata,
)
from realtime.review_recorder import make_directshow_source
from realtime.review_window import (
    FinishReviewLaunchDialog,
    FinishReviewSettings,
    FinishReviewWindow,
)
from realtime.stream_recorder import RecordingError
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

    def deliver_status(self, status):
        self.on_status(status)


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
    assert "已读取 1 个组别" in window.receiver_status_label.toolTip()
    assert "不能判断发送端持续在线" in window.receiver_status_label.toolTip()
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


def test_queued_racetiger_callbacks_are_discarded_after_stop(
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
    queued_status = review_window_module.RaceTigerStatus(
        "error",
        "stale queued status",
    )

    worker = threading.Thread(
        target=lambda: (
            source.deliver(_event(race_id="RID-2026", event_id="queued-event")),
            source.deliver_status(queued_status),
        )
    )
    worker.start()
    worker.join()
    window.stop_receiver()
    qapp.processEvents()

    assert not window._pending_passages
    assert window._receiver_error == ""
    assert window._racetiger_status is not queued_status
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
    window.close()


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
    assert window.operator_identity_label.text() == "当前运动员：15 张三"
    assert window.table.item(0, 6).text() == "可查看"
    assert len(window.timeline_store.segments()) == 1
    assert "可核对 1" in window.capture_status_label.text()

    recorder = window.recorder
    receiver = window.receiver
    window.close()
    qapp.processEvents()
    assert recorder.stopped
    assert receiver.stopped


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
    assert window.operator_identity_label.text() == "当前运动员：15 十五号运动员"

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


def test_plain_enter_confirms_pending_marker_without_advancing(
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
    monkeypatch.setattr(
        window,
        "_confirm_pending_marker",
        lambda pane: confirmed_panes.append(pane) or True,
    )
    monkeypatch.setattr(window, "_move_selection", move_calls.append)
    window._update_operator_controls()
    window.identity_search.setFocus()

    QTest.keyClick(window.identity_search, Qt.Key_Return)
    qapp.processEvents()

    assert confirmed_panes == [window.regular_pane]
    assert move_calls == []
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
    assert dialog.output_edit.isReadOnly()
    visible_text = " ".join(
        widget.text()
        for widget_type in (QLabel, QLineEdit, QPushButton)
        for widget in dialog.findChildren(widget_type)
    )
    assert "录像证据保存" in visible_text
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
    assert dialog.racetiger_info_url_edit.isEnabled()
    assert dialog.racetiger_info_url_edit.text() == ""
    assert "PC finish-pc / RID RID-2026" in dialog.racetiger_config_status.text()
    assert "令牌已保存" in dialog.racetiger_config_status.text()
    assert dialog.settings.timing_provider == "racetiger"
    assert dialog.settings.racetiger_token == "local-test-token"
    dialog.close()


def test_device_settings_parse_complete_racetiger_info_url(qapp, tmp_path):
    dialog = FinishReviewLaunchDialog(
        FinishReviewSettings(
            source="rtsp://camera/live",
            output_dir=tmp_path,
            passage_host="127.0.0.1",
            passage_port=18765,
            camera_index=1,
            timing_provider="racetiger",
        ),
        device_provider=lambda: (),
    )

    dialog.racetiger_info_url_edit.setText(
        "https://rqs.racetigertiming.com/Dif/info?"
        "pc=test-pc&rid=RID-TEST&token=local-test-token"
    )
    dialog.racetiger_info_url_edit.editingFinished.emit()

    assert dialog.racetiger_info_url_edit.text() == ""
    assert "PC test-pc / RID RID-TEST" in dialog.racetiger_config_status.text()
    assert "点击保存设置后生效" in dialog.racetiger_config_status.text()
    assert "令牌已保存" not in dialog.racetiger_config_status.text()
    assert dialog.settings.racetiger_base_url == "https://rqs.racetigertiming.com"
    assert dialog.settings.racetiger_pc == "test-pc"
    assert dialog.settings.racetiger_rid == "RID-TEST"
    assert dialog.settings.racetiger_token == "local-test-token"
    dialog.close()


@pytest.mark.parametrize(
    "value",
    [
        "https://example.test/Dif/info?pc=1&rid=2&token=test",
        "https://rqs.racetigertiming.com/Dif/score?pc=1&rid=2&token=test",
        "https://rqs.racetigertiming.com/Dif/info?pc=1&rid=2",
    ],
)
def test_parse_racetiger_info_url_rejects_wrong_or_incomplete_link(value):
    with pytest.raises(ValueError):
        review_window_module.parse_racetiger_info_url(value)


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


def test_partial_camera_failure_uses_one_click_session_recovery(qapp, tmp_path):
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
    original_recorders = tuple(_FakeRecorder.instances)
    original_recorders[1].is_running = False
    window._update_runtime_status()

    assert window.record_button.text() == "恢复录像"

    window._toggle_recording()

    assert len(_FakeRecorder.instances) == 4
    assert all(recorder.stopped for recorder in original_recorders)
    assert all(recorder.is_running for recorder in _FakeRecorder.instances[2:])
    assert window._recording_all_active()
    assert window.record_button.text() == "停止录像"
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


def test_recording_stop_failure_keeps_running_camera_for_retry(qapp, tmp_path):
    class _FailOnceStopRecorder(_FakeRecorder):
        def stop(self):
            self.stop_calls = getattr(self, "stop_calls", 0) + 1
            if self.stop_calls == 1:
                raise OSError("shutdown unavailable")
            super().stop()

    window = FinishReviewWindow(
        "rtsp://camera-one/live",
        tmp_path,
        passage_host="127.0.0.1",
        passage_port=18765,
        recorder_factory=_FailOnceStopRecorder,
        receiver_factory=_FakeReceiver,
    )
    window.start_recording()
    recorder = window._recorder

    assert not window.stop_recording()
    assert recorder is not None and recorder.is_running
    assert set(window._recorders) == {1}
    assert "机位1" in window._runtime_error

    assert window.stop_recording()
    assert not window._recorders
    assert recorder.stop_calls == 2
    assert window._runtime_error == ""
    assert window.camera_status_label.text() != "录像设备: 异常"
    window.close()


def test_partial_stop_keeps_active_camera_archive_open(qapp, tmp_path, monkeypatch):
    class _FailFirstCameraOnceRecorder(_FakeRecorder):
        def stop(self):
            self.stop_calls = getattr(self, "stop_calls", 0) + 1
            if self.camera_index == 1 and self.stop_calls == 1:
                raise OSError("shutdown unavailable")
            super().stop()

    class _CapturingArchivePublisher:
        def __init__(self, recorder):
            self.recorder = recorder
            self.recording_values = []

        def publish_completed(self, *, race_id, recording):
            self.recording_values.append(recording)
            return ()

    window = FinishReviewWindow(
        "rtsp://camera-one/live",
        tmp_path,
        secondary_source="rtsp://camera-two/live",
        passage_host="127.0.0.1",
        passage_port=18765,
        recorder_factory=_FailFirstCameraOnceRecorder,
        receiver_factory=_FakeReceiver,
    )
    window.start_recording()
    publishers = {
        camera_index: _CapturingArchivePublisher(recorder)
        for camera_index, recorder in window._recorders.items()
    }
    window._archive_publishers = list(publishers.values())
    monkeypatch.setattr(window, "_current_archive_race_id", lambda: "race-1")

    assert not window.stop_recording()

    assert publishers[1].recording_values
    assert all(publishers[1].recording_values)
    assert publishers[2].recording_values
    assert not any(publishers[2].recording_values)

    assert window.stop_recording()
    window.close()


def test_stopped_recorder_warning_does_not_block_settings_apply(
    qapp,
    tmp_path,
    monkeypatch,
):
    class _StoppedWithWarningRecorder(_FakeRecorder):
        def stop(self):
            self.is_running = False
            self.stopped = True
            raise RecordingError("ffmpeg exited with code 7")

    window = FinishReviewWindow(
        "rtsp://camera-one/live",
        tmp_path,
        passage_host="127.0.0.1",
        passage_port=18765,
        recorder_factory=_StoppedWithWarningRecorder,
        receiver_factory=_FakeReceiver,
    )
    window.start_recording()
    warnings = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *args: warnings.append(args[1:]),
    )
    settings = FinishReviewSettings(
        source="rtsp://camera-two/live",
        output_dir=tmp_path,
        passage_host="127.0.0.1",
        passage_port=18765,
        camera_index=1,
    )

    assert window._apply_settings(settings, stop_recording=True)
    assert window.source == "rtsp://camera-two/live"
    assert not any(title == "录像未停止" for title, _detail in warnings)
    window.close()


def test_settings_apply_rolls_back_when_recording_stop_fails(
    qapp,
    tmp_path,
    monkeypatch,
):
    class _FailOnceStopRecorder(_FakeRecorder):
        def stop(self):
            self.stop_calls = getattr(self, "stop_calls", 0) + 1
            if self.stop_calls == 1:
                raise OSError("shutdown unavailable")
            super().stop()

    saved = []
    window = FinishReviewWindow(
        "rtsp://camera-one/live",
        tmp_path,
        passage_host="127.0.0.1",
        passage_port=18765,
        recorder_factory=_FailOnceStopRecorder,
        receiver_factory=_FakeReceiver,
        settings_saver=saved.append,
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
        source="rtsp://camera-two/live",
        output_dir=tmp_path,
        passage_host="127.0.0.1",
        passage_port=18765,
        camera_index=1,
    )

    applied = window._apply_settings(settings, stop_recording=True)

    assert not applied
    assert window.source == "rtsp://camera-one/live"
    assert recorder is not None and recorder.is_running
    assert saved[0] == settings
    assert saved[-1].source == "rtsp://camera-one/live"
    assert warnings and warnings[-1][0] == "录像未停止"

    assert window.stop_recording()
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


def test_settings_apply_rolls_back_when_receiver_stop_fails(
    qapp,
    tmp_path,
    monkeypatch,
):
    class _FailOnceStopReceiver(_FakeReceiver):
        def stop(self):
            self.stop_calls = getattr(self, "stop_calls", 0) + 1
            if self.stop_calls == 1:
                raise OSError("shutdown unavailable")
            super().stop()

    saved = []
    current_output = tmp_path / "current"
    window = FinishReviewWindow(
        "rtsp://camera/live",
        current_output,
        passage_host="127.0.0.1",
        passage_port=18765,
        receiver_factory=_FailOnceStopReceiver,
        settings_saver=saved.append,
    )
    window.start_receiver()
    receiver = window.receiver
    warnings = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *args: warnings.append(args[1:]),
    )
    settings = FinishReviewSettings(
        source="rtsp://camera/live",
        output_dir=tmp_path / "next",
        passage_host="127.0.0.1",
        passage_port=18765,
        camera_index=1,
    )

    applied = window._apply_settings(settings)

    assert not applied
    assert window.output_dir == current_output.resolve()
    assert receiver is not None and receiver.is_running
    assert saved[0] == settings
    assert saved[-1].output_dir == current_output.resolve()
    assert warnings and warnings[-1][0] == "计时源未停止"

    assert window.stop_receiver()
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
    assert window.capture_status_label.text() == (
        "证据处理异常：timeline publish failed"
    )
    assert window.capture_status_label.toolTip() == "timeline publish failed"
    assert "#b54747" in window.capture_status_label.styleSheet()
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
        emitted_at_ms=received_at_ms,
    )
    window = _window(tmp_path)

    window._on_passage_received(event)
    window._passage_batch_timer.stop()

    assert event.timeline_timestamp_ms == formal_timestamp_ms
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
        emitted_at_ms=int(evidence_time.timestamp() * 1000) + 1_000,
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
    assert window.table.item(0, 6).text() == "可查看"
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


def test_preflight_evidence_accepts_default_clock_recording(qapp, tmp_path):
    window = _window(tmp_path)
    event = _event()
    video_path = tmp_path / "race_video" / "camera_01.mkv"
    video_path.parent.mkdir(parents=True)
    video_path.write_bytes(b"video")
    window.timeline_store.add_completed_segment(
        source_id="camera_01_review",
        camera_index=1,
        video_path=video_path,
        media_started_at_ms=event.timeline_timestamp_ms - 1_000,
        media_duration_ms=4_000,
        clock_source=DEFAULT_CLOCK_SOURCE,
        timing_error_ms=0,
        end_reason="archive_segment",
        race_id=event.race_id,
    )

    regular_ready, high_speed_ready, regular_detail, _high_speed_detail = (
        window._preflight_evidence_status(event)
    )

    assert regular_ready
    assert not high_speed_ready
    assert regular_detail == "普通录像全部机位已覆盖测试时间点"
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

    assert window.windowTitle() == "终点复核系统"
    assert window.recorder is None
    assert window.receiver.is_running
    assert window.record_button.text() == "开始录像"
    assert window.settings_button.text() == "设备设置"
    assert not hasattr(window, "import_high_speed_button")
    assert window.receiver_status_label.text() == "CycleRace: 监听中，等待数据"
    assert window.capture_status_label.y() > window.camera_status_label.y()
    assert window.capture_status_label.width() >= window.capture_status_label.sizeHint().width()

    window.start_recording()
    assert window.recorder.is_running
    assert window.record_button.text() == "停止录像"
    assert window.camera_status_label.text() in {
        "录像设备: 正在检查",
        "录像设备: 已连接",
        "录像设备: 全部已连接",
    }
    window.close()


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
