"""End-to-end offline problem list on a synthetic recording (no race data needed)."""

import json
from dataclasses import replace

import cv2
import numpy as np
import pytest

from realtime.analysis_workspace import AnalysisWorkspace
from realtime.passage_receiver import PassageEvent
from realtime.problem_analysis import (
    AnalysisConfig,
    analyse_camera,
    extract_strips,
    write_problem_list,
)
from realtime.problem_items import (
    KIND_CHIP_NO_CROSSING,
    KIND_NO_CHIP_CROSSING,
    KIND_TWO_CHIPS_ONE_CROSSING,
)
from realtime.video_timeline import VideoTimelineStore


RACE = "event-a-competition-b"
DAY = 1_789_747_200_000  # 2026-09-19 00:00 Beijing time
START = DAY + 10 * 3_600_000
OFFSET = 1_200
CROSSINGS_S = (5.0, 12.0, 18.5, 25.0, 31.0, 38.0, 44.5, 51.0, 57.0, 63.5, 70.0, 76.0, 83.0, 89.0)
FPS = 20
WIDTH, HEIGHT = 320, 240
SPEED = 200.0  # px per second


def _write_video(path):
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), FPS, (WIDTH, HEIGHT))
    rng = np.random.default_rng(1)
    background = rng.integers(120, 140, (HEIGHT, WIDTH, 3)).astype(np.uint8)
    background[:, 158:162] = 250
    for index in range(int(95 * FPS)):
        now = index / FPS
        frame = background.copy()
        for crossing in CROSSINGS_S:
            front = 160 + (now - crossing) * SPEED
            if -10 < front < WIDTH + 40:
                left = int(round(front)) - 24
                frame[70:150, max(0, left):max(0, min(WIDTH, left + 24))] = (30, 20, 160)
        writer.write(frame)
    writer.release()


@pytest.fixture(scope="module")
def synthetic_race(tmp_path_factory):
    root = tmp_path_factory.mktemp("race")
    (root / "videos").mkdir()
    video = root / "videos" / "camera_01_20260919_100000_000000_archive_0000.avi"
    _write_video(video)
    timeline = VideoTimelineStore(root / "video_timeline.jsonl")
    timeline.add_completed_segment(
        source_id="camera_01_review", camera_index=1, video_path=video,
        media_started_at_ms=START, media_duration_ms=95_000,
        clock_source="videopipe_system_clock", timing_error_ms=3_000,
        end_reason="continuous_archive_fallback", race_id=RACE,
    )
    rng = np.random.default_rng(2)
    events = []
    for index, crossing in enumerate(CROSSINGS_S, start=1):
        chip = int(START + crossing * 1000 - OFFSET + rng.normal(0, 15))
        events.append(PassageEvent(
            event_id=f"{RACE}-stage-1-passage-{index}", race_id=RACE, stage_id="stage-1", group_id="1",
            sequence=index, bib=f"{100 + index}", passage_time_ms=chip - DAY, lap=0,
            passage_timestamp_ms=chip, received_at_ms=chip + 300, group_name="男子组",
        ))
    (root / "cyclerace_passage_events.jsonl").write_text(
        "".join(json.dumps(event.to_payload(), ensure_ascii=False) + "\n" for event in events),
        encoding="utf-8",
    )
    (root / "finish_lines.json").write_text(json.dumps({
        "schema_version": 1,
        "finish_lines": [{"camera_index": 1, "x1": 0.5, "y1": 0.15, "x2": 0.5, "y2": 0.85, "band_width": 0.08}],
    }), encoding="utf-8")
    output = tmp_path_factory.mktemp("analysis")
    workspace = AnalysisWorkspace(root)
    config = AnalysisConfig(workspace=root, output_dir=output)
    segments = workspace.archive_segments(1)
    strips = extract_strips(config, segments, workspace.finish_lines()[1])
    return root, workspace, config, strips


def _kinds(result):
    return sorted(item.kind for item in result["items"] if item.pending)


def test_clean_race_auto_passes_every_rider(synthetic_race):
    root, workspace, config, strips = synthetic_race
    before = {path.name: path.read_bytes() for path in root.iterdir() if path.is_file()}

    result = analyse_camera(config, workspace, 1, strips)

    assert result["crossings"] == len(CROSSINGS_S)
    assert result["pairing"]["auto_paired"] == len(CROSSINGS_S)
    assert abs(result["calibration"]["offset_start_ms"] - OFFSET) < 80
    assert _kinds(result) == []
    assert len(result["auto_passed"]) == len(CROSSINGS_S)
    assert all(item["source"] == "auto" for item in result["auto_passed"])
    after = {path.name: path.read_bytes() for path in root.iterdir() if path.is_file()}
    assert after == before


def test_deleted_chip_read_becomes_crossing_without_chip(synthetic_race):
    _root, workspace, config, strips = synthetic_race
    reads = tuple(read for read in workspace.reads if read.bib != "107")

    result = analyse_camera(config, workspace, 1, strips, reads=reads)

    item, = [item for item in result["items"] if item.pending]
    assert item.kind == KIND_NO_CHIP_CROSSING
    expected = START + 44_500 - OFFSET
    assert abs(item.chip_time_ms - expected) < 150
    frame = item.evidence["frame"]
    assert frame["frame_index"] == pytest.approx(44.5 * FPS, abs=2)
    assert frame["calibration_version"] >= 1


def test_added_chip_read_becomes_chip_without_crossing(synthetic_race):
    _root, workspace, config, strips = synthetic_race
    template = workspace.reads[0]
    fake = replace(template, event_id=f"{RACE}-stage-1-passage-99", bib="999", chip_ms=START + 91_500 - OFFSET)

    result = analyse_camera(config, workspace, 1, strips, reads=workspace.reads + (fake,))

    item, = [item for item in result["items"] if item.pending]
    assert item.kind == KIND_CHIP_NO_CROSSING and item.bibs == ("999",)
    assert item.evidence["frame"]["frame_index"] == pytest.approx(91.5 * FPS, abs=2)


def test_second_chip_on_one_rider_is_two_chips_one_crossing(synthetic_race):
    _root, workspace, config, strips = synthetic_race
    template = next(read for read in workspace.reads if read.bib == "103")
    twin = replace(template, event_id=f"{RACE}-stage-1-passage-98", bib="198", chip_ms=template.chip_ms + 180)

    result = analyse_camera(config, workspace, 1, strips, reads=workspace.reads + (twin,))

    pending = [item for item in result["items"] if item.pending]
    assert len(pending) == 1, [(item.kind, item.status, item.bibs) for item in result["items"]] + [result["pairing"]]
    item = pending[0]
    assert item.kind == KIND_TWO_CHIPS_ONE_CROSSING
    assert set(item.bibs) == {"103", "198"}


def test_problem_list_file_and_cards(synthetic_race):
    _root, workspace, config, strips = synthetic_race
    reads = tuple(read for read in workspace.reads if read.bib != "107")
    result = analyse_camera(config, workspace, 1, strips, reads=reads)

    path = write_problem_list(config, workspace, [result])

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["summary"]["pending"] == 1
    assert payload["summary"]["auto_passed"] == len(CROSSINGS_S) - 1
    item = payload["items"][0]
    card = config.resolved_output() / item["evidence"]["card"]
    strip = config.resolved_output() / item["evidence"]["filmstrip"]
    assert card.is_file() and strip.is_file()
    image = cv2.imdecode(np.fromfile(str(strip), dtype=np.uint8), cv2.IMREAD_COLOR)
    assert image.shape[1] == 9 * image.shape[0]
