"""Discovery history for near-crossing candidates: opened once, closed with a reason."""

import json

from realtime.candidate_history import HISTORY_FILENAME, load_history, update_history
from realtime.problem_items import KIND_NEAR_CROSSING, STATUS_CLOSED_UPSTREAM, STATUS_INFO, ProblemItem


FRAME = {"camera_index": 2, "segment_id": "seg-b", "video_path": "camera_02_0008.mkv", "frame_index": 3210,
         "position_ms": 49917, "session_id": "camera_02_20260919_105600", "calibration_version": 4}


def near_item(item_id="near_crossing:abc", *, status="open", note=""):
    return ProblemItem(
        item_id, KIND_NEAR_CROSSING, "video",
        "10:58:08.700 线上另有一处变化，距芯片 348（10:58:08.100）的过线 +600 ms（认车：自行车）",
        chip_time_ms=1_789_784_100_000, status=status, status_note=note, priority=15,
        evidence={"frame": dict(FRAME), "recorder_ms": 1_789_784_101_000, "gap_ms": 600,
                  "near_read": {"bib": "348", "event_id": "e-348", "chip_ms": 1_789_784_099_000}},
    )


def test_a_candidate_is_recorded_once_and_kept_while_it_is_open(tmp_path):
    journal = tmp_path / HISTORY_FILENAME
    item = near_item()

    assert update_history(journal, [item], now_ms=1_000, race_id="r", stage_id="s") == []
    assert update_history(journal, [item], now_ms=2_000, race_id="r", stage_id="s") == []

    records = load_history(journal)
    record = records[item.item_id]
    assert record["state"] == "open" and record["first_seen_ms"] == 1_000
    assert record["gap_ms"] == 600
    assert record["near_read"]["bib"] == "348" and record["frame"]["frame_index"] == 3210
    assert len(journal.read_text(encoding="utf-8").splitlines()) == 1


def test_a_late_chip_matching_the_crossing_closes_the_candidate_with_a_reason(tmp_path):
    journal = tmp_path / HISTORY_FILENAME
    item = near_item()
    update_history(journal, [item], now_ms=1_000, race_id="r", stage_id="s")

    late = {"bib": "319", "frame": dict(FRAME), "chip_time_ms": item.chip_time_ms, "late_pair": True}
    closed = update_history(journal, [], [late], now_ms=3_000, race_id="r", stage_id="s")

    assert len(closed) == 1 and closed[0].status == STATUS_INFO
    assert "迟到芯片 319 已匹配该过线" in closed[0].status_note
    assert "发现历史" in closed[0].status_note and closed[0].detail == item.detail
    assert closed[0].evidence["recorded_history"] is True
    record = load_history(journal)[item.item_id]
    assert record["state"] == "closed" and record["closed_reason"] == "迟到芯片 319 已匹配该过线"
    # A closure is reported once; the journal keeps it for tracing.
    assert update_history(journal, [], [late], now_ms=4_000, race_id="r", stage_id="s") == []


def test_a_candidate_that_stops_being_reported_is_closed_without_inventing_a_chip(tmp_path):
    journal = tmp_path / HISTORY_FILENAME
    item = near_item()
    update_history(journal, [item], now_ms=1_000, race_id="r", stage_id="s")

    closed = update_history(journal, [], [], now_ms=2_000, race_id="r", stage_id="s")

    assert len(closed) == 1
    assert "未再出现" in closed[0].status_note and "迟到芯片" not in closed[0].status_note
    assert closed[0].item_id == item.item_id and closed[0].chip_time_ms == item.chip_time_ms


def test_a_camera_skipped_this_run_does_not_close_its_candidates(tmp_path):
    journal = tmp_path / HISTORY_FILENAME
    item = near_item()  # camera 2
    update_history(journal, [item], now_ms=1_000, race_id="r", stage_id="s")

    # Camera 2 waited for its line or sealed footage: silence is not a resolution.
    assert update_history(journal, [], [], now_ms=2_000, race_id="r", stage_id="s", analysed_cameras={1}) == []
    assert load_history(journal)[item.item_id]["state"] == "open"

    closed = update_history(journal, [], [], now_ms=3_000, race_id="r", stage_id="s", analysed_cameras={1, 2})
    assert len(closed) == 1 and load_history(journal)[item.item_id]["state"] == "closed"


def test_a_manual_entry_closure_is_recorded_with_its_bib(tmp_path):
    journal = tmp_path / HISTORY_FILENAME
    item = near_item(status=STATUS_CLOSED_UPSTREAM, note="CycleRace 已补录 319（10:58:08.700）")

    closed = update_history(journal, [item], now_ms=1_000, race_id="r", stage_id="s")

    assert len(closed) == 1 and "319" in closed[0].status_note
    assert load_history(journal)[item.item_id]["closed_reason"] == item.status_note


def test_history_from_another_scope_is_not_closed_and_replayed_items_are_ignored(tmp_path):
    journal = tmp_path / HISTORY_FILENAME
    other = near_item("near_crossing:other")
    update_history(journal, [other], now_ms=1_000, race_id="other-race", stage_id="s")

    assert update_history(journal, [], [], now_ms=2_000, race_id="r", stage_id="s") == []
    assert load_history(journal)[other.item_id]["state"] == "open"

    replay = dict(other.to_payload())
    replay["evidence"] = dict(replay["evidence"], recorded_history=True)
    update_history(journal, [ProblemItem.from_payload(replay)], now_ms=3_000, race_id="other-race", stage_id="s")
    assert load_history(journal)[other.item_id]["state"] == "open"


def test_a_torn_last_line_does_not_break_the_history(tmp_path):
    journal = tmp_path / HISTORY_FILENAME
    item = near_item()
    update_history(journal, [item], now_ms=1_000, race_id="r", stage_id="s")
    with journal.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"schema_version": 1, "item_id": "near_crossing:half"})[:-4])

    records = load_history(journal)

    assert set(records) == {item.item_id}
