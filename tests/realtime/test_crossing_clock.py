import pytest

from realtime.crossing_clock import CrossingClock, align_crossings, fit_crossing_clock


def arrivals():
    origin = 1_780_000_000_000
    # Irregular arrival intervals, not a periodic signal with ambiguous peaks.
    gaps = [0, 13117, 37954, 59901, 88322, 125707, 183029, 246336, 335515, 421781, 507816]
    chips = [origin + t for t in gaps]
    videos = [t + 58_000 - (t - origin) * 889 / 3_600_000 for t in chips]
    return chips, videos


def test_large_offset_drift_epoch_precision_and_inverse():
    chips, videos = arrivals()
    videos[5] += 1000  # isolated false anchor must be rejected
    clock = fit_crossing_clock(chips, videos)
    assert clock.drift_ms_per_hour == pytest.approx(-889, abs=.01)
    assert clock.anchors == 10
    assert clock.residual_median_ms < .01
    for chip in (chips[0], chips[-1], chips[-1] + 3_600_000):
        assert clock.to_chip(clock.to_video(chip)) == pytest.approx(chip, abs=.001)
    assert abs(clock.manual_difference(chips[0], 0)) > 300


def test_unsupported_fit_never_falls_back_to_zero():
    with pytest.raises(ValueError, match="等待"):
        fit_crossing_clock([1, 2], [3, 4])
    with pytest.raises(ValueError, match="等待"):
        fit_crossing_clock(range(0, 10000, 1000), range(100, 10100, 1000))


def test_repeated_pattern_with_two_equal_offsets_is_ambiguous():
    chips, videos = arrivals()
    with pytest.raises(ValueError, match="峰不唯一"):
        fit_crossing_clock(chips, videos + [v - 40_000 for v in videos])


def test_alignment_is_one_to_one_ordered_and_handles_extra_reads():
    clock = CrossingClock(0, 1000, 0, 0, 100000, 5, 0, 1000, "test")
    chips = [10000, 10020, 20000, 30000]
    videos = [11005, 21000, 25500, 31000]
    pairs = align_crossings(chips, videos, clock)
    assert [(c, v) for c, v, _ in pairs] == [(0, 0), (2, 1), (3, 3)]
    assert len({c for c, _, _ in pairs}) == len(pairs)
    assert len({v for _, v, _ in pairs}) == len(pairs)


def test_alignment_indices_refer_to_unsorted_inputs():
    clock = CrossingClock(0, 0, 0, 0, 100000, 5, 0, 0, "test")
    assert [(c, v) for c, v, _ in align_crossings([20000, 10000], [10000, 20000], clock)] == [(1, 0), (0, 1)]
