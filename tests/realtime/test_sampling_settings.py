import json
import os
import time
from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np
import pytest
from PyQt5.QtCore import QPoint, QPointF, Qt
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QDialogButtonBox

from realtime.analysis_settings import AnalysisSettingsStore, CONFIG_PATH, model_fingerprint, settings_token
from realtime.analysis_workspace import AnalysisWorkspace
from realtime.bike_check import BikeVerdict, probe_model
from realtime.line_sampling import read_strip_meta
from realtime.problem_analysis import AnalysisConfig, config_from_workspace, extract_strips, line_for_segment
from realtime.problem_items import ProblemItem, CATEGORY_VIDEO, KIND_NO_CHIP_CROSSING, STATUS_WAITING
from realtime.problem_panel import ProblemListPanel
from realtime.problem_resolutions import ProblemResolutionStore, export_lists
from realtime.sampling_dialog import SamplingLineView, SamplingSettingsDialog
from realtime.video_timeline import VideoTimelineStore


POINTS = [[.2 + i * .06, .2 + i * .07] for i in range(9)]
T = 1_789_784_000_000


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


def pump(app, condition):
    deadline = time.monotonic() + 10
    while not condition() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.01)
    assert condition()


@pytest.fixture
def sealed(tmp_path):
    (tmp_path / "review_event_scope.json").write_text(json.dumps({"race_id": "race", "stage_id": "stage"}))
    timeline = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    for i in range(2):
        path = tmp_path / f"camera_01_session_archive_{i:04d}.avi"
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 10, (320, 180))
        for _ in range(20):
            writer.write(np.full((180, 320, 3), 80, np.uint8))
        writer.release()
        timeline.add_completed_segment(source_id="cam", camera_index=1, video_path=path,
                                       media_started_at_ms=T + i * 2000, media_duration_ms=2000,
                                       clock_source="videopipe_system_clock", timing_error_ms=0,
                                       end_reason="continuous_archive_fallback", race_id="race")
    legacy = {"finish_lines": [{"camera_index": 1, "points": POINTS, "snap": False}]}
    (tmp_path / "long_image_lines.json").write_text(json.dumps(legacy), encoding="utf-8")
    source = AnalysisWorkspace(tmp_path)
    return tmp_path, source, source.archive_segments(1)


def save(store, segment, points=POINTS, **extra):
    return store.save(race_id="race", stage_id="stage", segment=segment, line={"points": points},
                      model_path=extra.get("model_path", ""), crop_mode=extra.get("crop_mode", "up"))


def test_polyline_roundtrip_keeps_legacy_and_session_boundaries(sealed):
    root, source, segments = sealed
    legacy = (root / "long_image_lines.json").read_bytes()
    store = AnalysisSettingsStore(root)
    shifted = [[x + .03, y] for x, y in POINTS]
    profile = save(store, segments[1], shifted)
    config = config_from_workspace(root)
    assert line_for_segment(config, source, segments[0])["points"] == POINTS
    assert line_for_segment(config, source, segments[1])["points"] == shifted
    assert line_for_segment(config, source, replace(segments[1], session_id="other"))["points"] == POINTS
    other_race = AnalysisWorkspace(root, race_id="other", stage_id="stage")
    assert line_for_segment(config, other_race, segments[1])["points"] == POINTS
    assert profile["revision"] == 1
    assert AnalysisSettingsStore(root).payload["sampling_profiles"][0]["points"] == shifted
    assert (root / "long_image_lines.json").read_bytes() == legacy
    save(store, segments[1])
    assert [p["revision"] for p in store.payload["sampling_profiles"]] == [1, 2]


def test_atomic_failure_and_conflicting_edit_do_not_change_memory_or_disk(sealed, monkeypatch):
    root, _source, segments = sealed
    store = AnalysisSettingsStore(root)
    save(store, segments[0])
    before, payload = store.path.read_bytes(), store.payload
    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("disk failure")))
        with pytest.raises(OSError):
            save(store, segments[0], [[.1, .1], [.9, .9]])
    assert store.payload == payload and store.path.read_bytes() == before
    competing = AnalysisSettingsStore(root)
    save(competing, segments[1])
    with pytest.raises(ValueError, match="其他窗口"):
        save(store, segments[0])


@pytest.mark.parametrize("points", [[], [[.5, .5]], [[.5, .5], [.5, .5]], [[0, 0], [1.1, 1]], [[0, 0], [float('nan'), 1]]])
def test_invalid_geometry_never_creates_config(sealed, points):
    root, _source, segments = sealed
    with pytest.raises(ValueError):
        save(AnalysisSettingsStore(root), segments[0], points)
    assert not (root / CONFIG_PATH).exists()


def test_editor_geometry_survives_resize_letterbox_and_node_edits(qapp):
    view = SamplingLineView()
    view.resize(800, 600)
    view.show()
    view.set_frame(np.zeros((180, 320, 3), np.uint8), {"points": POINTS})
    qapp.processEvents()
    original = [p[:] for p in view.points]
    view.resize(600, 800)
    qapp.processEvents()
    assert view.points == original
    assert view.normalized_at(QPoint(2, 2)) is None
    QTest.mouseClick(view.viewport(), Qt.LeftButton, pos=QPoint(2, 2))
    assert view.points == original
    position = view.mapFromScene(QPointF(150, 60))
    QTest.mouseClick(view.viewport(), Qt.LeftButton, Qt.ShiftModifier, position)
    assert len(view.points) == 10
    view.undo()
    assert view.points == original
    node = view.mapFromScene(QPointF(POINTS[3][0] * 320, POINTS[3][1] * 180))
    QTest.mouseClick(view.viewport(), Qt.RightButton, pos=node)
    assert len(view.points) == 8
    view.undo()
    QTest.mousePress(view.viewport(), Qt.LeftButton, pos=node)
    destination = view.mapFromScene(QPointF(180, 110))
    QTest.mouseMove(view.viewport(), destination)
    QTest.mouseRelease(view.viewport(), Qt.LeftButton, pos=destination)
    assert view.points[3] == pytest.approx([180 / 320, 110 / 180], abs=.01)
    view.close()


def test_dialog_cancel_does_not_save_and_save_uses_selected_session(qapp, sealed):
    root, source, segments = sealed
    dialog = SamplingSettingsDialog(root)
    pump(qapp, lambda: dialog.worker is None)
    assert dialog.view.points == POINTS
    dialog.view.points = [[.1, .2], [.8, .7]]
    dialog.reject()
    assert not (root / CONFIG_PATH).exists()
    dialog = SamplingSettingsDialog(root)
    pump(qapp, lambda: dialog.worker is None)
    dialog.view.points = [[.1, .2], [.8, .7]]
    dialog.crop.setCurrentText("side-left")
    dialog._save()
    config = config_from_workspace(root)
    assert config.crop_modes == {1: "side-left"}
    assert line_for_segment(config, source, segments[1])["points"] == [[.1, .2], [.8, .7]]
    reopened = SamplingSettingsDialog(root)
    pump(qapp, lambda: reopened.worker is None)
    assert reopened.view.points == [[.1, .2], [.8, .7]]
    reopened.reject()


def test_saved_line_replaces_only_affected_sampling_cache(sealed):
    root, source, segments = sealed
    config = AnalysisConfig(workspace=root, race_id="race", stage_id="stage")
    paths = extract_strips(config, segments, source.finish_lines()[1])
    before = [path.stat().st_mtime_ns for path in paths]
    old_geometry = paths[1].parent / read_strip_meta(paths[1])["geometry_file"]
    old_geometry_bytes = old_geometry.read_bytes()
    save(AnalysisSettingsStore(root), segments[1], [[.2, .1], [.7, .9]])
    config = replace(config_from_workspace(root), race_id="race", stage_id="stage")
    paths = extract_strips(config, segments, source.finish_lines()[1])
    assert paths[0].stat().st_mtime_ns == before[0]
    assert paths[1].stat().st_mtime_ns != before[1]
    meta = read_strip_meta(paths[1])
    assert meta["line_config"]["revision"] == 1
    assert meta["line_config"]["points"] == [[.2, .1], [.7, .9]]
    assert meta["geometry_digest"] and meta["line_source"] == "configured"
    assert old_geometry.read_bytes() == old_geometry_bytes
    assert (paths[1].parent / meta["geometry_file"]).is_file()


def test_no_sealed_video_disables_editing(qapp, tmp_path):
    (tmp_path / "review_event_scope.json").write_text('{"race_id":"race", "stage_id":"stage"}')
    dialog = SamplingSettingsDialog(tmp_path)
    assert not dialog.buttons.button(QDialogButtonBox.Save).isEnabled()
    assert "封存" in dialog.notice.text()
    dialog.reject()


def test_config_change_blocks_old_actions_and_exports_until_reanalysis(qapp, sealed):
    root, _source, segments = sealed
    directory = root / "finish_problems"
    directory.mkdir()
    item = ProblemItem("video", KIND_NO_CHIP_CROSSING, CATEGORY_VIDEO, "candidate",
                       evidence={"frame": {"video_path": "clip.avi", "frame_index": 1}})
    payload = {"race_id": "race", "stage_id": "stage", "items": [item.to_payload()],
               "auto_passed": [{"event_id": "a"}], "summary": {"auto_passed": 1}}
    (directory / "problem_list.json").write_text(json.dumps(payload), encoding="utf-8")
    panel = ProblemListPanel()
    panel.set_scope("race", "stage")
    panel.set_workspace(root)
    panel.store.resolve(item, "record_bib", bib="307")
    save(AnalysisSettingsStore(root), segments[0])
    panel.refresh()
    assert panel.items[0].status == STATUS_WAITING
    assert panel.payload["auto_passed"] == []
    assert panel.export()["补录清单.csv"] == 0
    panel.queue_filter.setCurrentIndex(1)
    assert not panel.action_buttons
    panel.close()


def test_evidence_signature_reopens_old_decision_without_rewriting_log(tmp_path):
    item = ProblemItem("v", KIND_NO_CHIP_CROSSING, CATEGORY_VIDEO, "candidate")
    store = ProblemResolutionStore(tmp_path / "decisions.jsonl")
    store.resolve(item, "record_bib", bib="307")
    before = store.journal_path.read_bytes()
    changed = replace(item, evidence={"analysis_signature": "line-v2"})
    assert store.state(changed)[1]
    assert export_lists([changed], store, tmp_path / "exports")["补录清单.csv"] == 0
    assert store.journal_path.read_bytes() == before


def test_probe_reports_disabled_and_failed_model_without_hiding_candidate(sealed):
    root, _source, segments = sealed
    request = dict(model_path="", video_path=str(segments[0].path), position_ms=0, points=POINTS)
    assert probe_model(request)["state"] == "not_configured"
    request["model_path"] = str(root / "missing.onnx")
    assert probe_model(request)["state"] == "failed"


def test_model_digest_changes_for_same_filename_and_metadata(sealed):
    root, _source, _segments = sealed
    path = root / "model.onnx"
    path.write_bytes(b"aaaa")
    stamp = path.stat()
    before = model_fingerprint(path, 640)
    path.write_bytes(b"bbbb")
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert model_fingerprint(path, 640) != before


def test_cached_verdict_invalidated_by_model_crop_and_sampling_points(sealed, monkeypatch):
    from realtime import problem_analysis as analysis
    from realtime.problem_rules import CrossingRecord

    root, _source, segments = sealed
    model = root / "model.onnx"
    model.write_bytes(b"aaaa")
    config = AnalysisConfig(root, model_path=model)
    crossing = CrossingRecord(1, "s", "seg", str(segments[0].path), T, 0, 0, 0, 0, 1,
                              segment_points=((30., 30.), (50., 50.)))
    loaded = []
    monkeypatch.setattr(analysis, "YoloxDetector", lambda *args: loaded.append(args) or object())
    monkeypatch.setattr(analysis, "classify_frames", lambda *a, **kw: BikeVerdict("bicycle", .9, "keep"))
    def run(value=crossing):
        return analysis._bike_verdicts(config, 1, [value], lambda _ms: False, progress=lambda *a: None, checkpoint=None)
    run()
    run()
    assert len(loaded) == 1
    model.write_bytes(b"bbbb")
    run()
    assert len(loaded) == 2
    config.crop_modes = {1: "side-left"}
    run()
    assert len(loaded) == 3
    run(replace(crossing, segment_points=((100., 30.), (120., 50.))))
    assert len(loaded) == 4


def test_recording_line_and_calibration_waits_are_distinct(sealed, tmp_path):
    from realtime.problem_analysis import run_offline_analysis

    root, _source, _segments = sealed
    config = AnalysisConfig(root, min_available_mb=0, render_cards=False)
    failed = run_offline_analysis(config, lower_priority=False)
    assert "自动校时失败" in failed["cameras"][1]["error"]
    (root / "long_image_lines.json").unlink()
    missing_line = run_offline_analysis(config, lower_priority=False)
    assert "终点线" in missing_line["cameras"][1]["skipped"]
    empty = root / "empty"
    empty.mkdir()
    (empty / "review_event_scope.json").write_text('{"race_id":"race", "stage_id":"stage"}')
    missing_video = run_offline_analysis(AnalysisConfig(empty, cameras=(1,), min_available_mb=0, render_cards=False), lower_priority=False)
    assert "封存录像" in missing_video["cameras"][1]["skipped"]


def test_archive_crossing_line_change_boundary_is_not_sampled_with_old_line(sealed):
    root, source, segments = sealed
    save(AnalysisSettingsStore(root), segments[1], [[.1, .1], [.8, .9]])
    combined = replace(segments[0], segment=replace(segments[0].segment, media_duration_ms=4000))
    config = config_from_workspace(root)
    assert line_for_segment(config, source, combined) is None
    assert extract_strips(config, (combined,), source.finish_lines()[1]) == []


def test_model_only_save_does_not_create_line_revision(sealed):
    root, _source, segments = sealed
    store = AnalysisSettingsStore(root)
    save(store, segments[0])
    save(store, segments[0], model_path="different.onnx", crop_mode="side-right")
    assert len(store.payload["sampling_profiles"]) == 1
    assert store.payload["settings_revision"] == 2


def test_old_handoff_stays_stale_when_new_analysis_no_longer_has_candidate(qapp, sealed):
    root, _source, segments = sealed
    directory = root / "finish_problems"
    directory.mkdir()
    item = ProblemItem("video", KIND_NO_CHIP_CROSSING, CATEGORY_VIDEO, "candidate")
    path = directory / "problem_list.json"
    payload = {"race_id": "race", "stage_id": "stage", "items": [item.to_payload()]}
    path.write_text(json.dumps(payload), encoding="utf-8")
    panel = ProblemListPanel()
    panel.set_scope("race", "stage")
    panel.set_workspace(root)
    panel.bib_input.setText("307")
    panel._resolve("record_bib")
    assert panel.export()["补录清单.csv"] == 1
    save(AnalysisSettingsStore(root), segments[0])
    path.write_text(json.dumps(dict(payload, items=[], settings_token=settings_token(root))), encoding="utf-8")
    panel.refresh()
    assert panel.export()["补录清单.csv"] == 0
    assert panel.items[0].evidence["configuration_stale"]
    panel.close()


def test_clock_or_original_frame_change_invalidates_decision(tmp_path):
    item = ProblemItem("v", KIND_NO_CHIP_CROSSING, CATEGORY_VIDEO, "candidate", calibration_version=1,
                       evidence={"frame": {"frame_index": 30}, "analysis_signature": "same-line"})
    store = ProblemResolutionStore(tmp_path / "decisions.jsonl")
    store.resolve(item, "record_bib", bib="307")
    assert store.state(replace(item, calibration_version=2))[1]
    assert store.state(replace(item, evidence={"frame": {"frame_index": 31}, "analysis_signature": "same-line"}))[1]


@pytest.mark.parametrize("accepted,paused,running", [(True, False, False), (False, False, True), (True, True, False)])
def test_configuration_only_pauses_video_analysis(qapp, sealed, monkeypatch, accepted, paused, running):
    from unittest.mock import Mock
    from PyQt5.QtWidgets import QDialog
    from realtime import sampling_dialog

    root, _source, _segments = sealed
    panel = ProblemListPanel()
    panel.set_scope("race", "stage")
    panel.set_workspace(root)
    process = Mock() if running else None
    panel._process = process
    panel._live_paused = paused
    chip_process = panel._chip_process = Mock()
    start = Mock()
    monkeypatch.setattr(panel, "_start_analysis", start)
    dialog = Mock()
    dialog.exec_.return_value = QDialog.Accepted if accepted else QDialog.Rejected
    monkeypatch.setattr(sampling_dialog, "SamplingSettingsDialog", lambda *a, **kw: dialog)
    panel._configure_sampling()
    assert panel._live_paused == paused
    assert start.call_count == int(not paused and (accepted or running))
    if process:
        process.kill.assert_called_once()
    assert chip_process.mock_calls == []
    assert panel._chip_process is chip_process
    panel._chip_process = None
    panel.close()


def test_motion_store_writes_separate_file_without_overwriting_sampling_line(tmp_path):
    from realtime.finish_line import FinishLine, FinishLineStore

    legacy = tmp_path / "finish_lines.json"
    legacy.write_text(json.dumps({"finish_lines": [{"camera_index": 1, "x1": .1, "y1": .1,
                                                   "x2": .9, "y2": .9}]}))
    before = legacy.read_bytes()
    motion = FinishLineStore(tmp_path / "motion_finish_lines.json", legacy_path=legacy)
    assert motion.get(1).x1 == .1
    motion.set(FinishLine(1, .5, .1, .5, .9))
    assert legacy.read_bytes() == before
    assert FinishLineStore(motion.path).get(1).x1 == .5


def test_detector_loads_model_bytes_for_unicode_paths(tmp_path, monkeypatch):
    from realtime.bike_check import YoloxDetector

    path = tmp_path / "认车模型.onnx"
    path.write_bytes(b"model data")
    loaded = []
    monkeypatch.setattr(cv2.dnn, "readNetFromONNX", lambda data: loaded.append(data.tobytes()) or object())
    YoloxDetector(path)
    assert loaded == [b"model data"]
