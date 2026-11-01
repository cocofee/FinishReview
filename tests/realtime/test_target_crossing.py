from dataclasses import replace

import pytest

from realtime.bike_check import BICYCLE, CAR, MOTORCYCLE, PERSON, Detection
from realtime.target_crossing import check_target_tracks


LINE = [(0, 100), (300, 100)]


def box(bottom, cls=PERSON, x=80):
    return Detection(cls, .91, x, bottom - 60, 60, 60)


@pytest.mark.parametrize("cls,label", [(PERSON, "pedestrian"), (BICYCLE, "bicycle"),
                                       (MOTORCYCLE, "vehicle"), (CAR, "vehicle")])
def test_same_target_must_change_sides(cls, label):
    result = check_target_tracks([(0, [box(120, cls)]), (600, [box(100, cls)]),
                                  (1200, [box(80, cls)])], LINE, frame_size=(320, 240))
    assert result.state == "confirmed" and result.label == label
    assert result.direction == -1
    if cls == MOTORCYCLE:
        assert result.vehicle


@pytest.mark.parametrize("bottoms", [(125, 120, 115), (120, 120, 120), (120, 100, 120),
                                    (103, 100, 97)])
def test_same_side_standing_touching_and_jitter_are_not_crossings(bottoms):
    result = check_target_tracks([(i * 600, [box(y)]) for i, y in enumerate(bottoms)],
                                 LINE, frame_size=(320, 240))
    assert result.state == "not_crossed"


def test_reverse_crossing_is_evidence_not_automatically_a_valid_finisher():
    result = check_target_tracks([(0, [box(80)]), (600, [box(100)]), (1200, [box(120)])],
                                 LINE, frame_size=(320, 240))
    assert result.state == "confirmed" and result.direction == 1


def test_unrelated_targets_on_opposite_sides_cannot_form_one_crossing():
    result = check_target_tracks([(0, [box(120, x=20)]), (600, []),
                                  (1200, [box(80, x=220)])], LINE, frame_size=(320, 240))
    assert result.state == "unverified"


def test_missing_frames_and_clipped_targets_fail_closed():
    assert check_target_tracks([(0, [box(120)])], LINE, frame_size=(320, 240)).state == "unverified"
    result = check_target_tracks([(0, [box(120, x=0)]), (600, [box(100, x=0)]),
                                  (1200, [box(80, x=0)])], LINE, frame_size=(320, 240))
    assert result.state == "unverified" and result.reason == "clipped_target"


def test_crossing_extension_outside_drawn_segment_is_not_confirmed():
    result = check_target_tracks([(0, [box(120, x=220)]), (600, [box(100, x=220)]),
                                  (1200, [box(80, x=220)])], [(0, 100), (150, 100)], frame_size=(320, 240))
    assert result.state != "confirmed"


def test_different_classes_cannot_be_joined_into_one_track():
    result = check_target_tracks([(0, [box(120, PERSON)]), (600, []),
                                  (1200, [box(80, MOTORCYCLE)])], LINE, frame_size=(320, 240))
    assert result.state == "unverified"


def test_ambiguous_neighbouring_tracks_fail_closed():
    a = box(100)
    result = check_target_tracks([(0, [box(120)]), (600, [a, replace(a, x=a.x + 1)]),
                                  (1200, [box(80)])], LINE, frame_size=(320, 240))
    assert result.state == "unverified" and result.reason == "ambiguous_tracks"


def test_two_tracks_competing_for_one_detection_are_ambiguous():
    first = box(120)
    result = check_target_tracks(
        [(0, [first, replace(first, x=first.x + 1)]),
         (600, [box(100)]), (1200, [box(80)])], LINE, frame_size=(320, 240),
    )
    assert result.state == "unverified" and result.reason == "ambiguous_tracks"


def test_stationary_background_does_not_discard_an_incomplete_target():
    stationary = box(140, x=200)
    result = check_target_tracks(
        [(0, [stationary]), (600, [stationary, box(100)]), (1200, [stationary])],
        LINE, frame_size=(320, 240),
    )
    assert result.state == "unverified" and result.reason == "insufficient_track"
