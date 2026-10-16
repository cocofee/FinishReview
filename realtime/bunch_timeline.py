"""The group's own time ruler: the strip, the chips, and every person detected.

Shared by the workbench's judging column and the large group window, so the two
cannot drift apart: one ruler, one meaning for each colour.
"""

from __future__ import annotations

import cv2
import numpy as np
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import QWidget

from .bunch_review import BunchContext, is_excluded
from .chip_history import format_chip_time
from .line_sampling import line_config_digest, load_strip


def group_strip(context: BunchContext, cancelled=lambda: False) -> tuple[np.ndarray, bool]:
    """Reuse sampled strips, retaining blank regions for actual cache gaps."""
    width, height = 1600, 90
    canvas = np.full((height, width, 3), 230, np.uint8)
    present = False
    for segment in context.segments:
        if cancelled():
            break
        path = (context.workspace / "finish_problems" / "strips" / f"camera_{context.camera:02d}"
                / (segment.path.stem + '.npz'))
        if not path.is_file():
            continue
        strip = load_strip(path)
        if (strip.meta or {}).get('segment_id') != segment.segment.segment_id:
            continue
        line = context.lines.get(segment.segment.segment_id, context.line)
        if not line or (strip.meta or {}).get("line_digest") != line_config_digest(line):
            continue
        times = strip.pts_ms + segment.start_ms
        a, b = np.searchsorted(times, (context.start_ms, context.end_ms))
        if b <= a:
            continue
        x0 = max(0, round((float(times[a]) - context.start_ms) / (context.end_ms - context.start_ms) * (width - 1)))
        x1 = min(width, round((float(times[b - 1]) - context.start_ms) / (context.end_ms - context.start_ms) * (width - 1)) + 1)
        if x1 > x0:
            canvas[:, x0:x1] = cv2.resize(strip.strip[:, a:b], (x1 - x0, height), interpolation=cv2.INTER_AREA)
            present = True
    return canvas, present


def pixmap_of(image):
    rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    return QPixmap.fromImage(QImage(rgb.data, rgb.shape[1], rgb.shape[0], rgb.strides[0], QImage.Format_RGB888).copy())


class BunchTimeline(QWidget):
    """The group's own ruler: the strip, the chips, and every person detected.

    Detection sees everyone the camera saw, not only the riders with a chip, so
    it is drawn as its own band: the operator reads the three counts against
    each other instead of trusting one of them alone.
    """

    # qint64，不是 int：录制时刻是毫秒时间戳，32 位会被悄悄截断成负数，
    # 点一下刻度就跳到几十年前的某一刻。
    seek = pyqtSignal("qint64")

    STRIP_TOP, STRIP_HEIGHT = 60, 72
    LABEL_ROWS = (12, 24, 36)       # 号码分三行错开，相邻芯片挤在一起也不重叠
    PEOPLE_TOP, PEOPLE_BOTTOM = 42, 56
    CHIP_TOP, CANDIDATE_Y = 38, 40

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(160)
        self.context, self.image, self.marks, self.current = None, None, [], None
        self.viewed = []
        self.candidates = []
        self.detected = []
        self.setToolTip('整团过线图：蓝色为画面检测到的过线（浅色表示当时画面被遮挡），红色为芯片预期时刻，'
                        '绿色为选手计数，灰色为已排除人员；点击跳到原帧')

    def bands(self):
        """刻度带跟着控件高度走。

        判读栏里那一条矮，正上方就是大画面，不需要再放一遍图片；集团大窗口
        那一条高，图片带照旧。
        """

        strip_height = max(0, min(self.STRIP_HEIGHT, self.height() - 88))
        return self.STRIP_TOP + strip_height, strip_height

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor('#edf1f5'))
        if self.context is None:
            return
        ctx = self.context
        bottom, strip_height = self.bands()
        xof = lambda t: int((t - ctx.start_ms) / (ctx.end_ms - ctx.start_ms) * (self.width() - 1))
        for entry in self.detected:
            painter.setPen(QColor('#9db8e8' if entry.get('in_obstruction') else '#1d4ed8'))
            x = xof(int(entry['recorder_ms']))
            painter.drawLine(x, self.PEOPLE_TOP, x, self.PEOPLE_BOTTOM)
        if self.image and strip_height >= 24:
            painter.drawPixmap(0, self.STRIP_TOP, self.width(), strip_height, self.image)
        for i, chip in enumerate(ctx.chips):
            x = xof(chip.recorder_ms)
            painter.setPen(QColor('#ba2525'))
            painter.drawLine(x, self.CHIP_TOP, x, bottom)
            painter.drawText(max(0, min(x, self.width() - 38)), self.LABEL_ROWS[i % len(self.LABEL_ROWS)], chip.bib)
        for mark in self.marks:
            painter.setPen(QColor('#8793a1' if is_excluded(mark) else '#18834a'))
            x = xof(mark['recorder_ms'])
            painter.drawLine(x, self.PEOPLE_BOTTOM + 1, x, bottom)
        if self.current is not None:
            painter.setPen(QPen(QColor('#b400cc'), 2))
            painter.drawLine(xof(self.current), self.CHIP_TOP - 2, xof(self.current), bottom + 1)
        for left, right in self.viewed:
            painter.fillRect(xof(left), bottom + 1, max(1, xof(right)-xof(left)), 4, QColor("#18834a"))
        for candidate in self.candidates:
            painter.setPen(QColor("#bb6b00"))
            painter.drawLine(xof(candidate["start_ms"]), self.CANDIDATE_Y, xof(candidate["end_ms"]), self.CANDIDATE_Y)
        painter.setPen(QColor('#374151'))
        painter.drawText(0, self.height() - 4, format_chip_time(ctx.start_ms))
        painter.drawText(max(0, self.width() - 100), self.height() - 4, format_chip_time(ctx.end_ms))

    def mousePressEvent(self, event):
        if self.context:
            self.seek.emit(round(self.context.start_ms + event.pos().x() / max(1, self.width() - 1)
                                 * (self.context.end_ms - self.context.start_ms)))
