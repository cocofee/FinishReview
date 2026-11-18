"""The zoomable evidence picture: person rings, number tags, finish line and counting gestures."""

from __future__ import annotations

from typing import Iterable, Optional

from PyQt5.QtCore import QPoint, QPointF, QRectF, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QFontMetrics, QImage, QPainter, QPen, QPixmap, QTransform
from PyQt5.QtWidgets import (
    QFrame,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsView,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .review_marker_style import marker_style
from .passage_review_common import CTRL_FRAME_STEP, SHIFT_FRAME_STEP


# The counting surface says the two gestures it accepts; the timeline under the
# picture is too thin to steer frame by frame on a moving group.
COUNT_MODE_HELP = (
    "单击画面里的一位过线者就是一个人；"
    "按住左键横向拖动回看前后帧；左右方向键逐帧"
)


# Screen size of the number beside a person on the picture.
PERSON_TAG_FONT_PX = 30  # 11-12: 20 px was too small to read at a glance
# 11-16: in a bunch the number tags covered the riders behind (古浪 507 over 547).
# A counted person is a ring only; their number shows while the mouse is on it.
HOVER_REACH_PX = 16
SELECTED_RING_PX = 15  # the outer ring of the one selected (just clicked, or picked on the left)
# The tag sits this far to the right of the click, level with it, off the
# person's body (operator, 11-12); left or above when that place is taken.
PERSON_TAG_LIFT_PX = 60


class EvidenceImageView(QGraphicsView):
    """Source-resolution image view with persistent zoom and pan state."""

    zoom_changed = pyqtSignal(int)
    full_resolution_requested = pyqtSignal()
    maximize_requested = pyqtSignal()
    activated = pyqtSignal()
    marker_position_selected = pyqtSignal(float, float)
    # Counting a crossing: one click lands on one person, position only — the moment
    # comes from whoever knows which frame is on screen.
    count_mark_selected = pyqtSignal(float, float)
    count_mark_cancelled = pyqtSignal(float, float)
    # The wheel over a picture people are counted on: one frame on (+1) or back (-1).
    frame_wheel = pyqtSignal(int)
    marker_confirm_requested = pyqtSignal()
    marker_cancel_requested = pyqtSignal()
    marker_delete_requested = pyqtSignal()
    frame_step_requested = pyqtSignal(int)
    passage_step_requested = pyqtSignal(int)
    scrub_started = pyqtSignal()
    scrub_delta_requested = pyqtSignal(int)
    scrub_finished = pyqtSignal(int)
    batch_event_requested = pyqtSignal(str)

    MIN_SCALE = 0.25
    MAX_SCALE = 8.0
    # A click that counts a person is a click, not a drag.  Past this many pixels
    # the left button means "scrub the picture", so the two never fire together.
    CLICK_SLOP_PX = 4
    SCRUB_START_PX = 4

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self._pixmap_item = QGraphicsPixmapItem()
        self._detail_pixmap_item = QGraphicsPixmapItem()
        self._pixmap_item.setTransformationMode(Qt.SmoothTransformation)
        self._detail_pixmap_item.setTransformationMode(Qt.SmoothTransformation)
        self._detail_pixmap_item.setZValue(1)
        self._detail_pixmap_item.hide()
        self._message_item = self._scene.addText("")
        self._message_item.setDefaultTextColor(QColor("#c9d2dc"))
        self._message_item.setZValue(2)
        self._scene.addItem(self._pixmap_item)
        self._scene.addItem(self._detail_pixmap_item)
        self.setScene(self._scene)
        self.setAlignment(Qt.AlignCenter)
        self.setBackgroundBrush(QColor("#090b0d"))
        self.setFrameShape(QFrame.NoFrame)
        self.setDragMode(QGraphicsView.NoDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self.setRenderHint(QPainter.SmoothPixmapTransform, True)
        self.setMinimumSize(360, 220)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._source_width = 0
        self._source_height = 0
        self._frame_cache_key = 0
        self._frame_source_x_offset = 0
        self._frame_source_span_width = 0
        self._detail_frame_cache_key = 0
        self._detail_source_x_offset = 0
        self._detail_source_span_width = 0
        self._fit_mode = True
        self._marker_mode = False
        self._count_mode = False
        # Compact workbench mode: one click counts, and neither a marker prompt
        # nor a second maximised picture may open behind the operator's back.
        self._workbench_compact = False
        self._finish_line_points = ()
        self._marker: Optional[tuple[float, float, str, bool]] = None
        self._marker_simple = False
        self._history_markers: tuple[
            tuple[float, float, str, bool, bool, bool, bool, bool, bool, bool], ...
        ] = ()
        self._hover_position: Optional[QPoint] = None  # the mouse over the picture, for ring numbers
        self._show_marker_label = True
        self._show_identity_badge = True
        self._mouse_press_position: Optional[QPoint] = None
        self._mouse_dragged = False
        self._marker_dragging = False
        self._video_scrubbing = False
        self._pan_dragging = False
        self._pan_last_position: Optional[QPoint] = None
        self._identity_badge = QLabel(self.viewport())
        self._identity_badge.setObjectName("evidenceIdentityBadge")
        self._identity_badge.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._identity_badge.setStyleSheet(
            "QLabel#evidenceIdentityBadge {"
            " background: rgba(15, 23, 32, 225);"
            " color: #ffffff;"
            " border: 2px solid #e26d68;"
            " border-radius: 3px;"
            " padding: 7px 13px;"
            " font-size: 24px;"
            " font-weight: 700;"
            "}"
        )
        self._identity_badge.hide()
        self._frame_badge = QLabel(self.viewport())
        self._frame_badge.setObjectName("evidenceFrameBadge")
        self._frame_badge.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._frame_badge.setAlignment(Qt.AlignCenter)
        self._frame_badge.setStyleSheet(
            "QLabel#evidenceFrameBadge {"
            " background: rgba(7, 20, 31, 235);"
            " color: #dffaff;"
            " border: 2px solid #00c8ff;"
            " border-radius: 3px;"
            " padding: 5px 9px;"
            " font-size: 13px;"
            " font-weight: 700;"
            "}"
        )
        self._frame_badge.setFixedSize(300, 34)
        self._frame_badge.hide()
        # 11-14: who the next click is, in the corner of the picture the eyes are on.
        self._target_badge = QLabel(self.viewport())
        self._target_badge.setObjectName("evidenceTargetBadge")
        self._target_badge.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._target_badge.setAlignment(Qt.AlignCenter)
        self._target_badge.hide()
        self._batch_roster_panel = QFrame(self.viewport())
        self._batch_roster_panel.setObjectName("evidenceBatchRoster")
        self._batch_roster_panel.setFixedWidth(176)
        self._batch_roster_panel.setStyleSheet(
            "QFrame#evidenceBatchRoster {"
            " background: rgba(5, 10, 16, 242);"
            " border: 2px solid #f0b24b;"
            " border-radius: 3px;"
            "}"
        )
        batch_layout = QVBoxLayout(self._batch_roster_panel)
        batch_layout.setContentsMargins(10, 8, 10, 10)
        batch_layout.setSpacing(6)
        self._batch_roster_title = QLabel("", self._batch_roster_panel)
        self._batch_roster_title.setStyleSheet(
            "color: #ffd27a; font-size: 14px; font-weight: 700;"
        )
        self._batch_roster_title.setWordWrap(True)
        batch_layout.addWidget(self._batch_roster_title)
        self._batch_roster_scroll = QScrollArea(self._batch_roster_panel)
        self._batch_roster_scroll.setFrameShape(QFrame.NoFrame)
        self._batch_roster_scroll.setWidgetResizable(True)
        self._batch_roster_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._batch_roster_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._batch_roster_scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollArea > QWidget > QWidget { background: transparent; }"
        )
        self._batch_roster_content = QWidget()
        self._batch_roster_content.setObjectName("evidenceBatchRosterContent")
        self._batch_roster_layout = QVBoxLayout(self._batch_roster_content)
        self._batch_roster_layout.setContentsMargins(0, 0, 0, 0)
        self._batch_roster_layout.setSpacing(6)
        self._batch_roster_layout.setAlignment(Qt.AlignTop)
        self._batch_roster_scroll.setWidget(self._batch_roster_content)
        batch_layout.addWidget(self._batch_roster_scroll, 1)
        self._batch_roster_buttons: list[QPushButton] = []
        self._batch_roster_entry_count = 0
        self._batch_roster_panel.hide()
        self.setFocusPolicy(Qt.StrongFocus)

    @property
    def has_frame(self) -> bool:
        return not self._pixmap_item.pixmap().isNull()

    @property
    def zoom_percent(self) -> int:
        return int(round(self.transform().m11() * 100.0))

    def reset_view(self) -> None:
        self._fit_mode = True
        if self.has_frame:
            self.fit_to_window()

    def set_frame(
        self,
        image: QImage,
        *,
        source_width: int = 0,
        source_height: int = 0,
    ) -> None:
        cache_key = int(image.cacheKey())
        expected_source_width = max(image.width(), int(source_width or 0))
        expected_source_height = max(image.height(), int(source_height or 0))
        try:
            source_x_offset = max(
                0,
                int(image.text("finishreview.source_x_offset") or 0),
            )
            source_span_width = max(
                1,
                int(
                    image.text("finishreview.source_span_width")
                    or expected_source_width
                ),
            )
        except ValueError:
            source_x_offset = 0
            source_span_width = expected_source_width
        is_detail_tile = bool(
            source_x_offset > 0 or source_span_width < expected_source_width
        )
        if is_detail_tile:
            if (
                cache_key == self._detail_frame_cache_key
                and source_x_offset == self._detail_source_x_offset
                and source_span_width == self._detail_source_span_width
            ):
                return
            pixmap = QPixmap.fromImage(image)
            if pixmap.isNull():
                return
            self._source_width = max(pixmap.width(), expected_source_width)
            self._source_height = max(pixmap.height(), expected_source_height)
            self._detail_frame_cache_key = cache_key
            self._detail_source_x_offset = min(
                source_x_offset,
                max(0, self._source_width - 1),
            )
            self._detail_source_span_width = min(
                source_span_width,
                max(1, self._source_width - self._detail_source_x_offset),
            )
            self._detail_pixmap_item.setPixmap(pixmap)
            self._detail_pixmap_item.setPos(self._detail_source_x_offset, 0)
            self._detail_pixmap_item.setTransform(
                QTransform.fromScale(
                    self._detail_source_span_width / max(1, pixmap.width()),
                    self._source_height / max(1, pixmap.height()),
                )
            )
            self._detail_pixmap_item.show()
            self._scene.setSceneRect(
                QRectF(0, 0, self._source_width, self._source_height)
            )
            self._message_item.hide()
            return
        self._detail_pixmap_item.setPixmap(QPixmap())
        self._detail_pixmap_item.hide()
        self._detail_frame_cache_key = 0
        self._detail_source_x_offset = 0
        self._detail_source_span_width = 0
        if (
            self.has_frame
            and cache_key == self._frame_cache_key
            and expected_source_width == self._source_width
            and expected_source_height == self._source_height
            and source_x_offset == self._frame_source_x_offset
            and source_span_width == self._frame_source_span_width
        ):
            return
        pixmap = QPixmap.fromImage(image)
        if pixmap.isNull():
            return
        self._frame_cache_key = cache_key
        self._source_width = max(pixmap.width(), expected_source_width)
        self._source_height = max(pixmap.height(), expected_source_height)
        self._frame_source_x_offset = min(
            source_x_offset,
            max(0, self._source_width - 1),
        )
        self._frame_source_span_width = min(
            source_span_width,
            max(1, self._source_width - self._frame_source_x_offset),
        )
        self._pixmap_item.setPixmap(pixmap)
        self._pixmap_item.setPos(self._frame_source_x_offset, 0)
        self._pixmap_item.setTransform(
            QTransform.fromScale(
                self._frame_source_span_width / max(1, pixmap.width()),
                self._source_height / max(1, pixmap.height()),
            )
        )
        self._scene.setSceneRect(
            QRectF(0, 0, self._source_width, self._source_height)
        )
        self._message_item.hide()
        if self._fit_mode:
            self.fit_to_window()

    def clear_frame(self, message: str = "") -> None:
        self._pixmap_item.setPixmap(QPixmap())
        self._pixmap_item.setPos(0, 0)
        self._detail_pixmap_item.setPixmap(QPixmap())
        self._detail_pixmap_item.setPos(0, 0)
        self._detail_pixmap_item.hide()
        self._frame_cache_key = 0
        self._frame_source_x_offset = 0
        self._frame_source_span_width = 0
        self._detail_frame_cache_key = 0
        self._detail_source_x_offset = 0
        self._detail_source_span_width = 0
        self._source_width = 0
        self._source_height = 0
        self.resetTransform()
        self._fit_mode = True
        self._scene.setSceneRect(
            QRectF(0, 0, max(1, self.viewport().width()), max(1, self.viewport().height()))
        )
        self._message_item.setPlainText(str(message or ""))
        self._message_item.setDefaultTextColor(QColor("#c9d2dc"))
        self._message_item.show()
        self._position_message()
        self.clear_identity_cue()
        self.clear_frame_indicator()
        self.zoom_changed.emit(100)
        self.viewport().update()

    def set_identity_cue(self, identity: str, status: str) -> None:
        if not self._show_identity_badge:
            self.clear_identity_cue()
            return
        identity = str(identity).strip()
        if not identity or not self.has_frame:
            self.clear_identity_cue()
            return
        status = str(status).strip()
        confirmed = status == "已确认"
        reference = status == "参考"
        background_color = (
            "#1bbf83" if confirmed else "#667085" if reference else "#c0372b"
        )
        text_color = "#07120e" if confirmed else "#ffffff"
        border_color = (
            "#4dd6a5" if confirmed else "#98a2b3" if reference else "#e26d68"
        )
        self._identity_badge.setText(identity)
        self._identity_badge.setStyleSheet(
            "QLabel#evidenceIdentityBadge {"
            f" background: {background_color};"
            f" color: {text_color};"
            f" border: 2px solid {border_color};"
            " border-radius: 3px;"
            " padding: 8px 18px;"
            " font-size: 34px;"
            " font-weight: 700;"
            "}"
        )
        self._identity_badge.adjustSize()
        self._position_identity_badge()
        self._identity_badge.show()
        self._identity_badge.raise_()

    def clear_identity_cue(self) -> None:
        self._identity_badge.clear()
        self._identity_badge.hide()

    def set_frame_indicator(self, text: str) -> None:
        value = str(text).strip()
        if not value:
            self.clear_frame_indicator()
            return
        self._frame_badge.setText(value)
        self._position_frame_badge()
        self._frame_badge.show()
        self._frame_badge.raise_()

    def set_target_badge(self, text: str, kind: str = "next") -> None:
        """下一位 N 号 / N 号 已点 in the top right corner of the picture; empty hides it."""
        value = str(text or "").strip()
        if not value:
            self._target_badge.hide()
            return
        state = (value, kind)
        if state != getattr(self, "_target_state", None):
            self._target_state = state
            self._target_badge.setStyleSheet(
                "QLabel#evidenceTargetBadge { background: %s; color: %s; border: 3px solid #111827;"
                " border-radius: 4px; padding: 2px 14px; font-size: 22pt; font-weight: 800; }"
                % {"alert": ("#b42318", "#ffffff"), "done": ("#2fb872", "#ffffff")}.get(kind, ("#ffd84d", "#111827")))
            self._target_badge.setText(value)
            self._target_badge.adjustSize()
        self._target_badge.show()
        self._target_badge.raise_()
        self._position_target_badge()

    def target_badge_text(self) -> str:
        return "" if self._target_badge.isHidden() else self._target_badge.text()

    def _position_target_badge(self) -> None:
        # 11-14: top, left of the middle, where the riders come in ("视角不在右边");
        # right of the camera's own date and time, which stay readable.
        width = self.viewport().width()
        left = min(int(width * 0.41), width - self._target_badge.width() - 12)
        self._target_badge.move(max(12, left), 6)

    def clear_frame_indicator(self) -> None:
        self._frame_badge.clear()
        self._frame_badge.hide()

    def set_identity_badge_visible(self, visible: bool) -> None:
        self._show_identity_badge = bool(visible)
        if not self._show_identity_badge:
            self.clear_identity_cue()

    def set_marker_label_visible(self, visible: bool) -> None:
        self._show_marker_label = bool(visible)
        self.viewport().update()

    def set_finish_line(self, points=()) -> None:
        """Keep actual camera geometry separate from rider identity markers."""

        points = tuple((float(x), float(y)) for x, y in points)
        if points != self._finish_line_points:
            self._finish_line_points = points
            self.viewport().update()

    def set_batch_roster(
        self,
        entries: Iterable[tuple[str, str, bool, bool]],
        *,
        title: str = "",
    ) -> None:
        values = tuple(entries)
        for button in self._batch_roster_buttons:
            self._batch_roster_layout.removeWidget(button)
            button.deleteLater()
        self._batch_roster_buttons.clear()
        self._batch_roster_entry_count = len(values)
        if not values:
            self.clear_batch_roster()
            return
        self._batch_roster_title.setText(
            str(title).strip() or f"待判号码 {len(values)} 人 · 看到谁就点谁"
        )
        for event_id, bib, confirmed, selected in values:
            button = QPushButton(str(bib).strip() or "?", self._batch_roster_content)
            button.setFixedHeight(38)
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            if selected:
                colors = "background: #f0b24b; color: #101820; border: 2px solid #ffffff;"
            elif confirmed:
                colors = "background: #1b9b68; color: #ffffff; border: 1px solid #58d7a7;"
            else:
                colors = "background: #f4f6f8; color: #17212b; border: 1px solid #aeb8c2;"
            button.setStyleSheet(
                "QPushButton {"
                f" {colors} border-radius: 3px; font-size: 20px; font-weight: 800;"
                "} QPushButton:hover { border: 2px solid #f0b24b; }"
            )
            button.setToolTip("在当前视频帧定位这个运动员")
            button.clicked.connect(
                lambda _checked=False, value=event_id: self.batch_event_requested.emit(value)
            )
            self._batch_roster_layout.addWidget(button)
            self._batch_roster_buttons.append(button)
        self._batch_roster_panel.show()
        self._position_batch_roster()
        self._batch_roster_scroll.verticalScrollBar().setValue(0)
        self._batch_roster_panel.raise_()

    def clear_batch_roster(self) -> None:
        self._batch_roster_title.clear()
        self._batch_roster_entry_count = 0
        self._batch_roster_panel.hide()

    def set_marker_mode(self, enabled: bool) -> None:
        if self._workbench_compact:
            # In the compact workbench a plain click means "one more person".
            # Never arm the identity line or show its placement prompt there.
            self._marker_mode = False
            self.viewport().setCursor(
                Qt.CrossCursor if self._count_mode else Qt.ArrowCursor
            )
            # 11-15: the floating hint sat on the picture where people are clicked.
            self.setToolTip("")
            return
        self._marker_mode = bool(enabled) and self.has_frame
        self.viewport().setCursor(
            Qt.CrossCursor if (self._marker_mode or self._count_mode) else Qt.ArrowCursor
        )
        if self._workbench_compact:
            self.setToolTip("")  # 11-15: no floating hint over the picture in the workbench
        elif self._marker_mode:
            self.setToolTip(
                "单击放置判读线；Shift + 左键拖动判读线；"
                "中键拖动缩放后的画面"
            )
        elif self._count_mode:
            self.setToolTip(COUNT_MODE_HELP)
        else:
            self.setToolTip("滚轮放大后，中键拖动查看号码")

    def set_count_mode(self, enabled: bool) -> None:
        """A click counts a person instead of placing the identity line.

        The frame is what the operator is looking at; counting must not make them
        reach for another window, so the picture itself takes the click.  Dragging
        the same button horizontally scrubs the frames around it, which is easier
        to steer than the thin timeline under the picture.
        """

        self._count_mode = bool(enabled) and self.has_frame
        if self._count_mode and self._workbench_compact:
            # Counting wins in the workbench: never leave the marker prompt
            # armed underneath it.  Ordinary review keeps its previous mode.
            self._marker_mode = False
        self.viewport().setCursor(
            Qt.CrossCursor if (self._marker_mode or self._count_mode) else Qt.ArrowCursor
        )
        if self._workbench_compact:
            self.setToolTip("")  # 11-15: no floating hint over the picture in the workbench
        elif self._marker_mode:
            self.setToolTip(
                "单击放置判读线；Shift + 左键拖动判读线；"
                "中键拖动缩放后的画面"
            )
        elif self._count_mode:
            self.setToolTip(COUNT_MODE_HELP)
        else:
            self.setToolTip("滚轮放大后，中键拖动查看号码")

    def set_workbench_compact(self, enabled: bool) -> None:
        """Switch the picture into the compact workbench interaction model."""

        self._workbench_compact = bool(enabled)
        if self._workbench_compact:
            self._marker_mode = False
        self.viewport().setCursor(
            Qt.CrossCursor if (self._marker_mode or self._count_mode) else Qt.ArrowCursor
        )
        if self._workbench_compact:
            self.setToolTip("")  # 11-15: no floating hint over the picture in the workbench
        elif self._marker_mode:
            self.setToolTip(
                "单击放置判读线；Shift + 左键拖动判读线；"
                "中键拖动缩放后的画面"
            )
        elif self._count_mode:
            self.setToolTip(COUNT_MODE_HELP)
        else:
            self.setToolTip("滚轮放大后，中键拖动查看号码")

    def set_marker(
        self,
        x_normalized: float,
        y_normalized: float,
        label: str,
        *,
        confirmed: bool,
        simple: bool = False,
    ) -> None:
        self._marker = (
            max(0.0, min(1.0, float(x_normalized))),
            max(0.0, min(1.0, float(y_normalized))),
            str(label),
            bool(confirmed),
        )
        self._marker_simple = bool(simple)
        self.viewport().update()

    def clear_marker(self) -> None:
        self._marker = None
        self._marker_simple = False
        self.viewport().update()

    def set_history_markers(
        self,
        markers: Iterable[tuple[float, float, str, bool, bool]],
    ) -> None:
        """Show confirmed markers from nearby passages without replacing the active marker."""

        self._history_markers = tuple(
            (
                max(0.0, min(1.0, float(x_normalized))),
                max(0.0, min(1.0, float(y_normalized))),
                str(label),
                bool(confirmed),
                bool(simple),
                bool(marker[5]) if len(marker) > 5 else False,
                bool(marker[6]) if len(marker) > 6 else False,
                bool(marker[7]) if len(marker) > 7 else False,
                # 11-16: the number only while the mouse is on the ring; the selected one ringed twice.
                bool(marker[8]) if len(marker) > 8 else False,
                bool(marker[9]) if len(marker) > 9 else False,
            )
            for marker in markers
            for x_normalized, y_normalized, label, confirmed, simple in [marker[:5]]
        )
        self.viewport().update()

    def fit_to_window(self) -> None:
        if not self.has_frame:
            return
        self._fit_mode = True
        self.fitInView(self._scene.sceneRect(), Qt.KeepAspectRatio)
        self.zoom_changed.emit(self.zoom_percent)

    def set_actual_size(self) -> None:
        if not self.has_frame:
            return
        self._fit_mode = False
        self.resetTransform()
        self.zoom_changed.emit(100)
        self.full_resolution_requested.emit()

    def zoom_by(self, factor: float) -> None:
        if not self.has_frame:
            return
        current = max(0.001, self.transform().m11())
        target = max(self.MIN_SCALE, min(self.MAX_SCALE, current * float(factor)))
        if abs(target - current) < 0.0001:
            return
        self._fit_mode = False
        self.scale(target / current, target / current)
        self.zoom_changed.emit(self.zoom_percent)
        self.full_resolution_requested.emit()

    def wheelEvent(self, event) -> None:
        if self.has_frame and event.angleDelta().y():
            if self._count_mode and not event.modifiers() & Qt.ControlModifier:
                # Counting people: the wheel walks the recording a frame at a
                # time (down is on), the hand never leaves the mouse to find
                # the frame someone meets the line on.  Ctrl + wheel zooms.
                self.frame_wheel.emit(-1 if event.angleDelta().y() > 0 else 1)
            else:
                self.zoom_by(1.2 if event.angleDelta().y() > 0 else 1.0 / 1.2)
            event.accept()
            return
        super().wheelEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.RightButton and self._count_mode and self.has_frame:
            if self._scene.sceneRect().contains(self.mapToScene(event.pos())):
                self.count_mark_cancelled.emit(*self._normalized_position(event.pos()))
            event.accept()
            return
        if event.button() == Qt.MiddleButton and self.has_frame:
            self._pan_dragging = True
            self._pan_last_position = event.pos()
            self.viewport().setCursor(Qt.ClosedHandCursor)
            event.accept()
            return
        if event.button() == Qt.LeftButton:
            self._mouse_press_position = event.pos()
            self._mouse_dragged = False
            self._video_scrubbing = False
            self._marker_dragging = bool(
                self._marker_mode
                and self.has_frame
                and event.modifiers() & Qt.ShiftModifier
            )
            if self._marker_dragging:
                self._select_marker_position(event.pos())
                event.accept()
                return
            event.accept()
            return
        super().mousePressEvent(event)

    def _hovered_marker(self, position) -> int:
        """Index of the ring (numbered on hover) under the mouse, -1 for none."""
        if position is None or self._source_width <= 0 or self._source_height <= 0:
            return -1
        best, reach = -1, HOVER_REACH_PX ** 2
        for index, marker in enumerate(self._history_markers):
            if not marker[8] or marker[6]:
                continue
            point = self.mapFromScene(QPointF(marker[0] * self._source_width, marker[1] * self._source_height))
            distance = (point.x() - position.x()) ** 2 + (point.y() - position.y()) ** 2
            if distance <= reach:
                best, reach = index, distance
        return best

    def leaveEvent(self, event) -> None:
        if self._hovered_marker(self._hover_position) >= 0:
            self.viewport().update()
        self._hover_position = None
        super().leaveEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if not event.buttons():
            # Only looking: the ring under the mouse shows its number, nothing else changes.
            before = self._hovered_marker(self._hover_position)
            self._hover_position = event.pos()
            if self._hovered_marker(self._hover_position) != before:
                self.viewport().update()
        if (
            self._pan_dragging
            and self._pan_last_position is not None
            and event.buttons() & Qt.MiddleButton
        ):
            delta = event.pos() - self._pan_last_position
            self._pan_last_position = event.pos()
            self.horizontalScrollBar().setValue(
                self.horizontalScrollBar().value() - delta.x()
            )
            self.verticalScrollBar().setValue(
                self.verticalScrollBar().value() - delta.y()
            )
            event.accept()
            return
        if (
            self._mouse_press_position is not None
            and (event.pos() - self._mouse_press_position).manhattanLength()
            > self.CLICK_SLOP_PX
        ):
            self._mouse_dragged = True
        if (
            self._marker_dragging
            and event.buttons() & Qt.LeftButton
        ):
            self._select_marker_position(event.pos())
            event.accept()
            return
        # Counting mode: a mostly-horizontal left drag scrubs the recording.
        # Movement below SCRUB_START_PX stays a click, so one person is still one
        # click; once the drag starts it never also counts on release.  Counting
        # wins over the identity-line mode just as it does for a plain click;
        # Shift + left still moves the line, and the middle button still pans.
        if (
            self._count_mode
            and self.has_frame
            and self._mouse_press_position is not None
            and event.buttons() & Qt.LeftButton
        ):
            horizontal_delta = event.pos().x() - self._mouse_press_position.x()
            if self._video_scrubbing or (
                abs(horizontal_delta) > self.SCRUB_START_PX
                and abs(horizontal_delta)
                >= abs(event.pos().y() - self._mouse_press_position.y())
            ):
                if not self._video_scrubbing:
                    self._video_scrubbing = True
                    self.viewport().setCursor(Qt.SizeHorCursor)
                    self.scrub_started.emit()
                # The pane coalesces these pixel deltas on an 80 ms timer and
                # snaps them to whole frames before asking the decoder to seek.
                self.scrub_delta_requested.emit(horizontal_delta)
                event.accept()
                return
        if (
            self._mouse_dragged
            and self._mouse_press_position is not None
            and event.buttons() & Qt.LeftButton
        ):
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MiddleButton and self._pan_dragging:
            self._pan_dragging = False
            self._pan_last_position = None
            self.viewport().setCursor(
                Qt.CrossCursor
                if (self._marker_mode or self._count_mode)
                else Qt.ArrowCursor
            )
            event.accept()
            return
        if (
            event.button() == Qt.LeftButton
            and self._mouse_press_position is not None
        ):
            press_position = self._mouse_press_position
            marker_dragging = self._marker_dragging
            was_click = (
                not self._mouse_dragged
                and (event.pos() - press_position).manhattanLength()
                <= self.CLICK_SLOP_PX
            )
            self._mouse_press_position = None
            self._mouse_dragged = False
            self._marker_dragging = False
            video_scrubbing = self._video_scrubbing
            self._video_scrubbing = False
            self.viewport().setCursor(
                Qt.CrossCursor
                if (self._marker_mode or self._count_mode)
                else Qt.ArrowCursor
            )
            if marker_dragging:
                self._select_marker_position(event.pos())
                self.setFocus(Qt.MouseFocusReason)
                event.accept()
                return
            if video_scrubbing:
                self.scrub_finished.emit(event.pos().x() - press_position.x())
                self.setFocus(Qt.MouseFocusReason)
                event.accept()
                return
            if was_click and self._count_mode and self.has_frame:
                # Which frame is on screen is the surface's business, not the view's;
                # the view reports the person's place and nothing else.  A click in
                # the black beside the picture counts nothing.
                if self._scene.sceneRect().contains(self.mapToScene(event.pos())):
                    self.count_mark_selected.emit(*self._normalized_position(event.pos()))
            elif was_click and self._marker_mode and self.has_frame:
                self._select_marker_position(event.pos())
                self.setFocus(Qt.MouseFocusReason)
            event.accept()
            return
        self._mouse_press_position = None
        self._mouse_dragged = False
        self._marker_dragging = False
        self._video_scrubbing = False
        super().mouseReleaseEvent(event)

    def _normalized_position(self, viewport_position: QPoint) -> tuple[float, float]:
        """Where in the source frame a click landed, in fractions of width and height."""

        scene_position = self.mapToScene(viewport_position)
        return (scene_position.x() / max(1, self._source_width),
                scene_position.y() / max(1, self._source_height))

    def _select_marker_position(self, viewport_position: QPoint) -> None:
        scene_position = self.mapToScene(viewport_position)
        if not self._scene.sceneRect().contains(scene_position):
            return
        self.marker_position_selected.emit(*self._normalized_position(viewport_position))

    def mouseDoubleClickEvent(self, event) -> None:
        if self._workbench_compact and event.button() == Qt.LeftButton:
            # The workbench picture already fills its space; a double click
            # must not open a second copy of the same camera.
            event.accept()
            return
        if event.button() == Qt.LeftButton and self.has_frame and not self._marker_mode:
            self.maximize_requested.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def focusInEvent(self, event) -> None:
        super().focusInEvent(event)
        self.activated.emit()

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key_Left, Qt.Key_Right):
            modifiers = event.modifiers()
            step_size = (
                CTRL_FRAME_STEP
                if modifiers & Qt.ControlModifier
                else SHIFT_FRAME_STEP
                if modifiers & Qt.ShiftModifier
                else 1
            )
            direction = -1 if event.key() == Qt.Key_Left else 1
            self.frame_step_requested.emit(direction * step_size)
            event.accept()
            return
        if event.key() in (Qt.Key_Up, Qt.Key_Down):
            self.passage_step_requested.emit(-1 if event.key() == Qt.Key_Up else 1)
            event.accept()
            return
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.marker_confirm_requested.emit()
            event.accept()
            return
        if event.key() == Qt.Key_Escape:
            self.marker_cancel_requested.emit()
            event.accept()
            return
        if event.key() == Qt.Key_Delete:
            self.marker_delete_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def _person_tag_rect(self, x, y, label, scale, visible, occupied):
        """Place a small screen-sized tag near its point, inside the visible image."""
        font = QFont(self.font())
        font.setPixelSize(PERSON_TAG_FONT_PX)
        font.setBold(True)
        width = min(max(34, QFontMetrics(font).horizontalAdvance(label) + 16) / scale, visible.width())
        height = min((PERSON_TAG_FONT_PX + 10) / scale, visible.height())
        gap, lift = 12 / scale, PERSON_TAG_LIFT_PX / scale
        candidates = []
        for dx, dy in ((lift, -height / 2), (-width - lift, -height / 2), (gap, -height - lift),
                       (-width - gap, -height - lift), (gap, gap), (-width - gap, gap)):
            left = max(visible.left(), min(x + dx, visible.right() - width))
            top = max(visible.top(), min(y + dy, visible.bottom() - height))
            candidate = QRectF(left, top, width, height)
            overlap = sum(candidate.intersected(other).width() * candidate.intersected(other).height()
                          for other in occupied if candidate.intersects(other))
            candidates.append((overlap, candidate))
        return min(candidates, key=lambda value: value[0])[1]

    @staticmethod
    def _draw_selected_ring(painter, x, y, color, scale):
        """A second ring round the one selected: their number is in the corner badge."""
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        pen = QPen(color, 2.5)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawEllipse(QPointF(x, y), SELECTED_RING_PX / scale, SELECTED_RING_PX / scale)
        painter.restore()

    def _draw_person_tag(self, painter, x, y, label, color, dashed, ghost, scale, visible, occupied):
        """A point and number label, not a timing line or a tracking box."""
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        radius = (7 if ghost else 10) / scale
        pen = QPen(color, 2.5)
        pen.setCosmetic(True)
        if dashed or ghost:
            pen.setStyle(Qt.DashLine)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawEllipse(QPointF(x, y), radius, radius)
        if self._show_marker_label and label and not ghost and not visible.isEmpty():
            tag = self._person_tag_rect(x, y, label, scale, visible, occupied)
            occupied.append(tag)
            # The shortest connector ends at the label edge and outside the ring.
            end = QPointF(max(tag.left(), min(x, tag.right())), max(tag.top(), min(y, tag.bottom())))
            delta = end - QPointF(x, y)
            length = (delta.x() ** 2 + delta.y() ** 2) ** .5
            if length > radius:
                painter.drawLine(QPointF(x, y) + delta * (radius / length), end)
            # The tag's border carries the marker colour: green counted, grey-blue paired by chip.
            painter.setBrush(QColor(20, 27, 35, 225))
            painter.drawRoundedRect(tag, 4 / scale, 4 / scale)
            font = QFont(self.font())
            font.setPixelSize(max(1, round(PERSON_TAG_FONT_PX / scale)))
            font.setBold(True)
            painter.setFont(font)
            painter.setPen(QColor("#ffffff"))
            painter.drawText(tag, Qt.AlignCenter, label)
        painter.restore()

    def drawForeground(self, painter: QPainter, rect: QRectF) -> None:
        super().drawForeground(painter, rect)
        if self._source_width <= 0 or self._source_height <= 0:
            return
        scale = max(0.001, abs(self.transform().m11()))
        visible = self.mapToScene(self.viewport().rect()).boundingRect().intersected(
            QRectF(0, 0, self._source_width, self._source_height))
        occupied = []
        painter.save()
        if self._finish_line_points:
            painter.setRenderHint(QPainter.Antialiasing)
            # Cosmetic strokes stay legible in both fitted and source-resolution views.
            for color, width in (("#071c17", 5), ("#00edbb", 3)):
                pen = QPen(QColor(color), width)
                pen.setCosmetic(True)
                painter.setPen(pen)
                for a, b in zip(self._finish_line_points, self._finish_line_points[1:]):
                    painter.drawLine(QPointF(a[0] * self._source_width, a[1] * self._source_height),
                                     QPointF(b[0] * self._source_width, b[1] * self._source_height))
        hovered = self._hovered_marker(self._hover_position)
        for index, (x_normalized, y_normalized, label, confirmed, simple, dot, ghost, auto,
                    hover_only, selected) in enumerate(self._history_markers):
            x = x_normalized * self._source_width
            y = y_normalized * self._source_height
            # Grey-blue and dashed: paired by the system (自动对上), nobody has counted them yet.
            stroke_color, dashed = marker_style(automatic=auto, manual=confirmed)
            color = QColor(stroke_color)
            if ghost:
                # Counted on a nearby frame: faded where they were, so they are not clicked twice.
                color.setAlpha(150)
            if self._workbench_compact or dot:
                if visible.adjusted(-6 / scale, -6 / scale, 6 / scale, 6 / scale).contains(QPointF(x, y)):
                    shown = label if not hover_only or index == hovered else ""
                    self._draw_person_tag(painter, x, y, shown, color,
                                          auto or (not confirmed and not simple), ghost, scale, visible, occupied)
                    if selected and not ghost:
                        self._draw_selected_ring(painter, x, y, color, scale)
                continue
            pen = QPen(color, 2)
            pen.setCosmetic(True)
            if dashed or ghost:
                pen.setStyle(Qt.DashLine)
            painter.setPen(pen)
            painter.drawLine(int(x), 0, int(x), self._source_height)
            if not self._show_marker_label or ghost:
                continue
            margin = 6.0 / scale
            size = 18.0
            label = label if confirmed or auto else f"{label} 待核"
            tag_width = max(52.0, 18.0 + len(label) * size) / scale
            tag_height = 30.0 / scale
            # Beside the ring, not on the rider.
            offset = margin
            tag_x = x + offset
            if tag_x + tag_width > self._source_width - margin:
                tag_x = x - tag_width - offset
            tag_x = max(margin, min(tag_x, self._source_width - tag_width - margin))
            tag_y = margin + index * (tag_height + margin)
            tag_y = max(margin, min(tag_y, max(margin, self._source_height - tag_height - margin)))
            tag_rect = QRectF(tag_x, tag_y, tag_width, tag_height)
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            painter.drawRoundedRect(tag_rect, 2.0 / scale, 2.0 / scale)
            painter.setPen(QColor("#ffffff"))
            font = QFont(self.font())
            font.setPixelSize(max(1, int(round(size / scale))))
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(tag_rect, Qt.AlignCenter, label)
        painter.restore()
        if self._marker is None:
            return
        x_normalized, y_normalized, label, confirmed = self._marker
        simple = self._marker_simple
        x = x_normalized * self._source_width
        y = y_normalized * self._source_height
        color = QColor("#1bbf83" if confirmed else "#c0372b")
        if self._workbench_compact:
            if visible.adjusted(-6 / scale, -6 / scale, 6 / scale, 6 / scale).contains(QPointF(x, y)):
                self._draw_person_tag(painter, x, y, label, color, not confirmed and not simple,
                                      False, scale, visible, occupied)
            return
        pen = QPen(color, 3)
        pen.setCosmetic(True)
        if not confirmed and not simple:
            pen.setStyle(Qt.DashLine)

        painter.save()
        painter.setPen(pen)
        painter.drawLine(int(x), 0, int(x), self._source_height)
        if not simple:
            cross_extent = 22.0 / scale
            painter.drawLine(int(x - cross_extent), int(y), int(x + cross_extent), int(y))
            painter.drawLine(int(x), int(y - cross_extent), int(x), int(y + cross_extent))

        if not self._show_marker_label:
            painter.restore()
            return

        tag_text = label
        margin = 8.0 / scale
        tag_width = max(76.0, 28.0 + len(tag_text) * 26.0) / scale
        tag_height = 52.0 / scale
        visible_left = max(0.0, rect.left())
        visible_top = max(0.0, rect.top())
        visible_right = min(float(self._source_width), rect.right())
        visible_bottom = min(float(self._source_height), rect.bottom())
        tag_x = x + margin
        if tag_x + tag_width > visible_right - margin:
            tag_x = x - tag_width - margin
        tag_x = max(
            visible_left + margin,
            min(tag_x, max(visible_left + margin, visible_right - tag_width - margin)),
        )
        tag_y = max(
            visible_top + margin,
            min(y + margin, max(visible_top + margin, visible_bottom - tag_height - margin)),
        )
        tag_rect = QRectF(tag_x, tag_y, tag_width, tag_height)
        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        painter.drawRoundedRect(tag_rect, 3.0 / scale, 3.0 / scale)
        painter.setPen(QColor("#07120e" if confirmed else "#231703"))
        font = QFont(self.font())
        font.setPixelSize(max(1, int(round(30.0 / scale))))
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(tag_rect, Qt.AlignCenter, tag_text)
        painter.restore()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._fit_mode and self.has_frame:
            self.fit_to_window()
        self._position_message()
        self._position_identity_badge()
        self._position_frame_badge()
        self._position_batch_roster()

    def _position_message(self) -> None:
        rect = self._message_item.boundingRect()
        scene_rect = self._scene.sceneRect()
        self._message_item.setPos(
            scene_rect.center().x() - rect.width() / 2.0,
            scene_rect.center().y() - rect.height() / 2.0,
        )

    def _position_identity_badge(self) -> None:
        self._identity_badge.move(12, 12)

    def _position_frame_badge(self) -> None:
        self._position_target_badge()
        if not self._frame_badge.isVisible():
            return
        margin = 12
        self._frame_badge.move(
            max(margin, self.viewport().width() - self._frame_badge.width() - margin),
            margin,
        )

    def _position_batch_roster(self) -> None:
        if not self._batch_roster_panel.isVisible():
            return
        margin = 12
        panel_width = self._batch_roster_panel.width()
        available_height = max(0, self.viewport().height() - 2 * margin)
        title_width = max(1, panel_width - 20)
        title_height = self._batch_roster_title.heightForWidth(title_width)
        if title_height < 0:
            title_height = self._batch_roster_title.sizeHint().height()
        visible_rows = min(max(1, self._batch_roster_entry_count), 10)
        desired_height = 8 + title_height + 6 + visible_rows * 44 + 10
        panel_height = min(available_height, desired_height)
        self._batch_roster_panel.setGeometry(
            margin,
            margin,
            panel_width,
            panel_height,
        )
