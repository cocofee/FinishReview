"""One chronological time/bib table beside the original recording."""

from __future__ import annotations

import csv
import json
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path
import uuid

from PyQt5.QtCore import QEvent, Qt, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QApplication, QAbstractSpinBox, QComboBox, QHBoxLayout, QLabel, QLineEdit,
    QMenu, QPlainTextEdit, QPushButton, QTextEdit, QVBoxLayout, QWidget,
)

from .arrival_review import (
    ARRIVAL_REVIEWS_FILENAME, ArrivalReviewStore, candidate_binding, event_binding,
)
from .bunch_review import add_viewed, is_excluded, mark_on_frame, rider_count, uncovered_intervals
from .chip_history import format_chip_time
from .crossing_time import item_crossing_time, recorder_crossing_time, workspace_clocks
from .problem_items import (
    CATEGORY_VIDEO, KIND_CHIP_NO_CROSSING, KIND_MANUAL_REVIEW, STATUS_EXCLUDED,
    STATUS_WAITING, ProblemItem, stable_item_id,
)
from .problem_panel import ProblemListPanel
from .race_strip import RaceStripPanel


# Chips closer together than this are one wave, and the operator counts the
# people of a wave against its chips.  On real races (古浪, 定边, 可克达拉)
# the gaps cluster below 2 s and above 3 s, so the cut falls in the quiet.
WAVE_GAP_MS = 2000
# A wave is looked at from a little before its first chip to a little after its
# last: a rider without a chip may lead it or close it.
WAVE_VIEW_MARGIN_MS = 500
# A wave is swept by dragging the picture; a rider stays in view about a second
# either side of the line, so one decoded frame covers 250 ms either side.
WAVE_VIEW_REACH_MS = 250
# A chip of a confirmed wave that nobody on the picture was counted for.
WAVE_NO_PERSON = "wave_no_person"


@dataclass
class ArrivalRow:
    key: str
    item: ProblemItem
    bib: str = ""
    event: object = None
    time_ms: int | None = None
    recorder_ms: int | None = None
    reference: bool = True
    decision: dict | None = None
    stale: bool = False
    note: str = ""


@dataclass
class WavePlan:
    """How the people counted on a wave's picture line up with its chips."""

    item: ProblemItem
    chips: list
    marks: list
    labels: dict = field(default_factory=dict)
    assigned: list = field(default_factory=list)
    extra: list = field(default_factory=list)
    no_chip: list = field(default_factory=list)
    unmatched: list = field(default_factory=list)
    people: int = 0

    @property
    def open_people(self):
        return bool(self.assigned or self.extra or self.no_chip)


class QuickReviewPanel(ProblemListPanel):
    quick_review = True
    confirmation_preparing = pyqtSignal()
    candidate_preparing = pyqtSignal()
    sampling_configured = pyqtSignal()
    sampling_preparing = pyqtSignal()
    # A chip record the operator wants gone (spare chip, misread); the window asks why.
    delete_requested = pyqtSignal(str)

    def __init__(self, parent=None):
        self._quick_ready = False
        self._active_item = None
        self._arrival_rows = []
        self._arrival_store = None
        self._arrival_scope = ("", "")
        self._selected_key = None
        # The ruling just saved, until the operator picks another row; undo may
        # reach it after the automatic step to the next row, and nothing older.
        self._last_saved_key = None
        # Chip rows grouped into waves (see WAVE_GAP_MS), and which wave each
        # row and each counting picture belongs to; rebuilt on every render.
        self._waves = []
        self._wave_of = {}
        self._candidate_wave = {}
        self._decided = {}
        self._plan_cache = None
        self._quick_render_signature = None
        self._simultaneous = Counter()
        self._frame_provider = lambda: None
        super().__init__(parent)
        self.header.hide()
        self.detail_scroll.hide()
        self.timeline.hide()
        self.timeline_help.hide()
        self.table.currentCellChanged.disconnect()
        self.table.cellDoubleClicked.disconnect()
        self.table.cellClicked.connect(self._arrival_clicked)
        self.table.cellDoubleClicked.connect(lambda index, _column: self._open_row(index))
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["顺序", "号码", "时间", "状态", "组别"])
        self.table.horizontalHeaderItem(2).setToolTip(
            "校时后的芯片域时间；悬浮单元格可看录像原时刻"
        )
        for column, width in enumerate((42, 66, 137, 72)):
            self.table.setColumnWidth(column, width)
        # The group column only appears when the race has more than one group.
        self.table.setColumnHidden(4, True)
        self.table.setMinimumWidth(340)
        self.table.setMaximumWidth(440)
        self.table.setToolTip("点击选择芯片记录，双击回看；单人直接按 1 保存，多人可点圈数车")
        # Search sits on top of the table: a bib or a name, Enter jumps to the row.
        column = self.table_column = QWidget(self.splitter)
        column_layout = QVBoxLayout(column)
        column_layout.setContentsMargins(0, 0, 0, 0)
        column_layout.setSpacing(3)
        self.search = QLineEdit(column)
        self.search.setPlaceholderText("搜号码 / 姓名，回车跳到")
        self.search.setClearButtonEnabled(True)
        self.search.returnPressed.connect(self._find)
        column_layout.addWidget(self.search)
        self.splitter.insertWidget(0, column)
        column_layout.addWidget(self.table)
        self.quick_toolbar = QWidget(self)
        bar = QHBoxLayout(self.quick_toolbar)
        bar.setContentsMargins(2, 3, 2, 3)
        self.quick_title = QLabel("到达记录", self.quick_toolbar)
        bar.addWidget(self.quick_title, 1)
        self.bicycle_button = QPushButton("1  运动员", self.quick_toolbar)
        self.bicycle_button.setToolTip("选中芯片记录，在压线原帧按 1 保存；画圈可选，多人时用于数车")
        self.non_rider_button = QPushButton("3  非运动员", self.quick_toolbar)
        self.non_rider_button.setToolTip("按 3 排除当前画面候选（路人、工作人员等）；不会删除芯片记录")
        self.no_chip_button = QPushButton("4  无芯片", self.quick_toolbar)
        self.no_chip_button.setToolTip(
            "点中画面上那个人再按 4：此人没有芯片，后面的号码顺延；确认本波后他单独列一行疑似漏读"
        )
        self.defer_button = QPushButton("暂放", self.quick_toolbar)
        self.confirm_arrival_button = QPushButton("确认过线", self.quick_toolbar)
        self.confirm_arrival_button.setToolTip(
            "先选左侧运动员，再将画面停在其前轮压终点线的一帧，最后点击确认过线"
        )
        self.quick_undo_button = QPushButton("撤销 ⌫", self.quick_toolbar)
        for button in (self.bicycle_button, self.non_rider_button, self.no_chip_button, self.defer_button,
                       self.confirm_arrival_button, self.quick_undo_button):
            bar.addWidget(button)
        self.confirm_arrival_button.hide()
        self.defer_button.hide()
        self.no_chip_button.hide()
        self.quick_undo_button.setToolTip("退格撤销所选这一行的判定；刚判完自动跳到下一行时，撤销刚才那一条")
        self.bicycle_button.clicked.connect(self._confirm_bicycle)
        self.non_rider_button.clicked.connect(self._exclude)
        self.no_chip_button.clicked.connect(self._mark_no_chip)
        self.defer_button.clicked.connect(lambda: self._classify("held"))
        self.confirm_arrival_button.clicked.connect(self._confirm_selected_arrival)
        self.quick_undo_button.clicked.connect(self._undo_arrival)
        more = QPushButton("更多", self.quick_toolbar)
        menu = QMenu(more)
        menu.addAction("分析录像", self._toggle_analysis)
        menu.addAction("导出过线记录", self.export_arrivals)
        menu.addAction("删除选中的芯片记录…", self._request_delete)
        menu.addSeparator()
        menu.addAction("清空试验数据", self.clear_data_requested.emit)
        more.setMenu(menu)
        bar.addWidget(more)
        self.layout().insertWidget(0, self.quick_toolbar)
        # One row: count and status on the left, then line setup and the judging keys.
        self.finish_line_button = QPushButton("终点线 / 模型", self.quick_toolbar)
        self.finish_line_button.setToolTip(
            "绘制本机位终点线，选择本机 YOLOX ONNX 模型并试运行；只分析已封存录像"
        )
        self.finish_line_button.clicked.connect(self._configure_sampling)
        self.finish_line_status = QLabel("终点线未设置：请先在实际画面上画线", self.quick_toolbar)
        self.confirmation_status = QLabel("", self.quick_toolbar)
        self.confirmation_status.setStyleSheet("color:#374151;")
        bar.insertWidget(1, self.confirmation_status, 1)
        bar.insertWidget(2, self.finish_line_status)
        bar.insertWidget(3, self.finish_line_button)
        bar.setStretchFactor(self.quick_title, 0)
        self.race_strip = RaceStripPanel(self)
        self.race_strip.seek_requested.connect(self._seek_race_strip)
        self.layout().insertWidget(1, self.race_strip)
        self._quick_ready = True
        QApplication.instance().installEventFilter(self)
        self._render()

    def eventFilter(self, watched, event):
        if (self._quick_ready and watched is self.race_strip.view and event.type() == QEvent.KeyPress
                and self.isVisible() and self.race_strip.view.navigate_key(event)):
            return True
        if (self._quick_ready and event.type() == QEvent.KeyPress and self.isVisible()
                and isinstance(watched, QWidget) and watched.window() is self.window()
                and not isinstance(watched, (QLineEdit, QAbstractSpinBox, QComboBox, QTextEdit, QPlainTextEdit))
                and self._queue_key(event)):
            return True
        return super().eventFilter(watched, event)

    def set_frame_provider(self, provider):
        self._frame_provider = provider

    def set_workspace(self, workspace_dir):
        if self._quick_ready:
            self.race_strip.load(workspace_dir)
        if Path(workspace_dir) != self.workspace_dir:
            self._active_item = None
            self._selected_key = None
            self._last_saved_key = None
            self._arrival_store = None
            self._arrival_rows = []
            self._quick_render_signature = None
        super().set_workspace(workspace_dir)

    def reset_data(self):
        self._active_item = None
        self._selected_key = None
        self._last_saved_key = None
        self._arrival_store = None
        self._arrival_rows = []
        self._quick_render_signature = None
        super().reset_data()
        self._render()

    def set_scope(self, race_id, stage_id):
        if (str(race_id or ""), str(stage_id or "")) != self._scope:
            self._active_item = None
            self._selected_key = None
            self._last_saved_key = None
            self._quick_render_signature = None
        super().set_scope(race_id, stage_id)

    def current_item(self):
        if not self._quick_ready:
            return super().current_item()
        return self._active_item

    def _show(self, row):
        if not self._quick_ready:
            super()._show(row)

    def _ensure_store(self):
        if self.problem_dir is None:
            return
        path = self.problem_dir / ARRIVAL_REVIEWS_FILENAME
        if self._arrival_store is None or self._arrival_store.path != path:
            self._arrival_store = ArrivalReviewStore(path)
        else:
            self._arrival_store.refresh()

    def _events(self):
        events = tuple(self._events_provider())
        scopes = {(event.race_id, event.stage_id) for event in events}
        self._arrival_scope = self._scope if self._scope[0] else next(iter(scopes)) if len(scopes) == 1 else self._scope
        return {event.event_id: event for event in events
                if (event.race_id, event.stage_id) == self._arrival_scope}

    def _category(self, item=None):
        item = item or self._candidate_item()
        return (self._arrival_store.latest(self._arrival_scope, "candidate:" + item.item_id,
                                           candidate_binding(item))
                if self._arrival_store is not None and item is not None else None)

    def _build_rows(self):
        events = self._events()
        clocks = workspace_clocks(self.workspace_dir) if self.workspace_dir else None
        rows, represented = [], set()
        # Parents hold the candidate picture; their members are individual selectable events.
        items = sorted(self.items, key=lambda item: bool(item.grouped))
        by_id = {item.item_id: item for item in items}
        for raw in items:
            # Chip-only placeholders are rebuilt from the live event provider.
            # The legacy refresh retains the selected item as superseded; that
            # retained placeholder must not shadow the current official event.
            if raw.item_id.startswith("arrival:") and raw.evidence.get("superseded"):
                continue
            item = raw
            if (item.status == STATUS_EXCLUDED and (item.evidence.get("bike") or {}).get("decision") == "exclude"):
                item = replace(item, status="open", status_note="")
            members = list(item.evidence.get("batch_members") or ())
            pairs = [(str(m.get("event_id", "")), str(m.get("bib", ""))) for m in members]
            pairs += list(zip(item.event_ids, item.bibs))
            if item.grouped in by_id:
                item = by_id[item.grouped]
            evidence_frame = item.evidence.get("frame") or {}
            raw_recorder_ms = item.evidence.get("recorder_ms", evidence_frame.get("recorder_ms"))
            if not pairs:
                if raw.grouped:
                    continue
                crossing = item_crossing_time(item, self.workspace_dir, clocks=clocks)
                rows.append(ArrivalRow("candidate:" + item.item_id, item,
                                       time_ms=crossing.value_ms if crossing else None,
                                       recorder_ms=raw_recorder_ms,
                                       reference=crossing is None or crossing.source in ("expected", "chip")))
            for event_id, bib in pairs:
                if not event_id or event_id in represented:
                    continue
                represented.add(event_id)
                event = events.get(event_id)
                if event is not None:
                    bib = event.bib
                crossing = item_crossing_time(item, self.workspace_dir, clocks=clocks)
                rows.append(ArrivalRow("event:" + event_id, item, bib, event,
                                       int(event.timeline_timestamp_ms) if event else
                                       crossing.value_ms if crossing else None,
                                       recorder_ms=raw_recorder_ms))
        for event_id, event in events.items():
            if event_id in represented or not event.is_active:
                continue
            item = ProblemItem("arrival:" + event_id, KIND_CHIP_NO_CROSSING, CATEGORY_VIDEO,
                               "", chip_time_ms=int(event.timeline_timestamp_ms),
                               bibs=(event.bib,), event_ids=(event_id,))
            rows.append(ArrivalRow("event:" + event_id, item, event.bib, event,
                                   int(event.timeline_timestamp_ms)))
        anchors = {row.item.item_id: row.item for row in rows}
        for row in rows:
            if self._arrival_store is not None and row.event is not None:
                previous = self._arrival_store.latest(self._arrival_scope, row.key)
                if previous is not None:
                    anchor = anchors.get(previous.get("candidate_id"))
                    row.stale = (previous["binding"] != event_binding(row.event)
                                 or anchor is None
                                 or previous.get("candidate_binding") != candidate_binding(anchor))
                    if (not row.stale and previous["action"] == "confirm" and row.event.is_active
                            and not row.item.evidence.get("configuration_stale")):
                        row.decision = previous
                        row.item = anchor
                        frame = previous.get("frame") or {}
                        raw_recorder_ms = frame.get("recorder_ms")
                        row.recorder_ms = raw_recorder_ms
                        # New confirmations store the already-calibrated crossing
                        # time; older ones stored the raw recorder time. Convert
                        # only the latter, so a live calibration is never applied
                        # twice.
                        stored_ms = previous["crossing_ms"]
                        if raw_recorder_ms is None or int(stored_ms) == int(raw_recorder_ms):
                            crossing = recorder_crossing_time(
                                stored_ms, frame, self.workspace_dir, clocks=clocks,
                            )
                            row.time_ms = crossing.value_ms if crossing is not None else stored_ms
                        else:
                            row.time_ms = stored_ms
                        row.reference = False
                    elif not row.stale and previous.get("reason") == WAVE_NO_PERSON:
                        row.note = "疑似双芯片"
        assigned = Counter(row.decision["candidate_id"] for row in rows if row.decision)
        counting = set(self._wave_candidates().values())
        rows = [row for row in rows if row.event is not None
                or self._candidate_shown(row.item, assigned, counting)]
        # Official chip order remains stable when a corrected video time is saved.
        chips = sorted((row for row in rows if row.event is not None), key=lambda row: (
            row.event.sequence, int(row.event.timeline_timestamp_ms), row.key))
        # A crossing without a chip is read in arrival order too: it sits at its
        # own time between the chips, never collected after the last finisher.
        others = sorted((row for row in rows if row.event is None), key=lambda row: (
            row.time_ms if row.time_ms is not None else 2**62, row.key))
        merged = []
        for chip in chips:
            while others and others[0].time_ms is not None and others[0].time_ms < chip.event.timeline_timestamp_ms:
                merged.append(others.pop(0))
            merged.append(chip)
        return merged + others

    def _candidate_shown(self, item, assigned, counting):
        """A picture is a row while it shows someone who is not yet a chip's crossing.

        The picture a wave is counted on is never a row of its own: its people
        are the wave's chip rows, and whoever is left over is split onto a row of
        their own when the wave is confirmed.  A split-off person is a row while
        they stand on it; undoing the wave takes them back into it.
        """
        if item.item_id in counting:
            return False
        marks = self._marks.get(item.item_id, ())
        if item.evidence.get("wave_parent"):
            return assigned[item.item_id] < rider_count(marks)
        return assigned[item.item_id] < max(1, len(marks))

    def _wave_candidates(self):
        """Chip event id -> the picture its wave is counted on (kept with the drafts)."""
        return self._arrival_drafts.setdefault("waves", {})

    def _index_waves(self):
        """Chain the chips into waves by their official time; rows keep table order."""
        chips = sorted((row for row in self._arrival_rows if row.event is not None and row.event.is_active),
                       key=lambda row: (int(row.event.timeline_timestamp_ms), row.key))
        waves, previous = [], None
        for row in chips:
            stamp = int(row.event.timeline_timestamp_ms)
            if previous is None or stamp - previous >= WAVE_GAP_MS:
                waves.append([])
            waves[-1].append(row)
            previous = stamp
        order = {row.key: index for index, row in enumerate(self._arrival_rows)}
        self._waves = [sorted(wave, key=lambda row: order[row.key]) for wave in waves]
        self._wave_of = {row.key: index for index, wave in enumerate(self._waves) for row in wave}
        mapping = self._wave_candidates()
        self._candidate_wave = {}
        for index, wave in enumerate(self._waves):
            for row in wave:
                if mapping.get(row.event.event_id):
                    self._candidate_wave.setdefault(mapping[row.event.event_id], index)
        # The picture is redrawn on every decoded frame; look decisions up by picture.
        self._decided = {}
        for row in self._arrival_rows:
            if row.decision:
                self._decided.setdefault(row.decision.get("candidate_id"), []).append(row)
        self._plan_cache = None

    def _row_wave(self, row):
        if row is None:
            return None
        index = self._wave_of.get(row.key, self._candidate_wave.get(row.item.item_id))
        return self._waves[index] if index is not None else None

    def _wave_candidate(self, wave):
        """The picture this wave's people are counted on, if one is still usable."""
        mapping = self._wave_candidates()
        wanted = [mapping[row.event.event_id] for row in wave or () if mapping.get(row.event.event_id)]
        for item_id in dict.fromkeys(wanted):
            item = next((item for item in self.items if item.item_id == item_id), None)
            if (item is not None and item.kind == KIND_MANUAL_REVIEW and item.status != STATUS_WAITING
                    and not item.evidence.get("superseded") and not item.evidence.get("configuration_stale")):
                return item
        return None

    def _row_state(self, row):
        if row.event is not None and not row.event.is_active:
            return "已排除"
        if row.stale:
            return "待定"
        if row.decision:
            return "已确认"
        if row.note:
            return row.note
        if (row.item.status == STATUS_WAITING or row.item.evidence.get("configuration_stale")
                or row.item.evidence.get("superseded")):
            return "待定"
        category = self._category(row.item)
        if category and category["action"] == "bicycle" and row.item.evidence.get("wave_parent"):
            return "疑似漏读"
        return "已排除" if category and category["action"] == "not_rider" else "待定"

    @staticmethod
    def _row_group(row):
        event = row.event
        if event is None:
            return ""
        return (getattr(event, "group_name", "") or "").strip() or str(getattr(event, "group_id", "") or "")

    def _find(self):
        """Jump to the next row whose bib is the text, or whose bib or name holds it."""
        text = self.search.text().strip()
        if not text:
            return
        rows = self._arrival_rows
        exact = [i for i, row in enumerate(rows) if row.bib == text]
        found = exact or [i for i, row in enumerate(rows)
                          if text in row.bib or text in (getattr(row.event, "athlete_name", "") or "")]
        if not found:
            self.hint.setText(f"没有找到 {text}")
            return
        current = self.table.currentRow()
        index = next((i for i in found if i > current), found[0])
        self._arrival_clicked(index)
        self.table.scrollToItem(self.table.item(index, 0))
        # Back on the table, so 1 / 3 / Backspace work at once.
        self.table.setFocus()

    def _request_delete(self):
        row = next((row for row in self._arrival_rows if row.key == self._selected_key), None)
        if row is None or row.event is None:
            self.hint.setText("先在左表选中要删除的芯片记录")
            return
        self.delete_requested.emit(row.event.event_id)

    def _row_category(self, row):
        if row.decision:
            return "运动员"
        category = self._category(row.item)
        return ({"bicycle": "运动员", "not_rider": "非运动员"}.get(category["action"], "待判定")
                if category and not row.stale else "待判定")

    def _render(self):
        if not self._quick_ready:
            return
        try:
            self._ensure_store()
            rows = self._build_rows()
        except (OSError, RuntimeError, ValueError) as error:
            self.hint.setText(f"到达记录读取失败：{error}")
            return
        self._arrival_rows = rows
        self.race_strip.set_chips([(int(row.event.timeline_timestamp_ms), row.bib, row.decision is not None)
                                   for row in rows if row.event is not None and row.event.is_active])
        self._index_waves()
        self._simultaneous = Counter(row.time_ms for row in rows if row.decision)
        self.rows = [row.item for row in rows]
        groups = [self._row_group(row) for row in rows]
        signature = (self._arrival_scope, self._selected_key,
                     tuple((row.key, row.bib, row.time_ms, row.reference, self._row_state(row), self._row_category(row),
                            candidate_binding(row.item), self._wave_of.get(row.key), group)
                           for row, group in zip(rows, groups)))
        if signature == self._quick_render_signature:
            self._sync_arrival_confirmation()
            return
        self._quick_render_signature = signature
        several_groups = len({group for group in groups if group}) > 1
        self.table.setColumnHidden(4, not several_groups)
        # Room for a name like 男子山地精英组 beside the state.
        self.table.setMinimumWidth(450 if several_groups else 340)
        for widget in (self.table, self.table_column):
            widget.setMaximumWidth(540 if several_groups else 440)
        self.table.blockSignals(True)
        scroll = self.table.verticalScrollBar().value()
        self.table.setRowCount(len(rows))
        selected = -1
        chip_number = 0
        for index, row in enumerate(rows):
            state = self._row_state(row)
            time = format_chip_time(row.time_ms) if row.time_ms is not None else "—"
            category = self._row_category(row)
            number = ""
            if row.event is not None:
                chip_number += 1
                if category != "非运动员":
                    number = str(chip_number)
            for column, text in enumerate((number, row.bib or "—", time, state, groups[index])):
                from PyQt5.QtWidgets import QTableWidgetItem
                cell = QTableWidgetItem(text)
                cell.setData(Qt.UserRole, row.key)
                if column == 3:
                    foreground, background = {"已确认": ("#126044", "#e5f4ec"),
                                              "已排除": ("#1d4ed8", "#e6f0ff"),
                                              "待定": ("#b42318", "#fdecec"),
                                              "疑似漏读": ("#9a3412", "#ffedd5"),
                                              "疑似双芯片": ("#9a3412", "#ffedd5")}[state]
                    cell.setForeground(QColor(foreground))
                    cell.setBackground(QColor(background))
                    cell.setTextAlignment(Qt.AlignCenter)
                tooltip = ("点击回看保存原帧" if row.decision else
                           "点击回看对应时段；已确认有自行车时单击只选择运动员，双击回看；不会自动确认过线")
                wave = self._row_wave(row) if row.key in self._wave_of else None
                if column == 0 and wave is not None and len(wave) > 1:
                    # Neighbouring waves alternate shades, so where one ends shows.
                    cell.setBackground(QColor("#e3ebfb" if self._wave_of[row.key] % 2 else "#eee5fb"))
                    tooltip = f"本波 {len(wave)} 个芯片：{wave[0].bib} → {wave[-1].bib}\n" + tooltip
                if (column == 2 and row.recorder_ms is not None and row.time_ms is not None
                        and int(row.recorder_ms) != int(row.time_ms)):
                    tooltip = (f"判读时间（校时后）：{format_chip_time(row.time_ms)}\n"
                               f"录像原时刻：{format_chip_time(row.recorder_ms)}\n" + tooltip)
                cell.setToolTip(tooltip)
                self.table.setItem(index, column, cell)
            if row.key == self._selected_key:
                selected = index
        if selected >= 0:
            self.table.setCurrentCell(selected, 0)
        elif rows:
            self.table.setCurrentCell(0, 0)
        self.table.verticalScrollBar().setValue(scroll)
        self.table.blockSignals(False)
        confirmed = sum(row.decision is not None for row in rows)
        self.quick_title.setText(f"到达记录 {len(rows)} 条 · 已确认 {confirmed} 人")
        self.summary.setText(self.quick_title.text())
        self.summary_changed.emit(self.quick_title.text())
        if self._active_item is None and rows:
            self._open_row(0)
        elif self._active_item is not None:
            fresh = next((row.item for row in rows if row.item.item_id == self._active_item.item_id), None)
            if fresh is None and self._active_item.item_id in self._candidate_wave:
                # A wave's counting picture has no row of its own; it stays open.
                fresh = next((item for item in self.items if item.item_id == self._active_item.item_id), None)
            if fresh is None:
                self._active_item = None
                if rows:
                    self._open_row(0)
                else:
                    self.current_changed.emit(None)
            else:
                changed = candidate_binding(fresh) != candidate_binding(self._active_item)
                self._active_item = fresh
                if changed:
                    self.current_changed.emit(fresh)
        self.bicycle_button.setEnabled(self.current_item() is not None)
        self._sync_arrival_confirmation()

    def _open_row(self, index):
        if not 0 <= index < len(self._arrival_rows):
            return
        row = self._arrival_rows[index]
        self._selected_key = row.key
        self._active_item = row.item
        self._shown_id = row.item.item_id
        self.table.setCurrentCell(index, 0)
        self._sync_timeline()
        self.current_changed.emit(row.item)
        self._sync_arrival_confirmation()
        self.marks_changed.emit(tuple(self.current_marks()))

    def _sync_timeline(self):
        if not self._quick_ready:
            return super()._sync_timeline()
        # The whole-race picture replaces the short group ruler here.  Keep
        # its current moment for N / Shift+N, without loading a second image.
        self.timeline.hide()
        self.timeline_help.hide()

    def _seek_race_strip(self, frame):
        if frame is None:
            self.hint.setText("这一刻没有可用的过线图原录像；灰色空档不代表无人过线。")
            return
        # Browsing the whole race is not confirmation of the row still
        # selected on the left; neither its identity nor its marks change.
        self.jump_requested.emit(dict(frame, reference_only=True))
        self.race_strip.view.setFocus(Qt.MouseFocusReason)
        self.hint.setText("已跳到这一刻（只看，不确认）")

    def select_item(self, item_id):
        self._last_saved_key = None
        index = next((i for i, row in enumerate(self._arrival_rows) if row.item.item_id == item_id), None)
        if index is not None:
            self._open_row(index)

    def _arrival_clicked(self, index, _column=0):
        if not 0 <= index < len(self._arrival_rows):
            return
        self._last_saved_key = None
        row = self._arrival_rows[index]
        if row.decision:
            self._open_row(index)
            self.jump_requested.emit(dict(row.decision["frame"]))
            return
        candidate = self._wave_candidate(self._row_wave(row))
        if candidate is not None:
            # A wave is judged as a whole: picking another of its riders keeps
            # the picture where it is; coming from elsewhere opens the wave's own.
            self._selected_key = row.key
            self.table.setCurrentCell(index, 0)
            if self._active_item is None or self._active_item.item_id != candidate.item_id:
                self._active_item = candidate
                self._shown_id = candidate.item_id
                self.current_changed.emit(candidate)
            self._sync_arrival_confirmation()
            self.marks_changed.emit(tuple(self.current_marks()))
            return
        current = self.current_item()
        if current is not None and current.item_id in self._candidate_wave:
            # Leaving a wave that is being counted for a row outside it.
            self._open_row(index)
            return
        # Preserve a deliberately classified picture while assigning identities,
        # but do not strand a newly received chip on an unrelated/empty picture.
        category = self._category()
        frame = self._frame_provider()
        item = self.current_item()
        circle = bool(item and item.evidence.get("frame", {}).get("marker_x_normalized") is not None)
        if not ((circle or category and category.get("action") == "bicycle") and frame
                and frame.get("frame_index", -1) >= 0):
            self._open_row(index)
            return
        self._selected_key = row.key
        self.table.setCurrentCell(index, 0)
        self._sync_arrival_confirmation()
        self.marks_changed.emit(tuple(self.current_marks()))
        self.hint.setText("已选中运动员；停在压线原帧按 1 保存，多人可点圈数车。")

    def _selected_arrival(self):
        index = self.table.currentRow()
        return self._arrival_rows[index] if 0 <= index < len(self._arrival_rows) else None

    def picture_mark_label(self, mark):
        """Use the saved identity for a circle; an unassigned circle has no count number."""
        if mark.get("exclusion_reason"):
            return "非运动员"
        plan = self._wave_plan()
        index = self._plan_index(plan, mark)
        if index is not None:
            return plan.labels.get(index, "")
        item = self.current_item()
        decided = self._decided_row(mark, item)
        if decided is not None:
            return decided.bib
        row = self._selected_arrival()
        marks = self.current_marks()
        editing = marks[self._editing_mark] if 0 <= self._editing_mark < len(marks) else None
        if ((len(marks) == 1 or mark == editing) and row is not None and row.event is not None
                and row.event.is_active and row.bib and not row.stale and row.decision is None):
            return row.bib
        return mark.get("bib") or "待选号码"

    def picture_mark_warning(self, mark):
        """A person beyond the wave's chips, or one without a chip: drawn in red."""
        plan = self._wave_plan()
        index = self._plan_index(plan, mark)
        return index is not None and (index in plan.extra or index in plan.no_chip)

    @staticmethod
    def _plan_index(plan, mark):
        if plan is None:
            return None
        return next((i for i, value in enumerate(plan.marks) if value is mark),
                    next((i for i, value in enumerate(plan.marks) if value == mark), None))

    def _decided_row(self, mark, item):
        """The chip row a counted person was confirmed as, by the frame and spot saved with it."""
        if item is None:
            return None
        for row in self._decided.get(item.item_id, ()):
            decision = row.decision
            if not mark_on_frame(mark, decision["frame"], decision["frame"].get("recorder_ms")):
                continue
            frame = decision["frame"]
            if frame.get("marker_x_normalized") is None and len(self._marks.get(item.item_id, ())) > 1:
                continue
            if (all(frame.get(key) is not None for key in ("marker_x_normalized", "marker_y_normalized"))
                    and (abs(float(frame["marker_x_normalized"]) - mark["x"]) > .001
                         or abs(float(frame["marker_y_normalized"]) - mark["y"]) > .001)):
                continue
            return row
        return None

    def _wave_plan(self, item=None):
        """Line up the people counted on a wave's picture with the wave's chips.

        None when the picture on screen is not a wave's.  A confirmed person
        keeps the bib saved with them.  The others take the wave's open chips in
        table order, one each, in the order they cross; a person marked 无芯片
        takes none, so the bibs after them shift back into line, and people
        beyond the last chip are 多 1, 多 2 ...
        """
        item = item or self.current_item()
        index = self._candidate_wave.get(item.item_id) if item is not None else None
        if index is None:
            return None
        chips = self._waves[index]
        marks = self._marks.get(item.item_id, [])
        key = (item.item_id, id(chips), len(self.items),
               tuple((mark["recorder_ms"], mark["x"], mark["y"], bool(mark.get("no_chip")),
                      mark.get("exclusion_reason", "")) for mark in marks))
        if self._plan_cache is not None and self._plan_cache[0] == key and self._plan_cache[1].marks is marks:
            return self._plan_cache[1]
        plan = WavePlan(item, chips, marks)
        matched = set()
        for i, mark in enumerate(marks):
            row = None if is_excluded(mark) else self._decided_row(mark, item)
            if row is not None:
                plan.labels[i] = row.bib
                matched.add(row.key)
        free = [row for row in chips if row.decision is None]
        for i in sorted((i for i, mark in enumerate(marks) if i not in plan.labels and not is_excluded(mark)),
                        key=lambda i: (int(marks[i]["recorder_ms"]), i)):
            if marks[i].get("no_chip"):
                plan.no_chip.append(i)
                plan.labels[i] = "无芯片"
            elif free:
                row = free.pop(0)
                plan.assigned.append((i, row))
                plan.labels[i] = row.bib
            else:
                plan.extra.append(i)
                plan.labels[i] = f"多 {len(plan.extra)}"
        plan.unmatched = free
        split = sum(rider_count(self._marks.get(other.item_id, ())) for other in self.items
                    if other.evidence.get("wave_parent") == item.item_id)
        # People confirmed one by one without a spot on this picture count too.
        plan.people = (rider_count(marks) + split
                       + sum(row.decision is not None and row.key not in matched for row in chips))
        self._plan_cache = (key, plan)
        return plan

    def _sync_arrival_confirmation(self):
        row = self._selected_arrival()
        item = self._candidate_item()
        category = self._category()
        frame = self._frame_provider()
        valid_frame = bool(frame and frame.get("frame_index", -1) >= 0
                           and frame.get("position_ms", -1) >= 0
                           and frame.get("segment_id") and frame.get("video_path")
                           and frame.get("recorder_ms") is not None)
        if row is None or row.event is None or not row.event.is_active or not row.bib:
            reason = "可先在画面点圈；确认过线前请选择对应芯片记录"
        elif row.decision is not None:
            reason = "该运动员已确认；修改前请先撤销"
        elif not valid_frame:
            reason = "请从胶卷打开有效原帧"
        elif item is None or not item.evidence.get("frame"):
            reason = "请先按 1 确认当前画面有自行车"
        elif (item.status == STATUS_WAITING or item.evidence.get("configuration_stale")
              or item.evidence.get("superseded")):
            reason = "当前候选证据已失效，请重新打开原帧"
        elif category is None or category.get("action") != "bicycle":
            reason = "请先按 1 确认当前画面有自行车"
        elif row.item.evidence.get("configuration_stale"):
            reason = "所选记录配置已变化，请重新分析"
        else:
            reason = ""
        self.confirm_arrival_button.setEnabled(not reason)
        self.confirm_arrival_button.setToolTip(reason or "保存所选运动员与当前显示的压线原帧；不会修改正式芯片时间")
        count = sum(not mark.get("exclusion_reason") for mark in self.current_marks())
        status = ((reason or "1 保存 · 3 排除 · ⌫ 撤销")
                  + (f" · 已圈 {count}" if count else ""))
        plan = self._wave_plan()
        self.no_chip_button.setVisible(bool(plan and plan.marks))
        wave = plan.chips if plan is not None else self._row_wave(row)
        if plan is not None and plan.marks:
            status = self._wave_status(plan)
        elif wave is not None and len(wave) > 1 and any(chip.decision is None for chip in wave):
            status = f"本波 {wave[0].bib}→{wave[-1].bib}（{len(wave)} 个芯片）：逐个点人"
        self.confirmation_status.setText(status)

    def _wave_status(self, plan):
        chips, people = len(plan.chips), plan.people
        status = f"芯片 {chips} · 已点 {people}"
        if people > chips:
            status += f" · 多 {people - chips} 人（按 4）"
        elif people < chips:
            status += f" · 少 {chips - people} 人"

        unseen = self._unseen(plan)
        if unseen:
            status += f" · 还有 {sum(right - left for left, right in unseen) / 1000:.1f} 秒没看"
        elif plan.open_people:
            status += " · 回车确认"
        return status

    def _unseen(self, plan):
        """The parts of a wave's span, in chip time, that no decoded frame has shown yet."""
        if len(plan.chips) < 2:
            return []
        stamps = [int(row.event.timeline_timestamp_ms) for row in plan.chips]
        camera = str(int((plan.item.evidence.get("frame") or {}).get("camera_index") or 1))
        seen = self._arrival_drafts.get("seen", {}).get(camera, [])
        return uncovered_intervals(min(stamps) - WAVE_VIEW_MARGIN_MS, max(stamps) + WAVE_VIEW_MARGIN_MS,
                                   seen, tolerance=100)

    def record_viewed_frame(self, frame, recorder_ms):
        """Remember, in chip time, what of the recording the operator has looked at."""
        super().record_viewed_frame(frame, recorder_ms)
        self.timeline.current = int(recorder_ms)
        crossing = recorder_crossing_time(recorder_ms, frame, self.workspace_dir)
        if crossing is None or self.workspace_dir is None:
            return
        stamp = crossing.value_ms
        self.race_strip.set_cursor(stamp, frame.get("camera_index") or 1)
        camera = str(int(frame.get("camera_index") or 1))
        seen = self._arrival_drafts.setdefault("seen", {})
        seen[camera] = add_viewed(seen.get(camera, []), stamp, stamp - WAVE_VIEW_REACH_MS,
                                  stamp + WAVE_VIEW_REACH_MS, WAVE_VIEW_REACH_MS)
        self._draft_timer.start()
        if self._wave_plan() is not None:
            self._sync_arrival_confirmation()  # "还有 x 秒没看" counts down on this very frame

    def picture_candidate(self, frame):
        """Which picture a click on the frame counts a person on.

        Returns (picture, refusal): picture None means a new one is needed.  Inside
        a wave every click goes onto the wave's one picture, however long the
        wave lasts, and only on the camera it was started on.
        """
        row = self._selected_arrival()
        wave = self._row_wave(row)
        if wave is not None:
            item = self._wave_candidate(wave)
            camera = int((item.evidence.get("frame") or {}).get("camera_index") or 1) if item else None
            if camera is not None and camera != int(frame["camera_index"]):
                return None, f"本波在机位 {camera} 上点人；换机位会把同一个人数两次。"
            return item, ""
        if row is not None and (row.decision is not None or row.stale):
            return None, "该记录已确认或证据已变化，请先撤销或重新打开原帧。"
        item = self.current_item()
        original = item.evidence.get("frame", {}) if item else {}
        reuse = bool(item and item.kind == KIND_MANUAL_REVIEW and not item.evidence.get("superseded")
                     and not item.evidence.get("configuration_stale")
                     and original.get("camera_index") == frame["camera_index"]
                     and original.get("recorder_ms") is not None
                     and abs(original["recorder_ms"] - frame["recorder_ms"]) <= 10000)
        return (item if reuse else None), ""

    def adopt_picture_candidate(self, item, row_key):
        """A new picture counts the wave of the row the operator had chosen."""
        row = next((entry for entry in self._arrival_rows if entry.key == row_key), None)
        wave = self._row_wave(row)
        if wave is None or item is None:
            return
        mapping = self._wave_candidates()
        for chip in wave:
            mapping[chip.event.event_id] = item.item_id
        self._index_waves()
        self.save_drafts()

    def picture_mark_hint(self):
        """The line shown after a person is counted on a wave's picture; None outside a wave."""
        plan = self._wave_plan()
        if plan is None:
            return None
        index = self._editing_mark if 0 <= self._editing_mark < len(plan.marks) else None
        label = plan.labels.get(index, "")
        if len(plan.chips) == 1 and plan.people == 1 and label:
            return f"已标记 {label} 的压线位置；按 1 保存过线。"
        if index in plan.extra:
            head = f"多出的人：没芯片的按 4"
        else:
            head = f"这一位标为 {label}" if label else "已点这一位"
        return f"{head}；回车确认，右键取消。"

    def set_finish_line_status(self, configured):
        self.finish_line_status.setText("" if configured else "终点线未设置：请先在实际画面上画线")
        self.finish_line_status.setStyleSheet("color:#17675b;" if configured else "color:#9a5b12;")

    def _configure_sampling(self):
        self.sampling_preparing.emit()
        super()._configure_sampling()
        self.sampling_configured.emit()

    def _confirm_selected_arrival(self):
        plan = self._wave_plan()
        if plan is not None and plan.open_people:
            self._confirm_wave(plan)
            return
        row = self._selected_arrival()
        if row is None:
            self.hint.setText("请先从左侧选择要确认的运动员。")
            return
        self._confirm_arrival(row)

    def _usable(self, item=None):
        item = item or self._candidate_item()
        if item is None or self._arrival_store is None:
            return False
        if item.status == STATUS_WAITING or item.evidence.get("configuration_stale") or item.evidence.get("superseded"):
            self.hint.setText("证据已变化，等待重新分析后再确认。")
            return False
        return True

    def _candidate_item(self):
        """Return the video candidate, not a synthetic chip-only table row."""
        item = self.current_item()
        if item is not None and item.evidence.get("frame"):
            return item
        return next((row.item for row in self._arrival_rows if row.item.evidence.get("frame")), item)

    def _confirm_bicycle(self):
        plan = self._wave_plan()
        if plan is not None and plan.open_people:
            # Counted on the picture: 1 saves what was counted, the same as Enter.
            self._confirm_wave(plan)
            return
        row = self._selected_arrival()
        if row is not None and row.decision:
            self.hint.setText(f"{row.bib} 已确认；修改前请先撤销。")
            return
        self._shortcut_operation = uuid.uuid4().hex
        try:
            self._classify("bicycle")
            if self.confirm_arrival_button.isEnabled():
                self._confirm_selected_arrival()
            else:
                self.hint.setText("已标记运动员；请选择对应芯片记录，再按 1 保存。")
        finally:
            self._shortcut_operation = None

    def _classify(self, action):
        if action != "held":
            self.candidate_preparing.emit()
        if not self._usable():
            return
        item = self._candidate_item()
        frame = self._frame_provider()
        # A classification belongs to the candidate currently on screen.  Do not
        # let a stale/partial provider result create an audit entry that cannot be
        # reopened later (``held`` is the sole action that may be frame-less).
        if action != "held" and (not frame or frame.get("frame_index", -1) < 0
                                  or frame.get("position_ms", -1) < 0
                                  or not frame.get("segment_id")
                                  or not frame.get("video_path")):
            self.hint.setText("请先打开当前候选的有效录像画面。")
            return
        if action == "not_rider" and any(row.decision and row.decision.get("candidate_id") == item.item_id
                                         for row in self._arrival_rows):
            self.hint.setText("本处已有选手确认，请先撤销误确认。")
            return
        try:
            frame = dict(frame or {})
            frame.setdefault("calibration_version", item.calibration_version)
            self._arrival_store.save(self._arrival_scope, "candidate:" + item.item_id,
                                     candidate_binding(item), action, candidate_id=item.item_id,
                                     frame=frame,
                                     analysis_signature=item.evidence.get("analysis_signature", ""),
                                     operation_id=getattr(self, "_shortcut_operation", None),
                                     calibration_version=item.calibration_version)
        except (OSError, RuntimeError, ValueError) as error:
            self.hint.setText(f"审核未保存：{error}")
            return
        self._render()
        self.hint.setText("选择芯片，停在压线原帧按 1 保存；多人可点圈数车（F 可放大）。" if action == "bicycle"
                          else "已暂放。" if action == "held" else "已排除此画面候选。")
        if action in ("held", "not_rider"):
            self._step(1)
        self._last_saved_key = "candidate:" + item.item_id

    def _confirm_arrival(self, row):
        if row.decision:
            self.hint.setText("该运动员已确认；若要修改，请先撤销误确认，再重新定位压线帧。")
            return
        item = self._candidate_item()
        category = self._category()
        if category is None or category.get("action") != "bicycle":
            self.hint.setText("请先确认画面中有自行车，再定位压线帧并确认运动员。")
            return
        if not self._usable(item):
            return
        events = self._events()
        event = events.get(row.event.event_id) if row.event else None
        if event is None or not event.is_active or not row.bib:
            self.hint.setText("没有明确的有效芯片记录，保留待匹配。")
            return
        if row.event is not None and event_binding(event) != event_binding(row.event):
            self._render()
            self.hint.setText("芯片记录已变化，请重新核对。")
            return
        if row.item.evidence.get("configuration_stale"):
            self.hint.setText("证据配置已变化，请重新分析。")
            return
        self.confirmation_preparing.emit()
        frame = self._frame_provider()
        if frame is None:
            self.hint.setText("请先打开有效录像，停在该选手前轮压线的原帧。")
            return
        frame = dict(frame, calibration_version=item.calibration_version)
        from .bunch_review import mark_on_frame
        marks = self.current_marks()
        mark = marks[self._editing_mark] if 0 <= self._editing_mark < len(marks) else None
        if mark and mark_on_frame(mark, frame, frame.get("recorder_ms")) and not mark.get("exclusion_reason"):
            frame.update(marker_x_normalized=mark["x"], marker_y_normalized=mark["y"])
        crossing = recorder_crossing_time(frame.get("recorder_ms"), frame, self.workspace_dir)
        try:
            self._arrival_store.save(
                self._arrival_scope, row.key, event_binding(event), "confirm", event_id=event.event_id,
                bib=event.bib, candidate_id=item.item_id, candidate_binding=candidate_binding(item),
                frame=frame, crossing_ms=crossing.value_ms if crossing else None,
                operation_id=getattr(self, "_shortcut_operation", None),
                calibration_version=item.calibration_version,
            )
        except (OSError, RuntimeError, ValueError) as error:
            self.hint.setText(f"过线未保存：{error}")
            return
        self._selected_key = row.key
        self._last_saved_key = row.key
        self._render()
        self.marks_changed.emit(tuple(self.current_marks()))
        self.hint.setText(f"已确认 {event.bib} 过线；继续看下一位。")

    def _confirm_wave(self, plan):
        """Enter on a counted wave: every counted person becomes their chip's crossing at once.

        Each person keeps the frame they were clicked on.  Whoever is left over,
        or was marked 无芯片, gets a row of their own (疑似漏读) at their own
        time, and a chip nobody was counted for is kept as 疑似双芯片.  Count
        differences never block this; not having looked at the whole wave does.
        """
        item = plan.item
        if not self._usable(item):
            return
        unseen = self._unseen(plan)
        if unseen:
            left, right = unseen[0]
            self.hint.setText(f"本波还没看完：{format_chip_time(left)} 到 {format_chip_time(right)} 没看到；"
                              "拖动画面看完整波再确认。")
            return
        events = self._events()
        changed = [row.bib for _index, row in plan.assigned
                   if events.get(row.event.event_id) is None
                   or event_binding(events[row.event.event_id]) != event_binding(row.event)]
        if changed:
            self._render()
            self.hint.setText(f"芯片记录已变化（{'、'.join(changed)}），请重新核对本波。")
            return
        self.confirmation_preparing.emit()
        wave = uuid.uuid4().hex
        scope, binding = self._arrival_scope, candidate_binding(item)
        clocks = workspace_clocks(self.workspace_dir) if self.workspace_dir else None
        split = []
        try:
            picture = dict(item.evidence.get("frame") or {})
            picture.setdefault("calibration_version", item.calibration_version)
            self._arrival_store.save(scope, "candidate:" + item.item_id, binding, "bicycle",
                                     candidate_id=item.item_id, frame=picture, operation_id=wave, wave=wave,
                                     analysis_signature=item.evidence.get("analysis_signature", ""),
                                     calibration_version=item.calibration_version)
            for index, row in plan.assigned:
                mark, event = plan.marks[index], events[row.event.event_id]
                frame = dict(mark.get("frame") or {}, calibration_version=item.calibration_version,
                             marker_x_normalized=mark["x"], marker_y_normalized=mark["y"])
                crossing = recorder_crossing_time(mark["recorder_ms"], frame, self.workspace_dir, clocks=clocks)
                self._arrival_store.save(
                    scope, row.key, event_binding(event), "confirm", event_id=event.event_id, bib=event.bib,
                    candidate_id=item.item_id, candidate_binding=binding, frame=frame,
                    crossing_ms=crossing.value_ms if crossing else None, operation_id=wave, wave=wave,
                    calibration_version=item.calibration_version,
                )
            for row in plan.unmatched:
                self._arrival_store.save(scope, row.key, event_binding(row.event), "held",
                                         event_id=row.event.event_id, bib=row.bib, candidate_id=item.item_id,
                                         candidate_binding=binding, reason=WAVE_NO_PERSON, wave=wave)
            people = [plan.marks[index] for index in plan.extra + plan.no_chip]
            if people:
                split = self._split_wave_people(item, people, wave)
        except (OSError, RuntimeError, ValueError) as error:
            self._render()
            self.hint.setText(f"本波未完整保存：{error}")
            return
        self._render()
        keys = {chip.key for chip in plan.chips}
        positions = [i for i, row in enumerate(self._arrival_rows) if row.key in keys]
        confirmed = sum(self._arrival_rows[i].decision is not None for i in positions)
        following = next((i for i in range(max(positions) + 1, len(self._arrival_rows))
                          if self._arrival_rows[i].item.evidence.get("wave_parent") != item.item_id),
                         None) if positions else None
        if following is not None:
            self._open_row(following)
        self._last_saved_key = f"wave:{wave}:{item.item_id}"
        parts = [f"本波 {len(plan.chips)} 个芯片、{plan.people} 人：已确认 {confirmed} 人"]
        if split:
            parts.append(f"{len(split)} 人没有芯片，已单独列为疑似漏读")
        if plan.unmatched:
            parts.append(f"{'、'.join(row.bib for row in plan.unmatched)} 没点到人，标为疑似双芯片")
        self.hint.setText("；".join(parts) + "。退格可撤销本波。")

    def _split_wave_people(self, item, people, wave):
        """Give each counted person without a chip a picture, and so a row, of their own."""
        path = self.problem_dir / "manual_problems.json"
        existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        known = {value["item_id"] for value in existing}
        children = []
        for mark in people:
            frame = dict(mark.get("frame") or {}, marker_x_normalized=mark["x"], marker_y_normalized=mark["y"])
            child = ProblemItem(stable_item_id("manual", item.item_id, mark["recorder_ms"], mark["x"], mark["y"]),
                                KIND_MANUAL_REVIEW, CATEGORY_VIDEO, "本波点出的人没有对应芯片，疑似漏读；芯片状态待核实",
                                evidence={"frame": frame, "recorder_ms": int(mark["recorder_ms"]),
                                          "operator_scope": self._scope, "wave_parent": item.item_id})
            if child.item_id not in known:
                existing.append(child.to_payload())
                known.add(child.item_id)
            children.append((child.item_id, mark))
        self.problem_dir.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(existing, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)
        owned = self._marks.setdefault(item.item_id, [])
        for child_id, mark in children:
            owned[:] = [value for value in owned if value is not mark]
            self._marks[child_id] = [dict(mark, wave_operation=wave)]
        self.save_drafts()
        self.refresh()
        for child_id, _mark in children:
            child = next((value for value in self.items if value.item_id == child_id), None)
            if child is not None:
                self._arrival_store.save(self._arrival_scope, "candidate:" + child_id, candidate_binding(child),
                                         "bicycle", candidate_id=child_id, frame=dict(child.evidence["frame"]),
                                         wave=wave, calibration_version=child.calibration_version)
        return children

    def _undo_wave(self, wave, candidate_id):
        """Backspace right after a wave confirm takes the whole wave back, its people included."""
        self._last_saved_key = None
        try:
            def confirms():
                return sum(entry["action"] == "confirm" for entry in self._arrival_store.active(self._arrival_scope))
            before = confirms()
            self._arrival_store.undo(self._arrival_scope, wave=wave)
            undone = before - confirms()
        except (OSError, RuntimeError, ValueError) as error:
            self.hint.setText(f"撤销未保存：{error}")
            return
        home = self._marks.setdefault(candidate_id, [])
        for item_id, marks in list(self._marks.items()):
            moved = [mark for mark in marks if mark.get("wave_operation") == wave]
            if moved and item_id != candidate_id:
                self._marks[item_id] = [mark for mark in marks if mark.get("wave_operation") != wave]
                home.extend({key: value for key, value in mark.items() if key != "wave_operation"}
                            for mark in moved)
        self.save_drafts()
        self._quick_render_signature = None
        self._render()
        candidate = next((item for item in self.items if item.item_id == candidate_id), None)
        index = self._candidate_wave.get(candidate_id)
        if candidate is not None and index is not None:
            first = self._waves[index][0].key
            self._selected_key = first
            self._active_item = candidate
            self._shown_id = candidate.item_id
            self.table.setCurrentCell(next(i for i, row in enumerate(self._arrival_rows) if row.key == first), 0)
            self.current_changed.emit(candidate)
            self._sync_arrival_confirmation()
            self.marks_changed.emit(tuple(self.current_marks()))
        self.hint.setText(f"已撤销本波确认（{undone} 人）；可接着点人，或回车重新确认。")

    def _exclude(self):
        """3 excludes what is pointed at: on a wave's picture the person clicked, else the picture."""
        plan = self._wave_plan()
        if plan is not None and plan.marks:
            self._flag_mark("exclusion_reason")
            return
        self._classify("not_rider")

    def _mark_no_chip(self):
        plan = self._wave_plan()
        if plan is None or not plan.marks:
            self.hint.setText("先在本波画面上点出这个人，再按 4 标为无芯片。")
            return
        self._flag_mark("no_chip")

    def _flag_mark(self, flag):
        """Toggle 无芯片 / 非运动员 on the person last clicked on the frame shown."""
        item = self.current_item()
        marks = self._marks.get(item.item_id, []) if item is not None else []
        frame = self._frame_provider() or {}
        index = self._editing_mark
        if not (0 <= index < len(marks) and mark_on_frame(marks[index], frame, frame.get("recorder_ms"))):
            self.hint.setText("先点中当前画面上的那个人。")
            return
        mark = marks[index]
        decided = self._decided_row(mark, item)
        if decided is not None:
            self.hint.setText(f"这一位已确认为 {decided.bib}；要改先按退格撤销。")
            return
        if flag == "no_chip":
            mark["no_chip"] = not mark.get("no_chip")
            mark["exclusion_reason"] = ""
            text = ("已标为无芯片：后面的号码顺延，确认本波后此人单独列一行疑似漏读。"
                    if mark["no_chip"] else "已取消无芯片。")
        else:
            mark["exclusion_reason"] = "" if mark.get("exclusion_reason") else "非运动员"
            mark.pop("no_chip", None)
            text = "已排除这一位（非运动员），不计人数。" if mark["exclusion_reason"] else "已恢复计数这一位。"
        self.save_drafts()
        self._sync_arrival_confirmation()
        self.marks_changed.emit(tuple(self.current_marks()))
        self.hint.setText(text)

    def cancel_picture_mark(self, x=None, y=None):
        marks = self.current_marks()
        frame = self._frame_provider()
        from .bunch_review import mark_on_frame
        candidates = [i for i, mark in enumerate(marks)
                      if frame and mark_on_frame(mark, frame, frame.get("recorder_ms"))]
        if x is not None:
            candidates = [i for i in candidates
                          if (marks[i]["x"] - x) ** 2 + (marks[i]["y"] - y) ** 2 <= .04 ** 2]
        index = (min(candidates, key=lambda i: (marks[i]["x"] - x) ** 2 + (marks[i]["y"] - y) ** 2)
                 if candidates and x is not None else candidates[-1] if candidates else None)
        if index is None:
            return False
        mark = marks[index]
        item = self.current_item()
        for row_index, row in enumerate(self._arrival_rows):
            saved = (row.decision or {}).get("frame", {})
            if (row.decision and item and row.decision.get("candidate_id") == item.item_id
                    and mark_on_frame(mark, saved, saved.get("recorder_ms"))
                    and abs(saved.get("marker_x_normalized", -1) - mark["x"]) <= .001
                    and abs(saved.get("marker_y_normalized", -1) - mark["y"]) <= .001):
                self.table.setCurrentCell(row_index, 0)
                self._undo_key(row.key)
                break
        # Undo may have re-rendered the table and moved the active item; remove
        # the circle from the item it was drawn on, not from whatever is current.
        owned = self._marks.get(item.item_id, []) if item is not None else []
        if 0 <= index < len(owned):
            owned.pop(index)
            self._editing_mark = min(index, len(owned) - 1)
            self._marks_updated()
        self._sync_arrival_confirmation()
        self.marks_changed.emit(tuple(self.current_marks()))
        self.hint.setText("已取消这个待选标记；左键可重新点选，右键取消。")
        return True

    def _undo_arrival(self):
        if self._last_saved_key and self._last_saved_key.startswith("wave:"):
            _prefix, wave, candidate_id = self._last_saved_key.split(":", 2)
            self._undo_wave(wave, candidate_id)
            return
        row = self._selected_arrival()
        if (row is None or not row.decision) and self.cancel_picture_mark():
            return
        if self._arrival_store is None:
            return
        # Undo once reached the newest ruling anywhere, so a press on an
        # undecided row reopened a rider confirmed long before.  Now it takes
        # the ruling just made, else the one the selected row shows.
        self._undo_key(self._last_saved_key or self._row_ruling_key(self._selected_arrival()))

    def _row_ruling_key(self, row):
        """The saved ruling a row shows: its own confirmation, else its picture's category."""
        if row is None:
            return None
        return next((key for key in (row.key, "candidate:" + row.item.item_id)
                     if self._arrival_store.latest(self._arrival_scope, key) is not None), None)

    def _undo_key(self, target_key):
        if self._arrival_store is None:
            return
        self._last_saved_key = None
        try:
            before = len(self._arrival_store.active(self._arrival_scope))
            previous = (self._arrival_store.undo(self._arrival_scope, key=target_key)
                        if target_key is not None else None)
            # A picture category that other confirmations still rely on stays.
            if previous is not None and len(self._arrival_store.active(self._arrival_scope)) == before:
                previous = None
        except (OSError, RuntimeError, ValueError) as error:
            self.hint.setText(f"撤销未保存：{error}")
            return
        self._render()
        if previous:
            candidate_id = previous.get("candidate_id")
            candidate = next((item for item in self.items if item.item_id == candidate_id), None)
            index = next((i for i, row in enumerate(self._arrival_rows)
                          if row.key == previous.get("key")), None)
            if index is None:
                index = next((i for i, row in enumerate(self._arrival_rows)
                              if row.item.item_id == candidate_id), None)
            if index is not None:
                row = self._arrival_rows[index]
                self._selected_key = row.key
                self._active_item = candidate or row.item
                self._shown_id = self._active_item.item_id
                self.table.setCurrentCell(index, 0)
            if previous.get("frame"):
                current = self._frame_provider()
                saved = previous["frame"]
                from .bunch_review import mark_on_frame
                self._editing_mark = next((i for i, mark in enumerate(self.current_marks())
                                           if saved.get("marker_x_normalized") is not None
                                           and saved.get("marker_y_normalized") is not None
                                           and abs(mark["x"] - saved["marker_x_normalized"]) <= .001
                                           and abs(mark["y"] - saved["marker_y_normalized"]) <= .001
                                           and mark_on_frame(mark, saved, saved.get("recorder_ms"))), -1)
                same_frame = bool(current and current.get("camera_index") == saved.get("camera_index")
                                  and current.get("segment_id") == saved.get("segment_id")
                                  and current.get("frame_index") == saved.get("frame_index"))
                if not same_frame:
                    self.jump_requested.emit(dict(saved))
            self._sync_arrival_confirmation()
            self.marks_changed.emit(tuple(self.current_marks()))
        undone = (None if previous is None
                  else f" {previous.get('bib')} 号确认" if previous["action"] == "confirm"
                  else {"not_rider": "排除", "held": "暂放"}.get(previous["action"], "运动员标记"))
        self.hint.setText(f"已撤销{undone}；可直接按 1 或 3 重判。" if undone
                          else "这一行没有可撤销的判定。")

    def _step(self, delta):
        items = list(dict.fromkeys(row.item.item_id for row in self._arrival_rows))
        current = self.current_item()
        index = items.index(current.item_id) if current is not None and current.item_id in items else -1
        if 0 <= index + delta < len(items):
            self.select_item(items[index + delta])

    def _queue_key(self, event):
        if event.isAutoRepeat():
            return event.key() in (Qt.Key_1, Qt.Key_3, Qt.Key_4, Qt.Key_Backspace)
        if event.modifiers() == Qt.ControlModifier and event.key() == Qt.Key_Z:
            self._undo_arrival()
            return True
        if event.modifiers() & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier):
            return False
        # The picture is already as large as the workbench allows: F does nothing.
        # Esc still leaves an enlarged view, however it was entered.
        if event.key() == Qt.Key_Escape and self._big_view:
            self.toggle_big_view()
            return True
        if event.key() in (Qt.Key_Left, Qt.Key_Right):
            step = 5 if event.modifiers() & Qt.ShiftModifier else 1
            self.frame_step_requested.emit(-step if event.key() == Qt.Key_Left else step)
            return True
        # 3 excludes; 4 marks a counted person 无芯片.  Undo is only Backspace / Ctrl+Z.
        actions = {Qt.Key_1: self._confirm_bicycle, Qt.Key_3: self._exclude,
                   Qt.Key_4: self._mark_no_chip, Qt.Key_Backspace: self._undo_arrival,
                   Qt.Key_Return: self._confirm_selected_arrival, Qt.Key_Enter: self._confirm_selected_arrival,
                   Qt.Key_Up: lambda: self._step(-1), Qt.Key_Down: lambda: self._step(1)}
        if event.key() in actions:
            actions[event.key()]()
            return True
        return False

    def set_browsing(self, browsing):
        self._browsing = bool(browsing)

    def toggle_big_view(self):
        """Expand the evidence area while keeping the arrival list and actions visible."""

        self._big_view = not self._big_view
        self._sync_step_buttons()
        self._sync_arrival_confirmation()
        self.count_mode_changed.emit(self._big_view)
        if self._big_view:
            self.hint.setText("选择芯片，停在压线原帧按 1 保存；多人可点圈数车。3 排除，退格撤销。Esc 返回。")
            self.setFocus()
        else:
            self.hint.setText("已返回胶卷视图；单人直接按 1 保存，多人可点圈数车。3 排除，退格撤销。")
            self.table.setFocus()
        self.big_view_changed.emit(self._big_view)

    def export_arrivals(self):
        if self.problem_dir is None:
            return None
        self.refresh()
        path = self.problem_dir / "exports" / "人工过线记录.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8-sig", newline="") as output:
            writer = csv.writer(output)
            writer.writerow(["顺序", "号码", "过线时间", "事件", "机位", "录像", "帧号", "校时版本"])
            for index, row in enumerate(row for row in self._arrival_rows if row.decision):
                frame = row.decision["frame"]
                writer.writerow([index + 1, row.bib, format_chip_time(row.time_ms), row.event.event_id,
                                 frame.get("camera_index", ""), frame.get("video_path", ""),
                                 frame.get("frame_index", ""),
                                 row.decision["calibration_version"]])
        self.hint.setText(f"已导出：{path}")
        return path
