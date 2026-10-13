import copy

from realtime.count_history import apply_reviews, observation_key, reconcile_report


SCOPE = {"race_id": "race", "stage_id": "stage", "source_id": "camera-1", "session_id": "session"}


def source(video=1, pending=0, reasons=None):
    return {"schema_version": 1, "scope": SCOPE, "clock": {"calibrated": True},
            "updated_at_ms": 3000, "cutoff_ms": 3000, "chip_watermark_ms": None,
            "groups": [{"window_id": "pack", "start_ms": 1000, "end_ms": 1100,
                        "video_count": video, "chip_count": 0, "pending_count": pending,
                        "excluded_count": 0, "status": "unresolved", "reasons": reasons or [],
                        "video_event_uids": ["track"], "chip_event_ids": [], "evidence": {}}]}


def chip(uid, bib, received, *, revision=1, active=True, race="race", stamp=1050):
    return {"event_id": uid, "bib": bib, "race_id": race, "stage_id": "stage",
            "passage_timestamp_ms": stamp, "received_at_ms": received,
            "revision": revision, "is_active": active}


def run(data, events, now=3000, **kwargs):
    return reconcile_report(data, events, as_of_ms=now, settlement_ms=0, **kwargs)


def test_second_chip_alert_survives_withdrawal_and_restart():
    data = source()
    events = [chip("kept", "023", 1020), chip("extra", "020", 1021),
              chip("extra", "020", 2000, revision=2, active=False)]
    before, cases = run(data, events, 1200)
    assert before["groups"][0]["status"] == "unresolved"
    assert before["groups"][0]["chip_count"] == 2
    after, saved = run(data, events, retained=cases)
    row = after["groups"][0]
    assert row["status"] == "resolved"
    assert row["initial_chip_count"] == 2 and row["chip_count"] == 1
    assert row["chip_changes"][0]["bib"] == "020"
    restarted, _ = run(data, events, retained=saved)
    assert restarted["groups"] == after["groups"]


def test_missing_read_is_retained_after_late_entry():
    events = [chip("late", "307", 2000)]
    before, cases = run(source(), events, 1200)
    assert before["groups"][0]["chip_count"] == 0
    after, _ = run(source(), events, retained=cases)
    row = after["groups"][0]
    assert row["initial_chip_count"] == 0 and row["chip_count"] == 1
    assert row["status"] == "resolved"
    assert row["chip_changes"][0]["kind"] == "added"


def test_equal_counts_and_chip_correction_do_not_clear_occluded_video():
    data = source(video=0, pending=1, reasons=["no_video_observation"])
    events = [chip("rider", "371", 1020), chip("extra", "376", 1021),
              chip("next", "096", 1022), chip("extra", "376", 2000, revision=2, active=False)]
    after, cases = run(data, events)
    assert after["groups"][0]["status"] == "unresolved"
    review = {"scope": SCOPE, "window_id": "pack", "verified_video_count": 2,
              "observation_key": observation_key(SCOPE, data["groups"][0])}
    reviewed, _ = run(data, events, reviews=[review], retained=cases)
    row = reviewed["groups"][0]
    assert row["status"] == "resolved" and row["video_count"] == 2
    assert row["detector_video_count"] == 0 and row["initial_chip_count"] == 3
    equal, _ = run(source(video=1, pending=1, reasons=["broken_track"]), [chip("r", "r", 1020)])
    assert equal["groups"][0]["status"] == "unresolved"


def test_review_is_invalidated_by_new_video_observations():
    data = source()
    review = {"scope": SCOPE, "window_id": "pack", "verified_video_count": 1,
              "observation_key": observation_key(SCOPE, data["groups"][0])}
    data["groups"][0]["video_event_uids"] = ["different-track"]
    result, _ = run(data, [chip("r", "r", 1020)], reviews=[review])
    assert result["groups"][0]["status"] == "unresolved"
    assert "manual_review_stale" in result["groups"][0]["reasons"]


def test_manual_count_cannot_clear_incomplete_chip_history_or_data_wait():
    data = source(video=0, pending=1, reasons=["no_video_observation", "chip_history_incomplete"])
    row = data["groups"][0]
    review = {"scope": SCOPE, "window_id": "pack", "verified_video_count": 1,
              "observation_key": observation_key(SCOPE, row)}
    result, _ = run(data, [chip("r", "r", 1020)], reviews=[review])
    assert result["groups"][0]["video_count"] == result["groups"][0]["chip_count"] == 1
    assert result["groups"][0]["status"] == "unresolved"
    assert "chip_history_incomplete" in result["groups"][0]["reasons"]
    overlay = apply_reviews(result, [review])
    assert overlay["groups"][0]["status"] == "unresolved"

    row.update(chip_count=1, status="waiting", reasons=["no_video_observation"])
    overlay = apply_reviews(data, [review])
    assert overlay["groups"][0]["video_count"] == 1
    assert overlay["groups"][0]["status"] == "waiting"


def test_case_remains_visible_when_producer_drops_matching_window():
    events = [chip("late", "307", 2000)]
    _, cases = run(source(), events, 1200)
    empty = copy.deepcopy(source())
    empty["groups"] = []
    after, _ = run(empty, events, retained=cases)
    row = after["groups"][0]
    assert row["history_only"] and row["status"] == "resolved"
    assert row["initial_chip_count"] == 0


def test_other_races_out_of_order_revisions_and_missing_receipt_are_handled():
    events = [chip("kept", "023", 1020), chip("extra", "020", 2000, revision=2, active=False),
              chip("extra", "020", 1021), chip("foreign", "x", 1020, race="other")]
    after, _ = run(source(), events)
    assert after["groups"][0]["chip_count"] == 1
    assert after["groups"][0]["initial_chip_count"] == 2
    missing = chip("rider", "r", 1020)
    missing.pop("received_at_ms")
    result, _ = run(source(), [missing])
    assert result["groups"][0]["chip_count"] == 1
    assert result["groups"][0]["status"] == "unresolved"
    assert "chip_receipt_missing" in result["groups"][0]["reasons"]


def test_incomplete_video_progress_and_uncalibrated_clock_wait():
    data = source()
    data["cutoff_ms"] = 1050
    result, _ = run(data, [chip("r", "r", 1020)])
    assert result["groups"][0]["status"] == "waiting"
    data["cutoff_ms"] = 3000
    data["clock"]["calibrated"] = False
    result, _ = run(data, [chip("r", "r", 1020)])
    assert result["groups"][0]["status"] == "waiting"


def test_later_manual_review_cannot_change_an_earlier_replay():
    data = source(video=0,pending=1,reasons=["no_video_observation"])
    review = {"scope":SCOPE,"window_id":"pack","verified_video_count":1,"verified_at_ms":5000,
              "observation_key":observation_key(SCOPE,data["groups"][0])}
    before,_ = run(data,[chip("r","r",1020)],reviews=[review])
    assert before["groups"][0]["video_count"] == 0
    assert before["groups"][0]["status"] == "unresolved"
    after,_ = run(data,[chip("r","r",1020)],now=6000,reviews=[review])
    assert after["groups"][0]["video_count"] == 1
    assert after["groups"][0]["status"] == "resolved"
