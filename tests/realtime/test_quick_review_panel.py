import json
from dataclasses import replace

import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtTest import QSignalSpy, QTest
from PyQt5.QtWidgets import QApplication, QLabel

from realtime.problem_items import CATEGORY_VIDEO, KIND_TWO_CHIPS_ONE_CROSSING, ProblemItem
from realtime.quick_review_panel import QuickReviewPanel
from realtime.chip_history import format_chip_time
from tests.realtime.test_arrival_review import event, FRAME, SCOPE


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def panel(qapp, tmp_path):
    directory = tmp_path / "finish_problems"
    directory.mkdir()
    item = ProblemItem("group", KIND_TWO_CHIPS_ONE_CROSSING, CATEGORY_VIDEO,
                       "旧识别描述不应出现在判读界面", chip_time_ms=FRAME["recorder_ms"],
                       bibs=("321", "258"), event_ids=("one", "two"),
                       evidence={"frame": FRAME, "bike": {"label": "bicycle", "score": .94}})
    (directory / "problem_list.json").write_text(json.dumps({"race_id": SCOPE[0], "stage_id": SCOPE[1],
                                                            "items": [item.to_payload()]}), encoding="utf-8")
    widget = QuickReviewPanel()
    widget.events = [event(), event("258", "two", FRAME["recorder_ms"] + 500)]
    widget.frame = dict(FRAME)
    widget.set_events_provider(lambda: widget.events)
    widget.set_frame_provider(lambda: widget.frame)
    widget.set_workspace(tmp_path)
    widget.set_scope(*SCOPE)
    return widget


def row(panel, bib):
    return next(i for i, entry in enumerate(panel._arrival_rows) if entry.bib == bib)


def test_one_table_no_model_descriptions_and_one_is_not_counter(panel):
    assert panel.table.columnCount() == 4
    assert [panel.table.horizontalHeaderItem(i).text() for i in range(4)] == ["顺序", "号码", "时间", "状态"]
    assert all("参考" not in panel.table.item(row_index, 2).text() for row_index in range(panel.table.rowCount()))
    for index, entry in enumerate(panel._arrival_rows):
        assert panel.table.item(index, 1).text() == entry.bib
        assert panel.table.item(index, 2).text() == format_chip_time(entry.time_ms)
    assert panel.detail_scroll.isHidden() and panel.header.isHidden()
    for _ in range(5):
        QTest.keyClick(panel.table, Qt.Key_1)
    assert sum(entry.decision is not None for entry in panel._arrival_rows) == 1
    assert [entry["action"] for entry in panel._arrival_store.active(SCOPE)] == ["bicycle", "confirm"]
    for i in range(panel.table.rowCount()):
        assert "自行车" not in " ".join(panel.table.item(i, j).text() for j in range(4))


def test_finish_line_has_one_visible_entry(panel):
    from PyQt5.QtWidgets import QPushButton

    assert panel.finish_line_button.text() == "终点线 / 模型"
    menu_texts = [
        action.text()
        for button in panel.quick_toolbar.findChildren(QPushButton)
        if button.menu() is not None
        for action in button.menu().actions()
    ]
    assert "终点线 / 模型" not in menu_texts


def test_unclassified_chip_click_requests_playback_but_classified_picture_is_preserved(panel):
    changes = QSignalSpy(panel.current_changed)
    panel._arrival_clicked(row(panel, "258"))
    assert changes and panel.current_item() == panel._selected_arrival().item
    assert not any(entry.decision for entry in panel._arrival_rows)

    panel._classify("bicycle")
    changes = QSignalSpy(panel.current_changed)
    panel._arrival_clicked(row(panel, "321"))
    assert not changes  # Identity assignment must not seek away from a judged frame.
    assert panel._selected_arrival().bib == "321"


def test_f_enlarges_evidence_and_escape_restores_arrival_table(panel):
    changes = QSignalSpy(panel.big_view_changed)
    panel.table.setFocus()
    QTest.keyClick(panel.table, Qt.Key_F)
    assert panel.big_view
    assert not panel.table.isHidden()
    assert not panel.quick_toolbar.isHidden()
    assert changes and changes[-1][0] is True

    QTest.keyClick(panel, Qt.Key_Escape)
    assert not panel.big_view
    assert not panel.table.isHidden()
    assert changes[-1][0] is False


def test_selecting_athlete_does_not_confirm_until_explicit_finish_line_action(panel):
    panel._classify("bicycle")
    panel._arrival_clicked(row(panel, "321"))
    assert not any(entry.decision for entry in panel._arrival_rows)
    assert panel._selected_arrival().bib == "321"
    assert panel.confirm_arrival_button.isEnabled()  # F changes layout, not permission to judge.

    panel.toggle_big_view()
    assert panel.confirm_arrival_button.isEnabled()
    panel.frame = dict(FRAME, frame_index=8, position_ms=400, recorder_ms=FRAME["recorder_ms"] + 150)
    panel.confirm_arrival_button.click()
    entries = [entry for entry in panel._arrival_rows if entry.decision]
    assert len(entries) == 1 and entries[0].bib == "321"
    assert entries[0].decision["frame"]["frame_index"] == 8

    panel.frame = dict(FRAME, frame_index=9, position_ms=450, recorder_ms=FRAME["recorder_ms"] + 200)
    panel._arrival_clicked(row(panel, "258"))
    assert len([entry for entry in panel._arrival_rows if entry.decision]) == 1
    assert panel.confirm_arrival_button.isEnabled()
    panel.confirm_arrival_button.click()
    entries = [entry for entry in panel._arrival_rows if entry.decision]
    assert [entry.bib for entry in entries] == ["321", "258"]
    assert entries[1].decision["frame"]["frame_index"] == 9
    jumps = []
    panel.jump_requested.connect(jumps.append)
    panel._arrival_clicked(row(panel, "321"))
    assert jumps[-1]["frame_index"] == 8
    assert sum(entry.decision is not None for entry in panel._arrival_rows) == 2


def test_backtracking_sorts_actual_times_and_undo_restores_reference(panel):
    panel._classify("bicycle")
    panel.toggle_big_view()
    panel._arrival_clicked(row(panel, "258"))
    panel.confirm_arrival_button.click()
    panel.frame = dict(FRAME, frame_index=3, recorder_ms=FRAME["recorder_ms"] - 100)
    panel._arrival_clicked(row(panel, "321"))
    panel.confirm_arrival_button.click()
    assert [entry.bib for entry in panel._arrival_rows] == ["321", "258"]
    assert all(not entry.reference for entry in panel._arrival_rows)
    QTest.keyClick(panel, Qt.Key_Backspace)
    assert panel._arrival_rows[row(panel, "321")].reference
    assert not panel._arrival_rows[row(panel, "258")].reference


def test_upstream_change_invalidates_identity_and_restart_recovers(panel, tmp_path):
    panel._classify("bicycle")
    panel.toggle_big_view()
    panel._arrival_clicked(row(panel, "321"))
    panel.confirm_arrival_button.click()
    panel.events[0] = replace(panel.events[0], bib="322", revision=2)
    panel.refresh()
    entry = panel._arrival_rows[row(panel, "322")]
    assert entry.stale and entry.decision is None
    assert panel.table.item(row(panel, "322"), 3).text() == "待定"
    panel.events[0] = event()
    panel.refresh()
    restored = QuickReviewPanel()
    restored.set_events_provider(lambda: panel.events)
    restored.set_workspace(tmp_path)
    restored.set_scope(*SCOPE)
    assert restored._arrival_rows[row(restored, "321")].decision is not None


def test_no_picture_no_confirmation_and_no_automatic_identity(panel):
    panel.frame = None
    panel._classify("bicycle")
    panel.toggle_big_view()
    panel._arrival_clicked(row(panel, "321"))
    panel.confirm_arrival_button.click()
    assert panel._arrival_store.active(SCOPE) == []
    panel.frame = dict(FRAME)
    panel._classify("bicycle")
    panel.frame = None
    panel._arrival_clicked(row(panel, "321"))
    assert not panel.confirm_arrival_button.isEnabled()
    panel.confirm_arrival_button.click()
    assert not any(entry.decision for entry in panel._arrival_rows)


def test_confirmation_explains_missing_classification_and_works_without_f(panel):
    assert not panel.confirm_arrival_button.isEnabled()
    assert "1" in panel.confirmation_status.text()
    panel._classify("bicycle")
    assert not panel.big_view
    assert panel.confirm_arrival_button.isEnabled()
    QTest.keyClick(panel, Qt.Key_Return)
    assert sum(row.decision is not None for row in panel._arrival_rows) == 1


def test_athlete_waiting_for_analysis_does_not_block_manual_candidate(panel):
    from realtime.problem_items import STATUS_WAITING
    panel._arrival_rows[0].item = replace(panel._arrival_rows[0].item, status=STATUS_WAITING)
    # The source candidate remains usable; the chip row's automatic-analysis status is separate.
    panel._active_item = panel.items[0]
    panel._classify("bicycle")
    assert panel.confirm_arrival_button.isEnabled()
    panel.confirm_arrival_button.click()
    assert sum(row.decision is not None for row in panel._arrival_rows) == 1


def test_exclude_and_hold_are_manual_and_do_not_delete_chip_events(panel):
    QTest.keyClick(panel, Qt.Key_2)
    assert panel._arrival_store.active(SCOPE)[-1]["action"] == "not_rider"
    assert all(target.is_active for target in panel.events)
    assert not any(entry.decision for entry in panel._arrival_rows)
    QTest.keyClick(panel, Qt.Key_3)
    assert not panel._arrival_store.active(SCOPE)
    panel._classify("held")
    assert panel._arrival_store.active(SCOPE)[-1]["action"] == "held"


def test_manual_confirmation_replaces_candidate_row_and_keeps_explicit_category(panel):
    from realtime.problem_items import STATUS_WAITING

    path = panel.problem_dir / "problem_list.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["items"][0]["status"] = STATUS_WAITING
    path.write_text(json.dumps(payload), encoding="utf-8")
    panel.refresh()
    manual = panel.add_manual_problem(FRAME, recorder_ms=FRAME["recorder_ms"])
    panel.select_item(manual.item_id)
    panel._classify("bicycle")
    candidate_index = next(i for i, entry in enumerate(panel._arrival_rows) if entry.item.item_id == manual.item_id)
    assert panel.table.item(candidate_index, 3).text() == "待定"
    panel._arrival_clicked(row(panel, "321"))
    panel.confirm_arrival_button.click()
    panel.refresh()

    target = panel._arrival_rows[row(panel, "321")]
    assert target.decision and target.item.item_id == manual.item_id
    assert panel.table.item(row(panel, "321"), 3).text() == "已确认"
    assert panel.table.item(row(panel, "321"), 3).foreground().color().name() == "#126044"
    assert not any(entry.event is None and entry.item.item_id == manual.item_id for entry in panel._arrival_rows)
    assert panel._selected_arrival().bib == "321"
    panel._undo_arrival()
    assert any(entry.event is None and entry.item.item_id == manual.item_id for entry in panel._arrival_rows)


def test_non_rider_category_is_explicit_and_preserved_after_refresh(panel):
    panel._classify("not_rider")
    panel.refresh()
    assert all(panel.table.item(i, 3).foreground().color().name() == "#1d4ed8" for i in range(panel.table.rowCount()))
    assert all(panel.table.item(i, 3).text() == "已排除" for i in range(panel.table.rowCount()))
    assert all(panel.table.item(i, 0).text() == "" for i in range(panel.table.rowCount()))
    assert all(target.is_active for target in panel.events)


def test_numbers_follow_chip_order_even_when_video_times_cross_and_candidates_are_added(panel):
    panel.events[0] = replace(panel.events[0], sequence=2)
    panel.events[1] = replace(panel.events[1], sequence=1)
    panel.refresh()
    manual = panel.add_manual_problem(FRAME, recorder_ms=FRAME["recorder_ms"])
    manual_row = next(i for i, entry in enumerate(panel._arrival_rows) if entry.event is None)
    assert panel.table.item(manual_row, 0).text() == ""
    assert [entry.bib for entry in panel._arrival_rows[:2]] == ["258", "321"]
    assert [panel.table.item(i, 0).text() for i in range(2)] == ["1", "2"]
    panel.select_item(manual.item_id)
    panel._classify("bicycle")
    panel._arrival_clicked(row(panel, "321"))
    panel.frame = dict(FRAME, recorder_ms=FRAME["recorder_ms"] - 3000)
    panel.confirm_arrival_button.click()
    assert [entry.bib for entry in panel._arrival_rows] == ["258", "321"]
    assert [panel.table.item(i, 0).text() for i in range(2)] == ["1", "2"]


def test_circle_uses_selected_bib_then_keeps_saved_bib_when_selection_changes(panel):
    frame = dict(FRAME, marker_x_normalized=.4, marker_y_normalized=.6)
    manual = panel.add_manual_problem(frame, recorder_ms=FRAME["recorder_ms"])
    panel.select_item(manual.item_id)
    panel.add_mark(.4, .6, FRAME["recorder_ms"], frame)
    panel._classify("bicycle")
    mark = panel.current_marks()[0]
    assert panel.picture_mark_label(mark) == "待选号码"
    panel._arrival_clicked(row(panel, "321"))
    assert panel.picture_mark_label(mark) == "321"
    panel._arrival_clicked(row(panel, "258"))
    assert panel.picture_mark_label(mark) == "258"
    panel._confirm_bicycle()
    assert panel._selected_arrival().decision
    panel._arrival_clicked(row(panel, "321"))
    assert panel.picture_mark_label(mark) == "258"
    panel.refresh()
    assert panel.picture_mark_label(mark) == "258"


def test_three_undoes_legacy_picture_entries_and_restores_the_last_confirmed_athlete(panel):
    panel._classify("bicycle")
    panel._arrival_clicked(row(panel, "321"))
    panel.confirm_arrival_button.click()
    manual = panel.add_manual_problem(dict(FRAME, marker_x_normalized=.4, marker_y_normalized=.6),
                                      recorder_ms=FRAME["recorder_ms"])
    panel.select_item(manual.item_id)
    panel._classify("bicycle")  # Old releases saved this automatically on a picture click.
    QTest.keyClick(panel, Qt.Key_3)
    assert not any(entry.decision for entry in panel._arrival_rows)
    assert panel._selected_arrival().bib == "321"
    assert panel.table.item(panel.table.currentRow(), 3).text() == "待定"


def test_csv_uses_bib_and_actual_frame_not_official_chip_time(panel):
    panel._classify("bicycle")
    panel.toggle_big_view()
    panel._arrival_clicked(row(panel, "321"))
    panel.confirm_arrival_button.click()
    path = panel.export_arrivals()
    text = path.read_text(encoding="utf-8-sig")
    assert "321" in text and "a.mkv,5,0" in text


def test_three_undoes_selected_210_after_later_confirmations_and_restart(panel, tmp_path):
    panel.events = [replace(panel.events[0], bib="210"), replace(panel.events[1], bib="218"),
                    event("212", "three", FRAME["recorder_ms"] + 1000)]
    panel.refresh()
    for bib in ("210", "218", "212"):
        panel._arrival_clicked(row(panel, bib))
        panel._confirm_bicycle()
    assert sum(entry.decision is not None for entry in panel._arrival_rows) == 3
    panel._arrival_store.save(SCOPE, "candidate:later", "frame", "not_rider")
    restored = QuickReviewPanel()
    restored.set_events_provider(lambda: panel.events)
    restored.set_frame_provider(lambda: dict(FRAME))
    restored.set_workspace(tmp_path)
    restored.set_scope(*SCOPE)
    restored.show()
    restored._arrival_clicked(row(restored, "210"))
    original_number = restored.table.item(row(restored, "210"), 0).text()
    other_ids = {entry.bib: entry.decision["id"] for entry in restored._arrival_rows if entry.bib != "210"}
    QTest.keyClick(restored.table, Qt.Key_3)
    assert restored._selected_arrival().bib == "210"
    assert restored._selected_arrival().decision is None
    assert restored.table.item(row(restored, "210"), 3).text() == "待定"
    assert restored.table.item(row(restored, "210"), 3).foreground().color().name() == "#b42318"
    assert restored.table.item(row(restored, "210"), 0).text() == original_number
    assert {entry.bib: entry.decision["id"] for entry in restored._arrival_rows
            if entry.decision} == other_ids
    assert restored._arrival_store.latest(SCOPE, "candidate:later")["action"] == "not_rider"


def test_targeted_undo_restores_earlier_riders_circle_before_reconfirming(panel):
    frame = dict(FRAME, marker_x_normalized=.4, marker_y_normalized=.6)
    manual = panel.add_manual_problem(frame, recorder_ms=FRAME["recorder_ms"])
    panel.select_item(manual.item_id)
    panel.add_mark(.4, .6, FRAME["recorder_ms"], frame)
    panel.add_mark(.8, .6, FRAME["recorder_ms"], frame)
    panel._arrival_clicked(row(panel, "321"))
    panel._editing_mark = 0
    panel._confirm_bicycle()
    panel._arrival_clicked(row(panel, "258"))
    panel._editing_mark = 1
    panel._confirm_bicycle()
    panel._arrival_clicked(row(panel, "321"))
    QTest.keyClick(panel, Qt.Key_3)
    assert panel._editing_mark == 0
    assert panel._selected_arrival().decision is None
    assert panel._arrival_rows[row(panel, "258")].decision is not None
    panel._confirm_bicycle()
    assert panel._selected_arrival().decision["frame"]["marker_x_normalized"] == .4




def test_cancel_unassigned_picture_mark_and_undo(panel):
    panel.add_mark(.4, .6, FRAME["recorder_ms"], FRAME)
    panel.add_mark(.8, .6, FRAME["recorder_ms"], FRAME)
    assert not panel.cancel_picture_mark(.1, .1)
    assert len(panel.current_marks()) == 2
    assert panel.cancel_picture_mark(.4, .6)
    assert [mark["x"] for mark in panel.current_marks()] == [.8]
    panel._undo_arrival()
    assert panel.current_marks() == []


def test_cancel_confirmed_circle_also_undoes_identity(panel):
    panel.add_mark(.4, .6, FRAME["recorder_ms"], FRAME)
    panel._arrival_clicked(row(panel, "321"))
    panel._confirm_bicycle()
    assert panel._selected_arrival().decision is not None
    assert panel.cancel_picture_mark(.4, .6)
    assert panel.current_marks() == []
    assert not any(entry.decision for entry in panel._arrival_rows)


def test_live_calibration_shows_judgment_time_and_keeps_recorder_time_in_tooltip(panel, tmp_path):
    from realtime.passage_evidence import VideoClockCalibrationStore

    VideoClockCalibrationStore(tmp_path / "video_clock_calibrations.jsonl").record(
        camera_index=1, session_key="camera_01_review|videos|a", offset_ms=2138,
        anchor_event_id="one", anchor_bib="321", calibrated_at_ms=1)
    panel._classify("bicycle")
    panel._arrival_clicked(row(panel, "321"))
    panel.confirm_arrival_button.click()

    index = row(panel, "321")
    entry = panel._arrival_rows[index]
    assert entry.time_ms == FRAME["recorder_ms"] - 2138
    assert panel.table.item(index, 2).text() == format_chip_time(FRAME["recorder_ms"] - 2138)
    tooltip = panel.table.item(index, 2).toolTip()
    assert "录像原时刻" in tooltip
    assert format_chip_time(FRAME["recorder_ms"]) in tooltip
