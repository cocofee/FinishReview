import numpy as np

from realtime.crossing_detection import DetectionParams, detect_crossings, frame_interval_ms
from realtime.crossing_pairing import pair_crossings


def _strip(frames=1_200, rows=500, seed=0):
    rng = np.random.default_rng(seed)
    base = rng.integers(150, 170, (rows, 1, 3)).astype(np.uint8)
    strip = np.repeat(base, frames, axis=1)
    noise = rng.integers(-3, 4, strip.shape)
    return np.clip(strip.astype(int) + noise, 0, 255).astype(np.uint8)


def _rider(strip, column, rows=(200, 320), width=4, colour=(20, 30, 200)):
    strip[rows[0]:rows[1], column:column + width] = colour


def test_riders_become_crossings_and_standing_people_an_obstruction():
    strip = _strip()
    pts = np.arange(strip.shape[1]) * 50.0
    for column in (100, 300, 301 + 600):
        _rider(strip, column)
    # Someone stands on the line for eight seconds and keeps moving a little.
    strip[100:180, 500:660] = (40, 40, 40)
    strip[100:180, 500:660:7] = (60, 50, 45)

    result = detect_crossings(strip, pts)

    riders = [item for item in result.crossings if item.rider_like and not item.in_obstruction]
    assert [item.c0 for item in riders] == [100, 300, 901]
    assert all(item.rows >= 20 for item in riders)
    # The rolling median absorbs the middle of a long stay; its edges remain.
    assert [item[0] for item in result.obstructions] == [489, 648]
    assert result.frame_ms == 50.0


def test_fragments_of_one_rider_are_merged():
    strip = _strip()
    pts = np.arange(strip.shape[1]) * 50.0
    _rider(strip, 400, rows=(200, 240))
    _rider(strip, 401, rows=(250, 300))

    result = detect_crossings(strip, pts)

    riders = [item for item in result.crossings if item.rider_like]
    assert len(riders) == 1
    assert riders[0].fragments == 2 and riders[0].r0 == 200 and riders[0].r1 == 299


def test_parameters_follow_the_real_frame_interval():
    strip = _strip(frames=900)
    for column in (100, 600):
        _rider(strip, column, width=22)  # 22 frames: 0.88 s at 25 fps, 1.1 s at 20 fps
    at_25 = detect_crossings(strip, np.arange(900) * 40.0)
    at_20 = detect_crossings(strip, np.arange(900) * 50.0)

    def riders(result):
        return sum(item.rider_like and not item.in_obstruction for item in result.crossings)

    assert riders(at_25) == 2
    assert riders(at_20) == 0
    assert len(at_20.obstructions) == 2
    assert frame_interval_ms(np.array([0.0, 1.0, 50.0, 100.0, 150.0])) == 50.0
    assert DetectionParams().rider_max_ms == 1_000


def test_pairing_is_one_to_one_and_order_preserving():
    crossings = ["a", "b", "c", "d"]
    crossing_ms = [10_000, 10_300, 20_000, 40_000]
    chips = ["x", "y", "z"]
    # 650 ms would be inside the window but costs more than skipping both.
    expected = [10_100, 20_350, 55_000]

    pairing = pair_crossings(crossings, crossing_ms, chips, expected)

    assert [(c, r) for c, r, _residual in pairing.matched] == [("a", "x"), ("c", "y")]
    assert pairing.video_only == ("b", "d")
    assert pairing.chip_only == ("z",)
    residuals = [residual for _c, _r, residual in pairing.matched]
    assert residuals == [-100.0, -350.0]
    assert pair_crossings(["c"], [20_000], ["y"], [20_650]).matched == ()


def test_pairing_never_crosses_order_even_when_nearest_disagrees():
    # Two riders 200 ms apart, chips swapped in time order by 250 ms jitter:
    # the alignment keeps order instead of taking each nearest chip.
    pairing = pair_crossings(["first", "second"], [1_000, 1_200], ["p", "q"], [1_150, 1_250])

    assert [(c, r) for c, r, _residual in pairing.matched] == [("first", "p"), ("second", "q")]


def test_pairing_blocks_are_independent_and_fast():
    rng = np.random.default_rng(3)
    chips = np.sort(rng.uniform(0, 7_200_000, 3_000))
    crossings = chips + rng.normal(0, 40, chips.size)

    pairing = pair_crossings(list(range(chips.size)), crossings, list(range(chips.size)), chips)

    assert len(pairing.matched) >= 2_950
