"""One chronological time/bib table beside the original recording."""

from __future__ import annotations

import csv
import json
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path
import uuid

from PyQt5.QtCore import QEvent, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QApplication, QAbstractSpinBox, QComboBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QMenu, QPlainTextEdit, QPushButton, QTextEdit, QVBoxLayout, QWidget,
)

from .arrival_review import (
    ARRIVAL_REVIEWS_FILENAME, ArrivalReviewStore, candidate_binding, event_binding,
)
from .bunch_review import add_viewed, is_excluded, mark_on_frame, rider_count, uncovered_intervals
from .chip_history import format_chip_time
from .crossing_time import item_crossing_time, recorder_crossing_time, workspace_clocks
from .lap_wave import INTERMEDIATE, Passage, lap_role, short_riders
from .problem_items import (
    CATEGORY_VIDEO, KIND_CHIP_NO_CROSSING, KIND_MANUAL_REVIEW, KIND_NO_CHIP_CROSSING, STATUS_EXCLUDED,
    STATUS_WAITING, ProblemItem, stable_item_id,
)
from .problem_panel import ProblemListPanel
from .problem_resolutions import _frame_text, _passage_number, export_lists
from .race_strip import RaceStripPanel, earlier_arrival, first_crossing, frame_at


# Chips closer together than this are one wave, and the operator counts the
# people of a wave against its chips.  On real races (古浪, 定边, 可克达拉)
# the gaps cluster below 2 s and above 3 s, so the cut falls in the quiet.
WAVE_GAP_MS = 2000
KEEP = "keep"  # landing: the picture already shows this row's wave; leave it
# A wave is looked at from a little before its first chip to a little after its
# last: a rider without a chip may lead it or close it.
WAVE_VIEW_MARGIN_MS = 500
# A crossing without a chip is looked at from this much before it to this much
# after its last piece.  Half a second each side was too much (10-30: the
# runners were still metres off at the start, and with many crossings the
# extra second each added up).
PASSAGE_VIEW_MARGIN_MS = 250
# Crossings without a chip read for 可能多人 per pass, so the table never waits on the strip.
EARLIER_BATCH = 8
# A wave is swept by dragging the picture; a rider stays in view about a second
# either side of the line, so one decoded frame covers 250 ms either side.
WAVE_VIEW_REACH_MS = 250
# One passage the detector broke into pieces (古浪, 10-28: 7 pieces in 1.1 s)
# is one row: pieces without a chip within a second of the first, never
# chained on from a later piece, so two riders a few seconds apart stay apart.
FRAGMENT_REACH_MS = 1000
# 3 on a row not yet seen whole plays the rest frame by frame (only single steps
# go on into the next recording file) and excludes it once seen; a refused 3
# looked like a dead key (10-28).  Stops if no frame comes.
REVIEW_STEP_FRAMES = 1
REVIEW_STEP_DELAY_MS = 15
REVIEW_STALL_MS = 3000
# Shift+3 excludes this row and the undecided pictures after it, up to a chip or
# a gap this long: someone walking about at the line (特奥测试, 10-28).
EXCLUSION_RUN_GAP_MS = 10_000
# A chip of a confirmed wave that nobody on the picture was counted for
# (older confirmations only; a wave now shows 少 N until someone is counted).
WAVE_NO_PERSON = "wave_no_person"
# A chip the operator marked as a second chip (双芯片, right click on its row):
# the rider counted for another bib carries it too, kept as carrier_bib.
# Exported to 建议删除清单; the wave counts without it.
EXTRA_CHIP = "extra_chip"
# The number the operator read on the frame of a rider without a chip, kept
# beside their 疑似漏读 row and exported to 补录清单.
RECORD_BIB = "record_bib"
# A bib read for a rider without a chip while that bib has a chip this close:
# it is the same passage (古浪 6 号 was a late import), not a miss.
BIB_CHIP_REACH_MS = 5000
# A detected crossing this close to someone counted on a wave's picture, on the
# same camera, is most likely that person (古浪 10-28: one rider showed as a
# 疑似漏读 row and a 待定 card at 11:28:57.101).  It stays a row, shown as
# 已点附近: it may be someone else nobody counted (review 10-29, 疑点保留).
COUNTED_REACH_MS = 500
NEAR_COUNTED_TEXT = ("已点附近：这一刻 0.5 秒内已点过人，多半就是那个人。看一眼：是同一个人按 1 排除；"
                     "是没点到的人就在画面上补点，或按 3 补录号码。")
# People counted up to about three frames before the frame shown are drawn as a
# faded ring, so a rider still on the line is not counted a second time.  Only
# earlier ones and only this close: further off the rider has moved on and the
# ring sits on empty road (古浪).
GHOST_REACH_MS = 150
# The left hand's keys (10-29): each does on the selected row what its right
# click offers under these names; 4 is the same undo as Backspace.
ROW_KEYS = {Qt.Key_1: ("这人不是运动员", "不是运动员"),
            Qt.Key_2: ("双芯片：这条芯片是谁带的…", "改携带人…"),
            Qt.Key_3: ("这人没芯片：补录号码…", "补录号码…", "改号码…")}
KEY_NAMES = {Qt.Key_1: "1 不是运动员", Qt.Key_2: "2 双芯片", Qt.Key_3: "3 补录号码"}
NUMBER_KEYS = (Qt.Key_1, Qt.Key_2, Qt.Key_3, Qt.Key_4, Qt.Key_5)


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
    fragments: tuple = ()  # later pieces of the same passage shown on this row (rows of their own before)
    entered: dict | None = None  # the bib read for a 疑似漏读 row (see RECORD_BIB)
    carrier: str = ""  # a 双芯片 row: the bib of the rider who carries this chip too


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
    extra_chips: list = field(default_factory=list)  # chips marked 多余芯片: nobody is counted for them
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
        # A 不是运动员 (1, or the run from the right click) still playing the unseen part of its rows
        # before excluding them.
        self._auto_review = None
        # 只看要处理的: the table shows only the rows still to settle, and those
        # settled for the timing system (补录, 多余芯片).
        self._attention_only = False
        # True while a row is opened whose picture the panel itself places (or
        # keeps): the window must not reopen the item's stored frame over it.
        self.holds_picture = False
        super().__init__(parent)
        self.header.hide()
        self.detail_scroll.hide()
        self.timeline.hide()
        self.timeline_help.hide()
        self.table.currentCellChanged.disconnect()
        # 可能多人: crossing (item id, moment) -> when someone else came onto the
        # line before it (None: nobody), read off the strip once (earlier_arrival).
        self._earlier = {}
        self._metadata_provider = lambda: None  # CycleRace's race metadata: the groups' target laps
        self._target_laps = {}  # group id -> target laps of this race (CycleRace's metadata), when sent
        self._intermediate_laps = 0  # chip reads of laps before the last, not listed
        self._short_bibs = ()  # riders whose last read is short of the target, once the group has a finisher
        self._earlier_shown = frozenset()  # the 可能多人 the table shows
        self._near_counted_items = frozenset()  # item ids of crossings within COUNTED_REACH_MS of someone counted
        self._strip_end = (None, 0.0)  # (pieces, the strip's last chip-clock moment)
        self._earlier_timer = QTimer(self)
        self._earlier_timer.setSingleShot(True)
        self._earlier_timer.setInterval(30)
        self._earlier_timer.timeout.connect(self._read_earlier)
        self._review_timer = QTimer(self)
        self._review_timer.setSingleShot(True)
        self._review_timer.setInterval(REVIEW_STALL_MS)
        self._review_timer.timeout.connect(
            lambda: self._stop_auto_review("自动补看没有画面了（录像缺口或已到末尾），这一行未排除；请手动看完再按 1 排除。"))
        self.table.cellDoubleClicked.disconnect()
        self.table.cellClicked.connect(self._arrival_clicked)
        self.table.cellDoubleClicked.connect(self._arrival_clicked)
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(["顺序", "号码", "时间", "状态", "组别", "圈数"])
        self.table.horizontalHeaderItem(2).setToolTip(
            "校时后的芯片域时间；悬浮单元格可看录像原时刻"
        )
        for column, width in enumerate((42, 66, 137, 72)):
            self.table.setColumnWidth(column, width)
        # The group column only appears when the race has more than one group.
        self.table.setColumnHidden(4, True)
        # The lap column only appears when CycleRace sends a group's target laps (多圈).
        self.table.setColumnWidth(5, 46)
        self.table.setColumnHidden(5, True)
        self.table.setMinimumWidth(340)
        self.table.setMaximumWidth(440)
        self.table.setToolTip("点一行看这一刻；选中后按 1 不是运动员、2 双芯片、3 补录号码、4 撤销（右键同样）")
        # Everything a row still needs is settled from the row itself (10-29),
        # by 1 / 2 / 3 / 4 or its right click: the picture only counts people.
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._row_menu)
        # Search sits on top of the table: a bib or a name, Enter jumps to the row.
        column = self.table_column = QWidget(self.splitter)
        column_layout = QVBoxLayout(column)
        column_layout.setContentsMargins(0, 0, 0, 0)
        column_layout.setSpacing(3)
        self.search = QLineEdit(column)
        self.search.setPlaceholderText("搜号码 / 姓名，回车跳到")
        self.search.setClearButtonEnabled(True)
        self.search.returnPressed.connect(self._find)
        self.attention_button = QPushButton("只看要处理的", column)
        self.attention_button.setCheckable(True)
        self.attention_button.toggled.connect(self._set_attention_only)
        search_row = QHBoxLayout()
        search_row.setContentsMargins(0, 0, 0, 0)
        search_row.addWidget(self.search, 1)
        search_row.addWidget(self.attention_button)
        column_layout.addLayout(search_row)
        self.splitter.insertWidget(0, column)
        column_layout.addWidget(self.table)
        self.quick_toolbar = QWidget(self)
        bar = QHBoxLayout(self.quick_toolbar)
        bar.setContentsMargins(2, 3, 2, 3)
        self.quick_title = QLabel("到达记录", self.quick_toolbar)
        bar.addWidget(self.quick_title, 1)
        self.defer_button = QPushButton("暂放", self.quick_toolbar)
        self.confirm_arrival_button = QPushButton("确认过线", self.quick_toolbar)
        self.confirm_arrival_button.setToolTip(
            "先选左侧运动员，再将画面停在其前轮压终点线的一帧，最后点击确认过线"
        )
        self.quick_undo_button = QPushButton("撤销 4", self.quick_toolbar)
        for button in (self.defer_button, self.confirm_arrival_button, self.quick_undo_button):
            bar.addWidget(button)
        self.confirm_arrival_button.hide()
        self.defer_button.hide()
        self.quick_undo_button.setToolTip("4 或退格：撤销所选这一行的判定；刚判完自动跳到下一行时，撤销刚才那一条")
        self.defer_button.clicked.connect(lambda: self._classify("held"))
        self.confirm_arrival_button.clicked.connect(self._confirm_selected_arrival)
        self.quick_undo_button.clicked.connect(self._undo_arrival)
        more = QPushButton("更多", self.quick_toolbar)
        menu = QMenu(more)
        menu.addAction("分析录像", self._toggle_analysis)
        menu.addAction("导出给计时系统（补录 / 建议删除）", self.export)
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

    def set_metadata_provider(self, provider):
        self._metadata_provider = provider

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
        """This race's chip reads, without the laps before a rider's last (多圈赛只判最后一圈).

        A lap is known as intermediate only when CycleRace's race metadata
        carries the group's target laps; without them every read is listed, as before.
        """
        events = tuple(self._events_provider())
        scopes = {(event.race_id, event.stage_id) for event in events}
        self._arrival_scope = self._scope if self._scope[0] else next(iter(scopes)) if len(scopes) == 1 else self._scope
        scoped = [event for event in events if (event.race_id, event.stage_id) == self._arrival_scope]
        metadata = self._metadata_provider()
        self._target_laps = ({group.group_id: group.target_laps for group in metadata.groups}
                             if metadata is not None and (metadata.race_id, metadata.stage_id) == self._arrival_scope
                             else {})
        listed = [event for event in scoped
                  if lap_role(event.lap, self._target_laps.get(event.group_id, 0)) != INTERMEDIATE]
        self._intermediate_laps = len(scoped) - len(listed)
        self._short_bibs = self._short_of_laps(scoped)
        return {event.event_id: event for event in listed}

    def _short_of_laps(self, events):
        """Bibs whose last read is short of the group's target laps, in groups someone has finished.

        Before anyone finishes, every rider is short: nothing is said then.  A
        hint only (圈数异常只提示): the rider may not have finished, or a lap
        may have gone unread.
        """
        laps = self._target_laps
        active = [event for event in events if event.is_active and laps.get(event.group_id, 0) > 1]
        finished = {event.group_id for event in active if event.lap >= laps[event.group_id]}
        passages = [Passage(event.event_id, event.athlete_id or event.bib, int(event.timeline_timestamp_ms),
                            event.lap, laps[event.group_id])
                    for event in active if event.group_id in finished]
        short = short_riders(passages)
        bibs = {event.athlete_id or event.bib: event.bib for event in active}
        return tuple(sorted((bibs[rider] or rider for rider in short), key=lambda bib: (len(bib), bib)))

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
            if self._arrival_store is not None and row.event is None:
                entered = self._arrival_store.latest(self._arrival_scope, "bib:" + row.item.item_id,
                                                     candidate_binding(row.item))
                if entered is not None and entered["action"] == "held" and entered.get("reason") == RECORD_BIB:
                    row.entered, row.bib = entered, entered["bib"]
            if self._arrival_store is not None and row.event is not None:
                previous = self._arrival_store.latest(self._arrival_scope, row.key)
                if previous is not None:
                    anchor = anchors.get(previous.get("candidate_id"))
                    if (previous["action"] == "held" and previous.get("reason") == EXTRA_CHIP
                            and previous["binding"] == event_binding(row.event)):
                        row.note = "多余芯片"  # needs no picture of its own: nobody crossed for it
                        row.carrier = str(previous.get("carrier_bib") or "")
                        continue
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
        moments = self._counted_moments(counting)
        rows = [row for row in rows if row.event is not None or self._candidate_shown(row.item, assigned, counting)]
        self._near_counted_items = frozenset(row.item.item_id for row in rows
                                       if row.event is None and self._near_counted_moment(row.item, assigned, moments))
        rows = self._without_repeats(rows)
        rows = self._fold_fragments(rows, assigned)
        # Arrival order is chip time.  Not the order the records came in: a chip
        # added later (古浪 6 号) sat after the whole wave and gave its rider's
        # click to the next bib.  A corrected video time never moves a chip.
        chips = sorted((row for row in rows if row.event is not None), key=lambda row: (
            int(row.event.timeline_timestamp_ms), row.event.sequence, row.key))
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

    def _without_repeats(self, rows):
        """One row per person without a chip: the same person flagged twice on a wave is listed once.

        Older builds let a 无芯片 person be clicked and flagged again (古浪 10-28,
        two 疑似漏读 rows 0.02 apart on one frame), which exported them twice.
        Both stay drawn on the wave's picture, where a right click cancels one.
        """
        kept, seen = [], []
        for row in rows:
            marks = self._marks.get(row.item.item_id, ()) if row.event is None else ()
            parent = row.item.evidence.get("wave_parent")
            if parent and len(marks) == 1:
                mark = marks[0]
                if any(owner == parent and mark_on_frame(mark, other.get("frame") or {}, other["recorder_ms"])
                       and (mark["x"] - other["x"]) ** 2 + (mark["y"] - other["y"]) ** 2 < .02 ** 2
                       for owner, other in seen):
                    continue
                seen.append((parent, mark))
            kept.append(row)
        return kept

    def _fold_fragments(self, rows, assigned):
        """Pieces of one passage without a chip, within a second of the first, share its row.

        Display only: every piece keeps its own record, and a ruling on the row
        is saved on each.  Only untouched pieces of a no-chip crossing fold, and
        only with pieces in the same state, so nothing judged or counted hides.
        """
        def foldable(row):
            item = row.item
            return (row.event is None and row.mark is None and row.decision is None and row.time_ms is not None
                    and item.kind == KIND_NO_CHIP_CROSSING and not item.evidence.get("wave_parent")
                    and not self._marks.get(item.item_id) and not assigned[item.item_id])
        folded, leads = set(), {}
        for row in sorted((row for row in rows if foldable(row)), key=lambda row: (row.time_ms, row.key)):
            state = (self._row_state(row), (self._category(row.item) or {}).get("action"),
                     int((row.item.evidence.get("frame") or {}).get("camera_index") or 1))
            lead = leads.get(state)
            if lead is not None and row.time_ms - lead.time_ms <= FRAGMENT_REACH_MS:
                lead.fragments += (row,)
                folded.add(row.key)
            else:
                leads[state] = row
        return [row for row in rows if row.key not in folded]

    def _fragment_unseen(self, row, whole=False):
        """What of a row, from where it is looked at from to its last piece + 0.25 s, nobody has looked at.

        Only a folded row has to be seen whole before it is ruled on, or any row
        of a Shift+3 run (WHOLE).
        """
        if row is None or row.time_ms is None or not (row.fragments or whole):
            return []
        camera = str(int((row.item.evidence.get("frame") or {}).get("camera_index") or 1))
        seen = self._arrival_drafts.get("seen", {}).get(camera, [])
        last = (row.fragments[-1] if row.fragments else row).time_ms
        return uncovered_intervals(row.time_ms - self._passage_lead(row), last + PASSAGE_VIEW_MARGIN_MS,
                                   seen, tolerance=100)

    def _passage_lead(self, row):
        """How long before a crossing it is looked at from: a quarter second without a chip, half with one.

        可能多人: from the moment the other one came onto the line (15:50,
        10-29: the first two of 4 came on 1.9 s before the row's moment).  2 s
        before every crossing, and half a second more on 可能多人, was too far
        back: the rider was still far off.
        """
        if row is None or row.event is not None:
            return WAVE_VIEW_MARGIN_MS
        arrival = self._earlier_arrival(row, read=False)
        if arrival is None:
            return PASSAGE_VIEW_MARGIN_MS
        return max(PASSAGE_VIEW_MARGIN_MS, -(-int(row.time_ms - arrival) // 100) * 100)

    def _earlier_arrival(self, row, read=True):
        """When someone else came onto the line up to 2 s before this crossing without a chip (可能多人).

        None if nobody did, or not known yet: READ reads it off the strip now,
        once the strip holds the moment (else the table's pass reads it later).
        """
        if row is None or row.event is not None or row.time_ms is None or self._row_state(row) != "待定":
            return None
        key = (row.item.item_id, int(row.time_ms))
        if key not in self._earlier and read:
            pieces = self.race_strip.view.pieces
            if pieces and self._strip_end[0] is not pieces:
                self._strip_end = (pieces, max(piece.chip_end for piece in pieces))
            if not pieces or self._strip_end[1] < row.time_ms + 1500:
                return None
            own = {piece.key for piece in row.fragments}
            others = [int(other.event.timeline_timestamp_ms) if other.event is not None else other.time_ms
                      for other in self._arrival_rows
                      if other is not row and other.key not in own and other.mark is None
                      and (other.event is None or other.event.is_active)
                      and (other.time_ms is not None or other.event is not None)]
            others += [piece.time_ms for other in self._arrival_rows if other is not row
                       for piece in other.fragments if piece.time_ms is not None]
            self._earlier[key] = earlier_arrival(pieces, self.race_strip.view.cache, int(row.time_ms), others)
            if self._earlier[key] is not None and not self._earlier_timer.isActive():
                self._earlier_timer.start()  # read on opening the row: the table shows it too
        return self._earlier.get(key)

    def _read_earlier(self):
        """A few crossings at a time; the table shows 可能多人 once they are read."""
        read = 0
        for row in self._arrival_rows:
            if read == EARLIER_BATCH:
                break
            key = (row.item.item_id, int(row.time_ms)) if row.time_ms is not None else None
            if row.event is None and key is not None and key not in self._earlier:
                self._earlier_arrival(row)
                read += key in self._earlier
        if {key for key, arrival in self._earlier.items() if arrival is not None} != self._earlier_shown:
            self._render()  # and on to the next few
        elif read == EARLIER_BATCH:
            self._earlier_timer.start()

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

    def _near_counted_moment(self, item, assigned, moments):
        """A detected crossing nobody ruled on, at the moment someone was counted on a wave's picture.

        Most likely that person, maybe someone nobody counted: a row, 已点附近
        (COUNTED_REACH_MS).
        """
        if not moments or self._marks.get(item.item_id) or assigned[item.item_id] or self._category(item) is not None:
            return False
        frame = item.evidence.get("frame") or {}
        stamp = item.evidence.get("recorder_ms", frame.get("recorder_ms"))
        near = moments.get(int(frame.get("camera_index") or 1), ())
        return stamp is not None and any(abs(int(stamp) - moment) <= COUNTED_REACH_MS for moment in near)

    def _counted_moments(self, counting):
        """Camera -> recorder moments of everyone counted on a wave's picture, those without a chip too."""
        moments = {}
        for item in self.items:
            if item.item_id in counting or item.evidence.get("wave_parent") in counting:
                for mark in self._marks.get(item.item_id, ()):
                    camera = int((mark.get("frame") or {}).get("camera_index") or 1)
                    moments.setdefault(camera, []).append(int(mark["recorder_ms"]))
        return moments

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
            # A click on someone only counts them; their bib is the chip's turn in
            # the wave, so it is 已点, not a confirmed identity (10-29).
            return "已点" if (row.decision.get("frame") or {}).get("marker_x_normalized") is not None else "已确认"
        if row.note:
            return row.note
        if (row.item.status == STATUS_WAITING or row.item.evidence.get("configuration_stale")
                or row.item.evidence.get("superseded")):
            return "待定"
        category = self._category(row.item)
        if category and category["action"] == "bicycle" and (row.item.evidence.get("wave_parent")
                                                             or category.get("no_chip")):
            return "补录" if row.entered else "疑似漏读"
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
        # Back on the table, so Enter / Backspace work at once.
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
        outdated = set()
        for index, plan in plans.items():
            if plan.marks or plan.split:
                if self._wave_outdated(plan):
                    outdated.add(index)
                if not self._unseen(plan):
                    # Looked through and counted, yet nobody for these chips: one
                    # rider with two chips, a chip without a rider, or someone missed.
                    for chip in plan.unmatched:
                        if chip.decision is None and not chip.note:
                            chip.note = "疑似双芯片"
        rows = self._arrival_rows = self._with_extra_rows(rows, plans)
        self._simultaneous = Counter(row.time_ms for row in rows if row.decision)
        self.rows = [row.item for row in rows]
        groups = [self._row_group(row) for row in rows]
        counted = {index: plan.matches and index not in outdated
                   for index, plan in plans.items() if plan.marks or plan.split}
        busy = self._earlier_shown = frozenset(key for key, arrival in self._earlier.items() if arrival is not None)
        signature = (self._arrival_scope, self._selected_key, tuple(sorted(counted.items())), self._attention_only,
                     busy, self._near_counted_items, tuple(sorted(self._target_laps.items())),
                     tuple((row.key, row.bib, row.time_ms, row.reference, self._row_state(row), self._row_category(row),
                            candidate_binding(row.item), self._wave_of.get(row.key), group, row.carrier,
                            tuple(piece.key for piece in row.fragments))
                           for row, group in zip(rows, groups)))
        if self._quick_ready and self.race_strip.view.pieces and not self._earlier_timer.isActive():
            self._earlier_timer.start()  # 可能多人 of crossings not read yet
        if signature == self._quick_render_signature:
            self._sync_arrival_confirmation()
            return
        self._quick_render_signature = signature
        # Who carries which second chip: shown on the carrier's row too.
        carried = {}
        for row in rows:
            if row.carrier and self._row_state(row) == "多余芯片":
                carried.setdefault(row.carrier, []).append(row.bib)
        several_groups = len({group for group in groups if group}) > 1
        self.table.setColumnHidden(4, not several_groups)
        self.table.setColumnHidden(5, not any(row.event is not None and self._target_laps.get(row.event.group_id, 0) > 1
                                              for row in rows))
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
            # A chip row shows its chip time, so the column reads in the table's
            # order; the frame its rider was clicked on is in the tooltip.
            moment = int(row.event.timeline_timestamp_ms) if row.event is not None else row.time_ms
            time = format_chip_time(moment) if moment is not None else "—"
            category = self._row_category(row)
            number = ""
            if row.event is not None:
                chip_number += 1
                if category != "非运动员":
                    number = str(chip_number)
            shown_state = f"{state}·{len(row.fragments) + 1}段" if row.fragments else state
            arrival = self._earlier_arrival(row, read=False)
            if arrival is not None:
                shown_state = "可能多人"
            near_counted = state == "待定" and row.item.item_id in self._near_counted_items
            if near_counted:
                shown_state = "已点附近"
            if state == "多余芯片":
                shown_state = f"{row.carrier}号带的" if row.carrier else "双芯片"
            target = self._target_laps.get(row.event.group_id, 0) if row.event is not None else 0
            laps = f"{row.event.lap}/{target}" if target > 1 else ""
            for column, text in enumerate((number, row.bib or "—", time, shown_state, groups[index], laps)):
                from PyQt5.QtWidgets import QTableWidgetItem
                cell = QTableWidgetItem(text)
                cell.setData(Qt.UserRole, row.key)
                if column == 3:
                    foreground, background = {"已确认": ("#126044", "#e5f4ec"),
                                              "已点": ("#126044", "#e5f4ec"),
                                              "已排除": ("#1d4ed8", "#e6f0ff"),
                                              "待定": ("#b42318", "#fdecec"),
                                              "多出": ("#b42318", "#fdecec"),
                                              "疑似漏读": ("#9a3412", "#ffedd5"),
                                              "疑似双芯片": ("#9a3412", "#ffedd5"),
                                              "补录": ("#0f766e", "#ccfbf1"),
                                              "多余芯片": ("#0f766e", "#ccfbf1")}[state]
                    cell.setForeground(QColor(foreground))
                    cell.setBackground(QColor(background))
                    cell.setTextAlignment(Qt.AlignCenter)
                tooltip = ("点击回看保存原帧" if row.decision else
                           f"点击回看这一刻（从过线前 {self._passage_lead(row) / 1000:g} 秒看起）；"
                           "选中按 1/2/3/4 或右键处理这一行")
                if row.mark is not None:
                    tooltip = "比芯片多点出的人：点这一行回到点他的那一帧；按 3 补录号码或 1 不是运动员，点错在画面上右键去掉"
                tooltip = {"疑似漏读": "画面上有人却没有芯片：核对原帧号码后按 3 补录号码，导出到补录清单；"
                                       "不是运动员按 1",
                           "补录": "已填号码，导出到补录清单；按 3 改号码，4 撤回",
                           "疑似双芯片": "本波点完、看完，这条芯片没有对上的人：漏点了就回画面补点；"
                                         "是别人带的第二个芯片就按 2 填携带人",
                           "多余芯片": f"{self._carrier_text(row)}，导出到建议删除清单（CycleRace 人工处理）；"
                                       "按 2 改携带人，4 撤回",
                           }.get(state, tooltip)
                if row.bib and row.bib in carried and row.event is not None:
                    tooltip = f"{row.bib} 号另带 {'、'.join(carried[row.bib])} 号芯片（双芯片）\n" + tooltip
                if arrival is not None:
                    tooltip = (f"可能多人：过线前 {(row.time_ms - arrival) / 1000:.1f} 秒线上还有人到，"
                               "看到几个人就点几个\n" + tooltip)
                if near_counted:
                    tooltip = NEAR_COUNTED_TEXT + "\n" + tooltip
                if row.fragments:
                    tooltip = (f"同一次过线被拆成 {len(row.fragments) + 1} 段画面，合成这一行"
                               f"（{(row.fragments[-1].time_ms - row.time_ms) / 1000:.1f} 秒）\n"
                               f"看完首段前 {self._passage_lead(row) / 1000:g} 秒到末段后 "
                               f"{PASSAGE_VIEW_MARGIN_MS / 1000:g} 秒，画面上看到几个人就点几个；"
                               "按 1（不是运动员）自动补看没看的部分再排除整行，4 或退格撤回整行\n"
                               + tooltip)
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
                    tooltip = ("本波人数与芯片数一致\n" if matches else
                               "芯片有变化：打开这一波按回车，按新顺序重存\n" if self._wave_of[row.key] in outdated
                               else "本波人数与芯片数不一致（多人或少人）\n") + tooltip
                if column == 2 and row.time_ms is not None and (
                        moment != row.time_ms or row.recorder_ms is not None and int(row.recorder_ms) != moment):
                    tooltip = (f"判读时间（校时后）：{format_chip_time(row.time_ms)}\n"
                               + (f"录像原时刻：{format_chip_time(row.recorder_ms)}\n"
                                  if row.recorder_ms is not None else "") + tooltip)
                cell.setToolTip(tooltip)
                self.table.setItem(index, column, cell)
            if row.key == self._selected_key or any(piece.key == self._selected_key for piece in row.fragments):
                selected = index
        attention = [self._attention(row, outdated, counted) for row in rows]
        for index, need in enumerate(attention):
            self.table.setRowHidden(index, self._attention_only and need is None)
        self._sync_attention_button(attention)
        if selected >= 0:
            self._selected_key = rows[selected].key
            self.table.setCurrentCell(selected, 0)
        elif rows:
            self.table.setCurrentCell(0, 0)
        self.table.verticalScrollBar().setValue(scroll)
        self.table.blockSignals(False)
        confirmed = sum(row.decision is not None for row in rows)
        self.quick_title.setText(f"到达记录 {len(rows)} 条 · 已对上 {confirmed} 个芯片"
                                 + (f" · 中间圈 {self._intermediate_laps} 条不列" if self._intermediate_laps else "")
                                 + (f" · 圈数不足 {len(self._short_bibs)} 人" if self._short_bibs else ""))
        self.quick_title.setToolTip(
            "圈数不足（只提示，不判定；可能未完赛或漏读一圈）：" + "、".join(self._short_bibs) + " 号"
            if self._short_bibs else "")
        self.summary.setText(self.quick_title.text())
        self.summary_changed.emit(self.quick_title.text())
        if self._active_item is None and rows:
            self._open_row(0)
        elif self._active_item is not None:
            fresh = next((row.item for row in rows if row.item.item_id == self._active_item.item_id), None)
            if fresh is None:
                # A piece just folded into the row of its passage: the row stays open, on this piece.
                fresh = next((piece.item for row in rows for piece in row.fragments
                              if piece.item.item_id == self._active_item.item_id), None)
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

        A decided row: the frame it was decided on.  A chip row: half a second
        before its wave's first rider meets the line as the strip sees it (the
        chip time alone lands on the wrong rider of a bunch), unless the picture
        already shows this wave: switching riders inside a wave never moves it.
        A crossing without a chip nobody ruled on: 2 s before it, so a passage
        of several people is seen from its start (15:50, 10-29: the row's own
        frame showed 2 of 4, the first two crossed 1.7 s before).  KEEP means stay; None means the row's
        own stored frame or chip reference.
        """
        self._landing_pending = None
        if row.decision and row.decision.get("frame"):
            return dict(row.decision["frame"])
        if row.mark is not None:
            return dict(row.mark.get("frame") or {}) or None
        stamps = [int(chip.event.timeline_timestamp_ms) for chip in self._row_wave(row) or ()
                  if chip.event is not None and chip.event.is_active]
        if not stamps:
            return self._passage_start(row)
        if not self._quick_ready:
            return None
        if not self.race_strip.view.pieces:
            self._landing_pending = row.key  # the strip is still loading: land when it is read
            return None
        first, last = min(stamps), max(stamps)
        if keep and self._shown_ms is not None and first - WAVE_GAP_MS / 2 <= self._shown_ms <= last + WAVE_GAP_MS / 2:
            return KEEP
        pieces, cache = self.race_strip.view.pieces, self.race_strip.view.cache
        moment = first_crossing(pieces, cache, first)
        moment = first if moment is None else moment
        frame = frame_at(pieces, moment - WAVE_VIEW_MARGIN_MS) or frame_at(pieces, moment)
        return dict(frame, reference_only=True) if frame is not None else None

    def _passage_start(self, row):
        """Where a crossing without a chip is looked at from (_passage_lead), on its own camera.

        Without a strip aligned to the chips (a race without chips: 现代五项
        10-29) it is that far back from the row's own frame, maybe before its
        file starts: the window opens the archive or the piece that holds it.
        """
        if (not self._quick_ready or row.event is not None or row.time_ms is None
                or self._row_state(row) != "待定"):
            return None
        own = row.item.evidence.get("frame") or {}
        camera = int(own.get("camera_index") or 1)
        pieces = self.race_strip.view.pieces
        self._earlier_arrival(row)
        lead = self._passage_lead(row)
        frame = frame_at(pieces, row.time_ms - lead) if pieces else None
        if frame is not None and int(frame.get("camera_index") or 1) == camera:
            return dict(frame, reference_only=True)
        if not own.get("video_path") or own.get("position_ms") is None:
            return None
        return {"camera_index": camera, "segment_id": own.get("segment_id"), "video_path": own["video_path"],
                "position_ms": int(own["position_ms"]) - lead, "frame_index": None,
                "reference_only": True}

    def _strip_ready(self):
        """The strip was still being read when the row was opened (at start): land it now."""
        key, self._landing_pending = self._landing_pending, None
        self._earlier_timer.start()  # 可能多人 of the crossings, read off the strip
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
        if index is None:  # a piece of a folded passage opens its row
            index = next((i for i, row in enumerate(self._arrival_rows)
                          if any(piece.item.item_id == item_id for piece in row.fragments)), None)
        if index is not None:
            self._arrival_clicked(index)

    def _arrival_clicked(self, index, _column=0):
        if not 0 <= index < len(self._arrival_rows):
            return
        if self._auto_review is not None:
            self._stop_auto_review("换了一行，自动补看已停止；这一行未排除。")
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
            else:
                self._show_row_hint(row)
            self._sync_arrival_confirmation()
            self.marks_changed.emit(tuple(self.current_marks()))
            return
        # Preserve a deliberately classified picture while assigning identities,
        # but not once everyone on it has a row: the next chip is someone else.
        # Only a chip can be assigned so: a row without one opens its own picture.
        item = self.current_item()
        people = max(1, sum(not is_excluded(mark) for mark in self.current_marks()))
        if (row.event is not None and item is not None and item.item_id not in self._candidate_wave
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
                self.hint.setText("已选中；在画面上点压线的人即保存。")
                return
        self._open_row(index)
        self._show_row_hint(row)

    def _show_row_hint(self, row):
        """What settles a row still to handle at the end (see 只看要处理的)."""
        text = {"疑似漏读": "疑似漏读：核对原帧上的号码，按 3 补录号码（导出到补录清单）；不是运动员按 1。",
                "补录": f"补录 {row.bib} 号：按 3 改号码，4 撤回。",
                "疑似双芯片": f"{row.bib} 号这一波看完了却没对上人：漏点了就在画面上补点；"
                              "是别人带的第二个芯片，就按 2 填携带人。",
                "多余芯片": f"{self._carrier_text(row)}（建议删除）；按 2 改携带人，4 撤回。"}.get(self._row_state(row))
        arrival = self._earlier_arrival(row, read=False)
        if arrival is not None:
            text = (f"可能多人：过线前 {(row.time_ms - arrival) / 1000:.1f} 秒线上还有人到，"
                    "已从那时放起；看到几个人就点几个。")
        if self._row_state(row) == "待定" and row.item.item_id in self._near_counted_items:
            text = NEAR_COUNTED_TEXT
        if text:
            self.hint.setText(text)

    def _row_menu(self, pos):
        """Right click on a row of the left table: what that row still needs, and nothing else."""
        index = self.table.rowAt(pos.y())
        if not 0 <= index < len(self._arrival_rows):
            return
        if self._arrival_rows[index].key != self._selected_key or self.table.currentRow() != index:
            self._arrival_clicked(index)
        row = self._selected_arrival()
        actions = self._row_actions(row) if row is not None else []
        if not actions:
            self.hint.setText("这一行没有要处理的。")
            return
        menu = QMenu(self.table)
        for label, action in actions:
            key = next((key for key, labels in ROW_KEYS.items() if label in labels), None)
            menu.addAction(label + (f"\t{KEY_NAMES[key][0]}" if key is not None else ""), action)
        menu.exec_(self.table.viewport().mapToGlobal(pos))

    def _row_key(self, key):
        """1 / 2 / 3 on the selected row: what its right click offers under that name, or why not."""
        row = self._selected_arrival()
        if row is None:
            self.hint.setText("先在左边选一行（或在画面上点中这个人），再按 " + KEY_NAMES[key])
            return
        actions = self._row_actions(row)
        action = next((action for label, action in actions if label in ROW_KEYS[key]), None)
        if action is not None:
            action()
            return
        offered = [name for other, name in KEY_NAMES.items()
                   if any(label in ROW_KEYS[other] for label, _ in actions)]
        offered += ["4 撤销"] if any(label.startswith("撤") for label, _ in actions) else []
        if (key != Qt.Key_2 and row.event is not None and row.event.is_active
                and self._row_state(row) != "多余芯片" and self._counted_person(row) is None):
            text = f"{row.bib} 号还没点人：先在画面上点中这个人，再按 {KEY_NAMES[key][0]}。"
        elif key == Qt.Key_2 and row.event is None:
            text = "双芯片用在芯片行：选中那条多出来的芯片再按 2。"
        else:
            text = f"这一行不能按 {KEY_NAMES[key][0]}。"
        self.hint.setText(text + (f"这一行可以按：{'、'.join(offered)}。" if offered else ""))

    def _row_actions(self, row):
        """(label, action) a right click on ROW offers, from the row's own state only (10-29).

        The picture only counts people; whatever a row still needs (a bib for
        someone without a chip, whose second chip this is, 不是运动员) is said here.
        """
        key, state = row.key, self._row_state(row)
        if row.event is not None:
            if not row.event.is_active:
                return []
            if state == "多余芯片":
                return [("改携带人…", lambda: self._mark_extra_chip(key)),
                        ("撤回双芯片", lambda: self._take_back_extra_chip(key))]
            counted = self._counted_person(row) is not None
            actions = [("这人没芯片：补录号码…", lambda: self._person_no_chip(key)),
                       ("这人不是运动员", lambda: self._person_flag(key, "exclusion_reason"))] if counted else []
            actions.append(("双芯片：这条芯片是谁带的…", lambda: self._mark_extra_chip(key)))
            if counted:
                actions.append(("撤销：去掉这个人", lambda: self._drop_person(key)))
            elif row.decision is not None:
                actions.append(("撤销这一行", lambda: self._undo_key(key)))
            return actions
        if row.mark is not None:  # 多出: counted beyond the wave's chips
            return [("补录号码…", lambda: self._person_no_chip(key)),
                    ("不是运动员", lambda: self._person_flag(key, "exclusion_reason")),
                    ("撤销：去掉这个人", lambda: self._drop_person(key))]
        if state == "补录":
            return [("改号码…", lambda: self._enter_bib(self._row_by_key(key))),
                    ("撤回号码", lambda: self._undo_bib(self._row_by_key(key)))]
        if state == "疑似漏读":
            return [("补录号码…", lambda: self._enter_bib(self._row_by_key(key))),
                    ("不是运动员", lambda: self._miss_not_rider(key)),
                    ("撤回疑似漏读", lambda: self._take_back_miss(key))]
        if state == "已排除":
            return [("撤销排除", lambda: self._undo_key("candidate:" + row.item.item_id))]
        if self._excludable(row):
            actions = [("补录号码…", lambda: self._record_candidate(key)),
                       ("不是运动员", lambda: self._candidate_not_rider(key))]
            if len(self._exclusion_run()) > 1:
                actions.append(("连续排除到下一个芯片", self._exclude_run))
            return actions
        return []

    def _counted_person(self, row):
        """(plan, index) of the person counted for ROW on its wave's picture: its chip's, or a 多出 row's."""
        if row is None:
            return None
        if row.mark is not None:
            plan = self._wave_plan(row.item)
            index = self._plan_index(plan, row.mark)
        else:
            plan = self._counted_plan(row)
            index = next((i for i, chip in plan.assigned if chip.key == row.key), None) if plan is not None else None
        return (plan, index) if index is not None else None

    def _person_picture(self, key):
        """The person of a row, with their wave's picture made the one acted on."""
        person = self._counted_person(self._row_by_key(key))
        if person is None:
            self.hint.setText("这一行没有在画面上点过的人。")
            return None
        plan = person[0]
        if self._active_item is None or self._active_item.item_id != plan.item.item_id:
            self._active_item = plan.item
            self._shown_id = plan.item.item_id
        return person

    def _person_no_chip(self, key):
        """这人没芯片：they leave the wave's chips for a 疑似漏读 row of their own, and their bib is asked."""
        person = self._person_picture(key)
        if person is None:
            return
        self._flag_mark("no_chip", index=person[1])
        row = self._selected_arrival()
        if row is not None and row.event is None and self._row_state(row) == "疑似漏读":
            self._enter_bib(row)

    def _person_flag(self, key, flag):
        person = self._person_picture(key)
        if person is not None:
            self._flag_mark(flag, index=person[1])

    def _drop_person(self, key):
        person = self._person_picture(key)
        if person is not None:
            plan, index = person
            self._drop_mark(plan.item.item_id, plan.marks[index])

    def _record_candidate(self, key):
        """补录号码 on a crossing without a chip: listed 疑似漏读, then its bib is asked."""
        row = self._row_by_key(key)
        if row is None or not self._excludable(row):
            return
        self._candidate_no_chip(row)
        row = self._row_by_key(key)
        if row is not None and self._row_state(row) == "疑似漏读":
            self._select_key(key)
            self._enter_bib(row)

    def _candidate_not_rider(self, key):
        index = next((i for i, row in enumerate(self._arrival_rows) if row.key == key), None)
        if index is None:
            return
        row = self._arrival_rows[index]
        if self.current_item() is None or self.current_item().item_id != row.item.item_id:
            self._open_row(index)
            row = self._row_by_key(key) or row
        self._classify("not_rider", row)

    def _miss_not_rider(self, key):
        """不是运动员 on a 疑似漏读 row: out of the wave's count, or the picture excluded."""
        row = self._row_by_key(key)
        if row is None:
            return
        if row.item.evidence.get("wave_parent"):
            self._return_to_wave(row, exclude=True)
            return
        if not self._frame_usable(self._row_frame(row)):
            self.hint.setText("请先打开当前候选的有效录像画面。")  # still 疑似漏读: nothing taken back
            return
        self._take_back_no_chip(row)
        self._candidate_not_rider(key)

    def _take_back_miss(self, key):
        row = self._row_by_key(key)
        if row is None:
            return
        if row.item.evidence.get("wave_parent"):
            self._return_to_wave(row)
        else:
            self._take_back_no_chip(row)

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
        plan.extra_chips = [row for row in chips if row.note == "多余芯片"]
        matched = set()
        for i, mark in enumerate(marks):
            row = None if is_excluded(mark) else self._decided_row(mark, item)
            if row is not None and not self._counted_here(row, item):
                plan.labels[i] = row.bib
                matched.add(row.key)
        free = [row for row in chips if (row.decision is None or self._counted_here(row, item))
                and row.note != "多余芯片"]
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
        split = []  # a person flagged twice by an older build counts once (see _without_repeats)
        for other in self.items:
            if other.evidence.get("wave_parent") == item.item_id:
                for mark in self._marks.get(other.item_id, ()):
                    if not is_excluded(mark) and not any(
                            mark_on_frame(mark, seen.get("frame") or {}, seen["recorder_ms"])
                            and (mark["x"] - seen["x"]) ** 2 + (mark["y"] - seen["y"]) ** 2 < .02 ** 2
                            for seen in split):
                        split.append(mark)
        plan.split = len(split)
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
            reason = "请先在画面上左键点中压线的人"
        elif (item.status == STATUS_WAITING or item.evidence.get("configuration_stale")
              or item.evidence.get("superseded")):
            reason = "当前候选证据已失效，请重新打开原帧"
        elif category is None or category.get("action") != "bicycle":
            reason = "请先在画面上左键点中压线的人"
        elif row.item.evidence.get("configuration_stale"):
            reason = "所选记录配置已变化，请重新分析"
        else:
            reason = ""
        self.confirm_arrival_button.setEnabled(not reason)
        self.confirm_arrival_button.setToolTip(reason or "保存所选运动员与当前显示的压线原帧；不会修改正式芯片时间")
        # The old confirm path's reasons only explain its hidden button; the
        # status says what the picture and the table do now.
        count = sum(not mark.get("exclusion_reason") for mark in self.current_marks())
        status = "左键点压线的人即保存 · 右键去掉 · 1 非运动员 2 双芯片 3 补录 4 撤销" + (f" · 已圈 {count}" if count else "")
        plan = self._wave_plan()
        wave = plan.chips if plan is not None else self._row_wave(row)
        color = ""
        if plan is not None and (plan.marks or plan.split):
            status, color = self._wave_status(plan)
        elif row is not None and wave is None and self._row_state(row) == "待定" and row.time_ms is not None:
            # A crossing without a chip may be several people: it is looked at
            # from a quarter second before (可能多人: from when the first came on)
            # to a quarter second after (15:50, 10-29: 4 people on one row).
            unseen = self._fragment_unseen(row, whole=True)
            span = (f"{len(row.fragments) + 1} 段 · {(row.fragments[-1].time_ms - row.time_ms) / 1000:.1f} 秒 · "
                    if row.fragments else "")
            if self._earlier_arrival(row, read=False) is not None:
                span = "可能多人 · " + span
            status = (span + (f"还差 {sum(right - left for left, right in unseen) / 1000:.1f} 秒没看" if unseen
                              else "已看完") + " · 看到几个人就点几个；3 补录、1 不是运动员")
            color = "#9a5b12" if unseen else "#126044"
        elif wave is not None and len(wave) > 1 and any(chip.decision is None for chip in wave):
            status = f"本波 {wave[0].bib}→{wave[-1].bib}（{len(wave)} 个芯片）：点一个人存一个"
        if plan is not None and plan.unmatched:
            status += f" · 再点一人 = {plan.unmatched[0].bib} 号"
        self.confirmation_status.setText(status)
        self.confirmation_status.setStyleSheet(f"color:{color}; font-weight:600;" if color else "")

    def _wave_status(self, plan):
        """The count of a wave against its chips, and its color: green 已核, red 多/少, amber not all seen."""
        chips, people = len(plan.chips) - len(plan.extra_chips), plan.people
        status = f"芯片 {chips} · 已点 {people}"
        if plan.extra_chips:
            status += f" · 双芯片 {len(plan.extra_chips)}"
        if plan.extra:
            status += f" · 多 {len(plan.extra)} 人（选中那一行按 3 补录号码或 1 不是运动员）"
        if plan.unmatched:
            status += f" · 少 {len(plan.unmatched)} 人"
        missing = len(plan.no_chip) + plan.split
        if missing:
            status += f" · 无芯片 {missing}"
        unseen = self._unseen(plan)
        if unseen:
            status += f" · 还差 {sum(right - left for left, right in unseen) / 1000:.1f} 秒没看"
        if self._wave_outdated(plan):
            # A chip came in (or changed) after the wave was counted: the bibs
            # saved no longer follow chip order.  Saved again only when asked.
            return status + " · 芯片有变化：按回车按新顺序重存", "#b42318"
        if not plan.matches:
            return status, "#b42318"
        if unseen:
            return status, "#9a5b12"
        return status + " · 已核", "#126044"

    def _wave_outdated(self, plan):
        """What is saved for a wave is not what its picture now counts (see _save_wave)."""
        wanted = {row.key: plan.marks[index] for index, row in plan.assigned}
        for row in plan.chips:
            if row.decision is not None and not self._counted_here(row, plan.item):
                continue
            mark = wanted.get(row.key)
            if (mark is None) != (row.decision is None) or (
                    mark is not None and not self._decision_holds(row.decision, mark)):
                return True
        return False

    def _counted_plan(self, row):
        """The plan of the wave this chip row is in, once someone is counted on its picture."""
        item = self._wave_candidate(self._row_wave(row)) if row is not None and row.event is not None else None
        plan = self._wave_plan(item) if item is not None else None
        return plan if plan is not None and (plan.marks or plan.split) else None

    def _unseen(self, plan):
        """The parts of a wave's span, in chip time, that no decoded frame has shown yet.

        A lone chip too: one crossing may be several people (15:50, 10-29).
        """
        if not plan.chips:
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
        selected = self._selected_arrival()
        plan = self._wave_plan()
        if plan is not None and (plan.marks or plan.split) and any(
                chip.decision is None and not chip.note for chip in plan.unmatched) and not self._unseen(plan):
            self._render()  # just looked through: its chips without a person turn 疑似双芯片
        elif plan is not None or (selected is not None and selected.fragments):
            self._sync_arrival_confirmation()  # "还有 x 秒没看" counts down on this very frame
        if self._auto_review is not None:
            self._advance_auto_review()

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
            head = "多出一人：按 3 补录号码，或按 1 标不是运动员"
        elif index in plan.no_chip:
            head = "这一位无芯片"
        elif label and not is_excluded(plan.marks[index]):
            head = f"已点：按芯片顺序记为 {label} 号"
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
        if row.event is None and self._row_state(row) in ("疑似漏读", "补录"):
            self._enter_bib(row)
            return
        self._confirm_arrival(row)

    def _enter_key(self):
        """Enter does what the selected row needs, never the old confirm.

        A 疑似漏读 / 补录 row: its bib.  A wave counted on its picture: saved in
        chip order and on to the next wave; a wave whose chips changed is saved
        again and stays, so the new bibs can be looked at.  Else the next row.
        """
        row = self._selected_arrival()
        if row is not None and row.event is None and self._row_state(row) in ("疑似漏读", "补录"):
            self._enter_bib(row)
            return
        plan = self._wave_plan()
        if plan is not None and (plan.marks or plan.split):
            outdated = self._wave_outdated(plan)
            if self._save_wave(plan):
                if outdated:
                    self.hint.setText("已按新的芯片顺序重存这一波，看一下号码；再按回车到下一波。")
                else:
                    self._next_wave(plan.item)
            return
        self._step(1)

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
                self.hint.setText("点人时已自动保存；看完这一波按回车到下一波。")
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
                self.hint.setText("已标记运动员；请在左边选对应的芯片记录。")
        finally:
            self._shortcut_operation = None

    @staticmethod
    def _frame_usable(frame):
        """A frame of a recording a ruling can be saved on and reopened from."""
        index = frame.get("frame_index") if frame else None
        return bool(frame and index is not None and index >= 0 and frame.get("position_ms", -1) >= 0
                    and frame.get("segment_id") and frame.get("video_path"))

    def _row_frame(self, row):
        """The frame a ruling on ROW is saved on: the one shown if it is this row's, else the row's own.

        This row's: from where it is looked at from (a second at least) to a
        second after its last piece.
        """
        frame = self._frame_provider()
        last = (row.fragments[-1] if row.fragments else row).time_ms
        if (not self._frame_usable(frame) or self._shown_ms is None or row.time_ms is None
                or not row.time_ms - max(1000, self._passage_lead(row)) <= self._shown_ms <= last + 1000):
            frame = row.item.evidence.get("frame")
        return frame

    def _classify(self, action, row=None):
        """Rule on the candidate on screen; with ROW (1, its right click), on that row of the table.

        On screen, a frame browsed to becomes a candidate of its own first (the
        window's candidate_preparing: Enter on a moment dragged to).  A row is
        ruled on as it is, wherever the picture is: 10-29 a row opened before
        its own frame made a new 人工发现 row of every 1 pressed on it.
        """
        if action != "held" and row is None:
            self.candidate_preparing.emit()
        item = row.item if row is not None else self._candidate_item()
        if not self._usable(item):
            return
        frame = self._row_frame(row) if row is not None else self._frame_provider()
        # A classification belongs to the candidate currently on screen.  Do not
        # let a stale/partial provider result create an audit entry that cannot be
        # reopened later (``held`` is the sole action that may be frame-less).
        if action != "held" and not self._frame_usable(frame):
            self.hint.setText("请先打开当前候选的有效录像画面。")
            return
        if action == "not_rider" and any(row.decision and row.decision.get("candidate_id") == item.item_id
                                         for row in self._arrival_rows):
            self.hint.setText("本处已有选手确认，请先撤销误确认。")
            return
        row = row if row is not None else self._selected_arrival()
        members = [row.item, *(piece.item for piece in row.fragments)] if row is not None and row.fragments else []
        pieces = [member for member in members if member.item_id != item.item_id] if any(
            member.item_id == item.item_id for member in members) else []
        unseen = self._fragment_unseen(row) if pieces else []
        if unseen and action == "not_rider" and self._excludable(row):
            # A folded row is ruled on as a whole, so all of it is looked at
            # first: 3 plays the rest and excludes it when it is seen.
            self._start_auto_review([row.key])
            return
        if unseen and action != "held":
            # A folded row is ruled on as a whole, so all of it is looked at first.
            self.hint.setText(f"这一行 {len(pieces) + 1} 段画面还差 "
                              f"{sum(right - left for left, right in unseen) / 1000:.1f} 秒没看；"
                              f"看完首段前 {self._passage_lead(row) / 1000:g} 秒到末段后 "
                              f"{PASSAGE_VIEW_MARGIN_MS / 1000:g} 秒再判。")
            return
        operation = getattr(self, "_shortcut_operation", None) or (uuid.uuid4().hex if pieces else None)
        if not self._save_ruling(item, pieces, action, frame, operation):
            return
        self._render()
        self.hint.setText("已标为运动员；在画面上点压线的人即保存。" if action == "bicycle"
                          else "已暂放。" if action == "held" else "已排除此画面候选。")
        if action in ("held", "not_rider"):
            self._step(1)
        self._last_saved_key = "candidate:" + item.item_id

    def _save_ruling(self, item, pieces, action, frame, operation, **extra):
        try:
            frame = dict(frame or {})
            frame.setdefault("calibration_version", item.calibration_version)
            # One ruling for the row is saved on every piece of it, under one
            # operation, so Backspace takes the whole row back.
            for target in (item, *pieces):
                self._arrival_store.save(self._arrival_scope, "candidate:" + target.item_id,
                                         candidate_binding(target), action, candidate_id=target.item_id,
                                         frame=frame,
                                         analysis_signature=target.evidence.get("analysis_signature", ""),
                                         operation_id=operation,
                                         calibration_version=target.calibration_version, **extra,
                                         **({"fragment_of": item.item_id} if target is not item else {}))
        except (OSError, RuntimeError, ValueError) as error:
            self.hint.setText(f"审核未保存：{error}")
            return False
        return True

    def _excludable(self, row):
        """An undecided picture without a chip that 3 may exclude."""
        item = row.item
        return (row.event is None and row.mark is None and not row.decision and row.time_ms is not None
                and self._row_state(row) == "待定" and bool(item.evidence.get("frame"))
                and item.status != STATUS_WAITING and not item.evidence.get("configuration_stale")
                and not item.evidence.get("superseded")
                and not any(other.decision and other.decision.get("candidate_id") == item.item_id
                            for other in self._arrival_rows))

    def _exclusion_run(self):
        """The selected undecided picture and those after it, up to a chip or a long gap."""
        index = self.table.currentRow()
        if not 0 <= index < len(self._arrival_rows) or not self._excludable(self._arrival_rows[index]):
            return []
        run, last = [], None
        for row in self._arrival_rows[index:]:
            if row.event is not None or row.mark is not None:
                break  # a chip, or someone counted: a rider passed here
            if row.time_ms is None:
                continue
            if last is not None and row.time_ms - last > EXCLUSION_RUN_GAP_MS:
                break
            if self._excludable(row):
                run.append(row)
            last = (row.fragments[-1] if row.fragments else row).time_ms
        return run

    def _exclude_run(self):
        rows = self._exclusion_run()
        if not rows:
            self.hint.setText("连续排除从选中的待定画面行开始，排除后面没有芯片的待定行。")
            return
        self._start_auto_review([row.key for row in rows])

    def _start_auto_review(self, keys):
        self._auto_review = {"keys": list(keys), "total": len(keys), "done": 0, "target": None,
                             "jumps": 0, "operation": uuid.uuid4().hex, "last_key": None}
        self._next_auto_review()

    def _review_row(self, key):
        index = next((i for i, row in enumerate(self._arrival_rows)
                      if row.key == key or any(piece.key == key for piece in row.fragments)), None)
        return (index, self._arrival_rows[index]) if index is not None else (None, None)

    def _next_auto_review(self):
        """Exclude the run's rows in order; a row not yet seen whole is played first."""
        review = self._auto_review
        if review is None:
            return
        while review["keys"]:
            index, row = self._review_row(review["keys"][0])
            if row is None or not self._excludable(row):
                review["keys"].pop(0)
                continue
            if self.table.currentRow() != index:
                self._open_row(index, keep=False)
            unseen = self._fragment_unseen(row, whole=True)
            if unseen:
                review["target"], review["jumps"] = row.key, 0
                self._review_seek(row, unseen)
                return
            if not self._exclude_seen(row, review["operation"]):
                self._auto_review = None
                self._review_timer.stop()
                return
            review["done"] += 1
            review["last_key"] = "candidate:" + row.item.item_id
            review["keys"].pop(0)
        self._auto_review = None
        self._review_timer.stop()
        done = review["done"]
        if done:
            self._step(1)
        self._last_saved_key = review["last_key"]  # Backspace takes back the whole run
        self.hint.setText(f"已排除 {done} 行；4 或退格一次全部撤回。" if done > 1
                          else "已排除此画面候选。" if done else "没有可排除的待定行。")

    def _review_seek(self, row, unseen):
        """Go to the first part of the row nobody has seen, else step on from here."""
        review = self._auto_review
        start, end = unseen[0]
        self.hint.setText(f"正在补看（第 {review['done'] + 1}/{review['total']} 行）："
                          f"还差 {sum(right - left for left, right in unseen) / 1000:.1f} 秒，"
                          "看完自动排除；按任意键停止。")
        self._review_timer.start()
        shown = self._shown_ms
        # Close before the part not seen yet (a jump that stopped at the end of
        # a short clip): stepping on reaches it, into the next file if need be.
        if shown is not None and (start - 300 <= shown <= end + WAVE_VIEW_REACH_MS
                                  or review["jumps"] and start - 3000 <= shown < start):
            # One step at a time: a decoded frame is reported more than once.
            if not review.get("stepping"):
                review["stepping"] = True
                QTimer.singleShot(REVIEW_STEP_DELAY_MS, self._review_step)
            return
        review["jumps"] += 1
        frame = self._review_frame(start, row) if review["jumps"] <= 6 else None
        if frame is None:
            self._stop_auto_review("这一段没有可补看的录像画面，这一行未排除；请手动看完再按 1 排除。")
            return
        self.jump_requested.emit(dict(frame, reference_only=True))

    def _review_frame(self, chip_ms, row):
        """The recording at CHIP_MS: from the race strip, else by offset in the row's own video.

        Recordings not yet calibrated have no strip (特奥, 10-28: grey), yet the
        row's own picture plays; its own file then serves the second around it.
        """
        pieces = self.race_strip.view.pieces
        frame = frame_at(pieces, chip_ms) if pieces else None
        own = row.item.evidence.get("frame") or {}
        if frame is None and own.get("video_path") and own.get("position_ms") is not None:
            frame = {key: own[key] for key in ("camera_index", "segment_id", "video_path") if key in own}
            frame.update(position_ms=max(0, int(own["position_ms"]) + chip_ms - row.time_ms), frame_index=None)
        return frame

    def _review_step(self):
        review = self._auto_review
        if review is not None:
            review["stepping"] = False
            review["idle_steps"] = review.get("idle_steps", 0) + 1
            self.hint.setText(f"正在补看（第 {review['done'] + 1}/{review['total']} 行），"
                              "看完自动排除；按任意键停止。")
            self.frame_step_requested.emit(REVIEW_STEP_FRAMES)

    def _advance_auto_review(self):
        """A frame was shown while 3 plays a row: go on, or exclude once all of it is seen."""
        review = self._auto_review
        if review["target"] is None:
            return  # between rows: the next one is opened outside this callback
        row = self._selected_arrival()
        if row is None or (row.key != review["target"]
                           and not any(piece.key == review["target"] for piece in row.fragments)):
            self._stop_auto_review("换了一行，自动补看已停止；这一行未排除。")
            return
        unseen = self._fragment_unseen(row, whole=True)
        if unseen:
            if self._shown_ms != review.get("shown"):
                review["shown"], review["idle_steps"] = self._shown_ms, 0
            if review.get("idle_steps", 0) >= 8:
                # Steps that no longer move the picture on: the recording ends here.
                self._stop_auto_review("这一段录像放到头了，补看不完，这一行未排除；请手动看完再按 1 排除。")
                return
            self._review_seek(row, unseen)
            return
        self._review_timer.stop()
        review["target"] = None
        QTimer.singleShot(0, self._next_auto_review)  # rule outside the frame callback

    def _exclude_seen(self, row, operation):
        """Exclude a row seen whole, on the frame shown if it is this row's, else the row's own."""
        frame = self._row_frame(row)
        if not self._frame_usable(frame):
            self.hint.setText("请先打开当前候选的有效录像画面。")
            return False
        if not self._save_ruling(row.item, [piece.item for piece in row.fragments], "not_rider", frame, operation):
            return False
        self._render()
        return True

    def _stop_auto_review(self, message):
        review, self._auto_review = self._auto_review, None
        self._review_timer.stop()
        if review is None:
            return
        if review["done"]:
            self._last_saved_key = review["last_key"]
            message = f"{message}前面已排除 {review['done']} 行，4 或退格一次全部撤回。"
        self.hint.setText(message)

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
        """A click on a person counts them; on a wave's picture it takes the next chip's bib at once.

        Someone already counted on this frame, those moved onto a 疑似漏读 row
        too, is not counted again (古浪 10-28: one rider, two 疑似漏读 rows).
        """
        item = self.current_item()
        ref = dict(frame or (item.evidence.get("frame") if item is not None else None) or {})
        if item is not None and any(owner != item.item_id and mark_on_frame(mark, ref, recorder_ms)
                                    and (mark["x"] - x) ** 2 + (mark["y"] - y) ** 2 < .02 ** 2
                                    for owner, mark in self.picture_marks()):
            self.hint.setText("这个人已点过（无芯片）；并排选手请分别点各自前轮。")
            return
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

        The picture stays where it is; 1 / 2 / 3, 4 and Backspace then act on this person.
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

        Runs on every click, right click, 1 and 3: each person counted is their
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
        """On to the next wave from its start, even if the end of this one was looked at nearly up to it.

        Never onto this wave's own 多出 / 无芯片 rows (Enter went round on its own
        多出 row, 10-29), nor onto a row 只看要处理的 hides.
        """
        plan = self._wave_plan(item)
        status = self._wave_status(plan)[0] if plan is not None else ""
        keys = {chip.key for chip in plan.chips} if plan is not None else set()
        positions = [i for i, row in enumerate(self._arrival_rows) if row.key in keys]
        following = next((i for i in range(max(positions) + 1, len(self._arrival_rows))
                          if not self.table.isRowHidden(i)
                          and self._arrival_rows[i].item.item_id != item.item_id
                          and self._arrival_rows[i].item.evidence.get("wave_parent") != item.item_id),
                         None) if positions else None
        if following is not None:
            self._open_row(following, keep=False)
        self.hint.setText((f"上一波：{status}。" if status else "已保存。")
                          + ("" if following is not None else "后面没有要看的了。"))

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

    def _take_back_no_chip(self, row):
        """撤回疑似漏读 on a picture of its own: back to 待定, its bib with it."""
        try:
            if row.entered is not None:
                self._arrival_store.undo(self._arrival_scope, key="bib:" + row.item.item_id)
            self._arrival_store.undo(self._arrival_scope, key="candidate:" + row.item.item_id)
        except (OSError, RuntimeError, ValueError) as error:
            self.hint.setText(f"撤回未保存：{error}")
            return
        self._quick_render_signature = None
        self._render()
        self.hint.setText("已撤回疑似漏读，这一行回到待定。")

    def _return_to_wave(self, row, exclude=False):
        """4 again (or Backspace) on someone split off a wave: they count for its chips again.

        With EXCLUDE (3) they go back as 非运动员 instead, still drawn on the wave.
        """
        child = row.item
        parent = next((item for item in self.items if item.item_id == child.evidence.get("wave_parent")), None)
        try:
            if row.entered is not None:
                self._arrival_store.undo(self._arrival_scope, key="bib:" + child.item_id)
            self._arrival_store.undo(self._arrival_scope, key="candidate:" + child.item_id)
        except (OSError, RuntimeError, ValueError) as error:
            self.hint.setText(f"撤回未保存：{error}")
            return
        moved = []
        if parent is not None:
            owned = self._marks.setdefault(parent.item_id, [])
            for mark in self._marks.pop(child.item_id, []):
                mark = {key: value for key, value in mark.items() if key not in ("no_chip", "wave_operation")}
                if exclude:
                    mark["exclusion_reason"] = "非运动员"
                owned.append(mark)
                moved.append(mark)
        self.save_drafts()
        plan = self._wave_plan(parent) if parent is not None else None
        if plan is None or not self._save_wave(plan):
            self._quick_render_signature = None
            self._render()
        # Pick them out again where they now are: their chip's row, or their 多出 row.
        position = next((i for i, other in enumerate(self._arrival_rows) for mark in moved
                         if other.mark is mark or other.decision and self._counted_here(other, parent)
                         and self._decision_holds(other.decision, mark)), None)
        if position is not None:
            self._arrival_clicked(position)
        self._sync_arrival_confirmation()
        self.marks_changed.emit(tuple(self.current_marks()))
        self.hint.setText("已排除这一位（非运动员）。" if exclude else
                          "已撤回无芯片：他回到这一波，按芯片顺序占号。" if moved else
                          "已撤回疑似漏读；这一波的画面已失效，请重新打开这一波。")

    def _row_by_key(self, key):
        return next((row for row in self._arrival_rows if row.key == key), None)

    def _carrier_text(self, row):
        """What a 双芯片 row says: whose second chip it is."""
        return (f"{row.bib} 号芯片由 {row.carrier} 号携带（一人双芯片）" if row.carrier
                else f"{row.bib} 号记为双芯片（多余芯片）")

    def _ask_carrier(self, bib, current):
        text, ok = QInputDialog.getText(self, "双芯片", f"{bib} 号芯片是谁带的？核对后填携带人的号码：",
                                        text=current)
        return text if ok else None

    def _mark_extra_chip(self, key):
        """双芯片 (right click on a chip row): the chip is a second chip of the rider with the bib given.

        Asked from the row alone, without the picture (10-29: "直接点 8 号右键
        双芯片"); the carrier's bib is kept with it and exported.  The wave's
        people move on to the chips left.  Asked again it changes the carrier.
        """
        row = self._row_by_key(key)
        if row is None or row.event is None or not row.event.is_active or self._arrival_store is None:
            self.hint.setText("双芯片用在左边的芯片行：选中那条多出来的芯片按 2（或右键）。")
            return
        plan = self._counted_plan(row)
        if row.decision is not None and not (plan is not None and self._counted_here(row, plan.item)):
            self.hint.setText(f"{row.bib} 号已在别处确认；要改先撤销这一行。")
            return
        editing = row.note == "多余芯片"
        carrier = self._ask_carrier(row.bib, row.carrier if editing else "")
        if carrier is None:
            return
        carrier = carrier.strip()
        if not carrier:
            self.hint.setText("没有填携带人，未改动。")
            return
        if carrier == row.bib:
            self.hint.setText(f"携带人不能是 {row.bib} 号自己：填带着这条芯片的那个人的号码。")
            return
        scope = self._arrival_scope
        # The frame of the carrier where he was counted, if he was: the evidence of who wore it.
        holder = next((other for other in self._arrival_rows if other.event is not None and other.bib == carrier
                       and other.decision and (other.decision.get("frame") or {}).get("video_path")), None)
        frame = dict(holder.decision["frame"]) if holder is not None else (
            dict(plan.item.evidence.get("frame") or {}) if plan is not None else {})
        try:
            if self._arrival_store.latest(scope, key) is not None:
                self._arrival_store.undo(scope, key=key)  # its person, or the old carrier, gives way
            self._arrival_store.save(scope, key, event_binding(row.event), "held", reason=EXTRA_CHIP,
                                     event_id=row.event.event_id, bib=row.bib, carrier_bib=carrier,
                                     candidate_id=plan.item.item_id if plan is not None else "",
                                     frame=frame, calibration_version=plan.item.calibration_version
                                     if plan is not None else "")
        except (OSError, RuntimeError, ValueError) as error:
            self.hint.setText(f"未保存：{error}")
            return
        bib = row.bib
        self._after_chip_change(key)
        wave = self._row_wave(self._row_by_key(key)) or ()
        text = f"{bib} 号芯片记为 {carrier} 号带的（一人双芯片），导出到建议删除清单；按 2 改携带人，4 撤回。"
        if carrier not in {chip.bib for chip in wave}:
            text += f"注意：{carrier} 号不在这一波的芯片里，请核对号码。"
        self.hint.setText(text)

    def _take_back_extra_chip(self, key):
        row = self._row_by_key(key)
        if row is None or row.note != "多余芯片" or self._arrival_store is None:
            return
        try:
            self._arrival_store.undo(self._arrival_scope, key=key)
        except (OSError, RuntimeError, ValueError) as error:
            self.hint.setText(f"撤回未保存：{error}")
            return
        bib = row.bib
        self._after_chip_change(key, saved=False)
        self.hint.setText(f"已撤回：{bib} 号不再记为双芯片。")

    def _after_chip_change(self, key, saved=True):
        """A chip of a wave turned 双芯片 or back: the people counted line up with the chips left."""
        self._quick_render_signature = None
        self._render()
        plan = self._counted_plan(self._row_by_key(key))
        if plan is not None:
            self._save_wave(plan)
        self._select_key(key)
        self._last_saved_key = key if saved else None

    def _select_key(self, key):
        position = next((i for i, other in enumerate(self._arrival_rows) if other.key == key), None)
        if position is not None:
            self._selected_key = key
            self.table.setCurrentCell(position, 0)
            self._sync_arrival_confirmation()

    def _enter_bib(self, row):
        """Enter on a 疑似漏读 row: the bib read off the frame; it goes to the 补录 list."""
        if row is None or row.event is not None or self._row_state(row) not in ("疑似漏读", "补录"):
            self.hint.setText("补录号码用在左边的疑似漏读行：选中按 3（或右键）。")
            return
        bib = self._ask_bib(row.bib if row.entered is not None else "")
        if bib is None:
            return
        bib = bib.strip()
        if bib:
            self._save_bib(row, bib)
        elif row.entered is not None:
            self._undo_bib(row)

    def _ask_bib(self, current):
        text, ok = QInputDialog.getText(self, "填写号码", "核对原帧上的号码后填写（导出到补录清单）：", text=current)
        return text if ok else None

    def _save_bib(self, row, bib):
        if row.time_ms is not None:
            chip = next((other for other in self._arrival_rows if other.event is not None and other.event.is_active
                         and other.bib == bib and other.note != "多余芯片"
                         and abs(int(other.event.timeline_timestamp_ms) - int(row.time_ms)) <= BIB_CHIP_REACH_MS),
                        None)
            if chip is not None:
                self.hint.setText(f"{bib} 号在 {format_chip_time(chip.event.timeline_timestamp_ms)} 有芯片，不是漏读："
                                  "号码看错就重填；确实是他，就右键这一行「撤回疑似漏读」，让他占这个芯片。")
                return
        twice = next((other for other in self._arrival_rows
                      if other is not row and other.entered is not None and other.bib == bib), None)
        if twice is not None:
            self.hint.setText(f"{bib} 号已在 {format_chip_time(twice.time_ms)} 补录过：同一人只补录一次；"
                              "要改先在那一行按 4 撤回。")
            return
        key = "bib:" + row.item.item_id
        frame = dict(row.item.evidence.get("frame") or {})
        frame.setdefault("calibration_version", row.item.calibration_version)
        try:
            if row.entered is not None:
                self._arrival_store.undo(self._arrival_scope, key=key)
            self._arrival_store.save(self._arrival_scope, key, candidate_binding(row.item), "held",
                                     reason=RECORD_BIB, bib=bib, candidate_id=row.item.item_id,
                                     crossing_ms=row.time_ms, frame=frame,
                                     calibration_version=row.item.calibration_version)
        except (OSError, RuntimeError, ValueError) as error:
            self.hint.setText(f"号码未保存：{error}")
            return
        self._quick_render_signature = None
        self._render()
        self._select_key(row.key)
        self._last_saved_key = key
        self.hint.setText(f"已记 {bib} 号补录（导出到补录清单）；按 3 改号码，4 撤回。")

    def _undo_bib(self, row):
        try:
            self._arrival_store.undo(self._arrival_scope, key="bib:" + row.item.item_id)
        except (OSError, RuntimeError, ValueError) as error:
            self.hint.setText(f"撤回未保存：{error}")
            return
        self._quick_render_signature = None
        self._render()
        self._select_key(row.key)
        self.hint.setText("已撤回号码，这一行回到疑似漏读。")

    def _candidate_no_chip(self, row):
        """A picture without a chip of its own shows a rider: a 疑似漏读 row, kept for the operator."""
        # The frame shown only if it is this row's: a right click may come from anywhere.
        frame = self._row_frame(row)
        members = [row.item, *(piece.item for piece in row.fragments)]
        try:
            for member in members:
                # Saved as a rider picture with no chip; one already marked a
                # rider is taken back first, or the flag would not be kept.
                if (self._category(member) or {}).get("action") == "bicycle":
                    self._arrival_store.undo(self._arrival_scope, key="candidate:" + member.item_id)
        except (OSError, RuntimeError, ValueError) as error:
            self.hint.setText(f"审核未保存：{error}")
            return
        if not self._save_ruling(row.item, members[1:], "bicycle", frame, uuid.uuid4().hex, no_chip=True):
            return
        self._render()
        self._last_saved_key = "candidate:" + row.item.item_id
        self.hint.setText("已列为疑似漏读（无芯片）：核对原帧号码后按 3 补录号码；4 撤回。")

    def _flag_mark(self, flag, index=None):
        """Toggle 无芯片 / 非运动员 on a counted person; saved at once.

        INDEX: the person picked by their row on the left (right click); else
        the one last clicked, only while on the frame shown.
        """
        item = self.current_item()
        marks = self._marks.get(item.item_id, []) if item is not None else []
        frame = self._frame_provider() or {}
        picked = index is not None
        index = self._editing_mark if index is None else index
        if not (0 <= index < len(marks)
                and (picked or mark_on_frame(marks[index], frame, frame.get("recorder_ms")))):
            self.hint.setText("先点中当前画面上的那个人。")
            return
        mark = marks[index]
        decided = self._decided_row(mark, item)
        if decided is not None and not self._counted_here(decided, item):
            self.hint.setText(f"这一位已在别处确认为 {decided.bib}；要改先按 4 撤销。")
            return
        self._editing_mark = index
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
                children = self._split_wave_people(item, [mark], uuid.uuid4().hex)
                self._editing_mark = -1
                plan = self._wave_plan()
                if children:
                    # Selected, on the wave's picture: 4 or Backspace takes it back.
                    self._selected_key = "candidate:" + children[0][0]
                    text = "已标无芯片（疑似漏读，不占号），后面的号码顺延；按 4 撤回。"
            if plan is not None:
                self._save_wave(plan)
        if self._editing_mark < 0:
            self._select_key(self._selected_key)
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
                # A 无芯片 person: their 疑似漏读 row, and a bib entered for it, go with them.
                if self._arrival_store.latest(self._arrival_scope, "bib:" + owner) is not None:
                    self._arrival_store.undo(self._arrival_scope, key="bib:" + owner)
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
        if row is not None and row.entered is not None:
            self._undo_bib(row)
            return
        if row is not None and row.note == "多余芯片":
            self._take_back_extra_chip(row.key)
            return
        if (row is not None and row.event is None and row.item.evidence.get("wave_parent")
                and self._row_state(row) == "疑似漏读"):
            self._return_to_wave(row)
            return
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
        self.hint.setText(f"已撤销{undone}；可重新点人或按 1/2/3 重判。" if undone
                          else "这一行没有可撤销的判定。")

    def _step(self, delta):
        # One step per picture, from the selected row (not the open picture,
        # which may be a wave's counting picture without a row of its own).
        # A 多出 row is opened by clicking it, never stepped onto.
        first = {}
        for position, row in enumerate(self._arrival_rows):
            if row.mark is None and not self.table.isRowHidden(position):
                first.setdefault(row.item.item_id, position)
        items = list(first)
        selected = self._selected_arrival()
        if selected is None or selected.mark is None and selected.item.item_id in first:
            index = (items.index(selected.item.item_id) if selected is not None else -1) + delta
        else:  # from a 多出 row: the picture either side of it
            before = sum(position < self.table.currentRow() for position in first.values())
            index = before if delta > 0 else before - 1
        if 0 <= index < len(items):
            self._arrival_clicked(first[items[index]])

    def _queue_key(self, event):
        if event.isAutoRepeat():
            return event.key() in NUMBER_KEYS + (Qt.Key_Backspace,)
        if self._auto_review is not None and event.key() not in (Qt.Key_Shift, Qt.Key_Control, Qt.Key_Alt):
            self._stop_auto_review("已停止自动补看，这一行未排除。")
            return True
        if event.key() in NUMBER_KEYS + (Qt.Key_NumberSign,) and not event.modifiers() & (
                Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier):
            # 1 / 2 / 3 act on the selected row, 4 undoes (10-29); 5 and # are no keys any more.
            if event.key() in ROW_KEYS:
                self._row_key(event.key())
            elif event.key() == Qt.Key_4:
                self._undo_arrival()
            else:
                self.hint.setText("这个键不用了：1 不是运动员、2 双芯片、3 补录号码、4 撤销。")
            return True
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
        # The picture counts, 1 / 2 / 3 or the row's right click settles; undo is 4, Backspace or Ctrl+Z.
        actions = {Qt.Key_Backspace: self._undo_arrival,
                   Qt.Key_Return: self._enter_key, Qt.Key_Enter: self._enter_key,
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
            self.hint.setText("左键点压线的人即保存，右键去掉；1 不是运动员、2 双芯片、3 补录、4 撤销。Esc 返回。")
            self.setFocus()
        else:
            self.hint.setText("已返回胶卷视图；左键点人即保存；1 不是运动员、2 双芯片、3 补录、4 撤销。")
            self.table.setFocus()
        self.big_view_changed.emit(self._big_view)

    def _attention(self, row, outdated, counted):
        """What is left at the end: pending, done (to export), or nothing to do (None)."""
        state = self._row_state(row)
        if state in ("补录", "多余芯片"):
            return "done"
        wave = self._wave_of.get(row.key) if row.event is not None else None
        if wave is not None and wave in outdated:
            return "芯片有变化"  # saved before a chip came in: Enter on the wave saves it in chip order
        if (state in ("疑似漏读", "多出", "疑似双芯片") or row.stale
                or state == "待定" and (row.event is None or wave in counted)):
            return state
        return None

    def _sync_attention_button(self, attention):
        pending = Counter(need for need in attention if need not in (None, "done"))
        done = Counter(self._row_state(row) for row, need in zip(self._arrival_rows, attention) if need == "done")
        total = sum(pending.values())
        self.attention_button.setText(f"只看要处理的 {total}" if total else "只看要处理的")
        parts = [f"{state} {count}" for state, count in pending.items()]
        tooltip = ("要处理：" + "、".join(parts) if parts else "没有要处理的行") + (
            "\n已处理（会导出）：" + "、".join(f"{state} {count}" for state, count in done.items()) if done else "")
        idle = sum(row.event is not None and row.event.is_active and row.decision is None and need is None
                   and self._row_state(row) == "待定" for row, need in zip(self._arrival_rows, attention))
        if idle:
            tooltip += f"\n还没点人的波里有 {idle} 个芯片没对上人（不算在内）"
        self.attention_button.setToolTip(
            tooltip + "\n选中左边这一行按键处理（右键同样）：疑似漏读按 3 补录号码，疑似双芯片按 2 填是谁带的，多出按 3 补录或 1 不是运动员")

    def _set_attention_only(self, on):
        self._attention_only = bool(on)
        self._quick_render_signature = None
        self._render()
        row = self._selected_arrival()
        if on and (row is None or self.table.isRowHidden(self.table.currentRow())):
            position = next((i for i in range(len(self._arrival_rows)) if not self.table.isRowHidden(i)), None)
            if position is not None:
                self._arrival_clicked(position)
            else:
                self.hint.setText("没有要处理的了：可以在「更多」里导出给计时系统。")

    def _arrival_exports(self):
        """补录 rows (a person without a chip) and 建议删除 rows (多余芯片) the arrival list settled."""
        clocks = workspace_clocks(self.workspace_dir) if self.workspace_dir else None
        sources = {"aligned": "校时换算（视频原帧）", "video": "未校时：录像原时刻"}
        manual, deletions, blank = [], [], 0
        for row in self._arrival_rows:
            state = self._row_state(row)
            if state in ("补录", "疑似漏读") and row.event is None:
                frame = (row.entered or {}).get("frame") or row.item.evidence.get("frame") or {}
                crossing = (item_crossing_time(row.item, self.workspace_dir, clocks=clocks)
                            if self.workspace_dir else None)
                blank += state == "疑似漏读"
                manual.append((row.bib if state == "补录" else "", format_chip_time(row.time_ms)
                               if row.time_ms is not None else "", _frame_text(frame), frame.get("camera_index", ""),
                               row.item.calibration_version,
                               "到达记录：画面有人无芯片" + ("" if state == "补录" else "；号码未填，请核对原帧"),
                               sources.get(crossing.source, "参考时间：请核对原帧") if crossing else "未换算：按原帧人工判断"))
            elif state == "多余芯片":
                entry = self._arrival_store.latest(self._arrival_scope, row.key) or {}
                frame = entry.get("frame") or {}
                event = row.event
                reason = (f"到达记录：{row.bib} 号芯片由 {row.carrier} 号携带（一人双芯片），"
                          f"{row.carrier} 号成绩不受影响" if row.carrier
                          else "到达记录：本波看完，人数比芯片少，这条芯片没有对应的人")
                deletions.append((row.bib, format_chip_time(event.timeline_timestamp_ms),
                                  _passage_number(event.event_id), "一人双芯片", reason, event.event_id,
                                  getattr(event, "revision", ""), "", _frame_text(frame),
                                  entry.get("calibration_version", ""), json.dumps(entry, ensure_ascii=False),
                                  row.carrier))
        return manual, deletions, blank

    def export(self):
        """The three lists for the timing operator, with what the arrival list settled in them too."""
        if self.store is None or self.problem_dir is None:
            return None
        try:
            self.refresh()
            manual, deletions, blank = self._arrival_exports()
            counts = export_lists(self.items, self.store, self.problem_dir / "exports",
                                  events=tuple(self._events_provider()),
                                  manual_extra=manual, deletions_extra=deletions)
        except (OSError, RuntimeError, ValueError) as error:
            self.hint.setText(f"导出失败：{error}")
            return None
        text = "已导出到 " + str(self.problem_dir / "exports") + "：" + "，".join(
            f"{name.removesuffix('.csv')} {count} 条" for name, count in counts.items())
        if blank:
            text += f"；其中 {blank} 条疑似漏读还没填号码"
        self.hint.setText(text)
        return counts

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
