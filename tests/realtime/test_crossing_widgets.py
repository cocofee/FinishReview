"""The compact crossing controls and the card rail under the evidence picture."""

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import cv2
import numpy as np
import pytest
from PyQt5.QtCore import QPoint, Qt
from PyQt5.QtGui import QColor, QPixmap
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QHBoxLayout, QListView, QPushButton

from realtime.chip_history import format_chip_time
from realtime.crossing_widgets import (
    CONFLICT_PLACEHOLDER,
    EXCLUSION_REASONS,
    MEMBER_PLACEHOLDER,
    RAIL_ICON_SIZE,
    RAIL_LIST_HEIGHT,
    CrossingCardRail,
    CrossingReviewControls,
)
from realtime.problem_items import (
    CATEGORY_VIDEO,
    KIND_BATCH_READS,
    KIND_NEAR_CROSSING,
    KIND_NO_CHIP_CROSSING,
    KIND_SYNC,
    KIND_TWO_CHIPS_ONE_CROSSING,
    STATUS_OPEN,
    STATUS_WAITING,
    ProblemItem,
)


T = 1_789_784_000_000
FRAME = {"camera_index": 1, "segment_id": "seg", "video_path": "videos/a.mkv", "frame_index": 812,
         "position_ms": 40_600, "session_id": "s", "calibration_version": 2}


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


def _mark(recorder_ms, bib="", reason="", x=0.5, y=0.5):
    return {"recorder_ms": recorder_ms, "x": x, "y": y, "bib": bib, "exclusion_reason": reason}


def _member(event_id, bib, chip_ms, state=None):
    member = {"event_id": event_id, "bib": bib, "chip_ms": chip_ms}
    if state is not None:
        member["state"] = state
    return member


def _watch(controls):
    seen = {name: [] for name in ("mark_selected", "bib_assigned", "exclusion_changed", "mark_removed",
                                 "member_decision", "conflict_selected", "conflict_decided",
                                 "fully_viewed_changed")}

    def recorder(name):
        # PyQt only passes as many arguments as the slot takes, so list.append
        # would silently drop the reason and the event id; keep every argument.
        def record(*args):
            seen[name].append(args[0] if len(args) == 1 else args)
        return record

    for name in seen:
        getattr(controls, name).connect(recorder(name))
    return seen


def _item(item_id, *, kind=KIND_NO_CHIP_CROSSING, chip_ms=None, frame=None, evidence=None,
          status=STATUS_OPEN, bibs=()):
    payload = dict(evidence or {})
    if frame is not None:
        payload["frame"] = dict(frame)
    return ProblemItem(item_id, kind, CATEGORY_VIDEO, f"{item_id} 的说明", chip_time_ms=chip_ms,
                       bibs=tuple(bibs), status=status, evidence=payload)


def _write_card(directory: Path, relative: str, colour):
    path = directory / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, data = cv2.imencode(".jpg", np.full((80, 100, 3), colour, np.uint8))
    assert ok
    path.write_bytes(data.tobytes())
    return path


# ----- CrossingReviewControls -----------------------------------------

def test_a_review_paints_counts_and_names_without_deciding_anything(qapp, tmp_path):
    controls = CrossingReviewControls()
    controls.show()
    seen = _watch(controls)
    marks = [_mark(T, "057"), _mark(T + 1_000, "071"), _mark(T + 2_000, reason="行人")]
    members = [_member("e-057", "057", T), _member("e-071", "071", T + 1_000)]
    untouched = ([dict(mark) for mark in marks], [dict(member) for member in members])

    controls.set_review(marks, members, can_complete=False, fully_viewed=True, notice="请逐帧看完整团")

    # The two numbers stay apart: people counted and chip records of this crossing.
    assert controls.summary.text() == "已确认 2 人 · 2 条有效记录 · 已排除 1 人"
    assert controls.notice.text() == "请逐帧看完整团"
    assert controls.mark_combo.count() == 3
    assert "057" in controls.mark_combo.itemText(0)
    assert format_chip_time(T) in controls.mark_combo.itemText(0)
    assert "已排除：行人" in controls.mark_combo.itemText(2)
    assert controls.member_combo.itemText(0) == MEMBER_PLACEHOLDER
    assert "023" not in controls.member_combo.itemText(1) and "057" in controls.member_combo.itemText(1)
    # The tick means the operator saw the whole group; it cannot be pre-set for them.
    assert not controls.fully_viewed.isEnabled() and not controls.fully_viewed.isChecked()
    assert not any(seen.values())
    assert ([*marks], [*members]) == untouched
    assert controls.mark_group.isVisibleTo(controls) and controls.member_group.isVisibleTo(controls)

    controls.set_review([], [])
    assert controls.summary.text() == "已确认 0 人 · 0 条有效记录"
    assert not controls.mark_group.isVisibleTo(controls)
    assert not controls.member_group.isVisibleTo(controls)
    assert not controls.exclude_button.isEnabled() and not controls.keep_button.isEnabled()
    assert not any(seen.values())


def test_choosing_a_person_follows_them_and_keeps_the_middle_of_a_typing(qapp):
    controls = CrossingReviewControls()
    controls.show()
    seen = _watch(controls)
    marks = [_mark(T, "057"), _mark(T + 1_000, "071"), _mark(T + 2_000, reason="车辆")]
    controls.set_review(marks)

    controls.select_mark(1)
    assert controls.mark_combo.currentIndex() == 1
    assert controls.bib_input.text() == "071"
    assert not any(seen.values())  # picking a row on the parent's behalf decides nothing

    # The operator's own pick from the combo is the one that announces itself.
    QTest.keyClick(controls.mark_combo, Qt.Key_Down)
    assert seen["mark_selected"] == [2]
    assert controls.mark_combo.currentIndex() == 2
    assert controls.bib_input.text() == ""
    assert not controls.bib_input.isEnabled() and not controls.exclude_button.isEnabled()
    assert controls.restore_button.isEnabled()  # an excluded person is restored, not excluded again

    # Counting another person must not move the operator off the one they work on.
    controls.select_mark(0)
    controls.set_review(marks + [_mark(T + 3_000)])
    assert controls.mark_combo.currentIndex() == 0
    assert controls.bib_input.text() == "057"
    assert controls.summary.text() == "已确认 3 人 · 0 条有效记录 · 已排除 1 人"

    # A removal below the selection puts another person under it: the field follows them.
    controls.set_review(marks[1:])
    assert controls.mark_combo.currentIndex() == 0
    assert controls.bib_input.text() == "071"
    assert controls.bib_input.isEnabled() and not controls.restore_button.isEnabled()


def test_a_number_is_recorded_by_enter_or_button_and_duplicates_are_stopped(qapp):
    controls = CrossingReviewControls()
    controls.show()
    seen = _watch(controls)
    marks = [_mark(T), _mark(T + 1_000)]
    controls.set_review(marks)

    controls.select_mark(0)
    QTest.keyClicks(controls.bib_input, "057")
    QTest.keyClick(controls.bib_input, Qt.Key_Return)
    assert seen["bib_assigned"] == [(0, "057")]

    # The caller stores it and repaints; the number the operator typed stays put.
    marks[0]["bib"] = "057"
    controls.set_review(marks)
    assert controls.bib_input.text() == "057"

    # The same number on two counted riders is a known mistake, not a new record.
    controls.select_mark(1)
    controls.bib_input.setText("057")
    controls.assign_button.click()
    assert seen["bib_assigned"] == [(0, "057")]
    assert "已经填在另一位" in controls.notice.text()
    controls.bib_input.setText("071")
    controls.assign_button.click()
    assert seen["bib_assigned"] == [(0, "057"), (1, "071")]

    # Emptying the field is how a wrong number is taken back off one rider.
    controls.select_mark(0)
    controls.bib_input.clear()
    controls.assign_button.click()
    assert seen["bib_assigned"][-1] == (0, "")


def test_excluding_needs_a_named_reason_and_restoring_gives_the_person_back(qapp):
    controls = CrossingReviewControls()
    controls.show()
    seen = _watch(controls)
    assert [controls.exclusion_combo.itemText(index) for index in range(controls.exclusion_combo.count())] \
        == list(EXCLUSION_REASONS)

    marks = [_mark(T), _mark(T + 1_000, reason="车辆")]
    controls.set_review(marks)
    controls.select_mark(0)
    controls.exclusion_combo.setCurrentIndex(2)  # 车辆
    controls.exclude_button.click()
    assert seen["exclusion_changed"] == [(0, "车辆")]

    marks[0]["exclusion_reason"] = "车辆"  # the caller records reason and frame
    controls.set_review(marks)
    assert "已排除：车辆" in controls.mark_combo.itemText(0)
    assert controls.summary.text() == "已确认 0 人 · 0 条有效记录 · 已排除 2 人"
    assert controls.restore_button.isEnabled() and not controls.exclude_button.isEnabled()
    assert controls.exclusion_combo.currentText() == "车辆"

    controls.restore_button.click()
    assert seen["exclusion_changed"] == [(0, "车辆"), (0, "")]  # "" hands the person back

    # Nothing to name means nothing to record: the widget never invents a reason.
    controls.set_review([_mark(T)])
    controls.select_mark(0)
    controls.exclusion_combo.clear()
    controls.exclude_button.click()
    assert seen["exclusion_changed"] == [(0, "车辆"), (0, "")]
    assert "写明依据" in controls.notice.text()


def test_a_mis_click_is_removed_and_an_excluded_person_is_restored_first(qapp):
    controls = CrossingReviewControls()
    controls.show()
    seen = _watch(controls)
    marks = [_mark(T, "057"), _mark(T + 1_000, reason="行人")]

    controls.set_review(marks)
    assert not controls.remove_button.isEnabled()  # nothing is chosen yet
    controls.select_mark(0)
    controls.remove_button.click()
    assert seen["mark_removed"] == [0]

    # An excluded person keeps its frame and reason; restore first, then remove.
    controls.select_mark(1)
    assert not controls.remove_button.isEnabled()
    controls.remove_button.setEnabled(True)  # a caller that re-enables it blindly
    controls.remove_button.click()
    assert seen["mark_removed"] == [0]
    assert "先恢复为选手" in controls.notice.text()


def test_a_chip_decision_waits_for_a_record_the_operator_chose(qapp):
    controls = CrossingReviewControls()
    controls.show()
    seen = _watch(controls)
    members = [_member("e-020", "020", T), _member("e-023", "023", T + 60, state="上游已作废")]
    controls.set_review([], members)

    # Nothing is auto-selected, and the placeholder carries no event id.
    assert controls.member_combo.currentIndex() == 0
    assert controls.member_combo.itemText(0) == MEMBER_PLACEHOLDER
    assert controls.member_combo.itemData(0, Qt.UserRole) is None
    assert not controls.keep_button.isEnabled() and not controls.delete_button.isEnabled()
    # A disabled button swallows the click; the guard still holds if a caller
    # re-enables it blindly, and it never decides on its own.
    controls.keep_button.setEnabled(True)
    controls.keep_button.click()
    assert seen["member_decision"] == []
    assert "先在下拉里选一条芯片记录" in controls.notice.text()
    # A record the upstream voided is shown for the record, but it is no record.
    assert controls.summary.text() == "已确认 0 人 · 1 条有效记录"
    assert not controls.member_combo.model().item(2).isEnabled()

    # The operator's own choice, and only that, arms the two decisions.
    QTest.keyClick(controls.member_combo, Qt.Key_Down)
    assert controls.member_combo.currentIndex() == 1
    assert controls.keep_button.isEnabled() and controls.delete_button.isEnabled()
    controls.delete_button.click()
    assert seen["member_decision"] == [("e-020", "delete")]

    # A repaint keeps the choice; a fresh review starts from none again.
    controls.set_review([], members)
    assert controls.member_combo.currentIndex() == 1
    assert seen["member_decision"] == [("e-020", "delete")]
    controls.set_review([], [_member("e-999", "999", T)])
    assert controls.member_combo.currentIndex() == 0
    assert not controls.keep_button.isEnabled()


def test_the_tick_is_cleared_when_this_group_cannot_be_closed(qapp):
    controls = CrossingReviewControls()
    controls.show()
    seen = _watch(controls)
    controls.set_review([_mark(T)], can_complete=True, fully_viewed=True)
    assert controls.fully_viewed.isEnabled() and controls.fully_viewed.isChecked()
    assert seen["fully_viewed_changed"] == []

    QTest.mouseClick(controls.fully_viewed, Qt.LeftButton)
    assert seen["fully_viewed_changed"] == [False]

    controls.set_review([_mark(T)], can_complete=False, fully_viewed=True)
    assert not controls.fully_viewed.isEnabled() and not controls.fully_viewed.isChecked()
    assert seen["fully_viewed_changed"] == [False]  # clearing it is not the operator's answer

    # Only a group has "the whole group has been seen"; elsewhere it is not shown
    # at all, and never left ticked behind the operator's back.
    controls.set_review([_mark(T)], can_complete=True, fully_viewed=True, show_fully_viewed=False)
    assert not controls.fully_viewed.isVisibleTo(controls) and not controls.fully_viewed.isChecked()
    assert seen["fully_viewed_changed"] == [False]


def test_a_child_doubt_is_chosen_explicitly_and_settled_by_its_own_evidence(qapp):
    controls = CrossingReviewControls()
    controls.show()
    seen = _watch(controls)
    children = [
        _item("two:1", kind=KIND_TWO_CHIPS_ONE_CROSSING, chip_ms=T, bibs=("020", "023")),
        _item("near:1", kind=KIND_NEAR_CROSSING, chip_ms=T + 500, bibs=("057",)),
    ]
    controls.set_review([_mark(T)], conflicts=children)

    # No child is chosen for the operator, and the refresh decides nothing.
    assert controls.conflict_combo.currentIndex() == 0
    assert controls.conflict_combo.itemText(0) == CONFLICT_PLACEHOLDER
    assert not controls.conflict_view_button.isEnabled() and not controls.conflict_decide_button.isEnabled()
    assert not any(seen.values())
    assert controls.conflict_group.isVisibleTo(controls)

    # The operator's own choice; the row names the numbers and offers only the
    # reasons that settle this doubt by evidence — no coarse chip deletion.
    QTest.keyClick(controls.conflict_combo, Qt.Key_Down)
    assert seen["conflict_selected"] == ["two:1"]
    assert controls.conflict_combo.currentIndex() == 1
    assert "020、023" in controls.conflict_combo.itemText(1)
    assert [controls.conflict_action.itemData(index, Qt.UserRole)
            for index in range(controls.conflict_action.count())] == ["side_by_side"]
    assert "确认是并排两人" in controls.conflict_action.itemText(0)
    assert "一次过线" in controls.conflict_label.text()
    controls.conflict_decide_button.click()
    assert seen["conflict_decided"] == [("two:1", "side_by_side")]

    # 查看疑点 says which doubt to open, for the same chosen child.
    controls.conflict_view_button.click()
    assert seen["conflict_selected"] == ["two:1", "two:1"]

    # Another doubt offers its own reasons only: recording a plate and deleting
    # chips belong to the row that owns that exact record.
    QTest.keyClick(controls.conflict_combo, Qt.Key_Down)
    assert controls.conflict_combo.currentIndex() == 2
    assert seen["conflict_selected"][-1] == "near:1"
    assert [controls.conflict_action.itemData(index, Qt.UserRole)
            for index in range(controls.conflict_action.count())] \
        == ["same_crossing", "not_rider", "ride_back", "insufficient"]

    # A repaint keeps the operator's choice; a doubt that is gone starts from none.
    controls.set_review([_mark(T)], conflicts=children)
    assert controls.conflict_combo.currentIndex() == 2
    assert seen["conflict_decided"] == [("two:1", "side_by_side")]
    controls.set_review([_mark(T)], conflicts=children[:1])
    assert controls.conflict_combo.currentIndex() == 0
    assert not controls.conflict_decide_button.isEnabled()
    controls.set_review([_mark(T)])
    assert not controls.conflict_group.isVisibleTo(controls)


def test_a_doubt_with_no_evidence_reason_cannot_be_settled_here(qapp):
    controls = CrossingReviewControls()
    controls.show()
    seen = _watch(controls)
    controls.set_review([_mark(T)], conflicts=[_item("sync:1", kind=KIND_SYNC, chip_ms=T)])

    QTest.keyClick(controls.conflict_combo, Qt.Key_Down)
    assert controls.conflict_combo.currentIndex() == 1
    assert controls.conflict_action.count() == 0  # 已在 CycleRace 处理 / 已核对 are not ours to claim
    assert not controls.conflict_decide_button.isEnabled()
    controls.conflict_decide_button.setEnabled(True)  # a caller that re-enables it blindly
    controls.conflict_decide_button.click()
    assert seen["conflict_decided"] == []
    assert "没有能由原帧证据直接确认的处理" in controls.notice.text()
    controls.conflict_view_button.click()  # looking at it is still allowed
    assert seen["conflict_selected"] == ["sync:1", "sync:1"]


def test_the_controls_stay_inside_a_laptop_width(qapp):
    controls = CrossingReviewControls()
    controls.show()
    controls.set_review(
        [_mark(T, "057"), _mark(T + 1_000)],
        [_member("e-1", "057", T)],
        [_item("two:1", kind=KIND_TWO_CHIPS_ONE_CROSSING, chip_ms=T, bibs=("020", "023"))],
        can_complete=True, notice="未查看 3.2 秒 · 未对应记录 1 · 多出 1 人")

    QApplication.instance().processEvents()
    # The rows are compact enough to sit under a 4:3 picture on a laptop, and
    # the long notice line cannot force the strip wider or squeeze it away.
    assert controls.sizeHint().width() <= 900
    assert controls.minimumSizeHint().width() <= 700
    assert controls.mark_combo.minimumWidth() <= 160
    assert "未对应记录 1" in controls.notice.toolTip()


# ----- CrossingCardRail ------------------------------------------------

def test_the_rail_shows_one_card_per_task_in_time_order(qapp, tmp_path):
    rail = CrossingCardRail()
    rail.show()
    assert rail.title.text() == "过线卡片"
    assert rail.list.viewMode() == QListView.IconMode
    assert rail.list.iconSize() == RAIL_ICON_SIZE

    parent = _item("batch:1", kind=KIND_BATCH_READS, chip_ms=T + 2_000, frame=FRAME,
                   evidence={"batch_members": [{"event_id": "e-020"}, {"event_id": "e-023"}]}, bibs=("020", "023"))
    first = _item("child:a", frame=dict(FRAME, frame_index=700), evidence={"group": "batch:1"})
    second = _item("child:b", frame=dict(FRAME, frame_index=701), evidence={"group": "batch:1"})
    # Two riders on one frame are two tasks; the same id twice is still one.
    peer_a = _item("peer:a", chip_ms=T + 1_000, frame=dict(FRAME, frame_index=805), bibs=("371",))
    peer_b = _item("peer:b", chip_ms=T + 1_000, frame=dict(FRAME, frame_index=805), bibs=("376",))
    solo = _item("solo:1", chip_ms=T + 1_500, frame=FRAME, bibs=("057",))
    without_frame = _item("waiting:1", chip_ms=T, status=STATUS_WAITING)

    rail.set_items([parent, first, second, solo, peer_b, peer_a, without_frame, parent], tmp_path)
    shown = [rail.list.item(index).data(Qt.UserRole) for index in range(rail.list.count())]
    assert shown == ["peer:a", "peer:b", "solo:1", "batch:1"]
    # The frame is the card's own evidence; the members keep their parent's task.
    assert "371" in rail.list.item(0).text() and "376" in rail.list.item(1).text()
    assert "020、023" in rail.list.item(3).text()


def test_a_card_is_chronological_by_the_recorded_moment_and_names_the_chip_time(qapp):
    rail = CrossingCardRail()
    by_chip = _item("a:1", chip_ms=T + 3_000, frame=FRAME)
    by_evidence = _item("b:1", chip_ms=T + 9_000, evidence={"recorder_ms": T + 1_000, "frame": FRAME})
    by_frame = _item("c:1", chip_ms=T + 8_000, evidence={"frame": dict(FRAME, frame_index=6, recorder_ms=T + 2_000)})
    without_time = _item("d:1", evidence={"frame": FRAME})

    rail.set_items([by_chip, without_time, by_frame, by_evidence])
    shown = [rail.list.item(index).data(Qt.UserRole) for index in range(rail.list.count())]
    # When the picture was taken orders the rail; a card with no moment sorts last.
    assert shown == ["b:1", "c:1", "a:1", "d:1"]
    # The card reads the recorded moment, and the official time lives apart.
    assert format_chip_time(T + 1_000) in rail.list.item(0).text()
    assert format_chip_time(T + 9_000) not in rail.list.item(0).text()
    assert f"官方芯片时间 {format_chip_time(T + 9_000)}" in rail.list.item(0).toolTip()


def test_an_auto_passed_card_reads_as_a_normal_single_rider(qapp):
    rail = CrossingCardRail()
    passed = _item("pass:1", chip_ms=T, frame=FRAME, bibs=("057",), evidence={"auto_passed": True})
    waiting = _item("waiting:1", chip_ms=T + 1_000, frame=FRAME, status=STATUS_WAITING, bibs=("071",))

    rail.set_items([passed, waiting])
    first = rail.list.item(0).text()
    assert "正常单人" in first and "已自动对应" in first
    assert "无芯片" not in first and "参考" not in first and "待处理" not in first
    assert "待证据" in rail.list.item(1).text()  # an untouched card still says what it waits for


def test_a_decoded_preview_from_the_workbench_fills_a_card_in_place(qapp, tmp_path):
    rail = CrossingCardRail()
    rail.show()
    app = QApplication.instance()
    _write_card(tmp_path, "cards/solo.jpg", (40, 40, 220))
    passed = _item("pass:1", chip_ms=T, frame=FRAME, bibs=("057",), evidence={"auto_passed": True})
    with_card = _item("solo:1", chip_ms=T + 1_000, frame=FRAME, bibs=("071",),
                      evidence={"card": "cards/solo.jpg"})
    rail.set_items([passed, with_card], tmp_path)
    app.processEvents()
    entry = rail.list.item(0)
    clicks = []
    rail.item_selected.connect(clicks.append)
    # An auto-passed rider has no backend JPEG, so their card carries no picture.
    assert entry.icon().isNull()

    pixmap = QPixmap(60, 40)
    pixmap.fill(QColor(220, 90, 60))
    rail.set_preview("pass:1", pixmap)
    app.processEvents()

    # The card takes the pane the workbench decoded: same entry, same words, no
    # rebuild — only the picture appears, and no card got opened by it.
    assert rail.list.item(0) is entry
    assert "正常单人" in entry.text()
    assert not clicks
    shown = entry.icon().pixmap(RAIL_ICON_SIZE).toImage()
    assert shown.pixelColor(shown.width() // 2, shown.height() // 2).getRgb()[:3] == (220, 90, 60)
    # The neighbour with its own JPEG is untouched by the preview (cv2 writes
    # BGR and JPEG is lossy, so only the colour family is checked).
    still = rail.list.item(1).icon().pixmap(RAIL_ICON_SIZE).toImage()
    red, green, blue = still.pixelColor(still.width() // 2, still.height() // 2).getRgb()[:3]
    assert red > 150 and green < 90 and blue < 90

    # The remembered preview survives a refresh; a written JPEG still wins where
    # one exists, and clearing the preview gives the picture-free card back.
    rail.set_items([passed, with_card], tmp_path)
    assert not rail.list.item(0).icon().isNull()
    rail.set_preview("pass:1", QPixmap())
    assert rail.list.item(0).icon().isNull()
    assert not rail.list.item(1).icon().isNull()


def test_the_header_line_is_a_toolbar_and_the_rail_keeps_one_card_height(qapp):
    rail = CrossingCardRail()
    assert isinstance(rail.toolbar, QHBoxLayout)
    assert rail.toolbar.itemAt(0).widget() is rail.title
    # The workbench drops its own controls in front of the trailing stretch,
    # beside the title, without a second row.
    marker = QPushButton("原始画面")
    rail.toolbar.insertWidget(rail.toolbar.count() - 1, marker)
    assert rail.toolbar.indexOf(marker) == 1
    # One row of cards: never squeezed to nothing and never spreading further.
    assert rail.list.minimumHeight() == rail.list.maximumHeight() == RAIL_LIST_HEIGHT


def test_a_card_is_the_written_jpeg_and_a_new_stamp_reloads_it(qapp, tmp_path):
    rail = CrossingCardRail()
    rail.show()
    card = _write_card(tmp_path, "cards/card.jpg", (40, 40, 220))
    with_card = _item("solo:1", chip_ms=T, frame=FRAME, evidence={"card": "cards/card.jpg"}, bibs=("057",))
    text_only = _item("plain:1", chip_ms=T + 1_000, frame=FRAME, bibs=("071",))

    rail.set_items([with_card, text_only], tmp_path)
    assert not rail.list.item(0).icon().isNull()
    # Without a card the tile still says when, which class and which numbers.
    assert rail.list.item(1).icon().isNull()
    assert format_chip_time(T + 1_000) in rail.list.item(1).text() and "071" in rail.list.item(1).text()
    before = rail.list.item(0).icon().pixmap(RAIL_ICON_SIZE).toImage()

    _write_card(tmp_path, "cards/card.jpg", (220, 40, 40))
    stamp = card.stat().st_mtime_ns
    os.utime(card, ns=(stamp + 10**9, stamp + 10**9))
    rail.set_items([with_card, text_only], tmp_path)
    after = rail.list.item(0).icon().pixmap(RAIL_ICON_SIZE).toImage()
    assert before != after


def test_a_click_opens_a_card_and_a_refresh_keeps_the_place(qapp):
    rail = CrossingCardRail()
    rail.resize(640, 200)
    rail.show()
    app = QApplication.instance()
    app.processEvents()
    items = [_item(f"solo:{index}", chip_ms=T + index * 1_000, frame=dict(FRAME, frame_index=index))
             for index in range(30)]
    rail.set_items(items, None)
    app.processEvents()

    clicks = []
    rail.item_selected.connect(clicks.append)
    scrollbar = rail.list.horizontalScrollBar()
    scrollbar.setValue(1_000)
    app.processEvents()
    scroll = scrollbar.value()
    point = QPoint(10, 10)
    visible = rail.list.itemAt(point)
    QTest.mouseClick(rail.list.viewport(), Qt.LeftButton, pos=point)
    assert clicks == [visible.data(Qt.UserRole)]
    assert rail.list.selectedItems()[0].data(Qt.UserRole) == clicks[0]

    # An unchanged refresh is not a rebuild: same items, same place, same choice.
    entries = [rail.list.item(index) for index in range(rail.list.count())]
    rail.set_items([_item(item.item_id, chip_ms=item.chip_time_ms, frame=item.evidence["frame"])
                    for item in items], None)
    assert scrollbar.value() == scroll
    assert rail.list.item(0) is entries[0]
    assert rail.list.selectedItems()[0].data(Qt.UserRole) == clicks[0]
    assert clicks == [visible.data(Qt.UserRole)]
    assert rail.list.itemAt(point).data(Qt.UserRole) == visible.data(Qt.UserRole)

    # A card whose content changed is updated in place, not rebuilt away.
    changed = [_item(item.item_id, chip_ms=item.chip_time_ms, frame=item.evidence["frame"],
                     status=STATUS_WAITING if item.item_id == "solo:1" else STATUS_OPEN)
               for item in items]
    rail.set_items(changed, None)
    assert scrollbar.value() == scroll
    assert rail.list.item(0) is entries[0]
    assert "待证据" in rail.list.item(1).text()
    assert rail.list.selectedItems()[0].data(Qt.UserRole) == clicks[0]

    # Opening a card on the parent's behalf is quiet too.
    rail.set_selected(items[4].item_id)
    assert rail.list.selectedItems()[0].data(Qt.UserRole) == items[4].item_id
    assert clicks == [visible.data(Qt.UserRole)]

    # The keyboard opens the card under the cursor as well, and reading a card
    # never changes what the rail says.
    texts = [rail.list.item(index).text() for index in range(rail.list.count())]
    rail.list.setCurrentRow(2)
    QTest.keyClick(rail.list, Qt.Key_Return)
    assert clicks[-1] == items[2].item_id
    assert [rail.list.item(index).text() for index in range(rail.list.count())] == texts
