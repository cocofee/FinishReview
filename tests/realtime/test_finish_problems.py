import copy
import csv

import pytest

from realtime.finish_problems import (
    apply_decisions, build_problem_report, export_suggestions, read_decisions, record_decision,
)


def inputs():
    times = [10_000, 29_000, 58_000, 89_000, 137_000, 165_000]
    scope = dict(race_id="race", stage_id="final", camera_index=1)
    chips = [dict(event_id=str(i), timestamp_ms=t, race_id="race", stage_id="final",
                  bib=str(i + 10), chip_id=str(i), lap=1, revision=1, is_active=True)
             for i, t in enumerate(times)]
    events = [observation(t + 200, i) for i, t in enumerate(times)]
    request = dict(scope=scope, chips=chips, sources=[dict(segment_id="video")],
                   final_lap=1, input_signature="original")
    return request, events


def observation(t, index):
    return dict(recorder_ms=t, locator=dict(segment_id="video", camera_index=1,
                frame_index=index, video_path="archive.avi", position_ms=t, recorder_ms=t,
                source_signature=[100, 200]), obstructed=False)


def test_report_fits_clock_and_retains_unmatched_source_evidence():
    request, events = inputs()
    events.append(observation(174_000, 99))
    report = build_problem_report(request, events, [(0, 180_000)])
    assert report["clock"]["offset_ms"] == pytest.approx(200)
    assert len(report["automatic_passes"]) == 6
    assert [p["kind"] for p in report["problems"]] == ["no_chip"]
    assert report["problems"][0]["locator"]["frame_index"] == 99


def test_missing_video_obstruction_and_manual_conflict_never_pass():
    request, events = inputs()
    report = build_problem_report(request, events, [(0, 150_000)],
                                  [(57_000, 59_000, events[2])])
    assert len(report["automatic_passes"]) == 4
    assert {p["kind"] for p in report["problems"]} == {"obstruction", "no_video"}
    request["manual_anchors"] = [dict(chip_ms=10_000, offset_ms=1000)]
    report = build_problem_report(request, events, [(0, 180_000)])
    assert report["manual_conflicts"]
    assert not report["automatic_passes"]


def test_dense_and_ambiguous_groups_and_close_candidates_require_review():
    request, events = inputs()
    for i in range(4):
        request["chips"].append(dict(request["chips"][0], event_id=f"group{i}",
                                      chip_id=f"g{i}", timestamp_ms=110_000 + i * 300))
        events.append(observation(110_200 + i * 300, 20 + i))
    events.append(observation(29_500, 50))
    report = build_problem_report(request, events, [(0, 180_000)])
    assert any(p["kind"] == "group" and len(p["chips"]) == 4 for p in report["problems"])
    assert any(p["kind"] == "no_chip" for p in report["problems"])
    assert len(report["automatic_passes"]) == 5


def test_duplicate_spare_and_wrong_scope_are_not_automatic_passes():
    request, events = inputs()
    request["chips"].append(dict(request["chips"][0], event_id="retry", timestamp_ms=10_050))
    request["chips"].append(dict(request["chips"][1], event_id="spare", group_name="备用",
                                  chip_id="spare", timestamp_ms=120_000))
    request["chips"].append(dict(request["chips"][2], event_id="other", race_id="other"))
    report = build_problem_report(request, events, [(0, 180_000)])
    assert {"duplicate", "test_chip"} <= {p["kind"] for p in report["problems"]}
    assert not any(p["chip"]["event_id"] in {"0", "retry", "spare", "other"}
                   for p in report["automatic_passes"])


def test_decisions_are_durable_scoped_and_invalidated_by_evidence(tmp_path):
    request, events = inputs()
    events.append(observation(174_000, 99))
    report = build_problem_report(request, events, [(0, 180_000)])
    row = report["problems"][0]
    record_decision(tmp_path, report, row, action="add", reason="=reviewed", bib="24", lap=1)
    rows = apply_decisions(report, read_decisions(tmp_path))
    assert rows[0]["status"] == "pending_sync"
    assert rows[0]["decision"]["chip_timestamp_ms"] == 173_800
    paths = export_suggestions(tmp_path, report, tmp_path / "csv")
    with paths[0].open(encoding="utf-8-sig", newline="") as handle:
        exported = list(csv.reader(handle))
    assert len(exported) == 2
    assert "'=reviewed" in exported[1]
    changed = copy.deepcopy(request)
    changed["input_signature"] = "line changed"
    new_report = build_problem_report(changed, events, [(0, 180_000)])
    assert apply_decisions(new_report, read_decisions(tmp_path))[0]["status"] == "pending"
    assert apply_decisions(new_report, read_decisions(tmp_path))[0].get("decision") is None


def test_delete_requires_specific_chip_and_reason(tmp_path):
    request, events = inputs()
    request["chips"].append(dict(request["chips"][0], event_id="retry", timestamp_ms=10_050))
    report = build_problem_report(request, events, [(0, 180_000)])
    row = next(p for p in report["problems"] if p["kind"] == "duplicate")
    with pytest.raises(ValueError):
        record_decision(tmp_path, report, row, action="delete", reason="duplicate")
    with pytest.raises(ValueError):
        record_decision(tmp_path, report, row, action="delete", reason=" ", chip_event_id="retry")
    record_decision(tmp_path, report, row, action="delete", reason="duplicate", chip_event_id="retry")
    assert read_decisions(tmp_path)[0]["chip_event_id"] == "retry"


def test_unreliable_clock_never_generates_passes():
    request, events = inputs()
    request["chips"] = request["chips"][:2]
    report = build_problem_report(request, events[:2], [(0, 180_000)])
    assert report["clock"] is None
    assert not report["automatic_passes"]
    assert report["problems"][0]["kind"] == "clock_wait"


def test_group_inside_missing_video_still_lists_unverifiable_chips():
    request, events = inputs()
    for i in range(4):
        request["chips"].append(dict(request["chips"][0], event_id=f"group{i}",
                                      chip_id=f"g{i}", timestamp_ms=190_000 + i * 300))
    report = build_problem_report(request, events, [(0, 180_000)])
    assert len([p for p in report["problems"] if p["kind"] == "no_video"]) == 4
    assert any(p["kind"] == "group" for p in report["problems"])
    assert len(report["automatic_passes"]) == 6
