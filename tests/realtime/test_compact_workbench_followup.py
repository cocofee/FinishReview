"""Keep actionable warnings and undo while removing routine workbench clutter."""

from dataclasses import replace

from PyQt5.QtCore import Qt
from PyQt5.QtTest import QTest

from realtime.quick_review_panel import QuickReviewPanel
from tests.realtime.test_quick_review_panel import (  # noqa: F401
    FRAME, count_on_picture, panel, qapp, row, three_laps, wave_panel,
)


def test_finish_line_menu_and_missing_line_button_use_same_entry(panel, monkeypatch):
    calls = []
    # Both paths must emit the existing preparation signal and open the same editor.
    monkeypatch.setattr(type(panel).__mro__[1], "_configure_sampling", lambda self: calls.append(self))
    action = next(action for action in panel.more_button.menu().actions() if action.text() == "终点线 / 模型")
    panel.set_finish_line_status(True)
    assert panel.finish_line_button.isHidden()
    action.trigger()
    panel.set_finish_line_status(False)
    panel.finish_line_button.click()
    assert calls == [panel, panel]
    assert panel.finish_line_status.isHidden()  # Do not repeat the button in a separate label.


def test_unknown_laps_warning_is_short_and_resolves_only_with_target(wave_panel):
    assert wave_panel.lap_status.text() == "圈数未知 · 显示全部圈次"
    assert "未收到" in wave_panel.lap_status.toolTip()
    three_laps(wave_panel)
    wave_panel.refresh()
    assert wave_panel.lap_status.isHidden()
    assert not wave_panel.lap_status.text()


def test_partial_group_metadata_keeps_unknown_laps_warning(wave_panel):
    three_laps(wave_panel)
    wave_panel.events[0] = replace(wave_panel.events[0], group_id="no-target")
    wave_panel.refresh()
    assert wave_panel.lap_status.text() == "圈数未知 · 显示全部圈次"


def test_exclusion_advances_then_undo_preserves_next_counted_rider(wave_panel):
    wave_panel._arrival_clicked(row(wave_panel, "61"))
    count_on_picture(wave_panel, .2, 100)
    saved = wave_panel._arrival_rows[row(wave_panel, "61")].decision
    # Exclude the last row: fallback to a preceding row, then ensure 4 still
    # undoes this exclusion rather than a pre-existing decision on that row.
    wave_panel._arrival_clicked(row(wave_panel, "5"))
    QTest.keyClick(wave_panel, Qt.Key_1)
    assert wave_panel.table.isRowHidden(row(wave_panel, "5"))
    wave_panel._select_key(wave_panel._arrival_rows[row(wave_panel, "61")].key)
    QTest.keyClick(wave_panel, Qt.Key_4)
    assert not wave_panel.table.isRowHidden(row(wave_panel, "5"))
    assert wave_panel._arrival_rows[row(wave_panel, "61")].decision == saved


def test_exclusion_undo_precedes_automatically_selected_counted_person(wave_panel):
    wave_panel.events = [value for value in wave_panel.events if value.bib in ("61", "20")]
    wave_panel.refresh()
    wave_panel._arrival_clicked(row(wave_panel, "61"))
    count_on_picture(wave_panel, .2, 100)
    saved = wave_panel._arrival_rows[row(wave_panel, "61")].decision
    wave_panel._arrival_clicked(row(wave_panel, "20"))
    QTest.keyClick(wave_panel, Qt.Key_1)
    assert wave_panel._selected_arrival().bib == "61"
    QTest.keyClick(wave_panel, Qt.Key_4)
    assert wave_panel._arrival_rows[row(wave_panel, "61")].decision == saved
    assert not wave_panel.table.isRowHidden(row(wave_panel, "20"))


def test_video_candidate_exclusion_is_hidden_and_undoes_after_last_row(panel):
    walker = panel.add_manual_problem(dict(FRAME, recorder_ms=FRAME["recorder_ms"] + 5000, frame_index=105),
                                      recorder_ms=FRAME["recorder_ms"] + 5000)
    panel.select_item(walker.item_id)
    dict(panel._row_actions(panel._selected_arrival()))["不是运动员"]()
    index = next(i for i, value in enumerate(panel._arrival_rows) if value.item.item_id == walker.item_id)
    assert panel.table.isRowHidden(index)
    assert panel.current_item().item_id != walker.item_id
    QTest.keyClick(panel, Qt.Key_4)
    assert not panel.table.isRowHidden(index)
    assert panel.current_item().item_id == walker.item_id


def test_display_switch_keeps_records_export_and_immediate_undo(wave_panel):
    original = tuple(wave_panel.events)
    wave_panel._arrival_clicked(row(wave_panel, "20"))
    QTest.keyClick(wave_panel, Qt.Key_1)
    assert wave_panel._selected_arrival().bib == "76"
    key = wave_panel._last_saved_key
    assert wave_panel.table.isRowHidden(row(wave_panel, "20"))
    exports = wave_panel._arrival_exports()
    wave_panel.show_excluded_action.setChecked(True)
    assert not wave_panel.table.isRowHidden(row(wave_panel, "20"))
    wave_panel.show_excluded_action.setChecked(False)
    assert wave_panel.table.isRowHidden(row(wave_panel, "20"))
    assert wave_panel._last_saved_key == key
    assert wave_panel._arrival_exports() == exports
    assert tuple(wave_panel.events) == original
    QTest.keyClick(wave_panel, Qt.Key_4)
    assert wave_panel._selected_arrival().bib == "20"
    assert not wave_panel.table.isRowHidden(row(wave_panel, "20"))


def test_search_reveals_excluded_but_does_not_change_judgement(wave_panel):
    wave_panel._arrival_clicked(row(wave_panel, "20"))
    QTest.keyClick(wave_panel, Qt.Key_1)
    before = wave_panel._arrival_store.active(wave_panel._arrival_scope)
    wave_panel.search.setText("20")
    wave_panel._find()
    assert wave_panel.show_excluded_action.isChecked()
    assert wave_panel._selected_arrival().bib == "20"
    assert not wave_panel.table.isRowHidden(row(wave_panel, "20"))
    assert wave_panel._arrival_store.active(wave_panel._arrival_scope) == before


def test_last_remaining_row_exclusion_has_no_hidden_selection(panel):
    for value in tuple(panel._arrival_rows):
        panel._arrival_clicked(row(panel, value.bib))
        QTest.keyClick(panel, Qt.Key_1)
    assert panel.table.currentRow() == -1
    assert panel._selected_arrival() is None
    assert all(panel.table.isRowHidden(i) for i in range(panel.table.rowCount()))
    QTest.keyClick(panel, Qt.Key_4)
    assert panel._selected_arrival() is not None
    assert not panel.table.isRowHidden(panel.table.currentRow())


def test_only_attention_and_show_excluded_have_independent_visibility(wave_panel):
    wave_panel._arrival_clicked(row(wave_panel, "20"))
    QTest.keyClick(wave_panel, Qt.Key_1)
    wave_panel.attention_button.setChecked(True)
    assert wave_panel.table.isRowHidden(row(wave_panel, "20"))
    wave_panel.show_excluded_action.setChecked(True)
    assert not wave_panel.table.isRowHidden(row(wave_panel, "20"))
    wave_panel.show_excluded_action.setChecked(False)
    assert wave_panel.table.isRowHidden(row(wave_panel, "20"))


def test_all_excluded_on_restart_selects_no_hidden_row(panel, tmp_path):
    for value in tuple(panel._arrival_rows):
        panel._arrival_clicked(row(panel, value.bib))
        QTest.keyClick(panel, Qt.Key_1)
    restored = QuickReviewPanel()
    restored.set_events_provider(lambda: panel.events)
    restored.set_workspace(tmp_path)
    restored.set_scope(*panel._arrival_scope)
    assert restored.table.currentRow() == -1
    assert restored._selected_arrival() is None
    restored.show_excluded_action.setChecked(True)
    assert restored._selected_arrival() is not None
    assert not restored.table.isRowHidden(restored.table.currentRow())
    restored.close()


def test_manually_counted_rows_remain_visible(wave_panel):
    wave_panel._arrival_clicked(row(wave_panel, "61"))
    count_on_picture(wave_panel, .2, 100)
    assert wave_panel._arrival_rows[row(wave_panel, "61")].decision is not None
    assert not wave_panel.table.isRowHidden(row(wave_panel, "61"))
    assert "灰蓝虚线" not in wave_panel.quick_title.text()
    assert "灰蓝虚线" in wave_panel.quick_title.toolTip()
