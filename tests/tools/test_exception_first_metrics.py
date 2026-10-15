"""The reduction table and the two numeric gates, on a hand-written list."""

import json

from tools.exception_first_metrics import (
    build_report,
    gates,
    history_row,
    known_case_report,
    reduction,
)

T = 1_789_784_000_000
FRAME = {"camera_index": 1, "segment_id": "seg", "video_path": "videos/a.mkv", "frame_index": 100,
         "position_ms": 40_600, "session_id": "s", "calibration_version": 1}


def _item(item_id, kind, *, category="video", status="open", bibs=(), events=(), evidence=None, chip_time=T):
    return {"item_id": item_id, "kind": kind, "category": category, "title": "", "detail": "",
            "chip_time_ms": chip_time, "bibs": list(bibs), "event_ids": list(events), "status": status,
            "status_note": "", "priority": 50, "evidence": evidence or {}, "calibration_version": 1}


def _payload(items, auto_passed=(), cameras=None, summary=None):
    return {"schema_version": 1, "race_id": "race", "generated_at_ms": T, "live": False,
            "summary": summary or {}, "items": items, "auto_passed": list(auto_passed),
            "cameras": cameras if cameras is not None else {
                "1": {"pairing": {"chips_with_video": 200, "residual_median_ms": 12.0},
                      "calibration": {"residual_mad_ms": 25.0}}}}


def test_the_table_counts_main_problems_not_members_or_chips():
    payload = _payload([
        _item("batch:1", "two_chips_one_crossing", bibs=("020", "023"), events=("e1", "e2"),
              evidence={"batch_members": [{"event_id": "e1", "bib": "020", "present": True, "active": True},
                                          {"event_id": "e2", "bib": "023", "present": True, "active": True}]}),
        _item("bunch:1", "bunch_check", bibs=("057",), evidence={"video_count": 9}),
        _item("near:1", "near_crossing", evidence={"recorder_ms": T}),
        _item("member:1", "two_chips_one_crossing", bibs=("023",), evidence={"group": "batch:1"}),
        _item("closed:1", "two_chips_one_crossing", status="closed_upstream"),
    ], auto_passed=[{"bib": "057", "event_id": "e9", "active_now": True}])

    table = reduction(payload)
    assert table["main_problems"] == 3  # the group member and the closed item are not tasks
    assert table["auto_passed"] == 1
    assert table["to_identify"] == {"missed_read_items": 1, "batch_members": 2, "total": 3}
    assert table["to_count"] == {"group_items": 1, "group_riders": 9}
    # The matched rider is inside the group item as a member, which is not a
    # request to confirm them again.
    assert table["matched_riders_asked_again"] == {} and table["normal_rider_confirmations"] == 0
    assert table["auto_passed_also_listed"]["riders"] == 1
    assert table["auto_passed_also_listed"]["by_kind"] == {"bunch_check": 1}


def test_a_matched_rider_who_is_still_a_problem_is_named():
    payload = _payload([_item("near:1", "near_crossing", bibs=("057",))],
                       auto_passed=[{"bib": "057", "event_id": "e1", "active_now": True}])

    table = reduction(payload)
    # The rider is the subject of a pending item although already matched.
    assert table["matched_riders_asked_again"] == {"057": ["near_crossing"]}
    assert table["normal_rider_confirmations"] == 1
    report = build_report(payload)
    assert any("自动对应" in value for value in report["findings"])


def test_the_two_gates_report_their_own_numbers_and_fail_loudly():
    payload = _payload([_item(f"near:{index}", "near_crossing") for index in range(31)],
                       cameras={"1": {"pairing": {"chips_with_video": 200, "residual_median_ms": 51.0},
                                      "calibration": {"residual_mad_ms": 25.0}}})

    result = gates(payload)
    ratio = result["top_level_video_pending_ratio"]
    assert ratio == {"value": 0.155, "limit": 0.15, "pass": False, "numerator": 31,
                     "denominator_chips_with_video": 200, "note": ratio["note"]}
    residual = result["auto_clock_residual_median_ms"]
    assert residual["worst"] == 51.0 and residual["pass"] is False and residual["per_camera"] == {"1": 51.0}
    findings = build_report(payload, whole_race=True)["findings"]
    assert sum("未达门槛" in value for value in findings) == 2
    # On a slice the ratio is reported but does not judge.
    slice_findings = build_report(payload)["findings"]
    assert sum("未达门槛" in value for value in slice_findings) == 1
    # A chip self-check is not video work and never enters the numerator.
    chip_only = _payload([_item("chip:1", "test_chip", category="chip"),
                          _item("near:1", "near_crossing")])
    assert gates(chip_only)["top_level_video_pending_ratio"]["numerator"] == 1
    # No analysed camera means no denominator: the gate stays open, not passed.
    assert gates(_payload([], cameras={}))["top_level_video_pending_ratio"]["pass"] is None


def test_the_history_row_is_a_comparison_not_an_equality():
    payload = _payload([_item("near:1", "near_crossing")],
                       summary={"pending": 26, "auto_passed": 233})

    row = history_row(payload, "dingbian")
    assert row["main_problems"] == 26 and row["auto_passed"] == 233
    assert row["new_chips_with_video"] == 200 and row["new_main_problems"] == 26
    assert "定边" in row["source"]
    assert history_row(payload, "tongxin")["main_problems_with_chip_checks"] == 63
    assert history_row(payload, "") is None


def test_a_known_case_is_reported_case_by_case_with_its_first_warning():
    payload = _payload([
        _item("near:1", "near_crossing", evidence={"recorder_ms": T + 100, "frame": dict(FRAME)}),
        _item("far:1", "near_crossing", evidence={"recorder_ms": T + 900_000, "frame": dict(FRAME)}),
        _item("closed:1", "near_crossing", status="closed_upstream", evidence={"recorder_ms": T + 500_000}),
    ], auto_passed=[{"bib": "550", "event_id": "e5", "active_now": True}])
    cases = {"319": {"camera": 1, "recorder_ms": T + 200, "frame": dict(FRAME), "note": "集团内漏读"},
             "550": {"camera": 1, "recorder_ms": T + 900_000, "frame": {"segment_id": "other", "frame_index": 1}},
             "999": {"camera": 1, "recorder_ms": T - 60_000}}
    replay = {"steps": [{"index": 0, "mark": T, "settle": False, "item_times": {"near:1": T + 100}},
                        {"index": 1, "mark": T + 900_000, "settle": True,
                         "item_times": {"near:1": T + 100, "far:1": T + 900_000}}]}

    report = {entry["bib"]: entry for entry in known_case_report(payload, cases, replay=replay)}
    assert report["319"]["located"] is True and report["319"]["matched_anyway"] is False
    assert report["319"]["first_shown_in_replay"]["index"] == 0
    # 550 is located by its own frame, and the replay shows it only once the
    # video that contains it had sealed.
    assert report["550"]["located"] is True and report["550"]["matched_anyway"] is True
    assert report["550"]["first_shown_in_replay"]["since_case_ms"] == 0
    assert report["999"]["located"] is False
    still_a_task = dict(cases, **{"550": dict(cases["550"], expect="candidate")})
    findings = build_report(payload, cases=still_a_task, replay=replay)["findings"]
    assert any("999" in value for value in findings)
    # 550 still sits in an open item, so demanding a candidate is satisfied.
    assert not any("550" in value for value in findings)
    # A case whose only trace is already closed is reported by name.
    closed_case = {"551": {"camera": 1, "recorder_ms": T + 500_000, "expect": "candidate"}}
    assert any("551" in value and "待核候选" in value for value in build_report(payload, cases=closed_case)["findings"])


def test_a_known_case_can_be_located_by_the_identity_a_human_verified():
    payload = _payload([_item("bunch:1", "bunch_check", bibs=("319", "320"), evidence={"recorder_ms": T})])
    report = {entry["bib"]: entry for entry in known_case_report(payload, {"319": {"camera": 2, "note": "集团内漏读"}})}

    assert report["319"]["located"] is True
    assert report["319"]["evidence_items"][0]["matched_by"] == ["bib"]


def test_a_case_without_a_location_is_never_counted_as_located_by_another_one():
    payload = _payload([_item("near:1", "near_crossing", evidence={"recorder_ms": T})])
    cases = {"319": {"camera": 1, "recorder_ms": T + 4_999}, "320": {"camera": 1, "recorder_ms": T + 5_001}}
    report = {entry["bib"]: entry for entry in known_case_report(payload, cases)}

    assert report["319"]["located"] is True and report["320"]["located"] is False


def test_the_tool_never_claims_what_it_cannot_measure(tmp_path):
    payload = _payload([_item("near:1", "near_crossing")])
    report = build_report(payload)
    assert report["realtime"] is None and report["known_cases"] is None
    assert any("第四场" in value for value in report["not_measured"])
    assert any("用时" in value for value in report["not_measured"])
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    assert json.loads(path.read_text(encoding="utf-8"))["reduction"]["main_problems"] == 1
