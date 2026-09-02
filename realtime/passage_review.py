"""FinishLynx-style review workspace for CycleRace passages and video evidence."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
import re
import time
from types import SimpleNamespace
from typing import Callable, Iterable, Mapping, Optional

from PyQt5.QtCore import QPoint, QRectF, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
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
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsView,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QInputDialog,
    QPushButton,
    QScrollArea,
    QSlider,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStyledItemDelegate,
    QShortcut,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import APP_DISPLAY_NAME, APP_WINDOW_TITLE
from .auyat_rgb import AUYAT_CLOCK_SOURCE, AuyatRgbPlaybackWorker
from .external_clip_import import EXTERNAL_CLOCK_SOURCE
from .passage_evidence import (
    HIGH_SPEED_SOURCE,
    REGULAR_SOURCE,
    PassageEvidenceAssociation,
    PassageEvidenceAssociationStore,
    VideoClockCalibrationStore,
)
from .passage_receiver import PassageEvent, PassageEventStore
from .passage_batch import (
    DEFAULT_REVIEW_GAP_MS,
    DEFAULT_SUBWAVE_GAP_MS,
    PassageReviewBatch,
    build_review_batches,
)
from .video_arrival import (
    DEFAULT_ARRIVAL_BATCH_GAP_MS,
    DEFAULT_ARRIVAL_SUBWAVE_GAP_MS,
    VideoArrivalBatch,
    VideoArrivalCandidateStore,
    build_video_arrival_batches,
)
from .video_discovery import VideoDiscoveryError, VideoDiscoveryStore
from .playback_coordinator import PlaybackCoordinator
from .race_metadata import (
    RaceAthleteMetadata,
    RaceMetadata,
    RaceMetadataStore,
)
from .review_selection import ReviewSelectionController, ReviewSelectionPlan
from .review_clip import PassageReviewBindingStore
from .thread_lifecycle import retire_qthread, track_qthread
from .video_playback import TargetTimelineSlider, VideoPlaybackWorker
from .video_filmstrip import VideoFilmstripWidget
from .video_activity import ActivityTimelineWidget
from .video_timeline import (
    DEFAULT_CLOCK_SOURCE,
    PassageVideoLocation,
    PassageVideoLookup,
    VideoTimelineStore,
)

logger = logging.getLogger("FinishReview.Review")


_STATUS_TEXT = {
    "no_segments": "没有录像时间线",
    "before_recording": "早于录像",
    "after_recording": "晚于录像",
    "recording_gap": "录像分段间隙",
    "race_mismatch": "录像属于其他赛事",
    "near_boundary": "位于时间误差边界，可打开核验",
    "recording": "对应机位仍在录像",
    "preview": "预览可用，完整证据仍在处理中",
    "missing_file": "录像文件缺失",
    "unverified": "录像可打开，但时间范围未验证",
    "outside_media": "Passage 超出录像真实媒体范围",
}

_CONFIRMABLE_STATUSES = {"located", "near_boundary", "unverified", "preview"}
_OPENABLE_STATUSES = _CONFIRMABLE_STATUSES | {"preview"}
_STATUS_PRIORITY = {
    "located": 0,
    "preview": 1,
    "near_boundary": 2,
    "unverified": 3,
    "recording": 4,
    "missing_file": 5,
    "outside_media": 6,
}

# The race recordings are normally 25 fps. Keep plain arrows frame-accurate,
# while modifier shortcuts cover gaps without jumping over an unchipped rider.
SHIFT_FRAME_STEP = 5
CTRL_FRAME_STEP = 50
BEIJING_TIMEZONE = timezone(timedelta(hours=8))
UI_FONT_FAMILY = "Microsoft YaHei UI"
UI_BASE_FONT_POINT_SIZE = 10
UI_INFO_PANEL_MIN_WIDTH = 300
UI_INFO_PANEL_MAX_WIDTH = 380
UI_INFO_PANEL_DEFAULT_WIDTH = 330
_ARCHIVE_SEGMENT_SUFFIX_RE = re.compile(r"_archive_\d+$", re.IGNORECASE)


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
    if location.status == "preview":
        return f"快速预览 · {position} · 完整证据处理中"
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


def _association_matches_location(
    association: Optional[PassageEvidenceAssociation],
    location: Optional[PassageVideoLocation],
) -> bool:
    """Keep preview confirmations visible when the sealed clip replaces them."""

    if association is None or location is None:
        return False
    if association.segment_id == location.segment.segment_id:
        return True
    match = re.match(r"^preview-(\d+)-", association.segment_id)
    if match is not None:
        return int(match.group(1)) == int(location.segment.camera_index)
    return association.segment_id.startswith("preview-")


def compact_source_status(location: Optional[PassageVideoLocation]) -> str:
    return "未确认"


def source_confirmation_status(
    location: Optional[PassageVideoLocation],
    association: Optional[PassageEvidenceAssociation],
) -> str:
    if association is not None:
        return "已确认"
    return "未确认"


def combined_review_status(
    regular: Optional[PassageVideoLocation],
    high_speed: Optional[PassageVideoLocation],
) -> str:
    if any(
        location is not None and location.status in _CONFIRMABLE_STATUSES
        for location in (regular, high_speed)
    ):
        return "待核对"
    return "受阻"


class _StatusColorDelegate(QStyledItemDelegate):
    """Preserve status colors when a table row is selected."""

    def initStyleOption(self, option, index) -> None:
        super().initStyleOption(option, index)
        foreground = index.data(Qt.ForegroundRole)
        if isinstance(foreground, QBrush):
            option.palette.setBrush(QPalette.Text, foreground)
            option.palette.setBrush(QPalette.HighlightedText, foreground)


_TABLE_COLUMN_MIN_WIDTHS = (58, 70, 120, 120, 58, 140, 104, 104, 104)
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
    target_width = max(content_total, int(available_width))
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
        self._compact_mode = False

    def set_compact_mode(self, enabled: bool) -> None:
        self._compact_mode = bool(enabled)
        self.schedule_auto_fit()

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
        visible_columns = tuple(
            column
            for column in range(self.columnCount())
            if not self.isColumnHidden(column)
        )
        if self._compact_mode:
            compact_widths = {0: 52, 1: 92, 2: 140, 5: 132, 8: 118}
            for column in visible_columns:
                header.resizeSection(column, compact_widths.get(column, 84))
            return
        content_widths = tuple(
            header.sectionSize(column) for column in visible_columns
        )
        minimum_widths = tuple(
            _TABLE_COLUMN_MIN_WIDTHS[column] for column in visible_columns
        )
        available_width = max(0, self.viewport().width() - 1)
        widths = _expanded_column_widths(
            content_widths,
            minimum_widths,
            available_width,
        )
        for column, width in zip(visible_columns, widths):
            # Chinese athlete names need only a compact, predictable column;
            # do not let proportional spare-space expansion make it dominant.
            if column == 2:
                width = min(width, 150)
            header.resizeSection(column, width)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.schedule_auto_fit()


def review_status_text(
    lookup: PassageVideoLookup,
    regular: Optional[PassageVideoLocation],
    high_speed: Optional[PassageVideoLocation],
) -> str:
    return combined_review_status(regular, high_speed)


class EvidenceImageView(QGraphicsView):
    """Source-resolution image view with persistent zoom and pan state."""

    zoom_changed = pyqtSignal(int)
    full_resolution_requested = pyqtSignal()
    maximize_requested = pyqtSignal()
    activated = pyqtSignal()
    marker_position_selected = pyqtSignal(float, float)
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
        self._marker_mode = bool(enabled) and self.has_frame
        self.viewport().setCursor(
            Qt.CrossCursor if self._marker_mode else Qt.ArrowCursor
        )
        self.setToolTip(
            "单击放置判读线；Shift + 左键拖动判读线；"
            "中键拖动缩放后的画面"
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
                self.scrub_finished.emit(event.pos().x() - press_position.x())
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

    def drawForeground(self, painter: QPainter, rect: QRectF) -> None:
        super().drawForeground(painter, rect)
        if self._marker is None or self._source_width <= 0 or self._source_height <= 0:
            return
        x_normalized, y_normalized, label, confirmed = self._marker
        simple = self._marker_simple
        x = x_normalized * self._source_width
        y = y_normalized * self._source_height
        scale = max(0.001, abs(self.transform().m11()))
        color = QColor("#1bbf83" if confirmed else "#c0372b")
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


class PassageEvidencePane(QFrame):
    open_requested = pyqtSignal(object, object)
    step_requested = pyqtSignal(int)
    play_requested = pyqtSignal()
    passage_delta_requested = pyqtSignal(int)
    selection_step_requested = pyqtSignal(int)
    position_changed = pyqtSignal(int)
    maximize_requested = pyqtSignal(object)
    marking_requested = pyqtSignal(object)
    confirmation_requested = pyqtSignal(object)
    cancel_requested = pyqtSignal(object)
    delete_requested = pyqtSignal(object)
    scrub_started = pyqtSignal()
    scrub_preview_requested = pyqtSignal(int)
    initial_frame_ready = pyqtSignal(str)
    preview_frame_ready = pyqtSignal(object, int, int)

    MAX_SCRUB_SPAN_MS = 6_000
    SCRUB_PREVIEW_INTERVAL_MS = 80
    FULL_RESOLUTION_IDLE_MS = 200
    MIN_LINKED_DRIFT_TOLERANCE_MS = 40
    LINKED_DRIFT_FRAME_MULTIPLIER = 1
    STATUS_COLORS = {
        "已确认": "#16845b",
        "未确认": "#c0372b",
        "参考": "#667085",
        "录像处理中": "#a56300",
        "未定位": "#a56300",
        "无录像": "#c0372b",
        "文件缺失": "#c0372b",
        "未提供": "#667085",
        "未选择": "#667085",
    }

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
        self._lookup_status = ""
        self._association: Optional[PassageEvidenceAssociation] = None
        self._reference_only = False
        self._pending_marker: Optional[tuple[float, float, int, int]] = None
        self._marking_enabled = False
        self._identity = ""
        self._worker: Optional[object] = None
        self._idle_prefetch_enabled = False
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
        self._video_scrubbing = False
        self._pending_scrub_delta_ms: Optional[int] = None
        self._scrub_preview_timer = QTimer(self)
        self._scrub_preview_timer.setSingleShot(True)
        self._scrub_preview_timer.setInterval(self.SCRUB_PREVIEW_INTERVAL_MS)
        self._scrub_preview_timer.timeout.connect(self._flush_scrub_preview)
        self._full_resolution_timer = QTimer(self)
        self._full_resolution_timer.setSingleShot(True)
        self._full_resolution_timer.setInterval(self.FULL_RESOLUTION_IDLE_MS)
        self._full_resolution_timer.timeout.connect(
            self._flush_full_resolution_request
        )

        self.setObjectName("passageEvidencePane")
        self.setFrameShape(QFrame.StyledPanel)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        header = QHBoxLayout()
        self.title_label = QLabel(title)
        self.title_label.setObjectName("evidencePaneTitle")
        self.active_badge = QLabel("当前判读")
        self.active_badge.setObjectName("activeJudgingBadge")
        self.active_badge.hide()
        self.frame_indicator_label = QLabel("当前帧 -- | --:--:--.---")
        self.frame_indicator_label.setObjectName("evidenceFrameIndicator")
        self.frame_indicator_label.setStyleSheet(
            "QLabel#evidenceFrameIndicator {"
            " background: #092333; color: #8feaff;"
            " border: 1px solid #00a9d6; border-radius: 3px;"
            " padding: 2px 7px; font-size: 10pt; font-weight: 700;"
            "}"
        )
        self.frame_indicator_label.setFixedSize(250, 30)
        self.camera_combo = QComboBox(self)
        self.camera_combo.setMinimumWidth(88)
        self.camera_combo.setToolTip("切换普通录像机位")
        self.camera_combo.hide()
        self.status_label = QLabel("未选择")
        self.status_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.status_label.setObjectName("evidencePaneStatus")
        self._set_status_label("未选择")
        header.addWidget(self.title_label)
        header.addWidget(self.active_badge)
        header.addWidget(self.frame_indicator_label)
        header.addWidget(self.camera_combo)
        header.addStretch()
        header.addWidget(self.status_label)
        layout.addLayout(header)

        self.video_view = EvidenceImageView(self)
        self.video_label = self.video_view
        self.video_view.clear_frame("选择一条通过记录后自动定位")
        self.video_view.zoom_changed.connect(self._on_zoom_changed)
        self.video_view.full_resolution_requested.connect(
            self._schedule_full_resolution_request
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
        self.video_view.scrub_finished.connect(self._on_video_scrub_finished)
        layout.addWidget(self.video_view, 1)

        self.timeline = TargetTimelineSlider(Qt.Horizontal, self)
        self.timeline.setInvertedAppearance(True)
        self.timeline.setInvertedControls(True)
        self.timeline.setRange(0, 0)
        self.timeline.setEnabled(False)
        self.timeline.setFocusPolicy(Qt.NoFocus)
        self.timeline.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.timeline.setToolTip("使用左右方向键逐帧查看")
        self.timeline.sliderPressed.connect(self._on_timeline_pressed)
        self.timeline.sliderMoved.connect(self._on_timeline_moved)
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
        self.maximize_btn = QPushButton("放大")
        self.maximize_btn.setToolTip("放大该机位（双击画面或按 F）")
        self.mark_btn = QPushButton("标线")
        self.mark_btn.setToolTip("在画面中按住左键移动身份判读线")
        self.confirm_btn = QPushButton("确认")
        self.confirm_btn.setToolTip("保存当前帧和标线位置为人工判罚结果")
        self.confirm_btn.setEnabled(False)
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
        controls.addWidget(self.confirm_btn)
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
        self._maximize_shortcut = QShortcut(QKeySequence(Qt.Key_F), self)
        self._maximize_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self._maximize_shortcut.setAutoRepeat(False)
        self._maximize_shortcut.activated.connect(
            lambda: self.maximize_requested.emit(self)
        )
        self.mark_btn.clicked.connect(lambda: self.marking_requested.emit(self))
        self.confirm_btn.clicked.connect(
            lambda: self.confirmation_requested.emit(self)
        )
        self.open_btn.clicked.connect(self._request_open)
        self._frame_step_shortcuts = []
        for sequence, frame_delta in (
            ("Left", -1),
            ("Right", 1),
            ("Shift+Left", -SHIFT_FRAME_STEP),
            ("Shift+Right", SHIFT_FRAME_STEP),
            ("Ctrl+Left", -CTRL_FRAME_STEP),
            ("Ctrl+Right", CTRL_FRAME_STEP),
        ):
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.setContext(Qt.WidgetWithChildrenShortcut)
            shortcut.setAutoRepeat(False)
            shortcut.activated.connect(
                lambda delta=frame_delta: self.step_requested.emit(delta)
            )
            self._frame_step_shortcuts.append(shortcut)
        self._set_transport_enabled(False)

    def set_active_judging(self, active: bool) -> None:
        active = bool(active)
        if self.property("activeJudging") == active:
            return
        self.setProperty("activeJudging", active)
        self.active_badge.setVisible(active)
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

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
        if (
            self._worker is None
            or not self.video_view.has_frame
            or self._location is None
            or self._location.status not in _CONFIRMABLE_STATUSES
        ):
            return
        self._marking_enabled = True
        self._playing = False
        self.play_btn.setText("▶")
        self._worker.pause()
        self.mark_btn.setText("拖动标线")
        self.confirm_btn.setEnabled(False)
        self.video_view.set_marker_mode(True)
        self.video_view.setFocus(Qt.ShortcutFocusReason)

    def cancel_marker_edit(self) -> None:
        self._pending_marker = None
        self._marking_enabled = (
            self._association is None
            and self._location is not None
            and self._location.status in _CONFIRMABLE_STATUSES
        )
        self.mark_btn.setText("重标" if self._association is not None else "标线")
        self.confirm_btn.setEnabled(False)
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
        self._marking_enabled = (
            association is None
            and self._location is not None
            and self._location.status in _CONFIRMABLE_STATUSES
        )
        self.video_view.set_marker_mode(
            self.video_view.has_frame and self._marking_enabled
        )
        self.mark_btn.setText("重标" if association is not None else "标线")
        self.confirm_btn.setEnabled(False)
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
        if (
            location is None
            or location.status not in _CONFIRMABLE_STATUSES
            or marker is None
        ):
            return None
        x_normalized, y_normalized, frame_index, position_ms = marker
        return {
            "segment_id": location.segment.segment_id,
            "frame_index": frame_index,
            "position_ms": position_ms,
            "marker_x_normalized": x_normalized,
            "marker_y_normalized": y_normalized,
        }

    def set_external_pending_marker(
        self,
        *,
        frame_index: int,
        position_ms: int,
        marker_x_normalized: float,
        marker_y_normalized: float,
    ) -> None:
        """Accept a marker selected on the primary time-film interface."""

        self._pending_marker = (
            max(0.0, min(1.0, float(marker_x_normalized))),
            max(0.0, min(1.0, float(marker_y_normalized))),
            int(frame_index),
            int(position_ms),
        )
        self._marking_enabled = True
        self.confirm_btn.setEnabled(True)
        self._render_marker()

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
        self.confirm_btn.setEnabled(True)
        self._render_marker()

    def _update_status_label(self) -> None:
        status = self._availability_status()
        detail = ""
        if self._location is not None:
            detail = location_status_text(self._location)
        if not detail and self._lookup_status:
            detail = _STATUS_TEXT.get(self._lookup_status, self._lookup_status)
        if self._association is not None:
            detail = f"{detail} · 已标记 {self._identity}" if detail else "已标记"
        self._set_status_label(status, detail)

    def _availability_status(self) -> str:
        if self._association is not None:
            return "已确认"
        location = self._location
        if (
            self._reference_only
            and location is not None
            and location.status in _OPENABLE_STATUSES
        ):
            return "参考"
        if location is not None and location.status == "preview":
            return "录像处理中"
        if location is not None and location.status in _OPENABLE_STATUSES:
            return "未确认"
        if location is not None and location.status == "recording":
            return "录像处理中"
        if location is not None and location.status == "missing_file":
            return "文件缺失"
        if self.source_kind == HIGH_SPEED_SOURCE and location is None:
            return "未提供"
        if self._lookup_status == "no_segments":
            return "无录像"
        return "未定位"

    def _set_status_label(self, status: str, tooltip: str = "") -> None:
        color = self.STATUS_COLORS.get(status, "#667085")
        self.status_label.setText(status)
        self.status_label.setToolTip(tooltip)
        self.status_label.setStyleSheet(
            f"color: {color}; font-size: 9pt; font-weight: 700;"
        )

    def _empty_message(
        self,
        location: Optional[PassageVideoLocation],
    ) -> str:
        source = "普通录像" if self.source_kind == REGULAR_SOURCE else "高速画面"
        if location is not None and location.status == "recording":
            return f"{source}正在录制，等待片段封口"
        if location is not None and location.status == "missing_file":
            return f"{source}文件缺失"
        if self.source_kind == HIGH_SPEED_SOURCE and location is None:
            return "暂无高速画面"
        if self._lookup_status == "no_segments":
            return "当前赛事没有普通录像"
        if self._lookup_status in {
            "before_recording",
            "after_recording",
            "recording_gap",
            "race_mismatch",
        } or (location is not None and location.status == "outside_media"):
            return "未定位到对应普通录像"
        return f"暂无{source}"

    def _locating_message(self) -> str:
        source = "普通录像" if self.source_kind == REGULAR_SOURCE else "高速画面"
        return f"正在定位{source}"

    def _render_marker(self) -> None:
        marker = self._pending_marker
        if self.is_auyat_rgb:
            self.video_view.clear_identity_cue()
        else:
            if self._association is not None:
                cue_status = "已确认"
            elif self._reference_only:
                cue_status = "参考"
            else:
                cue_status = "未确认"
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
        lookup_status: str = "",
        defer_worker_start: bool = False,
    ) -> None:
        previous_context = self._media_context(self._location)
        previous_event_id = self._event.event_id if self._event is not None else ""
        next_context = self._media_context(location)
        same_context = bool(previous_context) and previous_context == next_context
        self._event = event
        self._location = location
        self._lookup_status = str(
            lookup_status or (location.status if location is not None else "")
        )
        self._identity = event.bib.strip() or "未知"
        if association is not None and not _association_matches_location(
            association, location
        ):
            association = None
        self._association = association
        self._pending_marker = None
        self._marking_enabled = (
            association is None
            and location is not None
            and location.status in _CONFIRMABLE_STATUSES
        )
        self._playing = False
        self._current_frame_index = -1
        self._current_position_ms = 0
        self._last_full_resolution_request = -1
        self._full_resolution_timer.stop()
        self._reset_video_scrub()
        self.play_btn.setText("▶")
        self.mark_btn.setText("重标" if association is not None else "标线")
        self.video_view.set_marker_mode(False)
        self.video_view.clear_marker()
        if not same_context or previous_event_id != event.event_id:
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
            detail = location_status_text(location) if location is not None else ""
            if not detail and self._lookup_status:
                detail = _STATUS_TEXT.get(self._lookup_status, self._lookup_status)
            self._set_status_label(self._availability_status(), detail)
            self.video_label.clear_frame(self._empty_message(location))
            self.time_label.setText("--:--:--.---")
            self.frame_indicator_label.setText("当前帧 -- | --:--:--.---")
            self.video_view.clear_frame_indicator()
            self._set_transport_enabled(False)
            return

        self._target_position_ms = int(location.passage_position_ms)
        self._update_status_label()
        if not same_context:
            self.video_label.clear_frame(self._locating_message())
        self.time_label.setText(f"目标 {self._target_position_ms / 1000.0:.3f} s")
        if association is not None:
            # Preserve a previously confirmed frame instead of moving it back
            # to the generic preview lead-in.
            initial_position_ms = max(0, int(association.position_ms))
        elif str(getattr(location, "media_locator", "")):
            # A persisted clip binding already has an operator-selected origin.
            initial_position_ms = max(
                0,
                self._target_position_ms + int(initial_delta_ms),
            )
        elif self.source_kind == REGULAR_SOURCE:
            # Start before the passage so a rider who has already crossed the
            # line at the target frame remains visible for manual review.
            initial_position_ms = max(
                0,
                int(location.playback_position_ms) + int(initial_delta_ms),
            )
        else:
            initial_position_ms = max(
                0,
                self._target_position_ms + int(initial_delta_ms),
            )

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
        self.timeline.setProperty("initial_position_ms", int(initial_position_ms))
        worker = (
            AuyatRgbPlaybackWorker(location, self)
            if location.segment.clock_source == AUYAT_CLOCK_SOURCE
            else VideoPlaybackWorker(location.video_path, self)
        )
        worker.media_locator = str(location.media_locator)
        set_idle_prefetch = getattr(worker, "set_idle_prefetch_enabled", None)
        if callable(set_idle_prefetch):
            set_idle_prefetch(self._idle_prefetch_enabled)
        worker.pause()
        worker.metadata_ready.connect(self._on_metadata_ready)
        worker.frame_ready.connect(self._on_frame_ready)
        worker.full_resolution_ready.connect(self._on_full_resolution_ready)
        worker.playback_finished.connect(self._on_playback_finished)
        worker.playback_error.connect(self._on_playback_error)
        self._worker = worker
        if not defer_worker_start:
            track_qthread(worker)
            worker.start()

    def rebind_passage(
        self,
        event: PassageEvent,
        location: Optional[PassageVideoLocation],
        association: Optional[PassageEvidenceAssociation] = None,
        *,
        lookup_status: str = "",
    ) -> bool:
        """Change the athlete identity while keeping the current video frame."""

        if self._media_context(location) != self._media_context(self._location):
            return False
        if location is None or location.status not in _OPENABLE_STATUSES:
            return False
        if (
            association is not None
            and association.segment_id != location.segment.segment_id
        ):
            association = None
        self._event = event
        self._location = location
        self._target_position_ms = int(location.passage_position_ms)
        self.timeline.set_target_position(self._target_position_ms)
        if self._duration_ms > 0:
            self.timeline.setValue(
                max(0, min(self._current_position_ms, self._duration_ms))
            )
        self._lookup_status = str(lookup_status or location.status)
        self._identity = event.bib.strip() or "鏈煡"
        self._association = association
        self._pending_marker = None
        self._marking_enabled = association is None
        self._reference_only = False
        self.mark_btn.setText("閲嶆爣" if association is not None else "鏍囩嚎")
        self.video_view.set_marker_mode(
            self.video_view.has_frame and self._marking_enabled
        )
        self._update_status_label()
        self._render_marker()
        self._update_time_label(self._current_position_ms)
        self._update_frame_indicator(self._current_position_ms)
        return True

    @property
    def has_playback_worker(self) -> bool:
        return self._worker is not None

    @property
    def worker_start_deferred(self) -> bool:
        worker = self._worker
        return worker is not None and not worker.isRunning()

    def start_deferred_worker(self) -> bool:
        worker = self._worker
        if worker is None or worker.isRunning():
            return False
        track_qthread(worker)
        worker.start()
        return True

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
        self._lookup_status = ""
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
        self.mark_btn.setText("标线")
        self._set_status_label("未选择")
        self.time_label.setText("--:--:--.---")
        self.video_view.set_marker_mode(False)
        self.video_view.clear_marker()
        self.video_label.clear_frame(message)
        self.frame_indicator_label.setText("当前帧 -- | --:--:--.---")
        self.video_view.clear_frame_indicator()
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
        initial_position_ms = int(self.timeline.property("initial_position_ms") or 0)
        target_ms = min(max(0, initial_position_ms), max(0, duration_ms))
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
        self._update_status_label()
        self._set_transport_enabled(True)
        self.video_view.set_marker_mode(self._marking_enabled)
        self._render_marker()
        if not self._timeline_dragging:
            self.timeline.setValue(max(0, min(int(position_ms), self._duration_ms)))
        self._update_time_label(position_ms)
        self._update_frame_indicator(position_ms)
        self.position_changed.emit(
            self._current_position_ms - self._target_position_ms
        )
        self.preview_frame_ready.emit(image, self._current_position_ms, self._current_frame_index)
        if (
            not self._playing
            and not self._video_scrubbing
            and self.video_view.zoom_percent >= 100
        ):
            self._schedule_full_resolution_request()
        if previous_frame_index < 0 and self._event is not None:
            self.initial_frame_ready.emit(self._event.event_id)

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
        self._update_frame_indicator(position_ms)
        self.preview_frame_ready.emit(image, int(position_ms), int(frame_index))

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

    def _update_frame_indicator(self, position_ms: int) -> None:
        event = self._event
        if event is None or self._current_frame_index < 0:
            self.frame_indicator_label.setText("当前帧 -- | --:--:--.---")
            self.video_view.clear_frame_indicator()
            return
        delta_ms = int(position_ms) - int(self._target_position_ms)
        timestamp_ms = int(event.timeline_timestamp_ms) + delta_ms
        text = (
            f"帧 {self._current_frame_index + 1}"
            f" | {format_passage_time(timestamp_ms)}"
            f" | Δ{delta_ms:+d}ms"
        )
        self.frame_indicator_label.setText(text)
        # The pane header is the single authoritative frame/time display.
        # Avoid repeating the same text over the video image.
        self.video_view.clear_frame_indicator()

    def _request_full_resolution(self) -> None:
        worker = self._worker
        frame_index = self._current_frame_index
        if worker is None or frame_index < 0 or self._video_scrubbing:
            return
        if frame_index == self._last_full_resolution_request:
            return
        self._last_full_resolution_request = frame_index
        worker.request_full_resolution(frame_index)

    def _schedule_full_resolution_request(self) -> None:
        if (
            self._worker is None
            or self._current_frame_index < 0
            or self._playing
            or self._video_scrubbing
        ):
            return
        self._full_resolution_timer.start()

    def _flush_full_resolution_request(self) -> None:
        self._request_full_resolution()

    def _on_zoom_changed(self, percent: int) -> None:
        self.actual_size_btn.setText(f"{int(percent)}%")

    def _on_timeline_pressed(self) -> None:
        self._timeline_dragging = True
        self.scrub_started.emit()

    def _on_timeline_moved(self, position_ms: int) -> None:
        if not self._timeline_dragging:
            return
        position_ms = int(position_ms)
        self._update_time_label(position_ms)
        self.passage_delta_requested.emit(
            position_ms - self._target_position_ms
        )

    def _on_timeline_released(self) -> None:
        self._timeline_dragging = False
        self.passage_delta_requested.emit(
            int(self.timeline.value()) - self._target_position_ms
        )

    def _on_video_scrub_started(self) -> None:
        self._reset_video_scrub()
        self._video_scrubbing = True
        self._last_full_resolution_request = -1
        self._scrub_origin_delta_ms = (
            self._current_position_ms - self._target_position_ms
        )
        self.scrub_started.emit()

    def _on_video_scrub_delta(self, horizontal_pixels: int) -> None:
        delta_ms = self._scrub_delta_ms(horizontal_pixels)
        self._pending_scrub_delta_ms = delta_ms
        if not self._scrub_preview_timer.isActive():
            self._scrub_preview_timer.start()

    def _scrub_delta_ms(self, horizontal_pixels: int) -> int:
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
        return self._scrub_origin_delta_ms + frame_delta * frame_ms

    def _flush_scrub_preview(self) -> None:
        delta_ms = self._pending_scrub_delta_ms
        self._pending_scrub_delta_ms = None
        if delta_ms is not None and self._video_scrubbing:
            self.scrub_preview_requested.emit(delta_ms)

    def _on_video_scrub_finished(self, horizontal_pixels: int) -> None:
        final_delta_ms = self._scrub_delta_ms(horizontal_pixels)
        self._scrub_preview_timer.stop()
        self._pending_scrub_delta_ms = None
        self._video_scrubbing = False
        self.passage_delta_requested.emit(final_delta_ms)

    def _reset_video_scrub(self) -> None:
        self._scrub_preview_timer.stop()
        self._full_resolution_timer.stop()
        self._pending_scrub_delta_ms = None
        self._video_scrubbing = False

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
        self._set_status_label(self._availability_status(), message)
        self.video_label.clear_frame("画面读取失败")
        self._set_transport_enabled(False)
        self.open_btn.setEnabled(
            self.source_kind == REGULAR_SOURCE
            and self._location is not None
            and self._location.status in _CONFIRMABLE_STATUSES
            and self._location.video_path.is_file()
        )

    def set_playing(self, playing: bool) -> None:
        worker = self._worker
        if worker is None:
            return
        if playing:
            self.start_deferred_worker()
        self._full_resolution_timer.stop()
        self._playing = bool(playing)
        if self._playing:
            worker.set_shuttle_speed(1.0)
            self.play_btn.setText("Ⅱ")
        else:
            worker.pause()
            self.play_btn.setText("▶")

    def toggle_playing(self) -> None:
        self.set_playing(not self._playing)

    @property
    def is_playing(self) -> bool:
        return self._playing

    def step(self, frame_delta: int) -> None:
        worker = self._worker
        if worker is None:
            return
        self.start_deferred_worker()
        self._full_resolution_timer.stop()
        self._last_full_resolution_request = -1
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
        preview: bool = False,
    ) -> None:
        worker = self._worker
        if worker is None:
            return
        self.start_deferred_worker()
        self._full_resolution_timer.stop()
        self._last_full_resolution_request = -1
        bounds = self.available_delta_bounds()
        if bounds is None:
            return
        lower, upper = bounds
        clamped_delta_ms = max(lower, min(int(delta_ms), upper))
        if preview:
            self._video_scrubbing = True
            self._last_full_resolution_request = -1
        position_ms = self._target_position_ms + clamped_delta_ms
        self._playing = bool(linked_playing)
        seek_and_play = getattr(worker, "seek_and_play", None)
        if self._playing and callable(seek_and_play):
            seek_and_play(position_ms, 1.0)
        else:
            worker.pause()
            preview_seek = getattr(worker, "seek_preview", None)
            if preview and callable(preview_seek):
                preview_seek(position_ms)
            else:
                worker.seek(position_ms)
            if self._playing:
                worker.set_shuttle_speed(1.0)
        self.play_btn.setText("Ⅱ" if self._playing else "▶")

    def set_linked_playing(self, playing: bool) -> None:
        self._playing = bool(playing)
        worker = self._worker
        if worker is not None:
            if self._playing:
                worker.set_shuttle_speed(1.0)
            else:
                worker.pause()
        self.play_btn.setText("Ⅱ" if self._playing else "▶")

    def linked_drift_ms(self, delta_ms: int) -> Optional[int]:
        bounds = self.available_delta_bounds()
        if bounds is None:
            return None
        lower, upper = bounds
        clamped_delta_ms = max(lower, min(int(delta_ms), upper))
        expected_position_ms = self._target_position_ms + clamped_delta_ms
        current_position_ms = self._current_position_ms
        worker = self._worker
        if worker is not None:
            worker_position_ms = getattr(worker, "current_position_ms", None)
            if isinstance(worker_position_ms, (int, float)):
                current_position_ms = int(worker_position_ms)
        return current_position_ms - expected_position_ms

    def current_delta_ms(self) -> Optional[int]:
        bounds = self.available_delta_bounds()
        if bounds is None:
            return None
        current_frame_index = self._current_frame_index
        current_position_ms = self._current_position_ms
        worker = self._worker
        if worker is not None:
            worker_frame_index = getattr(worker, "current_frame_index", None)
            if isinstance(worker_frame_index, int):
                current_frame_index = worker_frame_index
            worker_position_ms = getattr(worker, "current_position_ms", None)
            if isinstance(worker_position_ms, (int, float)):
                current_position_ms = int(worker_position_ms)
        if current_frame_index < 0:
            return None
        lower, upper = bounds
        return max(
            lower,
            min(current_position_ms - self._target_position_ms, upper),
        )

    def release_playback_cache(self) -> None:
        worker = self._worker
        if worker is None:
            return
        release_cache = getattr(worker, "release_cache", None)
        if callable(release_cache):
            release_cache()

    def park_playback_cache(self) -> None:
        worker = self._worker
        if worker is None:
            return
        park_cache = getattr(worker, "park_cache", None)
        if callable(park_cache):
            park_cache()
            return
        release_cache = getattr(worker, "release_cache", None)
        if callable(release_cache):
            release_cache()

    def set_idle_prefetch_enabled(self, enabled: bool) -> None:
        self._idle_prefetch_enabled = bool(enabled)
        worker = self._worker
        if worker is None:
            return
        set_enabled = getattr(worker, "set_idle_prefetch_enabled", None)
        if callable(set_enabled):
            set_enabled(self._idle_prefetch_enabled)

    def linked_drift_tolerance_ms(self) -> int:
        return max(
            self.MIN_LINKED_DRIFT_TOLERANCE_MS,
            self.frame_duration_ms() * self.LINKED_DRIFT_FRAME_MULTIPLIER,
        )

    def _set_transport_enabled(self, enabled: bool) -> None:
        self.previous_frame_btn.setEnabled(enabled)
        self.play_btn.setEnabled(enabled)
        self.next_frame_btn.setEnabled(enabled)
        self.mark_btn.setEnabled(
            enabled
            and self.video_view.has_frame
            and self._location is not None
            and self._location.status in _CONFIRMABLE_STATUSES
        )
        self.confirm_btn.setEnabled(enabled and self.pending_confirmation() is not None)
        self.open_btn.setEnabled(
            enabled
            and self.source_kind == REGULAR_SOURCE
            and self._location is not None
            and self._location.status in _CONFIRMABLE_STATUSES
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
        worker.request_stop()
        if wait:
            worker.wait(2_000)
        if worker.isRunning():
            retire_qthread(worker)
        else:
            worker.deleteLater()

    def _stop_worker(self, *, wait: bool = False) -> None:
        self._reset_video_scrub()
        worker = self._worker
        self._worker = None
        if worker is None:
            return
        self._retire_worker(worker, wait=wait)

    def shutdown(self, *, wait: bool = True) -> None:
        self._stop_worker(wait=wait)


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


class PassageReviewSurface(QDialog):
    """Shared Qt review surface used by the standalone and formal windows.

    This class is an implementation surface rather than an application window;
    concrete windows are siblings so neither owns the other's lifecycle.
    """
    INACTIVE_CAMERA_START_DELAY_MS = 120
    FILMSTRIP_WINDOW_MS = 300_000
    FILMSTRIP_ALWAYS_AVAILABLE = False
    VIDEO_ASSIST_ENABLED = True
    # Automatic gap seeking can hide riders whose chips were not read. Keep it
    # disabled; operators can use the guarded Shift/Ctrl shortcuts while
    # watching the video.
    CONTINUOUS_AUTO_SKIP = False
    CONTINUOUS_SKIP_GAP_MS = 2_000
    CONTINUOUS_SKIP_LEAD_MS = 2_000
    CONTINUOUS_ROSTER_SIZE = 24
    SINGLE_CAMERA_PREVIEW = True

    clock_offset_changed = pyqtSignal(int)
    evidence_pane_added = pyqtSignal(object)
    video_review_updated = pyqtSignal(object)
    video_candidates_received = pyqtSignal(object)
    video_candidate_requested = pyqtSignal(object)
    video_review_apply_requested = pyqtSignal(object)
    video_review_status_changed = pyqtSignal(str, str, str)

    SYNC_STARTUP_GRACE_MS = 250
    SYNC_CORRECTION_COOLDOWN_SECONDS = 0.5
    REVIEW_BATCH_GAP_MS = DEFAULT_REVIEW_GAP_MS
    REVIEW_SUBWAVE_GAP_MS = DEFAULT_SUBWAVE_GAP_MS

    def __init__(
        self,
        passage_store: PassageEventStore,
        timeline_store: VideoTimelineStore,
        parent=None,
        *,
        clock_offset_ms: int = 0,
        clock_offset_by_camera: Optional[Mapping[int, int]] = None,
        pre_roll_ms: int = 3_000,
        association_store: Optional[PassageEvidenceAssociationStore] = None,
        calibration_store: Optional[VideoClockCalibrationStore] = None,
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
        review_binding_store: Optional[PassageReviewBindingStore] = None,
        video_discovery_store: Optional[VideoDiscoveryStore] = None,
        video_arrival_store: Optional[VideoArrivalCandidateStore] = None,
    ):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.Window
            | Qt.WindowTitleHint
            | Qt.WindowSystemMenuHint
            | Qt.WindowMinMaxButtonsHint
            | Qt.WindowCloseButtonHint
        )
        self.passage_store = passage_store
        self.timeline_store = timeline_store
        self.review_binding_store = review_binding_store
        self.video_discovery_store = video_discovery_store or VideoDiscoveryStore(
            passage_store.journal_path.with_name("video_discoveries.jsonl")
        )
        self.video_arrival_store = video_arrival_store or VideoArrivalCandidateStore(
            passage_store.journal_path.with_name("video_arrival_candidates.jsonl")
        )
        self.association_store = association_store or PassageEvidenceAssociationStore(
            passage_store.journal_path.with_name("passage_evidence_associations.jsonl")
        )
        self.calibration_store = calibration_store or VideoClockCalibrationStore(
            passage_store.journal_path.with_name("video_clock_calibrations.jsonl")
        )
        self.metadata_store = metadata_store
        self.clock_offset_ms = int(clock_offset_ms)
        self._clock_offset_by_camera = {
            int(camera): int(offset)
            for camera, offset in (clock_offset_by_camera or {}).items()
        }
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
        self._last_sync_correction_at = 0.0
        self._sync_timer = QTimer(self)
        self._sync_timer.setInterval(30)
        self._sync_timer.timeout.connect(self._on_sync_tick)
        self._active_pane: Optional[PassageEvidencePane] = None
        self._selection_event_id = ""
        self._selection_started_at = 0.0
        self._selection_first_frame_ms = 0.0
        self._selection_expected_panes = 0
        self._selection_pending_panes: set[PassageEvidencePane] = set()
        self._deferred_selection_panes: set[PassageEvidencePane] = set()
        self._selection_priority_pane: Optional[PassageEvidencePane] = None
        self._deferred_start_timer = QTimer(self)
        self._deferred_start_timer.setSingleShot(True)
        self._deferred_start_timer.setInterval(
            self.INACTIVE_CAMERA_START_DELAY_MS
        )
        self._deferred_start_timer.timeout.connect(
            self._start_deferred_selection_panes
        )
        self._maximized_pane: Optional[PassageEvidencePane] = None
        self._maximized_window: Optional[QDialog] = None
        self._maximized_escape_shortcut: Optional[QShortcut] = None
        self._maximized_pane_index = -1
        self._maximized_content_splitter: Optional[QSplitter] = None
        self._maximized_original_indexes: dict[PassageEvidencePane, int] = {}
        self._maximized_hosted_panes: tuple[PassageEvidencePane, ...] = ()
        self._maximized_mode_label: Optional[QLabel] = None
        self._maximized_mode_buttons: dict[object, QPushButton] = {}
        self._maximized_camera_shortcuts: list[QShortcut] = []
        self._available_evidence_count = 0
        self._located_event_ids: set[str] = set()
        self._confirmed_event_ids: set[str] = set()
        self._event_review_statuses: dict[str, str] = {}
        self._review_batches: tuple[PassageReviewBatch, ...] = ()
        self._review_batch_by_event_id: dict[str, PassageReviewBatch] = {}
        self._active_review_batch_id = ""
        self._batch_mode = False
        self._auto_advance_before_continuous = False
        self._continuous_clock_offsets: dict[tuple[int, str], int] = {}
        self._continuous_calibration_revision = 0
        # Video candidates are advisory only; they never enter PassageEventStore.
        self._video_reconciliation: tuple[object, ...] = ()
        self._video_reconciliation_by_id: dict[str, object] = {}
        # Keep every visual candidate for fast Ctrl+Left/Right navigation.
        # This is intentionally separate from the anomaly-only review list.
        self._video_navigation_candidates: tuple[object, ...] = tuple(
            self.video_arrival_store.candidates()
        )
        self._video_arrival_batch_gap_ms = DEFAULT_ARRIVAL_BATCH_GAP_MS
        self._video_arrival_subwave_gap_ms = DEFAULT_ARRIVAL_SUBWAVE_GAP_MS
        self._video_arrival_batches: tuple[VideoArrivalBatch, ...] = (
            build_video_arrival_batches(
                self._video_navigation_candidates,
                batch_gap_ms=self._video_arrival_batch_gap_ms,
                subwave_gap_ms=self._video_arrival_subwave_gap_ms,
            )
        )
        self._video_review_statuses: dict[str, str] = {}
        self._video_review_bibs: dict[str, str] = {}
        self._active_video_anomaly_id = ""
        self._video_anomaly_cursor = -1
        self._video_anomaly_dialog: Optional[QDialog] = None
        self._video_anomaly_dialog_refresh: Optional[Callable[[], None]] = None
        self._video_arrival_dialog: Optional[QDialog] = None
        self._video_arrival_dialog_refresh: Optional[Callable[[], None]] = None
        self._video_discovered_entries: list[dict[str, object]] = [
            {
                "entry_id": record.discovery_id,
                "race_id": record.race_id,
                "stage_id": record.stage_id,
                "batch_id": record.batch_id,
                "bib": record.bib,
                "camera_index": record.camera_index,
                "frame_index": record.frame_index,
                "position_ms": record.position_ms,
                "started_at_ms": record.started_at_ms,
                "ended_at_ms": record.ended_at_ms,
                "status": record.status,
            }
            for record in self.video_discovery_store.records()
        ]
        self._active_video_discovered_entry_id = ""
        self._active_review_filter = "all"
        self._total_event_count = 0
        self._queue_expanded = False
        self._queue_default_sizes: list[int] = []
        # The filmstrip is the fast locating surface; the lower camera pane is
        # the frame-accurate judgment surface. Do not render the duplicate
        # large preview in continuous review mode.
        self._top_preview_video_visible = False
        self._metadata_context_key: tuple[str, str] = ("", "")
        self._search_refresh_timer = QTimer(self)
        self._search_refresh_timer.setSingleShot(True)
        self._search_refresh_timer.setInterval(120)
        self._search_refresh_timer.timeout.connect(self._refresh_filtered_view)
        self._filmstrip_context: tuple[Path, int, int] | None = None
        self._filmstrip_absolute_window: tuple[int, int] | None = None
        self._review_split_resize_pending = False
        self._pending_filmstrip_position: int | None = None
        self._filmstrip_seek_pending = False
        self._filmstrip_seek_retry_count = 0
        self._pending_filmstrip_preview_position: int | None = None
        self._filmstrip_preview_timer = QTimer(self)
        self._filmstrip_preview_timer.setSingleShot(True)
        self._filmstrip_preview_timer.setInterval(80)
        self._filmstrip_preview_timer.timeout.connect(
            self._flush_filmstrip_preview
        )
        self._pending_filmstrip_anchor: tuple[Path, int] | None = None
        self._filmstrip_anchor_timer = QTimer(self)
        self._filmstrip_anchor_timer.setSingleShot(True)
        self._filmstrip_anchor_timer.setInterval(100)
        self._filmstrip_anchor_timer.timeout.connect(
            self._flush_filmstrip_anchor
        )
        self.playback_coordinator: PlaybackCoordinator | None = None

        self.setWindowTitle(APP_WINDOW_TITLE)
        self.resize(1400, 860)
        self.setMinimumSize(1100, 700)
        self._init_ui()
        self.selection_controller = ReviewSelectionController(
            self,
            high_speed_location=lambda lookup: source_location(
                lookup,
                high_speed=True,
            ),
            openable_statuses=frozenset(_OPENABLE_STATUSES),
        )
        # Keep the standard queue/evidence layout until the operator enters
        # continuous filmstrip review explicitly.
        self._set_continuous_review_layout(False)
        self._set_video_assist_controls_visible(self._video_assist_enabled())
        self.space_shortcut = QShortcut(QKeySequence(Qt.Key_Space), self)
        self.space_shortcut.setContext(Qt.WindowShortcut)
        self.space_shortcut.activated.connect(self._toggle_active_pane)
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
        self.fullscreen_shortcut = QShortcut(QKeySequence(Qt.Key_F11), self)
        self.fullscreen_shortcut.setContext(Qt.WindowShortcut)
        self.fullscreen_shortcut.activated.connect(self._toggle_fullscreen)
        self.refresh()

    def _init_ui(self) -> None:
        self.setStyleSheet(
            f'QDialog {{ background: #edf1f4; color: #17212b; '
            f'font-family: "{UI_FONT_FAMILY}"; '
            f'font-size: {UI_BASE_FONT_POINT_SIZE}pt; }}'
            "QFrame#reviewPanel, QFrame#passageEvidencePane { background: #ffffff; "
            "border: 1px solid #cfd7df; border-radius: 4px; }"
            "QFrame#passageEvidencePane[activeJudging=\"true\"] { "
            "border: 3px solid #1976c9; background: #f5faff; }"
            "QLabel#panelTitle, QLabel#evidencePaneTitle { font-size: 11pt; font-weight: 700; }"
            "QLabel#activeJudgingBadge { background: #1976c9; color: #ffffff; "
            "border-radius: 3px; padding: 3px 8px; font-size: 10pt; font-weight: 700; }"
            "QLabel#evidencePaneStatus { color: #667085; font-size: 9pt; }"
            "QLabel#evidencePaneTime { font-family: Consolas; font-size: 10pt; font-weight: 700; }"
            "QPushButton { min-height: 28px; padding: 0 9px; font-size: 10pt; "
            "border: 1px solid #aeb8c2; "
            "border-radius: 4px; background: #ffffff; }"
            "QPushButton:hover { background: #eef5fa; border-color: #5d91b5; }"
            "QPushButton:disabled { color: #9ba5ae; background: #f4f6f8; }"
            "QPushButton[queueFilter='true'] { min-height: 28px; padding: 0 8px; "
            "border-color: transparent; color: #526170; font-weight: 600; }"
            "QPushButton[queueFilterActive='true'], "
            "QPushButton[queueFilter='true']:checked { background: #185f73; "
            "border-color: #185f73; color: #ffffff; font-weight: 700; }"
            "QPushButton[queueFilterActive='true']:hover, "
            "QPushButton[queueFilter='true']:checked:hover { background: #124d5e; "
            "border-color: #124d5e; color: #ffffff; }"
            "QCheckBox, QComboBox, QLineEdit, QSpinBox { min-height: 28px; font-size: 10pt; }"
            "QComboBox, QLineEdit, QSpinBox { background: #ffffff; "
            "border: 1px solid #b8c5cf; border-radius: 4px; padding: 0 7px; }"
            "QTableWidget { background: #ffffff; gridline-color: #d8dee5; "
            "alternate-background-color: #f8fafb; font-size: 10pt; }"
            "QHeaderView::section { background: #eef2f5; color: #526170; "
            "font-size: 10pt; font-weight: 600; padding: 5px; "
            "border: none; border-right: 1px solid #d5dce3; border-bottom: 1px solid #c8d1da; }"
            "QTableWidget::item:selected { background: #dcecf8; }"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self.info_panel = QFrame(self)
        self.info_panel.setObjectName("reviewPanel")
        self.info_panel.setMinimumWidth(UI_INFO_PANEL_MIN_WIDTH)
        self.info_panel.setMaximumWidth(UI_INFO_PANEL_MAX_WIDTH)
        self.info_panel.hide()
        self.race_value = QLabel("--", self.info_panel)
        self.stage_value = QLabel("--", self.info_panel)
        self.group_value = QLabel("--", self.info_panel)
        self.selected_identity_value = QLabel("--", self.info_panel)
        self.athlete_value = QLabel("--", self.info_panel)
        self.team_value = QLabel("--", self.info_panel)
        self.selected_time_value = QLabel("--", self.info_panel)
        self.selected_time_value.setStyleSheet("font-family: Consolas; font-weight: 700;")
        self.source_value = QLabel("--", self.info_panel)
        self.source_value.setWordWrap(True)

        results_panel = QFrame(self)
        results_panel.setObjectName("reviewPanel")
        self.results_panel = results_panel
        results_layout = QVBoxLayout(results_panel)
        results_layout.setContentsMargins(8, 6, 8, 8)
        results_layout.setSpacing(6)
        filters = QHBoxLayout()
        filters.setSpacing(7)
        results_title = QLabel("通过记录")
        results_title.setObjectName("panelTitle")
        filters.addWidget(results_title)

        self.review_filter_buttons: dict[str, QPushButton] = {}
        self.review_filter_labels = {
            "pending": "异常复核",
            "blocked": "待人工确认",
            "confirmed": "已确认",
            "all": "全部",
        }
        review_filter_tooltips = {
            "pending": "有可用视频或时间位置，打开证据进行异常复核",
            "blocked": "暂时没有可直接核实的证据，需要人工确认或补充",
            "confirmed": "已经保存复核确认结果的记录",
            "all": "显示全部通过记录",
        }
        for filter_key in ("pending", "blocked", "confirmed", "all"):
            button = QPushButton(self.review_filter_labels[filter_key], self)
            button.setCheckable(True)
            button.setProperty("queueFilter", True)
            button.setToolTip(review_filter_tooltips[filter_key])
            button.clicked.connect(
                lambda _checked=False, key=filter_key: self._set_review_filter(key)
            )
            self.review_filter_buttons[filter_key] = button
            filters.addWidget(button)
        self._sync_review_filter_buttons()

        filters.addStretch(1)
        self.group_combo = QComboBox(self)
        self.group_combo.setMinimumWidth(180)
        self.group_combo.setMaximumWidth(220)
        self.group_combo.currentIndexChanged.connect(self._on_group_changed)
        filters.addWidget(self.group_combo)
        self.identity_search = QLineEdit(self)
        self.identity_search.setPlaceholderText("运动员编号 / 姓名")
        self.identity_search.setClearButtonEnabled(True)
        self.identity_search.setMinimumWidth(180)
        self.identity_search.setMaximumWidth(320)
        self.identity_search.textChanged.connect(self._on_search_changed)
        self.identity_search.returnPressed.connect(self._find_identity)
        filters.addWidget(self.identity_search)

        self.summary_label = QLabel()
        self.summary_label.setStyleSheet("color: #667085; font-size: 9pt;")
        self.summary_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        filters.addWidget(self.summary_label)
        self.video_arrival_button = QPushButton(
            f"到达候选：{len(self._video_arrival_batches)}批/"
            f"{len(self._video_navigation_candidates)}点"
        )
        self.video_arrival_button.setToolTip(
            "打开由视频自动扫描生成的到达批次；候选独立于芯片记录"
        )
        self.video_arrival_button.setVisible(bool(self._video_arrival_batches))
        self.video_arrival_button.clicked.connect(self._open_video_arrival_list)
        filters.addWidget(self.video_arrival_button)
        self.video_review_apply_requested.connect(self.set_video_reconciliation)
        self.video_review_button = QPushButton("视频异常：0")
        self.video_review_button.setToolTip("打开视频辅助异常列表")
        self.video_review_button.setVisible(False)
        self.video_review_button.clicked.connect(self._open_video_anomaly_list)
        filters.addWidget(self.video_review_button)
        self.video_review_done_button = QPushButton("已核实")
        self.video_review_done_button.setToolTip("将当前视频异常标记为已核实")
        self.video_review_done_button.setVisible(False)
        self.video_review_done_button.clicked.connect(
            lambda: self._mark_current_video_anomaly("verified")
        )
        filters.addWidget(self.video_review_done_button)
        self.video_review_ignore_button = QPushButton("忽略")
        self.video_review_ignore_button.setToolTip("将当前视频异常标记为背景或无效")
        self.video_review_ignore_button.setVisible(False)
        self.video_review_ignore_button.clicked.connect(
            lambda: self._mark_current_video_anomaly("ignored")
        )
        filters.addWidget(self.video_review_ignore_button)
        self.video_review_bib_edit = QLineEdit(self)
        self.video_review_bib_edit.setPlaceholderText("异常批次号码")
        self.video_review_bib_edit.setClearButtonEnabled(True)
        self.video_review_bib_edit.setMinimumWidth(110)
        self.video_review_bib_edit.setMaximumWidth(150)
        self.video_review_bib_edit.setVisible(False)
        self.video_review_bib_edit.returnPressed.connect(self._apply_video_review_bib)
        filters.addWidget(self.video_review_bib_edit)

        self.video_discovered_btn = QPushButton("添加视频发现号码", self)
        self.video_discovered_btn.setToolTip(
            "在当前集团视频帧记录芯片未读到的号码，仅标记为待补录，不写入正式成绩"
        )
        self.video_discovered_btn.setEnabled(False)
        self.video_discovered_btn.clicked.connect(self._add_video_discovered_bib)
        filters.addWidget(self.video_discovered_btn)

        self.queue_expand_btn = QPushButton("↕", self)
        self.queue_expand_btn.setFixedWidth(34)
        self.queue_expand_btn.setToolTip("展开运动员列表")
        self.queue_expand_btn.clicked.connect(self._toggle_queue_expanded)
        filters.addWidget(self.queue_expand_btn)

        self.offset_spin = QSpinBox(self)
        self.offset_spin.setRange(-600_000, 600_000)
        self.offset_spin.setSingleStep(100)
        self.offset_spin.setPrefix("校时 ")
        self.offset_spin.setSuffix(" ms")
        self.offset_spin.setMinimumWidth(125)
        self.offset_spin.setMaximumWidth(145)
        self.offset_spin.setValue(self.clock_offset_ms)
        self.offset_spin.setToolTip("复核系统时间 = CycleRace passage 时间 + 此偏移")
        self.offset_spin.valueChanged.connect(self._on_offset_changed)
        filters.addWidget(self.offset_spin)
        results_layout.addLayout(filters)

        self.table = _AutoFitTableWidget(0, 9, self)
        table_palette = self.table.palette()
        table_palette.setColor(QPalette.HighlightedText, QColor("#17212b"))
        self.table.setPalette(table_palette)
        self.table.setHorizontalHeaderLabels(
            [
                "序号",
                "号码",
                "姓名",
                "组别",
                "",
                "通过时间",
                "普通录像",
                "高速摄像",
                "复核状态",
            ]
        )
        # The operator opens the selected row directly in the single-camera
        # judgment pane, so per-source availability columns only add clutter.
        # Keep their underlying data for navigation/status decisions.
        for column in (3, 4, 6, 7):
            self.table.setColumnHidden(column, True)
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

        self.video_discovered_table = QTableWidget(0, 5, self)
        self.video_discovered_table.setObjectName("videoDiscoveredTable")
        self.video_discovered_table.setHorizontalHeaderLabels(
            ["视频发现", "批次", "视频位置", "帧号", "状态"]
        )
        self.video_discovered_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.video_discovered_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.video_discovered_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.video_discovered_table.verticalHeader().setVisible(False)
        self.video_discovered_table.setMaximumHeight(130)
        self.video_discovered_table.setVisible(False)
        self.video_discovered_table.itemSelectionChanged.connect(
            self._on_video_discovered_selected
        )
        results_layout.addWidget(self.video_discovered_table)

        transport = QFrame(self)
        transport.setObjectName("reviewPanel")
        self.transport = transport
        self.transport_layout = QHBoxLayout(transport)
        self.transport_layout.setContentsMargins(10, 4, 10, 4)
        self.transport_layout.setSpacing(7)
        self.current_passage_label = QLabel("未选择通过记录")
        self.current_passage_label.setStyleSheet(
            "font-size: 12pt; font-weight: 700; color: #17212b;"
        )
        self.current_context_label = QLabel()
        self.current_context_label.setStyleSheet(
            "color: #667085; font-size: 9pt; font-weight: 600;"
        )
        self.batch_context_label = QLabel()
        self.batch_context_label.setStyleSheet(
            "color: #185f73; font-size: 9pt; font-weight: 700;"
        )
        self.batch_review_btn = QPushButton("连续判读", self)
        self.batch_review_btn.setToolTip(
            "按芯片时间从前向后连续播放，看到谁就点击号码并确认当前帧"
        )
        self.batch_review_btn.setVisible(True)
        self.batch_review_btn.clicked.connect(self._toggle_batch_mode)
        self.current_time_label = QLabel("--:--:--.---")
        self.current_time_label.setStyleSheet(
            "font-family: Consolas; font-size: 10pt; font-weight: 700;"
        )
        self.target_position_btn = QPushButton("目标", self)
        self.target_position_btn.setToolTip("跳转到当前运动员的预计过线时间")
        self.preview_mark_btn = QPushButton("预览标线", self)
        self.preview_mark_btn.setCheckable(True)
        self.preview_mark_btn.setToolTip("直接在顶部预览画面上点击运动员位置")
        self.preview_confirm_btn = QPushButton("预览确认", self)
        self.preview_confirm_btn.setToolTip("确认顶部预览画面上的当前标线")
        self.preview_confirm_btn.setEnabled(False)
        self.previous_passage_btn = QPushButton("▲")
        self.previous_passage_btn.setToolTip("上一条")
        self.previous_frame_btn = QPushButton("|◀")
        self.previous_frame_btn.setToolTip("上一帧")
        self.play_both_btn = QPushButton("联动 ▶")
        self.play_both_btn.setToolTip("同时播放或暂停全部画面")
        self.next_frame_btn = QPushButton("▶|")
        self.next_frame_btn.setToolTip("下一帧")
        self.next_passage_btn = QPushButton("▼")
        self.next_passage_btn.setToolTip("下一条")
        self.fullscreen_btn = QPushButton("全屏")
        self.fullscreen_btn.setToolTip("全屏显示整个复核窗口（F11）")
        self.fullscreen_btn.setFixedWidth(48)
        for button in (
            self.previous_passage_btn,
            self.previous_frame_btn,
            self.play_both_btn,
            self.next_frame_btn,
            self.next_passage_btn,
        ):
            button.setFixedWidth(36)
        self.play_both_btn.setFixedWidth(68)
        self.auto_advance_checkbox = QCheckBox("确认后下一条")
        self.auto_advance_checkbox.setChecked(False)
        self.auto_advance_checkbox.setToolTip(
            "连续判读时确认后选择下一条；密集通过保持当前帧，长空档才向前跳转"
        )
        self.previous_passage_btn.clicked.connect(lambda: self._move_selection(-1))
        self.previous_frame_btn.setToolTip("当前画面上一帧")
        self.next_frame_btn.setToolTip("当前画面下一帧")
        self.previous_frame_btn.clicked.connect(lambda: self._step_active_pane(-1))
        self.play_both_btn.clicked.connect(self._toggle_both)
        self.next_frame_btn.clicked.connect(lambda: self._step_active_pane(1))
        self.next_passage_btn.clicked.connect(lambda: self._move_selection(1))
        self.target_position_btn.clicked.connect(self._seek_to_target_position)
        self.fullscreen_btn.clicked.connect(self._toggle_fullscreen)
        self.transport_layout.addWidget(self.current_passage_label)
        self.transport_layout.addWidget(self.current_context_label)
        self.transport_layout.addWidget(self.batch_context_label)
        self.transport_layout.addWidget(self.batch_review_btn)
        self.transport_layout.addStretch(1)
        self.transport_layout.addWidget(self.current_time_label)
        self.transport_layout.addWidget(self.target_position_btn)
        self.transport_layout.addWidget(self.preview_mark_btn)
        self.transport_layout.addWidget(self.preview_confirm_btn)
        self.transport_layout.addWidget(self.previous_frame_btn)
        self.transport_layout.addWidget(self.play_both_btn)
        self.transport_layout.addWidget(self.next_frame_btn)
        self.transport_layout.addWidget(self.auto_advance_checkbox)
        self.transport_layout.addWidget(self.previous_passage_btn)
        self.transport_layout.addWidget(self.next_passage_btn)
        self.transport_layout.addWidget(self.fullscreen_btn)

        self.evidence_splitter = QSplitter(Qt.Horizontal)
        self.evidence_splitter.setChildrenCollapsible(False)
        self.evidence_splitter.setHandleWidth(5)
        self.evidence_splitter.setMinimumWidth(0)
        self.evidence_splitter.setSizePolicy(
            QSizePolicy.Ignored,
            QSizePolicy.Expanding,
        )
        for camera_index in self._configured_regular_camera_indexes:
            self._create_regular_pane(camera_index)
        self.regular_pane = self.regular_panes[0]
        self.high_speed_pane = PassageEvidencePane(
            "高速摄像", HIGH_SPEED_SOURCE, self
        )
        self._connect_evidence_pane(self.high_speed_pane)
        self._active_pane = self.regular_pane
        self.evidence_splitter.addWidget(self.high_speed_pane)
        self.configure_evidence_panes(
            self._configured_regular_camera_indexes,
            show_high_speed=self._show_high_speed_pane,
        )
        self._set_active_idle_prefetch(self._active_pane)

        preview_panel = QFrame(self)
        self.preview_panel = preview_panel
        # Reserve enough space for the transport row plus the complete
        # filmstrip. QSizePolicy.Ignored otherwise lets the outer splitter
        # compress this panel even when the inner filmstrip has a minimum.
        preview_panel.setMinimumSize(0, 390)
        preview_panel.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        preview_layout = QVBoxLayout(preview_panel)
        self.preview_layout = preview_layout
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_layout.setSpacing(6)
        preview_layout.addWidget(transport)
        self.video_filmstrip = VideoFilmstripWidget(self)
        self.video_filmstrip.setVisible(False)
        # This course is reviewed in the athletes' travel direction: scan the
        # filmstrip from right to left while keeping timestamps unchanged.
        self.video_filmstrip.direction_combo.setCurrentIndex(1)
        self.preview_video_view = EvidenceImageView(self)
        self.preview_video_view.setMinimumHeight(260)
        self.preview_video_view.setSizePolicy(
            QSizePolicy.Expanding,
            QSizePolicy.Expanding,
        )
        self.preview_video_view.clear_frame("选择一条通过记录后在这里预览判读")
        self.preview_video_view.marker_position_selected.connect(
            self._on_preview_marker_selected
        )
        self.preview_video_view.marker_confirm_requested.connect(
            self._confirm_preview_marker
        )
        self.preview_video_view.frame_step_requested.connect(
            self._step_active_pane
        )
        self.preview_video_view.passage_step_requested.connect(
            self._move_selection
        )
        self.preview_video_view.activated.connect(
            lambda: self._activate_pane(self._active_playback_pane(), align=False)
        )
        preview_layout.addWidget(self.preview_video_view, 4)
        self.preview_mark_btn.toggled.connect(self.video_filmstrip.set_marker_mode)
        self.preview_mark_btn.toggled.connect(self.preview_video_view.set_marker_mode)
        self.preview_confirm_btn.clicked.connect(self._confirm_filmstrip_marker)
        self.video_filmstrip.position_selected.connect(
            self._seek_filmstrip_position
        )
        self.video_filmstrip.scrub_position_changed.connect(
            self._preview_filmstrip_position
        )
        self.video_filmstrip.position_double_clicked.connect(
            self._open_filmstrip_position_for_judgment
        )
        self.video_filmstrip.marker_position_selected.connect(
            self._on_filmstrip_marker_selected
        )
        self.video_filmstrip.confirm_button.clicked.connect(
            self._confirm_filmstrip_marker
        )
        self.video_filmstrip.cancel_button.clicked.connect(
            self._clear_filmstrip_marker
        )
        self.video_filmstrip.reload_requested.connect(self._update_filmstrip)
        # The header, enlarged activity overview, thumbnails, timestamps and
        # horizontal scrollbar need the full height. Keep divider dragging
        # from clipping the timestamp row at the bottom of the filmstrip.
        self.video_filmstrip.setMinimumHeight(340)
        self.video_filmstrip.setMaximumHeight(340)
        self.activity_timeline = ActivityTimelineWidget(self)
        self.playback_coordinator = PlaybackCoordinator(
            self.activity_timeline,
            self,
            filmstrip_update_delay_ms=self.INACTIVE_CAMERA_START_DELAY_MS + 80,
        )
        self.playback_coordinator.filmstrip_update_requested.connect(
            self._update_filmstrip
        )
        self.activity_timeline.position_selected.connect(
            self._seek_filmstrip_position
        )
        self.video_filmstrip.visible_range_changed.connect(
            self.activity_timeline.set_visible_range
        )
        self.activity_timeline.hide()
        # Activity is part of the filmstrip, not a separate preview panel:
        # title -> activity overview -> chronological thumbnails.
        self.video_filmstrip.layout().insertWidget(1, self.activity_timeline)
        preview_layout.addWidget(self.video_filmstrip, 1)
        self.preview_timeline = TargetTimelineSlider(Qt.Horizontal, self)
        self.preview_timeline.setInvertedAppearance(True)
        self.preview_timeline.setInvertedControls(True)
        self.preview_timeline.setRange(0, 0)
        self.preview_timeline.setEnabled(False)
        self.preview_timeline.setFixedHeight(22)
        self.preview_timeline.hide()
        self.preview_timeline.setToolTip("拖动时间线快速定位，松开后同步到机位 1")
        self.preview_timeline.sliderPressed.connect(
            self._on_preview_timeline_pressed
        )
        self.preview_timeline.sliderMoved.connect(
            self._on_preview_timeline_moved
        )
        self.preview_timeline.sliderReleased.connect(
            self._on_preview_timeline_released
        )
        # The camera pane already exposes the authoritative frame timeline;
        # keep this preview slider for compatibility but do not render a
        # duplicate control beneath the filmstrip.
        self.preview_video_view.setVisible(self._top_preview_video_visible)
        self.preview_mark_btn.setVisible(self._top_preview_video_visible)
        self.preview_confirm_btn.setVisible(self._top_preview_video_visible)
        if not self._top_preview_video_visible:
            # A hidden expanding graphics view can still leave excess space in
            # nested Qt layouts. Collapse it explicitly so the filmstrip sits
            # directly below the transport bar.
            self.preview_video_view.setMinimumHeight(0)
            self.preview_video_view.setMaximumHeight(0)
            self.preview_video_view.setSizePolicy(
                QSizePolicy.Ignored,
                QSizePolicy.Ignored,
            )
            self.preview_layout.setStretch(1, 0)
        # In filmstrip-first mode the judgment controls belong to the filmstrip
        # header, while the duplicate preview controls stay hidden with it.

        # Bottom row: the athlete order list stays on the left, while all
        # camera panes remain grouped on the right.
        self.workspace_splitter = QSplitter(Qt.Horizontal, self)
        self.workspace_splitter.setChildrenCollapsible(False)
        self.workspace_splitter.setHandleWidth(5)
        # Keep the judgment row usable when the horizontal divider is dragged
        # downward. Without a vertical minimum, the camera image and its
        # frame controls can be compressed into a thin, broken-looking strip.
        self.workspace_splitter.setMinimumSize(0, 320)
        self.workspace_splitter.addWidget(results_panel)
        self.workspace_splitter.addWidget(self.evidence_splitter)
        results_panel.setMinimumWidth(0)
        results_panel.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        self.workspace_splitter.setStretchFactor(0, 1)
        self.workspace_splitter.setStretchFactor(1, 1)
        self.workspace_splitter.setSizes([30, 70])

        self.review_content_splitter = QSplitter(Qt.Vertical, self)
        self.review_content_splitter.setChildrenCollapsible(False)
        self.review_content_splitter.setHandleWidth(6)
        self.review_content_splitter.setMinimumSize(0, 0)
        self.review_content_splitter.addWidget(preview_panel)
        self.review_content_splitter.addWidget(self.workspace_splitter)
        self.review_content_splitter.setStretchFactor(0, 1)
        self.review_content_splitter.setStretchFactor(1, 1)
        # Full-width preview above the bottom athlete/camera row.
        self.review_content_splitter.setSizes([60, 40])
        layout.addWidget(self.review_content_splitter, 1)
        # Nested splitters are not laid out yet during construction.  Apply
        # the initial proportions after the dialog receives its real size;
        # otherwise Qt may clamp the evidence area to a few pixels because
        # the results table has a wide horizontal size hint.
        # The real dimensions are available after the dialog is shown.

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
        pane.step_requested.connect(
            lambda delta, current=pane: self._step_pane(current, delta)
        )
        pane.play_requested.connect(lambda current=pane: self._toggle_pane(current))
        pane.passage_delta_requested.connect(
            lambda delta, current=pane: self._seek_pane_delta(
                current, delta, preview=False
            )
        )
        pane.scrub_preview_requested.connect(
            lambda delta, current=pane: self._seek_pane_delta(
                current, delta, preview=True
            )
        )
        pane.scrub_started.connect(
            lambda current=pane: self._on_pane_scrub_started(current)
        )
        pane.position_changed.connect(
            lambda delta, current=pane: self._on_pane_position_changed(
                current, delta
            )
        )
        pane.preview_frame_ready.connect(
            lambda image, position_ms, frame_index, current=pane:
            self._on_preview_frame_ready(
                current,
                image,
                position_ms,
                frame_index,
            )
        )
        pane.initial_frame_ready.connect(
            lambda event_id, current=pane: self._on_pane_initial_frame_ready(
                current,
                event_id,
            )
        )
        pane.selection_step_requested.connect(self._move_selection)
        pane.maximize_requested.connect(self._toggle_maximized_pane)
        pane.video_view.activated.connect(
            lambda current=pane: self._activate_pane(current, align=True)
        )
        pane.video_view.batch_event_requested.connect(
            lambda event_id, current=pane: self._select_batch_event_at_current_frame(
                event_id,
                current,
            )
        )
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
        self._restore_maximized_pane()
        for camera_index in normalized_indexes:
            self._create_regular_pane(camera_index)
        self._configured_regular_camera_indexes = normalized_indexes
        self._show_high_speed_pane = resolved_high_speed
        self.regular_pane = self.regular_panes[0]
        if self._active_pane not in self.evidence_panes:
            self._active_pane = self.regular_pane
        self._set_active_idle_prefetch(self._active_pane)

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
        for pane in self.all_evidence_panes:
            pane.set_compact_controls(compact and pane in self.evidence_panes)
            pane.maximize_btn.setText("放大")
            pane.maximize_btn.setToolTip("放大该机位（双击画面或按 F）")
        for index in range(self.evidence_splitter.count()):
            self.evidence_splitter.setStretchFactor(index, 1)
        self._distribute_evidence_panes()
        QTimer.singleShot(0, self._distribute_evidence_panes)

    def set_video_reconciliation(self, items: Iterable[object]) -> None:
        """Publish advisory video anomalies to the current review workspace.

        The detector is intentionally decoupled from the official passage
        table. Callers should pass reconciliation objects whose ``needs_review``
        property identifies the small set requiring attention.
        """
        if not self._video_assist_enabled():
            self._video_reconciliation_by_id.clear()
            self._video_reconciliation = ()
            self._set_video_assist_controls_visible(False)
            return

        def candidate_of(value: object):
            return getattr(value, "candidate", None)

        values = tuple(items)

        def is_visual(value: object) -> bool:
            candidate = candidate_of(value)
            return str(getattr(candidate, "candidate_id", "")).startswith("visual:")

        def overlaps(left: object, right: object) -> bool:
            a = candidate_of(left)
            b = candidate_of(right)
            if a is None or b is None:
                return False
            if int(getattr(a, "camera_index", 0)) != int(getattr(b, "camera_index", 0)):
                return False
            return abs(
                int(getattr(a, "peak_at_ms", 0)) - int(getattr(b, "peak_at_ms", 0))
            ) <= 1_500

        for item in values:
            candidate = getattr(item, "candidate", None)
            candidate_id = str(getattr(candidate, "candidate_id", ""))
            if not candidate_id:
                candidate_id = f"video-anomaly-{id(item)}"
            matching_visual_ids = {
                existing_id
                for existing_id, existing in self._video_reconciliation_by_id.items()
                if is_visual(existing) and overlaps(existing, item)
            }
            if not bool(getattr(item, "needs_review", False)):
                for existing_id in matching_visual_ids:
                    self._video_reconciliation_by_id.pop(existing_id, None)
                continue
            # Prefer the ordinary-video candidate because it contains the
            # recorded file and playback position; the live candidate is only
            # a timestamp hint.
            if not is_visual(item):
                for existing_id in matching_visual_ids:
                    previous_status = self._video_review_statuses.pop(existing_id, "pending")
                    previous_bib = self._video_review_bibs.pop(existing_id, "")
                    self._video_reconciliation_by_id.pop(existing_id, None)
                    self._video_review_statuses.setdefault(candidate_id, previous_status)
                    if previous_bib:
                        self._video_review_bibs.setdefault(candidate_id, previous_bib)
            self._video_reconciliation_by_id[candidate_id] = item
            self._video_review_statuses.setdefault(candidate_id, "pending")
        self._video_reconciliation = tuple(
            item
            for candidate_id, item in self._video_reconciliation_by_id.items()
            if self._video_review_statuses.get(candidate_id, "pending") == "pending"
        )
        if hasattr(self, "video_assist_status_label"):
            self._update_runtime_status()
        pending_count = len(self._video_reconciliation)
        total_count = len(self._video_reconciliation_by_id)
        self.video_review_button.setText(
            f"视频异常：{pending_count}"
            if pending_count
            else f"视频异常记录：{total_count}"
        )
        self.video_review_button.setVisible(bool(total_count))
        if self._video_anomaly_dialog_refresh is not None:
            self._video_anomaly_dialog_refresh()
        has_active = bool(self._active_video_anomaly_id)
        self.video_review_done_button.setVisible(has_active)
        self.video_review_ignore_button.setVisible(has_active)
        self.video_review_bib_edit.setVisible(has_active)
        self.video_review_updated.emit(self._video_reconciliation)
        if self._video_reconciliation:
            self.summary_label.setToolTip(
                f"视频辅助发现 {len(self._video_reconciliation)} 个异常批次，需重点复核"
            )
        else:
            self.summary_label.setToolTip("视频辅助：当前没有待复核异常批次")

    def video_reconciliation(self) -> tuple[object, ...]:
        """Return advisory anomalies currently supplied by the scanner."""
        return self._video_reconciliation

    def _video_assist_enabled(self) -> bool:
        return bool(
            getattr(self, "_video_assist_enabled_value", self.VIDEO_ASSIST_ENABLED)
        )

    def _set_video_assist_controls_visible(self, visible: bool) -> None:
        visible = bool(visible)
        for name in (
            "video_arrival_button",
            "video_review_button",
            "video_review_done_button",
            "video_review_ignore_button",
            "video_review_bib_edit",
            "video_discovered_btn",
            "video_discovered_table",
        ):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.setVisible(visible and widget.isVisible() if visible else False)

    def set_video_navigation_candidates(self, candidates: Iterable[object]) -> None:
        """Store visual passage candidates for modifier-key navigation.

        Candidates are advisory only. They never alter ``PassageEventStore``;
        camera-motion detections are excluded because they are not likely
        athlete passages.
        """

        if not self._video_assist_enabled():
            self._video_navigation_candidates = ()
            self._video_arrival_batches = ()
            self._set_video_assist_controls_visible(False)
            return

        def candidate_key(value: object) -> tuple[int, int, str]:
            return (
                int(getattr(value, "camera_index", 0)),
                int(getattr(value, "peak_at_ms", 0)),
                str(getattr(value, "candidate_id", "")),
            )

        ordered: list[object] = []
        seen: set[tuple[int, int]] = set()
        for candidate in sorted(tuple(candidates), key=candidate_key):
            if bool(getattr(candidate, "is_camera_motion", False)):
                continue
            key = (
                int(getattr(candidate, "camera_index", 0)),
                int(getattr(candidate, "peak_at_ms", 0)),
            )
            if key in seen:
                continue
            seen.add(key)
            ordered.append(candidate)
            candidate_id = str(getattr(candidate, "candidate_id", "")).strip()
            if candidate_id:
                self._video_review_statuses.setdefault(candidate_id, "pending")
        self._video_navigation_candidates = tuple(ordered)
        self._video_arrival_batches = build_video_arrival_batches(
            self._video_navigation_candidates,
            batch_gap_ms=self._video_arrival_batch_gap_ms,
            subwave_gap_ms=self._video_arrival_subwave_gap_ms,
        )
        if hasattr(self, "video_arrival_button"):
            self.video_arrival_button.setText(
                f"到达候选：{len(self._video_arrival_batches)}批/"
                f"{len(self._video_navigation_candidates)}点"
            )
            self.video_arrival_button.setVisible(bool(self._video_arrival_batches))
        if self._video_arrival_dialog_refresh is not None:
            self._video_arrival_dialog_refresh()

    def video_navigation_candidates(self) -> tuple[object, ...]:
        """Return visual candidates used by Ctrl+Left/Right."""

        return self._video_navigation_candidates

    def video_arrival_batches(self) -> tuple[VideoArrivalBatch, ...]:
        """Return video-driven review batches independent from chip events."""

        return self._video_arrival_batches

    def video_review_status(self, candidate_id: str) -> str:
        return self._video_review_statuses.get(str(candidate_id), "pending")

    def restore_video_review_record(
        self,
        candidate_id: str,
        *,
        status: str = "pending",
        bib: str = "",
    ) -> None:
        candidate_id = str(candidate_id).strip()
        if not candidate_id or status not in {"pending", "verified", "ignored"}:
            return
        self._video_review_statuses[candidate_id] = status
        if bib:
            self._video_review_bibs[candidate_id] = str(bib).strip()
        if self._video_arrival_dialog_refresh is not None:
            self._video_arrival_dialog_refresh()

    def _mark_current_video_anomaly(self, status: str) -> None:
        candidate_id = self._active_video_anomaly_id
        if not candidate_id or status not in {"verified", "ignored"}:
            return
        self._video_review_statuses[candidate_id] = status
        self._active_video_anomaly_id = ""
        self.video_review_bib_edit.clear()
        self.video_review_status_changed.emit(
            candidate_id,
            status,
            self._video_review_bibs.get(candidate_id, ""),
        )
        self.set_video_reconciliation(())
        if self._video_arrival_dialog_refresh is not None:
            self._video_arrival_dialog_refresh()

    def _open_next_video_anomaly(self) -> None:
        if not self._video_reconciliation:
            return
        current = getattr(self, "_video_anomaly_cursor", -1)
        current = (current + 1) % len(self._video_reconciliation)
        self._video_anomaly_cursor = current
        self._activate_video_anomaly(self._video_reconciliation[current], current)

    def _activate_video_anomaly(self, item: object, index: int | None = None) -> None:
        candidate = getattr(item, "candidate", None)
        candidate_id = str(getattr(candidate, "candidate_id", ""))
        if not candidate_id:
            return
        if index is not None:
            self._video_anomaly_cursor = int(index)
        else:
            try:
                self._video_anomaly_cursor = self._video_reconciliation.index(item)
            except ValueError:
                self._video_anomaly_cursor = -1
        self._active_video_anomaly_id = candidate_id
        self.video_review_done_button.setVisible(True)
        self.video_review_ignore_button.setVisible(True)
        self.video_review_bib_edit.setText(
            self._video_review_bibs.get(candidate_id, "")
        )
        self.video_review_bib_edit.setVisible(True)
        candidate = getattr(item, "candidate", None)
        timestamp = getattr(candidate, "peak_at_ms", None)
        if timestamp is None:
            return
        if not self._visible_events:
            self.refresh()
        if not self._visible_events or int(getattr(item, "chip_count", 0)) == 0:
            self.video_candidate_requested.emit(item)
            self.summary_label.setToolTip(
                "视频异常已发现，已发出独立视频复核请求"
            )
            return
        camera_index = int(getattr(candidate, "camera_index", 1))
        offset = self._clock_offset_for_camera(camera_index)
        nearest = min(
            self._visible_events,
            key=lambda event: abs(
                event.timeline_timestamp_ms + offset - int(timestamp)
            ),
        )
        self._enter_batch_mode(nearest.event_id)
        self._select_event(nearest.event_id)
        position = self._video_anomaly_cursor + 1
        total = len(self._video_reconciliation)
        self.summary_label.setToolTip(
            f"视频异常 {position}/{total}："
            f"已定位到最接近的芯片记录（{getattr(item, 'anomaly', '待复核')}）"
        )
        self.video_candidate_requested.emit(item)

    @staticmethod
    def _video_review_status_label(status: str) -> str:
        return {
            "pending": "待核实",
            "verified": "已核实",
            "ignored": "已忽略",
        }.get(str(status), "待核实")

    def _video_arrival_candidates_for_batch(
        self,
        batch: VideoArrivalBatch,
    ) -> tuple[object, ...]:
        by_id = {
            str(getattr(candidate, "candidate_id", "")): candidate
            for candidate in self._video_navigation_candidates
        }
        return tuple(
            by_id[candidate_id]
            for candidate_id in batch.candidate_ids
            if candidate_id in by_id
        )

    def _video_arrival_batch_chip_count(self, batch: VideoArrivalBatch) -> int:
        offset = self._clock_offset_for_camera(batch.camera_index)
        return sum(
            1
            for event in self._events_for_current_metadata(
                self.passage_store.events()
            )
            if batch.started_at_ms
            <= int(event.timeline_timestamp_ms) + offset
            <= batch.ended_at_ms
        )

    def _video_arrival_batch_progress(
        self,
        batch: VideoArrivalBatch,
    ) -> tuple[int, int]:
        completed = sum(
            self._video_review_statuses.get(candidate_id, "pending")
            in {"verified", "ignored"}
            for candidate_id in batch.candidate_ids
        )
        return completed, batch.size

    def _activate_video_arrival_batch(self, batch: VideoArrivalBatch) -> None:
        candidates = self._video_arrival_candidates_for_batch(batch)
        if not candidates:
            return
        candidate = next(
            (
                value
                for value in candidates
                if self._video_review_statuses.get(
                    str(getattr(value, "candidate_id", "")),
                    "pending",
                )
                == "pending"
            ),
            candidates[0],
        )
        candidate_id = str(getattr(candidate, "candidate_id", ""))
        self._active_video_anomaly_id = candidate_id
        self.video_review_done_button.setVisible(True)
        self.video_review_ignore_button.setVisible(True)
        self.video_review_bib_edit.setText(
            self._video_review_bibs.get(candidate_id, "")
        )
        self.video_review_bib_edit.setVisible(True)
        chip_count = self._video_arrival_batch_chip_count(batch)
        item = SimpleNamespace(
            candidate=candidate,
            chip_count=chip_count,
            anomaly=(
                f"到达批次，{batch.size} 个候选点，"
                f"芯片匹配 {chip_count} 条"
            ),
            arrival_batch=batch,
        )
        if chip_count:
            events = self._events_for_current_metadata(self.passage_store.events())
            offset = self._clock_offset_for_camera(batch.camera_index)
            nearest = min(
                events,
                key=lambda event: abs(
                    int(event.timeline_timestamp_ms)
                    + offset
                    - int(getattr(candidate, "peak_at_ms", 0))
                ),
                default=None,
            )
            if nearest is not None:
                self._enter_batch_mode(nearest.event_id)
                self._select_event(nearest.event_id)
        self.video_candidate_requested.emit(item)

    def _open_video_arrival_list(self) -> None:
        if not self._video_arrival_batches:
            return
        dialog = self._video_arrival_dialog
        if dialog is not None:
            dialog.raise_()
            dialog.activateWindow()
            return

        dialog = QDialog(self)
        dialog.setObjectName("videoArrivalDialog")
        dialog.setWindowTitle("视频到达候选时间轴")
        dialog.resize(1040, 560)
        layout = QVBoxLayout(dialog)

        controls = QHBoxLayout()
        pending_only = QCheckBox("只看未完成", dialog)
        pending_only.setChecked(True)
        controls.addWidget(pending_only)
        controls.addWidget(QLabel("机位", dialog))
        camera_filter = QComboBox(dialog)
        camera_filter.setObjectName("videoArrivalCameraFilter")
        camera_filter.addItem("全部机位", 0)
        camera_indexes = sorted(
            {batch.camera_index for batch in self._video_arrival_batches}
        )
        for camera_index in camera_indexes:
            camera_filter.addItem(f"机位 {camera_index}", camera_index)
        fallback_camera = camera_indexes[0] if camera_indexes else 0
        active_camera = int(
            getattr(self._active_pane, "camera_index", fallback_camera)
        )
        selected_camera_index = camera_filter.findData(active_camera)
        if selected_camera_index >= 0:
            camera_filter.setCurrentIndex(selected_camera_index)
        controls.addWidget(camera_filter)
        controls.addStretch(1)
        controls.addWidget(QLabel("分批间隔", dialog))
        gap_spin = QSpinBox(dialog)
        gap_spin.setObjectName("videoArrivalBatchGapSpin")
        gap_spin.setRange(2, 30)
        gap_spin.setSuffix(" 秒")
        gap_spin.setValue(max(2, self._video_arrival_batch_gap_ms // 1_000))
        gap_spin.setToolTip("相邻候选超过该时间后建立新的到达批次")
        controls.addWidget(gap_spin)
        layout.addLayout(controls)

        summary = QLabel(
            "双击批次直接打开第一个未核候选；候选来自视频，不依赖芯片记录。",
            dialog,
        )
        summary.setStyleSheet("color: #667085;")
        layout.addWidget(summary)

        table = QTableWidget(0, 8, dialog)
        table.setObjectName("videoArrivalTable")
        table.setHorizontalHeaderLabels(
            [
                "序号",
                "时间范围",
                "机位",
                "候选点",
                "芯片匹配",
                "类型",
                "子波",
                "审核进度",
            ]
        )
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.SingleSelection)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        table.setColumnWidth(0, 55)
        table.setColumnWidth(1, 280)
        table.setColumnWidth(2, 60)
        table.setColumnWidth(3, 75)
        table.setColumnWidth(4, 85)
        table.setColumnWidth(5, 110)
        table.setColumnWidth(6, 60)
        records: list[VideoArrivalBatch] = []

        def refresh_table() -> None:
            nonlocal records
            selected_id = ""
            current_row = table.currentRow()
            if 0 <= current_row < len(records):
                selected_id = records[current_row].batch_id
            records = [
                batch
                for batch in self._video_arrival_batches
                if (
                    int(camera_filter.currentData() or 0) == 0
                    or batch.camera_index == int(camera_filter.currentData())
                )
                and (
                    not pending_only.isChecked()
                    or self._video_arrival_batch_progress(batch)[0] < batch.size
                )
            ]
            table.setRowCount(len(records))
            selected_row = -1
            for row, batch in enumerate(records):
                if batch.batch_id == selected_id:
                    selected_row = row
                completed, total = self._video_arrival_batch_progress(batch)
                start_text = format_passage_time(batch.started_at_ms)
                end_text = format_passage_time(batch.ended_at_ms)
                time_text = (
                    start_text
                    if start_text == end_text
                    else f"{start_text} ～ {end_text}"
                )
                batch_type = (
                    "大集团候选"
                    if batch.is_large
                    else "多人候选"
                    if batch.size > 1 or batch.contains_group_detection
                    else "单人候选"
                )
                values = (
                    str(row + 1),
                    time_text,
                    str(batch.camera_index),
                    str(batch.size),
                    str(self._video_arrival_batch_chip_count(batch)),
                    batch_type,
                    str(len(batch.subwave_breaks)),
                    f"{completed}/{total}",
                )
                for column, value in enumerate(values):
                    table.setItem(row, column, QTableWidgetItem(value))
                table.item(row, 0).setData(Qt.UserRole, batch.batch_id)
            if selected_row >= 0:
                table.selectRow(selected_row)
            summary.setText(
                f"当前 {len(self._video_arrival_batches)} 个批次、"
                f"{len(self._video_navigation_candidates)} 个候选点；"
                "双击批次直接打开第一个未核候选。"
            )

        refresh_table()
        self._video_arrival_dialog_refresh = refresh_table
        layout.addWidget(table, 1)

        buttons = QHBoxLayout()
        open_button = QPushButton("打开选中批次", dialog)
        open_button.setObjectName("videoArrivalOpenButton")
        close_button = QPushButton("关闭", dialog)
        buttons.addStretch(1)
        buttons.addWidget(open_button)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

        def open_selected() -> None:
            row = table.currentRow()
            if not (0 <= row < len(records)):
                return
            self._activate_video_arrival_batch(records[row])
            dialog.close()

        def change_gap(value: int) -> None:
            self._video_arrival_batch_gap_ms = max(2_000, int(value) * 1_000)
            self.set_video_navigation_candidates(
                self._video_navigation_candidates
            )

        pending_only.toggled.connect(lambda _checked: refresh_table())
        camera_filter.currentIndexChanged.connect(lambda _index: refresh_table())
        gap_spin.valueChanged.connect(change_gap)
        open_button.clicked.connect(open_selected)
        table.cellDoubleClicked.connect(lambda _row, _column: open_selected())
        close_button.clicked.connect(dialog.close)

        def clear_dialog_reference(_result: int) -> None:
            self._video_arrival_dialog = None
            self._video_arrival_dialog_refresh = None

        dialog.finished.connect(clear_dialog_reference)
        self._video_arrival_dialog = dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _open_video_anomaly_list(self) -> None:
        if not self._video_reconciliation_by_id:
            return
        dialog = self._video_anomaly_dialog
        if dialog is not None:
            dialog.raise_()
            dialog.activateWindow()
            return
        dialog = QDialog(self)
        dialog.setObjectName("videoAnomalyDialog")
        dialog.setWindowTitle("视频辅助异常列表")
        dialog.resize(980, 480)
        layout = QVBoxLayout(dialog)
        summary = QLabel(
            "双击一行或点击“打开选中”进入复核；已核实和已忽略记录会保留。",
            dialog,
        )
        summary.setStyleSheet("color: #667085;")
        layout.addWidget(summary)
        table = QTableWidget(0, 7, dialog)
        table.setObjectName("videoAnomalyTable")
        table.setHorizontalHeaderLabels(
            ["序号", "时间", "机位", "异常原因", "芯片数", "号码", "状态"]
        )
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.SingleSelection)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        table.setColumnWidth(0, 55)
        table.setColumnWidth(1, 170)
        table.setColumnWidth(2, 60)
        table.setColumnWidth(4, 65)
        table.setColumnWidth(5, 90)
        records: list[tuple[str, object]] = []

        def refresh_table() -> None:
            nonlocal records
            selected_id = ""
            current_row = table.currentRow()
            if 0 <= current_row < len(records):
                selected_item = table.item(current_row, 0)
                if selected_item is not None:
                    selected_id = str(selected_item.data(Qt.UserRole) or "")
            records = sorted(
                self._video_reconciliation_by_id.items(),
                key=lambda pair: int(
                    getattr(getattr(pair[1], "candidate", None), "peak_at_ms", 0)
                ),
            )
            table.setRowCount(len(records))
            selected_row = -1
            for row, (candidate_id, item) in enumerate(records):
                if candidate_id == selected_id:
                    selected_row = row
                candidate = getattr(item, "candidate", None)
                values = (
                    str(row + 1),
                    format_passage_time(int(getattr(candidate, "peak_at_ms", 0))),
                    str(getattr(candidate, "camera_index", "-")),
                    str(getattr(item, "anomaly", "待核实")),
                    str(getattr(item, "chip_count", 0)),
                    self._video_review_bibs.get(candidate_id, ""),
                    self._video_review_status_label(
                        self._video_review_statuses.get(candidate_id, "pending")
                    ),
                )
                for column, value in enumerate(values):
                    table.setItem(row, column, QTableWidgetItem(value))
                table.item(row, 0).setData(Qt.UserRole, candidate_id)
            if selected_row >= 0:
                table.selectRow(selected_row)

        refresh_table()
        self._video_anomaly_dialog_refresh = refresh_table
        layout.addWidget(table, 1)
        buttons = QHBoxLayout()
        open_button = QPushButton("打开选中", dialog)
        open_button.setObjectName("videoAnomalyOpenButton")
        close_button = QPushButton("关闭", dialog)
        buttons.addStretch(1)
        buttons.addWidget(open_button)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

        def open_selected() -> None:
            row = table.currentRow()
            if not (0 <= row < len(records)):
                return
            self._activate_video_anomaly(records[row][1])
            dialog.close()

        open_button.clicked.connect(open_selected)
        table.cellDoubleClicked.connect(lambda _row, _column: open_selected())
        close_button.clicked.connect(dialog.close)
        def clear_dialog_reference(_result: int) -> None:
            self._video_anomaly_dialog = None
            self._video_anomaly_dialog_refresh = None

        dialog.finished.connect(clear_dialog_reference)
        self._video_anomaly_dialog = dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _apply_video_review_bib(self) -> None:
        candidate_id = self._active_video_anomaly_id
        bib = self.video_review_bib_edit.text().strip()
        if not candidate_id or not bib:
            return
        metadata = self._current_metadata()
        if metadata is None:
            self.summary_label.setToolTip("当前没有运动员名单，无法按号码定位")
            return
        if not self.focus_athlete(metadata.race_id, metadata.stage_id, bib=bib):
            self.summary_label.setToolTip(f"名单中没有找到号码 {bib}")
            return
        self._video_review_bibs[candidate_id] = bib
        self.video_review_status_changed.emit(
            candidate_id,
            self._video_review_statuses.get(candidate_id, "pending"),
            bib,
        )
        if self._video_arrival_dialog_refresh is not None:
            self._video_arrival_dialog_refresh()
        self.summary_label.setToolTip(f"已按号码 {bib} 定位名单运动员")

    def _on_offset_changed(self, value: int) -> None:
        self.clock_offset_ms = int(value)
        if self._clock_offset_by_camera:
            for camera_index in self._configured_regular_camera_indexes:
                self._clock_offset_by_camera[camera_index] = self.clock_offset_ms
        self.clock_offset_changed.emit(self.clock_offset_ms)
        self._lookup_cache.clear()
        self.refresh()

    def set_camera_clock_offset(self, camera_index: int, offset_ms: int) -> None:
        """Set a camera-specific review offset without changing race timing."""

        camera_index = max(1, int(camera_index))
        self._clock_offset_by_camera[camera_index] = int(offset_ms)
        self._lookup_cache.clear()
        self.refresh()

    def _clock_offset_for_camera(self, camera_index: int) -> int:
        return int(
            self._clock_offset_by_camera.get(
                max(1, int(camera_index)),
                self.clock_offset_ms,
            )
        )

    def _on_group_changed(self) -> None:
        if self._batch_mode:
            self._exit_batch_mode()
        self._refresh_filtered_view()

    def _on_search_changed(self) -> None:
        if self._batch_mode:
            self._exit_batch_mode()
        self._search_refresh_timer.start()

    def _set_review_filter(self, filter_key: str) -> None:
        if filter_key not in self.review_filter_buttons:
            return
        if filter_key == self._active_review_filter:
            return
        if self._batch_mode:
            self._exit_batch_mode()
        self._active_review_filter = filter_key
        self._sync_review_filter_buttons()
        self._refresh_filtered_view()

    def _sync_review_filter_buttons(self) -> None:
        for filter_key, button in self.review_filter_buttons.items():
            active = filter_key == self._active_review_filter
            button.setChecked(active)
            button.setProperty("queueFilterActive", active)
            button.style().unpolish(button)
            button.style().polish(button)

    def _toggle_queue_expanded(self) -> None:
        sizes = self.workspace_splitter.sizes()
        total = sum(sizes)
        if total <= 0:
            return
        if self._queue_expanded:
            restore = self._queue_default_sizes or [310, max(1, total - 310)]
            self.workspace_splitter.setSizes(restore)
            self._queue_expanded = False
            self.queue_expand_btn.setToolTip("展开运动员列表")
            return
        self._queue_default_sizes = sizes
        queue_height = max(360, int(total * 0.58))
        self.workspace_splitter.setSizes([queue_height, max(1, total - queue_height)])
        self._queue_expanded = True
        self.queue_expand_btn.setToolTip("恢复运动员列表高度")

    def _matches_search(self, event: PassageEvent, query: str) -> bool:
        if not query:
            return True
        metadata_athlete = self._metadata_athlete_for_event(event)
        athlete_name = event.athlete_name.strip() or (
            metadata_athlete.name.strip() if metadata_athlete is not None else ""
        )
        return query in event.bib.strip().casefold() or query in athlete_name.casefold()

    def _review_status_for_event(
        self,
        event_id: str,
        lookup: PassageVideoLookup,
    ) -> str:
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
            event_id,
            REGULAR_SOURCE,
            regular,
        )
        high_speed_association = self._source_association(
            event_id,
            HIGH_SPEED_SOURCE,
            high_speed,
        )
        fallback = review_status_text(lookup, regular, high_speed)
        return self._confirmation_status(
            regular_association,
            high_speed_association,
            fallback,
        )

    def _matches_review_filter(self, status: str) -> bool:
        if self._active_review_filter == "all":
            return True
        if self._active_review_filter == "confirmed":
            return status == "已确认"
        if self._active_review_filter == "blocked":
            return status == "受阻"
        return status == "待核对"

    def _bound_regular_locations(
        self,
        event: PassageEvent,
    ) -> tuple[PassageVideoLocation, ...]:
        store = self.review_binding_store
        if store is None:
            return ()
        locations = []
        for binding in store.active_bindings(event.event_id, event.revision):
            clip = store.get_clip(binding.clip_id)
            if clip is None:
                continue
            segment = self.timeline_store.get_segment(clip.timeline_segment_id)
            if segment is None:
                continue
            video_path = self.timeline_store.resolve_video_path(segment)
            camera_offset = self._clock_offset_for_camera(
                segment.camera_index
            )
            passage_position_ms = max(
                0,
                binding.passage_offset_ms + camera_offset,
            )
            if segment.media_duration_ms is not None:
                passage_position_ms = min(
                    passage_position_ms,
                    segment.media_duration_ms,
                )
            status = (
                "located"
                if self.timeline_store.video_path_is_playable(video_path)
                else "missing_file"
            )
            locations.append(
                PassageVideoLocation(
                    segment=segment,
                    video_path=video_path,
                    passage_position_ms=passage_position_ms,
                    playback_position_ms=max(
                        0,
                        passage_position_ms - self.pre_roll_ms,
                    ),
                    clock_offset_ms=camera_offset,
                    timing_error_ms=segment.timing_error_ms,
                    status=status,
                    media_locator=clip.clip_id,
                )
            )
        return tuple(sorted(locations, key=lambda item: item.segment.camera_index))

    @staticmethod
    def _recording_session_key_from_path(source_id: str, video_path: Path) -> str:
        path = Path(video_path).absolute()
        stem = _ARCHIVE_SEGMENT_SUFFIX_RE.sub("", path.stem)
        return f"{source_id}|{path.parent}|{stem}"

    @classmethod
    def _recording_session_key(cls, location: PassageVideoLocation) -> str:
        return cls._recording_session_key_from_path(
            location.segment.source_id,
            location.video_path,
        )

    def _seed_continuous_clock_offsets(self) -> None:
        events_by_id = {
            event.event_id: event
            for event in self._events_for_current_metadata(
                self.passage_store.events()
            )
        }
        changed = False
        for calibration in self.calibration_store.calibrations():
            key = (calibration.camera_index, calibration.session_key)
            if key not in self._continuous_clock_offsets:
                self._continuous_clock_offsets[key] = int(calibration.offset_ms)
                changed = True
        associations = sorted(
            self.association_store.associations(),
            key=lambda item: (
                events_by_id.get(item.passage_event_id).timeline_timestamp_ms
                if item.passage_event_id in events_by_id
                else 2**63 - 1,
                item.confirmed_at_ms,
            ),
        )
        for association in associations:
            if association.confirmed_source != REGULAR_SOURCE:
                continue
            event = events_by_id.get(association.passage_event_id)
            segment = self.timeline_store.get_segment(association.segment_id)
            if (
                event is None
                or segment is None
                or segment.media_started_at_ms is None
            ):
                continue
            continuous_lookup = self.timeline_store.locate_passage(
                event.timeline_timestamp_ms,
                clock_offset_ms=self._clock_offset_for_camera(
                    segment.camera_index
                ),
                pre_roll_ms=self.CONTINUOUS_SKIP_LEAD_MS,
                race_id=event.race_id,
                prefer_continuous_media=True,
            )
            continuous_location = self._regular_location_for_camera(
                continuous_lookup,
                segment.camera_index,
            )
            session_segment = (
                continuous_location.segment
                if continuous_location is not None
                else segment
            )
            video_path = (
                continuous_location.video_path
                if continuous_location is not None
                else self.timeline_store.resolve_video_path(segment)
            )
            key = (
                session_segment.camera_index,
                self._recording_session_key_from_path(
                    session_segment.source_id,
                    video_path,
                ),
            )
            if key in self._continuous_clock_offsets:
                continue
            self._continuous_clock_offsets[key] = (
                int(segment.media_started_at_ms)
                + int(association.position_ms)
                - int(event.timeline_timestamp_ms)
            )
            changed = True
        if changed:
            self._continuous_calibration_revision += 1

    def _continuous_lookup_for_camera(
        self,
        event: PassageEvent,
        camera_index: int,
    ) -> PassageVideoLookup:
        base_offset = self._clock_offset_for_camera(camera_index)
        lookup = self.timeline_store.locate_passage(
            event.timeline_timestamp_ms,
            clock_offset_ms=base_offset,
            pre_roll_ms=self.CONTINUOUS_SKIP_LEAD_MS,
            race_id=event.race_id,
            prefer_continuous_media=True,
        )
        location = self._regular_location_for_camera(lookup, camera_index)
        if location is None:
            return lookup
        calibrated_offset = self._continuous_clock_offsets.get(
            (camera_index, self._recording_session_key(location))
        )
        if calibrated_offset is None or calibrated_offset == base_offset:
            return lookup
        return self.timeline_store.locate_passage(
            event.timeline_timestamp_ms,
            clock_offset_ms=calibrated_offset,
            pre_roll_ms=self.CONTINUOUS_SKIP_LEAD_MS,
            race_id=event.race_id,
            prefer_continuous_media=True,
        )

    def _lookup(self, event: PassageEvent) -> PassageVideoLookup:
        direct_locations = (
            () if self._batch_mode else self._bound_regular_locations(event)
        )
        configured = set(self._configured_regular_camera_indexes)
        direct_camera_indexes = {
            location.segment.camera_index
            for location in direct_locations
            if location.status in _OPENABLE_STATUSES
        }
        needs_fallback = not direct_locations or not configured.issubset(
            direct_camera_indexes
        )
        if needs_fallback:
            fallback_locations = []
            fallback_statuses = []
            for camera_index in self._configured_regular_camera_indexes:
                fallback = (
                    self._continuous_lookup_for_camera(event, camera_index)
                    if self._batch_mode
                    else self.timeline_store.locate_passage(
                        event.timeline_timestamp_ms,
                        clock_offset_ms=self._clock_offset_for_camera(camera_index),
                        pre_roll_ms=self.pre_roll_ms,
                        race_id=event.race_id,
                    )
                )
                fallback_statuses.append(fallback.status)
                fallback_locations.extend(
                    location
                    for location in fallback.locations
                    if location.segment.camera_index == camera_index
                )
            # Preserve recorded high-speed/external evidence that is not part
            # of the configured ordinary-camera set.
            external_camera_indexes = sorted(
                {
                    segment.camera_index
                    for segment in self.timeline_store.segments()
                    if segment.camera_index
                    not in self._configured_regular_camera_indexes
                }
            )
            for camera_index in external_camera_indexes:
                fallback = self.timeline_store.locate_passage(
                    event.timeline_timestamp_ms,
                    clock_offset_ms=self._clock_offset_for_camera(camera_index),
                    pre_roll_ms=self.pre_roll_ms,
                    race_id=event.race_id,
                )
                fallback_statuses.append(fallback.status)
                fallback_locations.extend(
                    location
                    for location in fallback.locations
                    if location.segment.camera_index == camera_index
                )
            locations_by_camera = {
                location.segment.camera_index: location
                for location in direct_locations
            }
            for location in fallback_locations:
                current = locations_by_camera.get(location.segment.camera_index)
                if current is None or (
                    current.status == "missing_file"
                    and location.status in _OPENABLE_STATUSES
                ):
                    locations_by_camera[location.segment.camera_index] = location
            locations = list(locations_by_camera.values())
            lookup_status = (
                "located"
                if fallback_locations
                else next(iter(fallback_statuses), "no_segments")
            )
        else:
            locations = list(direct_locations)
            lookup_status = "located"
        lookup = PassageVideoLookup(
            "located"
            if any(location.status == "located" for location in locations)
            else lookup_status,
            event.timeline_timestamp_ms + self.clock_offset_ms,
            tuple(sorted(locations, key=lambda item: item.segment.camera_index)),
        )
        locations = list(lookup.locations)
        if not self._include_recorded_evidence:
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
        return (self.timeline_store.revision,)

    @staticmethod
    def _cache_survives_timeline_update(lookup: PassageVideoLookup) -> bool:
        return bool(lookup.locations) and all(
            location.status != "preview" for location in lookup.locations
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
            self._batch_mode,
            self._continuous_calibration_revision,
            self.review_binding_store.revision
            if self.review_binding_store is not None
            else 0,
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
        if not _association_matches_location(association, location):
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
                    and _association_matches_location(association, location)
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
        if regular is not None or high_speed is not None:
            return "已确认"
        return fallback

    @staticmethod
    def _display_confirmation_status(status: str) -> str:
        return "已确认" if status == "已确认" else "未确认"

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

    def _resize_group_popup(self) -> None:
        metrics = QFontMetrics(self.group_combo.font())
        widest_text = max(
            (
                metrics.horizontalAdvance(self.group_combo.itemText(index))
                for index in range(self.group_combo.count())
            ),
            default=0,
        )
        popup_width = min(360, max(self.group_combo.minimumWidth(), widest_text + 44))
        self.group_combo.view().setMinimumWidth(popup_width)
        for index in range(self.group_combo.count()):
            self.group_combo.setItemData(
                index,
                self.group_combo.itemText(index),
                Qt.ToolTipRole,
            )

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
            self._resize_group_popup()
            return False
        self.group_combo.blockSignals(True)
        self.group_combo.clear()
        self.group_combo.addItem("全部组别", "")
        for group_id, group_label in expected_items:
            self.group_combo.addItem(group_label, group_id)
        group_index = self.group_combo.findData(previous_group)
        self.group_combo.setCurrentIndex(max(0, group_index))
        self.group_combo.blockSignals(False)
        self._resize_group_popup()
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
        display_review_status = self._display_confirmation_status(review_status)
        self._event_review_statuses[event.event_id] = review_status
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
            display_review_status,
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

    def _renumber_visible_rows(self) -> None:
        for row, event in enumerate(self._visible_events):
            item = self.table.item(row, 0)
            if item is None:
                item = QTableWidgetItem()
                item.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(row, 0, item)
            item.setText(str(row + 1))
            item.setData(Qt.UserRole, event.event_id)

    def _render_filtered_queue(
        self,
        events: tuple[PassageEvent, ...],
        previous_event_id: str,
    ) -> None:
        if self._batch_mode:
            self._visible_events = list(events)
        else:
            selected_group = str(self.group_combo.currentData() or "")
            query = self.identity_search.text().strip().casefold()
            self._visible_events = [
                event
                for event in events
                if (not selected_group or event.group_id == selected_group)
                and self._matches_search(event, query)
                and self._matches_review_filter(
                    self._event_review_statuses.get(event.event_id, "待核对")
                )
            ]

        self.table.blockSignals(True)
        self.table.setRowCount(len(self._visible_events))
        selected_row = -1
        for row, event in enumerate(self._visible_events):
            lookup = self._lookups[event.event_id]
            self._write_event_row(row, event, lookup)
            if event.event_id == previous_event_id:
                selected_row = row

        if selected_row < 0 and self._visible_events:
            selected_row = 0
        if selected_row >= 0:
            self.table.setCurrentCell(selected_row, 1)
            self.table.selectRow(selected_row)
        self.table.blockSignals(False)
        self.table.schedule_auto_fit()

        self._render_summary()
        if selected_row >= 0:
            self._select_event(self._visible_events[selected_row].event_id)
        else:
            self._clear_selection_details()

    def _active_review_batch(self) -> Optional[PassageReviewBatch]:
        if not self._active_review_batch_id:
            return None
        return next(
            (
                batch
                for batch in self._review_batches
                if batch.batch_id == self._active_review_batch_id
            ),
            None,
        )

    def _update_batch_controls(self, event_id: str = "") -> None:
        batch = self._review_batch_by_event_id.get(str(event_id))
        if not self._visible_events:
            self.batch_context_label.clear()
            self.batch_review_btn.setVisible(False)
            self.video_discovered_btn.setEnabled(False)
            self.video_discovered_table.setVisible(False)
            for pane in self.evidence_panes:
                pane.video_view.clear_batch_roster()
            return
        self.batch_review_btn.setVisible(True)
        if self._batch_mode:
            row = max(0, self.table.currentRow())
            context = f"判读 {row + 1:,}/{len(self._visible_events):,}"
            calibration = self._continuous_calibration_summary()
            self.batch_context_label.setText(
                " · ".join(value for value in (context, calibration) if value)
            )
        elif batch is not None and batch.size >= 2:
            self.batch_context_label.setText(f"附近 {batch.size} 人")
        else:
            self.batch_context_label.clear()
        self.batch_review_btn.setText(
            "退出判读" if self._batch_mode else "判读"
        )
        # The split review workflow is always active; no mode toggle is shown.
        self.batch_review_btn.setVisible(False)
        self.video_discovered_btn.setEnabled(self._batch_mode and batch is not None)
        self._update_batch_roster_overlays(batch)
        self._refresh_video_discovered_table()

    def _continuous_calibration_summary(self) -> str:
        offsets: dict[int, int] = {}
        for (camera_index, _session_key), offset_ms in sorted(
            self._continuous_clock_offsets.items()
        ):
            offsets[int(camera_index)] = int(offset_ms)
        if not offsets:
            return ""
        return "补偿 " + " / ".join(
            f"机位{camera_index} {offset_ms:+d} ms"
            for camera_index, offset_ms in sorted(offsets.items())
        )

    def _update_batch_roster_overlays(
        self,
        batch: Optional[PassageReviewBatch],
    ) -> None:
        # The athlete table is the single source of truth for bibs during
        # review. Keep camera panes unobstructed for direct frame inspection.
        for pane in self.evidence_panes:
            pane.video_view.clear_batch_roster()

    def _select_batch_event_at_current_frame(
        self,
        event_id: str,
        pane: PassageEvidencePane,
    ) -> None:
        event_id = str(event_id)
        if (
            not self._batch_mode
            or not any(
                event.event_id == event_id for event in self._visible_events
            )
            or pane not in self.evidence_panes
        ):
            return
        pane.set_playing(False)
        row = next(
            (
                index
                for index, event in enumerate(self._visible_events)
                if event.event_id == event_id
            ),
            -1,
        )
        if row < 0:
            return
        self.table.blockSignals(True)
        self.table.setCurrentCell(row, 1)
        self.table.selectRow(row)
        self.table.blockSignals(False)
        self._select_event(
            event_id,
            preserve_current_frame=pane,
        )
        self._activate_pane(pane, align=False)
        batch = self._active_review_batch()
        event = self.passage_store.get(event_id)
        if (
            batch is not None
            and event is not None
            and batch.event_ids
            and event_id == batch.event_ids[0]
        ):
            self._calibrate_continuous_session_at_position(
                event,
                pane,
                int(pane._current_position_ms),
                int(time.time() * 1000.0),
            )
        self._begin_marking(pane)

    def _refresh_video_discovered_table(self) -> None:
        table = getattr(self, "video_discovered_table", None)
        if table is None:
            return
        batch = self._active_review_batch() if self._batch_mode else None
        metadata = self._current_metadata()
        race_id = metadata.race_id if metadata is not None else ""
        stage_id = metadata.stage_id if metadata is not None else ""
        entries = [
            entry
            for entry in self._video_discovered_entries
            if batch is not None
            and (not race_id or entry.get("race_id") == race_id)
            and (not stage_id or entry.get("stage_id") == stage_id)
            and self._video_discovery_matches_batch(entry, batch)
        ]
        for entry in entries:
            if entry.get("batch_id") != batch.batch_id:
                entry["batch_id"] = batch.batch_id
                try:
                    self.video_discovery_store.update_batch(
                        str(entry.get("entry_id", "")), batch.batch_id
                    )
                except VideoDiscoveryError:
                    logger.exception("failed to migrate video discovery batch")
        table.blockSignals(True)
        table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            values = (
                str(entry.get("bib", "")),
                f"{batch.size}人" if batch is not None else "",
                f"{max(0, int(entry.get('position_ms', 0))) / 1000.0:.3f} s",
                str(entry.get("frame_index", "-")),
                self._video_discovery_status_label(str(entry.get("status", ""))),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column in {0, 2, 3}:
                    item.setTextAlignment(Qt.AlignCenter)
                if column == 4:
                    self._apply_status_style(item, "待核对")
                item.setData(Qt.UserRole, str(entry.get("entry_id", "")))
                table.setItem(row, column, item)
        table.blockSignals(False)
        table.setVisible(bool(entries) and self._batch_mode)
        table.resizeColumnsToContents()
        table.setColumnWidth(4, max(180, table.columnWidth(4)))

    @staticmethod
    def _video_discovery_status_label(status: str) -> str:
        return {
            "pending_manual_entry": "视频发现 · 芯片缺失 · 待补录",
            "resolved": "已补录芯片记录",
            "ignored": "已忽略",
        }.get(status, "视频发现 · 芯片缺失 · 待补录")

    def _video_discovery_matches_batch(
        self,
        entry: Mapping[str, object],
        batch: PassageReviewBatch,
    ) -> bool:
        if str(entry.get("batch_id", "")) == batch.batch_id:
            return True
        started_at_ms = int(entry.get("started_at_ms", 0))
        ended_at_ms = int(entry.get("ended_at_ms", started_at_ms))
        return (
            started_at_ms <= batch.ended_at_ms + self.REVIEW_BATCH_GAP_MS
            and ended_at_ms >= batch.started_at_ms - self.REVIEW_BATCH_GAP_MS
        )

    def _reconcile_video_discoveries(
        self,
        events: tuple[PassageEvent, ...],
    ) -> None:
        for entry in self._video_discovered_entries:
            if entry.get("status") != "pending_manual_entry":
                continue
            entry_race_id = str(entry.get("race_id", ""))
            entry_stage_id = str(entry.get("stage_id", ""))
            entry_bib = str(entry.get("bib", "")).strip().casefold()
            started_at_ms = int(entry.get("started_at_ms", 0))
            ended_at_ms = int(entry.get("ended_at_ms", started_at_ms))
            resolved = any(
                event.race_id == entry_race_id
                and event.stage_id == entry_stage_id
                and event.bib.strip().casefold() == entry_bib
                and started_at_ms - self.REVIEW_BATCH_GAP_MS
                <= event.timeline_timestamp_ms
                <= ended_at_ms + self.REVIEW_BATCH_GAP_MS
                for event in events
            )
            if not resolved:
                continue
            entry["status"] = "resolved"
            try:
                self.video_discovery_store.update_status(
                    str(entry.get("entry_id", "")), "resolved"
                )
            except VideoDiscoveryError:
                logger.exception("failed to update resolved video discovery")

    def _add_video_discovered_bib(self) -> None:
        batch = self._active_review_batch()
        if not self._batch_mode or batch is None:
            return
        bib, accepted = QInputDialog.getText(
            self,
            "添加视频发现号码",
            "输入视频中看到但芯片没有的号码：",
        )
        bib = str(bib).strip()
        if not accepted or not bib:
            return
        if any(
            entry.get("batch_id") == batch.batch_id
            and str(entry.get("bib", "")).casefold() == bib.casefold()
            for entry in self._video_discovered_entries
        ):
            self.summary_label.setToolTip(f"集团中已经记录视频发现号码 {bib}")
            return
        pane = self._active_pane if self._active_pane in self.evidence_panes else self.regular_pane
        position_ms = int(getattr(pane, "_current_position_ms", 0))
        frame_index = int(getattr(pane, "_current_frame_index", -1))
        camera_index = int(getattr(pane, "camera_index", 1))
        metadata = self._current_metadata()
        selected_event = self.passage_store.get(self._selected_event_id)
        race_id = (
            metadata.race_id
            if metadata is not None
            else (selected_event.race_id if selected_event is not None else "")
        )
        stage_id = (
            metadata.stage_id
            if metadata is not None
            else (selected_event.stage_id if selected_event is not None else "")
        )
        discovery_id = f"{batch.batch_id}:video:{time.time_ns()}"
        try:
            record = self.video_discovery_store.add(
                discovery_id=discovery_id,
                race_id=race_id,
                stage_id=stage_id,
                batch_id=batch.batch_id,
                bib=bib,
                camera_index=camera_index,
                frame_index=frame_index,
                position_ms=position_ms,
                started_at_ms=batch.started_at_ms,
                ended_at_ms=batch.ended_at_ms,
            )
        except (VideoDiscoveryError, OSError, ValueError) as error:
            self.summary_label.setToolTip(f"无法保存视频发现号码 {bib}: {error}")
            return
        entry = {
            "entry_id": record.discovery_id,
            "race_id": record.race_id,
            "stage_id": record.stage_id,
            "batch_id": record.batch_id,
            "bib": record.bib,
            "position_ms": record.position_ms,
            "frame_index": record.frame_index,
            "camera_index": record.camera_index,
            "started_at_ms": record.started_at_ms,
            "ended_at_ms": record.ended_at_ms,
            "status": record.status,
        }
        self._video_discovered_entries.append(entry)
        self._refresh_video_discovered_table()
        self._focus_video_discovered_entry(entry)
        self.summary_label.setToolTip(
            f"已记录视频发现号码 {bib}，请在 CycleRace 中补录后再确认正式成绩"
        )

    def _focus_video_discovered_entry(self, entry: Mapping[str, object]) -> None:
        bib = str(entry.get("bib", "")).strip()
        if not bib:
            return
        self._active_video_discovered_entry_id = str(entry.get("entry_id", ""))
        self.selected_identity_value.setText(bib)
        self.athlete_value.setText("视频发现号码")
        self.team_value.setText("--")
        self.selected_time_value.setText(
            f"视频位置 {max(0, int(entry.get('position_ms', 0))) / 1000.0:.3f} s"
        )
        self.source_value.setText("视频发现 · 芯片缺失 · 待补录")
        self.current_passage_label.setText(f"视频发现 {bib}（尚无芯片记录）")
        self.current_context_label.setText("集团复核 · 待补录")
        pane = self._pane_for_camera(int(entry.get("camera_index", 1)))
        position_ms = int(entry.get("position_ms", 0))
        worker = getattr(pane, "_worker", None)
        if worker is not None and worker.isRunning():
            worker.seek(max(0, position_ms))
            pane.timeline.setValue(min(max(0, position_ms), pane.timeline.maximum()))
            pane._current_position_ms = position_ms
        pane.mark_btn.setEnabled(False)
        pane.video_view.set_marker_mode(False)
        pane.video_view.set_identity_cue(bib, "视频发现 · 待补录")

    def _pane_for_camera(self, camera_index: int) -> PassageEvidencePane:
        camera_index = max(1, int(camera_index))
        return next(
            (
                pane
                for pane in self.evidence_panes
                if pane.camera_index == camera_index
            ),
            self._active_pane
            if self._active_pane in self.evidence_panes
            else self.regular_pane,
        )

    def _on_video_discovered_selected(self) -> None:
        row = self.video_discovered_table.currentRow()
        if row < 0:
            return
        item = self.video_discovered_table.item(row, 0)
        if item is None:
            return
        entry_id = str(item.data(Qt.UserRole) or "")
        entry = next(
            (
                candidate
                for candidate in self._video_discovered_entries
                if str(candidate.get("entry_id", "")) == entry_id
            ),
            None,
        )
        if entry is not None:
            self._focus_video_discovered_entry(entry)

    def _enter_batch_mode(self, event_id: str = "") -> bool:
        explicit_event_id = str(event_id)
        event_id = explicit_event_id or self._continuous_resume_event_id()
        batch = self._review_batch_by_event_id.get(event_id)
        if not event_id or self.passage_store.get(event_id) is None:
            return False
        self._batch_mode = True
        self._set_continuous_review_layout(True)
        self._active_review_batch_id = batch.batch_id if batch is not None else ""
        self._auto_advance_before_continuous = self.auto_advance_checkbox.isChecked()
        self.auto_advance_checkbox.setChecked(True)
        self._deferred_start_timer.stop()
        self._deferred_selection_panes.clear()
        self._selection_pending_panes.clear()
        self.group_combo.blockSignals(True)
        self.group_combo.setCurrentIndex(0)
        self.group_combo.blockSignals(False)
        self.identity_search.blockSignals(True)
        self.identity_search.clear()
        self.identity_search.blockSignals(False)
        self._seed_continuous_clock_offsets()
        self._lookup_cache.clear()
        self._selected_event_id = event_id
        self.refresh()
        self._update_filmstrip()
        return True

    def _continuous_resume_event_id(self) -> str:
        events = self._events_for_current_metadata(self.passage_store.events())
        if not events:
            return ""
        anchor_index = next(
            (
                index
                for index, event in enumerate(events)
                if event.event_id == self._selected_event_id
            ),
            0,
        )
        ordered_events = events[anchor_index:] + events[:anchor_index]
        return next(
            (
                event.event_id
                for event in ordered_events
                if self.association_store.get(event.event_id, REGULAR_SOURCE) is None
            ),
            events[anchor_index].event_id,
        )

    def _exit_batch_mode(self) -> None:
        if not self._batch_mode and not self._active_review_batch_id:
            return
        self._batch_mode = False
        self._set_continuous_review_layout(True)
        self._active_review_batch_id = ""
        self.auto_advance_checkbox.setChecked(
            self._auto_advance_before_continuous
        )
        self._lookup_cache.clear()
        self._update_batch_controls(self._selected_event_id)
        self._filmstrip_preview_timer.stop()
        self._pending_filmstrip_preview_position = None
        # Filmstrip review remains available after leaving continuous mode.
        self._update_filmstrip()

    def _toggle_batch_mode(self) -> None:
        if self._batch_mode:
            self._exit_batch_mode()
            self._refresh_filtered_view()
            return
        self._enter_batch_mode()

    def _set_continuous_review_layout(self, enabled: bool) -> None:
        """Keep the top preview and lower reference panes usable together."""

        if not hasattr(self, "workspace_splitter"):
            return
        if enabled:
            self._set_continuous_results_table(True)
            # The top preview is the judgment surface. Keep the lower camera
            # panes visible as a fast before/after reference while the operator
            # scans for the next rider.
            self.evidence_splitter.setVisible(True)
            for pane in getattr(self, "evidence_panes", ()):
                pane.setMinimumSize(0, 0)
                pane.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
                pane.video_view.setMinimumSize(0, 0)
                pane.video_view.setSizePolicy(
                    QSizePolicy.Ignored,
                    QSizePolicy.Expanding,
                )
                # Keep the lower reference panes self-describing even though
                # the top preview owns the actual judgment action.
                pane.video_view.set_identity_badge_visible(True)
                pane.video_view.set_marker_label_visible(True)
            if self.workspace_splitter.orientation() != Qt.Horizontal:
                self.workspace_splitter.setOrientation(Qt.Horizontal)
            self.workspace_splitter.setStretchFactor(0, 1)
            self.workspace_splitter.setStretchFactor(1, 1)
            # Keep the athlete list fixed without disabling the native Qt
            # splitter handle, which can crash Qt5 during video re-layout.
            if self.workspace_splitter.handleWidth() != 0:
                self.workspace_splitter.setHandleWidth(0)
            self._apply_review_split_sizes()
            return
        self.evidence_splitter.setVisible(True)
        if self.workspace_splitter.orientation() != Qt.Horizontal:
            self.workspace_splitter.setOrientation(Qt.Horizontal)
        if self.workspace_splitter.handleWidth() != 5:
            self.workspace_splitter.setHandleWidth(5)
        for pane in getattr(self, "evidence_panes", ()):
            pane.video_view.set_identity_badge_visible(True)
            pane.video_view.set_marker_label_visible(True)
        self._set_continuous_results_table(False)
        if hasattr(self, "results_panel"):
            self.results_panel.setMinimumWidth(0)
            self.results_panel.setMaximumWidth(16777215)
        self.workspace_splitter.setStretchFactor(0, 4)
        self.workspace_splitter.setStretchFactor(1, 6)
        total = max(2, self.workspace_splitter.height())
        queue_height = min(360, max(220, int(total * 0.38)))
        self.workspace_splitter.setSizes([queue_height, total - queue_height])

    def _apply_review_split_sizes(self) -> None:
        splitter = getattr(self, "workspace_splitter", None)
        if splitter is None or splitter.orientation() != Qt.Horizontal:
            return
        total = splitter.width()
        if total <= 0:
            return
        total = splitter.width()
        if total <= 0:
            return
        current = splitter.sizes()
        athlete_width = max(240, int(total * 0.30))
        target = [athlete_width, max(1, total - athlete_width)]
        if len(current) == 2 and abs(current[0] - target[0]) <= 2:
            return
        splitter.setSizes(target)

    def _apply_review_content_split_sizes(self) -> None:
        splitter = getattr(self, "review_content_splitter", None)
        if splitter is None or splitter.height() <= 0:
            return
        total = splitter.height()
        preview_height = (
            max(360, int(total * 0.60))
            if getattr(self, "_top_preview_video_visible", True)
            else min(380, max(320, int(total * 0.50)))
        )
        target = [preview_height, max(1, total - preview_height)]
        current = splitter.sizes()
        if len(current) == 2 and abs(current[0] - target[0]) <= 2:
            return
        splitter.setSizes(target)

    def _finish_review_content_split_resize(self) -> None:
        self._review_split_resize_pending = False
        self._apply_review_content_split_sizes()

    def _set_continuous_results_table(self, enabled: bool) -> None:
        table = getattr(self, "table", None)
        if table is None:
            return
        hidden_columns = (3, 6, 7)
        compact_controls = (
            "group_combo",
            "identity_search",
            "summary_label",
            "video_arrival_button",
            "video_review_button",
            "video_review_done_button",
            "video_review_ignore_button",
            "video_review_bib_edit",
            "video_discovered_btn",
            "queue_expand_btn",
            "offset_spin",
        )
        if enabled:
            table.setMinimumWidth(0)
            table.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
            if not hasattr(self, "_continuous_table_state"):
                self._continuous_table_state = {
                    column: (table.isColumnHidden(column), table.columnWidth(column))
                    for column in range(table.columnCount())
                }
            if not hasattr(self, "_continuous_control_state"):
                self._continuous_control_state = {
                    name: getattr(self, name).isVisible()
                    for name in compact_controls
                    if hasattr(self, name)
                }
            for column in hidden_columns:
                table.setColumnHidden(column, True)
            for name in compact_controls:
                widget = getattr(self, name, None)
                if widget is not None:
                    widget.setVisible(False)
            table.set_compact_mode(True)
            return
        state = getattr(self, "_continuous_table_state", None)
        if state is None:
            return
        table.set_compact_mode(False)
        table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        for column, (was_hidden, width) in state.items():
            table.setColumnHidden(column, was_hidden)
            table.setColumnWidth(column, width)
        del self._continuous_table_state
        control_state = getattr(self, "_continuous_control_state", {})
        for name, was_visible in control_state.items():
            widget = getattr(self, name, None)
            if widget is not None:
                widget.setVisible(was_visible)
        if hasattr(self, "_continuous_control_state"):
            del self._continuous_control_state

    def _refresh_filtered_view(self) -> None:
        events = self._events_for_current_metadata(self.passage_store.events())
        metadata = self._current_metadata()
        metadata_context_key = (
            (metadata.race_id, metadata.stage_id)
            if metadata is not None
            else ("", "")
        )
        event_ids = {event.event_id for event in events}
        if (
            metadata_context_key != self._metadata_context_key
            or self._timeline_cache_signature() != self._timeline_signature
            or event_ids != set(self._lookups)
            or event_ids != set(self._event_review_statuses)
        ):
            self.refresh()
            return
        self._total_event_count = len(events)
        self._render_filtered_queue(events, self._selected_event_id)

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
        self._total_event_count = len(events)
        signature = self._timeline_cache_signature()
        if signature != self._timeline_signature:
            self._timeline_signature = signature
            self._lookup_cache = {
                event_id: cached
                for event_id, cached in self._lookup_cache.items()
                if self._cache_survives_timeline_update(cached[1])
            }

        self._update_group_combo(events)
        self._review_batches = build_review_batches(
            events,
            review_gap_ms=self.REVIEW_BATCH_GAP_MS,
            subwave_gap_ms=self.REVIEW_SUBWAVE_GAP_MS,
        )
        self._review_batch_by_event_id = {
            event_id: batch
            for batch in self._review_batches
            for event_id in batch.event_ids
        }
        self._reconcile_video_discoveries(events)
        if self._active_review_batch_id and not any(
            batch.batch_id == self._active_review_batch_id
            for batch in self._review_batches
        ):
            self._batch_mode = False
            self._active_review_batch_id = ""
        self._lookups = {
            event.event_id: self._cached_lookup(event)
            for event in events
        }
        self._located_event_ids.clear()
        self._confirmed_event_ids.clear()
        self._event_review_statuses.clear()
        for event in events:
            lookup = self._lookups[event.event_id]
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
            regular_association = self._source_association(
                event.event_id,
                REGULAR_SOURCE,
                regular,
            )
            high_speed_association = self._source_association(
                event.event_id,
                HIGH_SPEED_SOURCE,
                high_speed,
            )
            readiness_status = review_status_text(lookup, regular, high_speed)
            self._event_review_statuses[event.event_id] = self._confirmation_status(
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

        self._render_filtered_queue(events, previous_event_id)

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
        query = self.identity_search.text().strip()
        if selected_group or query or self._active_review_filter != "all":
            self.refresh()
            return
        self._total_event_count = len(events)
        signature = self._timeline_cache_signature()
        if signature != self._timeline_signature:
            self._timeline_signature = signature
            self._lookup_cache = {
                event_id: cached
                for event_id, cached in self._lookup_cache.items()
                if self._cache_survives_timeline_update(cached[1])
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

        self._renumber_visible_rows()

        selected_row = next(
            (
                index
                for index, event in enumerate(self._visible_events)
                if event.event_id == selected_event_id
            ),
            -1,
        )
        if selected_row >= 0:
            self.table.setCurrentCell(selected_row, 1)
            self.table.selectRow(selected_row)
        elif self._visible_events:
            selected_row = 0
            selected_event_id = self._visible_events[0].event_id
            self.table.setCurrentCell(selected_row, 1)
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
        if value == "已确认":
            return QColor("#16845b")
        if value == "未确认":
            return QColor("#c0372b")
        if value in {"异常", "受阻"}:
            return QColor("#c0372b")
        if value == "待核对":
            return QColor("#7d5b0c")
        return QColor("#526170")

    @classmethod
    def _apply_status_style(cls, item: QTableWidgetItem, value: str) -> None:
        item.setForeground(cls._status_color(value))
        font = item.font()
        font.setBold(value in {
            "未确认",
            "异常",
            "受阻",
            "已确认",
        })
        item.setFont(font)

    def _on_table_selection_changed(self) -> None:
        row = self.table.currentRow()
        if 0 <= row < len(self._visible_events):
            event_id = self._visible_events[row].event_id
            self._select_event(event_id)

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
                for location in PassageReviewSurface._regular_locations(lookup)
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
                    if _association_matches_location(association, location)
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

    def _begin_selection_timing(
        self,
        event_id: str,
        priority_pane: PassageEvidencePane,
    ) -> None:
        self._deferred_start_timer.stop()
        self._selection_event_id = str(event_id)
        self._selection_started_at = time.perf_counter()
        self._selection_first_frame_ms = 0.0
        self._selection_expected_panes = 0
        self._selection_pending_panes.clear()
        self._deferred_selection_panes.clear()
        self._selection_priority_pane = priority_pane

    def _track_selection_pane(
        self,
        pane: PassageEvidencePane,
        *,
        deferred: bool,
    ) -> None:
        if not pane.has_playback_worker:
            return
        self._selection_expected_panes += 1
        self._selection_pending_panes.add(pane)
        if deferred and pane.worker_start_deferred:
            self._deferred_selection_panes.add(pane)

    def _start_deferred_selection_panes(self) -> None:
        self._deferred_start_timer.stop()
        panes = tuple(self._deferred_selection_panes)
        self._deferred_selection_panes.clear()
        for pane in panes:
            pane.start_deferred_worker()

    def _on_pane_initial_frame_ready(
        self,
        pane: PassageEvidencePane,
        event_id: str,
    ) -> None:
        if (
            str(event_id) != self._selection_event_id
            or pane not in self._selection_pending_panes
        ):
            return
        elapsed_ms = (time.perf_counter() - self._selection_started_at) * 1000.0
        if self._selection_first_frame_ms <= 0.0:
            self._selection_first_frame_ms = elapsed_ms
        self._selection_pending_panes.discard(pane)
        if pane is self._selection_priority_pane:
            self._start_deferred_selection_panes()
        if self._selection_pending_panes:
            return
        logger.info(
            "Athlete selection event=%s panes=%d first_frame=%.1fms all_frames=%.1fms",
            self._selection_event_id,
            self._selection_expected_panes,
            self._selection_first_frame_ms,
            elapsed_ms,
        )
        self._deferred_start_timer.stop()
        self._selection_event_id = ""
        self._selection_priority_pane = None

    def _location_on_current_media(
        self,
        event: PassageEvent,
        pane: PassageEvidencePane,
    ) -> Optional[PassageVideoLocation]:
        location = pane.location
        if location is None or location.segment.media_started_at_ms is None:
            return None
        camera_index = max(1, int(pane.camera_index))
        session_key = self._recording_session_key(location)
        offset_ms = self._continuous_clock_offsets.get(
            (camera_index, session_key),
            self._clock_offset_for_camera(camera_index),
        )
        position_ms = max(
            0,
            event.timeline_timestamp_ms
            + offset_ms
            - int(location.segment.media_started_at_ms),
        )
        if location.segment.media_duration_ms is not None:
            position_ms = min(position_ms, location.segment.media_duration_ms)
        return replace(
            location,
            passage_position_ms=position_ms,
            playback_position_ms=max(
                0,
                position_ms - self.CONTINUOUS_SKIP_LEAD_MS,
            ),
            clock_offset_ms=offset_ms,
        )

    def _select_event(
        self,
        event_id: str,
        *,
        preserve_current_frame: Optional[PassageEvidencePane] = None,
    ) -> None:
        self.selection_controller.select(
            event_id,
            preserve_current_frame=preserve_current_frame,
        )

    def _apply_selection_plan(
        self,
        plan: ReviewSelectionPlan,
        *,
        preserve_current_frame: Optional[PassageEvidencePane] = None,
    ) -> None:
        event = plan.event
        lookup = plan.lookup
        regular_locations = plan.regular_locations
        regular = plan.regular_summary
        high_speed = plan.high_speed
        reuse_continuous_media = plan.reuse_continuous_media
        same_batch_media = plan.same_batch_media
        preserve_media = plan.preserve_media
        active_pane = plan.active_pane
        switching_batch_event = plan.switching_batch_event
        if switching_batch_event:
            # Identity changes must leave the displayed frame stable. Do not
            # start deferred workers merely to pause them.
            self._set_sync_playing(False, seek_final=False)
            for pane in self.evidence_panes:
                pane.set_playing(False)
        regular_association = self._source_association(
            event.event_id, REGULAR_SOURCE, regular
        )
        high_speed_association = self._source_association(
            event.event_id, HIGH_SPEED_SOURCE, high_speed
        )
        if not preserve_media:
            self._set_sync_playing(False)
            self._begin_selection_timing(event.event_id, active_pane)
            self._shared_delta_ms = self._saved_delta_ms(
                regular,
                high_speed,
                regular_association,
                high_speed_association,
            )
        self._selected_event_id = event.event_id
        if self._batch_mode:
            batch = self._review_batch_by_event_id.get(event.event_id)
            self._active_review_batch_id = batch.batch_id if batch is not None else ""
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
        display_status = self._display_confirmation_status(status)
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
        self.selected_time_value.setText(passage_time)
        self.source_value.setText(display_status)
        athlete_summary = (
            f"{identity} {athlete_name if athlete_name != '--' else ''}".strip()
        )
        self.current_passage_label.setText(athlete_summary)
        row = self.table.currentRow()
        position_text = (
            f"{row + 1:,} / {len(self._visible_events):,}"
            if 0 <= row < len(self._visible_events)
            else f"0 / {len(self._visible_events):,}"
        )
        group_label = self.group_value.text().strip()
        self.current_context_label.setText(
            " · ".join(
                value
                for value in (group_label, position_text, display_status)
                if value and value != "--"
            )
        )
        if not preserve_media:
            self.current_time_label.setText(
                format_passage_time(event.timeline_timestamp_ms + self._shared_delta_ms)
            )
            for pane in self.regular_panes:
                pane_location = regular_locations.get(pane.camera_index)
                defer_worker_start = pane is not active_pane
                pane.set_passage(
                    event,
                    pane_location,
                    self._source_association(
                        event.event_id,
                        REGULAR_SOURCE,
                        pane_location,
                    ),
                    initial_delta_ms=self._shared_delta_ms,
                    lookup_status=(
                        pane_location.status
                        if pane_location is not None
                        else lookup.status
                    ),
                    defer_worker_start=defer_worker_start,
                )
                if not (
                    self._batch_mode
                    and defer_worker_start
                    and self.SINGLE_CAMERA_PREVIEW
                ):
                    self._track_selection_pane(
                        pane,
                        deferred=defer_worker_start,
                    )
            if self._show_high_speed_pane:
                defer_high_speed = (
                    self._batch_mode and self.high_speed_pane is not active_pane
                )
                self.high_speed_pane.set_passage(
                    event,
                    high_speed,
                    high_speed_association,
                    initial_delta_ms=self._shared_delta_ms,
                    lookup_status=high_speed.status if high_speed is not None else "",
                    defer_worker_start=defer_high_speed,
                )
                if not defer_high_speed or not self.SINGLE_CAMERA_PREVIEW:
                    self._track_selection_pane(
                        self.high_speed_pane,
                        deferred=False,
                    )
            if self._deferred_selection_panes:
                if active_pane not in self._selection_pending_panes:
                    self._start_deferred_selection_panes()
                else:
                    self._deferred_start_timer.start()
            self.play_both_btn.setText("联动 ▶")
        else:
            for pane in self.regular_panes:
                pane_location = regular_locations.get(pane.camera_index)
                pane_association = self._source_association(
                    event.event_id,
                    REGULAR_SOURCE,
                    pane_location,
                )
                pane_same_media = (
                    pane.location is not None
                    and pane._media_context(pane_location)
                    == pane._media_context(pane.location)
                )
                if reuse_continuous_media and pane_same_media:
                    pane.rebind_passage(
                        event,
                        pane_location,
                        pane_association,
                        lookup_status=(
                            pane_location.status
                            if pane_location is not None
                            else lookup.status
                        ),
                    )
                elif pane_location is not None and not pane_same_media:
                    pane.set_passage(
                        event,
                        pane_location,
                        pane_association,
                        initial_delta_ms=self._shared_delta_ms,
                        lookup_status=pane_location.status,
                        defer_worker_start=pane is not active_pane,
                    )
                elif pane_location is None and pane.location is not None:
                    pane.clear_passage()
                elif pane.association != pane_association:
                    pane.set_association(pane_association)
            if (
                self._show_high_speed_pane
            ):
                high_speed_same_media = (
                    self.high_speed_pane.location is not None
                    and self.high_speed_pane._media_context(high_speed)
                    == self.high_speed_pane._media_context(
                        self.high_speed_pane.location
                    )
                )
                if reuse_continuous_media and high_speed_same_media:
                    self.high_speed_pane.rebind_passage(
                        event,
                        high_speed,
                        high_speed_association,
                        lookup_status=(
                            high_speed.status if high_speed is not None else ""
                        ),
                    )
                elif high_speed is not None and not high_speed_same_media:
                    self.high_speed_pane.set_passage(
                        event,
                        high_speed,
                        high_speed_association,
                        initial_delta_ms=self._shared_delta_ms,
                        lookup_status=high_speed.status,
                        defer_worker_start=(
                            self._batch_mode
                            and self.high_speed_pane is not active_pane
                        ),
                    )
                elif self.high_speed_pane.association != high_speed_association:
                    self.high_speed_pane.set_association(high_speed_association)
        self._update_reference_states(event.event_id)
        if same_batch_media:
            self._update_shared_from_pane(active_pane)
            if preserve_current_frame is None:
                self._skip_continuous_gap(event, active_pane)
        self._update_batch_controls(event.event_id)
        self._update_navigation_controls()
        if not self._batch_mode and self._filmstrip_context is not None:
            # Avoid blocking the UI while the previous strip worker is being
            # stopped; let the inactive camera start first on low-end PCs.
            if self.playback_coordinator is not None:
                self.playback_coordinator.request_filmstrip_update(deferred=True)
        elif self.playback_coordinator is not None:
            self.playback_coordinator.request_filmstrip_update(deferred=False)
        else:
            self._update_filmstrip()

    def _filmstrip_context_for_active_pane(
        self,
    ) -> Optional[tuple[Path, int, int, int, tuple[int, ...], int, int]]:
        if not self._batch_mode and not self.FILMSTRIP_ALWAYS_AVAILABLE:
            return None
        pane = (
            self._active_pane
            if self._active_pane in self.regular_panes
            else self.regular_pane
        )
        location = pane.location
        if location is None or not location.video_path.is_file():
            return None
        duration_ms = int(
            location.segment.media_duration_ms
            or getattr(pane, "_duration_ms", 0)
            or 0
        )
        if duration_ms <= 0:
            return None
        positions: list[int] = []
        for event in self._visible_events:
            projected = self._location_on_current_media(event, pane)
            if projected is None:
                continue
            if projected.segment.segment_id != location.segment.segment_id:
                continue
            positions.append(int(projected.passage_position_ms))
        for candidate in self._video_navigation_candidates:
            if int(getattr(candidate, "camera_index", 0)) != int(pane.camera_index):
                continue
            if str(getattr(candidate, "segment_id", "")) != location.segment.segment_id:
                continue
            positions.append(int(getattr(candidate, "video_position_ms", 0)))
        if not positions:
            positions.append(int(location.passage_position_ms))
        # Before the playback worker emits its first frame, the pane reports
        # position 0 even though the selected athlete's target is later in
        # the recording. Use the target passage frame in that transient state
        # so the filmstrip's rightmost 0-second tile shows the athlete rather
        # than an empty lead-in frame.
        current_frame_index = int(getattr(pane, "_current_frame_index", -1))
        current_position = int(getattr(pane, "_current_position_ms", 0))
        target_position = int(getattr(pane, "_target_position_ms", 0))
        center = (
            target_position
            if current_frame_index < 0
            else current_position
        )
        window_start = (max(0, center) // self.FILMSTRIP_WINDOW_MS) * self.FILMSTRIP_WINDOW_MS
        start_ms = min(window_start, max(0, duration_ms - 1))
        end_ms = min(duration_ms, start_ms + self.FILMSTRIP_WINDOW_MS)
        if end_ms <= start_ms:
            end_ms = min(duration_ms, start_ms + 1_000)
        window_positions = tuple(
            position for position in positions if start_ms <= position <= end_ms
        )
        if not window_positions:
            window_positions = (max(start_ms, min(end_ms, center)),)
        origin_ms = int(
            location.segment.media_started_at_ms
            if location.segment.media_started_at_ms is not None
            else location.segment.started_at_ms
        )
        return (
            location.video_path,
            start_ms,
            end_ms,
            center,
            window_positions,
            origin_ms + start_ms,
            origin_ms + end_ms,
        )

    def _stop_activity_analysis(self) -> None:
        if self.playback_coordinator is not None:
            self.playback_coordinator.stop_activity()

    def _schedule_activity_analysis(
        self, video_path: Path, start_ms: int, end_ms: int
    ) -> None:
        if self.playback_coordinator is not None:
            self.playback_coordinator.schedule_activity(video_path, start_ms, end_ms)

    def _set_activity_paused(self, paused: bool) -> None:
        if self.playback_coordinator is not None:
            self.playback_coordinator.set_operator_busy(paused)

    def _update_filmstrip(self) -> None:
        if not hasattr(self, "video_filmstrip"):
            return
        context = self._filmstrip_context_for_active_pane()
        if context is None:
            self._filmstrip_anchor_timer.stop()
            self._pending_filmstrip_anchor = None
            self._filmstrip_context = None
            self._filmstrip_absolute_window = None
            if self.playback_coordinator is not None:
                self.playback_coordinator.clear_activity()
            self.activity_timeline.set_target_position(None)
            self.activity_timeline.hide()
            self.video_filmstrip.setVisible(True)
            self.video_filmstrip.clear("当前没有可用视频胶卷")
            self.preview_timeline.setRange(0, 0)
            self.preview_timeline.setEnabled(False)
            self.preview_mark_btn.setChecked(False)
            self.preview_mark_btn.setEnabled(False)
            self.preview_confirm_btn.setEnabled(False)
            return
        (
            video_path,
            start_ms,
            end_ms,
            current_position_ms,
            anchors,
            absolute_start_ms,
            absolute_end_ms,
        ) = context
        origin_ms = int(absolute_start_ms) - int(start_ms)
        pane = (
            self._active_pane
            if self._active_pane in self.regular_panes
            else self.regular_pane
        )
        location = pane.location
        if location is not None:
            session_key = self._recording_session_key(location)
            offset_ms = self._continuous_clock_offsets.get(
                (int(pane.camera_index), session_key),
                int(location.clock_offset_ms),
            )
            # Filmstrip positions are media-local. Convert them to the same
            # calibrated race clock used by the lower camera panes.
            origin_ms -= int(offset_ms)
        was_visible = self.video_filmstrip.isVisible()
        self.video_filmstrip.setVisible(True)
        if not was_visible and not self._review_split_resize_pending:
            self._review_split_resize_pending = True
            QTimer.singleShot(0, self._finish_review_content_split_resize)
        self.video_filmstrip.set_display_origin(origin_ms)
        self.video_filmstrip.set_current_position(current_position_ms)
        self.activity_timeline.set_current_position(current_position_ms)
        self.activity_timeline.set_target_position(current_position_ms)
        self._schedule_activity_analysis(video_path, start_ms, end_ms)
        self.preview_timeline.setRange(int(start_ms), int(end_ms))
        self.preview_timeline.set_target_position(
            int(pane._target_position_ms)
            if int(start_ms) <= int(pane._target_position_ms) <= int(end_ms)
            else None
        )
        self.preview_timeline.setValue(
            max(int(start_ms), min(int(current_position_ms), int(end_ms)))
        )
        self.preview_timeline.setEnabled(int(end_ms) > int(start_ms))
        signature = (video_path, start_ms, end_ms)
        if signature != self._filmstrip_context:
            self._filmstrip_anchor_timer.stop()
            self._pending_filmstrip_anchor = None
            self.video_filmstrip.clear_marker()
            if (
                self._filmstrip_context is not None
                and self._filmstrip_absolute_window is not None
                and self._filmstrip_context[0] == video_path
                and self._filmstrip_absolute_window[0]
                <= absolute_start_ms
                < self._filmstrip_absolute_window[1]
            ):
                # Adjacent athletes can resolve through overlapping recording
                # metadata. Keep the active five-minute filmstrip until the
                # review time leaves its absolute window.
                return
            self._filmstrip_context = signature
            self._filmstrip_absolute_window = (
                absolute_start_ms,
                absolute_end_ms,
            )
            self.video_filmstrip.load(
                video_path,
                start_ms,
                end_ms,
                positions_ms=anchors,
                origin_ms=origin_ms,
            )
            return
        # The time range is unchanged: keep the existing filmstrip and only
        # decode newly discovered arrival positions.
        self.video_filmstrip.append_positions(video_path, anchors)

    def _filmstrip_judgment_pane(self) -> Optional[PassageEvidencePane]:
        pane = (
            self._active_pane
            if self._active_pane in self.regular_panes
            else self.regular_pane
        )
        if pane.location is None or pane.location.status not in _OPENABLE_STATUSES:
            return None
        return pane

    def _on_preview_frame_ready(
        self,
        pane: PassageEvidencePane,
        image,
        position_ms: int,
        frame_index: int,
    ) -> None:
        if pane is not self._active_playback_pane():
            return
        self.preview_video_view.set_frame(
            image,
            source_width=pane._source_width,
            source_height=pane._source_height,
        )
        self.preview_video_view.set_frame_indicator(
            pane.frame_indicator_label.text()
        )
        self.preview_video_view.set_marker_mode(
            self.preview_mark_btn.isChecked()
        )
        can_mark = (
            pane is self._filmstrip_judgment_pane()
            and pane._current_frame_index >= 0
            and pane.location is not None
            and pane.location.status in _CONFIRMABLE_STATUSES
        )
        self.preview_mark_btn.setEnabled(can_mark)
        if not can_mark:
            self.preview_mark_btn.setChecked(False)
            self.preview_confirm_btn.setEnabled(False)
        pending = pane.pending_confirmation()
        if pending is not None:
            self.preview_video_view.set_marker(
                float(pending["marker_x_normalized"]),
                float(pending["marker_y_normalized"]),
                pane._identity,
                confirmed=False,
                simple=pane.is_auyat_rgb,
            )
        elif pane.association is not None:
            self.preview_video_view.set_marker(
                pane.association.marker_x_normalized,
                pane.association.marker_y_normalized,
                pane._identity,
                confirmed=True,
                simple=pane.is_auyat_rgb,
            )
        else:
            self.preview_video_view.clear_marker()

    def _on_preview_marker_selected(
        self,
        x_normalized: float,
        y_normalized: float,
    ) -> None:
        pane = self._filmstrip_judgment_pane()
        if pane is None:
            return
        self._activate_pane(pane, align=False)
        pane._on_marker_position_selected(
            float(x_normalized),
            float(y_normalized),
        )
        self.preview_video_view.set_marker(
            float(x_normalized),
            float(y_normalized),
            pane._identity,
            confirmed=False,
            simple=pane.is_auyat_rgb,
        )
        self.preview_mark_btn.setChecked(True)
        self.preview_confirm_btn.setEnabled(True)

    def _confirm_preview_marker(self) -> None:
        pane = self._filmstrip_judgment_pane()
        if pane is None or pane.pending_confirmation() is None:
            return
        if not self._confirm_pending_marker(pane):
            return
        association = pane.association
        if association is not None:
            self.preview_video_view.set_marker(
                association.marker_x_normalized,
                association.marker_y_normalized,
                pane._identity,
                confirmed=True,
                simple=pane.is_auyat_rgb,
            )
        else:
            self.preview_video_view.clear_marker()
        self.preview_mark_btn.setChecked(False)
        self.preview_confirm_btn.setEnabled(False)

    def _on_filmstrip_marker_selected(
        self,
        position_ms: int,
        marker_x_normalized: float,
        marker_y_normalized: float,
        frame_index: int,
    ) -> None:
        pane = self._filmstrip_judgment_pane()
        if pane is None:
            return
        self._activate_pane(pane, align=False)
        self.preview_mark_btn.setChecked(True)
        self.preview_mark_btn.setEnabled(True)
        self.preview_confirm_btn.setEnabled(True)
        pane.set_external_pending_marker(
            frame_index=int(frame_index),
            position_ms=int(position_ms),
            marker_x_normalized=float(marker_x_normalized),
            marker_y_normalized=float(marker_y_normalized),
        )
        self.video_filmstrip.set_marker(
            int(position_ms),
            float(marker_x_normalized),
            float(marker_y_normalized),
            int(frame_index),
        )

    def _confirm_filmstrip_marker(self) -> bool:
        pane = self._filmstrip_judgment_pane()
        if pane is None or pane.pending_confirmation() is None:
            return False
        confirmed = self._confirm_pending_marker(pane)
        if confirmed:
            self.video_filmstrip.clear_marker()
            self.preview_mark_btn.setChecked(False)
            self.preview_confirm_btn.setEnabled(False)
        return confirmed

    def _clear_filmstrip_marker(self) -> None:
        self.video_filmstrip.clear_marker()
        self.preview_mark_btn.setChecked(False)
        self.preview_confirm_btn.setEnabled(False)

    def _preview_filmstrip_position(self, position_ms: int) -> None:
        """Preview the latest drag position without queuing every mouse move."""

        self._pending_filmstrip_preview_position = int(position_ms)
        self.video_filmstrip.set_current_position(int(position_ms))
        if hasattr(self, "preview_timeline") and self.preview_timeline.maximum() > self.preview_timeline.minimum():
            self.preview_timeline.setValue(
                max(
                    self.preview_timeline.minimum(),
                    min(int(position_ms), self.preview_timeline.maximum()),
                )
            )
        if not self._filmstrip_preview_timer.isActive():
            self._filmstrip_preview_timer.start()

    def _on_preview_timeline_pressed(self) -> None:
        self._preview_timeline_dragging = True

    def _on_preview_timeline_moved(self, position_ms: int) -> None:
        if not getattr(self, "_preview_timeline_dragging", False):
            return
        self._preview_filmstrip_position(int(position_ms))

    def _on_preview_timeline_released(self) -> None:
        self._preview_timeline_dragging = False
        self._seek_filmstrip_position(int(self.preview_timeline.value()))

    def _flush_filmstrip_preview(self) -> None:
        position_ms = self._pending_filmstrip_preview_position
        self._pending_filmstrip_preview_position = None
        if position_ms is None:
            return
        pane = self._filmstrip_judgment_pane()
        if pane is None or pane.available_delta_bounds() is None:
            return
        target_delta_ms = int(position_ms) - int(pane._target_position_ms)
        if self.SINGLE_CAMERA_PREVIEW:
            self._seek_preview_pane_delta(pane, target_delta_ms)
        else:
            self._preview_both_delta(target_delta_ms)

    def _seek_filmstrip_position(self, position_ms: int) -> None:
        self._filmstrip_preview_timer.stop()
        self._pending_filmstrip_preview_position = None
        pane = self._filmstrip_judgment_pane()
        if not bool(getattr(self.video_filmstrip, "_marker_mode", False)):
            event_id = self._filmstrip_event_id_at_position(int(position_ms))
            if event_id and event_id != self._selected_event_id:
                # Changing the roster row must not first seek to that athlete's
                # target frame. Keep the currently displayed media frame; the
                # exact filmstrip position is applied below after the row is
                # rebound, preventing a visible intermediate jump.
                self._select_event(
                    event_id,
                    preserve_current_frame=pane,
                )
        self._pending_filmstrip_position = int(position_ms)
        self._filmstrip_seek_retry_count = 0
        self.video_filmstrip.set_current_position(int(position_ms))
        if hasattr(self, "preview_timeline") and self.preview_timeline.maximum() > self.preview_timeline.minimum():
            self.preview_timeline.setValue(
                max(
                    self.preview_timeline.minimum(),
                    min(int(position_ms), self.preview_timeline.maximum()),
                )
            )
        if self._filmstrip_seek_pending:
            return
        self._filmstrip_seek_pending = True
        QTimer.singleShot(40, self._flush_filmstrip_position)

    def _open_filmstrip_position_for_judgment(self, position_ms: int) -> None:
        """Seek the linked cameras and enlarge the active pane for marking."""

        self._seek_filmstrip_position(position_ms)
        pane = (
            self._active_pane
            if self._active_pane in self.regular_panes
            else self.regular_pane
        )
        QTimer.singleShot(60, lambda: self._toggle_maximized_pane(pane))

    def _flush_filmstrip_position(self) -> None:
        self._filmstrip_seek_pending = False
        position_ms = self._pending_filmstrip_position
        self._pending_filmstrip_position = None
        if position_ms is None:
            return
        preferred_pane = (
            self._active_pane
            if self._active_pane in self.evidence_panes
            else self.regular_pane
        )
        seek_pane = next(
            (
                pane
                for pane in (preferred_pane, *self.evidence_panes)
                if pane.location is not None
                and pane.available_delta_bounds() is not None
            ),
            None,
        )
        if seek_pane is None:
            # The filmstrip can be ready before the lower video worker has
            # emitted metadata. Keep the requested position briefly so a
            # double-click still links to both camera panes once duration and
            # frame bounds become available.
            if (
                preferred_pane.location is not None
                and self._filmstrip_seek_retry_count < 25
            ):
                self._filmstrip_seek_retry_count += 1
                self._filmstrip_seek_pending = True
                QTimer.singleShot(120, self._flush_filmstrip_position)
                return
            self._pending_filmstrip_position = None
            return
        self._filmstrip_seek_retry_count = 0
        target_delta_ms = int(position_ms) - int(seek_pane._target_position_ms)
        # The filmstrip represents the shared race timeline.  Move every
        # visible camera together so each pane applies its own calibrated
        # passage target instead of leaving the secondary angle behind.
        if self.SINGLE_CAMERA_PREVIEW:
            self._seek_preview_pane_delta(seek_pane, target_delta_ms)
        else:
            self._seek_both_delta(target_delta_ms)

    def _seek_preview_pane_delta(
        self,
        pane: PassageEvidencePane,
        delta_ms: int,
    ) -> None:
        """Seek only the active review camera during low-power preview."""

        self._activate_pane(pane, align=False)
        bounds = pane.available_delta_bounds()
        if bounds is None:
            return
        lower, upper = bounds
        self._shared_delta_ms = max(lower, min(int(delta_ms), upper))
        pane.seek_passage_delta(self._shared_delta_ms, preview=True)
        self._update_shared_time_label()

    def _filmstrip_event_id_at_position(self, position_ms: int) -> str:
        """Resolve a normal filmstrip click to the nearest visible athlete."""

        pane = (
            self._active_pane
            if self._active_pane in self.regular_panes
            else self.regular_pane
        )
        location = pane.location
        if location is None:
            return ""
        target_path = str(Path(location.video_path).absolute())
        candidates: list[tuple[int, int, str]] = []
        for row, event in enumerate(self._visible_events):
            lookup = self._lookups.get(event.event_id)
            if lookup is None:
                continue
            event_location = self._regular_location_for_camera(
                lookup,
                pane.camera_index,
            )
            if event_location is None:
                continue
            projected = self._location_on_current_media(event, pane)
            if projected is None or projected.segment.segment_id != location.segment.segment_id:
                continue
            if str(Path(event_location.video_path).absolute()) != target_path:
                continue
            distance = abs(int(projected.passage_position_ms) - int(position_ms))
            candidates.append((distance, row, event.event_id))
        if not candidates:
            return ""
        distance, _row, event_id = min(candidates)
        # A sparse strip is sampled every two seconds; do not retarget the
        # athlete list when a click is clearly outside its passage window.
        return event_id if distance <= 1_500 else ""

    def _skip_continuous_gap(
        self,
        event: PassageEvent,
        pane: PassageEvidencePane,
    ) -> bool:
        if (
            not self.CONTINUOUS_AUTO_SKIP
            or not self._batch_mode
            or pane.location is None
        ):
            return False
        target_position_ms = int(pane.location.passage_position_ms)
        current_position_ms = int(pane._current_position_ms)
        gap_ms = target_position_ms - current_position_ms
        if abs(gap_ms) < self.CONTINUOUS_SKIP_GAP_MS:
            return False
        seek_position_ms = max(0, target_position_ms - self.CONTINUOUS_SKIP_LEAD_MS)
        if gap_ms > 0:
            seek_position_ms = max(current_position_ms, seek_position_ms)
        self._shared_delta_ms = seek_position_ms - target_position_ms
        pane.seek_passage_delta(self._shared_delta_ms)
        self._update_shared_time_label()
        return True

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
        self.current_context_label.clear()
        self.current_time_label.setText("--:--:--.---")
        self.batch_context_label.clear()
        self.batch_review_btn.setVisible(False)
        self.video_discovered_btn.setEnabled(False)
        self.video_discovered_table.setVisible(False)
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
            self.table.setCurrentCell(row, 1)
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

    def _calibrate_continuous_session(
        self,
        event: PassageEvent,
        pane: PassageEvidencePane,
        association: PassageEvidenceAssociation,
    ) -> bool:
        return self._calibrate_continuous_session_at_position(
            event,
            pane,
            int(association.position_ms),
            int(association.confirmed_at_ms),
        )

    def _calibrate_continuous_session_at_position(
        self,
        event: PassageEvent,
        pane: PassageEvidencePane,
        position_ms: int,
        calibrated_at_ms: int,
    ) -> bool:
        location = pane.location
        if (
            not self._batch_mode
            or pane.source_kind != REGULAR_SOURCE
            or location is None
            or location.segment.media_started_at_ms is None
        ):
            return False
        key = (
            location.segment.camera_index,
            self._recording_session_key(location),
        )
        if key in self._continuous_clock_offsets:
            return False
        offset_ms = (
            int(location.segment.media_started_at_ms)
            + int(position_ms)
            - int(event.timeline_timestamp_ms)
        )
        self._continuous_clock_offsets[key] = offset_ms
        try:
            self.calibration_store.record(
                camera_index=location.segment.camera_index,
                session_key=key[1],
                offset_ms=offset_ms,
                anchor_event_id=event.event_id,
                anchor_bib=event.bib.strip() or "未知",
                calibrated_at_ms=int(calibrated_at_ms),
            )
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            logger.warning("failed to persist camera calibration: %s", error)
        self._continuous_calibration_revision += 1
        self._update_batch_controls(event.event_id)
        self._lookup_cache.clear()
        events = self._events_for_current_metadata(self.passage_store.events())
        self._lookups = {
            candidate.event_id: self._cached_lookup(candidate)
            for candidate in events
        }
        self.batch_context_label.setToolTip(
            f"机位 {location.segment.camera_index} 已按首人校准 {offset_ms:+d} ms"
        )
        return True

    def _confirm_pending_marker(self, pane: PassageEvidencePane) -> bool:
        event = self.passage_store.get(self._selected_event_id)
        pending = pane.pending_confirmation()
        if event is None or pending is None:
            return False
        identity = event.bib.strip() or "未知"
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
        self._calibrate_continuous_session(event, pane, association)
        for candidate in self.evidence_panes:
            if candidate is pane or candidate.source_kind != pane.source_kind:
                continue
            candidate.set_association(None)
        self._update_reference_states(event.event_id)
        should_advance = self._batch_mode or (
            self.auto_advance_checkbox.isChecked()
            and self._all_available_sources_confirmed(event.event_id)
        )
        self._update_event_confirmation_status(event.event_id)
        if should_advance and confirmed_row >= 0:
            self._move_selection(1, skip_confirmed=True)
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
        self._event_review_statuses[event_id] = status
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
            (8, self._display_confirmation_status(status)),
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
            self.source_value.setText(self._display_confirmation_status(status))
        batch = self._active_review_batch()
        if self._batch_mode and batch is not None:
            self._update_batch_roster_overlays(batch)
        if self._active_review_filter != "all":
            self.refresh()
            return
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
        self._event_review_statuses.pop(event_id, None)

    def _render_summary(self) -> None:
        counts = {
            "pending": sum(
                status == "待核对" for status in self._event_review_statuses.values()
            ),
            "blocked": sum(
                status == "受阻" for status in self._event_review_statuses.values()
            ),
            "confirmed": sum(
                status == "已确认" for status in self._event_review_statuses.values()
            ),
            "all": self._total_event_count,
        }
        for filter_key, button in self.review_filter_buttons.items():
            prefix = "✓ " if filter_key == self._active_review_filter else ""
            button.setText(
                f"{prefix}{self.review_filter_labels[filter_key]} "
                f"{counts[filter_key]:,}"
            )
        active_label = self.review_filter_labels[self._active_review_filter]
        if self._active_review_filter == "all":
            summary = (
                f"当前显示 {len(self._visible_events):,} / "
                f"{self._total_event_count:,} 条"
            )
        else:
            summary = (
                f"当前筛选：{active_label} · "
                f"{len(self._visible_events):,} / {self._total_event_count:,} 条"
            )
        self.summary_label.setText(summary)
        self._available_evidence_count = len(self._located_event_ids)

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
            self.table.setCurrentCell(row, 1)
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

    def _move_selection(self, delta: int, *, skip_confirmed: bool = False) -> None:
        if self.table.rowCount() <= 0:
            return
        current_row = self.table.currentRow()
        direction = 1 if int(delta) >= 0 else -1
        row = current_row + int(delta)
        if skip_confirmed:
            while 0 <= row < self.table.rowCount():
                candidate = self._visible_events[row]
                has_evidence = (
                    self.association_store.get(candidate.event_id, REGULAR_SOURCE)
                    is not None
                    or self.association_store.get(
                        candidate.event_id,
                        HIGH_SPEED_SOURCE,
                    )
                    is not None
                )
                if not has_evidence:
                    break
                row += direction
            if not 0 <= row < self.table.rowCount():
                return
        row = max(0, min(row, self.table.rowCount() - 1))
        if row == self.table.currentRow():
            return
        event_id = self._visible_events[row].event_id
        self.table.blockSignals(True)
        self.table.setCurrentCell(row, 1)
        self.table.selectRow(row)
        self.table.blockSignals(False)
        item = self.table.item(row, 1)
        if item is not None:
            self.table.scrollToItem(item, QAbstractItemView.PositionAtCenter)
        active_pane = self._active_playback_pane()
        moving_back_in_continuous_mode = self._batch_mode and int(delta) < 0
        self._select_event(
            event_id,
            preserve_current_frame=(
                active_pane if moving_back_in_continuous_mode else None
            ),
        )
        if not moving_back_in_continuous_mode:
            return
        if self._seek_saved_confirmation(event_id):
            return
        event = self.passage_store.get(event_id)
        if event is not None:
            self._skip_continuous_gap(event, active_pane)

    def _seek_saved_confirmation(self, event_id: str) -> bool:
        association = self.association_store.get(event_id, REGULAR_SOURCE)
        if association is None:
            return False
        segment = self.timeline_store.get_segment(association.segment_id)
        pane = (
            self._pane_for_camera(segment.camera_index)
            if segment is not None
            else self._active_playback_pane()
        )
        if (
            pane.location is None
            or pane.location.segment.segment_id != association.segment_id
        ):
            return False
        self._activate_pane(pane, align=False)
        self._shared_delta_ms = (
            int(association.position_ms) - int(pane._target_position_ms)
        )
        if pane.available_delta_bounds() is not None:
            pane.seek_passage_delta(self._shared_delta_ms)
        self._update_shared_time_label()
        return True

    def _focused_evidence_pane(self) -> Optional[PassageEvidencePane]:
        widget = self.focusWidget()
        while widget is not None:
            for pane in self.evidence_panes:
                if widget is pane:
                    return pane
            widget = widget.parentWidget()
        return None

    def _active_playback_pane(self) -> PassageEvidencePane:
        focused = self._focused_evidence_pane()
        if focused is not None and focused.available_delta_bounds() is not None:
            return focused
        if (
            self._active_pane is not None
            and self._active_pane.available_delta_bounds() is not None
        ):
            return self._active_pane
        for pane in self.evidence_panes:
            if pane.available_delta_bounds() is not None:
                return pane
        return self._active_pane or self.regular_pane

    def _pause_inactive_panes(self, active: PassageEvidencePane) -> None:
        self._set_active_idle_prefetch(active)
        for pane in self.evidence_panes:
            if pane is not active:
                pane.set_playing(False)
                pane.park_playback_cache()

    def _set_active_idle_prefetch(
        self,
        active: Optional[PassageEvidencePane],
    ) -> None:
        for pane in self.all_evidence_panes:
            pane.set_idle_prefetch_enabled(pane is active)
            pane.set_active_judging(pane is active)

    def _update_shared_from_pane(self, pane: PassageEvidencePane) -> None:
        delta_ms = pane.current_delta_ms()
        if delta_ms is None:
            return
        self._shared_delta_ms = delta_ms
        self._update_shared_time_label()

    def _activate_pane(
        self,
        pane: PassageEvidencePane,
        *,
        align: bool,
    ) -> bool:
        if pane not in self.evidence_panes:
            return False
        if self._sync_playing:
            self._set_sync_playing(False, seek_final=False)
        previous = self._active_pane
        switched = previous is not pane
        if switched and previous is not None:
            self._update_shared_from_pane(previous)
            previous.set_playing(False)
        self._active_pane = pane
        # In low-power preview mode secondary cameras are prepared but not
        # decoded until the operator explicitly switches to them.
        pane.start_deferred_worker()
        self._pause_inactive_panes(pane)
        if switched and align:
            pane.seek_passage_delta(self._shared_delta_ms)
        if self._maximized_window is not None and pane in self._maximized_hosted_panes:
            self._maximized_pane = pane
            self._maximized_pane_index = self._maximized_original_indexes.get(pane, -1)
            self._update_maximized_mode_controls(
                side_by_side=len(self._maximized_hosted_panes) > 1
            )
        return switched

    def _toggle_active_pane(self) -> None:
        self._toggle_pane(self._active_playback_pane())

    def _toggle_pane(self, pane: PassageEvidencePane) -> None:
        self._set_activity_paused(not pane.is_playing)
        was_sync_playing = self._sync_playing
        if was_sync_playing:
            self._set_sync_playing(False, seek_final=False)
        switched = self._activate_pane(pane, align=False)
        if pane.is_playing and not was_sync_playing:
            pane.set_playing(False)
            self._update_shared_from_pane(pane)
            return
        if not switched:
            self._update_shared_from_pane(pane)
        pane.seek_passage_delta(self._shared_delta_ms, linked_playing=True)

    def _step_active_pane(self, frame_delta: int) -> None:
        self._step_pane(self._active_playback_pane(), frame_delta)

    def _step_pane(
        self,
        pane: PassageEvidencePane,
        frame_delta: int,
    ) -> None:
        if abs(int(frame_delta)) == CTRL_FRAME_STEP:
            direction = 1 if int(frame_delta) > 0 else -1
            if self._navigate_video_candidate(direction, pane):
                return
        switched = self._activate_pane(pane, align=False)
        if not switched:
            current_delta_ms = pane.current_delta_ms()
            pane.step(frame_delta)
            bounds = pane.available_delta_bounds()
            if bounds is not None:
                lower, upper = bounds
                base_delta_ms = (
                    self._shared_delta_ms
                    if current_delta_ms is None
                    else current_delta_ms
                )
                self._shared_delta_ms = max(
                    lower,
                    min(
                        base_delta_ms
                        + int(frame_delta) * pane.frame_duration_ms(),
                        upper,
                    ),
                )
                self._update_shared_time_label()
            return
        bounds = pane.available_delta_bounds()
        if bounds is None:
            return
        lower, upper = bounds
        self._shared_delta_ms = max(
            lower,
            min(
                self._shared_delta_ms
                + int(frame_delta) * pane.frame_duration_ms(),
                upper,
            ),
        )
        pane.seek_passage_delta(self._shared_delta_ms)
        self._update_shared_time_label()

    def _navigate_video_candidate(
        self,
        direction: int,
        pane: PassageEvidencePane,
    ) -> bool:
        """Seek to the next visual candidate for the active camera.

        A visual candidate is only a navigation hint. The selected passage
        row and official timing data remain unchanged. Candidates in another
        recording segment rebind the same pane to that segment so navigation
        remains continuous across archived files.
        """

        direction = 1 if int(direction) >= 0 else -1
        if pane not in self.evidence_panes:
            return False
        candidates = tuple(
            candidate
            for candidate in self._video_navigation_candidates
            if int(getattr(candidate, "camera_index", 0)) == int(pane.camera_index)
        )
        location = pane.location
        if not candidates or location is None or getattr(pane, "_worker", None) is None:
            return False

        current_path = str(Path(location.video_path).absolute())
        current_segment_id = str(location.segment.segment_id)
        origin_ms = int(
            location.segment.media_started_at_ms
            if location.segment.media_started_at_ms is not None
            else location.segment.started_at_ms
        )
        current_absolute_ms = origin_ms + int(getattr(pane, "_current_position_ms", 0))
        ordered = tuple(
            sorted(
                candidates,
                key=lambda candidate: (
                    int(getattr(candidate, "peak_at_ms", 0)),
                    str(getattr(candidate, "candidate_id", "")),
                ),
            )
        )
        frame_guard = max(1, pane.frame_duration_ms())
        if direction > 0:
            target = next(
                (
                    candidate
                    for candidate in ordered
                    if int(getattr(candidate, "peak_at_ms", 0))
                    > current_absolute_ms + frame_guard
                ),
                None,
            )
        else:
            target = next(
                (
                    candidate
                    for candidate in reversed(ordered)
                    if int(getattr(candidate, "peak_at_ms", 0))
                    < current_absolute_ms - frame_guard
                ),
                None,
            )
        if target is None:
            return False

        target_segment_id = str(getattr(target, "segment_id", ""))
        target_path_value = str(getattr(target, "video_path", ""))
        target_path = str(Path(target_path_value).absolute()) if target_path_value else ""
        target_position_ms = int(getattr(target, "video_position_ms", 0))
        if (
            target_segment_id
            and target_segment_id != current_segment_id
        ) or (
            target_path
            and target_path != current_path
        ):
            # Rebind the same pane to the target segment without changing the
            # selected passage row. This keeps the operation inside the main
            # review window and still lets the operator bind a roster number
            # at the displayed frame.
            event = self.passage_store.get(self._selected_event_id)
            if event is None:
                return False
            lookup = self.timeline_store.locate_passage(
                int(getattr(target, "peak_at_ms", 0)),
                race_id=event.race_id,
            )
            target_location = next(
                (
                    location
                    for location in lookup.locations
                    if int(location.segment.camera_index) == int(pane.camera_index)
                    and (
                        not target_segment_id
                        or location.segment.segment_id == target_segment_id
                    )
                    and location.status in _OPENABLE_STATUSES
                ),
                None,
            )
            if target_location is None:
                return False
            target_position_ms = int(
                getattr(target, "video_position_ms", 0)
            )
            initial_delta_ms = target_position_ms - int(
                target_location.passage_position_ms
            )
            self._activate_pane(pane, align=False)
            pane.set_passage(
                event,
                target_location,
                initial_delta_ms=initial_delta_ms,
            )
            self._shared_delta_ms = int(initial_delta_ms)
            self._update_shared_time_label()
            return True

        target_delta_ms = target_position_ms - int(pane._target_position_ms)
        self._activate_pane(pane, align=False)
        pane.seek_passage_delta(target_delta_ms)
        self._shared_delta_ms = target_delta_ms
        self._update_shared_time_label()
        return True

    def _seek_pane_delta(
        self,
        pane: PassageEvidencePane,
        delta_ms: int,
        *,
        preview: bool,
    ) -> None:
        self._activate_pane(pane, align=False)
        # Preview clips from camera 1/2 are one temporary evidence window. A
        # scrub or seek on either preview pane must move both cameras together;
        # formal clips keep the existing single-camera precision workflow.
        if preview or (
            pane.location is not None and pane.location.status == "preview"
        ):
            self._apply_both_delta(delta_ms, preview=preview)
            return
        bounds = pane.available_delta_bounds()
        if bounds is None:
            return
        lower, upper = bounds
        self._shared_delta_ms = max(lower, min(int(delta_ms), upper))
        pane.seek_passage_delta(self._shared_delta_ms, preview=preview)
        self._update_shared_time_label()

    def _on_pane_scrub_started(self, pane: PassageEvidencePane) -> None:
        self._set_activity_paused(True)
        if self._sync_playing:
            self._set_sync_playing(False, seek_final=False)
        self._active_pane = pane
        self._pause_inactive_panes(pane)
        self._update_shared_from_pane(pane)

    def _on_pane_position_changed(
        self,
        pane: PassageEvidencePane,
        delta_ms: int,
    ) -> None:
        if self._sync_playing or pane is not self._active_pane:
            return
        self._shared_delta_ms = int(delta_ms)
        self._update_shared_time_label()
        self._sync_filmstrip_from_pane(pane)

    def _sync_filmstrip_from_pane(self, pane: PassageEvidencePane) -> None:
        """Keep the top filmstrip aligned with the active camera frame."""

        if not hasattr(self, "video_filmstrip") or pane.location is None:
            return
        position_ms = int(getattr(pane, "_current_position_ms", 0))
        context = self._filmstrip_context_for_active_pane()
        if context is None:
            return
        video_path, start_ms, end_ms, _center, _anchors, *_ = context
        same_path = (
            self._filmstrip_context is not None
            and self._filmstrip_context[0] == video_path
        )
        in_window = int(start_ms) <= position_ms <= int(end_ms)
        if not same_path or not in_window:
            self._update_filmstrip()
            return
        self.video_filmstrip.set_current_position(position_ms)
        self.activity_timeline.set_current_position(position_ms)
        if self.playback_coordinator is not None:
            self.playback_coordinator.set_operator_busy(
                pane.is_playing
                or bool(getattr(pane, "_video_scrubbing", False))
            )
        # When the operator stops on a lower-pane frame, decode that exact
        # frame as a temporary filmstrip anchor. During playback/scrubbing we
        # deliberately avoid per-frame thumbnail generation.
        if not pane.is_playing and not bool(getattr(pane, "_video_scrubbing", False)):
            self._pending_filmstrip_anchor = (
                Path(pane.location.video_path),
                position_ms,
            )
            self._filmstrip_anchor_timer.start()

    def _flush_filmstrip_anchor(self) -> None:
        pending = self._pending_filmstrip_anchor
        self._pending_filmstrip_anchor = None
        if pending is None or not hasattr(self, "video_filmstrip"):
            return
        video_path, position_ms = pending
        if Path(getattr(self.video_filmstrip, "_video_path", "")) != video_path:
            return
        self.video_filmstrip.append_positions(video_path, (position_ms,))

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
        self._apply_both_delta(delta_ms, preview=False)

    def _seek_to_target_position(self) -> None:
        """Jump every visible camera to its calculated passage timestamp."""

        if self.passage_store.get(self._selected_event_id) is None:
            return
        self._set_sync_playing(False)
        self._seek_both_delta(0)

    def _preview_both_delta(self, delta_ms: int) -> None:
        self._apply_both_delta(delta_ms, preview=True)

    def _apply_both_delta(self, delta_ms: int, *, preview: bool) -> None:
        bounds = self._sync_delta_bounds()
        if bounds is None:
            return
        lower, upper = bounds
        self._shared_delta_ms = max(lower, min(int(delta_ms), upper))
        for pane in self.evidence_panes:
            pane.seek_passage_delta(
                self._shared_delta_ms,
                linked_playing=self._sync_playing,
                preview=preview,
            )
        self._update_shared_time_label()

    def _set_sync_playing(
        self,
        playing: bool,
        *,
        seek_final: bool = True,
    ) -> None:
        playing = bool(playing) and self._sync_delta_bounds() is not None
        self._set_activity_paused(playing)
        if playing == self._sync_playing:
            return
        if playing:
            self._pause_inactive_panes(self.regular_pane)
            self._sync_origin_delta_ms = self._shared_delta_ms
            self._sync_playing = True
            self._seek_both_delta(self._shared_delta_ms)
            self._sync_started_at = time.monotonic()
            self._last_sync_correction_at = (
                self._sync_started_at - self.SYNC_CORRECTION_COOLDOWN_SECONDS
            )
            self._sync_timer.start()
        else:
            if self._sync_playing:
                elapsed_ms = int((time.monotonic() - self._sync_started_at) * 1000.0)
                self._shared_delta_ms = self._sync_origin_delta_ms + elapsed_ms
            self._sync_playing = False
            self._sync_timer.stop()
            for pane in self.evidence_panes:
                pane.set_linked_playing(False)
        self.play_both_btn.setText(
            "联动 Ⅱ" if self._sync_playing else "联动 ▶"
        )
        if not self._sync_playing and seek_final:
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
        self._shared_delta_ms = target_delta_ms
        self._update_shared_time_label()
        self._correct_sync_drift(target_delta_ms)

    def _correct_sync_drift(self, target_delta_ms: int) -> None:
        now = time.monotonic()
        elapsed_ms = int((now - self._sync_started_at) * 1000.0)
        if (
            elapsed_ms < self.SYNC_STARTUP_GRACE_MS
            or (now - self._last_sync_correction_at)
            < self.SYNC_CORRECTION_COOLDOWN_SECONDS
        ):
            return

        corrected = False
        for pane in self.evidence_panes:
            drift_ms = pane.linked_drift_ms(target_delta_ms)
            if drift_ms is None:
                continue
            if abs(drift_ms) <= pane.linked_drift_tolerance_ms():
                continue
            pane.seek_passage_delta(target_delta_ms, linked_playing=True)
            corrected = True
        if corrected:
            self._last_sync_correction_at = now

    def _update_shared_time_label(self) -> None:
        event = self.passage_store.get(self._selected_event_id)
        if event is None:
            return
        self.current_time_label.setText(
            f"{format_passage_time(event.timeline_timestamp_ms + self._shared_delta_ms)} "
            f"(Δ{self._shared_delta_ms:+d} ms)"
        )

    def _fit_visible_evidence_views(self) -> None:
        for pane in self.evidence_panes:
            if pane.isVisible() and pane.video_view.has_frame:
                pane.video_view.fit_to_window()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        QTimer.singleShot(0, self._initialize_review_splitters)

    def _initialize_review_splitters(self) -> None:
        """Give the queue and evidence panes usable initial dimensions."""

        total_width = self.workspace_splitter.width()
        if total_width <= 0:
            return
        queue_width = max(320, int(total_width * 0.42))
        evidence_width = max(1, total_width - queue_width)
        self.workspace_splitter.setSizes([queue_width, evidence_width])
        self._queue_default_sizes = [queue_width, evidence_width]
        self._distribute_evidence_panes()

    def _distribute_evidence_panes(self) -> None:
        """Distribute the visible camera panes across the evidence splitter."""

        count = self.evidence_splitter.count()
        total_width = self.evidence_splitter.width()
        if count <= 0 or total_width <= 0:
            return
        active_panes = set(self.evidence_panes)
        active_count = sum(
            1
            for index in range(count)
            if self.evidence_splitter.widget(index) in active_panes
        )
        if active_count <= 0:
            return
        share = max(1, total_width // active_count)
        sizes = [
            share if self.evidence_splitter.widget(index) in active_panes else 0
            for index in range(count)
        ]
        self.evidence_splitter.setSizes(sizes)

    def _toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showMaximized()
            self.fullscreen_btn.setText("全屏")
        else:
            self.showFullScreen()
            self.fullscreen_btn.setText("退出全屏")
        QTimer.singleShot(0, self._fit_visible_evidence_views)

    def _toggle_maximized_pane(self, pane: PassageEvidencePane) -> None:
        if self._maximized_pane is pane and len(self._maximized_hosted_panes) == 1:
            self._restore_maximized_pane()
            return
        if self._maximized_pane is not None:
            if (
                pane.source_kind == REGULAR_SOURCE
                and self._maximized_pane.source_kind == REGULAR_SOURCE
            ):
                self._set_maximized_regular_mode(pane.camera_index)
                return
            self._restore_maximized_pane()

        pane_index = self.evidence_splitter.indexOf(pane)
        if pane_index < 0:
            return
        window = QDialog(
            self,
            Qt.Window
            | Qt.WindowTitleHint
            | Qt.WindowSystemMenuHint
            | Qt.WindowMinMaxButtonsHint
            | Qt.WindowCloseButtonHint,
        )
        title = pane.title_label.text().strip() or "机位画面"
        if pane._identity:
            title = f"{title} - {pane._identity}"
        window.setWindowTitle(title)
        window.setModal(False)
        window_layout = QVBoxLayout(window)
        window_layout.setContentsMargins(8, 8, 8, 8)
        window_layout.setSpacing(6)
        mode_bar = QHBoxLayout()
        mode_bar.setContentsMargins(4, 0, 4, 0)
        mode_bar.setSpacing(6)
        mode_label = QLabel(window)
        mode_label.setStyleSheet(
            "color: #0f4f86; font-size: 14pt; font-weight: 700; padding: 4px 2px;"
        )
        mode_bar.addWidget(mode_label)
        mode_bar.addStretch(1)
        mode_buttons: dict[object, QPushButton] = {}
        if pane.source_kind == REGULAR_SOURCE and len(self.regular_panes) > 1:
            side_by_side_btn = QPushButton("并排", window)
            side_by_side_btn.setCheckable(True)
            side_by_side_btn.setToolTip("同时显示全部普通机位（B）")
            side_by_side_btn.clicked.connect(
                lambda _checked=False: self._set_maximized_regular_mode(None)
            )
            mode_buttons["side_by_side"] = side_by_side_btn
            mode_bar.addWidget(side_by_side_btn)
            for regular_pane in self.regular_panes:
                camera_index = regular_pane.camera_index
                camera_btn = QPushButton(f"机位 {camera_index}", window)
                camera_btn.setCheckable(True)
                camera_btn.setToolTip(f"切换到机位 {camera_index}（{camera_index}）")
                camera_btn.clicked.connect(
                    lambda _checked=False, index=camera_index: (
                        self._set_maximized_regular_mode(index)
                    )
                )
                mode_buttons[camera_index] = camera_btn
                mode_bar.addWidget(camera_btn)
        window_layout.addLayout(mode_bar)
        content_splitter = QSplitter(Qt.Horizontal, window)
        content_splitter.setChildrenCollapsible(False)
        content_splitter.setHandleWidth(5)
        window_layout.addWidget(content_splitter, 1)

        self._maximized_pane = pane
        self._maximized_window = window
        self._maximized_pane_index = pane_index
        self._maximized_content_splitter = content_splitter
        self._maximized_original_indexes = {
            candidate: self.evidence_splitter.indexOf(candidate)
            for candidate in self.evidence_panes
        }
        self._maximized_mode_label = mode_label
        self._maximized_mode_buttons = mode_buttons
        self._maximized_camera_shortcuts = []
        escape_shortcut = QShortcut(QKeySequence(Qt.Key_Escape), window)
        escape_shortcut.setContext(Qt.WindowShortcut)
        escape_shortcut.setAutoRepeat(False)
        escape_shortcut.activated.connect(self._restore_maximized_pane)
        self._maximized_escape_shortcut = escape_shortcut
        if pane.source_kind == REGULAR_SOURCE and len(self.regular_panes) > 1:
            side_by_side_shortcut = QShortcut(QKeySequence("B"), window)
            side_by_side_shortcut.setContext(Qt.WindowShortcut)
            side_by_side_shortcut.setAutoRepeat(False)
            side_by_side_shortcut.activated.connect(
                lambda: self._set_maximized_regular_mode(None)
            )
            self._maximized_camera_shortcuts.append(side_by_side_shortcut)
            cycle_shortcut = QShortcut(QKeySequence(Qt.Key_Tab), window)
            cycle_shortcut.setContext(Qt.WindowShortcut)
            cycle_shortcut.setAutoRepeat(False)
            cycle_shortcut.activated.connect(self._cycle_maximized_regular_pane)
            self._maximized_camera_shortcuts.append(cycle_shortcut)
            for regular_pane in self.regular_panes:
                camera_index = regular_pane.camera_index
                shortcut = QShortcut(QKeySequence(str(camera_index)), window)
                shortcut.setContext(Qt.WindowShortcut)
                shortcut.setAutoRepeat(False)
                shortcut.activated.connect(
                    lambda index=camera_index: self._set_maximized_regular_mode(index)
                )
                self._maximized_camera_shortcuts.append(shortcut)
        window.finished.connect(
            lambda _result: self._restore_maximized_pane(close_window=False)
        )
        self._host_maximized_panes((pane,))
        self._activate_pane(pane, align=True)
        self._update_maximized_mode_controls(side_by_side=False)

        screen = self.screen() or QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            window.resize(
                max(1, int(available.width() * 0.9)),
                max(1, int(available.height() * 0.88)),
            )
        window.show()
        if screen is not None:
            frame = window.frameGeometry()
            frame.moveCenter(available.center())
            window.move(frame.topLeft())
        window.raise_()
        window.activateWindow()
        QTimer.singleShot(0, pane.video_view.fit_to_window)

    def _restore_hosted_panes_to_splitter(self) -> None:
        if not self._maximized_hosted_panes:
            return
        for pane in sorted(
            self._maximized_hosted_panes,
            key=lambda candidate: self._maximized_original_indexes.get(candidate, 0),
        ):
            pane_index = self._maximized_original_indexes.get(pane, 0)
            pane.setParent(self.evidence_splitter)
            self.evidence_splitter.insertWidget(max(0, pane_index), pane)
        self._maximized_hosted_panes = ()

    def _host_maximized_panes(
        self,
        panes: tuple[PassageEvidencePane, ...],
    ) -> None:
        content_splitter = self._maximized_content_splitter
        if content_splitter is None:
            return
        self._restore_hosted_panes_to_splitter()
        active_panes = self.evidence_panes
        for candidate in self.all_evidence_panes:
            if candidate not in panes:
                candidate.setVisible(candidate in active_panes)
                candidate.maximize_btn.setText("放大")
                candidate.maximize_btn.setToolTip("放大该机位（双击画面或按 F）")
        for pane in panes:
            content_splitter.addWidget(pane)
            pane.show()
            pane.maximize_btn.setText("缩小")
            pane.maximize_btn.setToolTip("恢复主界面（Esc 或 F）")
            content_splitter.setStretchFactor(content_splitter.indexOf(pane), 1)
        content_splitter.setSizes([1] * len(panes))
        self._maximized_hosted_panes = panes

    def _set_maximized_regular_mode(self, camera_index: Optional[int]) -> None:
        if self._maximized_window is None or not self.regular_panes:
            return
        if camera_index is None:
            panes = self.regular_panes
            active = (
                self._active_pane
                if self._active_pane in panes
                else panes[0]
            )
            side_by_side = True
        else:
            active = self._regular_panes_by_camera.get(int(camera_index))
            if active is None or active not in self.regular_panes:
                return
            panes = (active,)
            side_by_side = False
        self._host_maximized_panes(panes)
        self._maximized_pane = active
        self._maximized_pane_index = self._maximized_original_indexes.get(active, -1)
        self._activate_pane(active, align=True)
        active.video_view.setFocus()
        self._update_maximized_mode_controls(side_by_side=side_by_side)
        for pane in panes:
            QTimer.singleShot(0, pane.video_view.fit_to_window)

    def _cycle_maximized_regular_pane(self) -> None:
        panes = self.regular_panes
        if not panes:
            return
        active = self._active_pane if self._active_pane in panes else panes[0]
        next_index = (panes.index(active) + 1) % len(panes)
        self._set_maximized_regular_mode(panes[next_index].camera_index)

    def _update_maximized_mode_controls(self, *, side_by_side: bool) -> None:
        active = self._active_pane or self._maximized_pane
        if active is None:
            return
        if active.source_kind == REGULAR_SOURCE:
            current_text = f"当前判读：机位 {active.camera_index}"
        else:
            current_text = "当前判读：高速摄像"
        if self._maximized_mode_label is not None:
            self._maximized_mode_label.setText(current_text)
        if self._maximized_window is not None:
            identity = f" - {active._identity}" if active._identity else ""
            self._maximized_window.setWindowTitle(
                f"{APP_DISPLAY_NAME} · {current_text}{identity}"
            )
        for key, button in self._maximized_mode_buttons.items():
            checked = (
                side_by_side
                if key == "side_by_side"
                else not side_by_side
                and active.source_kind == REGULAR_SOURCE
                and key == active.camera_index
            )
            button.setChecked(bool(checked))
            button.setStyleSheet(
                "QPushButton { padding: 6px 12px; font-weight: 700; }"
                "QPushButton:checked { background: #1976c9; color: #ffffff; "
                "border: 2px solid #0f5f9f; }"
            )

    def _restore_maximized_pane(self, *, close_window: bool = True) -> None:
        pane = self._maximized_pane
        window = self._maximized_window
        if pane is None or window is None:
            return

        self._restore_hosted_panes_to_splitter()
        self._maximized_pane = None
        self._maximized_window = None
        self._maximized_escape_shortcut = None
        self._maximized_pane_index = -1
        self._maximized_content_splitter = None
        self._maximized_original_indexes = {}
        self._maximized_mode_label = None
        self._maximized_mode_buttons = {}
        self._maximized_camera_shortcuts = []

        active_panes = self.evidence_panes
        for candidate in self.all_evidence_panes:
            candidate.setVisible(candidate in active_panes)
            candidate.maximize_btn.setText("放大")
            candidate.maximize_btn.setToolTip("放大该机位（双击画面或按 F）")
        for index in range(self.evidence_splitter.count()):
            self.evidence_splitter.setStretchFactor(index, 1)
        self._distribute_evidence_panes()
        QTimer.singleShot(0, self._distribute_evidence_panes)
        if close_window:
            window.close()
        window.deleteLater()
        QTimer.singleShot(0, self._fit_visible_evidence_views)

    def _open_preferred_source(self, row: int, _column: int) -> None:
        if not (0 <= row < len(self._visible_events)):
            return
        event = self._visible_events[row]
        lookup = self._lookups.get(event.event_id)
        if lookup is None:
            return
        regular = source_location(lookup, high_speed=False)
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

    def reject(self) -> None:
        if self._maximized_window is not None:
            self._restore_maximized_pane()

    def closeEvent(self, event) -> None:
        self._restore_maximized_pane()
        self._sync_playing = False
        self._sync_timer.stop()
        if hasattr(self, "video_filmstrip"):
            self.video_filmstrip.stop()
        if self.playback_coordinator is not None:
            self.playback_coordinator.shutdown()
        for pane in self.all_evidence_panes:
            pane.shutdown(wait=True)
        event.accept()


class PassageReviewDialog(PassageReviewSurface):
    """Standalone passage-review window."""


__all__ = [
    "PassageEvidencePane",
    "PassageReviewDialog",
    "PassageReviewSurface",
    "UI_BASE_FONT_POINT_SIZE",
    "UI_FONT_FAMILY",
    "compact_source_status",
    "combined_review_status",
    "format_passage_time",
    "lookup_status_text",
    "review_status_text",
    "source_location",
]
