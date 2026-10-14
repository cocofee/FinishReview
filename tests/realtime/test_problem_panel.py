import csv
import json
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
from PyQt5.QtWidgets import QApplication, QMessageBox

from realtime.problem_items import (
    CATEGORY_COVERAGE,
    CATEGORY_VIDEO,
    KIND_NO_CHIP_CROSSING,
    KIND_NOT_ARRIVED,
    KIND_TWO_CHIPS_ONE_CROSSING,
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
