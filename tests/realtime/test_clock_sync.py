import json

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
