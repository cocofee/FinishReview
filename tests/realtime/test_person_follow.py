"""Numbers that walk with their runner: saved paths and how the picture reads them."""

import json

from realtime import problem_analysis
from realtime.person_follow import (
    MAX_GAP_MS,
    OUTPUT_DIRNAME,
    FollowTrack,
    FollowTracks,
    follow_path,
)
from realtime.problem_analysis import analyse_camera

from tests.realtime.test_person_crossing import ATHLETES, _write_people, person_race  # noqa: F401
from tests.realtime.test_problem_analysis import CROSSINGS_S, synthetic_race  # noqa: F401


def _track(times, xs, crossing_ms=1000, bib="7"):
    return FollowTrack(crossing_ms, bib, tuple(float(t) for t in times), tuple(xs),
                       tuple(0.5 for _ in xs), tuple(0.2 for _ in xs))


def test_feet_are_joined_between_looks_but_not_across_a_gap():
    track = _track([0, 200, 200 + MAX_GAP_MS + 100], [0.1, 0.3, 0.9])
    assert track.at(100)[0] == 0.2
    assert track.at(400) is None            # not seen in between
    assert track.at(-50) is None
    assert track.at(-50, reach_ms=100)[0] == 0.1
    assert track.near(1000 - 1500, 1000) and not track.near(1000 + 1600, 1000)


def test_a_mark_belongs_to_the_runner_under_it(tmp_path):
    path = follow_path(tmp_path, 1)
    path.write_text(json.dumps({"version": 1, "camera_index": 1, "video_size": [200, 100], "tracks": [
        {"crossing_ms": 1000, "bib": "7", "points": [[800, 0.40, 0.5, 0.2], [1200, 0.60, 0.5, 0.2]]},
        {"crossing_ms": 1100, "bib": "", "points": [[900, 0.10, 0.5, 0.2], [1300, 0.20, 0.5, 0.2]]},
        {"crossing_ms": 9000, "bib": "9", "points": [[8800, 0.5, 0.5, 0.2], [9200, 0.6, 0.5, 0.2]]},
    ]}), encoding="utf-8")
    tracks = FollowTracks(path).refresh()

    nearby = tracks.around(1000)
    assert [t.crossing_ms for t in nearby] == [1000, 1100]
    assert tracks.owner(nearby, 1000, 0.51, 0.5).bib == "7"
    assert tracks.owner(nearby, 1000, 0.15, 0.5).bib == ""
    assert tracks.owner(nearby, 1000, 0.9, 0.5) is None   # nobody there
    # A broken or missing file shows nothing rather than failing.
    path.write_text("{", encoding="utf-8")
    assert tracks.refresh().tracks == ()
    path.unlink()
    assert tracks.refresh().tracks == ()


def test_analysis_saves_each_runner_path_with_their_chip_number(person_race):  # noqa: F811
    _root, workspace, config, strips, model = person_race
    _write_people(config.resolved_output(), strips, model, ATHLETES)

    result = analyse_camera(config, workspace, 1, strips)

    assert OUTPUT_DIRNAME == problem_analysis.OUTPUT_DIRNAME
    saved = json.loads(follow_path(config.resolved_output(), 1).read_text(encoding="utf-8"))
    assert len(saved["tracks"]) == len(CROSSINGS_S) == result["pairing"]["auto_paired"]
    assert all(track["bib"] for track in saved["tracks"])
    tracks = FollowTracks(follow_path(config.resolved_output(), 1)).refresh()
    first = tracks.tracks[0]
    before, after = first.at(first.crossing_ms - 500), first.at(first.crossing_ms + 500)
    assert before and after and before[0] != after[0]   # the number moves with the runner
