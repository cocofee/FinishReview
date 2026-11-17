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
    QMenu, QPlainTextEdit, QPushButton, QSizePolicy, QTextEdit, QVBoxLayout, QWidget,
)

from .auto_label import WAVE_REACH_MS, label_wave, people_on_line
from .arrival_review import (
    ARRIVAL_REVIEWS_FILENAME, ArrivalReviewStore, candidate_binding, event_binding,
)
from .extra_person import fit_wave
from .bunch_review import add_viewed, is_excluded, mark_on_frame, rider_count, uncovered_intervals
from .chip_history import format_chip_time
from .crossing_time import item_crossing_time, manual_clock_offsets, recorder_crossing_time, workspace_clocks
from .lap_wave import Passage, short_riders
from .problem_items import (
    CATEGORY_VIDEO, KIND_CHIP_NO_CROSSING, KIND_MANUAL_REVIEW, KIND_NO_CHIP_CROSSING,
    STATUS_EXCLUDED,
    STATUS_WAITING, ProblemItem, stable_item_id,
)
from .problem_panel import ProblemListPanel
from .wave_slots import DONE, ELSEWHERE, EXTRA, NEXT, NO_CHIP, TODO, Slot, WaveSlotRow
from .problem_resolutions import _frame_text, _passage_number, export_lists
from .race_strip import (AUTO_LABELLED, RaceStripPanel, earlier_arrival, first_crossing, frame_at, line_next_entry,
                         line_arrivals)
from .quick_review_common import (
    AUTO_GO_ON,
    AUTO_PICTURE_REASON,
    AUTO_TEXT,
    ArrivalRow,
    BIB_CHIP_REACH_MS,
    CLOSE_BEHIND_MS,
    COUNTED_REACH_MS,
    EARLIER_BATCH,
    EXCLUSION_RUN_GAP_MS,
    EXTRA_CHIP,
    FRAGMENT_REACH_MS,
    GHOST_REACH_MS,
    KEEP,
    KEY_NAMES,
    LOCAL_CHIP_EXCLUSION,
    NEAR_COUNTED_TEXT,
    NUMBER_KEYS,
    PASSAGE_VIEW_MARGIN_MS,
    RECORD_BIB,
    REVIEW_STALL_MS,
    REVIEW_STEP_DELAY_MS,
    REVIEW_STEP_FRAMES,
    ROW_KEYS,
    RUN_FAST_MIN_MS,
    RUN_FAST_SPEED,
    RUN_NEAR_MS,
    RUN_SLOW_MS,
    RUN_STALE_MS,
    RUN_STEP_MS,
    RUN_STOP_LEAD_MS,
    RUN_TAIL_MS,
    SAME_BODY_MS,
    SAME_FRAME_MS,
    SWEEP_REACH_MS,
    UNVERIFIED_SPAN_MS,
    WAVE_ALERT_WORDS,
    WAVE_GAP_MS,
    WAVE_NO_PERSON,
    WAVE_VIEW_MARGIN_MS,
    WAVE_VIEW_REACH_MS,
    WavePlan,
    row_moment,
)
from .quick_review_rows import QuickReviewRowsMixin
from .quick_review_display import QuickReviewDisplayMixin
from .quick_review_waves import QuickReviewWavesMixin
from .quick_review_play import QuickReviewPlayMixin


class QuickReviewPanel(
    QuickReviewRowsMixin,
    QuickReviewDisplayMixin,
    QuickReviewWavesMixin,
    QuickReviewPlayMixin,
    ProblemListPanel,
):
    quick_review = True
    confirmation_preparing = pyqtSignal()
    candidate_preparing = pyqtSignal()
    sampling_configured = pyqtSignal()
    sampling_preparing = pyqtSignal()
    # A chip record the operator wants gone (spare chip, misread); the window asks why.
    delete_requested = pyqtSignal(str)
    # 校时: the window learns the video offset from the current frame and a chip record.
    calibration_requested = pyqtSignal()
    run_speed = pyqtSignal(float)               # how fast the run plays (RUN_FAST_SPEED)
    # True: play the recording on from the frame shown; False: hold it (see RUN_NEAR_MS).
    run_requested = pyqtSignal(bool)

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
        self._table_builds = 0  # times the table was filled again (a click fills it once, not twice)
        self._frame_provider = lambda: None
        self._shown_ms = None
        self._landing_pending = None
        # A 不是运动员 (1, or the run from the right click) still playing the unseen part of its rows
        # before excluding them.
        self._auto_review = None
        # The chip-clock moment the recording is playing on to after a click,
        # whether that is the wave's tail (nobody left to count before it), and
        # the moment of the click a run is still to start from.
        self._run_to = None
        self._run_fast, self._run_swept = False, False
        self._run_between = None
        self._run_tail = False
        self._run_from = None
        self._clicked_at = None  # chip time of the person just clicked (see CLOSE_BEHIND_MS)
        self._close_behind_ms = CLOSE_BEHIND_MS
        self._run_counted = None  # mark index a run to someone already clicked lands on
        self._offset_cache = None  # (key, _run_offset result): one Space asks for it several times
        # 只看要处理的: the table shows only the rows still to settle, and those
        # settled for the timing system (补录, 多余芯片).
        self._attention_only = False
        self._show_excluded = False
        self._show_unverified = False
        self._unverified_items = frozenset()
        self._unverified_groups = 0
        # True while a row is opened whose picture the panel itself places (or
        # keeps): the window must not reopen the item's stored frame over it.
        self.holds_picture = False
        super().__init__(parent)
        self.jump_requested.connect(self._forget_run)  # a jump elsewhere ends any run
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
        self._short_bibs = ()  # riders whose last read is short of the target, once the group has a finisher
        # 自动对上 (auto_label): chip row key -> the person the analysis saw cross
        # for it, on waves nobody has counted yet.  Shown only, never saved.
        self._auto_labels = {}
        self._auto_reasons = {}  # wave index -> why it was not paired (shown on its rows)
        self._auto_landing_key = None  # only keep a paired wave after opening its bound evidence
        self._earlier_shown = frozenset()  # the 可能多人 the table shows
        self._near_counted_times = ()  # their moments: hidden from the table, still a doubt for 自动对上
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
        self.table.setToolTip("点一行看这一刻；有人压线就在画面上点他，滚轮前后一帧；选中后按 1 不是运动员、2 双芯片、3 补录号码、4 撤销（右键同样）")
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
        toolbar_layout = QVBoxLayout(self.quick_toolbar)
        toolbar_layout.setContentsMargins(2, 3, 2, 3)
        toolbar_layout.setSpacing(3)
        self.status_widget = QWidget(self.quick_toolbar)
        self.status_bar = QHBoxLayout(self.status_widget)
        self.status_bar.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        toolbar_layout.addLayout(bar)
        bar.addWidget(self.status_widget, 1)
        self.quick_title = QLabel("到达记录", column)
        column_layout.insertWidget(0, self.quick_title)
        # 校时 is the first thing done at every recording: until it is done every
        # chip mark sits seconds away from its rider (11-03: 3.2 s).  It used to
        # hide under the picture, behind 查找原录像.
        self.calibrate_button = QPushButton("先校时", self.quick_toolbar)
        self.calibrate_button.clicked.connect(self.calibration_requested.emit)
        bar.addWidget(self.calibrate_button)
        self._calibration_needed = None
        self.row_action_buttons = {}
        for key, label in ((Qt.Key_1, "1 非运动员"), (Qt.Key_2, "2 双芯片"), (Qt.Key_3, "3 补录")):
            button = QPushButton(label, self.quick_toolbar)
            button.setToolTip("处理左侧选中行")
            button.clicked.connect(lambda _checked=False, value=key: self._row_key(value))
            self.row_action_buttons[key] = button
            bar.addWidget(button)
        self.defer_button = QPushButton("暂放", self.quick_toolbar)
        self.confirm_arrival_button = QPushButton("确认过线", self.quick_toolbar)
        self.confirm_arrival_button.setToolTip(
            "先选左侧运动员，再将画面停在其前轮压终点线的一帧，最后点击确认过线"
        )
        self.quick_undo_button = QPushButton("4 撤销", self.quick_toolbar)
        for button in (self.defer_button, self.confirm_arrival_button, self.quick_undo_button):
            bar.addWidget(button)
        self.confirm_arrival_button.hide()
        self.defer_button.hide()
        self.quick_undo_button.setToolTip("4 或退格：撤销所选这一行的判定；刚判完自动跳到下一行时，撤销刚才那一条")
        self.defer_button.clicked.connect(lambda: self._classify("held"))
        self.confirm_arrival_button.clicked.connect(self._confirm_selected_arrival)
        self.quick_undo_button.clicked.connect(self._undo_arrival)
        more = self.more_button = QPushButton("更多", self.quick_toolbar)
        menu = QMenu(more)
        menu.addAction("终点线 / 模型", self._configure_sampling)
        self.show_excluded_action = menu.addAction("显示已排除")
        self.show_excluded_action.setCheckable(True)
        self.show_excluded_action.toggled.connect(self._set_show_excluded)
        self.show_unverified_action = menu.addAction("显示待核画面（不计到达）")
        self.show_unverified_action.setCheckable(True)
        self.show_unverified_action.toggled.connect(self._set_show_unverified)
        menu.addSeparator()
        menu.addAction("分析录像", self._toggle_analysis)
        menu.addAction("导出给计时系统（补录 / 建议删除）", self.export)
        menu.addAction("删除选中的芯片记录…", self._request_delete)
        menu.addSeparator()
        menu.addAction("清空试验数据", self.clear_data_requested.emit)
        more.setMenu(menu)
        bar.addWidget(more)
        self.layout().insertWidget(0, self.quick_toolbar)
        # Judging actions stay visible; routine pipeline details belong in hover text.
        self.finish_line_button = QPushButton("设置终点线", self.quick_toolbar)
        self.finish_line_button.setStyleSheet("color:#9a5b12; font-weight:600;")
        self.finish_line_button.setToolTip(
            "绘制本机位终点线，选择本机 YOLOX ONNX 模型并试运行；只分析已封存录像"
        )
        self.finish_line_button.clicked.connect(self._configure_sampling)
        self.finish_line_status = QLabel("未设终点线", self.quick_toolbar)
        self.confirmation_status = QLabel("", self.quick_toolbar)
        self.confirmation_status.setStyleSheet("color:#374151;")
        self.confirmation_status.setMinimumWidth(0)
        self.confirmation_status.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.status_bar.addWidget(self.confirmation_status, 1)
        self.finish_line_status.hide()  # Missing configuration is an actionable button, not duplicate text.
        self.lap_status = QLabel("", self.quick_toolbar)
        self.lap_status.setStyleSheet("color:#9a5b12; font-weight:600;")
        self.status_bar.addWidget(self.lap_status)
        bar.addWidget(self.finish_line_button)
        self.race_strip = RaceStripPanel(self)
        self.race_strip.seek_requested.connect(self._seek_race_strip)
        self.race_strip.ready.connect(self._strip_ready)
        self.layout().insertWidget(1, self.race_strip)
        # The head count; the window puts it above the picture, in the place of 已过 N 人 (see wave_slots).
        self.wave_slots = WaveSlotRow(self)
        self.wave_slots.mark_clicked.connect(self._slot_clicked)
        self._slot_cache, self.slot_cache_hits = None, 0
        self._target_row_state, self._target_cells = None, []
        self.layout().insertWidget(2, self.wave_slots)
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
                # Every lap keeps its chip row, without duplicating the bound picture.
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
                    if (previous["action"] == "held" and previous.get("reason") == LOCAL_CHIP_EXCLUSION
                            and previous["binding"] == event_binding(row.event)):
                        row.note = "已排除"
                        continue
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
        self._unverified_items = frozenset(row.item.item_id for row in rows
                                          if row.event is None and self._unverified_picture(row.item))
        rows = self._fold_unverified(rows, assigned, counting)
        self._unverified_groups = sum(self._row_state(row) == "画面待核" for row in rows)
        rows = [row for row in rows if row.event is not None or self._candidate_shown(row.item, assigned, counting)]
        # 11-15: a crossing the analysis saw within COUNTED_REACH_MS of someone
        # counted is no longer a row (it was 已点附近: "这样会造成困惑").  People
        # are counted one by one now and Space stops on whoever comes onto the
        # line right behind (line_next_entry).  Nothing on disk changes.
        near = frozenset(row.item.item_id for row in rows
                         if row.event is None and self._near_counted_moment(row.item, assigned, moments))
        # Still a doubt for 自动对上: a wave with such a crossing is left to hand counting.
        self._near_counted_times = tuple(row.time_ms for row in rows
                                         if row.item.item_id in near and row.time_ms is not None)
        self._near_counted_items = frozenset()
        rows = self._without_repeats(rows)
        # Folded first, then hidden: the later pieces of a hidden crossing go with
        # it instead of standing as rows of their own beside the counted rider.
        rows = self._fold_fragments(rows, assigned)
        rows = [row for row in rows if row.item.item_id not in near or row.event is not None]
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
                      and other.note != "已排除"
                      and (other.event is None or other.event.is_active)
                      and (other.time_ms is not None or other.event is not None)]
            others += [piece.time_ms for other in self._arrival_rows if other is not row
                       for piece in other.fragments if piece.time_ms is not None]
            self._earlier[key] = earlier_arrival(pieces, self.race_strip.view.cache, int(row.time_ms), others)
            if self._earlier[key] is not None and not self._earlier_timer.isActive():
                self._earlier_timer.start()  # read on opening the row: the table shows it too
        return self._earlier.get(key)

    def _auto_label_waves(self, plans):
        """自动对上: pair each uncounted wave's chips with the people the analysis saw cross for it.

        Only where every condition of auto_label.label_wave holds: as many
        people as chips, timed, apart enough, and no 可能多人
        or 已点附近 close by.  Any doubt leaves the whole wave to hand.  Nothing
        is saved until Enter explicitly accepts the bound evidence; its automatic
        origin remains distinct from manual clicks. 4 withdraws the proposal.
        """
        self._auto_labels, self._auto_reasons = {}, {}
        if not self.workspace_dir or not self._waves:
            return
        people = people_on_line(self.workspace_dir, int(self.race_strip.camera or 1))
        if not people:
            return
        by_key = {person.key: person for person in people}
        withdrawn = set(self._arrival_drafts.get("auto_withdrawn", ()))
        loose = [row for row in self._arrival_rows if row.event is None and row.time_ms is not None]
        for index, wave in enumerate(self._waves):
            plan = plans.get(index)
            if plan is not None and (plan.marks or plan.split):
                continue  # counted by hand
            if self._auto_key(wave) in withdrawn:
                self._auto_reasons[index] = ("已按 4 撤回",)
                continue
            if any(row.decision or row.note or row.stale or row.entered for row in wave):
                continue
            stamps = [int(row.event.timeline_timestamp_ms) for row in wave]
            start, end = min(stamps) - WAVE_REACH_MS, max(stamps) + WAVE_REACH_MS
            near = [row for row in loose if start <= row.time_ms <= end]
            match = label_wave(
                [(row.key, stamp) for row, stamp in zip(wave, stamps)], people, clock_ok=True,
                maybe_several=any(self._earlier_arrival(row, read=False) is not None for row in near),
                near_counted=any(start <= moment <= end for moment in self._near_counted_times))
            if match.reasons:
                self._auto_reasons[index] = match.reasons
                continue
            for chip_key, person_key in match.pairs:
                self._auto_labels[chip_key] = by_key[person_key]

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
            moment = row_moment(row)
            while extra and moment is not None and extra[0].time_ms < moment:
                merged.append(extra.pop(0))
            merged.append(row)
        return merged + extra

    def queue_text(self):
        if self._unverified_items and not any(row.event is not None or not self._unverified_picture(row.item)
                                              for row in self._arrival_rows):
            return f"尚未确认目标穿越 · 待核画面 {self._unverified_groups} 组（{len(self._unverified_items)} 段，不计到达）"
        return super().queue_text()

    def _landing(self, row, keep=True):
        """Where the picture goes when this row is opened; the one rule for every way in.

        A decided row uses its saved frame. A proposed automatic wave opens its
        first paired person's exact frame; member changes keep the picture.
        Unpaired chips use the strip's first crossing without playback padding,
        falling back to chip time. Video-only passages use their small lead, or
        the earlier arrival when several people may share one crossing.
        KEEP means stay; None means the row's stored frame or chip reference.
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
        first, last = min(stamps), max(stamps)
        wave = self._row_wave(row) or ()
        paired = [self._auto_labels[chip.key] for chip in wave if chip.key in self._auto_labels]
        visible = self._shown_ms is not None and first - WAVE_GAP_MS / 2 <= self._shown_ms <= last + WAVE_GAP_MS / 2
        if paired and len(paired) == len(wave):
            binding = (str(self.workspace_dir), self._arrival_scope,
                       tuple((chip.key, self._auto_labels[chip.key].key) for chip in wave))
            if keep and visible and self._auto_landing_key == binding:
                return KEEP
            self._auto_landing_key = binding
            # Same evidence as the labels and acceptance, not another strip search.
            person = min(paired, key=lambda entry: entry.chip_ms)
            return dict(person.frame, reference_only=True, auto_pair=True)
        if keep and visible:
            return KEEP
        if not self.race_strip.view.pieces:
            self._landing_pending = row.key  # unpaired reference awaits the strip
            return None
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
                or self._row_state(row) not in ("待定", "画面待核")):
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

    def _sync_timeline(self):
        if not self._quick_ready:
            return super()._sync_timeline()
        # The whole-race picture replaces the short group ruler here.  Keep
        # its current moment for N / Shift+N, without loading a second image.
        self.timeline.hide()
        self.timeline_help.hide()

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
        if row is None or not self._excludable(row) or self._row_state(row) == "画面待核":
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
        clocks = workspace_clocks(self.workspace_dir) if self.workspace_dir else None
        key = (item.item_id, id(chips), len(self.items), id(clocks),
               tuple((mark["recorder_ms"], mark["x"], mark["y"], bool(mark.get("no_chip")),
                      mark.get("exclusion_reason", ""), (mark.get("frame") or {}).get("session_id", ""))
                     for mark in marks))
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
        def crossing_order(index):
            mark = marks[index]
            crossing = recorder_crossing_time(mark["recorder_ms"], mark.get("frame") or {},
                                              self.workspace_dir, clocks=clocks)
            return (crossing.value_ms if crossing is not None else int(mark["recorder_ms"]), index)

        ordered = sorted((i for i, mark in enumerate(marks) if i not in plan.labels and not is_excluded(mark)),
                         key=crossing_order)
        counting = [i for i in ordered if not marks[i].get("no_chip")]

        def chip_ms(row):
            # The chip's own time.  A row already saved from a click carries that
            # click's moment as its time_ms; fitting against it would just give
            # back the pairing saved click by click (11-15 盲测, 261 号: the one
            # without a chip always ended up as the wave's last person).
            return int(row.event.timeline_timestamp_ms)  # every row of a wave has its chip

        fit = None
        if len(counting) > len(free) and free:
            # 11-14 (古浪 6 号): the one without a chip is where the clicks shift
            # onto the next chip's time, not the last person of the wave.
            free.sort(key=chip_ms)
            fit = fit_wave([crossing_order(i)[0] for i in counting], [chip_ms(row) for row in free])
        if fit is not None:
            chosen = dict(zip(counting, fit.chip_of_click))
            for i in ordered:
                if marks[i].get("no_chip"):
                    plan.no_chip.append(i)
                    plan.labels[i] = "无芯片"
                elif chosen[i] is not None:
                    plan.assigned.append((i, free[chosen[i]]))
                    plan.labels[i] = free[chosen[i]].bib
                else:
                    plan.extra.append(i)
                    plan.labels[i] = f"多 {len(plan.extra)}"
            for extra in fit.extras:
                if not extra.sure:
                    plan.near[counting[extra.click]] = tuple(counting[c] for c in extra.candidates)
            ordered, free = [], []
        for i in ordered:
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

    def record_viewed_frame(self, frame, recorder_ms):
        """Remember, in chip time, what of the recording the operator has looked at.

        Gives that chip time back (None when the frame has none) for the picture to show.
        """
        super().record_viewed_frame(frame, recorder_ms)
        self.timeline.current = int(recorder_ms)
        crossing = recorder_crossing_time(recorder_ms, frame, self.workspace_dir)
        if crossing is None or self.workspace_dir is None:
            return None
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
        if self._run_to is not None:
            if abs(stamp - self._run_to) > RUN_STALE_MS:
                self._run_to = None  # the operator went elsewhere: nothing to stop
                # ... nor a wave to open at its end (11-15: 63 号 Space opened 23 号, the
                # row after a wave left mid-sweep minutes before).
                self._run_between = self._run_counted = None
                self._run_slow()
                self.run_requested.emit(False)  # never left playing with nowhere to stop
            elif stamp >= self._run_to - RUN_STOP_LEAD_MS:
                tail, self._run_to = self._run_tail, None
                self._run_slow()
                self.run_requested.emit(False)
                QTimer.singleShot(0, lambda: self._run_arrived(tail))  # rule outside the frame callback
            elif stamp >= self._run_to - RUN_SLOW_MS:
                self._run_slow()  # the last stretch at normal speed, to stop on the frame
        return stamp

    def _run_target(self, plan, now, ahead=None):
        """(moment, tail) the recording plays on to from NOW, in chip time.

        () when it stays where it is, None when there is nothing left to play.

        The next person: a chip of this wave nobody is counted for, or someone
        the analysis saw cross before the wave's tail ends.  The tail: nobody
        left, so on to RUN_TAIL_MS after the last chip.
        """
        stamps = [int(row.event.timeline_timestamp_ms) for row in plan.chips]
        if not stamps:
            return None
        offset, crossing_of = self._run_offset(plan)
        end = max(stamps) + offset + RUN_TAIL_MS
        due = sorted(int(row.event.timeline_timestamp_ms) + offset for row in plan.unmatched if row.decision is None)
        if due and due[0] <= now + RUN_NEAR_MS:
            # The next chip in order is on this picture, or (clicked out of order)
            # behind it: either way nothing is to be played past.
            return ()
        limit = due[0] if due else end
        seen = []
        if self.workspace_dir:
            try:
                people = people_on_line(self.workspace_dir, int(self.race_strip.camera or 1))
            except (OSError, ValueError, KeyError):
                people = []
            counted = list(crossing_of.values())
            seen = [person.chip_ms for person in people
                    if person.aligned and now + RUN_NEAR_MS < person.chip_ms < limit
                    and not any(abs(person.chip_ms - value) <= RUN_NEAR_MS for value in counted)]
        # Whatever comes onto the line before then, chip or not, seen by the
        # analysis or not: the strip shows it (None: no strip of this stretch).
        arrivals = None
        pieces = self.race_strip.view.pieces
        if pieces and limit > now + RUN_NEAR_MS:
            arrivals = line_arrivals(pieces, self.race_strip.view.cache, now + RUN_NEAR_MS, limit)
        self._run_swept = arrivals is not None
        seen += [int(moment) for moment in arrivals or ()]
        ahead = ahead or self._next_counted(now, plan)
        if ahead is not None and ahead[0] < limit:
            seen.append(int(ahead[0]))  # someone already clicked comes first: stop on them
        if seen or due:
            return min(seen + due[:1]), False
        return (end, True) if end > now + RUN_NEAR_MS else None

    def _compute_run_offset(self, plan):
        crossing_of = {}
        if self.workspace_dir:
            for index, mark in enumerate(plan.marks):
                crossing = recorder_crossing_time(mark["recorder_ms"], mark.get("frame") or {}, self.workspace_dir)
                if crossing is not None:
                    crossing_of[index] = crossing.value_ms
        offsets = sorted(crossing_of[index] - int(row.event.timeline_timestamp_ms)
                         for index, row in plan.assigned if index in crossing_of)
        return (offsets[len(offsets) // 2] if offsets else 0), crossing_of

    def _next_on_line(self, now):
        """When someone new comes onto the line within CLOSE_BEHIND_MS (inf: nobody, None: no strip)."""
        pieces = self.race_strip.view.pieces
        if not pieces:
            return None
        return line_next_entry(pieces, self.race_strip.view.cache, now + SAME_BODY_MS, now + CLOSE_BEHIND_MS)

    def _sweep_on(self, now):
        """Space with no wave to count here: fast on to the next chip, or to what comes onto the line.

        The long empty stretches are between waves (11-14); played at normal
        speed they take as long as they did in the race, and a jump to the next
        chip would pass someone without one.  False where the strip does not
        cover the stretch: Space then plays as it always did.
        """
        pieces = self.race_strip.view.pieces
        if not pieces:
            return False
        ahead = [int(row.event.timeline_timestamp_ms) for row in self._arrival_rows
                 if row.event is not None and int(row.event.timeline_timestamp_ms) > now + RUN_NEAR_MS]
        limit = min([now + SWEEP_REACH_MS] + ahead)
        arrivals = line_arrivals(pieces, self.race_strip.view.cache, now + RUN_NEAR_MS, limit)
        if arrivals is None:
            return False
        target = int(min(arrivals + [limit]))
        self._run_to, self._run_tail = target, False
        self._run_fast = target - now >= RUN_FAST_MIN_MS
        self.hint.setText(("快扫到线上有人影的地方" if arrivals else "这一段线上没有人影，快扫到下一个芯片"
                           if limit in ahead else "这一段线上没有人影，快扫过去；再按空格继续")
                          + "。看到有人压线就按空格停下再点他。")
        self.run_speed.emit(RUN_FAST_SPEED if self._run_fast else 1.0)
        self.run_requested.emit(True)
        return True

    TARGET_ROW_STYLE = "QTableWidget::item:selected {background:#ffd84d; color:#111827;}"
    DONE_ROW_STYLE = "QTableWidget::item:selected {background:#7fdca4; color:#0b3d2a;}"
    PLAIN_ROW_STYLE = "QTableWidget::item:selected {background:#d6eeea; color:#123d36;}"

    def _sweep_between(self, now, item, there=False):
        """From a wave counted even to the next row: played fast, stopping where something comes onto the line.

        The wave used to open the next one by itself, over seconds nobody had
        looked at (11-14, 古浪 11:49:14 to 11:49:21: "感觉不是按照录像的那个顺序
        过去的，直接就跳过去的").  Where the strip does not cover the stretch
        the next row opens as it did, and the hint says so.
        """
        following = self._following_row(item)
        moment = None
        if following is not None:
            moment = (following.time_ms if following.time_ms is not None
                      else int(following.event.timeline_timestamp_ms) if following.event is not None else None)
        pieces = self.race_strip.view.pieces
        arrivals = None
        if not there and moment is not None and moment > now + RUN_NEAR_MS and pieces:
            limit = min(int(moment), now + SWEEP_REACH_MS)
            arrivals = line_arrivals(pieces, self.race_strip.view.cache, now + RUN_NEAR_MS, limit)
        if arrivals is None:
            jumped = not there and moment is not None and moment > now + RUN_FAST_MIN_MS
            self._next_wave(item)
            self.hint.setText("上一波人数与芯片对上，已到下一处。"
                              + ("中间这一段没有过线图，是直接跳过来的。" if jumped else "")
                              + ("后面没有要看的了。" if "后面没有" in self.hint.text() else ""))
            return
        target = int(min(arrivals + [limit]))
        self._run_between = (item, "figure" if arrivals else "there" if limit == int(moment) else "hop")
        self._run_to, self._run_tail = target, False
        self._run_fast = target - now >= RUN_FAST_MIN_MS
        self.hint.setText("上一波人数与芯片对上，快扫到下一波（线上一有人影就停）；回车直接跳到下一波，空格停。")
        self.run_speed.emit(RUN_FAST_SPEED if self._run_fast else 1.0)
        self.run_requested.emit(True)

    def picture_candidate(self, frame):
        """Which picture a click on the frame counts a person on.

        Returns (picture, refusal): picture None means a new one is needed.  Inside
        a wave every click goes onto the wave's one picture, however long the
        wave lasts, and only on the camera it was started on.
        """
        row = self._selected_arrival()
        if row is not None and row.note == "已排除":
            return None, "这条芯片已在本机排除，请先按 4 撤回或选择其他记录。"
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
            head = "多出一人 · 待处理"
        elif index in plan.no_chip:
            head = "这一位无芯片"
        elif label and not is_excluded(plan.marks[index]):
            head = f"已点：按芯片顺序记为 {label} 号"
        else:
            head = "已点这一位"
        near = self._near_counted(plan, index)
        if near:
            head += f"；{near}，若是同一人请右键取消它"
        return head

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
        """Person markers: (x, y, label, manual, alert, dot=False, ghost, auto).

        Bindings are visible without selecting their chip row. Nearby-frame
        ghosts are faded and unlabelled: these are positions, not tracking.
        AUTO describes the source, never an operator's identity confirmation.
        """
        if frame is None:
            return []
        current, camera = int(frame["recorder_ms"]), int(frame.get("camera_index") or 1)
        bibs = {row.key: row.bib for row in self._arrival_rows}
        # 11-16: rings only, the number while the mouse is on one (it is in the corner
        # badge and the cells above); the one selected is ringed twice.
        chosen = self._selected_key
        marks_now = self.current_marks()
        editing = marks_now[self._editing_mark] if 0 <= self._editing_mark < len(marks_now) else None
        # 自动对上: grey-blue dashed with the bib on the frame the person was seen
        # crossing on, faded on the frames just around it.
        automatic = []
        for key, person in self._auto_labels.items():
            if person.camera != camera:
                continue
            here = mark_on_frame({"recorder_ms": person.recorder_ms, "frame": person.frame}, frame, current)
            if here or abs(current - person.recorder_ms) <= GHOST_REACH_MS:
                automatic.append((person.x, person.y, bibs.get(key, ""), False, False, False, not here, True,
                                  True, key == chosen))
        # Saved bindings also remain visible when another table row is selected.
        for row in self._arrival_rows:
            decision = row.decision or {}
            saved = decision.get("frame") or {}
            if (saved.get("marker_x_normalized") is None or saved.get("marker_y_normalized") is None
                    or saved.get("recorder_ms") is None or int(saved.get("camera_index") or 1) != camera):
                continue
            if mark_on_frame({"recorder_ms": saved["recorder_ms"], "frame": saved}, frame, current):
                auto = bool(decision.get("auto_label"))
                automatic.append((float(saved["marker_x_normalized"]), float(saved["marker_y_normalized"]),
                                  row.bib, not auto, False, False, False, auto, True, row.key == chosen))
        item = self.current_item()
        if item is None:
            return automatic
        shown, ghosts = [], []
        for owner, mark in self.picture_marks():
            if owner == item.item_id:
                label = self.picture_mark_label(mark)
                reason = mark.get("exclusion_reason", "").strip() if is_excluded(mark) else ""
                label += f" 已排除：{reason}" if reason and reason != label else ""
                alert = bool(self.picture_mark_warning(mark))
            else:
                label, alert = "无芯片", True
            auto = bool(mark.get("auto_label"))
            picked = mark is editing
            if mark_on_frame(mark, frame, current):
                shown.append((float(mark["x"]), float(mark["y"]), label, not alert and not auto, alert, False, False, auto,
                              True, picked))
            elif (int((mark.get("frame") or {}).get("camera_index") or camera) == camera
                  and 0 < current - int(mark["recorder_ms"]) <= GHOST_REACH_MS):
                ghosts.append((float(mark["x"]), float(mark["y"]), label, not alert and not auto, alert, False, True, auto,
                               True, False))
        # A saved binding may also be a current picture mark; draw it only once
        # (selected when either says so).
        unique = {}
        for overlay in automatic + ghosts + shown:
            spot = (round(overlay[0], 4), round(overlay[1], 4), overlay[2], overlay[6])
            if spot in unique and unique[spot][9]:
                overlay = overlay[:9] + (True,)
            unique[spot] = overlay
        return list(unique.values())

    def _configure_sampling(self):
        self.sampling_preparing.emit()
        super()._configure_sampling()
        self.sampling_configured.emit()

    def _confirm_selected_arrival(self):
        row = self._selected_arrival()
        if row is not None and row.note == "已排除":
            self.hint.setText("这条芯片已在本机排除，按 4 撤回后再处理。")
            return
        if row is not None and row.key in self._auto_labels:
            self._accept_auto_wave(row)
            return
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
        if row is not None and row.key in self._auto_labels:
            self._accept_auto_wave(row)
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

    def _usable(self, item=None, *, action=""):
        item = item or self._candidate_item()
        if item is None or self._arrival_store is None:
            return False
        waiting_exclusion = action == "not_rider" and self._unverified_picture(item)
        if ((item.status == STATUS_WAITING and not waiting_exclusion)
                or item.evidence.get("configuration_stale") or item.evidence.get("superseded")):
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
        if not self._usable(item, action=action):
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
        """Whether an undecided picture without a chip can be excluded with 1."""
        item = row.item
        state = self._row_state(row)
        status_allows = state == "画面待核" or item.status != STATUS_WAITING
        time_allows = state == "画面待核" or row.time_ms is not None
        return (row.event is None and row.mark is None and not row.decision
                and time_allows and state in ("待定", "画面待核") and bool(item.evidence.get("frame"))
                and status_allows and not item.evidence.get("configuration_stale")
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

    def _confirm_arrival(self, row):
        if row.note == "已排除":
            self.hint.setText("这条芯片已在本机排除，按 4 撤回后再处理。")
            return
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

    def add_mark(self, x, y, recorder_ms, frame=None, *, save=True):
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
        self.stop_run()  # a click while it plays counts on the frame shown, and holds it
        before = len(self.current_marks())
        super().add_mark(x, y, recorder_ms, frame)
        plan = self._wave_plan()
        if save and plan is not None and len(plan.marks) > before:
            stored = self._save_wave(plan)
            self.show_counted(self._editing_mark)
            self.marks_changed.emit(tuple(self.current_marks()))
            self.hint.setText(self.picture_mark_hint())
            if stored:
                crossing = recorder_crossing_time(recorder_ms, ref, self.workspace_dir) if self.workspace_dir else None
                self._run_from = crossing.value_ms if crossing is not None else int(recorder_ms)
                self._clicked_at = self._run_from
                QTimer.singleShot(0, self._run_on)  # once the window has drawn the click

    def _save_wave(self, plan, *, operation_id=None):
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
        operation_id = operation_id or getattr(self, "_shortcut_operation", None)
        self.confirmation_preparing.emit()
        try:
            category = self._category(item)
            if wanted and (category is None or category.get("action") != "bicycle"):
                picture = dict(item.evidence.get("frame") or {})
                picture.setdefault("calibration_version", item.calibration_version)
                self._arrival_store.save(scope, "candidate:" + item.item_id, binding, "bicycle",
                                         candidate_id=item.item_id, frame=picture, operation_id=operation_id,
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
                    operation_id=operation_id,
                    auto_label=bool(mark.get("auto_label")),
                    review_method="auto_label_operator_accepted" if mark.get("auto_label") else "manual_click",
                    calibration_version=item.calibration_version,
                )
        except (OSError, RuntimeError, ValueError) as error:
            rollback_error = self._rollback_wave_operation(scope, operation_id) if operation_id else ""
            self._quick_render_signature = None
            self._render()
            self.hint.setText(f"过线未保存：{error}{rollback_error}")
            return False
        rollback_error = (self._rollback_wave_operation(scope, operation_id)
                          if changed and operation_id else "")
        self._quick_render_signature = None
        self._render()
        if changed:
            self.hint.setText(f"芯片记录已变化（{'、'.join(changed)}），请重新核对本波。{rollback_error}")
            return False
        return True

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

    def _row_by_key(self, key):
        return next((row for row in self._arrival_rows if row.key == key), None)

    def _carrier_text(self, row):
        """What a 双芯片 row says: whose second chip it is."""
        return (f"{row.bib} 号芯片由 {row.carrier} 号携带（一人双芯片）" if row.carrier
                else f"{row.bib} 号记为双芯片（多余芯片）")

    def _exclude_local_chip(self, key):
        """Exclude just this chip locally, without inventing a video crossing."""
        row = self._row_by_key(key)
        if (row is None or row.event is None or not row.event.is_active or self._arrival_store is None
                or row.decision is not None or self._counted_person(row) is not None or row.note == "多余芯片"):
            self.hint.setText("已有点人或确认记录，请先按 4 撤销，再做本机排除。")
            return
        scope, binding = self._arrival_scope, event_binding(row.event)
        store = self._arrival_store
        if self._render() is False:
            return
        row = self._row_by_key(key)
        if (self._arrival_scope != scope or self._arrival_store is not store or row is None or row.event is None
                or not row.event.is_active or event_binding(row.event) != binding or row.decision is not None
                or self._counted_person(row) is not None or row.note == "多余芯片"):
            self.hint.setText("这条芯片或处理状态已变化，未排除，请重新核对。")
            return
        try:
            store.save(scope, key, binding, "held", reason=LOCAL_CHIP_EXCLUSION,
                       exclusion_reason="非运动员（本机排除）", event_id=row.event.event_id, bib=row.bib,
                       operation_id=uuid.uuid4().hex)
        except (OSError, RuntimeError, ValueError) as error:
            self.hint.setText(f"本机排除未保存：{error}")
            return
        self._quick_render_signature = None
        if self._render() is False:
            self._last_saved_key = key
            return
        if self._row_by_key(key) is not None and self._row_by_key(key).note == "已排除":
            position = next(i for i, value in enumerate(self._arrival_rows) if value.key == key)
            self._select_next_visible(position)
            self._last_saved_key = key
            self.hint.setText(f"{row.bib} 号已在本机排除 · 原始记录保留 · 4 撤销")

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
                         and other.bib == bib and other.note not in ("多余芯片", "已排除")
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
        # Automatic advance may land on a previously counted rider. Undo the
        # exclusion just made before considering that rider's own saved marks.
        last = (self._arrival_store.latest(self._arrival_scope, self._last_saved_key)
                if self._arrival_store is not None and self._last_saved_key is not None else None)
        if last is not None and (last["action"] == "not_rider" or last.get("reason") == LOCAL_CHIP_EXCLUSION):
            self._undo_key(self._last_saved_key)
            return
        plan = self._wave_plan()
        row = self._selected_arrival()
        if row is not None and row.note == "已排除":
            self._undo_key(row.key)
            return
        if row is not None and row.key in self._auto_labels:
            self._withdraw_auto_wave(row)
            return
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
            latest = self._arrival_store.latest(self._arrival_scope, target_key) if target_key is not None else None
            operation = (latest.get("operation_id") if latest is not None
                         and latest.get("reason") == LOCAL_CHIP_EXCLUSION else None)
            if operation:
                previous = self._arrival_store.undo(self._arrival_scope, operation_id=operation)
            else:
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
                  else "本机排除" if previous.get("reason") == LOCAL_CHIP_EXCLUSION
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
        elif selected is not None and self._row_state(selected) == "已排除":
            self._select_next_visible(self.table.currentRow())

    def _queue_key(self, event):
        if event.isAutoRepeat():
            return event.key() in NUMBER_KEYS + (Qt.Key_Backspace,)
        if self._auto_review is not None and event.key() not in (Qt.Key_Shift, Qt.Key_Control, Qt.Key_Alt):
            self._stop_auto_review("已停止自动补看，这一行未排除。")
            return True
        if event.key() not in (Qt.Key_Shift, Qt.Key_Control, Qt.Key_Alt, Qt.Key_Meta):
            self.stop_run()  # the key then does what it always does, on the frame held
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
