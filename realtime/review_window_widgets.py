"""Small widgets of the finish review window: passage signal bridge, elided label, logo and status indicator."""

from __future__ import annotations

import re

from PyQt5.QtCore import QObject, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QPainter, QPen
from PyQt5.QtWidgets import QFrame, QHBoxLayout, QLabel, QWidget


class _PassageSignalBridge(QObject):
    accepted = pyqtSignal(object)
    metadata_accepted = pyqtSignal(object)
    focus_accepted = pyqtSignal(object)
    timing_status = pyqtSignal(object)
    capture_refresh_finished = pyqtSignal(object)


class _ElidedLabel(QLabel):
    """Keep the full value available while eliding long display text."""

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self._full_text = ""
        self.setText(text)

    def text(self) -> str:
        return self._full_text

    def setText(self, value: str) -> None:
        self._full_text = str(value)
        self._refresh_display_text()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._refresh_display_text()

    def _refresh_display_text(self) -> None:
        width = max(0, self.contentsRect().width())
        rendered = self.fontMetrics().elidedText(
            self._full_text,
            Qt.ElideRight,
            width,
        )
        QLabel.setText(self, rendered)


class _FinishReviewLogo(QWidget):
    """Small scalable brand mark that does not require packaged image assets."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(34, 34)
        self.setToolTip("FinishReview 终点多源复核")

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.scale(self.width() / 64.0, self.height() / 64.0)

        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#14232c"))
        painter.drawRoundedRect(2, 2, 60, 60, 12, 12)
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(QColor("#31505a"), 1.5))
        painter.drawRoundedRect(3, 3, 58, 58, 11, 11)

        painter.setPen(QPen(QColor("#79a7ad"), 3, Qt.SolidLine, Qt.RoundCap))
        painter.drawLine(10, 24, 23, 24)
        painter.setPen(QPen(QColor("#268e73"), 4, Qt.SolidLine, Qt.RoundCap))
        painter.drawLine(10, 34, 28, 34)
        painter.setPen(QPen(QColor("#34bd83"), 7, Qt.SolidLine, Qt.RoundCap))
        painter.drawLine(10, 45, 18, 45)
        painter.drawLine(18, 45, 34, 32)
        painter.drawLine(34, 32, 41, 32)

        painter.setPen(QPen(QColor("#14232c"), 3))
        painter.setBrush(QColor("#ffd15c"))
        painter.drawEllipse(33, 26, 12, 12)

        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#edf4f4"))
        painter.drawRoundedRect(47, 9, 9, 46, 2, 2)
        painter.setBrush(QColor("#16866d"))
        for rectangle in (
            (47, 9, 5, 8),
            (52, 17, 4, 7),
            (47, 24, 5, 8),
            (52, 32, 4, 8),
            (47, 40, 5, 7),
            (52, 47, 4, 8),
        ):
            painter.drawRect(*rectangle)


class _CompactStatusIndicator(QFrame):
    """Compact one-line runtime status with details kept in the tooltip."""

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self._base_title = title
        self._raw_text = ""
        self._status_style = ""
        self._surface_style = ""
        self._state = "waiting"
        self.setMinimumWidth(150)
        self.setMaximumWidth(220)
        self.setMinimumHeight(30)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 0, 10, 0)
        layout.setSpacing(6)
        self._dot = QLabel(self)
        self._dot.setFixedSize(8, 8)
        layout.addWidget(self._dot, 0, Qt.AlignVCenter)
        self._title_label = QLabel(title, self)
        self._title_label.setStyleSheet(
            "color: #344054; font-size: 9pt; font-weight: 600;"
        )
        layout.addWidget(self._title_label)
        self._detail_label = QLabel("待机", self)
        self._detail_label.setStyleSheet(
            "color: #667085; font-size: 9pt; font-weight: 700;"
        )
        layout.addWidget(self._detail_label)
        layout.addStretch(1)
        self._apply_color("#667085")

    def text(self) -> str:
        return self._raw_text

    def setText(self, value: str) -> None:
        self._raw_text = str(value)
        self._title_label.setText(self._display_title())
        self._detail_label.setText(self._display_state())

    def setStatus(self, value: str, state: str) -> None:
        if state not in {"waiting", "busy", "ready", "error"}:
            raise ValueError(f"unsupported status state: {state}")
        self._state = state
        self.setText(value)
        # The strip is an alarm row, not a nameplate: a device that is merely
        # idle, connected or working stays out of the operator's way.
        self.setVisible(state == "error")

    def styleSheet(self) -> str:
        return self._status_style

    def setStyleSheet(self, style: str) -> None:
        self._status_style = str(style)
        match = re.search(r"color\s*:\s*(#[0-9a-fA-F]{6})", self._status_style)
        if match:
            self._apply_color(match.group(1))

    def _display_title(self) -> str:
        return self._base_title

    def _display_state(self) -> str:
        if self._state == "error":
            if "认证失败" in self._raw_text:
                return "认证失败"
            return "异常"
        if self._state == "busy":
            return "处理中"
        if self._state == "ready" and self._base_title == "高速摄像":
            return "就绪"
        if self._state == "ready":
            return "正常"
        if self._base_title == "计时源":
            return "待数据"
        return "待机"

    def _apply_color(self, color: str) -> None:
        normalized = color.lower()
        detail_color = {
            "#247a52": "#176b49",
            "#16845b": "#176b49",
            "#a56300": "#8a5700",
            "#b54747": "#a43b3b",
        }.get(
            normalized,
            "#667085",
        )
        self._surface_style = "QFrame { background: transparent; border: none; }"
        QFrame.setStyleSheet(self, self._surface_style)
        self._title_label.setStyleSheet(
            "color: #344054; font-size: 9pt; font-weight: 600;"
        )
        self._detail_label.setStyleSheet(
            f"color: {detail_color}; font-size: 9pt; font-weight: 700;"
        )
        self._dot.setStyleSheet(
            f"background: {color}; border: none; border-radius: 4px;"
        )
