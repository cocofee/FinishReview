"""The whole race on one finish-line picture, the way a photo-finish camera shows it.

Every closed recording was already sampled along the finish line
(``finish_problems/strips``): one column per frame.  Laid side by side on the
chip clock, every rider of the race shows up exactly once, under the chips the
timing system read.  Where no sampled recording exists the picture stays grey;
grey never means that nobody crossed.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from zipfile import BadZipFile

import cv2
import numpy as np
from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import QHBoxLayout, QLabel, QPushButton, QScrollBar, QVBoxLayout, QWidget

from .chip_history import format_chip_time
from .crossing_time import recorder_crossing_time, workspace_clocks
from .line_sampling import read_strip_meta


STRIP_ROWS = 100                    # rows kept of each sampled line; drawn one to one
ZOOM_LEVELS = (5, 10, 25, 50, 100, 200)   # pixels per second; 25 is one pixel per frame at 25 fps
DEFAULT_ZOOM = 50
CACHE_BYTES = 96 * 1024 * 1024
CLICK_SLOP_PX = 4                   # a press that moves further drags the picture instead
EXTENT_MARGIN_MS = 30_000
RESCAN_MS = 10_000
AUTO_LABELLED = 2                   # a chip's "done": labelled by the system, not yet looked at (auto_label)
FIGURE_LEVEL = 15                   # colour change on the line that is someone there, not light or noise


@dataclass(frozen=True)
class StripPiece:
    """One sampled recording file, placed on the chip clock."""

    path: Path
    camera: int
    segment_id: str
    video_path: Path
    start_ms: int                   # recording clock
    end_ms: int
    archive: bool
    offset_ms: int                  # recording minus chip time at the start
    drift_ms: int = 0               # how much that offset changes by the end
    aligned: bool = True
    stamp: tuple = ()              # replacing a sampled file invalidates only this cache entry

    def chip_of(self, recorder_ms):
        span = max(1, self.end_ms - self.start_ms)
        return recorder_ms - self.offset_ms - self.drift_ms * (recorder_ms - self.start_ms) / span

    def recorder_of(self, chip_ms) -> int:
        span = max(1, self.end_ms - self.start_ms)
        if span <= self.drift_ms:
            raise ValueError("过线图校时时间范围无效")
        return int(round(self.start_ms + (chip_ms + self.offset_ms - self.start_ms)
                         * span / (span - self.drift_ms)))

    @property
    def chip_start(self) -> float:
        return self.chip_of(self.start_ms)

    @property
    def chip_end(self) -> float:
        return self.chip_of(self.end_ms)


def strip_folder(workspace, camera) -> Path:
    return Path(workspace) / "finish_problems" / "strips" / f"camera_{int(camera):02d}"


def strip_pieces(workspace, camera, known=None):
    """Every sampled recording of one camera, oldest first.

    ``known`` holds what each strip file's metadata said last time, so a
    growing race reads only its new files.  Returns (pieces, metadata).
    """

    from .analysis_workspace import AnalysisWorkspace, TIMELINE_JOURNAL, race_clock_window
    from .video_timeline import DEFAULT_CLOCK_SOURCE

    workspace = Path(workspace)
    folder = strip_folder(workspace, camera)
    journal = workspace / TIMELINE_JOURNAL
    if not folder.is_dir() or not journal.is_file():
        return [], {}
    source = AnalysisWorkspace(workspace, allow_empty_scope=True)
    store = source.timeline()
    window = race_clock_window(source.reads)
    clocks = workspace_clocks(workspace)
    metas, pieces = {}, []
    for path in sorted(folder.glob("*.npz")):
        try:
            stat = path.stat()
        except OSError:
            continue
        stamp = (stat.st_mtime_ns, stat.st_size)
        cached = (known or {}).get(path)
        try:
            meta = cached[1] if cached and cached[0] == stamp else read_strip_meta(path)
        except (BadZipFile, EOFError):
            continue
        if not isinstance(meta, dict):
            continue
        metas[path] = (stamp, meta)
        segment = store.get_segment(str(meta.get("segment_id", "")))
        if (segment is None or int(segment.camera_index) != int(camera)
                or segment.clock_source != DEFAULT_CLOCK_SOURCE
                or segment.race_id and source.race_id and segment.race_id != source.race_id):
            continue
        start = segment.media_started_at_ms
        start = meta.get("media_started_at_ms") if start is None else start
        duration = meta.get("media_duration_ms")
        if segment is None or start is None or not duration:
            continue
        start, end = int(start), int(start) + int(duration)
        video = store.resolve_video_path(segment)
        frame = {"camera_index": int(camera), "segment_id": segment.segment_id,
                 "session_id": str(meta.get("session_id", ""))}
        first = recorder_crossing_time(start, frame, workspace, clocks=clocks)
        last = recorder_crossing_time(end, frame, workspace, clocks=clocks)
        offset = start - first.value_ms
        pieces.append(StripPiece(path, int(camera), segment.segment_id, video, start, end,
                                 "archive" in video.stem.lower(), offset,
                                 (end - last.value_ms) - offset, first.source != "video", stamp))
    if window is not None:
        pieces = [piece for piece in pieces if piece.chip_end > window[0] and piece.chip_start < window[1]]
    pieces.sort(key=lambda piece: (piece.start_ms, piece.archive))
    return pieces, metas


def _rows(strip: np.ndarray) -> np.ndarray:
    if strip.shape[0] == STRIP_ROWS:
        return strip
    image = np.empty((STRIP_ROWS, strip.shape[1], 3), np.uint8)
    for index in range(0, strip.shape[1], 4096):
        block = np.ascontiguousarray(strip[:, index:index + 4096])
        image[:, index:index + block.shape[1]] = cv2.resize(
            block, (block.shape[1], STRIP_ROWS), interpolation=cv2.INTER_AREA)
    return image


class StripCache:
    """Recently drawn strips, reduced to the rows the picture shows."""

    def __init__(self, budget: int = CACHE_BYTES):
        self.budget = budget
        self._items: OrderedDict = OrderedDict()
        self._bytes = 0

    def clear(self):
        self._items.clear()
        self._bytes = 0

    def get(self, piece: StripPiece):
        hit = self._items.get(piece.path)
        if hit is not None and hit[0] == piece:
            self._items.move_to_end(piece.path)
            return hit[1], hit[2]
        # Only the picture and timestamps are needed, not geometry or a
        # second copy of the full 500-row archive made by load_strip.
        with np.load(piece.path, allow_pickle=False) as data:
            image = _rows(data["strip"])
            times = piece.chip_of(piece.start_ms + data["pts_ms"].astype(np.float64))
        if times.ndim != 1 or times.size != image.shape[1] or not np.all(np.diff(times) >= 0):
            raise ValueError("过线图帧时间无效")
        if hit is not None:
            self._items.pop(piece.path)
            self._bytes -= hit[1].nbytes + hit[2].nbytes
        size = times.nbytes + image.nbytes
        # A very large recording may be displayed once, but never retained
        # beyond the cache budget. Normal archive pieces fit comfortably.
        if size <= self.budget:
            while self._items and self._bytes + size > self.budget:
                _path, (_piece, old_times, old) = self._items.popitem(last=False)
                self._bytes -= old_times.nbytes + old.nbytes
            self._items[piece.path] = (piece, times, image)
            self._bytes += size
        return times, image


def _paint(canvas, times, image, edges, mask=None):
    """Each pixel column shows the frames whose time falls inside it.

    Zoomed out a column averages several frames; zoomed in a frame is held
    until the next one, but never across a gap in the recording.
    """

    if times.size == 0 or image.shape[1] != times.size:
        return
    left = np.searchsorted(times, edges[:-1], "left")
    right = np.searchsorted(times, edges[1:], "left")
    step = float(np.median(np.diff(times))) if times.size > 1 else 40.0
    many = right > left
    if mask is not None:
        many &= mask
    if many.any():
        low, high = int(left[many].min()), int(right[many].max())
        sums = np.zeros((image.shape[0], high - low + 1, 3), np.float32)
        np.cumsum(image[:, low:high], axis=1, dtype=np.float32, out=sums[:, 1:])
        a, b = left[many] - low, right[many] - low
        canvas[:, many] = ((sums[:, b] - sums[:, a]) / (b - a)[None, :, None] + 0.5).astype(np.uint8)
    previous = left - 1
    held = (~many & (previous >= 0)
            & (edges[:-1] - times[np.maximum(previous, 0)] < 1.5 * max(step, 1.0)))
    if mask is not None:
        held &= mask
    if held.any():
        canvas[:, held] = image[:, previous[held]]


def render_strip(pieces, cache, view_ms, ms_per_px, width):
    """The finish line from ``view_ms`` on, ``width`` pixels wide (BGR)."""

    canvas = np.full((STRIP_ROWS, width, 3), 205, np.uint8)
    edges = view_ms + np.arange(width + 1, dtype=np.float64) * ms_per_px
    visible = [piece for piece in pieces if piece.chip_end > edges[0] and piece.chip_start < edges[-1]]
    # A long recording outranks the short clips cut from the same stream, so
    # the seams are its own; the clips only fill what it does not cover.
    archives = []
    for archive in (True, False):
        for piece in (part for part in visible if part.archive == archive and part.aligned):
            if not archive and any(a <= piece.chip_start and piece.chip_end <= b for a, b in archives):
                continue
            try:
                times, image = cache.get(piece)
            except (OSError, ValueError, KeyError, BadZipFile, EOFError):
                continue                        # one corrupt file does not hide its usable clips
            if archive:
                _paint(canvas, times, image, edges)
                if times.size:
                    archives.append((piece.chip_start, piece.chip_end))
            else:
                # Mask pixels, not timestamps: a removed span must not make
                # the last frame before it smear across the archive.
                free = np.ones(width, dtype=bool)
                for a, b in archives:
                    free &= (edges[1:] <= a) | (edges[:-1] >= b)
                _paint(canvas, times, image, edges, free)
    return canvas


def frame_at(pieces, chip_ms):
    """The recorded frame at a moment of the chip clock, as a jump request."""

    covering = [piece for piece in pieces if piece.aligned and piece.chip_start <= chip_ms < piece.chip_end]
    # The long recording first: the operator can drag on from there instead of
    # stopping at the end of a two-second clip.
    for piece in sorted(covering, key=lambda piece: (not piece.archive, piece.start_ms)):
        if piece.video_path.is_file():
            return {"camera_index": piece.camera, "segment_id": piece.segment_id,
                    "video_path": str(piece.video_path),
                    "position_ms": max(0, min(piece.end_ms - piece.start_ms - 1,
                                              piece.recorder_of(chip_ms) - piece.start_ms)),
                    "frame_index": None}
    return None


def first_crossing(pieces, cache, chip_ms):
    """Where on the chip clock the first rider of a wave really meets the line.

    A chip is read up to a few hundred milliseconds off its rider's crossing,
    more than the gap between riders in a bunch.  A wave is preceded by at
    least two quiet seconds, so its first rider is the first figure to arrive
    on the line.  A rider crosses it square on: many places along it change
    within a frame or two.  Someone walking along the line, a drifting shadow or
    staff shifting their feet change a place or two at a time, or stay there;
    the crowd at the end of the line never settles.  None of those is a rider.
    The front wheel is thin, so one frame back is taken when the places the
    rider fills were already stirring.  None when the strip cannot tell.
    """

    low, high = chip_ms - 3000, chip_ms + 2500
    for archive in (True, False):
        parts = sorted((piece for piece in pieces if piece.aligned and piece.archive == archive
                        and piece.chip_end > low and piece.chip_start < high), key=lambda piece: piece.chip_start)
        try:
            loaded = [cache.get(piece) for piece in parts]
        except (OSError, ValueError, KeyError, BadZipFile, EOFError):
            continue
        if not loaded:
            continue
        times = np.concatenate([part[0] for part in loaded])
        image = np.concatenate([part[1] for part in loaded], axis=1)
        order = np.argsort(times, kind="stable")
        times, image = times[order], image[:, order]
        inside = (times >= low) & (times <= high)
        times, image = times[inside], image[:, inside]
        quiet = times < chip_ms - 1100
        if quiet.sum() < 10:
            continue
        background = np.median(image[:, quiet].astype(np.int16), axis=1)
        change = np.abs(image.astype(np.int16) - background[:, None, :]).max(axis=2)
        calm = (change[:, quiet] > FIGURE_LEVEL).mean(axis=1) <= 0.25
        if calm.sum() < 20:
            continue
        changed = (change > FIGURE_LEVEL) & calm[:, None]
        stirred = np.zeros_like(changed)
        for back in range(1, 5):
            stirred[:, back:] |= changed[:, :-back]
        fresh = changed & ~stirred
        count = fresh.sum(axis=0)
        arriving = count + np.append(count[1:], 0) + np.append(count[2:], [0, 0])
        for index in np.flatnonzero((times >= chip_ms - 1000) & (times <= chip_ms + 800)):
            if count[index] < 2 or arriving[index] < 6:
                continue
            rows = fresh[:, index:index + 3].any(axis=1)
            rows = (np.convolve(rows, np.ones(7), "same") > 0) & calm
            if times[index] < chip_ms - 250:
                # Well before the chip and staying on: not someone riding over the line.
                later = (times >= times[index]) & (times < times[index] + 1500)
                if changed[rows][:, later].any(axis=0).mean() >= 0.8:
                    continue
            noise = np.percentile(change[rows][:, quiet], 90, axis=1)
            if index > 0 and (change[rows, index - 1] - noise > 12).sum() >= 2:
                index -= 1
            return float(times[index])
    return None


def earlier_arrival(pieces, cache, at_ms, others=()):
    """When someone else came onto the line up to 2 s before a crossing without a chip; None if nobody did.

    One crossing row may be several people (15:50, 10-29: the row's moment
    showed the last 2 of 4; the first two came on 1.7 s earlier).  An arrival
    as in first_crossing, 0.7-2 s before AT_MS, counts when the body it starts
    is gone again before AT_MS: still on the line then (someone walking along
    it, 10-29: 5 of 7 such hints) it is the one crossing.  Arrivals near
    another crossing in OTHERS are that one's.  Any recording of the moment
    will do, aligned to the chips or not.
    """

    low, high = at_ms - 4500, at_ms + 1500
    for archive in (True, False):
        parts = sorted((piece for piece in pieces if piece.archive == archive
                        and piece.chip_end > low and piece.chip_start < high), key=lambda piece: piece.chip_start)
        try:
            loaded = [cache.get(piece) for piece in parts]
        except (OSError, ValueError, KeyError, BadZipFile, EOFError):
            continue
        if not loaded:
            continue
        times = np.concatenate([part[0] for part in loaded])
        image = np.concatenate([part[1] for part in loaded], axis=1)
        order = np.argsort(times, kind="stable")
        times, image = times[order], image[:, order]
        inside = (times >= low) & (times <= high)
        times, image = times[inside], image[:, inside]
        quiet = times < at_ms - 2500
        if inside.sum() <= 20 or quiet.sum() < 10:
            continue
        background = np.median(image[:, quiet].astype(np.int16), axis=1)
        change = np.abs(image.astype(np.int16) - background[:, None, :]).max(axis=2)
        calm = (change[:, quiet] > FIGURE_LEVEL).mean(axis=1) <= 0.25
        if calm.sum() < 20:
            return None
        changed = (change > FIGURE_LEVEL) & calm[:, None]
        stirred = np.zeros_like(changed)
        for back in range(1, 5):
            stirred[:, back:] |= changed[:, :-back]
        fresh = changed & ~stirred
        count = fresh.sum(axis=0)
        arriving = count + np.append(count[1:], 0) + np.append(count[2:], [0, 0])
        # Bodies on the line over time: what touches across a few places and frames is one.
        _count, bodies = cv2.connectedComponents(
            cv2.dilate(changed.astype(np.uint8), np.ones((5, 3), np.uint8)), connectivity=8)
        at = int(np.argmin(np.abs(times - at_ms)))
        crossing = set(np.unique(bodies[:, max(0, at - 2):at + 3][changed[:, max(0, at - 2):at + 3]])) - {0}
        for index in np.flatnonzero((times >= at_ms - 2000) & (times <= at_ms - 700)):
            if count[index] < 2 or arriving[index] < 6:
                continue
            if any(abs(times[index] - other) <= 400 for other in others):
                continue
            rows = fresh[:, index:index + 3].any(axis=1)
            rows = (np.convolve(rows, np.ones(7), "same") > 0) & calm
            later = (times >= times[index]) & (times < times[index] + 1500)
            if changed[rows][:, later].any(axis=0).mean() >= 0.8:
                continue  # staying on: not someone passing
            if set(np.unique(bodies[:, index:index + 3][fresh[:, index:index + 3]])) & crossing:
                continue
            return float(times[index])
        return None
    return None


class RaceStripView(QWidget):
    """The picture itself: the strip, the chips above it, the time below."""

    clicked = pyqtSignal(object)                # chip-clock milliseconds
    view_changed = pyqtSignal()

    LABEL_ROWS = (11, 22, 33)
    STRIP_TOP = 36
    AXIS_HEIGHT = 16

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(self.STRIP_TOP + STRIP_ROWS + self.AXIS_HEIGHT)
        self.setMinimumWidth(200)
        self.pieces = []
        self.chips = []                         # (chip ms, bib, confirmed)
        self.cursor = None
        self.px_per_s = DEFAULT_ZOOM
        self.view_ms = None
        self.message = "正在读取过线图…"
        self.cache = StripCache()
        self._pixmap, self._pixmap_key, self._version = None, None, 0
        self._press = None
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)
        self.setToolTip("全程过线图：横向是时间。号码是芯片参考，并非已认出画面中的人。"
                        "单击跳转；点过图后 ←/→ 移动，+/− 缩放，Home/End 到首尾。"
                        "滚轮移动，Ctrl+滚轮缩放；回到视频后 ←/→ 仍逐帧。灰色为无过线图，不代表无人。")

    @property
    def ms_per_px(self) -> float:
        return 1000.0 / self.px_per_s

    def span_ms(self) -> float:
        return self.width() * self.ms_per_px

    def extent(self):
        chips = [stamp for stamp, _bib, _done in self.chips]
        ranges = [(min(chips) - EXTENT_MARGIN_MS, max(chips) + EXTENT_MARGIN_MS)] if chips else []
        ranges.extend((piece.chip_start, piece.chip_end) for piece in self.pieces)
        return (min(a for a, _b in ranges), max(b for _a, b in ranges)) if ranges else None

    def set_pieces(self, pieces, message=""):
        self.pieces = list(pieces)
        self.message = message
        self._version += 1
        self._settle()

    def set_chips(self, chips):
        chips = sorted(chips)
        if chips != self.chips:
            self.chips = chips
            self._settle()

    def set_cursor(self, chip_ms):
        self.cursor = None if chip_ms is None else int(chip_ms)
        if self.cursor is not None and self.view_ms is not None:
            span = self.span_ms()
            if not self.view_ms + span * 0.05 <= self.cursor <= self.view_ms + span * 0.95:
                self.set_view(self.cursor - span / 2)
                return
        self._settle()

    def _settle(self):
        """Keep a view once there is something to show, and keep it in range."""

        if self.view_ms is None:
            extent = self.extent()
            if extent is None:
                self.update()
                return
            start = self.cursor - self.span_ms() / 2 if self.cursor is not None else extent[0]
            self.set_view(start)
            return
        self.set_view(self.view_ms)

    def set_view(self, view_ms):
        extent = self.extent()
        if extent is not None:
            view_ms = max(extent[0], min(view_ms, extent[1] - self.span_ms()))
        self.view_ms = float(view_ms)
        self.view_changed.emit()
        self.update()

    def zoom(self, direction, anchor_x=None):
        index = ZOOM_LEVELS.index(self.px_per_s) if self.px_per_s in ZOOM_LEVELS else ZOOM_LEVELS.index(DEFAULT_ZOOM)
        index = max(0, min(len(ZOOM_LEVELS) - 1, index + (1 if direction > 0 else -1)))
        if ZOOM_LEVELS[index] == self.px_per_s:
            return
        if anchor_x is None:
            cursor = self.x_of(self.cursor) if self.cursor is not None else None
            anchor_x = cursor if cursor is not None and 0 <= cursor < self.width() else self.width() / 2
        anchor = self.time_at(anchor_x)
        self.px_per_s = ZOOM_LEVELS[index]
        if anchor is not None:
            self.set_view(anchor - anchor_x * self.ms_per_px)
        else:
            self.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.view_ms is not None:
            self.set_view(self.view_ms)

    def time_at(self, x):
        return None if self.view_ms is None else self.view_ms + x * self.ms_per_px

    def x_of(self, chip_ms):
        return None if self.view_ms is None else int(round((chip_ms - self.view_ms) / self.ms_per_px))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#edf1f5"))
        bottom = self.STRIP_TOP + STRIP_ROWS
        if self.view_ms is None:
            painter.setPen(QColor("#667085"))
            painter.drawText(8, self.STRIP_TOP + 40, self.message or "没有可显示的过线图")
            return
        key = (self.view_ms, self.px_per_s, self.width(), self._version)
        if key != self._pixmap_key:
            image = render_strip(self.pieces, self.cache, self.view_ms, self.ms_per_px, self.width())
            rgb = np.ascontiguousarray(image[:, :, ::-1])
            self._pixmap = QPixmap.fromImage(
                QImage(rgb.data, rgb.shape[1], rgb.shape[0], rgb.strides[0], QImage.Format_RGB888).copy())
            self._pixmap_key = key
        painter.drawPixmap(0, self.STRIP_TOP, self._pixmap)
        if self.message:
            painter.setPen(QColor("#667085"))
            painter.drawText(8, self.STRIP_TOP + 16, self.message)
        # Bibs in three staggered rows; a label takes the first row it fits in.
        ends = [-1e9] * len(self.LABEL_ROWS)
        metrics = painter.fontMetrics()
        for stamp, bib, done in self.chips:
            x = self.x_of(stamp)
            if x is None or not -40 <= x <= self.width() + 40:
                continue
            painter.setPen(QColor("#ba2525"))
            painter.drawLine(x, self.STRIP_TOP - 6, x, self.STRIP_TOP)
            label = bib or "?"
            width = metrics.horizontalAdvance(label) + (8 if done else 0)
            left = max(0, min(x - width // 2, self.width() - width))
            row = next((i for i, end in enumerate(ends) if left > end + 3), None)
            if row is not None:
                baseline = self.LABEL_ROWS[row]
                if done:
                    # Confirmed: a solid green badge, readable at a glance; grey-blue
                    # when only the system has paired it (自动对上, not a confirmation).
                    painter.fillRect(left, baseline - metrics.ascent(), width, metrics.height(),
                                     QColor("#5b7a99" if done == AUTO_LABELLED else "#16a34a"))
                    painter.setPen(QColor("#ffffff"))
                    painter.drawText(left + 4, baseline, label)
                else:
                    painter.setPen(QColor("#374151"))
                    painter.drawText(left, baseline, label)
                ends[row] = left + width
        if self.cursor is not None:
            x = self.x_of(self.cursor)
            if 0 <= x < self.width():
                painter.setPen(QPen(QColor("#b400cc"), 2))
                painter.drawLine(x, self.STRIP_TOP - 4, x, bottom + 2)
        self._paint_axis(painter, bottom)

    def _paint_axis(self, painter, bottom):
        step = next((s for s in (1, 2, 5, 10, 30, 60, 120, 300, 600) if s * self.px_per_s >= 90), 600) * 1000
        first = int(self.view_ms // step + 1) * step
        painter.setPen(QColor("#374151"))
        for stamp in range(first, int(self.view_ms + self.span_ms()) + 1, step):
            x = self.x_of(stamp)
            painter.drawLine(x, bottom, x, bottom + 4)
            painter.drawText(x + 2, self.height() - 2, format_chip_time(stamp)[:8])

    def navigate_key(self, event):
        if event.modifiers() & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier):
            return False
        key = event.key()
        if key in (Qt.Key_Plus, Qt.Key_Equal, Qt.Key_Minus):
            self.zoom(-1 if key == Qt.Key_Minus else 1)
        elif self.view_ms is not None and key in (Qt.Key_Left, Qt.Key_Right):
            step = self.span_ms() * (0.9 if event.modifiers() & Qt.ShiftModifier else 0.25)
            self.set_view(self.view_ms + (-step if key == Qt.Key_Left else step))
        elif key in (Qt.Key_Home, Qt.Key_End) and self.extent() is not None:
            self.set_view(self.extent()[0] if key == Qt.Key_Home else self.extent()[1] - self.span_ms())
        else:
            return False
        return True

    def keyPressEvent(self, event):
        if not self.navigate_key(event):
            super().keyPressEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.view_ms is not None:
            self.setFocus(Qt.MouseFocusReason)
            self._press = (event.pos(), self.view_ms, False)

    def mouseMoveEvent(self, event):
        if self._press is None:
            return
        pos, view, dragging = self._press
        moved = event.pos() - pos
        if dragging or moved.manhattanLength() > CLICK_SLOP_PX:
            self._press = (pos, view, True)
            self.set_view(view - moved.x() * self.ms_per_px)

    def mouseReleaseEvent(self, event):
        press, self._press = self._press, None
        if (press is not None and not press[2] and event.button() == Qt.LeftButton
                and (event.pos() - press[0]).manhattanLength() <= CLICK_SLOP_PX):
            self.clicked.emit(self.time_at(event.pos().x()))

    def wheelEvent(self, event):
        if self.view_ms is None:
            return
        delta = event.angleDelta().y() or event.angleDelta().x()
        if not delta:
            return
        if event.modifiers() & Qt.ControlModifier:
            self.zoom(1 if delta > 0 else -1, event.pos().x())
        else:
            self.set_view(self.view_ms - delta / 120 * self.span_ms() / 8)
        event.accept()


class RaceStripPanel(QWidget):
    """全程过线图 with its zoom buttons and scroll bar.

    Reading the strip files' metadata takes about a second for a whole race,
    so it runs off the GUI thread; a growing race rereads only new files.
    """

    seek_requested = pyqtSignal(object)         # jump request, or None where nothing was recorded
    ready = pyqtSignal()                        # the race's pieces are readable now
    _loaded = pyqtSignal(object)

    SCROLL_UNIT_MS = 100

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        column = QVBoxLayout()
        column.setSpacing(0)
        self.view = RaceStripView(self)
        self.scroll = QScrollBar(Qt.Horizontal, self)
        self.scroll.setFocusPolicy(Qt.NoFocus)
        column.addWidget(self.view)
        column.addWidget(self.scroll)
        layout.addLayout(column, 1)
        self.view.view_changed.connect(self._sync_scroll)
        self.view.clicked.connect(lambda chip_ms: self.seek_requested.emit(frame_at(self.view.pieces, chip_ms)))
        self.scroll.valueChanged.connect(self._scrolled)
        self._loaded.connect(self._apply)
        self.workspace, self.camera = None, 1
        self._metas, self._token, self._stamp = {}, 0, None
        self._timer = QTimer(self)
        self._timer.setInterval(RESCAN_MS)
        self._timer.timeout.connect(self._rescan_if_changed)
        self._timer.start()

    def load(self, workspace, camera=1, *, background=True):
        """Show one camera of one race; rereads only what changed since last time."""

        workspace = None if workspace is None else Path(workspace)
        changed = (workspace, int(camera)) != (self.workspace, self.camera)
        if changed:
            self.workspace, self.camera, self._metas = workspace, int(camera), {}
            self.view.cache.clear()
            self.view.cursor = None
            self.view.view_ms = None
            self.view.set_pieces([], "正在读取过线图…" if workspace else "")
        stamp = self._source_stamp()
        if not changed and stamp == self._stamp:
            return                              # the window refreshes much faster than a race scan
        self._stamp = stamp
        self._token += 1
        if workspace is None:
            return
        job = (self._token, workspace, self.camera, dict(self._metas))
        if background:
            threading.Thread(target=self._read, args=job, daemon=True, name="race-strip-index").start()
        else:
            self._read(*job)

    def _read(self, token, workspace, camera, known):
        try:
            result = strip_pieces(workspace, camera, known)
        except (OSError, RuntimeError, ValueError, KeyError, BadZipFile, EOFError) as error:
            result = error
        try:
            self._loaded.emit((token, result))
        except RuntimeError:
            pass                                # the window closed while reading

    def _apply(self, payload):
        token, result = payload
        if token != self._token:
            return
        if isinstance(result, Exception):
            self.view.set_pieces(self.view.pieces, f"过线图读取失败：{result}")
            return
        pieces, self._metas = result
        if not pieces:
            message = "本机位还没有过线图：录像封存并分析后生成。"
        elif not all(piece.aligned for piece in pieces):
            message = "部分录像尚未校时：对应区域暂留灰色，请先校时后回看。"
        else:
            message = ""
        self.view.set_pieces(pieces, message)
        if pieces:
            self.ready.emit()

    def _source_stamp(self):
        if self.workspace is None:
            return None
        from .clock_sync import CLOCK_MODEL_FILENAME
        stamp = []
        for path in (strip_folder(self.workspace, self.camera), self.workspace / CLOCK_MODEL_FILENAME,
                     self.workspace / "video_clock_calibrations.jsonl", self.workspace / "video_timeline.jsonl"):
            try:
                stamp.append(path.stat().st_mtime_ns)
            except OSError:
                stamp.append(None)
        return tuple(stamp)

    def _rescan_if_changed(self):
        if self.workspace is not None and self.isVisible() and self._source_stamp() != self._stamp:
            self.load(self.workspace, self.camera)

    def set_chips(self, chips):
        self.view.set_chips(chips)

    def set_cursor(self, chip_ms, camera=None):
        if camera is not None and int(camera) != self.camera and self.workspace is not None:
            self.load(self.workspace, camera)
        self.view.set_cursor(chip_ms)

    def _sync_scroll(self):
        extent = self.view.extent()
        self.scroll.blockSignals(True)
        if extent is None or self.view.view_ms is None:
            self.scroll.setRange(0, 0)
        else:
            unit = self.SCROLL_UNIT_MS
            self.scroll.setRange(0, max(0, int((extent[1] - extent[0] - self.view.span_ms()) // unit)))
            self.scroll.setPageStep(max(1, int(self.view.span_ms() // unit)))
            self.scroll.setSingleStep(max(1, int(self.view.span_ms() // unit // 8)))
            self.scroll.setValue(int((self.view.view_ms - extent[0]) // unit))
        self.scroll.blockSignals(False)

    def _scrolled(self, value):
        extent = self.view.extent()
        if extent is not None:
            self.view.set_view(extent[0] + value * self.SCROLL_UNIT_MS)
