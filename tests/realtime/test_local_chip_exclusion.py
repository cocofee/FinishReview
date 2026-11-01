"""Local test-chip exclusion never changes official events or suggestion exports."""

from dataclasses import replace

import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QInputDialog

from realtime.quick_review_panel import LOCAL_CHIP_EXCLUSION, QuickReviewPanel
from tests.realtime.test_quick_review_panel import (
    auto_people, count_on_picture, menu_labels, qapp, row, wave_panel,
)


def exclude(panel, monkeypatch, bib="5"):
    panel._arrival_clicked(row(panel, bib))
    monkeypatch.setattr(QInputDialog, "getText", lambda *_args, **_kwargs: pytest.fail("must not ask for input"))
    QTest.keyClick(panel, Qt.Key_1)


def test_unmatched_test_chip_excludes_without_frame_then_restarts_and_undoes(wave_panel, monkeypatch, tmp_path):
    wave_panel.events = [replace(event, bib="19") if event.bib == "5" else event for event in wave_panel.events]
    wave_panel.refresh()
    original = tuple(wave_panel.events)
    wave_panel.set_frame_provider(lambda: None)
    exclude(wave_panel, monkeypatch, "19")
    entry = wave_panel._arrival_store.latest(wave_panel._arrival_scope, wave_panel._selected_key)
    assert entry["reason"] == LOCAL_CHIP_EXCLUSION
    assert entry["exclusion_reason"] == "非运动员（本机排除）"
    assert not entry.get("frame") and not entry.get("candidate_id")
    target = wave_panel._arrival_rows[row(wave_panel, "19")]
    assert wave_panel._row_state(target) == "已排除" and target.event.is_active
    assert target.key not in wave_panel._wave_of
    assert wave_panel._row_wave(target) is None
    assert all(bib != "19" for _stamp, bib, _source in wave_panel.race_strip.view.chips)
    assert menu_labels(wave_panel) == ["撤销本机排除"]
    assert "先按 4" in wave_panel.picture_candidate(dict(wave_panel.frame))[1]
    assert wave_panel._arrival_exports() == ([], [], 0)
    assert not wave_panel.current_marks()
    assert tuple(wave_panel.events) == original

    restored = QuickReviewPanel()
    restored.set_events_provider(lambda: wave_panel.events)
    restored.set_frame_provider(lambda: None)
    restored.set_workspace(tmp_path)
    restored.set_scope(*wave_panel._arrival_scope)
    restored._arrival_clicked(row(restored, "19"))
    assert restored._row_state(restored._selected_arrival()) == "已排除"
    QTest.keyClick(restored, Qt.Key_4)
    assert restored._row_state(restored._selected_arrival()) == "待定"
    assert "本机排除" in restored.hint.text()
    assert restored._selected_arrival().key in restored._wave_of
    assert restored._arrival_exports() == ([], [], 0)
    assert not restored._arrival_store.active(restored._arrival_scope)
    assert tuple(wave_panel.events) == original
    restored.close()


def test_right_menu_directly_excludes_without_input_and_four_undoes(wave_panel, monkeypatch):
    wave_panel._arrival_clicked(row(wave_panel, "5"))
    monkeypatch.setattr(QInputDialog, "getText", lambda *_args, **_kwargs: pytest.fail("must not ask for input"))
    actions = dict(wave_panel._row_actions(wave_panel._selected_arrival()))
    actions["非运动员（本机排除）"]()
    assert wave_panel._row_state(wave_panel._selected_arrival()) == "已排除"
    QTest.keyClick(wave_panel, Qt.Key_4)
    assert wave_panel._row_state(wave_panel._selected_arrival()) == "待定"


def test_exclusion_is_bound_to_one_revision_and_one_scope(wave_panel, monkeypatch):
    exclude(wave_panel, monkeypatch)
    wave_panel.events = [replace(event, revision=event.revision + 1) if event.bib == "5" else event
                         for event in wave_panel.events]
    wave_panel.refresh()
    assert wave_panel._row_state(wave_panel._arrival_rows[row(wave_panel, "5")]) == "待定"
    assert wave_panel._arrival_exports() == ([], [], 0)
    assert not wave_panel._arrival_store.active(("another-race", wave_panel._arrival_scope[1]))


def test_actual_csv_exports_do_not_gain_local_exclusion_rows(wave_panel, monkeypatch):
    before = wave_panel.export()
    assert before is not None
    exclude(wave_panel, monkeypatch)
    after = wave_panel.export()
    assert after == before
    path = wave_panel.export_arrivals()
    import csv
    with path.open(encoding="utf-8-sig", newline="") as stream:
        assert len(list(csv.reader(stream))) == 1  # no invented manual crossing


def test_same_bib_other_lap_is_not_excluded(wave_panel, monkeypatch):
    original = next(event for event in wave_panel.events if event.bib == "5")
    exclude(wave_panel, monkeypatch)
    wave_panel.events.append(replace(original, event_id="other-lap-5", lap=2,
                                     passage_timestamp_ms=original.timeline_timestamp_ms + 60_000))
    wave_panel.refresh()
    same_bib = [entry for entry in wave_panel._arrival_rows if entry.bib == "5"]
    assert len(same_bib) == 2
    assert {entry.event.event_id: wave_panel._row_state(entry) for entry in same_bib} == {
        original.event_id: "已排除", "other-lap-5": "待定"}


def test_refresh_scope_change_does_not_write_to_another_race(wave_panel, monkeypatch):
    wave_panel._arrival_clicked(row(wave_panel, "5"))
    store, scope = wave_panel._arrival_store, wave_panel._arrival_scope

    render = wave_panel._render

    def update():
        monkeypatch.setattr(wave_panel, "_render", render)
        wave_panel.set_scope("other-race", "stage")
        render()

    monkeypatch.setattr(wave_panel, "_render", update)
    QTest.keyClick(wave_panel, Qt.Key_1)
    assert "已变化" in wave_panel.hint.text()
    assert not store.active(scope)
    assert not store.active(("other-race", "stage"))


def test_refresh_upstream_change_does_not_exclude_the_changed_record(wave_panel, monkeypatch):
    wave_panel._arrival_clicked(row(wave_panel, "5"))
    render = wave_panel._render

    def update():
        wave_panel.events = [replace(event, revision=event.revision + 1) if event.bib == "5" else event
                             for event in wave_panel.events]
        render()

    monkeypatch.setattr(wave_panel, "_render", update)
    QTest.keyClick(wave_panel, Qt.Key_1)
    assert "已变化" in wave_panel.hint.text()
    assert not wave_panel._arrival_store.active(wave_panel._arrival_scope)


def test_local_exclusion_never_excludes_other_members_of_the_wave(wave_panel, monkeypatch):
    exclude(wave_panel, monkeypatch, "20")
    assert wave_panel._row_state(wave_panel._arrival_rows[row(wave_panel, "20")]) == "已排除"
    assert all(wave_panel._row_state(wave_panel._arrival_rows[row(wave_panel, bib)]) == "待定"
               for bib in ("61", "76", "5"))
    assert not wave_panel._arrival_exports()[1]
    QTest.keyClick(wave_panel, Qt.Key_4)
    assert not wave_panel._arrival_store.active(wave_panel._arrival_scope)


def test_an_automatic_proposal_can_be_locally_excluded_without_accepting(wave_panel, monkeypatch):
    auto_people(wave_panel, monkeypatch, (100, 500, 1000))
    exclude(wave_panel, monkeypatch, "20")
    target = wave_panel._arrival_rows[row(wave_panel, "20")]
    assert wave_panel._row_state(target) == "已排除"
    assert target.key not in wave_panel._auto_labels
    assert not any(entry.decision for entry in wave_panel._arrival_rows)
    assert not wave_panel.current_marks()
    assert wave_panel._arrival_exports() == ([], [], 0)


def test_excluding_uncounted_chip_keeps_neighbours_saved_identity(wave_panel, monkeypatch):
    wave_panel._arrival_clicked(row(wave_panel, "61"))
    count_on_picture(wave_panel, .2, 100)
    saved = wave_panel._arrival_rows[row(wave_panel, "61")].decision
    exclude(wave_panel, monkeypatch, "76")
    assert wave_panel._arrival_rows[row(wave_panel, "61")].decision == saved
    target = wave_panel._arrival_rows[row(wave_panel, "76")]
    assert wave_panel._attention(target, {0}, {}) is None
    assert "非运动员（本机排除）" in wave_panel.table.item(row(wave_panel, "76"), 3).toolTip()
    QTest.keyClick(wave_panel, Qt.Key_4)
    assert wave_panel._arrival_rows[row(wave_panel, "61")].decision == saved


def test_write_failure_leaves_test_chip_pending(wave_panel, monkeypatch):
    def fail(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(wave_panel._arrival_store, "save", fail)
    exclude(wave_panel, monkeypatch)
    assert "未保存" in wave_panel.hint.text()
    assert wave_panel._row_state(wave_panel._selected_arrival()) == "待定"
    assert not wave_panel._arrival_store.active(wave_panel._arrival_scope)


def test_existing_person_must_be_undone_before_chip_exclusion(wave_panel, monkeypatch):
    wave_panel._arrival_clicked(row(wave_panel, "61"))
    count_on_picture(wave_panel, .2, 100)
    key = wave_panel._selected_key
    before = wave_panel._arrival_store.active(wave_panel._arrival_scope)
    monkeypatch.setattr(QInputDialog, "getText", lambda *_args: pytest.fail("must not ask to exclude"))
    wave_panel._exclude_local_chip(key)
    assert "先按 4" in wave_panel.hint.text()
    assert wave_panel._arrival_store.active(wave_panel._arrival_scope) == before
