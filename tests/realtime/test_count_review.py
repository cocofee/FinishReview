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
