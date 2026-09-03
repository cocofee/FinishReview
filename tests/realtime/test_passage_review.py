import json
import os
import time
from dataclasses import replace
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtCore import QEvent, QObject, QPoint, QPointF, Qt, pyqtSignal
from PyQt5.QtGui import QImage, QMouseEvent, QPalette
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QHeaderView,
    QPushButton,
    QSpinBox,
    QTableWidget,
)

import realtime.passage_review as passage_review
from realtime.passage_evidence import (
    HIGH_SPEED_SOURCE,
    REGULAR_SOURCE,
    PassageEvidenceAssociation,
    PassageEvidenceAssociationStore,
    VideoClockCalibrationStore,
)
from realtime.passage_receiver import PassageEvent, PassageEventStore
from realtime.passage_review import (
    PassageEvidencePane,
    PassageReviewDialog,
    lookup_status_text,
)
from realtime.race_metadata import (
    RaceAthleteMetadata,
    RaceGroupMetadata,
    RaceMetadata,
    RaceMetadataStore,
)
from realtime.review_clip import PassageReviewBindingStore
from realtime.video_timeline import VideoTimelineStore
from realtime.video_passage_detector import VideoPassageCandidate


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _wait_until(predicate, *, timeout_ms=1_000):
    deadline = time.monotonic() + max(0, int(timeout_ms)) / 1_000.0
    while time.monotonic() < deadline:
        if predicate():
            return True
        QTest.qWait(10)
    return predicate()


class _FakePlaybackWorker(QObject):
    metadata_ready = pyqtSignal(int, float, int, int, int)
    frame_ready = pyqtSignal(object, int, int)
    full_resolution_ready = pyqtSignal(object, int, int)
    playback_finished = pyqtSignal()
    step_boundary_reached = pyqtSignal(int)
    playback_error = pyqtSignal(str)
    finished = pyqtSignal()
    instances = []

    def __init__(self, video_path, parent=None, *, idle_prefetch=True):
        super().__init__(parent)
        self.video_path = Path(video_path)
        self.running = False
        self.start_calls = 0
        self.stopped = False
        self.seek_calls = []
        self.preview_seek_calls = []
        self.pause_calls = 0
        self.wait_calls = []
        self.speed_calls = []
        self.step_calls = []
        self.full_resolution_calls = []
        self.release_cache_calls = 0
        self.park_cache_calls = 0
        self.idle_prefetch_calls = [bool(idle_prefetch)]
        type(self).instances.append(self)

    def start(self):
        self.start_calls += 1
        self.running = True
        fps = 250.0 if "high_speed" in self.video_path.name else 50.0
        self.metadata_ready.emit(60_000, fps, 2560, 1440, int(60 * fps))

    def isRunning(self):
        return self.running

    def pause(self):
        self.pause_calls += 1

    def seek(self, position_ms):
        self.seek_calls.append(int(position_ms))

    def seek_and_play(self, position_ms, speed=1.0):
        self.seek_calls.append(int(position_ms))
        self.speed_calls.append(float(speed))

    def seek_preview(self, position_ms):
        self.preview_seek_calls.append(int(position_ms))

    def set_shuttle_speed(self, speed):
        self.speed_calls.append(float(speed))

    def step(self, frame_delta):
        self.step_calls.append(int(frame_delta))

    def request_full_resolution(self, frame_index=None):
        self.full_resolution_calls.append(frame_index)

    def release_cache(self):
        self.release_cache_calls += 1

    def park_cache(self):
        self.park_cache_calls += 1

    def set_idle_prefetch_enabled(self, enabled):
        self.idle_prefetch_calls.append(bool(enabled))

    def stop(self):
        self.stopped = True
        if self.running:
            self.running = False
            self.finished.emit()

    def request_stop(self):
        self.stop()

    def wait(self, timeout_ms):
        self.wait_calls.append(int(timeout_ms))
        return True


@pytest.fixture
def fake_playback(monkeypatch):
    _FakePlaybackWorker.instances.clear()
    monkeypatch.setattr(passage_review, "VideoPlaybackWorker", _FakePlaybackWorker)
    return _FakePlaybackWorker


def _event(
    passage_time_ms=15_000,
    passage_timestamp_ms=None,
    *,
    event_id="passage-1",
    sequence=1,
    group_id="men-open",
    bib="23",
    chip_id="chip-23",
    race_name="",
    stage_name="",
    group_name="",
    athlete_name="",
    team_name="",
    athlete_id="",
    revision=1,
    is_active=True,
):
    return PassageEvent(
        event_id=event_id,
        race_id="race-1",
        stage_id="stage-1",
        group_id=group_id,
        sequence=sequence,
        chip_id=chip_id,
        bib=bib,
        passage_time_ms=passage_time_ms,
        lap=2,
        emitted_at_ms=passage_time_ms + 100,
        passage_timestamp_ms=passage_timestamp_ms,
        race_name=race_name,
        stage_name=stage_name,
        group_name=group_name,
        athlete_name=athlete_name,
        team_name=team_name,
        athlete_id=athlete_id,
        revision=revision,
        is_active=is_active,
    )


def _add_segment(
    timeline_store,
    video_path,
    *,
    source_id,
    camera_index,
    started_at_ms,
    ended_at_ms,
    clock_source="videopipe_system_clock",
    timing_error_ms=1_000,
    race_id="race-1",
    media_duration_ms=None,
    media_started_at_ms=None,
):
    video_path.parent.mkdir(parents=True, exist_ok=True)
    video_path.write_bytes(b"video")
    segment = timeline_store.start_segment(
        source_id=source_id,
        camera_index=camera_index,
        video_path=video_path,
        started_at_ms=started_at_ms,
        clock_source=clock_source,
        timing_error_ms=timing_error_ms,
        race_id=race_id,
    )
    timeline_store.finish_segment(
        segment.segment_id,
        ended_at_ms=ended_at_ms,
        media_duration_ms=(
            ended_at_ms - started_at_ms
            if media_duration_ms is None
            else media_duration_ms
        ),
        media_started_at_ms=(
            started_at_ms
            if media_started_at_ms is None
            else media_started_at_ms
        ),
    )
    return segment


def test_empty_preview_keeps_time_filmstrip_visible(qapp, tmp_path):
    dialog = PassageReviewDialog(
        PassageEventStore(tmp_path / "passages.jsonl"),
        VideoTimelineStore(tmp_path / "video_timeline.jsonl"),
    )
    dialog.show()
    qapp.processEvents()

    assert dialog.video_filmstrip.isVisible()
    dialog.close()


def test_review_uses_one_row_per_passage_and_opens_regular_video(
    qapp,
    tmp_path,
    fake_playback,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(_event(passage_time_ms=5_000, passage_timestamp_ms=15_000))
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    video_path = tmp_path / "videos" / "camera_01.mkv"
    _add_segment(
        timeline_store,
        video_path,
        source_id="camera_01",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=20_000,
    )
    opened = []

    dialog = PassageReviewDialog(
        passage_store,
        timeline_store,
        clock_offset_ms=500,
        pre_roll_ms=3_000,
        open_location=lambda event, location: opened.append((event, location)),
    )
    qapp.processEvents()

    assert dialog.table.rowCount() == 1
    assert dialog.table.item(0, 6).text() == "未确认"
    assert dialog.table.item(0, 7).text() == "未确认"
    assert dialog.table.item(0, 7).foreground().color().name() == "#c0372b"
    assert dialog.table.item(0, 8).text() == "未确认"
    assert dialog.regular_pane.location.video_path == video_path.absolute()
    assert dialog.high_speed_pane.location is None
    assert fake_playback.instances[0].seek_calls == [2_500]

    dialog.regular_pane.open_btn.click()
    assert opened[0][0].event_id == "passage-1"
    assert opened[0][1].passage_position_ms == 5_500
    assert opened[0][1].playback_position_ms == 2_500
    dialog.close()


def test_review_shows_two_regular_camera_locations_side_by_side(
    qapp,
    tmp_path,
    fake_playback,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(_event(passage_time_ms=15_000))
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    first_path = tmp_path / "videos" / "camera_01.mkv"
    second_path = tmp_path / "videos" / "camera_02.mkv"
    _add_segment(
        timeline_store,
        first_path,
        source_id="camera_01",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=20_000,
    )
    _add_segment(
        timeline_store,
        second_path,
        source_id="camera_02",
        camera_index=2,
        started_at_ms=10_000,
        ended_at_ms=20_000,
    )

    dialog = PassageReviewDialog(passage_store, timeline_store)
    qapp.processEvents()

    assert len(dialog.regular_panes) == 2
    first_pane, second_pane = dialog.regular_panes
    assert first_pane.title_label.text() == "机位 1"
    assert second_pane.title_label.text() == "机位 2"
    assert first_pane.camera_combo.isVisible() is False
    assert second_pane.camera_combo.isVisible() is False
    assert first_pane.location.video_path == first_path.absolute()
    assert second_pane.location.video_path == second_path.absolute()
    first_worker, second_worker = fake_playback.instances
    assert first_worker.start_calls == 1
    assert second_worker.start_calls == 0
    assert first_worker.idle_prefetch_calls[-1] is True
    assert second_worker.idle_prefetch_calls[-1] is False

    assert _wait_until(
        lambda: second_worker.start_calls == 1,
        timeout_ms=dialog.INACTIVE_CAMERA_START_DELAY_MS + 500,
    )

    preview = QImage(1280, 720, QImage.Format_RGB888)
    preview.fill(0)
    first_worker.frame_ready.emit(preview, 5_000, 250)
    qapp.processEvents()

    assert second_worker.start_calls == 1

    second_pane.step_requested.emit(1)
    qapp.processEvents()

    assert first_worker.idle_prefetch_calls[-1] is False
    assert second_worker.idle_prefetch_calls[-1] is True
    dialog.show()
    qapp.processEvents()
    assert first_pane.isVisible()
    assert second_pane.isVisible()
    assert dialog.high_speed_pane.isHidden()
    dialog.close()


def test_review_uses_direct_clip_binding_without_timeline_scan(
    qapp,
    tmp_path,
    fake_playback,
    monkeypatch,
):
    event = _event(passage_time_ms=5_000, passage_timestamp_ms=15_000)
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(event)
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    video_path = tmp_path / "review_buffer" / "camera_01" / "shared.mkv"
    segment = _add_segment(
        timeline_store,
        video_path,
        source_id="camera_01_review",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=20_000,
    )
    binding_store = PassageReviewBindingStore(tmp_path / "review_clips.jsonl")
    clip = binding_store.get_or_add_clip(
        race_id="race-1",
        camera_index=1,
        source_id="camera_01_review",
        started_at_ms=10_000,
        ended_at_ms=20_000,
        playlist_path=video_path,
        segment_signature="shared-segments",
        timeline_segment_id=segment.segment_id,
    )
    binding_store.bind(
        event_id=event.event_id,
        revision=event.revision,
        camera_index=1,
        clip_id=clip.clip_id,
        passage_timestamp_ms=15_000,
        passage_offset_ms=5_000,
    )
    monkeypatch.setattr(
        timeline_store,
        "locate_passage",
        lambda *_args, **_kwargs: pytest.fail("direct binding must skip timeline scan"),
    )

    dialog = PassageReviewDialog(
        passage_store,
        timeline_store,
        clock_offset_ms=500,
        regular_camera_indexes=(1,),
        show_high_speed_pane=False,
        review_binding_store=binding_store,
    )
    qapp.processEvents()

    location = dialog.regular_pane.location
    assert location.video_path == video_path.absolute()
    assert location.passage_position_ms == 5_500
    assert location.media_locator == clip.clip_id
    assert fake_playback.instances[0].media_locator == clip.clip_id
    dialog.close()


def test_switching_events_in_same_clip_reuses_playback_worker(
    qapp,
    tmp_path,
    fake_playback,
):
    first_event = _event(
        event_id="passage-1",
        sequence=1,
        bib="1",
        passage_timestamp_ms=15_000,
    )
    second_event = _event(
        event_id="passage-2",
        sequence=2,
        bib="2",
        passage_timestamp_ms=16_000,
    )
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(first_event)
    passage_store.append(second_event)
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    video_path = tmp_path / "review_buffer" / "camera_01" / "shared.mkv"
    segment = _add_segment(
        timeline_store,
        video_path,
        source_id="camera_01_review",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=20_000,
    )
    binding_store = PassageReviewBindingStore(tmp_path / "review_clips.jsonl")
    clip = binding_store.get_or_add_clip(
        race_id="race-1",
        camera_index=1,
        source_id="camera_01_review",
        started_at_ms=10_000,
        ended_at_ms=20_000,
        playlist_path=video_path,
        segment_signature="shared-segments",
        timeline_segment_id=segment.segment_id,
    )
    for event, offset_ms in ((first_event, 5_000), (second_event, 6_000)):
        binding_store.bind(
            event_id=event.event_id,
            revision=event.revision,
            camera_index=1,
            clip_id=clip.clip_id,
            passage_timestamp_ms=event.timeline_timestamp_ms,
            passage_offset_ms=offset_ms,
        )
    dialog = PassageReviewDialog(
        passage_store,
        timeline_store,
        regular_camera_indexes=(1,),
        show_high_speed_pane=False,
        review_binding_store=binding_store,
    )
    qapp.processEvents()
    worker = fake_playback.instances[0]

    dialog._move_selection(1)
    qapp.processEvents()

    assert len(fake_playback.instances) == 1
    assert dialog.regular_pane._worker is worker
    assert worker.seek_calls[-1] == 6_000
    dialog.close()


def test_switching_athletes_starts_the_active_regular_camera_first(
    qapp,
    tmp_path,
    fake_playback,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(
        _event(event_id="passage-1", sequence=1, passage_time_ms=15_000)
    )
    passage_store.append(
        _event(event_id="passage-2", sequence=2, passage_time_ms=25_000)
    )
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    for camera_index in (1, 2):
        _add_segment(
            timeline_store,
            tmp_path / "videos" / f"camera_{camera_index:02d}_first.mkv",
            source_id=f"camera_{camera_index:02d}_first",
            camera_index=camera_index,
            started_at_ms=10_000,
            ended_at_ms=20_000,
        )
        _add_segment(
            timeline_store,
            tmp_path / "videos" / f"camera_{camera_index:02d}_second.mkv",
            source_id=f"camera_{camera_index:02d}_second",
            camera_index=camera_index,
            started_at_ms=20_000,
            ended_at_ms=30_000,
        )

    dialog = PassageReviewDialog(passage_store, timeline_store)
    qapp.processEvents()
    preview = QImage(1280, 720, QImage.Format_RGB888)
    preview.fill(0)
    first_active, first_inactive = fake_playback.instances
    first_active.frame_ready.emit(preview, 5_000, 250)
    qapp.processEvents()
    assert first_inactive.start_calls == 1

    dialog._move_selection(1)
    qapp.processEvents()

    second_active, second_inactive = fake_playback.instances[-2:]
    assert second_active.start_calls == 1
    assert second_inactive.start_calls == 0

    second_active.frame_ready.emit(preview, 5_000, 250)
    qapp.processEvents()

    assert second_inactive.start_calls == 1
    dialog.close()


def test_rapid_athlete_switch_cancels_the_previous_inactive_camera_start(
    qapp,
    tmp_path,
    fake_playback,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(
        _event(event_id="passage-1", sequence=1, passage_time_ms=15_000)
    )
    passage_store.append(
        _event(event_id="passage-2", sequence=2, passage_time_ms=25_000)
    )
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    for camera_index in (1, 2):
        _add_segment(
            timeline_store,
            tmp_path / "videos" / f"camera_{camera_index:02d}_first.mkv",
            source_id=f"camera_{camera_index:02d}_first",
            camera_index=camera_index,
            started_at_ms=10_000,
            ended_at_ms=20_000,
        )
        _add_segment(
            timeline_store,
            tmp_path / "videos" / f"camera_{camera_index:02d}_second.mkv",
            source_id=f"camera_{camera_index:02d}_second",
            camera_index=camera_index,
            started_at_ms=20_000,
            ended_at_ms=30_000,
        )

    dialog = PassageReviewDialog(passage_store, timeline_store)
    qapp.processEvents()
    first_active, first_inactive = fake_playback.instances

    dialog._move_selection(1)
    qapp.processEvents()
    second_active, second_inactive = fake_playback.instances[-2:]

    assert first_active.start_calls == 1
    assert first_inactive.start_calls == 0
    assert second_active.start_calls == 1
    assert second_inactive.start_calls == 0

    assert first_inactive.start_calls == 0
    assert _wait_until(
        lambda: second_inactive.start_calls == 1,
        timeout_ms=dialog.INACTIVE_CAMERA_START_DELAY_MS + 500,
    )
    dialog.close()


def test_regular_camera_opens_in_centered_system_window_and_restores(
    qapp,
    tmp_path,
):
    dialog = PassageReviewDialog(
        PassageEventStore(tmp_path / "passages.jsonl"),
        VideoTimelineStore(tmp_path / "video_timeline.jsonl"),
        regular_camera_indexes=(1, 2),
        show_high_speed_pane=False,
        include_recorded_evidence=False,
    )
    dialog.show()
    qapp.processEvents()

    assert dialog.windowFlags() & Qt.WindowMinMaxButtonsHint
    assert dialog.windowFlags() & Qt.WindowSystemMenuHint
    assert dialog.windowFlags() & Qt.WindowCloseButtonHint
    assert not dialog.windowFlags() & Qt.WindowContextHelpButtonHint
    first_pane, second_pane = dialog.regular_panes
    original_index = dialog.evidence_splitter.indexOf(second_pane)
    second_pane.video_view.setFocus()
    QTest.keyClick(second_pane.video_view.viewport(), Qt.Key_F)
    qapp.processEvents()

    window = dialog._maximized_window
    assert window is not None
    assert window.isVisible()
    assert window.windowFlags() & Qt.WindowMinMaxButtonsHint
    assert window.windowFlags() & Qt.WindowSystemMenuHint
    assert window.windowFlags() & Qt.WindowCloseButtonHint
    assert second_pane.parentWidget() is dialog._maximized_content_splitter
    assert second_pane.maximize_btn.text() == "缩小"
    assert first_pane.isVisible()
    assert dialog._maximized_mode_label.text() == "当前判读：机位 2"
    assert dialog._maximized_mode_buttons[2].isChecked()
    assert second_pane.active_badge.isVisible()
    assert not first_pane.active_badge.isVisible()

    QTest.keyClick(window, Qt.Key_B)
    qapp.processEvents()

    assert dialog._maximized_mode_buttons["side_by_side"].isChecked()
    assert first_pane.parentWidget() is dialog._maximized_content_splitter
    assert second_pane.parentWidget() is dialog._maximized_content_splitter
    assert first_pane.isVisible()
    assert second_pane.isVisible()

    QTest.keyClick(window, Qt.Key_1)
    qapp.processEvents()

    assert dialog._maximized_pane is first_pane
    assert dialog._maximized_mode_label.text() == "当前判读：机位 1"
    assert dialog._maximized_mode_buttons[1].isChecked()
    assert first_pane.parentWidget() is dialog._maximized_content_splitter
    assert second_pane.parentWidget() is dialog.evidence_splitter
    assert first_pane.active_badge.isVisible()
    assert not second_pane.active_badge.isVisible()

    QTest.keyClick(window, Qt.Key_Tab)
    qapp.processEvents()

    assert dialog._maximized_pane is second_pane
    assert dialog._maximized_mode_label.text() == "当前判读：机位 2"
    assert dialog._maximized_mode_buttons[2].isChecked()

    second_pane.video_view.setFocus()
    QTest.keyClick(second_pane.video_view.viewport(), Qt.Key_Escape)
    qapp.processEvents()

    assert dialog._maximized_window is None
    assert dialog._maximized_pane is None
    assert second_pane.parentWidget() is dialog.evidence_splitter
    assert dialog.evidence_splitter.indexOf(second_pane) == original_index
    assert first_pane.isVisible()
    assert second_pane.isVisible()

    QTest.keyClick(dialog, Qt.Key_Escape)
    qapp.processEvents()
    assert dialog.isVisible()
    dialog.close()


def test_maximized_camera_switch_reuses_existing_playback_workers(
    qapp,
    tmp_path,
    fake_playback,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(_event(passage_time_ms=15_000))
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    for camera_index in (1, 2):
        _add_segment(
            timeline_store,
            tmp_path / "videos" / f"camera_{camera_index:02d}.mkv",
            source_id=f"camera_{camera_index:02d}",
            camera_index=camera_index,
            started_at_ms=10_000,
            ended_at_ms=20_000,
        )
    dialog = PassageReviewDialog(passage_store, timeline_store)
    dialog.show()
    qapp.processEvents()
    first_pane, second_pane = dialog.regular_panes
    workers = tuple(fake_playback.instances)

    dialog._toggle_maximized_pane(first_pane)
    qapp.processEvents()
    window = dialog._maximized_window
    assert window is not None

    QTest.keyClick(window, Qt.Key_2)
    qapp.processEvents()

    assert tuple(fake_playback.instances) == workers
    assert first_pane._worker is workers[0]
    assert second_pane._worker is workers[1]
    assert dialog._active_pane is second_pane
    assert dialog._maximized_mode_label.text() == "当前判读：机位 2"

    QTest.keyClick(window, Qt.Key_B)
    qapp.processEvents()

    assert tuple(fake_playback.instances) == workers
    assert first_pane.parentWidget() is dialog._maximized_content_splitter
    assert second_pane.parentWidget() is dialog._maximized_content_splitter
    dialog.close()


def test_confirming_either_regular_camera_confirms_passage_and_marks_others_reference(
    qapp,
    tmp_path,
    fake_playback,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(_event(passage_time_ms=15_000, bib="23"))
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    first_segment = _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01.mkv",
        source_id="camera_01",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=20_000,
    )
    second_segment = _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_02.mkv",
        source_id="camera_02",
        camera_index=2,
        started_at_ms=10_000,
        ended_at_ms=20_000,
    )
    association_store = PassageEvidenceAssociationStore(
        tmp_path / "passage_evidence_associations.jsonl"
    )
    dialog = PassageReviewDialog(
        passage_store,
        timeline_store,
        association_store=association_store,
    )
    dialog.show()
    qapp.processEvents()
    first_pane, second_pane = dialog.regular_panes
    frame = QImage(1280, 720, QImage.Format_RGB888)
    frame.fill(0)
    fake_playback.instances[1].frame_ready.emit(frame, 5_000, 250)
    qapp.processEvents()

    QTest.mouseClick(
        second_pane.video_view.viewport(),
        Qt.LeftButton,
        pos=second_pane.video_view.viewport().rect().center(),
    )
    QTest.keyClick(second_pane.video_view, Qt.Key_Return)
    qapp.processEvents()

    association = association_store.get("passage-1", REGULAR_SOURCE)
    assert association is not None
    assert association.segment_id == second_segment.segment_id
    assert association.segment_id != first_segment.segment_id
    assert dialog.table.item(0, 8).text() == "已确认"
    assert second_pane.status_label.text() == "已确认"
    assert first_pane.status_label.text() == "参考"
    assert first_pane.association is None

    fake_playback.instances[0].frame_ready.emit(frame, 5_000, 250)
    qapp.processEvents()
    QTest.mouseClick(
        first_pane.video_view.viewport(),
        Qt.LeftButton,
        pos=first_pane.video_view.viewport().rect().center(),
    )
    QTest.keyClick(first_pane.video_view, Qt.Key_Return)
    qapp.processEvents()

    association = association_store.get("passage-1", REGULAR_SOURCE)
    assert association is not None
    assert association.segment_id == first_segment.segment_id
    assert first_pane.status_label.text() == "已确认"
    assert second_pane.status_label.text() == "参考"
    assert second_pane.association is None
    dialog.close()


@pytest.mark.parametrize(
    ("regular_camera_indexes", "show_high_speed", "expected_count"),
    [
        ((1,), False, 1),
        ((1,), True, 2),
        ((1, 2), False, 2),
        ((1, 2), True, 3),
    ],
)
def test_review_evidence_layout_follows_current_configuration(
    qapp,
    tmp_path,
    regular_camera_indexes,
    show_high_speed,
    expected_count,
):
    dialog = PassageReviewDialog(
        PassageEventStore(tmp_path / "passages.jsonl"),
        VideoTimelineStore(tmp_path / "video_timeline.jsonl"),
        regular_camera_indexes=regular_camera_indexes,
        show_high_speed_pane=show_high_speed,
        include_recorded_evidence=False,
    )

    assert tuple(pane.camera_index for pane in dialog.regular_panes) == (
        *regular_camera_indexes,
    )
    assert len(dialog.evidence_panes) == expected_count
    assert dialog.high_speed_pane.isHidden() is (not show_high_speed)
    dialog.close()


def test_realtime_review_does_not_restore_unconfigured_recorded_evidence(
    qapp,
    tmp_path,
    fake_playback,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    event = _event(passage_time_ms=15_000)
    passage_store.append(event)
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01.mkv",
        source_id="camera_01",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=20_000,
    )
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_02.mkv",
        source_id="camera_02",
        camera_index=2,
        started_at_ms=10_000,
        ended_at_ms=20_000,
    )
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "high_speed_01.mp4",
        source_id="high_speed_01",
        camera_index=3,
        started_at_ms=10_000,
        ended_at_ms=20_000,
        clock_source="external_clip_sidecar_beijing",
        timing_error_ms=100,
    )

    dialog = PassageReviewDialog(
        passage_store,
        timeline_store,
        regular_camera_indexes=(1,),
        show_high_speed_pane=False,
        include_recorded_evidence=False,
    )

    lookup = dialog._lookup(event)
    assert len(dialog.evidence_panes) == 1
    assert [location.segment.camera_index for location in lookup.locations] == [1]
    assert dialog.high_speed_pane.isHidden()
    dialog.close()


def test_historical_review_can_restore_recorded_evidence_panes(
    qapp,
    tmp_path,
    fake_playback,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    event = _event(passage_time_ms=15_000)
    passage_store.append(event)
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01.mkv",
        source_id="camera_01",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=20_000,
    )
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_02.mkv",
        source_id="camera_02",
        camera_index=2,
        started_at_ms=10_000,
        ended_at_ms=20_000,
    )
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "high_speed_01.mp4",
        source_id="high_speed_01",
        camera_index=3,
        started_at_ms=10_000,
        ended_at_ms=20_000,
        clock_source="external_clip_sidecar_beijing",
        timing_error_ms=100,
    )

    dialog = PassageReviewDialog(
        passage_store,
        timeline_store,
        regular_camera_indexes=(1,),
        show_high_speed_pane=False,
        include_recorded_evidence=True,
    )

    assert tuple(pane.camera_index for pane in dialog.regular_panes) == (1, 2)
    assert len(dialog.evidence_panes) == 3
    assert not dialog.high_speed_pane.isHidden()
    dialog.close()


def test_review_uses_consistent_laptop_typography(qapp, tmp_path):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(_event())
    dialog = PassageReviewDialog(
        passage_store,
        VideoTimelineStore(tmp_path / "video_timeline.jsonl"),
    )

    style = dialog.styleSheet()
    assert f'font-family: "{passage_review.UI_FONT_FAMILY}"' in style
    assert f"font-size: {passage_review.UI_BASE_FONT_POINT_SIZE}pt" in style
    assert dialog.table.verticalHeader().defaultSectionSize() == 34
    assert "font-size: 12pt" in dialog.current_passage_label.styleSheet()
    assert "font-size: 9pt" in dialog.summary_label.styleSheet()
    assert dialog.info_panel.minimumWidth() == passage_review.UI_INFO_PANEL_MIN_WIDTH
    assert dialog.info_panel.maximumWidth() == passage_review.UI_INFO_PANEL_MAX_WIDTH
    dialog.close()


def test_review_auto_fits_table_columns_without_squeezing_them(qapp, tmp_path):
    dialog = PassageReviewDialog(
        PassageEventStore(tmp_path / "passages.jsonl"),
        VideoTimelineStore(tmp_path / "video_timeline.jsonl"),
    )

    header = dialog.table.horizontalHeader()
    for column in range(dialog.table.columnCount()):
        assert header.sectionResizeMode(column) == QHeaderView.Interactive
    widths = passage_review._expanded_column_widths(
        (42, 48, 160, 120, 42, 126, 84, 84, 84),
        passage_review._TABLE_COLUMN_MIN_WIDTHS,
        1_600,
    )
    assert sum(widths) == 1_600
    assert widths[2] > widths[1]
    assert widths[3] > widths[4]
    assert all(
        width >= minimum
        for width, minimum in zip(widths, passage_review._TABLE_COLUMN_MIN_WIDTHS)
    )
    dialog.table._fit_columns_to_viewport()
    assert header.sectionSize(2) <= 150
    dialog.close()


def test_large_finish_queue_supports_debounced_search_and_status_filters(
    qapp,
    tmp_path,
    monkeypatch,
):
    journal_path = tmp_path / "passages.jsonl"
    events = [
        _event(
            event_id=f"passage-{index:04d}",
            sequence=index + 1,
            passage_time_ms=index,
            passage_timestamp_ms=1_800_000_000_000 + index,
            bib=f"{index:04d}",
            chip_id=f"chip-{index:04d}",
            athlete_name=f"运动员{index:04d}",
        )
        for index in range(5_000)
    ]
    journal_path.write_text(
        "\n".join(
            json.dumps(event.to_payload(), ensure_ascii=False)
            for event in events
        )
        + "\n",
        encoding="utf-8",
    )
    timeline_path = tmp_path / "video_timeline.jsonl"
    timeline_records = []
    for index in range(160):
        segment_id = f"segment-{index:04d}"
        started_at_ms = 1_799_000_000_000 + index * 1_000
        timeline_records.extend(
            (
                {
                    "schema_version": 1,
                    "record_type": "segment_started",
                    "segment_id": segment_id,
                    "source_id": "camera_01",
                    "camera_index": 1,
                    "video_path": "videos/camera_01.mkv",
                    "started_at_ms": started_at_ms,
                    "clock_source": "videopipe_system_clock",
                    "timing_error_ms": 1_000,
                    "race_id": "race-1",
                },
                {
                    "schema_version": 1,
                    "record_type": "segment_ended",
                    "segment_id": segment_id,
                    "ended_at_ms": started_at_ms + 900,
                    "end_reason": "rotation",
                    "media_duration_ms": 900,
                    "media_started_at_ms": started_at_ms,
                },
            )
        )
    timeline_path.write_text(
        "\n".join(
            json.dumps(record, ensure_ascii=False) for record in timeline_records
        )
        + "\n",
        encoding="utf-8",
    )
    dialog = PassageReviewDialog(
        PassageEventStore(journal_path),
        VideoTimelineStore(timeline_path),
    )
    qapp.processEvents()

    assert dialog.table.rowCount() == 5_000
    source_location_calls = 0
    original_source_location = dialog._source_location_with_saved_association

    def counted_source_location(*args, **kwargs):
        nonlocal source_location_calls
        source_location_calls += 1
        return original_source_location(*args, **kwargs)

    monkeypatch.setattr(
        dialog,
        "_source_location_with_saved_association",
        counted_source_location,
    )
    dialog.identity_search.setText("4999")
    assert dialog._search_refresh_timer.isActive()
    QTest.qWait(180)
    qapp.processEvents()

    assert dialog.table.rowCount() == 1
    assert dialog.table.item(0, 1).text() == "4999"
    assert source_location_calls < 20
    dialog.review_filter_buttons["pending"].click()
    assert dialog.table.rowCount() == 0
    assert dialog.review_filter_buttons["pending"].text().startswith("✓ ")
    assert dialog.summary_label.text() == "当前筛选：异常复核 · 0 / 5,000 条"
    dialog.review_filter_buttons["blocked"].click()
    assert dialog.table.rowCount() == 1
    assert dialog.review_filter_buttons["blocked"].text().startswith("✓ 待人工确认 ")
    assert dialog.summary_label.text() == "当前筛选：待人工确认 · 1 / 5,000 条"
    dialog.review_filter_buttons["all"].click()
    assert dialog.table.rowCount() == 1
    assert dialog.review_filter_buttons["all"].text().startswith("✓ ")
    dialog.close()


def test_review_orders_equal_passage_times_by_event_id(
    qapp,
    tmp_path,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(
        _event(
            event_id="event-b",
            sequence=1,
            bib="1",
            passage_timestamp_ms=20_000,
        )
    )
    passage_store.append(
        _event(
            event_id="event-a",
            sequence=2,
            bib="2",
            passage_timestamp_ms=20_000,
        )
    )
    dialog = PassageReviewDialog(
        passage_store,
        VideoTimelineStore(tmp_path / "video_timeline.jsonl"),
    )
    qapp.processEvents()

    assert dialog.table.item(0, 0).data(Qt.UserRole) == "event-a"
    assert dialog.table.item(1, 0).data(Qt.UserRole) == "event-b"
    dialog.close()


def test_review_displays_absolute_passage_in_beijing_time(qapp, tmp_path):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(
        _event(
            passage_time_ms=48_179_215,
            passage_timestamp_ms=1_786_252_979_215,
        )
    )
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")

    dialog = PassageReviewDialog(passage_store, timeline_store)
    qapp.processEvents()

    assert dialog.table.item(0, 5).text() == "13:22:59.215"
    dialog.close()


def test_review_marks_legacy_video_as_unverified(qapp, tmp_path, fake_playback):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(_event())
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    video_path = tmp_path / "videos" / "legacy.mkv"
    video_path.parent.mkdir()
    video_path.write_bytes(b"video")
    segment = timeline_store.start_segment(
        source_id="camera_01",
        camera_index=1,
        video_path=video_path,
        started_at_ms=10_000,
    )
    timeline_store.finish_segment(segment.segment_id, ended_at_ms=20_000)

    dialog = PassageReviewDialog(passage_store, timeline_store)
    qapp.processEvents()

    assert dialog.table.item(0, 6).text() == "未确认"
    assert dialog.table.item(0, 8).text() == "未确认"
    assert dialog.regular_pane.open_btn.isEnabled()
    dialog.close()


def test_review_shows_missing_evidence_without_starting_workers(qapp, tmp_path):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(_event())
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")

    dialog = PassageReviewDialog(passage_store, timeline_store)
    qapp.processEvents()

    assert dialog.table.item(0, 6).text() == "未确认"
    assert dialog.table.item(0, 7).text() == "未确认"
    assert dialog.table.item(0, 8).text() == "未确认"
    assert dialog.regular_pane._worker is None
    assert dialog.high_speed_pane._worker is None
    assert dialog.regular_pane.status_label.text() == "无录像"
    assert dialog.high_speed_pane.isHidden()
    assert (
        dialog.regular_pane.video_view._message_item.toPlainText()
        == "当前赛事没有普通录像"
    )
    assert (
        dialog.regular_pane.video_view._message_item.defaultTextColor().name()
        == "#c9d2dc"
    )
    dialog.close()


def test_review_shows_active_recording_as_waiting(qapp, tmp_path):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(_event())
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    video_path = tmp_path / "videos" / "camera_01.mkv"
    video_path.parent.mkdir(parents=True, exist_ok=True)
    video_path.write_bytes(b"video")
    timeline_store.start_segment(
        source_id="camera_01",
        camera_index=1,
        video_path=video_path,
        started_at_ms=10_000,
        race_id="race-1",
    )

    dialog = PassageReviewDialog(passage_store, timeline_store)
    qapp.processEvents()

    assert dialog.table.item(0, 6).text() == "未确认"
    assert dialog.table.item(0, 6).foreground().color().name() == "#c0372b"
    assert dialog.table.item(0, 7).text() == "未确认"
    assert dialog.regular_pane.status_label.text() == "录像处理中"
    assert (
        dialog.regular_pane.video_view._message_item.toPlainText()
        == "普通录像正在录制，等待片段封口"
    )
    assert (
        dialog.regular_pane.video_view._message_item.defaultTextColor().name()
        == "#c9d2dc"
    )
    assert dialog.regular_pane._worker is None
    dialog.close()


def test_preview_can_be_marked_and_confirmed_before_final_clip_is_ready(
    qapp,
    tmp_path,
    fake_playback,
):
    event = _event(passage_time_ms=15_000)
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    _add_segment(
        timeline_store,
        tmp_path / "review_buffer" / "preview.m3u8",
        source_id="camera_01",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=20_000,
    )
    location = timeline_store.locate_passage(15_000).locations[0]
    preview_location = replace(
        location,
        segment=replace(
            location.segment,
            end_reason="passage_review_preview",
        ),
        status="preview",
    )
    pane = PassageEvidencePane("普通录像", REGULAR_SOURCE)

    pane.set_passage(event, preview_location, lookup_status="preview")
    worker = fake_playback.instances[-1]
    image = QImage(640, 360, QImage.Format_RGB888)
    image.fill(0)
    worker.frame_ready.emit(image, 5_000, 250)
    qapp.processEvents()

    assert pane.status_label.text() == "录像处理中"
    assert "完整证据处理中" in pane.status_label.toolTip()
    assert pane.play_btn.isEnabled()
    assert pane.mark_btn.isEnabled()
    assert pane.open_btn.isEnabled()
    pane._on_marker_position_selected(0.5, 0.5)
    pending = pane.pending_confirmation()
    assert pending is not None
    assert pending["position_ms"] == 5_000
    assert pane.confirm_btn.isEnabled()
    worker.playback_error.emit("preview decode failed")
    qapp.processEvents()
    pane.close()


def test_preview_confirmation_survives_final_segment_replacement(
    qapp,
    tmp_path,
    fake_playback,
):
    event = _event(passage_time_ms=15_000)
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01.mkv",
        source_id="camera_01",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=20_000,
    )
    location = timeline_store.locate_passage(15_000).locations[0]
    association = PassageEvidenceAssociation(
        passage_event_id=event.event_id,
        bib=event.bib,
        confirmed_source=REGULAR_SOURCE,
        segment_id="preview-temporary-segment",
        frame_index=125,
        position_ms=5_000,
        marker_x_normalized=0.5,
        marker_y_normalized=0.5,
        confirmed_at_ms=1,
    )
    pane = PassageEvidencePane("Camera 1", REGULAR_SOURCE)
    pane.set_passage(event, location, association=association)

    assert pane.association == association
    assert not pane.mark_btn.isEnabled()
    pane.close()


def test_target_button_seeks_to_passage_time(
    qapp,
    tmp_path,
    fake_playback,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(_event(passage_time_ms=15_000))
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    for camera_index in (1,):
        _add_segment(
            timeline_store,
            tmp_path / "videos" / f"camera_{camera_index:02d}.mkv",
            source_id=f"camera_{camera_index:02d}",
            camera_index=camera_index,
            started_at_ms=10_000,
            ended_at_ms=20_000,
        )
    dialog = PassageReviewDialog(
        passage_store,
        timeline_store,
        regular_camera_indexes=(1,),
        show_high_speed_pane=False,
    )
    qapp.processEvents()

    for worker in fake_playback.instances:
        worker.seek_calls.clear()
    dialog.target_position_btn.click()
    qapp.processEvents()

    assert dialog._shared_delta_ms == 0
    assert all(worker.seek_calls[-1] == 5_000 for worker in fake_playback.instances)
    dialog.close()


def test_preview_seek_links_camera_panes(
    qapp,
    tmp_path,
    fake_playback,
    monkeypatch,
):
    event = _event(passage_time_ms=15_000)
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(event)
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    for camera_index in (1, 2):
        _add_segment(
            timeline_store,
            tmp_path / "videos" / f"camera_{camera_index:02d}.mkv",
            source_id=f"camera_{camera_index:02d}",
            camera_index=camera_index,
            started_at_ms=10_000,
            ended_at_ms=20_000,
        )
    dialog = PassageReviewDialog(
        passage_store,
        timeline_store,
        regular_camera_indexes=(1, 2),
        show_high_speed_pane=False,
    )
    qapp.processEvents()
    locations = {
        location.segment.camera_index: replace(location, status="preview")
        for location in dialog._lookups[event.event_id].locations
    }
    for pane in dialog.regular_panes:
        pane.set_passage(event, locations[pane.camera_index], lookup_status="preview")

    calls = []

    def record_apply(delta_ms, *, preview):
        calls.append((int(delta_ms), bool(preview)))

    monkeypatch.setattr(dialog, "_apply_both_delta", record_apply)
    dialog._seek_pane_delta(dialog.regular_panes[0], 250, preview=False)

    assert calls == [(250, False)]
    dialog.close()


def test_review_distinguishes_unlocated_time_from_missing_recording(qapp, tmp_path):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(_event(passage_time_ms=5_000))
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01.mkv",
        source_id="camera_01",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=20_000,
    )

    dialog = PassageReviewDialog(passage_store, timeline_store)
    qapp.processEvents()

    assert dialog._lookups["passage-1"].status == "before_recording"
    assert dialog.regular_pane.status_label.text() == "未定位"
    assert (
        dialog.regular_pane.video_view._message_item.toPlainText()
        == "未定位到对应普通录像"
    )
    assert dialog.regular_pane.status_label.toolTip() == "早于录像"
    dialog.close()


def test_review_shows_high_speed_boundary_independently(
    qapp,
    tmp_path,
    fake_playback,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(_event(passage_time_ms=9_950))
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    video_path = tmp_path / "videos" / "high_speed_01.mp4"
    _add_segment(
        timeline_store,
        video_path,
        source_id="high_speed_01",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=20_000,
        clock_source="external_test_clock",
        timing_error_ms=100,
    )

    dialog = PassageReviewDialog(passage_store, timeline_store)
    qapp.processEvents()

    assert dialog.table.rowCount() == 1
    assert dialog.table.item(0, 6).text() == "未确认"
    assert dialog.table.item(0, 7).text() == "未确认"
    assert dialog.table.item(0, 8).text() == "未确认"
    assert dialog.high_speed_pane.location.status == "near_boundary"
    assert dialog.high_speed_pane.location.playback_position_ms == 0
    dialog.close()


def test_review_shows_regular_and_high_speed_sources_on_one_row(
    qapp,
    tmp_path,
    fake_playback,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(_event(passage_time_ms=20_050))
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    videos_dir = tmp_path / "videos"
    standard_path = videos_dir / "camera_01.mkv"
    _add_segment(
        timeline_store,
        standard_path,
        source_id="camera_01",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=30_000,
    )
    high_speed_path = videos_dir / "high_speed_02.mp4"
    _add_segment(
        timeline_store,
        high_speed_path,
        source_id="high_speed_02",
        camera_index=2,
        started_at_ms=19_000,
        ended_at_ms=20_000,
        clock_source="external_clip_sidecar_beijing",
        timing_error_ms=100,
    )
    opened = []

    dialog = PassageReviewDialog(
        passage_store,
        timeline_store,
        open_location=lambda event, location: opened.append((event, location)),
    )
    qapp.processEvents()

    assert dialog.table.rowCount() == 1
    assert dialog.table.item(0, 6).text() == "未确认"
    assert dialog.table.item(0, 7).text() == "未确认"
    assert dialog.table.item(0, 8).text() == "未确认"
    assert dialog.regular_pane.location.segment.source_id == "camera_01"
    assert dialog.high_speed_pane.location.segment.source_id == "high_speed_02"
    assert "另有 1 个机位位于误差边界" in lookup_status_text(
        dialog._lookup(passage_store.get("passage-1"))
    )

    assert dialog.high_speed_pane.open_btn.isHidden()
    assert not dialog.regular_pane.open_btn.isHidden()
    assert not dialog.high_speed_pane.open_btn.isEnabled()
    dialog.regular_pane.open_btn.click()
    assert [location.segment.source_id for _event, location in opened] == [
        "camera_01",
    ]
    dialog.close()


def test_high_speed_only_double_click_maximizes_judging_pane(
    qapp,
    tmp_path,
    fake_playback,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(_event(passage_time_ms=20_050))
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "high_speed_02.mp4",
        source_id="high_speed_02",
        camera_index=2,
        started_at_ms=19_000,
        ended_at_ms=21_000,
        clock_source="external_clip_sidecar_beijing",
        timing_error_ms=100,
    )
    opened = []
    dialog = PassageReviewDialog(
        passage_store,
        timeline_store,
        open_location=lambda event, location: opened.append((event, location)),
    )
    dialog.show()
    qapp.processEvents()

    dialog._open_preferred_source(0, 7)
    qapp.processEvents()

    assert opened == []
    assert dialog._maximized_pane is dialog.high_speed_pane
    assert dialog._maximized_window is not None
    assert dialog._maximized_window.isVisible()
    assert dialog.high_speed_pane.parentWidget() is dialog._maximized_content_splitter
    assert not dialog.high_speed_pane.isHidden()
    assert dialog.high_speed_pane.maximize_btn.text() == "缩小"
    assert dialog.high_speed_pane.maximize_btn.toolTip() == "恢复主界面（Esc 或 F）"
    assert dialog.regular_pane.maximize_btn.text() == "放大"

    dialog.high_speed_pane.video_view.setFocus()
    QTest.keyClick(dialog.high_speed_pane.video_view.viewport(), Qt.Key_Escape)
    qapp.processEvents()

    assert dialog._maximized_pane is None
    assert dialog._maximized_window is None
    assert dialog.high_speed_pane.parentWidget() is dialog.evidence_splitter
    assert dialog.high_speed_pane.maximize_btn.text() == "放大"
    assert dialog.regular_pane.maximize_btn.text() == "放大"
    dialog.close()


def test_identity_search_selects_bib_15_without_recomputing_lookups(
    qapp,
    tmp_path,
    fake_playback,
    monkeypatch,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(_event(event_id="passage-9", sequence=1, bib="9"))
    passage_store.append(
        _event(event_id="passage-15", sequence=2, bib="15", chip_id="chip-15")
    )
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01.mkv",
        source_id="camera_01",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=20_000,
    )
    locate_calls = []
    original_locate = timeline_store.locate_passage

    def locate_passage(*args, **kwargs):
        locate_calls.append((args, kwargs))
        return original_locate(*args, **kwargs)

    monkeypatch.setattr(timeline_store, "locate_passage", locate_passage)
    dialog = PassageReviewDialog(passage_store, timeline_store)
    qapp.processEvents()
    assert len(locate_calls) == 2

    passage_9_row = next(
        index
        for index, event in enumerate(dialog._visible_events)
        if event.event_id == "passage-9"
    )
    passage_15_row = next(
        index
        for index, event in enumerate(dialog._visible_events)
        if event.event_id == "passage-15"
    )
    dialog.table.setCurrentCell(passage_9_row, 0)
    dialog.table.selectRow(passage_9_row)

    dialog.identity_search.setText("15")
    dialog._find_identity()
    qapp.processEvents()

    assert dialog.table.currentRow() == passage_15_row
    assert dialog._selected_event_id == "passage-15"
    assert dialog.selected_identity_value.text() == "15"
    assert len(locate_calls) == 2
    dialog.refresh()
    assert dialog.table.currentRow() == passage_15_row
    assert dialog._selected_event_id == "passage-15"
    assert dialog.selected_identity_value.text() == "15"
    assert dialog.identity_search.text() == "15"
    assert len(locate_calls) == 2
    dialog.close()


def test_batch_mode_keeps_one_video_worker_when_switching_events(
    qapp,
    tmp_path,
    fake_playback,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(
        _event(event_id="passage-4", sequence=1, passage_time_ms=15_000, bib="4")
    )
    passage_store.append(
        _event(event_id="passage-1", sequence=2, passage_time_ms=18_000, bib="1", group_id="women-open")
    )
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01.mkv",
        source_id="camera_01",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=25_000,
    )

    dialog = PassageReviewDialog(passage_store, timeline_store)
    dialog.show()
    qapp.processEvents()
    assert dialog._enter_batch_mode("passage-4")
    qapp.processEvents()
    worker = dialog.regular_pane._worker
    frame = QImage(1280, 720, QImage.Format_RGB888)
    frame.fill(0)
    worker.frame_ready.emit(frame, 7_250, 181)
    qapp.processEvents()
    seek_count = len(worker.seek_calls)
    view = dialog.regular_pane.video_view
    # Bibs are shown only in the athlete table; camera panes remain clear.
    assert not view._batch_roster_panel.isVisible()

    dialog._select_event("passage-1")
    qapp.processEvents()

    assert dialog._batch_mode is True
    assert dialog.regular_pane._worker is worker
    assert len(worker.seek_calls) == seek_count
    assert dialog.selected_identity_value.text() == "1"
    assert dialog.regular_pane._marking_enabled is True
    assert dialog.regular_pane.video_view._marker_mode is True
    dialog.close()


def test_batch_switch_pauses_current_frame_before_rebinding_identity(
    qapp,
    tmp_path,
    fake_playback,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(
        _event(event_id="passage-4", sequence=1, passage_time_ms=15_000, bib="4")
    )
    passage_store.append(
        _event(event_id="passage-1", sequence=2, passage_time_ms=16_000, bib="1")
    )
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01_archive_0000.mkv",
        source_id="camera_01_review",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=25_000,
    )

    dialog = PassageReviewDialog(passage_store, timeline_store)
    assert dialog._enter_batch_mode("passage-4")
    qapp.processEvents()
    pane = dialog.regular_pane
    worker = pane._worker
    pane._current_position_ms = 7_250
    pane._current_frame_index = 181
    pane._playing = True
    pause_count = worker.pause_calls

    dialog._move_selection(1)
    qapp.processEvents()

    assert dialog._selected_event_id == "passage-1"
    assert pane._playing is False
    assert worker.pause_calls > pause_count
    assert pane._current_position_ms == 7_250
    dialog.close()


def test_batch_switch_across_archive_seeks_to_target_lead_in(
    qapp,
    tmp_path,
    fake_playback,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(
        _event(event_id="passage-4", sequence=1, passage_time_ms=15_000, bib="4")
    )
    passage_store.append(
        _event(event_id="passage-35", sequence=2, passage_time_ms=40_000, bib="35")
    )
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01_archive_0000.mkv",
        source_id="camera_01_review_0000",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=20_000,
    )
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01_archive_0001.mkv",
        source_id="camera_01_review_0001",
        camera_index=1,
        started_at_ms=30_000,
        ended_at_ms=50_000,
    )

    dialog = PassageReviewDialog(passage_store, timeline_store)
    assert dialog._enter_batch_mode("passage-4")
    qapp.processEvents()
    first_worker = dialog.regular_pane._worker

    dialog._move_selection(1)
    qapp.processEvents()

    second_worker = dialog.regular_pane._worker
    assert second_worker is not first_worker
    assert second_worker.video_path.name == "camera_01_archive_0001.mkv"
    assert second_worker.seek_calls == [8_000]
    assert dialog.regular_pane._playing is False
    dialog.close()


def test_batch_mode_records_video_discovered_bib_without_creating_passage(
    qapp,
    tmp_path,
    fake_playback,
    monkeypatch,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(
        _event(event_id="passage-4", sequence=1, passage_time_ms=15_000, bib="4")
    )
    passage_store.append(
        _event(event_id="passage-1", sequence=2, passage_time_ms=18_000, bib="1")
    )
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01.mkv",
        source_id="camera_01",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=25_000,
    )

    dialog = PassageReviewDialog(passage_store, timeline_store)
    dialog.show()
    qapp.processEvents()
    assert dialog._enter_batch_mode("passage-4")
    pane = dialog.regular_pane
    pane._current_position_ms = 16_250
    pane._current_frame_index = 406
    monkeypatch.setattr(
        passage_review.QInputDialog,
        "getText",
        staticmethod(lambda *_args, **_kwargs: ("319", True)),
    )

    dialog._add_video_discovered_bib()
    qapp.processEvents()

    assert len(dialog._video_discovered_entries) == 1
    assert dialog._video_discovered_entries[0]["bib"] == "319"
    assert dialog.video_discovered_table.isVisible()
    assert dialog.video_discovered_table.item(0, 0).text() == "319"
    assert "芯片缺失" in dialog.video_discovered_table.item(0, 4).text()
    assert not pane.mark_btn.isEnabled()
    pane._current_position_ms = 22_000
    pane._worker.seek_calls.clear()
    dialog.video_discovered_table.selectRow(0)
    qapp.processEvents()
    assert pane._worker.seek_calls[-1] == 16_250
    passage_store.append(
        _event(
            event_id="passage-319-later",
            sequence=3,
            passage_time_ms=60_000,
            bib="319",
        )
    )
    dialog.refresh()
    assert dialog._video_discovered_entries[0]["status"] == "pending_manual_entry"
    passage_store.append(
        _event(
            event_id="passage-319-in-batch",
            sequence=4,
            passage_time_ms=16_500,
            bib="319",
        )
    )
    dialog.refresh()
    assert dialog._video_discovered_entries[0]["status"] == "resolved"
    dialog._select_event("passage-1")
    assert pane.mark_btn.isEnabled()
    assert any(event.bib == "319" for event in passage_store.events())
    dialog.close()


def test_batch_confirmation_advances_without_returning_to_confirmed_position(
    qapp,
    tmp_path,
    fake_playback,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(
        _event(event_id="passage-246", sequence=1, passage_time_ms=15_000, bib="246")
    )
    passage_store.append(
        _event(event_id="passage-235", sequence=2, passage_time_ms=16_500, bib="235")
    )
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01.mkv",
        source_id="camera_01",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=25_000,
    )

    dialog = PassageReviewDialog(passage_store, timeline_store)
    dialog.show()
    qapp.processEvents()
    assert dialog._enter_batch_mode("passage-246")
    pane = dialog.regular_pane
    worker = pane._worker
    frame = QImage(1280, 720, QImage.Format_RGB888)
    frame.fill(0)
    worker.frame_ready.emit(frame, 7_250, 181)
    qapp.processEvents()
    worker.seek_calls.clear()

    QTest.mouseClick(
        pane.video_view.viewport(),
        Qt.LeftButton,
        pos=pane.video_view.viewport().rect().center(),
    )
    QTest.keyClick(pane.video_view, Qt.Key_Return)
    qapp.processEvents()

    assert dialog._selected_event_id == "passage-235"
    assert pane._current_position_ms == 7_250
    assert pane._target_position_ms == 8_750
    assert dialog._shared_delta_ms == -1_500
    assert worker.seek_calls == []

    dialog._toggle_both()

    assert worker.seek_calls == [7_250]
    dialog.close()


def test_batch_next_with_three_second_gap_keeps_current_frame_for_safe_scan(
    qapp,
    tmp_path,
    fake_playback,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(
        _event(event_id="passage-246", sequence=1, passage_time_ms=15_000, bib="246")
    )
    passage_store.append(
        _event(event_id="passage-235", sequence=2, passage_time_ms=18_000, bib="235")
    )
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01.mkv",
        source_id="camera_01",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=25_000,
    )

    dialog = PassageReviewDialog(passage_store, timeline_store)
    assert dialog._enter_batch_mode("passage-246")
    qapp.processEvents()
    pane = dialog.regular_pane
    worker = pane._worker
    frame = QImage(1280, 720, QImage.Format_RGB888)
    frame.fill(0)
    worker.frame_ready.emit(frame, 5_000, 125)
    qapp.processEvents()
    worker.seek_calls.clear()

    dialog._move_selection(1)
    qapp.processEvents()

    assert dialog._selected_event_id == "passage-235"
    assert worker.seek_calls == []
    assert dialog._shared_delta_ms == -3_000
    dialog.close()


def test_batch_next_with_several_seconds_gap_keeps_current_frame_for_safe_scan(
    qapp,
    tmp_path,
    fake_playback,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(
        _event(event_id="passage-246", sequence=1, passage_time_ms=15_000, bib="246")
    )
    passage_store.append(
        _event(event_id="passage-235", sequence=2, passage_time_ms=24_000, bib="235")
    )
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01.mkv",
        source_id="camera_01",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=35_000,
    )

    dialog = PassageReviewDialog(passage_store, timeline_store)
    assert dialog._enter_batch_mode("passage-246")
    qapp.processEvents()
    pane = dialog.regular_pane
    worker = pane._worker
    frame = QImage(1280, 720, QImage.Format_RGB888)
    frame.fill(0)
    worker.frame_ready.emit(frame, 5_000, 125)
    qapp.processEvents()
    worker.seek_calls.clear()

    dialog._move_selection(1)
    qapp.processEvents()

    # Keep the current frame so an unchipped rider in the gap cannot be skipped.
    assert dialog._selected_event_id == "passage-235"
    assert worker.seek_calls == []
    assert dialog._shared_delta_ms == -9_000
    dialog.close()


def test_batch_next_across_media_boundary_keeps_current_frame_for_split_riders(
    qapp,
    tmp_path,
    fake_playback,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(
        _event(event_id="passage-246", sequence=1, passage_time_ms=15_000, bib="246")
    )
    passage_store.append(
        _event(event_id="passage-235", sequence=2, passage_time_ms=24_000, bib="235")
    )
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01_part1.mkv",
        source_id="camera_01",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=20_000,
    )
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01_part2.mkv",
        source_id="camera_01",
        camera_index=1,
        started_at_ms=20_000,
        ended_at_ms=30_000,
    )

    dialog = PassageReviewDialog(passage_store, timeline_store)
    assert dialog._enter_batch_mode("passage-246")
    qapp.processEvents()
    pane = dialog.regular_pane
    worker = pane._worker
    frame = QImage(1280, 720, QImage.Format_RGB888)
    frame.fill(0)
    worker.frame_ready.emit(frame, 9_500, 237)
    qapp.processEvents()
    worker.seek_calls.clear()

    dialog._move_selection(1)
    qapp.processEvents()

    assert dialog._selected_event_id == "passage-235"
    assert pane._worker is worker
    assert pane._current_position_ms == 9_500
    assert worker.seek_calls == []
    dialog.close()


def test_enter_confirmation_outside_batch_keeps_frame_across_video_boundary(
    qapp,
    tmp_path,
    fake_playback,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(
        _event(event_id="passage-35", sequence=1, passage_time_ms=19_000, bib="35")
    )
    passage_store.append(
        _event(event_id="passage-29", sequence=2, passage_time_ms=21_000, bib="29")
    )
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    first = _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01_part1.mkv",
        source_id="camera_01",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=20_000,
    )
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01_part2.mkv",
        source_id="camera_01",
        camera_index=1,
        started_at_ms=20_000,
        ended_at_ms=30_000,
    )

    dialog = PassageReviewDialog(passage_store, timeline_store)
    dialog.show()
    dialog.auto_advance_checkbox.setChecked(True)
    dialog._select_event("passage-35")
    qapp.processEvents()
    assert not dialog._batch_mode
    pane = dialog.regular_pane
    worker = pane._worker
    frame = QImage(1280, 720, QImage.Format_RGB888)
    frame.fill(0)
    worker.frame_ready.emit(frame, 9_500, 475)
    qapp.processEvents()
    QTest.mouseClick(
        pane.video_view.viewport(),
        Qt.LeftButton,
        pos=pane.video_view.viewport().rect().center(),
    )
    QTest.keyClick(pane.video_view, Qt.Key_Return)
    qapp.processEvents()

    assert dialog._selected_event_id == "passage-29"
    assert pane.location.segment.segment_id == first.segment_id
    assert pane._worker is worker
    assert pane._current_position_ms == 9_500
    assert pane.association is None
    assert not pane.has_pending_marker
    assert pane.mark_btn.text() == "标线"
    assert pane.status_label.text() != "已确认"
    passage_35_row = next(
        row
        for row, event in enumerate(dialog._visible_events)
        if event.event_id == "passage-35"
    )
    passage_29_row = next(
        row
        for row, event in enumerate(dialog._visible_events)
        if event.event_id == "passage-29"
    )
    assert dialog.table.item(passage_35_row, 8).text() == "已确认"
    assert dialog.table.item(passage_29_row, 8).text() != "已确认"
    dialog.close()


def test_manual_clicks_keep_overlapping_split_video_after_confirmation(
    qapp,
    tmp_path,
    fake_playback,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(
        _event(event_id="passage-29", sequence=1, passage_time_ms=19_000, bib="29")
    )
    passage_store.append(
        _event(event_id="passage-40", sequence=2, passage_time_ms=20_500, bib="40")
    )
    passage_store.append(
        _event(event_id="passage-3", sequence=3, passage_time_ms=20_800, bib="3")
    )
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    first = _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01_part1.mkv",
        source_id="camera_01",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=20_000,
        media_started_at_ms=10_000,
        media_duration_ms=10_000,
    )
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01_part2.mkv",
        source_id="camera_01",
        camera_index=1,
        started_at_ms=20_000,
        ended_at_ms=30_000,
        media_started_at_ms=20_000,
        media_duration_ms=10_000,
    )

    dialog = PassageReviewDialog(passage_store, timeline_store)
    dialog.show()
    dialog.auto_advance_checkbox.setChecked(True)
    dialog._select_event("passage-29")
    qapp.processEvents()
    pane = dialog.regular_pane
    worker = pane._worker
    frame = QImage(1280, 720, QImage.Format_RGB888)
    frame.fill(0)
    worker.frame_ready.emit(frame, 8_500, 425)
    qapp.processEvents()
    view = pane.video_view
    QTest.mouseClick(view.viewport(), Qt.LeftButton, pos=view.viewport().rect().center())
    QTest.keyClick(view, Qt.Key_Return)
    qapp.processEvents()

    assert dialog._selected_event_id == "passage-40"
    assert pane.location.segment.segment_id == first.segment_id
    assert pane._worker is worker
    assert pane._current_position_ms == 8_500

    row = next(
        index
        for index, event in enumerate(dialog._visible_events)
        if event.event_id == "passage-3"
    )
    dialog.table.setCurrentCell(row, 1)
    dialog.table.selectRow(row)
    qapp.processEvents()

    assert dialog._selected_event_id == "passage-3"
    assert pane.location.segment.segment_id == first.segment_id
    assert pane._worker is worker
    assert pane._current_position_ms == 8_500
    dialog.close()


def test_previous_frame_crosses_into_adjacent_recording_without_changing_rider(
    qapp,
    tmp_path,
    fake_playback,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    event = _event(
        event_id="passage-first-in-batch",
        sequence=2,
        passage_time_ms=21_000,
        bib="62",
    )
    passage_store.append(event)
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    first = _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01_archive_0000.mkv",
        source_id="camera_01",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=20_000,
    )
    second = _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01_archive_0001.mkv",
        source_id="camera_01",
        camera_index=1,
        started_at_ms=20_000,
        ended_at_ms=30_000,
    )
    # A short review-buffer clip can overlap the archive boundary. It is not
    # the previous member of the active archive recording sequence.
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01_review_buffer.ts",
        source_id="camera_01_review_buffer",
        camera_index=1,
        started_at_ms=19_500,
        ended_at_ms=20_000,
    )

    dialog = PassageReviewDialog(passage_store, timeline_store)
    qapp.processEvents()
    pane = dialog.regular_pane
    assert pane.location.segment.segment_id == second.segment_id
    frame = QImage(1280, 720, QImage.Format_RGB888)
    frame.fill(0)
    pane._worker.frame_ready.emit(frame, 0, 0)
    qapp.processEvents()

    dialog._step_active_pane(-1)
    qapp.processEvents()

    assert dialog._selected_event_id == event.event_id
    assert pane.location.segment.segment_id == first.segment_id
    assert pane._worker.video_path == pane.location.video_path
    assert pane._worker.seek_calls[-1] >= 9_900
    dialog.close()


def test_next_frame_crosses_when_decoder_is_on_last_frame_before_reported_duration(
    qapp,
    tmp_path,
    fake_playback,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    event = _event(
        event_id="passage-last-in-recording",
        sequence=1,
        passage_time_ms=19_000,
        bib="35",
    )
    passage_store.append(event)
    next_event = _event(
        event_id="passage-next-same-boundary-batch",
        sequence=2,
        passage_time_ms=19_100,
        bib="29",
    )
    passage_store.append(next_event)
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    first = _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01_archive_0000.mkv",
        source_id="camera_01_review",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=20_000,
    )
    second = _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01_archive_0001.mkv",
        source_id="camera_01_review",
        camera_index=1,
        started_at_ms=20_000,
        ended_at_ms=30_000,
    )

    dialog = PassageReviewDialog(passage_store, timeline_store)
    qapp.processEvents()
    pane = dialog.regular_pane
    assert pane.location.segment.segment_id == first.segment_id
    frame = QImage(1280, 720, QImage.Format_RGB888)
    frame.fill(0)
    # Matroska commonly reports a 10 s duration although its final decodable
    # frame has an earlier timestamp. Frame index is authoritative at the tail.
    pane._worker.metadata_ready.emit(10_000, 25.0, 1280, 720, 250)
    pane._worker.frame_ready.emit(frame, 9_900, 249)
    qapp.processEvents()

    dialog._step_active_pane(1)
    qapp.processEvents()

    assert dialog._selected_event_id == event.event_id
    assert pane.location.segment.segment_id == second.segment_id
    assert pane._worker.video_path == pane.location.video_path
    assert pane._worker.seek_calls[-1] == 0

    # Confirmation in the adjacent segment must count for the selected rider,
    # even though their official timestamp originally resolved to `first`.
    pane._worker.frame_ready.emit(frame, 1_240, 31)
    qapp.processEvents()
    pane._on_marker_position_selected(0.5, 0.5)
    assert dialog._confirm_pending_marker(pane)
    event_row = next(
        row
        for row, visible in enumerate(dialog._visible_events)
        if visible.event_id == event.event_id
    )
    assert dialog.table.item(event_row, 8).text() == "已确认"

    current_worker = pane._worker
    current_position_ms = pane._current_position_ms
    dialog._move_selection(1, preserve_current_frame=True)
    qapp.processEvents()

    assert dialog._selected_event_id == next_event.event_id
    assert pane._worker is current_worker
    assert pane.location.segment.segment_id == second.segment_id
    assert pane._current_position_ms == current_position_ms
    assert not pane.has_pending_marker
    assert pane.association is None
    assert pane._reference_only is False
    assert pane._marking_enabled is True
    assert pane.status_label.text() != "参考"
    dialog.close()


def test_continuous_mode_shows_future_numbers_and_keeps_current_frame(
    qapp,
    tmp_path,
    fake_playback,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(
        _event(event_id="passage-4", sequence=1, passage_time_ms=15_000, bib="4")
    )
    passage_store.append(
        _event(
            event_id="passage-1",
            sequence=2,
            passage_time_ms=35_000,
            bib="1",
            group_id="women-open",
        )
    )
    passage_store.append(
        _event(event_id="passage-62", sequence=3, passage_time_ms=36_000, bib="62")
    )
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01_archive_0000.mkv",
        source_id="camera_01_review",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=50_000,
    )

    dialog = PassageReviewDialog(passage_store, timeline_store)
    dialog.show()
    qapp.processEvents()
    assert dialog._enter_batch_mode("passage-4")
    qapp.processEvents()
    pane = dialog.regular_pane
    worker = pane._worker
    frame = QImage(1280, 720, QImage.Format_RGB888)
    frame.fill(0)
    worker.frame_ready.emit(frame, 7_250, 181)
    qapp.processEvents()
    worker.seek_calls.clear()

    assert [event.bib for event in dialog._visible_events] == ["4", "1", "62"]
    assert not pane.video_view._batch_roster_panel.isVisible()

    dialog._select_event("passage-1")
    qapp.processEvents()

    assert dialog._selected_event_id == "passage-1"
    assert pane._worker is worker
    assert pane._current_position_ms == 7_250
    assert worker.seek_calls == []
    assert pane.video_view._marker_mode is True
    assert not pane.video_view._batch_roster_panel.isVisible()
    dialog.close()


def test_continuous_first_confirmation_calibrates_without_skipping_long_gap(
    qapp,
    tmp_path,
    fake_playback,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(
        _event(event_id="passage-4", sequence=1, passage_time_ms=15_000, bib="4")
    )
    passage_store.append(
        _event(event_id="passage-35", sequence=2, passage_time_ms=40_000, bib="35")
    )
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01_session_archive_0000.mkv",
        source_id="camera_01_review",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=60_000,
    )

    dialog = PassageReviewDialog(passage_store, timeline_store)
    dialog.show()
    qapp.processEvents()
    assert dialog._enter_batch_mode("passage-4")
    qapp.processEvents()
    pane = dialog.regular_pane
    worker = pane._worker
    frame = QImage(1280, 720, QImage.Format_RGB888)
    frame.fill(0)
    worker.frame_ready.emit(frame, 3_700, 92)
    qapp.processEvents()
    worker.seek_calls.clear()

    QTest.mouseClick(
        pane.video_view.viewport(),
        Qt.LeftButton,
        pos=pane.video_view.viewport().rect().center(),
    )
    QTest.keyClick(pane.video_view, Qt.Key_Return)
    qapp.processEvents()

    assert dialog._selected_event_id == "passage-35"
    assert tuple(dialog._continuous_clock_offsets.values()) == (-1_300,)
    assert pane._target_position_ms == 28_700
    assert worker.seek_calls == []
    assert dialog._shared_delta_ms == -25_000
    dialog.close()


def test_continuous_mode_seeds_first_saved_confirmation_for_session(
    qapp,
    tmp_path,
    fake_playback,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    first = _event(
        event_id="passage-4",
        sequence=1,
        passage_time_ms=15_000,
        bib="4",
    )
    second = _event(
        event_id="passage-35",
        sequence=2,
        passage_time_ms=40_000,
        bib="35",
    )
    passage_store.append(first)
    passage_store.append(second)
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    segment = _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01_session_archive_0000.mkv",
        source_id="camera_01_review",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=60_000,
    )
    association_store = PassageEvidenceAssociationStore(
        tmp_path / "passage_evidence_associations.jsonl"
    )
    association_store.confirm(
        passage_event_id=first.event_id,
        bib=first.bib,
        confirmed_source=REGULAR_SOURCE,
        segment_id=segment.segment_id,
        frame_index=92,
        position_ms=3_700,
        marker_x_normalized=0.5,
        marker_y_normalized=0.5,
        confirmed_at_ms=50_000,
    )

    dialog = PassageReviewDialog(
        passage_store,
        timeline_store,
        association_store=association_store,
    )
    qapp.processEvents()
    assert dialog._enter_batch_mode(first.event_id)
    qapp.processEvents()

    assert tuple(dialog._continuous_clock_offsets.values()) == (-1_300,)
    second_location = dialog._regular_location_for_camera(
        dialog._lookups[second.event_id],
        1,
    )
    assert second_location.passage_position_ms == 28_700
    assert second_location.playback_position_ms == 26_700
    dialog.close()


def test_continuous_camera_calibrations_persist_independently(
    qapp,
    tmp_path,
    fake_playback,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    first = _event(event_id="passage-first", sequence=1, passage_time_ms=15_000, bib="4")
    passage_store.append(first)
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    camera_one = _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01_archive_0000.mkv",
        source_id="camera_01_review",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=60_000,
    )
    camera_two = _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_02_archive_0000.mkv",
        source_id="camera_02_review",
        camera_index=2,
        started_at_ms=10_000,
        ended_at_ms=60_000,
    )
    calibration_path = tmp_path / "video_clock_calibrations.jsonl"
    calibration_store = VideoClockCalibrationStore(calibration_path)
    dialog = PassageReviewDialog(
        passage_store,
        timeline_store,
        regular_camera_indexes=(1, 2),
        calibration_store=calibration_store,
    )
    dialog.show()
    qapp.processEvents()
    dialog._batch_mode = True
    first_pane, second_pane = dialog.regular_panes
    assert first_pane.location.segment.segment_id == camera_one.segment_id
    assert second_pane.location.segment.segment_id == camera_two.segment_id

    association_one = PassageEvidenceAssociation(
        passage_event_id=first.event_id,
        bib=first.bib,
        confirmed_source=REGULAR_SOURCE,
        segment_id=camera_one.segment_id,
        frame_index=100,
        position_ms=3_700,
        marker_x_normalized=0.5,
        marker_y_normalized=0.5,
        confirmed_at_ms=50_000,
    )
    association_two = PassageEvidenceAssociation(
        passage_event_id=first.event_id,
        bib=first.bib,
        confirmed_source=REGULAR_SOURCE,
        segment_id=camera_two.segment_id,
        frame_index=110,
        position_ms=4_100,
        marker_x_normalized=0.5,
        marker_y_normalized=0.5,
        confirmed_at_ms=50_001,
    )
    assert dialog._calibrate_continuous_session(first, first_pane, association_one)
    assert dialog._calibrate_continuous_session(first, second_pane, association_two)
    assert sorted(dialog._continuous_clock_offsets.values()) == [-1_300, -900]
    persisted = VideoClockCalibrationStore(calibration_path)
    assert sorted(item.offset_ms for item in persisted.calibrations()) == [-1_300, -900]
    dialog.close()

    reopened = PassageReviewDialog(
        passage_store,
        timeline_store,
        regular_camera_indexes=(1, 2),
        calibration_store=VideoClockCalibrationStore(calibration_path),
    )
    assert reopened._enter_batch_mode(first.event_id)
    assert sorted(reopened._continuous_clock_offsets.values()) == [-1_300, -900]
    reopened.close()


def test_clicking_first_roster_number_calibrates_current_camera_immediately(
    qapp,
    tmp_path,
    fake_playback,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    first = _event(event_id="passage-first", sequence=1, passage_time_ms=15_000, bib="4")
    second = _event(event_id="passage-second", sequence=2, passage_time_ms=18_000, bib="1")
    passage_store.append(first)
    passage_store.append(second)
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01_archive_0000.mkv",
        source_id="camera_01_review",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=60_000,
    )
    calibration_store = VideoClockCalibrationStore(
        tmp_path / "video_clock_calibrations.jsonl"
    )
    dialog = PassageReviewDialog(
        passage_store,
        timeline_store,
        calibration_store=calibration_store,
    )
    dialog.show()
    qapp.processEvents()
    assert dialog._enter_batch_mode(first.event_id)
    pane = dialog.regular_pane
    worker = pane._worker
    frame = QImage(1280, 720, QImage.Format_RGB888)
    frame.fill(0)
    worker.frame_ready.emit(frame, 3_700, 92)
    qapp.processEvents()

    dialog._select_batch_event_at_current_frame(first.event_id, pane)
    qapp.processEvents()

    assert dialog._continuous_clock_offsets
    assert tuple(dialog._continuous_clock_offsets.values()) == (-1_300,)
    assert calibration_store.calibrations()[0].anchor_event_id == first.event_id
    assert dialog.association_store.get(first.event_id, REGULAR_SOURCE) is None
    assert pane.video_view._marker_mode is True
    dialog.close()


def test_continuous_mode_resumes_at_first_unconfirmed_event_after_reopen(
    qapp,
    tmp_path,
    fake_playback,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    events = (
        _event(event_id="passage-4", sequence=1, passage_time_ms=15_000, bib="4"),
        _event(event_id="passage-1", sequence=2, passage_time_ms=18_000, bib="1"),
        _event(event_id="passage-62", sequence=3, passage_time_ms=22_000, bib="62"),
    )
    for event in events:
        passage_store.append(event)
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01_session_archive_0000.mkv",
        source_id="camera_01_review",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=60_000,
    )
    journal_path = tmp_path / "passage_evidence_associations.jsonl"
    association_store = PassageEvidenceAssociationStore(journal_path)

    dialog = PassageReviewDialog(
        passage_store,
        timeline_store,
        association_store=association_store,
    )
    dialog.show()
    qapp.processEvents()
    assert dialog._enter_batch_mode(events[0].event_id)
    pane = dialog.regular_pane
    worker = pane._worker
    frame = QImage(1280, 720, QImage.Format_RGB888)
    frame.fill(0)

    worker.frame_ready.emit(frame, 3_700, 92)
    qapp.processEvents()
    QTest.mouseClick(
        pane.video_view.viewport(),
        Qt.LeftButton,
        pos=pane.video_view.viewport().rect().center(),
    )
    QTest.keyClick(pane.video_view, Qt.Key_Return)
    qapp.processEvents()
    assert dialog._selected_event_id == events[1].event_id

    worker.frame_ready.emit(frame, 4_500, 112)
    qapp.processEvents()
    QTest.mouseClick(
        pane.video_view.viewport(),
        Qt.LeftButton,
        pos=pane.video_view.viewport().rect().center(),
    )
    QTest.keyClick(pane.video_view, Qt.Key_Return)
    qapp.processEvents()
    assert dialog._selected_event_id == events[2].event_id
    dialog.close()

    reopened = PassageReviewDialog(
        passage_store,
        timeline_store,
        association_store=PassageEvidenceAssociationStore(journal_path),
    )
    reopened.show()
    qapp.processEvents()
    assert reopened._selected_event_id == events[0].event_id
    assert reopened._enter_batch_mode()
    qapp.processEvents()

    assert reopened._selected_event_id == events[2].event_id
    assert reopened.table.currentRow() == 2
    assert tuple(reopened._continuous_clock_offsets.values()) == (-1_300,)
    reopened.close()


def test_continuous_previous_restores_saved_frame_and_reconfirms_previous_event(
    qapp,
    tmp_path,
    fake_playback,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(
        _event(event_id="passage-246", sequence=1, passage_time_ms=15_000, bib="246")
    )
    passage_store.append(
        _event(event_id="passage-235", sequence=2, passage_time_ms=18_000, bib="235")
    )
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01.mkv",
        source_id="camera_01",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=25_000,
    )
    association_store = PassageEvidenceAssociationStore(
        tmp_path / "passage_evidence_associations.jsonl"
    )

    dialog = PassageReviewDialog(
        passage_store,
        timeline_store,
        association_store=association_store,
    )
    dialog.show()
    qapp.processEvents()
    assert dialog._enter_batch_mode("passage-246")
    pane = dialog.regular_pane
    worker = pane._worker
    frame = QImage(1280, 720, QImage.Format_RGB888)
    frame.fill(0)
    worker.frame_ready.emit(frame, 7_250, 181)
    qapp.processEvents()
    QTest.mouseClick(
        pane.video_view.viewport(),
        Qt.LeftButton,
        pos=pane.video_view.viewport().rect().center(),
    )
    QTest.keyClick(pane.video_view, Qt.Key_Return)
    qapp.processEvents()
    assert dialog._selected_event_id == "passage-235"
    worker.seek_calls.clear()

    QTest.mouseClick(dialog.previous_passage_btn, Qt.LeftButton)
    qapp.processEvents()

    assert dialog._selected_event_id == "passage-246"
    assert pane.association is not None
    assert pane.association.passage_event_id == "passage-246"
    assert worker.seek_calls == [7_250]

    QTest.mouseClick(pane.mark_btn, Qt.LeftButton)
    QTest.mouseClick(
        pane.video_view.viewport(),
        Qt.LeftButton,
        pos=pane.video_view.viewport().rect().center(),
    )
    QTest.keyClick(pane.video_view, Qt.Key_Return)
    qapp.processEvents()

    revised = association_store.get("passage-246", REGULAR_SOURCE)
    assert revised is not None
    assert revised.revision == 2
    assert association_store.get("passage-235", REGULAR_SOURCE) is None
    assert dialog._selected_event_id == "passage-235"
    dialog.close()


def test_continuous_previous_keeps_frame_across_long_unconfirmed_gap(
    qapp,
    tmp_path,
    fake_playback,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(
        _event(event_id="passage-4", sequence=1, passage_time_ms=15_000, bib="4")
    )
    passage_store.append(
        _event(event_id="passage-35", sequence=2, passage_time_ms=40_000, bib="35")
    )
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01_session_archive_0000.mkv",
        source_id="camera_01_review",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=60_000,
    )

    dialog = PassageReviewDialog(passage_store, timeline_store)
    dialog.show()
    qapp.processEvents()
    assert dialog._enter_batch_mode("passage-4")
    pane = dialog.regular_pane
    worker = pane._worker
    frame = QImage(1280, 720, QImage.Format_RGB888)
    frame.fill(0)
    worker.frame_ready.emit(frame, 5_000, 125)
    qapp.processEvents()
    dialog._move_selection(1)
    worker.frame_ready.emit(frame, 30_000, 750)
    qapp.processEvents()
    worker.seek_calls.clear()

    QTest.mouseClick(dialog.previous_passage_btn, Qt.LeftButton)
    qapp.processEvents()

    assert dialog._selected_event_id == "passage-4"
    assert worker.seek_calls == []
    assert dialog._shared_delta_ms == 25_000
    dialog.close()


def test_continuous_mode_does_not_start_inactive_camera_decoder(
    qapp,
    tmp_path,
    fake_playback,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(
        _event(event_id="passage-4", sequence=1, passage_time_ms=15_000, bib="4")
    )
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    for camera_index in (1, 2):
        _add_segment(
            timeline_store,
            tmp_path / "videos" / f"camera_{camera_index:02d}_archive_0000.mkv",
            source_id=f"camera_{camera_index:02d}_review",
            camera_index=camera_index,
            started_at_ms=10_000,
            ended_at_ms=20_000,
        )

    dialog = PassageReviewDialog(passage_store, timeline_store)
    assert dialog._enter_batch_mode("passage-4")
    qapp.processEvents()
    first_worker, second_worker = fake_playback.instances

    QTest.qWait(dialog.INACTIVE_CAMERA_START_DELAY_MS + 100)

    assert first_worker.start_calls == 1
    assert second_worker.start_calls == 0
    dialog.close()


def test_identity_search_selects_latest_matching_passage(qapp, tmp_path):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(
        _event(
            event_id="passage-130-old",
            sequence=1,
            passage_time_ms=10_000,
            bib="130",
            chip_id="chip-130",
        )
    )
    passage_store.append(
        _event(
            event_id="passage-130-latest",
            sequence=2,
            passage_time_ms=20_000,
            bib="130",
            chip_id="chip-130",
        )
    )
    dialog = PassageReviewDialog(
        passage_store,
        VideoTimelineStore(tmp_path / "video_timeline.jsonl"),
    )

    dialog.identity_search.setText("130")
    dialog._find_identity()
    qapp.processEvents()

    assert dialog._selected_event_id == "passage-130-latest"
    assert dialog.table.currentRow() == next(
        index
        for index, event in enumerate(dialog._visible_events)
        if event.event_id == "passage-130-latest"
    )
    dialog.close()


def test_selected_passage_uses_cyclerace_display_metadata(qapp, tmp_path):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(
        _event(
            bib="15",
            chip_id="261623",
            race_name="2026 城市自行车赛",
            stage_name="第一赛段",
            group_name="男子公开组",
            athlete_name="张三",
            team_name="示例车队",
        )
    )

    dialog = PassageReviewDialog(
        passage_store,
        VideoTimelineStore(tmp_path / "video_timeline.jsonl"),
    )
    qapp.processEvents()

    assert dialog.race_value.text() == "2026 城市自行车赛"
    assert dialog.stage_value.text() == "第一赛段"
    assert dialog.group_value.text() == "男子公开组"
    assert dialog.selected_identity_value.text() == "15"
    assert dialog.athlete_value.text() == "张三"
    assert dialog.team_value.text() == "示例车队"
    visible_columns = [
        column
        for column in range(dialog.table.columnCount())
        if not dialog.table.isColumnHidden(column)
    ]
    assert [
        dialog.table.horizontalHeaderItem(column).text()
        for column in visible_columns
    ] == [
        "序号",
        "号码",
        "姓名",
        "通过时间",
        "复核状态",
    ]
    assert dialog.table.item(0, 0).text() == "1"
    assert dialog.table.item(0, 1).text() == "15"
    assert dialog.table.item(0, 2).text() == "张三"
    assert (
        "QTableWidget::item:selected { background: #dcecf8; }"
        in dialog.styleSheet()
    )
    assert dialog.table.palette().color(QPalette.HighlightedText).name() == "#17212b"
    assert dialog.current_passage_label.text() == "15 张三"
    assert dialog.group_combo.itemText(1) == "男子公开组"
    assert dialog.group_combo.itemData(1) == "men-open"
    dialog.close()


def test_group_filter_popup_fits_long_group_names(qapp, tmp_path):
    long_group_name = "山地自行车男子公开组"
    metadata_store = RaceMetadataStore(tmp_path / "race_metadata.json")
    metadata_store.store(
        RaceMetadata(
            race_id="race-1",
            stage_id="stage-1",
            revision=1,
            emitted_at_ms=1,
            groups=(RaceGroupMetadata("mountain-open", long_group_name),),
        )
    )
    dialog = PassageReviewDialog(
        PassageEventStore(tmp_path / "passages.jsonl"),
        VideoTimelineStore(tmp_path / "video_timeline.jsonl"),
        metadata_store=metadata_store,
    )
    qapp.processEvents()

    group_index = dialog.group_combo.findData("mountain-open")
    assert group_index >= 0
    assert dialog.group_combo.minimumWidth() >= 180
    assert dialog.group_combo.view().minimumWidth() >= (
        dialog.group_combo.fontMetrics().horizontalAdvance(long_group_name) + 44
    )
    assert (
        dialog.group_combo.itemData(group_index, Qt.ToolTipRole)
        == long_group_name
    )
    dialog.close()


def test_review_never_displays_chip_id_as_the_athlete_number(qapp, tmp_path):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(
        _event(bib="", chip_id="261623", athlete_name="张三")
    )

    dialog = PassageReviewDialog(
        passage_store,
        VideoTimelineStore(tmp_path / "video_timeline.jsonl"),
    )
    qapp.processEvents()

    assert dialog.table.item(0, 1).text() == "未知"
    assert dialog.table.item(0, 2).text() == "张三"
    assert dialog.selected_identity_value.text() == "未知"
    assert "261623" not in dialog.regular_pane.mark_btn.text()
    assert "261623" not in dialog.high_speed_pane.mark_btn.text()
    dialog.close()


def test_race_metadata_populates_context_before_first_passage(qapp, tmp_path):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(_event(event_id="old-test", bib="TEST-15"))
    metadata_store = RaceMetadataStore(tmp_path / "race_metadata.json")
    metadata_store.store(
        RaceMetadata(
            race_id="race-11",
            stage_id="stage-1",
            revision=1,
            emitted_at_ms=1,
            race_name="11",
            stage_name="1",
            stage_date="2026-08-22",
            groups=(RaceGroupMetadata("elite-men", "男子精英组"),),
            athletes=(
                RaceAthleteMetadata(
                    athlete_id="15",
                    bib="15",
                    name="十五号运动员",
                    team_name="示例队",
                    group_id="elite-men",
                    chip_ids=("261623",),
                ),
            ),
        )
    )

    dialog = PassageReviewDialog(
        passage_store,
        VideoTimelineStore(tmp_path / "video_timeline.jsonl"),
        metadata_store=metadata_store,
    )
    qapp.processEvents()

    assert dialog.table.rowCount() == 0
    assert dialog.race_value.text() == "11"
    assert dialog.stage_value.text() == "1"
    assert dialog.group_combo.itemText(1) == "男子精英组"
    assert dialog.group_combo.itemData(1) == "elite-men"

    dialog.identity_search.setText("261623")
    dialog._find_identity()

    assert dialog.selected_identity_value.text() == "15"
    assert dialog.athlete_value.text() == "十五号运动员"
    assert dialog.team_value.text() == "示例队"
    assert dialog.selected_time_value.text() == "尚无通过记录"
    dialog.close()


def test_selected_identity_does_not_pollute_search_across_metadata_context(
    qapp,
    tmp_path,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(_event(event_id="old-test", bib="TEST-15"))
    metadata_store = RaceMetadataStore(tmp_path / "race_metadata.json")
    dialog = PassageReviewDialog(
        passage_store,
        VideoTimelineStore(tmp_path / "video_timeline.jsonl"),
        metadata_store=metadata_store,
    )
    qapp.processEvents()
    assert dialog.identity_search.text() == ""
    assert dialog.regular_pane.mark_btn.text() == "标线"

    metadata_store.store(
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
    dialog.refresh()
    qapp.processEvents()

    assert dialog.table.rowCount() == 0
    assert dialog.identity_search.text() == ""
    assert dialog.regular_pane.mark_btn.text() == "标线"
    assert dialog.high_speed_pane.mark_btn.text() == "标线"
    dialog.close()


def test_focus_athlete_selects_latest_passage_and_switches_group(qapp, tmp_path):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(
        _event(
            event_id="passage-15-first",
            sequence=1,
            passage_time_ms=10_000,
            group_id="elite-men",
            bib="15",
            athlete_id="15",
        )
    )
    passage_store.append(
        _event(
            event_id="passage-15-latest",
            sequence=2,
            passage_time_ms=20_000,
            group_id="elite-men",
            bib="15",
            athlete_id="15",
        )
    )
    metadata_store = RaceMetadataStore(tmp_path / "race_metadata.json")
    metadata_store.store(
        RaceMetadata(
            race_id="race-1",
            stage_id="stage-1",
            revision=1,
            emitted_at_ms=1,
            race_name="11",
            stage_name="1",
            groups=(
                RaceGroupMetadata("elite-men", "男子精英组"),
                RaceGroupMetadata("women-open", "女子公开组"),
            ),
        )
    )
    dialog = PassageReviewDialog(
        passage_store,
        VideoTimelineStore(tmp_path / "video_timeline.jsonl"),
        metadata_store=metadata_store,
    )
    dialog.group_combo.setCurrentIndex(dialog.group_combo.findData("women-open"))

    assert dialog.focus_athlete(
        "race-1",
        "stage-1",
        athlete_id="15",
        bib="15",
        group_id="elite-men",
    ) is True

    assert dialog.group_combo.currentData() == "elite-men"
    assert dialog._selected_event_id == "passage-15-latest"
    assert dialog.selected_identity_value.text() == "15"
    dialog.close()


def test_focus_athlete_without_passage_clears_stale_video_and_shows_roster(
    qapp,
    tmp_path,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(
        _event(event_id="passage-12", bib="12", athlete_id="12")
    )
    metadata_store = RaceMetadataStore(tmp_path / "race_metadata.json")
    metadata_store.store(
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
    dialog = PassageReviewDialog(
        passage_store,
        VideoTimelineStore(tmp_path / "video_timeline.jsonl"),
        metadata_store=metadata_store,
    )
    assert dialog._selected_event_id == "passage-12"

    assert dialog.focus_athlete(
        "race-1",
        "stage-1",
        athlete_id="15",
        bib="15",
        group_id="men-open",
    ) is True

    assert dialog._selected_event_id == ""
    assert dialog.selected_identity_value.text() == "15"
    assert dialog.athlete_value.text() == "十五号运动员"
    assert dialog.team_value.text() == "示例队"
    assert dialog.selected_time_value.text() == "尚无通过记录"
    assert dialog.regular_pane._event is None
    assert dialog.high_speed_pane._event is None
    assert dialog.identity_search.text() == ""
    assert dialog.table.rowCount() == 1
    dialog.close()


def test_switching_video_files_does_not_wait_on_ui_thread(
    qapp,
    tmp_path,
    fake_playback,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(
        _event(event_id="passage-1", sequence=1, passage_time_ms=15_000)
    )
    passage_store.append(
        _event(event_id="passage-2", sequence=2, passage_time_ms=35_000, bib="15")
    )
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01_first.mkv",
        source_id="camera_01_first",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=20_000,
    )
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01_second.mkv",
        source_id="camera_01_second",
        camera_index=1,
        started_at_ms=30_000,
        ended_at_ms=40_000,
    )

    dialog = PassageReviewDialog(passage_store, timeline_store)
    qapp.processEvents()
    first_worker = fake_playback.instances[0]

    dialog.table.selectRow(1)
    qapp.processEvents()

    assert first_worker.stopped
    assert first_worker.wait_calls == []
    assert len(fake_playback.instances) == 2
    assert fake_playback.instances[1].video_path.name == "camera_01_second.mkv"
    dialog.close()


def test_review_rejects_external_clip_from_another_race(qapp, tmp_path):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(_event())
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "other-race.mkv",
        source_id="high_speed_01",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=20_000,
        clock_source="external_test_clock",
        race_id="race-2",
    )

    dialog = PassageReviewDialog(passage_store, timeline_store)
    qapp.processEvents()

    assert dialog.table.item(0, 6).text() == "未确认"
    assert dialog.table.item(0, 7).text() == "未确认"
    assert dialog.table.item(0, 8).text() == "未确认"
    dialog.close()


def test_frame_step_controls_only_focused_pane_at_its_native_frame_rate(
    qapp,
    tmp_path,
    fake_playback,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(_event(passage_time_ms=20_050))
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01.mkv",
        source_id="camera_01",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=30_000,
    )
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "high_speed_01.mp4",
        source_id="high_speed_01",
        camera_index=2,
        started_at_ms=19_000,
        ended_at_ms=22_000,
        clock_source="external_clip_sidecar_beijing",
        timing_error_ms=100,
    )

    dialog = PassageReviewDialog(passage_store, timeline_store)
    dialog.show()
    qapp.processEvents()
    regular_worker, high_speed_worker = fake_playback.instances
    regular_seek_calls = list(regular_worker.seek_calls)
    high_speed_seek_calls = list(high_speed_worker.seek_calls)
    assert all(
        not shortcut.autoRepeat()
        for shortcut in dialog.regular_pane._frame_step_shortcuts
    )

    QTest.keyClick(dialog.regular_pane.video_view, Qt.Key_Right)
    qapp.processEvents()

    assert dialog._shared_delta_ms == 20
    assert regular_worker.seek_calls == regular_seek_calls
    assert regular_worker.step_calls == [1]
    assert high_speed_worker.seek_calls == high_speed_seek_calls
    assert "Δ+20 ms" in dialog.current_time_label.text()

    dialog.regular_pane.timeline.setFocus()
    QTest.keyClick(
        dialog.regular_pane.timeline,
        Qt.Key_Right,
        Qt.ShiftModifier,
    )
    qapp.processEvents()

    assert dialog._shared_delta_ms == 120
    assert regular_worker.seek_calls == regular_seek_calls
    assert regular_worker.step_calls == [1, 5]

    QTest.keyClick(
        dialog.regular_pane.timeline,
        Qt.Key_Left,
        Qt.ControlModifier,
    )
    qapp.processEvents()

    assert dialog._shared_delta_ms == -880
    assert regular_worker.seek_calls == regular_seek_calls
    assert regular_worker.step_calls == [1, 5, -50]
    assert high_speed_worker.seek_calls == high_speed_seek_calls
    dialog.close()


def test_ctrl_step_jumps_to_visual_candidate_on_current_recording(
    qapp,
    tmp_path,
    fake_playback,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(_event(passage_time_ms=20_050))
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    video_path = tmp_path / "videos" / "camera_01.mkv"
    segment = _add_segment(
        timeline_store,
        video_path,
        source_id="camera_01",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=30_000,
    )

    dialog = PassageReviewDialog(passage_store, timeline_store)
    dialog.show()
    qapp.processEvents()
    worker = fake_playback.instances[0]
    dialog.set_video_navigation_candidates(
        (
            VideoPassageCandidate(
                "candidate-late",
                1,
                22_000,
                22_000,
                22_000,
                0.2,
                0.1,
                segment_id=segment.segment_id,
                video_path=str(video_path),
                video_position_ms=12_000,
            ),
            VideoPassageCandidate(
                "candidate-early",
                1,
                18_000,
                18_000,
                18_000,
                0.2,
                0.1,
                segment_id=segment.segment_id,
                video_path=str(video_path),
                video_position_ms=8_000,
            ),
        )
    )
    # Simulate the operator already reviewing the first candidate area.
    dialog.regular_pane._current_position_ms = 9_000

    dialog._step_pane(dialog.regular_pane, 50)
    qapp.processEvents()

    assert worker.seek_calls[-1] == 12_000
    assert dialog.passage_store.get("passage-1").bib == "23"
    assert dialog._selected_event_id == "passage-1"
    dialog.close()


def test_video_arrival_queue_groups_candidates_and_opens_selected_batch(
    qapp,
    tmp_path,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    dialog = PassageReviewDialog(passage_store, timeline_store)
    candidates = (
        VideoPassageCandidate(
            "candidate-one", 1, 1_000, 1_200, 1_100, 0.2, 0.1
        ),
        VideoPassageCandidate(
            "candidate-two", 1, 6_000, 6_200, 6_100, 0.2, 0.1
        ),
        VideoPassageCandidate(
            "candidate-three", 1, 15_000, 15_200, 15_100, 0.2, 0.1
        ),
    )
    requested = []
    dialog.video_candidate_requested.connect(requested.append)

    dialog.set_video_navigation_candidates(candidates)

    assert len(dialog.video_arrival_batches()) == 2
    assert dialog.video_arrival_button.text() == "到达候选：2批/3点"
    dialog.video_arrival_button.click()
    arrival_dialog = dialog.findChild(QDialog, "videoArrivalDialog")
    assert arrival_dialog is not None
    table = arrival_dialog.findChild(QTableWidget, "videoArrivalTable")
    assert table is not None and table.rowCount() == 2
    assert table.item(0, 3).text() == "2"
    table.selectRow(0)
    open_button = arrival_dialog.findChild(QPushButton, "videoArrivalOpenButton")
    assert open_button is not None
    open_button.click()

    assert len(requested) == 1
    assert requested[0].candidate.candidate_id == "candidate-one"
    dialog.close()


def test_video_arrival_batch_gap_can_be_adjusted_in_queue(qapp, tmp_path):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    dialog = PassageReviewDialog(passage_store, timeline_store)
    dialog.set_video_navigation_candidates(
        (
            VideoPassageCandidate(
                "candidate-one", 1, 1_000, 1_100, 1_050, 0.2, 0.1
            ),
            VideoPassageCandidate(
                "candidate-two", 1, 7_000, 7_100, 7_050, 0.2, 0.1
            ),
        )
    )
    dialog.video_arrival_button.click()
    arrival_dialog = dialog.findChild(QDialog, "videoArrivalDialog")
    gap_spin = arrival_dialog.findChild(QSpinBox, "videoArrivalBatchGapSpin")

    gap_spin.setValue(5)
    qapp.processEvents()

    assert len(dialog.video_arrival_batches()) == 2
    assert dialog.video_arrival_button.text() == "到达候选：2批/2点"
    dialog.close()


def test_video_arrival_queue_defaults_to_active_camera(qapp, tmp_path):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    dialog = PassageReviewDialog(
        passage_store,
        timeline_store,
        regular_camera_indexes=(1, 2),
    )
    dialog.set_video_navigation_candidates(
        (
            VideoPassageCandidate(
                "camera-one", 1, 1_000, 1_100, 1_050, 0.2, 0.1
            ),
            VideoPassageCandidate(
                "camera-two", 2, 1_000, 1_100, 1_050, 0.2, 0.1
            ),
        )
    )

    dialog.video_arrival_button.click()
    arrival_dialog = dialog.findChild(QDialog, "videoArrivalDialog")
    table = arrival_dialog.findChild(QTableWidget, "videoArrivalTable")
    camera_filter = arrival_dialog.findChild(
        QComboBox,
        "videoArrivalCameraFilter",
    )

    assert camera_filter.currentData() == 1
    assert table.rowCount() == 1
    assert table.item(0, 2).text() == "1"
    dialog.close()


def test_ctrl_step_falls_back_to_frame_step_without_visual_candidates(
    qapp,
    tmp_path,
    fake_playback,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(_event(passage_time_ms=20_050))
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01.mkv",
        source_id="camera_01",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=30_000,
    )
    dialog = PassageReviewDialog(passage_store, timeline_store)
    dialog.show()
    qapp.processEvents()
    worker = fake_playback.instances[0]

    dialog._step_pane(dialog.regular_pane, 50)
    qapp.processEvents()

    assert worker.step_calls == [50]
    dialog.close()


def test_zoom_requests_full_resolution_without_replacing_the_worker(
    qapp,
    tmp_path,
    fake_playback,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(_event(passage_time_ms=15_000))
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01.mkv",
        source_id="camera_01",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=20_000,
    )
    dialog = PassageReviewDialog(passage_store, timeline_store)
    qapp.processEvents()
    worker = fake_playback.instances[0]
    preview = QImage(1280, 720, QImage.Format_RGB888)
    preview.fill(0)

    worker.frame_ready.emit(preview, 5_000, 250)
    qapp.processEvents()
    dialog.regular_pane.video_view.set_actual_size()
    assert _wait_until(
        lambda: bool(worker.full_resolution_calls),
        timeout_ms=dialog.regular_pane.FULL_RESOLUTION_IDLE_MS + 500,
    )

    assert fake_playback.instances == [worker]
    assert worker.full_resolution_calls[-1] == 250
    assert dialog.regular_pane.video_view.zoom_percent == 100
    assert dialog.regular_pane.video_view.sceneRect().width() == 2560
    dialog.close()


def test_full_resolution_request_waits_for_the_latest_paused_frame(
    qapp,
    tmp_path,
    fake_playback,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(_event(passage_time_ms=15_000))
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01.mkv",
        source_id="camera_01",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=20_000,
    )
    dialog = PassageReviewDialog(passage_store, timeline_store)
    qapp.processEvents()
    pane = dialog.regular_pane
    worker = fake_playback.instances[0]
    preview = QImage(1280, 720, QImage.Format_RGB888)
    preview.fill(0)

    worker.frame_ready.emit(preview, 5_000, 250)
    qapp.processEvents()
    pane.video_view.set_actual_size()
    QTest.qWait(50)
    worker.frame_ready.emit(preview, 5_080, 254)
    assert _wait_until(
        lambda: worker.full_resolution_calls == [254],
        timeout_ms=pane.FULL_RESOLUTION_IDLE_MS + 500,
    )
    dialog.close()


def test_timeline_ignores_mouse_drag_for_frame_navigation(
    qapp,
    tmp_path,
    fake_playback,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(_event(passage_time_ms=15_000))
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01.mkv",
        source_id="camera_01",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=20_000,
    )
    dialog = PassageReviewDialog(passage_store, timeline_store)
    dialog.resize(1_200, 800)
    dialog.show()
    qapp.processEvents()
    pane = dialog.regular_pane
    worker = fake_playback.instances[0]
    worker.seek_calls.clear()

    assert not pane._timeline_dragging
    assert worker.seek_calls == []
    assert pane.timeline.testAttribute(Qt.WA_TransparentForMouseEvents)
    assert pane.timeline.focusPolicy() == Qt.NoFocus
    dialog.close()


def test_manual_marker_uses_enter_while_space_controls_focused_pane(
    qapp,
    tmp_path,
    fake_playback,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(_event(passage_time_ms=20_050, bib="15", chip_id="chip-15"))
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01.mkv",
        source_id="camera_01",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=30_000,
    )
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "high_speed_01.mp4",
        source_id="high_speed_01",
        camera_index=2,
        started_at_ms=19_000,
        ended_at_ms=22_000,
        clock_source="external_clip_sidecar_beijing",
        timing_error_ms=100,
    )
    association_store = PassageEvidenceAssociationStore(
        tmp_path / "passage_evidence_associations.jsonl"
    )
    dialog = PassageReviewDialog(
        passage_store,
        timeline_store,
        association_store=association_store,
    )
    dialog.show()
    qapp.processEvents()
    regular_worker, high_speed_worker = fake_playback.instances
    frame = QImage(1280, 720, QImage.Format_RGB888)
    frame.fill(0)
    regular_worker.frame_ready.emit(frame, 10_050, 502)
    high_speed_worker.frame_ready.emit(frame, 1_050, 262)
    qapp.processEvents()

    regular_view = dialog.regular_pane.video_view
    assert regular_view._identity_badge.isVisible()
    assert regular_view._identity_badge.text() == "15"
    assert "background: #c0372b" in regular_view._identity_badge.styleSheet()
    assert regular_view._marker is None
    QTest.mouseClick(
        regular_view.viewport(),
        Qt.LeftButton,
        pos=regular_view.viewport().rect().center(),
    )
    assert dialog.regular_pane.has_pending_marker
    assert regular_view._identity_badge.text() == "15"
    assert "background: #c0372b" in regular_view._identity_badge.styleSheet()
    pending_marker = regular_view._marker
    assert pending_marker is not None
    assert pending_marker[2:] == ("15", False)

    QTest.keyClick(dialog.regular_pane.video_view, Qt.Key_Return)
    qapp.processEvents()

    regular_association = association_store.get("passage-1", REGULAR_SOURCE)
    assert regular_association is not None
    assert regular_association.frame_index == 502
    assert regular_association.position_ms == 10_050
    assert regular_association.marker_x_normalized == pytest.approx(pending_marker[0])
    assert regular_association.marker_y_normalized == pytest.approx(pending_marker[1])
    assert regular_view._identity_badge.text() == "15"
    assert "background: #1bbf83" in regular_view._identity_badge.styleSheet()
    assert not regular_view._marker_mode
    assert dialog.table.item(0, 6).text() == "已确认"
    assert dialog.table.item(0, 8).text() == "已确认"

    regular_worker.seek_calls.clear()
    high_speed_worker.seek_calls.clear()
    regular_worker.speed_calls.clear()
    high_speed_worker.speed_calls.clear()
    dialog.regular_pane.video_view.setFocus()
    QTest.keyClick(dialog.regular_pane.video_view, Qt.Key_Space)
    qapp.processEvents()
    assert not dialog._sync_playing
    assert dialog.regular_pane.is_playing
    assert not dialog.high_speed_pane.is_playing
    assert regular_worker.speed_calls == [1.0]
    assert high_speed_worker.speed_calls == []
    QTest.keyClick(dialog.regular_pane.video_view, Qt.Key_Space)
    qapp.processEvents()
    assert not dialog._sync_playing
    assert not dialog.regular_pane.is_playing
    assert association_store.get("passage-1", HIGH_SPEED_SOURCE) is None
    dialog.close()


def test_linked_playback_ticks_do_not_seek_workers_that_follow_master_clock(
    qapp,
    tmp_path,
    fake_playback,
    monkeypatch,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(_event(passage_time_ms=15_000))
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01.mkv",
        source_id="camera_01",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=20_000,
    )
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "high_speed_01.mp4",
        source_id="high_speed_01",
        camera_index=2,
        started_at_ms=14_000,
        ended_at_ms=17_000,
        clock_source="external_clip_sidecar_beijing",
        timing_error_ms=100,
    )
    dialog = PassageReviewDialog(passage_store, timeline_store)
    qapp.processEvents()
    regular_worker, high_speed_worker = fake_playback.instances
    frame = QImage(1280, 720, QImage.Format_RGB888)
    frame.fill(0)
    regular_worker.frame_ready.emit(frame, 5_000, 250)
    high_speed_worker.frame_ready.emit(frame, 1_000, 250)
    qapp.processEvents()
    for worker in (regular_worker, high_speed_worker):
        worker.seek_calls.clear()
        worker.speed_calls.clear()
        worker.pause_calls = 0

    clock = [100.0]
    monkeypatch.setattr(passage_review.time, "monotonic", lambda: clock[0])
    dialog._set_sync_playing(True)

    assert regular_worker.seek_calls == [5_000]
    assert high_speed_worker.seek_calls == [1_000]
    assert regular_worker.speed_calls == [1.0]
    assert high_speed_worker.speed_calls == [1.0]

    regular_worker.seek_calls.clear()
    high_speed_worker.seek_calls.clear()
    clock[0] = 100.12
    regular_worker.frame_ready.emit(frame, 5_120, 256)
    high_speed_worker.frame_ready.emit(frame, 1_120, 280)
    qapp.processEvents()
    dialog._on_sync_tick()

    assert dialog._shared_delta_ms == 120
    assert regular_worker.seek_calls == []
    assert high_speed_worker.seek_calls == []
    dialog._set_sync_playing(False)
    dialog.close()


def test_linked_playback_corrects_only_the_pane_outside_drift_tolerance(
    qapp,
    tmp_path,
    fake_playback,
    monkeypatch,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(_event(passage_time_ms=15_000))
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01.mkv",
        source_id="camera_01",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=20_000,
    )
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "high_speed_01.mp4",
        source_id="high_speed_01",
        camera_index=2,
        started_at_ms=14_000,
        ended_at_ms=17_000,
        clock_source="external_clip_sidecar_beijing",
        timing_error_ms=100,
    )
    dialog = PassageReviewDialog(passage_store, timeline_store)
    qapp.processEvents()
    regular_worker, high_speed_worker = fake_playback.instances
    frame = QImage(1280, 720, QImage.Format_RGB888)
    frame.fill(0)
    regular_worker.frame_ready.emit(frame, 5_000, 250)
    high_speed_worker.frame_ready.emit(frame, 1_000, 250)
    qapp.processEvents()

    clock = [200.0]
    monkeypatch.setattr(passage_review.time, "monotonic", lambda: clock[0])
    dialog._set_sync_playing(True)
    for worker in (regular_worker, high_speed_worker):
        worker.seek_calls.clear()
        worker.speed_calls.clear()

    clock[0] = 200.6
    regular_worker.frame_ready.emit(frame, 5_000, 250)
    high_speed_worker.frame_ready.emit(frame, 1_600, 400)
    qapp.processEvents()
    dialog._on_sync_tick()

    assert 599 <= dialog._shared_delta_ms <= 600
    assert regular_worker.seek_calls == [5_000 + dialog._shared_delta_ms]
    assert regular_worker.speed_calls == [1.0]
    assert high_speed_worker.seek_calls == []
    assert high_speed_worker.speed_calls == []
    dialog._set_sync_playing(False)
    dialog.close()


def test_stopping_linked_playback_seeks_once_to_exact_master_position(
    qapp,
    tmp_path,
    fake_playback,
    monkeypatch,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(_event(passage_time_ms=15_000))
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01.mkv",
        source_id="camera_01",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=20_000,
    )
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "high_speed_01.mp4",
        source_id="high_speed_01",
        camera_index=2,
        started_at_ms=14_000,
        ended_at_ms=17_000,
        clock_source="external_clip_sidecar_beijing",
        timing_error_ms=100,
    )
    dialog = PassageReviewDialog(passage_store, timeline_store)
    qapp.processEvents()
    regular_worker, high_speed_worker = fake_playback.instances
    clock = [300.0]
    monkeypatch.setattr(passage_review.time, "monotonic", lambda: clock[0])
    dialog._set_sync_playing(True)
    for worker in (regular_worker, high_speed_worker):
        worker.seek_calls.clear()
        worker.pause_calls = 0

    clock[0] = 300.25
    dialog._set_sync_playing(False)

    assert not dialog._sync_playing
    assert dialog._shared_delta_ms == 250
    assert regular_worker.pause_calls == 2
    assert high_speed_worker.pause_calls == 2
    assert regular_worker.seek_calls == [5_250]
    assert high_speed_worker.seek_calls == [1_250]
    dialog.close()


def test_switching_single_pane_playback_aligns_without_starting_other_pane(
    qapp,
    tmp_path,
    fake_playback,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(_event(passage_time_ms=15_000))
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01.mkv",
        source_id="camera_01",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=20_000,
    )
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "high_speed_01.mp4",
        source_id="high_speed_01",
        camera_index=2,
        started_at_ms=14_000,
        ended_at_ms=17_000,
        clock_source="external_clip_sidecar_beijing",
        timing_error_ms=100,
    )
    dialog = PassageReviewDialog(passage_store, timeline_store)
    qapp.processEvents()
    regular_worker, high_speed_worker = fake_playback.instances
    frame = QImage(1280, 720, QImage.Format_RGB888)
    frame.fill(0)
    regular_worker.frame_ready.emit(frame, 5_120, 256)
    high_speed_worker.frame_ready.emit(frame, 1_000, 250)
    qapp.processEvents()
    for worker in (regular_worker, high_speed_worker):
        worker.seek_calls.clear()
        worker.speed_calls.clear()
        worker.pause_calls = 0

    dialog._toggle_pane(dialog.regular_pane)

    assert dialog.regular_pane.is_playing
    assert not dialog.high_speed_pane.is_playing
    assert regular_worker.seek_calls == [5_120]
    assert regular_worker.speed_calls == [1.0]
    assert high_speed_worker.seek_calls == []
    assert high_speed_worker.speed_calls == []

    dialog._toggle_pane(dialog.high_speed_pane)

    assert dialog._active_pane is dialog.high_speed_pane
    assert not dialog.regular_pane.is_playing
    assert dialog.high_speed_pane.is_playing
    assert high_speed_worker.seek_calls == [1_120]
    assert high_speed_worker.speed_calls == [1.0]
    assert regular_worker.park_cache_calls == 1
    assert regular_worker.release_cache_calls == 0
    dialog.close()


def test_left_drag_does_not_scrub_middle_drag_pans_and_click_places_marker(
    qapp,
    tmp_path,
    fake_playback,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(_event(passage_time_ms=15_000, bib="12"))
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01.mkv",
        source_id="camera_01",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=20_000,
    )
    dialog = PassageReviewDialog(passage_store, timeline_store)
    dialog.show()
    qapp.processEvents()
    worker = fake_playback.instances[0]
    frame = QImage(1280, 720, QImage.Format_RGB888)
    frame.fill(0)
    worker.frame_ready.emit(frame, 5_000, 250)
    qapp.processEvents()

    view = dialog.regular_pane.video_view
    view.set_actual_size()
    qapp.processEvents()
    horizontal_scrollbar = view.horizontalScrollBar()
    vertical_scrollbar = view.verticalScrollBar()
    before_pan = (horizontal_scrollbar.value(), vertical_scrollbar.value())
    shared_delta_before_drag = dialog._shared_delta_ms
    worker.seek_calls.clear()
    center = view.viewport().rect().center()
    scrub_target = center + QPoint(60, 0)

    QTest.mousePress(view.viewport(), Qt.LeftButton, pos=center)
    move_event = QMouseEvent(
        QEvent.MouseMove,
        QPointF(scrub_target),
        QPointF(view.viewport().mapToGlobal(scrub_target)),
        Qt.NoButton,
        Qt.LeftButton,
        Qt.NoModifier,
    )
    QApplication.sendEvent(view.viewport(), move_event)
    QTest.mouseRelease(view.viewport(), Qt.LeftButton, pos=scrub_target)
    qapp.processEvents()

    assert dialog._shared_delta_ms == shared_delta_before_drag
    assert worker.seek_calls == []
    assert (horizontal_scrollbar.value(), vertical_scrollbar.value()) == before_pan
    assert not dialog.regular_pane.has_pending_marker
    assert view._marker is None

    pan_target = center + QPoint(60, 40)
    QTest.mousePress(view.viewport(), Qt.MiddleButton, pos=center)
    pan_event = QMouseEvent(
        QEvent.MouseMove,
        QPointF(pan_target),
        QPointF(view.viewport().mapToGlobal(pan_target)),
        Qt.NoButton,
        Qt.MiddleButton,
        Qt.NoModifier,
    )
    QApplication.sendEvent(view.viewport(), pan_event)
    QTest.mouseRelease(view.viewport(), Qt.MiddleButton, pos=pan_target)
    qapp.processEvents()

    assert (horizontal_scrollbar.value(), vertical_scrollbar.value()) != before_pan
    assert not dialog.regular_pane.has_pending_marker

    QTest.mouseClick(view.viewport(), Qt.LeftButton, pos=center)
    assert dialog.regular_pane.has_pending_marker
    assert view._marker is not None
    dialog.close()


def test_fullscreen_toggle_refits_the_entire_review_window(
    qapp,
    tmp_path,
    fake_playback,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(_event(passage_time_ms=15_000, bib="12"))
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01.mkv",
        source_id="camera_01",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=20_000,
    )
    dialog = PassageReviewDialog(passage_store, timeline_store)
    dialog.show()
    qapp.processEvents()
    worker = fake_playback.instances[0]
    frame = QImage(1280, 720, QImage.Format_RGB888)
    frame.fill(0)
    worker.frame_ready.emit(frame, 5_000, 250)
    qapp.processEvents()
    dialog.regular_pane.video_view.set_actual_size()

    dialog._toggle_fullscreen()
    qapp.processEvents()

    assert dialog.isFullScreen()
    assert dialog.fullscreen_btn.text() == "退出全屏"
    assert dialog.regular_pane.video_view._fit_mode

    dialog._toggle_fullscreen()
    qapp.processEvents()

    assert not dialog.isFullScreen()
    assert dialog.fullscreen_btn.text() == "全屏"
    dialog.close()


def test_video_scrub_throttles_preview_and_commits_exact_seek(
    qapp,
    tmp_path,
    fake_playback,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(_event(passage_time_ms=15_000, bib="12"))
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01.mkv",
        source_id="camera_01",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=20_000,
    )
    dialog = PassageReviewDialog(passage_store, timeline_store)
    dialog.show()
    qapp.processEvents()
    pane = dialog.regular_pane
    worker = fake_playback.instances[0]
    dialog._set_sync_playing(True)
    worker.seek_calls.clear()
    worker.preview_seek_calls.clear()

    pane._on_video_scrub_started()
    pane._on_video_scrub_delta(10)
    pane._on_video_scrub_delta(30)
    pane._on_video_scrub_delta(60)

    assert worker.preview_seek_calls == []
    assert _wait_until(
        lambda: len(worker.preview_seek_calls) == 1,
        timeout_ms=pane.SCRUB_PREVIEW_INTERVAL_MS + 500,
    )
    assert not dialog._sync_playing

    final_delta_ms = pane._scrub_delta_ms(80)
    pane._on_video_scrub_finished(80)
    qapp.processEvents()

    assert worker.seek_calls == [pane._target_position_ms + final_delta_ms]
    assert not pane._video_scrubbing
    assert not pane._scrub_preview_timer.isActive()
    dialog.close()


def test_video_scrub_defers_full_resolution_until_exact_frame(
    qapp,
    tmp_path,
    fake_playback,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(_event(passage_time_ms=15_000))
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01.mkv",
        source_id="camera_01",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=20_000,
    )
    dialog = PassageReviewDialog(passage_store, timeline_store)
    qapp.processEvents()
    pane = dialog.regular_pane
    worker = fake_playback.instances[0]
    frame = QImage(1280, 720, QImage.Format_RGB888)
    frame.fill(0)
    worker.frame_ready.emit(frame, 5_000, 250)
    qapp.processEvents()
    pane.video_view.set_actual_size()
    qapp.processEvents()
    worker.full_resolution_calls.clear()

    pane._on_video_scrub_started()
    worker.frame_ready.emit(frame, 5_040, 252)
    qapp.processEvents()
    assert worker.full_resolution_calls == []

    pane._on_video_scrub_finished(40)
    worker.frame_ready.emit(frame, 5_080, 254)
    assert _wait_until(
        lambda: worker.full_resolution_calls == [254],
        timeout_ms=pane.FULL_RESOLUTION_IDLE_MS + 500,
    )
    dialog.close()


def test_manual_marker_restores_and_upgrades_to_dual_source_confirmation(
    qapp,
    tmp_path,
    fake_playback,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(_event(passage_time_ms=20_050, bib="15", chip_id="chip-15"))
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    regular_segment = _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01.mkv",
        source_id="camera_01",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=30_000,
    )
    high_speed_segment = _add_segment(
        timeline_store,
        tmp_path / "videos" / "high_speed_01.mp4",
        source_id="high_speed_01",
        camera_index=2,
        started_at_ms=19_000,
        ended_at_ms=22_000,
        clock_source="external_clip_sidecar_beijing",
        timing_error_ms=100,
    )
    association_store = PassageEvidenceAssociationStore(
        tmp_path / "passage_evidence_associations.jsonl"
    )
    association_store.confirm(
        passage_event_id="passage-1",
        bib="15",
        confirmed_source=REGULAR_SOURCE,
        segment_id=regular_segment.segment_id,
        frame_index=502,
        position_ms=10_050,
        marker_x_normalized=0.25,
        marker_y_normalized=0.5,
        confirmed_at_ms=1_000,
    )

    dialog = PassageReviewDialog(
        passage_store,
        timeline_store,
        association_store=PassageEvidenceAssociationStore(
            association_store.journal_path
        ),
    )
    qapp.processEvents()
    regular_worker, high_speed_worker = fake_playback.instances
    assert regular_worker.seek_calls[-1] == 10_050
    assert high_speed_worker.seek_calls[-1] == 1_050
    assert dialog.regular_pane.association is not None
    assert dialog.table.item(0, 8).text() == "已确认"

    frame = QImage(1280, 720, QImage.Format_RGB888)
    frame.fill(0)
    regular_worker.frame_ready.emit(frame, 10_050, 502)
    high_speed_worker.frame_ready.emit(frame, 1_050, 262)
    qapp.processEvents()
    assert dialog.regular_pane.video_view._marker == (0.25, 0.5, "15", True)

    regular_worker.frame_ready.emit(frame, 10_069, 503)
    qapp.processEvents()
    assert dialog.regular_pane.video_view._marker == (0.25, 0.5, "15", True)

    regular_worker.frame_ready.emit(frame, 10_091, 505)
    qapp.processEvents()
    assert dialog.regular_pane.video_view._marker is None

    regular_worker.frame_ready.emit(frame, 10_050, 502)
    qapp.processEvents()

    high_speed_view = dialog.high_speed_pane.video_view
    high_speed_view.set_actual_size()
    high_speed_view.zoom_by(1.2)
    zoom_before_confirmation = high_speed_view.zoom_percent
    QTest.mouseClick(
        high_speed_view.viewport(),
        Qt.LeftButton,
        pos=high_speed_view.viewport().rect().center(),
    )
    QTest.keyClick(dialog.high_speed_pane.video_view, Qt.Key_Return)
    qapp.processEvents()

    high_speed_association = dialog.association_store.get(
        "passage-1", HIGH_SPEED_SOURCE
    )
    assert high_speed_association is not None
    assert high_speed_association.segment_id == high_speed_segment.segment_id
    assert high_speed_view.zoom_percent == zoom_before_confirmation
    assert dialog.table.item(0, 8).text() == "已确认"
    assert dialog.source_value.text() == "已确认"
    assert dialog.table.item(0, 6).foreground().color().name() == "#16845b"
    assert dialog.table.item(0, 7).foreground().color().name() == "#16845b"
    dialog.close()


def test_escape_cancels_pending_marker_and_delete_clears_confirmed_marker(
    qapp,
    tmp_path,
    fake_playback,
    monkeypatch,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(_event(passage_time_ms=15_000, bib="15"))
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    segment = _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01.mkv",
        source_id="camera_01",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=20_000,
    )
    association_store = PassageEvidenceAssociationStore(
        tmp_path / "passage_evidence_associations.jsonl"
    )
    association_store.confirm(
        passage_event_id="passage-1",
        bib="15",
        confirmed_source=REGULAR_SOURCE,
        segment_id=segment.segment_id,
        frame_index=250,
        position_ms=5_000,
        marker_x_normalized=0.4,
        marker_y_normalized=0.6,
        confirmed_at_ms=1_000,
    )
    dialog = PassageReviewDialog(
        passage_store,
        timeline_store,
        association_store=association_store,
    )
    qapp.processEvents()
    worker = fake_playback.instances[0]
    frame = QImage(1280, 720, QImage.Format_RGB888)
    frame.fill(0)
    worker.frame_ready.emit(frame, 5_000, 250)
    qapp.processEvents()

    regular_view = dialog.regular_pane.video_view
    assert not regular_view._marker_mode
    QTest.mouseClick(
        regular_view.viewport(),
        Qt.LeftButton,
        pos=regular_view.viewport().rect().center(),
    )
    assert not dialog.regular_pane.has_pending_marker
    assert dialog.regular_pane.video_view._marker == (0.4, 0.6, "15", True)

    QTest.mouseClick(dialog.regular_pane.mark_btn, Qt.LeftButton)
    assert regular_view._marker_mode
    QTest.mouseClick(
        regular_view.viewport(),
        Qt.LeftButton,
        pos=regular_view.viewport().rect().center(),
    )
    assert dialog.regular_pane.has_pending_marker
    QTest.keyClick(dialog.regular_pane.video_view, Qt.Key_Escape)
    assert not dialog.regular_pane.has_pending_marker
    assert dialog.regular_pane.video_view._marker == (0.4, 0.6, "15", True)
    assert not regular_view._marker_mode

    monkeypatch.setattr(
        passage_review.QMessageBox,
        "question",
        lambda *args, **kwargs: passage_review.QMessageBox.Yes,
    )
    QTest.keyClick(dialog.regular_pane.video_view, Qt.Key_Delete)
    qapp.processEvents()
    assert association_store.get("passage-1", REGULAR_SOURCE) is None
    assert dialog.table.item(0, 6).text() == "未确认"
    assert dialog.table.item(0, 8).text() == "未确认"
    dialog.close()


def test_enter_stays_by_default_and_opt_in_auto_advance_moves_to_next_passage(
    qapp,
    tmp_path,
    fake_playback,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(
        _event(event_id="passage-12", sequence=1, passage_time_ms=15_000, bib="12")
    )
    passage_store.append(
        _event(event_id="passage-15", sequence=2, passage_time_ms=16_000, bib="15")
    )
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01.mkv",
        source_id="camera_01",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=20_000,
    )
    association_store = PassageEvidenceAssociationStore(
        tmp_path / "passage_evidence_associations.jsonl"
    )
    dialog = PassageReviewDialog(
        passage_store,
        timeline_store,
        association_store=association_store,
    )
    dialog.show()
    qapp.processEvents()
    worker = fake_playback.instances[0]
    frame = QImage(1280, 720, QImage.Format_RGB888)
    frame.fill(0)
    worker.frame_ready.emit(frame, 5_000, 250)
    qapp.processEvents()

    view = dialog.regular_pane.video_view
    QTest.mouseClick(
        view.viewport(),
        Qt.LeftButton,
        pos=view.viewport().rect().center(),
    )
    QTest.keyClick(view, Qt.Key_Return)
    qapp.processEvents()

    assert not dialog.auto_advance_checkbox.isChecked()
    assert association_store.get("passage-12", REGULAR_SOURCE) is not None
    assert dialog.table.item(0, 8).text() == "已确认"
    assert dialog.table.currentRow() == 0
    assert dialog._selected_event_id == "passage-12"

    worker.frame_ready.emit(frame, 5_000, 250)
    qapp.processEvents()
    dialog.auto_advance_checkbox.setChecked(True)
    QTest.mouseClick(dialog.regular_pane.mark_btn, Qt.LeftButton)
    QTest.mouseClick(
        view.viewport(),
        Qt.LeftButton,
        pos=view.viewport().rect().center(),
    )
    QTest.keyClick(view, Qt.Key_Return)
    qapp.processEvents()

    assert dialog.table.currentRow() == 1
    assert dialog._selected_event_id == "passage-15"
    assert dialog.selected_identity_value.text() == "15"
    assert dialog.identity_search.text() == ""

    QTest.keyClick(view, Qt.Key_PageUp)
    qapp.processEvents()
    assert dialog.table.currentRow() == 0
    assert dialog._selected_event_id == "passage-12"
    dialog.close()


def test_short_high_speed_capture_does_not_limit_regular_video_scrubbing(
    qapp,
    tmp_path,
    fake_playback,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(_event(passage_time_ms=15_000, bib="12"))
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01.mkv",
        source_id="camera_01",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=20_000,
    )
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "high_speed_01.mp4",
        source_id="high_speed_01",
        camera_index=2,
        started_at_ms=14_900,
        ended_at_ms=15_100,
        clock_source="external_clip_sidecar_beijing",
        timing_error_ms=100,
    )
    dialog = PassageReviewDialog(passage_store, timeline_store)
    qapp.processEvents()
    regular_worker, high_speed_worker = fake_playback.instances

    dialog._seek_both_delta(-2_000)

    assert dialog._shared_delta_ms == -2_000
    assert regular_worker.seek_calls[-1] == 3_000
    assert high_speed_worker.seek_calls[-1] == 0
    dialog.close()


def test_confirm_updates_current_row_without_full_refresh_or_reseek(
    qapp,
    tmp_path,
    fake_playback,
    monkeypatch,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(
        _event(event_id="passage-12", passage_time_ms=15_000, bib="12")
    )
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01.mkv",
        source_id="camera_01",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=20_000,
    )
    association_store = PassageEvidenceAssociationStore(
        tmp_path / "passage_evidence_associations.jsonl"
    )
    dialog = PassageReviewDialog(
        passage_store,
        timeline_store,
        association_store=association_store,
    )
    qapp.processEvents()
    worker = fake_playback.instances[0]
    frame = QImage(1280, 720, QImage.Format_RGB888)
    frame.fill(0)
    worker.frame_ready.emit(frame, 5_000, 250)
    qapp.processEvents()

    refresh_calls = 0

    def counted_refresh():
        nonlocal refresh_calls
        refresh_calls += 1

    monkeypatch.setattr(dialog, "refresh", counted_refresh)
    seek_calls_before = list(worker.seek_calls)
    view = dialog.regular_pane.video_view
    QTest.mouseClick(
        view.viewport(),
        Qt.LeftButton,
        pos=view.viewport().rect().center(),
    )
    QTest.keyClick(view, Qt.Key_Return)
    qapp.processEvents()

    assert refresh_calls == 0
    assert worker.seek_calls == seek_calls_before
    assert association_store.get("passage-12", REGULAR_SOURCE) is not None
    assert dialog.table.item(0, 8).text() == "已确认"
    assert dialog.source_value.text() == "已确认"
    dialog.close()


def test_failed_confirmation_keeps_current_passage_and_pending_marker(
    qapp,
    tmp_path,
    fake_playback,
    monkeypatch,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(
        _event(event_id="passage-12", passage_time_ms=15_000, bib="12")
    )
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01.mkv",
        source_id="camera_01",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=20_000,
    )
    association_store = PassageEvidenceAssociationStore(
        tmp_path / "passage_evidence_associations.jsonl"
    )
    dialog = PassageReviewDialog(
        passage_store,
        timeline_store,
        association_store=association_store,
    )
    qapp.processEvents()
    worker = fake_playback.instances[0]
    frame = QImage(1280, 720, QImage.Format_RGB888)
    frame.fill(0)
    worker.frame_ready.emit(frame, 5_000, 250)
    qapp.processEvents()
    view = dialog.regular_pane.video_view
    QTest.mouseClick(
        view.viewport(),
        Qt.LeftButton,
        pos=view.viewport().rect().center(),
    )
    monkeypatch.setattr(
        association_store,
        "confirm",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("disk unavailable")),
    )
    messages = []
    monkeypatch.setattr(
        passage_review.QMessageBox,
        "critical",
        lambda *args: messages.append(args),
    )

    confirmed = dialog._confirm_pending_marker(dialog.regular_pane)

    assert confirmed is False
    assert dialog._selected_event_id == "passage-12"
    assert dialog.regular_pane.has_pending_marker
    assert association_store.get("passage-12", REGULAR_SOURCE) is None
    assert messages
    dialog.close()


def test_confirm_and_advance_keeps_current_frame_in_same_recording(
    qapp,
    tmp_path,
    fake_playback,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(
        _event(event_id="passage-12", sequence=1, passage_time_ms=15_000, bib="12")
    )
    passage_store.append(
        _event(event_id="passage-15", sequence=2, passage_time_ms=16_000, bib="15")
    )
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01.mkv",
        source_id="camera_01",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=20_000,
    )
    dialog = PassageReviewDialog(passage_store, timeline_store)
    dialog.auto_advance_checkbox.setChecked(True)
    qapp.processEvents()
    worker = fake_playback.instances[0]
    frame = QImage(1280, 720, QImage.Format_RGB888)
    frame.fill(0)
    worker.frame_ready.emit(frame, 5_000, 250)
    qapp.processEvents()

    seek_count_before = len(worker.seek_calls)
    view = dialog.regular_pane.video_view
    QTest.mouseClick(
        view.viewport(),
        Qt.LeftButton,
        pos=view.viewport().rect().center(),
    )
    QTest.keyClick(view, Qt.Key_Return)
    qapp.processEvents()

    assert dialog._selected_event_id == "passage-15"
    assert worker.seek_calls[seek_count_before:] == []
    assert dialog.regular_pane._current_position_ms == 5_000
    dialog.close()


def test_refresh_after_new_passage_preserves_selected_video_position(
    qapp,
    tmp_path,
    fake_playback,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(
        _event(event_id="passage-12", sequence=1, passage_time_ms=15_000, bib="12")
    )
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01.mkv",
        source_id="camera_01",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=20_000,
    )
    dialog = PassageReviewDialog(passage_store, timeline_store)
    qapp.processEvents()
    worker = fake_playback.instances[0]
    frame = QImage(1280, 720, QImage.Format_RGB888)
    frame.fill(0)
    worker.frame_ready.emit(frame, 5_120, 256)
    qapp.processEvents()
    seek_calls_before = list(worker.seek_calls)

    passage_store.append(
        _event(event_id="passage-15", sequence=2, passage_time_ms=16_000, bib="15")
    )
    dialog.refresh()
    qapp.processEvents()

    assert dialog.table.rowCount() == 2
    assert dialog._selected_event_id == "passage-12"
    assert worker.seek_calls == seek_calls_before
    dialog.close()


def test_refresh_invalidates_negative_lookup_after_timeline_changes(
    qapp,
    tmp_path,
    fake_playback,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(
        _event(event_id="passage-15", passage_time_ms=15_000, bib="15")
    )
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    dialog = PassageReviewDialog(passage_store, timeline_store)
    qapp.processEvents()
    assert dialog._lookups["passage-15"].status == "no_segments"

    _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01.mkv",
        source_id="camera_01",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=20_000,
    )
    dialog.refresh()
    qapp.processEvents()

    assert dialog._lookups["passage-15"].status == "located"
    assert dialog.regular_pane.location is not None
    dialog.close()


def test_incremental_refresh_appends_only_the_new_passage_row(
    qapp,
    tmp_path,
    fake_playback,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(
        _event(event_id="passage-12", sequence=1, passage_time_ms=15_000, bib="12")
    )
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01.mkv",
        source_id="camera_01",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=20_000,
    )
    dialog = PassageReviewDialog(passage_store, timeline_store)
    qapp.processEvents()
    worker = fake_playback.instances[0]
    first_row_item = dialog.table.item(0, 0)
    seek_calls_before = list(worker.seek_calls)

    passage_store.append(
        _event(event_id="passage-15", sequence=2, passage_time_ms=16_000, bib="15")
    )
    dialog.refresh_events(("passage-15",))
    qapp.processEvents()

    assert dialog.table.rowCount() == 2
    assert dialog.table.item(0, 0) is first_row_item
    assert dialog.table.item(1, 1).text() == "15"
    assert dialog._selected_event_id == "passage-12"
    assert worker.seek_calls == seek_calls_before
    assert dialog.next_passage_btn.isEnabled()
    dialog.close()


def test_incremental_refresh_renumbers_rows_after_inserting_earlier_passage(
    qapp,
    tmp_path,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(
        _event(event_id="passage-later", passage_time_ms=20_000, bib="20")
    )
    dialog = PassageReviewDialog(
        passage_store,
        VideoTimelineStore(tmp_path / "video_timeline.jsonl"),
    )

    passage_store.append(
        _event(event_id="passage-earlier", passage_time_ms=10_000, bib="10")
    )
    dialog.refresh_events(("passage-earlier",))
    qapp.processEvents()

    assert [dialog.table.item(row, 0).text() for row in range(2)] == ["1", "2"]
    assert [dialog.table.item(row, 1).text() for row in range(2)] == ["10", "20"]
    dialog.close()


def test_incremental_refresh_removes_inactive_passage_revision(qapp, tmp_path):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    active = _event(event_id="passage-15", bib="15")
    passage_store.append(active)
    dialog = PassageReviewDialog(
        passage_store,
        VideoTimelineStore(tmp_path / "video_timeline.jsonl"),
    )
    assert dialog.table.rowCount() == 1

    passage_store.append(
        _event(
            event_id=active.event_id,
            bib="15",
            revision=2,
            is_active=False,
        )
    )
    dialog.refresh_events((active.event_id,))
    qapp.processEvents()

    assert dialog.table.rowCount() == 0
    assert dialog._selected_event_id == ""
    assert dialog.regular_pane._event is None
    assert passage_store.events(include_inactive=True)[0].is_active is False
    dialog.close()


def test_large_incremental_batch_falls_back_to_one_full_refresh(
    qapp,
    tmp_path,
    monkeypatch,
):
    dialog = PassageReviewDialog(
        PassageEventStore(tmp_path / "passages.jsonl"),
        VideoTimelineStore(tmp_path / "video_timeline.jsonl"),
    )
    refresh_calls = 0

    def counted_refresh():
        nonlocal refresh_calls
        refresh_calls += 1

    monkeypatch.setattr(dialog, "refresh", counted_refresh)
    dialog.refresh_events(f"passage-{index}" for index in range(65))

    assert refresh_calls == 1
    dialog.close()


def test_overlapping_window_does_not_retarget_existing_passage(
    qapp,
    tmp_path,
    fake_playback,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(
        _event(event_id="passage-12", sequence=1, passage_time_ms=15_000, bib="12")
    )
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    first_segment = _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01_first.mkv",
        source_id="camera_01",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=20_000,
    )
    dialog = PassageReviewDialog(passage_store, timeline_store)
    qapp.processEvents()
    first_location = passage_review.source_location(
        dialog._lookups["passage-12"], high_speed=False
    )
    assert first_location is not None
    assert first_location.segment.segment_id == first_segment.segment_id

    passage_store.append(
        _event(event_id="passage-15", sequence=2, passage_time_ms=16_000, bib="15")
    )
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01_second.mkv",
        source_id="camera_01",
        camera_index=1,
        started_at_ms=12_000,
        ended_at_ms=22_000,
    )
    dialog.refresh_events(("passage-15",))
    dialog.refresh()
    qapp.processEvents()

    retained_location = passage_review.source_location(
        dialog._lookups["passage-12"], high_speed=False
    )
    assert retained_location is not None
    assert retained_location.segment.segment_id == first_segment.segment_id
    dialog.close()


def test_confirmation_summary_work_is_bounded_by_changed_passage(
    qapp,
    tmp_path,
    fake_playback,
    monkeypatch,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    for index in range(1, 41):
        passage_store.append(
            _event(
                event_id=f"passage-{index}",
                sequence=index,
                passage_time_ms=15_000 + index * 10,
                bib=str(index),
            )
        )
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01.mkv",
        source_id="camera_01",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=20_000,
    )
    dialog = PassageReviewDialog(passage_store, timeline_store)
    qapp.processEvents()
    worker = fake_playback.instances[0]
    frame = QImage(1280, 720, QImage.Format_RGB888)
    frame.fill(0)
    worker.frame_ready.emit(frame, 5_010, 251)
    qapp.processEvents()
    source_association_calls = 0
    original_source_association = dialog._source_association

    def counted_source_association(*args, **kwargs):
        nonlocal source_association_calls
        source_association_calls += 1
        return original_source_association(*args, **kwargs)

    monkeypatch.setattr(dialog, "_source_association", counted_source_association)
    view = dialog.regular_pane.video_view
    QTest.mouseClick(
        view.viewport(),
        Qt.LeftButton,
        pos=view.viewport().rect().center(),
    )
    assert dialog._confirm_pending_marker(dialog.regular_pane) is True

    assert source_association_calls <= 8
    dialog.close()


def test_page_shortcuts_move_selection_from_search_and_table_focus(
    qapp,
    tmp_path,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(
        _event(event_id="passage-12", sequence=1, passage_time_ms=15_000, bib="12")
    )
    passage_store.append(
        _event(event_id="passage-15", sequence=2, passage_time_ms=16_000, bib="15")
    )
    dialog = PassageReviewDialog(
        passage_store,
        VideoTimelineStore(tmp_path / "video_timeline.jsonl"),
    )
    dialog.show()
    dialog.activateWindow()
    qapp.processEvents()

    dialog.identity_search.setFocus()
    QTest.keyClick(dialog.identity_search, Qt.Key_PageDown)
    qapp.processEvents()
    assert dialog.table.currentRow() == 1
    assert dialog._selected_event_id == "passage-15"

    dialog.table.setFocus()
    QTest.keyClick(dialog.table, Qt.Key_PageUp)
    qapp.processEvents()
    assert dialog.table.currentRow() == 0
    assert dialog._selected_event_id == "passage-12"
    dialog.close()


def test_video_view_up_down_shortcuts_move_selection_while_marking(
    qapp,
    tmp_path,
    fake_playback,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(
        _event(event_id="passage-12", sequence=1, passage_time_ms=15_000, bib="12")
    )
    passage_store.append(
        _event(event_id="passage-15", sequence=2, passage_time_ms=16_000, bib="15")
    )
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01.mkv",
        source_id="camera_01",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=20_000,
    )
    dialog = PassageReviewDialog(passage_store, timeline_store)
    dialog.show()
    qapp.processEvents()

    view = dialog.regular_pane.video_view
    frame = QImage(1280, 720, QImage.Format_RGB888)
    frame.fill(0)
    fake_playback.instances[-1].frame_ready.emit(frame, 5_000, 250)
    qapp.processEvents()
    QTest.mouseClick(view.viewport(), Qt.LeftButton, pos=view.viewport().rect().center())
    assert dialog.regular_pane.has_pending_marker

    QTest.keyClick(view, Qt.Key_Down)
    qapp.processEvents()
    assert dialog.table.currentRow() == 1
    assert dialog._selected_event_id == "passage-15"

    fake_playback.instances[-1].frame_ready.emit(frame, 6_000, 300)
    qapp.processEvents()
    QTest.mouseClick(view.viewport(), Qt.LeftButton, pos=view.viewport().rect().center())
    assert dialog.regular_pane.has_pending_marker

    QTest.keyClick(view, Qt.Key_Up, Qt.ShiftModifier)
    qapp.processEvents()
    assert dialog.table.currentRow() == 0
    assert dialog._selected_event_id == "passage-12"
    dialog.close()


def test_opt_in_auto_advance_after_first_marked_source(
    qapp,
    tmp_path,
    fake_playback,
):
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    passage_store.append(
        _event(event_id="passage-12", sequence=1, passage_time_ms=15_000, bib="12")
    )
    passage_store.append(
        _event(event_id="passage-15", sequence=2, passage_time_ms=16_000, bib="15")
    )
    timeline_store = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "camera_01.mkv",
        source_id="camera_01",
        camera_index=1,
        started_at_ms=10_000,
        ended_at_ms=20_000,
    )
    _add_segment(
        timeline_store,
        tmp_path / "videos" / "high_speed_01.mp4",
        source_id="high_speed_01",
        camera_index=2,
        started_at_ms=14_000,
        ended_at_ms=17_000,
        clock_source="external_clip_sidecar_beijing",
        timing_error_ms=100,
    )
    association_store = PassageEvidenceAssociationStore(
        tmp_path / "passage_evidence_associations.jsonl"
    )
    dialog = PassageReviewDialog(
        passage_store,
        timeline_store,
        association_store=association_store,
    )
    dialog.show()
    dialog.auto_advance_checkbox.setChecked(True)
    qapp.processEvents()
    regular_worker, high_speed_worker = fake_playback.instances
    frame = QImage(1280, 720, QImage.Format_RGB888)
    frame.fill(0)
    regular_worker.frame_ready.emit(frame, 5_000, 250)
    high_speed_worker.frame_ready.emit(frame, 1_000, 250)
    qapp.processEvents()

    regular_view = dialog.regular_pane.video_view
    QTest.mouseClick(
        regular_view.viewport(),
        Qt.LeftButton,
        pos=regular_view.viewport().rect().center(),
    )
    QTest.keyClick(regular_view, Qt.Key_Return)
    qapp.processEvents()

    assert association_store.get("passage-12", REGULAR_SOURCE) is not None
    assert association_store.get("passage-12", HIGH_SPEED_SOURCE) is None
    assert dialog.table.currentRow() == 1
    assert dialog._selected_event_id == "passage-15"
    dialog.close()
