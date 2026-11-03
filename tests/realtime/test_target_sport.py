"""Explicit target-sport policy: cycling stays the default, running is opt-in."""

from types import SimpleNamespace

import numpy as np
from realtime.analysis_settings import AnalysisSettingsStore, CONFIG_PATH, settings_token
from realtime.bike_check import (
    BICYCLE,
    CAR,
    DECISION_EXCLUDE,
    DECISION_KEEP,
    LABEL_BICYCLE,
    LABEL_PEDESTRIAN,
    PERSON,
    BikeVerdict,
    CropPlan,
    Detection,
    classify_frames,
)
from realtime.chip_history import ChipRead
from realtime.clock_sync import ClockModel
from realtime.problem_analysis import config_from_workspace
from realtime.problem_rules import CrossingRecord, build_video_items
from realtime.target_crossing import check_target_tracks
from realtime.target_sport import DEFAULT_SPORT, RUNNING, normalize_sport, policy_for


T0 = 1_789_784_000_000
MODEL = ClockModel(
    camera_index=1, intercept_ms=1_000.0, slope=0.0, reference_ms=T0, valid_from_ms=T0,
    valid_to_ms=T0 + 3_600_000, support=50, candidates=60, residual_mad_ms=25.0,
    coarse_offset_ms=1_000, coarse_support=100, version=3,
)


def _read(bib, chip_ms):
    return ChipRead(
        event_id=f"race-stage-1-passage-{bib}", bib=bib, chip_id=bib, group_id="1", group_name="组",
        athlete_name="", chip_ms=chip_ms, passage_time_ms=chip_ms % 86_400_000, lap=0,
        received_at_ms=chip_ms, revision=1, latest_revision=1, active_now=True, manual_entry=False,
    )


def _crossing(recorder_ms, row=250):
    return CrossingRecord(
        camera_index=1, session_id="s", segment_id="seg", video_path="v.mkv",
        recorder_ms=recorder_ms, frame_index=(recorder_ms - T0) // 50, position_ms=recorder_ms - T0,
        row=row, r0=row - 20, r1=row + 20, contact=(100.0, 200.0),
    )


def _rules(**kwargs):
    defaults = dict(camera_index=1, model=MODEL, matched=(), video_only=(), chip_only=(), no_video=())
    defaults.update(kwargs)
    return build_video_items(**defaults)


def test_normalize_sport_defaults_to_cycling_and_rejects_unknown_values():
    assert normalize_sport(None) == DEFAULT_SPORT == "cycling"
    assert normalize_sport("") == "cycling"
    assert normalize_sport(" RUNNING ") == "running"
    assert policy_for(None).target_label == LABEL_BICYCLE
    assert policy_for("running").target_label == LABEL_PEDESTRIAN
    assert policy_for("running").label_text("unchecked") == "未认人"
    assert policy_for("running").label_text("nothing") == "未认出人形目标"
    assert normalize_sport("swimming") == DEFAULT_SPORT


def test_old_config_means_cycling_and_running_is_read_explicitly(tmp_path):
    config_path = tmp_path / CONFIG_PATH
    config_path.parent.mkdir(parents=True)
    config_path.write_text("{}", encoding="utf-8")
    assert config_from_workspace(tmp_path).target_sport == "cycling"

    config_path.write_text('{"target_sport": "running"}', encoding="utf-8")
    assert config_from_workspace(tmp_path).target_sport == "running"

    config_path.write_text('{"target_sport": "swimming"}', encoding="utf-8")
    assert config_from_workspace(tmp_path).target_sport == "cycling"


def test_saving_the_sport_is_scoped_to_this_event_and_changes_the_token(tmp_path):
    segment = SimpleNamespace(camera_index=1, session_id="session", start_ms=1_000)
    store = AnalysisSettingsStore(tmp_path)
    store.save(race_id="race", stage_id="stage", segment=segment,
               line={"points": [[.1, .1], [.9, .9]]}, model_path="", crop_mode="up",
               target_sport="cycling")
    cycling_token = settings_token(tmp_path)
    assert cycling_token and config_from_workspace(tmp_path).target_sport == "cycling"

    store.save(race_id="race", stage_id="stage", segment=segment,
               line={"points": [[.1, .1], [.9, .9]]}, model_path="", crop_mode="up",
               target_sport="running")
    assert config_from_workspace(tmp_path).target_sport == "running"
    assert settings_token(tmp_path) != cycling_token

    # A model-only save that omits the sport must not silently flip it back.
    store.save(race_id="race", stage_id="stage", segment=segment,
               line={"points": [[.1, .1], [.9, .9]]}, model_path="m.onnx", crop_mode="up")
    assert config_from_workspace(tmp_path).target_sport == "running"


def test_running_lets_a_verified_person_absorb_a_late_chip_but_not_a_bicycle():
    read = _read("88", T0 + 60_000)
    person = BikeVerdict(LABEL_PEDESTRIAN, .9, DECISION_KEEP)
    bicycle = BikeVerdict(LABEL_BICYCLE, .9, DECISION_KEEP)

    person_crossing = _crossing(T0 + 60_480)
    running_person = _rules(video_only=[person_crossing], chip_only=[read],
                            verdicts={person_crossing.key: person}, target_sport="running")
    assert running_person.late_pairs == [("88", -520)]
    assert running_person.late_pair_frames[0]["late_pair"] is True

    bicycle_crossing = _crossing(T0 + 60_480)
    running_bicycle = _rules(video_only=[bicycle_crossing], chip_only=[read],
                             verdicts={bicycle_crossing.key: bicycle}, target_sport="running")
    assert running_bicycle.late_pairs == []
    assert any(item.evidence.get("target_sport") == "running" for item in running_bicycle.items)

    # Cycling keeps the historical rule: only a bicycle may absorb the chip.
    cycling_person = _rules(video_only=[person_crossing], chip_only=[read],
                            verdicts={person_crossing.key: person}, target_sport="cycling")
    assert cycling_person.late_pairs == []
    cycling_bicycle = _rules(video_only=[bicycle_crossing], chip_only=[read],
                             verdicts={bicycle_crossing.key: bicycle}, target_sport="cycling")
    assert cycling_bicycle.late_pairs == [("88", -520)]


def _box(bottom, cls, x=80):
    return Detection(cls, .91, x, bottom - 60, 60, 60)


def test_running_prefers_a_non_target_bicycle_when_both_cross_in_the_same_window():
    line = [(0, 100), (300, 100)]
    observations = [
        (0, [_box(120, BICYCLE), _box(120, PERSON)]),
        (600, [_box(100, BICYCLE), _box(100, PERSON)]),
        (1200, [_box(80, BICYCLE), _box(80, PERSON)]),
    ]
    cycling = check_target_tracks(observations, line, frame_size=(320, 240))
    running = check_target_tracks(observations, line, frame_size=(320, 240), preferred_label="pedestrian")
    assert cycling.state == running.state == "confirmed"
    assert cycling.label == LABEL_BICYCLE
    assert running.label == LABEL_BICYCLE


class _FakeDetector:
    def __init__(self, per_frame):
        self.per_frame = list(per_frame)

    def detect(self, _image):
        return self.per_frame.pop(0)


def _frames(count):
    return [np.zeros((1000, 1500, 3), np.uint8) for _ in range(count)]


def test_running_prefers_an_escort_vehicle_over_its_rider():
    line = [(0, 100), (300, 100)]
    observations = [
        (0, [_box(120, CAR), _box(120, PERSON)]),
        (600, [_box(100, CAR), _box(100, PERSON)]),
        (1200, [_box(80, CAR), _box(80, PERSON)]),
    ]
    running = check_target_tracks(observations, line, frame_size=(320, 240), preferred_label="pedestrian")
    assert running.state == "confirmed"
    assert running.label == "vehicle"


def test_running_classification_names_a_person_as_the_target_not_a_bicycle():
    points = [(700.0, 700.0 + offset) for offset in range(0, 90, 10)]
    plan = CropPlan.for_mode("up")
    person = Detection(PERSON, 0.9, 400.0, 300.0, 250.0, 500.0)
    bicycle = Detection(BICYCLE, 0.9, 400.0, 300.0, 250.0, 500.0)

    running = classify_frames(_FakeDetector([[person]]), _frames(1), points, plan,
                              target_label=LABEL_PEDESTRIAN)
    assert (running.label, running.decision) == (LABEL_PEDESTRIAN, DECISION_KEEP)

    car = Detection(CAR, 0.9, 400.0, 300.0, 250.0, 500.0)
    running_car = classify_frames(_FakeDetector([[person, car]]), _frames(1), points, plan,
                                  target_label=LABEL_PEDESTRIAN)
    assert (running_car.label, running_car.decision) == ("vehicle", DECISION_EXCLUDE)

    running_bike = classify_frames(_FakeDetector([[bicycle]]), _frames(1), points, plan,
                                   target_label=LABEL_PEDESTRIAN)
    assert (running_bike.label, running_bike.decision) == (LABEL_BICYCLE, DECISION_EXCLUDE)

    # Cycling keeps excluding a lone person and keeping a bicycle.
    assert classify_frames(_FakeDetector([[person]]), _frames(1), points, plan).decision == DECISION_EXCLUDE
    assert classify_frames(_FakeDetector([[bicycle]]), _frames(1), points, plan).decision == DECISION_KEEP
