import json
import os
from dataclasses import replace
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtCore import QEvent, QObject, QPoint, QPointF, Qt, pyqtSignal
from PyQt5.QtGui import QImage, QMouseEvent, QPalette
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QHeaderView

import realtime.passage_review as passage_review
from realtime.passage_evidence import (
    HIGH_SPEED_SOURCE,
    REGULAR_SOURCE,
    PassageEvidenceAssociationStore,
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
from realtime.video_timeline import VideoTimelineStore


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
    instances = []

    def __init__(self, video_path, parent=None):
        super().__init__(parent)
        self.video_path = Path(video_path)
        self.running = False
        self.stopped = False
        self.seek_calls = []
        self.wait_calls = []
        self.speed_calls = []
        self.step_calls = []
        self.full_resolution_calls = []
        type(self).instances.append(self)

    def start(self):
        self.running = True
        fps = 250.0 if "high_speed" in self.video_path.name else 50.0
        self.metadata_ready.emit(60_000, fps, 2560, 1440, int(60 * fps))

    def isRunning(self):
        return self.running

    def pause(self):
        return None

    def seek(self, position_ms):
        self.seek_calls.append(int(position_ms))

    def set_shuttle_speed(self, speed):
        self.speed_calls.append(float(speed))

    def step(self, frame_delta):
        self.step_calls.append(int(frame_delta))

    def request_full_resolution(self, frame_index=None):
        self.full_resolution_calls.append(frame_index)

    def stop(self):
        self.stopped = True
        if self.running:
            self.running = False
            self.finished.emit()

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
        media_duration_ms=ended_at_ms - started_at_ms,
        media_started_at_ms=started_at_ms,
    )
    return segment


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
    assert fake_playback.instances[0].seek_calls == [5_500]

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
    dialog.show()
    qapp.processEvents()
    assert first_pane.isVisible()
    assert second_pane.isVisible()
    assert dialog.high_speed_pane.isHidden()
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
    assert dialog.summary_label.text() == "当前筛选：待核对 · 0 / 5,000 条"
    dialog.review_filter_buttons["blocked"].click()
    assert dialog.table.rowCount() == 1
    assert dialog.review_filter_buttons["blocked"].text().startswith("✓ 待确认 ")
    assert dialog.summary_label.text() == "当前筛选：待确认 · 1 / 5,000 条"
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


def test_preview_is_playable_but_cannot_be_confirmed(
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
    assert not pane.mark_btn.isEnabled()
    assert not pane.open_btn.isEnabled()
    pane._pending_marker = (0.5, 0.5, 250, 5_000)
    assert pane.pending_confirmation() is None
    worker.playback_error.emit("preview decode failed")
    qapp.processEvents()
    assert not pane.open_btn.isEnabled()
    pane.close()


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
    qapp.processEvents()

    dialog._open_preferred_source(0, 7)

    assert opened == []
    assert dialog._maximized_pane is dialog.high_speed_pane
    assert not dialog.high_speed_pane.isHidden()
    assert dialog.regular_pane.isHidden()
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
        "运动员编号",
        "姓名",
        "组别",
        "通过时间",
        "普通录像",
        "高速摄像",
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


def test_linked_frame_step_keeps_regular_and_high_speed_on_one_time_cursor(
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

    QTest.keyClick(dialog.regular_pane.video_view, Qt.Key_Right)
    qapp.processEvents()

    assert dialog._shared_delta_ms == 4
    assert regular_worker.seek_calls[-1] == 10_054
    assert high_speed_worker.seek_calls[-1] == 1_054
    assert "Δ+4 ms" in dialog.current_time_label.text()
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
    qapp.processEvents()

    assert fake_playback.instances == [worker]
    assert worker.full_resolution_calls[-1] == 250
    assert dialog.regular_pane.video_view.zoom_percent == 100
    assert dialog.regular_pane.video_view.sceneRect().width() == 2560
    dialog.close()


def test_timeline_drag_seeks_video_before_mouse_release(
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
    timeline_y = pane.timeline.height() // 2

    QTest.mousePress(
        pane.timeline,
        Qt.LeftButton,
        pos=QPoint(pane.timeline.width() // 4, timeline_y),
    )
    QTest.mouseMove(
        pane.timeline,
        QPoint(pane.timeline.width() * 3 // 4, timeline_y),
        delay=10,
    )
    qapp.processEvents()

    assert pane._timeline_dragging
    assert worker.seek_calls
    assert worker.seek_calls[-1] == pane.timeline.value()
    assert "Δ" in pane.time_label.text()

    QTest.mouseRelease(
        pane.timeline,
        Qt.LeftButton,
        pos=QPoint(pane.timeline.width() * 3 // 4, timeline_y),
    )
    qapp.processEvents()
    assert not pane._timeline_dragging
    dialog.close()


def test_manual_marker_uses_enter_while_space_keeps_linked_playback(
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

    QTest.keyClick(dialog.regular_pane.video_view, Qt.Key_Space)
    qapp.processEvents()
    assert dialog._sync_playing
    QTest.keyClick(dialog.regular_pane.video_view, Qt.Key_Space)
    qapp.processEvents()
    assert not dialog._sync_playing
    assert association_store.get("passage-1", HIGH_SPEED_SOURCE) is None
    dialog.close()


def test_left_drag_scrubs_video_middle_drag_pans_and_click_places_marker(
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

    assert dialog._shared_delta_ms > 0
    assert worker.seek_calls[-1] == 5_000 + dialog._shared_delta_ms
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


def test_confirm_and_advance_seeks_only_the_next_passage_once(
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
    assert worker.seek_calls[seek_count_before:] == [6_000]
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
