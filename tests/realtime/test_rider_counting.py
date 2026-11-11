"""Cycling riders counted at a count line before the finish (synthetic boxes)."""

import json
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from realtime import rider_counting
from realtime.person_crossing import PersonCrossing, _free_record, not_athlete_reason
from realtime.problem_analysis import _load_file_strips, _rider_setup, analyse_camera
from realtime.problem_rules import CrossingRecord
from realtime.rider_counting import (
    HIGH_SCORE,
    LOOK_STEP_MS,
    WATCH_STEP_MS,
    _cache_meta,
    _file_setup,
    _RiderGate,
    _wake_area,
    count_line_problem,
    detect_file_riders,
    rider_model_key,
    rider_path,
    rider_tracks,
    riders_at_line,
    valid_count_line,
)

from tests.realtime.test_person_crossing import _with_extra_chip
from tests.realtime.test_problem_analysis import CROSSINGS_S, OFFSET, START, synthetic_race  # noqa: F401


FINISH_X = 160.0             # the synthetic finish line, x = 0.5 of a 320 px picture
COUNT = [[0.8, 0.15], [0.8, 0.85]]
COUNT_X = 256.0
HEIGHT_PX = 80.0
FOOT_Y = 150.0
SPEED = -200.0               # px/s: riders come from the right towards the finish line


def _box(x_feet, score=0.8, height=HEIGHT_PX):
    return [score, round(x_feet - 12, 1), round(FOOT_Y - height, 1), 24.0, height]


def _frames(riders, pts_ms, step=2):
    """``riders``: (finish_s, seen(x) -> score or None[, px/s]); boxes every ``step`` frames."""
    frames = []
    for index in range(0, len(pts_ms), step):
        now = pts_ms[index] / 1000
        boxes = []
        for finish_s, seen, *speed in riders:
            x = FINISH_X + (now - finish_s) * (speed[0] if speed else SPEED)
            score = seen(x, now - finish_s)
            if score is not None and 20 < x < 310:
                boxes.append(_box(x, score))
        frames.append([index, boxes])
    return frames


def _write_riders(output, strips, model, riders, count_points=COUNT):
    files = _load_file_strips(strips)
    for item in files:
        _finish, count, roi, band, road = _file_setup(item, count_points)
        target = rider_path(output, item.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"frames": _frames(riders, item.pts), "stats": {},
                                      "meta": _cache_meta(item.meta, rider_model_key(model), roi, band, road, count)}),
                          encoding="utf-8")
    return files


def _always(_x, _t):
    return 0.8


def _gone_before_the_finish(x, _t):
    # Cut by the picture's edge and hidden by the rider beside: only seen
    # until a little before the finish line.
    return 0.8 if x > 200 else None


@pytest.fixture()
def rider_race(synthetic_race, tmp_path):  # noqa: F811
    root, workspace, config, strips = synthetic_race
    model = tmp_path / "yolox_nano.onnx"
    model.write_bytes(b"not a real model")
    config = replace(config, output_dir=tmp_path / "out", target_sport="cycling", model_path=model,
                     count_lines={1: COUNT})
    return root, workspace, config, strips, model


def _pending(result):
    return sorted(item.kind for item in result["items"] if item.pending)


def test_every_rider_counted_before_the_line_pairs_with_its_chip(rider_race):
    _root, workspace, config, strips, model = rider_race
    _write_riders(config.resolved_output(), strips, model, [(value, _always) for value in CROSSINGS_S])

    result = analyse_camera(config, workspace, 1, strips)

    assert result["crossings"] == len(CROSSINGS_S)
    assert result["pairing"]["auto_paired"] == len(CROSSINGS_S)
    assert _pending(result) == []
    assert result["people"]["counting"] == "count_line"
    assert result["people"]["finish_timing"]["seen_at_finish"] == len(CROSSINGS_S)
    # Timed at the finish line, not at the count line about half a second before it.
    saved = json.loads((config.resolved_output() / "crossings_camera1.json").read_text(encoding="utf-8"))
    times = sorted(entry["recorder_ms"] for entry in saved["crossings"])
    assert all(abs(time - (START + value * 1000)) <= 60 for time, value in zip(times, CROSSINGS_S))


def test_riders_lost_before_the_finish_are_timed_from_the_riders_seen_there(rider_race):
    _root, workspace, config, strips, model = rider_race
    seen = [(value, _always) for value in CROSSINGS_S[:6]]
    lost = [(value, _gone_before_the_finish) for value in CROSSINGS_S[6:]]
    files = _write_riders(config.resolved_output(), strips, model, seen + lost)

    result = analyse_camera(config, workspace, 1, strips)

    assert result["pairing"]["auto_paired"] == len(CROSSINGS_S)
    assert result["people"]["finish_timing"] == {"seen_at_finish": 6, "race_delay": len(CROSSINGS_S) - 6,
                                                 "velocity": 0, "count_line": 0}
    # (256 - 160) px at 200 px/s.
    assert abs(result["people"]["finish_delay_ms"] - 480) <= 60
    assert files


def test_a_rider_tucked_behind_another_is_still_counted_from_weak_boxes(rider_race):
    root, _workspace, config, strips, model = rider_race
    behind = CROSSINGS_S[2] + 0.3

    def tucked(_x, t):
        # Clear on the road before the count line, then mostly hidden.
        return 0.8 if t < -0.55 else 0.25

    _write_riders(config.resolved_output(), strips, model,
                  [(value, _always) for value in CROSSINGS_S] + [(behind, tucked)])

    result = _with_extra_chip(root, config, strips, int(START + behind * 1000 - OFFSET), "199", 99)

    assert result["crossings"] == len(CROSSINGS_S) + 1
    assert result["pairing"]["auto_paired"] == len(CROSSINGS_S) + 1


def test_weak_boxes_alone_never_make_a_rider():
    observations = [(t * 80.0, "s", (0, t), [_box(300 - 16 * t, score=HIGH_SCORE - 0.1)]) for t in range(12)]

    assert rider_tracks(observations) == []


def test_a_rider_without_a_chip_is_asked(rider_race):
    _root, workspace, config, strips, model = rider_race
    _write_riders(config.resolved_output(), strips, model,
                  [(value, _always) for value in CROSSINGS_S] + [(48.0, _always)])

    result = analyse_camera(config, workspace, 1, strips)

    pending = [item for item in result["items"] if item.pending]
    assert len(pending) == 1
    assert abs(pending[0].evidence["recorder_ms"] - (START + 48_000)) <= 100
    # Riding is what an athlete does here, not a reason to leave them out.
    assert "骑车或车辆" not in result["people"]["not_athletes"]


def test_count_line_only_for_cycling_with_a_model_and_a_drawn_line(rider_race):
    _root, _workspace, config, _strips, model = rider_race

    assert _rider_setup(config, 1) == (model, COUNT)
    assert _rider_setup(config, 2) is None
    assert _rider_setup(replace(config, target_sport="running"), 1) is None
    assert _rider_setup(replace(config, count_lines={}), 1) is None
    assert _rider_setup(replace(config, model_path=None), 1) is None


def test_a_count_line_must_lie_before_the_finish_line():
    finish = [[0.5, 0.15], [0.5, 0.85]]

    assert valid_count_line([[0.8, 0.1], [0.8, 0.9]], finish) == ""
    assert "相交" in valid_count_line([[0.3, 0.5], [0.8, 0.5]], finish)
    assert valid_count_line([[0.8, 0.1]], finish)
    assert valid_count_line([[0.8, 0.1], [0.8, 0.1]], finish)
    assert valid_count_line([[0.8, 0.1], [0.8, 0.9]], [])


def test_riding_is_not_a_reason_to_drop_a_cycling_rider():
    record = CrossingRecord(1, "s", "seg", "v", 0, 0, 0, 0, 0, 0)
    rider = PersonCrossing(record, 1, 0.5, 80.0, 2.5, 0.8, 6, "bicycle")

    assert not_athlete_reason(rider, None, 1) == "骑车或车辆"
    assert not_athlete_reason(rider, None, 1, riders=True) == ""


def test_a_lost_riders_time_does_not_change_when_later_riders_arrive(rider_race):
    # The review item of a rider lost before the finish is keyed by the frame
    # of its estimated finish moment; later, slower riders must not move it.
    _root, _workspace, config, strips, model = rider_race
    output = config.resolved_output()
    early = [(value, _always) for value in CROSSINGS_S[:3]] + [(value, _gone_before_the_finish)
                                                              for value in CROSSINGS_S[3:5]]
    files = _write_riders(output, strips, model, early)
    before = riders_at_line(output, files, 1, model, COUNT)
    slow = [(value, _always, SPEED / 2) for value in CROSSINGS_S[5:]]
    files = _write_riders(output, strips, model, early + slow)
    after = riders_at_line(output, files, 1, model, COUNT)

    def lost(people):
        return sorted((c.record.recorder_ms, c.record.key) for c in people.crossings
                      if min(abs(c.record.recorder_ms - (START + value * 1000)) for value in CROSSINGS_S[3:5]) < 2000)

    assert len(lost(before)) == 2
    assert lost(after) == lost(before)
    assert after.stats["finish_timing"]["race_delay"] == 2
    # Most riders seen at the finish now take twice as long from the count line.
    assert after.stats["finish_delay_ms"] > 800
    assert all(abs(ms - (START + value * 1000)) <= 60 for (ms, _key), value in zip(lost(after), CROSSINGS_S[3:5]))


def test_a_count_line_across_a_recordings_finish_line_falls_back_with_a_warning(rider_race):
    _root, workspace, config, strips, _model = rider_race
    across = [[0.3, 0.5], [0.8, 0.5]]
    files = _load_file_strips(strips)

    assert "相交" in count_line_problem(files, across)
    assert count_line_problem(files, COUNT) == ""

    result = analyse_camera(replace(config, count_lines={1: across}), workspace, 1, strips)

    assert any("相交" in text and "原来的认车方法" in text for text in result["calibration_warnings"])
    assert (result.get("people") or {}).get("counting") != "count_line"


def test_a_count_line_behind_the_finish_line_falls_back_with_a_warning(rider_race):
    # Drawn after the finish (x = 0.3): every rider crosses it going away from the finish.
    _root, workspace, config, strips, model = rider_race
    behind = [[0.3, 0.15], [0.3, 0.85]]
    _write_riders(config.resolved_output(), strips, model, [(value, _always) for value in CROSSINGS_S], behind)

    result = analyse_camera(replace(config, count_lines={1: behind}), workspace, 1, strips)

    assert any("反方向" in text for text in result["calibration_warnings"])
    assert (result.get("people") or {}).get("counting") != "count_line"


def test_a_crossing_in_a_used_last_row_takes_a_free_row_and_never_hangs():
    def make(at):
        return SimpleNamespace(key=f"row{at}")

    assert _free_record(make, 9, 10, {"row9"}).key == "row8"
    assert _free_record(make, 4, 10, {"row4", "row5"}).key == "row6"
    assert _free_record(make, 4, 10, set()).key == "row4"
    assert _free_record(make, 9, 10, {f"row{i}" for i in range(10)}) is None


class _FakeDetector:
    """Person boxes in decoded pixels for each frame, from ``boxes(frame_number)``."""

    def __init__(self, boxes):
        self.boxes, self.calls, self.frame = boxes, [], -1

    def detect(self, image, classes=()):
        if not self.calls or self.calls[-1] != self.frame:  # two looks per frame: the picture and the road
            self.calls.append(self.frame)
        return [SimpleNamespace(score=0.8, x=x - 12, y=FOOT_Y - HEIGHT_PX, w=24.0, h=HEIGHT_PX)
                for x in self.boxes(self.frame)]


def _gate_run(monkeypatch, frames, moving, boxes):
    """``frames`` frames at 25 fps; ``moving(i)``: something moves on the road; ``boxes(i)``: feet x of people."""

    detector = _FakeDetector(boxes)
    rng = np.random.default_rng(3)
    still = np.full((240, 320, 3), 128, np.uint8)

    def fake_frames(_path, _roi, _size, _executable, *, cancelled):
        for index in range(frames):
            detector.frame = index
            yield rng.integers(0, 255, still.shape).astype(np.uint8) if moving(index) else still

    monkeypatch.setattr(rider_counting, "_roi_frames", fake_frames)
    finish = ((FINISH_X, 0.0), (FINISH_X, 240.0))
    count = ((COUNT_X, 36.0), (COUNT_X, 204.0))
    roi = (0, 0, 320, 240)
    pts = np.arange(frames, dtype=float) * 40.0
    result = detect_file_riders(None, 0, pts, roi, roi, roi, _wake_area(finish, count, 320, 240), detector,
                                gate=_RiderGate())
    return result, detector.calls


def test_a_still_picture_is_never_looked_at(monkeypatch):
    result, calls = _gate_run(monkeypatch, 100, lambda i: False, lambda i: [])

    assert calls == []
    assert result["stats"]["quiet"] > 0
    assert all(boxes is None for _index, boxes in result["frames"])


def test_spectators_moving_in_place_only_get_a_glance(monkeypatch):
    # Waving beside the road all the time, nobody coming towards the finish.
    result, calls = _gate_run(monkeypatch, 100, lambda i: True, lambda i: [300.0])

    gaps = np.diff(calls) * 40
    assert result["stats"]["looked"] == 0
    assert len(calls) == result["stats"]["watched"] <= 100 * 40 // WATCH_STEP_MS + 1
    assert gaps.min() >= WATCH_STEP_MS - 25


def test_a_rider_coming_up_the_road_wakes_a_close_look(monkeypatch):
    # From frame 20 a rider rides up the road from x = 352 towards the finish at 100 px/s.
    def rider(i):
        return [352.0 - (i - 20) * 4.0] if 20 <= i <= 60 else []

    result, calls = _gate_run(monkeypatch, 100, lambda i: True, rider)

    assert result["stats"]["looked"] > 0
    close = [b - a for a, b in zip(calls, calls[1:]) if 30 <= a <= 50]
    assert min(close) * 40 <= LOOK_STEP_MS
    # Looked at closely while crossing the count line (x = 256 at frame 44).
    assert sum(40 <= i <= 48 for i in calls) >= 4


def test_the_closer_look_is_the_far_road_of_the_tongxin_experiment():
    # Tongxin, 2560 x 1920: finish line on the left, count line drawn at x = 0.664 from y = 0.2 to 0.85.
    finish = ((593.0, 220.0), (532.0, 1775.0))
    count = ((1699.8, 384.0), (1699.8, 1632.0))

    roi, band, road = rider_counting.rider_areas(finish, count, 2560, 1920)

    assert roi == (0, 0, 2560, 1920)
    # The experiment's hand-drawn crop was x 1000-2560, y 300-1300.
    assert abs(road[0] - 1000) <= 10 and road[2] == 2560
    assert abs(road[1] - 300) <= 10 and abs(road[3] - 1300) <= 10
    # Drawn the other way round, the far end is still the upper one.
    assert rider_counting.rider_areas(finish, count[::-1], 2560, 1920)[2] == road
    # Movement is still watched along the whole line.
    assert band[1] <= 384 and band[3] >= 1632


def test_one_slow_passer_by_before_the_race_does_not_time_the_first_riders(rider_race):
    # Tongxin: someone walked slowly past both lines before the race (4.5 s
    # between them) and the first bunch, lost before the finish, was timed late.
    _root, _workspace, config, strips, model = rider_race
    output = config.resolved_output()
    walker = (3.5, _always, SPEED / 5)
    first = [(value, _gone_before_the_finish) for value in CROSSINGS_S[:2]]
    files = _write_riders(output, strips, model, [walker] + first)

    people = riders_at_line(output, files, 1, model, COUNT)

    assert people.stats["finish_timing"] == {"seen_at_finish": 1, "race_delay": 0, "velocity": 2, "count_line": 0}
    riders = sorted(c.record.recorder_ms for c in people.crossings)[1:]
    assert all(abs(ms - (START + value * 1000)) <= 100 for ms, value in zip(riders, CROSSINGS_S[:2]))


_GATE_META = {"model": "abc", "roi": [0, 0, 320, 240], "band": [10, 10, 200, 200], "count_line": [[150.0, 36.0], [150.0, 204.0]]}


def _left_gate():
    return _RiderGate(background=np.arange(12, dtype=np.float32).reshape(3, 4), last_motion_ms=9_900.0,
                      near_until_ms=10_800.0, last_look_ms=9_960.0,
                      recent=[(9_880.0, [[1.0, 2.0, 3.0, 4.0]]), (9_960.0, [])])


def test_the_next_process_takes_up_the_gate_where_the_last_one_left_it(tmp_path):
    path = rider_counting._gate_path(tmp_path, tmp_path / "strips" / "camera_01" / "piece_0001.npz", "session")
    rider_counting._save_gate(path, _left_gate(), 10_000.0, _GATE_META)

    gate = rider_counting._load_gate(path, 10_000.0, _GATE_META)

    left = _left_gate()
    assert np.array_equal(gate.background, left.background)
    assert (gate.last_motion_ms, gate.near_until_ms, gate.last_look_ms) == (9_900.0, 10_800.0, 9_960.0)
    assert gate.recent == [(9_880.0, [[1.0, 2.0, 3.0, 4.0]]), (9_960.0, [])]
    assert not list(path.parent.glob("*.json"))  # never mistaken for a rider record


def test_a_gate_left_elsewhere_or_long_ago_is_not_taken_up(tmp_path):
    path = tmp_path / "riders" / "camera_01" / "session.gate.npz"
    rider_counting._save_gate(path, _left_gate(), 10_000.0, _GATE_META)

    assert rider_counting._load_gate(path, 10_000.0 + rider_counting.GATE_CARRY_MS + 1, _GATE_META) is None  # a gap in the recording
    assert rider_counting._load_gate(path, 9_000.0, _GATE_META) is None                                       # an earlier file redone
    assert rider_counting._load_gate(path, 10_000.0, dict(_GATE_META, count_line=[[160.0, 36.0], [160.0, 204.0]])) is None
    assert rider_counting._load_gate(path, 10_000.0, dict(_GATE_META, model="other")) is None
    assert rider_counting._load_gate(tmp_path / "missing.gate.npz", 10_000.0, _GATE_META) is None
    path.write_bytes(b"not a gate")
    assert rider_counting._load_gate(path, 10_000.0, _GATE_META) is None
