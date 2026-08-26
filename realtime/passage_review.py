"""FinishLynx-style review workspace for CycleRace passages and video evidence."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import time
from typing import Callable, Iterable, Optional

from PyQt5.QtCore import QPoint, QRectF, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import (
    QBrush,
    QColor,
    QFont,
    QImage,
    QKeySequence,
    QPainter,
    QPalette,
    QPen,
    QPixmap,
    QTransform,
)
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSlider,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStyledItemDelegate,
    QShortcut,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from .auyat_rgb import AUYAT_CLOCK_SOURCE, AuyatRgbPlaybackWorker
from .external_clip_import import EXTERNAL_CLOCK_SOURCE
from .passage_evidence import (
    HIGH_SPEED_SOURCE,
    REGULAR_SOURCE,
    PassageEvidenceAssociation,
    PassageEvidenceAssociationStore,
)
from .passage_receiver import PassageEvent, PassageEventStore
from .race_metadata import (
    RaceAthleteMetadata,
    RaceMetadata,
    RaceMetadataStore,
)
from .video_playback import TargetTimelineSlider, VideoPlaybackWorker
from .video_timeline import (
    DEFAULT_CLOCK_SOURCE,
    PassageVideoLocation,
    PassageVideoLookup,
    VideoTimelineStore,
)


_STATUS_TEXT = {
    "no_segments": "没有录像时间线",
    "before_recording": "早于录像",
    "after_recording": "晚于录像",
    "recording_gap": "录像分段间隙",
    "race_mismatch": "录像属于其他赛事",
    "near_boundary": "位于时间误差边界，可打开核验",
    "recording": "对应机位仍在录像",
    "missing_file": "录像文件缺失",
    "unverified": "录像可打开，但时间范围未验证",
    "outside_media": "Passage 超出录像真实媒体范围",
}

_OPENABLE_STATUSES = {"located", "near_boundary", "unverified"}
_STATUS_PRIORITY = {
    "located": 0,
    "near_boundary": 1,
    "unverified": 2,
    "recording": 3,
    "missing_file": 4,
    "outside_media": 5,
}
BEIJING_TIMEZONE = timezone(timedelta(hours=8))
UI_FONT_FAMILY = "Microsoft YaHei UI"
UI_BASE_FONT_POINT_SIZE = 10
UI_INFO_PANEL_MIN_WIDTH = 300
UI_INFO_PANEL_MAX_WIDTH = 380
UI_INFO_PANEL_DEFAULT_WIDTH = 330


def format_passage_time(timestamp_ms: int) -> str:
    try:
        value = datetime.fromtimestamp(
            int(timestamp_ms) / 1000.0,
            tz=BEIJING_TIMEZONE,
        )
    except (OSError, OverflowError, ValueError):
        return f"{int(timestamp_ms)} ms"
    return value.strftime("%H:%M:%S.%f")[:-3]


def lookup_status_text(lookup: PassageVideoLookup) -> str:
    located = [item for item in lookup.locations if item.status == "located"]
    if located:
        uncertainty = max(item.timing_error_ms for item in located)
        nearby = [item for item in lookup.locations if item.status == "near_boundary"]
        unverified_count = sum(
            item.status == "unverified" for item in lookup.locations
        )
        suffixes = []
        if nearby:
            nearby_uncertainty = max(item.timing_error_ms for item in nearby)
            suffixes.append(
                f"另有 {len(nearby)} 个机位位于误差边界（约 ±{nearby_uncertainty} ms）"
            )
        if unverified_count:
            suffixes.append(f"另有 {unverified_count} 个机位时间范围未验证")
        suffix = f"，{'；'.join(suffixes)}" if suffixes else ""
        return f"{len(located)} 个机位，近似 ±{uncertainty} ms{suffix}"
    nearby = [item for item in lookup.locations if item.status == "near_boundary"]
    if nearby:
        uncertainty = max(item.timing_error_ms for item in nearby)
        return f"{len(nearby)} 个机位位于误差边界，约 ±{uncertainty} ms"
    unverified = [item for item in lookup.locations if item.status == "unverified"]
    if unverified:
        return f"{len(unverified)} 个机位可打开，时间范围未验证"
    return _STATUS_TEXT.get(lookup.status, lookup.status)


def is_high_speed(location: PassageVideoLocation) -> bool:
    return location.segment.clock_source != DEFAULT_CLOCK_SOURCE


def location_source_text(location: PassageVideoLocation) -> str:
    media_type = "高速摄像" if is_high_speed(location) else "普通录像"
    return (
        f"{media_type} · 机位 {location.segment.camera_index} · "
        f"{location.segment.source_id}"
    )


def location_status_text(location: PassageVideoLocation) -> str:
    position = f"{location.passage_position_ms / 1000.0:.3f} s"
    if location.status == "located":
        return f"已定位 · {position} · ±{location.timing_error_ms} ms"
    if location.status == "near_boundary":
        return f"误差边界 · {position} · ±{location.timing_error_ms} ms"
    if location.status == "unverified":
        return f"可打开 · 时间范围未验证 · {position}"
    return _STATUS_TEXT.get(location.status, location.status)


def location_clock_text(location: PassageVideoLocation) -> str:
    if location.segment.clock_source == EXTERNAL_CLOCK_SOURCE:
        return "北京时间 sidecar"
    if location.segment.clock_source == DEFAULT_CLOCK_SOURCE:
        return "复核系统时钟"
    return location.segment.clock_source


def source_location(
    lookup: PassageVideoLookup,
    *,
    high_speed: bool,
) -> Optional[PassageVideoLocation]:
    matches = [
        location
        for location in lookup.locations
        if is_high_speed(location) is bool(high_speed)
    ]
    if not matches:
        return None
    return min(
        matches,
        key=lambda location: (
            _STATUS_PRIORITY.get(location.status, 99),
            location.timing_error_ms,
            location.segment.camera_index,
        ),
    )


def compact_source_status(location: Optional[PassageVideoLocation]) -> str:
    if location is not None and location.status in _OPENABLE_STATUSES:
        return "可查看"
    return "无画面"


def source_confirmation_status(
    location: Optional[PassageVideoLocation],
    association: Optional[PassageEvidenceAssociation],
) -> str:
    if association is not None:
        return "已标记"
    return compact_source_status(location)


def combined_review_status(
    regular: Optional[PassageVideoLocation],
    high_speed: Optional[PassageVideoLocation],
) -> str:
    return "芯片记录"


class _StatusColorDelegate(QStyledItemDelegate):
    """Preserve status colors when a table row is selected."""

    def initStyleOption(self, option, index) -> None:
        super().initStyleOption(option, index)
        foreground = index.data(Qt.ForegroundRole)
        if isinstance(foreground, QBrush):
            option.palette.setBrush(QPalette.Text, foreground)
            option.palette.setBrush(QPalette.HighlightedText, foreground)


_TABLE_COLUMN_MIN_WIDTHS = (58, 70, 120, 120, 58, 140, 104, 104, 104)
_TABLE_COLUMN_EXPANSION_NUMERATOR = 6
_TABLE_COLUMN_EXPANSION_DENOMINATOR = 5


def _expanded_column_widths(
    content_widths: tuple[int, ...],
    minimum_widths: tuple[int, ...],
    available_width: int,
) -> tuple[int, ...]:
    widths = [
        max(int(content_width), int(minimum_width))
        for content_width, minimum_width in zip(content_widths, minimum_widths)
    ]
    if not widths:
        return ()
    content_total = sum(widths)
    expanded_total = (
        content_total * _TABLE_COLUMN_EXPANSION_NUMERATOR
        + _TABLE_COLUMN_EXPANSION_DENOMINATOR
        - 1
    ) // _TABLE_COLUMN_EXPANSION_DENOMINATOR
    target_width = min(int(available_width), expanded_total)
    extra_width = max(0, target_width - content_total)
    if extra_width == 0:
        return tuple(widths)

    weight_total = sum(widths)
    additions = [extra_width * width // weight_total for width in widths]
    remainder = extra_width - sum(additions)
    widest_first = sorted(range(len(widths)), key=widths.__getitem__, reverse=True)
    for index in widest_first[:remainder]:
        additions[index] += 1
    return tuple(width + addition for width, addition in zip(widths, additions))


class _AutoFitTableWidget(QTableWidget):
    """Fit columns to their content, then use the remaining viewport width."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._column_fit_scheduled = False

    def schedule_auto_fit(self) -> None:
        if self._column_fit_scheduled:
            return
        self._column_fit_scheduled = True
        QTimer.singleShot(0, self._fit_columns_to_viewport)

    def _fit_columns_to_viewport(self) -> None:
        self._column_fit_scheduled = False
        if self.columnCount() != len(_TABLE_COLUMN_MIN_WIDTHS):
            return
        self.resizeColumnsToContents()
        header = self.horizontalHeader()
        content_widths = tuple(
            header.sectionSize(column) for column in range(self.columnCount())
        )
        available_width = max(0, self.viewport().width() - 1)
        widths = _expanded_column_widths(
            content_widths,
            _TABLE_COLUMN_MIN_WIDTHS,
            available_width,
        )
        for column, width in enumerate(widths):
            header.resizeSection(column, width)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.schedule_auto_fit()


def review_status_text(
    lookup: PassageVideoLookup,
    regular: Optional[PassageVideoLocation],
    high_speed: Optional[PassageVideoLocation],
) -> str:
    return "芯片记录"


class EvidenceImageView(QGraphicsView):
    """Source-resolution image view with persistent zoom and pan state."""

    zoom_changed = pyqtSignal(int)
    full_resolution_requested = pyqtSignal()
    maximize_requested = pyqtSignal()
    marker_position_selected = pyqtSignal(float, float)
    marker_confirm_requested = pyqtSignal()
    marker_cancel_requested = pyqtSignal()
    marker_delete_requested = pyqtSignal()
    frame_step_requested = pyqtSignal(int)
    passage_step_requested = pyqtSignal(int)
    scrub_started = pyqtSignal()
    scrub_delta_requested = pyqtSignal(int)

    MIN_SCALE = 0.25
    MAX_SCALE = 8.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self._pixmap_item = QGraphicsPixmapItem()
        self._message_item = self._scene.addText("")
        self._message_item.setDefaultTextColor(QColor("#c9d2dc"))
        self._scene.addItem(self._pixmap_item)
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
        self._fit_mode = True
        self._marker_mode = False
        self._marker: Optional[tuple[float, float, str, bool]] = None
        self._marker_simple = False
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
            " border: 2px solid #ffb020;"
            " border-radius: 3px;"
            " padding: 7px 13px;"
            " font-size: 24px;"
            " font-weight: 700;"
            "}"
        )
        self._identity_badge.hide()
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
        if (
            self.has_frame
            and cache_key == self._frame_cache_key
            and expected_source_width == self._source_width
            and expected_source_height == self._source_height
        ):
            return
        pixmap = QPixmap.fromImage(image)
        if pixmap.isNull():
            return
        self._frame_cache_key = cache_key
        self._source_width = max(pixmap.width(), expected_source_width)
        self._source_height = max(pixmap.height(), expected_source_height)
        self._pixmap_item.setPixmap(pixmap)
        self._pixmap_item.setTransform(
            QTransform.fromScale(
                self._source_width / max(1, pixmap.width()),
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
        self._frame_cache_key = 0
        self._source_width = 0
        self._source_height = 0
        self.resetTransform()
        self._fit_mode = True
        self._scene.setSceneRect(
            QRectF(0, 0, max(1, self.viewport().width()), max(1, self.viewport().height()))
        )
        self._message_item.setPlainText(str(message or ""))
        self._message_item.show()
        self._position_message()
        self.clear_identity_cue()
        self.zoom_changed.emit(100)
        self.viewport().update()

    def set_identity_cue(self, identity: str, status: str) -> None:
        identity = str(identity).strip()
        if not identity or not self.has_frame:
            self.clear_identity_cue()
            return
        status = str(status).strip()
        confirmed = status == "已确认"
        reference = status == "参考"
        background_color = (
            "#1bbf83" if confirmed else "#667085" if reference else "#ffb020"
        )
        text_color = "#07120e" if confirmed else "#ffffff" if reference else "#231703"
        border_color = (
            "#4dd6a5" if confirmed else "#98a2b3" if reference else "#ffcc66"
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

    def set_marker_mode(self, enabled: bool) -> None:
        self._marker_mode = bool(enabled) and self.has_frame
        self.viewport().setCursor(
            Qt.CrossCursor if self._marker_mode else Qt.ArrowCursor
        )
        self.setToolTip(
            "单击放置判读线；左键横向拖动视频；"
            "Shift + 左键拖动判读线；中键拖动画面"
            if self._marker_mode
            else ""
        )

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
            self.zoom_by(1.2 if event.angleDelta().y() > 0 else 1.0 / 1.2)
            event.accept()
            return
        super().wheelEvent(event)

    def mousePressEvent(self, event) -> None:
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

    def mouseMoveEvent(self, event) -> None:
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
            and (event.pos() - self._mouse_press_position).manhattanLength() > 4
        ):
            self._mouse_dragged = True
        if (
            self._marker_dragging
            and event.buttons() & Qt.LeftButton
        ):
            self._select_marker_position(event.pos())
            event.accept()
            return
        if (
            self._mouse_dragged
            and self._mouse_press_position is not None
            and event.buttons() & Qt.LeftButton
        ):
            if not self._video_scrubbing:
                self._video_scrubbing = True
                self.viewport().setCursor(Qt.SizeHorCursor)
                self.scrub_started.emit()
            self.scrub_delta_requested.emit(
                event.pos().x() - self._mouse_press_position.x()
            )
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MiddleButton and self._pan_dragging:
            self._pan_dragging = False
            self._pan_last_position = None
            self.viewport().setCursor(
                Qt.CrossCursor if self._marker_mode else Qt.ArrowCursor
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
                and (event.pos() - press_position).manhattanLength() <= 4
            )
            self._mouse_press_position = None
            self._mouse_dragged = False
            self._marker_dragging = False
            video_scrubbing = self._video_scrubbing
            self._video_scrubbing = False
            self.viewport().setCursor(
                Qt.CrossCursor if self._marker_mode else Qt.ArrowCursor
            )
            if marker_dragging:
                self._select_marker_position(event.pos())
                self.setFocus(Qt.MouseFocusReason)
                event.accept()
                return
            if video_scrubbing:
                self.setFocus(Qt.MouseFocusReason)
                event.accept()
                return
            if was_click and self._marker_mode and self.has_frame:
                self._select_marker_position(event.pos())
                self.setFocus(Qt.MouseFocusReason)
            event.accept()
            return
        self._mouse_press_position = None
        self._mouse_dragged = False
        self._marker_dragging = False
        self._video_scrubbing = False
        super().mouseReleaseEvent(event)

    def _select_marker_position(self, viewport_position: QPoint) -> None:
        scene_position = self.mapToScene(viewport_position)
        if not self._scene.sceneRect().contains(scene_position):
            return
        self.marker_position_selected.emit(
            scene_position.x() / max(1, self._source_width),
            scene_position.y() / max(1, self._source_height),
        )

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self.has_frame and not self._marker_mode:
            self.maximize_requested.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key_Left, Qt.Key_Right):
            self.frame_step_requested.emit(-1 if event.key() == Qt.Key_Left else 1)
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

    def drawForeground(self, painter: QPainter, rect: QRectF) -> None:
        super().drawForeground(painter, rect)
        if self._marker is None or self._source_width <= 0 or self._source_height <= 0:
            return
        x_normalized, y_normalized, label, confirmed = self._marker
        simple = self._marker_simple
        x = x_normalized * self._source_width
        y = y_normalized * self._source_height
        scale = max(0.001, abs(self.transform().m11()))
        color = QColor("#1bbf83" if confirmed else "#ffb020")
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

    def _position_message(self) -> None:
        rect = self._message_item.boundingRect()
        scene_rect = self._scene.sceneRect()
        self._message_item.setPos(
            scene_rect.center().x() - rect.width() / 2.0,
            scene_rect.center().y() - rect.height() / 2.0,
        )

    def _position_identity_badge(self) -> None:
        self._identity_badge.move(12, 12)


class PassageEvidencePane(QFrame):
    open_requested = pyqtSignal(object, object)
    step_requested = pyqtSignal(int)
    play_requested = pyqtSignal()
    passage_delta_requested = pyqtSignal(int)
    selection_step_requested = pyqtSignal(int)
    maximize_requested = pyqtSignal(object)
    marking_requested = pyqtSignal(object)
    confirmation_requested = pyqtSignal(object)
    cancel_requested = pyqtSignal(object)
    delete_requested = pyqtSignal(object)
    scrub_started = pyqtSignal()

    MAX_SCRUB_SPAN_MS = 6_000

    def __init__(
        self,
        title: str,
        source_kind: str,
        parent=None,
        *,
        camera_index: int = 0,
    ):
        super().__init__(parent)
        if source_kind not in {REGULAR_SOURCE, HIGH_SPEED_SOURCE}:
            raise ValueError("source_kind must be regular or high_speed")
        self.source_kind = source_kind
        self.camera_index = max(0, int(camera_index))
        self._event: Optional[PassageEvent] = None
        self._location: Optional[PassageVideoLocation] = None
        self._association: Optional[PassageEvidenceAssociation] = None
        self._reference_only = False
        self._pending_marker: Optional[tuple[float, float, int, int]] = None
        self._marking_enabled = False
        self._identity = ""
        self._worker: Optional[object] = None
        self._retired_workers: set[object] = set()
        self._target_position_ms = 0
        self._playing = False
        self._duration_ms = 0
        self._fps = 0.0
        self._source_width = 0
        self._source_height = 0
        self._current_frame_index = -1
        self._current_position_ms = 0
        self._timeline_dragging = False
        self._last_full_resolution_request = -1
        self._scrub_origin_delta_ms = 0

        self.setObjectName("passageEvidencePane")
        self.setFrameShape(QFrame.StyledPanel)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        header = QHBoxLayout()
        self.title_label = QLabel(title)
        self.title_label.setObjectName("evidencePaneTitle")
        self.camera_combo = QComboBox(self)
        self.camera_combo.setMinimumWidth(88)
        self.camera_combo.setToolTip("切换普通录像机位")
        self.camera_combo.hide()
        self.status_label = QLabel("未选择通过记录")
        self.status_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.status_label.setObjectName("evidencePaneStatus")
        header.addWidget(self.title_label)
        header.addWidget(self.camera_combo)
        header.addStretch()
        header.addWidget(self.status_label)
        layout.addLayout(header)

        self.video_view = EvidenceImageView(self)
        self.video_label = self.video_view
        self.video_view.clear_frame("选择一条通过记录后自动定位")
        self.video_view.zoom_changed.connect(self._on_zoom_changed)
        self.video_view.full_resolution_requested.connect(
            self._request_full_resolution
        )
        self.video_view.maximize_requested.connect(
            lambda: self.maximize_requested.emit(self)
        )
        self.video_view.marker_position_selected.connect(
            self._on_marker_position_selected
        )
        self.video_view.marker_confirm_requested.connect(
            lambda: self.confirmation_requested.emit(self)
        )
        self.video_view.marker_cancel_requested.connect(
            lambda: self.cancel_requested.emit(self)
        )
        self.video_view.marker_delete_requested.connect(
            lambda: self.delete_requested.emit(self)
        )
        self.video_view.frame_step_requested.connect(self.step_requested.emit)
        self.video_view.passage_step_requested.connect(
            self.selection_step_requested.emit
        )
        self.video_view.scrub_started.connect(self._on_video_scrub_started)
        self.video_view.scrub_delta_requested.connect(
            self._on_video_scrub_delta
        )
        layout.addWidget(self.video_view, 1)

        self.timeline = TargetTimelineSlider(Qt.Horizontal, self)
        self.timeline.setRange(0, 0)
        self.timeline.setEnabled(False)
        self.timeline.sliderPressed.connect(self._on_timeline_pressed)
        self.timeline.sliderReleased.connect(self._on_timeline_released)
        layout.addWidget(self.timeline)

        controls = QHBoxLayout()
        controls.setSpacing(6)
        self.controls_layout = controls
        self.previous_frame_btn = QPushButton("|◀")
        self.previous_frame_btn.setToolTip("上一帧")
        self.play_btn = QPushButton("▶")
        self.play_btn.setToolTip("播放或暂停")
        self.next_frame_btn = QPushButton("▶|")
        self.next_frame_btn.setToolTip("下一帧")
        self.time_label = QLabel("--:--:--.---")
        self.time_label.setObjectName("evidencePaneTime")
        self.zoom_out_btn = QPushButton("−")
        self.zoom_out_btn.setToolTip("缩小")
        self.actual_size_btn = QPushButton("100%")
        self.actual_size_btn.setToolTip("按原始像素显示")
        self.fit_btn = QPushButton("适应")
        self.fit_btn.setToolTip("适应当前窗格")
        self.zoom_in_btn = QPushButton("+")
        self.zoom_in_btn.setToolTip("放大")
        self.maximize_btn = QPushButton("□")
        self.maximize_btn.setToolTip("最大化或恢复该机位")
        self.mark_btn = QPushButton("标记")
        self.mark_btn.setToolTip("在画面中按住左键移动身份判读线")
        self.open_btn = QPushButton("定点回放")
        self.open_btn.setToolTip("回看当前目标点前 45 秒、后 15 秒")
        self.open_btn.setVisible(self.source_kind == REGULAR_SOURCE)
        self.open_btn.setEnabled(False)
        controls.addWidget(self.previous_frame_btn)
        controls.addWidget(self.play_btn)
        controls.addWidget(self.next_frame_btn)
        controls.addWidget(self.time_label)
        controls.addStretch()
        controls.addWidget(self.zoom_out_btn)
        controls.addWidget(self.actual_size_btn)
        controls.addWidget(self.fit_btn)
        controls.addWidget(self.zoom_in_btn)
        controls.addWidget(self.maximize_btn)
        controls.addWidget(self.mark_btn)
        controls.addWidget(self.open_btn)
        layout.addLayout(controls)

        self.previous_frame_btn.clicked.connect(lambda: self.step_requested.emit(-1))
        self.play_btn.clicked.connect(self.play_requested.emit)
        self.next_frame_btn.clicked.connect(lambda: self.step_requested.emit(1))
        self.zoom_out_btn.clicked.connect(lambda: self.video_view.zoom_by(1.0 / 1.2))
        self.actual_size_btn.clicked.connect(self.video_view.set_actual_size)
        self.fit_btn.clicked.connect(self.video_view.fit_to_window)
        self.zoom_in_btn.clicked.connect(lambda: self.video_view.zoom_by(1.2))
        self.maximize_btn.clicked.connect(lambda: self.maximize_requested.emit(self))
        self.mark_btn.clicked.connect(lambda: self.marking_requested.emit(self))
        self.open_btn.clicked.connect(self._request_open)
        self._set_transport_enabled(False)

    def set_compact_controls(self, compact: bool) -> None:
        compact = bool(compact)
        for button in (
            self.previous_frame_btn,
            self.play_btn,
            self.next_frame_btn,
            self.zoom_out_btn,
            self.actual_size_btn,
            self.zoom_in_btn,
        ):
            button.setVisible(not compact)
        self.open_btn.setText("回放" if compact else "定点回放")
        self.controls_layout.setSpacing(4 if compact else 6)

    @property
    def location(self) -> Optional[PassageVideoLocation]:
        return self._location

    @property
    def association(self) -> Optional[PassageEvidenceAssociation]:
        return self._association

    @property
    def has_pending_marker(self) -> bool:
        return self._pending_marker is not None

    @property
    def is_auyat_rgb(self) -> bool:
        return bool(
            self._location is not None
            and self._location.segment.clock_source == AUYAT_CLOCK_SOURCE
        )

    def matches_passage_context(
        self,
        event: PassageEvent,
        location: Optional[PassageVideoLocation],
    ) -> bool:
        return (
            self._event is not None
            and self._event.event_id == event.event_id
            and self._location == location
        )

    def begin_marking(self) -> None:
        if self._worker is None or not self.video_view.has_frame:
            return
        self._marking_enabled = True
        self._playing = False
        self.play_btn.setText("▶")
        self._worker.pause()
        self.mark_btn.setText(f"拖动标线 {self._identity}")
        self.video_view.set_marker_mode(True)
        self.video_view.setFocus(Qt.ShortcutFocusReason)

    def cancel_marker_edit(self) -> None:
        self._pending_marker = None
        self._marking_enabled = self._association is None
        self.mark_btn.setText(
            f"重标 {self._identity}" if self._association is not None else f"标线 {self._identity}"
        )
        self.video_view.set_marker_mode(
            self.video_view.has_frame and self._marking_enabled
        )
        self._render_marker()

    def set_association(
        self,
        association: Optional[PassageEvidenceAssociation],
    ) -> None:
        self._association = association
        if association is not None:
            self._reference_only = False
        self._pending_marker = None
        self._marking_enabled = association is None
        self.video_view.set_marker_mode(
            self.video_view.has_frame and self._marking_enabled
        )
        self.mark_btn.setText(
            f"重标 {self._identity}" if association is not None else f"标线 {self._identity}"
        )
        self._update_status_label()
        self._render_marker()

    def set_reference_only(self, reference_only: bool) -> None:
        next_value = bool(reference_only) and self._association is None
        if next_value == self._reference_only:
            return
        self._reference_only = next_value
        self._update_status_label()
        self._render_marker()

    def pending_confirmation(self) -> Optional[dict[str, object]]:
        location = self._location
        marker = self._pending_marker
        if location is None or marker is None:
            return None
        x_normalized, y_normalized, frame_index, position_ms = marker
        return {
            "segment_id": location.segment.segment_id,
            "frame_index": frame_index,
            "position_ms": position_ms,
            "marker_x_normalized": x_normalized,
            "marker_y_normalized": y_normalized,
        }

    def _on_marker_position_selected(
        self,
        x_normalized: float,
        y_normalized: float,
    ) -> None:
        if self._current_frame_index < 0:
            return
        self.marking_requested.emit(self)
        frame_index = self._current_frame_index
        position_ms = self._current_position_ms
        worker = self._worker
        if self.is_auyat_rgb and worker is not None:
            mapped_position = worker.position_ms_for_x(x_normalized)
            if mapped_position is not None:
                position_ms = int(mapped_position)
                frame_index = max(
                    0,
                    min(
                        self._source_width - 1,
                        int(round(float(x_normalized) * (self._source_width - 1))),
                    ),
                )
                self.passage_delta_requested.emit(
                    position_ms - self._target_position_ms
                )
        self._pending_marker = (
            float(x_normalized),
            float(y_normalized),
            frame_index,
            position_ms,
        )
        self._render_marker()

    def _update_status_label(self) -> None:
        status = compact_source_status(self._location)
        if self._location is not None:
            status = location_status_text(self._location)
        if self._association is not None:
            status = f"{status} · 已标记 {self._identity}"
        elif self._reference_only:
            status = f"{status} · 参考"
        self.status_label.setText(status)

    def _render_marker(self) -> None:
        marker = self._pending_marker
        if self.is_auyat_rgb:
            self.video_view.clear_identity_cue()
        else:
            if marker is not None:
                cue_status = "待确认"
            elif self._association is not None:
                cue_status = "已确认"
            elif self._reference_only:
                cue_status = "参考"
            else:
                cue_status = "待判读"
            self.video_view.set_identity_cue(self._identity, cue_status)
        if marker is not None:
            self.video_view.set_marker(
                marker[0],
                marker[1],
                self._identity,
                confirmed=False,
                simple=self.is_auyat_rgb,
            )
            return
        association = self._association
        if (
            association is not None
            and self._association_is_visible(association)
        ):
            self.video_view.set_marker(
                association.marker_x_normalized,
                association.marker_y_normalized,
                self._identity,
                confirmed=True,
                simple=self.is_auyat_rgb,
            )
            return
        if self.is_auyat_rgb and self._current_frame_index >= 0:
            worker = self._worker
            marker_x = (
                worker.x_for_position_ms(self._current_position_ms)
                if worker is not None
                else None
            )
            if marker_x is not None:
                self.video_view.set_marker(
                    marker_x,
                    0.5,
                    self._identity,
                    confirmed=False,
                    simple=True,
                )
                return
        self.video_view.clear_marker()

    def _association_is_visible(
        self,
        association: PassageEvidenceAssociation,
    ) -> bool:
        if self._current_frame_index < 0:
            return False
        if self._current_frame_index == association.frame_index:
            return True
        return (
            abs(self._current_position_ms - association.position_ms)
            <= self.frame_duration_ms()
        )

    def set_passage(
        self,
        event: PassageEvent,
        location: Optional[PassageVideoLocation],
        association: Optional[PassageEvidenceAssociation] = None,
        *,
        initial_delta_ms: int = 0,
    ) -> None:
        previous_context = self._media_context(self._location)
        next_context = self._media_context(location)
        same_context = bool(previous_context) and previous_context == next_context
        self._event = event
        self._location = location
        self._reference_only = False
        self._identity = event.bib.strip() or "未知"
        if (
            association is not None
            and location is not None
            and association.segment_id != location.segment.segment_id
        ):
            association = None
        self._association = association
        self._pending_marker = None
        self._marking_enabled = association is None
        self._playing = False
        self._current_frame_index = -1
        self._current_position_ms = 0
        self._last_full_resolution_request = -1
        self.play_btn.setText("▶")
        self.mark_btn.setText(
            f"重标 {self._identity}" if association is not None else f"标线 {self._identity}"
        )
        self.video_view.set_marker_mode(False)
        self.video_view.clear_marker()
        if not same_context:
            self.video_view.reset_view()
        if (
            location is None
            or location.status not in _OPENABLE_STATUSES
            or not location.video_path.is_file()
        ):
            self._stop_worker()
            self._duration_ms = 0
            self._fps = 0.0
            self._source_width = 0
            self._source_height = 0
            self.timeline.setRange(0, 0)
            self.timeline.setEnabled(False)
            self._target_position_ms = 0
            status = compact_source_status(location)
            self.status_label.setText(status)
            self.video_label.clear_frame(status)
            self.time_label.setText("--:--:--.---")
            self._set_transport_enabled(False)
            return

        self._target_position_ms = int(location.passage_position_ms)
        self._update_status_label()
        if not same_context:
            self.video_label.clear_frame(f"正在定位 {self._identity} 号...")
        self.time_label.setText(f"目标 {self._target_position_ms / 1000.0:.3f} s")
        initial_position_ms = max(0, self._target_position_ms + int(initial_delta_ms))

        worker = self._worker
        if (
            worker is not None
            and worker.isRunning()
            and worker.video_path == Path(location.video_path)
            and str(getattr(worker, "media_locator", ""))
            == str(location.media_locator)
        ):
            worker.pause()
            self.timeline.set_target_position(self._target_position_ms)
            self.timeline.setValue(min(initial_position_ms, self._duration_ms))
            self.timeline.setEnabled(self._duration_ms > 0)
            worker.seek(min(initial_position_ms, self._duration_ms))
            self._set_transport_enabled(True)
            return

        self._stop_worker()
        self._duration_ms = 0
        self._fps = 0.0
        self._source_width = 0
        self._source_height = 0
        self.timeline.setRange(0, 0)
        self.timeline.setEnabled(False)
        self.timeline.setProperty("initial_delta_ms", int(initial_delta_ms))
        worker = (
            AuyatRgbPlaybackWorker(location, self)
            if location.segment.clock_source == AUYAT_CLOCK_SOURCE
            else VideoPlaybackWorker(location.video_path, self)
        )
        worker.pause()
        worker.metadata_ready.connect(self._on_metadata_ready)
        worker.frame_ready.connect(self._on_frame_ready)
        worker.full_resolution_ready.connect(self._on_full_resolution_ready)
        worker.playback_finished.connect(self._on_playback_finished)
        worker.playback_error.connect(self._on_playback_error)
        self._worker = worker
        worker.start()

    @staticmethod
    def _media_context(
        location: Optional[PassageVideoLocation],
    ) -> tuple[str, str]:
        if location is None:
            return ("", "")
        return (
            str(Path(location.video_path).absolute()),
            str(location.media_locator or location.segment.segment_id),
        )

    def clear_passage(self, message: str = "没有通过记录") -> None:
        self._stop_worker()
        self._event = None
        self._location = None
        self._association = None
        self._reference_only = False
        self._pending_marker = None
        self._marking_enabled = False
        self._identity = ""
        self._target_position_ms = 0
        self._playing = False
        self._duration_ms = 0
        self._fps = 0.0
        self._source_width = 0
        self._source_height = 0
        self._current_frame_index = -1
        self._current_position_ms = 0
        self.timeline.setRange(0, 0)
        self.timeline.setEnabled(False)
        self.play_btn.setText("▶")
        self.mark_btn.setText("标记")
        self.status_label.setText("未选择通过记录")
        self.time_label.setText("--:--:--.---")
        self.video_view.set_marker_mode(False)
        self.video_view.clear_marker()
        self.video_label.clear_frame(message)
        self._set_transport_enabled(False)

    def _on_metadata_ready(
        self,
        duration_ms: int,
        fps: float,
        width: int,
        height: int,
        _frame_count: int,
    ) -> None:
        if self.sender() is not self._worker:
            return
        self._duration_ms = max(0, int(duration_ms))
        self._fps = max(0.0, float(fps))
        self._source_width = max(0, int(width))
        self._source_height = max(0, int(height))
        initial_delta_ms = int(self.timeline.property("initial_delta_ms") or 0)
        target_ms = min(
            max(0, self._target_position_ms + initial_delta_ms),
            max(0, duration_ms),
        )
        self.timeline.setRange(0, self._duration_ms)
        self.timeline.set_target_position(self._target_position_ms)
        self.timeline.setValue(target_ms)
        self.timeline.setEnabled(self._duration_ms > 0)
        self._set_transport_enabled(True)
        self._worker.seek(target_ms)

    def _on_frame_ready(self, image, position_ms: int, frame_index: int) -> None:
        if self.sender() is not self._worker:
            return
        previous_frame_index = self._current_frame_index
        self._current_frame_index = int(frame_index)
        self._current_position_ms = int(position_ms)
        if (
            self._pending_marker is not None
            and previous_frame_index >= 0
            and self._current_frame_index != self._pending_marker[2]
        ):
            self.cancel_marker_edit()
        self.video_view.set_frame(
            image,
            source_width=self._source_width,
            source_height=self._source_height,
        )
        self.mark_btn.setEnabled(True)
        self.video_view.set_marker_mode(self._marking_enabled)
        self._render_marker()
        if not self._timeline_dragging:
            self.timeline.setValue(max(0, min(int(position_ms), self._duration_ms)))
        self._update_time_label(position_ms)
        if not self._playing and self.video_view.zoom_percent >= 100:
            self._request_full_resolution()

    def _on_full_resolution_ready(
        self,
        image,
        position_ms: int,
        frame_index: int,
    ) -> None:
        if self.sender() is not self._worker:
            return
        if int(frame_index) != self._current_frame_index:
            return
        self.video_view.set_frame(
            image,
            source_width=self._source_width,
            source_height=self._source_height,
        )
        self._current_position_ms = int(position_ms)
        self._render_marker()
        self._update_time_label(position_ms)

    def _update_time_label(self, position_ms: int) -> None:
        event = self._event
        if event is None:
            return
        delta_ms = int(position_ms) - self._target_position_ms
        current_timestamp_ms = event.timeline_timestamp_ms + delta_ms
        self.time_label.setText(
            f"{format_passage_time(current_timestamp_ms)}  Δ{delta_ms:+d} ms"
        )
        self.time_label.setToolTip(
            f"文件位置 {position_ms / 1000.0:.3f} s；"
            f"Passage 目标 {format_passage_time(event.timeline_timestamp_ms)}"
        )

    def _request_full_resolution(self) -> None:
        worker = self._worker
        frame_index = self._current_frame_index
        if worker is None or frame_index < 0:
            return
        if frame_index == self._last_full_resolution_request:
            return
        self._last_full_resolution_request = frame_index
        worker.request_full_resolution(frame_index)

    def _on_zoom_changed(self, percent: int) -> None:
        self.actual_size_btn.setText(f"{int(percent)}%")

    def _on_timeline_pressed(self) -> None:
        self._timeline_dragging = True

    def _on_timeline_released(self) -> None:
        self._timeline_dragging = False
        self.passage_delta_requested.emit(
            int(self.timeline.value()) - self._target_position_ms
        )

    def _on_video_scrub_started(self) -> None:
        self._scrub_origin_delta_ms = (
            self._current_position_ms - self._target_position_ms
        )
        self.scrub_started.emit()

    def _on_video_scrub_delta(self, horizontal_pixels: int) -> None:
        viewport_width = max(1, self.video_view.viewport().width())
        frame_ms = self.frame_duration_ms()
        scrub_span_ms = max(
            frame_ms,
            min(self._duration_ms, self.MAX_SCRUB_SPAN_MS),
        )
        raw_delta_ms = (
            int(horizontal_pixels) * scrub_span_ms / viewport_width
        )
        frame_delta = int(round(raw_delta_ms / frame_ms))
        self.passage_delta_requested.emit(
            self._scrub_origin_delta_ms + frame_delta * frame_ms
        )

    def _on_playback_finished(self) -> None:
        if self.sender() is not self._worker:
            return
        self._playing = False
        self.play_btn.setText("▶")

    def _on_playback_error(self, message: str) -> None:
        if self.sender() is not self._worker:
            return
        self._playing = False
        self.play_btn.setText("▶")
        self.status_label.setText("打开失败")
        self.video_label.clear_frame(message)
        self._set_transport_enabled(False)
        self.open_btn.setEnabled(
            self.source_kind == REGULAR_SOURCE
            and self._location is not None
            and self._location.video_path.is_file()
        )

    def set_playing(self, playing: bool) -> None:
        worker = self._worker
        if worker is None:
            return
        self._playing = bool(playing)
        if self._playing:
            worker.set_shuttle_speed(1.0)
            self.play_btn.setText("Ⅱ")
        else:
            worker.pause()
            self.play_btn.setText("▶")

    def toggle_playing(self) -> None:
        self.set_playing(not self._playing)

    def step(self, frame_delta: int) -> None:
        worker = self._worker
        if worker is None:
            return
        worker.step(frame_delta)
        self._playing = False
        self.play_btn.setText("▶")

    def frame_duration_ms(self) -> int:
        if self._fps <= 0.1:
            return 40
        return max(1, int(round(1000.0 / self._fps)))

    def available_delta_bounds(self) -> Optional[tuple[int, int]]:
        if self._worker is None or self._duration_ms <= 0:
            return None
        return (
            -self._target_position_ms,
            self._duration_ms - self._target_position_ms,
        )

    def seek_passage_delta(
        self,
        delta_ms: int,
        *,
        linked_playing: bool = False,
    ) -> None:
        worker = self._worker
        if worker is None:
            return
        bounds = self.available_delta_bounds()
        if bounds is None:
            return
        lower, upper = bounds
        clamped_delta_ms = max(lower, min(int(delta_ms), upper))
        worker.pause()
        worker.seek(self._target_position_ms + clamped_delta_ms)
        self._playing = bool(linked_playing)
        self.play_btn.setText("Ⅱ" if self._playing else "▶")

    def set_linked_playing(self, playing: bool) -> None:
        self._playing = bool(playing)
        worker = self._worker
        if worker is not None:
            worker.pause()
        self.play_btn.setText("Ⅱ" if self._playing else "▶")

    def _set_transport_enabled(self, enabled: bool) -> None:
        self.previous_frame_btn.setEnabled(enabled)
        self.play_btn.setEnabled(enabled)
        self.next_frame_btn.setEnabled(enabled)
        self.mark_btn.setEnabled(enabled and self.video_view.has_frame)
        self.open_btn.setEnabled(
            enabled
            and self.source_kind == REGULAR_SOURCE
            and self._location is not None
            and self._location.status in _OPENABLE_STATUSES
            and self._location.segment.clock_source != AUYAT_CLOCK_SOURCE
        )

    def _request_open(self) -> None:
        if self._event is not None and self._location is not None:
            self.open_requested.emit(self._event, self._location)

    def _retire_worker(
        self,
        worker: VideoPlaybackWorker,
        *,
        wait: bool,
    ) -> None:
        if not worker.isRunning():
            worker.deleteLater()
            return
        if worker not in self._retired_workers:
            self._retired_workers.add(worker)
            worker.finished.connect(
                lambda retired=worker: self._dispose_worker(retired)
            )
        worker.stop()
        if wait:
            worker.wait(2_000)
        if not worker.isRunning():
            self._dispose_worker(worker)

    def _dispose_worker(self, worker: VideoPlaybackWorker) -> None:
        if worker not in self._retired_workers:
            return
        self._retired_workers.discard(worker)
        worker.deleteLater()

    def _stop_worker(self, *, wait: bool = False) -> None:
        worker = self._worker
        self._worker = None
        if worker is None:
            return
        self._retire_worker(worker, wait=wait)

    def shutdown(self, *, wait: bool = True) -> None:
        self._stop_worker(wait=wait)
        for worker in tuple(self._retired_workers):
            self._retire_worker(worker, wait=wait)


def _resolve_evidence_layout(
    timeline_store: VideoTimelineStore,
    *,
    regular_camera_indexes: Optional[Iterable[int]] = None,
    show_high_speed_pane: Optional[bool] = None,
    include_recorded: bool = True,
) -> tuple[tuple[int, ...], bool]:
    configured_indexes = tuple(
        dict.fromkeys(
            max(1, int(camera_index))
            for camera_index in (regular_camera_indexes or ())
        )
    )
    recorded_regular_indexes: tuple[int, ...] = ()
    recorded_high_speed = False
    if include_recorded:
        segments = timeline_store.segments()
        recorded_regular_indexes = tuple(
            sorted(
                {
                    segment.camera_index
                    for segment in segments
                    if segment.clock_source == DEFAULT_CLOCK_SOURCE
                }
            )
        )
        recorded_high_speed = any(
            segment.clock_source != DEFAULT_CLOCK_SOURCE for segment in segments
        )
    regular_indexes = tuple(
        dict.fromkeys((*configured_indexes, *recorded_regular_indexes))
    ) or (1,)
    show_high_speed = bool(show_high_speed_pane) or recorded_high_speed
    return regular_indexes, show_high_speed


class PassageReviewDialog(QDialog):
    clock_offset_changed = pyqtSignal(int)
    evidence_pane_added = pyqtSignal(object)

    def __init__(
        self,
        passage_store: PassageEventStore,
        timeline_store: VideoTimelineStore,
        parent=None,
        *,
        clock_offset_ms: int = 0,
        pre_roll_ms: int = 3_000,
        association_store: Optional[PassageEvidenceAssociationStore] = None,
        metadata_store: Optional[RaceMetadataStore] = None,
        open_location: Optional[
            Callable[[PassageEvent, PassageVideoLocation], None]
        ] = None,
        high_speed_locator: Optional[
            Callable[[PassageEvent, int, int], Optional[PassageVideoLocation]]
        ] = None,
        regular_camera_indexes: Optional[Iterable[int]] = None,
        show_high_speed_pane: Optional[bool] = None,
        include_recorded_evidence: bool = True,
    ):
        super().__init__(parent)
        self.passage_store = passage_store
        self.timeline_store = timeline_store
        self.association_store = association_store or PassageEvidenceAssociationStore(
            passage_store.journal_path.with_name("passage_evidence_associations.jsonl")
        )
        self.metadata_store = metadata_store
        self.clock_offset_ms = int(clock_offset_ms)
        self.pre_roll_ms = max(0, int(pre_roll_ms))
        self._open_location = open_location
        self._high_speed_locator = high_speed_locator
        requested_high_speed = (
            bool(high_speed_locator)
            if show_high_speed_pane is None
            else bool(show_high_speed_pane)
        )
        (
            self._configured_regular_camera_indexes,
            self._show_high_speed_pane,
        ) = _resolve_evidence_layout(
            timeline_store,
            regular_camera_indexes=regular_camera_indexes,
            show_high_speed_pane=requested_high_speed,
            include_recorded=include_recorded_evidence,
        )
        self._legacy_camera_selector = regular_camera_indexes is None
        self._preferred_regular_camera_index = 0
        self._include_recorded_evidence = bool(include_recorded_evidence)
        self._regular_panes_by_camera: dict[int, PassageEvidencePane] = {}
        self._external_location_revision = 0
        self._visible_events: list[PassageEvent] = []
        self._lookups: dict[str, PassageVideoLookup] = {}
        self._lookup_cache: dict[str, tuple[tuple, PassageVideoLookup]] = {}
        self._timeline_signature: tuple = ()
        self._selected_event_id = ""
        self._shared_delta_ms = 0
        self._sync_playing = False
        self._sync_origin_delta_ms = 0
        self._sync_started_at = 0.0
        self._sync_timer = QTimer(self)
        self._sync_timer.setInterval(30)
        self._sync_timer.timeout.connect(self._on_sync_tick)
        self._maximized_pane: Optional[PassageEvidencePane] = None
        self._available_evidence_count = 0
        self._located_event_ids: set[str] = set()
        self._confirmed_event_ids: set[str] = set()
        self._metadata_context_key: tuple[str, str] = ("", "")

        self.setWindowTitle("终点多源核对")
        self.resize(1400, 860)
        self.setMinimumSize(1100, 700)
        self._init_ui()
        self.space_shortcut = QShortcut(QKeySequence(Qt.Key_Space), self)
        self.space_shortcut.setContext(Qt.WindowShortcut)
        self.space_shortcut.activated.connect(self._toggle_both)
        self.previous_passage_shortcut = QShortcut(
            QKeySequence(Qt.Key_PageUp), self
        )
        self.previous_passage_shortcut.setContext(Qt.WindowShortcut)
        self.previous_passage_shortcut.activated.connect(
            lambda: self._move_selection(-1)
        )
        self.next_passage_shortcut = QShortcut(
            QKeySequence(Qt.Key_PageDown), self
        )
        self.next_passage_shortcut.setContext(Qt.WindowShortcut)
        self.next_passage_shortcut.activated.connect(
            lambda: self._move_selection(1)
        )
        self.refresh()

    def _init_ui(self) -> None:
        self.setStyleSheet(
            f'QDialog {{ background: #e9eef3; color: #17212b; '
            f'font-family: "{UI_FONT_FAMILY}"; '
            f'font-size: {UI_BASE_FONT_POINT_SIZE}pt; }}'
            "QFrame#reviewPanel, QFrame#passageEvidencePane { background: #ffffff; "
            "border: 1px solid #cfd7df; border-radius: 4px; }"
            "QLabel#panelTitle, QLabel#evidencePaneTitle { font-size: 11pt; font-weight: 700; }"
            "QLabel#evidencePaneStatus { color: #667085; font-size: 9pt; }"
            "QLabel#evidencePaneTime { font-family: Consolas; font-size: 10pt; font-weight: 700; }"
            "QPushButton { min-height: 30px; padding: 0 10px; font-size: 10pt; "
            "border: 1px solid #aeb8c2; "
            "border-radius: 4px; background: #ffffff; }"
            "QPushButton:hover { background: #eef5fa; border-color: #5d91b5; }"
            "QPushButton:disabled { color: #9ba5ae; background: #f4f6f8; }"
            "QCheckBox, QComboBox, QLineEdit, QSpinBox { font-size: 10pt; }"
            "QTableWidget { background: #ffffff; gridline-color: #d8dee5; "
            "alternate-background-color: #f8fafb; font-size: 10pt; }"
            "QHeaderView::section { background: #eef2f5; color: #526170; "
            "font-size: 10pt; font-weight: 600; padding: 5px; "
            "border: none; border-right: 1px solid #d5dce3; border-bottom: 1px solid #c8d1da; }"
            "QTableWidget::item:selected { background: #dcecf8; }"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        upper_splitter = QSplitter(Qt.Horizontal)
        upper_splitter.setChildrenCollapsible(False)
        upper_splitter.setHandleWidth(5)

        self.info_panel = QFrame(self)
        self.info_panel.setObjectName("reviewPanel")
        self.info_panel.setMinimumWidth(UI_INFO_PANEL_MIN_WIDTH)
        self.info_panel.setMaximumWidth(UI_INFO_PANEL_MAX_WIDTH)
        info_layout = QVBoxLayout(self.info_panel)
        info_layout.setContentsMargins(12, 10, 12, 10)
        info_layout.setSpacing(8)
        title = QLabel("赛事与组别")
        title.setObjectName("panelTitle")
        info_layout.addWidget(title)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignLeft)
        form.setFormAlignment(Qt.AlignTop)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)
        self.race_value = QLabel("--")
        self.stage_value = QLabel("--")
        self.group_value = QLabel("--")
        self.selected_identity_value = QLabel("--")
        self.athlete_value = QLabel("--")
        self.team_value = QLabel("--")
        self.selected_time_value = QLabel("--")
        self.selected_time_value.setStyleSheet("font-family: Consolas; font-weight: 700;")
        self.source_value = QLabel("--")
        self.source_value.setWordWrap(True)
        form.addRow("赛事", self.race_value)
        form.addRow("赛段", self.stage_value)
        form.addRow("当前组别", self.group_value)
        form.addRow("号码", self.selected_identity_value)
        form.addRow("姓名", self.athlete_value)
        form.addRow("队伍", self.team_value)
        form.addRow("通过时间", self.selected_time_value)
        form.addRow("证据状态", self.source_value)
        info_layout.addLayout(form)
        info_layout.addStretch()
        authority = QLabel("通过时间只读；正式成绩由 CycleRace 计算")
        authority.setWordWrap(True)
        authority.setStyleSheet(
            "background: #e8f2fa; color: #15547f; padding: 7px; border-radius: 3px;"
        )
        info_layout.addWidget(authority)
        upper_splitter.addWidget(self.info_panel)

        results_panel = QFrame(self)
        results_panel.setObjectName("reviewPanel")
        results_layout = QVBoxLayout(results_panel)
        results_layout.setContentsMargins(10, 8, 10, 10)
        results_layout.setSpacing(7)
        filters = QHBoxLayout()
        results_title = QLabel("通过记录与证据匹配")
        results_title.setObjectName("panelTitle")
        filters.addWidget(results_title)
        filters.addStretch()
        filters.addWidget(QLabel("组别"))
        self.group_combo = QComboBox(self)
        self.group_combo.setMinimumWidth(145)
        self.group_combo.currentIndexChanged.connect(self._on_group_changed)
        filters.addWidget(self.group_combo)
        self.identity_search = QLineEdit(self)
        self.identity_search.setPlaceholderText("输入号码或姓名")
        self.identity_search.setClearButtonEnabled(True)
        self.identity_search.setMaximumWidth(170)
        self.identity_search.returnPressed.connect(self._find_identity)
        filters.addWidget(self.identity_search)
        find_btn = QPushButton("定位")
        find_btn.clicked.connect(self._find_identity)
        filters.addWidget(find_btn)
        filters.addWidget(QLabel("时钟偏移"))
        self.offset_spin = QSpinBox(self)
        self.offset_spin.setRange(-600_000, 600_000)
        self.offset_spin.setSingleStep(100)
        self.offset_spin.setSuffix(" ms")
        self.offset_spin.setMinimumWidth(115)
        self.offset_spin.setValue(self.clock_offset_ms)
        self.offset_spin.setToolTip("复核系统时间 = CycleRace passage 时间 + 此偏移")
        self.offset_spin.valueChanged.connect(self._on_offset_changed)
        filters.addWidget(self.offset_spin)
        refresh_btn = QPushButton("刷新")
        refresh_btn.clicked.connect(self.refresh)
        filters.addWidget(refresh_btn)
        results_layout.addLayout(filters)

        self.table = _AutoFitTableWidget(0, 9, self)
        self.table.setHorizontalHeaderLabels(
            [
                "序号",
                "号码",
                "姓名",
                "组别",
                "圈次",
                "通过时间",
                "普通录像",
                "高速摄像",
                "核对状态",
            ]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        table_palette = self.table.palette()
        table_palette.setColor(QPalette.HighlightedText, QColor("#17212b"))
        self.table.setPalette(table_palette)
        self.table.verticalHeader().setDefaultSectionSize(34)
        for column in (6, 7, 8):
            self.table.setItemDelegateForColumn(
                column,
                _StatusColorDelegate(self.table),
            )
        self.table.verticalHeader().setVisible(False)
        self.table.itemSelectionChanged.connect(self._on_table_selection_changed)
        self.table.cellDoubleClicked.connect(self._open_preferred_source)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)
        results_layout.addWidget(self.table, 1)
        upper_splitter.addWidget(results_panel)
        upper_splitter.setStretchFactor(0, 0)
        upper_splitter.setStretchFactor(1, 1)
        upper_splitter.setSizes([UI_INFO_PANEL_DEFAULT_WIDTH, 1000])
        layout.addWidget(upper_splitter, 4)

        transport = QFrame(self)
        transport.setObjectName("reviewPanel")
        transport_layout = QHBoxLayout(transport)
        transport_layout.setContentsMargins(10, 5, 10, 5)
        transport_layout.setSpacing(7)
        self.current_passage_label = QLabel("未选择通过记录")
        self.current_passage_label.setStyleSheet(
            "font-size: 12pt; font-weight: 700; color: #17212b;"
        )
        self.current_time_label = QLabel("--:--:--.---")
        self.current_time_label.setStyleSheet(
            "font-family: Consolas; font-size: 10pt; font-weight: 700;"
        )
        self.previous_passage_btn = QPushButton("上一条")
        self.previous_frame_btn = QPushButton("|◀")
        self.play_both_btn = QPushButton("▶")
        self.next_frame_btn = QPushButton("▶|")
        self.next_passage_btn = QPushButton("下一条")
        self.auto_advance_checkbox = QCheckBox("确认后下一条")
        self.auto_advance_checkbox.setChecked(False)
        self.auto_advance_checkbox.setToolTip(
            "开启后，当前可用录像均确认时自动定位下一条 passage"
        )
        self.previous_passage_btn.clicked.connect(lambda: self._move_selection(-1))
        self.previous_frame_btn.clicked.connect(lambda: self._step_both(-1))
        self.play_both_btn.clicked.connect(self._toggle_both)
        self.next_frame_btn.clicked.connect(lambda: self._step_both(1))
        self.next_passage_btn.clicked.connect(lambda: self._move_selection(1))
        transport_layout.addWidget(self.current_passage_label)
        transport_layout.addWidget(self.current_time_label)
        transport_layout.addStretch()
        transport_layout.addWidget(self.auto_advance_checkbox)
        transport_layout.addWidget(self.previous_passage_btn)
        transport_layout.addWidget(self.previous_frame_btn)
        transport_layout.addWidget(self.play_both_btn)
        transport_layout.addWidget(self.next_frame_btn)
        transport_layout.addWidget(self.next_passage_btn)
        layout.addWidget(transport)

        self.evidence_splitter = QSplitter(Qt.Horizontal)
        self.evidence_splitter.setChildrenCollapsible(False)
        self.evidence_splitter.setHandleWidth(5)
        for camera_index in self._configured_regular_camera_indexes:
            self._create_regular_pane(camera_index)
        self.regular_pane = self.regular_panes[0]
        self.high_speed_pane = PassageEvidencePane(
            "高速摄像", HIGH_SPEED_SOURCE, self
        )
        self._connect_evidence_pane(self.high_speed_pane)
        self.evidence_splitter.addWidget(self.high_speed_pane)
        self.configure_evidence_panes(
            self._configured_regular_camera_indexes,
            show_high_speed=self._show_high_speed_pane,
        )
        if self._legacy_camera_selector:
            self.regular_pane.camera_combo.currentIndexChanged.connect(
                self._on_legacy_regular_camera_changed
            )
        layout.addWidget(self.evidence_splitter, 6)

        footer = QHBoxLayout()
        self.summary_label = QLabel()
        self.summary_label.setStyleSheet("color: #667085; font-size: 9pt;")
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.close)
        footer.addWidget(self.summary_label)
        footer.addStretch()
        footer.addWidget(close_btn)
        layout.addLayout(footer)

    @property
    def regular_panes(self) -> tuple[PassageEvidencePane, ...]:
        return tuple(
            self._regular_panes_by_camera[camera_index]
            for camera_index in self._configured_regular_camera_indexes
        )

    @property
    def evidence_panes(self) -> tuple[PassageEvidencePane, ...]:
        panes = list(self.regular_panes)
        if self._show_high_speed_pane:
            panes.append(self.high_speed_pane)
        return tuple(panes)

    @property
    def all_evidence_panes(self) -> tuple[PassageEvidencePane, ...]:
        panes = list(self._regular_panes_by_camera.values())
        if hasattr(self, "high_speed_pane"):
            panes.append(self.high_speed_pane)
        return tuple(panes)

    def _connect_evidence_pane(self, pane: PassageEvidencePane) -> None:
        if pane.source_kind == REGULAR_SOURCE:
            pane.open_requested.connect(self._open_location_if_available)
        pane.step_requested.connect(self._step_both)
        pane.play_requested.connect(self._toggle_both)
        pane.passage_delta_requested.connect(self._seek_both_delta)
        pane.scrub_started.connect(lambda: self._set_sync_playing(False))
        pane.selection_step_requested.connect(self._move_selection)
        pane.maximize_requested.connect(self._toggle_maximized_pane)
        pane.marking_requested.connect(self._begin_marking)
        pane.confirmation_requested.connect(self._confirm_pending_marker)
        pane.cancel_requested.connect(self._cancel_pending_marker)
        pane.delete_requested.connect(self._delete_marker)

    def _create_regular_pane(self, camera_index: int) -> PassageEvidencePane:
        camera_index = max(1, int(camera_index))
        existing = self._regular_panes_by_camera.get(camera_index)
        if existing is not None:
            return existing
        pane = PassageEvidencePane(
            f"机位 {camera_index}",
            REGULAR_SOURCE,
            self,
            camera_index=camera_index,
        )
        pane.camera_combo.hide()
        self._connect_evidence_pane(pane)
        self._regular_panes_by_camera[camera_index] = pane
        if hasattr(self, "high_speed_pane"):
            high_speed_index = self.evidence_splitter.indexOf(self.high_speed_pane)
            self.evidence_splitter.insertWidget(max(0, high_speed_index), pane)
        else:
            self.evidence_splitter.addWidget(pane)
        self.evidence_pane_added.emit(pane)
        return pane

    def configure_evidence_panes(
        self,
        regular_camera_indexes: Iterable[int],
        *,
        show_high_speed: bool,
        include_recorded: bool = False,
    ) -> None:
        normalized_indexes, resolved_high_speed = _resolve_evidence_layout(
            self.timeline_store,
            regular_camera_indexes=regular_camera_indexes,
            show_high_speed_pane=show_high_speed,
            include_recorded=include_recorded,
        )
        self._set_sync_playing(False)
        self._maximized_pane = None
        for camera_index in normalized_indexes:
            self._create_regular_pane(camera_index)
        self._configured_regular_camera_indexes = normalized_indexes
        self._show_high_speed_pane = resolved_high_speed
        self.regular_pane = self.regular_panes[0]

        active_regular = set(normalized_indexes)
        for camera_index, pane in self._regular_panes_by_camera.items():
            active = camera_index in active_regular
            if not active:
                pane.clear_passage()
            pane.setVisible(active)
        if not self._show_high_speed_pane:
            self.high_speed_pane.clear_passage()
        self.high_speed_pane.setVisible(self._show_high_speed_pane)

        compact = len(self.evidence_panes) >= 3
        active_panes = set(self.evidence_panes)
        for pane in self.all_evidence_panes:
            pane.set_compact_controls(compact and pane in active_panes)
            pane.maximize_btn.setText("□")
        for index in range(self.evidence_splitter.count()):
            self.evidence_splitter.setStretchFactor(index, 1)
        self.evidence_splitter.setSizes([1] * self.evidence_splitter.count())

    def _update_legacy_camera_selector(
        self,
        lookup: PassageVideoLookup,
        selected: Optional[PassageVideoLocation],
    ) -> None:
        if not self._legacy_camera_selector:
            return
        combo = self.regular_pane.camera_combo
        locations = self._regular_locations(lookup)
        combo.blockSignals(True)
        combo.clear()
        for location in locations:
            combo.addItem(f"机位 {location.segment.camera_index}", location.segment.camera_index)
        if selected is not None:
            combo.setCurrentIndex(
                max(0, combo.findData(selected.segment.camera_index))
            )
        combo.setVisible(len(locations) > 1)
        combo.blockSignals(False)

    def _on_legacy_regular_camera_changed(self, _index: int) -> None:
        if not self._selected_event_id:
            return
        event = self.passage_store.get(self._selected_event_id)
        lookup = self._lookups.get(self._selected_event_id)
        if event is None or lookup is None:
            return
        camera_index = int(self.regular_pane.camera_combo.currentData() or 0)
        location = self._regular_location_for_camera(lookup, camera_index)
        if location is None:
            return
        self._preferred_regular_camera_index = camera_index
        self.regular_pane.set_passage(
            event,
            location,
            self._source_association(event.event_id, REGULAR_SOURCE, location),
            initial_delta_ms=self._shared_delta_ms,
        )

    def _on_offset_changed(self, value: int) -> None:
        self.clock_offset_ms = int(value)
        self.clock_offset_changed.emit(self.clock_offset_ms)
        self._lookup_cache.clear()
        self.refresh()

    def _on_group_changed(self) -> None:
        self.refresh()

    def _lookup(self, event: PassageEvent) -> PassageVideoLookup:
        lookup = self.timeline_store.locate_passage(
            event.timeline_timestamp_ms,
            clock_offset_ms=self.clock_offset_ms,
            pre_roll_ms=self.pre_roll_ms,
            race_id=event.race_id,
        )
        locations = list(lookup.locations)
        if not self._include_recorded_evidence:
            configured = set(self._configured_regular_camera_indexes)
            locations = [
                location
                for location in locations
                if (
                    is_high_speed(location)
                    and self._show_high_speed_pane
                )
                or (
                    not is_high_speed(location)
                    and location.segment.camera_index in configured
                )
            ]
        if self._high_speed_locator is None or not self._show_high_speed_pane:
            if tuple(locations) == lookup.locations:
                return lookup
            status = (
                "located"
                if any(location.status == "located" for location in locations)
                else "near_boundary"
                if any(location.status == "near_boundary" for location in locations)
                else lookup.status
            )
            return PassageVideoLookup(
                status,
                event.timeline_timestamp_ms + self.clock_offset_ms,
                tuple(locations),
            )
        locations = [location for location in locations if not is_high_speed(location)]
        high_speed = self._high_speed_locator(
            event,
            self.clock_offset_ms,
            self.pre_roll_ms,
        )
        if high_speed is not None:
            locations.append(high_speed)
        if any(location.status == "located" for location in locations):
            status = "located"
        elif any(location.status == "near_boundary" for location in locations):
            status = "near_boundary"
        else:
            status = lookup.status
        return PassageVideoLookup(
            status,
            event.timeline_timestamp_ms + self.clock_offset_ms,
            tuple(locations),
        )

    def invalidate_external_locations(self) -> None:
        self._external_location_revision += 1
        self._lookup_cache.clear()
        self.refresh()

    def _timeline_cache_signature(self) -> tuple:
        return tuple(
            (
                segment.segment_id,
                segment.video_path,
                segment.ended_at_ms,
                segment.media_started_at_ms,
                segment.media_duration_ms,
                segment.timing_error_ms,
                segment.race_id,
            )
            for segment in self.timeline_store.segments()
        )

    def _cached_lookup(self, event: PassageEvent) -> PassageVideoLookup:
        key = (
            event.revision,
            event.timeline_timestamp_ms,
            event.race_id,
            self.clock_offset_ms,
            self.pre_roll_ms,
            self._external_location_revision,
            tuple(self._configured_regular_camera_indexes),
            self._show_high_speed_pane,
            self._include_recorded_evidence,
        )
        cached = self._lookup_cache.get(event.event_id)
        if cached is not None and cached[0] == key:
            return cached[1]
        lookup = self._lookup(event)
        self._lookup_cache[event.event_id] = (key, lookup)
        return lookup

    def _source_association(
        self,
        event_id: str,
        source_kind: str,
        location: Optional[PassageVideoLocation],
    ) -> Optional[PassageEvidenceAssociation]:
        association = self.association_store.get(event_id, source_kind)
        if (
            association is None
            or location is None
            or association.segment_id != location.segment.segment_id
        ):
            return None
        return association

    def _source_location_with_saved_association(
        self,
        event_id: str,
        lookup: PassageVideoLookup,
        *,
        high_speed: bool,
    ) -> Optional[PassageVideoLocation]:
        source_kind = HIGH_SPEED_SOURCE if high_speed else REGULAR_SOURCE
        association = self.association_store.get(event_id, source_kind)
        if association is not None:
            associated = next(
                (
                    location
                    for location in lookup.locations
                    if is_high_speed(location) is bool(high_speed)
                    and location.segment.segment_id == association.segment_id
                ),
                None,
            )
            if associated is not None:
                return associated
        return source_location(lookup, high_speed=high_speed)

    @staticmethod
    def _confirmation_status(
        regular: Optional[PassageEvidenceAssociation],
        high_speed: Optional[PassageEvidenceAssociation],
        fallback: str,
    ) -> str:
        if regular is not None and high_speed is not None:
            return "双源标记"
        if regular is not None:
            return "录像标记"
        if high_speed is not None:
            return "高速标记"
        return "芯片记录"

    @staticmethod
    def _saved_delta_ms(
        regular_location: Optional[PassageVideoLocation],
        high_speed_location: Optional[PassageVideoLocation],
        regular: Optional[PassageEvidenceAssociation],
        high_speed: Optional[PassageEvidenceAssociation],
    ) -> int:
        candidates = []
        if regular is not None and regular_location is not None:
            candidates.append(
                (
                    regular.confirmed_at_ms,
                    regular.position_ms - regular_location.passage_position_ms,
                )
            )
        if high_speed is not None and high_speed_location is not None:
            candidates.append(
                (
                    high_speed.confirmed_at_ms,
                    high_speed.position_ms - high_speed_location.passage_position_ms,
                )
            )
        if not candidates:
            return 0
        return int(max(candidates)[1])

    def _current_metadata(self) -> Optional[RaceMetadata]:
        if self.metadata_store is None:
            return None
        return self.metadata_store.current()

    def _events_for_current_metadata(
        self,
        events: tuple[PassageEvent, ...],
    ) -> tuple[PassageEvent, ...]:
        metadata = self._current_metadata()
        if metadata is None:
            filtered = events
        else:
            filtered = tuple(
                event
                for event in events
                if event.race_id == metadata.race_id
                and event.stage_id == metadata.stage_id
            )
        return tuple(
            sorted(
                filtered,
                key=lambda event: (
                    event.timeline_timestamp_ms,
                    event.event_id,
                ),
            )
        )

    def _metadata_athlete_for_event(
        self,
        event: PassageEvent,
    ) -> Optional[RaceAthleteMetadata]:
        metadata = self._current_metadata()
        if metadata is None:
            return None
        for athlete in metadata.athletes:
            if event.athlete_id and athlete.athlete_id == event.athlete_id:
                return athlete
            if athlete.matches_identity(event.bib or event.chip_id):
                return athlete
        return None

    def _update_group_combo(self, events: tuple[PassageEvent, ...]) -> bool:
        previous_group = str(self.group_combo.currentData() or "")
        group_labels = {
            event.group_id: event.group_name.strip() or event.group_id
            for event in events
        }
        metadata = self._current_metadata()
        if metadata is not None:
            group_labels.update(
                {group.group_id: group.name for group in metadata.groups}
            )
        expected_items = [
            (group_id, group_label)
            for group_id, group_label in sorted(
                group_labels.items(), key=lambda item: (item[1], item[0])
            )
        ]
        current_items = [
            (
                str(self.group_combo.itemData(index) or ""),
                self.group_combo.itemText(index),
            )
            for index in range(1, self.group_combo.count())
        ]
        if current_items == expected_items:
            return False
        self.group_combo.blockSignals(True)
        self.group_combo.clear()
        self.group_combo.addItem("全部组别", "")
        for group_id, group_label in expected_items:
            self.group_combo.addItem(group_label, group_id)
        group_index = self.group_combo.findData(previous_group)
        self.group_combo.setCurrentIndex(max(0, group_index))
        self.group_combo.blockSignals(False)
        return str(self.group_combo.currentData() or "") != previous_group

    def _write_event_row(
        self,
        row: int,
        event: PassageEvent,
        lookup: PassageVideoLookup,
    ) -> None:
        regular = self._source_location_with_saved_association(
            event.event_id,
            lookup,
            high_speed=False,
        )
        high_speed = self._source_location_with_saved_association(
            event.event_id,
            lookup,
            high_speed=True,
        )
        readiness_status = review_status_text(lookup, regular, high_speed)
        regular_association = self._source_association(
            event.event_id, REGULAR_SOURCE, regular
        )
        high_speed_association = self._source_association(
            event.event_id, HIGH_SPEED_SOURCE, high_speed
        )
        review_status = self._confirmation_status(
            regular_association,
            high_speed_association,
            readiness_status,
        )
        self._record_summary_state(
            event.event_id,
            regular,
            high_speed,
            readiness_status,
            regular_association,
            high_speed_association,
        )
        metadata_athlete = self._metadata_athlete_for_event(event)
        identity = event.bib.strip() or "未知"
        athlete_name = event.athlete_name.strip() or (
            metadata_athlete.name.strip() if metadata_athlete is not None else ""
        ) or "--"
        values = (
            str(row + 1),
            identity,
            athlete_name,
            event.group_name.strip() or event.group_id,
            str(event.lap),
            format_passage_time(event.timeline_timestamp_ms),
            source_confirmation_status(regular, regular_association),
            source_confirmation_status(high_speed, high_speed_association),
            review_status,
        )
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            if column in {0, 4, 5, 6, 7, 8}:
                item.setTextAlignment(Qt.AlignCenter)
            if column == 0:
                item.setData(Qt.UserRole, event.event_id)
            if column in {6, 7, 8}:
                self._apply_status_style(item, value)
            self.table.setItem(row, column, item)

    def refresh(self) -> None:
        previous_event_id = self._selected_event_id
        metadata = self._current_metadata()
        metadata_context_key = (
            (metadata.race_id, metadata.stage_id)
            if metadata is not None
            else ("", "")
        )
        if metadata_context_key != self._metadata_context_key:
            self._metadata_context_key = metadata_context_key
            previous_event_id = ""
            self.identity_search.clear()
        events = self._events_for_current_metadata(self.passage_store.events())
        signature = self._timeline_cache_signature()
        if signature != self._timeline_signature:
            self._timeline_signature = signature
            self._lookup_cache = {
                event_id: cached
                for event_id, cached in self._lookup_cache.items()
                if cached[1].locations
            }

        self._update_group_combo(events)
        selected_group = str(self.group_combo.currentData() or "")

        self._visible_events = [
            event
            for event in events
            if not selected_group or event.group_id == selected_group
        ]
        self._lookups = {
            event.event_id: self._cached_lookup(event)
            for event in self._visible_events
        }

        self.table.blockSignals(True)
        self.table.setRowCount(len(self._visible_events))
        self._located_event_ids.clear()
        self._confirmed_event_ids.clear()
        selected_row = -1
        for row, event in enumerate(self._visible_events):
            lookup = self._lookups[event.event_id]
            self._write_event_row(row, event, lookup)
            if event.event_id == previous_event_id:
                selected_row = row

        if selected_row < 0 and self._visible_events:
            selected_row = 0
        if selected_row >= 0:
            self.table.setCurrentCell(selected_row, 0)
            self.table.selectRow(selected_row)
        self.table.blockSignals(False)
        self.table.schedule_auto_fit()

        self._render_summary()
        if selected_row >= 0:
            self._select_event(self._visible_events[selected_row].event_id)
        else:
            self._clear_selection_details()

    def refresh_events(self, event_ids: Iterable[str]) -> None:
        changed_event_ids = {str(event_id) for event_id in event_ids if event_id}
        if not changed_event_ids:
            return
        if len(changed_event_ids) > 64:
            self.refresh()
            return
        events = self._events_for_current_metadata(self.passage_store.events())
        if self._update_group_combo(events):
            self.refresh()
            return
        selected_group = str(self.group_combo.currentData() or "")
        signature = self._timeline_cache_signature()
        if signature != self._timeline_signature:
            self._timeline_signature = signature
            self._lookup_cache = {
                event_id: cached
                for event_id, cached in self._lookup_cache.items()
                if cached[1].locations
            }
        for event_id in changed_event_ids:
            self._lookup_cache.pop(event_id, None)

        event_by_id = {event.event_id: event for event in events}
        ordered_changed_events = [
            event for event in events if event.event_id in changed_event_ids
        ]
        selected_event_changed = self._selected_event_id in changed_event_ids
        selected_event_id = self._selected_event_id

        self.table.blockSignals(True)
        for event_id in changed_event_ids:
            event = event_by_id.get(event_id)
            row = next(
                (
                    index
                    for index, visible_event in enumerate(self._visible_events)
                    if visible_event.event_id == event_id
                ),
                -1,
            )
            should_show = event is not None and (
                not selected_group or event.group_id == selected_group
            )
            if row >= 0 and not should_show:
                self.table.removeRow(row)
                self._visible_events.pop(row)
                self._lookups.pop(event_id, None)
                self._discard_summary_state(event_id)

        for event in ordered_changed_events:
            if selected_group and event.group_id != selected_group:
                continue
            row = next(
                (
                    index
                    for index, visible_event in enumerate(self._visible_events)
                    if visible_event.event_id == event.event_id
                ),
                -1,
            )
            lookup = self._cached_lookup(event)
            self._lookups[event.event_id] = lookup
            if row < 0:
                visible_order = [
                    candidate.event_id
                    for candidate in events
                    if not selected_group or candidate.group_id == selected_group
                ]
                desired_row = visible_order.index(event.event_id)
                row = min(desired_row, len(self._visible_events))
                self._visible_events.insert(row, event)
                self.table.insertRow(row)
            else:
                self._visible_events[row] = event
            self._write_event_row(row, event, lookup)

        selected_row = next(
            (
                index
                for index, event in enumerate(self._visible_events)
                if event.event_id == selected_event_id
            ),
            -1,
        )
        if selected_row >= 0:
            self.table.setCurrentCell(selected_row, 0)
            self.table.selectRow(selected_row)
        elif self._visible_events:
            selected_row = 0
            selected_event_id = self._visible_events[0].event_id
            self.table.setCurrentCell(selected_row, 0)
            self.table.selectRow(selected_row)
            selected_event_changed = True
        self.table.blockSignals(False)
        self.table.schedule_auto_fit()

        self._update_navigation_controls()
        self._render_summary()
        if selected_event_changed and selected_event_id:
            self._select_event(selected_event_id)
        elif not self._visible_events:
            self._clear_selection_details()

    @staticmethod
    def _status_color(value: str) -> QColor:
        if value in {
            "已标记",
            "录像标记",
            "高速标记",
            "双源标记",
        }:
            return QColor("#16845b")
        if value == "可查看":
            return QColor("#276d9b")
        if value == "无画面":
            return QColor("#c0372b")
        return QColor("#526170")

    @classmethod
    def _apply_status_style(cls, item: QTableWidgetItem, value: str) -> None:
        item.setForeground(cls._status_color(value))
        font = item.font()
        font.setBold(value == "无画面" or value in {
            "已标记",
            "录像标记",
            "高速标记",
            "双源标记",
        })
        item.setFont(font)

    def _on_table_selection_changed(self) -> None:
        row = self.table.currentRow()
        if 0 <= row < len(self._visible_events):
            self._select_event(self._visible_events[row].event_id)

    @staticmethod
    def _regular_locations(
        lookup: PassageVideoLookup,
    ) -> tuple[PassageVideoLocation, ...]:
        return tuple(
            sorted(
                (
                    location
                    for location in lookup.locations
                    if not is_high_speed(location)
                ),
                key=lambda location: (
                    _STATUS_PRIORITY.get(location.status, 99),
                    location.segment.camera_index,
                    location.segment.segment_id,
                ),
            )
        )

    @staticmethod
    def _regular_location_for_camera(
        lookup: PassageVideoLookup,
        camera_index: int,
    ) -> Optional[PassageVideoLocation]:
        return next(
            (
                location
                for location in PassageReviewDialog._regular_locations(lookup)
                if location.segment.camera_index == int(camera_index)
            ),
            None,
        )

    def _regular_summary_location(
        self,
        event_id: str,
        lookup: PassageVideoLookup,
    ) -> Optional[PassageVideoLocation]:
        locations = self._regular_locations(lookup)
        association = self.association_store.get(event_id, REGULAR_SOURCE)
        if association is not None:
            associated = next(
                (
                    location
                    for location in locations
                    if location.segment.segment_id == association.segment_id
                ),
                None,
            )
            if associated is not None:
                return associated
        configured = set(self._configured_regular_camera_indexes)
        return next(
            (
                location
                for location in locations
                if location.segment.camera_index in configured
            ),
            locations[0] if locations else None,
        )

    def _select_event(self, event_id: str) -> None:
        event = self.passage_store.get(event_id)
        lookup = self._lookups.get(event_id)
        if event is None or lookup is None:
            self.refresh()
            return
        regular_locations = {
            pane.camera_index: self._regular_location_for_camera(
                lookup,
                pane.camera_index,
            )
            for pane in self.regular_panes
        }
        regular = self._regular_summary_location(event.event_id, lookup)
        self._update_legacy_camera_selector(lookup, regular)
        high_speed = source_location(lookup, high_speed=True)
        preserve_media = (
            self._selected_event_id == event.event_id
            and all(
                pane.matches_passage_context(
                    event,
                    regular_locations.get(pane.camera_index),
                )
                for pane in self.regular_panes
            )
            and (
                not self._show_high_speed_pane
                or self.high_speed_pane.matches_passage_context(event, high_speed)
            )
        )
        regular_association = self._source_association(
            event.event_id, REGULAR_SOURCE, regular
        )
        high_speed_association = self._source_association(
            event.event_id, HIGH_SPEED_SOURCE, high_speed
        )
        if not preserve_media:
            self._set_sync_playing(False)
            self._shared_delta_ms = self._saved_delta_ms(
                regular,
                high_speed,
                regular_association,
                high_speed_association,
            )
        self._selected_event_id = event.event_id
        identity = event.bib.strip() or "未知"
        metadata = self._current_metadata()
        metadata_athlete = self._metadata_athlete_for_event(event)
        athlete_name = event.athlete_name.strip() or (
            metadata_athlete.name.strip() if metadata_athlete is not None else ""
        ) or "--"
        team_name = event.team_name.strip() or (
            metadata_athlete.team_name.strip()
            if metadata_athlete is not None
            else ""
        ) or "--"
        passage_time = format_passage_time(event.timeline_timestamp_ms)
        status = self._confirmation_status(
            regular_association,
            high_speed_association,
            review_status_text(lookup, regular, high_speed),
        )
        self.race_value.setText(
            (metadata.race_name.strip() if metadata is not None else "")
            or event.race_name.strip()
            or event.race_id
        )
        self.stage_value.setText(
            (metadata.stage_name.strip() if metadata is not None else "")
            or event.stage_name.strip()
            or event.stage_id
        )
        self.group_value.setText(
            (
                metadata.group_label(event.group_id)
                if metadata is not None
                else ""
            )
            or event.group_name.strip()
            or event.group_id
        )
        self.selected_identity_value.setText(identity)
        self.athlete_value.setText(athlete_name)
        self.team_value.setText(team_name)
        self.identity_search.setText(identity)
        self.selected_time_value.setText(passage_time)
        self.source_value.setText(status)
        athlete_summary = (
            f"{identity} {athlete_name if athlete_name != '--' else ''}".strip()
        )
        self.current_passage_label.setText(f"当前运动员 {athlete_summary}")
        if not preserve_media:
            self.current_time_label.setText(
                format_passage_time(event.timeline_timestamp_ms + self._shared_delta_ms)
            )
            for pane in self.regular_panes:
                pane_location = regular_locations.get(pane.camera_index)
                pane.set_passage(
                    event,
                    pane_location,
                    self._source_association(
                        event.event_id,
                        REGULAR_SOURCE,
                        pane_location,
                    ),
                    initial_delta_ms=self._shared_delta_ms,
                )
            if self._show_high_speed_pane:
                self.high_speed_pane.set_passage(
                    event,
                    high_speed,
                    high_speed_association,
                    initial_delta_ms=self._shared_delta_ms,
                )
            self.play_both_btn.setText("▶")
        else:
            for pane in self.regular_panes:
                pane_association = self._source_association(
                    event.event_id,
                    REGULAR_SOURCE,
                    regular_locations.get(pane.camera_index),
                )
                if pane.association != pane_association:
                    pane.set_association(pane_association)
            if (
                self._show_high_speed_pane
                and self.high_speed_pane.association != high_speed_association
            ):
                self.high_speed_pane.set_association(high_speed_association)
        self._update_reference_states(event.event_id)
        self._update_navigation_controls()

    def _update_navigation_controls(self) -> None:
        row = self.table.currentRow()
        self.previous_passage_btn.setEnabled(row > 0)
        self.next_passage_btn.setEnabled(0 <= row < self.table.rowCount() - 1)

    def _clear_selection_details(self) -> None:
        self._set_sync_playing(False)
        self._shared_delta_ms = 0
        self._selected_event_id = ""
        metadata = self._current_metadata()
        self.race_value.setText(
            (metadata.race_name.strip() or metadata.race_id)
            if metadata is not None
            else "--"
        )
        self.stage_value.setText(
            (metadata.stage_name.strip() or metadata.stage_id)
            if metadata is not None
            else "--"
        )
        selected_group = str(self.group_combo.currentData() or "")
        self.group_value.setText(
            metadata.group_label(selected_group)
            if metadata is not None and selected_group
            else "--"
        )
        for label in (
            self.selected_identity_value,
            self.athlete_value,
            self.team_value,
            self.selected_time_value,
            self.source_value,
        ):
            label.setText("--")
        self.current_passage_label.setText("未选择通过记录")
        self.current_time_label.setText("--:--:--.---")
        for pane in self.evidence_panes:
            pane.clear_passage()
        self._update_navigation_controls()

    def focus_athlete(
        self,
        race_id: str,
        stage_id: str,
        *,
        athlete_id: str = "",
        bib: str = "",
        group_id: str = "",
    ) -> bool:
        """Select the newest passage or show roster-only details without a modal."""

        race_id = str(race_id).strip()
        stage_id = str(stage_id).strip()
        athlete_id = str(athlete_id).strip()
        bib = str(bib).strip()
        group_id = str(group_id).strip()
        metadata = self._current_metadata()
        if metadata is not None and (
            metadata.race_id != race_id or metadata.stage_id != stage_id
        ):
            return False

        events = tuple(
            event
            for event in self._events_for_current_metadata(self.passage_store.events())
            if event.race_id == race_id and event.stage_id == stage_id
        )
        athlete_matches = (
            [event for event in events if athlete_id and event.athlete_id == athlete_id]
            if athlete_id
            else []
        )
        matches = athlete_matches or [
            event for event in events if bib and event.bib.strip() == bib
        ]
        if matches:
            event = max(
                matches,
                key=lambda item: (
                    item.timeline_timestamp_ms,
                    item.sequence,
                    item.revision,
                ),
            )
            target_group = event.group_id or group_id
            group_index = self.group_combo.findData(target_group)
            if group_index >= 0 and group_index != self.group_combo.currentIndex():
                self.group_combo.setCurrentIndex(group_index)
            row = next(
                (
                    index
                    for index, visible in enumerate(self._visible_events)
                    if visible.event_id == event.event_id
                ),
                -1,
            )
            if row < 0:
                self.refresh()
                row = next(
                    (
                        index
                        for index, visible in enumerate(self._visible_events)
                        if visible.event_id == event.event_id
                    ),
                    -1,
                )
            if row < 0:
                return False
            self.table.blockSignals(True)
            self.table.setCurrentCell(row, 0)
            self.table.selectRow(row)
            self.table.blockSignals(False)
            item = self.table.item(row, 1)
            if item is not None:
                self.table.scrollToItem(item, QAbstractItemView.PositionAtCenter)
            self._select_event(event.event_id)
            return True

        roster_athlete = None
        if metadata is not None:
            for candidate in metadata.athletes:
                if athlete_id and candidate.athlete_id == athlete_id:
                    roster_athlete = candidate
                    break
            if roster_athlete is None and bib:
                roster_athlete = next(
                    (
                        candidate
                        for candidate in metadata.athletes
                        if candidate.bib.strip() == bib
                    ),
                    None,
                )
        target_group = (
            roster_athlete.group_id if roster_athlete is not None else group_id
        )
        group_index = self.group_combo.findData(target_group)
        if group_index >= 0 and group_index != self.group_combo.currentIndex():
            self.group_combo.setCurrentIndex(group_index)
        self.table.blockSignals(True)
        self.table.clearSelection()
        self.table.setCurrentCell(-1, -1)
        self.table.blockSignals(False)
        self._clear_selection_details()

        identity = (
            roster_athlete.bib.strip()
            if roster_athlete is not None
            else bib
        )
        athlete_name = (
            roster_athlete.name.strip() if roster_athlete is not None else ""
        )
        team_name = (
            roster_athlete.team_name.strip() if roster_athlete is not None else ""
        )
        if metadata is not None:
            self.race_value.setText(metadata.race_name.strip() or metadata.race_id)
            self.stage_value.setText(metadata.stage_name.strip() or metadata.stage_id)
            self.group_value.setText(
                metadata.group_label(target_group) if target_group else "--"
            )
        self.identity_search.setText(identity)
        self.selected_identity_value.setText(identity or "--")
        self.athlete_value.setText(athlete_name or "--")
        self.team_value.setText(team_name or "--")
        self.selected_time_value.setText("尚无通过记录")
        self.source_value.setText("等待 CycleRace 通过时间")
        athlete_summary = f"{identity} {athlete_name}".strip()
        self.current_passage_label.setText(
            f"名单运动员 {athlete_summary}（尚无通过记录）"
        )
        return bool(roster_athlete is not None or identity)

    def _begin_marking(self, pane: PassageEvidencePane) -> None:
        self._set_sync_playing(False)
        for candidate in self.evidence_panes:
            if candidate is not pane:
                candidate.cancel_marker_edit()
        pane.begin_marking()

    def _update_reference_states(self, event_id: str) -> None:
        confirmed = any(
            self.association_store.get(event_id, source_kind) is not None
            for source_kind in (REGULAR_SOURCE, HIGH_SPEED_SOURCE)
        )
        for pane in self.evidence_panes:
            pane.set_reference_only(confirmed and pane.association is None)

    def _confirm_pending_marker(self, pane: PassageEvidencePane) -> bool:
        event = self.passage_store.get(self._selected_event_id)
        pending = pane.pending_confirmation()
        if event is None or pending is None:
            return False
        identity = event.bib.strip() or event.chip_id.strip()
        try:
            association = self.association_store.confirm(
                passage_event_id=event.event_id,
                bib=identity,
                confirmed_source=pane.source_kind,
                segment_id=str(pending["segment_id"]),
                frame_index=int(pending["frame_index"]),
                position_ms=int(pending["position_ms"]),
                marker_x_normalized=float(pending["marker_x_normalized"]),
                marker_y_normalized=float(pending["marker_y_normalized"]),
                confirmed_at_ms=int(time.time() * 1000.0),
            )
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            QMessageBox.critical(self, "保存失败", f"无法保存证据标记：{error}")
            return False
        confirmed_row = self.table.currentRow()
        pane.set_association(association)
        self._update_reference_states(event.event_id)
        should_advance = (
            self.auto_advance_checkbox.isChecked()
            and self._all_available_sources_confirmed(event.event_id)
        )
        self._update_event_confirmation_status(event.event_id)
        if (
            should_advance
            and confirmed_row >= 0
            and confirmed_row < self.table.rowCount() - 1
        ):
            self._move_selection(1)
        return True

    def _update_event_confirmation_status(self, event_id: str) -> None:
        lookup = self._lookups.get(event_id)
        if lookup is None:
            return
        regular = self._source_location_with_saved_association(
            event_id,
            lookup,
            high_speed=False,
        )
        high_speed = self._source_location_with_saved_association(
            event_id,
            lookup,
            high_speed=True,
        )
        regular_association = self._source_association(
            event_id, REGULAR_SOURCE, regular
        )
        high_speed_association = self._source_association(
            event_id, HIGH_SPEED_SOURCE, high_speed
        )
        readiness_status = review_status_text(lookup, regular, high_speed)
        status = self._confirmation_status(
            regular_association,
            high_speed_association,
            readiness_status,
        )
        self._record_summary_state(
            event_id,
            regular,
            high_speed,
            readiness_status,
            regular_association,
            high_speed_association,
        )
        row_values = (
            (6, source_confirmation_status(regular, regular_association)),
            (7, source_confirmation_status(high_speed, high_speed_association)),
            (8, status),
        )
        for row, event in enumerate(self._visible_events):
            if event.event_id != event_id:
                continue
            for column, value in row_values:
                item = self.table.item(row, column)
                if item is not None:
                    item.setText(value)
                    self._apply_status_style(item, value)
            break
        if event_id == self._selected_event_id:
            self.source_value.setText(status)
        self._render_summary()

    def _record_summary_state(
        self,
        event_id: str,
        regular: Optional[PassageVideoLocation],
        high_speed: Optional[PassageVideoLocation],
        _readiness_status: str,
        regular_association: Optional[PassageEvidenceAssociation],
        high_speed_association: Optional[PassageEvidenceAssociation],
    ) -> None:
        self._set_membership(
            self._located_event_ids,
            event_id,
            any(
                location is not None and location.status in _OPENABLE_STATUSES
                for location in (regular, high_speed)
            ),
        )
        self._set_membership(
            self._confirmed_event_ids,
            event_id,
            regular_association is not None or high_speed_association is not None,
        )

    @staticmethod
    def _set_membership(values: set[str], event_id: str, present: bool) -> None:
        if present:
            values.add(event_id)
        else:
            values.discard(event_id)

    def _discard_summary_state(self, event_id: str) -> None:
        self._located_event_ids.discard(event_id)
        self._confirmed_event_ids.discard(event_id)

    def _render_summary(self) -> None:
        located_count = len(self._located_event_ids)
        confirmed_count = len(self._confirmed_event_ids)
        self.summary_label.setText(
            f"共 {len(self.passage_store)} 条 passage，"
            f"当前显示 {len(self._visible_events)} 条；"
            f"{located_count} 条有画面，{confirmed_count} 条已人工标记；"
            f"时钟偏移 {self.clock_offset_ms:+d} ms"
        )
        self._available_evidence_count = located_count

    def _all_available_sources_confirmed(self, event_id: str) -> bool:
        lookup = self._lookups.get(event_id)
        if lookup is None:
            return False
        for location in lookup.locations:
            if location.status not in _OPENABLE_STATUSES:
                continue
            source_kind = HIGH_SPEED_SOURCE if is_high_speed(location) else REGULAR_SOURCE
            if self._source_association(event_id, source_kind, location) is not None:
                return True
        return False

    def _cancel_pending_marker(self, pane: PassageEvidencePane) -> None:
        pane.cancel_marker_edit()

    def _delete_marker(self, pane: PassageEvidencePane) -> None:
        association = pane.association
        if association is None:
            pane.cancel_marker_edit()
            return
        answer = QMessageBox.question(
            self,
            "删除证据标记",
            f"确定删除 {association.bib} 号在{pane.title_label.text()}中的人工标记吗？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        try:
            self.association_store.clear(
                association.passage_event_id,
                association.confirmed_source,
                confirmed_at_ms=int(time.time() * 1000.0),
            )
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            QMessageBox.critical(self, "删除失败", f"无法删除证据标记：{error}")
            return
        pane.set_association(None)
        self._update_reference_states(association.passage_event_id)
        self._update_event_confirmation_status(association.passage_event_id)

    def _find_identity(self) -> None:
        value = self.identity_search.text().strip().casefold()
        if not value:
            return
        matches = []
        for row, event in enumerate(self._visible_events):
            metadata_athlete = self._metadata_athlete_for_event(event)
            athlete_name = event.athlete_name.strip() or (
                metadata_athlete.name.strip() if metadata_athlete is not None else ""
            )
            if value in {
                event.bib.strip().casefold(),
                event.chip_id.strip().casefold(),
                athlete_name.casefold(),
            }:
                matches.append((row, event))
        if matches:
            row, _event = max(
                matches,
                key=lambda match: (
                    match[1].timeline_timestamp_ms,
                    match[1].sequence,
                    match[1].revision,
                ),
            )
            self.table.setCurrentCell(row, 0)
            self.table.selectRow(row)
            item = self.table.item(row, 1)
            if item is not None:
                self.table.scrollToItem(item, QAbstractItemView.PositionAtCenter)
            return
        metadata = self._current_metadata()
        if metadata is not None:
            selected_group = str(self.group_combo.currentData() or "")
            for athlete in metadata.athletes:
                if selected_group and athlete.group_id != selected_group:
                    continue
                if not (
                    athlete.matches_identity(value)
                    or athlete.name.strip().casefold() == value
                ):
                    continue
                identity = athlete.bib.strip() or "未知"
                self.race_value.setText(metadata.race_name or metadata.race_id)
                self.stage_value.setText(metadata.stage_name or metadata.stage_id)
                self.group_value.setText(metadata.group_label(athlete.group_id))
                self.selected_identity_value.setText(identity)
                self.athlete_value.setText(athlete.name.strip() or "--")
                self.team_value.setText(athlete.team_name.strip() or "--")
                self.selected_time_value.setText("尚无通过记录")
                self.source_value.setText("等待 CycleRace 通过时间")
                athlete_summary = f"{identity} {athlete.name}".strip()
                self.current_passage_label.setText(
                    f"名单运动员 {athlete_summary}（尚无通过记录）"
                )
                return
        QMessageBox.information(self, "未找到", "当前组别没有该号码或姓名的通过记录。")

    def _move_selection(self, delta: int) -> None:
        if self.table.rowCount() <= 0:
            return
        row = max(
            0,
            min(self.table.currentRow() + int(delta), self.table.rowCount() - 1),
        )
        if row == self.table.currentRow():
            return
        event_id = self._visible_events[row].event_id
        self.table.blockSignals(True)
        self.table.setCurrentCell(row, 0)
        self.table.selectRow(row)
        self.table.blockSignals(False)
        item = self.table.item(row, 1)
        if item is not None:
            self.table.scrollToItem(item, QAbstractItemView.PositionAtCenter)
        self._select_event(event_id)

    def _step_both(self, frame_delta: int) -> None:
        self._set_sync_playing(False)
        reference = next(
            (
                pane
                for pane in reversed(self.evidence_panes)
                if pane.available_delta_bounds() is not None
            ),
            self.regular_pane,
        )
        step_ms = reference.frame_duration_ms()
        self._seek_both_delta(
            self._shared_delta_ms + int(frame_delta) * step_ms
        )

    def _toggle_both(self) -> None:
        self._set_sync_playing(not self._sync_playing)

    def _sync_delta_bounds(self) -> Optional[tuple[int, int]]:
        bounds = [
            pane_bounds
            for pane in self.evidence_panes
            if (pane_bounds := pane.available_delta_bounds()) is not None
        ]
        if not bounds:
            return None
        lower = min(value[0] for value in bounds)
        upper = max(value[1] for value in bounds)
        return lower, upper

    def _seek_both_delta(self, delta_ms: int) -> None:
        bounds = self._sync_delta_bounds()
        if bounds is None:
            return
        lower, upper = bounds
        self._shared_delta_ms = max(lower, min(int(delta_ms), upper))
        for pane in self.evidence_panes:
            pane.seek_passage_delta(
                self._shared_delta_ms,
                linked_playing=self._sync_playing,
            )
        self._update_shared_time_label()

    def _set_sync_playing(self, playing: bool) -> None:
        playing = bool(playing) and self._sync_delta_bounds() is not None
        if playing == self._sync_playing:
            return
        if playing:
            self._sync_origin_delta_ms = self._shared_delta_ms
            self._sync_started_at = time.monotonic()
            self._sync_playing = True
            self._sync_timer.start()
        else:
            if self._sync_playing:
                elapsed_ms = int((time.monotonic() - self._sync_started_at) * 1000.0)
                self._shared_delta_ms = self._sync_origin_delta_ms + elapsed_ms
            self._sync_playing = False
            self._sync_timer.stop()
        for pane in self.evidence_panes:
            pane.set_linked_playing(self._sync_playing)
        self.play_both_btn.setText("Ⅱ" if self._sync_playing else "▶")
        if not self._sync_playing:
            self._seek_both_delta(self._shared_delta_ms)

    def _on_sync_tick(self) -> None:
        if not self._sync_playing:
            return
        elapsed_ms = int((time.monotonic() - self._sync_started_at) * 1000.0)
        target_delta_ms = self._sync_origin_delta_ms + elapsed_ms
        bounds = self._sync_delta_bounds()
        if bounds is None or target_delta_ms >= bounds[1]:
            if bounds is not None:
                self._shared_delta_ms = bounds[1]
            self._set_sync_playing(False)
            return
        self._seek_both_delta(target_delta_ms)

    def _update_shared_time_label(self) -> None:
        event = self.passage_store.get(self._selected_event_id)
        if event is None:
            return
        self.current_time_label.setText(
            f"{format_passage_time(event.timeline_timestamp_ms + self._shared_delta_ms)} "
            f"(Δ{self._shared_delta_ms:+d} ms)"
        )

    def _toggle_maximized_pane(self, pane: PassageEvidencePane) -> None:
        if self._maximized_pane is pane:
            for candidate in self.evidence_panes:
                candidate.show()
                candidate.maximize_btn.setText("□")
            self._maximized_pane = None
            return
        for candidate in self.evidence_panes:
            candidate.setVisible(candidate is pane)
            candidate.maximize_btn.setText("▣" if candidate is pane else "□")
        self._maximized_pane = pane

    def _open_preferred_source(self, row: int, _column: int) -> None:
        if not (0 <= row < len(self._visible_events)):
            return
        event = self._visible_events[row]
        lookup = self._lookups.get(event.event_id)
        if lookup is None:
            return
        regular = self._regular_summary_location(event.event_id, lookup)
        high_speed = source_location(lookup, high_speed=True)
        location = next(
            (
                candidate
                for candidate in (regular, high_speed)
                if candidate is not None
                and candidate.status in _OPENABLE_STATUSES
            ),
            None,
        )
        if location is not None:
            self._open_location_if_available(event, location)

    def _open_location_if_available(
        self,
        event: PassageEvent,
        location: PassageVideoLocation,
    ) -> None:
        if is_high_speed(location):
            if self._maximized_pane is not self.high_speed_pane:
                self._toggle_maximized_pane(self.high_speed_pane)
            return
        if self._open_location is not None:
            self._open_location(event, location)

    def _open_row(self, row: int, column: int) -> None:
        self._open_preferred_source(row, column)

    def _open_event(self, event_id: str) -> None:
        self._open_event_location(event_id, "")

    def _open_event_location(self, event_id: str, segment_id: str) -> None:
        event = self.passage_store.get(event_id)
        if event is None:
            self.refresh()
            return
        lookup = self._lookups.get(event_id) or self._cached_lookup(event)
        available = [
            item for item in lookup.locations if item.status in _OPENABLE_STATUSES
        ]
        if not available:
            QMessageBox.information(self, "无法定位", lookup_status_text(lookup))
            return
        location = next(
            (
                item
                for item in available
                if not segment_id or item.segment.segment_id == segment_id
            ),
            None,
        )
        if location is None:
            QMessageBox.information(self, "无法定位", "该证据已变化，请刷新后重试。")
            self.refresh()
            return
        self._open_location_if_available(event, location)

    def closeEvent(self, event) -> None:
        self._sync_playing = False
        self._sync_timer.stop()
        for pane in self.all_evidence_panes:
            pane.shutdown(wait=True)
        super().closeEvent(event)


__all__ = [
    "PassageEvidencePane",
    "PassageReviewDialog",
    "UI_BASE_FONT_POINT_SIZE",
    "UI_FONT_FAMILY",
    "compact_source_status",
    "combined_review_status",
    "format_passage_time",
    "lookup_status_text",
    "review_status_text",
    "source_location",
]
