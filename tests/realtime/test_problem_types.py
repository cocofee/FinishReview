"""The counting row of the workbench: one class per kind, one row per item.

The three classes the operator works through (multi-chip, group, missed read)
plus the chip-record checks and the evidence-blocked items.  A kind that fell
through to a default would disappear from the counts while still sitting in the
queue, so the mapping is complete and tested.
"""

import json

import pytest
from PyQt5.QtWidgets import QApplication

from realtime import problem_items as kinds_module
from realtime.problem_items import (
    CATEGORY_CHIP,
    CATEGORY_VIDEO,
    KIND_BEFORE_GUN,
    KIND_BUNCH,
    KIND_NEAR_CROSSING,
    KIND_NOT_ARRIVED,
    KIND_TWO_CHIPS_ONE_CROSSING,
    KIND_WAITING,
    KIND_TITLES,
    KIND_TYPES,
    STATUS_CLOSED_UPSTREAM,
    STATUS_INFO,
    STATUS_WAITING,
    TYPE_BUNCH,
    TYPE_EVIDENCE,
    TYPE_MISSED,
    TYPE_MULTI_CHIP,
    TYPE_ORDER,
    TYPE_OTHER,
    TYPE_RECORD,
    TYPE_TITLES,
    ProblemItem,
)
from realtime.problem_panel import ProblemListPanel

T = 1_789_784_000_000
FRAME = {"camera_index": 1, "segment_id": "seg", "video_path": "videos/a.mkv", "frame_index": 812,
         "position_ms": 40_600, "session_id": "s", "calibration_version": 2}


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


def test_every_kind_has_one_class_and_no_kind_falls_through():
    declared = {getattr(kinds_module, name) for name in dir(kinds_module)
                if name.startswith("KIND_") and name not in ("KIND_TITLES", "KIND_TYPES", "KIND_ACTIONS")}

    assert declared == set(KIND_TYPES), "每个问题类型都要显式归类，不能靠默认值兜底"
    assert declared <= set(KIND_TITLES), "每个类型都要有给操作员看的标题"
    assert set(KIND_TYPES.values()) <= set(TYPE_ORDER)
    assert TYPE_OTHER not in set(KIND_TYPES.values())
    assert set(TYPE_TITLES) == set(TYPE_ORDER)


def test_classes_group_the_three_task_kinds_and_the_evidence_wait():
    assert KIND_TYPES[KIND_TWO_CHIPS_ONE_CROSSING] == TYPE_MULTI_CHIP
    assert KIND_TYPES[KIND_BUNCH] == TYPE_BUNCH
    assert KIND_TYPES[KIND_NEAR_CROSSING] == TYPE_MISSED
    assert KIND_TYPES[KIND_BEFORE_GUN] == TYPE_RECORD
    assert KIND_TYPES[KIND_WAITING] == TYPE_EVIDENCE
    item = ProblemItem("near:1", KIND_NEAR_CROSSING, CATEGORY_VIDEO, "附近可能另有过线")
    assert item.task_type == TYPE_MISSED
    # An unknown kind stays visible instead of being filed under a wrong class.
    assert ProblemItem("x:1", "kind-from-a-newer-build", CATEGORY_VIDEO, "").task_type == TYPE_OTHER


def _items():
    return [
        ProblemItem("near:1", KIND_NEAR_CROSSING, CATEGORY_VIDEO, "线上另有一处变化",
                    chip_time_ms=T, evidence={"frame": dict(FRAME)}),
        ProblemItem("two:1", KIND_TWO_CHIPS_ONE_CROSSING, CATEGORY_VIDEO, "020 与 023 同一次过线",
                    chip_time_ms=T + 1_000, bibs=("020", "023"), evidence={"frame": dict(FRAME, frame_index=900)}),
        ProblemItem("bunch:1", KIND_BUNCH, CATEGORY_VIDEO, "2 秒内自动计数 6 次过线，只对上 3 条芯片",
                    chip_time_ms=T + 2_000, evidence={"frame": dict(FRAME, frame_index=950),
                                                      "camera_index": 1, "recorder_start_ms": T + 2_000,
                                                      "recorder_end_ms": T + 8_000}),
        ProblemItem("gun:1", KIND_BEFORE_GUN, CATEGORY_CHIP, "早于发枪", chip_time_ms=T + 3_000),
        ProblemItem("wait:1", KIND_WAITING, CATEGORY_VIDEO, "等待已封存录像", chip_time_ms=T + 4_000,
                    status=STATUS_WAITING),
        ProblemItem("missing:1", KIND_NOT_ARRIVED, "coverage", "女子组 应到未到 2 人",
                    bibs=("511", "512"), status=STATUS_INFO),
        ProblemItem("closed:1", KIND_TWO_CHIPS_ONE_CROSSING, CATEGORY_VIDEO, "已作废", chip_time_ms=T + 5_000,
                    status=STATUS_CLOSED_UPSTREAM),
    ]


def _workspace(tmp_path, auto_passed):
    directory = tmp_path / "finish_problems"
    directory.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": 1, "summary": {"auto_passed": len(auto_passed), "unverifiable": 2},
               "items": [item.to_payload() for item in _items()], "auto_passed": auto_passed}
    (directory / "problem_list.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return tmp_path


def test_counting_row_counts_each_class_once_and_ignores_reference_items(qapp, tmp_path):
    _workspace(tmp_path, [{"bib": "057", "frame": dict(FRAME), "source": "auto"}])
    panel = ProblemListPanel()
    panel.set_workspace(tmp_path)

    counts = panel.type_counts()
    assert counts[TYPE_MULTI_CHIP] == 1 and counts[TYPE_BUNCH] == 1 and counts[TYPE_MISSED] == 1
    assert counts[TYPE_RECORD] == 1 and counts[TYPE_EVIDENCE] == 1 and counts[TYPE_OTHER] == 0
    assert panel.type_buttons[TYPE_MULTI_CHIP].text() == "多芯片疑点 1"
    assert panel.type_buttons[TYPE_MISSED].text() == "漏读疑点 1"
    # Reference and closed items are shown in the queue but are nobody's task.
    assert panel.type_buttons[TYPE_OTHER].isHidden()
    assert panel.auto_label.text() == "无需人工处理 1 人"
    assert panel.evidence_gap_text() == "证据缺口 1 条待证据 · 2 人无录像可核"


def test_a_match_inside_a_pending_group_is_counted_but_still_flagged(qapp, tmp_path):
    _workspace(tmp_path, [{"bib": "057", "frame": dict(FRAME), "source": "auto"},
                          {"bib": "058", "frame": dict(FRAME, frame_index=951), "source": "auto",
                           "pending_period": "bunch:1", "pending_period_note": "集团核对：2 秒内自动计数"}])
    panel = ProblemListPanel()
    panel.set_workspace(tmp_path)

    assert panel.auto_label.text() == "无需人工处理 2 人（其中 1 人落在待核集团/遮挡时段内，不代表该时段已核清）"


def test_clicking_a_class_filters_the_queue_to_it_and_clicking_again_returns(qapp, tmp_path):
    _workspace(tmp_path, [])
    panel = ProblemListPanel()
    panel.set_workspace(tmp_path)
    assert len(panel.rows) == 5  # four open tasks plus the reference list

    panel.type_buttons[TYPE_MISSED].click()
    assert panel._type_filter == TYPE_MISSED and panel.type_buttons[TYPE_MISSED].isChecked()
    assert [item.item_id for item in panel.rows] == ["near:1"]

    panel.type_buttons[TYPE_MISSED].click()
    assert panel._type_filter == "" and not panel.type_buttons[TYPE_MISSED].isChecked()
    assert len(panel.rows) == 5

    # The class filter narrows by class; the state row still decides what is shown,
    # so the reference entry of the same class appears with the open one.
    panel.type_buttons[TYPE_RECORD].click()
    assert [item.item_id for item in panel.rows] == ["gun:1", "missing:1"]
    # The class row and the state row are one filter, not two competing ones.
    panel.queue_filter.setCurrentIndex(4)
    assert panel._type_filter == "" and len(panel.rows) == 7


def test_the_evidence_class_shows_what_only_waits_for_evidence(qapp, tmp_path):
    _workspace(tmp_path, [])
    panel = ProblemListPanel()
    panel.set_workspace(tmp_path)

    panel.type_buttons[TYPE_EVIDENCE].click()
    assert panel.queue_filter.currentIndex() == 1
    assert [item.item_id for item in panel.rows] == ["wait:1"]
    assert not panel.action_buttons


def test_each_class_opens_its_own_tool_from_the_problem_itself(qapp, tmp_path):
    _workspace(tmp_path, [])
    panel = ProblemListPanel()
    panel.set_workspace(tmp_path)
    jumps = []
    panel.jump_requested.connect(jumps.append)

    def select(item_id):
        panel.table.setCurrentCell(next(i for i, item in enumerate(panel.rows) if item.item_id == item_id), 0)

    select("near:1")
    assert panel.missed_button.text() == "查看候选证据"
    assert panel.missed_button.isVisibleTo(panel) and panel.bunch_button.isHidden()
    panel.missed_button.click()
    assert jumps == [FRAME] and "保留待证据" in panel.hint.text()

    select("bunch:1")
    assert panel.bunch_button.text() == "打开集团逐帧核对"
    assert panel.bunch_button.isVisibleTo(panel) and panel.missed_button.isHidden()

    select("gun:1")
    # The record class is decided in place: no class tool claims it.
    assert panel.missed_button.isHidden() and panel.bunch_button.isHidden()
    assert [button.property("problemAction") for button in panel.action_buttons]


def test_the_html_prototype_carries_the_same_classes_and_entries():
    """The prototype is the interaction reference; its words must not drift apart."""

    from pathlib import Path

    from realtime.problem_items import TYPE_ENTRY_LABELS

    prototype = (Path(__file__).resolve().parents[2] / "ui-prototype" / "live-review-workbench.html"
                 ).read_text(encoding="utf-8")
    for value in (TYPE_MULTI_CHIP, TYPE_BUNCH, TYPE_MISSED, TYPE_RECORD, TYPE_EVIDENCE):
        assert TYPE_TITLES[value] in prototype, value
    for label in TYPE_ENTRY_LABELS.values():
        assert label in prototype, label
    # The counting row and the member tool of the batch class are part of the prototype.
    assert 'id="typeCounts"' in prototype and 'id="memberTools"' in prototype
    assert 'class="auto-count"' in prototype and 'id="gapHealth"' in prototype
