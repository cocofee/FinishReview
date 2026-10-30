import json
from dataclasses import replace

import numpy as np

from realtime.auto_label import detected_people, label_wave, people_of_wave
from realtime.bunch_review import _DETECTIONS

SESSION = "camera_01_20261030_162645_131998"
VIDEO = rf"D:\rec\{SESSION}_review_0000000000000011.ts"
START = 1_793_348_835_000
WIDTH, HEIGHT = 2560, 1920


def workspace(tmp_path, detections, clock=False):
    """A finish_problems folder with one strip file (a straight line, 500 samples) and these detections."""
    folder = tmp_path / "finish_problems"
    strips = folder / "strips" / "camera_01"
    strips.mkdir(parents=True, exist_ok=True)
    meta = {"width": WIDTH, "height": HEIGHT, "segment_id": "seg1", "video_path": VIDEO, "session_id": SESSION,
            "media_started_at_ms": START}
    xs = np.linspace(0.8 * WIDTH, 0.4 * WIDTH, 500)
    ys = np.linspace(0.4 * HEIGHT, 0.99 * HEIGHT, 500)
    np.savez(strips / f"{SESSION}_review_0000000000000011.npz", xs=xs, ys=ys,
             meta=np.frombuffer(json.dumps(meta).encode("utf-8"), dtype=np.uint8))
    (folder / "crossings_camera1.json").write_text(json.dumps({"crossings": [
        {"recorder_ms": recorder, "session_id": SESSION, "frame_index": index, "video_path": VIDEO, "row": row,
         "in_obstruction": blocked}
        for index, (recorder, row, blocked) in enumerate(detections)]}), encoding="utf-8")
    _DETECTIONS.clear()
    return tmp_path


def aligned(people):
    """As if the recording were timed (these synthetic recordings have no clock fit)."""
    return [replace(person, aligned=True) for person in people]


def test_a_detected_person_has_a_place_on_the_frame_and_the_frame_to_count_them_on(tmp_path):
    people = detected_people(workspace(tmp_path, [(START + 2_000, 0, False), (START + 1_000, 499, False)]), 1)
    assert [person.recorder_ms - START for person in people] == [1_000, 2_000]  # in time order
    first, second = people
    assert (round(first.x, 3), round(first.y, 3)) == (0.4, 0.99)
    assert (round(second.x, 3), round(second.y, 3)) == (0.8, 0.4)
    assert second.frame == {"camera_index": 1, "segment_id": "seg1", "video_path": VIDEO, "frame_index": 0,
                            "session_id": SESSION, "recorder_ms": START + 2_000, "position_ms": 2_000}
    assert second.key == f"1|{VIDEO}|0|0"


def test_a_detection_without_its_strip_is_left_out_and_no_analysis_is_no_one(tmp_path):
    assert detected_people(tmp_path, 1) == []
    base = workspace(tmp_path, [(START, 0, False), (START + 100, 9999, False)])  # row beyond the line
    assert len(detected_people(base, 1)) == 1
    (base / "finish_problems" / "strips" / "camera_01" / f"{SESSION}_review_0000000000000011.npz").unlink()
    _DETECTIONS.clear()
    assert detected_people(base, 1) == []


def test_a_wave_takes_the_people_who_crossed_within_reach_of_its_chips(tmp_path):
    people = detected_people(workspace(tmp_path, [(START + t, 0, False) for t in (0, 5_000, 5_650, 6_800)]), 1)
    chips_at = people[1].chip_ms
    near = people_of_wave(people, chips_at, chips_at + 100)
    assert [person.recorder_ms - START for person in near] == [5_000, 5_650]


def test_one_person_for_one_chip_is_labelled_and_a_blocked_view_is_not(tmp_path):
    base = workspace(tmp_path, [(START + 5_000, 100, False)])
    people = detected_people(base, 1)
    chip = [("c1", people[0].chip_ms + 120)]
    assert any("未校时" in reason for reason in label_wave(chip, people, clock_ok=True).reasons)
    people = aligned(people)
    match = label_wave(chip, people, clock_ok=True)
    assert match.reasons == () and match.pairs == (("c1", people[0].key),)
    blocked = aligned(detected_people(workspace(tmp_path, [(START + 5_000, 100, True)]), 1))
    assert any("视线被挡" in reason for reason in label_wave(chip, blocked, clock_ok=True).reasons)


def test_two_people_for_one_chip_are_left_to_hand(tmp_path):
    people = aligned(detected_people(workspace(tmp_path, [(START + 5_000, 100, False), (START + 5_250, 300, False)]),
                                     1))
    match = label_wave([("c1", people[0].chip_ms)], people, clock_ok=True)
    assert match.pairs == () and any("不一致" in reason for reason in match.reasons)
