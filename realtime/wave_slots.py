"""The head count above the picture: one cell an arrival, filled as people are clicked.

11-14: the operator could not tell how many people of a bunch were counted, or
which ones, without reading the table on the left; the ring of whoever was
clicked is gone as soon as the recording moves.  The cells stay: the whole
race from left to right in arrival order, in the place of 已过 N 人 · X 号
above the picture, moving along so the wave on screen is in view.
"""

from __future__ import annotations

from dataclasses import dataclass

from PyQt5.QtCore import QRect, QSize, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PyQt5.QtWidgets import QSizePolicy, QToolTip, QWidget

DONE, NEXT, TODO, EXTRA, NO_CHIP, ELSEWHERE = "done", "next", "todo", "extra", "no_chip", "elsewhere"
ROW_HEIGHT = 46
CELL_WIDTH = 46
THUMB_HEIGHT = 26
COLOURS = {  # border, fill, text
    DONE: ("#0f766e", "#d5f0ec", "#0b3b37"),
    ELSEWHERE: ("#0f766e", "#eef7f6", "#0b3b37"),
    NEXT: ("#0f766e", "#ffffff", "#0f766e"),
    TODO: ("#b6bcc6", "#ffffff", "#667085"),
    EXTRA: ("#b42318", "#fde4e1", "#b42318"),
    NO_CHIP: ("#9a5b12", "#fbefd9", "#7a4508"),
}
TIPS = {
    DONE: "已点；点这一格回到点他的那一帧",
    ELSEWHERE: "这条芯片已在别处确认，画面上没有他的点",
    NEXT: "下一位：下一个点的人记为这个号码",
    TODO: "还没点",
    EXTRA: "芯片用完后多点的人；点这一格回到点他的那一帧",
    NO_CHIP: "标为无芯片的人；点这一格回到点他的那一帧",
}


@dataclass(frozen=True)
class Slot:
    label: str
    state: str
    mark: int | None = None      # index among the wave's counted people, None: nobody clicked
    thumb_key: tuple | None = None
    current: bool = False        # of the wave on the picture


class WaveSlotRow(QWidget):
    """One row of cells above the picture; a click on a filled cell asks for that person's frame."""

    mark_clicked = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._slots = ()
        self._summary = ""
        self._thumbs = {}
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMouseTracking(True)
        self.hide()

    def sizeHint(self):
        return QSize(400, ROW_HEIGHT)

    def minimumSizeHint(self):
        return QSize(120, ROW_HEIGHT)

    def slots(self):
        return self._slots

    def summary(self):
        return self._summary

    def set_slots(self, slots, summary="", thumbs=None):
        """SLOTS in arrival order; an empty list hides the row."""
        slots = tuple(slots)
        thumbs = thumbs if thumbs is not None else {}
        shown = tuple(slot.thumb_key in thumbs for slot in slots)
        if (slots, summary, shown) == (self._slots, self._summary, getattr(self, "_shown", None)):
            return
        self._slots, self._summary, self._thumbs, self._shown = slots, summary, thumbs, shown
        self.setVisible(bool(slots))
        self.update()

    def _summary_width(self):
        if not self._summary:
            return 0
        font = QFont(self.font())
        font.setBold(True)
        font.setPointSize(13)
        return QFontMetrics(font).horizontalAdvance(self._summary) + 16

    def focus_index(self):
        """The cell kept in view: the next person of the wave on the picture, else its first cell."""
        current = [i for i, slot in enumerate(self._slots) if slot.current]
        if not current:
            return None
        return next((i for i in current if self._slots[i].state == NEXT), current[0])

    def cell_rects(self):
        """Every cell's place; those scrolled out of the row lie outside it."""
        count = len(self._slots)
        if not count:
            return []
        left = 4 + self._summary_width()
        room = max(CELL_WIDTH, self.width() - left - 4)
        focus = self.focus_index()
        if focus is None:
            focus = max((i for i, slot in enumerate(self._slots) if slot.state in (DONE, ELSEWHERE)), default=0)
        # The cell in view sits a third of the way along: the people before it stay on its left.
        offset = max(0, min(count * CELL_WIDTH - room, focus * CELL_WIDTH - room // 3))
        offset -= offset % CELL_WIDTH
        return [QRect(left + i * CELL_WIDTH - offset, 2, CELL_WIDTH - 3, self.height() - 4) for i in range(count)]

    def _cells_area(self):
        left = 4 + self._summary_width()
        return QRect(left, 0, max(0, self.width() - left), self.height())

    def slot_at(self, position):
        if not self._cells_area().contains(position):
            return None
        return next((i for i, rect in enumerate(self.cell_rects()) if rect.contains(position)), None)

    def mousePressEvent(self, event):
        index = self.slot_at(event.pos()) if event.button() == Qt.LeftButton else None
        if index is not None and self._slots[index].mark is not None:
            self.mark_clicked.emit(self._slots[index].mark)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        index = self.slot_at(event.pos())
        if index is None:
            QToolTip.hideText()
        else:
            slot = self._slots[index]
            QToolTip.showText(event.globalPos(), f"{slot.label}：{TIPS[slot.state]}", self)
        super().mouseMoveEvent(event)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        if self._summary:
            painter.setPen(QColor("#b42318" if "多" in self._summary else "#0b3b37"))
            font = QFont(self.font())
            font.setBold(True)
            font.setPointSize(13)
            painter.setFont(font)
            painter.drawText(QRect(4, 0, self._summary_width(), self.height()),
                             Qt.AlignVCenter | Qt.AlignLeft, self._summary)
        area = self._cells_area()
        painter.setClipRect(area)
        font = QFont(self.font())
        font.setBold(True)
        font.setPointSize(9)
        for slot, rect in zip(self._slots, self.cell_rects()):
            if not rect.intersects(area):
                continue
            border, fill, text = COLOURS[slot.state]
            pen = QPen(QColor(border), 2 if slot.state == NEXT else 1)
            if slot.state == TODO:
                pen.setStyle(Qt.DashLine)
            painter.setPen(pen)
            painter.setBrush(QColor(fill))
            painter.drawRoundedRect(rect, 3, 3)
            thumb = self._thumbs.get(slot.thumb_key) if slot.thumb_key is not None else None
            label_rect = rect
            if thumb is not None and rect.height() >= THUMB_HEIGHT + 14:
                scaled = thumb.scaled(rect.width() - 4, rect.height() - 15, Qt.KeepAspectRatio,
                                      Qt.SmoothTransformation)
                painter.drawPixmap(rect.x() + (rect.width() - scaled.width()) // 2, rect.y() + 2, scaled)
                label_rect = QRect(rect.x(), rect.bottom() - 13, rect.width(), 14)
            painter.setFont(font)
            painter.setPen(QColor(text))
            painter.drawText(label_rect, Qt.AlignCenter, slot.label)
            if slot.current:  # the wave on the picture
                painter.fillRect(QRect(rect.x(), self.height() - 2, rect.width() + 3, 2), QColor("#0b3b37"))
        painter.end()


def person_thumb(pixmap, x, y, height=THUMB_HEIGHT * 2):
    """A small cut of the frame around the person clicked at (X, Y), fractions of the frame.

    The click is on the front wheel or the foot at the line: the person is above it.
    """
    if pixmap is None or pixmap.isNull():
        return None
    width, full = pixmap.width(), pixmap.height()
    box = QRect(int((x - .07) * width), int((y - .30) * full), int(.14 * width), int(.36 * full))
    box = box.intersected(pixmap.rect())
    if box.width() < 4 or box.height() < 4:
        return None
    return pixmap.copy(box).scaledToHeight(height, Qt.SmoothTransformation)
