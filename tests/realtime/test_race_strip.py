from dataclasses import replace

import pytest
from PyQt5.QtCore import QPoint, Qt
from PyQt5.QtTest import QSignalSpy, QTest
from PyQt5.QtWidgets import QApplication

from realtime.race_strip import RaceStripPanel, RaceStripView, StripPiece
from tests.realtime.test_quick_review_panel import panel, qapp


T = 1_789_784_000_000


def piece(tmp_path, start=T, end=T + 60_000):
    video = tmp_path / "camera_01_archive_0000.avi"
    video.touch()
    return StripPiece(tmp_path / "strip.npz", 1, "archive", video, start, end, True, 300)


def test_zoom_keeps_the_point_under_the_pointer_and_scroll_stays_in_range(qapp, tmp_path):
    view = RaceStripView()
    view.resize(800, view.height())
    view.set_pieces([piece(tmp_path, T, T + 600_000)])
    view.set_view(T + 60_000)
    anchor = view.time_at(200)
    view.zoom(1, 200)
    assert view.time_at(200) == pytest.approx(anchor)
    view.set_view(T - 1_000_000)
    assert view.view_ms == view.extent()[0]
    view.set_view(T + 1_000_000)
    assert view.view_ms + view.span_ms() == pytest.approx(view.extent()[1])


def test_click_is_only_a_video_jump_and_the_chip_row_does_not_change(panel, tmp_path):
    panel.race_strip.view.set_pieces([piece(tmp_path)])
    current, selected = panel.current_item(), panel._selected_key
    before = panel._arrival_store.active(panel._arrival_scope)
    jumps = QSignalSpy(panel.jump_requested)
    panel.race_strip.view.clicked.emit(T + 2_000)
    assert jumps[-1][0] == {"camera_index": 1, "segment_id": "archive",
                            "video_path": str(tmp_path / "camera_01_archive_0000.avi"),
                            "position_ms": 2300, "frame_index": None, "reference_only": True}
    assert panel.current_item() is current and panel._selected_key == selected
    assert panel._arrival_store.active(panel._arrival_scope) == before
    assert not panel.current_marks()
    assert "不确认" in panel.hint.text()


def test_jump_handler_cannot_steal_the_strip_keyboard_focus(panel, qapp, tmp_path):
    panel.show()
    qapp.processEvents()
    panel.jump_requested.connect(lambda _frame: panel.table.setFocus())
    panel.race_strip.view.set_pieces([piece(tmp_path)])
    panel.race_strip.view.clicked.emit(T + 2_000)
    qapp.processEvents()
    assert QApplication.focusWidget() is panel.race_strip.view


def test_click_in_a_gap_does_not_jump_or_count(panel):
    jumps = QSignalSpy(panel.jump_requested)
    panel.race_strip.view.set_pieces([])
    panel.race_strip.view.clicked.emit(T)
    assert not jumps and not panel.current_marks()
    assert "不代表无人" in panel.hint.text()


def test_strip_keys_scroll_but_picture_keys_still_step_frames(panel, qapp):
    panel.resize(1100, 700)
    panel.show()
    qapp.processEvents()
    view = panel.race_strip.view
    stamps = [row.event.timeline_timestamp_ms for row in panel._arrival_rows if row.event]
    view.set_chips([(min(stamps), "1", False), (max(stamps) + 600_000, "2", False)])
    view.set_view(min(stamps) + 30_000)
    steps = QSignalSpy(panel.frame_step_requested)
    start = view.view_ms
    QTest.keyClick(view, Qt.Key_Right)
    assert view.view_ms > start and not steps
    start = view.view_ms
    QTest.keyClick(view, Qt.Key_Left, Qt.ShiftModifier)
    assert view.view_ms < start and not steps
    zoom = view.px_per_s
    QTest.keyClick(view, Qt.Key_Plus)
    assert view.px_per_s > zoom
    QTest.keyClick(view, Qt.Key_End)
    assert view.view_ms + view.span_ms() == pytest.approx(view.extent()[1])
    QTest.keyClick(view, Qt.Key_Home)
    assert view.view_ms == view.extent()[0]
    QTest.keyClick(panel.table, Qt.Key_Right)
    QTest.keyClick(panel.table, Qt.Key_Left, Qt.ShiftModifier)
    assert [entry[0] for entry in steps] == [1, -5]


def test_release_after_a_drag_is_not_a_jump(qapp, tmp_path):
    view = RaceStripView()
    view.set_pieces([piece(tmp_path)])
    view.set_view(T)
    clicks = QSignalSpy(view.clicked)
    QTest.mousePress(view, Qt.LeftButton, pos=QPoint(60, 60))
    # Even if Qt coalesces the move events before release, this was a drag.
    QTest.mouseRelease(view, Qt.LeftButton, pos=QPoint(60, 80))
    assert not clicks
    QTest.mouseClick(view, Qt.LeftButton, pos=QPoint(80, 60))
    assert len(clicks) == 1


def test_frame_cursor_is_in_chip_time_and_does_not_load_a_second_ruler(panel):
    frame = dict(panel.frame, recorder_ms=panel.frame["recorder_ms"] + 600)
    panel.record_viewed_frame(frame, frame["recorder_ms"])
    assert panel.race_strip.view.cursor == frame["recorder_ms"]  # no calibration in this fixture
    assert panel.timeline.isHidden() and panel.timeline_help.isHidden()
    assert panel.timeline.current == frame["recorder_ms"]


def test_old_workspace_result_cannot_replace_the_new_picture(qapp, tmp_path):
    strip = RaceStripPanel()
    strip.load(tmp_path, background=False)
    old_token = strip._token
    strip.load(tmp_path / "other", background=False)
    strip._apply((old_token, ([piece(tmp_path)], {})))
    assert strip.view.pieces == []


def test_full_extent_includes_footage_before_and_after_all_chips(qapp, tmp_path):
    view = RaceStripView()
    recording = piece(tmp_path, T, T + 600_000)
    view.set_pieces([recording])
    view.set_chips([(T + 300_000, "1", False)])
    assert view.extent() == (recording.chip_start, recording.chip_end)


def test_rescan_keeps_unchanged_strip_images_but_file_stamp_invalidates_one(qapp, tmp_path):
    from tests.realtime.test_race_strip_data import _save

    recording = piece(tmp_path)
    _save(recording.path, segment_id="archive", start_ms=T, duration_ms=60_000, frames=25)
    view = RaceStripView()
    view.set_pieces([recording])
    _times, image = view.cache.get(recording)
    view.set_pieces([recording])
    assert view.cache.get(recording)[1] is image
    assert view.cache.get(replace(recording, stamp=(1, 2)))[1] is not image


def test_frequent_workbench_refresh_does_not_restart_the_strip_scan(qapp, tmp_path, monkeypatch):
    strip = RaceStripPanel()
    reads = []
    monkeypatch.setattr(strip, "_read", lambda *args: reads.append(args))
    strip.load(tmp_path, background=False)
    token = strip._token
    for _ in range(10):
        strip.load(tmp_path, background=False)
    assert len(reads) == 1 and strip._token == token
