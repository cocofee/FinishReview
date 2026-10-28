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
from .race_strip import RaceStripPanel, first_crossing, frame_at


# Chips closer together than this are one wave, and the operator counts the
# people of a wave against its chips.  On real races (古浪, 定边, 可克达拉)
# the gaps cluster below 2 s and above 3 s, so the cut falls in the quiet.
WAVE_GAP_MS = 2000
KEEP = "keep"  # landing: the picture already shows this row's wave; leave it
# A wave is looked at from a little before its first chip to a little after its
# last: a rider without a chip may lead it or close it.
WAVE_VIEW_MARGIN_MS = 500
# A wave is swept by dragging the picture; a rider stays in view about a second
# either side of the line, so one decoded frame covers 250 ms either side.
WAVE_VIEW_REACH_MS = 250
# A chip of a confirmed wave that nobody on the picture was counted for
# (older confirmations only; a wave now shows 少 N until someone is counted).
WAVE_NO_PERSON = "wave_no_person"
# People counted up to about three frames before the frame shown are drawn as a
# faded ring, so a rider still on the line is not counted a second time.  Only
# earlier ones and only this close: further off the rider has moved on and the
# ring sits on empty road (古浪).
GHOST_REACH_MS = 150


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
    mark: dict | None = None  # a 多出 row: someone counted beyond the wave's chips


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
    split: int = 0  # people marked 无芯片 and moved onto a 疑似漏读 row of their own

    @property
    def matches(self):
        """Every chip has its person and nobody counted is left over."""
        return not self.extra and not self.unmatched


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
        self._shown_ms = None
        self._landing_pending = None
        # True while a row is opened whose picture the panel itself places (or
        # keeps): the window must not reopen the item's stored frame over it.
        self.holds_picture = False
        super().__init__(parent)
        self.header.hide()
        self.detail_scroll.hide()
        self.timeline.hide()
        self.timeline_help.hide()
        self.table.currentCellChanged.disconnect()
        self.table.cellDoubleClicked.disconnect()
        self.table.cellClicked.connect(self._arrival_clicked)
        self.table.cellDoubleClicked.connect(self._arrival_clicked)
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
        self.race_strip.ready.connect(self._strip_ready)
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

    def _wave_plans(self):
        """Wave index -> its plan, for the waves that have a picture to count on."""
        plans = {}
        for index, wave in enumerate(self._waves):
            item = self._wave_candidate(wave)
            plan = self._wave_plan(item) if item is not None else None
            if plan is not None:
                plans[index] = plan
        self._plan_cache = None
        return plans

    def _with_extra_rows(self, rows, plans):
        """Everyone counted beyond a wave's chips gets a row of their own, at their own time.

        So a 多 1 can be found: the row opens the frame they were counted on,
        where 4 (无芯片), 3 (非运动员) or a right click settles them.
        """
        clocks = workspace_clocks(self.workspace_dir) if self.workspace_dir else None
        extra = []
        for plan in plans.values():
            for index in sorted(plan.extra):
                mark = plan.marks[index]
                crossing = (recorder_crossing_time(mark["recorder_ms"], mark.get("frame") or {}, self.workspace_dir,
                                                   clocks=clocks) if self.workspace_dir else None)
                extra.append(ArrivalRow(
                    f"extra:{plan.item.item_id}:{mark['recorder_ms']}:{mark['x']}:{mark['y']}", plan.item,
                    time_ms=crossing.value_ms if crossing else int(mark["recorder_ms"]),
                    recorder_ms=int(mark["recorder_ms"]), note="多出", mark=mark))
        if not extra:
            return rows
        extra.sort(key=lambda row: row.time_ms)
        merged = []
        for row in rows:
            moment = int(row.event.timeline_timestamp_ms) if row.event is not None else row.time_ms
            while extra and moment is not None and extra[0].time_ms < moment:
                merged.append(extra.pop(0))
            merged.append(row)
        return merged + extra

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
        plans = self._wave_plans()
        rows = self._arrival_rows = self._with_extra_rows(rows, plans)
        self._simultaneous = Counter(row.time_ms for row in rows if row.decision)
        self.rows = [row.item for row in rows]
        groups = [self._row_group(row) for row in rows]
        counted = {index: plan.matches for index, plan in plans.items() if plan.marks or plan.split}
        signature = (self._arrival_scope, self._selected_key, tuple(sorted(counted.items())),
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
                                              "多出": ("#b42318", "#fdecec"),
                                              "疑似漏读": ("#9a3412", "#ffedd5"),
                                              "疑似双芯片": ("#9a3412", "#ffedd5")}[state]
                    cell.setForeground(QColor(foreground))
                    cell.setBackground(QColor(background))
                    cell.setTextAlignment(Qt.AlignCenter)
                tooltip = ("点击回看保存原帧" if row.decision else
                           "点击回看对应时段；已确认有自行车时单击只选择运动员，双击回看；不会自动确认过线")
                if row.mark is not None:
                    tooltip = "比芯片多点出的人：点这一行回到点他的那一帧；没芯片按 4，不是运动员按 3，点错右键取消"
                wave = self._row_wave(row) if row.key in self._wave_of else None
                if column == 0 and wave is not None and len(wave) > 1:
                    # Neighbouring waves alternate shades, so where one ends shows.
                    cell.setBackground(QColor("#e3ebfb" if self._wave_of[row.key] % 2 else "#eee5fb"))
                    tooltip = f"本波 {len(wave)} 个芯片：{wave[0].bib} → {wave[-1].bib}\n" + tooltip
                if column == 0 and self._wave_of.get(row.key) in counted:
                    # The wave's count against its chips: green when they agree, red when not.
                    matches = counted[self._wave_of[row.key]]
                    cell.setForeground(QColor("#126044" if matches else "#b42318"))
                    font = cell.font()
                    font.setBold(True)
                    cell.setFont(font)
                    tooltip = ("本波人数与芯片数一致\n" if matches else "本波人数与芯片数不一致（多人或少人）\n") + tooltip
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

    def _open_row(self, index, keep=True):
        if not 0 <= index < len(self._arrival_rows):
            return
        row = self._arrival_rows[index]
        self._selected_key = row.key
        self._active_item = row.item
        self._shown_id = row.item.item_id
        self.table.setCurrentCell(index, 0)
        self._sync_timeline()
        self._present(row.item, self._landing(row, keep))
        self._sync_arrival_confirmation()
        self.marks_changed.emit(tuple(self.current_marks()))

    def _landing(self, row, keep=True):
        """Where the picture goes when this row is opened; the one rule for every way in.

        A decided row: the frame it was decided on.  A chip row: its wave's first
        rider meeting the line as the strip sees it (the chip time alone lands on
        the wrong rider of a bunch), unless the picture already shows this wave:
        switching riders inside a wave never moves it.  KEEP means stay; None
        means the row's own stored frame or chip reference.
        """
        self._landing_pending = None
        if row.decision and row.decision.get("frame"):
            return dict(row.decision["frame"])
        if row.mark is not None:
            return dict(row.mark.get("frame") or {}) or None
        stamps = [int(chip.event.timeline_timestamp_ms) for chip in self._row_wave(row) or ()
                  if chip.event is not None and chip.event.is_active]
        if not stamps or not self._quick_ready:
            return None
        if not self.race_strip.view.pieces:
            self._landing_pending = row.key  # the strip is still loading: land when it is read
            return None
        first, last = min(stamps), max(stamps)
        if keep and self._shown_ms is not None and first - WAVE_GAP_MS / 2 <= self._shown_ms <= last + WAVE_GAP_MS / 2:
            return KEEP
        pieces, cache = self.race_strip.view.pieces, self.race_strip.view.cache
        moment = first_crossing(pieces, cache, first)
        frame = frame_at(pieces, first if moment is None else moment)
        return dict(frame, reference_only=True) if frame is not None else None

    def _strip_ready(self):
        """The strip was still being read when the row was opened (at start): land it now."""
        key, self._landing_pending = self._landing_pending, None
        row = self._selected_arrival()
        if key is not None and row is not None and row.key == key:
            landing = self._landing(row, keep=False)
            if isinstance(landing, dict):
                self.jump_requested.emit(landing)

    def _present(self, item, landing):
        self.holds_picture = landing is not None
        try:
            self.current_changed.emit(item)
        finally:
            self.holds_picture = False
        if isinstance(landing, dict):
            self.jump_requested.emit(landing)

    def open_wave_start(self):
        """回到团首: the selected row's wave, from its first rider at the line."""
        row = self._selected_arrival()
        landing = self._landing(row, keep=False) if row is not None and not row.decision else None
        if isinstance(landing, dict):
            self.jump_requested.emit(landing)
            return True
        return False

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

    def open_picture(self, item):
        """Make a wave's counting picture the one clicks go onto, without moving the frame shown."""
        if self._active_item is not None and self._active_item.item_id == item.item_id:
            return
        self._active_item = item
        self._shown_id = item.item_id
        self._present(item, KEEP)
        self._sync_arrival_confirmation()

    def select_item(self, item_id):
        # A wave's counting picture is shared by its confirmed rows: selecting the
        # picture must not reopen one of them and pull the frame back to where that
        # person was counted (古浪 261).
        counting = item_id in self._candidate_wave
        index = next((i for i, row in enumerate(self._arrival_rows)
                      if row.item.item_id == item_id and not (counting and (row.decision or row.mark))), None)
        if index is not None:
            self._arrival_clicked(index)

    def _arrival_clicked(self, index, _column=0):
        if not 0 <= index < len(self._arrival_rows):
            return
        self._last_saved_key = None
        row = self._arrival_rows[index]
        if row.decision:
            self._open_row(index)
            # Backspace now takes back this row's person, not the last one clicked.
            self._editing_mark = next((i for i, mark in enumerate(self.current_marks())
                                       if self._decision_holds(row.decision, mark)), -1)
            return
        candidate = self._wave_candidate(self._row_wave(row))
        if candidate is not None:
            # A wave is judged as a whole on its one picture.
            self._selected_key = row.key
            self.table.setCurrentCell(index, 0)
            landing = self._landing(row)
            if self._active_item is None or self._active_item.item_id != candidate.item_id:
                self._active_item = candidate
                self._shown_id = candidate.item_id
                self._present(candidate, landing)
            elif isinstance(landing, dict):
                self.jump_requested.emit(landing)
            if row.mark is not None:
                self._editing_mark = next((i for i, mark in enumerate(self.current_marks()) if mark is row.mark), -1)
                self.hint.setText(self.picture_mark_hint())
            self._sync_arrival_confirmation()
            self.marks_changed.emit(tuple(self.current_marks()))
            return
        # Preserve a deliberately classified picture while assigning identities,
        # but not once everyone on it has a row: the next chip is someone else.
        item = self.current_item()
        people = max(1, sum(not is_excluded(mark) for mark in self.current_marks()))
        if (item is not None and item.item_id not in self._candidate_wave
                and len(self._decided.get(item.item_id, ())) < people):
            category = self._category()
            frame = self._frame_provider()
            circle = item.evidence.get("frame", {}).get("marker_x_normalized") is not None
            if ((circle or category and category.get("action") == "bicycle") and frame
                    and frame.get("frame_index", -1) >= 0):
                self._selected_key = row.key
                self.table.setCurrentCell(index, 0)
                self._sync_arrival_confirmation()
                self.marks_changed.emit(tuple(self.current_marks()))
                self.hint.setText("已选中运动员；停在压线原帧按 1 保存，多人可点圈数车。")
                return
        self._open_row(index)

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

        None when the picture on screen is not a wave's.  The people counted
        here take the wave's chips in table order, one each, in the order they
        cross, whether saved yet or not: a click before someone shifts the bibs
        after it.  A person marked 无芯片 takes none, people beyond the last chip
        are 多 1, 多 2 ...  Only a chip confirmed elsewhere (another picture, or
        without a spot) keeps its bib out of the count.
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
            if row is not None and not self._counted_here(row, item):
                plan.labels[i] = row.bib
                matched.add(row.key)
        free = [row for row in chips if row.decision is None or self._counted_here(row, item)]
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
        plan.split = sum(rider_count(self._marks.get(other.item_id, ())) for other in self.items
                         if other.evidence.get("wave_parent") == item.item_id)
        # People confirmed elsewhere, or without a spot on this picture, count too.
        plan.people = (rider_count(marks) + plan.split
                       + sum(row.decision is not None and row.key not in matched
                             and not self._counted_here(row, item) for row in chips))
        self._plan_cache = (key, plan)
        return plan

    @staticmethod
    def _counted_here(row, item):
        """A chip confirmed by a person clicked on this picture (its spot saved with it)."""
        decision = row.decision
        return bool(decision and item is not None and decision.get("candidate_id") == item.item_id
                    and (decision.get("frame") or {}).get("marker_x_normalized") is not None)

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
        status = ((reason or "点中压线的人即保存 · 3 排除 · 右键取消")
                  + (f" · 已圈 {count}" if count else ""))
        plan = self._wave_plan()
        self.no_chip_button.setVisible(bool(plan and plan.marks))
        wave = plan.chips if plan is not None else self._row_wave(row)
        color = ""
        if plan is not None and (plan.marks or plan.split):
            status, color = self._wave_status(plan)
        elif wave is not None and len(wave) > 1 and any(chip.decision is None for chip in wave):
            status = f"本波 {wave[0].bib}→{wave[-1].bib}（{len(wave)} 个芯片）：点一个人存一个"
        self.confirmation_status.setText(status)
        self.confirmation_status.setStyleSheet(f"color:{color}; font-weight:600;" if color else "")

    def _wave_status(self, plan):
        """The count of a wave against its chips, and its color: green 已核, red 多/少, amber not all seen."""
        chips, people = len(plan.chips), plan.people
        status = f"芯片 {chips} · 已点 {people}"
        if plan.extra:
            status += f" · 多 {len(plan.extra)} 人（没芯片按 4，不是运动员按 3）"
        if plan.unmatched:
            status += f" · 少 {len(plan.unmatched)} 人"
        missing = len(plan.no_chip) + plan.split
        if missing:
            status += f" · 无芯片 {missing}"
        unseen = self._unseen(plan)
        if unseen:
            status += f" · 还差 {sum(right - left for left, right in unseen) / 1000:.1f} 秒没看"
        if not plan.matches:
            return status, "#b42318"
        if unseen:
            return status, "#9a5b12"
        return status + " · 已核", "#126044"

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
        self._shown_ms = stamp
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
        wave = self._wave_for_frame(frame)
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

    def _wave_for_frame(self, frame):
        """The wave a click on FRAME counts for: the one whose chips are nearest in time.

        Not the row selected on the left: after a wave's last rider the operator
        steps on into the next wave without touching the table (古浪: riders of
        1 / 74 / 250 were counted as 多 2 on the wave of 11 and 64).  Someone far
        from every chip is 多 1 on the nearest wave, in a row at their own time.
        """
        selected = self._row_wave(self._selected_arrival())
        crossing = (recorder_crossing_time(frame["recorder_ms"], frame, self.workspace_dir)
                    if frame and frame.get("recorder_ms") is not None and self.workspace_dir else None)
        if crossing is None:
            return selected
        best = None
        for wave in self._waves:
            stamps = [int(row.event.timeline_timestamp_ms) for row in wave]
            key = (max(min(stamps) - crossing.value_ms, crossing.value_ms - max(stamps), 0), wave is not selected)
            if best is None or key < best[0]:
                best = (key, wave)
        return best[1] if best else selected

    def adopt_picture_candidate(self, item, row_key):
        """A new picture counts the wave of the moment clicked (else of the row the operator had chosen)."""
        row = next((entry for entry in self._arrival_rows if entry.key == row_key), None)
        frame = dict(item.evidence.get("frame") or {}) if item is not None else {}
        if frame.get("recorder_ms") is None and item is not None:
            frame["recorder_ms"] = item.evidence.get("recorder_ms")
        wave = self._wave_for_frame(frame) or self._row_wave(row)
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
        if index in plan.extra:
            head = "多出一人：没芯片按 4，不是运动员按 3"
        elif index in plan.no_chip:
            head = "这一位无芯片"
        elif label and not is_excluded(plan.marks[index]):
            head = f"{label} 已保存过线"
        else:
            head = "已点这一位"
        near = self._near_counted(plan, index)
        if near:
            head += f"；{near}，若是同一人请右键取消它"
        return head + "；点错右键取消。"

    def _near_counted(self, plan, index):
        """Someone already counted close by on a nearby frame: likely the same rider clicked twice."""
        if index is None:
            return ""
        mark = plan.marks[index]
        frame = dict(mark.get("frame") or {}, recorder_ms=mark["recorder_ms"])
        near = [i for i, other in enumerate(plan.marks)
                if i != index and not is_excluded(other)
                and not mark_on_frame(other, frame, mark["recorder_ms"])
                and abs(int(other["recorder_ms"]) - int(mark["recorder_ms"])) <= GHOST_REACH_MS
                and (other["x"] - mark["x"]) ** 2 + (other["y"] - mark["y"]) ** 2 <= .08 ** 2]
        if not near:
            return ""
        other = min(near, key=lambda i: abs(int(plan.marks[i]["recorder_ms"]) - int(mark["recorder_ms"])))
        return f"{plan.labels.get(other) or '有人'} 在{self._frame_offset(plan.marks[other], frame)}点过"

    @staticmethod
    def _frame_offset(mark, frame):
        """When a counted person was clicked, seen from the frame shown: 3 帧前, 0.4 秒后."""
        own = mark.get("frame") or {}
        if (own.get("segment_id") and own.get("segment_id") == frame.get("segment_id")
                and own.get("frame_index") is not None and frame.get("frame_index") is not None):
            steps = int(frame["frame_index"]) - int(own["frame_index"])
            return f" {abs(steps)} 帧{'前' if steps > 0 else '后'}"
        delta = int(frame["recorder_ms"]) - int(mark["recorder_ms"])
        return f" {abs(delta) / 1000:.1f} 秒{'前' if delta > 0 else '后'}"

    def picture_marks(self):
        """(owner, mark) for everyone counted on the picture shown.

        On a wave's picture this includes the people marked 无芯片, who were moved
        onto a 疑似漏读 row of their own but are still drawn, and cancelled, here.
        """
        item = self.current_item()
        if item is None:
            return []
        marks = [(item.item_id, mark) for mark in self._marks.get(item.item_id, ())]
        if item.item_id in self._candidate_wave:
            marks += [(other.item_id, mark) for other in self.items
                      if other.evidence.get("wave_parent") == item.item_id
                      for mark in self._marks.get(other.item_id, ())]
        return marks

    def picture_overlays(self, frame):
        """Rings for the people counted, as (x, y, label, confirmed, alert, dot, ghost).

        Someone clicked on a frame up to GHOST_REACH_MS before this one is drawn
        as a faded ring where they were, without a tag (a crowd of tags is only
        noise), so identical kits do not get counted twice while stepping frame
        by frame.
        """
        item = self.current_item()
        if item is None or frame is None:
            return []
        current, camera = int(frame["recorder_ms"]), int(frame.get("camera_index") or 1)
        shown, ghosts = [], []
        for owner, mark in self.picture_marks():
            if owner == item.item_id:
                label = self.picture_mark_label(mark)
                reason = mark.get("exclusion_reason", "").strip() if is_excluded(mark) else ""
                label += f" 已排除：{reason}" if reason and reason != label else ""
                alert = bool(self.picture_mark_warning(mark))
            else:
                label, alert = "无芯片", True
            if mark_on_frame(mark, frame, current):
                shown.append((float(mark["x"]), float(mark["y"]), label, not alert, alert, True, False))
            elif (int((mark.get("frame") or {}).get("camera_index") or camera) == camera
                  and 0 < current - int(mark["recorder_ms"]) <= GHOST_REACH_MS):
                ghosts.append((float(mark["x"]), float(mark["y"]), label, not alert, alert, True, True))
        return ghosts + shown  # the marks of this very frame are drawn on top

    def set_finish_line_status(self, configured):
        self.finish_line_status.setText("" if configured else "终点线未设置：请先在实际画面上画线")
        self.finish_line_status.setStyleSheet("color:#17675b;" if configured else "color:#9a5b12;")

    def _configure_sampling(self):
        self.sampling_preparing.emit()
        super()._configure_sampling()
        self.sampling_configured.emit()

    def _confirm_selected_arrival(self):
        plan = self._wave_plan()
        if plan is not None and plan.marks:
            # Everyone counted is saved already; Enter (or 1) goes on to the next wave.
            if self._save_wave(plan):
                self._next_wave(plan.item)
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
        if plan is not None and plan.marks:
            # Counted on the picture, so saved already.  A 1 out of habit must not
            # jump to the next wave before the rest of this one is counted.
            if self._save_wave(plan):
                self.hint.setText("点人时已自动保存，不用按 1；看完这一波按回车到下一波。")
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

    def add_mark(self, x, y, recorder_ms, frame=None):
        """A click on a person counts them; on a wave's picture it is their chip's crossing at once."""
        before = len(self.current_marks())
        super().add_mark(x, y, recorder_ms, frame)
        plan = self._wave_plan()
        if plan is not None and len(plan.marks) > before:
            self._save_wave(plan)
            self.show_counted(self._editing_mark)
            self.marks_changed.emit(tuple(self.current_marks()))
            self.hint.setText(self.picture_mark_hint())

    def show_counted(self, index):
        """Pick out someone counted on a wave's picture: their row is selected on the left.

        The picture stays where it is; Backspace, 3 and 4 then act on this person.
        """
        plan = self._wave_plan()
        if plan is None or not 0 <= index < len(plan.marks):
            return
        self._editing_mark = index
        row = next((row for i, row in plan.assigned if i == index), None)
        row = row or self._decided_row(plan.marks[index], plan.item)
        mark = plan.marks[index]
        position = next((i for i, other in enumerate(self._arrival_rows)
                         if (other.key == row.key if row is not None else other.mark is mark)), None)
        if position is not None:  # 已排除 has no row; 多出 has its own
            self._selected_key = self._arrival_rows[position].key
            self.table.setCurrentCell(position, 0)
        self._sync_arrival_confirmation()

    @staticmethod
    def _decision_holds(decision, mark):
        """A saved confirmation is this very click: same frame, same spot."""
        frame = decision.get("frame") or {}
        return (frame.get("marker_x_normalized") is not None and frame.get("marker_y_normalized") is not None
                and mark_on_frame(mark, frame, frame.get("recorder_ms"))
                and abs(float(frame["marker_x_normalized"]) - mark["x"]) <= .001
                and abs(float(frame["marker_y_normalized"]) - mark["y"]) <= .001)

    def _save_wave(self, plan):
        """Make what is saved for a wave what its picture now counts.

        Runs on every click, right click, 3 and 4: each person counted is their
        chip's crossing at once, with the frame and spot they were clicked on.
        A chip whose person moved on to another bib (someone was clicked, or
        cancelled, before them) is taken back first and saved again.  Count
        differences never block; the wave shows 多 / 少 in red instead.
        """
        item = plan.item
        if not self._usable(item):
            return False
        events = self._events()
        wanted = {row.key: plan.marks[index] for index, row in plan.assigned}
        scope, binding = self._arrival_scope, candidate_binding(item)
        clocks = workspace_clocks(self.workspace_dir) if self.workspace_dir else None
        changed = []
        self.confirmation_preparing.emit()
        try:
            category = self._category(item)
            if wanted and (category is None or category.get("action") != "bicycle"):
                picture = dict(item.evidence.get("frame") or {})
                picture.setdefault("calibration_version", item.calibration_version)
                self._arrival_store.save(scope, "candidate:" + item.item_id, binding, "bicycle",
                                         candidate_id=item.item_id, frame=picture,
                                         analysis_signature=item.evidence.get("analysis_signature", ""),
                                         calibration_version=item.calibration_version)
            for row in plan.chips:
                if row.decision is not None and not self._counted_here(row, item):
                    continue  # confirmed elsewhere: not this picture's to change
                mark = wanted.get(row.key)
                if row.decision is not None:
                    if mark is not None and self._decision_holds(row.decision, mark):
                        continue
                    self._arrival_store.undo(scope, key=row.key)
                if mark is None:
                    continue
                event = events.get(row.event.event_id)
                if event is None or not event.is_active or event_binding(event) != event_binding(row.event):
                    changed.append(row.bib)
                    continue
                frame = dict(mark.get("frame") or {}, calibration_version=item.calibration_version,
                             marker_x_normalized=mark["x"], marker_y_normalized=mark["y"])
                crossing = recorder_crossing_time(mark["recorder_ms"], frame, self.workspace_dir, clocks=clocks)
                self._arrival_store.save(
                    scope, row.key, event_binding(event), "confirm", event_id=event.event_id, bib=event.bib,
                    candidate_id=item.item_id, candidate_binding=binding, frame=frame,
                    crossing_ms=crossing.value_ms if crossing else None,
                    calibration_version=item.calibration_version,
                )
        except (OSError, RuntimeError, ValueError) as error:
            self._quick_render_signature = None
            self._render()
            self.hint.setText(f"过线未保存：{error}")
            return False
        self._quick_render_signature = None
        self._render()
        if changed:
            self.hint.setText(f"芯片记录已变化（{'、'.join(changed)}），请重新核对本波。")
            return False
        return True

    def _next_wave(self, item):
        """On to the next wave from its start, even if the end of this one was looked at nearly up to it."""
        plan = self._wave_plan(item)
        status = self._wave_status(plan)[0] if plan is not None else ""
        keys = {chip.key for chip in plan.chips} if plan is not None else set()
        positions = [i for i, row in enumerate(self._arrival_rows) if row.key in keys]
        following = next((i for i in range(max(positions) + 1, len(self._arrival_rows))
                          if self._arrival_rows[i].item.evidence.get("wave_parent") != item.item_id),
                         None) if positions else None
        if following is not None:
            self._open_row(following, keep=False)
        self.hint.setText(f"上一波：{status}。" if status else "已保存。")

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
        """Toggle 无芯片 / 非运动员 on the person last clicked on the frame shown; saved at once."""
        item = self.current_item()
        marks = self._marks.get(item.item_id, []) if item is not None else []
        frame = self._frame_provider() or {}
        index = self._editing_mark
        if not (0 <= index < len(marks) and mark_on_frame(marks[index], frame, frame.get("recorder_ms"))):
            self.hint.setText("先点中当前画面上的那个人。")
            return
        mark = marks[index]
        decided = self._decided_row(mark, item)
        if decided is not None and not self._counted_here(decided, item):
            self.hint.setText(f"这一位已在别处确认为 {decided.bib}；要改先按退格撤销。")
            return
        if flag == "no_chip":
            mark["no_chip"] = not mark.get("no_chip")
            mark["exclusion_reason"] = ""
            text = "已标为无芯片：单独列一行疑似漏读，后面的号码顺延。"
        else:
            mark["exclusion_reason"] = "" if mark.get("exclusion_reason") else "非运动员"
            mark.pop("no_chip", None)
            text = ("已排除这一位（非运动员），不计人数，后面的号码顺延。" if mark["exclusion_reason"]
                    else "已恢复计数这一位。")
        self.save_drafts()
        plan = self._wave_plan()
        if plan is not None:
            if mark.get("no_chip"):
                # Their own 疑似漏读 row at their own time; still drawn, and cancelled, here.
                self._split_wave_people(item, [mark], uuid.uuid4().hex)
                self._editing_mark = len(self._marks.get(item.item_id, ())) - 1
                plan = self._wave_plan()
            if plan is not None:
                self._save_wave(plan)
        self._keep_selection()
        self._sync_arrival_confirmation()
        self.marks_changed.emit(tuple(self.current_marks()))
        self.hint.setText(text)

    def _keep_selection(self):
        """A settled 多出 row is gone: select the wave's person being edited instead of row 1."""
        if all(row.key != self._selected_key for row in self._arrival_rows):
            self.show_counted(self._editing_mark)

    def _pointed_mark(self, x=None, y=None):
        """The counted person a right click points at: on this frame first, else a faded one nearby."""
        frame = self._frame_provider()
        if not frame or frame.get("recorder_ms") is None:
            return None
        marks = self.picture_marks()
        shown = [entry for entry in marks if mark_on_frame(entry[1], frame, frame["recorder_ms"])]
        if x is None:
            return shown[-1] if shown else None

        def distance(entry):
            return (entry[1]["x"] - x) ** 2 + (entry[1]["y"] - y) ** 2

        near = [entry for entry in shown if distance(entry) <= .04 ** 2]
        if not near:
            camera = int(frame.get("camera_index") or 1)
            near = [entry for entry in marks if entry not in shown and distance(entry) <= .06 ** 2
                    and int((entry[1].get("frame") or {}).get("camera_index") or camera) == camera
                    and 0 < int(frame["recorder_ms"]) - int(entry[1]["recorder_ms"]) <= GHOST_REACH_MS]
        return min(near, key=distance) if near else None

    def _drop_mark(self, owner, mark):
        """Take one counted person back; the bibs after them shift into line and are saved so."""
        item = self.current_item()
        label = self.picture_mark_label(mark) if owner == item.item_id else "无芯片的人"
        try:
            if owner != item.item_id:
                # A 无芯片 person: their 疑似漏读 row goes with them.
                self._arrival_store.undo(self._arrival_scope, key="candidate:" + owner)
        except (OSError, RuntimeError, ValueError) as error:
            self.hint.setText(f"取消未保存：{error}")
            return
        owned = self._marks.get(owner, [])
        owned[:] = [value for value in owned if value is not mark]
        if owner == item.item_id:
            self._editing_mark = len(owned) - 1
        self.save_drafts()
        plan = self._wave_plan(item)
        if plan is None or not self._save_wave(plan):
            self._quick_render_signature = None
            self._render()
        self._keep_selection()
        self._sync_arrival_confirmation()
        self.marks_changed.emit(tuple(self.current_marks()))
        self.hint.setText(f"已取消 {label}；后面的号码已顺延。")

    def cancel_picture_mark(self, x=None, y=None):
        if self._wave_plan() is not None:
            target = self._pointed_mark(x, y)
            if target is None:
                return False
            self._drop_mark(*target)
            return True
        marks = self.current_marks()
        frame = self._frame_provider()
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
        plan = self._wave_plan()
        row = self._selected_arrival()
        if plan is not None and plan.marks:
            # Backspace takes back one person: the one just clicked (or whose row was just opened).
            index = (self._editing_mark if 0 <= self._editing_mark < len(plan.marks) else
                     next((i for i, mark in enumerate(plan.marks)
                           if row is not None and self._counted_here(row, plan.item)
                           and self._decision_holds(row.decision, mark)), -1))
            if index >= 0:
                self._drop_mark(plan.item.item_id, plan.marks[index])
                return
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
        # One step per picture, from the selected row (not the open picture,
        # which may be a wave's counting picture without a row of its own).
        # A 多出 row is opened by clicking it, never stepped onto.
        first = {}
        for position, row in enumerate(self._arrival_rows):
            if row.mark is None:
                first.setdefault(row.item.item_id, position)
        items = list(first)
        selected = self._selected_arrival()
        if selected is None or selected.mark is None:
            index = (items.index(selected.item.item_id) if selected is not None else -1) + delta
        else:  # from a 多出 row: the picture either side of it
            before = sum(position < self.table.currentRow() for position in first.values())
            index = before if delta > 0 else before - 1
        if 0 <= index < len(items):
            self._arrival_clicked(first[items[index]])

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
