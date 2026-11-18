"""Status texts, time formatting and small table widgets of the passage review surface."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
from typing import Iterable, Optional

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QBrush, QPalette
from PyQt5.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QSizePolicy,
    QStyledItemDelegate,
    QTableWidget,
    QWidget,
)

from .external_clip_import import EXTERNAL_CLOCK_SOURCE
from .passage_evidence import PassageEvidenceAssociation
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


class _ReviewFilterBar(QWidget):
    """窄名单区自动分两行，避免筛选和搜索相互挤压。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._wrapped = None
        self.width_controls = ()
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(5)
        self.status = QHBoxLayout()
        self.tools = QHBoxLayout()
        self.status.setSpacing(5)
        self.tools.setSpacing(5)
        self.grid.addLayout(self.status, 0, 0)
        self.grid.addLayout(self.tools, 0, 1)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Maximum)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        wrapped = self.width() < 1100
        if wrapped != self._wrapped:
            self._wrapped = wrapped
            for widget, wide, narrow in self.width_controls:
                widget.setMinimumWidth(narrow if wrapped else wide)
            self.grid.removeItem(self.tools)
            self.grid.addLayout(self.tools, 1 if wrapped else 0, 0 if wrapped else 1)


class _AutoFitTableWidget(QTableWidget):
    """Fit columns to their content, then use the remaining viewport width."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._column_fit_scheduled = False
        self._compact_mode = False
        # Bound content measurement as the live roster grows. Full text is
        # still available in the cells/tooltips and the selected rider pane.
        self.horizontalHeader().setResizeContentsPrecision(100)

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
        self.resizeColumnsToContents()
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
