import numpy as np

from realtime.analysis_resources import CpuMonitor, ResourceGovernor
from realtime.bike_check import (
    BICYCLE,
    DECISION_DISPUTED,
    DECISION_EXCLUDE,
    DECISION_KEEP,
    LABEL_BICYCLE,
    LABEL_PEDESTRIAN,
    LABEL_VEHICLE,
    MOTORCYCLE,
    PERSON,
    BikeVerdict,
    CropPlan,
    Detection,
    classify_frames,
)
from realtime.chip_history import ChipRead
from realtime.clock_sync import ClockModel
from realtime.problem_items import (
    KIND_BUNCH,
    KIND_CHIP_NO_CROSSING,
    KIND_NO_CHIP_CROSSING,
    KIND_STANDING,
    KIND_TWO_CHIPS_ONE_CROSSING,
    STATUS_CLOSED_UPSTREAM,
    STATUS_DISPUTED,
    STATUS_EXCLUDED,
)
from realtime.problem_rules import CrossingRecord, Obstruction, build_video_items


T0 = 1_789_784_000_000
MODEL = ClockModel(
    camera_index=1, intercept_ms=1_000.0, slope=0.0, reference_ms=T0, valid_from_ms=T0,
    valid_to_ms=T0 + 3_600_000, support=50, candidates=60, residual_mad_ms=25.0,
    coarse_offset_ms=1_000, coarse_support=100, version=3,
)


def _read(bib, chip_ms, *, active=True, manual=False):
    return ChipRead(
        event_id=f"race-stage-1-passage-{bib}", bib=bib, chip_id=bib, group_id="1", group_name="组",
        athlete_name="", chip_ms=chip_ms, passage_time_ms=chip_ms % 86_400_000, lap=0,
        received_at_ms=chip_ms, revision=1, latest_revision=1, active_now=active, manual_entry=manual,
    )


def _crossing(recorder_ms, *, row=250, obstruction=False):
    return CrossingRecord(
        camera_index=1, session_id="s", segment_id="seg", video_path="v.mkv",
        recorder_ms=recorder_ms, frame_index=(recorder_ms - T0) // 50, position_ms=recorder_ms - T0,
        row=row, r0=row - 20, r1=row + 20, in_obstruction=obstruction, contact=(100.0, 200.0),
    )


def _rules(**kwargs):
    defaults = dict(camera_index=1, model=MODEL, matched=(), video_only=(), chip_only=(), no_video=())
    defaults.update(kwargs)
    return build_video_items(**defaults)


def test_late_chip_is_paired_and_extra_crossings_near_chips_are_merged():
    late = _read("88", T0 + 60_000)
    paired = _read("10", T0 + 120_000)
    outcome = _rules(
        matched=[(_crossing(T0 + 121_000), paired, 0.0)],
        video_only=[_crossing(T0 + 61_480), _crossing(T0 + 121_600)],
        chip_only=[late],
    )

    assert outcome.late_pairs == [("88", 480)]
    assert outcome.duplicates == 1
    assert [item.kind for item in outcome.items] == []
    assert {item["bib"] for item in outcome.auto_passed} == {"88", "10"}
    assert all(item["source"] == "auto" for item in outcome.auto_passed)


def test_isolated_late_chip_gets_the_wider_window():
    outcome = _rules(video_only=[_crossing(T0 + 62_400)], chip_only=[_read("744", T0 + 60_000)])

    assert outcome.late_pairs == [("744", 1_400)]


def test_unpaired_crossings_chips_and_repeated_spots():
    chip_only = [_read("5", T0 + 200_000), _read("6", T0 + 300_000, active=False)]
    paired = _read("7", T0 + 300_200)
    standing = [_crossing(T0 + 500_000 + step * 20_000, row=420) for step in range(4)]
    outcome = _rules(
        matched=[(_crossing(T0 + 301_200), paired, 0.0)],
        video_only=[_crossing(T0 + 400_000), *standing],
        chip_only=chip_only,
        manual_entries=[],
    )

    by_kind = {item.kind: item for item in outcome.items}
    assert by_kind[KIND_NO_CHIP_CROSSING].chip_time_ms == T0 + 399_000
    assert by_kind[KIND_NO_CHIP_CROSSING].evidence["frame"]["calibration_version"] == 3
    assert by_kind[KIND_STANDING].evidence["count"] == 4
    assert by_kind[KIND_CHIP_NO_CROSSING].bibs == ("5",)
    two = by_kind[KIND_TWO_CHIPS_ONE_CROSSING]
    assert set(two.bibs) == {"6", "7"} and two.status == STATUS_CLOSED_UPSTREAM


def test_manual_entry_closes_crossing_and_bike_verdicts_decide_exclusion():
    crossing = _crossing(T0 + 50_000)
    walker = _crossing(T0 + 70_000)
    unsure = _crossing(T0 + 90_000)
    verdicts = {
        walker.key: BikeVerdict(LABEL_PEDESTRIAN, 0.9, DECISION_EXCLUDE),
        unsure.key: BikeVerdict(LABEL_PEDESTRIAN, 0.3, DECISION_DISPUTED, "把握不高"),
    }
    outcome = _rules(
        video_only=[crossing, walker, unsure], verdicts=verdicts,
        manual_entries=[_read("307", T0 + 49_300, manual=True)],
    )

    statuses = {item.chip_time_ms: item.status for item in outcome.items}
    assert statuses[T0 + 49_000] == STATUS_CLOSED_UPSTREAM
    assert statuses[T0 + 69_000] == STATUS_EXCLUDED
    assert statuses[T0 + 89_000] == STATUS_DISPUTED


def test_bunches_and_obstructions_get_manual_check_items():
    reads = [_read(str(index), T0 + 10_000 + index * 500) for index in range(5)]
    matched = [(_crossing(read.chip_ms + 1_000), read, 0.0) for read in reads[:4]]
    outcome = _rules(
        matched=matched, chip_only=[reads[4]],
        obstructions=[Obstruction(1, T0 + 100_000, T0 + 108_000), Obstruction(1, T0 + 120_000, T0 + 125_000)],
    )

    bunch = next(item for item in outcome.items if item.kind == KIND_BUNCH)
    assert bunch.evidence["chip_count"] == 5 and bunch.evidence["video_count"] == 4
    # The bunch opens at its first crossing's source frame (card and jump).
    assert bunch.evidence["frame"]["position_ms"] == reads[0].chip_ms + 1_000 - T0
    assert any("不一致" in item.detail for item in outcome.items if item.kind == KIND_BUNCH)
    assert sum(item.kind == "obstruction" for item in outcome.items) == 1


class _FakeDetector:
    def __init__(self, per_frame):
        self.per_frame = list(per_frame)

    def detect(self, _image):
        return self.per_frame.pop(0)


def _frames(count):
    return [np.zeros((1000, 1500, 3), np.uint8) for _ in range(count)]


def test_classify_keeps_bicycles_and_only_excludes_confident_consistent_people():
    points = [(700.0, 700.0 + offset) for offset in range(0, 90, 10)]
    bike = Detection(BICYCLE, 0.8, 400.0, 300.0, 250.0, 500.0)
    person = Detection(PERSON, 0.9, 400.0, 300.0, 250.0, 500.0)
    weak = Detection(PERSON, 0.3, 400.0, 300.0, 250.0, 500.0)
    moto = Detection(MOTORCYCLE, 0.85, 400.0, 300.0, 250.0, 500.0)
    plan = CropPlan.for_mode("up")

    assert classify_frames(_FakeDetector([[bike, person]]), _frames(1), points, plan).decision == DECISION_KEEP
    verdict = classify_frames(_FakeDetector([[person]]), _frames(1), points, plan)
    assert (verdict.label, verdict.decision) == (LABEL_PEDESTRIAN, DECISION_EXCLUDE)
    assert classify_frames(_FakeDetector([[weak]]), _frames(1), points, plan).decision == DECISION_DISPUTED
    crowded = classify_frames(_FakeDetector([[person]]), _frames(1), points, plan, crowded=True)
    assert crowded.decision == DECISION_DISPUTED and "拥挤" in crowded.reason
    vehicle = classify_frames(_FakeDetector([[moto]]), _frames(1), points, plan)
    assert vehicle.label == LABEL_VEHICLE and vehicle.vehicle == "摩托车"


def test_vehicles_may_miss_a_frame_but_pedestrians_must_be_clear_in_every_frame():
    points = [(700.0, 700.0 + offset) for offset in range(0, 90, 10)]
    person = Detection(PERSON, 0.9, 400.0, 300.0, 250.0, 500.0)
    weak = Detection(PERSON, 0.3, 400.0, 300.0, 250.0, 500.0)
    car = Detection(MOTORCYCLE, 0.9, 400.0, 300.0, 250.0, 500.0)
    bike = Detection(BICYCLE, 0.4, 400.0, 300.0, 250.0, 500.0)
    plan = CropPlan.for_mode("up")

    def verdict(*frames):
        return classify_frames(_FakeDetector(list(frames)), _frames(len(frames)), points, plan)

    # A fast motorcycle is out of view in one of three frames: still excluded.
    assert verdict([], [car], [car]).decision == DECISION_EXCLUDE
    assert verdict([], [], [car]).decision == DECISION_DISPUTED
    assert verdict([bike, car], [car], [car]).decision == DECISION_DISPUTED
    # A person who appears late moved fast, maybe a rider whose bicycle was
    # missed: disputed. A walking person is clear in every frame.
    assert verdict([], [person], [person]).decision == DECISION_DISPUTED
    assert verdict([weak], [person], [person]).decision == DECISION_DISPUTED
    assert verdict([person], [person], [person]).decision == DECISION_EXCLUDE


def test_side_mode_uses_earlier_frames_and_stops_at_first_bicycle():
    points = [(400.0, 500.0 + offset) for offset in range(0, 90, 10)]
    plan = CropPlan.for_mode("side-right")
    assert plan.offsets_ms == (-400, -200, 50)
    detector = _FakeDetector([[], [Detection(BICYCLE, 0.7, 200.0, 400.0, 300.0, 300.0)], []])

    verdict = classify_frames(detector, _frames(3), points, plan)

    assert verdict.label == LABEL_BICYCLE and verdict.frames == 2


def test_governor_pauses_while_cpu_is_busy():
    class Monitor(CpuMonitor):
        def __init__(self, loads):
            self.loads = list(loads)

        def sample(self):
            return self.loads.pop(0)

    sleeps = []
    governor = ResourceGovernor(monitor=Monitor([0.95, 0.9, 0.5]), check_interval_s=0.0, sleep=sleeps.append)
    governor.checkpoint()

    assert governor.pauses == 1 and len(sleeps) == 2


def test_line_activity_before_the_first_rider_is_excluded_not_listed():
    outcome = _rules(video_only=[_crossing(T0 + 5_000), _crossing(T0 + 600_000)], race_start_ms=T0 + 300_000)

    statuses = sorted((item.status, item.status_note) for item in outcome.items)
    assert statuses == [("excluded", "已排除：赛前活动"), ("open", "")]
    assert outcome.stats["before_race"] == 1


def test_items_inside_bunches_and_crowded_periods_are_folded_into_the_group():
    reads = [_read(str(index), T0 + 10_000 + index * 400) for index in range(5)]
    matched = [(_crossing(read.chip_ms + 1_000), read, 0.0) for read in reads[:4]]
    walker = _crossing(T0 + 103_000)
    rider = _crossing(T0 + 104_500)
    verdicts = {
        walker.key: BikeVerdict(LABEL_PEDESTRIAN, 0.9, DECISION_DISPUTED, "终点线拥挤时段"),
        rider.key: BikeVerdict(LABEL_BICYCLE, 0.9, "keep"),
    }
    outcome = _rules(
        matched=matched, chip_only=[reads[4]], video_only=[walker, rider], verdicts=verdicts,
        obstructions=[Obstruction(1, T0 + 100_000, T0 + 112_000)],
    )

    bunch = next(item for item in outcome.items if item.kind == KIND_BUNCH)
    crowd = next(item for item in outcome.items if item.kind == "obstruction")
    two = next(item for item in outcome.items if item.kind == KIND_TWO_CHIPS_ONE_CROSSING)
    assert two.grouped == bunch.item_id and not two.pending
    assert bunch.evidence["members"] == [two.item_id] and "组内另有 1 条" in bunch.detail
    folded = [item for item in outcome.items if item.grouped == crowd.item_id]
    assert [item.status for item in folded] == [STATUS_DISPUTED]
    separate = [item for item in outcome.items if item.kind == KIND_NO_CHIP_CROSSING and not item.grouped]
    assert len(separate) == 1 and separate[0].pending


def test_reads_paired_only_to_motion_in_a_crowded_period_are_not_auto_passed():
    burst = _read("1", T0 + 200_000)
    rider = _read("3", T0 + 205_000)
    outside = _read("2", T0 + 400_000)
    outcome = _rules(
        matched=[
            (_crossing(T0 + 201_000, obstruction=True), burst, 0.0),
            (_crossing(T0 + 206_000), rider, 0.0),
            (_crossing(T0 + 401_000), outside, 0.0),
        ],
        obstructions=[Obstruction(1, T0 + 195_000, T0 + 215_000)],
    )

    # A rider-like crossing in the crowded period still matches its chip; a
    # chip that only lines up with motion inside the blockage does not pass.
    assert sorted(item["bib"] for item in outcome.auto_passed) == ["2", "3"]
    crowd = next(item for item in outcome.items if item.kind == "obstruction")
    assert crowd.bibs == ("1", "3") and crowd.pending
    assert crowd.evidence["unverified"] == ["1"] and "未自动通过：1" in crowd.detail
    assert crowd.evidence["frame"]["position_ms"] == 201_000
    assert outcome.stats["paired_in_crowd"] == 1


def test_memory_readings_are_positive_or_unavailable():
    from realtime.analysis_resources import available_memory_mb, peak_memory_mb

    for value in (available_memory_mb(), peak_memory_mb()):
        assert value is None or value > 0


def test_changes_at_two_places_in_the_same_moment_are_one_item():
    outcome = _rules(video_only=[
        _crossing(T0 + 50_000, row=100), _crossing(T0 + 50_000, row=400), _crossing(T0 + 90_000, row=250),
    ])

    items = sorted((item for item in outcome.items if item.kind == "no_chip_crossing"), key=lambda i: i.chip_time_ms)
    assert len(items) == 2
    assert "另有 1 处变化" in items[0].detail and "另有" not in items[1].detail
