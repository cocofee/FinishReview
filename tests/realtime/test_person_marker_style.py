"""Presentation-only person tags: compact, readable and bound to the original point."""

import pytest
from PyQt5.QtCore import QRectF
from PyQt5.QtGui import QColor, QImage, QPainter, QTransform

from realtime.passage_review import PERSON_TAG_FONT_PX, EvidenceImageView
from tests.realtime.test_quick_review_panel import qapp


def painted(view, *, scale=1):
    view.setTransform(QTransform().scale(scale, scale))
    image = QImage(640, 480, QImage.Format_RGB32)
    image.fill(QColor("#606060"))
    painter = QPainter(image)
    painter.scale(scale, scale)
    view.drawForeground(painter, QRectF(0, 0, 640 / scale, 480 / scale))
    painter.end()
    return image


def view_with_frame():
    view = EvidenceImageView()
    view.resize(644, 484)
    view.set_frame(QImage(640, 480, QImage.Format_RGB32))
    return view


@pytest.mark.parametrize("scale", [.5, 1, 2])
def test_number_tag_is_screen_sized_and_kept_inside_visible_image(qapp, scale):
    view = view_with_frame()
    try:
        visible = QRectF(0, 0, 400 / scale, 300 / scale)
        occupied = []
        for x, y in ((0, 0), (visible.right(), 0), (0, visible.bottom()),
                     (visible.right(), visible.bottom()), (100 / scale, 150 / scale)):
            tag = view._person_tag_rect(x, y, "321", scale, visible, occupied)
            assert visible.contains(tag)
            assert tag.height() * scale == pytest.approx(PERSON_TAG_FONT_PX + 10)
            # 30 px bold digits (11-12): a three-digit tag is about 106 px wide on screen.
            assert 34 <= tag.width() * scale < 120
            occupied.append(tag)
    finally:
        view.close()


def test_nearby_labels_choose_different_positions(qapp):
    view = view_with_frame()
    try:
        visible = QRectF(0, 0, 640, 480)
        first = view._person_tag_rect(300, 240, "19", 1, visible, [])
        second = view._person_tag_rect(302, 240, "20", 1, visible, [first])
        assert not first.intersects(second)
    finally:
        view.close()


@pytest.mark.parametrize("dot", [False, True])
def test_workbench_markers_do_not_draw_long_lines_or_change_identity(qapp, dot):
    view = view_with_frame()
    try:
        view.set_workbench_compact(True)
        view.set_history_markers([(.5, .5, "19", True, False, dot, False, False)])
        before = view._history_markers
        image = painted(view)
        assert image.pixelColor(320, 80) == QColor("#606060")
        assert image.pixelColor(330, 240) != QColor("#606060")  # ring (radius 10) at saved point
        assert view._history_markers == before
        view.set_marker(.5, .5, "19", confirmed=True)
        image = painted(view)
        assert image.pixelColor(320, 80) == QColor("#606060")
        assert view._marker[:3] == (.5, .5, "19")
    finally:
        view.close()


def test_nearby_frame_ghost_has_no_number_label_and_ordinary_timing_line_stays(qapp):
    view = view_with_frame()
    try:
        view.set_history_markers([(.5, .5, "19", False, False, True, True, True)])
        ghost = painted(view)
        view.set_history_markers([(.5, .5, "321", False, False, True, True, True)])
        assert painted(view) == ghost  # changing a ghost's hidden number changes no pixels
        view.set_history_markers([(.5, .5, "19", True, False)])
        image = painted(view)
        assert image.pixelColor(320, 80) != QColor("#606060")  # ordinary judging, not the workbench
    finally:
        view.close()
