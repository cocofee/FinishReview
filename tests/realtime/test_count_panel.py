import json
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication

from realtime.count_panel import CountReconciliationPanel


def report():
    return {"schema_version": 1, "scope": {"race_id": "race", "stage_id": "stage", "source_id": "cam"},
            "clock": {"calibrated": True}, "updated_at_ms": int(time.time() * 1000), "chip_watermark_ms": None,
            "groups": [{"window_id": "g1", "start_ms": 1000, "end_ms": 1100,
                        "video_count": 2, "chip_count": 1, "pending_count": 0, "excluded_count": 0,
                        "status": "unresolved", "reasons": ["count_difference"],
                        "video_event_uids": ["v1", "v2"], "chip_event_ids": ["c1"]}]}


def test_inline_group_selection_and_late_match_release_native_scope(tmp_path):
    app = QApplication.instance() or QApplication([])
    path = tmp_path / "count_reconciliation.json"
    data = report()
    path.write_text(json.dumps(data), encoding="utf-8")
    panel = CountReconciliationPanel(tmp_path)
    selected, cleared, located = [], [], []
    panel.group_selected.connect(selected.append)
    panel.scope_cleared.connect(lambda: cleared.append(True))
    panel.locate_requested.connect(located.append)
    try:
        panel.set_context(tmp_path, "race", "stage")
        panel.refresh()
        panel.table.setCurrentCell(0, 0)
        assert selected[-1]["chip_event_ids"] == ["c1"]
        panel.locate_button.click()
        assert located[-1]["window_id"] == "g1"
        data["groups"][0].update(window_id="revised", chip_count=2, chip_event_ids=["c1", "c2"])
        path.write_text(json.dumps(data), encoding="utf-8")
        panel.refresh()
        assert selected[-1]["chip_event_ids"] == ["c1", "c2"]
        data["groups"][0]["status"] = "matched"
        path.write_text(json.dumps(data), encoding="utf-8")
        panel.refresh()
        assert panel.table.rowCount() == 0
        assert cleared == [True]
        assert not panel.locate_button.isEnabled()
        panel.only_anomalies.setChecked(False)
        assert panel.table.rowCount() == 1
    finally:
        panel.close()
        panel.deleteLater()
        app.processEvents()


def test_bad_report_or_new_race_clears_old_counts_and_selection(tmp_path):
    app = QApplication.instance() or QApplication([])
    path = tmp_path / "count_reconciliation.json"
    data = report()
    path.write_text(json.dumps(data), encoding="utf-8")
    panel = CountReconciliationPanel(tmp_path)
    cleared = []
    panel.scope_cleared.connect(lambda: cleared.append(True))
    try:
        panel.set_context(tmp_path, "race", "stage")
        panel.refresh()
        panel.table.setCurrentCell(0, 0)
        panel.set_context(tmp_path, "another", "stage")
        panel.refresh()
        assert panel.table.rowCount() == 0
        assert panel.report is None
        assert "等待" in panel.summary.text()
        assert cleared == [True]
        path.write_text("bad JSON", encoding="utf-8")
        panel.refresh()
        assert not panel.locate_button.isEnabled()
    finally:
        panel.close()
        panel.deleteLater()
        app.processEvents()


def test_uncalibrated_or_stale_count_report_is_not_presented_as_live_confirmation(tmp_path):
    app = QApplication.instance() or QApplication([])
    path = tmp_path / "count_reconciliation.json"
    data = report()
    data["clock"]["calibrated"] = False
    data["updated_at_ms"] = 0
    path.write_text(json.dumps(data), encoding="utf-8")
    panel = CountReconciliationPanel(tmp_path)
    try:
        panel.set_context(tmp_path, "race", "stage")
        panel.refresh()
        panel.table.setCurrentCell(0, 0)
        assert "更新已停止" in panel.summary.text()
        assert "校时待确认" in panel.summary.text()
        assert not panel.locate_button.isEnabled()
        assert "暂不按时间跳转" in panel.hint.text()
    finally:
        panel.close()
        panel.deleteLater()
        app.processEvents()


def test_resolved_case_keeps_original_chip_difference_visible(tmp_path):
    app = QApplication.instance() or QApplication([])
    data = report()
    data["groups"][0].update(status="resolved", video_count=1, chip_count=1,
                             initial_video_count=1, initial_chip_count=2,
                             chip_changes=[{"kind": "withdrawn", "event_id": "extra", "bib": "020"}])
    data["groups"].append({**data["groups"][0], "window_id": "historical", "history_only": True})
    (tmp_path / "count_reconciliation.json").write_text(json.dumps(data), encoding="utf-8")
    panel = CountReconciliationPanel(tmp_path)
    try:
        panel.set_context(tmp_path, "race", "stage")
        panel.refresh()
        assert panel.table.rowCount() == 2
        assert "视频 1 · 芯片 1" in panel.summary.text()
        assert "已处理 2" in panel.summary.text()
        panel.table.setCurrentCell(0, 0)
        assert "原始视频 1 / 芯片 2" in panel.hint.text()
        assert "已撤销 020" in panel.hint.text()
        assert "已处理" in panel.table.item(0, 6).text()
    finally:
        panel.close()
        panel.deleteLater()
        app.processEvents()


def test_count_review_updates_panel_and_new_observations_invalidate_it(tmp_path):
    from realtime.count_history import observation_key
    app = QApplication.instance() or QApplication([])
    data = report()
    path = tmp_path / "count_reconciliation.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    row = data["groups"][0]
    review = {"scope": data["scope"], "window_id": row["window_id"], "verified_video_count": 1,
              "observation_key": observation_key(data["scope"], row)}
    (tmp_path / "count_reviews.jsonl").write_text(json.dumps(review)+"\n", encoding="utf-8")
    panel = CountReconciliationPanel(tmp_path)
    try:
        panel.set_context(tmp_path, "race", "stage")
        panel.refresh()
        assert panel.report["groups"][0]["status"] == "resolved"
        panel.table.setCurrentCell(0, 0)
        assert "核片人数 1" in panel.hint.text()
        assert panel.verify_button.isEnabled()
        row.update(video_count=3, video_event_uids=["new"])
        path.write_text(json.dumps(data), encoding="utf-8")
        panel.refresh()
        assert panel.report["groups"][0]["video_count"] == 3
        assert panel.report["groups"][0]["status"] == "unresolved"
        assert "manual_review_stale" in panel.report["groups"][0]["reasons"]
    finally:
        panel.close()
        panel.deleteLater()
        app.processEvents()


@pytest.mark.parametrize("payload", [None, [], {"schema_version": 1, "scope": None, "groups": []},
                                     {**report(), "groups": [None]}, {**report(), "clock": None}])
def test_invalid_report_structure_clears_selection_and_recovers(tmp_path, payload):
    app = QApplication.instance() or QApplication([])
    path = tmp_path / "count_reconciliation.json"
    path.write_text(json.dumps(report()), encoding="utf-8")
    panel = CountReconciliationPanel(tmp_path)
    cleared = []
    panel.scope_cleared.connect(lambda: cleared.append(True))
    try:
        panel.set_context(tmp_path, "race", "stage")
        panel.refresh()
        panel.table.setCurrentCell(0, 0)
        path.write_text(json.dumps(payload), encoding="utf-8")
        panel.refresh()
        assert panel.report is None
        assert panel.table.rowCount() == 0
        assert cleared == [True]
        assert not panel.locate_button.isEnabled()
        assert not panel.discovery_button.isEnabled()
        assert "等待可用结果" in panel.summary.text()
        path.write_text(json.dumps(report()), encoding="utf-8")
        panel.refresh()
        assert panel.table.rowCount() == 1
        panel.table.setCurrentCell(0, 0)
        assert panel.locate_button.isEnabled()
    finally:
        panel.close()
        panel.deleteLater()
        app.processEvents()


@pytest.mark.parametrize("change", ["new_session", "split_group", "cross_stream_id"])
def test_changed_report_cannot_silently_select_a_different_group(tmp_path, change):
    app = QApplication.instance() or QApplication([])
    path = tmp_path / "count_reconciliation.json"
    data = report()
    path.write_text(json.dumps(data), encoding="utf-8")
    panel = CountReconciliationPanel(tmp_path)
    cleared = []
    panel.scope_cleared.connect(lambda: cleared.append(True))
    try:
        panel.set_context(tmp_path, "race", "stage")
        panel.refresh()
        panel.table.setCurrentCell(0, 0)
        if change == "new_session":
            data["scope"]["session_id"] = "next-session"
        elif change == "split_group":
            row = data["groups"][0]
            data["groups"] = [{**row, "window_id": "split-a", "video_event_uids": ["v1"]},
                              {**row, "window_id": "split-b", "video_event_uids": ["v2"]}]
        else:
            data["groups"][0].update(window_id="other", video_event_uids=["c1"], chip_event_ids=["v1"])
        path.write_text(json.dumps(data), encoding="utf-8")
        panel.refresh()
        assert cleared == [True]
        assert panel._selected_group is None
        assert not panel.discovery_button.isEnabled()
    finally:
        panel.close()
        panel.deleteLater()
        app.processEvents()
