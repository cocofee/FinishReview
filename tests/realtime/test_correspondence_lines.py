"""Correspondence is visible without counting or changing the selected row."""

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QImage, QPainter

from realtime.passage_review import EvidenceImageView
from realtime.race_strip import AUTO_LABELLED, RaceStripView
from realtime.review_marker_style import AUTO_COLOR, LEGEND, MANUAL_COLOR, PENDING_COLOR, marker_style
from tests.realtime.test_quick_review_panel import (
    FRAME, T0, auto_people, count_on_picture, qapp, row, wave_panel,
)
from tests.realtime.test_race_strip import _Strips, piece


def test_status_styles_are_shared_and_have_non_colour_encoding():
    assert marker_style(automatic=True, manual=True) == (AUTO_COLOR, True)
    assert marker_style(manual=True) == (MANUAL_COLOR, False)
    assert marker_style() == (PENDING_COLOR, True)
    assert all(text in LEGEND for text in ("自动对应", "非人工确认", "人工处理", "待核", "播放位置"))


def test_auto_lines_exist_without_selecting_or_saving_each_person(wave_panel, monkeypatch):
    people = auto_people(wave_panel, monkeypatch, (100, 500, 1000))
    selected = wave_panel._selected_key
    before = wave_panel._arrival_store.active(wave_panel._arrival_scope)
    for bib, person in zip(("61", "20", "76"), people):
        overlays = wave_panel.picture_overlays(person.frame)
        assert [(entry[2], entry[5], entry[7]) for entry in overlays if not entry[6]] == [(bib, False, True)]
        chip = wave_panel._arrival_rows[row(wave_panel, bib)].event
        assert wave_panel.race_strip.view.line_positions[(int(chip.timeline_timestamp_ms), bib)] == person.chip_ms
    assert wave_panel._selected_key == selected
    assert wave_panel._arrival_store.active(wave_panel._arrival_scope) == before


def test_selected_row_highlights_only_its_strip_record_without_new_decisions(wave_panel, monkeypatch):
    auto_people(wave_panel, monkeypatch, (100, 500, 1000))
    wave_panel._arrival_clicked(row(wave_panel, "20"))
    chip = wave_panel._selected_arrival().event
    assert wave_panel.race_strip.view.selected_chip == (int(chip.timeline_timestamp_ms), "20")
    assert not wave_panel._arrival_store.active(wave_panel._arrival_scope)
    wave_panel._arrival_clicked(row(wave_panel, "76"))
    assert wave_panel.race_strip.view.selected_chip[1] == "76"


def test_table_uses_one_warning_colour_instead_of_red_and_teal(wave_panel):
    for index, entry in enumerate(wave_panel._arrival_rows):
        if wave_panel._row_state(entry) == "待定":
            assert wave_panel.table.item(index, 3).background().color().name() == "#ffedd5"


def test_saved_person_line_survives_selection_of_a_different_wave(wave_panel):
    wave_panel._arrival_clicked(row(wave_panel, "61"))
    count_on_picture(wave_panel, .2, 100)
    evidence = dict(wave_panel.frame)
    before = wave_panel._arrival_store.active(wave_panel._arrival_scope)
    chip_row = wave_panel._arrival_rows[row(wave_panel, "61")]
    assert wave_panel.race_strip.view.line_positions[(int(chip_row.event.timeline_timestamp_ms), "61")] == chip_row.time_ms
    wave_panel._arrival_clicked(row(wave_panel, "5"))
    overlays = wave_panel.picture_overlays(evidence)
    assert any(entry[2] == "61" and entry[3] and not entry[5] for entry in overlays)
    assert wave_panel._arrival_store.active(wave_panel._arrival_scope) == before
    assert not wave_panel.picture_overlays(dict(evidence, camera_index=2))


def test_saved_auto_binding_is_one_line_and_keeps_automatic_source(wave_panel, monkeypatch):
    people = auto_people(wave_panel, monkeypatch, (100, 500, 1000))
    wave_panel._arrival_clicked(row(wave_panel, "61"))
    wave_panel._enter_key()
    wave_panel._arrival_clicked(row(wave_panel, "61"))
    overlays = wave_panel.picture_overlays(people[0].frame)
    assert len([entry for entry in overlays if entry[2] == "61" and not entry[6]]) == 1
    assert all(not entry[3] and not entry[5] and entry[7] for entry in overlays)


def test_strip_line_uses_bound_video_time_without_rewriting_chip_time(qapp, tmp_path):
    import numpy as np

    view = RaceStripView()
    view.resize(800, view.height())
    view.cache = _Strips(np.array([T0, T0 + 10_000], dtype=np.float64), np.full((100, 2, 3), 220, dtype=np.uint8))
    view.set_pieces([piece(tmp_path, T0, T0 + 10_000)])
    view.set_chips([(T0 + 1000, "2", AUTO_LABELLED)])
    view.set_line_positions({(T0 + 1000, "2"): T0 + 1200})
    view.set_view(T0)
    view.set_cursor(T0 + 1200)  # playback must not repaint a bound line purple
    image = view.grab().toImage()
    x = view.x_of(T0 + 1200)
    colors = {image.pixelColor(x, y).name() for y in range(view.STRIP_TOP + 5, view.STRIP_TOP + 90)}
    assert AUTO_COLOR in colors  # full-height dashed line, not a six-pixel tick
    assert view.chips == [(T0 + 1000, "2", AUTO_LABELLED)]
    view.close()


def test_video_numbered_line_replaces_ring(qapp):
    view = EvidenceImageView()
    view._source_width, view._source_height = 400, 300
    view._count_mode = True
    view.set_history_markers(((.5, .7, "2", False, False, False, False, True),))
    image = QImage(400, 300, QImage.Format_RGB32)
    image.fill(QColor("#dddddd"))
    painter = QPainter(image)
    view.drawForeground(painter, view.sceneRect())
    painter.end()
    # Medium-length line above the position, with no stroke across empty road.
    assert any(image.pixelColor(200, y).name() == AUTO_COLOR for y in range(80, 200))
    assert image.pixelColor(197, 180).name() == "#ffffff"  # white rim separates green/blue from video
    assert image.pixelColor(200, 270).name() == "#dddddd"
    assert image.pixelColor(200, 30).name() == "#dddddd"
    assert view._history_markers[0][5] is False
    view.close()
