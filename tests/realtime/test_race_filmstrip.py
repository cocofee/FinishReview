import os
from dataclasses import replace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import cv2
import numpy as np
import pytest
from PyQt5.QtCore import QObject, QPoint, QPointF, Qt, pyqtSignal
from PyQt5.QtGui import QImage, QWheelEvent
from PyQt5.QtTest import QSignalSpy, QTest
from PyQt5.QtWidgets import QApplication

import realtime.race_filmstrip as filmstrip
from realtime.race_filmstrip import (
    FilmstripSource, RaceRecordingIndex, RaceFilmstripFrame, RaceFilmstripPanel,
    RaceThumbnailWorker, MAX_CACHE, recording_sources,
)
from realtime.video_timeline import PassageVideoLocation, RecordingSegment, VideoTimelineStore
from realtime.camera_judgments import CameraJudgment


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def source(path, start=10000, duration=10000, *, priority=0, available=True, camera=1, race="race-1"):
    segment = RecordingSegment(str(path), f"camera_{camera:02}", camera, str(path), start,
                               start + duration, duration, start, race_id=race)
    location = PassageVideoLocation(segment, path, 0, 0, 0, 0, "located")
    return FilmstripSource(location, start, start + duration, available, priority)


def test_new_recording_scope_hides_history_but_allows_explicit_browsing(qapp, tmp_path, manual_worker):
    old = source(tmp_path / "yesterday.mkv", 10000, 1000, available=False)
    live = source(tmp_path / "current.ts", 100000, 2000)
    panel = RaceFilmstripPanel()
    try:
        panel.set_sources((old,), pending=1)
        panel.open_time(10000)
        panel.set_recording_start(99000)
        assert not panel.index.sources
        assert panel._pending is None
        assert "本次录像尚无可用画面" in panel.status_label.text()
        assert "已归档部分仍可判读" not in panel.status_label.text()
        panel.set_sources((old, live))
        assert panel.index.sources == (live,)
        assert panel.index.start_ms == live.start_ms
        panel.scope_combo.setCurrentIndex(panel.scope_combo.findData("all"))
        assert panel.index.sources == (old, live)
        panel.scope_combo.setCurrentIndex(panel.scope_combo.findData("current"))
        assert panel.index.sources == (live,)
        # A later recording and a workspace change both reset the view scope.
        panel.set_recording_start(103000)
        assert not panel.index.sources
        panel.set_recording_start(None)
        assert panel.scope_combo.isHidden()
        assert panel.index.sources == (old, live)
    finally:
        panel.close()


def test_recording_scope_survives_archive_handover_and_clear(qapp, tmp_path, manual_worker):
    live = source(tmp_path / "live.ts", 100000, 2000, priority=2)
    archive = source(tmp_path / "archive.mkv", 100000, 5000)
    panel = RaceFilmstripPanel()
    try:
        panel.set_recording_start(99000)
        panel.set_sources((live,))
        panel.browse_to(100500)
        panel.set_sources((live, archive))
        assert panel.index.span_at(100500).source == archive
        panel.clear()
        panel.set_sources((source(tmp_path / "old", 10000, 1000), archive))
        assert panel.index.sources == (archive,)
    finally:
        panel.close()


def test_recording_index_prefers_archive_and_preserves_missing_time(tmp_path):
    archive = source(tmp_path / "archive.mkv", 10000, 10000)
    overlap = source(tmp_path / "clip.m3u8", 14000, 14000, priority=2)
    later = source(tmp_path / "later.mkv", 40000, 10000)
    missing = source(tmp_path / "missing.mkv", 60000, 5000, available=False)
    index = RaceRecordingIndex((overlap, later, archive, missing))
    assert (index.start_ms, index.end_ms) == (10000, 65000)
    assert [(span.start_ms, span.end_ms, span.source) for span in index.spans] == [
        (10000, 20000, archive), (20000, 28000, overlap), (28000, 40000, None),
        (40000, 50000, later), (50000, 60000, None), (60000, 65000, missing),
    ]
    assert index.span_at(16000).source == archive
    assert index.span_at(35000).source is None
    assert index.span_at(65000) is None


def test_short_recording_between_sampling_points_still_appears(tmp_path):
    first = source(tmp_path / "first", 10000, 1000)
    short = source(tmp_path / "short", 11130, 40)
    index = RaceRecordingIndex((first, short))
    assert index.sample_at(11100, 100) == (short, 11130)
    assert index.sample_at(11000, 100) == (None, 11000)


def test_catalog_uses_all_camera_one_recordings_in_race_without_roster(tmp_path):
    store = VideoTimelineStore(tmp_path / "timeline.jsonl")
    for name, camera, race in (("first", 1, "race-1"), ("unread_riders", 1, "race-1"),
                              ("second_camera", 2, "race-1"), ("other_race", 1, "race-2")):
        path = tmp_path / f"{name}.mkv"
        path.write_bytes(b"video")
        segment = store.start_segment(source_id=f"camera_{camera:02}", camera_index=camera,
                                      video_path=path, started_at_ms=10000, race_id=race)
        store.finish_segment(segment.segment_id, ended_at_ms=20000, media_started_at_ms=10000, media_duration_ms=10000)
    sources, pending = recording_sources(store, 1, "race-1")
    assert {item.location.video_path.stem for item in sources} == {"first", "unread_riders"}
    assert pending == 0


def test_catalog_includes_live_hls_tail_while_archive_is_pending(tmp_path):
    store = VideoTimelineStore(tmp_path / "timeline.jsonl")
    archive_path = tmp_path / "archive.mkv"
    archive_path.write_bytes(b"archive")
    archive = store.start_segment(
        source_id="camera_01",
        camera_index=1,
        video_path=archive_path,
        started_at_ms=10_000,
        race_id="race-1",
    )
    store.finish_segment(
        archive.segment_id,
        ended_at_ms=20_000,
        media_started_at_ms=10_000,
        media_duration_ms=10_000,
    )
    live_path = tmp_path / "live.mkv"
    live_path.write_bytes(b"live")
    live_segment = RecordingSegment(
        "live-filmstrip-1-1",
        "camera_01_review",
        1,
        str(live_path),
        20_000,
        25_000,
        5_000,
        20_000,
        race_id="race-1",
        end_reason="live_filmstrip_tail",
    )
    live_location = PassageVideoLocation(
        live_segment,
        live_path,
        4_000,
        1_000,
        0,
        2_000,
        "unverified",
    )
    sources, pending = recording_sources(
        store,
        1,
        "race-1",
        live_location=live_location,
    )
    assert pending == 0
    assert any(source.location.segment.end_reason == "live_filmstrip_tail" for source in sources)
    index = RaceRecordingIndex(sources)
    assert index.span_at(22_000).source is not None
    assert index.span_at(22_000).source.location.segment.end_reason == "live_filmstrip_tail"


def test_new_live_segments_preserve_inflight_click_and_thumbnail_worker(qapp, tmp_path, manual_worker):
    first = source(tmp_path / "first.ts", duration=2000)
    second = source(tmp_path / "second.ts", start=12000, duration=2000)
    panel = RaceFilmstripPanel()
    panel.resize(1000, 450)
    panel.show()
    panel.set_sources((first,))
    panel.open_time(10500)
    panel._load_visible()
    worker = panel._worker
    selected = QSignalSpy(panel.frame_requested)
    panel._pending_judgment = (first.key, 10500)
    try:
        panel.set_sources((first, second))
        assert panel._worker is worker
        assert not worker.stopped
        assert panel._pending == panel._pending_judgment == (first.key, 10500)
        frame = result(first, 10500)
        worker.frame_ready.emit(frame)
        assert len(selected) == 1 and selected[0][0] == frame
    finally:
        panel.close()


def test_expired_live_source_cancels_pending_click(qapp, tmp_path, manual_worker):
    first = source(tmp_path / "first.ts", duration=2000)
    second = source(tmp_path / "second.ts", start=12000, duration=2000)
    panel = RaceFilmstripPanel()
    panel.show()
    panel.set_sources((first,))
    panel.open_time(10500)
    panel._load_visible()
    worker = panel._worker
    selected = QSignalSpy(panel.frame_requested)
    try:
        panel.set_sources((second,))
        assert worker.stopped
        assert panel._pending is None
        worker.frame_ready.emit(result(first, 10500))
        assert not selected
    finally:
        panel.close()


@pytest.mark.parametrize("available", (False, True))
def test_unavailable_source_with_same_identity_cancels_pending_click(qapp, tmp_path, manual_worker, available):
    recording_path = tmp_path / "rolling.ts"
    first = source(recording_path, duration=2000)
    unavailable = source(recording_path, duration=1000, available=available)
    panel = RaceFilmstripPanel()
    panel.show()
    panel.set_sources((first,))
    panel.open_time(11500)
    panel._load_visible()
    worker = panel._worker
    selected = QSignalSpy(panel.frame_requested)
    try:
        panel._pending_judgment = panel._pending
        panel.set_sources((unavailable,))
        assert worker.stopped
        assert panel._pending is None
        assert panel._pending_judgment is None
        stale = result(first, 11500)
        worker.frame_ready.emit(stale)
        assert not selected
        assert stale.key not in panel.cache
    finally:
        panel.close()


def test_calibration_keeps_recording_labels_and_original_frame(qapp, tmp_path, manual_worker):
    recording = source(tmp_path / "first", duration=60000)
    panel = RaceFilmstripPanel()
    panel.resize(1200, 450)
    panel.show()
    panel.set_sources((recording,))
    qapp.processEvents()
    frame = result(recording, 12200)
    panel.cache[frame.key] = frame
    panel.browse_to(12200)
    panel.set_current_frame(recording.location, 2200)
    scroll = panel.canvas.horizontalScrollBar().value()
    calibrated = replace(recording, location=replace(recording.location, clock_offset_ms=2000))
    panel.set_sources((calibrated,))
    assert panel.display_time(12200, frame.source) == "08:00:12.200"
    assert panel.judgment_time(12200, frame.source) == "08:00:10.200"
    assert panel.cache[frame.key] is frame
    assert panel.current_time == 12200
    assert panel.canvas.horizontalScrollBar().value() == scroll
    panel.close()


def test_old_chip_calibration_does_not_backdate_current_recording(qapp, tmp_path, manual_worker):
    from datetime import datetime

    start = int(datetime(2026, 9, 18, 17, 38, 6, tzinfo=filmstrip.BEIJING).timestamp() * 1000)
    timestamp = int(datetime(2026, 9, 18, 17, 44, 28, 387000, tzinfo=filmstrip.BEIJING).timestamp() * 1000)
    recording = source(tmp_path / "current.ts", start=start, duration=7 * 60000)
    calibrated = replace(recording, location=replace(recording.location, clock_offset_ms=432996329))
    panel = RaceFilmstripPanel()
    try:
        panel.set_recording_start(start)
        panel.set_sources((calibrated,))
        assert panel.index.sources == (calibrated,)
        assert panel.display_time(timestamp) == "17:44:28.387"
        assert panel.judgment_time(timestamp) == "17:27:52.058"
        assert "录像时间（北京时间）" in panel.range_label.toolTip()
        assert "校时后判读时间" in panel.range_label.toolTip()
    finally:
        panel.close()


def test_judgment_ruler_keeps_exact_times_and_dense_records_clickable(qapp, tmp_path, manual_worker):
    recording = source(tmp_path / "first", duration=60000)
    panel = RaceFilmstripPanel()
    panel.resize(1200, 450)
    panel.show()
    panel.set_sources((recording,))
    qapp.processEvents()
    records = tuple(CameraJudgment(str(i), str(i), "segment", time - 10000, time,
                                   str(101 + i), filmstrip.format_time(time))
                    for i, time in enumerate((12000, 12000, 12001, 12400)))
    panel.set_judgments(records)
    panel.browse_to(12000)
    panel.set_current_frame(recording.location, 2000)
    assert panel.time_for_x(panel.x_for_time(12001)) == 12001
    assert panel.x_for_time(12001) > panel.x_for_time(12000)
    regions = panel.canvas.judgment_regions()
    assert [record.key for record in regions[0][1]] == ["0", "1", "2"]
    clicked = QSignalSpy(panel.saved_judgment_requested)
    QTest.mouseClick(panel.canvas.viewport(), Qt.LeftButton, pos=regions[0][0].center().toPoint())
    menu = panel.canvas._judgment_menu
    assert len(menu.actions()) == 3
    menu.actions()[1].trigger()
    assert clicked[0][0] == "1"
    menu.close()
    assert not panel._pending
    # A changed density keeps the same canonical times, including same-frame riders.
    panel.density_combo.setCurrentIndex(panel.density_combo.findData(200))
    panel.browse_to(12000)
    assert panel.time_for_x(panel.x_for_time(12001)) == 12001
    assert len(panel._judgments) == 4
    panel.close()


def test_ruler_click_requests_exact_time_and_does_not_seek_across_gap(qapp, tmp_path, manual_worker):
    first = source(tmp_path / "first", duration=1000)
    second = source(tmp_path / "second", start=13000, duration=1000)
    panel = RaceFilmstripPanel()
    panel.resize(1200, 450)
    panel.show()
    panel.set_sources((first, second))
    qapp.processEvents()
    panel.browse_to(10340)
    x = round(panel.x_for_time(10340))
    y = panel.canvas.viewport().height() - filmstrip.IMAGE_FOOTER + 10
    QTest.mouseClick(panel.canvas.viewport(), Qt.LeftButton, pos=QPoint(x, y))
    panel._load_visible()
    assert panel._worker.jobs[0] == (first, 10340)
    selected = QSignalSpy(panel.frame_requested)
    panel._worker.frame_ready.emit(result(first, 10340))
    assert selected[0][0].recorder_time_ms == 10340
    panel.open_time(12000)
    assert panel._pending is None
    assert len(selected) == 1
    panel.close()


def test_worker_thumbnail_is_whole_original_frame_with_exact_index(qapp, tmp_path):
    path = tmp_path / "camera.avi"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"FFV1"), 30, (80, 60))
    assert writer.isOpened()
    for index in range(60):
        frame = np.zeros((60, 80, 3), dtype=np.uint8)
        frame[:, :, 2] = index * 3
        frame[0, 0, :] = (4, 5, 6)
        frame[-1, -1, :] = (7, 8, 9)
        writer.write(frame)
    writer.release()
    recording = source(path, 10000, 2000)
    worker = RaceThumbnailWorker(((recording, 10350), (recording, 10990)))
    frames, errors = QSignalSpy(worker.frame_ready), QSignalSpy(worker.failed)
    worker.run()
    assert not errors
    assert len(frames) == 2
    for (result,), expected_index in zip(frames, (10, 29)):
        assert result.frame_index == expected_index
        assert result.position_ms == int(expected_index * 1000 / 30)
        assert result.image.width() == 80
        assert result.image.height() == 60
        assert result.image.pixelColor(40, 30).red() == expected_index * 3
        assert result.image.pixelColor(0, 0).getRgb()[:3] == (6, 5, 4)
        assert result.image.pixelColor(79, 59).getRgb()[:3] == (9, 8, 7)


def test_worker_errors_and_cancel_do_not_emit_wrong_frames(qapp, tmp_path):
    recording = source(tmp_path / "does_not_exist.mkv")
    worker = RaceThumbnailWorker(((recording, 11000), (recording, 11200)))
    frames, errors = QSignalSpy(worker.frame_ready), QSignalSpy(worker.failed)
    worker.run()
    assert not frames
    assert len(errors) == 2
    cancelled = RaceThumbnailWorker(((recording, 11000),))
    frames, errors = QSignalSpy(cancelled.frame_ready), QSignalSpy(cancelled.failed)
    cancelled.request_stop()
    cancelled.run()
    assert not frames and not errors


@pytest.fixture
def manual_worker(monkeypatch):
    class Worker(QObject):
        frame_ready = pyqtSignal(object)
        failed = pyqtSignal(object, str)
        finished = pyqtSignal()
        instances = []

        def __init__(self, jobs, parent=None):
            super().__init__(parent)
            self.jobs = tuple(jobs)
            self.stopped = False
            self.instances.append(self)

        def start(self, priority=None):
            self.priority = priority
            pass

        def request_stop(self):
            self.stopped = True

    monkeypatch.setattr(filmstrip, "RaceThumbnailWorker", Worker)
    monkeypatch.setattr(filmstrip, "retire_qthread", lambda worker: None)
    return Worker


def result(recording, timestamp):
    image = QImage(80, 60, QImage.Format_RGB32)
    image.fill(0)
    position = timestamp - recording.start_ms
    return RaceFilmstripFrame(recording, timestamp, position, position // 50, image)


def test_operator_busy_defers_thumbnails_but_allows_explicit_click(qapp, tmp_path, manual_worker):
    recording = source(tmp_path / "video", duration=60000)
    panel = RaceFilmstripPanel()
    panel.show()
    panel.set_operator_busy(True)
    panel.set_sources((recording,))
    panel._load_visible()
    assert panel._worker is None
    panel.open_time(10500)
    panel._load_visible()
    assert panel._worker is not None
    assert panel._worker.jobs == ((recording, 10500),)
    panel.close()


def wheel(panel, *, angle=0, pixels=0):
    viewport = panel.canvas.viewport()
    point = viewport.rect().center()
    event = QWheelEvent(QPointF(point), QPointF(viewport.mapToGlobal(point)),
                        QPoint(0, pixels), QPoint(0, angle), Qt.NoButton, Qt.NoModifier,
                        Qt.NoScrollPhase, False)
    QApplication.sendEvent(viewport, event)


def test_default_half_second_wheel_accumulates_without_seeking_camera(qapp, tmp_path, manual_worker):
    panel = RaceFilmstripPanel()
    try:
        panel.resize(1200, 450)
        panel.show()
        recording = source(tmp_path / "first", duration=60000)
        panel.set_sources((recording,))
        qapp.processEvents()
        assert panel.density_combo.currentData() == 500
        assert panel.time_at(1) - panel.time_at(0) == 500
        panel.browse_to(30000)
        panel.set_current_frame(recording.location, 2000)
        bar = panel.canvas.horizontalScrollBar()
        before = bar.value()
        selected = QSignalSpy(panel.frame_requested)
        wheel(panel, angle=-120)
        wheel(panel, angle=-120)
        QTest.qWait(160)
        assert bar.value() == before + 2 * panel.tile_pitch
        assert not selected and panel.current_time == 12000
        wheel(panel, angle=120)
        QTest.qWait(160)
        assert bar.value() == before + panel.tile_pitch
        wheel(panel, pixels=-37)
        assert bar.value() == before + panel.tile_pitch + 37
        panel.follow_button.click()
        wheel(panel, angle=120)
        assert not panel.follow_button.isChecked()
    finally:
        panel.close()


def test_wheel_reversal_and_click_stop_old_scroll_target(qapp, tmp_path, manual_worker):
    panel = RaceFilmstripPanel()
    try:
        panel.resize(1200, 450)
        panel.show()
        recording = source(tmp_path / "first", duration=60000)
        panel.set_sources((recording,))
        qapp.processEvents()
        panel.browse_to(30000)
        bar = panel.canvas.horizontalScrollBar()
        wheel(panel, angle=-360)
        QTest.qWait(40)
        turning_point = bar.value()
        wheel(panel, angle=120)
        QTest.qWait(160)
        assert bar.value() == turning_point - panel.tile_pitch
        wheel(panel, angle=-120)
        QTest.qWait(30)
        clicked_at = bar.value()
        tile = (clicked_at + 100) // panel.tile_pitch
        frame = result(recording, panel.time_at(tile))
        panel.cache[frame.key] = frame
        selected = QSignalSpy(panel.frame_requested)
        QTest.mouseClick(panel.canvas.viewport(), Qt.LeftButton, pos=QPoint(100, 80))
        QTest.qWait(160)
        assert bar.value() == clicked_at
        assert len(selected) == 1 and selected[0][0] is frame
    finally:
        panel.close()


def test_wheel_boundaries_and_density_changes_leave_no_stale_scroll(qapp, tmp_path, manual_worker):
    panel = RaceFilmstripPanel()
    try:
        panel.resize(1200, 450)
        panel.show()
        panel.set_sources((source(tmp_path / "first", duration=60000),))
        qapp.processEvents()
        bar = panel.canvas.horizontalScrollBar()
        wheel(panel, angle=120)
        QTest.qWait(160)
        assert bar.value() == 0
        bar.setValue(bar.maximum())
        wheel(panel, angle=-120)
        QTest.qWait(160)
        assert bar.value() == bar.maximum()
        panel.browse_to(30000)
        wheel(panel, angle=-360)
        QTest.qWait(30)
        panel.density_combo.setCurrentIndex(panel.density_combo.findData(100))
        changed_at = bar.value()
        QTest.qWait(160)
        assert bar.value() == changed_at
        wheel(panel, angle=-120)
        panel.clear()
        QTest.qWait(160)
        assert bar.value() == 0 and panel.tile_count() == 0
    finally:
        panel.close()


def test_continuous_wheel_keeps_visible_decoding_and_loads_before_stopping(qapp, tmp_path, manual_worker):
    panel = RaceFilmstripPanel()
    try:
        panel.resize(1200, 450)
        panel.show()
        panel.set_sources((source(tmp_path / "first", duration=60000),))
        qapp.processEvents()
        panel._load_visible()
        worker = panel._worker
        assert worker is not None
        wheel(panel, angle=-120)
        QTest.qWait(40)
        assert not worker.stopped
        worker.finished.emit()
        for _ in range(6):
            wheel(panel, angle=-30)
            QTest.qWait(25)
        assert len(manual_worker.instances) == 2
        assert panel._worker is not None and not panel._worker.stopped
        # Scrolling well beyond a running batch still retires that batch;
        # no second decoder may start until the old one acknowledges stop.
        stale_worker = panel._worker
        wheel(panel, angle=-2400)
        QTest.qWait(160)
        assert stale_worker.stopped
        assert len(manual_worker.instances) == 2
    finally:
        panel.close()


def test_whole_day_rail_loads_only_visible_jobs_and_bounds_cache(qapp, tmp_path, manual_worker):
    recording = source(tmp_path / "whole_day", duration=12 * 3600 * 1000)
    panel = RaceFilmstripPanel()
    panel.density_combo.setCurrentIndex(panel.density_combo.findData(100))
    panel.resize(1200, 300)
    panel.show()
    panel.set_sources((recording,))
    qapp.processEvents()
    panel._load_visible()
    worker = panel._worker
    assert worker is not None
    assert len(worker.jobs) <= filmstrip.MAX_BATCH
    assert panel.tile_count() == 432000
    assert all(timestamp < recording.start_ms + 10000 for _, timestamp in worker.jobs)
    for index in range(MAX_CACHE + 15):
        worker.frame_ready.emit(result(recording, recording.start_ms + index * 100))
    assert len(panel.cache) == MAX_CACHE
    assert panel.tile_count() == 432000
    panel.close()


def test_identity_cursor_and_new_archives_do_not_move_browsing_position(qapp, tmp_path, manual_worker):
    recording = source(tmp_path / "first", duration=60000)
    panel = RaceFilmstripPanel()
    panel.resize(1000, 300)
    panel.show()
    panel.set_sources((recording,))
    qapp.processEvents()
    panel.browse_to(40000)
    scroll = panel.canvas.horizontalScrollBar().value()
    left = panel.visible_times()[0]
    panel.set_current_frame(recording.location, 2000)
    assert panel.canvas.horizontalScrollBar().value() == scroll
    # Extending the end keeps the same absolute left edge, not a percentage.
    panel.set_sources((recording, source(tmp_path / "second", 80000, 60000)))
    assert panel.visible_times()[0] == left
    panel.set_sources((source(tmp_path / "earlier", 0, 5000), recording))
    assert panel.visible_times()[0] == left
    panel.close()


def test_click_uses_its_original_recording_and_never_opens_gap(qapp, tmp_path, manual_worker):
    first = source(tmp_path / "first", 10000, 1000)
    second = source(tmp_path / "second", 12000, 1000)
    panel = RaceFilmstripPanel()
    panel.density_combo.setCurrentIndex(panel.density_combo.findData(100))
    panel.set_sources((first, second))
    selected = QSignalSpy(panel.frame_requested)
    frame = result(second, 12200)
    panel.cache[frame.key] = frame
    panel.open_tile(22)
    assert selected[0][0] is frame
    panel.open_tile(15)
    assert len(selected) == 1
    assert "缺口" in panel.status_label.text()
    panel.stop()


def test_pending_click_is_cancelled_when_operator_browses_elsewhere(qapp, tmp_path, manual_worker):
    recording = source(tmp_path / "first", duration=60000)
    panel = RaceFilmstripPanel()
    panel.density_combo.setCurrentIndex(panel.density_combo.findData(100))
    panel.resize(1000, 300)
    panel.show()
    panel.set_sources((recording,))
    qapp.processEvents()
    panel._load_visible()
    worker = panel._worker
    selected = QSignalSpy(panel.frame_requested)
    panel.open_tile(1)
    panel.browse_to(40000)
    worker.frame_ready.emit(result(recording, 10100))
    assert not selected
    # No second decoder can start until cancellation has actually returned.
    panel._load_visible()
    assert len(manual_worker.instances) == 1
    worker.finished.emit()
    panel._load_visible()
    assert len(manual_worker.instances) == 2
    panel.close()


def test_race_switch_and_close_reject_old_queued_frames(qapp, tmp_path, manual_worker):
    first = source(tmp_path / "first", duration=60000)
    second = source(tmp_path / "second", duration=60000, race="race-2")
    panel = RaceFilmstripPanel()
    panel.density_combo.setCurrentIndex(panel.density_combo.findData(100))
    panel.resize(1000, 300)
    panel.show()
    panel.set_sources((first,))
    qapp.processEvents()
    panel._load_visible()
    worker = panel._worker
    selected = QSignalSpy(panel.frame_requested)
    panel.open_tile(1)
    panel.clear()
    panel.set_sources((second,))
    worker.frame_ready.emit(result(first, 10100))
    assert not panel.cache and not selected
    panel.stop()
    worker.frame_ready.emit(result(second, 10100))
    assert not panel.cache and not selected


def test_dragging_does_not_seek_and_thumbnail_click_does(qapp, tmp_path, manual_worker):
    recording = source(tmp_path / "first", duration=60000)
    panel = RaceFilmstripPanel()
    panel.resize(1000, 300)
    panel.show()
    panel.set_sources((recording,))
    qapp.processEvents()
    frame = result(recording, 10000)
    panel.cache[frame.key] = frame
    selected = QSignalSpy(panel.frame_requested)
    QTest.mouseClick(panel.canvas.viewport(), Qt.LeftButton, pos=QPoint(100, 80))
    assert len(selected) == 1 and selected[0][0] is frame
    QTest.mousePress(panel.canvas.viewport(), Qt.LeftButton, pos=QPoint(400, 80))
    QTest.mouseMove(panel.canvas.viewport(), QPoint(80, 80))
    QTest.mouseRelease(panel.canvas.viewport(), Qt.LeftButton, pos=QPoint(80, 80))
    assert panel.canvas.horizontalScrollBar().value() > 0
    assert len(selected) == 1
    panel.close()


def test_follow_latest_is_explicit_and_page_or_click_stops_following(qapp, tmp_path, manual_worker):
    first = source(tmp_path / "first", duration=60000)
    second = source(tmp_path / "second", 70000, 60000)
    third = source(tmp_path / "third", 130000, 60000)
    panel = RaceFilmstripPanel()
    panel.resize(1000, 350)
    panel.show()
    panel.set_sources((first,))
    qapp.processEvents()
    bar = panel.canvas.horizontalScrollBar()
    panel.browse_to(40000)
    bar.setValue(bar.value() + 57)  # A partially visible thumbnail must not jump.
    scroll = bar.value()
    panel.set_sources((first, second))
    assert bar.value() == scroll
    assert "新增可回看" in panel.new_recording_label.text()
    panel.follow_button.click()
    assert bar.value() == bar.maximum()
    panel.set_sources((first, second, third))
    assert bar.value() == bar.maximum()
    panel.set_current_frame(first.location, 1000)
    assert bar.value() == bar.maximum()
    panel.page(-1)
    assert not panel.follow_button.isChecked()
    scroll = bar.value()
    panel.set_sources((first, second, third, source(tmp_path / "fourth", 190000, 60000)))
    assert bar.value() == scroll
    panel.follow_button.click()
    panel.open_tile(1)
    assert not panel.follow_button.isChecked()
    panel.close()


def test_inspection_skips_gaps_failed_unloaded_and_partial_tiles(qapp, tmp_path, manual_worker):
    first = source(tmp_path / "first", 10000, 300)
    second = source(tmp_path / "second", 10500, 500)
    missing = source(tmp_path / "missing", 11000, 500, available=False)
    panel = RaceFilmstripPanel()
    panel.density_combo.setCurrentIndex(panel.density_combo.findData(100))
    panel.resize(1900, 350)
    panel.show()
    panel.set_check_context(tmp_path / "checks.jsonl", "race-1")
    panel.set_sources((first, second, missing))
    qapp.processEvents()
    # Keep the loaded second segment fully visible as toolbar height changes.
    panel.resize(7 * panel.tile_pitch, panel.height())
    qapp.processEvents()
    panel.canvas.horizontalScrollBar().setValue(80)
    for recording, timestamp in ((first, 10000), (first, 10100), (second, 10500)):
        frame = result(recording, timestamp)
        panel.cache[frame.key] = frame
    panel.errors[(first.key, 10200)] = "decode failed"
    assert not panel.checked_ranges()  # Browsing or loaded images alone do not mark.
    panel.mark_visible_checked()
    assert panel.checked_ranges() == ((10100, 10200), (10500, 10600))
    assert (10300, 10500) not in panel.available_ranges()
    panel.browse_unchecked()
    assert panel.visible_times()[0] == 10000  # Cropped first tile was never inspected.
    panel.mark_visible_checked()
    panel.browse_unchecked()
    assert panel.visible_times()[0] == 10200
    panel.clear()
    panel.set_check_context(tmp_path / "checks.jsonl", "race-2")
    panel.set_sources((first, second))
    assert not panel.checked_ranges()
    panel.clear()
    panel.set_check_context(tmp_path / "checks.jsonl", "race-1")
    panel.set_sources((first, second))
    assert panel.checked_ranges() == ((10000, 10200), (10500, 10600))
    panel.close()


def test_coarse_browsing_and_inspection_survive_archive_boundaries(qapp, tmp_path, manual_worker):
    first = source(tmp_path / "first", 10000, 2500)
    second = source(tmp_path / "second", 12500, 7500)
    later = source(tmp_path / "later", 22000, 8000)
    panel = RaceFilmstripPanel()
    panel.resize(1300, 350)
    panel.show()
    panel.set_check_context(tmp_path / "checks.jsonl", "race-1")
    panel.set_sources((first, second, later))
    qapp.processEvents()
    panel.density_combo.setCurrentIndex(panel.density_combo.findData(5000))
    panel.resize(4 * panel.tile_pitch, panel.height())
    qapp.processEvents()
    panel.browse_to(10000)
    for timestamp in (10000, 15000, 22000, 25000):
        recording = panel.index.span_at(timestamp).source
        frame = result(recording, timestamp)
        panel.cache[frame.key] = frame
    panel.mark_visible_checked()
    # File boundaries are continuous; the two-second real gap stays unchecked.
    assert panel.checked_ranges() == ((10000, 20000), (22000, 30000))
    panel.mark_visible_checked(False)
    assert not panel.checked_ranges()
    assert panel.interval_ms == 5000
    panel.close()


def test_progress_load_failure_disables_marking_without_overwriting(qapp, tmp_path, manual_worker):
    path = tmp_path / "checks.jsonl"
    path.write_bytes(b'{"broken":')
    panel = RaceFilmstripPanel()
    panel.set_check_context(path, "race-1")
    panel.set_sources((source(tmp_path / "first"),))
    assert not panel.check_button.isEnabled()
    assert "读取失败" in panel.inspection_label.text()
    panel.mark_visible_checked()
    assert path.read_bytes() == b'{"broken":'
    panel.close()


def test_enlarging_tiles_preserves_time_and_clicks_correct_original(qapp, tmp_path, manual_worker):
    recording = source(tmp_path / "first", duration=60000)
    panel = RaceFilmstripPanel()
    panel.resize(1400, 350)
    panel.show()
    panel.set_sources((recording,))
    qapp.processEvents()
    panel.browse_to(40000)
    bar = panel.canvas.horizontalScrollBar()
    before = panel.index.start_ms + bar.value() / panel.tile_pitch * panel.interval_ms
    image_height = panel.image_height()
    panel.resize(1400, 550)
    qapp.processEvents()
    assert panel.image_height() >= image_height + 190
    assert panel.tile_width == round(panel.image_height() * 4 / 3)
    after = panel.index.start_ms + bar.value() / panel.tile_pitch * panel.interval_ms
    assert abs(after - before) < 1
    tile = next(t for t in panel.visible_tiles() if t * panel.tile_pitch >= bar.value())
    frame = result(recording, panel.time_at(tile))
    panel.cache[frame.key] = frame
    selected = QSignalSpy(panel.frame_requested)
    click_x = tile * panel.tile_pitch - bar.value() + panel.tile_width // 2
    QTest.mouseClick(panel.canvas.viewport(), Qt.LeftButton, pos=QPoint(click_x, 90))
    assert len(selected) == 1 and selected[0][0] is frame
    panel.close()


def test_larger_thumbnails_obey_memory_budget(qapp, tmp_path, manual_worker):
    recording = source(tmp_path / "first", duration=60000)
    panel = RaceFilmstripPanel()
    panel.resize(1400, 450)
    panel.show()
    panel.set_sources((recording,))
    qapp.processEvents()
    panel._load_visible()
    image = QImage(960, 720, QImage.Format_RGB888)
    image.fill(0)
    for index in range(45):
        frame = replace(result(recording, 10000 + index * 100), image=image)
        panel._worker.frame_ready.emit(frame)
    assert sum(frame.image.sizeInBytes() for frame in panel.cache.values()) <= filmstrip.MAX_CACHE_BYTES
    assert len(panel.cache) < 45
    panel.close()


def test_f_shortcut_opens_selected_original_frame(qapp, tmp_path, manual_worker):
    recording = source(tmp_path / "first", duration=60000)
    panel = RaceFilmstripPanel()
    panel.density_combo.setCurrentIndex(panel.density_combo.findData(100))
    panel.resize(1400, 450)
    panel.show()
    panel.activateWindow()
    panel.set_sources((recording,))
    frame = result(recording, 12000)
    panel.cache[frame.key] = frame
    panel.open_tile(20)
    selected = QSignalSpy(panel.frame_requested)
    enlarged = QSignalSpy(panel.judgment_requested)
    panel.canvas.setFocus()
    qapp.processEvents()
    QTest.keyClick(panel.canvas.viewport(), Qt.Key_F)
    assert not selected
    assert len(enlarged) == 1 and enlarged[0][0] is frame
    panel.open_tile(panel.tile_count() + 1)
    QTest.keyClick(panel.canvas.viewport(), Qt.Key_F)
    assert len(enlarged) == 1  # Never fall back to an old selected picture.
    panel.close()


@pytest.mark.parametrize("cancel", [None, "page", "invalid_tile", "clear", "failure"])
def test_f_waits_for_original_frame_and_cancels_stale_requests(qapp, tmp_path, manual_worker, cancel):
    recording = source(tmp_path / "first", duration=60000)
    panel = RaceFilmstripPanel()
    panel.density_combo.setCurrentIndex(panel.density_combo.findData(100))
    panel.resize(1400, 450)
    panel.show()
    panel.activateWindow()
    panel.set_sources((recording,))
    qapp.processEvents()
    panel.open_tile(1)
    panel._load_visible()
    worker = panel._worker
    selected = QSignalSpy(panel.frame_requested)
    enlarged = QSignalSpy(panel.judgment_requested)
    QTest.keyClick(panel.canvas, Qt.Key_F)
    assert panel._pending_judgment == (recording.key, 10100)
    assert not enlarged
    if cancel == "page":
        panel.page(1)
    elif cancel == "invalid_tile":
        panel.open_tile(panel.tile_count() + 1)
    elif cancel == "clear":
        panel.clear()
    elif cancel == "failure":
        worker.failed.emit((recording.key, 10100), "decode failed")
    frame = result(recording, 10100)
    worker.frame_ready.emit(frame)
    assert len(enlarged) == (1 if cancel is None else 0)
    assert len(selected) == len(enlarged)
    if enlarged:
        assert enlarged[0][0] is frame
    panel.close()
