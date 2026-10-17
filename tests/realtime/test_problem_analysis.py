"""End-to-end offline problem list on a synthetic recording (no race data needed)."""

import json
from dataclasses import replace

import cv2
import numpy as np
import pytest

from realtime.analysis_workspace import AnalysisWorkspace
from realtime.passage_receiver import PassageEvent
from realtime import problem_analysis
from realtime.problem_analysis import (
    ANALYSIS_CONFIG_FILENAME,
    AnalysisConfig,
    analyse_camera,
    config_from_workspace,
    extract_strips,
    run_offline_analysis,
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


def test_live_analysis_defers_tail_and_reuses_detection_cache(synthetic_race, monkeypatch):
    _root, workspace, config, strips = synthetic_race
    live = replace(config, live=True)
    result = analyse_camera(live, workspace, 1, strips)
    from realtime.problem_items import KIND_WAITING
    assert result["analyzed_through_ms"] < START + 85_000
    assert result["waiting_reads"] == 1
    assert len([item for item in result["items"] if item.kind == KIND_WAITING]) == 1
    assert _kinds(result) == []
    assert len(result["auto_passed"]) == len(CROSSINGS_S) - 1
    monkeypatch.setattr(problem_analysis, "detect_crossings", lambda *_args: pytest.fail("cached strip detected again"))
    repeated = analyse_camera(live, workspace, 1, strips)
    assert len(repeated["auto_passed"]) == len(result["auto_passed"])


def test_settle_run_reports_a_chip_outside_the_recording_as_a_coverage_gap(synthetic_race):
    """After recording stopped nothing arrives, so "waiting" would be a lie.

    During the race a chip beyond the last sealed frame may still get video;
    at the settle run it never will, and the operator must see it as a
    recording gap (with its own count) instead of an open wait.
    """

    from collections import Counter

    from realtime.chip_history import chip_reads
    from realtime.problem_items import KIND_NO_VIDEO

    _root, workspace, config, strips = synthetic_race
    after = START + 300_000  # five minutes past the end of the recording
    before = START - 60_000  # a minute before it started
    outside = chip_reads([
        PassageEvent(event_id=f"{RACE}-stage-1-passage-90", race_id=RACE, stage_id="stage-1", group_id="1",
                     sequence=90, bib="900", passage_time_ms=after - DAY, lap=0, passage_timestamp_ms=after,
                     received_at_ms=after + 300, group_name="男子组"),
        PassageEvent(event_id=f"{RACE}-stage-1-passage-91", race_id=RACE, stage_id="stage-1", group_id="1",
                     sequence=91, bib="901", passage_time_ms=before - DAY, lap=0, passage_timestamp_ms=before,
                     received_at_ms=before + 300, group_name="男子组"),
    ])
    reads = (*workspace.reads, *outside)

    live = analyse_camera(replace(config, live=True), workspace, 1, strips, reads=reads)
    live_kinds = Counter(item.kind for item in live["items"])
    assert live["waiting_reads"] == 2 and live_kinds["evidence_pending"] == 2
    assert live_kinds[KIND_NO_VIDEO] == 1  # the one before the recording, still inside the watermark

    settled = analyse_camera(replace(config, live=True, evidence_delay_ms=0), workspace, 1, strips, reads=reads)
    settle_kinds = Counter(item.kind for item in settled["items"])
    # Both gaps are reported once each, and nothing is listed twice.
    assert settle_kinds[KIND_NO_VIDEO] == 2 and settle_kinds["evidence_pending"] == 0
    assert len({item.item_id for item in settled["items"]}) == len(settled["items"])
    assert settled["waiting_reads"] == 0
    assert settled["analyzed_through_ms"] == live["analyzed_through_ms"] + 10_000
    # The rest of the race is unaffected: the same riders are matched, plus the
    # one the live waiting zone was still holding back.
    assert len(settled["auto_passed"]) == len(live["auto_passed"]) + 1
    assert {entry["bib"] for entry in live["auto_passed"]} < {entry["bib"] for entry in settled["auto_passed"]}


def test_two_second_segments_are_analyzed_before_archive_and_keep_ids(synthetic_race, tmp_path):
    root, _workspace, _config, _strips = synthetic_race
    for name in ("cyclerace_passage_events.jsonl", "finish_lines.json"):
        (tmp_path / name).write_bytes((root / name).read_bytes())
    source = next((root / "videos").glob("*.avi"))
    capture = cv2.VideoCapture(str(source))
    timeline = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    index = 0
    while True:
        frames = []
        for _ in range(2 * FPS):
            ok, frame = capture.read()
            if not ok:
                break
            frames.append(frame)
        if not frames:
            break
        path = tmp_path / f"camera_01_session_review_{index:016d}.avi"
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), FPS, (WIDTH, HEIGHT))
        for frame in frames:
            writer.write(frame)
        writer.release()
        timeline.add_completed_segment(source_id="camera_01_review", camera_index=1, video_path=path,
                                       media_started_at_ms=START + index * 2_000,
                                       media_duration_ms=len(frames) * 1000 // FPS,
                                       clock_source="videopipe_system_clock", timing_error_ms=0,
                                       end_reason="retained_problem_segment", race_id=RACE)
        index += 1
    capture.release()
    config = AnalysisConfig(workspace=tmp_path, live=True, render_cards=False, min_available_mb=0)
    live = run_offline_analysis(config, lower_priority=False)
    payload = json.loads((tmp_path / "finish_problems/problem_list.json").read_text(encoding="utf-8"))
    assert live["summary"]["auto_passed"] >= len(CROSSINGS_S) - 2
    before = {item["frame"]["segment_id"] for item in payload["auto_passed"]}
    archive = tmp_path / "camera_01_session_archive_0000.avi"
    archive.write_bytes(source.read_bytes())
    timeline.add_completed_segment(source_id="camera_01_review", camera_index=1, video_path=archive,
                                   media_started_at_ms=START, media_duration_ms=95_000,
                                   clock_source="videopipe_system_clock", timing_error_ms=0,
                                   end_reason="continuous_archive_fallback", race_id=RACE)
    workspace = AnalysisWorkspace(tmp_path, include_review_segments=True)
    assert len(workspace.archive_segments()) == index
    config.live = False
    run_offline_analysis(config, lower_priority=False)
    final = json.loads((tmp_path / "finish_problems/problem_list.json").read_text(encoding="utf-8"))
    assert before <= {item["frame"]["segment_id"] for item in final["auto_passed"]}
    assert not any(item["kind"] == "evidence_pending" for item in final["items"])
    # A partial short-segment gap uses the archive for just the missing span,
    # without dropping historical coverage or counting every rider twice.
    (tmp_path / "camera_01_session_review_0000000000000022.avi").unlink()
    run_offline_analysis(config, lower_priority=False)
    repaired = json.loads((tmp_path / "finish_problems/problem_list.json").read_text(encoding="utf-8"))
    bibs = [item["bib"] for item in repaired["auto_passed"]]
    assert len(bibs) == len(set(bibs)) == len(CROSSINGS_S)


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


@pytest.mark.parametrize("days", [-3, 3])
def test_different_official_date_pairs_video_and_preserves_export_time(synthetic_race, days):
    _root, workspace, config, strips = synthetic_race
    shift = days * 86_400_000
    reads = tuple(replace(read, chip_ms=read.chip_ms + shift,
                          latest_chip_ms=read.latest_chip_ms + shift)
                  for read in workspace.reads)
    result = analyse_camera(config, workspace, 1, strips, reads=reads)
    assert result["pairing"]["auto_paired"] == len(CROSSINGS_S)
    assert _kinds(result) == []
    assert {item["chip_time_ms"] for item in result["auto_passed"]} == {read.chip_ms for read in reads}
    missing = analyse_camera(config, workspace, 1, strips,
                             reads=tuple(read for read in reads if read.bib != "107"))
    item, = [item for item in missing["items"] if item.pending]
    assert item.kind == KIND_NO_CHIP_CROSSING
    assert abs(item.chip_time_ms - (START + 44_500 - OFFSET + shift)) < 150
    assert item.evidence["frame"]["frame_index"] == pytest.approx(44.5 * FPS, abs=2)


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


def test_frame_reader_reads_forward_and_seeks_back_by_real_frame_times(synthetic_race):
    from realtime.bike_check import FrameReader

    root, _workspace, _config, _strips = synthetic_race
    reader = FrameReader(root / "videos" / "camera_01_20260919_100000_000000_archive_0000.avi")
    try:
        first = [pts for _frame, pts in reader.frames_at([10_000, 10_250])]
        ahead = [pts for _frame, pts in reader.frames_at([13_000])]
        back = [pts for _frame, pts in reader.frames_at([2_000])]
    finally:
        reader.close()

    assert first == [10_000.0, 10_250.0]
    assert ahead == [13_000.0]
    assert back == [2_000.0]


def test_gui_started_run_uses_workspace_config_and_publishes_calibration(synthetic_race, monkeypatch):
    root, _workspace, _config, _strips = synthetic_race
    (root / "finish_problems").mkdir(exist_ok=True)
    (root / "finish_problems" / ANALYSIS_CONFIG_FILENAME).write_text(
        json.dumps({"crop_modes": {"1": "side-right"}, "cameras": [1]}), encoding="utf-8",
    )
    config = config_from_workspace(root)
    assert config.crop_modes == {1: "side-right"} and config.model_path is None
    monkeypatch.setattr(problem_analysis, "available_memory_mb", lambda: 6_000)

    summary = run_offline_analysis(config, lower_priority=False)

    assert summary["summary"]["pending"] == 0
    assert summary["summary"]["auto_passed"] == len(CROSSINGS_S)
    assert (root / "finish_problems" / "problem_list.json").is_file()
    assert (root / "video_clock_models.jsonl").is_file()


def test_analysis_does_not_start_when_memory_is_short(synthetic_race, monkeypatch, tmp_path):
    root, _workspace, _config, _strips = synthetic_race
    monkeypatch.setattr(problem_analysis, "available_memory_mb", lambda: 1_500)
    config = AnalysisConfig(workspace=root, output_dir=tmp_path / "out", min_available_mb=4_096)

    summary = run_offline_analysis(config, lower_priority=False)

    assert "1500 MB" in summary["skipped"] and "4096 MB" in summary["skipped"]
    assert summary["memory"]["available_at_start_mb"] == 1_500
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("available,starts", [(1535, False), (1536, True), (2048, True)])
def test_default_memory_gate_boundary(synthetic_race, monkeypatch, tmp_path, available, starts):
    root, _workspace, _config, _strips = synthetic_race
    monkeypatch.setattr(problem_analysis, "available_memory_mb", lambda: available)
    extracted = []
    monkeypatch.setattr(problem_analysis, "extract_strips", lambda *args, **kwargs: extracted.append(True) or [])
    config = AnalysisConfig(workspace=root, output_dir=tmp_path / "out")

    summary = run_offline_analysis(config, stage="strips", lower_priority=False)

    assert config.min_available_mb == 1536
    assert bool(extracted) == starts
    assert ("skipped" not in summary) == starts
    if not starts:
        assert "1536 MB" in summary["skipped"]
        assert not config.resolved_output().exists()


def test_workspace_memory_threshold_default_and_override(tmp_path):
    assert config_from_workspace(tmp_path).min_available_mb == 1536
    output = tmp_path / "finish_problems"
    output.mkdir()
    (output / ANALYSIS_CONFIG_FILENAME).write_text(
        json.dumps({"min_available_mb": 4096}), encoding="utf-8",
    )
    assert config_from_workspace(tmp_path).min_available_mb == 4096


@pytest.mark.parametrize("options,expected", [([], 1536), (["--min-free-mb", "2048"], 2048)])
def test_cli_memory_threshold_default_and_override(tmp_path, monkeypatch, options, expected):
    from tools import finish_problem_list

    seen = []
    monkeypatch.setattr(finish_problem_list, "run", lambda config, stage: seen.append(config) or {})
    assert finish_problem_list.main(["--workspace", str(tmp_path), *options]) == 0
    assert seen[0].min_available_mb == expected


def test_bike_check_keeps_one_decoder_open_at_a_time(monkeypatch, tmp_path):
    from realtime.bike_check import BikeVerdict
    from realtime.problem_rules import CrossingRecord

    opened, state = [], {"open": 0, "max": 0}

    class FakeReader:
        def __init__(self, path, threads=2):
            self.path = path
            opened.append(str(path))
            state["open"] += 1
            state["max"] = max(state["max"], state["open"])

        def frames_at(self, targets, tolerance_ms=20.0):
            return []

        def close(self):
            state["open"] -= 1

    monkeypatch.setattr(problem_analysis, "FrameReader", FakeReader)
    monkeypatch.setattr(problem_analysis, "YoloxDetector", lambda *args: object())
    monkeypatch.setattr(
        problem_analysis, "classify_frames",
        lambda *args, **kwargs: BikeVerdict("bicycle", 0.9, "keep"),
    )
    crossings = [
        CrossingRecord(1, "s", "seg", f"video_{index % 3}.mkv", 1_000 * index, index, 1_000 * index, 0, 0, 1)
        for index in range(9)
    ]
    config = AnalysisConfig(workspace=tmp_path, output_dir=tmp_path / "out", model_path=tmp_path / "m.onnx")
    config.model_path.write_bytes(b"mocked model content")

    verdicts = problem_analysis._bike_verdicts(
        config, 1, crossings, lambda _ms: False, progress=lambda *args: None, checkpoint=None,
    )

    assert len(verdicts) == 9
    assert state["max"] == 1 and state["open"] == 0
    assert opened == ["video_0.mkv", "video_1.mkv", "video_2.mkv"]


def test_cached_bike_verdicts_are_re_decided_with_current_rules(tmp_path):
    from realtime.problem_rules import CrossingRecord

    crossing = CrossingRecord(1, "s", "seg", "video.mkv", 5_000, 100, 5_000, 0, 0, 1)
    config = AnalysisConfig(workspace=tmp_path, output_dir=tmp_path, model_path=tmp_path / "m.onnx")
    config.model_path.write_bytes(b"cached model content")
    key = problem_analysis.bike_cache_key(problem_analysis.model_fingerprint(config.model_path, 640),
                                         problem_analysis.CropPlan.for_mode("up"), crossing)
    (tmp_path / "bike_camera1.json").write_text(json.dumps({key: {
        "label": "vehicle", "score": 0.9, "decision": "disputed", "reason": "把握不高或几帧不一致",
        "frames": 3, "vehicle": "摩托车", "details": [[0, 0, 0], [0, 0, 0.9], [0, 0, 0.85]],
    }}), encoding="utf-8")

    verdicts = problem_analysis._bike_verdicts(
        config, 1, [crossing], lambda _ms: False, progress=lambda *args: None, checkpoint=None,
    )

    assert verdicts[crossing.key].decision == "exclude"


def test_editor_line_drives_full_pipeline_and_new_calibration_version(synthetic_race, tmp_path):
    import shutil
    import time
    from PyQt5.QtWidgets import QApplication
    from realtime.sampling_dialog import SamplingSettingsDialog
    from realtime.line_sampling import read_strip_meta

    source, _workspace, _config, _strips = synthetic_race
    root = tmp_path / "isolated"
    shutil.copytree(source, root)
    app = QApplication.instance() or QApplication([])
    before = run_offline_analysis(replace(config_from_workspace(root), render_cards=False, min_available_mb=0), lower_priority=False)
    previous = before["cameras"][1]["calibration"]["version"]
    dialog = SamplingSettingsDialog(root)
    deadline = time.monotonic() + 10
    while dialog.worker is not None and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.01)
    assert dialog.worker is None
    dialog.view.points = [[.5, .15 + i * .7 / 8] for i in range(9)]
    dialog._save()
    summary = run_offline_analysis(replace(config_from_workspace(root), render_cards=False, min_available_mb=0), lower_priority=False)
    assert summary["summary"]["auto_passed"] == len(CROSSINGS_S)
    assert summary["cameras"][1]["calibration"]["version"] > previous
    strips = list((root / "finish_problems/strips/camera_01").glob("*.npz"))
    assert len(read_strip_meta(strips[0])["line_config"]["points"]) == 9
    payload = json.loads((root / "finish_problems/problem_list.json").read_text(encoding="utf-8"))
    assert payload["auto_passed"][0]["frame"]["line_config"]["revision"] == 1
    assert payload["auto_passed"][0]["frame"]["geometry_digest"]


def test_crossing_gate_failure_keeps_line_evidence_without_claiming_crossings(synthetic_race, tmp_path, monkeypatch):
    from realtime.target_crossing import TargetCrossingCheck

    _root, workspace, original, strips = synthetic_race
    reads = tuple(read for i, read in enumerate(workspace.reads) if i != 5)
    config = replace(original, output_dir=tmp_path)
    disabled = analyse_camera(config, workspace, 1, strips, reads=reads)
    assert disabled["bike_status"]["state"] == "not_configured"
    config.model_path = tmp_path / "bad.onnx"
    config.model_path.write_bytes(b"invalid model")
    failed = analyse_camera(config, workspace, 1, strips, reads=reads)
    assert failed["bike_status"]["state"] == "failed"
    assert failed["crossings"] == 0 and not failed["auto_passed"]
    assert failed["line_candidates"] == len(CROSSINGS_S)
    assert len([i for i in failed["items"] if i.evidence.get("crossing_check")]) == len(CROSSINGS_S)
    monkeypatch.setattr(problem_analysis, "_verify_crossing_candidates", lambda _c, _n, _f, crossings, **kw: {
        c.key: TargetCrossingCheck("confirmed", "anchor_crossed_line", "bicycle", .9) for c in crossings
    })
    enabled = analyse_camera(config, workspace, 1, strips, reads=reads)
    assert enabled["bike_status"]["state"] == "enabled"
    assert _kinds(enabled) == _kinds(disabled)


def test_near_crossing_candidate_is_published_and_its_closure_keeps_the_history(synthetic_race, tmp_path):
    from realtime.problem_items import KIND_NEAR_CROSSING, ProblemItem

    _root, workspace, original, _strips = synthetic_race
    config = replace(original, output_dir=tmp_path, render_cards=False)
    segment = workspace.archive_segments(1)[0]
    moment = segment.start_ms + 35_000
    frame = {"camera_index": 1, "segment_id": segment.segment.segment_id, "video_path": str(segment.path),
             "frame_index": 700, "position_ms": 35_000, "session_id": segment.session_id, "calibration_version": 3}
    read = workspace.reads[0]
    candidate = ProblemItem(
        "near_crossing:test", KIND_NEAR_CROSSING, "video",
        f"芯片 {read.bib} 附近 500 ms 处线上另有一处变化", chip_time_ms=read.chip_ms, priority=15,
        evidence={"frame": frame, "recorder_ms": moment, "gap_ms": 500,
                  "near_read": {"bib": read.bib, "event_id": read.event_id, "chip_ms": read.chip_ms}},
    )

    first = json.loads(write_problem_list(
        config, workspace, [{"camera_index": 1, "items": [candidate], "auto_passed": []}]).read_text(encoding="utf-8"))
    assert first["summary"]["pending"] == 1 and first["summary"]["candidates_closed"] == 0
    assert [item["kind"] for item in first["items"] if item["kind"] == KIND_NEAR_CROSSING] == [KIND_NEAR_CROSSING]

    # Next run: the late chip has claimed that crossing, so no candidate is left.
    late = {"bib": "319", "frame": frame, "chip_time_ms": read.chip_ms, "late_pair": True}
    second = json.loads(write_problem_list(
        config, workspace, [{"camera_index": 1, "items": [], "auto_passed": [], "late_pair_frames": [late]}]
    ).read_text(encoding="utf-8"))
    assert second["summary"]["pending"] == 0 and second["summary"]["candidates_closed"] == 1
    closed = [item for item in second["items"] if item["kind"] == KIND_NEAR_CROSSING]
    assert len(closed) == 1 and closed[0]["status"] == "info"
    assert "迟到芯片 319 已匹配该过线" in closed[0]["status_note"]
    assert closed[0]["evidence"]["recorded_history"] is True
    journal = (config.resolved_output() / "near_crossing_history.jsonl").read_text(encoding="utf-8")
    assert '"state": "open"' in journal and '"state": "closed"' in journal


def test_the_publisher_and_the_ledger_name_a_batch_the_same_way(tmp_path):
    """The list and the workbench's ledger keep one id for the operator's task."""
    from realtime.batch_review import BATCH_IDENTITY_FILENAME
    from realtime.chip_history import ChipRead

    values = [ChipRead(f"event-{index}", str(index), f"chip-{index}", "1", "男子组", "",
                       START + index * 20, index * 20, 0, START + index * 20, 1, 1, True, False)
              for index in range(8)]

    class _Workspace:
        race_id, stage_id, race_date = RACE, "stage-1", "2026-09-19"

        def __init__(self):
            self.reads, self.root = values, tmp_path

        def metadata(self):
            return None

        def latest_event(self, _event_id):
            return None

    config = replace(AnalysisConfig(workspace=tmp_path), output_dir=tmp_path / "out", render_cards=False)
    ledger_path = config.output_dir / BATCH_IDENTITY_FILENAME
    first = json.loads(write_problem_list(config, _Workspace(), []).read_text(encoding="utf-8"))
    parent = next(item for item in first["items"] if item["evidence"].get("batch_members"))
    assert [entry["item_id"] for entry in json.loads(ledger_path.read_text(encoding="utf-8"))["items"]] \
        == [parent["item_id"]]

    # The queue handed this batch an id of its own (a restarted publisher would
    # otherwise hash the same chips into a new one and orphan the operator's
    # drafts).  The next list must keep that id, not mint another.
    handed_over = "chip_batch:handed-over"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    ledger["items"][0]["item_id"] = handed_over
    ledger_path.write_text(json.dumps(ledger, ensure_ascii=False), encoding="utf-8")
    second = json.loads(write_problem_list(config, _Workspace(), []).read_text(encoding="utf-8"))
    kept = next(item for item in second["items"] if item["evidence"].get("batch_members"))
    assert kept["item_id"] == handed_over
    assert len(kept["event_ids"]) == len(parent["event_ids"])
    assert json.loads(ledger_path.read_text(encoding="utf-8"))["items"][0]["item_id"] == handed_over


def test_summary_keeps_matches_inside_pending_periods_separate():
    from realtime.problem_analysis import summarize_items

    summary = summarize_items([], [{"event_id": "a", "active_now": True},
                                   {"event_id": "b", "active_now": True, "pending_period": "bunch:1"}], 2)

    assert summary["auto_passed"] == 2 and summary["auto_passed_in_pending_period"] == 1
    assert summary["unverifiable"] == 2


# The operator's first-rider calibration ("以此人校时") must start the backend
# even before the automatic fit has any two isolated riders to fit on.

SESSION = "camera_01_20260919_100000_000000"


def _isolated_race(tmp_path, synthetic_race):
    """A per-test copy of the synthetic race, so calibration anchors stay local."""

    root, _workspace, config, strips = synthetic_race
    workspace_root = tmp_path / "race"
    workspace_root.mkdir()
    for name in ("cyclerace_passage_events.jsonl", "finish_lines.json"):
        (workspace_root / name).write_bytes((root / name).read_bytes())
    workspace = AnalysisWorkspace(workspace_root)
    config = replace(config, workspace=workspace_root, output_dir=tmp_path / "out")
    return workspace_root, workspace, config, strips


def _record_first_rider(root, read, *, session_id=SESSION, offset_ms=OFFSET, bib=None, camera_index=1):
    """What the operator's first-rider confirmation writes into the journal."""

    from realtime.passage_evidence import VideoClockCalibrationStore

    VideoClockCalibrationStore(root / "video_clock_calibrations.jsonl").record(
        camera_index=camera_index,
        session_key=f"camera_01_review|{root / 'videos'}|{session_id}",
        offset_ms=int(offset_ms), anchor_event_id=read.event_id,
        anchor_bib=str(bib if bib is not None else read.bib), calibrated_at_ms=int(read.chip_ms) + 1_000,
    )


def _blank_strip(path, start_ms):
    """One sealed recording session whose line nothing ever crosses."""

    from realtime.line_sampling import LineGeometry, LineStrip, save_strip

    pts = np.arange(0.0, 60_000.0, 50.0)
    geometry = LineGeometry.from_points(
        [[0.5 * WIDTH, 0.1 * HEIGHT], [0.5 * WIDTH, 0.9 * HEIGHT]], WIDTH, HEIGHT, samples=120,
    )
    strip = LineStrip(strip=np.zeros((geometry.samples, pts.size, 3), np.uint8), pts_ms=pts,
                      geometry=geometry, video_path="", fps=float(FPS))
    save_strip(path, strip, {
        "line_digest": "blank-line", "line_config": {"camera_index": 1}, "samples": 120,
        "segment_id": "blank-segment", "session_id": "camera_01_20260919_235959_000000",
        "camera_index": 1, "media_started_at_ms": int(start_ms), "media_duration_ms": int(pts[-1]),
        "video_size": 0, "excluded_ranges": [],
    })
    return path


def test_first_rider_anchor_starts_pairing_before_any_automatic_fit(synthetic_race, tmp_path):
    from realtime.clock_sync import ClockModelStore

    root, workspace, config, strips = _isolated_race(tmp_path, synthetic_race)
    first = workspace.reads[0]
    _record_first_rider(root, first)

    result = analyse_camera(config, workspace, 1, strips, reads=(first,))

    assert "error" not in result
    calibration = result["calibration"]
    assert calibration["source"] == "manual_anchor" and calibration["source_text"] == "人工首人锚点"
    assert calibration["slope_ms_per_hour"] == 0.0  # one point, no invented drift
    assert calibration["kept"] == 1 and calibration["paired_sessions"] == [SESSION]
    entry, = calibration["anchors"]
    assert entry["within_tolerance"] and "尚无自动校时可比" in entry["message"]
    assert result["pairing"]["auto_paired"] == 1
    assert [value["bib"] for value in result["auto_passed"]] == [first.bib]
    assert any("人工首人锚点" in text for text in result["calibration_warnings"])
    model = ClockModelStore(config.resolved_output() / "video_clock_models.jsonl").latest(1)
    assert model.source == "manual_anchor" and model.slope == 0.0 and model.support == 1


def test_without_a_usable_anchor_the_camera_still_waits(synthetic_race, tmp_path):
    from realtime.problem_items import KIND_WAITING

    _root, workspace, config, strips = _isolated_race(tmp_path, synthetic_race)
    first = workspace.reads[0]

    result = analyse_camera(replace(config, live=True), workspace, 1, strips, reads=(first,))

    assert result.get("calibration") is None and "自动校时失败" in result["error"]
    assert [item.kind for item in result["items"]] == [KIND_WAITING]
    assert not (config.resolved_output() / "video_clock_models.jsonl").exists()


def test_video_model_runs_before_first_rider_calibration(synthetic_race, tmp_path, monkeypatch):
    """A finish-line camera can be tested with a walking person before chips arrive."""

    from realtime.target_crossing import TargetCrossingCheck

    root, workspace, config, strips = _isolated_race(tmp_path, synthetic_race)
    model = tmp_path / "camera-test.onnx"
    model.write_bytes(b"test model")
    config = replace(config, model_path=model, live=True)
    monkeypatch.setattr(
        problem_analysis,
        "_verify_crossing_candidates",
        lambda _config, _camera, _files, crossings, **_kwargs: {
            crossing.key: TargetCrossingCheck("confirmed", "anchor_crossed_line", "pedestrian", .91)
            for crossing in crossings
        },
    )

    result = analyse_camera(config, workspace, 1, strips, reads=())

    assert "error" not in result
    assert result["calibration_pending"] is True
    assert result["bike_status"]["state"] == "enabled"
    assert any(item.evidence.get("bike", {}).get("label") == "pedestrian" for item in result["items"])
    assert "first_rider_candidate" not in result  # A pedestrian is not the first rider.


@pytest.mark.parametrize("state", ["confirmed", "not_crossed", "unverified"])
def test_obstructed_line_events_need_verified_crossings_before_entering_the_queue(
    synthetic_race, tmp_path, monkeypatch, state,
):
    from realtime.target_crossing import TargetCrossingCheck

    _root, workspace, config, strips = _isolated_race(tmp_path, synthetic_race)
    model = tmp_path / "test.onnx"
    model.write_bytes(b"test")
    detector = problem_analysis.detect_session_crossings

    def obstructed(*args, **kwargs):
        crossings, obstacles, coverage, raw = detector(*args, **kwargs)
        return [replace(c, in_obstruction=True) for c in crossings], obstacles, coverage, raw

    monkeypatch.setattr(problem_analysis, "detect_session_crossings", obstructed)
    monkeypatch.setattr(problem_analysis, "_verify_crossing_candidates", lambda _c, _n, _f, crossings, **kw: {
        c.key: TargetCrossingCheck(state, "test", "pedestrian", .91) for c in crossings
    })
    result = analyse_camera(replace(config, model_path=model, live=True), workspace, 1, strips, reads=())

    assert result["analyzed_through_ms"] > START
    assert result["calibration_pending"] and not result["auto_passed"]
    assert result["crossings"] == (len(CROSSINGS_S) if state == "confirmed" else 0)
    if state == "confirmed":
        assert all(i.status == "disputed" and i.kind == KIND_NO_CHIP_CROSSING for i in result["items"])
    elif state == "unverified":
        assert all(i.kind == "line_activity" and i.evidence["frame"] for i in result["items"])
        assert len(result["items"]) == len(CROSSINGS_S)
    else:
        assert result["items"] == []
    saved = json.loads((config.resolved_output() / "crossings_camera1.json").read_text(encoding="utf-8"))
    assert len(saved["crossings"]) == result["crossings"]
    assert len(saved["line_events"]) == len(CROSSINGS_S)


def test_crossing_gate_can_read_context_across_two_second_segments(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from realtime.problem_rules import CrossingRecord
    from realtime.bike_check import Detection

    model = tmp_path / "test.onnx"
    model.write_bytes(b"test")
    files = []
    for i in range(2):
        video = tmp_path / f"part{i}.ts"
        video.write_bytes(b"sealed")
        files.append(problem_analysis._FileStrip(
            tmp_path / f"part{i}.npz",
            {"video_path": str(video), "session_id": "session", "line_digest": "line"},
            i * 2000, np.arange(0, 2000, 40), SimpleNamespace(width=320, height=240),
        ))
    crossing = CrossingRecord(1, "session", "segment", str(tmp_path / "part0.ts"),
                              1960, 49, 1960, 0, 0, 1, contact=(110, 100),
                              segment_points=((0, 100), (300, 100)))
    opened, requested = [], []

    class Reader:
        def __init__(self, path, threads):
            self.path = path
            opened.append(path)

        def frames_at(self, positions):
            requested.append((self.path, positions[0]))
            return [(np.zeros((240, 320, 3), np.uint8), positions[0])]

        def close(self):
            pass

    class Detector:
        def __init__(self, *args):
            self.i = 0

        def detect(self, frame):
            bottom = (120, 100, 80)[self.i]
            self.i += 1
            return [Detection(0, .91, 40, bottom - 60, 60, 60)]

    monkeypatch.setattr(problem_analysis, "FrameReader", Reader)
    monkeypatch.setattr(problem_analysis, "YoloxDetector", Detector)
    config = AnalysisConfig(tmp_path, model_path=model)
    result = problem_analysis._verify_crossing_candidates(
        config, 1, files, [crossing], progress=lambda *a: None, checkpoint=None,
    )
    assert result[crossing.key].state == "confirmed"
    assert opened == [str(tmp_path / "part0.ts"), str(tmp_path / "part1.ts")]
    assert requested[-1] == (str(tmp_path / "part1.ts"), 560)
    # A cached verified event does not repeat model inference.
    monkeypatch.setattr(problem_analysis, "YoloxDetector", lambda *a: pytest.fail("cache missed"))
    assert problem_analysis._verify_crossing_candidates(
        config, 1, files, [crossing], progress=lambda *a: None, checkpoint=None,
    )[crossing.key].state == "confirmed"
    # A different line/profile cannot supply the missing neighbour.
    files[1].meta["line_digest"] = "changed"
    assert len(problem_analysis._verification_sources(files, crossing)) == 2


@pytest.mark.parametrize("label", ["pedestrian", "vehicle"])
def test_verified_non_bicycle_targets_cannot_be_auto_paired_to_chips(synthetic_race, tmp_path, monkeypatch, label):
    from realtime.target_crossing import TargetCrossingCheck

    root, workspace, config, strips = _isolated_race(tmp_path, synthetic_race)
    first = workspace.reads[0]
    _record_first_rider(root, first)
    workspace = AnalysisWorkspace(root)
    model = tmp_path / "test.onnx"
    model.write_bytes(b"test")
    monkeypatch.setattr(problem_analysis, "_verify_crossing_candidates", lambda _c, _n, _f, crossings, **kw: {
        c.key: TargetCrossingCheck("confirmed", "anchor_crossed_line", label, .91) for c in crossings
    })
    result = analyse_camera(replace(config, model_path=model), workspace, 1, strips)
    assert result["calibration"]["source"] == "manual_anchor"
    assert not result["auto_passed"] and not result["late_pair_frames"]
    assert result["pairing"]["auto_paired"] == 0


def test_scanned_empty_video_advances_without_chips_or_clock_calibration(synthetic_race, tmp_path, monkeypatch):
    _root, workspace, config, strips = _isolated_race(tmp_path, synthetic_race)
    model = tmp_path / "test.onnx"
    model.write_bytes(b"test")
    monkeypatch.setattr(problem_analysis, "detect_session_crossings", lambda *a, **kw: (
        [], [], [(START, START + 95_000, "session")], [],
    ))
    monkeypatch.setattr(problem_analysis, "YoloxDetector", lambda *a: pytest.fail("empty video invoked model"))
    result = analyse_camera(replace(config, model_path=model, live=True), workspace, 1, strips, reads=())
    assert result["analyzed_through_ms"] == START + 85_000
    assert result["items"] == [] and result["crossings"] == 0
    assert result["bike_status"]["state"] == "no_candidates"


def test_an_anchor_of_another_recording_session_cannot_start_pairing(synthetic_race, tmp_path):
    root, workspace, config, strips = _isolated_race(tmp_path, synthetic_race)
    first = workspace.reads[0]
    _record_first_rider(root, first, session_id="camera_01_20260919_235959_000000")

    result = analyse_camera(config, workspace, 1, strips, reads=(first,))

    assert result.get("calibration") is None
    assert any("其他录像会话" in text for text in result["calibration_warnings"])
    assert not (config.resolved_output() / "video_clock_models.jsonl").exists()


def test_a_changed_chip_record_cannot_start_pairing(synthetic_race, tmp_path):
    root, workspace, config, strips = _isolated_race(tmp_path, synthetic_race)
    first = workspace.reads[0]
    _record_first_rider(root, first, bib="999")  # the confirmed bib no longer stands

    result = analyse_camera(config, workspace, 1, strips, reads=(first,))

    assert result.get("calibration") is None
    assert any("已作废、替换或已变更" in text for text in result["calibration_warnings"])


def test_a_voided_or_revised_chip_record_cannot_start_pairing(synthetic_race, tmp_path):
    root, workspace, config, strips = _isolated_race(tmp_path, synthetic_race)
    first = workspace.reads[0]
    _record_first_rider(root, first)

    for stale in (replace(first, active_now=False), replace(first, chip_ms=first.chip_ms + 5_000)):
        result = analyse_camera(config, workspace, 1, strips, reads=(stale,))
        assert result.get("calibration") is None
        assert any("已作废、替换或已变更" in text for text in result["calibration_warnings"])


def test_a_conflicting_automatic_model_does_not_replace_the_manual_anchor(synthetic_race, tmp_path):
    from realtime.clock_sync import ClockModelStore

    root, workspace, config, strips = _isolated_race(tmp_path, synthetic_race)
    _record_first_rider(root, workspace.reads[0], offset_ms=OFFSET + 345)

    result = analyse_camera(config, workspace, 1, strips)

    calibration = result["calibration"]
    assert calibration["source"] == "manual_anchor" and calibration["slope_ms_per_hour"] == 0.0
    assert calibration["manual_conflict"] is True and calibration["manual_conflict_ms"] > 300
    entry, = calibration["anchors"]
    assert not entry["within_tolerance"] and entry["difference_ms"] > 300
    assert any("未采用自动模型" in text for text in result["calibration_warnings"])
    # Pairing still follows the operator's offset, not the automatic fit.
    assert result["pairing"]["auto_paired"] == len(CROSSINGS_S)
    assert -400 < result["pairing"]["residual_median_ms"] < -300
    model = ClockModelStore(config.resolved_output() / "video_clock_models.jsonl").latest(1)
    assert model.source == "manual_anchor" and model.slope == 0.0


def test_an_anchor_within_tolerance_keeps_the_automatic_model(synthetic_race, tmp_path):
    root, workspace, config, strips = _isolated_race(tmp_path, synthetic_race)
    _record_first_rider(root, workspace.reads[0], offset_ms=OFFSET + 100)

    result = analyse_camera(config, workspace, 1, strips)

    calibration = result["calibration"]
    assert calibration["source"] == "auto" and calibration["manual_conflict"] is False
    assert "paired_sessions" not in calibration
    entry, = calibration["anchors"]
    assert entry["within_tolerance"] and "一致" in entry["message"]
    assert result["pairing"]["auto_paired"] == len(CROSSINGS_S)
    assert _kinds(result) == []


def test_an_anchor_serves_only_its_own_recording_session(synthetic_race, tmp_path):
    from realtime.problem_items import KIND_WAITING

    root, workspace, config, strips = _isolated_race(tmp_path, synthetic_race)
    first = workspace.reads[0]
    _record_first_rider(root, first)
    far_start = START + 24 * 3_600_000
    blank = _blank_strip(tmp_path / "camera_01_20260919_235959_000000.npz", far_start)
    straggler = replace(first, event_id="late-passage", bib="777",
                        chip_ms=far_start + 40_000 - OFFSET, received_at_ms=far_start + 40_000)

    result = analyse_camera(replace(config, live=True), workspace, 1, [*strips, blank],
                            reads=(first, straggler))

    assert result["calibration"]["paired_sessions"] == [SESSION]
    assert [value["bib"] for value in result["auto_passed"]] == [first.bib]
    assert result["pairing"]["auto_paired"] == 1 and result["pairing"]["uncalibrated"] == 1
    waiting, = [item for item in result["items"] if item.kind == KIND_WAITING]
    assert waiting.bibs == ("777",) and "235959" in waiting.detail
    assert result["waiting_reads"] == 1
    assert any("没有自动校时或人工首人锚点" in text for text in result["calibration_warnings"])
