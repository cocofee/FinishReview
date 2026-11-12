import numpy as np
import pytest
from PyQt5.QtGui import QImage
from PyQt5.QtWidgets import QApplication

from realtime.mark_follow import FOLLOW_MS, MarkFollower


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


def picture(x_px, seed=3):
    """A 960x540 noisy road with a 40x80 bright 'runner' whose left edge is at X_PX."""
    rng = np.random.default_rng(seed)
    grey = rng.integers(40, 90, (540, 960), dtype=np.uint8)
    grey[200:280, x_px:x_px + 40] = 230
    grey[220:240, x_px + 10:x_px + 30] = 20  # some texture on the person
    rgb = np.ascontiguousarray(np.dstack([grey] * 3))
    return QImage(rgb.data, 960, 540, 960 * 3, QImage.Format_RGB888).copy()


def frame(index, ms):
    return {"camera_index": 1, "segment_id": "s1", "frame_index": index, "recorder_ms": ms}


def mark(x_px):
    return {"key": (0, 1), "x": (x_px + 20) / 960, "y": 240 / 540, "recorder_ms": 0, "label": "61",
            "camera": 1, "segment": "s1", "frame_index": 0}


def test_the_tag_rides_along_with_the_person_clicked(qapp):
    follower = MarkFollower()
    marks = [mark(300)]
    assert follower.update(frame(0, 0), picture(300), marks) == []  # the click frame draws its own ring
    for step in range(1, 6):
        shown = follower.update(frame(step, step * 40), picture(300 + 12 * step), marks)
        assert len(shown) == 1 and shown[0][2] == "61"
        assert abs(shown[0][0] * 960 - (320 + 12 * step)) <= 4 and abs(shown[0][1] * 540 - 240) <= 4


def test_the_tag_stops_after_a_jump_and_after_follow_ms(qapp):
    follower = MarkFollower()
    marks = [mark(300)]
    follower.update(frame(0, 0), picture(300), marks)
    assert follower.update(frame(1, 40), picture(310), marks)
    assert follower.update(frame(9, 360), picture(400), marks) == []  # jumped ahead: no guessing
    follower = MarkFollower()
    follower.update(frame(0, 0), picture(300), marks)
    assert follower.update(frame(1, FOLLOW_MS + 1), picture(310), marks) == []


def test_a_cancelled_count_stops_its_tag(qapp):
    follower = MarkFollower()
    follower.update(frame(0, 0), picture(300), [mark(300)])
    assert follower.update(frame(1, 40), picture(310), [mark(300)])
    assert follower.update(frame(2, 80), picture(320), []) == []


def test_a_click_on_a_frame_shown_with_nobody_counted_yet_is_followed(qapp):
    """11-12：点之前这一帧附近没人点过，点完录像往前走，号码也要跟上（第一次实测没跟）。"""
    follower = MarkFollower()
    assert follower.update(frame(0, 0), picture(300), []) == []  # shown before the click
    assert follower.update(frame(0, 0), picture(300), []) == []  # playback starts: same frame again
    marks = [mark(300)]
    assert follower.update(frame(0, 0), picture(300), marks) == []  # and once more after the click
    shown = follower.update(frame(1, 40), picture(312), marks)
    assert len(shown) == 1 and abs(shown[0][0] * 960 - 332) <= 4
    assert follower.update(frame(2, 80), picture(324), marks)
