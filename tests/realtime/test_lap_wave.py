from realtime.lap_wave import (
    FINAL,
    INTERMEDIATE,
    OVER,
    Passage,
    lap_role,
    match_wave,
    short_riders,
)


def test_only_the_last_lap_of_a_lap_group_is_final():
    assert lap_role(1, 3) == INTERMEDIATE
    assert lap_role(2, 3) == INTERMEDIATE
    assert lap_role(3, 3) == FINAL
    assert lap_role(4, 3) == OVER


def test_a_group_that_does_not_run_laps_is_all_final():
    assert lap_role(0, 0) == FINAL
    assert lap_role(1, 1) == FINAL


def test_a_read_whose_lap_is_not_known_is_listed_not_hidden():
    assert lap_role(0, 4) == FINAL
    assert short_riders([Passage("a", "16", 1000, 0, 4)]) == frozenset()


def test_a_rider_whose_last_passage_is_short_of_the_target_is_a_hint_not_a_judgement():
    passages = [
        Passage("a1", "16", 1000, 1, 3), Passage("a2", "16", 2000, 2, 3),  # 16 stopped at 2 of 3
        Passage("b1", "20", 1000, 1, 3), Passage("b2", "20", 2000, 2, 3), Passage("b3", "20", 3000, 3, 3),
        Passage("c1", "9", 1000, 1, 0),  # a group that does not run laps
    ]
    assert short_riders(passages) == {"16"}


def test_a_full_wave_is_paired_in_order_of_crossing_and_chip_time():
    match = match_wave(
        chips=[("c2", 5_000), ("c1", 4_000)],
        crossings=[("p2", 5_400), ("p1", 4_100)],
        clock_ok=True,
    )
    assert match.reasons == ()
    assert match.pairs == (("c1", "p1"), ("c2", "p2"))


def test_any_one_failed_condition_leaves_the_whole_wave_to_hand():
    chips = [("c1", 4_000), ("c2", 5_000)]
    crossings = [("p1", 4_100), ("p2", 5_100)]
    cases = [
        ({"clock_ok": False}, "录像未校时"),
        ({"clock_ok": True, "short_rider_count": 1}, "圈数不足"),
        ({"clock_ok": True, "maybe_several": True}, "可能多人"),
        ({"clock_ok": True, "near_counted": True}, "已点附近"),
        ({"clock_ok": True, "obstructed": True}, "视线被挡"),
    ]
    for flags, text in cases:
        match = match_wave(chips, crossings, **flags)
        assert match.pairs == ()
        assert any(text in reason for reason in match.reasons), flags


def test_a_count_that_does_not_agree_with_the_chips_is_left_to_hand():
    match = match_wave(
        chips=[("c1", 4_000), ("c2", 5_000)],
        crossings=[("p1", 4_100), ("p2", 5_100), ("p3", 5_900)],
        clock_ok=True,
    )
    assert match.pairs == ()
    assert "人数 3 与芯片 2 不一致" in match.reasons


def test_two_people_closer_than_the_gap_may_have_swapped_so_none_is_labelled():
    match = match_wave(
        chips=[("c1", 4_000), ("c2", 4_100)],
        crossings=[("p1", 4_050), ("p2", 4_150)],
        clock_ok=True,
    )
    assert match.pairs == ()
    assert any("顺序可能对错" in reason for reason in match.reasons)


def test_a_wave_without_final_lap_chips_is_not_labelled():
    match = match_wave(chips=[], crossings=[], clock_ok=True)
    assert match.pairs == ()
    assert match.reasons == ("本波没有最后一圈的芯片",)
