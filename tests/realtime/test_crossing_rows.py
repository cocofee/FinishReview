from realtime.bunch_review import BunchChip
from realtime.crossing_order import mark_order_labels
from realtime.crossing_rows import crossing_rows


T = 1_789_784_000_000


def test_backtracking_changes_display_order_without_rewriting_saved_marks():
    marks = [{"recorder_ms": T + 300, "bib": "103"},
             {"recorder_ms": T + 100}, {"recorder_ms": T + 100},
             {"recorder_ms": T + 200, "exclusion_reason": "行人"}]
    chips = [BunchChip(str(i), str(101 + i), T + t - 30, T + t)
             for i, t in enumerate((100, 100, 300, 500))]
    untouched = [dict(mark) for mark in marks]
    rows = crossing_rows(marks, chips)
    people = [row for row in rows if row["mark_index"] is not None]
    assert [row["mark_index"] for row in people] == [1, 2, 3, 0]
    assert [row["order"] for row in people] == ["1 同时", "1 同时", "排除", "3"]
    assert people[-1]["chip_index"] == 2
    assert all(row["reference"] for row in people[:2])
    assert not people[2]["reference"]
    assert rows[-1]["order"] == "待看" and rows[-1]["mark_index"] is None
    assert marks == untouched


def test_a_time_reference_does_not_identify_an_untyped_person():
    marks = [{"recorder_ms": T + 100}]
    chips = [BunchChip("a", "057", T, T + 100)]
    row = crossing_rows(marks, chips)[0]
    assert row["reference"] and row["chip_index"] == 0
    assert "bib" not in marks[0]
    assert crossing_rows(marks, chips, time_reference=False)[0]["chip_index"] is None


def test_excluded_people_do_not_occupy_rider_positions():
    marks = [{"recorder_ms": T + 50, "exclusion_reason": "工作人员"},
             {"recorder_ms": T + 100}, {"recorder_ms": T + 200}]
    assert mark_order_labels(marks) == {0: "排除", 1: "1", 2: "2"}
