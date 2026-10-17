from PyQt5.QtCore import QPointF, Qt
from PyQt5.QtGui import QImage
from PyQt5.QtWidgets import QApplication

from realtime.passage_review import EvidenceImageView


def test_finish_line_is_painted_at_source_coordinates_after_zoom_and_hd():
    app = QApplication.instance() or QApplication([])
    view = EvidenceImageView()
    view.resize(800, 600)
    view.show()
    preview = QImage(320, 240, QImage.Format_RGB32)
    preview.fill(Qt.black)
    view.set_frame(preview, source_width=640, source_height=480)
    view.set_finish_line(((.5, .1), (.5, .9)))
    app.processEvents()

    def assert_green_line():
        center = view.mapFromScene(QPointF(320, 240))
        rendered = view.viewport().grab().toImage()
        color = rendered.pixelColor(center)
        assert color.green() > 180 and color.red() < 30

    assert_green_line()
    view.zoom_by(1.5)
    assert_green_line()
    original = QImage(640, 480, QImage.Format_RGB32)
    original.fill(Qt.black)
    view.set_frame(original, source_width=640, source_height=480)
    view.resize(1000, 700)
    view.fit_to_window()
    assert_green_line()
    view.set_finish_line()
    center = view.mapFromScene(QPointF(320, 240))
    assert view.viewport().grab().toImage().pixelColor(center).green() == 0
    view.close()
