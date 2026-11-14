from PyQt5.QtCore import QPoint, Qt
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication
import pytest

from realtime.wave_slots import DONE, NEXT, TODO, Slot, WaveSlotRow


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


def test_the_row_follows_the_wave_on_screen_and_only_filled_cells_answer_a_click(qapp):
    row = WaveSlotRow()
    row.resize(1400, row.sizeHint().height())
    assert row.isHidden()
    # 200 arrivals: 120 counted, the wave on the picture is 120..134 with two of it clicked.
    slots = [Slot(str(100 + i), DONE if i < 122 else NEXT if i == 122 else TODO,
                  i if 120 <= i < 122 else None, 120 <= i < 135) for i in range(200)]
    row.set_slots(slots, "已过 122 人　本波 2/15")
    row.show()
    rects = row.cell_rects()
    assert len(rects) == 200 and row.focus_index() == 122
    area = row._cells_area()
    visible = [i for i, rect in enumerate(rects) if area.contains(rect)]
    # The next person is in view with those just before on the left and the rest of the wave on the right.
    assert 122 in visible and 120 in visible and 126 in visible and 0 not in visible
    assert visible == list(range(visible[0], visible[-1] + 1))
    clicked = []
    row.mark_clicked.connect(clicked.append)
    QTest.mouseClick(row, Qt.LeftButton, pos=rects[121].center())
    QTest.mouseClick(row, Qt.LeftButton, pos=rects[125].center())   # nobody clicked for this chip yet
    QTest.mouseClick(row, Qt.LeftButton, pos=rects[119].center())   # another wave: not this picture's
    QTest.mouseClick(row, Qt.LeftButton, pos=QPoint(2, 2))          # the count in front
    assert clicked == [121]
    row.grab()  # paints without raising
    row.set_slots([])
    assert row.isHidden()


def test_the_cell_of_the_person_the_operator_is_on_is_painted_apart(qapp):
    from PyQt5.QtGui import QColor

    row = WaveSlotRow()
    row.resize(900, row.sizeHint().height())
    row.set_slots([Slot("50", DONE, 0, True), Slot("519", NEXT, None, True, True), Slot("7", TODO, None, True)])
    row.show()
    image = row.grab().toImage()
    rects = row.cell_rects()
    corner = lambda rect: QColor(image.pixel(rect.left() + 5, rect.top() + 5)).name()  # noqa: E731
    assert corner(rects[1]) == "#ffd84d" and corner(rects[0]) != "#ffd84d" and corner(rects[2]) != "#ffd84d"

