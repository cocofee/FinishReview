from realtime.extra_person import fit_wave

# 古浪, the wave of 11:28:56–11:29:06 as the operator clicked it on 11-14 with 6 号's chip
# taken out (ms after 11:28:50).  The third click is 6 号.
CLICKS = [6420, 6820, 7020, 8300, 8580, 8580, 8860, 9020, 10180, 10260, 11500, 11860, 12940, 13060, 14740, 16180]
CHIPS = [6450, 6890, 8380, 8620, 8620, 8890, 9040, 10270, 10320, 11520, 11850, 12990, 13080, 14710, 16180]


def test_the_person_without_a_chip_is_found_where_the_times_shift():
    """11-14：以前“多 1”记在这一波最后一个人身上。按时间看，多的是第 3 个点的人。"""

    fit = fit_wave(CLICKS, CHIPS)
    assert [extra.click for extra in fit.extras] == [2]
    # 14 号 and 6 号 crossed 0.2 s apart: the times cannot say which of the two has no chip.
    assert fit.extras[0].candidates == (1, 2)
    assert fit.chip_of_click[:4] == (0, 1, None, 2) and fit.chip_of_click[-1] == 14
    assert fit.worst_ms <= 100                       # everyone else sits on their own chip


def test_a_lone_rider_ahead_of_a_chip_is_the_extra_one():
    fit = fit_wave([51_500, 54_140], [54_151])
    assert [extra.click for extra in fit.extras] == [0] and fit.extras[0].sure


def test_riders_too_close_to_tell_apart_are_all_named():
    """大集团：人挨得太近，时间分不出是谁，只给范围，交给裁判看。"""

    clicks = [0, 1000, 1100, 1200, 1300, 3000]
    chips = [0, 1000, 1150, 1300, 3000]              # one of the three in the middle has no chip
    fit = fit_wave(clicks, chips)
    extra, = fit.extras
    assert not extra.sure
    assert set(extra.candidates) <= {1, 2, 3, 4} and extra.click in extra.candidates
    assert {2, 3} <= set(extra.candidates)
    assert 0 not in extra.candidates and 5 not in extra.candidates


def test_two_people_without_chips_in_one_wave():
    clicks = [0, 800, 2000, 3500, 5000, 6000]
    chips = [0, 2000, 3500, 6000]
    fit = fit_wave(clicks, chips)
    assert [extra.click for extra in fit.extras] == [1, 4]
    assert all(extra.sure for extra in fit.extras)


def test_a_wave_whose_clicks_all_sit_a_little_late_is_still_read_right():
    late = [t + 180 for t in CLICKS]
    fit = fit_wave(late, CHIPS)
    assert [extra.click for extra in fit.extras] == [2] and 100 <= fit.offset_ms <= 260


def test_nothing_is_said_when_clicks_do_not_outnumber_chips():
    assert fit_wave(CHIPS, CHIPS) is None
    assert fit_wave(CHIPS[:-1], CHIPS) is None
