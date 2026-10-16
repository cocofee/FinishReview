import json
from dataclasses import replace

import numpy as np
import pytest

from realtime.clock_sync import (
    ClockAnchor,
    ClockCrossing,
    ClockModel,
    ClockModelStore,
    ClockSyncError,
    calibrate_camera,
    check_anchors,
    coarse_offset,
    isolated_pairs,
    manual_anchor_model,
    robust_line_fit,
)


START = 1_789_784_000_000
HOUR = 3_600_000


def _race(seed=0, *, offset=1_200.0, drift_per_hour=-334.0, riders=300, jitter=25.0, sessions=("s1",)):
    """Chip times plus crossings that follow ``offset + drift``; a few extras."""

    rng = np.random.default_rng(seed)
    chips = np.sort(START + rng.uniform(0, 1.2 * HOUR, riders)).astype(np.int64)
    expected = offset + drift_per_hour / HOUR * (chips - START)
    crossings = chips + expected + rng.normal(0, jitter, riders)
    noise = START + rng.uniform(0, 1.2 * HOUR, 60)  # pedestrians, shadows
    session_for = lambda t: sessions[min(len(sessions) - 1, int((t - START) / (1.2 * HOUR) * len(sessions)))]
    items = [ClockCrossing(int(t), session_for(t)) for t in np.concatenate([crossings, noise])]
    return chips.tolist(), sorted(items, key=lambda item: item.recorder_ms)


@pytest.mark.parametrize("days", [-3, 3])
def test_date_offset_preserves_drift_and_inverse_in_official_time_domain(days):
    chips, original = _race()
    shift = days * 86_400_000
    shifted = [replace(item, recorder_ms=item.recorder_ms + shift) for item in original]
    base = calibrate_camera(original, chips, camera_index=1)
    result = calibrate_camera(shifted, chips, camera_index=1, date_offset_ms=shift)
    assert result.model is not None and base.model is not None
    assert result.model.reference_ms == base.model.reference_ms
    assert result.model.slope == pytest.approx(base.model.slope)
    assert result.residuals_ms == pytest.approx(base.residuals_ms)
    for chip in chips:
        assert result.model.to_recorder(chip) == base.model.to_recorder(chip) + shift
        assert abs(result.model.to_chip(result.model.to_recorder(chip)) - chip) <= 1


def test_coarse_search_finds_single_peak_far_from_any_manual_guess():
    chips, crossings = _race(offset=-58_000.0, drift_per_hour=0.0)
    coarse = coarse_offset([item.recorder_ms for item in crossings], chips)

    assert coarse is not None
    assert abs(coarse.offset_ms - (-58_000)) <= 150
    assert coarse.distinct
    assert coarse.support > 10 * coarse.background


def test_coarse_search_reports_ambiguous_peak():
    rng = np.random.default_rng(3)
    chips = START + np.sort(rng.uniform(0, HOUR, 50))
    crossings = START + np.sort(rng.uniform(0, HOUR, 60))
    coarse = coarse_offset(crossings, chips)

    assert coarse is not None
    assert not coarse.distinct


def test_isolated_pairs_skip_close_riders_and_ambiguous_crossings():
    chips = [0, 3_000, 20_000, 40_000]
    crossings = [1_000, 4_000, 21_000, 41_000, 41_900]

    pairs = isolated_pairs(crossings, chips, 1_000)

    # 0 and 3_000 are within five seconds of each other; 40_000 has two
    # crossings within the 1.5 s window, so only 20_000 is usable.
    assert [(chip, difference) for _index, chip, difference, _crossing in pairs] == [(20_000, 1_000)]


def test_robust_fit_rejects_outliers():
    t = np.arange(0, 100_000, 1_000, dtype=float)
    y = 500 + 0.001 * t
    y[[5, 50, 70]] += [3_000, -2_500, 900]

    a, b, mean, keep, residuals = robust_line_fit(t, y)

    assert keep.sum() == len(t) - 3
    assert abs(b - 0.001) < 1e-6
    assert abs(a + b * (0 - mean) - 500) < 1


def test_calibrate_camera_recovers_offset_and_drift_without_anchor():
    chips, crossings = _race(seed=11)
    result = calibrate_camera(crossings, chips, camera_index=1)

    model = result.model
    assert model is not None
    assert abs(model.slope_ms_per_hour - (-334)) < 25
    assert abs(model.offset_at(START) - 1_200) < 25
    assert model.residual_mad_ms < 40
    assert model.support >= 20
    assert result.coarse.distinct


def test_model_applies_offset_exactly_once_in_both_directions():
    model = ClockModel(
        camera_index=1, intercept_ms=-1_950.0, slope=-889 / HOUR, reference_ms=START,
        valid_from_ms=START, valid_to_ms=START + HOUR, support=30, candidates=34,
        residual_mad_ms=27.0, coarse_offset_ms=-1_950, coarse_support=244,
    )
    for chip in (START, START + HOUR // 2, START + HOUR):
        recorder = model.to_recorder(chip)
        assert abs(model.to_chip(recorder) - chip) <= 1
        assert recorder - chip == model.offset_at(chip)


def test_sessions_far_from_shared_drift_line_are_reported():
    chips, crossings = _race(seed=5, riders=400)
    # A short second recording session whose latency differs by 400 ms.
    late = START + int(1.0 * HOUR)
    shifted = [
        ClockCrossing(item.recorder_ms + 400, "second") if item.recorder_ms >= late
        else ClockCrossing(item.recorder_ms, "main")
        for item in crossings
    ]
    result = calibrate_camera(shifted, chips, camera_index=1)

    assert result.model is not None
    assert abs(result.session_medians_ms["main"]) < 30
    assert any("second" in warning for warning in result.warnings)
    assert not any("main" in warning for warning in result.warnings)
    # The shared drift line is fitted on the main session; the short session
    # keeps it and only gets its own measured shift.
    model = result.model
    assert abs(model.slope_ms_per_hour - (-334)) < 40
    assert abs(dict(model.session_offsets)["second"] - 400) < 40
    chip = START + int(1.1 * HOUR)
    assert abs(model.offset_at(chip, "second") - model.offset_at(chip) - dict(model.session_offsets)["second"]) <= 1
    assert abs(model.to_chip(model.to_recorder(chip, "second"), "second") - chip) <= 1
    assert ClockModel.from_payload(model.to_payload()) == model


def test_manual_anchor_is_only_checked_against_automatic_model():
    chips, crossings = _race(seed=2, offset=-1_950.0, drift_per_hour=-889.0)
    model = calibrate_camera(crossings, chips, camera_index=2).model
    good = ClockAnchor(2, chips[10], model.offset_at(chips[10]) + 120, "manual_calibration", "4")
    wrong = ClockAnchor(2, chips[11], model.offset_at(chips[11]) - 58_300, "manual_calibration", "1")
    other_camera = ClockAnchor(1, chips[12], 0, "manual_calibration", "9")

    checks = check_anchors(model, [good, wrong, other_camera])

    assert [check.within_tolerance for check in checks] == [True, False]
    assert "超过 300 ms" in checks[1].message
    assert checks[1].difference_ms < -58_000


def test_clock_model_store_versions_refits_and_keeps_old_versions(tmp_path):
    chips, crossings = _race(seed=7)
    store = ClockModelStore(tmp_path / "video_clock_models.jsonl")
    first = store.publish(calibrate_camera(crossings, chips, camera_index=1).model)
    same = store.publish(calibrate_camera(crossings, chips, camera_index=1).model)
    shifted = [ClockCrossing(item.recorder_ms + 200, item.session_id) for item in crossings]
    second = store.publish(calibrate_camera(shifted, chips, camera_index=1).model)

    assert (first.version, same.version, second.version) == (1, 1, 2)
    reopened = ClockModelStore(tmp_path / "video_clock_models.jsonl")
    assert reopened.latest(1).version == 2
    assert reopened.get(1, 1).offset_at(START) == first.offset_at(START)
    assert len(reopened.models(1)) == 2


def test_clock_model_store_rejects_corrupt_middle_line(tmp_path):
    path = tmp_path / "video_clock_models.jsonl"
    path.write_text("{not json}\n" + json.dumps({"schema_version": 1}) + "\n", encoding="utf-8")
    with pytest.raises(ClockSyncError):
        ClockModelStore(path)


def test_calibration_without_isolated_riders_fails_with_reason():
    chips = [START + index * 1_000 for index in range(20)]
    crossings = [chip + 900 for chip in chips]
    result = calibrate_camera(crossings, chips, camera_index=1)

    assert result.model is None
    assert result.warnings


def test_manual_anchor_model_fixes_one_offset_without_inventing_drift():
    anchors = [
        ClockAnchor(2, START, 500, "manual_calibration", "9", "s1"),            # other camera
        ClockAnchor(1, START + 10_000, 1_200, "manual_calibration", "7", "other"),  # other recording
        ClockAnchor(1, START, -1_950, "manual_calibration", "4", "s1"),
    ]

    model = manual_anchor_model(anchors, camera_index=1, sessions=("s1",))

    assert model is not None
    assert model.source == "manual_anchor" and model.source_text == "人工首人锚点"
    assert model.slope == 0.0 and model.support == 1 and model.candidates == 1
    assert model.session_ids == ("s1",)
    assert model.notes
    for chip in (START - HOUR, START, START + HOUR):
        assert model.offset_at(chip) == -1_950
        assert model.to_recorder(chip) == chip - 1_950
        assert model.to_chip(chip - 1_950) == chip
    assert manual_anchor_model(anchors, camera_index=1, sessions=("s2",)) is None
    assert manual_anchor_model([], camera_index=1, sessions=("s1",)) is None


def test_manual_anchor_model_keeps_each_confirmed_session_on_its_own_offset():
    anchors = [
        ClockAnchor(1, START + HOUR, 1_450, "manual_calibration", "2", "s2"),
        ClockAnchor(1, START, 1_200, "manual_calibration", "1", "s1"),
    ]

    model = manual_anchor_model(anchors, camera_index=1, sessions=("s1", "s2", "s3"))

    assert model.session_ids == ("s1", "s2") and model.support == 2
    assert model.offset_at(START) == 1_200
    assert model.offset_at(START, "s1") == 1_200
    assert model.offset_at(START + HOUR, "s2") == 1_450
    assert model.offset_at(START + HOUR) == 1_200  # no session named: the primary offset
    assert dict(model.session_offsets) == {"s2": 250.0}
    # s3 has no anchor, so the model never claims it.
    assert model.session_ids.count("s3") == 0


def test_anchor_check_compares_on_the_anchors_recording_session():
    model = ClockModel(
        camera_index=1, intercept_ms=-1_950.0, slope=0.0, reference_ms=START,
        valid_from_ms=START, valid_to_ms=START + HOUR, support=30, candidates=34,
        residual_mad_ms=27.0, coarse_offset_ms=-1_950, coarse_support=244,
        session_offsets=(("second", 400.0),),
    )
    chip = START + 30_000
    on_session = ClockAnchor(1, chip, -1_550, "manual_calibration", "7", "second")
    unknown = ClockAnchor(1, chip, -1_550, "manual_calibration", "7", "")

    session_check, unknown_check = check_anchors(model, [on_session, unknown])

    assert session_check.difference_ms == 0 and session_check.within_tolerance
    assert unknown_check.difference_ms == 400 and not unknown_check.within_tolerance


def test_same_fit_keeps_source_and_session_set_apart(tmp_path):
    store = ClockModelStore(tmp_path / "video_clock_models.jsonl")
    auto = ClockModel(
        camera_index=1, intercept_ms=-1_950.0, slope=-889 / HOUR, reference_ms=START,
        valid_from_ms=START, valid_to_ms=START + HOUR, support=30, candidates=34,
        residual_mad_ms=27.0, coarse_offset_ms=-1_950, coarse_support=244,
        source="auto", session_ids=("s1", "s2"),
    )
    manual = replace(
        auto, source="manual_anchor", session_ids=("s1",), support=1,
        residual_mad_ms=0.0, coarse_support=0,
    )

    # Equal numbers, different source or session set: not the same fit.
    assert not auto.same_fit(manual) and not manual.same_fit(auto)
    assert not auto.same_fit(replace(auto, source="other"))
    assert not auto.same_fit(replace(auto, session_ids=("s1",)))
    assert auto.same_fit(replace(auto, session_ids=("s2", "s1")))  # order does not matter
    assert auto.same_fit(replace(auto, support=7, notes=("支持数不同不影响拟合",)))

    first = store.publish(auto)
    second = store.publish(manual)
    third = store.publish(auto)
    assert (first.version, second.version, third.version) == (1, 2, 3)
    assert (first.source, second.source, third.source) == ("auto", "manual_anchor", "auto")
    reopened = ClockModelStore(tmp_path / "video_clock_models.jsonl")
    assert reopened.get(1, 2).source == "manual_anchor" and reopened.get(1, 2).support == 1
    assert reopened.get(1, 1).source == "auto"
    assert reopened.latest(1) == third
