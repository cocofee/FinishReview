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


def test_a_counted_person_is_a_ring_whose_number_shows_only_under_the_mouse(qapp, monkeypatch):
    """11-16 古浪：人多时号码框挡住后面的人（507 挡住 547）。画面上只留小圈，鼠标移到圈上才显示号码。"""

    from PyQt5.QtCore import QPointF

    view = view_with_frame()
    try:
        view.set_workbench_compact(True)
        view.set_history_markers([(.5, .5, "507", True, False, False, False, False, True, False)])
        labels = []
        draw_tag = view._draw_person_tag

        def record(painter, x, y, label, *rest):
            labels.append(label)
            draw_tag(painter, x, y, label, *rest)

        monkeypatch.setattr(view, "_draw_person_tag", record)
        plain = painted(view)
        assert labels == [""]                                   # a ring, no number box
        assert plain.pixelColor(330, 240) != QColor("#606060")  # the ring is there
        assert all(plain.pixelColor(px, 240) == QColor("#606060") for px in range(345, 420))  # nothing beside it
        ring = view.mapFromScene(QPointF(320, 240))
        view._hover_position = ring
        painted(view)
        assert labels[-1] == "507"                              # the mouse on the ring: its number
        view._hover_position = ring + type(ring)(60, 0)
        painted(view)
        assert labels[-1] == ""
    finally:
        view.close()


def test_the_selected_ring_is_ringed_twice_and_others_keep_their_numbers(qapp):
    view = view_with_frame()
    try:
        view.set_workbench_compact(True)
        view.set_history_markers([(.5, .5, "507", True, False, False, False, False, True, True)])
        image = painted(view)
        assert image.pixelColor(335, 240) != QColor("#606060")  # the outer ring (15 px)
        view.set_history_markers([(.5, .5, "507", True, False, False, False, False, True, False)])
        assert painted(view).pixelColor(335, 240) == QColor("#606060")
        # Pictures that are not the arrival workbench keep their numbers as before.
        view.set_history_markers([(.5, .5, "507", True, False, False, False, False)])
        assert any(painted(view).pixelColor(px, 240) != QColor("#606060") for px in range(345, 420))
    finally:
        view.close()
