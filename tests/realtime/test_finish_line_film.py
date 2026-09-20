from pathlib import Path

import cv2
import numpy as np
import pytest
from PyQt5.QtCore import QPoint, Qt
from PyQt5.QtGui import QImage
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication

from realtime.decode_resources import DECODE_RESOURCES
from realtime.finish_line import FinishLine
from realtime.finish_line_film import (
    FinishLineCanvas, FinishLineEditor, FinishStrip, decode_finish_strip, sample_finish_line,
)
from realtime.recording_catalog import FilmstripSource
from realtime.video_timeline import PassageVideoLocation, RecordingSegment


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def source(path, duration=2000):
    segment = RecordingSegment("s", "camera_01", 1, str(path), 10000,
                               10000 + duration, duration, 10000, race_id="race")
    return FilmstripSource(PassageVideoLocation(segment, path, 0, 0, 0, 0, "located"),
                          10000, 10000 + duration, True)


def test_slanted_finish_line_samples_spatial_endpoints():
    frame = np.zeros((11, 21, 3), dtype=np.uint8)
    for y in range(11):
        for x in range(21):
            frame[y, x] = (x, y, x + y)
    strip = sample_finish_line(frame, FinishLine(1, 0, 0, 1, 1), height=11)
    assert np.array_equal(strip[:, 0, 0], np.arange(0, 21, 2))
    assert np.array_equal(strip[:, 0, 1], np.arange(11))


def test_continuous_decode_retains_every_frame_and_source_time(tmp_path):
    path = tmp_path / "frames.avi"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 20, (80, 60))
    for i in range(40):
        writer.write(np.full((60, 80, 3), i * 5, dtype=np.uint8))
    writer.release()
    result = decode_finish_strip(source(path), 10320, 11510, FinishLine(1, .5, 0, .5, 1))
    assert result.frame_indices == tuple(range(7, 31))
    assert result.times == tuple(10000 + i * 50 for i in range(7, 31))
    assert result.image.width() == 24
    for column, index in enumerate(result.frame_indices):
        assert abs(result.image.pixelColor(column, 10).red() - index * 5) < 5
    assert DECODE_RESOURCES.snapshot().captures == 0


class BrokenCapture:
    def __init__(self, path):
        self.position = 0
        self.released = False

    def isOpened(self):
        return True

    def get(self, prop):
        return 25 if prop == cv2.CAP_PROP_FPS else self.position

    def set(self, prop, value):
        self.position = value
        return True

    def read(self):
        return False, None

    def release(self):
        self.released = True


def test_decode_failure_releases_capture_without_fabricating_pixels():
    capture = BrokenCapture("")
    before = DECODE_RESOURCES.snapshot().captures
    with pytest.raises(ValueError, match="解码中断"):
        decode_finish_strip(source(Path("broken.avi")), 10000, 11000, FinishLine(1, .5, 0, .5, 1),
                            capture_factory=lambda path: capture)
    assert capture.released
    assert DECODE_RESOURCES.snapshot().captures == before


def test_cancelled_decode_does_not_open_media():
    def no_capture(path):
        pytest.fail("cancelled request must not open a capture")
    assert decode_finish_strip(source(Path("x")), 10000, 11000, FinishLine(1, .5, 0, .5, 1),
                               cancelled=lambda: True, capture_factory=no_capture) is None


def test_clicked_column_maps_back_to_exact_frame_and_gaps_do_nothing(qapp):
    class Owner:
        def __init__(self):
            self.requests = []
        def open_sample(self, strip, column, enlarge=False):
            self.requests.append((strip.frame_indices[column], enlarge))
    owner = Owner()
    # Canvas normally receives a QWidget controller; use an actual host here.
    from PyQt5.QtWidgets import QWidget
    host = QWidget()
    canvas = FinishLineCanvas(owner, host)
    canvas.resize(1000, 350)
    image = QImage(3, 100, QImage.Format_RGB888)
    image.fill(Qt.white)
    strip = FinishStrip(source(Path("a")), (10000., 10040., 10080.), (0, 1, 2), 10120., image)
    canvas.set_data((strip,), 10000, 10200, ((10040, "068", "芯片"),))
    QTest.mouseClick(canvas, Qt.LeftButton, pos=QPoint(250, 100))
    assert owner.requests == [(1, True)]
    QTest.mouseClick(canvas, Qt.LeftButton, pos=QPoint(900, 100))
    assert owner.requests == [(1, True)]
    canvas.close()


def test_line_editor_uses_image_rect_and_requires_two_points(qapp):
    image = QImage(800, 400, QImage.Format_RGB888)
    image.fill(Qt.white)
    dialog = FinishLineEditor(image, 1)
    dialog.show()
    qapp.processEvents()
    rect = dialog.editor.image_rect()
    for x, y in ((.1, .3), (.9, .7)):
        QTest.mouseClick(dialog.editor, Qt.LeftButton,
                         pos=QPoint(round(rect.x() + x * rect.width()), round(rect.y() + y * rect.height())))
    dialog.accept()
    assert dialog.line.x1 == pytest.approx(.1, abs=.003)
    assert dialog.line.y2 == pytest.approx(.7, abs=.003)


def test_finish_mode_does_not_change_full_frame_sampling_or_manual_state(qapp, tmp_path):
    from realtime.race_filmstrip import RaceFilmstripPanel
    panel = RaceFilmstripPanel()
    panel.set_check_context(tmp_path / "checks.jsonl", "race", 1)
    panel.finish_panel.store.set(FinishLine(1, .1, .4, .9, .8))
    panel.finish_panel.line = panel.finish_panel.store.get(1)
    panel.set_sources((source(tmp_path / "missing"),))
    panel.finish_line_button.click()
    assert panel.interval_ms == 500
    assert not panel._judgments and not panel._pending
    panel.clear()
    assert panel.finish_panel.line is None
    assert not panel.finish_panel._strips
    assert not panel.finish_line_button.isChecked()
    panel.stop()


def test_finish_focus_keeps_full_frame_canvas_and_does_not_start_slit_worker(qapp, tmp_path, monkeypatch):
    from realtime.race_filmstrip import RaceFilmstripPanel
    panel = RaceFilmstripPanel()
    panel.resize(1200, 450)
    panel.show()
    panel.set_check_context(tmp_path / "race1" / "checks.jsonl", "race1")
    panel.finish_panel.line = FinishLine(1, .1, .4, .9, .8)
    panel.set_sources((source(tmp_path / "one"),))
    panel.finish_line_button.click()
    assert panel.finish_panel._worker is None
    assert panel.canvas.isVisible()
    assert panel.finish_panel.isVisible()
    assert panel.finish_panel.canvas.isHidden()
    assert panel.interval_ms == 500
    panel.clear()
    panel.stop()


def test_finish_review_progress_excludes_gaps_and_does_not_confirm_athletes(qapp, tmp_path):
    from realtime.race_filmstrip import RaceFilmstripPanel
    panel = RaceFilmstripPanel()
    panel.set_check_context(tmp_path / "checks.jsonl", "race")
    first = source(tmp_path / "first")
    panel.set_sources((first,))
    image = QImage(2, 100, QImage.Format_RGB888)
    image.fill(Qt.white)
    finish = panel.finish_panel
    finish.start_ms, finish.end_ms = 10000, 12000
    finish._strips = (FinishStrip(first, (10000., 10050.), (0, 1), 10100., image),
                      FinishStrip(first, (10500., 10550.), (10, 11), 10600., image))
    finish.check_and_next()
    assert panel._check_store.ranges == ((10000, 10100), (10500, 10600))
    assert not panel._judgments
    panel.stop()
