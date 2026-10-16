import csv
import json
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QMessageBox

from realtime.batch_review import aggregate_batches, read_snapshots
from realtime.chip_checks import chip_check_items
from realtime.chip_history import ChipRead
from realtime.problem_items import (
    CATEGORY_COVERAGE,
    CATEGORY_VIDEO,
    KIND_BUNCH,
    KIND_NEAR_CROSSING,
    KIND_NO_CHIP_CROSSING,
    KIND_NOT_ARRIVED,
    KIND_TWO_CHIPS_ONE_CROSSING,
    KIND_UNKNOWN_BIB,
    ACTION_REASON_PRESETS,
    STATUS_CLOSED_UPSTREAM,
    STATUS_EXCLUDED,
    STATUS_INFO,
    ProblemItem,
)
from realtime.problem_panel import ProblemListPanel


T = 1_789_784_000_000
FRAME = {"camera_index": 1, "segment_id": "seg", "video_path": "videos/a.mkv", "frame_index": 812,
         "position_ms": 40_600, "session_id": "s", "calibration_version": 2}


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


def _write_list(root, items, auto_passed):
    directory = root / "finish_problems"
    (directory / "cards").mkdir(parents=True, exist_ok=True)
    ok, data = cv2.imencode(".jpg", np.full((120, 120, 3), 90, np.uint8))
    (directory / "cards" / "card.jpg").write_bytes(data.tobytes())
    ok, data = cv2.imencode(".jpg", np.full((60, 540, 3), 90, np.uint8))
    (directory / "cards" / "strip.jpg").write_bytes(data.tobytes())
    payload = {
        "schema_version": 1, "summary": {"auto_passed": len(auto_passed), "unverifiable": 3},
        "items": [item.to_payload() for item in items], "auto_passed": auto_passed,
    }
    (directory / "problem_list.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _items(two_chip_bibs=("020", "023")):
    return [
        ProblemItem("no_chip:1", KIND_NO_CHIP_CROSSING, CATEGORY_VIDEO, "11:01:28.28 有人过线，附近没有芯片读数",
                    chip_time_ms=T, evidence={"frame": dict(FRAME), "card": "cards/card.jpg",
                                              "filmstrip": "cards/strip.jpg"}, calibration_version=2),
        ProblemItem("two:1", KIND_TWO_CHIPS_ONE_CROSSING, CATEGORY_VIDEO, "芯片 020 与 023 对应同一次过线",
                    chip_time_ms=T + 1_000, bibs=two_chip_bibs, event_ids=("e-020", "e-023"),
                    evidence={"frame": dict(FRAME, frame_index=900)}),
        ProblemItem("closed:1", KIND_TWO_CHIPS_ONE_CROSSING, CATEGORY_VIDEO, "已作废", chip_time_ms=T + 2_000,
                    bibs=("371", "376"), status=STATUS_CLOSED_UPSTREAM),
        ProblemItem("walker:1", KIND_NO_CHIP_CROSSING, CATEGORY_VIDEO, "行人", chip_time_ms=T + 3_000,
                    status=STATUS_EXCLUDED),
        ProblemItem("missing:1", KIND_NOT_ARRIVED, CATEGORY_COVERAGE, "女子组 应到未到 2 人",
                    bibs=("511", "512"), status=STATUS_INFO),
    ]


def test_panel_lists_problems_resolves_and_exports(qapp, tmp_path, monkeypatch):
    _write_list(tmp_path, _items(), [{"bib": "057", "chip_time_ms": T + 500, "frame": dict(FRAME), "source": "auto"}])
    panel = ProblemListPanel()
    jumps = []
    panel.jump_requested.connect(jumps.append)
    panel.set_workspace(tmp_path)

    # The line on screen is the queue in four classes; the long count is its tooltip.
    assert panel.summary.text() == "疑点 一人双芯片 1 · 漏读 1"
    assert panel.summary.toolTip() == "自动通过 1 人 · 待处理 2 条 · 无法核实 3 人 · 已处理 1 条"
    assert panel.table.rowCount() == 3  # two pending plus the reference list
    panel.queue_filter.setCurrentIndex(3)  # 全部
    # 自动对应的单人不上表，但「全部」是这场比赛的过线记录：它也在里面。
    assert panel.table.rowCount() == 6
    assert [row.text() for row in (panel.table.item(index, 1) for index in range(panel.table.rowCount()))] \
        == ["漏读", "单人", "一人双芯片", "一人双芯片", "非选手", "待确认"]
    panel.queue_filter.setCurrentIndex(0)

    panel.table.setCurrentCell(0, 0)
    assert not panel.card.pixmap().isNull()
    assert not panel.filmstrip.pixmap().isNull()
    panel.jump_button.click()
    assert jumps == [FRAME]

    rider = next(button for button in panel.action_buttons if button.property("problemAction") == "rider_crossing")
    # 号码看不清也是同一个按钮：先记下这次过线，留在待处理里等认号。
    rider.click()
    assert panel.store.get("no_chip:1").action == "confirm_crossing"
    assert panel.state(panel.current_item()).startswith("待处理")
    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *args: asked.append(args[2]) or QMessageBox.No)
    panel.bib_input.setText("067")
    rider.click()
    assert asked and "057" in asked[0]
    assert panel.store.get("no_chip:1").action == "confirm_crossing"
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.Yes)
    panel.bib_input.setText("067")
    rider.click()
    assert panel.store.get("no_chip:1").bib == "067"

    panel.table.setCurrentCell(0, 0)
    assert panel.current_item().item_id == "two:1"
    # 一个人带两块芯片：按钮上写着留哪一块，按下就是答案。
    keep = next(button for button in panel.action_buttons if button.text() == "留 020")
    keep.click()
    assert panel.store.get("two:1").action == "delete_extra_chip"
    assert panel.store.get("two:1").reason == ACTION_REASON_PRESETS["delete_extra_chip"]
    assert panel.summary_text().startswith("自动通过 1 人 · 待处理 0 条")

    counts = panel.export()
    assert counts == {"补录清单.csv": 1, "建议删除清单.csv": 1, "建议改号改圈清单.csv": 0}
    rows = list(csv.reader((tmp_path / "finish_problems" / "exports" / "建议删除清单.csv").open(encoding="utf-8-sig")))
    assert rows[1][0] == "023" and rows[1][2] == "e-023" and rows[1][4] == ACTION_REASON_PRESETS["delete_extra_chip"]
    manual = list(csv.reader((tmp_path / "finish_problems" / "exports" / "补录清单.csv").open(encoding="utf-8-sig")))
    assert manual[1][0] == "067" and "第 813 帧" in manual[1][2]


def test_the_queue_follows_crossings_including_manual_riders_without_chips(qapp, tmp_path):
    from dataclasses import replace

    from realtime.chip_history import format_chip_time
    from realtime.clock_sync import CLOCK_MODEL_FILENAME, ClockModel, ClockModelStore

    fit = ClockModel(1, 2000, 0, T, T, T + 20_000, 10, 10, 0, 2000, 10)
    fit = ClockModelStore(tmp_path / CLOCK_MODEL_FILENAME).publish(fit)
    frame = dict(FRAME, calibration_version=fit.version)
    first = replace(_items()[1], evidence={"frame": frame, "recorder_ms": T + 2100},
                    calibration_version=fit.version, chip_time_ms=T + 5000)
    later = replace(_items()[0], evidence={"frame": frame, "recorder_ms": T + 2300},
                    calibration_version=fit.version)
    group = ProblemItem("group", KIND_BUNCH, CATEGORY_VIDEO, "集团", chip_time_ms=T + 400,
                        bibs=("040", "041"), calibration_version=fit.version,
                        evidence={"frame": frame, "recorder_ms": T + 2400, "recorder_start_ms": T - 5000})
    normal = {"bib": "057", "event_id": "e-057", "chip_time_ms": T + 250,
              "frame": frame, "recorder_ms": T + 2250}
    _write_list(tmp_path, [group, later, first], [normal])
    panel = ProblemListPanel()
    try:
        panel.set_workspace(tmp_path)
        manual = panel.add_manual_problem(frame, recorder_ms=T + 2200)
        assert manual.chip_time_ms is None
        assert [entry.item_id for entry in panel.rows] == [first.item_id, manual.item_id, later.item_id, "group"]
        assert [panel.table.item(row, 0).text() for row in range(4)] == [
            format_chip_time(T + value) for value in (100, 200, 300, 400)]
        assert panel.current_item().item_id == manual.item_id
        panel._step(1)
        assert panel.current_item().item_id == later.item_id

        panel.queue_filter.setCurrentIndex(3)
        assert [entry.item_id for entry in panel.rows if entry.kind != "auto_passed"] == [
            first.item_id, manual.item_id, later.item_id, "group"]
        assert [panel.table.item(row, 0).text() for row in range(5)] == [
            format_chip_time(T + value) for value in (100, 200, 250, 300, 400)]
        assert next(entry for entry in panel.rows if entry.item_id == first.item_id).chip_time_ms == T + 5000
        panel.queue_filter.setCurrentIndex(0)
        assert [entry.item_id for entry in panel.rows] == [first.item_id, manual.item_id, later.item_id, "group"]
    finally:
        panel.close()


def test_new_calibration_reorders_unversioned_crossings_and_preserves_selection(qapp, tmp_path):
    from dataclasses import replace

    from realtime.chip_history import format_chip_time
    from realtime.clock_sync import CLOCK_MODEL_FILENAME, ClockModel, ClockModelStore

    clocks = ClockModelStore(tmp_path / CLOCK_MODEL_FILENAME)
    fit = ClockModel(1, 1000, 0, T, T, T + 20_000, 10, 10, 0, 1000, 10)
    clocks.publish(fit)
    clocks.publish(replace(fit, camera_index=2, intercept_ms=5000))
    unversioned = replace(_items()[0], calibration_version=0,
                          evidence={"frame": dict(FRAME, calibration_version=0), "recorder_ms": T + 1600})
    pinned = replace(_items()[1], calibration_version=1,
                     evidence={"frame": dict(FRAME, camera_index=2, calibration_version=1),
                               "recorder_ms": T + 5500})
    _write_list(tmp_path, [unversioned, pinned], [])
    panel = ProblemListPanel()
    try:
        panel.set_workspace(tmp_path)
        assert [entry.item_id for entry in panel.rows] == [pinned.item_id, unversioned.item_id]
        assert panel.current_item().item_id == pinned.item_id
        clocks.publish(replace(fit, intercept_ms=2000))
        panel.refresh()  # 清单没变，只有录像校时新增一版。
        assert [entry.item_id for entry in panel.rows] == [unversioned.item_id, pinned.item_id]
        assert panel.current_item().item_id == pinned.item_id
        assert panel.table.item(0, 0).text() == format_chip_time(T - 400)
        assert panel.table.item(1, 0).text() == format_chip_time(T + 500)
        assert [entry.chip_time_ms for entry in panel.rows] == [unversioned.chip_time_ms, pinned.chip_time_ms]
    finally:
        panel.close()


def test_upstream_voids_and_changes_reopen_or_close_items(qapp, tmp_path):
    _write_list(tmp_path, _items(), [])
    panel = ProblemListPanel()
    panel.set_workspace(tmp_path)
    two = next(item for item in panel.items if item.item_id == "two:1")
    panel.store.resolve(two, "side_by_side")
    assert panel.state(two).startswith("已处理")

    _write_list(tmp_path, _items(two_chip_bibs=("021", "023")), [])
    panel.refresh()
    changed = next(item for item in panel.items if item.item_id == "two:1")
    assert panel.state(changed) == "上游已修改，请重新处理"

    events = [SimpleNamespace(event_id="e-020", is_active=False), SimpleNamespace(event_id="e-023", is_active=False)]
    panel.set_events_provider(lambda: events)
    assert panel.state(changed) == "CycleRace 已处理"
    assert panel.analysis_command()[1][-2:] == ["--analyze-problems", str(tmp_path)]


def test_progress_preserves_split_chinese_json_and_final_memory_message(qapp):
    panel = ProblemListPanel()
    message = (json.dumps({"stage": "提取线", "done": 2, "total": 21}, ensure_ascii=False) + "\n").encode("utf-8")
    split = message.index("提".encode("utf-8")) + 1
    panel._consume_progress(message[:split])
    assert not panel.hint.text()
    panel._consume_progress(message[split:])
    assert panel.analysis_status == "后台分析：提取线 2/21"
    panel._consume_progress(json.dumps({"stage": "skipped", "message": "可用内存不足 1536 MB"},
                                      ensure_ascii=False).encode("utf-8"))
    panel._analysis_done()
    assert panel.analysis_status == "可用内存不足 1536 MB"


def test_gui_analysis_sets_utf8_unbuffered_worker_environment(qapp, tmp_path, monkeypatch):
    from PyQt5.QtCore import QProcess

    monkeypatch.setattr(QProcess, "start", lambda self: None)
    panel = ProblemListPanel()
    panel.set_workspace(tmp_path)
    panel._toggle_analysis()
    environment = panel._process.processEnvironment()
    assert environment.value("PYTHONIOENCODING") == "utf-8"
    assert environment.value("PYTHONUNBUFFERED") == "1"
    panel._process = None


def test_refresh_preserves_selected_problem_and_per_item_drafts(qapp, tmp_path):
    items = _items()
    _write_list(tmp_path, items, [])
    panel = ProblemListPanel()
    panel.set_workspace(tmp_path)
    panel.bib_input.setText("307")
    panel.reason_input.setText("check jersey")
    panel.set_browsing(True)
    _write_list(tmp_path, list(reversed(items)), [])
    panel.refresh()
    assert panel.current_item().item_id == "no_chip:1"
    assert panel.bib_input.text() == "307"
    assert panel._browsing
    assert all(not button.isEnabled() for button in panel.action_buttons)
    panel._resolve("record_bib")
    assert panel.store.get("no_chip:1") is None
    other = next(i for i, item in enumerate(panel.rows) if item.item_id == "two:1")
    panel.table.setCurrentCell(other, 0)
    assert panel.bib_input.text() == ""
    panel.save_drafts()
    restored = ProblemListPanel()
    restored.set_workspace(tmp_path)
    row = next(i for i, item in enumerate(restored.rows) if item.item_id == "no_chip:1")
    restored.table.setCurrentCell(row, 0)
    assert restored.bib_input.text() == "307"


def test_hold_is_not_resolution_and_manual_item_survives_analysis(qapp, tmp_path):
    from realtime.problem_panel import PENDING_REASON
    _write_list(tmp_path, _items(), [])
    panel = ProblemListPanel()
    panel.set_workspace(tmp_path)
    panel._hold()
    # 待确认 is one press with nothing typed, and it decides nothing.
    assert panel._held["no_chip:1"] == PENDING_REASON
    assert panel.store.get("no_chip:1") is None
    # The row stays in the queue greyed, so the operator meets it again.
    assert "no_chip:1" in [item.item_id for item in panel.rows]
    row = next(i for i, item in enumerate(panel.rows) if item.item_id == "no_chip:1")
    assert panel.table.item(row, 2).foreground().color().name() == "#8a8f98"
    panel.reason_input.setText("wait for judge")
    panel._hold()
    assert not panel._held  # pressing it again hands the row back
    manual = panel.add_manual_problem(FRAME, "check jersey")
    assert manual.evidence["frame"] == FRAME
    assert not manual.bibs and manual.chip_time_ms is None
    _write_list(tmp_path, _items(), [])
    panel.refresh()
    assert sum(item.item_id == manual.item_id for item in panel.items) == 1
    panel.refresh()
    assert sum(item.item_id == manual.item_id for item in panel.items) == 1


def test_live_scheduler_serializes_pauses_and_drains_last_segment(qapp, tmp_path, monkeypatch):
    from PyQt5.QtCore import QProcess
    monkeypatch.setattr(QProcess, "start", lambda self: None)
    panel = ProblemListPanel()
    panel.set_workspace(tmp_path)
    (tmp_path / "finish_lines.json").write_text('{}')
    panel.live_tick(True)
    process = panel._process
    assert process is not None and "--live-problems" in process.arguments()
    panel.live_tick(True)
    assert panel._process is process
    panel._analysis_done(0)
    panel._next_analysis = 0
    panel.live_tick(False)
    assert panel._process is not None
    assert "--live-problems" not in panel._process.arguments()
    assert "--finish-live-problems" in panel._process.arguments()
    panel.stop_analysis()


def test_waiting_evidence_cannot_be_resolved(qapp, tmp_path):
    from dataclasses import replace
    from realtime.problem_items import KIND_WAITING, STATUS_WAITING
    waiting = replace(_items()[0], kind=KIND_WAITING, status=STATUS_WAITING)
    _write_list(tmp_path, [waiting], [])
    panel = ProblemListPanel()
    panel.set_workspace(tmp_path)
    assert not panel.rows
    panel.queue_filter.setCurrentIndex(1)
    assert panel.current_item() == waiting
    assert not panel.action_buttons
    assert not panel.hold_button.isEnabled()


def test_superseded_problem_keeps_draft_but_cannot_be_submitted(qapp, tmp_path):
    _write_list(tmp_path, _items(), [])
    panel = ProblemListPanel()
    panel.set_workspace(tmp_path)
    panel.bib_input.setText("307")
    _write_list(tmp_path, _items()[1:], [])
    panel.refresh()
    assert panel.current_item().item_id == "no_chip:1"
    assert panel.bib_input.text() == "307"
    assert panel.current_item().evidence["superseded"]
    assert not panel.action_buttons
    panel._resolve("record_bib")
    assert panel.store.get("no_chip:1") is None


def test_handoff_survives_new_analysis_until_explicit_upstream_ack(qapp, tmp_path):
    _write_list(tmp_path, _items(), [])
    panel = ProblemListPanel()
    panel.set_workspace(tmp_path)
    panel.bib_input.setText("307")
    panel._resolve("record_bib")
    _write_list(tmp_path, _items()[1:], [])
    panel.refresh()
    panel.queue_filter.setCurrentIndex(3)
    row = next(i for i, item in enumerate(panel.rows) if item.item_id == "no_chip:1")
    panel.table.setCurrentCell(row, 0)
    assert panel.state(panel.current_item()).startswith("待 CycleRace")
    assert panel.export()["补录清单.csv"] == 1
    panel._resolve("synced")
    assert panel.export()["补录清单.csv"] == 0


def test_workspace_switch_does_not_keep_previous_problem_or_draft(qapp, tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    _write_list(old, _items(), [])
    new.mkdir()
    panel = ProblemListPanel()
    panel.set_workspace(old)
    panel.bib_input.setText("307")
    panel.set_workspace(new)
    assert not panel.items
    assert panel.current_item() is None
    assert panel.bib_input.text() == ""
    panel.set_workspace(old)
    assert panel.bib_input.text() == "307"


def test_the_picture_counts_people_and_the_count_survives_a_restart(qapp, tmp_path):
    _write_list(tmp_path, _items(), [])
    panel = ProblemListPanel()
    panel.set_workspace(tmp_path)
    panel.table.setCurrentCell(0, 0)
    drawn = []
    panel.marks_changed.connect(drawn.append)

    panel.add_mark(0.25, 0.70, T)
    panel.add_mark(0.31, 0.68, T + 60)
    panel.add_mark(1.40, 0.68, T + 90)  # beside the picture, not on a person
    assert [mark["x"] for mark in panel.current_marks()] == [0.25, 0.31]
    assert panel.current_marks()[0]["recorder_ms"] == T
    assert panel.marks_label.text() == "已点 2 人" and drawn[-1] == tuple(panel.current_marks())

    panel.undo_mark()
    assert len(panel.current_marks()) == 1 and panel.marks_label.text() == "已点 1 人"
    panel.clear_marks()
    assert not panel.current_marks()

    panel.add_mark(0.4, 0.6, T + 200)
    panel.save_drafts()
    restored = ProblemListPanel()
    restored.set_workspace(tmp_path)
    restored.table.setCurrentCell(0, 0)
    assert restored.current_marks() == panel.current_marks()
    # Nothing was decided by pointing at people.
    assert restored.state(restored.current_item()) == "待处理"


def test_crossing_order_survives_backtracking_editing_and_undo(qapp, tmp_path):
    from realtime.chip_history import format_chip_time

    _write_list(tmp_path, [_items()[0]], [])
    panel = ProblemListPanel()
    panel.set_workspace(tmp_path)
    panel.toggle_big_view()
    later = dict(FRAME, frame_index=900, position_ms=45_000)
    earlier = dict(FRAME, frame_index=800, position_ms=40_000)
    panel.add_mark(.3, .5, T + 300, later)
    panel.add_mark(.4, .5, T + 100, earlier)
    panel.add_mark(.6, .5, T + 100, earlier)
    assert [panel.count_table.item(row, 0).text() for row in range(3)] == ["1 同时", "1 同时", "3"]
    assert panel.count_table.item(0, 1).text() == format_chip_time(T + 100)
    assert panel.count_table.currentRow() == 1  # 新点的是第三条原记录、第二行过线
    assert [mark["recorder_ms"] for mark in panel.current_marks()] == [T + 300, T + 100, T + 100]

    panel.count_table.setCurrentCell(0, 0)
    panel.bib_input.setText("057")
    panel.assign_button.click()
    assert panel.current_marks()[1]["bib"] == "057"
    assert panel.current_marks()[0]["bib"] == ""
    panel.exclude_button.click()
    assert panel.current_marks()[1]["exclusion_reason"]
    panel.restore_button.click()
    panel.remove_button.click()
    assert [mark["x"] for mark in panel.current_marks()] == [.3, .6]
    panel.undo_mark()  # 撤销最后一次点击，不是时间轴上最后过线的人。
    assert [mark["x"] for mark in panel.current_marks()] == [.3]
    panel.close()


def test_the_chip_time_column_uses_each_events_official_time(qapp, tmp_path):
    from realtime.chip_history import format_chip_time
    from realtime.passage_receiver import PassageEvent

    item = _items()[1]
    _write_list(tmp_path, [item], [])
    panel = ProblemListPanel()
    events = [PassageEvent(event_id=identity, bib=bib, passage_time_ms=100 + i * 50,
                           race_id="race", stage_id="stage", group_id="group", sequence=i + 1,
                           passage_timestamp_ms=T + 100 + i * 50)
              for i, (identity, bib) in enumerate(zip(item.event_ids, item.bibs))]
    panel._events_provider = lambda: events
    panel.set_workspace(tmp_path)
    panel.toggle_big_view()
    assert panel.count_table.rowCount() == 2
    assert [panel.count_table.item(row, 3).text() for row in range(2)] == [
        format_chip_time(T + 100), format_chip_time(T + 150)]
    assert [panel.count_table.item(row, 0).text() for row in range(2)] == ["待看", "待看"]
    panel.count_table.setCurrentCell(0, 0)
    assert panel.person_box.isHidden()  # 芯片读数不是已确认的画面选手。
    panel.close()


def test_enter_settles_the_counted_crossing_by_its_class(qapp, tmp_path):
    _write_list(tmp_path, _items(), [])
    panel = ProblemListPanel()
    panel.set_workspace(tmp_path)
    panel.set_browsing(False)
    panel.table.setCurrentCell(next(i for i, item in enumerate(panel.rows)
                                    if item.item_id == "two:1"), 0)

    panel.add_mark(0.2, 0.5, T)
    panel.add_mark(0.6, 0.5, T)
    two = panel.current_item()
    panel._confirm_pending()
    # Two people on one crossing with two chips are two riders side by side.
    assert panel.state(two).startswith("已处理：两个人")
    assert panel.store.get("two:1").reason == "画面点出 2 人"

    panel.table.setCurrentCell(next(i for i, item in enumerate(panel.rows)
                                    if item.item_id == "no_chip:1"), 0)
    panel.add_mark(0.2, 0.5, T)
    panel._confirm_pending()
    # Crossing evidence can be confirmed now, while the identity remains pending.
    assert panel.store.get("no_chip:1").action == "confirm_crossing"
    assert panel.state(panel.current_item()) == "待处理"
    assert panel.current_marks() and panel.export()["补录清单.csv"] == 0
    panel.bib_input.setText("307")
    panel._confirm_pending()
    assert panel.store.get("no_chip:1").bib == "307"
    assert panel.store.get("no_chip:1").reason == "画面点出 1 人"


def test_counting_several_people_keeps_each_crossing_of_its_own(qapp, tmp_path):
    """Two riders on one frame are two crossings; naming one cannot close the other."""

    _write_list(tmp_path, _items(), [])
    panel = ProblemListPanel()
    panel.set_workspace(tmp_path)
    panel.table.setCurrentCell(_row_of(panel, "no_chip:1"), 0)
    panel.add_mark(0.2, 0.5, T)
    panel.add_mark(0.6, 0.5, T)
    panel._confirm_pending()

    manual = json.loads((tmp_path / "finish_problems" / "manual_problems.json").read_text(encoding="utf-8"))
    assert len(manual) == 2 and all(value["kind"] == "manual_review" for value in manual)
    assert all(value["evidence"]["frame"] == FRAME for value in manual)
    # Each person is pending on their own, and the old question is only a reference.
    assert sum(1 for item in panel.rows if item.kind == "manual_review") == 2
    assert "no_chip:1" not in [item.item_id for item in panel.rows]
    assert panel.store.get("no_chip:1") is None
    assert panel.export()["补录清单.csv"] == 0

    # Naming one rider leaves the other crossing waiting for its own number.
    first = next(item for item in panel.rows if item.kind == "manual_review")
    panel.table.setCurrentCell(_row_of(panel, first.item_id), 0)
    panel.bib_input.setText("307")
    panel._confirm_pending()
    assert panel.store.get(first.item_id).bib == "307"
    assert panel.export()["补录清单.csv"] == 1
    other = next(item for item in panel.rows if item.kind == "manual_review")
    assert other.item_id != first.item_id
    assert panel.state(other) == "待处理"


def test_background_progress_does_not_replace_operator_validation(qapp):
    panel = ProblemListPanel()
    panel.hint.setText("请填写原因")
    panel._consume_progress(b'{"stage":"sampling","done":1,"total":2}\n')
    assert panel.hint.text() == "请填写原因"
    assert "1/2" in panel.analysis_status


def _near_crossing_item():
    return ProblemItem(
        "near:1", KIND_NEAR_CROSSING, CATEGORY_VIDEO,
        "11:01:28.309 线上另有一处变化，距芯片 366（11:01:27.809）的过线 +500 ms（认车：自行车）",
        chip_time_ms=T + 500, priority=15,
        evidence={"frame": dict(FRAME), "recorder_ms": T + 500, "gap_ms": 500,
                  "near_read": {"bib": "366", "event_id": "e-366", "chip_ms": T},
                  "members": [{"frame": dict(FRAME), "chip_time_ms": T + 500}]},
    )


def test_near_crossing_records_a_plate_and_exports_its_time_source(qapp, tmp_path):
    near = _near_crossing_item()
    _write_list(tmp_path, [near], [])
    panel = ProblemListPanel()
    panel.set_workspace(tmp_path)
    panel.table.setCurrentCell(0, 0)

    assert [button.property("problemAction") for button in panel.action_buttons] == [
        "rider_crossing", "not_rider", "same_crossing",
    ]
    assert panel.state(near) == "待处理"
    panel.bib_input.setText("307")
    panel.action_buttons[0].click()  # 是选手：号码框里有 307，就按补录走
    assert panel.state(near).startswith("待 CycleRace：认号补录")

    assert panel.export() == {"补录清单.csv": 1, "建议删除清单.csv": 0, "建议改号改圈清单.csv": 0}
    rows = list(csv.DictReader((tmp_path / "finish_problems" / "exports" / "补录清单.csv").open(encoding="utf-8-sig")))
    assert len(rows) == 1
    assert rows[0]["号码"] == "307" and "第 813 帧" in rows[0]["证据帧"]
    assert rows[0]["时间来源"] == "校时换算（视频原帧）"
    assert "附近可能另有过线" in rows[0]["说明"]


def test_one_press_decides_with_its_standing_reason(qapp, tmp_path):
    """一按就是判罚：理由跟着按压一起记下，不再让人读一遍再确认一遍。"""

    near = _near_crossing_item()
    _write_list(tmp_path, [near], [])
    panel = ProblemListPanel()
    panel.set_workspace(tmp_path)
    panel.table.setCurrentCell(0, 0)

    same = next(button for button in panel.action_buttons if button.property("problemAction") == "same_crossing")
    same.click()
    # Confirming "same crossing" closes the question without inventing a rider.
    assert panel.state(near).startswith("已处理：同一次过线")
    assert panel.store.get(near.item_id).reason == ACTION_REASON_PRESETS["same_crossing"]
    assert panel.export() == {"补录清单.csv": 0, "建议删除清单.csv": 0, "建议改号改圈清单.csv": 0}

    # The journal itself still refuses a ruling with no reason at all.
    for action in ("not_rider", "ride_back", "same_crossing"):
        with pytest.raises(ValueError, match="依据"):
            panel.store.resolve(near, action)


def _keyboard_items():
    """One row of each entry: a missed candidate, a batch, a group, two plain rows."""

    reads = [ChipRead(f"e-{index}", f"0{index}", f"chip-{index}", "g", "race group", "",
                      T + index * 20, index * 20, 0, T + index * 20, 1, 1, True, False)
             for index in range(30, 38)]
    batch = next(item for item in aggregate_batches(chip_check_items(reads), read_snapshots(reads),
                                                   ["", ""], ())
                 if item.evidence.get("batch_members"))
    return [
        _near_crossing_item(),
        batch,
        ProblemItem("bunch:1", KIND_BUNCH, CATEGORY_VIDEO, "集团核对", chip_time_ms=T + 2_000,
                    event_ids=("e-040",), evidence={"frame": dict(FRAME)}),
        ProblemItem("unknown:1", KIND_UNKNOWN_BIB, CATEGORY_VIDEO, "号码无法核实", chip_time_ms=T + 3_000,
                    bibs=("099",), event_ids=("e-099",), evidence={"frame": dict(FRAME)}),
        ProblemItem("unknown:2", KIND_UNKNOWN_BIB, CATEGORY_VIDEO, "无名无帧", chip_time_ms=T + 4_000,
                    bibs=("098",), event_ids=("e-098",), evidence={}),
    ]


def _row_of(panel, item_id):
    return next(index for index, item in enumerate(panel.rows) if item.item_id == item_id)


def test_f_opens_the_judging_column_beside_the_big_picture(qapp, tmp_path):
    """F is the key the operators already use: the picture grows, the queue steps aside."""

    _write_list(tmp_path, _keyboard_items(), [])
    panel = ProblemListPanel()
    jumps = []
    panel.jump_requested.connect(jumps.append)
    panel.set_workspace(tmp_path)
    near = panel.rows[_row_of(panel, "near:1")]

    panel.table.setCurrentCell(_row_of(panel, "near:1"), 0)
    QTest.keyClick(panel, Qt.Key_F)
    assert jumps == [FRAME]
    assert not panel.table.isVisibleTo(panel)
    # 过线顺序和芯片时间在同一张表里；名单不再与另一张芯片表来回对。
    assert panel.detail_scroll.isVisibleTo(panel) and panel.decision_box.isVisibleTo(panel)
    assert panel.big_help.isVisibleTo(panel) and panel.side_notice.isVisibleTo(panel)
    assert panel.count_table.isVisibleTo(panel) and not hasattr(panel, "chip_table")
    assert [panel.count_table.horizontalHeaderItem(i).text() for i in range(4)] == [
        "顺序", "过线时间", "号码", "芯片时间"]
    assert panel.undo_button.isHidden() and panel.clear_button.isHidden()
    assert not panel.header.isVisibleTo(panel) and panel.big_keys.isVisibleTo(panel)
    # 卡片图片让位给名单，放大以后看的就是人。
    assert not panel.card.isVisibleTo(panel) and not panel.filmstrip.isVisibleTo(panel)

    # Looking and deciding happen in the same view: one digit is the decision.
    QTest.keyClick(panel, Qt.Key_3)  # 同一次过线
    assert panel.state(near).startswith("已处理：同一次过线")
    assert panel.store.get(near.item_id).reason == ACTION_REASON_PRESETS["same_crossing"]
    assert not panel.table.isVisibleTo(panel)  # deciding keeps the picture up

    # The same key is the way back, and the column returns to the queue.
    QTest.keyClick(panel, Qt.Key_F)
    assert panel.table.isVisibleTo(panel) and panel.header.isVisibleTo(panel)
    assert panel.card.isVisibleTo(panel) and panel.decision_box.isVisibleTo(panel)
    assert not panel.count_table.isVisibleTo(panel) and not panel.big_keys.isVisibleTo(panel)

    # Esc is the same way back, so a quick look never traps the queue.
    panel.table.setCurrentCell(_row_of(panel, "unknown:1"), 0)
    QTest.keyClick(panel, Qt.Key_F)
    assert len(jumps) == 2 and not panel.table.isVisibleTo(panel)
    QTest.keyClick(panel, Qt.Key_Escape)
    assert panel.table.isVisibleTo(panel)

    # A row with no frame still opens on F: the press must never look dead, and the
    # big screen says why it has no picture instead of jumping to one that is not this
    # crossing's.
    batch_row = next(index for index, item in enumerate(panel.rows) if item.evidence.get("batch_members"))
    assert not panel.rows[batch_row].evidence.get("frame")
    panel.table.setCurrentCell(batch_row, 0)
    QTest.keyClick(panel, Qt.Key_F)
    assert len(jumps) == 2 and not panel.table.isVisibleTo(panel)
    assert "没有原帧" in panel.big_title.text()
    QTest.keyClick(panel, Qt.Key_F)
    assert panel.table.isVisibleTo(panel)

    # Enter with nothing standing says what it is for instead of guessing.
    panel.hint.clear()
    QTest.keyClick(panel, Qt.Key_Return)
    assert "回车确认" in panel.hint.text()


def test_digits_press_the_decision_buttons_of_the_row_on_screen(qapp, tmp_path):
    """Digits run down the decisions of the row on screen, in the order they are shown."""

    _write_list(tmp_path, [_near_crossing_item()], [])
    panel = ProblemListPanel()
    panel.set_workspace(tmp_path)
    panel.table.setCurrentCell(0, 0)
    assert [button.property("problemAction") for button in panel.action_buttons] == [
        "rider_crossing", "not_rider", "same_crossing",
    ]

    # One digit is the whole decision: the standing reason goes in with it.
    near = panel.current_item()
    QTest.keyClick(panel, Qt.Key_3)
    assert panel.store.get(near.item_id).action == "same_crossing"
    assert panel.store.get(near.item_id).reason == ACTION_REASON_PRESETS["same_crossing"]
    assert panel.state(near).startswith("已处理：同一次过线")

    # A digit with no button behind it decides nothing.
    panel.hint.clear()
    QTest.keyClick(panel, Qt.Key_9)
    assert panel.hint.text() == ""


def test_enter_answers_the_rows_first_button(qapp, tmp_path):
    """回车就是第一颗按钮：号码填好了就是补录，不用再按一次选它。"""

    _write_list(tmp_path, [_near_crossing_item()], [])
    panel = ProblemListPanel()
    panel.set_workspace(tmp_path)
    panel.table.setCurrentCell(0, 0)

    panel.bib_input.setText("307")
    QTest.keyClick(panel, Qt.Key_Return)
    assert panel.store.get("near:1").action == "record_bib"
    assert panel.store.get("near:1").bib == "307"
