"""Boundaries of the blind replay: what a step may and may not see."""

import json
from pathlib import Path

from realtime.problem_items import (
    CATEGORY_VIDEO,
    KIND_BUNCH,
    KIND_TWO_CHIPS_ONE_CROSSING,
    ProblemItem,
)
from tools.exception_first_replay import (
    build_workspace,
    clear_outputs,
    provenance_leaks,
    release_marks,
    released_journal,
    sealed_timeline,
    step_config,
    write_jsonl,
    read_jsonl,
)

T = 1_789_784_000_000
FRAME = {"camera_index": 1, "segment_id": "seg-2", "video_path": "videos/b.mkv", "frame_index": 12,
         "position_ms": 40_600, "session_id": "s", "calibration_version": 1}


def test_a_segment_is_released_at_its_seal_and_not_one_frame_before():
    lines = [
        {"segment_id": "seg-1", "camera_index": 1, "started_at_ms": T, "video_path": "videos/a.mkv"},
        {"segment_id": "seg-1", "ended_at_ms": T + 2_000, "media_duration_ms": 2_000, "media_started_at_ms": T},
        {"segment_id": "seg-2", "camera_index": 1, "started_at_ms": T + 2_000, "video_path": "videos/b.mkv"},
        {"segment_id": "seg-2", "ended_at_ms": T + 4_000, "media_duration_ms": 2_000, "media_started_at_ms": T + 2_000},
    ]

    entries, sealed = sealed_timeline(lines, T + 2_000)
    assert sealed == {"seg-1"} and len(entries) == 2
    entries, sealed = sealed_timeline(lines, T + 4_000)
    assert sealed == {"seg-1", "seg-2"} and len(entries) == 4
    # A segment that has only announced itself is not released.
    entries, sealed = sealed_timeline(lines[:3], T + 9_000)
    assert sealed == {"seg-1"} and [entry["segment_id"] for entry in entries] == ["seg-1", "seg-1"]


def test_a_chip_revision_appears_only_when_it_arrives():
    journal = [
        {"event_id": "e1", "bib": "001", "chip_id": "c1", "received_at_ms": T, "revision": 1},
        {"event_id": "e1", "bib": "002", "chip_id": "c1", "received_at_ms": T + 3_000, "revision": 2},
        {"event_id": "e2", "bib": "003", "chip_id": "c2", "received_at_ms": T + 9_000, "revision": 1},
    ]

    kept, events, undated = released_journal(journal, T)
    assert [entry["bib"] for entry in kept] == ["001"] and events == {"e1"} and undated == 0
    # The later correction is invisible until the moment it arrives.
    kept, events, undated = released_journal(journal, T + 3_000)
    assert [entry["bib"] for entry in kept] == ["001", "002"] and events == {"e1"}
    kept, events, undated = released_journal(journal, T + 9_000)
    assert len(kept) == 3 and events == {"e1", "e2"}
    # A journal without reception times cannot be released gradually; it is
    # counted, not silently dropped.
    kept, _events, undated = released_journal([{"event_id": "e3", "bib": "004"}], T)
    assert len(kept) == 1 and undated == 1


def test_every_seal_gets_a_step_and_the_last_step_releases_everything():
    ends = [T + 2_000, T + 4_000, T + 600_000]
    receives = [T + 100, T + 60_500, T + 601_000]

    marks = release_marks(ends, receives, step_ms=60_000, max_steps=40)
    assert set(ends) <= set(marks)
    assert marks == sorted(marks) and marks[-1] == T + 601_000
    # Thinning keeps the seals and still ends on everything.
    thin = release_marks(ends, receives, step_ms=1_000, max_steps=5)
    assert set(ends) <= set(thin) and thin[-1] == T + 601_000 and len(thin) <= 5


def test_an_arrival_days_after_the_recording_does_not_get_a_step_per_minute():
    ends = [T + 2_000, T + 4_000]
    # One revision during the race and one re-uploaded three days later.
    marks = release_marks(ends, [T + 3_000, T + 3 * 86_400_000], step_ms=60_000, max_steps=40)

    assert set(ends) <= set(marks)
    assert max(marks) == T + 3 * 86_400_000
    assert len(marks) < 10, marks
    # The tail of the recording still gets its own steps.
    assert any(T + 4_000 < mark < T + 3 * 86_400_000 for mark in marks)
    # Even when the schedule is thinned hard, the last step is still the run
    # that sees the whole journal.
    many = [T + index * 1_000 for index in range(500)] + [T + 3 * 86_400_000]
    thinned = release_marks(ends, many, step_ms=1_000, max_steps=6)
    assert thinned[-1] == T + 3 * 86_400_000 and len(thinned) <= 7


def test_an_item_that_uses_a_later_chip_or_segment_is_reported_as_a_leak():
    released_item = ProblemItem("two:1", KIND_TWO_CHIPS_ONE_CROSSING, CATEGORY_VIDEO, "同一次过线",
                                chip_time_ms=T, bibs=("020", "023"), event_ids=("e1", "e2"),
                                evidence={"frame": dict(FRAME, segment_id="seg-1")})
    later_item = ProblemItem("bunch:1", KIND_BUNCH, CATEGORY_VIDEO, "集团", chip_time_ms=T + 1_000,
                             event_ids=("e3",), evidence={"frame": FRAME})

    leaks = provenance_leaks([released_item, later_item], [{"bib": "057", "event_id": "e9"}],
                             released_events={"e1", "e2", "e3"}, sealed_segments={"seg-1"})
    # The first item is settled evidence; the group item points at a segment
    # that had not sealed, and the match belongs to a chip that had not arrived.
    assert [leak["item_id"] for leak in leaks] == ["bunch:1", "auto_passed:057"]
    assert leaks[0]["segment_not_sealed"] == "seg-2" and not leaks[0]["events_not_released"]
    assert leaks[1]["events_not_released"] == ["e9"]

    leaks = provenance_leaks([later_item], [], released_events={"e1"}, sealed_segments={"seg-2"})
    assert leaks[0]["events_not_released"] == ["e3"] and leaks[0]["segment_not_sealed"] is None

    assert provenance_leaks([released_item], [{"bib": "057", "event_id": "e1"}],
                            released_events={"e1", "e2"}, sealed_segments={"seg-1", "seg-2"}) == []


def test_the_replay_copy_carries_inputs_only(tmp_path):
    source = tmp_path / "slice"
    (source / "finish_problems").mkdir(parents=True)
    write_jsonl(source / "cyclerace_passage_events.jsonl", [{"event_id": "e1", "received_at_ms": T}])
    write_jsonl(source / "video_timeline.jsonl", [{"segment_id": "seg-1", "started_at_ms": T}])
    (source / "review_event_scope.json").write_text("{}", encoding="utf-8")
    (source / "finish_problems" / "analysis_config.json").write_text('{"cameras": [1]}', encoding="utf-8")
    (source / "finish_problems" / "problem_list.json").write_text('{"items": []}', encoding="utf-8")
    (source / "video_clock_models.jsonl").write_text('{"camera_index": 1}\n', encoding="utf-8")
    (source / "finish_problems" / "strips").mkdir()
    (source / "finish_problems" / "strips" / "camera_01.npz").write_bytes(b"cache")

    workdir = tmp_path / "replay"
    build = build_workspace(source, workdir)
    assert build["strips_cached"] is True
    # The answers of the source are not part of the replay's inputs.
    assert not (workdir / "finish_problems" / "problem_list.json").exists()
    assert not (workdir / "video_clock_models.jsonl").exists()
    assert read_jsonl(workdir / "cyclerace_passage_events.jsonl")[0]["event_id"] == "e1"
    assert (workdir / "finish_problems" / "strips" / "camera_01.npz").read_bytes() == b"cache"

    (workdir / "finish_problems" / "problem_list.json").write_text("{}", encoding="utf-8")
    (workdir / "finish_problems" / "near_crossing_history.jsonl").write_text("{}\n", encoding="utf-8")
    (workdir / "finish_problems" / "cards").mkdir()
    (workdir / "video_clock_models.jsonl").write_text("{}\n", encoding="utf-8")
    (workdir / "finish_problems" / "crossings_camera1.json").write_text("{}", encoding="utf-8")
    clear_outputs(workdir)
    # Every output of a previous run goes, including the candidate history that
    # would otherwise hand a later step a frame from a segment not yet sealed.
    assert not (workdir / "finish_problems" / "problem_list.json").exists()
    assert not (workdir / "finish_problems" / "near_crossing_history.jsonl").exists()
    assert not (workdir / "finish_problems" / "cards").exists()
    assert not (workdir / "video_clock_models.jsonl").exists()
    assert not (workdir / "finish_problems" / "crossings_camera1.json").exists()
    # Inputs survive: the journals, the scope file, the settings and the cache.
    assert (workdir / "cyclerace_passage_events.jsonl").is_file()
    assert (workdir / "video_timeline.jsonl").is_file()
    assert (workdir / "review_event_scope.json").is_file()
    assert (workdir / "finish_problems" / "analysis_config.json").is_file()
    assert (workdir / "finish_problems" / "strips" / "camera_01.npz").is_file()


def test_the_replay_can_run_with_the_bike_model_off(tmp_path):
    workdir = tmp_path / "replay"
    (workdir / "finish_problems").mkdir(parents=True)
    (workdir / "finish_problems" / "analysis_config.json").write_text(
        json.dumps({"cameras": [1], "model_path": "D:/models/yolox.onnx"}), encoding="utf-8")

    kept = step_config(workdir, camera=2, mode="live")
    assert kept.model_path == Path("D:/models/yolox.onnx") and kept.cameras == (2,) and kept.live is True
    off = step_config(workdir, camera=2, mode="settle", model=False)
    # The no-model arm differs in exactly one switch, so the two arms compare.
    assert off.model_path is None and off.live is True and off.evidence_delay_ms == 0
    assert off.render_cards is False and off.cameras == kept.cameras


def test_a_non_empty_workdir_is_refused_instead_of_overwritten(tmp_path):
    source = Path(tmp_path) / "slice"
    source.mkdir()
    workdir = Path(tmp_path) / "replay"
    workdir.mkdir()
    (workdir / "keep.txt").write_text("mine", encoding="utf-8")

    try:
        build_workspace(source, workdir)
    except SystemExit as stop:
        assert "empty" in str(stop)
    else:
        raise AssertionError("an occupied workdir must not be reused")
    assert (workdir / "keep.txt").is_file()
