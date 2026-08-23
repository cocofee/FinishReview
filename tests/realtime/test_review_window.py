import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import ClassVar

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtCore import QObject, Qt, pyqtSignal
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QComboBox, QLabel, QLineEdit, QPushButton

from realtime import passage_review
from realtime import review_window as review_window_module
from realtime.auyat_rgb import AuyatScanResult
from realtime.passage_receiver import PassageEvent, PassageEventStore, RaceFocus
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
    assert not dialog.start_button.isEnabled()
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
    for forbidden in ("RTSP", "0.0.0.0", "18765", "端口", "机位"):
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
    assert dialog.racetiger_base_url_edit.isEnabled()
    assert dialog.racetiger_rid_edit.text() == "RID-2026"
    assert dialog.settings.timing_provider == "racetiger"
    assert dialog.settings.racetiger_token == "local-test-token"
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
