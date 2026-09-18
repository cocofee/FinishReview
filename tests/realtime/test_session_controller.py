"""Session boundary failures using temporary races and the real Qt event loop."""
from dataclasses import replace
import threading
import time

import pytest
from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QApplication, QMessageBox

from realtime.event_ingestion import EventCommit
from realtime.passage_receiver import PassageEventStore
from realtime.race_metadata import RaceMetadata, RaceMetadataStore
from realtime.review_window import FinishReviewWindow
import realtime.review_window as module
from tests.realtime.test_review_window import _FakeReceiver, _FakeRecorder, _event, _wait_until


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


def window_at(path):
    return FinishReviewWindow("", path, passage_batch_interval_ms=0,
                              receiver_factory=_FakeReceiver, recorder_factory=_FakeRecorder)


def history_at(root):
    path = root / "history"
    RaceMetadataStore(path / "cyclerace_race_metadata.json").store(
        RaceMetadata(race_id="history", stage_id="s", revision=1, emitted_at_ms=1))
    return path


def test_slow_projection_keeps_input_and_timer_alive(qapp, tmp_path, monkeypatch):
    window = window_at(tmp_path)
    window.start_receiver()
    entered, release = threading.Event(), threading.Event()
    append = window.passage_store.append
    def slow_append(item):
        entered.set()
        assert release.wait(3)
        return append(item)
    monkeypatch.setattr(window.passage_store, "append", slow_append)
    try:
        window.receiver.deliver(_event())
        _wait_until(qapp, entered.is_set)
        assert window.isEnabled()
        assert window.table.rowCount() == 0
        ticks = []
        QTimer.singleShot(5, lambda: ticks.append(True))
        _wait_until(qapp, lambda: bool(ticks))
        assert not release.is_set()
        release.set()
        _wait_until(qapp, lambda: window.table.rowCount() == 1)
        current = window.passage_store.get(_event().event_id)
        window.receiver.deliver(replace(current, revision=2, is_active=False))
        _wait_until(qapp, lambda: window.table.rowCount() == 0)
    finally:
        release.set()
        window.close()


def test_slow_history_load_and_explicit_barrier_process_qt_events(qapp, tmp_path, monkeypatch):
    window = window_at(tmp_path)
    path = history_at(tmp_path)
    caller = threading.get_ident()
    threads, ticks = [], []
    original = module.prepare_session
    def prepare(*args, **kwargs):
        threads.append(threading.get_ident())
        time.sleep(.1)
        return original(*args, **kwargs)
    monkeypatch.setattr(module, "prepare_session", prepare)
    completed = []
    for index in range(3):
        window._capture_refresh_worker.submit_task(lambda index=index: (time.sleep(.03), completed.append(index)))
    timer = QTimer()
    timer.setInterval(5)
    timer.timeout.connect(lambda: ticks.append(window._session_busy))
    timer.start()
    try:
        assert window._open_saved_event_workspace(path)
        assert completed == [0, 1, 2]
        assert threads and all(value != caller for value in threads)
        assert sum(ticks) >= 5
        assert window._session_controller.context.snapshot.output_dir == path
    finally:
        timer.stop()
        window.close()


def test_failed_prepare_retains_context_and_applies_queued_withdrawal(qapp, tmp_path, monkeypatch):
    window = window_at(tmp_path)
    window.start_receiver()
    first = _event()
    window.receiver.deliver(first)
    _wait_until(qapp, lambda: window.table.rowCount() == 1)
    original_store = window.passage_store
    path = history_at(tmp_path)
    def fail(*_args, **_kwargs):
        time.sleep(.1)
        raise OSError("target unreadable")
    monkeypatch.setattr(module, "prepare_session", fail)
    monkeypatch.setattr(QMessageBox, "warning", lambda *_: None)
    QTimer.singleShot(20, lambda: window.receiver.deliver(replace(first, revision=2, is_active=False)))
    try:
        assert not window._open_saved_event_workspace(path)
        assert window.passage_store is original_store
        assert window.output_dir == tmp_path
        _wait_until(qapp, lambda: window.table.rowCount() == 0)
        assert window.passage_store.get(first.event_id).revision == 2
    finally:
        window.close()


def test_return_live_merges_withdrawal_received_during_prepare_and_rejects_old_commit(qapp, tmp_path, monkeypatch):
    window = window_at(tmp_path)
    window.start_receiver()
    metadata = RaceMetadata(race_id="race-1", stage_id="stage-1", revision=1, emitted_at_ms=1)
    window.receiver.deliver_metadata(metadata)
    live_dir = window.output_dir
    event = _event()
    window.receiver.deliver(event)
    _wait_until(qapp, lambda: window.table.rowCount() == 1)
    generation = window._session_controller.generation
    old_journal = str(window.passage_store.journal_path)
    assert window._open_saved_event_workspace(history_at(tmp_path))
    original = module.prepare_session
    def slow(*args, **kwargs):
        result = original(*args, **kwargs)
        time.sleep(.1)  # inject after the initial inbox watermark was merged
        return result
    monkeypatch.setattr(module, "prepare_session", slow)
    QTimer.singleShot(35, lambda: window.receiver.deliver(replace(event, revision=2, is_active=False)))
    try:
        assert window._return_to_live_event()
        _wait_until(qapp, lambda: window.passage_store.get(event.event_id).revision == 2)
        _wait_until(qapp, lambda: window.table.rowCount() == 0)
        window._on_event_committed(EventCommit(generation, old_journal, event, 0, 0))
        assert window.table.rowCount() == 0
        assert window.output_dir == live_dir
        window.close()
        restored = window_at(tmp_path)
        try:
            assert restored.passage_store.get(event.event_id).is_active is False
        finally:
            restored.close()
    finally:
        window.close()


def test_close_is_responsive_drains_projection_and_is_repeatable(qapp, tmp_path, monkeypatch):
    window = window_at(tmp_path)
    window.start_receiver()
    append = window.passage_store.append
    def slow(item):
        time.sleep(.1)
        return append(item)
    monkeypatch.setattr(window.passage_store, "append", slow)
    window.receiver.deliver(_event())
    ticked = []
    QTimer.singleShot(10, lambda: ticked.append(True))
    window.close()
    assert ticked
    assert window._ingestion.close().done()
    assert PassageEventStore(window.passage_store.journal_path).get(_event().event_id) is not None
    window.close()
    assert window.stop()


def test_transition_rejects_reentry_and_defers_close(qapp, tmp_path, monkeypatch):
    window = window_at(tmp_path)
    path = history_at(tmp_path)
    original = module.prepare_session
    def slow(*args, **kwargs):
        time.sleep(.1)
        return original(*args, **kwargs)
    monkeypatch.setattr(module, "prepare_session", slow)
    reentries = []
    QTimer.singleShot(15, lambda: reentries.append(window._open_saved_event_workspace(path)))
    QTimer.singleShot(30, window.close)
    assert window._open_saved_event_workspace(path)
    _wait_until(qapp, lambda: getattr(window, "_session_closed", False))
    assert reentries == [False]


def test_switch_stop_failure_keeps_running_process_and_old_services(qapp, tmp_path):
    class StubbornRecorder(_FakeRecorder):
        allow_stop = False
        def stop(self):
            if not self.allow_stop:
                raise RuntimeError("cannot stop process")
            return super().stop()
    window = FinishReviewWindow("rtsp://camera/live", tmp_path,
        receiver_factory=_FakeReceiver, recorder_factory=StubbornRecorder)
    window.start_recording()
    recorder = window.recorder
    context = window._session_controller.context
    try:
        # Avoid a modal warning but retain the real controller's stop contract.
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(QMessageBox, "warning", lambda *_: None)
            assert not window._apply_settings(
                window._current_settings(output_dir=tmp_path / "new"), stop_recording=True)
        assert window.recorder is recorder and recorder.is_running
        assert window._session_controller.context is context
        assert window.passage_store is context.passage_store
        assert window._ring_buffers and window._coordinators
    finally:
        recorder.allow_stop = True
        window.close()


def test_late_source_notification_cannot_write_new_workspace(qapp, tmp_path):
    window = window_at(tmp_path)
    old_source = window._receiver_passage_store
    event = _event()
    old_source.append(event)
    assert window._apply_settings(window._current_settings(output_dir=tmp_path / "new"))
    window._on_passage_received((old_source, event))
    assert window._ingestion.pending_count == 0
    assert window.passage_store.get(event.event_id) is None
    window.close()


def test_receiver_stop_failure_keeps_original_session_and_settings(qapp, tmp_path):
    class StubbornReceiver(_FakeReceiver):
        allow_stop = False

        def stop(self):
            if not self.allow_stop:
                raise RuntimeError("receiver still running")
            super().stop()

    saved = []
    window = FinishReviewWindow("", tmp_path, receiver_factory=StubbornReceiver,
                                settings_saver=saved.append)
    window.start_receiver()
    receiver = window.receiver
    context = window._session_controller.context
    try:
        assert not window._apply_settings(window._current_settings(output_dir=tmp_path / "new"))
        assert window.receiver is receiver and receiver.is_running
        assert window._session_controller.context is context
        assert saved[-1].output_dir == tmp_path
        assert window.output_dir == tmp_path
    finally:
        receiver.allow_stop = True
        window.close()


def test_history_open_recovers_tombstone_left_in_inbox(qapp, tmp_path):
    window = window_at(tmp_path)
    path = history_at(tmp_path)
    first = _event(race_id="history", stage_id="s")
    PassageEventStore(path / "cyclerace_passage_events.jsonl").append(first)
    window._receiver_passage_store.append(replace(first, revision=2, is_active=False))
    try:
        assert window._open_saved_event_workspace(path)
        assert not window.passage_store.get(first.event_id).is_active
        assert window.table.rowCount() == 0
    finally:
        window.close()


def test_failed_switch_keeps_commit_not_yet_delivered_to_gui(qapp, tmp_path, monkeypatch):
    window = window_at(tmp_path)
    event = _event()
    window._receiver_passage_store.append(event)
    window._on_passage_received(window._receiver_passage_store.get(event.event_id))
    # Let the disk worker finish without dispatching its queued Qt notification.
    deadline = time.monotonic() + 3
    while not window._ingestion.idle:
        assert time.monotonic() < deadline
        time.sleep(.005)
    assert window.passage_store.get(event.event_id) is not None
    assert window.table.rowCount() == 0
    path = history_at(tmp_path)

    def fail(*_args, **_kwargs):
        raise OSError("target unreadable")

    monkeypatch.setattr(module, "prepare_session", fail)
    monkeypatch.setattr(QMessageBox, "warning", lambda *_: None)
    try:
        assert not window._open_saved_event_workspace(path)
        _wait_until(qapp, lambda: window.table.rowCount() == 1)
        assert window.output_dir == tmp_path
    finally:
        window.close()
