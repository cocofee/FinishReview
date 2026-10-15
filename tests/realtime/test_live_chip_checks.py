import json
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from PyQt5.QtCore import QProcess
from PyQt5.QtWidgets import QApplication

from realtime.live_chip_checks import LIVE_CHIP_FILENAME, live_chip_payload, merge_chip_snapshot
from realtime.passage_receiver import PassageEvent
from realtime.problem_items import CATEGORY_VIDEO, KIND_NO_CHIP_CROSSING, ProblemItem
from realtime.problem_panel import ProblemListPanel


T = 1_789_784_000_000


def event(identity="a", bib="20", offset=0, **changes):
    value = PassageEvent(event_id=identity, race_id="race", stage_id="stage", group_id="1", sequence=1,
                         bib=bib, chip_id="chip-" + bib, passage_time_ms=36_800_000 + offset,
                         lap=0, revision=1, is_active=True, passage_timestamp_ms=T + offset,
                         received_at_ms=T + offset, group_name="race group")
    return replace(value, **changes)


def journal(root, events):
    root.mkdir(parents=True, exist_ok=True)
    (root / "cyclerace_passage_events.jsonl").write_text(
        "".join(json.dumps(value.to_payload()) + "\n" for value in events), encoding="utf-8")


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


def wait_for(qapp, condition, timeout=15):
    until = time.monotonic() + timeout
    while not condition() and time.monotonic() < until:
        qapp.processEvents()
        time.sleep(.01)
    assert condition()


def test_snapshot_counts_messages_and_effective_events_separately(tmp_path):
    first = event()
    journal(tmp_path, [first, first, replace(first, revision=2), event("b", offset=100),
                       event("foreign", race_id="other")])
    payload = live_chip_payload(tmp_path, race_id="race", stage_id="stage")
    assert payload["counts"] == {"raw_messages": None, "journal_messages": 4,
                                  "effective_passages": 2, "video_candidates": None}
    assert len(payload["items"]) == 1
    assert payload["items"][0]["kind"] == "duplicate_read"
    assert payload["missing_inputs"]
    assert not (tmp_path / "finish_problems").exists()


def test_revision_void_restore_and_date_independence(tmp_path):
    first, second = event(), event("b", offset=100)
    journal(tmp_path, [first, second, replace(second, revision=2, is_active=False)])
    payload = live_chip_payload(tmp_path, race_id="race", stage_id="stage")
    assert payload["items"][0]["status"] == "closed_upstream"
    journal(tmp_path, [first, second, replace(second, revision=2, is_active=False),
                       replace(second, revision=3, received_at_ms=T + 20 * 86_400_000)])
    restored = live_chip_payload(tmp_path, race_id="race", stage_id="stage")
    assert restored["items"][0]["status"] == "open"
    assert restored["items"][0]["item_id"] == payload["items"][0]["item_id"]
    journal(tmp_path, [first, second, replace(second, revision=4, passage_timestamp_ms=T + 20_000)])
    moved = live_chip_payload(tmp_path, race_id="race", stage_id="stage")
    assert all(item["kind"] != "duplicate_read" for item in moved["items"])


def test_normal_sprint_and_missing_roster_do_not_force_confirmation(tmp_path):
    journal(tmp_path, [event(str(i), str(i), i * 110) for i in range(8)])
    payload = live_chip_payload(tmp_path, race_id="race", stage_id="stage")
    assert payload["items"] == []
    assert any("名单" in value for value in payload["missing_inputs"])


def test_merge_keeps_video_and_does_not_cross_race(tmp_path):
    journal(tmp_path, [event(), event("b", offset=100)])
    chips = live_chip_payload(tmp_path, race_id="race", stage_id="stage")
    video_item = ProblemItem("video", KIND_NO_CHIP_CROSSING, CATEGORY_VIDEO, "candidate").to_payload()
    video = {"race_id": "race", "stage_id": "stage", "items": [video_item],
             "summary": {"auto_passed": 12}, "auto_passed": [{"event_id": "ok"}]}
    merged = merge_chip_snapshot(video, chips)
    assert len(merged["items"]) == 2
    assert video["items"] == [video_item]
    assert merged["auto_passed"] == video["auto_passed"]
    assert merged["summary"]["auto_passed"] == 12
    assert merge_chip_snapshot(dict(video, race_id="other"), chips)["items"] == [video_item]
    sync = ProblemItem("sync", "sync", "sync", "external snapshot mismatch").to_payload()
    merged = merge_chip_snapshot(dict(video, items=[video_item, sync]), chips)
    assert any(item["item_id"] == "sync" for item in merged["items"])


def test_automatic_path_without_video_or_line_preserves_draft_and_restart(qapp, tmp_path):
    journal(tmp_path, [event(), event("b", offset=100)])
    panel = ProblemListPanel()
    panel.set_workspace(tmp_path)
    panel.set_scope("race", "stage")
    panel._live_paused = True  # pausing video must not pause chip checks
    panel.live_tick(False)
    wait_for(qapp, lambda: panel.payload is not None)
    assert panel._process is None
    assert panel.items[0].kind == "duplicate_read"
    assert "有效通过 2" in panel.chip_status
    panel.reason_input.setText("operator draft")
    selected = panel.current_item().item_id
    journal(tmp_path, [event(), event("b", offset=100), event("normal", "30", 30_000)])
    panel.live_tick(False)
    wait_for(qapp, lambda: panel.payload["chip_checks"]["counts"]["effective_passages"] == 3)
    assert panel.current_item().item_id == selected
    assert panel.reason_input.text() == "operator draft"
    panel.close()
    restarted = ProblemListPanel()
    restarted.set_scope("race", "stage")
    restarted.set_workspace(tmp_path)
    restarted.live_tick(False)
    assert not restarted._chip_timer.isActive()
    assert restarted.reason_input.text() == "operator draft"
    assert len(restarted.items) == 1
    restarted.close()


def test_chip_publication_and_later_video_refresh_keep_each_other(qapp, tmp_path):
    journal(tmp_path, [event(), event("b", offset=100)])
    directory = tmp_path / "finish_problems"
    directory.mkdir()
    video = ProblemItem("video", KIND_NO_CHIP_CROSSING, CATEGORY_VIDEO, "candidate", chip_time_ms=T - 1000)
    payload = {"race_id": "race", "stage_id": "stage", "items": [video.to_payload()]}
    path = directory / "problem_list.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    panel = ProblemListPanel()
    panel.set_scope("race", "stage")
    panel.set_workspace(tmp_path)
    panel.reason_input.setText("video draft")
    panel.live_tick(False)
    wait_for(qapp, lambda: len(panel.items) == 2)
    assert panel.current_item().item_id == "video"
    assert panel.reason_input.text() == "video draft"
    assert json.loads(path.read_text(encoding="utf-8")) == payload
    payload["summary"] = {"auto_passed": 5}
    path.write_text(json.dumps(payload), encoding="utf-8")
    panel.refresh()
    assert len(panel.items) == 2
    assert panel.reason_input.text() == "video draft"
    panel.close()


def test_switch_discards_old_generation_and_ignores_other_race_snapshot(qapp, tmp_path, monkeypatch):
    journal(tmp_path, [event(), event("b", offset=100)])
    panel = ProblemListPanel()
    panel.set_workspace(tmp_path)
    panel.set_scope("race", "stage")
    monkeypatch.setattr(QProcess, "start", lambda self: None)
    panel._start_chip_checks()
    old_process, generation = panel._chip_process, panel._chip_generation
    panel.set_scope("other", "stage")
    panel._chip_done(old_process, generation, 0)
    assert not (tmp_path / "finish_problems" / LIVE_CHIP_FILENAME).exists()
    assert not panel.items
    panel.close()


def test_partial_receive_snapshot_is_not_proof_of_upstream_deletion(qapp, tmp_path):
    panel = ProblemListPanel()
    panel.set_workspace(tmp_path)
    item = ProblemItem("x", "duplicate_read", "chip", "duplicate", event_ids=("a", "b"))
    panel.set_events_provider(lambda: [event("unrelated")])
    assert not panel._upstream_closed(item)
    panel.set_events_provider(lambda: [event("a", is_active=False), event("b", is_active=False)])
    assert panel._upstream_closed(item)
    panel.close()


def test_publication_failure_keeps_last_snapshot_and_retries(qapp, tmp_path, monkeypatch):
    from pathlib import Path

    journal(tmp_path, [event(), event("b", offset=100)])
    payload = live_chip_payload(tmp_path, race_id="race", stage_id="stage")
    directory = tmp_path / "finish_problems"
    directory.mkdir()
    path = directory / LIVE_CHIP_FILENAME
    path.write_text(json.dumps(payload), encoding="utf-8")
    before = path.read_bytes()
    panel = ProblemListPanel()
    panel.set_scope("race", "stage")
    panel.set_workspace(tmp_path)
    journal(tmp_path, [event(), event("b", offset=100), event("c", "30", 20_000)])
    updated = live_chip_payload(tmp_path, race_id="race", stage_id="stage")
    process = SimpleNamespace(readAllStandardOutput=lambda: json.dumps(updated).encode(), deleteLater=lambda: None)
    panel._chip_process = process
    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", lambda *_args: (_ for _ in ()).throw(OSError("disk failure")))
        panel._chip_done(process, panel._chip_generation, 0)
    assert path.read_bytes() == before
    assert panel.payload["chip_checks"]["counts"]["effective_passages"] == 2
    assert "失败" in panel.chip_status
    panel.live_tick(False)
    assert not panel._chip_timer.isActive()
    panel._chip_retry_after = 0
    panel.live_tick(False)
    assert panel._chip_timer.isActive()
    panel.close()


def test_close_cancels_debounce_before_worker_start(qapp, tmp_path):
    journal(tmp_path, [event()])
    panel = ProblemListPanel()
    panel.set_workspace(tmp_path)
    panel.live_tick(False)
    assert panel._chip_timer.isActive()
    panel.close()
    assert not panel._chip_timer.isActive()
    assert panel._chip_process is None
