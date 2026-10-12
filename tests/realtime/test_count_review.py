import json

import pytest
from PyQt5.QtWidgets import QApplication

from realtime.count_review import CountReviewDialog, read_count_report


def report():
    def window(uid, status):
        return {"window_id": uid, "start_ms": 1000, "end_ms": 1100, "status": status,
                "video_count": 2, "chip_count": 1, "pending_count": 0, "excluded_count": 0,
                "reasons": ["count_difference"], "evidence": {}}
    return {"schema_version": 1, "scope": {"race_id": "race", "stage_id": "stage", "timing_position": "finish", "source_id": "cam"},
            "updated_at_ms": 1000, "cutoff_ms": 2000, "chip_watermark_ms": None,
            "groups": [window("manual", "unresolved"), window("auto", "rescanning"), window("wait", "waiting")]}


def test_report_rejects_other_race_and_stage(tmp_path):
    path = tmp_path / "count_reconciliation.json"
    path.write_text(json.dumps(report()), encoding="utf-8")
    with pytest.raises(ValueError, match="不一致"):
        read_count_report(path, race_id="other")
    assert len(read_count_report(path, race_id="race", stage_id="stage")["groups"]) == 3


@pytest.mark.parametrize("payload", [None, [], 1, "invalid", {"schema_version": 1, "scope": None, "groups": []}])
def test_report_rejects_invalid_object_structure(tmp_path, payload):
    path = tmp_path / "count_reconciliation.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="必须是对象"):
        read_count_report(path, race_id="race", stage_id="stage")


@pytest.mark.parametrize("field,value", [
    ("clock", None), ("clock", {"calibrated": "false"}),
    ("updated_at_ms", "now"), ("cutoff_ms", 10**100), ("runtime_quality", [None]),
    ("groups", [None]),
])
def test_report_rejects_invalid_runtime_fields(tmp_path, field, value):
    data = report()
    data[field] = value
    path = tmp_path / "count_reconciliation.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        read_count_report(path)


@pytest.mark.parametrize("field,value", [
    ("video_count", -1), ("chip_count", True), ("start_ms", "1000"), ("end_ms", 900),
    ("status", []), ("reasons", [None]), ("chip_event_ids", [{}]),
    ("evidence", None), ("evidence", {"keyframes": "image.jpg"}),
    ("rescan", None), ("chip_passages", [None]),
    ("chip_passages", [{"passage_timestamp_ms": "1000"}]),
])
def test_report_rejects_invalid_group_before_either_ui_renders_it(tmp_path, field, value):
    data = report()
    data["groups"][0][field] = value
    path = tmp_path / "count_reconciliation.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        read_count_report(path)


def test_operator_defaults_to_remaining_windows_and_can_inspect_waiting_counts(tmp_path):
    app = QApplication.instance() or QApplication([])
    path = tmp_path / "count_reconciliation.json"
    path.write_text(json.dumps(report()), encoding="utf-8")
    dialog = CountReviewDialog(tmp_path, race_id="race", stage_id="stage")
    try:
        assert dialog.table.rowCount() == 1
        assert "人工未决 1 窗" in dialog.summary.text()
        assert "暂定" in dialog.summary.text()
        dialog.remaining_only.setChecked(False)
        assert dialog.table.rowCount() == 3
        dialog.table.selectRow(0)
        assert not dialog.clip_button.isEnabled()
        assert "证据不足" in dialog.evidence_hint.text()
    finally:
        dialog.close()
        dialog.deleteLater()
        app.processEvents()


def test_selected_window_shows_chip_roster_and_late_refresh_clears_old_identity(tmp_path):
    app = QApplication.instance() or QApplication([])
    path = tmp_path / "count_reconciliation.json"
    data = report()
    row = data["groups"][0]
    row["chip_passages"] = [{"event_id": "c1", "bib": "21", "athlete_name": "Rider One",
                              "group_name": "Open", "chip_id": "chip-21", "lap": 2, "passage_timestamp_ms": 1000}]
    path.write_text(json.dumps(data), encoding="utf-8")
    dialog = CountReviewDialog(tmp_path, race_id="race", stage_id="stage")
    try:
        dialog.table.selectRow(0)
        assert dialog.chip_table.rowCount() == 1
        assert dialog.chip_table.item(0, 1).text() == "21"
        assert dialog.chip_table.item(0, 2).text() == "Rider One"
        assert "视频多 1 次" in dialog.passage_hint.text()
        assert "未与匿名视频逐人绑定" in dialog.passage_hint.text()
        row["chip_passages"] = []
        row["chip_count"] = 0
        path.write_text(json.dumps(data), encoding="utf-8")
        dialog.refresh()
        assert dialog.chip_table.rowCount() == 0
        assert "视频多 2 次" in dialog.passage_hint.text()
        path.write_text("invalid", encoding="utf-8")
        dialog.refresh()
        assert dialog.chip_table.rowCount() == 0
        assert "选择短窗" in dialog.passage_hint.text()
    finally:
        dialog.close()
        dialog.deleteLater()
        app.processEvents()
