import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtTest import QSignalSpy, QTest
from PyQt5.QtWidgets import QApplication, QLabel

from realtime.problem_items import (
    CATEGORY_VIDEO, KIND_NO_CHIP_CROSSING, KIND_TWO_CHIPS_ONE_CROSSING, ProblemItem,
)
from realtime.quick_review_panel import QuickReviewPanel
from realtime.chip_history import format_chip_time
from realtime.arrival_review import candidate_binding
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


def menu_labels(panel, index=None):
    target = panel._selected_arrival() if index is None else panel._arrival_rows[index]
    return [label for label, _ in panel._row_actions(target)]


def menu(panel, label, index=None):
    """Pick an item of the left table's right-click menu, as the operator would."""

    target = panel._selected_arrival() if index is None else panel._arrival_rows[index]
    dict(panel._row_actions(target))[label]()


def test_one_table_no_model_descriptions_and_one_is_not_counter(panel):
    assert panel.table.columnCount() == 5
    assert [panel.table.horizontalHeaderItem(i).text() for i in range(4)] == ["顺序", "号码", "时间", "状态"]
    assert panel.table.isColumnHidden(4)  # one group: no group column
    assert all("参考" not in panel.table.item(row_index, 2).text() for row_index in range(panel.table.rowCount()))
    for index, entry in enumerate(panel._arrival_rows):
        assert panel.table.item(index, 1).text() == entry.bib
        assert panel.table.item(index, 2).text() == format_chip_time(entry.time_ms)
    assert panel.detail_scroll.isHidden() and panel.header.isHidden()
    for key in (Qt.Key_5, Qt.Key_NumberSign):
        QTest.keyClick(panel.table, key)  # 5 and # are no keys any more: nothing is judged
    assert not panel._arrival_store.active(SCOPE)
    assert "这个键不用了" in panel.hint.text()
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


def test_search_jumps_to_the_bib_and_leaves_the_judging_keys_on_the_table(panel, qapp):
    panel.show()
    qapp.processEvents()
    panel._arrival_clicked(row(panel, "321"))
    panel.search.setFocus()
    QTest.keyClicks(panel.search, "258")
    assert not panel._arrival_store.active(SCOPE)  # typed digits are not judging keys
    QTest.keyClick(panel.search, Qt.Key_Return)
    assert panel._selected_key == panel._arrival_rows[row(panel, "258")].key
    assert QApplication.focusWidget() is panel.table
    panel.search.setText("999")
    panel._find()
    assert "没有找到 999" in panel.hint.text()
    assert panel._selected_key == panel._arrival_rows[row(panel, "258")].key


def test_group_column_appears_only_when_the_race_has_several_groups(panel):
    assert panel.table.isColumnHidden(4)
    panel.events = [replace(panel.events[0], group_name="男子精英"),
                    replace(panel.events[1], group_id="h", group_name="女子组")]
    panel.refresh()
    assert not panel.table.isColumnHidden(4)
    assert panel.table.item(row(panel, "258"), 4).text() == "女子组"
    assert panel.table.minimumWidth() >= 450


def test_delete_asks_the_window_for_the_selected_chip_only(panel):
    deletes = QSignalSpy(panel.delete_requested)
    panel._selected_key = None
    panel._request_delete()
    assert not deletes and "先在左表选中" in panel.hint.text()
    panel._arrival_clicked(row(panel, "258"))
    panel._request_delete()
    assert [entry[0] for entry in deletes] == ["two"]


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
    QTest.keyClick(panel.table, Qt.Key_F)  # F no longer enlarges anything
    assert not panel.big_view and not changes
    panel.toggle_big_view()
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


def test_enter_never_confirms_a_picture_and_no_number_key_is_asked_for(panel):
    assert not panel.confirm_arrival_button.isEnabled()
    assert "按 1" not in panel.confirmation_status.text()
    panel._classify("bicycle")
    assert not panel.big_view
    QTest.keyClick(panel, Qt.Key_Return)  # nobody counted: Enter only moves on
    assert not any(row.decision for row in panel._arrival_rows)


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
    walker = panel.add_manual_problem(dict(FRAME, recorder_ms=FRAME["recorder_ms"] + 5000, frame_index=105),
                                      recorder_ms=FRAME["recorder_ms"] + 5000)
    panel.select_item(walker.item_id)
    assert menu_labels(panel) == ["补录号码…", "不是运动员"]
    menu(panel, "不是运动员")
    assert panel._arrival_store.active(SCOPE)[-1]["action"] == "not_rider"
    assert all(target.is_active for target in panel.events)
    assert not any(entry.decision for entry in panel._arrival_rows if entry.event is not None)
    QTest.keyClick(panel, Qt.Key_Backspace)
    assert not panel._arrival_store.active(SCOPE)
    assert "已撤销排除" in panel.hint.text()
    QTest.keyClick(panel, Qt.Key_2)  # 2 is for a chip row: nothing on a picture without one
    assert not panel._arrival_store.active(SCOPE)
    assert "双芯片用在芯片行" in panel.hint.text()
    panel._classify("held")
    assert panel._arrival_store.active(SCOPE)[-1]["action"] == "held"


def test_not_a_rider_on_a_pedestrian_never_reopens_a_confirmed_rider(panel):
    """路人那一行右键「不是运动员」；不会把前面已确认的选手撤掉。"""

    panel._classify("bicycle")
    panel._arrival_clicked(row(panel, "321"))
    panel.confirm_arrival_button.click()
    walker = panel.add_manual_problem(dict(FRAME, recorder_ms=FRAME["recorder_ms"] + 300, frame_index=11),
                                      recorder_ms=FRAME["recorder_ms"] + 300)
    panel.select_item(walker.item_id)
    menu(panel, "不是运动员")
    assert panel._arrival_store.latest(SCOPE, "candidate:" + walker.item_id)["action"] == "not_rider"
    assert panel._arrival_rows[row(panel, "321")].decision is not None


def test_undo_on_an_undecided_row_leaves_the_earlier_confirmation_alone(panel):
    """古浪测试：在没判过的行上撤销，曾经撤掉了 25 分钟前确认的 20 号。"""

    panel._classify("bicycle")
    panel._arrival_clicked(row(panel, "321"))
    panel.confirm_arrival_button.click()
    blank = panel.add_manual_problem(dict(FRAME, recorder_ms=FRAME["recorder_ms"] + 300, frame_index=11),
                                     recorder_ms=FRAME["recorder_ms"] + 300)
    panel.select_item(blank.item_id)
    QTest.keyClick(panel, Qt.Key_Backspace)
    assert panel._arrival_rows[row(panel, "321")].decision is not None
    assert panel.hint.text() == "这一行没有可撤销的判定。"
    panel._arrival_clicked(row(panel, "258"))
    QTest.keyClick(panel, Qt.Key_Backspace)
    assert panel._arrival_rows[row(panel, "321")].decision is not None
    panel._arrival_clicked(row(panel, "321"))
    QTest.keyClick(panel, Qt.Key_Backspace)
    assert panel._arrival_rows[row(panel, "321")].decision is None
    assert "已撤销 321 号确认" in panel.hint.text()


def test_undo_right_after_exclusion_reaches_it_across_the_automatic_step(panel):
    first = panel.add_manual_problem(dict(FRAME, recorder_ms=FRAME["recorder_ms"] + 300, frame_index=11),
                                     recorder_ms=FRAME["recorder_ms"] + 300)
    second = panel.add_manual_problem(dict(FRAME, recorder_ms=FRAME["recorder_ms"] + 5000, frame_index=105),
                                      recorder_ms=FRAME["recorder_ms"] + 5000)
    panel.select_item(first.item_id)
    menu(panel, "不是运动员")
    assert panel.current_item().item_id == second.item_id  # 排除后自动跳到下一处
    QTest.keyClick(panel, Qt.Key_Backspace)
    assert panel._arrival_store.latest(SCOPE, "candidate:" + first.item_id) is None
    assert panel.current_item().item_id == first.item_id


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
    """Chip time, not the order records came in (古浪 6 号 was imported last but crossed mid-wave)."""
    panel.events[0] = replace(panel.events[0], sequence=2)
    panel.events[1] = replace(panel.events[1], sequence=1)
    panel.refresh()
    manual = panel.add_manual_problem(FRAME, recorder_ms=FRAME["recorder_ms"])
    manual_row = next(i for i, entry in enumerate(panel._arrival_rows) if entry.event is None)
    assert panel.table.item(manual_row, 0).text() == ""

    def chip_order():
        return [(entry.bib, panel.table.item(i, 0).text())
                for i, entry in enumerate(panel._arrival_rows) if entry.event is not None]

    assert chip_order() == [("321", "1"), ("258", "2")]
    panel.select_item(manual.item_id)
    panel._classify("bicycle")
    panel._arrival_clicked(row(panel, "321"))
    panel.frame = dict(FRAME, recorder_ms=FRAME["recorder_ms"] - 3000)
    panel.confirm_arrival_button.click()
    assert chip_order() == [("321", "1"), ("258", "2")]


def test_a_crossing_without_a_chip_sits_at_its_own_time_between_the_chips(panel):
    """画面里没芯片的过线按到达时间插在芯片之间，不集中放在最后一名之后。"""

    early = panel.add_manual_problem(dict(FRAME, recorder_ms=FRAME["recorder_ms"] - 1000),
                                     recorder_ms=FRAME["recorder_ms"] - 1000)
    middle = panel.add_manual_problem(dict(FRAME, recorder_ms=FRAME["recorder_ms"] + 300, frame_index=11),
                                      recorder_ms=FRAME["recorder_ms"] + 300)
    late = panel.add_manual_problem(dict(FRAME, recorder_ms=FRAME["recorder_ms"] + 5000, frame_index=105),
                                    recorder_ms=FRAME["recorder_ms"] + 5000)
    order = [entry.bib or entry.item.item_id for entry in panel._arrival_rows]
    assert order == [early.item_id, "321", middle.item_id, "258", late.item_id]
    # 名次只跟芯片走：插进来的那一行没有名次，后面的号码不因它往后挪。
    numbers = [panel.table.item(i, 0).text() for i in range(panel.table.rowCount())]
    assert numbers == ["", "1", "", "2", ""]


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
    panel.refresh()
    assert panel.picture_mark_label(mark) == "258"
    # Everyone on the picture has a row now: the next chip opens its own.
    panel._arrival_clicked(row(panel, "321"))
    assert panel.current_item().item_id != manual.item_id
    panel._arrival_clicked(row(panel, "258"))
    assert panel.current_item().item_id == manual.item_id and panel.picture_mark_label(mark) == "258"


def test_undo_on_another_picture_takes_back_only_that_picture(panel):
    panel._classify("bicycle")
    panel._arrival_clicked(row(panel, "321"))
    panel.confirm_arrival_button.click()
    manual = panel.add_manual_problem(dict(FRAME, marker_x_normalized=.4, marker_y_normalized=.6),
                                      recorder_ms=FRAME["recorder_ms"])
    panel.select_item(manual.item_id)
    panel._classify("bicycle")  # Old releases saved this automatically on a picture click.
    QTest.keyClick(panel, Qt.Key_Backspace)
    assert panel._arrival_store.latest(SCOPE, "candidate:" + manual.item_id) is None
    assert panel._arrival_rows[row(panel, "321")].decision is not None


def test_csv_uses_bib_and_actual_frame_not_official_chip_time(panel):
    panel._classify("bicycle")
    panel.toggle_big_view()
    panel._arrival_clicked(row(panel, "321"))
    panel.confirm_arrival_button.click()
    path = panel.export_arrivals()
    text = path.read_text(encoding="utf-8-sig")
    assert "321" in text and "a.mkv,5,0" in text


def test_undo_takes_back_selected_210_after_later_confirmations_and_restart(panel, tmp_path):
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
    QTest.keyClick(restored.table, Qt.Key_Backspace)
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
    QTest.keyClick(panel, Qt.Key_Backspace)
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
    # The column reads in chip order; the judged moment is in the tooltip.
    assert panel.table.item(index, 2).text() == format_chip_time(entry.event.timeline_timestamp_ms)
    tooltip = panel.table.item(index, 2).toolTip()
    assert f"判读时间（校时后）：{format_chip_time(FRAME['recorder_ms'] - 2138)}" in tooltip
    assert "录像原时刻" in tooltip
    assert format_chip_time(FRAME["recorder_ms"]) in tooltip


T0 = FRAME["recorder_ms"]


@pytest.fixture
def wave_panel(qapp, tmp_path):
    """Three chips within 2 s (one wave: 61, 20, 76), then 5 alone ten seconds later."""
    directory = tmp_path / "finish_problems"
    directory.mkdir()
    (directory / "problem_list.json").write_text(json.dumps({"race_id": SCOPE[0], "stage_id": SCOPE[1],
                                                            "items": []}), encoding="utf-8")
    widget = QuickReviewPanel()
    widget.events = [event("61", "a", T0), event("20", "b", T0 + 400), event("76", "c", T0 + 900),
                     event("5", "d", T0 + 10_000)]
    widget.frame = dict(FRAME)
    widget.set_events_provider(lambda: widget.events)
    widget.set_frame_provider(lambda: widget.frame)
    widget.set_workspace(tmp_path)
    widget.set_scope(*SCOPE)
    return widget


def count_on_picture(panel, x, offset_ms):
    """What the window does for a click on the picture (see _quick_review_mark_arrival)."""
    frame = dict(FRAME, frame_index=5 + offset_ms // 50, position_ms=250 + offset_ms, recorder_ms=T0 + offset_ms,
                 marker_x_normalized=x, marker_y_normalized=.5)
    panel.frame = frame
    selected = panel._selected_arrival()
    item, refusal = panel.picture_candidate(frame)
    assert not refusal
    if item is None:
        item = panel.add_manual_problem(frame, recorder_ms=frame["recorder_ms"])
        panel.adopt_picture_candidate(item, selected.key)
    panel._selected_key = selected.key
    panel._active_item = next(value for value in panel.items if value.item_id == item.item_id)
    panel.table.setCurrentCell(next(i for i, entry in enumerate(panel._arrival_rows) if entry.key == selected.key), 0)
    panel.add_mark(x, .5, frame["recorder_ms"], frame)
    panel._quick_render_signature = None
    panel._render()
    return frame


def look_through(panel, start_ms, end_ms, step_ms=200):
    for offset in range(start_ms, end_ms + 1, step_ms):
        panel.record_viewed_frame(dict(FRAME, recorder_ms=T0 + offset), T0 + offset)


def labels(panel):
    return [panel.picture_mark_label(mark) for mark in panel.current_marks()]


def test_chips_within_two_seconds_are_one_wave_and_a_lone_chip_its_own(wave_panel):
    assert [[entry.bib for entry in wave] for wave in wave_panel._waves] == [["61", "20", "76"], ["5"]]
    assert "本波 3 个芯片：61 → 76" in wave_panel.table.item(row(wave_panel, "61"), 0).toolTip()
    assert "本波" not in wave_panel.table.item(row(wave_panel, "5"), 0).toolTip()


def saved(panel):
    """Bib -> chip-relative moment each confirmed chip was saved at."""
    return {entry.bib: entry.decision["frame"]["recorder_ms"] - T0 for entry in panel._arrival_rows
            if entry.decision}


def test_a_click_counts_for_the_wave_at_its_moment_not_the_row_left_selected(wave_panel):
    """古浪：数完 11 号那一波后直接往后看下一波点人，被记成上一波的“多 2”。"""

    wave_panel._arrival_clicked(row(wave_panel, "61"))
    for x, offset in ((.2, 100), (.4, 500), (.6, 700)):
        count_on_picture(wave_panel, x, offset)
    assert wave_panel._selected_arrival().bib == "76"  # the last rider counted stays selected

    count_on_picture(wave_panel, .5, 10_000)
    assert labels(wave_panel) == ["5"]
    assert saved(wave_panel) == {"61": 100, "20": 500, "76": 700, "5": 10_000}
    assert "多出" not in [wave_panel.table.item(i, 3).text() for i in range(wave_panel.table.rowCount())]


def test_an_extra_person_is_a_row_that_opens_where_they_were_clicked(wave_panel):
    wave_panel._arrival_clicked(row(wave_panel, "61"))
    for x, offset in ((.2, 100), (.4, 500), (.6, 700), (.8, 1000)):
        count_on_picture(wave_panel, x, offset)
    jumps = []
    wave_panel.jump_requested.connect(jumps.append)
    wave_panel._arrival_clicked(row(wave_panel, "5"))
    extra = next(i for i, entry in enumerate(wave_panel._arrival_rows) if entry.mark is not None)
    wave_panel._arrival_clicked(extra)
    assert jumps[-1]["recorder_ms"] == T0 + 1000
    assert wave_panel._editing_mark == 3
    assert "多出一人" in wave_panel.hint.text()
    # Up / Down step past it to the pictures either side.
    wave_panel._step(1)
    assert wave_panel._selected_arrival().bib == "5"

    wave_panel._arrival_clicked(extra)
    wave_panel.frame = dict(jumps[-1])
    assert wave_panel.cancel_picture_mark(.8, .5)
    assert all(entry.mark is None for entry in wave_panel._arrival_rows)
    assert wave_panel._selected_arrival().bib in {"61", "20", "76"}
    assert "芯片 3 · 已点 3" in wave_panel.confirmation_status.text()


def test_counting_a_wave_labels_people_in_chip_order_and_flags_the_one_without_a_chip(wave_panel):
    """古浪 6 号：一波 3 个芯片，画面上点出 4 个人；第三个人没有芯片。"""

    wave_panel._arrival_clicked(row(wave_panel, "61"))
    for x, offset in ((.2, 100), (.4, 500), (.6, 700), (.8, 1000)):
        count_on_picture(wave_panel, x, offset)
    assert labels(wave_panel) == ["61", "20", "76", "多 1"]
    assert [wave_panel.picture_mark_warning(mark) for mark in wave_panel.current_marks()] == [False] * 3 + [True]
    assert "芯片 3 · 已点 4 · 多 1 人" in wave_panel.confirmation_status.text()
    assert saved(wave_panel) == {"61": 100, "20": 500, "76": 700}  # each click saved at once
    assert wave_panel.table.item(row(wave_panel, "61"), 0).foreground().color().name() == "#b42318"
    # The wave's picture is not a row of its own; the one extra person is, at their own time.
    assert [entry.bib for entry in wave_panel._arrival_rows] == ["61", "20", "76", "", "5"]
    assert wave_panel.table.item(3, 3).text() == "多出"
    assert wave_panel._arrival_rows[3].time_ms == T0 + 1000

    wave_panel.frame = dict(FRAME, frame_index=19, position_ms=950, recorder_ms=T0 + 700)
    wave_panel._arrival_clicked(row(wave_panel, "76"))  # the third person, picked on the left
    wave_panel._ask_bib = lambda current: None  # the bib is not read yet
    menu(wave_panel, "这人没芯片：补录号码…")
    # The person without a chip moves onto a 疑似漏读 row at their own time; 76 shifts to the next one.
    assert labels(wave_panel) == ["61", "20", "76"]
    assert saved(wave_panel) == {"61": 100, "20": 500, "76": 1000}
    rows = wave_panel._arrival_rows
    assert [entry.bib or "—" for entry in rows] == ["61", "20", "—", "76", "5"]
    # A click only counts someone: 已点, never 已确认 (10-29).
    assert [wave_panel.table.item(i, 3).text() for i in range(4)] == ["已点", "已点", "疑似漏读", "已点"]
    assert rows[2].time_ms == T0 + 700
    assert "无芯片 1" in wave_panel.confirmation_status.text()
    assert "已核" in wave_panel.confirmation_status.text() or "秒没看" in wave_panel.confirmation_status.text()
    # Still drawn on the wave's picture, in red.
    drawn = wave_panel.picture_overlays(wave_panel.frame)
    assert any(label == "无芯片" and alert and not ghost for _x, _y, label, _ok, alert, _dot, ghost in drawn)


def test_every_click_saves_and_the_unseen_part_only_shows_never_blocks(wave_panel):
    wave_panel._arrival_clicked(row(wave_panel, "61"))
    for x, offset in ((.2, 100), (.4, 500), (.6, 1000)):
        count_on_picture(wave_panel, x, offset)
    assert saved(wave_panel) == {"61": 100, "20": 500, "76": 1000}
    look_through(wave_panel, -200, 400)
    assert "秒没看" in wave_panel.confirmation_status.text()  # 20 to 76 is still unseen
    look_through(wave_panel, 400, 1400)
    assert "已核" in wave_panel.confirmation_status.text()
    assert wave_panel.table.item(row(wave_panel, "61"), 0).foreground().color().name() == "#126044"


def test_a_chip_wave_is_looked_at_without_a_margin_but_a_lone_chip_still_has_its_moment_shown(wave_panel):
    """现代五项接力 10-30：有芯片的不再前后多看；只有一个芯片时，那一刻本身仍要看过。"""

    wave_panel._arrival_clicked(row(wave_panel, "5"))
    entry = wave_panel._arrival_rows[row(wave_panel, "5")]
    plan = SimpleNamespace(chips=[entry], item=entry.item)
    assert wave_panel._unseen(plan) == [[T0 + 10_000, T0 + 10_100]]
    assert "从过线那一刻看起" in wave_panel.table.item(row(wave_panel, "5"), 3).toolTip()
    look_through(wave_panel, 10_000, 10_000)
    assert wave_panel._unseen(plan) == []


def test_a_click_before_someone_shifts_the_bibs_after_it_and_a_right_click_shifts_them_back(wave_panel):
    wave_panel._arrival_clicked(row(wave_panel, "61"))
    count_on_picture(wave_panel, .2, 100)
    count_on_picture(wave_panel, .6, 1000)
    assert saved(wave_panel) == {"61": 100, "20": 1000}
    count_on_picture(wave_panel, .4, 500)  # found someone between them
    assert saved(wave_panel) == {"61": 100, "20": 500, "76": 1000}
    assert wave_panel._selected_arrival().bib == "20"  # the left table follows the click
    assert labels(wave_panel) == ["61", "76", "20"]

    wave_panel.frame = dict(FRAME, frame_index=15, position_ms=750, recorder_ms=T0 + 500)
    assert wave_panel.cancel_picture_mark(.4, .5)
    assert saved(wave_panel) == {"61": 100, "20": 1000}
    assert "少 1 人" in wave_panel.confirmation_status.text()
    assert "已取消 20" in wave_panel.hint.text()


def test_a_chip_nobody_was_counted_for_shows_short_and_is_saved_when_found(wave_panel):
    wave_panel._arrival_clicked(row(wave_panel, "61"))
    for x, offset in ((.2, 100), (.4, 500)):
        count_on_picture(wave_panel, x, offset)
    assert "少 1 人" in wave_panel.confirmation_status.text()
    assert wave_panel.table.item(row(wave_panel, "76"), 3).text() == "待定"
    wave_panel._arrival_clicked(row(wave_panel, "76"))
    count_on_picture(wave_panel, .7, 950)
    assert labels(wave_panel)[-1] == "76"
    assert wave_panel.table.item(row(wave_panel, "76"), 3).text() == "已点"


def test_a_person_counted_on_a_nearby_frame_is_drawn_faded_and_can_be_cancelled_there(wave_panel):
    """一帧一帧往后走时，前几帧点过的人以淡色圈留在画面上，免得同一个人点两次。"""

    wave_panel._arrival_clicked(row(wave_panel, "61"))
    count_on_picture(wave_panel, .2, 100)  # frame 7
    later = dict(FRAME, frame_index=10, position_ms=400, recorder_ms=T0 + 250)
    drawn = wave_panel.picture_overlays(later)
    assert [(label, ghost) for _x, _y, label, _ok, _alert, _dot, ghost in drawn] == [("61", True)]
    # Further on the rider has moved away: no ring left on empty road.
    assert wave_panel.picture_overlays(dict(later, frame_index=11, recorder_ms=T0 + 300)) == []
    assert wave_panel.picture_overlays(dict(later, frame_index=5, recorder_ms=T0)) == []  # not before the click
    count_on_picture(wave_panel, .23, 250)
    assert "61 在 3 帧前点过" in wave_panel.picture_mark_hint()
    wave_panel.frame = later
    assert wave_panel.cancel_picture_mark(.23, .5)  # the click of this frame, not the faded one
    assert saved(wave_panel) == {"61": 100}
    wave_panel.frame = dict(later, frame_index=9, recorder_ms=T0 + 200)
    assert wave_panel.cancel_picture_mark(.21, .5)  # the faded one, from a later frame
    assert saved(wave_panel) == {} and wave_panel.current_marks() == []


def test_selecting_the_counting_picture_never_pulls_the_frame_back_to_a_counted_person(wave_panel):
    """古浪 261：点第 5 个人，窗口会再"选中这张图"，不能因此跳回 69 保存的那一帧。"""

    wave_panel._arrival_clicked(row(wave_panel, "61"))
    count_on_picture(wave_panel, .2, 100)
    count_on_picture(wave_panel, .4, 500)
    jumps = QSignalSpy(wave_panel.jump_requested)
    wave_panel.select_item(wave_panel.current_item().item_id)
    assert not jumps
    # Up / Down still go chip row by chip row: from the first chip to the next picture's.
    wave_panel._arrival_clicked(row(wave_panel, "76"))
    wave_panel._step(1)
    assert wave_panel._selected_arrival().bib == "5"
    wave_panel._step(-1)  # 76 has nobody counted yet: a row of its own
    assert wave_panel._selected_arrival().bib == "76"
    wave_panel._step(-1)  # the counted ones share the wave's picture: back to its first
    assert wave_panel._selected_arrival().bib == "61"


def test_backspace_takes_back_the_person_just_clicked_and_a_restart_keeps_the_rest(wave_panel, tmp_path):
    wave_panel._arrival_clicked(row(wave_panel, "61"))
    for x, offset in ((.2, 100), (.4, 500), (.6, 1000)):
        count_on_picture(wave_panel, x, offset)
    QTest.keyClick(wave_panel, Qt.Key_Backspace)
    assert saved(wave_panel) == {"61": 100, "20": 500}
    restored = QuickReviewPanel()
    restored.set_events_provider(lambda: wave_panel.events)
    restored.set_workspace(tmp_path)
    restored.set_scope(*SCOPE)
    assert saved(restored) == {"61": 100, "20": 500}
    assert [restored.table.item(i, 3).text() for i in range(3)] == ["已点", "已点", "待定"]


def test_switching_riders_inside_a_wave_keeps_the_picture_and_leaving_it_opens_the_next(wave_panel):
    wave_panel._arrival_clicked(row(wave_panel, "61"))
    count_on_picture(wave_panel, .2, 100)
    picture = wave_panel.current_item().item_id
    changes = QSignalSpy(wave_panel.current_changed)
    wave_panel._arrival_clicked(row(wave_panel, "76"))
    assert not changes and wave_panel.current_item().item_id == picture
    wave_panel._arrival_clicked(row(wave_panel, "5"))
    assert changes and wave_panel.current_item().item_id != picture
    wave_panel._arrival_clicked(row(wave_panel, "20"))  # coming back opens the wave's own picture
    assert wave_panel.current_item().item_id == picture
    assert labels(wave_panel) == ["61"]


def strip_piece(tmp_path):
    from realtime.race_strip import StripPiece
    video = tmp_path / "camera_01_archive_0000.avi"
    video.touch()
    return StripPiece(tmp_path / "strip.npz", 1, "archive", video, T0 - 60_000, T0 + 60_000, True, 0)


@pytest.fixture
def strip_landing(wave_panel, monkeypatch, tmp_path):
    """The strip sees each wave's first rider 107 ms before its first chip; jumps are shown at once."""

    from realtime import quick_review_panel as module
    wave_panel.race_strip.view.set_pieces([strip_piece(tmp_path)])
    asked = []
    monkeypatch.setattr(module, "first_crossing", lambda pieces, cache, chip_ms: asked.append(chip_ms) or chip_ms - 107)
    monkeypatch.setattr(module, "frame_at", lambda pieces, chip_ms: {"camera_index": 1, "position_ms": chip_ms})
    jumps = []

    def show(frame):
        jumps.append(frame)
        moment = frame.get("recorder_ms", frame["position_ms"])
        wave_panel.record_viewed_frame(dict(FRAME, recorder_ms=moment), moment)

    wave_panel.jump_requested.connect(show)
    return asked, jumps


def test_every_way_into_a_row_lands_on_its_wave_s_first_rider_not_the_chip_time(wave_panel, strip_landing):
    """古浪 267、7、167、76：芯片时间换算落在别人身上；过线图上第一处变化才是这一波第一个人，
    不再提前（15:50 现代五项：落在压线那一刻只看到 4 人中的 2 人，靠整波看完解决；10-30 接力：线前等接棒的人，提前只会看到他们）。"""

    asked, jumps = strip_landing
    first, lone = T0, T0 + 10_000
    wave_panel._arrival_clicked(row(wave_panel, "5"))
    assert jumps[-1] == {"camera_index": 1, "position_ms": lone - 107, "reference_only": True}
    wave_panel._arrival_clicked(row(wave_panel, "76"))  # any rider of the wave: it starts at its first
    assert asked[-1] == first and jumps[-1]["position_ms"] == first - 107
    for enter in (lambda index: wave_panel.table.cellDoubleClicked.emit(index, 0),
                  lambda index: wave_panel.select_item(wave_panel._arrival_rows[index].item.item_id)):
        wave_panel._arrival_clicked(row(wave_panel, "5"))
        enter(row(wave_panel, "20"))
        assert jumps[-1]["position_ms"] == first - 107
    wave_panel._arrival_clicked(row(wave_panel, "76"))
    QTest.keyClick(wave_panel.table, Qt.Key_Down)
    assert wave_panel._selected_arrival().bib == "5" and jumps[-1]["position_ms"] == lone - 107
    QTest.keyClick(wave_panel.table, Qt.Key_Up)
    assert wave_panel._selected_arrival().bib == "76" and jumps[-1]["position_ms"] == first - 107


def test_inside_the_shown_wave_the_picture_stays_and_the_window_does_not_reopen_it(wave_panel, strip_landing):
    _asked, jumps = strip_landing
    held = []
    wave_panel.current_changed.connect(lambda _item: held.append(wave_panel.holds_picture))
    wave_panel._arrival_clicked(row(wave_panel, "61"))
    assert held == [True]  # the panel placed the picture; the window must not reopen the item
    look_through(wave_panel, 300, 700)
    count = len(jumps)
    wave_panel._arrival_clicked(row(wave_panel, "76"))
    wave_panel._arrival_clicked(row(wave_panel, "20"))
    assert len(jumps) == count and all(held)
    count_on_picture(wave_panel, .2, 100)
    wave_panel._arrival_clicked(row(wave_panel, "76"))  # on the wave's own picture too
    assert len(jumps) == count
    assert wave_panel.open_wave_start()  # 回到团首: the first rider, from anywhere in the wave
    assert jumps[-1]["position_ms"] == T0 - 107


def test_after_a_wave_and_a_decided_row_the_next_click_still_lands(wave_panel, strip_landing, monkeypatch):
    from realtime import quick_review_panel as module
    _asked, jumps = strip_landing
    wave_panel._arrival_clicked(row(wave_panel, "61"))
    for x, offset in ((.2, 100), (.4, 500), (.6, 1000)):
        count_on_picture(wave_panel, x, offset)
    look_through(wave_panel, -600, 1500)
    QTest.keyClick(wave_panel, Qt.Key_Return)
    # Confirming goes on to the next wave at its first rider.
    assert wave_panel._selected_arrival().bib == "5" and jumps[-1]["position_ms"] == T0 + 10_000 - 107
    wave_panel._arrival_clicked(row(wave_panel, "20"))  # a decided row: the frame it was decided on
    assert jumps[-1]["recorder_ms"] == T0 + 500 and not jumps[-1].get("reference_only")
    monkeypatch.setattr(module, "first_crossing", lambda pieces, cache, chip_ms: None)
    wave_panel._arrival_clicked(row(wave_panel, "5"))  # the strip cannot tell: the chip time
    assert jumps[-1] == {"camera_index": 1, "position_ms": T0 + 10_000, "reference_only": True}


def test_a_row_opened_before_the_strip_is_read_lands_once_it_is(wave_panel, monkeypatch, tmp_path):
    """启动时过线图还在后台读：先按芯片时间打开，读完再落到这一波第一个人。"""

    from realtime import quick_review_panel as module
    monkeypatch.setattr(module, "first_crossing", lambda pieces, cache, chip_ms: chip_ms - 43)
    monkeypatch.setattr(module, "frame_at", lambda pieces, chip_ms: {"camera_index": 1, "position_ms": chip_ms})
    jumps = QSignalSpy(wave_panel.jump_requested)
    wave_panel._arrival_clicked(row(wave_panel, "76"))
    assert not jumps  # nothing to land on yet: the window opens the chip time meanwhile
    strip = wave_panel.race_strip
    strip._apply((strip._token, ([strip_piece(tmp_path)], {})))
    assert [jump[0]["position_ms"] for jump in jumps] == [T0 - 43]
    strip._apply((strip._token, ([strip_piece(tmp_path)], {})))  # a later rescan does not move it again
    assert len(jumps) == 1


def test_a_crossing_without_a_chip_opens_150_ms_before_it(wave_panel, strip_landing):
    """现代五项：从前 2 秒（10-29）、0.5 秒（10-30）看起人都还在远处；0.25 秒仍嫌多（人多时太麻烦）；从前 0.15 秒看起，只为不在两段视频之间卡一下。"""
    _asked, jumps = strip_landing
    lone = wave_panel.add_manual_problem(dict(FRAME, frame_index=400, recorder_ms=T0 + 20_000),
                                         recorder_ms=T0 + 20_000)
    wave_panel._arrival_clicked(next(i for i, entry in enumerate(wave_panel._arrival_rows)
                                     if entry.item.item_id == lone.item_id))
    assert jumps[-1] == {"camera_index": 1, "position_ms": T0 + 19_850, "reference_only": True}


def test_without_an_aligned_strip_a_crossing_opens_150_ms_back_from_its_own_frame(wave_panel):
    """现代五项 10-29：没有芯片，过线图对不上芯片时间，仍从前 0.15 秒开始看（可能在上一段录像里）。"""
    jumps = []
    wave_panel.jump_requested.connect(jumps.append)
    lone = wave_panel.add_manual_problem(dict(FRAME, frame_index=40, position_ms=2000, recorder_ms=T0 + 20_000),
                                         recorder_ms=T0 + 20_000)
    wave_panel._arrival_clicked(next(i for i, entry in enumerate(wave_panel._arrival_rows)
                                     if entry.item.item_id == lone.item_id))
    assert jumps[-1] == {"camera_index": 1, "segment_id": "seg", "video_path": "a.mkv", "position_ms": 1850,
                         "frame_index": None, "reference_only": True}
    wave_panel.add_manual_problem(dict(FRAME, frame_index=4, position_ms=200, recorder_ms=T0 + 30_000),
                                  recorder_ms=T0 + 30_000)
    wave_panel._arrival_clicked(next(i for i, entry in enumerate(wave_panel._arrival_rows)
                                     if entry.time_ms == T0 + 30_000))
    assert jumps[-1]["position_ms"] == 50  # before the file: the window opens what holds it


def test_a_crossing_where_someone_came_on_earlier_is_marked_maybe_several(wave_panel, strip_landing, monkeypatch):
    """15:50 现代五项：前两人早 1.9 秒到线，落在那一刻只看到后两人；标“可能多人”，从前两人到线时看起，只是提示。"""
    from realtime import quick_review_panel as module
    _asked, jumps = strip_landing
    asked = []
    monkeypatch.setattr(module, "earlier_arrival", lambda pieces, cache, at_ms, others: asked.append(
        (at_ms, list(others))) or (at_ms - 1900 if at_ms == T0 + 20_000 else None))
    busy = wave_panel.add_manual_problem(dict(FRAME, frame_index=400, recorder_ms=T0 + 20_000),
                                         recorder_ms=T0 + 20_000)
    wave_panel.add_manual_problem(dict(FRAME, frame_index=600, recorder_ms=T0 + 30_000), recorder_ms=T0 + 30_000)
    wave_panel._read_earlier()  # the table's pass
    shown = {entry.time_ms: wave_panel.table.item(i, 3).text()
             for i, entry in enumerate(wave_panel._arrival_rows) if entry.event is None}
    assert shown == {T0 + 20_000: "可能多人", T0 + 30_000: "待定"}
    index = next(i for i, entry in enumerate(wave_panel._arrival_rows) if entry.item.item_id == busy.item_id)
    assert wave_panel._row_state(wave_panel._arrival_rows[index]) == "待定"  # a hint, nothing decided
    wave_panel._arrival_clicked(index)
    assert jumps[-1] == {"camera_index": 1, "position_ms": T0 + 18_100, "reference_only": True}
    assert "过线前 1.9 秒线上还有人到，已从那时放起" in wave_panel.hint.text()
    assert wave_panel.confirmation_status.text().startswith("可能多人 · ")
    # Read once per crossing; the other crossings are someone else's, not this one's people.
    assert [at for at, _others in asked].count(T0 + 20_000) == 1
    assert T0 + 30_000 in asked[0][1] and T0 + 20_000 not in asked[0][1]


def test_enter_goes_past_the_wave_s_own_extra_row_to_the_next_wave(wave_panel):
    """10-29：多 1 没处理时，回车曾在本波自己的“多出”行上打转。"""
    wave_panel._arrival_clicked(row(wave_panel, "61"))
    for x, offset in ((.2, 100), (.4, 500), (.6, 700), (.8, 1000)):
        count_on_picture(wave_panel, x, offset)
    assert any(entry.mark is not None for entry in wave_panel._arrival_rows)
    QTest.keyClick(wave_panel, Qt.Key_Return)
    assert wave_panel._selected_arrival().bib == "5"
    assert "多 1 人" in wave_panel.hint.text()


def test_a_lone_chip_also_says_what_is_left_to_look_at(wave_panel):
    wave_panel._arrival_clicked(row(wave_panel, "5"))
    count_on_picture(wave_panel, .5, 10_000)
    assert "秒没看" in wave_panel.confirmation_status.text()
    look_through(wave_panel, 9_400, 10_600)
    assert "秒没看" not in wave_panel.confirmation_status.text()


def test_not_a_rider_on_a_counted_chip_row_excludes_the_person_not_the_picture(wave_panel):
    wave_panel._arrival_clicked(row(wave_panel, "61"))
    count_on_picture(wave_panel, .2, 100)
    count_on_picture(wave_panel, .5, 100)
    assert saved(wave_panel) == {"61": 100, "20": 100}
    menu(wave_panel, "这人不是运动员", row(wave_panel, "20"))
    assert labels(wave_panel) == ["61", "非运动员"]
    assert saved(wave_panel) == {"61": 100}


# 古浪 10-28: one rider's passage came out as 7 rows of "—" within 1.1 s.
PIECES = (20_000, 20_300, 20_900, 21_200, 40_000)


@pytest.fixture
def fragment_panel(qapp, tmp_path):
    """Chips 61 and 5; video pieces with no chip at 20.0 / 20.3 / 20.9 / 21.2 s and 40 s."""
    directory = tmp_path / "finish_problems"
    directory.mkdir()
    items = [ProblemItem(f"piece-{offset}", KIND_NO_CHIP_CROSSING, CATEGORY_VIDEO, "",
                         chip_time_ms=T0 + offset,
                         evidence={"frame": dict(FRAME, recorder_ms=T0 + offset, position_ms=250 + offset,
                                                 frame_index=5 + offset // 50),
                                   "recorder_ms": T0 + offset}).to_payload()
             for offset in PIECES]
    (directory / "problem_list.json").write_text(json.dumps({"race_id": SCOPE[0], "stage_id": SCOPE[1],
                                                            "items": items}), encoding="utf-8")
    widget = QuickReviewPanel()
    widget.events = [event("61", "a", T0), event("5", "d", T0 + 60_000)]
    widget.frame = dict(FRAME, recorder_ms=T0 + 20_000, position_ms=20_250, frame_index=405)
    widget.set_events_provider(lambda: widget.events)
    widget.set_frame_provider(lambda: widget.frame)
    widget.set_workspace(tmp_path)
    widget.set_scope(*SCOPE)
    return widget


def video_rows(panel):
    return [(entry.time_ms - T0, [piece.time_ms - T0 for piece in entry.fragments])
            for entry in panel._arrival_rows if entry.event is None]


def test_pieces_of_one_passage_within_a_second_are_one_row_never_chained(fragment_panel):
    # 21.2 s is 0.3 s after 20.9 s but 1.2 s after the first piece: a row of its own.
    assert video_rows(fragment_panel) == [(20_000, [20_300, 20_900]), (21_200, []), (40_000, [])]
    assert [entry.bib for entry in fragment_panel._arrival_rows] == ["61", "", "", "", "5"]
    index = next(i for i, entry in enumerate(fragment_panel._arrival_rows) if entry.fragments)
    assert fragment_panel.table.item(index, 3).text() == "待定·3段"
    assert "拆成 3 段" in fragment_panel.table.item(index, 3).toolTip()


def test_a_folded_row_is_excluded_only_after_it_is_all_seen_and_comes_back_whole(fragment_panel):
    panel = fragment_panel
    index = next(i for i, entry in enumerate(panel._arrival_rows) if entry.fragments)
    panel._arrival_clicked(index)
    assert "还差" in panel.confirmation_status.text()
    menu(panel, "不是运动员")  # plays the rest first (nothing plays here)
    assert "正在补看" in panel.hint.text()
    assert not panel._arrival_store.active(SCOPE)  # nothing ruled on a span nobody saw
    QTest.keyClick(panel, Qt.Key_3)  # a second key stops it
    assert panel._auto_review is None

    look_through(panel, 18_000, 21_400)  # from 2 s before the first piece
    assert "已看完" in panel.confirmation_status.text()
    panel._arrival_clicked(index)
    menu(panel, "不是运动员")
    ruled = {entry["key"]: entry["action"] for entry in panel._arrival_store.active(SCOPE)}
    assert ruled == {f"candidate:piece-{offset}": "not_rider" for offset in (20_000, 20_300, 20_900)}
    assert video_rows(panel) == [(20_000, [20_300, 20_900]), (21_200, []), (40_000, [])]
    excluded = next(i for i, entry in enumerate(panel._arrival_rows) if entry.fragments)
    assert panel.table.item(excluded, 3).text() == "已排除·3段"

    panel._arrival_clicked(excluded)
    QTest.keyClick(panel, Qt.Key_Backspace)
    assert not panel._arrival_store.active(SCOPE)


def test_a_piece_judged_on_its_own_stays_a_row_of_its_own(fragment_panel):
    panel = fragment_panel
    piece = next(item for item in panel.items if item.item_id == "piece-20300")
    panel._arrival_store.save(SCOPE, "candidate:piece-20300", candidate_binding(piece), "not_rider",
                              candidate_id="piece-20300", frame=dict(FRAME))
    panel._quick_render_signature = None
    panel._render()
    assert video_rows(panel) == [(20_000, [20_900]), (20_300, []), (21_200, []), (40_000, [])]


def test_opening_a_folded_piece_opens_its_row(fragment_panel):
    panel = fragment_panel
    panel.select_item("piece-20900")
    selected = panel._selected_arrival()
    assert selected.time_ms - T0 == 20_000 and len(selected.fragments) == 2


class _Player:
    """Shows a frame, in chip time, wherever the panel jumps or steps to (40 ms a frame)."""

    def __init__(self, panel, at_ms=None):
        self.panel, self.at = panel, at_ms
        panel._review_frame = lambda chip_ms, _row: {"chip_ms": chip_ms}
        panel.jump_requested.connect(self.jump)
        panel.frame_step_requested.connect(self.step)

    def show(self):
        at = self.at
        QTimer.singleShot(1, lambda: self.panel.record_viewed_frame(dict(FRAME, recorder_ms=at), at))

    def jump(self, frame):
        if "chip_ms" in frame:
            self.at = frame["chip_ms"]
            self.show()

    def step(self, frames):
        self.at += 40 * frames
        self.show()


def _until_done(panel, timeout_ms=5000):
    for _ in range(timeout_ms // 10):
        if panel._auto_review is None:
            return
        QTest.qWait(10)
    raise AssertionError("auto review never finished")


def test_not_a_rider_on_a_folded_row_plays_the_rest_then_excludes_it_whole(fragment_panel):
    """10-28: a 3 refused for “还差 0.8 秒没看” looked like a dead key."""
    panel = fragment_panel
    player = _Player(panel)  # noqa: F841 - its slots are held weakly
    index = next(i for i, entry in enumerate(panel._arrival_rows) if entry.fragments)
    panel._arrival_clicked(index)
    menu(panel, "不是运动员")
    assert "正在补看" in panel.hint.text()
    assert not panel._arrival_store.active(SCOPE)  # nothing ruled before it is seen
    _until_done(panel)
    ruled = {entry["key"]: entry["action"] for entry in panel._arrival_store.active(SCOPE)}
    assert ruled == {f"candidate:piece-{offset}": "not_rider" for offset in (20_000, 20_300, 20_900)}
    assert not panel._fragment_unseen(next(entry for entry in panel._arrival_rows if entry.fragments), whole=True)

    QTest.keyClick(panel, Qt.Key_Backspace)
    assert not panel._arrival_store.active(SCOPE)


def test_any_key_stops_the_play_and_nothing_is_excluded(fragment_panel):
    panel = fragment_panel
    player = _Player(panel)  # noqa: F841 - its slots are held weakly
    index = next(i for i, entry in enumerate(panel._arrival_rows) if entry.fragments)
    panel._arrival_clicked(index)
    menu(panel, "不是运动员")
    QTest.keyClick(panel, Qt.Key_Down)
    assert panel._auto_review is None and "未排除" in panel.hint.text()
    QTest.qWait(300)
    assert not panel._arrival_store.active(SCOPE)


def test_the_play_stops_when_no_frame_comes(fragment_panel):
    panel = fragment_panel
    panel._review_frame = lambda chip_ms, _row: {"chip_ms": chip_ms}  # a jump nobody answers
    panel._review_timer.setInterval(100)
    index = next(i for i, entry in enumerate(panel._arrival_rows) if entry.fragments)
    panel._arrival_clicked(index)
    menu(panel, "不是运动员")
    _until_done(panel)
    assert "没有画面" in panel.hint.text()
    assert not panel._arrival_store.active(SCOPE)


def test_excluding_on_to_the_next_chip_takes_the_pictures_after_it_up_to_a_long_gap_and_undoes_at_once(fragment_panel):
    """Someone walking about at the line: 20 s (3 pieces) and 21.2 s go; 40 s is 18.8 s on and stays."""
    panel = fragment_panel
    player = _Player(panel)  # noqa: F841 - its slots are held weakly
    index = next(i for i, entry in enumerate(panel._arrival_rows) if entry.fragments)
    panel._arrival_clicked(index)
    menu(panel, "连续排除到下一个芯片")
    _until_done(panel)
    ruled = {entry["key"] for entry in panel._arrival_store.active(SCOPE)}
    assert ruled == {f"candidate:piece-{offset}" for offset in (20_000, 20_300, 20_900, 21_200)}
    assert "已排除 2 行" in panel.hint.text()

    QTest.keyClick(panel, Qt.Key_Backspace)
    assert not panel._arrival_store.active(SCOPE)


def test_a_chip_row_nobody_was_counted_for_offers_only_double_chip_and_a_click_just_counts(wave_panel):
    """10-29：左键只有一个意思（点人）；1 / 3 要先点中这个人，号码、双芯片按键或右键。"""
    wave_panel._arrival_clicked(row(wave_panel, "61"))
    assert menu_labels(wave_panel) == ["双芯片：这条芯片是谁带的…"]
    for key in (Qt.Key_1, Qt.Key_3):
        QTest.keyClick(wave_panel, key)
        assert "61 号还没点人" in wave_panel.hint.text() and "可以按：2 双芯片" in wave_panel.hint.text()
    QTest.keyClick(wave_panel, Qt.Key_4)
    assert "没有可撤销" in wave_panel.hint.text()
    assert not wave_panel._arrival_store.active(SCOPE)
    count_on_picture(wave_panel, .2, 100)
    assert saved(wave_panel) == {"61": 100}
    assert "已点" in wave_panel.hint.text()
    assert menu_labels(wave_panel, row(wave_panel, "61")) == [
        "这人没芯片：补录号码…", "这人不是运动员", "双芯片：这条芯片是谁带的…", "撤销：去掉这个人"]


def test_keys_1_2_3_4_do_on_the_selected_row_what_its_right_click_offers(wave_panel):
    """10-29：左手操作。1 不是运动员、2 双芯片、3 补录号码、4 撤销，作用在左边选中的那一行。"""
    wave_panel._ask_bib = lambda current: None  # the bib is not read yet
    wave_panel._arrival_clicked(row(wave_panel, "61"))
    count_on_picture(wave_panel, .2, 100)
    count_on_picture(wave_panel, .4, 500)
    assert saved(wave_panel) == {"61": 100, "20": 500}
    assert wave_panel._selected_arrival().bib == "20"  # the click picked out the person's row
    QTest.keyClick(wave_panel, Qt.Key_3)  # this one has no chip: a 疑似漏读 row of their own
    assert saved(wave_panel) == {"61": 100}
    miss = wave_panel._selected_arrival()
    assert miss.event is None and wave_panel._row_state(miss) == "疑似漏读"
    QTest.keyClick(wave_panel, Qt.Key_4)  # 4 takes it back, as Backspace does
    assert saved(wave_panel) == {"61": 100, "20": 500}

    wave_panel._ask_carrier = lambda bib, current: "61"
    wave_panel._arrival_clicked(row(wave_panel, "76"))
    QTest.keyClick(wave_panel, Qt.Key_2)
    assert wave_panel._arrival_rows[row(wave_panel, "76")].carrier == "61"
    assert "61号带的" == wave_panel.table.item(row(wave_panel, "76"), 3).text()
    QTest.keyClick(wave_panel, Qt.Key_1)  # nothing to exclude on a second chip
    assert "这一行不能按 1" in wave_panel.hint.text() and "2 双芯片、4 撤销" in wave_panel.hint.text()
    QTest.keyClick(wave_panel, Qt.Key_4)
    assert wave_panel._arrival_rows[row(wave_panel, "76")].carrier == ""

    wave_panel._arrival_clicked(row(wave_panel, "20"))
    QTest.keyClick(wave_panel, Qt.Key_1)  # the person counted for 20 is no rider
    assert saved(wave_panel) == {"61": 100}
    assert "非运动员" in wave_panel.hint.text()


def test_keys_on_a_crossing_without_a_chip(fragment_panel):
    """3 补录号码、1 不是运动员，和右键一样；自动补看照旧先看完再排除。"""
    panel = fragment_panel
    lone = next(i for i, entry in enumerate(panel._arrival_rows) if entry.event is None and not entry.fragments)
    panel._arrival_clicked(lone)
    panel._ask_bib = lambda current: "88"
    QTest.keyClick(panel, Qt.Key_3)
    entered = panel._arrival_rows[lone]
    assert panel._row_state(entered) == "补录" and entered.bib == "88"
    QTest.keyClick(panel, Qt.Key_4)  # the bib goes, the row stays 疑似漏读
    assert panel._row_state(panel._arrival_rows[lone]) == "疑似漏读"
    # 10-29 现场：画面不在这一行自己那一帧（提前看起、换到完整录像）时按 1，
    # 每按一次就新建一条“人工发现”并排除它，选中的行不动。按键只处理选中的行。
    preparing = QSignalSpy(panel.candidate_preparing)
    own = panel._arrival_rows[lone].item.evidence["frame"]
    provider, panel._frame_provider = panel._frame_provider, lambda: {}  # the picture is not this row's
    QTest.keyClick(panel, Qt.Key_1)
    assert panel._row_state(panel._arrival_rows[lone]) == "已排除"
    assert not preparing  # nothing on screen made into a 人工发现 row
    ruling = next(entry for entry in panel._arrival_store.active(SCOPE) if entry["action"] == "not_rider")
    assert (ruling["frame"]["segment_id"], ruling["frame"]["position_ms"]) == (own["segment_id"], own["position_ms"])
    panel._frame_provider = provider
    excluded = sum(entry["action"] == "not_rider" for entry in panel._arrival_store.active(SCOPE))
    folded = next(i for i, entry in enumerate(panel._arrival_rows) if entry.fragments)
    panel._arrival_clicked(folded)
    QTest.keyClick(panel, Qt.Key_1)  # plays the rest first, as the right click does
    assert "正在补看" in panel.hint.text()
    assert sum(entry["action"] == "not_rider" for entry in panel._arrival_store.active(SCOPE)) == excluded


def test_no_chip_from_the_menu_flags_the_row_s_person_and_takes_it_back(wave_panel):
    wave_panel._ask_bib = lambda current: None  # the bib is not read yet
    wave_panel._arrival_clicked(row(wave_panel, "61"))
    count_on_picture(wave_panel, .2, 100)
    count_on_picture(wave_panel, .4, 500)
    assert saved(wave_panel) == {"61": 100, "20": 500}
    menu(wave_panel, "这人没芯片：补录号码…", row(wave_panel, "20"))  # the second person has no chip
    assert saved(wave_panel) == {"61": 100}
    selected = wave_panel._selected_arrival()
    assert selected.event is None and wave_panel._row_state(selected) == "疑似漏读"
    assert selected.time_ms == T0 + 500
    menu(wave_panel, "撤回疑似漏读")  # changed mind: they are 20 after all
    assert saved(wave_panel) == {"61": 100, "20": 500}
    assert all(entry.event is not None for entry in wave_panel._arrival_rows)
    menu(wave_panel, "这人没芯片：补录号码…", row(wave_panel, "20"))
    QTest.keyClick(wave_panel, Qt.Key_Backspace)  # Backspace takes it back too
    assert saved(wave_panel) == {"61": 100, "20": 500}


def test_the_menu_acts_on_the_row_s_person_whatever_frame_is_shown(wave_panel):
    wave_panel._ask_bib = lambda current: None
    wave_panel._arrival_clicked(row(wave_panel, "61"))
    count_on_picture(wave_panel, .2, 100)
    wave_panel.frame = dict(FRAME, frame_index=40, position_ms=2000, recorder_ms=T0 + 1750)
    menu(wave_panel, "这人没芯片：补录号码…", row(wave_panel, "61"))
    assert saved(wave_panel) == {}
    miss = next(entry for entry in wave_panel._arrival_rows if entry.event is None)
    assert wave_panel._row_state(miss) == "疑似漏读" and miss.time_ms == T0 + 100


def test_a_bib_on_a_picture_without_a_chip_lists_it_as_a_suspected_miss(fragment_panel):
    panel = fragment_panel
    panel._ask_bib = lambda current: None
    index = next(i for i, entry in enumerate(panel._arrival_rows) if entry.fragments)
    panel._arrival_clicked(index)
    assert menu_labels(panel) == ["补录号码…", "不是运动员", "连续排除到下一个芯片"]
    menu(panel, "补录号码…")
    ruled = {entry["key"]: (entry["action"], entry.get("no_chip")) for entry in panel._arrival_store.active(SCOPE)}
    assert ruled == {f"candidate:piece-{offset}": ("bicycle", True) for offset in (20_000, 20_300, 20_900)}
    folded = next(entry for entry in panel._arrival_rows if entry.fragments)
    assert panel._row_state(folded) == "疑似漏读"
    assert menu_labels(panel) == ["补录号码…", "不是运动员", "撤回疑似漏读"]

    QTest.keyClick(panel, Qt.Key_Backspace)
    assert not panel._arrival_store.active(SCOPE)


def test_a_chip_imported_late_takes_its_place_in_the_wave_by_chip_time(wave_panel):
    """古浪 6 号：芯片后补进来，排在整波最后，他的那一下给了后面的号码。"""
    wave_panel.events.append(event("6", "e", T0 + 600))
    wave_panel.refresh()
    assert [entry.bib for entry in wave_panel._arrival_rows] == ["61", "20", "6", "76", "5"]
    wave_panel._arrival_clicked(row(wave_panel, "61"))
    for x, offset in ((.2, 100), (.4, 500), (.5, 650), (.6, 1000)):
        count_on_picture(wave_panel, x, offset)
    assert saved(wave_panel) == {"61": 100, "20": 500, "6": 650, "76": 1000}


def test_a_person_flagged_without_a_chip_cannot_be_counted_again(wave_panel):
    wave_panel._arrival_clicked(row(wave_panel, "61"))
    frame = count_on_picture(wave_panel, .2, 100)
    wave_panel._ask_bib = lambda current: None
    menu(wave_panel, "这人没芯片：补录号码…", row(wave_panel, "61"))
    wave_panel.add_mark(.205, .5, frame["recorder_ms"], frame)
    assert "已点过" in wave_panel.hint.text()
    assert saved(wave_panel) == {}


def test_a_detected_crossing_at_a_counted_moment_stays_a_row_shown_as_near_someone_counted(wave_panel):
    """Review 10-29: hidden, it might be someone nobody counted; it is shown, 已点附近 (疑点保留)."""
    wave_panel._arrival_clicked(row(wave_panel, "61"))
    count_on_picture(wave_panel, .2, 100)
    detected = wave_panel.add_manual_problem(dict(FRAME, frame_index=9, recorder_ms=T0 + 300), recorder_ms=T0 + 300)
    far = wave_panel.add_manual_problem(dict(FRAME, frame_index=60, recorder_ms=T0 + 3000), recorder_ms=T0 + 3000)
    rows = {entry.item.item_id: index for index, entry in enumerate(wave_panel._arrival_rows)}
    assert detected.item_id in rows and far.item_id in rows
    assert wave_panel.table.item(rows[detected.item_id], 3).text() == "已点附近"
    assert wave_panel.table.item(rows[far.item_id], 3).text() == "待定"
    assert "多半就是那个人" in wave_panel.table.item(rows[detected.item_id], 3).toolTip()
    assert "待定" in wave_panel.attention_button.toolTip()  # still to look at
    wave_panel._arrival_clicked(rows[detected.item_id])
    assert "按 1 排除" in wave_panel.hint.text()
    assert menu_labels(wave_panel, rows[detected.item_id])[:2] == ["补录号码…", "不是运动员"]


def counted_short(wave_panel):
    """61, 20, 76 within 0.9 s; only two people crossed, looked through from start to end."""
    wave_panel._arrival_clicked(row(wave_panel, "61"))
    count_on_picture(wave_panel, .2, 100)
    count_on_picture(wave_panel, .4, 500)
    look_through(wave_panel, -600, 1500)
    wave_panel._quick_render_signature = None
    wave_panel._render()


def test_a_chip_left_without_anyone_after_the_whole_wave_is_a_suspected_double_chip(wave_panel):
    wave_panel._arrival_clicked(row(wave_panel, "61"))
    count_on_picture(wave_panel, .2, 100)
    count_on_picture(wave_panel, .4, 500)
    assert wave_panel.table.item(row(wave_panel, "76"), 3).text() == "待定"  # not all seen yet
    look_through(wave_panel, -600, 1500)
    assert wave_panel.table.item(row(wave_panel, "76"), 3).text() == "疑似双芯片"


def test_double_chip_records_who_carries_it_and_the_person_moves_on_to_the_next_chip(wave_panel, tmp_path):
    """10-29：“2 号带的 8 号芯片，直接点 8 号右键双芯片”，并记下是谁带的。"""
    counted_short(wave_panel)
    wave_panel._ask_carrier = lambda bib, current: "76"  # the second rider wore 20 and 76: 20 is the extra one
    menu(wave_panel, "双芯片：这条芯片是谁带的…", row(wave_panel, "20"))
    assert wave_panel.table.item(row(wave_panel, "20"), 3).text() == "76号带的"
    assert wave_panel._arrival_rows[row(wave_panel, "20")].carrier == "76"
    assert "76 号另带 20 号芯片" in wave_panel.table.item(row(wave_panel, "76"), 3).toolTip()
    assert saved(wave_panel) == {"61": 100, "76": 500}
    status = wave_panel._wave_status(wave_panel._wave_plan(wave_panel._counted_plan(
        wave_panel._arrival_rows[row(wave_panel, "61")]).item))[0]
    assert "双芯片 1" in status and "已核" in status
    assert menu_labels(wave_panel, row(wave_panel, "20")) == ["改携带人…", "撤回双芯片"]
    restored = QuickReviewPanel()
    restored.set_events_provider(lambda: wave_panel.events)
    restored.set_workspace(tmp_path)
    restored.set_scope(*SCOPE)
    assert restored.table.item(row(restored, "20"), 3).text() == "76号带的"

    wave_panel._ask_carrier = lambda bib, current: "61"
    menu(wave_panel, "改携带人…", row(wave_panel, "20"))
    assert wave_panel._arrival_rows[row(wave_panel, "20")].carrier == "61"
    wave_panel._arrival_clicked(row(wave_panel, "20"))
    QTest.keyClick(wave_panel, Qt.Key_Backspace)  # or 撤回双芯片
    assert saved(wave_panel) == {"61": 100, "20": 500}
    assert wave_panel.table.item(row(wave_panel, "76"), 3).text() == "疑似双芯片"


def test_double_chip_needs_the_carrier_bib_and_warns_when_not_in_the_wave(wave_panel):
    wave_panel._arrival_clicked(row(wave_panel, "76"))
    for answer, said in ((None, None), ("  ", "没有填携带人"), ("76", "携带人不能是 76 号自己")):
        wave_panel._ask_carrier = lambda bib, current, answer=answer: answer
        menu(wave_panel, "双芯片：这条芯片是谁带的…")
        assert not wave_panel._arrival_store.active(SCOPE)
        if said:
            assert said in wave_panel.hint.text()
    wave_panel._ask_carrier = lambda bib, current: "99"  # no picture needed, but 99 has no chip here
    menu(wave_panel, "双芯片：这条芯片是谁带的…")
    assert wave_panel._arrival_rows[row(wave_panel, "76")].carrier == "99"
    assert "99 号不在这一波的芯片里" in wave_panel.hint.text()


def no_chip_row(wave_panel):
    """A fourth person on the wave without a chip, opened on their own row."""
    wave_panel._arrival_clicked(row(wave_panel, "61"))
    for x, offset in ((.2, 100), (.4, 500), (.6, 700), (.8, 1000)):
        count_on_picture(wave_panel, x, offset)
    wave_panel._ask_bib = lambda current: None
    menu(wave_panel, "补录号码…", next(i for i, entry in enumerate(wave_panel._arrival_rows)
                                    if entry.mark is not None))  # the 多出 one has no chip
    index = next(i for i, entry in enumerate(wave_panel._arrival_rows) if entry.event is None)
    wave_panel._arrival_clicked(index)
    assert wave_panel._wave_plan() is None
    return index


def test_enter_on_a_suspected_miss_takes_the_bib_read_off_the_frame(wave_panel):
    index = no_chip_row(wave_panel)
    wave_panel._ask_bib = lambda current: "88"
    QTest.keyClick(wave_panel, Qt.Key_Return)
    entry = wave_panel._arrival_rows[index]
    assert entry.bib == "88" and wave_panel.table.item(index, 3).text() == "补录"
    assert saved(wave_panel) == {"61": 100, "20": 500, "76": 700}  # chips untouched
    QTest.keyClick(wave_panel, Qt.Key_Backspace)
    assert wave_panel._row_state(wave_panel._arrival_rows[index]) == "疑似漏读"


def test_a_bib_with_a_chip_close_by_or_already_entered_is_refused(wave_panel):
    index = no_chip_row(wave_panel)
    wave_panel._ask_bib = lambda current: "76"
    QTest.keyClick(wave_panel, Qt.Key_Return)
    assert "有芯片，不是漏读" in wave_panel.hint.text()
    assert wave_panel._row_state(wave_panel._arrival_rows[index]) == "疑似漏读"

    wave_panel._ask_bib = lambda current: "88"
    QTest.keyClick(wave_panel, Qt.Key_Return)
    other = wave_panel.add_manual_problem(dict(FRAME, frame_index=400, recorder_ms=T0 + 20_000),
                                          recorder_ms=T0 + 20_000)
    position = next(i for i, entry in enumerate(wave_panel._arrival_rows) if entry.item.item_id == other.item_id)
    wave_panel._arrival_clicked(position)
    menu(wave_panel, "补录号码…")  # a picture without a chip: 疑似漏读, and its bib asked at once
    assert "补录过" in wave_panel.hint.text()


def test_only_what_is_left_to_handle_is_shown_on_request(wave_panel):
    counted_short(wave_panel)
    no_chip = wave_panel.add_manual_problem(dict(FRAME, frame_index=400, recorder_ms=T0 + 20_000),
                                            recorder_ms=T0 + 20_000)
    position = next(i for i, entry in enumerate(wave_panel._arrival_rows) if entry.item.item_id == no_chip.item_id)
    wave_panel._arrival_clicked(position)
    wave_panel._ask_bib = lambda current: None
    menu(wave_panel, "补录号码…")
    assert wave_panel.attention_button.text() == "只看要处理的 2"
    wave_panel.attention_button.setChecked(True)
    shown = [wave_panel._row_state(entry) for i, entry in enumerate(wave_panel._arrival_rows)
             if not wave_panel.table.isRowHidden(i)]
    assert shown == ["疑似双芯片", "疑似漏读"]
    assert not wave_panel.table.isRowHidden(wave_panel.table.currentRow())
    wave_panel._arrival_clicked(row(wave_panel, "76"))
    wave_panel._step(1)
    assert wave_panel._row_state(wave_panel._selected_arrival()) == "疑似漏读"
    wave_panel.attention_button.setChecked(False)
    assert not any(wave_panel.table.isRowHidden(i) for i in range(wave_panel.table.rowCount()))


def test_export_lists_the_bibs_entered_and_the_second_chips(wave_panel, tmp_path):
    import csv
    from realtime.problem_resolutions import EXPORT_DELETIONS, EXPORT_MANUAL_ENTRIES
    counted_short(wave_panel)
    wave_panel._ask_carrier = lambda bib, current: "20"
    menu(wave_panel, "双芯片：这条芯片是谁带的…", row(wave_panel, "76"))
    lone = wave_panel.add_manual_problem(dict(FRAME, frame_index=400, recorder_ms=T0 + 20_000),
                                         recorder_ms=T0 + 20_000)
    position = next(i for i, entry in enumerate(wave_panel._arrival_rows) if entry.item.item_id == lone.item_id)
    wave_panel._arrival_clicked(position)
    wave_panel._ask_bib = lambda current: "88"
    menu(wave_panel, "补录号码…")
    assert wave_panel.export() is not None
    exports = tmp_path / "finish_problems" / "exports"

    def read(name):
        with (exports / name).open(encoding="utf-8-sig") as handle:
            return list(csv.reader(handle))[1:]

    assert [entry[0] for entry in read(EXPORT_MANUAL_ENTRIES)] == ["88"]
    deletions = read(EXPORT_DELETIONS)
    assert [(entry[0], entry[5], entry[11]) for entry in deletions] == [("76", "c", "20")]
    assert "76 号芯片由 20 号携带" in " ".join(deletions[0])
    assert "已导出" in wave_panel.hint.text()


def test_one_person_flagged_twice_by_an_older_build_is_one_row(wave_panel):
    wave_panel._arrival_clicked(row(wave_panel, "61"))
    frame = count_on_picture(wave_panel, .2, 100)
    wave_panel._ask_bib = lambda current: None
    menu(wave_panel, "这人没芯片：补录号码…", row(wave_panel, "61"))
    parent = wave_panel._wave_candidate(wave_panel._waves[0])
    # What an older build allowed: the same rider clicked again and flagged again.
    wave_panel._split_wave_people(parent, [dict(x=.21, y=.51, recorder_ms=frame["recorder_ms"], frame=frame)],
                                  "older")
    assert sum(entry.event is None for entry in wave_panel._arrival_rows) == 1
    assert wave_panel._wave_plan(parent).split == 1
