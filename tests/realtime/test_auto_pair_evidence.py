"""Automatic labels, opening and decoded evidence must share one binding."""

from dataclasses import replace

import cv2
import numpy as np
from PyQt5.QtTest import QSignalSpy

from realtime import quick_review_panel as module
from tests.realtime.test_quick_review_panel import (
    FRAME, T0, auto_people, qapp, row, wave_panel,
)


def test_auto_wave_opens_bound_frame_even_before_strip_is_ready(wave_panel, monkeypatch, tmp_path):
    """Do not search the strip again or rely on padding to find the labelled person."""
    video = tmp_path / "evidence.avi"
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"MJPG"), 20, (160, 120))
    assert writer.isOpened()
    for index in range(40):
        image = np.zeros((120, 160, 3), np.uint8)
        image[:, :, 1] = index * 5
        writer.write(image)
    writer.release()
    people = auto_people(wave_panel, monkeypatch, (100, 500, 1000))
    people = [replace(person, frame=dict(person.frame, video_path=str(video))) for person in people]
    monkeypatch.setattr(module, "people_on_line", lambda *_args: people)
    wave_panel._render()
    assert not wave_panel.race_strip.view.pieces
    monkeypatch.setattr(module, "first_crossing", lambda *_args: (_ for _ in ()).throw(AssertionError("second search")))
    jumps = []
    decoded = []

    def display(frame):
        capture = cv2.VideoCapture(frame["video_path"])
        try:
            capture.set(cv2.CAP_PROP_POS_FRAMES, frame["frame_index"])
            ok, image = capture.read()
            assert ok
            decoded.append((int(capture.get(cv2.CAP_PROP_POS_FRAMES)) - 1, image))
        finally:
            capture.release()
        jumps.append(frame)
        wave_panel.record_viewed_frame(frame, frame["recorder_ms"])

    wave_panel.jump_requested.connect(display)
    wave_panel._shown_ms = None
    wave_panel._arrival_clicked(row(wave_panel, "20"))
    assert jumps[-1] == dict(people[0].frame, reference_only=True, auto_pair=True)
    assert decoded[-1][0] == people[0].frame["frame_index"]
    assert abs(float(decoded[-1][1][:, :, 1].mean()) - decoded[-1][0] * 5) < 4
    drawn = wave_panel.picture_overlays(jumps[-1])
    assert [(label, auto, ghost) for _x, _y, label, _ok, _alert, _dot, ghost, auto in drawn] == [
        ("61", True, False)]
    count = len(jumps)
    wave_panel._arrival_clicked(row(wave_panel, "76"))
    assert len(jumps) == count  # same wave never jumps on member selection
    assert wave_panel.open_wave_start()
    assert jumps[-1]["frame_index"] == people[0].frame["frame_index"]


def test_auto_row_click_decodes_same_person_in_window_archive(qapp, tmp_path, monkeypatch):
    from realtime.auto_label import DetectedPerson
    from tests.realtime.test_review_window import _event, _wait_until, _window

    window = _window(tmp_path, workbench=True)
    try:
        start = 1_789_600_000_000
        stem = "camera_01_20261029_153844_113960"
        paths = [tmp_path / "videos" / f"{stem}_archive_0002.avi",
                 tmp_path / "review_buffer" / "camera_01" / f"{stem}_review_0000000000000007.avi"]
        for path, indexes in zip(paths, (range(180), range(100, 150))):
            path.parent.mkdir(parents=True, exist_ok=True)
            writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 20, (160, 120))
            assert writer.isOpened()
            for index in indexes:
                image = np.zeros((120, 160, 3), np.uint8)
                cv2.rectangle(image, (index * 3 % 150, 40), (index * 3 % 150 + 10, 80), (255, 255, 255), -1)
                writer.write(image)
            writer.release()
        archive = window.timeline_store.add_completed_segment(
            source_id="camera_01_review", camera_index=1, video_path=paths[0], media_started_at_ms=start,
            media_duration_ms=9000, clock_source="videopipe_system_clock", timing_error_ms=0,
            end_reason="continuous_archive_fallback", race_id="race-1")
        piece = window.timeline_store.add_completed_segment(
            source_id="camera_01_review", camera_index=1, video_path=paths[1], media_started_at_ms=start + 4850,
            media_duration_ms=2500, clock_source="videopipe_system_clock", timing_error_ms=0,
            end_reason="retained_problem_segment", race_id="race-1")
        moment = start + 5350
        event = _event(passage_time_ms=moment, passage_timestamp_ms=moment)
        window.passage_store.append(event)
        person = DetectedPerson("person-4", 1, moment, moment, .5, .5, False, True,
                                dict(camera_index=1, segment_id=piece.segment_id, video_path=str(paths[1]),
                                     frame_index=10, position_ms=500, recorder_ms=moment))
        monkeypatch.setattr(module, "people_on_line", lambda *_args: [person])
        panel = window.problem_panel
        panel.set_events_provider(lambda: [event])
        panel.set_scope(event.race_id, event.stage_id)
        panel._quick_render_signature = None
        panel._render()
        panel._shown_ms = None
        panel._arrival_clicked(0)
        pane = window._camera_one_pane()
        try:
            _wait_until(qapp, lambda: pane.location is not None and pane.location.segment.segment_id == archive.segment_id
                        and pane._current_frame_index == 110)
        except AssertionError:
            raise AssertionError((panel.workspace_dir, panel._auto_labels, panel.hint.text(),
                                  pane.location, pane._current_frame_index)) from None
        assert pane._current_position_ms == 5500
        assert pane.location.segment.media_started_at_ms == start - 150
        shown = dict(camera_index=1, segment_id=archive.segment_id, video_path=str(paths[0]),
                     frame_index=110, position_ms=5500, recorder_ms=moment)
        overlays = panel.picture_overlays(shown)
        assert any(label == event.bib and auto and not ghost
                   for _x, _y, label, _ok, _alert, _dot, ghost, auto in overlays)
        assert not panel._arrival_store.active((event.race_id, event.stage_id))
        assert "未校时/匹配" not in panel.hint.text()
    finally:
        window.close()


def test_auto_landing_ignores_an_unrelated_earlier_strip_change(wave_panel, monkeypatch):
    people = auto_people(wave_panel, monkeypatch, (100, 500, 1000))
    wave_panel._shown_ms = T0 + 10_000  # leaving the next wave
    target = wave_panel._landing(wave_panel._arrival_rows[row(wave_panel, "76")])
    assert target == dict(people[0].frame, reference_only=True, auto_pair=True)
    assert target["recorder_ms"] == T0 + 100


def test_missing_target_laps_warns_without_hiding_reads_or_guessing(wave_panel):
    wave_panel.events = [replace(event, lap=2) for event in wave_panel.events]
    wave_panel._quick_render_signature = None
    wave_panel._render()
    assert wave_panel.lap_status.text() == "圈数未知 · 显示全部圈次"
    assert "未收到" in wave_panel.lap_status.toolTip()
    assert len(wave_panel._arrival_rows) == 4
    assert wave_panel._target_laps == {}


def test_single_chip_without_padding_still_needs_its_moment_for_manual_review(wave_panel):
    lone = wave_panel._row_wave(wave_panel._arrival_rows[row(wave_panel, "5")])
    wave_panel._arrival_drafts["seen"] = {}
    assert wave_panel._span_unseen(lone)
    moment = int(lone[0].event.timeline_timestamp_ms)
    wave_panel._arrival_drafts["seen"] = {"1": [[moment - 50, moment + 50]]}
    assert wave_panel._span_unseen(lone) == []


def test_accept_does_not_claim_manual_viewing_or_enter_exports(wave_panel, monkeypatch):
    people = auto_people(wave_panel, monkeypatch, (100, 500, 1000))
    wave_panel._arrival_clicked(row(wave_panel, "61"))
    wave_panel._arrival_drafts["seen"] = {}
    spy = QSignalSpy(wave_panel.jump_requested)
    wave_panel._enter_key()
    decisions = [entry.decision for entry in wave_panel._arrival_rows if entry.decision]
    assert len(decisions) == 3
    assert all(entry["review_method"] == "auto_label_operator_accepted" for entry in decisions)
    assert wave_panel._arrival_drafts["seen"] == {}
    assert wave_panel._arrival_exports() == ([], [], 0)
    assert not any(entry[0] == dict(FRAME) for entry in spy)
    wave_panel._arrival_clicked(row(wave_panel, "61"))
    drawn = wave_panel.picture_overlays(people[0].frame)
    assert any(label == "61" and auto and not confirmed for _, _, label, confirmed, _, _, _, auto in drawn)
    assert wave_panel.race_strip.view.chips[0][2] == 2
