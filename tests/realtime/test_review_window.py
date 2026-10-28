import csv
import os
import threading
import time
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import ClassVar

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
import realtime.launch_dialog as launch_dialog_module
from PyQt5.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt5.QtTest import QSignalSpy, QTest
from PyQt5.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
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
from realtime.recording_lock import FolderInUseError
from realtime.review_export import REVIEW_SUMMARY_FILENAME
from realtime.review_recorder import (
    DirectShowVideoDevice,
    PassageReviewState,
    PassageReviewWindow,
    ReviewSegment,
    make_directshow_source,
)
from realtime.video_passage_detector import (
    VideoPassageCandidate,
    VideoPassageReconciliation,
)
from realtime.review_window import (
    FinishReviewLaunchDialog,
    FinishReviewSettings,
    FinishReviewWindow,
)
from realtime.video_timeline import DEFAULT_CLOCK_SOURCE, VideoTimelineStore
from realtime.workspace_context import save_workspace_mode


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

    def request_stop(self):
        self.stop()

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


def _window(tmp_path, *, passage_batch_interval_ms=0, workbench=False):
    _FakeRecorder.instances.clear()
    _FakeReceiver.instances.clear()
    window = FinishReviewWindow(
        "rtsp://camera/live",
        tmp_path,
        passage_host="127.0.0.1",
        passage_port=18765,
        passage_batch_interval_ms=passage_batch_interval_ms,
        recorder_factory=_FakeRecorder,
        receiver_factory=_FakeReceiver,
    )
    # Legacy judging tests explicitly enter the auxiliary passage workflow.
    if not workbench:
        window.problem_list_button.click()
    return window


def _wait_until(qapp, predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        qapp.processEvents()
        QTest.qWait(5)
    qapp.processEvents()
    assert predicate()


def test_workbench_is_default_and_incoming_passages_do_not_take_over(qapp, tmp_path):
    import json
    from PyQt5.QtGui import QImage
    from realtime.problem_items import ProblemItem, KIND_NO_CHIP_CROSSING, CATEGORY_VIDEO
    window = _window(tmp_path, workbench=True)
    assert window._problem_workbench_active
    assert window.table.isHidden()
    assert window.review_content_splitter.widget(1) is window.preview_panel
    video = tmp_path / "original.mkv"
    video.write_bytes(b"video")
    segment = window.timeline_store.add_completed_segment(
        source_id="camera_01", camera_index=1, video_path=video,
        media_started_at_ms=1_789_600_000_000, media_duration_ms=6000,
        clock_source=DEFAULT_CLOCK_SOURCE, timing_error_ms=0, end_reason="test", race_id="race-1",
    )
    frame = dict(camera_index=1, segment_id=segment.segment_id, video_path=str(video),
                 position_ms=2000, frame_index=50)
    item = ProblemItem("test:1", KIND_NO_CHIP_CROSSING, CATEGORY_VIDEO, "test", evidence={"frame": frame})
    path = tmp_path / "finish_problems/problem_list.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"items": [item.to_payload()], "summary": {}}), encoding="utf-8")
    window.problem_panel.refresh()
    pane = window.regular_pane
    image = QImage(640, 360, QImage.Format_RGB888)
    image.fill(0)
    pane._worker.frame_ready.emit(image, 2000, 50)
    assert not window.problem_panel._browsing
    window.video_filmstrip.full_race._begin_browsing()
    assert window.problem_panel._browsing
    window._return_to_problem()
    pane._worker.frame_ready.emit(image, 2000, 50)
    assert not window.problem_panel._browsing
    window.problem_panel.bib_input.setText("307")
    window.passage_store.append(_event(event_id="new"))
    window.refresh()
    assert pane.location.segment.segment_id == segment.segment_id
    assert pane._current_frame_index == 50
    assert window.problem_panel.current_item().item_id == "test:1"
    assert window.problem_panel.bib_input.text() == "307"
    pane._worker.frame_ready.emit(image, 4000, 100)
    assert window.problem_panel._browsing
    window.problem_panel._resolve("record_bib")
    assert window.problem_panel.store.get("test:1") is None
    window._return_to_problem()
    pane._worker.frame_ready.emit(image, 2000, 50)
    assert not window.problem_panel._browsing
    assert not window._confirm_pending_marker(pane)
    window.close()


def test_workbench_keeps_picture_and_unified_table_visible_on_f(qapp, tmp_path):
    """The arrival table stays beside the picture without a second judging layout."""

    import json
    from realtime.problem_items import ProblemItem, KIND_NO_CHIP_CROSSING, CATEGORY_VIDEO
    window = _window(tmp_path, workbench=True)
    try:
        window.resize(1280, 720)
        window.show()
        video = tmp_path / "original.mkv"
        video.write_bytes(b"video")
        segment = window.timeline_store.add_completed_segment(
            source_id="camera_01", camera_index=1, video_path=video,
            media_started_at_ms=1_789_600_000_000, media_duration_ms=6000,
            clock_source=DEFAULT_CLOCK_SOURCE, timing_error_ms=0, end_reason="test", race_id="race-1",
        )
        frame = dict(camera_index=1, segment_id=segment.segment_id, video_path=str(video),
                     position_ms=2000, frame_index=50)
        item = ProblemItem("test:1", KIND_NO_CHIP_CROSSING, CATEGORY_VIDEO, "test", evidence={"frame": frame})
        path = tmp_path / "finish_problems/problem_list.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({"items": [item.to_payload()], "summary": {}}), encoding="utf-8")
        window.problem_panel.refresh()
        window.problem_panel.table.setCurrentCell(0, 0)
        qapp.processEvents()
        window.problem_panel.set_browsing(True)
        pane_height = window.regular_pane.video_view.height()
        window._toggle_maximized_pane(window.regular_pane)
        qapp.processEvents()
        assert window._maximized_window is None
        assert window.problem_panel._big_view
        assert not window.preview_panel.isVisibleTo(window)
        assert window.regular_pane.video_view.height() > pane_height
        assert window.problem_panel.quick_toolbar.isVisibleTo(window.problem_panel)
        assert window.problem_panel.table.isVisibleTo(window.problem_panel)
        assert not window.problem_panel.count_table.isVisibleTo(window.problem_panel)
        assert window.problem_panel.detail_scroll.isHidden()
        assert window.problem_panel.table.columnCount() == 5  # the group column hides with one group
        assert window.problem_panel._browsing
        window._return_to_problem()
        assert not window.problem_panel._browsing
        qapp.processEvents()

        window._toggle_maximized_pane(window.regular_pane)
        qapp.processEvents()
        assert not window.problem_panel._big_view
        assert window.problem_panel.table.isVisibleTo(window.problem_panel)
        assert not window.problem_panel.count_table.isVisibleTo(window.problem_panel)
        assert window.preview_panel.isVisibleTo(window)

        view = window.regular_pane.video_view
        view.setFocus()
        qapp.processEvents()
        QTest.keyClick(view.viewport(), Qt.Key_F)
        qapp.processEvents()
        assert not window.problem_panel.big_view  # F enlarges nothing in the workbench
        window.problem_panel.toggle_big_view()
        qapp.processEvents()
        assert window.problem_panel.big_view
        assert not window.preview_panel.isVisibleTo(window)
        QTest.keyClick(view.viewport(), Qt.Key_Escape)
        qapp.processEvents()
        assert not window.problem_panel.big_view
        assert window.preview_panel.isVisibleTo(window)

        window.regular_pane.maximize_btn.click()
        qapp.processEvents()
        assert window.problem_panel.big_view
        window.regular_pane.maximize_btn.click()
        qapp.processEvents()
        assert not window.problem_panel.big_view
    finally:
        window.close()


def test_picture_and_number_clicks_only_prepare_explicit_frame_confirmation(qapp, tmp_path):
    """Choose the athlete, locate the finish-line frame, then confirm explicitly."""

    import json
    from PyQt5.QtCore import QPoint
    from PyQt5.QtGui import QImage
    from realtime.problem_items import ProblemItem, KIND_NO_CHIP_CROSSING, CATEGORY_VIDEO
    window = _window(tmp_path, workbench=True)
    try:
        window.resize(1280, 720)
        window.show()
        video = tmp_path / "original.mkv"
        video.write_bytes(b"video")
        start = 1_789_600_000_000
        segment = window.timeline_store.add_completed_segment(
            source_id="camera_01", camera_index=1, video_path=video,
            media_started_at_ms=start, media_duration_ms=6000,
            clock_source=DEFAULT_CLOCK_SOURCE, timing_error_ms=0, end_reason="test", race_id="race-1",
        )
        frame = dict(camera_index=1, segment_id=segment.segment_id, video_path=str(video),
                     position_ms=2000, frame_index=50)
        item = ProblemItem("test:1", KIND_NO_CHIP_CROSSING, CATEGORY_VIDEO, "test", evidence={"frame": frame})
        path = tmp_path / "finish_problems/problem_list.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({"items": [item.to_payload()], "summary": {}}), encoding="utf-8")
        window.problem_panel.refresh()
        window.problem_panel.table.setCurrentCell(0, 0)
        window._open_problem_frame(frame)
        qapp.processEvents()

        pane = window.regular_pane
        view = pane.video_view
        pane._worker.frame_ready.emit(QImage(640, 480, QImage.Format_RGB32), 2000, 50)
        qapp.processEvents()
        window.problem_panel.toggle_big_view()  # F: the picture takes the clicks
        qapp.processEvents()
        assert pane.video_view._count_mode

        target = view.viewport().rect().center()
        QTest.mouseClick(view.viewport(), Qt.LeftButton, pos=target)
        qapp.processEvents()
        assert len(window.problem_panel.current_marks()) == 1
        assert window.problem_panel._category() is None
        assert not window.problem_panel.confirm_arrival_button.isEnabled()
        target_event = _event(event_id="read-321", bib="321", passage_timestamp_ms=start + 1900)
        window.passage_store.append(target_event)
        window.problem_panel.refresh()
        for _ in range(5):
            QTest.keyClick(view.viewport(), Qt.Key_1)
        assert not any(row.decision for row in window.problem_panel._arrival_rows)
        target_row = next(i for i, row in enumerate(window.problem_panel._arrival_rows) if row.bib == "321")
        pane._worker.frame_ready.emit(QImage(640, 480, QImage.Format_RGB32), 2050, 51)
        cell = window.problem_panel.table.item(target_row, 2)
        window.problem_panel.table.scrollToItem(cell)
        qapp.processEvents()
        QTest.mouseClick(window.problem_panel.table.viewport(), Qt.LeftButton,
                         pos=window.problem_panel.table.visualItemRect(cell).center())
        assert not any(row.decision for row in window.problem_panel._arrival_rows)
        assert window.problem_panel.confirm_arrival_button.isEnabled()
        assert pane._current_frame_index == 51
        # Only explicit confirmation saves the frame, after the operator steps forward.
        pane._worker.frame_ready.emit(QImage(640, 480, QImage.Format_RGB32), 2250, 56)
        window.problem_panel.confirm_arrival_button.click()
        confirmed = [row for row in window.problem_panel._arrival_rows if row.decision]
        assert len(confirmed) == 1 and confirmed[0].bib == "321"
        assert confirmed[0].decision["frame"]["frame_index"] == 56
        assert confirmed[0].decision["frame"]["recorder_ms"] == start + 2250
    finally:
        window.close()


def test_group_count_picture_drag_scrubs_active_pane_without_counting(qapp, tmp_path):
    """The main counting picture takes a horizontal drag; it never becomes a mark."""

    from PyQt5.QtCore import QEvent, QPoint, QPointF
    from PyQt5.QtGui import QImage, QMouseEvent
    from realtime.problem_items import ProblemItem, KIND_NO_CHIP_CROSSING, CATEGORY_VIDEO
    import json

    window = _window(tmp_path, workbench=True)
    try:
        window.resize(1280, 720)
        window.show()
        video = tmp_path / "camera_01_test_archive_000.mkv"
        video.write_bytes(b"video")
        start = 1_789_600_000_000
        segment = window.timeline_store.add_completed_segment(
            source_id="camera_01", camera_index=1, video_path=video,
            media_started_at_ms=start, media_duration_ms=6000,
            clock_source=DEFAULT_CLOCK_SOURCE, timing_error_ms=0, end_reason="test", race_id="race-1",
        )
        frame = dict(camera_index=1, segment_id=segment.segment_id, video_path=str(video),
                     position_ms=2000, frame_index=50)
        item = ProblemItem("test:1", KIND_NO_CHIP_CROSSING, CATEGORY_VIDEO, "test",
                           evidence={"frame": frame})
        path = tmp_path / "finish_problems/problem_list.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({"items": [item.to_payload()], "summary": {}}), encoding="utf-8")
        window.problem_panel.refresh()
        window.problem_panel.table.setCurrentCell(0, 0)
        window._open_problem_frame(frame)
        qapp.processEvents()

        pane = window.regular_pane
        view = pane.video_view
        pane._worker.frame_ready.emit(QImage(640, 480, QImage.Format_RGB32), 2000, 50)
        qapp.processEvents()
        window.problem_panel.toggle_big_view()
        qapp.processEvents()
        assert view._count_mode

        worker = pane._worker
        seeks = []
        worker.seek = seeks.append
        worker.seek_preview = seeks.append
        marks_before = list(window.problem_panel.current_marks())

        center = view.viewport().rect().center()
        move = center + QPoint(90, 0)
        for event_type, position, pressed, buttons in (
            (QEvent.MouseButtonPress, center, Qt.LeftButton, Qt.LeftButton),
            (QEvent.MouseMove, move, Qt.NoButton, Qt.LeftButton),
        ):
            QApplication.sendEvent(
                view.viewport(),
                QMouseEvent(event_type, QPointF(position), pressed, buttons, Qt.NoModifier),
            )
        QTest.qWait(120)  # let the pane's 80 ms preview timer fire once
        assert seeks
        QApplication.sendEvent(
            view.viewport(),
            QMouseEvent(QEvent.MouseButtonRelease, QPointF(move),
                        Qt.LeftButton, Qt.NoButton, Qt.NoModifier),
        )
        QTest.qWait(10)

        assert window.problem_panel.current_marks() == marks_before
        assert seeks[-1] != 2000  # The release committed the frame-relative scrub.
        assert pane._video_scrubbing is False
    finally:
        window.close()



    from PyQt5.QtGui import QImage
    window = _window(tmp_path, workbench=True)
    try:
        window.show()
        start = 1_789_600_000_000
        video = tmp_path / "camera_01_test_archive_000.mkv"
        video.write_bytes(b"video")
        segment = window.timeline_store.add_completed_segment(
            source_id="camera_01", camera_index=1, video_path=video,
            media_started_at_ms=start, media_duration_ms=6000,
            clock_source=DEFAULT_CLOCK_SOURCE, timing_error_ms=0, end_reason="test", race_id="race-1")
        target = _event(event_id="raw-321", bib="321", passage_timestamp_ms=start + 2000)
        window.passage_store.append(target)
        panel = window.problem_panel
        panel.refresh()
        row_index = next(i for i, row in enumerate(panel._arrival_rows) if row.bib == "321")
        panel._arrival_clicked(row_index)
        window._open_problem_frame(dict(camera_index=1, segment_id=segment.segment_id,
                                       video_path=str(video), position_ms=2000, frame_index=50))
        pane = window.regular_pane
        pane._worker.frame_ready.emit(QImage(640, 480, QImage.Format_RGB32), 2000, 50)
        assert "1" in panel.confirmation_status.text()
        QTest.keyClick(pane.video_view.viewport(), Qt.Key_1)
        assert panel._selected_arrival().bib == "321"
        assert panel.current_item().item_id.startswith("manual:")
        assert not panel.big_view
        assert pane._current_frame_index == 50
        saved = next(row.decision for row in panel._arrival_rows if row.bib == "321")
        assert saved["frame"]["frame_index"] == 50
        assert saved["frame"]["segment_id"] == segment.segment_id
        assert not panel.current_marks()
        assert "marker_x_normalized" not in saved["frame"]
        QTest.keyClick(pane.video_view.viewport(), Qt.Key_Backspace)
        assert not any(row.decision for row in panel._arrival_rows)
        assert panel._selected_arrival().bib == "321"
        QTest.keyClick(pane.video_view.viewport(), Qt.Key_1)
        assert sum(row.decision is not None for row in panel._arrival_rows) == 1
    finally:
        window.close()


@pytest.mark.parametrize("save_action", ["key", "button"])
def test_quick_review_picture_click_marks_bicycle_and_keeps_the_chip_row(qapp, tmp_path, save_action):
    from PyQt5.QtGui import QImage

    window = _window(tmp_path, workbench=True)
    try:
        window.show()
        start = 1_789_600_000_000
        video = tmp_path / "camera_01_test_archive_000.mkv"
        video.write_bytes(b"video")
        segment = window.timeline_store.add_completed_segment(
            source_id="camera_01", camera_index=1, video_path=video,
            media_started_at_ms=start, media_duration_ms=6000,
            clock_source=DEFAULT_CLOCK_SOURCE, timing_error_ms=0, end_reason="test", race_id="race-1")
        target = _event(event_id="click-321", bib="21", passage_timestamp_ms=start + 2000)
        window.passage_store.append(target)
        panel = window.problem_panel
        panel.refresh()
        row_index = next(i for i, row in enumerate(panel._arrival_rows) if row.bib == "21")
        panel._arrival_clicked(row_index)
        frame = dict(camera_index=1, segment_id=segment.segment_id, video_path=str(video),
                     position_ms=2000, frame_index=50)
        window._open_problem_frame(frame)
        pane = window.regular_pane
        pane._worker.frame_ready.emit(QImage(640, 480, QImage.Format_RGB32), 2000, 50)
        qapp.processEvents()

        assert pane.video_view._count_mode
        pane._identity = "待补录"
        pane._pending_marker = (.55, .62, 50, 2000)
        pane._on_detail_requested()
        pane._worker.full_resolution_ready.emit(QImage(640, 480, QImage.Format_RGB32), 2000, 50)
        assert pane.video_view._marker is None
        worker = pane._worker
        window._problem_count_clicked(0.55, 0.62, pane)
        qapp.processEvents()
        assert pane._worker is worker
        assert pane._current_frame_index == 50

        assert panel._selected_arrival().bib == "21"
        assert panel._selected_key == "event:click-321"
        assert panel.current_item().evidence["frame"]["marker_x_normalized"] == pytest.approx(0.55)
        assert panel.current_item().evidence["frame"]["marker_y_normalized"] == pytest.approx(0.62)
        # The click on the rider is the save: no 1 needed.
        assert panel._category()["action"] == "bicycle"
        saved = next(row.decision for row in panel._arrival_rows if row.bib == "21")
        assert saved["frame"]["frame_index"] == 50
        assert saved["frame"]["marker_x_normalized"] == pytest.approx(0.55)
        assert saved["frame"]["marker_y_normalized"] == pytest.approx(0.62)
        overlays = window._problem_count_overlays(pane)
        assert len(overlays) == 1
        assert overlays[0][0] == pytest.approx(0.55)
        assert overlays[0][1] == pytest.approx(0.62)
        assert overlays[0][2] == "21"
        assert overlays[0][5] is True and overlays[0][6] is False
        assert pane.video_view._history_markers[0][2] == "21"

        assert panel.confirm_arrival_button.isHidden()
        if save_action == "key":
            QTest.keyClick(pane.video_view.viewport(), Qt.Key_1)  # out of habit: harmless
        else:
            panel.bicycle_button.click()
        assert "不用按 1" in panel.hint.text()
        assert panel._selected_arrival().bib == "21"
        assert sum(row.decision is not None for row in panel._arrival_rows) == 1
        target_row = next(i for i, row in enumerate(panel._arrival_rows) if row.bib == "21")
        assert panel.table.item(target_row, 3).text() == "已确认"
        assert panel.table.item(target_row, 3).foreground().color().name() == "#126044"
        panel.refresh()
        panel._arrival_clicked(next(i for i, row in enumerate(panel._arrival_rows) if row.bib == "21"))
        pane._worker.frame_ready.emit(QImage(640, 480, QImage.Format_RGB32), 2000, 50)
        assert pane.video_view._history_markers[0][2] == "21"
        QTest.keyClick(pane.video_view.viewport(), Qt.Key_Backspace)
        assert not any(row.decision for row in panel._arrival_rows)
        assert panel._selected_arrival().bib == "21"
        assert not panel.current_marks()
        assert pane._worker is worker
        assert pane._current_frame_index == 50
        assert panel.table.item(panel.table.currentRow(), 3).text() == "待定"
        assert panel.table.item(panel.table.currentRow(), 3).foreground().color().name() == "#b42318"
        window._problem_count_clicked(0.55, 0.62, pane)
        assert sum(row.decision is not None for row in panel._arrival_rows) == 1
        # 22 and 23 arrive within 2 s of 21: one wave, counted on this same picture,
        # each circle labelled in chip order and saved at once. Re-clicking a circle adds no duplicate.
        for bib in ("22", "23"):
            window.passage_store.append(_event(event_id="click-" + bib, bib=bib,
                                               passage_timestamp_ms=start + 2000 + int(bib)))
        panel.refresh()
        panel._arrival_clicked(next(i for i, row in enumerate(panel._arrival_rows) if row.bib == "22"))
        candidate_id = panel.current_item().item_id
        jumps = QSignalSpy(panel.jump_requested)
        window._problem_count_clicked(.7, .62, pane)
        window._problem_count_clicked(.8, .62, pane)
        # 古浪 261：点第 5 个人时画面不能被拉回已保存的 69 那一帧。
        assert len(jumps) == 0
        assert len(panel.current_marks()) == 3
        assert panel.current_item().item_id == candidate_id
        assert panel._selected_arrival().bib == "23"  # the one just clicked, on the left
        window._problem_count_clicked(.7, .62, pane)
        assert len(panel.current_marks()) == 3
        assert panel._selected_arrival().bib == "22"  # a click on a counted ring picks them out
        assert len(jumps) == 0
        assert [overlay[2] for overlay in window._problem_count_overlays(pane)] == ["21", "22", "23"]
        assert sum(row.decision is not None for row in panel._arrival_rows) == 3
        window._problem_count_cancelled(.7, .62, pane)  # right click: 22 is taken back, 23 moves up
        assert [overlay[2] for overlay in window._problem_count_overlays(pane)] == ["21", "22"]
        assert sum(row.decision is not None for row in panel._arrival_rows) == 2
    finally:
        window.close()


def test_real_finish_line_follows_session_time_and_clears_identity_guide(qapp, tmp_path):
    import json
    from PyQt5.QtGui import QImage
    window = _window(tmp_path, workbench=True)
    try:
        window.show()
        start = 1_789_600_000_000
        video = tmp_path / "camera_01_test_archive_000.mkv"
        video.write_bytes(b"video")
        segment = window.timeline_store.add_completed_segment(
            source_id="camera_01", camera_index=1, video_path=video,
            media_started_at_ms=start, media_duration_ms=6000,
            clock_source=DEFAULT_CLOCK_SOURCE, timing_error_ms=0, end_reason="test", race_id="race-1")
        window.passage_store.append(_event(passage_timestamp_ms=start + 2000))
        window.problem_panel.refresh()
        line = dict(camera_index=1, race_id="race-1", stage_id="stage-1", session_id="camera_01_test",
                    effective_from_ms=start, revision=1, points=[[.2, .1], [.5, .6], [.8, .9]])
        config = tmp_path / "finish_problems/analysis_config.json"
        config.parent.mkdir(exist_ok=True)
        config.write_text(json.dumps({"sampling_profiles": [line,
            dict(line, revision=2, effective_from_ms=start + 3000, points=[[.7, .1], [.7, .9]])]}), encoding="utf-8")
        window._open_problem_frame(dict(camera_index=1, segment_id=segment.segment_id,
                                       video_path=str(video), position_ms=2000, frame_index=50))
        pane = window.regular_pane
        pane._worker.frame_ready.emit(QImage(640, 480, QImage.Format_RGB32), 2000, 50)
        assert pane.video_view._finish_line_points == ((.2, .1), (.5, .6), (.8, .9))
        assert pane.video_view._marker is None
        window.problem_panel.toggle_big_view()
        assert pane.video_view._finish_line_points == ((.2, .1), (.5, .6), (.8, .9))
        pane._worker.frame_ready.emit(QImage(640, 480, QImage.Format_RGB32), 3500, 88)
        assert pane.video_view._finish_line_points == ((.7, .1), (.7, .9))
        # A session without its own line shows the one it inherits, like the analysis.
        config.write_text(json.dumps({"sampling_profiles": [dict(line, session_id="another-session")]}), encoding="utf-8")
        window._refresh_problem_finish_lines()
        assert pane.video_view._finish_line_points == ((.2, .1), (.5, .6), (.8, .9))
        # Another race's line never does.
        config.write_text(json.dumps({"sampling_profiles": [
            dict(line, session_id="another-session", race_id="race-other")]}), encoding="utf-8")
        window._refresh_problem_finish_lines()
        assert not pane.video_view._finish_line_points
        assert "未设置" in window.problem_panel.finish_line_status.text()
    finally:
        window.close()


@pytest.mark.parametrize("workbench", [True, False])
def test_each_f_entry_reloads_saved_line_even_when_no_new_frame(qapp, tmp_path, workbench):
    import json
    from PyQt5.QtGui import QImage

    window = _window(tmp_path, workbench=workbench)
    try:
        window.show()
        video = tmp_path / "camera_01_test_archive_000.mkv"
        video.write_bytes(b"video")
        segment = window.timeline_store.add_completed_segment(
            source_id="camera_01", camera_index=1, video_path=video,
            media_started_at_ms=1_789_600_000_000, media_duration_ms=6000,
            clock_source=DEFAULT_CLOCK_SOURCE, timing_error_ms=0, end_reason="test", race_id="race-1")
        window._open_problem_frame(dict(camera_index=1, segment_id=segment.segment_id,
                                       video_path=str(video), position_ms=2000, frame_index=50))
        pane = window.regular_pane
        pane._worker.frame_ready.emit(QImage(640, 480, QImage.Format_RGB32), 2000, 50)
        qapp.processEvents()
        config = tmp_path / "finish_lines.json"
        for x in (.3, .6, .8):
            # Save while the same paused frame remains on screen; opening F must
            # not depend on another decoder callback to pick up the new line.
            config.write_text(json.dumps({"finish_lines": [dict(camera_index=1, x1=x, y1=.1,
                                                               x2=x, y2=.9)]}), encoding="utf-8")
            window._toggle_maximized_pane(pane)
            assert pane.video_view._finish_line_points == ((x, .1), (x, .9))
            assert pane._current_frame_index == 50
            pane.video_view.zoom_by(1.2)
            pane.video_view.fit_to_window()
            assert pane.video_view._finish_line_points == ((x, .1), (x, .9))
            window._toggle_maximized_pane(pane)
        config.unlink()
        window._toggle_maximized_pane(pane)
        assert not pane.video_view._finish_line_points
        window._toggle_maximized_pane(pane)
        qapp.processEvents()
    finally:
        window.close()


def test_picture_numbers_follow_crossing_time_after_backtracking(qapp, tmp_path):
    import json

    from PyQt5.QtGui import QImage
    from realtime.problem_items import CATEGORY_VIDEO, KIND_NO_CHIP_CROSSING, ProblemItem

    window = _window(tmp_path, workbench=True)
    try:
        window.resize(1280, 720)
        window.show()
        video = tmp_path / "original.mkv"
        video.write_bytes(b"video")
        start = 1_789_600_000_000
        segment = window.timeline_store.add_completed_segment(
            source_id="camera_01", camera_index=1, video_path=video,
            media_started_at_ms=start, media_duration_ms=6000,
            clock_source=DEFAULT_CLOCK_SOURCE, timing_error_ms=0, end_reason="test", race_id="race-1",
        )
        early = dict(camera_index=1, segment_id=segment.segment_id, video_path=str(video),
                     position_ms=2000, frame_index=50)
        late = dict(early, position_ms=3000, frame_index=75)
        item = ProblemItem("test:1", KIND_NO_CHIP_CROSSING, CATEGORY_VIDEO, "test", evidence={"frame": early})
        path = tmp_path / "finish_problems/problem_list.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({"items": [item.to_payload()], "summary": {}}), encoding="utf-8")
        panel = window.problem_panel
        panel.refresh()
        window._open_problem_frame(early)
        pane = window.regular_pane
        pane._worker.frame_ready.emit(QImage(640, 480, QImage.Format_RGB32), 2000, 50)
        qapp.processEvents()
        panel.toggle_big_view()
        panel.add_mark(.3, .5, start + 3000, late)
        panel.add_mark(.4, .5, start + 2000, early)
        panel.add_mark(.6, .5, start + 2000, early)

        assert [panel.count_table.item(row, 0).text() for row in range(3)] == ["1 同时", "1 同时", "3"]
        overlays = window._problem_count_overlays(pane)
        assert [(overlay[0], overlay[2]) for overlay in overlays] == [(.4, "待选号码"), (.6, "待选号码")]
        pane._worker.frame_ready.emit(QImage(640, 480, QImage.Format_RGB32), 3000, 75)
        assert [overlay[2] for overlay in window._problem_count_overlays(pane)] == ["待选号码"]
        pane._worker.frame_ready.emit(QImage(640, 480, QImage.Format_RGB32), 2050, 51)
        assert all(overlay[6] for overlay in window._problem_count_overlays(pane))  # faded, a frame later

        panel._exclude_mark(1, "工作人员")
        pane._worker.frame_ready.emit(QImage(640, 480, QImage.Format_RGB32), 2000, 50)
        assert [overlay[2] for overlay in window._problem_count_overlays(pane)] == ["非运动员 已排除：工作人员", "待选号码"]
        assert [panel.count_table.item(row, 0).text() for row in range(3)] == ["排除", "1", "2"]
        assert [mark["recorder_ms"] for mark in panel.current_marks()] == [start + 3000, start + 2000, start + 2000]
    finally:
        window.close()


def test_a_normal_single_is_one_row_and_a_click_on_the_rider_confirms_it(qapp, tmp_path):
    """正常单人只留一行过线记录；古浪 60 号：点到人就算数，自动对应的单人也一样。"""

    import json
    from PyQt5.QtGui import QImage

    from realtime.problem_items import KIND_AUTO_PASSED

    window = _window(tmp_path, workbench=True)
    try:
        window.resize(1280, 720)
        window.show()
        video = tmp_path / "original.mkv"
        video.write_bytes(b"video")
        start = 1_789_600_000_000
        segment = window.timeline_store.add_completed_segment(
            source_id="camera_01", camera_index=1, video_path=video,
            media_started_at_ms=start, media_duration_ms=6000,
            clock_source=DEFAULT_CLOCK_SOURCE, timing_error_ms=0, end_reason="test", race_id="race-1",
        )
        frame = dict(camera_index=1, segment_id=segment.segment_id, video_path=str(video),
                     position_ms=2000, frame_index=50)
        passed = {"bib": "057", "event_id": "e-057", "chip_time_ms": start + 1900, "active_now": True,
                  "recorder_ms": start + 2000, "source": "auto", "frame": frame}
        window.passage_store.append(_event(event_id="e-057", bib="057", passage_timestamp_ms=start + 1900))
        path = tmp_path / "finish_problems/problem_list.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({"items": [], "summary": {"auto_passed": 1}, "auto_passed": [passed]}),
                        encoding="utf-8")
        window.problem_panel.refresh()
        window._refresh_crossing_cards()
        qapp.processEvents()

        # Normal and exceptional arrivals share the same permanent table.
        auto = [item for item in window.problem_panel.rows if item.kind == KIND_AUTO_PASSED]
        assert len(auto) == 1
        qapp.processEvents()

        # 点开这一行就打开那条过线的原帧；点中人就保存，不用再按 1。
        assert window.problem_panel._selected_arrival().bib == "057"
        pane = window.regular_pane
        pane._worker.frame_ready.emit(QImage(640, 480, QImage.Format_RGB32), 2000, 50)
        qapp.processEvents()
        assert not window._opened_normal_crossing
        window._problem_count_clicked(0.5, 0.5, pane)
        saved = next(row.decision for row in window.problem_panel._arrival_rows if row.bib == "057")
        assert saved["frame"]["frame_index"] == 50
        assert saved["frame"]["marker_x_normalized"] == pytest.approx(0.5)
        assert "057 已保存过线" in window.problem_panel.hint.text()
    finally:
        window.close()


def test_the_first_rider_calibration_keeps_the_original_chip_record(qapp, tmp_path, monkeypatch):
    """Calibrating from the current frame stores the exact record it was made on."""

    from datetime import datetime
    from PyQt5.QtGui import QImage
    from PyQt5.QtWidgets import QInputDialog
    from realtime.passage_evidence import VideoClockCalibrationStore
    from realtime.video_timeline import PassageVideoLocation

    window = _window(tmp_path, workbench=True)
    try:
        video = tmp_path / "original.mkv"
        video.write_bytes(b"video")
        start = 1_789_600_000_000
        segment = window.timeline_store.add_completed_segment(
            source_id="camera_01", camera_index=1, video_path=video,
            media_started_at_ms=start, media_duration_ms=6000,
            clock_source=DEFAULT_CLOCK_SOURCE, timing_error_ms=0, end_reason="test", race_id="race-1",
        )
        event = _event(event_id="race-1-stage-1-passage-15", bib="15",
                       passage_timestamp_ms=start + 1900, received_at_ms=start + 2000)
        window.passage_store.append(event)
        window.problem_panel._live_paused = True  # calibrating must not spawn the analysis worker

        pane = window.regular_pane
        location = PassageVideoLocation(segment, video, 2000, 2000, 0, 0, "located")
        assert window._seek_retained_camera_frame(location, 2000, 50, 1)
        pane._worker.frame_ready.emit(QImage(640, 480, QImage.Format_RGB32), 2000, 50)
        stored = window.passage_store.get(event.event_id)
        label = (f"{stored.bib} · "
                 f"{datetime.fromtimestamp(stored.timeline_timestamp_ms / 1000).strftime('%H:%M:%S.%f')[:-3]}"
                 f" · {stored.event_id}")
        monkeypatch.setattr(QInputDialog, "getItem", lambda *args, **kwargs: (label, True))
        window._calibrate_crossing_frame()

        journal = VideoClockCalibrationStore(tmp_path / "video_clock_calibrations.jsonl",
                                             recover_incomplete_tail=False)
        calibration = journal.calibrations()[-1]
        assert calibration.anchor_event_id == stored.event_id
        assert calibration.anchor_event_revision == stored.revision
        assert calibration.anchor_chip_time_ms == stored.timeline_timestamp_ms
        assert calibration.anchor_race_id == "race-1" and calibration.anchor_stage_id == "stage-1"
        assert calibration.anchor_frame["frame_index"] == 50
        assert calibration.offset_ms == 2000 + start - stored.timeline_timestamp_ms
        # The official chip time is untouched: only the video offset was learned.
        assert window.passage_store.get(event.event_id).timeline_timestamp_ms == event.timeline_timestamp_ms
    finally:
        window.close()


@pytest.mark.parametrize("days", [0, 1])
@pytest.mark.parametrize("offset", [0, 2714, -1500])
def test_chip_click_opens_raw_reference_before_model_line_or_calibration(qapp, tmp_path, days, offset):
    from PyQt5.QtGui import QImage

    window = _window(tmp_path, workbench=True)
    try:
        start = 1_789_600_000_000
        video = tmp_path / "camera_01_test_archive_000.mkv"
        video.write_bytes(b"video")
        segment = window.timeline_store.add_completed_segment(
            source_id="camera_01", camera_index=1, video_path=video,
            media_started_at_ms=start, media_duration_ms=6000,
            clock_source=DEFAULT_CLOCK_SOURCE, timing_error_ms=0, end_reason="test", race_id="race-1")
        event = _event(passage_timestamp_ms=start + 2000 + days * 86_400_000,
                       received_at_ms=start + 2200)
        window.passage_store.append(event)
        if offset:
            key = window._recording_session_key_from_path(segment.source_id, video)
            window._continuous_clock_offsets[(1, key)] = offset
        panel = window.problem_panel
        panel.refresh()
        index = next(i for i, row in enumerate(panel._arrival_rows) if row.bib == event.bib)
        panel._arrival_clicked(index)
        pane = window.regular_pane
        assert pane.location.segment.segment_id == segment.segment_id
        assert pane.location.video_path == video
        assert pane.location.playback_position_ms == 2000 + offset
        pane._worker.frame_ready.emit(QImage(640, 480, QImage.Format_RGB32), 2000, 50)
        assert "参考录像" in panel.hint.text() and "不代表已确认过线" in panel.hint.text()
        assert pane._reference_only is True
        assert not panel.confirm_arrival_button.isEnabled()
        assert not any(row.decision for row in panel._arrival_rows)
        assert window.passage_store.get(event.event_id).timeline_timestamp_ms == event.timeline_timestamp_ms
        assert not (tmp_path / "video_clock_calibrations.jsonl").exists()
        if days:
            assert "正式时间未改" in panel.hint.text()
        # Reference playback remains usable for the explicit manual workflow.
        panel._confirm_bicycle()
        assert panel.current_item().evidence.get("frame")
        assert sum(row.decision is not None for row in panel._arrival_rows) == 1
    finally:
        window.close()


@pytest.mark.parametrize("mismatch", ["race", "camera", "time", "missing"])
def test_chip_reference_does_not_borrow_unrelated_or_missing_video(qapp, tmp_path, mismatch):
    window = _window(tmp_path, workbench=True)
    try:
        start = 1_789_600_000_000
        video = tmp_path / "original.mkv"
        if mismatch != "missing":
            video.write_bytes(b"video")
        window.timeline_store.add_completed_segment(
            source_id="camera_01", camera_index=2 if mismatch == "camera" else 1, video_path=video,
            media_started_at_ms=start, media_duration_ms=6000,
            clock_source=DEFAULT_CLOCK_SOURCE, timing_error_ms=0, end_reason="test",
            race_id="other-race" if mismatch == "race" else "race-1")
        event = _event(passage_timestamp_ms=start + (60_000 if mismatch == "time" else 2000),
                       received_at_ms=start + (60_200 if mismatch == "time" else 2200))
        window.passage_store.append(event)
        window.problem_panel.refresh()
        index = next(i for i, row in enumerate(window.problem_panel._arrival_rows) if row.bib == event.bib)
        window.problem_panel._arrival_clicked(index)
        assert window.regular_pane.location is None
        assert not window.problem_panel.confirm_arrival_button.isEnabled()
    finally:
        window.close()


def test_hls_windows_share_first_rider_calibration_including_legacy_key(qapp, tmp_path):
    from realtime.analysis_workspace import session_id_for_path
    window = _window(tmp_path, workbench=True)
    try:
        folder = tmp_path / "review_buffer" / "camera_01"
        folder.mkdir(parents=True)
        session = "camera_01_20261018_104528_433517"
        first = folder / "evidence_shared_first.m3u8"
        second = folder / "evidence_shared_second.m3u8"
        for index, path in enumerate((first, second)):
            path.write_text(f"#EXTM3U\n{session}_review_{index:016d}.ts\n#EXT-X-ENDLIST\n", encoding="utf-8")
        key = window._recording_session_key_from_path("camera_01_review", second)
        assert key == window._recording_session_key_from_path("camera_01_review", first)
        assert key == window._recording_session_key_from_path("camera_01_review", tmp_path / "videos" / (session + "_archive_0000.mkv"))
        assert session_id_for_path(first) == session_id_for_path(second) == session
        window.calibration_store.record(
            camera_index=1, session_key=f"camera_01_review|{folder}|{first.stem}",
            offset_ms=2525, anchor_event_id="first", anchor_bib="901", calibrated_at_ms=1000)
        assert window._calibrated_clock_offset(1, key) == 2525
        window._continuous_clock_offsets[(1, key)] = 2714
        assert window._calibrated_clock_offset(1, key) == 2714
    finally:
        window.close()


def test_workbench_keys_from_other_widgets_keep_the_unified_layout(qapp, tmp_path):
    """The view does not split into a second counting screen when focus changes."""

    import json
    from realtime.problem_items import ProblemItem, KIND_NO_CHIP_CROSSING, CATEGORY_VIDEO
    window = _window(tmp_path, workbench=True)
    try:
        window.resize(1280, 720)
        window.show()
        video = tmp_path / "original.mkv"
        video.write_bytes(b"video")
        start = 1_789_600_000_000
        segment = window.timeline_store.add_completed_segment(
            source_id="camera_01", camera_index=1, video_path=video,
            media_started_at_ms=start, media_duration_ms=6000,
            clock_source=DEFAULT_CLOCK_SOURCE, timing_error_ms=0, end_reason="test", race_id="race-1",
        )
        frame = dict(camera_index=1, segment_id=segment.segment_id, video_path=str(video),
                     position_ms=2000, frame_index=50)
        item = ProblemItem("test:1", KIND_NO_CHIP_CROSSING, CATEGORY_VIDEO, "test", evidence={"frame": frame})
        path = tmp_path / "finish_problems/problem_list.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({"items": [item.to_payload()], "summary": {}}), encoding="utf-8")
        window.problem_panel.refresh()
        qapp.processEvents()
        panel = window.problem_panel
        panel.table.setCurrentCell(0, 0)

        # The time rail is where the keyboard sits when the app opens, and it used
        # to keep F for its own judging window - the press then went nowhere.
        rail = window.video_filmstrip.full_race
        assert not rail._maximize_shortcut.isEnabled()
        rail.canvas.setFocus()
        qapp.processEvents()
        QTest.keyClick(rail.canvas.viewport(), Qt.Key_F)
        qapp.processEvents()
        assert not panel.big_view
        panel.toggle_big_view()
        qapp.processEvents()
        assert panel.big_view
        assert panel.table.isVisibleTo(panel) and panel.detail_scroll.isHidden()
        assert not window.preview_panel.isVisibleTo(window)
        assert panel.quick_toolbar.isVisibleTo(panel)
        panel.toggle_big_view()
        qapp.processEvents()

        window.recheck_button.setFocus()
        qapp.processEvents()
        QTest.keyClick(window.recheck_button, Qt.Key_F)
        qapp.processEvents()
        assert not panel.big_view

        # A field that takes typing keeps the letter: the bib is typed, not enlarged.
        panel.bib_input.setText("")
        panel.bib_input.setFocus()
        qapp.processEvents()
        QTest.keyClick(panel.bib_input, Qt.Key_F)
        qapp.processEvents()
        assert panel.bib_input.text() == "f"
        assert not panel.big_view
    finally:
        window.close()


def test_the_problem_frame_counts_by_clock_across_the_archive(qapp, tmp_path):
    """Judging a rebound two-minute archive is still judging this crossing."""

    from types import SimpleNamespace

    window = _window(tmp_path, workbench=True)
    try:
        clip = window.timeline_store.add_completed_segment(
            source_id="camera_01", camera_index=1, video_path=tmp_path / "clip.mkv",
            media_started_at_ms=1_789_600_000_000, media_duration_ms=6000,
            clock_source=DEFAULT_CLOCK_SOURCE, timing_error_ms=0, end_reason="test", race_id="race-1",
        )
        archive = window.timeline_store.add_completed_segment(
            source_id="camera_01", camera_index=1, video_path=tmp_path / "archive.mkv",
            media_started_at_ms=1_789_600_000_000 - 60_000, media_duration_ms=120_000,
            clock_source=DEFAULT_CLOCK_SOURCE, timing_error_ms=0, end_reason="test", race_id="race-1",
        )
        frame = dict(camera_index=1, segment_id=clip.segment_id, video_path=str(tmp_path / "clip.mkv"),
                     position_ms=2000, frame_index=50)
        pane = SimpleNamespace(location=SimpleNamespace(segment=archive))
        # 62 s into the archive is the clip's own second, another session's file.
        assert window._shows_the_problem_frame(pane, 62_000, frame)
        assert not window._shows_the_problem_frame(pane, 65_000, frame)
        pane.location = SimpleNamespace(segment=clip)
        assert window._shows_the_problem_frame(pane, 2_500, frame)
        assert not window._shows_the_problem_frame(pane, 8_000, frame)
        assert not window._shows_the_problem_frame(SimpleNamespace(location=None), 2_000, frame)
    finally:
        window.close()


def test_workbench_primary_actions_and_filmstrip_fit_laptop(qapp, tmp_path):
    window = _window(tmp_path, workbench=True)
    window.resize(1280, 720)
    window.show()
    qapp.processEvents()
    assert window.height() <= 720
    assert window.video_filmstrip.height() >= 180
    assert not window.evidence_splitter.isHidden()
    assert not window.video_filmstrip.full_race.follow_button.isHidden()
    window.video_filmstrip.full_race.follow_button.setChecked(True)
    window.video_filmstrip.full_race.follow_button.setChecked(False)
    assert not window.video_filmstrip.full_race.follow_button.isHidden()
    window.close()


def _legacy_mixed_workspace(path):
    passages = PassageEventStore(path / "cyclerace_passage_events.jsonl")
    event = _event(event_id="saved-rider", race_name="历史比赛")
    passages.append(event)
    passages.append(_event(event_id="foreign-rider", race_id="foreign-race"))
    passages.append(_event(event_id="other-stage", stage_id="stage-2"))
    video = path / "videos" / "camera_01_session_archive_0000.mkv"
    video.parent.mkdir()
    video.write_bytes(b"saved-video")
    timeline = VideoTimelineStore(path / "video_timeline.jsonl")
    segment = timeline.add_completed_segment(
        source_id="camera_01_review", camera_index=1, video_path=video,
        media_started_at_ms=event.timeline_timestamp_ms - 1000, media_duration_ms=4000,
        clock_source=DEFAULT_CLOCK_SOURCE, timing_error_ms=0,
        end_reason="continuous_archive_fallback", race_id=event.race_id,
    )
    PassageEvidenceAssociationStore(path / "passage_evidence_associations.jsonl").confirm(
        passage_event_id=event.event_id, bib=event.bib, confirmed_source=REGULAR_SOURCE,
        segment_id=segment.segment_id, frame_index=12, position_ms=480,
        marker_x_normalized=0.5, marker_y_normalized=0.5, confirmed_at_ms=2000,
    )
    return event, video


def test_legacy_mixed_workspace_recovers_scope_and_filmstrip_without_rewriting_evidence(qapp, tmp_path):
    event, video = _legacy_mixed_workspace(tmp_path)
    preserved = {p: p.read_bytes() for p in tmp_path.glob("*.jsonl")}
    inbox = RaceMetadataStore(tmp_path / ".finishreview" / "cyclerace_metadata_inbox.json")
    inbox.store(RaceMetadata(race_id="foreign-race", stage_id="stage-1", revision=1, emitted_at_ms=1))
    window = _window(tmp_path)
    try:
        assert window._current_metadata().race_id == event.race_id
        assert window._current_metadata().stage_id == event.stage_id
        assert window._workspace_mode == "archive"
        assert [item.event_id for item in window._visible_events] == [event.event_id]
        _wait_until(qapp, lambda: bool(window.video_filmstrip.full_race.index.sources))
        snapshot = window._recording_catalog_snapshot
        assert snapshot.context[1] == window._filmstrip_race_id() == event.race_id
        assert video in {source.location.video_path for source in snapshot.sources}
        assert window.association_store.get_for_event(event, REGULAR_SOURCE).frame_index == 12
        assert not (tmp_path / "cyclerace_race_metadata.json").exists()
        assert all(p.read_bytes() == content for p, content in preserved.items())
    finally:
        window.close()

    # An unrelated live event must not replace the persisted historical view.
    RaceMetadataStore(tmp_path / ".finishreview" / "cyclerace_metadata_inbox.json").store(
        RaceMetadata(race_id="foreign-race", stage_id="stage-1", revision=1, emitted_at_ms=1)
    )
    restored = _window(tmp_path)
    try:
        assert restored._workspace_mode == "archive"
        assert restored._current_metadata().race_id == event.race_id
        restored.start_receiver()
        receiver = _FakeReceiver.instances[-1]
        receiver.deliver(_event(event_id="new-foreign", race_id="foreign-race"))
        receiver.deliver_metadata(RaceMetadata(race_id="foreign-race", stage_id="stage-1",
                                              revision=2, emitted_at_ms=2))
        qapp.processEvents()
        assert restored._current_metadata().race_id == event.race_id
        assert restored.passage_store.get("new-foreign") is None
        assert restored._receiver_passage_store.get("new-foreign") is not None
    finally:
        restored.close()


def test_ambiguous_legacy_workspace_requires_selection_and_remembers_it(qapp, tmp_path):
    passages = PassageEventStore(tmp_path / "cyclerace_passage_events.jsonl")
    passages.append(_event(event_id="first"))
    passages.append(_event(event_id="second", race_id="another-race"))
    timeline = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    for race_id in ("race-1", "another-race"):
        video = tmp_path / f"{race_id}.mkv"
        video.write_bytes(b"saved-video")
        timeline.add_completed_segment(source_id="camera_01_review", camera_index=1, video_path=video,
                                       media_started_at_ms=1000, media_duration_ms=4000,
                                       clock_source=DEFAULT_CLOCK_SOURCE, timing_error_ms=0,
                                       end_reason="continuous_archive_fallback", race_id=race_id)
    window = _window(tmp_path)
    try:
        assert window._current_metadata() is None
        assert not window._visible_events
        assert window._workspace_mode == "archive"
        assert not window.video_filmstrip.full_race.index.sources
        assert window.event_name_label.text() == "未确定赛事"
        # Event switching is exposed from the modal settings dialog only;
        # the workbench must not open a second, competing selector.
        assert not any(
            isinstance(widget, QPushButton) and widget.text() == "选择赛事"
            for widget in window.findChildren(QPushButton)
        )
        assert window._select_workspace_event("another-race", "stage-1")
        assert [item.event_id for item in window._visible_events] == ["second"]
    finally:
        window.close()
    restored = _window(tmp_path)
    try:
        assert restored._current_metadata().race_id == "another-race"
        assert restored._workspace_mode == "archive"
        assert [item.event_id for item in restored._visible_events] == ["second"]
    finally:
        restored.close()


def test_single_event_workspace_hides_useless_event_picker(qapp, tmp_path):
    passages = PassageEventStore(tmp_path / "cyclerace_passage_events.jsonl")
    passages.append(_event(event_id="only"))
    RaceMetadataStore(tmp_path / "cyclerace_race_metadata.json").store(
        RaceMetadata(race_id="race-1", stage_id="stage-1", revision=1, emitted_at_ms=1)
    )
    window = _window(tmp_path)
    try:
        assert window._current_metadata() is not None
        assert not any(
            isinstance(widget, QPushButton) and widget.text() == "选择赛事"
            for widget in window.findChildren(QPushButton)
        )
    finally:
        window.close()


def test_received_race_can_be_selected_without_metadata_and_survives_restart(qapp, tmp_path):
    old = RaceMetadata(race_id="old-race", stage_id="stage-1", revision=1, emitted_at_ms=1,
                       race_name="Old race")
    RaceMetadataStore(tmp_path / "cyclerace_race_metadata.json").store(old)
    RaceMetadataStore(tmp_path / ".finishreview/cyclerace_metadata_inbox.json").store(old)
    original = (tmp_path / "cyclerace_race_metadata.json").read_bytes()
    window = _window(tmp_path)
    window.start_receiver()
    incoming = _event(event_id="current-read", race_id="current-race", race_name="Current race",
                      group_name="test", is_active=False)
    _FakeReceiver.instances[-1].deliver(incoming)
    qapp.processEvents()
    try:
        assert window._select_received_event("current-race", "stage-1")
        target = window.output_dir
        assert target != tmp_path
        assert window._current_metadata().race_id == "current-race"
        assert window._workspace_mode == "live"
        assert window.passage_store.get(incoming.event_id) is not None
        assert not (target / "cyclerace_race_metadata.json").exists()
        assert (tmp_path / "cyclerace_race_metadata.json").read_bytes() == original
        window.timeline_store.add_completed_segment(
            source_id="camera_01_review", camera_index=1, video_path=target / "recorded.mkv",
            media_started_at_ms=1000, media_duration_ms=2000,
            clock_source=DEFAULT_CLOCK_SOURCE, timing_error_ms=0,
            end_reason="continuous_archive_fallback", race_id="current-race",
        )
    finally:
        window.close()
    restored = _window(tmp_path)
    try:
        assert restored.output_dir == target
        assert restored._workspace_mode == "live"
        assert restored._current_metadata().race_id == "current-race"
        restored.start_receiver()
        receiver = _FakeReceiver.instances[-1]
        receiver.deliver(_event(event_id="old-replay", race_id="old-race"))
        qapp.processEvents()
        assert restored._current_metadata().race_id == "current-race"
        authoritative = RaceMetadata(race_id="current-race", stage_id="stage-1", revision=1,
                                     emitted_at_ms=1, race_name="Full current race")
        receiver.deliver_metadata(authoritative)
        qapp.processEvents()
        assert restored.metadata_store.current() == authoritative
    finally:
        restored.close()


def test_changing_root_keeps_inbox_races_even_when_target_has_old_metadata(qapp, tmp_path):
    window = _window(tmp_path / "old")
    window.start_receiver()
    _FakeReceiver.instances[-1].deliver(_event(race_id="current-race", race_name="Current race", is_active=False))
    qapp.processEvents()
    target = tmp_path / "new"
    RaceMetadataStore(target / "cyclerace_race_metadata.json").store(
        RaceMetadata(race_id="historical", stage_id="stage-1", revision=1, emitted_at_ms=1))
    try:
        assert window._apply_settings(replace(window._current_settings(), output_dir=target))
        assert "current-race" in {item.race_id for item in window._received_event_candidates()}
        assert window._select_received_event("current-race", "stage-1")
        assert window._current_metadata().race_id == "current-race"
    finally:
        window.close()


def test_receiver_keeps_other_stage_in_inbox_before_metadata_arrives(qapp, tmp_path):
    window = _window(tmp_path)
    try:
        window.start_receiver()
        receiver = _FakeReceiver.instances[-1]
        receiver.deliver(_event(event_id="first", group_name="备用测试"))
        receiver.deliver(_event(event_id="other", stage_id="stage-2"))
        qapp.processEvents()
        assert window.passage_store.get("first") is not None
        assert window.passage_store.get("other") is None
        assert window._receiver_passage_store.get("other") is not None
    finally:
        window.close()


def test_removed_count_workflow_ignores_and_preserves_legacy_files(qapp, tmp_path, monkeypatch):
    legacy_files = {
        name: b"legacy data is not a current report\n"
        for name in ("count_reconciliation.json", "count_reviews.jsonl", "count_anomaly_history.jsonl")
    }
    for name, data in legacy_files.items():
        (tmp_path / name).write_bytes(data)
    original_open = Path.open

    def reject_legacy_access(path, *args, **kwargs):
        if path.name in legacy_files:
            pytest.fail(f"Removed count workflow accessed {path.name}")
        return original_open(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", reject_legacy_access)
        window = _window(tmp_path)
        try:
            assert window.findChild(QPushButton, "finishCountReviewButton") is None
            assert not hasattr(window, "count_panel")
            assert not hasattr(window, "count_status_label")
            for index in range(3):
                window.passage_store.append(_event(event_id=f"c{index}", sequence=index + 1, bib=str(index + 1)))
            window.refresh()
            window._on_clock_tick()
            assert {event.event_id for event in window._visible_events} == {"c0", "c1", "c2"}
        finally:
            window.close()
    for name, data in legacy_files.items():
        assert (tmp_path / name).read_bytes() == data


def test_problem_list_toggle_restores_layout_without_filtering_passages(qapp, tmp_path):
    window = _window(tmp_path)
    try:
        window.passage_store.append(_event(event_id="visible"))
        window.refresh()
        widgets = (window.preview_panel, window.video_filmstrip, window.results_panel, window.table)
        minimums = tuple(widget.minimumHeight() for widget in widgets)
        manually_sized = window._review_split_manually_sized
        for _ in range(2):
            window.problem_list_button.click()
            assert not window.problem_panel.isHidden()
            assert window._problem_workbench_active
            assert window.review_content_splitter.widget(1) is window.preview_panel
            assert window.problem_panel.splitter.widget(1) is window.evidence_splitter
            assert window.table.isHidden()
            # 集团不再另开窗口：判读栏下面那条刻度就是集团核对。
            assert window.problem_panel.timeline.parent() is window.problem_panel
            window._on_clock_tick()
            assert [event.event_id for event in window._visible_events] == ["visible"]
            window.problem_list_button.click()
            assert window.problem_panel.isHidden()
            assert not window._problem_workbench_active
            assert window.workspace_splitter.widget(1) is window.evidence_splitter
            assert tuple(widget.minimumHeight() for widget in widgets) == minimums
            assert window._review_split_manually_sized == manually_sized
    finally:
        window.close()


def test_problem_item_opens_its_original_frame_in_its_own_camera(qapp, tmp_path, monkeypatch):
    window = _window(tmp_path)
    try:
        video = tmp_path / "camera_02.mkv"
        video.write_bytes(b"video")
        segment = window.timeline_store.add_completed_segment(
            source_id="camera_02", camera_index=2, video_path=video,
            media_started_at_ms=1_789_000_000_000, media_duration_ms=5000,
            clock_source=DEFAULT_CLOCK_SOURCE, timing_error_ms=0,
            end_reason="continuous_archive_fallback", race_id="race-1")
        opened = []
        monkeypatch.setattr(window, "_seek_retained_camera_frame",
                            lambda loc, pos, frame, camera=1: opened.append((loc.video_path, pos, frame, camera)) or True)
        frame = dict(segment_id=segment.segment_id, video_path=str(video), camera_index=2,
                     frame_index=37, position_ms=1850)
        window._open_problem_frame(frame)
        assert opened == [(video, 1850, 37, 2)]
        window._open_problem_frame(dict(frame, video_path=str(tmp_path / "missing.mkv")))
        assert len(opened) == 1
        assert "原录像缺失" in window.problem_panel.hint.text()
    finally:
        window.close()


def test_video_discovery_store_follows_workspace_switch_and_restores_records(qapp, tmp_path):
    from realtime.video_discovery import VideoDiscoveryStore
    first = tmp_path / "first"
    second = tmp_path / "second"
    for directory, bib in ((first, "11"), (second, "22")):
        VideoDiscoveryStore(directory / "video_discoveries.jsonl").add(
            discovery_id=f"discovery-{bib}", race_id="race-1", stage_id="stage-1", batch_id="batch",
            bib=bib, camera_index=1, frame_index=10, position_ms=400, started_at_ms=1000, ended_at_ms=2000)
    window = _window(first)
    try:
        assert [entry["bib"] for entry in window._video_discovered_entries] == ["11"]
        original_bytes = (first / "video_discoveries.jsonl").read_bytes()
        assert window._apply_settings(replace(window._current_settings(), output_dir=second))
        assert window.video_discovery_store.journal_path.parent == second
        assert [entry["bib"] for entry in window._video_discovered_entries] == ["22"]
        window.video_discovery_store.update_status("discovery-22", "ignored")
        assert (first / "video_discoveries.jsonl").read_bytes() == original_bytes
        assert window._apply_settings(replace(window._current_settings(), output_dir=first))
        assert [entry["bib"] for entry in window._video_discovered_entries] == ["11"]
        assert window.video_discovery_store.journal_path.parent == first
    finally:
        window.close()


def test_modal_disk_task_keeps_qt_alive_and_defers_context_changes(qapp, tmp_path, monkeypatch):
    window = _window(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    caller = threading.get_ident()
    threads = []
    changes = []
    timer = QTimer()
    timer.setInterval(5)

    def tick():
        if entered.is_set():
            assert not window.isEnabled()
            assert window._defer_capture_callback(changes.append, "metadata")
            assert changes == []
            release.set()
            timer.stop()

    def disk_task():
        threads.append(threading.get_ident())
        entered.set()
        assert release.wait(2), "Qt stopped dispatching while disk task was blocked"
        return "prepared"

    timer.timeout.connect(tick)
    timer.start()
    try:
        assert window._run_capture_task(disk_task) == "prepared"
        assert window.isEnabled()
        assert threads != [caller]
        _wait_until(qapp, lambda: changes == ["metadata"])
    finally:
        release.set()
        timer.stop()
        window.close()


def test_concrete_review_windows_are_sibling_types():
    assert not issubclass(FinishReviewWindow, passage_review.PassageReviewDialog)
    assert issubclass(FinishReviewWindow, passage_review.PassageReviewSurface)
    assert issubclass(
        passage_review.PassageReviewDialog,
        passage_review.PassageReviewSurface,
    )


def test_new_recording_excludes_previous_day_and_reconnect_keeps_scope(qapp, tmp_path, monkeypatch):
    window = _window(tmp_path)
    monkeypatch.setattr(window, "_video_assist_enabled", lambda: False)
    old_start = int(time.time() * 1000) - 86400000
    old = tmp_path / "yesterday.mkv"
    old.write_bytes(b"old video")
    window.timeline_store.add_completed_segment(
        source_id="camera_01_review", camera_index=1, video_path=old,
        media_started_at_ms=old_start, media_duration_ms=10000,
        clock_source=DEFAULT_CLOCK_SOURCE, timing_error_ms=2000,
        end_reason="continuous_archive_fallback", race_id="race-1",
    )
    try:
        window.passage_store.append(_event())
        window.start_recording()
        panel = window.video_filmstrip.full_race
        start = panel._recording_start_ms
        _wait_until(qapp, lambda: bool(panel.index.sources))
        assert all(item.end_ms > start for item in panel.index.sources)
        assert old not in {item.location.video_path for item in panel.index.sources}
        window._restart_recording_camera(1, window.source)
        assert panel._recording_start_ms == start
        window.stop_recording()
        assert panel._recording_start_ms == start
        panel.scope_combo.setCurrentIndex(panel.scope_combo.findData("all"))
        assert old in {item.location.video_path for item in panel.index.sources}
        window.start_recording()
        assert panel.scope_combo.currentData() == "current"
        assert panel._recording_start_ms >= start
    finally:
        window.close()
    # Only a view filter changed; old evidence remains recoverable from disk.
    assert old.is_file()
    assert any(item.media_started_at_ms == old_start
               for item in VideoTimelineStore(tmp_path / "video_timeline.jsonl").segments())


def test_live_filmstrip_confirmation_reopens_after_cleanup_and_archive(qapp, tmp_path, monkeypatch):
    from PyQt5.QtGui import QImage
    from realtime.review_recorder import ReviewRingBuffer
    from realtime.race_filmstrip import RaceFilmstripFrame

    window = _window(tmp_path)
    root = tmp_path / "review_buffer" / "camera_01"
    root.mkdir(parents=True, exist_ok=True)
    stem = "camera_01_20260917_091639_094654"
    start = 1_789_600_000_000
    video = root / f"{stem}_review_0000000000000000.ts"
    video.write_bytes(b"video")
    playlist = root / f"{stem}.m3u8"
    playlist.write_text(
        "#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:2.000,\n"
        f"#EXT-X-PROGRAM-DATE-TIME:{datetime.fromtimestamp(start / 1000, timezone.utc).isoformat()}\n"
        f"{video.name}\n", encoding="utf-8",
    )
    ring = ReviewRingBuffer(playlist, camera_index=1, retention_seconds=1)
    ring.scan()
    event = _event(passage_timestamp_ms=start + 1000)
    window.passage_store.append(event)
    window._ring_buffers = {1: ring}
    window.refresh()
    window.auto_advance_checkbox.setChecked(False)
    panel = window.video_filmstrip.full_race
    _wait_until(qapp, lambda: any(s.location.video_path == video for s in panel.index.sources))
    source = next(s for s in panel.index.sources if s.location.video_path == video)
    image = QImage(640, 360, QImage.Format_RGB888)
    image.fill(0)
    frame = RaceFilmstripFrame(source, start + 520, 520, 13, image)
    try:
        assert window._open_race_filmstrip_frame(frame)
        pane = window.regular_pane
        pane._worker.frame_ready.emit(image, 520, 13)
        pane._on_marker_position_selected(.5, .5)
        assert window._confirm_pending_marker(pane)
        records = window._camera_judgment_records(pane)
        assert len(records) == 1
        assert records[0].recorder_time_ms == start + 520
        assert records[0].clock_offset_ms == -480
        segment_id = records[0].segment_id
        ring.cleanup(current_time_ms=start + 100000)
        assert video.is_file()
        archive = tmp_path / "videos" / f"{stem}_archive_0000.mkv"
        archive.parent.mkdir(exist_ok=True)
        archive.write_bytes(b"archive")
        window.timeline_store.add_completed_segment(
            source_id="camera_01_review", camera_index=1, video_path=archive,
            media_started_at_ms=start, media_duration_ms=10000,
            clock_source=DEFAULT_CLOCK_SOURCE, timing_error_ms=2000,
            end_reason="continuous_archive_fallback", race_id=event.race_id,
        )
        window._update_filmstrip()
        _wait_until(qapp, lambda: panel.index.span_at(start + 520).source.location.video_path == archive)
        assert panel.index.span_at(start + 520).source.location.video_path == archive
        assert window._continuous_offset_for_location(panel.index.span_at(start + 520).source.location) == -480
    finally:
        window._ring_buffers = {}
        window.close()
    reopened = _window(tmp_path)
    try:
        reopened.refresh()
        records = reopened._camera_judgment_records(reopened.regular_pane)
        assert len(records) == 1 and records[0].segment_id == segment_id
        assert records[0].clock_offset_ms == -480
        monkeypatch.setattr(reopened, "_uses_popup_judging", lambda: False)
        reopened._open_camera_judgment(records[0].key)
        assert reopened.regular_pane.location.video_path == video
        assert reopened.regular_pane.location.playback_position_ms == 520
    finally:
        reopened.close()


def test_select_event_forwards_selection_options(qapp, tmp_path, monkeypatch):
    window = _window(tmp_path)
    captured = {}

    def select_event(
        _self,
        event_id,
        *,
        preserve_current_frame=None,
        locate_target=False,
    ):
        captured["event_id"] = event_id
        captured["pane"] = preserve_current_frame
        captured["locate_target"] = locate_target

    monkeypatch.setattr(passage_review.PassageReviewSurface, "_select_event", select_event)
    window._select_event(
        "passage-1",
        preserve_current_frame=window.regular_pane,
        locate_target=True,
    )

    assert captured == {
        "event_id": "passage-1",
        "pane": window.regular_pane,
        "locate_target": True,
    }
    window.close()


def test_runtime_status_collection_does_not_mutate_labels(qapp, tmp_path):
    window = _window(tmp_path)
    window.beijing_clock_label.setText("unchanged")

    snapshot = window._collect_runtime_status()

    assert isinstance(snapshot, review_window_module.RuntimeStatusSnapshot)
    assert window.beijing_clock_label.text() == "unchanged"
    window.close()


def test_runtime_status_render_uses_supplied_snapshot(qapp, tmp_path):
    window = _window(tmp_path)
    snapshot = replace(
        window._collect_runtime_status(),
        beijing_clock_text="12:34:56",
    )

    window._render_runtime_status(snapshot)

    assert window.beijing_clock_label.text() == "12:34:56"
    window.close()


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

    today = datetime.now(review_window_module.BEIJING_TIMEZONE).date().isoformat()
    event_dir = root / f"{today} 2026 城市公路自行车赛"
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


def test_changing_recording_root_keeps_live_metadata_and_received_passages(qapp, tmp_path):
    window = _window(tmp_path / "old")
    window.start_receiver()
    receiver = _FakeReceiver.instances[-1]
    metadata = RaceMetadata(race_id="race-2026", stage_id="stage-1", revision=1,
                            emitted_at_ms=1, race_name="Live race", stage_date="2026-10-18")
    receiver.deliver_metadata(metadata)
    qapp.processEvents()
    _FakeReceiver.instances[-1].deliver(_event(race_id=metadata.race_id))
    qapp.processEvents()
    before = window.passage_store.events()
    try:
        assert window._apply_settings(replace(window._current_settings(), output_dir=tmp_path / "new"))
        assert window.metadata_store.current() == metadata
        assert window.event_name_label.text() == "Live race"
        assert window.passage_store.events() == before
        assert window._receiver_metadata_store.current() == metadata
        assert window._receiver_passage_store.events() == before
    finally:
        window.close()


def test_cyclerace_workspace_merge_carries_inactive_tombstone(qapp, tmp_path):
    window = _window(tmp_path / "events")
    active = _event(
        race_id="race-idle",
        event_id="passage-1",
        revision=10,
        bib="001",
    )
    withdrawn = replace(active, revision=11, is_active=False)
    other = _event(
        race_id="race-idle",
        event_id="passage-2",
        revision=3,
        bib="002",
    )
    window.passage_store.append(active)
    window._receiver_passage_store.append(withdrawn)
    window._receiver_passage_store.append(other)

    assert window._merge_cyclerace_events(window.passage_store, "race-idle") == 2
    assert window.passage_store.get("passage-1") == withdrawn
    assert window.passage_store.events() == (other,)
    window.close()


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

    day = date(2026, 10, 28)

    assert review_window_module._event_workspace_dir(root, metadata, today=day) == (root / "2026-10-28 城市赛").resolve()
    (root / "2026-10-28 城市赛" / "videos").mkdir(parents=True)
    assert review_window_module._event_workspace_dir(root, metadata, today=day) == (
        root / "2026-10-28 城市赛_race-new").resolve()


def test_a_saved_race_is_found_under_its_old_or_dated_folder_on_any_day(qapp, tmp_path):
    """10-28 起新赛事建“日期 赛事名”文件夹；之前建的、或前一天建的赛事，回来仍进原文件夹。"""
    root = tmp_path / "events"
    old = RaceMetadata(race_id="race-old", stage_id="stage-1", revision=1, emitted_at_ms=1, race_name="城市赛")
    RaceMetadataStore(root / "城市赛_race-old" / "cyclerace_race_metadata.json").store(old)
    dated = replace(old, race_id="race-dated")
    RaceMetadataStore(root / "2026-10-28 城市赛" / "cyclerace_race_metadata.json").store(dated)

    later = date(2026, 10, 30)
    assert review_window_module._event_workspace_dir(root, old, today=later) == (root / "城市赛_race-old").resolve()
    assert review_window_module._event_workspace_dir(root, dated, today=later) == (
        root / "2026-10-28 城市赛").resolve()
    fresh = replace(old, race_id="race-fresh")
    assert review_window_module._event_workspace_dir(root, fresh, today=later) == (
        root / "2026-10-30 城市赛").resolve()


def test_a_race_kept_in_the_root_itself_is_its_own_workspace(qapp, tmp_path):
    """古浪测试副本：CycleRace 切到别的赛事再切回来，录像仍在根目录，不另建空文件夹。"""
    root = tmp_path / "gulang"
    metadata = RaceMetadata(race_id="race-gulang", stage_id="stage-1", revision=1, emitted_at_ms=1,
                            race_name="古浪站", stage_name="终点")
    RaceMetadataStore(root / "cyclerace_race_metadata.json").store(metadata)
    assert review_window_module._event_workspace_dir(root, metadata) == root.resolve()
    other = replace(metadata, race_id="race-other", race_name="特奥")
    assert review_window_module._event_workspace_dir(root, other, today=date(2026, 10, 28)) == (
        root / "2026-10-28 特奥").resolve()


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
    assert window.record_button.isEnabled()
    assert window.record_button.text() == "返回现场"
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


def _archived_field_event(root, name="古浪公路赛", race_id="race-gulang"):
    """A finished race opened from disk, as the field finds it before a start."""

    event_dir = root / name
    event_dir.mkdir(parents=True)
    PassageEventStore(event_dir / "cyclerace_passage_events.jsonl").append(
        _event(event_id="yesterday-read", race_id=race_id)
    )
    RaceMetadataStore(event_dir / "cyclerace_race_metadata.json").store(
        RaceMetadata(race_id=race_id, stage_id="stage-1", revision=1, emitted_at_ms=1,
                     race_name=name, stage_name="终点")
    )
    save_workspace_mode(root, event_dir, "archive")
    return event_dir


def test_returning_to_the_field_needs_no_timing_data_and_starts_recording(qapp, tmp_path):
    root = tmp_path / "events"
    event_dir = _archived_field_event(root)
    window = _window(root)
    try:
        assert window._workspace_mode == "archive"
        assert window.record_button.text() == "返回现场"

        # 计时设备空闲：收件箱还没有本次赛事的任何正式信息。
        assert window._receiver_metadata_store.current() is None
        assert window._received_event_candidates() == ()
        assert window._return_to_live_event()

        assert window._workspace_mode == "live"
        assert window.output_dir == event_dir.resolve()
        assert window._current_metadata().race_id == "race-gulang"
        assert window.record_button.text() == "开始录像"

        window.start_recording()

        assert window._recording_any_active()
        panel = window.video_filmstrip.full_race
        assert panel.scope_combo.currentData() == "current"
        assert panel._recording_start_ms is not None
        assert window.passage_store.journal_path.parent == event_dir.resolve()
    finally:
        window.stop_recording()
        window.close()


def test_returning_to_the_field_without_any_race_uses_an_isolated_folder(qapp, tmp_path):
    root = tmp_path / "events"
    history = root / "旧赛事"
    history.mkdir(parents=True)
    passages_path = history / "cyclerace_passage_events.jsonl"
    passages = PassageEventStore(passages_path)
    # 两个赛段混在同一个旧目录里：没有唯一赛事可以当作本次现场赛事。
    passages.append(_event(event_id="old-1", race_id="race-old"))
    passages.append(_event(event_id="old-2", race_id="race-other"))
    before = passages_path.read_bytes()
    save_workspace_mode(root, history, "archive")

    window = _window(root)
    try:
        assert window._workspace_mode == "archive"
        assert window._current_metadata() is None

        assert window._return_to_live_event()

        assert window._workspace_mode == "live"
        assert window.output_dir.parent == root.resolve()
        assert window.output_dir != history.resolve()
        window.start_recording()
        assert window.passage_store.journal_path.parent == window.output_dir
        assert passages_path.read_bytes() == before
        field_dir = window.output_dir
    finally:
        window.stop_recording()
        window.close()
    # 显式返回现场后重启仍停在这一处待机，不被旧目录的历史推断拉回历史查看。
    restored = _window(root)
    try:
        assert restored._workspace_mode == "live"
        assert restored.output_dir == field_dir
    finally:
        restored.close()


def test_clearing_the_race_data_removes_test_readings_and_keeps_settings(qapp, tmp_path, monkeypatch):
    root = tmp_path / "events"
    window = _window(root)
    monkeypatch.setattr(QMessageBox, "warning",
                        lambda _parent, title, message: pytest.fail(f"清空时不应弹出警告：{title} {message}"))
    monkeypatch.setattr(QMessageBox, "question", lambda *_args, **_kwargs: QMessageBox.Yes)
    try:
        metadata = RaceMetadata(race_id="race-live", stage_id="stage-1", revision=1, emitted_at_ms=1,
                                race_name="本场比赛", stage_name="终点")
        assert window._activate_cyclerace_workspace(metadata)
        event_dir = window.output_dir
        window.passage_store.append(_event(event_id="test-read", race_id="race-live"))
        window._receiver_passage_store.append(_event(event_id="test-inbox", race_id="race-live"))
        window.set_finish_line_roi(2, (0.2, 0.1, 0.4, 0.9))
        problems = event_dir / "finish_problems"
        problems.mkdir(exist_ok=True)
        (problems / "analysis_config.json").write_text('{"profiles": []}', encoding="utf-8")
        (problems / "problem_list.json").write_text('{"items": []}', encoding="utf-8")
        (problems / "operator_drafts.json").write_text('{"marks": {}}', encoding="utf-8")
        video = event_dir / "videos" / "test_000.mkv"
        video.parent.mkdir()
        video.write_bytes(b"test video")
        window.timeline_store.add_completed_segment(
            source_id="camera_01_review", camera_index=1, video_path=video,
            media_started_at_ms=1000, media_duration_ms=2000,
            clock_source=DEFAULT_CLOCK_SOURCE, timing_error_ms=0,
            end_reason="test", race_id="race-live",
        )

        released = set()
        for pane in window.all_evidence_panes:
            shutdown = pane.shutdown
            def release(*, wait=True, pane=pane, shutdown=shutdown):
                shutdown(wait=wait)
                released.add(pane)
            monkeypatch.setattr(pane, "shutdown", release)
        remove_data = window._remove_event_data
        def remove_after_release(event_dir, root):
            assert released == set(window.all_evidence_panes)
            return remove_data(event_dir, root)
        monkeypatch.setattr(window, "_remove_event_data", remove_after_release)

        window.problem_panel.clear_data_button.click()

        assert window.passage_store.events(include_inactive=True) == ()
        assert window._receiver_passage_store.events(include_inactive=True) == ()
        assert not (event_dir / "video_timeline.jsonl").exists()
        assert not video.exists()
        assert not (problems / "problem_list.json").exists()
        assert not (problems / "operator_drafts.json").exists()
        assert (problems / "analysis_config.json").is_file()
        assert (event_dir / "motion_finish_lines.json").is_file()
        assert (event_dir / "cyclerace_race_metadata.json").is_file()
        assert window._finish_line_rois[2] == pytest.approx((0.2, 0.1, 0.4, 0.9))
        assert window._workspace_mode == "live"
        assert window.record_button.text() == "开始录像"
        # The folder was held while files went, and handed back afterwards.
        assert not window._recording_lock.held
        assert not (event_dir / "recording.lock").exists()
        # 草稿计时器不能在删除后把清掉的点数写回磁盘。
        qapp.processEvents()
        assert not (problems / "operator_drafts.json").exists()
        # 清空之后到来的读数就是正式比赛数据。
        window.passage_store.append(_event(event_id="race-read", race_id="race-live"))
        assert window.passage_store.get("race-read") is not None
    finally:
        window.close()


def test_clearing_race_data_is_blocked_while_recording_or_in_history(qapp, tmp_path, monkeypatch):
    window = _window(tmp_path)
    window.passage_store.append(_event(event_id="test-read"))
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning",
                        lambda _parent, title, message: warnings.append((title, message)))
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *_args, **_kwargs: pytest.fail("受保护的清空不应开始询问"))
    try:
        window.start_recording()
        window._clear_event_data()
        assert warnings[-1][0] == "无法清空数据"
        assert "请先停止普通录像" in warnings[-1][1]
        assert window.passage_store.get("test-read") is not None
        window.stop_recording()

        window._workspace_mode = "archive"
        window._clear_event_data()
        assert warnings[-1][0] == "无法清空数据"
        assert warnings[-1][1].startswith("正在查看历史赛事")
        assert window.passage_store.get("test-read") is not None
    finally:
        window.close()


def test_a_second_window_cannot_record_into_the_same_race_folder(qapp, tmp_path, monkeypatch):
    # 古浪 2026-10-18: two builds recorded into one folder and every rider counted twice.
    first = _window(tmp_path)
    second = _window(tmp_path)
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning",
                        lambda _parent, title, message: warnings.append((title, message)))
    monkeypatch.setattr(QMessageBox, "question", lambda *_args, **_kwargs: QMessageBox.Yes)
    try:
        assert second.output_dir == first.output_dir
        second.passage_store.append(_event(event_id="test-read"))
        first.start_recording()
        _FakeRecorder.instances.clear()

        with pytest.raises(FolderInUseError, match="另一个 FinishReview"):
            second.start_recording()
        assert _FakeRecorder.instances == []
        # Clearing from the idle window would wipe the recording one's race.
        second._clear_event_data()
        assert warnings[-1][0] == "无法清空数据"
        assert "未做任何改动" in warnings[-1][1]
        assert second.passage_store.get("test-read") is not None

        first.stop_recording()
        second.start_recording()
        assert second._recording_any_active()
    finally:
        second.stop_recording()
        first.close()
        second.close()


def test_changing_the_recording_root_returns_to_the_field(qapp, tmp_path):
    root = tmp_path / "events"
    _archived_field_event(root)
    window = _window(root)
    try:
        assert window._workspace_mode == "archive"
        assert window.record_button.text() == "返回现场"

        target = tmp_path / "new-root"
        assert window._apply_settings(replace(window._current_settings(), output_dir=target))

        assert window.workspace_root == target.resolve()
        assert window._workspace_mode == "live"
        assert window.record_button.text() == "开始录像"
    finally:
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


def test_deleting_passage_immediately_updates_review_export(qapp, tmp_path, monkeypatch):
    window = _window(tmp_path / "events")
    metadata = RaceMetadata(
        race_id="race-delete", stage_id="stage-1", revision=1,
        emitted_at_ms=1, race_name="删除测试", stage_name="终点",
    )
    assert window._activate_cyclerace_workspace(metadata)
    window.passage_store.append(_event(race_id="race-delete", event_id="spare"))
    window.refresh()
    window._export_review_summary()
    summary = window.output_dir / REVIEW_SUMMARY_FILENAME
    with summary.open(encoding="utf-8-sig", newline="") as handle:
        assert len(list(csv.reader(handle))) == 2
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.Yes)
    from PyQt5.QtWidgets import QInputDialog
    monkeypatch.setattr(QInputDialog, "getText", lambda *_args, **_kwargs: ("备用芯片", True))
    window.delete_record_button.click()
    assert window.table.rowCount() == 0
    with summary.open(encoding="utf-8-sig", newline="") as handle:
        assert len(list(csv.reader(handle))) == 1
    window.close()


def test_arrival_table_deletes_a_chip_and_the_old_records_page_has_no_entry(qapp, tmp_path, monkeypatch):
    window = _window(tmp_path / "events")
    assert window.problem_list_button.isHidden() and not hasattr(window, "passages_button")
    metadata = RaceMetadata(
        race_id="race-delete", stage_id="stage-1", revision=1,
        emitted_at_ms=1, race_name="删除测试", stage_name="终点",
    )
    assert window._activate_cyclerace_workspace(metadata)
    window.passage_store.append(_event(race_id="race-delete", event_id="spare"))
    window.refresh()
    window._export_review_summary()
    panel = window.problem_panel
    panel.refresh()
    assert [entry.event.event_id for entry in panel._arrival_rows if entry.event] == ["spare"]
    from PyQt5.QtWidgets import QInputDialog
    monkeypatch.setattr(QInputDialog, "getText", lambda *_args, **_kwargs: ("", False))
    panel._arrival_clicked(0)
    panel._request_delete()  # cancelled: nothing happens
    assert window.passage_store.get("spare") in window.passage_store.events()
    monkeypatch.setattr(QInputDialog, "getText", lambda *_args, **_kwargs: ("备用芯片", True))
    panel._request_delete()
    assert not [entry for entry in panel._arrival_rows if entry.event]
    summary = window.output_dir / REVIEW_SUMMARY_FILENAME
    with summary.open(encoding="utf-8-sig", newline="") as handle:
        assert len(list(csv.reader(handle))) == 1
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
    _wait_until(qapp, lambda: window.regular_pane.location is not None)

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
    assert len([segment for segment in window.timeline_store.segments()
                if segment.end_reason == "passage_review_window"]) == 1
    assert any(segment.end_reason == "retained_problem_segment" for segment in window.timeline_store.segments())
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


def test_another_race_never_cuts_the_running_recording_over(qapp, tmp_path):
    """10-28: CycleRace test races switched the live console every 3-4 s, restarting the recording."""
    window = _window(tmp_path)
    window.start_receiver()
    receiver = _FakeReceiver.instances[-1]
    receiver.deliver_metadata(RaceMetadata(
        race_id="race-formal", stage_id="stage-1", revision=1, emitted_at_ms=1,
        race_name="正式赛事", stage_name="终点", groups=(RaceGroupMetadata("men-open", "男子公开组"),)))
    qapp.processEvents()
    recorder = window.recorder
    folder = window.output_dir
    assert recorder is not None and recorder.is_running

    for index in range(3):
        receiver.deliver_metadata(RaceMetadata(
            race_id=f"legacy-db-{index}-race-1", stage_id="stage-1", revision=1, emitted_at_ms=2,
            race_name="接力测试赛", stage_name="接力赛段", groups=(RaceGroupMetadata("1", "公开组"),)))
        qapp.processEvents()

    assert window.recorder is recorder and recorder.is_running and not recorder.stopped
    assert window.output_dir == folder
    assert window.metadata_store.current().race_id == "race-formal"
    assert "接力测试赛" in window._workspace_notice and "未切换" in window._workspace_notice
    assert not [path for path in tmp_path.rglob("cyclerace_race_metadata.json") if "接力测试赛" in str(path)]
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
    # A device that is in trouble is exactly what the strip exists for.
    assert not window.camera_status_label.isHidden()
    assert not window.runtime_status_strip.isHidden()
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
    window._open_passages()
    window.start()
    _FakeReceiver.instances[-1].deliver(_event())
    _wait_until(qapp, lambda: window.regular_pane.location is not None)

    preview_location = window.regular_pane.location
    assert preview_location is not None
    assert preview_location.status == "preview"
    assert preview_location.segment.end_reason == "passage_review_preview"
    assert preview_location.video_path.name.startswith("preview_")
    assert window.regular_pane.status_label.text() == "录像处理中"
    assert all(segment.end_reason == "retained_problem_segment" for segment in window.timeline_store.segments())
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
    assert len([segment for segment in window.timeline_store.segments()
                if segment.end_reason == "passage_review_window"]) == 1
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


def test_evidence_failure_does_not_hide_received_passage(qapp, tmp_path, monkeypatch):
    window = _window(tmp_path, passage_batch_interval_ms=1000)
    window.start()
    receiver = _FakeReceiver.instances[0]
    event = _event()

    def fail_publish(*_args, **_kwargs):
        raise RuntimeError("evidence unavailable")

    monkeypatch.setattr(window._publishers[1], "publish_many", fail_publish)
    try:
        receiver.deliver(event)
        qapp.processEvents()
        window._passage_batch_timer.stop()
        window._flush_passage_batch()
        assert window.table.rowCount() == 1
        assert window.table.item(0, 1).text() == event.bib
        _wait_until(qapp, lambda: "evidence unavailable" in window._capture_error)
        withdrawn = replace(event, revision=2, is_active=False)
        receiver.deliver(withdrawn)
        qapp.processEvents()
        window._passage_batch_timer.stop()
        window._flush_passage_batch()
        assert window.table.rowCount() == 0
    finally:
        window.close()


def test_restarted_recording_reuses_persisted_binding_without_regrouping(qapp, tmp_path, monkeypatch):
    from types import SimpleNamespace

    window = _window(tmp_path)
    event = _event(passage_timestamp_ms=15000)
    window.passage_store.append(event)
    clip = window.review_binding_store.get_or_add_clip(
        race_id=event.race_id, camera_index=1, source_id="camera_01_review",
        started_at_ms=10000, ended_at_ms=20000,
        playlist_path=tmp_path / "saved.m3u8", segment_signature="saved-group",
        timeline_segment_id="saved-timeline",
    )
    window.review_binding_store.bind(
        event_id=event.event_id, revision=event.revision, camera_index=1,
        clip_id=clip.clip_id, passage_timestamp_ms=15000, passage_offset_ms=5000,
    )
    # Restart forgets only the in-memory publication set, never the durable binding.
    coordinator = SimpleNamespace(register=lambda *a, **k: pytest.fail("must reuse saved binding"), refresh=lambda **k: ())
    window._coordinators = {1: coordinator}
    try:
        request = window._capture_refresh_request()
        request.evidence_job.pipeline.publishers[1] = SimpleNamespace()
        request.evidence_job.pipeline.refresh(request.evidence_job.passages, lambda: False)
        assert window.review_binding_store.active_bindings(event.event_id, event.revision)
        assert not window.review_binding_store.active_bindings(event.event_id, 2)
    finally:
        window._coordinators = {}
        window.close()


def test_slow_evidence_does_not_delay_visible_withdrawal(qapp, tmp_path, monkeypatch):
    import threading

    window = _window(tmp_path)
    window.start()
    entered = threading.Event()
    release = threading.Event()
    publisher = window._publishers[1]
    publish = publisher.publish_many

    def blocked_publish(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return publish(*args, **kwargs)

    monkeypatch.setattr(publisher, "publish_many", blocked_publish)
    event = _event()
    try:
        _FakeReceiver.instances[0].deliver(event)
        _wait_until(qapp, entered.is_set)
        assert window.table.rowCount() == 1
        assert window.table.item(0, 1).text() == event.bib
        _FakeReceiver.instances[0].deliver(replace(event, revision=2, is_active=False))
        _wait_until(qapp, lambda: window.table.rowCount() == 0)
        assert not release.is_set()
        release.set()
        _wait_until(qapp, lambda: window._evidence_pipeline._registered.get(event.event_id)
                    is not None and not window._evidence_pipeline._registered[event.event_id].active)
        assert window.table.rowCount() == 0
        assert window.review_binding_store.active_bindings(event.event_id, 1) == ()
    finally:
        release.set()
        window.close()


def test_received_passages_are_coalesced_into_one_ui_and_archive_batch(
    qapp,
    tmp_path,
    monkeypatch,
):
    window = _window(tmp_path, passage_batch_interval_ms=1_000)
    window.start_receiver()
    window.start_recording()
    # Drain the initial filmstrip/catalog refresh scheduled by the window
    # before observing the passage-triggered refresh below.
    qapp.processEvents()
    receiver = _FakeReceiver.instances[0]
    refreshed_batches = []
    requests = []

    def record_refresh(event_ids):
        refreshed_batches.append(set(event_ids))

    monkeypatch.setattr(window, "refresh_events", record_refresh)
    monkeypatch.setattr(window._capture_refresh_worker, "submit", requests.append)
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
    assert len(requests) == 1
    assert len(requests[0].evidence_job.passages) == 3
    assert "本次收到 3 条" in window.receiver_status_label.text()
    window.close()


def test_ready_windows_are_grouped_by_overlap_and_twenty_second_limit(
    qapp,
    tmp_path,
):
    window = _window(tmp_path)
    calls = []

    class _Publisher:
        def publish_many(self, windows, *, race_id):
            calls.append((windows, race_id))

    def ready_window(event_id, started_at_ms, ended_at_ms):
        return PassageReviewWindow(
            event_id=event_id,
            passage_timestamp_ms=started_at_ms + 3_000,
            started_at_ms=started_at_ms,
            ended_at_ms=ended_at_ms,
            state=PassageReviewState.READY,
            segments=(
                ReviewSegment(
                    segment_id=f"{event_id}.ts",
                    source_id="camera_01_review",
                    camera_index=1,
                    video_path=f"{event_id}.ts",
                    started_at_ms=started_at_ms,
                    duration_ms=ended_at_ms - started_at_ms,
                ),
            ),
        )

    windows = (
        ready_window("passage-1", 10_000, 16_000),
        ready_window("passage-2", 14_000, 20_000),
        ready_window("passage-3", 40_000, 46_000),
    )
    for index, capture_window in enumerate(windows, start=1):
        window.passage_store.append(
            _event(
                event_id=capture_window.event_id,
                sequence=index,
                bib=str(index),
                passage_timestamp_ms=capture_window.passage_timestamp_ms,
            )
        )
    window._publishers = {1: _Publisher()}
    window._capture_windows_by_camera = {
        1: {capture_window.event_id: capture_window for capture_window in windows}
    }

    from types import SimpleNamespace
    from realtime.evidence_pipeline import EvidencePassage, EvidencePipeline
    pipeline = EvidencePipeline(
        {1: SimpleNamespace(register=lambda *a, **k: None, refresh=lambda **k: windows)},
        window._publishers, window.review_binding_store,
    )
    passages = tuple(EvidencePassage(event.event_id, event.revision, event.race_id,
                                    event.timeline_timestamp_ms)
                     for event in window.passage_store.events())
    pipeline.refresh(passages, lambda: False)
    assert [len(group) for group, _race_id in calls] == [2, 1]
    assert all(race_id == "race-1" for _group, race_id in calls)
    window.close()


def test_archive_refresh_only_targets_events_in_range_without_binding(
    qapp,
    tmp_path,
):
    window = _window(tmp_path)
    in_range = _event(
        event_id="passage-in-range",
        sequence=1,
        bib="1",
        passage_timestamp_ms=15_000,
    )
    out_of_range = _event(
        event_id="passage-out-of-range",
        sequence=2,
        bib="2",
        passage_timestamp_ms=50_000,
    )
    window.passage_store.append(in_range)
    window.passage_store.append(out_of_range)
    video_path = tmp_path / "videos" / "archive.mkv"
    video_path.parent.mkdir(parents=True)
    video_path.write_bytes(b"video")
    segment = window.timeline_store.add_completed_segment(
        source_id="camera_01_review",
        camera_index=1,
        video_path=video_path,
        media_started_at_ms=10_000,
        media_duration_ms=10_000,
        clock_source=DEFAULT_CLOCK_SOURCE,
        timing_error_ms=0,
        end_reason="archive_segment",
        race_id="race-1",
    )

    job = window._capture_refresh_request().evidence_job
    assert job.pipeline.archive_affected_events(job.passages, (segment,), window.timeline_store) == {
        in_range.event_id
    }

    clip = window.review_binding_store.get_or_add_clip(
        race_id="race-1",
        camera_index=1,
        source_id="camera_01_review",
        started_at_ms=10_000,
        ended_at_ms=20_000,
        playlist_path=video_path,
        segment_signature="archive-binding",
        timeline_segment_id=segment.segment_id,
    )
    window.review_binding_store.bind(
        event_id=in_range.event_id,
        revision=in_range.revision,
        camera_index=1,
        clip_id=clip.clip_id,
        passage_timestamp_ms=15_000,
        passage_offset_ms=5_000,
    )

    assert job.pipeline.archive_affected_events(job.passages, (segment,), window.timeline_store) == set()
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
    assert "旧运动检测区域" not in visible_text
    assert "调整红黄蓝标线" not in visible_text
    assert dialog.minimumSizeHint().height() <= 680
    for forbidden in ("0.0.0.0", "18765", "端口"):
        assert forbidden not in visible_text
    dialog.close()


def test_device_settings_keep_legacy_motion_values_without_showing_them(qapp, tmp_path):
    dialog = FinishReviewLaunchDialog(
        FinishReviewSettings(
            source="",
            output_dir=tmp_path,
            passage_host="0.0.0.0",
            passage_port=18765,
            camera_index=1,
            visual_detection_enabled=False,
            visual_camera_index=2,
            visual_finish_line=0.42,
            visual_gate_width=0.1,
            visual_forward_direction="right_to_left",
        ),
        device_provider=lambda: (),
    )

    settings = dialog.settings
    assert settings.visual_detection_enabled is False
    assert settings.visual_camera_index == 2
    assert settings.visual_finish_line == 0.42
    assert settings.visual_gate_width == 0.1
    assert settings.visual_forward_direction == "right_to_left"
    labels = " ".join(widget.text() for widget in dialog.findChildren(QLabel))
    assert "旧运动检测区域" not in labels
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
        launch_dialog_module,
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
    monkeypatch.setattr(launch_dialog_module, "IS_WINDOWS", True)
    monkeypatch.setattr(
        launch_dialog_module.subprocess,
        "Popen",
        lambda command: launched.append(command),
    )

    assert launch_dialog_module._open_event_directory(tmp_path)

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


@pytest.mark.skipif(os.name != "nt", reason="Windows user-scoped password encryption")
@pytest.mark.parametrize("save_button", ["rtsp_save_button", "secondary_rtsp_save_button"])
def test_camera_credentials_save_buttons_persist_and_refill_after_restart(
    qapp, tmp_path, monkeypatch, save_button,
):
    import json

    from realtime.review_main import load_review_settings, save_review_settings

    config_path = tmp_path / "camera_config.json"
    credentials = (("camera user", "first@pass:#%"), ("second-user", "second@pass:#%"))

    class _SaveCredentialsDialog(FinishReviewLaunchDialog):
        def __init__(self, *args, **kwargs):
            kwargs["device_provider"] = lambda: ()
            super().__init__(*args, **kwargs)

        def exec_(self):
            self.rtsp_address_edit.setText("rtsp://192.0.2.101/live")
            self.rtsp_username_edit.setText(credentials[0][0])
            self.rtsp_password_edit.setText(credentials[0][1])
            self.secondary_enabled_checkbox.setChecked(True)
            self.secondary_source_type_combo.setCurrentIndex(
                self.secondary_source_type_combo.findData("rtsp")
            )
            self.secondary_rtsp_address_edit.setText("rtsp://192.0.2.102/live")
            self.secondary_rtsp_username_edit.setText(credentials[1][0])
            self.secondary_rtsp_password_edit.setText(credentials[1][1])
            getattr(self, save_button).click()
            return self.result()

    monkeypatch.setattr(review_window_module, "FinishReviewLaunchDialog", _SaveCredentialsDialog)
    window = _window(tmp_path)
    window._settings_saver = lambda settings: save_review_settings(config_path, settings)
    try:
        window._configure_devices()
        saved_text = config_path.read_text(encoding="utf-8")
        payload = json.loads(saved_text)
        assert payload["source"] == "rtsp://192.0.2.101/live"
        assert payload["secondary_source"] == "rtsp://192.0.2.102/live"
        assert payload["rtsp_password_protected"].startswith("dpapi:v1:")
        assert payload["secondary_rtsp_password_protected"].startswith("dpapi:v1:")
        assert all(password not in saved_text for _username, password in credentials)

        restored = load_review_settings(
            config_path, output_dir=None, passage_host="127.0.0.1",
            passage_port=18765, camera_index=1,
        )
        assert restored.source == window.source
        assert restored.secondary_source == window.secondary_source
        reopened = FinishReviewLaunchDialog(restored, device_provider=lambda: ())
        try:
            assert reopened.rtsp_username_edit.text() == credentials[0][0]
            assert reopened.rtsp_password_edit.text() == credentials[0][1]
            assert reopened.secondary_rtsp_username_edit.text() == credentials[1][0]
            assert reopened.secondary_rtsp_password_edit.text() == credentials[1][1]
            assert reopened.rtsp_password_edit.echoMode() == QLineEdit.Password
            assert reopened.secondary_rtsp_password_edit.echoMode() == QLineEdit.Password
        finally:
            reopened.close()
    finally:
        window.close()


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


def test_device_settings_can_disable_high_speed_for_preflight(
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

    assert dialog.high_speed_enabled_checkbox.isChecked()
    dialog.high_speed_enabled_checkbox.setChecked(False)

    assert not dialog.high_speed_edit.isEnabled()
    assert not dialog.high_speed_browse_button.isEnabled()
    assert dialog.settings.high_speed_dir is None

    dialog._start_preflight()

    assert calls[:2] == ["recording", "passages"]
    assert dialog._preflight_run is not None
    assert not dialog._preflight_run.require_high_speed
    dialog.close()


@pytest.mark.parametrize(
    ("secondary_enabled", "high_speed_enabled", "expected_cameras"),
    [
        (False, False, (1,)),
        (True, False, (1, 2)),
        (False, True, (1,)),
        (True, True, (1, 2)),
    ],
)
def test_finish_review_window_layout_matches_active_sources(
    qapp,
    tmp_path,
    secondary_enabled,
    high_speed_enabled,
    expected_cameras,
):
    window = FinishReviewWindow(
        "rtsp://camera-one/live",
        tmp_path,
        secondary_source=("rtsp://camera-two/live" if secondary_enabled else ""),
        high_speed_dir=(tmp_path / "auyat" if high_speed_enabled else None),
        recorder_factory=_FakeRecorder,
        receiver_factory=_FakeReceiver,
    )

    assert tuple(pane.camera_index for pane in window.regular_panes) == expected_cameras
    assert len(window.evidence_panes) == len(expected_cameras) + int(
        high_speed_enabled
    )
    assert window.high_speed_pane.isHidden() is (not high_speed_enabled)
    window.close()


def test_recheck_does_not_start_high_speed_scan_when_disabled(
    qapp,
    tmp_path,
    monkeypatch,
):
    window = _window(tmp_path)
    scan_calls = []
    monkeypatch.setattr(window, "start_receiver", lambda: None)
    monkeypatch.setattr(
        type(window._high_speed_scan_worker),
        "request_scan",
        lambda _worker: scan_calls.append(True),
    )

    window._recheck_connections()

    assert scan_calls == []
    window.close()


def test_apply_settings_updates_evidence_layout_immediately(
    qapp,
    tmp_path,
):
    window = FinishReviewWindow(
        "rtsp://camera-one/live",
        tmp_path,
        secondary_source="rtsp://camera-two/live",
        high_speed_dir=tmp_path / "auyat",
        recorder_factory=_FakeRecorder,
        receiver_factory=_FakeReceiver,
    )
    settings = FinishReviewSettings(
        source="rtsp://camera-one/live",
        output_dir=tmp_path,
        passage_host="0.0.0.0",
        passage_port=18765,
        camera_index=1,
        high_speed_dir=None,
    )

    assert window._apply_settings(settings)

    assert tuple(pane.camera_index for pane in window.regular_panes) == (1,)
    assert len(window.evidence_panes) == 1
    assert window.high_speed_dir is None
    assert window.high_speed_pane.isHidden()
    window.close()


def test_running_high_speed_scan_requests_refresh_without_recursion(
    qapp,
    tmp_path,
    monkeypatch,
):
    window = _window(tmp_path)
    window.high_speed_dir = tmp_path / "auyat"
    requests = []
    worker_type = type(window._high_speed_scan_worker)
    monkeypatch.setattr(worker_type, "isRunning", lambda _worker: True)
    monkeypatch.setattr(
        worker_type,
        "request_scan",
        lambda _worker: requests.append(True),
    )

    window._request_high_speed_scan()

    assert requests == [True]
    monkeypatch.undo()
    window.close()


def test_apply_settings_refreshes_lookup_after_camera_toggle(
    qapp,
    tmp_path,
):
    event = _event(passage_time_ms=43_201_000)
    window = FinishReviewWindow(
        "rtsp://camera-one/live",
        tmp_path,
        secondary_source="rtsp://camera-two/live",
        recorder_factory=_FakeRecorder,
        receiver_factory=_FakeReceiver,
    )
    window._open_passages()
    for camera_index in (1, 2):
        video_path = tmp_path / f"camera_{camera_index}.mkv"
        video_path.write_bytes(b"video")
        window.timeline_store.add_completed_segment(
            source_id=f"camera_{camera_index}",
            camera_index=camera_index,
            video_path=video_path,
            media_started_at_ms=event.timeline_timestamp_ms - 1_000,
            media_duration_ms=4_000,
            clock_source=DEFAULT_CLOCK_SOURCE,
            timing_error_ms=0,
            end_reason="test",
            race_id=event.race_id,
        )
    window.passage_store.append(event)
    window.refresh()
    qapp.processEvents()

    assert window.regular_panes[1].location is not None

    def settings(secondary_source):
        return FinishReviewSettings(
            source="rtsp://camera-one/live",
            secondary_source=secondary_source,
            output_dir=tmp_path,
            passage_host=window.passage_host,
            passage_port=window.passage_port,
            camera_index=1,
        )

    assert window._apply_settings(settings(""))
    assert tuple(pane.camera_index for pane in window.regular_panes) == (1,)
    assert window._apply_settings(settings("rtsp://camera-two/live"))
    assert tuple(pane.camera_index for pane in window.regular_panes) == (1, 2)
    assert window.regular_panes[1].location is not None
    window.close()


def test_operator_regular_marking_uses_camera_with_a_frame(
    qapp,
    tmp_path,
):
    event = _event(passage_time_ms=43_201_000)
    window = FinishReviewWindow(
        "rtsp://camera-one/live",
        tmp_path,
        secondary_source="rtsp://camera-two/live",
        recorder_factory=_FakeRecorder,
        receiver_factory=_FakeReceiver,
    )
    window._open_passages()
    for camera_index in (1, 2):
        video_path = tmp_path / f"camera_{camera_index}.mkv"
        video_path.write_bytes(b"video")
        window.timeline_store.add_completed_segment(
            source_id=f"camera_{camera_index}",
            camera_index=camera_index,
            video_path=video_path,
            media_started_at_ms=event.timeline_timestamp_ms - 1_000,
            media_duration_ms=4_000,
            clock_source=DEFAULT_CLOCK_SOURCE,
            timing_error_ms=0,
            end_reason="test",
            race_id=event.race_id,
        )
    window.passage_store.append(event)
    window.refresh()
    qapp.processEvents()

    second_pane = window.regular_panes[1]
    frame = passage_review.QImage(640, 360, passage_review.QImage.Format_RGB888)
    frame.fill(0)
    second_pane._worker.frame_ready.emit(frame, 1_000, 25)
    qapp.processEvents()
    window._update_operator_controls()

    assert window.mark_regular_button.isEnabled()
    QTest.mouseClick(window.mark_regular_button, Qt.LeftButton)

    assert not window.regular_panes[0].video_view._marker_mode
    assert second_pane.video_view._marker_mode
    window.close()


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


def test_recording_start_and_reconnect_keep_dispatching_qt(qapp, tmp_path, monkeypatch):
    window = _window(tmp_path)
    gui_thread = threading.get_ident()
    entered, release = threading.Event(), threading.Event()
    calls = []
    original_start, original_stop = _FakeRecorder.start, _FakeRecorder.stop

    def blocked_start(recorder):
        calls.append(("start", threading.get_ident()))
        release.clear()
        entered.set()
        assert release.wait(2), "recording start blocked Qt dispatch"
        return original_start(recorder)

    def blocked_stop(recorder):
        calls.append(("stop", threading.get_ident()))
        release.clear()
        entered.set()
        assert release.wait(2), "recording stop blocked Qt dispatch"
        return original_stop(recorder)

    def tick():
        if entered.is_set():
            entered.clear()
            release.set()

    timer = QTimer()
    timer.timeout.connect(tick)
    timer.start(5)
    monkeypatch.setattr(_FakeRecorder, "start", blocked_start)
    monkeypatch.setattr(_FakeRecorder, "stop", blocked_stop)
    try:
        window.start_recording()
        window._restart_recording_camera(1, window.source)
        window.stop_recording()
        assert [kind for kind, _thread in calls] == ["start", "stop", "start", "stop"]
        assert all(thread != gui_thread for _kind, thread in calls)
        assert len({thread for _kind, thread in calls}) == 1
    finally:
        release.set()
        timer.stop()
        window.close()


def test_recording_watchdog_uses_monotonic_segment_progress(qapp, tmp_path):
    window = _window(tmp_path)
    window.start_recording()
    window._camera_segment_progress.clear()

    assert window._recording_stall_error(1, now=100.0) == ""
    assert window._recording_stall_error(1, now=159.0) == ""
    assert "60 秒没有产生新片段" in window._recording_stall_error(
        1,
        now=160.0,
    )

    ring_buffer = window._ring_buffer
    ring_buffer._segment_revision += 1
    assert window._recording_stall_error(1, now=161.0) == ""
    window.close()


def test_stalled_live_recorder_is_stopped_before_replacement(
    qapp,
    tmp_path,
    monkeypatch,
):
    window = _window(tmp_path)
    window.start_recording()
    original = _FakeRecorder.instances[0]
    signature = window._ring_buffer.segment_revision
    window._camera_segment_progress[1] = (signature, 939.0)
    monkeypatch.setattr(review_window_module.time, "monotonic", lambda: 1_000.0)

    window._refresh_capture_windows()

    replacement = _FakeRecorder.instances[-1]
    assert len(_FakeRecorder.instances) == 2
    assert original.stopped
    assert not original.is_running
    assert replacement is window._recorders[1]
    assert replacement.is_running
    window.close()


def test_explicit_stop_does_not_restart_stalled_camera(qapp, tmp_path, monkeypatch):
    window = _window(tmp_path)
    window.start_recording()
    recorder = _FakeRecorder.instances[0]
    reconnect_calls = []
    monkeypatch.setattr(
        window,
        "_restart_recording_camera",
        lambda *args: reconnect_calls.append(args),
    )
    recorder.stop()
    recorder.check_error = lambda: "process exited"

    window.stop_recording()
    window._poll_recording_health(now=time.monotonic() + 120)

    assert reconnect_calls == []
    window.close()


def test_failed_secondary_camera_reconnects_without_stopping_primary(qapp, tmp_path):
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
    primary, failed_secondary = _FakeRecorder.instances
    previous_secondary_buffer = window._ring_buffers[2]
    generation = window._video_scan_generation
    primary_scan_token = window._video_scan_tokens[1]
    failed_secondary.is_running = False
    failed_secondary.check_error = lambda: "connection lost"

    window._refresh_capture_windows()

    replacement = _FakeRecorder.instances[-1]
    assert len(_FakeRecorder.instances) == 3
    assert window._recorders[1] is primary
    assert primary.is_running
    assert window._recorders[2] is replacement
    assert replacement.is_running
    assert window._ring_buffers[2] is not previous_secondary_buffer
    assert window._video_scan_generation == generation
    assert window._video_scan_tokens[1] is primary_scan_token
    assert not window._camera_reconnect_errors
    assert window.camera_status_label.text() == "录像设备: 全部已连接"
    window.close()


def test_failed_camera_reconnect_uses_backoff_and_keeps_other_camera_running(
    qapp,
    tmp_path,
):
    class _ReconnectFailureRecorder(_FakeRecorder):
        fail_reconnect = False

        def start(self):
            if self.camera_index == 2 and type(self).fail_reconnect:
                raise RuntimeError("camera still offline")
            return super().start()

    _ReconnectFailureRecorder.instances.clear()
    window = FinishReviewWindow(
        "rtsp://camera-one/live",
        tmp_path,
        secondary_source="rtsp://camera-two/live",
        passage_host="127.0.0.1",
        passage_port=18765,
        recorder_factory=_ReconnectFailureRecorder,
        receiver_factory=_FakeReceiver,
    )
    window.start_recording()
    primary, failed_secondary = _ReconnectFailureRecorder.instances
    failed_secondary.is_running = False
    failed_secondary.check_error = lambda: "connection lost"
    _ReconnectFailureRecorder.fail_reconnect = True

    window._refresh_capture_windows()
    instance_count = len(_ReconnectFailureRecorder.instances)
    window._refresh_capture_windows()

    assert primary.is_running
    assert window._recorders[1] is primary
    assert window._recorders[2] is failed_secondary
    assert len(_ReconnectFailureRecorder.instances) == instance_count
    assert 2 in window._camera_reconnect_errors
    assert window.camera_status_label.text() == "录像设备: 自动重连中"
    assert "2秒后重试" in window.camera_status_label.toolTip()
    window.close()


def test_auth_failure_pauses_retries_without_stopping_passage_intake(qapp, tmp_path):
    window = _window(tmp_path)
    window.start_receiver()
    window.start_recording()
    failed = _FakeRecorder.instances[-1]
    failed.is_running = False
    failed.check_error = lambda: "Server returned 401 Unauthorized (authorization failed)"
    event = _event(passage_timestamp_ms=int(time.time() * 1000), received_at_ms=int(time.time() * 1000))
    try:
        # A fresh passage may arrive before the health timer notices the failure.
        window.receiver.deliver(event)
        for offset in (1, 120, 3600):
            window._poll_recording_health(now=time.monotonic() + offset)
        window._update_runtime_status()

        assert len(_FakeRecorder.instances) == 1
        assert window.passage_store.get(event.event_id) is not None
        assert window.receiver.is_running
        assert window.camera_status_label._detail_label.text() == "认证失败"
        assert "自动重试已暂停" in window.camera_status_label.toolTip()

        metadata = RaceMetadata(
            race_id="race-1", stage_id="stage-1", revision=1, emitted_at_ms=1,
            groups=(RaceGroupMetadata(group_id="men-open", name="男子公开组"),),
        )
        assert not window._auto_start_recording_for_metadata(metadata)
        assert len(_FakeRecorder.instances) == 1

        # An explicit retry after correcting the device is still possible.
        window.start_recording()
        assert len(_FakeRecorder.instances) == 2
        assert window._recorders[1].is_running
        assert not window._camera_auth_failed_sources
    finally:
        window.close()


@pytest.mark.parametrize("secondary", [False, True])
def test_rtsp_probe_ignores_result_after_credentials_change(qapp, tmp_path, secondary):
    dialog = FinishReviewLaunchDialog(
        FinishReviewSettings(
            source="rtsp://user:old@camera-one/live", output_dir=tmp_path,
            secondary_source="rtsp://user:old@camera-two/live",
            passage_host="127.0.0.1", passage_port=18765, camera_index=1,
        ),
        device_provider=lambda: (),
    )
    prefix = "secondary_rtsp" if secondary else "rtsp"
    try:
        current = getattr(dialog, f"_current_{prefix}_source")()
        setattr(dialog, f"_{prefix}_probe_source", current)
        getattr(dialog, f"{prefix}_password_edit").setText("corrected")
        getattr(dialog, f"_on_{prefix}_probe_finished")(False, "401 Unauthorized")
        assert "401" not in dialog.camera_status_label.text()
        assert not getattr(dialog, f"_{prefix}_probe_message")
    finally:
        dialog.close()


@pytest.mark.parametrize(
    ("detail", "expected"),
    [
        ("method DESCRIBE failed: 401 (Unauthorized)", "认证失败（401）"),
        ("Connection refused", "端口拒绝连接"),
        ("Connection timed out", "连接超时"),
    ],
)
def test_rtsp_probe_error_is_concise_and_hides_credentials(detail, expected):
    raw = detail + "\nError opening rtsp://admin:private-password@camera/live"
    result = review_window_module._format_rtsp_probe_error(raw)
    assert expected in result
    assert "private-password" not in result
    assert "\n" not in result


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


def test_window_stop_retries_while_recorder_is_still_running(qapp, tmp_path):
    class _StubbornRecorder(_FakeRecorder):
        allow_stop = False

        def stop(self):
            if not type(self).allow_stop:
                raise OSError("device driver is stuck")
            super().stop()

    _StubbornRecorder.instances.clear()
    window = FinishReviewWindow(
        "rtsp://camera/live",
        tmp_path,
        passage_host="127.0.0.1",
        passage_port=18765,
        recorder_factory=_StubbornRecorder,
        receiver_factory=_FakeReceiver,
    )
    window.start_recording()
    recorder = _StubbornRecorder.instances[0]

    assert not window.stop()
    assert recorder.is_running
    assert window._recorders[1] is recorder

    _StubbornRecorder.allow_stop = True
    assert window.stop()
    assert not recorder.is_running
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
    window._refresh_capture_windows()
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
    window._refresh_capture_windows()
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

    assert window.windowTitle() == "FinishReview v0.25.6 · 终点多源复核"
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
    assert window.product_title_label.text() == "FinishReview v0.25.6"
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
    assert window.beijing_clock_label.minimumWidth() >= 72
    assert window.beijing_zone_label.minimumWidth() >= 64
    assert (
        window.beijing_clock_label.sizePolicy().horizontalPolicy()
        == QSizePolicy.Fixed
    )
    assert (
        window.beijing_zone_label.sizePolicy().horizontalPolicy()
        == QSizePolicy.Fixed
    )
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
    assert window.runtime_status_strip.isHidden()

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


def test_periodic_capture_refresh_keeps_one_context_generation(
    qapp,
    tmp_path,
    monkeypatch,
):
    window = _window(tmp_path)
    window.start_recording()
    requests = []
    monkeypatch.setattr(window._capture_refresh_worker, "submit", requests.append)

    window._request_capture_refresh()
    window._request_capture_refresh()

    assert len(requests) == 2
    assert requests[0].generation == requests[1].generation
    assert requests[0].ring_buffers == (window._ring_buffer,)
    window.close()


def test_stale_capture_refresh_result_is_not_applied(qapp, tmp_path, monkeypatch):
    from realtime.capture_refresh import CaptureRefreshResult
    from realtime.evidence_pipeline import EvidenceSnapshot

    window = _window(tmp_path)
    window.start_recording()
    applied = []
    monkeypatch.setattr(
        window,
        "_apply_evidence_snapshot",
        lambda snapshot: applied.append(snapshot) or set(),
    )

    snapshot = EvidenceSnapshot((), (), ())
    window._on_capture_refresh_finished(
        CaptureRefreshResult(window._capture_refresh_generation - 1, evidence=snapshot)
    )
    window._on_capture_refresh_finished(
        CaptureRefreshResult(window._capture_refresh_generation, evidence=snapshot)
    )

    assert applied == [snapshot]
    window.close()


def test_stale_queued_video_candidates_do_not_enter_new_workspace(qapp, tmp_path):
    from realtime.video_arrival import VideoArrivalCandidateStore

    window = _window(tmp_path)
    video_path = tmp_path / "candidate.ts"
    video_path.write_bytes(b"video")
    candidate = VideoPassageCandidate(
        "candidate-old-workspace",
        1,
        1_000,
        1_200,
        1_100,
        0.2,
        0.1,
        segment_id="segment-old",
        video_path=str(video_path),
    )
    old_store = window.video_arrival_store
    generation = window._video_scan_generation
    delivery_thread = threading.Thread(
        target=window._on_video_candidates,
        args=((candidate,), generation),
    )
    delivery_thread.start()
    delivery_thread.join(timeout=2)
    assert not delivery_thread.is_alive()
    assert old_store.candidates() == (candidate,)

    window._video_scan_generation += 1
    window.video_arrival_store = VideoArrivalCandidateStore(
        tmp_path / "new_workspace_candidates.jsonl"
    )
    window._video_candidate_cache.clear()
    qapp.processEvents()

    assert window.video_arrival_store.candidates() == ()
    assert "candidate-old-workspace" not in window._video_candidate_cache
    window.close()


def test_video_anomaly_queue_accumulates_and_can_be_closed_as_verified(
    qapp,
    tmp_path,
    monkeypatch,
):
    opened = []

    class _PlaybackDialog(QDialog):
        def __init__(self, video_path, parent, **kwargs):
            super().__init__(parent)
            opened.append((video_path, kwargs))

    monkeypatch.setattr(review_window_module, "VideoPlaybackDialog", _PlaybackDialog)
    window = _window(tmp_path)
    video_path = tmp_path / "camera_01.ts"
    video_path.write_bytes(b"video")
    candidate = VideoPassageCandidate(
        "segment-1:video-1",
        1,
        1_000,
        1_500,
        1_200,
        0.3,
        0.2,
        video_path=str(video_path),
        video_position_ms=200,
    )
    item = VideoPassageReconciliation(
        candidate,
        chip_count=0,
        anomaly="视频候选无芯片记录",
    )

    requested = []
    window.video_candidate_requested.connect(requested.append)
    window.set_video_reconciliation((item,))
    window.set_video_reconciliation((item,))

    assert len(window.video_reconciliation()) == 1
    assert window.video_review_button.text() == "视频异常：1"
    window.video_review_button.click()
    dialog = window.findChild(QDialog, "videoAnomalyDialog")
    assert dialog is not None
    table = dialog.findChild(QTableWidget, "videoAnomalyTable")
    assert table is not None and table.rowCount() == 1
    table.selectRow(0)
    open_button = dialog.findChild(QPushButton, "videoAnomalyOpenButton")
    assert open_button is not None
    open_button.click()
    assert requested == [item]
    assert opened[0][0] == video_path and opened[0][1]["target_position_ms"] == 200
    window.video_review_done_button.click()
    assert window.video_review_status(candidate.candidate_id) == "verified"
    assert window.video_reconciliation() == ()
    window.close()


def test_video_arrival_candidates_persist_across_workspace_reopen(
    qapp,
    tmp_path,
):
    video_path = tmp_path / "camera_01.ts"
    video_path.write_bytes(b"video")
    candidate = VideoPassageCandidate(
        "segment-1:video-arrival-1",
        1,
        1_000,
        1_300,
        1_200,
        0.3,
        0.2,
        segment_id="segment-1",
        video_path=str(video_path),
        video_position_ms=1_200,
    )
    window = _window(tmp_path)

    window._reconcile_video_candidates((candidate,))

    assert (tmp_path / "video_arrival_candidates.jsonl").is_file()
    assert [item.candidate_id for item in window.video_navigation_candidates()] == [
        candidate.candidate_id
    ]
    window.close()

    restored = _window(tmp_path)
    assert [item.candidate_id for item in restored.video_navigation_candidates()] == [
        candidate.candidate_id
    ]
    assert restored.video_arrival_button.text() == "到达候选：1批/1点"
    restored.close()


def test_background_video_candidate_does_not_change_current_passage(
    qapp,
    tmp_path,
    monkeypatch,
):
    window = _window(tmp_path)
    window.passage_store.append(
        _event(
            event_id="passage-149",
            sequence=149,
            bib="149",
            passage_timestamp_ms=1_000,
        )
    )
    window.passage_store.append(
        _event(
            event_id="passage-67",
            sequence=67,
            bib="67",
            passage_timestamp_ms=2_000,
        )
    )
    window.refresh()
    selected_event_id = window._selected_event_id
    selected_row = window.table.currentRow()
    focused = []
    monkeypatch.setattr(
        window,
        "_focus_video_candidate_in_review",
        lambda candidate: focused.append(candidate) or True,
    )
    candidate = VideoPassageCandidate(
        "visual:candidate-1",
        1,
        1_900,
        2_100,
        2_000,
        0.3,
        0.1,
    )

    window._reconcile_video_candidates((candidate,))
    qapp.processEvents()

    assert focused == []
    assert window._selected_event_id == selected_event_id
    assert window.table.currentRow() == selected_row
    window.close()


def test_archive_video_scan_is_manual_and_returns_to_idle_after_completion(
    qapp,
    tmp_path,
    monkeypatch,
):
    from realtime import video_passage_detector as detector_module

    video_path = tmp_path / "camera_01.ts"
    video_path.write_bytes(b"video")
    VideoTimelineStore(tmp_path / "video_timeline.jsonl").add_completed_segment(
        source_id="camera_01",
        camera_index=1,
        video_path=video_path,
        media_started_at_ms=1_000,
        media_duration_ms=2_000,
        clock_source=DEFAULT_CLOCK_SOURCE,
        timing_error_ms=0,
        end_reason="test",
        race_id="race-1",
    )

    class _ScanWorker:
        instances = []

        def __init__(self, provider, callback, **kwargs):
            self.provider = provider
            self.callback = callback
            self.kwargs = kwargs
            self.is_running = False
            self.stop_calls = []
            type(self).instances.append(self)

        def start(self):
            self.is_running = True

        def stop(self, timeout=2.0):
            self.stop_calls.append(timeout)
            self.is_running = False

    monkeypatch.setattr(detector_module, "VideoPassageScanWorker", _ScanWorker)
    window = _window(tmp_path)

    assert _ScanWorker.instances == []
    assert window._archive_video_scan_workers == {}

    window._workspace_mode = "archive"
    window._update_runtime_status()
    assert not window.archive_scan_button.isHidden()
    assert window.archive_scan_button.text() == "分析历史视频"

    window.archive_scan_button.click()
    qapp.processEvents()
    worker = _ScanWorker.instances[-1]
    assert worker.is_running
    assert worker.kwargs["continuous"] is False
    assert window.archive_scan_button.text() == "停止视频分析"

    worker.is_running = False
    window._update_runtime_status()
    assert window._archive_video_scan_workers == {}
    assert worker.stop_calls == [0.1]
    assert window.archive_scan_button.text() == "分析历史视频"
    window.close()


def test_video_anomaly_list_refreshes_status_while_open(qapp, tmp_path):
    window = _window(tmp_path)
    video_path = tmp_path / "camera_01.ts"
    video_path.write_bytes(b"video")
    candidate = VideoPassageCandidate(
        "segment-1:video-1",
        1,
        1_000,
        1_500,
        1_200,
        0.3,
        0.2,
        video_path=str(video_path),
        video_position_ms=200,
    )
    item = VideoPassageReconciliation(candidate, 0, "瑙嗛鍊欏緟澶嶆牳")
    window.set_video_reconciliation((item,))
    window.video_review_button.click()
    dialog = window.findChild(QDialog, "videoAnomalyDialog")
    table = dialog.findChild(QTableWidget, "videoAnomalyTable")
    assert table.item(0, 6).text() == window._video_review_status_label("pending")

    window._active_video_anomaly_id = candidate.candidate_id
    window._mark_current_video_anomaly("verified")

    assert table.item(0, 6).text() == window._video_review_status_label("verified")
    window.close()


def test_video_scan_workers_pause_with_nested_playback_tokens(qapp, tmp_path):
    window = _window(tmp_path)

    class _Worker:
        def __init__(self):
            self.pause_calls = 0
            self.resume_calls = 0

        def pause(self):
            self.pause_calls += 1

        def resume(self):
            self.resume_calls += 1

        def stop(self):
            pass

    worker = _Worker()
    window._video_scan_workers = {1: worker}

    first = window._pause_video_scan_workers()
    second = window._pause_video_scan_workers()
    assert worker.pause_calls == 1

    window._resume_video_scan_workers(first)
    assert worker.resume_calls == 0
    window._resume_video_scan_workers(second)
    assert worker.resume_calls == 1

    window.close()


def test_finish_line_roi_is_persisted_per_event_workspace(qapp, tmp_path):
    window = _window(tmp_path)
    window.set_finish_line_roi(2, (0.2, 0.1, 0.4, 0.9))
    window.close()

    restored = _window(tmp_path)
    assert restored._finish_line_rois[2] == pytest.approx((0.2, 0.1, 0.4, 0.9))
    restored.close()


def test_video_anomaly_can_quickly_focus_roster_bib_without_creating_passage(
    qapp,
    tmp_path,
):
    window = _window(tmp_path)
    metadata = RaceMetadata(
        race_id="race-1",
        stage_id="stage-1",
        revision=1,
        emitted_at_ms=1,
        groups=(RaceGroupMetadata("men-open", "男子公开组"),),
        athletes=(
            RaceAthleteMetadata(
                athlete_id="321",
                bib="321",
                name="运动员321",
                team_name="示例队",
                group_id="men-open",
            ),
        ),
    )
    window.metadata_store.store(metadata)
    candidate = VideoPassageCandidate(
        "video-321", 1, 1_000, 1_200, 1_100, 0.2, 0.1
    )
    item = VideoPassageReconciliation(candidate, 0, "视频候选无芯片记录")
    window.set_video_reconciliation((item,))
    window.video_review_button.click()
    dialog = window.findChild(QDialog, "videoAnomalyDialog")
    assert dialog is not None
    table = dialog.findChild(QTableWidget, "videoAnomalyTable")
    assert table is not None and table.rowCount() == 1
    table.selectRow(0)
    open_button = dialog.findChild(QPushButton, "videoAnomalyOpenButton")
    assert open_button is not None
    open_button.click()
    window.video_review_bib_edit.setText("321")
    window.video_review_bib_edit.returnPressed.emit()

    assert window._video_review_bibs[candidate.candidate_id] == "321"
    assert window.selected_identity_value.text() == "321"
    assert window.passage_store.events() == ()
    window.close()
