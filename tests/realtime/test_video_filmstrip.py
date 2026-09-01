import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtGui import QImage
from PyQt5.QtCore import QPoint, Qt
from PyQt5.QtTest import QSignalSpy, QTest
from PyQt5.QtWidgets import QApplication

from realtime.video_filmstrip import (
    FilmstripFrame,
    VideoFilmstripWidget,
    filmstrip_positions,
)


def test_filmstrip_positions_include_end_without_duplicates():
    assert filmstrip_positions(100, 1_000, interval_ms=250) == (
        100,
        350,
        600,
        850,
        1_000,
    )


def test_filmstrip_positions_clamp_invalid_interval_and_range():
    assert filmstrip_positions(1_000, 100, interval_ms=0) == (1_000,)


def test_filmstrip_positions_keep_base_sampling_with_anchors():
    assert filmstrip_positions(0, 3_000, interval_ms=1_000, anchors=(1_500,)) == (
        0,
        1_000,
        1_500,
        2_000,
        3_000,
    )


def test_filmstrip_reverses_visual_order_without_changing_positions():
    app = QApplication.instance() or QApplication([])
    widget = VideoFilmstripWidget()
    widget._video_path = Path("race.mp4")
    image = QImage(8, 8, QImage.Format_RGB888)
    widget._on_frame_ready(image, 100, 1)
    widget._on_frame_ready(image, 200, 2)

    def positions():
        return [frame.position_ms for frame in widget.content._visual_frames()]

    assert positions() == [100, 200]
    widget.direction_combo.setCurrentIndex(1)
    assert positions() == [200, 100]
    widget.close()


def test_filmstrip_append_positions_deduplicates_existing_positions():
    app = QApplication.instance() or QApplication([])
    widget = VideoFilmstripWidget()
    widget._video_path = Path("race.mp4")
    widget._requested_positions.add(100)
    started: list[tuple[int, ...]] = []
    widget._start_worker = lambda positions: started.append(tuple(positions))

    widget.append_positions(Path("race.mp4"), [100, 200, 200])

    assert started == [(200,)]
    assert widget._requested_positions == {100, 200}
    widget.close()


def test_filmstrip_load_reuses_cached_frames_for_same_path():
    app = QApplication.instance() or QApplication([])
    widget = VideoFilmstripWidget()
    path = Path("race.mp4")
    image = QImage(8, 8, QImage.Format_RGB888)
    cached = widget._frames_by_path.setdefault(path, {})
    cached[1_000] = FilmstripFrame(1_000, 25, image)
    widget._video_path = path
    started: list[tuple[int, ...]] = []
    widget._start_worker = lambda positions: started.append(tuple(positions))

    widget.load(path, 0, 2_000, positions_ms=(1_000,))

    assert started == [(0, 2_000)]
    assert [frame.position_ms for frame in widget._frames] == [1_000]
    widget.close()


def test_canvas_click_and_drag_emit_one_final_position():
    app = QApplication.instance() or QApplication([])
    widget = VideoFilmstripWidget()
    widget._video_path = Path("race.mp4")
    image = QImage(8, 8, QImage.Format_RGB888)
    widget._on_frame_ready(image, 0, 0)
    widget._on_frame_ready(image, 2_000, 50)
    widget.resize(500, 320)
    widget.show()
    app.processEvents()

    spy = QSignalSpy(widget.position_selected)
    QTest.mouseClick(widget.content, Qt.LeftButton, pos=QPoint(380, 20))
    app.processEvents()
    assert len(spy) == 1
    assert spy[-1][0] == 2_000

    spy = QSignalSpy(widget.position_selected)
    QTest.mousePress(widget.content, Qt.LeftButton, pos=QPoint(380, 20))
    QTest.mouseMove(widget.content, QPoint(300, 20), 50)
    QTest.mouseMove(widget.content, QPoint(220, 20), 50)
    QTest.mouseRelease(widget.content, Qt.LeftButton, pos=QPoint(220, 20))
    app.processEvents()
    assert len(spy) == 1
    widget.close()


def test_canvas_double_click_emits_preview_position():
    app = QApplication.instance() or QApplication([])
    widget = VideoFilmstripWidget()
    widget._video_path = Path("race.mp4")
    image = QImage(8, 8, QImage.Format_RGB888)
    widget._on_frame_ready(image, 0, 0)
    widget._on_frame_ready(image, 2_000, 50)
    widget.resize(500, 320)
    widget.show()
    app.processEvents()

    spy = QSignalSpy(widget.position_selected)
    QTest.mouseDClick(widget.content, Qt.LeftButton, pos=QPoint(380, 20))
    app.processEvents()
    assert len(spy) >= 1
    assert spy[-1][0] == 2_000
    widget.close()


def test_filmstrip_load_prioritizes_current_area_and_queues_remaining_positions():
    app = QApplication.instance() or QApplication([])
    widget = VideoFilmstripWidget()
    path = Path("race.mp4")
    widget._video_path = path
    widget.set_current_position(10_000)
    started: list[tuple[int, ...]] = []
    widget._start_worker = lambda positions: started.append(tuple(positions))

    widget.load(path, 0, 30_000)

    assert started == [(10_000, 8_000, 12_000, 6_000, 14_000, 4_000, 16_000, 2_000, 18_000, 0, 20_000, 22_000)]
    assert len(widget._pending_positions) == 4
    widget.close()


def test_filmstrip_releases_old_path_cache_when_switching_recording():
    app = QApplication.instance() or QApplication([])
    widget = VideoFilmstripWidget()
    first = Path("first.mkv")
    second = Path("second.mkv")
    image = QImage(8, 8, QImage.Format_RGB888)
    widget._video_path = first
    widget._on_frame_ready(image, 100, 1)

    started: list[tuple[int, ...]] = []
    widget._start_worker = lambda positions: started.append(tuple(positions))
    widget.load(second, 0, 0)

    assert first not in widget._frames_by_path
    assert widget._video_path == second
    widget.close()
