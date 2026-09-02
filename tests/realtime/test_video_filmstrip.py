import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtGui import QImage
from PyQt5.QtCore import QObject, QPoint, Qt, pyqtSignal
from PyQt5.QtTest import QSignalSpy, QTest
from PyQt5.QtWidgets import QApplication

from realtime.video_filmstrip import (
    FilmstripFrame,
    FILMSTRIP_TILE_GAP,
    FILMSTRIP_TILE_WIDTH,
    VideoFilmstripWidget,
    filmstrip_positions,
)


class _SlowFilmstripWorker(QObject):
    frame_ready = pyqtSignal(QImage, int, int)
    failed = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.stop_requested = False
        self.wait_calls = 0

    def request_stop(self):
        self.stop_requested = True

    def isRunning(self):
        return True

    def wait(self, _timeout):
        self.wait_calls += 1
        return False


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
    QTest.qWait(QApplication.doubleClickInterval() + 140)
    app.processEvents()
    assert len(spy) == 1
    assert spy[-1][0] == 2_000
    spy = QSignalSpy(widget.position_selected)
    QTest.qWait(500)
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
    double_spy = QSignalSpy(widget.position_double_clicked)
    QTest.mouseDClick(widget.content, Qt.LeftButton, pos=QPoint(380, 20))
    QTest.qWait(QApplication.doubleClickInterval() + 80)
    app.processEvents()
    assert len(spy) == 1
    assert spy[-1][0] == 2_000
    assert len(double_spy) == 1
    assert double_spy[-1][0] == 2_000
    widget.close()


def test_canvas_hit_testing_uses_content_coordinates_after_scroll():
    app = QApplication.instance() or QApplication([])
    widget = VideoFilmstripWidget()
    widget._video_path = Path("race.mp4")
    image = QImage(8, 8, QImage.Format_RGB888)
    for index in range(8):
        widget._on_frame_ready(image, index * 2_000, index * 50)
    widget.resize(500, 320)
    widget.show()
    app.processEvents()
    widget.content.refresh_geometry()
    app.processEvents()

    step = FILMSTRIP_TILE_WIDTH + FILMSTRIP_TILE_GAP
    scroll = widget.scroll.horizontalScrollBar()
    scroll.setValue(2 * step)
    content_x = scroll.value() + 180

    assert widget.content._frame_at_x(content_x).position_ms == 4_000
    widget.close()


def test_canvas_scrub_position_interpolates_between_thumbnail_times():
    app = QApplication.instance() or QApplication([])
    widget = VideoFilmstripWidget()
    widget._video_path = Path("race.mp4")
    image = QImage(8, 8, QImage.Format_RGB888)
    widget._on_frame_ready(image, 0, 0)
    widget._on_frame_ready(image, 2_000, 50)

    halfway_x = 4 + (FILMSTRIP_TILE_WIDTH + FILMSTRIP_TILE_GAP) / 2
    assert widget.content._position_at_x(halfway_x) == 1_000
    widget.close()


def test_canvas_marker_mode_emits_frame_and_normalized_position():
    app = QApplication.instance() or QApplication([])
    widget = VideoFilmstripWidget()
    widget._video_path = Path("race.mp4")
    image = QImage(8, 8, QImage.Format_RGB888)
    widget._on_frame_ready(image, 0, 0)
    widget._on_frame_ready(image, 2_000, 50)
    widget.resize(700, 320)
    widget.show()
    app.processEvents()

    spy = QSignalSpy(widget.marker_position_selected)
    widget.mark_button.click()
    QTest.mouseClick(widget.content, Qt.LeftButton, pos=QPoint(500, 100))
    app.processEvents()

    assert len(spy) == 1
    assert spy[-1][0] == 2_000
    assert 0.0 <= spy[-1][1] <= 1.0
    assert 0.0 <= spy[-1][2] <= 1.0
    assert spy[-1][3] == 50
    assert widget.confirm_button.isEnabled()
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

    assert started == [(10_000, 8_000, 12_000, 6_000, 14_000)]
    assert len(widget._pending_positions) == 11
    widget.close()


def test_filmstrip_labels_are_relative_to_current_position():
    app = QApplication.instance() or QApplication([])
    widget = VideoFilmstripWidget()
    widget._display_start_ms = 0
    widget._display_end_ms = 10_000
    widget._display_reference_ms = 10_000
    image = QImage(8, 8, QImage.Format_RGB888)
    frame = FilmstripFrame(8_000, 0, image)
    assert widget.content._display_time_text(frame) == "2.000s"
    frame = FilmstripFrame(10_000, 0, image)
    assert widget.content._display_time_text(frame) == "0.000s"
    frame = FilmstripFrame(12_000, 0, image)
    assert widget.content._display_time_text(frame) == "+2.000s"
    widget.set_display_origin(1_756_600_000_000)
    assert widget.content._display_time_text(frame) == "08:26:52.000"
    widget.close()


def test_filmstrip_centers_current_frame_in_viewport():
    app = QApplication.instance() or QApplication([])
    widget = VideoFilmstripWidget()
    widget._video_path = Path("race.mp4")
    image = QImage(8, 8, QImage.Format_RGB888)
    for position_ms in (0, 2_000, 4_000):
        widget._on_frame_ready(image, position_ms, position_ms // 40)
    widget.resize(500, 320)
    widget.show()
    app.processEvents()

    widget.set_current_position(2_000)
    widget._flush_render_pending()

    bar = widget.scroll.horizontalScrollBar()
    assert bar.value() > 0
    step = FILMSTRIP_TILE_WIDTH + FILMSTRIP_TILE_GAP
    target_center = 4 + step + FILMSTRIP_TILE_WIDTH / 2.0
    expected = max(
        0,
        int(round(target_center - widget.scroll.viewport().width() / 2.0)),
    )
    assert bar.value() == min(bar.maximum(), expected)
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


def test_filmstrip_stop_never_waits_for_slow_decoder_on_gui_thread():
    app = QApplication.instance() or QApplication([])
    widget = VideoFilmstripWidget()
    worker = _SlowFilmstripWorker()
    widget._worker = worker

    widget.stop()

    assert worker.stop_requested
    assert worker.wait_calls == 0
    assert worker in widget._retired_workers
    worker.finished.emit()
    app.processEvents()
    assert worker not in widget._retired_workers
    widget.close()
