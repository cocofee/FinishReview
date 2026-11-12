"""People at the line: tracks, crossings and the person-mode pipeline (synthetic boxes)."""

import json
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from realtime.analysis_workspace import AnalysisWorkspace
from realtime.passage_receiver import PassageEvent
from realtime.person_crossing import (
    EDGE_PROJ_HIGH,
    PROJ_HIGH,
    PROJ_LOW,
    PersonCrossing,
    _proj_range,
    _track_crossings,
    _cache_meta,
    configured_direction,
    learn_profile,
    model_key,
    not_athlete_reason,
    people_at_line,
    person_path,
    person_roi,
)
from realtime.problem_analysis import _load_file_strips, analyse_camera
from realtime.problem_items import KIND_NO_CHIP_CROSSING, KIND_TWO_CHIPS_ONE_CROSSING
from realtime.problem_rules import CrossingRecord

from tests.realtime.test_problem_analysis import CROSSINGS_S, DAY, OFFSET, RACE, START, synthetic_race  # noqa: F401


LINE_X = 160.0
HEIGHT_PX = 80.0
FOOT_Y = 150.0


def _box(x_feet, height=HEIGHT_PX):
    return [0, 0.8, round(x_feet - 12, 1), round(FOOT_Y - height, 1), 24.0, height]


def _people(walkers, pts_ms, step=2):
    """``walkers``: (cross_s, px_per_s, height); boxes every ``step`` frames."""
    frames = []
    for index in range(0, len(pts_ms), step):
        now = pts_ms[index] / 1000
        boxes = []
        for cross_s, speed, height in walkers:
            x = LINE_X + (now - cross_s) * speed
            if 20 < x < 300:
                boxes.append(_box(x, height=height))
        frames.append([index, boxes])
    return frames


def _write_people(output, strips, model, walkers):
    files = _load_file_strips(strips)
    for item in files:
        roi = person_roi(item.geometry.xs, item.geometry.ys, int(item.geometry.width), int(item.geometry.height))
        target = person_path(output, item.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"frames": _people(walkers, item.pts), "stats": {},
                                      "meta": _cache_meta(item.meta, model_key(model), roi)}), encoding="utf-8")
    return files


ATHLETES = [(value, 200.0, HEIGHT_PX) for value in CROSSINGS_S]


@pytest.fixture()
def person_race(synthetic_race, tmp_path):  # noqa: F811
    root, workspace, config, strips = synthetic_race
    model = tmp_path / "yolox_nano.onnx"
    model.write_bytes(b"not a real model")
    config = replace(config, output_dir=tmp_path / "out", target_sport="running", model_path=model)
    return root, workspace, config, strips, model


def _pending(result):
    return sorted(item.kind for item in result["items"] if item.pending)


def test_every_athlete_pairs_and_nothing_is_queued(person_race):
    _root, workspace, config, strips, model = person_race
    _write_people(config.resolved_output(), strips, model, ATHLETES)

    result = analyse_camera(config, workspace, 1, strips)

    assert result["crossings"] == len(CROSSINGS_S)
    assert result["pairing"]["auto_paired"] == len(CROSSINGS_S)
    assert _pending(result) == []
    assert result["people"]["profile"]["samples"] == len(CROSSINGS_S)


def test_people_unlike_athletes_are_still_asked_with_the_machine_s_reason(person_race):
    """11-12：机器看着像工作人员的人也列出来，由操作员判断，机器不能自己去掉（可能漏掉运动员）。"""
    _root, workspace, config, strips, model = person_race
    walkers = ATHLETES + [
        (9.0, -200.0, HEIGHT_PX),   # running the wrong way
        (22.0, 30.0, HEIGHT_PX),    # a photographer strolling across
        (48.0, 200.0, HEIGHT_PX),   # an athlete-like crossing with no chip
    ]
    _write_people(config.resolved_output(), strips, model, walkers)

    result = analyse_camera(config, workspace, 1, strips)

    assert result["people"]["not_athletes"] == {"反方向": 1, "站着或慢走": 1}
    asked = sorted((item for item in result["items"] if item.pending and item.kind == KIND_NO_CHIP_CROSSING),
                   key=lambda item: item.evidence["recorder_ms"])
    assert [round((item.evidence["recorder_ms"] - START) / 1000) for item in asked] == [9, 22, 48]
    assert [item.evidence.get("unlike_athlete") for item in asked] == ["反方向", "站着或慢走", None]
    assert "机器看着不像选手（站着或慢走），是不是由你判断" in asked[1].detail


def _with_extra_chip(root, config, strips, chip_ms, bib, number):
    """One more automatic chip read, for this analysis only.

    CycleRace numbers passages in crossing order; a read appended with an
    earlier chip time would be a manual entry (补录), so the feed is renumbered.
    """
    extra = PassageEvent(
        event_id=f"{RACE}-stage-1-passage-{number}", race_id=RACE, stage_id="stage-1", group_id="1",
        sequence=1, bib=bib, passage_time_ms=chip_ms - DAY, lap=0, passage_timestamp_ms=chip_ms,
        received_at_ms=chip_ms + 300, group_name="男子组",
    )
    events = root / "cyclerace_passage_events.jsonl"
    original = events.read_text(encoding="utf-8")
    payloads = [json.loads(line) for line in original.splitlines() if line.strip()] + [extra.to_payload()]
    payloads.sort(key=lambda payload: payload["passage_timestamp_ms"])
    for sequence, payload in enumerate(payloads, start=1):
        payload["sequence"] = sequence
    try:
        events.write_text("".join(json.dumps(payload, ensure_ascii=False) + "\n" for payload in payloads),
                          encoding="utf-8")
        return analyse_camera(config, AnalysisWorkspace(root), 1, strips)
    finally:
        events.write_text(original, encoding="utf-8")


def test_two_runners_close_together_are_two_crossings(person_race):
    root, _workspace, config, strips, model = person_race
    # A second, smaller runner 0.4 s behind the third one, with their own chip.
    behind = CROSSINGS_S[2] + 0.4
    _write_people(config.resolved_output(), strips, model, ATHLETES + [(behind, 200.0, 70.0)])

    result = _with_extra_chip(root, config, strips, int(START + behind * 1000 - OFFSET), "199", 99)

    assert result["crossings"] == len(CROSSINGS_S) + 1
    assert result["pairing"]["auto_paired"] == len(CROSSINGS_S) + 1


def test_a_runner_boxed_twice_with_their_shadow_is_one_person(person_race):
    _root, workspace, config, strips, model = person_race
    files = _write_people(config.resolved_output(), strips, model, ATHLETES)
    for item in files:
        target = person_path(config.resolved_output(), item.path)
        payload = json.loads(target.read_text(encoding="utf-8"))
        for _index, boxes in payload["frames"]:
            # A weaker, wider box around each runner and the shadow beside them.
            boxes += [[0, 0.5, box[2] - 6, box[3], box[4] + 30, box[5]] for box in list(boxes)]
        target.write_text(json.dumps(payload), encoding="utf-8")

    result = analyse_camera(config, workspace, 1, strips)

    assert result["crossings"] == len(CROSSINGS_S)
    assert _pending(result) == []


def test_two_chips_on_one_runner_still_asks(person_race):
    root, _workspace, config, strips, model = person_race
    _write_people(config.resolved_output(), strips, model, ATHLETES)

    result = _with_extra_chip(root, config, strips, int(START + CROSSINGS_S[4] * 1000 - OFFSET + 120), "198", 98)

    assert KIND_TWO_CHIPS_ONE_CROSSING in _pending(result)


def test_cycling_and_missing_person_records_keep_the_line_pipeline(synthetic_race, tmp_path):  # noqa: F811
    _root, workspace, config, strips = synthetic_race
    model = tmp_path / "yolox_nano.onnx"
    model.write_bytes(b"x")
    cycling = replace(config, output_dir=tmp_path / "c", model_path=None)
    running = replace(config, output_dir=tmp_path / "r", target_sport="running", model_path=model)

    assert "people" not in analyse_camera(cycling, workspace, 1, strips)
    assert people_at_line(running.resolved_output(), _load_file_strips(strips), 1, model) is None


def _crossing(direction=1, height=80.0, speed=2.5, proj=0.5, vehicle=""):
    record = CrossingRecord(1, "s", "seg", "v", 0, 0, 0, 0, 0, 0)
    return PersonCrossing(record, direction, proj, height, speed, 0.8, 6, vehicle)


def test_profile_learns_direction_and_judges_leftovers():
    matched = [_crossing(height=70 + i, speed=2.0 + i / 10) for i in range(10)]
    profile = learn_profile(matched, None)

    assert profile.direction == 1
    assert not_athlete_reason(_crossing(), profile) == ""
    assert not_athlete_reason(_crossing(direction=-1), profile) == "反方向"
    assert not_athlete_reason(_crossing(height=30), profile) == "大小不像选手"
    assert not_athlete_reason(_crossing(speed=0.3), profile) == "站着或慢走"
    assert not_athlete_reason(_crossing(proj=1.4), profile) == "不在选手通道"
    assert not_athlete_reason(_crossing(vehicle="bicycle"), profile) == "骑车或车辆"
    # 不限方向 keeps both ways; too few pairs teach nothing yet.
    assert learn_profile(matched, 0).direction == 0
    assert learn_profile(matched[:3], None) is None
    assert not_athlete_reason(_crossing(direction=-1), None) == ""
    assert not_athlete_reason(_crossing(direction=-1), None, direction=1) == "反方向"


def test_sprint_arrow_setting_and_roi():
    # A vertical strip line x=100, from top to bottom: the positive side is x<100 (see _side).
    geometry = SimpleNamespace(xs=np.array([100.0, 100.0]), ys=np.array([0.0, 200.0]), width=200, height=200)
    assert configured_direction(None, geometry) is None
    assert configured_direction(0, geometry) == 0
    assert configured_direction([-1.0, 0.0], geometry) == 1
    assert configured_direction([1.0, 0.0], geometry) == -1
    assert configured_direction([0.0, 1.0], geometry) is None
    x0, y0, x1, y1 = person_roi(np.array([881.0, 238.0]), np.array([826.0, 1900.0]), 2560, 1920)
    assert (x0, x1) == (0, 1342) and y0 == 250 and y1 == 1920
    assert all(value % 2 == 0 for value in (x0, y0, x1, y1))


def _track_of(files, xs):
    """A track whose feet sit at ``xs``, one point every 100 ms of the first file."""

    item = files[0]
    return [(item.start_ms + float(item.pts[index]), str(item.meta.get("session_id", "")), (0, index),
             [x - 12, FOOT_Y - HEIGHT_PX, 24.0, HEIGHT_PX], False, 0.8)
            for index, x in zip(range(0, 5 * len(xs), 5), xs)]


def test_one_track_holding_two_people_counts_both_and_wavering_feet_once(person_race):
    _root, _workspace, config, strips, model = person_race
    files = _write_people(config.resolved_output(), strips, model, [])
    # One walks across and out of the picture; the next comes in there and walks back.
    there_and_back = _track_of(files, [100, 130, 190, 230, 260, 230, 190, 130, 100])
    crossings = _track_crossings(there_and_back, files, 1, set())
    assert [c.direction for c in crossings] in ([1, -1], [-1, 1])
    # Feet hovering on the line: one crossing.
    hovering = _track_of(files, [100, 140, 168, 152, 170, 150, 175, 200])
    assert len(_track_crossings(hovering, files, 1, set())) == 1


def test_a_line_drawn_to_the_picture_edge_runs_on_past_it():
    inside = SimpleNamespace(xs=np.array([100.0, 100.0]), ys=np.array([40.0, 160.0]), width=200, height=200)
    to_bottom = SimpleNamespace(xs=np.array([100.0, 60.0]), ys=np.array([40.0, 198.0]), width=200, height=200)
    assert _proj_range(inside) == (PROJ_LOW, PROJ_HIGH)
    assert _proj_range(to_bottom) == (PROJ_LOW, EDGE_PROJ_HIGH)


@pytest.mark.parametrize("arrow", ([1.0, 0.0], [-1.0, 0.0]))
def test_an_arrow_saved_backwards_is_ignored_with_a_warning(person_race, arrow):
    _root, workspace, config, strips, model = person_race
    _write_people(config.resolved_output(), strips, model, ATHLETES)

    result = analyse_camera(replace(config, sprint_arrows={1: arrow}), workspace, 1, strips)

    # Whichever way the arrow points, every athlete still meets their chip.
    assert result["pairing"]["auto_paired"] == len(CROSSINGS_S)
    warned = any("冲刺方向可能设反了" in text for text in result["calibration_warnings"])
    assert warned == (result["people"]["direction"] is None)

