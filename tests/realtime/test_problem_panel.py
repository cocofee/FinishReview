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
    _write_list(tmp_path, _items(), [{"bib": "057", "frame": dict(FRAME), "source": "auto"}])
    panel = ProblemListPanel()
    jumps = []
    panel.jump_requested.connect(jumps.append)
    panel.set_workspace(tmp_path)

    assert panel.summary.text() == "自动通过 1 人 · 待处理 2 条 · 无法核实 3 人 · 已处理 1 条"
    assert panel.table.rowCount() == 3  # two pending plus the reference list
    panel.show_closed.setChecked(True)
    assert panel.table.rowCount() == 5
    panel.show_closed.setChecked(False)

    panel.table.setCurrentCell(0, 0)
    assert not panel.card.pixmap().isNull()
    assert not panel.filmstrip.pixmap().isNull()
    panel.jump_button.click()
    assert jumps == [FRAME]

    record = next(button for button in panel.action_buttons if button.property("problemAction") == "record_bib")
    record.click()
    assert "号码" in panel.hint.text()
    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *args: asked.append(args[2]) or QMessageBox.No)
    panel.bib_input.setText("067")
    record.click()
    assert asked and "057" in asked[0]
    assert panel.store.get("no_chip:1") is None
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.Yes)
    panel.bib_input.setText("067")
    record.click()
    assert panel.store.get("no_chip:1").bib == "067"

    panel.table.setCurrentCell(0, 0)
    assert panel.current_item().item_id == "two:1"
    delete = next(button for button in panel.action_buttons if button.property("problemAction") == "delete_extra_chip")
    panel.bib_input.setText("020")
    delete.click()
    assert "原因" in panel.hint.text()
    panel.bib_input.setText("020")
    panel.reason_input.setText("一人带两块芯片")
    delete.click()
    assert panel.summary.text().startswith("自动通过 1 人 · 待处理 0 条")

    counts = panel.export()
    assert counts == {"补录清单.csv": 1, "建议删除清单.csv": 1, "建议改号改圈清单.csv": 0}
    rows = list(csv.reader((tmp_path / "finish_problems" / "exports" / "建议删除清单.csv").open(encoding="utf-8-sig")))
    assert rows[1][0] == "020" and rows[1][2] == "e-020" and rows[1][4] == "一人带两块芯片"
    manual = list(csv.reader((tmp_path / "finish_problems" / "exports" / "补录清单.csv").open(encoding="utf-8-sig")))
    assert manual[1][0] == "067" and "第 813 帧" in manual[1][2]


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
    _write_list(tmp_path, _items(), [])
    panel = ProblemListPanel()
    panel.set_workspace(tmp_path)
    panel._hold()
    assert not panel._held
    panel.reason_input.setText("wait for judge")
    panel._hold()
    assert panel.store.get("no_chip:1") is None
    assert "no_chip:1" not in [item.item_id for item in panel.rows]
    panel.queue_filter.setCurrentIndex(2)
    assert panel.current_item().item_id == "no_chip:1"
    panel._hold()
    panel.queue_filter.setCurrentIndex(0)
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
        "record_bib", "same_crossing", "not_rider", "ride_back", "insufficient",
    ]
    assert panel.state(near) == "待处理"
    panel.bib_input.setText("307")
    record = next(button for button in panel.action_buttons if button.property("problemAction") == "record_bib")
    record.click()
    assert panel.state(near).startswith("待 CycleRace：认号补录")

    assert panel.export() == {"补录清单.csv": 1, "建议删除清单.csv": 0, "建议改号改圈清单.csv": 0}
    rows = list(csv.DictReader((tmp_path / "finish_problems" / "exports" / "补录清单.csv").open(encoding="utf-8-sig")))
    assert len(rows) == 1
    assert rows[0]["号码"] == "307" and "第 813 帧" in rows[0]["证据帧"]
    assert rows[0]["时间来源"] == "校时换算（视频原帧）"
    assert "附近可能另有过线" in rows[0]["说明"]


def test_excluding_or_confirming_a_crossing_requires_a_written_reason(qapp, tmp_path):
    near = _near_crossing_item()
    _write_list(tmp_path, [near], [])
    panel = ProblemListPanel()
    panel.set_workspace(tmp_path)
    panel.table.setCurrentCell(0, 0)

    same = next(button for button in panel.action_buttons if button.property("problemAction") == "same_crossing")
    same.click()
    assert "依据" in panel.hint.text() and panel.state(near) == "待处理"

    for action in ("not_rider", "ride_back", "same_crossing"):
        with pytest.raises(ValueError, match="依据"):
            panel.store.resolve(near, action)

    panel.reason_input.setText("同一人后轮，前轮已在同一帧压线")
    same.click()
    # Confirming "same crossing" closes the question without inventing a rider.
    assert panel.state(near).startswith("已处理：确认是同一次过线")
    assert panel.export() == {"补录清单.csv": 0, "建议删除清单.csv": 0, "建议改号改圈清单.csv": 0}


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


def test_enter_opens_the_tool_of_the_row_on_screen(qapp, tmp_path, monkeypatch):
    """The queue is worked one row after another, in time order, mouse untouched."""

    _write_list(tmp_path, _keyboard_items(), [])
    panel = ProblemListPanel()
    jumps = []
    panel.jump_requested.connect(jumps.append)
    panel.set_workspace(tmp_path)
    calls = []
    monkeypatch.setattr(panel, "_open_batch", lambda: calls.append("batch"))
    monkeypatch.setattr(panel, "_open_bunch", lambda: calls.append("bunch"))
    monkeypatch.setattr(panel, "_open_missed", lambda: calls.append("missed"))
    batch_row = next(index for index, item in enumerate(panel.rows) if item.evidence.get("batch_members"))

    for row, expected in ((_row_of(panel, "near:1"), "missed"), (batch_row, "batch"),
                          (_row_of(panel, "bunch:1"), "bunch")):
        panel.table.setCurrentCell(row, 0)
        before = len(calls)
        QTest.keyClick(panel.table, Qt.Key_Return)  # the table itself holds the focus
        assert calls[before:] == [expected]
        before = len(calls)
        QTest.keyClick(panel, Qt.Key_Return)  # a button or the panel holds it
        assert calls[before:] == [expected]

    # A row without a tool is looked at, and one without a frame says so.
    panel.table.setCurrentCell(_row_of(panel, "unknown:1"), 0)
    QTest.keyClick(panel.table, Qt.Key_Return)
    assert jumps[-1] == FRAME
    panel.table.setCurrentCell(_row_of(panel, "unknown:2"), 0)
    panel.hint.clear()
    QTest.keyClick(panel.table, Qt.Key_Return)
    assert "没有工具也没有原帧" in panel.hint.text() and len(jumps) == 1


def test_digits_press_the_decision_buttons_of_the_row_on_screen(qapp, tmp_path):
    """Digits run down the decisions of the row on screen, in the order they are shown."""

    _write_list(tmp_path, [_near_crossing_item()], [])
    panel = ProblemListPanel()
    panel.set_workspace(tmp_path)
    panel.table.setCurrentCell(0, 0)
    assert [button.property("problemAction") for button in panel.action_buttons][:2] == ["record_bib",
                                                                                         "same_crossing"]

    QTest.keyClick(panel, Qt.Key_2)
    assert "依据" in panel.hint.text() and not panel.store.resolutions()
    QTest.keyClick(panel, Qt.Key_1)
    assert "号码" in panel.hint.text()

    # A digit with no button behind it decides nothing.
    panel.hint.clear()
    QTest.keyClick(panel, Qt.Key_9)
    assert panel.hint.text() == ""

    near = panel.current_item()
    panel.reason_input.setText("同一人后轮，前轮已在同一帧压线")
    QTest.keyClick(panel, Qt.Key_2)
    assert panel.state(near).startswith("已处理：确认是同一次过线")
